// Package web 提供只监听本机回环地址的配置界面。
//
// 安全要求见文档第 04 章：会话令牌、跨站请求伪造防护、Host 与 Origin 校验、
// 不开放公网 CORS、不把管理密码写进地址栏或日志。
package web

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"html"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/agent"
	"github.com/sherkevin/open-claude-code/connector/internal/cloud"
	"github.com/sherkevin/open-claude-code/connector/internal/diagnostics"
	"github.com/sherkevin/open-claude-code/connector/internal/huaweiauth"
)

// The tracked Flintworks mark is embedded so the local UI works offline.
//
//go:embed flintworks-mark.png
var flintworksMark []byte

// Server 是本机配置界面。
type Server struct {
	Agent *agent.Agent
	Token string
	Addr  string

	httpServer       *http.Server
	supportPortalURL string
}

// New 创建服务并生成一次性会话令牌。
func New(a *agent.Agent, addr string) (*Server, error) {
	if !isLoopbackListenAddr(addr) {
		return nil, errors.New("本机界面只允许监听回环地址")
	}
	raw := make([]byte, 24)
	if _, err := rand.Read(raw); err != nil {
		return nil, err
	}
	return &Server{
		Agent: a, Token: hex.EncodeToString(raw), Addr: addr,
		supportPortalURL: supportPortalURL(machineSupportPortalSetting()),
	}, nil
}

// The machine administrator supplies a stable landing page, without session credentials.
func supportPortalURL(value string) string {
	if strings.ContainsAny(value, "\\?#") {
		return ""
	}
	portal, err := url.ParseRequestURI(value)
	if err != nil || portal.Scheme != "https" || portal.Hostname() == "" || portal.User != nil {
		return ""
	}
	return portal.String()
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
	if listener == nil || !isLoopbackListenAddr(listener.Addr().String()) {
		if listener != nil {
			_ = listener.Close()
		}
		return errors.New("拒绝在非回环地址提供本机界面")
	}
	server := &http.Server{
		Handler:           s.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    32 << 10,
	}
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
	mux.HandleFunc("/support", s.handleIndex)
	mux.HandleFunc("GET /flintworks-mark.png", s.guard(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "image/png")
		_, _ = w.Write(flintworksMark)
	}))
	mux.HandleFunc("/api/status", s.guard(s.handleStatus))
	mux.HandleFunc("/api/bind", s.guard(postOnly(s.handleBind)))
	mux.HandleFunc("/api/import", s.guard(postOnly(s.handleImport)))
	mux.HandleFunc("/api/source/save", s.guard(postOnly(s.handleSourceSave)))
	mux.HandleFunc("/api/source/save-and-connect", s.guard(postOnly(s.handleSourceSaveAndConnect)))
	mux.HandleFunc("/api/source/tools", s.guard(postOnly(s.handleSourceTools)))
	mux.HandleFunc("/api/source/enabled", s.guard(postOnly(s.handleSourceEnabled)))
	mux.HandleFunc("/api/builtin-demo/enabled", s.guard(postOnly(s.handleBuiltinDemoEnabled)))
	mux.HandleFunc("/api/self-test", s.guard(postOnly(s.handleSelfTest)))
	mux.HandleFunc("/api/auth-profile", s.guard(postOnly(s.handleAuthProfile)))
	mux.HandleFunc("/api/diagnose", s.guard(postOnly(s.handleDiagnose)))
	mux.HandleFunc("/api/connect", s.guard(postOnly(s.handleConnect)))
	mux.HandleFunc("/api/pause", s.guard(postOnly(s.handlePause)))
	mux.HandleFunc("/api/disconnect", s.guard(postOnly(s.handleDisconnect)))
	mux.HandleFunc("/api/revoke", s.guard(postOnly(s.handleRevoke)))
	mux.HandleFunc("/api/export", s.guard(s.handleExport))
	mux.HandleFunc("/api/platform-handoff", s.guard(postOnly(s.handlePlatformHandoff)))
	mux.HandleFunc("/api/mcp-export", s.guard(postOnly(s.handleMCPExport)))
	mux.HandleFunc("GET /api/assist/offer", s.guard(s.handleAssistOffer))
	mux.HandleFunc("/api/assist/start", s.guard(postOnly(s.handleAssistStart)))
	mux.HandleFunc("/api/assist/target", s.guard(postOnly(s.handleAssistTarget)))
	mux.HandleFunc("/api/assist/stop", s.guard(postOnly(s.handleAssistStop)))
	mux.HandleFunc("/api/logs", s.guard(s.handleLogs))
	mux.HandleFunc("/api/logs/download", s.guard(s.handleLogsDownload))
	mux.HandleFunc("/api/platform-logs", s.guard(s.handlePlatformLogs))
	mux.HandleFunc("/api/diagnostics/settings", s.guard(postOnly(s.handleDiagnosticsSettings)))
	return mux
}

func (s *Server) handleAssistOffer(w http.ResponseWriter, r *http.Request) {
	if !tokenMatches(r.Header.Get("X-Connector-Token"), s.Token) {
		writeError(w, http.StatusForbidden, "TOKEN_REQUIRED", "缺少本机会话令牌，请刷新页面后重试")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 7*time.Second)
	defer cancel()
	status, err := s.Agent.AssistStatus(ctx)
	if err != nil {
		writeError(w, http.StatusBadGateway, "ASSIST_UNAVAILABLE", "暂时无法查询协助状态")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "data": status})
}

func (s *Server) handleAssistStart(w http.ResponseWriter, r *http.Request) {
	var body struct {
		SessionID string `json:"session_id"`
		cloud.AssistTarget
	}
	if err := decode(w, r, &body); err != nil {
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	if body.SessionID != "" {
		if body.Name != "" || body.URL != "" {
			writeError(w, http.StatusBadRequest, "ASSIST_TARGET_INVALID", "协助请求包含多个目标")
			return
		}
		if err := s.Agent.StartAssist(ctx, body.SessionID); err != nil {
			writeAPIError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
		return
	}
	var offer *cloud.AssistOffer
	var err error
	if body.AssistTarget == (cloud.AssistTarget{}) {
		offer, err = s.Agent.OpenAssist(ctx)
	} else {
		offer, err = s.Agent.StartAssistForTarget(ctx, body.AssistTarget)
	}
	if err != nil {
		writeAPIError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "data": offer})
}

func (s *Server) handleAssistTarget(w http.ResponseWriter, r *http.Request) {
	var body struct {
		SessionID string `json:"session_id"`
		cloud.AssistTarget
	}
	if err := decode(w, r, &body); err != nil {
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	offer, err := s.Agent.SetAssistTarget(ctx, body.SessionID, body.AssistTarget)
	if err != nil {
		writeAPIError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "data": offer})
}

func (s *Server) handleAssistStop(w http.ResponseWriter, r *http.Request) {
	var body struct {
		SessionID string `json:"session_id"`
	}
	if err := decode(w, r, &body); err != nil {
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	if err := s.Agent.StopAssist(ctx, body.SessionID); err != nil {
		writeAPIError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (s *Server) guard(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		setSecurityHeaders(w)
		if !isLoopbackHost(r.Host) {
			writeError(w, http.StatusForbidden, "HOST_NOT_ALLOWED", "只允许通过本机地址访问")
			return
		}
		if origin := r.Header.Get("Origin"); origin != "" && !originAllowed(origin, r.Host) {
			writeError(w, http.StatusForbidden, "ORIGIN_NOT_ALLOWED", "来源站点不受信任")
			return
		}
		if r.Method == http.MethodPost && !tokenMatches(r.Header.Get("X-Connector-Token"), s.Token) {
			writeError(w, http.StatusForbidden, "TOKEN_REQUIRED", "缺少本机会话令牌，请刷新页面后重试")
			return
		}
		w.Header().Set("Cache-Control", "no-store")
		next(w, r)
	}
}

func (s *Server) handleIndex(w http.ResponseWriter, r *http.Request) {
	var template string
	switch r.URL.Path {
	case "/":
		template = onboardingPage
	case "/support":
		template = page
	default:
		http.NotFound(w, r)
		return
	}
	if !isLoopbackHost(r.Host) {
		http.Error(w, "只允许本机访问", http.StatusForbidden)
		return
	}
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		w.Header().Set("Allow", "GET, HEAD")
		http.Error(w, "只支持 GET 请求", http.StatusMethodNotAllowed)
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
	setSecurityHeaders(w)
	template = strings.Replace(template, "__CONNECTOR_TOKEN__", s.Token, 1)
	if r.URL.Path == "/" {
		content := `<p class="muted">开物尚未配置远程协助入口。</p>`
		if s.supportPortalURL != "" {
			content = `<p class="muted">在远程支持工具中允许工程师操作命令行，需要停止时，在同一工具中结束会话。</p>
    <a class="button-link" target="_blank" rel="noopener noreferrer" href="` + html.EscapeString(s.supportPortalURL) + `">打开远程协助</a>`
		}
		template = strings.Replace(template, "__ATTENDED_SUPPORT_ENTRY__", content, 1)
	}
	io.WriteString(w, template)
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", http.MethodGet)
		writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "只支持 GET 请求")
		return
	}
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

func (s *Server) handleSourceSave(w http.ResponseWriter, r *http.Request) {
	var body agent.SourceInput
	if err := decode(w, r, &body); err != nil {
		return
	}
	result, err := s.Agent.SaveSource(body)
	if err != nil {
		writeAPIError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "data": result})
}

func (s *Server) handleSourceSaveAndConnect(w http.ResponseWriter, r *http.Request) {
	var body agent.SourceInput
	if err := decode(w, r, &body); err != nil {
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 120*time.Second)
	defer cancel()
	result, err := s.Agent.SaveSourceAndConnect(ctx, body)
	if err != nil {
		writeAPIError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "data": result})
}

func (s *Server) handleSourceTools(w http.ResponseWriter, r *http.Request) {
	var body agent.SourceInput
	if err := decode(w, r, &body); err != nil {
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	tools, err := s.Agent.DiscoverSourceTools(ctx, body)
	if err != nil {
		var upstream interface{ HTTPStatusCode() int }
		if errors.As(err, &upstream) && upstream.HTTPStatusCode() == http.StatusUnauthorized {
			writeError(w, http.StatusBadRequest, "UPSTREAM_UNAUTHORIZED", err.Error()+"。请核对这个 MCP 服务的访问令牌，再点「读取工具」。")
			return
		}
		writeAPIError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "data": tools})
}

func (s *Server) handleSourceEnabled(w http.ResponseWriter, r *http.Request) {
	var body struct {
		SourceID string `json:"source_id"`
		Enabled  *bool  `json:"enabled"`
	}
	if err := decode(w, r, &body); err != nil {
		return
	}
	if body.Enabled == nil {
		writeError(w, http.StatusBadRequest, "BODY_INVALID", "必须明确指定启用或停用")
		return
	}
	result, err := s.Agent.SetSourceEnabled(body.SourceID, *body.Enabled)
	if err != nil {
		writeAPIError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "data": result})
}

func (s *Server) handleBuiltinDemoEnabled(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Enabled *bool `json:"enabled"`
	}
	if err := decode(w, r, &body); err != nil {
		return
	}
	if body.Enabled == nil {
		writeError(w, http.StatusBadRequest, "BODY_INVALID", "必须明确指定启用或停用")
		return
	}
	status, err := s.Agent.SetBuiltinDemoEnabled(*body.Enabled)
	if err != nil {
		writeAPIError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "data": status})
}

func (s *Server) handleSelfTest(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 120*time.Second)
	defer cancel()
	result := s.Agent.SelfTest(ctx)
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "data": result})
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

func (s *Server) handleAuthProfile(w http.ResponseWriter, r *http.Request) {
	var body struct {
		SourceID string `json:"source_id"`
		AppID    string `json:"app_id"`
		Channel  string `json:"channel"`
		UserID   string `json:"user_id"`
		HMACKey  string `json:"hmac_key"`
		Tools    string `json:"tools"`
	}
	if err := decode(w, r, &body); err != nil {
		return
	}
	tools := strings.FieldsFunc(body.Tools, func(r rune) bool {
		return r == ',' || r == '\n' || r == '\r' || r == '\t'
	})
	status, err := s.Agent.SetAuthProfile(body.SourceID, huaweiauth.Profile{
		AppID:   strings.TrimSpace(body.AppID),
		Channel: strings.TrimSpace(body.Channel),
		UserID:  strings.TrimSpace(body.UserID),
		HMACKey: body.HMACKey,
	}, tools)
	if err != nil {
		writeAPIError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "data": status})
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
	status, err := s.Agent.Pause(ctx)
	if err != nil {
		writeAPIError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "data": status})
}

func (s *Server) handleDisconnect(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 20*time.Second)
	defer cancel()
	status, err := s.Agent.Disconnect(ctx)
	if err != nil {
		writeAPIError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "data": status})
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
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", http.MethodGet)
		writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "只支持 GET 请求")
		return
	}
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

func (s *Server) handlePlatformHandoff(w http.ResponseWriter, r *http.Request) {
	var body struct {
		SourceID string `json:"source_id"`
	}
	if err := decode(w, r, &body); err != nil {
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()
	link, err := s.Agent.CreatePlatformHandoff(ctx, body.SourceID)
	if err != nil {
		writeAPIError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "data": map[string]string{"url": link}})
}

func (s *Server) handleMCPExport(w http.ResponseWriter, r *http.Request) {
	var body struct {
		SourceID string `json:"source_id"`
	}
	if err := decode(w, r, &body); err != nil {
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()
	result, err := s.Agent.ExportPlatformMCP(ctx, body.SourceID)
	if err != nil {
		writeMCPExportError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "data": result})
}

func (s *Server) handleLogs(w http.ResponseWriter, r *http.Request) {
	query, ok := localLogQuery(w, r)
	if !ok {
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true,
		"data": map[string]any{
			"events": s.Agent.DiagnosticLogs(query),
			"stats":  s.Agent.DiagnosticStats(),
		},
	})
}

func (s *Server) handlePlatformLogs(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", http.MethodGet)
		writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "只支持 GET 请求")
		return
	}
	level, ok := localLevel(w, r)
	if !ok {
		return
	}
	limit, ok := localLimit(w, r, 200)
	if !ok {
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	events, errorCode := s.Agent.PlatformDiagnosticLogs(ctx, limit)
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true,
		"data": map[string]any{
			"events":     filterPlatformLevel(events, level),
			"available":  errorCode == "",
			"error_code": errorCode,
			"stats":      s.Agent.DiagnosticStats(),
		},
	})
}

func (s *Server) handleDiagnosticsSettings(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Paused *bool `json:"paused"`
	}
	if err := decode(w, r, &body); err != nil {
		return
	}
	if body.Paused == nil {
		writeError(w, http.StatusBadRequest, "BODY_INVALID", "必须明确指定暂停或恢复诊断上报")
		return
	}
	stats := s.Agent.SetDiagnosticsPaused(*body.Paused)
	if stats.StorageErrorCode != "" {
		writeError(w, http.StatusServiceUnavailable, stats.StorageErrorCode, "本机诊断设置暂时无法保存，请稍后重试")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "data": stats})
}

func (s *Server) handleLogsDownload(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", http.MethodGet)
		writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "只支持 GET 请求")
		return
	}
	level, ok := localLevel(w, r)
	if !ok {
		return
	}
	connectorEvents := filterConnectorLevel(s.Agent.DiagnosticLogs(diagnostics.Query{Limit: diagnostics.DefaultMaxLogEvents}), level)
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	platformEvents, platformError := s.Agent.PlatformDiagnosticLogs(ctx, 200)
	platformEvents = filterPlatformLevel(platformEvents, level)
	payload := map[string]any{
		"generated_at":        time.Now().UTC().Format(time.RFC3339),
		"connector_events":    connectorEvents,
		"platform_events":     platformEvents,
		"platform_available":  platformError == "",
		"platform_error_code": platformError,
		"diagnostics":         s.Agent.DiagnosticStats(),
	}
	raw, err := json.MarshalIndent(payload, "", "  ")
	if err != nil {
		writeError(w, http.StatusInternalServerError, "EXPORT_FAILED", "生成脱敏日志失败")
		return
	}
	filename := fmt.Sprintf("kaiwu-diagnostics-%s.json", time.Now().Format("20060102-150405"))
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Content-Disposition", `attachment; filename="`+filename+`"`)
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(raw)
}

func localLogQuery(w http.ResponseWriter, r *http.Request) (diagnostics.Query, bool) {
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", http.MethodGet)
		writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "只支持 GET 请求")
		return diagnostics.Query{}, false
	}
	level, ok := localLevel(w, r)
	if !ok {
		return diagnostics.Query{}, false
	}
	limit, ok := localLimit(w, r, 200)
	if !ok {
		return diagnostics.Query{}, false
	}
	component := diagnostics.Component(strings.TrimSpace(r.URL.Query().Get("component")))
	if component != "" {
		valid := false
		for _, allowed := range diagnostics.SortedComponents() {
			if string(component) == allowed {
				valid = true
				break
			}
		}
		if !valid {
			writeError(w, http.StatusBadRequest, "FILTER_INVALID", "日志组件筛选无效")
			return diagnostics.Query{}, false
		}
	}
	return diagnostics.Query{
		Level:     level,
		Component: component,
		SourceID:  strings.TrimSpace(r.URL.Query().Get("source_id")),
		TraceID:   strings.TrimSpace(r.URL.Query().Get("trace_id")),
		Limit:     limit,
	}, true
}

func localLevel(w http.ResponseWriter, r *http.Request) (diagnostics.Level, bool) {
	raw := strings.TrimSpace(r.URL.Query().Get("level"))
	switch diagnostics.Level(raw) {
	case "":
		return "", true
	case diagnostics.LevelInfo, diagnostics.LevelWarn, diagnostics.LevelError:
		return diagnostics.Level(raw), true
	default:
		writeError(w, http.StatusBadRequest, "FILTER_INVALID", "日志级别筛选无效")
		return "", false
	}
}

func localLimit(w http.ResponseWriter, r *http.Request, fallback int) (int, bool) {
	raw := strings.TrimSpace(r.URL.Query().Get("limit"))
	if raw == "" {
		return fallback, true
	}
	value, err := strconv.Atoi(raw)
	if err != nil || value <= 0 || value > 1000 {
		writeError(w, http.StatusBadRequest, "FILTER_INVALID", "日志数量必须在 1 到 1000 之间")
		return 0, false
	}
	return value, true
}

func filterConnectorLevel(events []diagnostics.View, level diagnostics.Level) []diagnostics.View {
	if level == "" {
		return events
	}
	out := make([]diagnostics.View, 0, len(events))
	for _, event := range events {
		if event.Level == level {
			out = append(out, event)
		}
	}
	return out
}

func filterPlatformLevel(events []diagnostics.PlatformView, level diagnostics.Level) []diagnostics.PlatformView {
	if level == "" {
		return events
	}
	out := make([]diagnostics.PlatformView, 0, len(events))
	for _, event := range events {
		if event.Level == level {
			out = append(out, event)
		}
	}
	return out
}

func decode(w http.ResponseWriter, r *http.Request, target any) error {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
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

func postOnly(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			w.Header().Set("Allow", http.MethodPost)
			writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "只支持 POST 请求")
			return
		}
		next(w, r)
	}
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

// Only forward a known, fixed export error. Upstream messages can contain
// private data and must not become customer-facing instructions.
func writeMCPExportError(w http.ResponseWriter, err error) {
	var cloudErr *cloud.APIError
	if errors.As(err, &cloudErr) && cloudErr.Code == "DEVICE_EXPORT_DENIED" && cloudErr.Status == http.StatusForbidden {
		writeError(w, http.StatusForbidden, "DEVICE_EXPORT_DENIED", "业务服务已保存，开物平台尚未开通配置权限。请联系开物技术支持，无需修改地址或重新安装。")
		return
	}
	// The detailed outcome is recorded by ExportPlatformMCP. Do not echo
	// upstream response bodies, URLs or arbitrary error text into this page.
	writeError(w, http.StatusBadRequest, "OPERATION_FAILED", "未能生成配置，请检查连接状态或联系开物技术支持。")
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

func isLoopbackListenAddr(addr string) bool {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return false
	}
	host = strings.Trim(host, "[]")
	if strings.EqualFold(host, "localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func tokenMatches(got, expected string) bool {
	if len(got) != len(expected) || expected == "" {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(got), []byte(expected)) == 1
}

func setSecurityHeaders(w http.ResponseWriter) {
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("X-Frame-Options", "DENY")
	w.Header().Set("Referrer-Policy", "no-referrer")
	w.Header().Set("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
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
