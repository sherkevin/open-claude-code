// Package web 提供只监听本机回环地址的配置界面。
//
// 安全要求见文档第 04 章：会话令牌、跨站请求伪造防护、Host 与 Origin 校验、
// 不开放公网 CORS、不把管理密码写进地址栏或日志。
package web

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"strings"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/agent"
)

// Server 是本机配置界面。
type Server struct {
	Agent *agent.Agent
	Token string
	Addr  string

	httpServer *http.Server
}

// New 创建服务并生成一次性会话令牌。
func New(a *agent.Agent, addr string) (*Server, error) {
	raw := make([]byte, 24)
	if _, err := rand.Read(raw); err != nil {
		return nil, err
	}
	return &Server{Agent: a, Token: hex.EncodeToString(raw), Addr: addr}, nil
}

// ListenAndServe 启动本机界面。
func (s *Server) ListenAndServe() error {
	listener, err := net.Listen("tcp", s.Addr)
	if err != nil {
		return err
	}
	return s.Serve(listener)
}

// Serve 在已创建的监听器上提供本机界面。
func (s *Server) Serve(listener net.Listener) error {
	server := &http.Server{Handler: s.Handler(), ReadHeaderTimeout: 10 * time.Second}
	s.httpServer = server
	err := server.Serve(listener)
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}

// Shutdown 优雅停止本机界面。
func (s *Server) Shutdown(ctx context.Context) error {
	if s.httpServer == nil {
		return nil
	}
	return s.httpServer.Shutdown(ctx)
}

// Handler 返回完整的 HTTP 处理器，便于测试。
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/", s.handleIndex)
	mux.HandleFunc("/api/status", s.guard(s.handleStatus))
	mux.HandleFunc("/api/bind", s.guard(s.handleBind))
	mux.HandleFunc("/api/import", s.guard(s.handleImport))
	mux.HandleFunc("/api/diagnose", s.guard(s.handleDiagnose))
	mux.HandleFunc("/api/connect", s.guard(s.handleConnect))
	mux.HandleFunc("/api/pause", s.guard(s.handlePause))
	mux.HandleFunc("/api/disconnect", s.guard(s.handleDisconnect))
	mux.HandleFunc("/api/revoke", s.guard(s.handleRevoke))
	mux.HandleFunc("/api/export", s.guard(s.handleExport))
	return mux
}

func (s *Server) guard(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !isLoopbackHost(r.Host) {
			writeError(w, http.StatusForbidden, "HOST_NOT_ALLOWED", "只允许通过本机地址访问")
			return
		}
		if origin := r.Header.Get("Origin"); origin != "" && !originAllowed(origin, r.Host) {
			writeError(w, http.StatusForbidden, "ORIGIN_NOT_ALLOWED", "来源站点不受信任")
			return
		}
		if r.Method == http.MethodPost && r.Header.Get("X-Connector-Token") != s.Token {
			writeError(w, http.StatusForbidden, "TOKEN_REQUIRED", "缺少本机会话令牌，请刷新页面后重试")
			return
		}
		w.Header().Set("Cache-Control", "no-store")
		next(w, r)
	}
}

func (s *Server) handleIndex(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	if !isLoopbackHost(r.Host) {
		http.Error(w, "只允许本机访问", http.StatusForbidden)
		return
	}
	if origin := r.Header.Get("Origin"); origin != "" && !originAllowed(origin, r.Host) {
		http.Error(w, "来源站点不受信任", http.StatusForbidden)
		return
	}
	http.SetCookie(w, &http.Cookie{
		Name:     "kconn",
		Value:    s.Token,
		Path:     "/",
		HttpOnly: true,
		SameSite: http.SameSiteStrictMode,
	})
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	io.WriteString(w, strings.Replace(page, "__CONNECTOR_TOKEN__", s.Token, 1))
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "data": s.Agent.Status()})
}

func (s *Server) handleBind(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Code string `json:"code"`
	}
	if err := decode(w, r, &body); err != nil {
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	status, err := s.Agent.Bind(ctx, body.Code)
	if err != nil {
		writeAPIError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "data": status})
}

func (s *Server) handleImport(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Config string `json:"config"`
	}
	if err := decode(w, r, &body); err != nil {
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	report, err := s.Agent.ImportConfig(ctx, []byte(body.Config))
	if err != nil {
		writeAPIError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "data": report})
}

func (s *Server) handleDiagnose(w http.ResponseWriter, r *http.Request) {
	var body struct {
		SourceID  string `json:"source_id"`
		ToolProbe string `json:"tool_probe"`
	}
	if err := decode(w, r, &body); err != nil {
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
	defer cancel()
	reports, err := s.Agent.Diagnose(ctx, body.SourceID, body.ToolProbe)
	if err != nil {
		writeAPIError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "data": reports})
}

func (s *Server) handleConnect(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 90*time.Second)
	defer cancel()
	status, err := s.Agent.Connect(ctx)
	if err != nil {
		writeAPIError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "data": status})
}

func (s *Server) handlePause(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 20*time.Second)
	defer cancel()
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "data": s.Agent.Pause(ctx)})
}

func (s *Server) handleDisconnect(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 20*time.Second)
	defer cancel()
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "data": s.Agent.Disconnect(ctx)})
}

func (s *Server) handleRevoke(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	status, err := s.Agent.Revoke(ctx)
	if err != nil {
		writeAPIError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "data": status})
}

func (s *Server) handleExport(w http.ResponseWriter, r *http.Request) {
	raw, err := s.Agent.Export()
	if err != nil {
		writeAPIError(w, err)
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusOK)
	w.Write(raw)
}

func decode(w http.ResponseWriter, r *http.Request, target any) error {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "只支持 POST 请求")
		return errors.New("method")
	}
	if r.Body == nil {
		writeError(w, http.StatusBadRequest, "BODY_REQUIRED", "请求内容为空")
		return errors.New("body")
	}
	raw, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		writeError(w, http.StatusBadRequest, "BODY_INVALID", "读取请求内容失败")
		return err
	}
	if len(strings.TrimSpace(string(raw))) == 0 {
		writeError(w, http.StatusBadRequest, "BODY_REQUIRED", "请求内容为空")
		return errors.New("body")
	}
	if err := json.Unmarshal(raw, target); err != nil {
		writeError(w, http.StatusBadRequest, "BODY_INVALID", "请求内容不是有效 JSON")
		return err
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

func writeError(w http.ResponseWriter, status int, code, message string) {
	writeJSON(w, status, map[string]any{"ok": false, "error": map[string]string{"code": code, "message": message}})
}

func writeAPIError(w http.ResponseWriter, err error) {
	writeError(w, http.StatusBadRequest, "OPERATION_FAILED", err.Error())
}

func isLoopbackHost(hostport string) bool {
	host := hostport
	if value, _, err := net.SplitHostPort(hostport); err == nil {
		host = value
	}
	host = strings.Trim(host, "[]")
	if strings.EqualFold(host, "localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func originAllowed(origin, host string) bool {
	origin = strings.TrimRight(strings.TrimSpace(origin), "/")
	if origin == "" || origin == "null" {
		return false
	}
	scheme := "http://"
	if strings.HasPrefix(origin, "https://") {
		scheme = "https://"
	}
	hostPart := strings.TrimPrefix(origin, scheme)
	return strings.EqualFold(hostPart, host) && isLoopbackHost(hostPart)
}
