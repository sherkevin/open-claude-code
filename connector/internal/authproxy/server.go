// Package authproxy provides a loopback-only, source-bound MCP proxy that
// rebuilds requests before forwarding them to a fixed customer endpoint.
package authproxy

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"crypto/tls"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime"
	"net"
	"net/http"
	"net/url"
	"path"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/huaweiauth"
)

const (
	defaultMaxRequestBytes  = 4 << 20
	defaultMaxResponseBytes = 8 << 20
	maxSessions             = 256
	sessionTTL              = 30 * time.Minute
)

var traceIDPattern = regexp.MustCompile(`^kwtr_[0-9a-f]{24,64}$`)

// Options describes one fixed source binding.
type Options struct {
	SourceID        string
	UpstreamURL     string
	Headers         map[string]string
	Signing         *huaweiauth.Profile
	ToolAllowlist   []string
	MaxRequestSize  int64
	MaxResponseSize int64
	OnMCPCall       func(Observation)
}

// Observation contains only bounded metadata for one MCP request. It never
// carries request arguments, response content, headers, or upstream errors.
type Observation struct {
	Method       string
	SourceID     string
	ToolName     string
	TraceID      string
	Platform     bool
	StartedAt    time.Time
	HTTPStatus   int
	RPCErrorCode string
	Failed       bool
}

// Server is a source-bound local proxy.
type Server struct {
	opts       Options
	upstream   *url.URL
	httpClient *http.Client
	allowed    map[string]bool

	listener   net.Listener
	httpServer *http.Server

	mu       sync.Mutex
	sessions map[string]session
}

type session struct {
	upstreamID string
	expiresAt  time.Time
}

type rpcEnvelope struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

type mcpOutcome struct {
	failed       bool
	rpcErrorCode string
	responded    bool
}

// New validates and builds a source-bound proxy.
func New(opts Options) (*Server, error) {
	if strings.TrimSpace(opts.SourceID) == "" {
		return nil, errors.New("来源编号不能为空")
	}
	upstream, err := parseUpstream(opts.UpstreamURL)
	if err != nil {
		return nil, err
	}
	if opts.MaxRequestSize <= 0 {
		opts.MaxRequestSize = defaultMaxRequestBytes
	}
	if opts.MaxResponseSize <= 0 {
		opts.MaxResponseSize = defaultMaxResponseBytes
	}
	allowed := map[string]bool{}
	for _, item := range opts.ToolAllowlist {
		item = strings.TrimSpace(item)
		if item == "" || strings.ContainsAny(item, "\r\n") {
			return nil, errors.New("工具允许列表包含无效名称")
		}
		if allowed[item] {
			return nil, fmt.Errorf("工具允许列表重复: %s", item)
		}
		allowed[item] = true
	}
	if opts.Signing != nil {
		if err := huaweiauth.Validate(*opts.Signing); err != nil {
			return nil, err
		}
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	transport.DisableCompression = true
	transport.ResponseHeaderTimeout = 30 * time.Second
	transport.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12}
	client := &http.Client{
		Transport: transport,
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	return &Server{
		opts:       opts,
		upstream:   upstream,
		httpClient: client,
		allowed:    allowed,
		sessions:   map[string]session{},
	}, nil
}

func parseUpstream(raw string) (*url.URL, error) {
	upstream, err := url.Parse(raw)
	if err != nil || upstream.Host == "" || (upstream.Scheme != "http" && upstream.Scheme != "https") {
		return nil, errors.New("上游 MCP 地址无效")
	}
	if upstream.User != nil || upstream.RawQuery != "" || upstream.Fragment != "" {
		return nil, errors.New("上游 MCP 地址不能包含凭据、查询串或片段")
	}
	return upstream, nil
}

// Start binds the proxy to an ephemeral loopback port.
func (s *Server) Start() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.listener != nil {
		return nil
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return fmt.Errorf("启动来源代理失败: %w", err)
	}
	server := &http.Server{
		Handler:           s,
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    32 << 10,
	}
	s.listener = listener
	s.httpServer = server
	go func() {
		_ = server.Serve(listener)
	}()
	return nil
}

// UpdateUpstream changes only this source's target. The loopback listener and
// the FRP localPort bound to it stay unchanged; old MCP sessions are discarded.
func (s *Server) UpdateUpstream(raw string) error {
	upstream, err := parseUpstream(raw)
	if err != nil {
		return err
	}
	s.mu.Lock()
	s.upstream = upstream
	s.sessions = map[string]session{}
	s.mu.Unlock()
	return nil
}

// UpstreamURL returns the currently selected upstream MCP endpoint.
func (s *Server) UpstreamURL() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.upstream == nil {
		return ""
	}
	return s.upstream.String()
}

// Endpoint returns the loopback host and port used by frpc.
func (s *Server) Endpoint() (string, int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.listener == nil {
		return "", 0, errors.New("来源代理尚未启动")
	}
	addr, ok := s.listener.Addr().(*net.TCPAddr)
	if !ok {
		return "", 0, errors.New("来源代理监听地址无效")
	}
	return "127.0.0.1", addr.Port, nil
}

// Close stops the proxy without affecting other sources.
func (s *Server) Close(ctx context.Context) error {
	s.mu.Lock()
	if s.httpServer == nil {
		s.mu.Unlock()
		return nil
	}
	httpServer := s.httpServer
	s.sessions = map[string]session{}
	s.listener = nil
	s.httpServer = nil
	s.mu.Unlock()
	err := httpServer.Shutdown(ctx)
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}

// ServeHTTP accepts only the MCP methods needed by the controlled read-only
// integration and never forwards arbitrary paths or headers.
func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	observation := Observation{
		SourceID:  s.opts.SourceID,
		TraceID:   safeTraceID(r.Header.Get("X-Kaiwu-Trace-ID")),
		StartedAt: time.Now(),
	}
	observation.Platform = observation.TraceID != ""
	defer func() { s.report(observation) }()

	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	if !s.acceptedPath(r.URL.Path) {
		markFailed(&observation, http.StatusNotFound)
		writeProxyError(w, http.StatusNotFound, "SOURCE_NOT_FOUND", "来源路径不存在")
		return
	}
	if r.Method != http.MethodPost {
		markFailed(&observation, http.StatusMethodNotAllowed)
		w.Header().Set("Allow", http.MethodPost)
		writeProxyError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "只支持 MCP POST 请求")
		return
	}
	if !isJSONContentType(r.Header.Get("Content-Type")) {
		markFailed(&observation, http.StatusUnsupportedMediaType)
		writeProxyError(w, http.StatusUnsupportedMediaType, "CONTENT_TYPE_INVALID", "只支持 application/json")
		return
	}
	raw, err := readLimited(r.Body, s.opts.MaxRequestSize)
	if err != nil {
		markFailed(&observation, http.StatusRequestEntityTooLarge)
		writeProxyError(w, http.StatusRequestEntityTooLarge, "REQUEST_TOO_LARGE", "MCP 请求超过大小限制")
		return
	}
	var envelope rpcEnvelope
	if err := json.Unmarshal(raw, &envelope); err != nil || envelope.JSONRPC != "2.0" {
		markFailed(&observation, http.StatusBadRequest)
		writeProxyError(w, http.StatusBadRequest, "MCP_INVALID", "请求不是有效的 MCP JSON-RPC 对象")
		return
	}
	observation.Method = safeMethod(envelope.Method)
	if envelope.Method == "" {
		markFailed(&observation, http.StatusBadRequest)
		writeProxyError(w, http.StatusBadRequest, "MCP_INVALID", "缺少 MCP 方法")
		return
	}
	toolName, err := s.policy(envelope)
	if err != nil {
		markFailed(&observation, http.StatusForbidden)
		writeProxyError(w, http.StatusForbidden, "POLICY_DENIED", err.Error())
		return
	}
	observation.ToolName = toolName

	proxySessionID := strings.TrimSpace(r.Header.Get("Mcp-Session-Id"))
	upstreamSessionID, err := s.resolveSession(envelope.Method, proxySessionID)
	if err != nil {
		markFailed(&observation, http.StatusNotFound)
		writeProxyError(w, http.StatusNotFound, "MCP_SESSION_INVALID", "MCP 会话不存在或已过期")
		return
	}

	upstream := s.currentUpstream()
	if upstream == nil {
		markFailed(&observation, http.StatusServiceUnavailable)
		writeProxyError(w, http.StatusServiceUnavailable, "UPSTREAM_NOT_CONFIGURED", "来源代理尚未配置上游地址")
		return
	}
	outgoing, err := http.NewRequestWithContext(r.Context(), http.MethodPost, upstream.String(), bytes.NewReader(raw))
	if err != nil {
		markFailed(&observation, http.StatusBadGateway)
		writeProxyError(w, http.StatusBadGateway, "UPSTREAM_REQUEST_FAILED", "无法构造上游请求")
		return
	}
	outgoing.Header.Set("Content-Type", "application/json")
	outgoing.Header.Set("Accept", "application/json, text/event-stream")
	outgoing.Header.Set("User-Agent", "kaiwu-intranet-connector-authproxy/1")
	if protocol := strings.TrimSpace(r.Header.Get("MCP-Protocol-Version")); validProtocolVersion(protocol) {
		outgoing.Header.Set("MCP-Protocol-Version", protocol)
	}
	if upstreamSessionID != "" {
		outgoing.Header.Set("Mcp-Session-Id", upstreamSessionID)
	}
	for name, value := range s.opts.Headers {
		if validHeader(name, value) {
			outgoing.Header.Set(name, value)
		}
	}
	if envelope.Method == "tools/call" && s.opts.Signing != nil {
		headers, err := huaweiauth.SignedHeaders(*s.opts.Signing, time.Now(), rand.Reader)
		if err != nil {
			markFailed(&observation, http.StatusServiceUnavailable)
			writeProxyError(w, http.StatusServiceUnavailable, "AUTH_PROFILE_INVALID", "本机认证配置不可用")
			return
		}
		for name, value := range headers {
			outgoing.Header.Set(name, value)
		}
	}

	resp, err := s.httpClient.Do(outgoing)
	if err != nil {
		markFailed(&observation, http.StatusBadGateway)
		writeProxyError(w, http.StatusBadGateway, "UPSTREAM_UNAVAILABLE", "您当前服务器的 MCP 暂不可用")
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 && resp.StatusCode < 400 {
		markFailed(&observation, http.StatusBadGateway)
		writeProxyError(w, http.StatusBadGateway, "UPSTREAM_REDIRECT_DENIED", "您当前服务器的 MCP 返回了不允许的重定向")
		return
	}
	contentType := resp.Header.Get("Content-Type")
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		payload, _ := readLimited(resp.Body, s.opts.MaxResponseSize)
		outcome := inspectMCPResponse(envelope.ID, payload)
		observation.HTTPStatus = resp.StatusCode
		observation.Failed = true
		observation.RPCErrorCode = outcome.rpcErrorCode
		writeProxyError(w, http.StatusBadGateway, "UPSTREAM_HTTP_ERROR", "您当前服务器的 MCP 请求失败")
		return
	}
	if strings.Contains(strings.ToLower(contentType), "text/event-stream") {
		if upstreamSID := resp.Header.Get("Mcp-Session-Id"); upstreamSID != "" {
			proxySessionID = s.rememberSession(proxySessionID, upstreamSID)
		}
		if proxySessionID != "" {
			w.Header().Set("Mcp-Session-Id", proxySessionID)
		}
		if contentType != "" {
			w.Header().Set("Content-Type", contentType)
		}
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Accel-Buffering", "no")
		w.WriteHeader(resp.StatusCode)
		flusher, ok := w.(http.Flusher)
		if !ok {
			markFailed(&observation, resp.StatusCode)
			return
		}
		flusher.Flush()
		outcome, streamErr := s.streamSSE(resp.Body, w, flusher, envelope.Method, envelope.ID)
		if streamErr != nil {
			if relayWriter, ok := w.(interface{ AbortMCPStream() }); ok {
				relayWriter.AbortMCPStream()
			}
		}
		observation.HTTPStatus = resp.StatusCode
		observation.RPCErrorCode = outcome.rpcErrorCode
		observation.Failed = outcome.failed || streamErr != nil
		return
	}
	payload, err := readLimited(resp.Body, s.opts.MaxResponseSize)
	if err != nil {
		markFailed(&observation, http.StatusBadGateway)
		writeProxyError(w, http.StatusBadGateway, "UPSTREAM_RESPONSE_TOO_LARGE", "您当前服务器的 MCP 响应超过大小限制")
		return
	}
	payload, err = s.filterResponse(envelope.Method, payload, contentType)
	if err != nil {
		markFailed(&observation, http.StatusBadGateway)
		writeProxyError(w, http.StatusBadGateway, "UPSTREAM_RESPONSE_INVALID", "您当前服务器的 MCP 响应不符合协议")
		return
	}
	outcome := inspectMCPResponse(envelope.ID, payload).finalize(envelope.Method)
	observation.HTTPStatus = resp.StatusCode
	observation.RPCErrorCode = outcome.rpcErrorCode
	observation.Failed = outcome.failed

	if upstreamSID := resp.Header.Get("Mcp-Session-Id"); upstreamSID != "" {
		proxySessionID = s.rememberSession(proxySessionID, upstreamSID)
	}
	if proxySessionID != "" {
		w.Header().Set("Mcp-Session-Id", proxySessionID)
	}
	if contentType != "" {
		w.Header().Set("Content-Type", contentType)
	}
	w.WriteHeader(resp.StatusCode)
	if r.Method != http.MethodHead && len(payload) > 0 {
		_, _ = w.Write(payload)
	}
}

func (s *Server) currentUpstream() *url.URL {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.upstream
}

func (s *Server) report(observation Observation) {
	if s.opts.OnMCPCall == nil {
		return
	}
	if !observation.Platform && observation.Method != "tools/call" {
		return
	}
	s.opts.OnMCPCall(observation)
}

func markFailed(observation *Observation, status int) {
	observation.Failed = true
	observation.HTTPStatus = status
}

func (s *Server) acceptedPath(value string) bool {
	if value == "/mcp" {
		return true
	}
	return path.Clean(value) == "/sources/"+url.PathEscape(s.opts.SourceID)+"/mcp"
}

func (s *Server) policy(envelope rpcEnvelope) (string, error) {
	switch envelope.Method {
	case "initialize", "notifications/initialized", "ping":
		return "", nil
	case "tools/list":
		return "", nil
	case "tools/call":
		var params struct {
			Name string `json:"name"`
		}
		if err := json.Unmarshal(envelope.Params, &params); err != nil || strings.TrimSpace(params.Name) == "" {
			return "", errors.New("tools/call 缺少工具名称")
		}
		if !s.allowed[params.Name] {
			return "", errors.New("工具不在当前已批准列表内")
		}
		// Signing is optional. Sources using no authentication or a fixed
		// Authorization header must still be callable through the local proxy.
		// Dynamic-auth sources carry Signing and add their signed headers below.
		return params.Name, nil
	default:
		return "", errors.New("MCP 方法未获准")
	}
}

func (s *Server) resolveSession(method, proxyID string) (string, error) {
	now := time.Now()
	s.mu.Lock()
	defer s.mu.Unlock()
	s.expireSessions(now)
	if proxyID == "" {
		return "", nil
	}
	value, ok := s.sessions[proxyID]
	if !ok || value.expiresAt.Before(now) {
		delete(s.sessions, proxyID)
		return "", errors.New("MCP 会话已过期")
	}
	value.expiresAt = now.Add(sessionTTL)
	s.sessions[proxyID] = value
	return value.upstreamID, nil
}

func (s *Server) rememberSession(proxyID, upstreamID string) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := time.Now()
	s.expireSessions(now)
	if proxyID == "" {
		var err error
		proxyID, err = randomID()
		if err != nil {
			return ""
		}
	}
	if len(s.sessions) >= maxSessions {
		var oldestID string
		var oldest time.Time
		for id, item := range s.sessions {
			if oldestID == "" || item.expiresAt.Before(oldest) {
				oldestID = id
				oldest = item.expiresAt
			}
		}
		delete(s.sessions, oldestID)
	}
	s.sessions[proxyID] = session{upstreamID: upstreamID, expiresAt: now.Add(sessionTTL)}
	return proxyID
}

func (s *Server) expireSessions(now time.Time) {
	for id, item := range s.sessions {
		if item.expiresAt.Before(now) {
			delete(s.sessions, id)
		}
	}
}

func (s *Server) filterResponse(method string, payload []byte, contentType string) ([]byte, error) {
	lowerType := strings.ToLower(contentType)
	if strings.Contains(lowerType, "text/event-stream") {
		return nil, errors.New("SSE responses must be streamed")
	}
	if contentType != "" && !strings.Contains(lowerType, "application/json") && !strings.Contains(lowerType, "+json") {
		return nil, errors.New("unsupported response content type")
	}
	return s.filterJSON(method, payload)
}

func (s *Server) filterJSON(method string, payload []byte) ([]byte, error) {
	if len(bytes.TrimSpace(payload)) == 0 {
		return payload, nil
	}
	var object map[string]any
	if err := json.Unmarshal(payload, &object); err != nil {
		return nil, err
	}
	switch method {
	case "tools/list":
		filtered, err := s.filterToolsObject(object)
		if err != nil {
			return nil, err
		}
		object = filtered
	case "initialize":
		filterInitializeCapabilities(object)
	}
	return json.Marshal(object)
}

// streamSSE forwards one upstream SSE event at a time. Only initialize and
// tools/list events are rewritten; all other event payloads stay byte-for-byte
// equivalent at the event boundary. Multiple data lines are joined before one
// JSON filter pass and emitted as one data line, so a filtered result is never
// duplicated once per source line.
func (s *Server) streamSSE(body io.Reader, writer io.Writer, flusher http.Flusher, method string, requestID json.RawMessage) (mcpOutcome, error) {
	reader := bufio.NewReaderSize(body, 32<<10)
	var event []string
	eventBytes := 0
	result := mcpOutcome{}
	limit := s.opts.MaxResponseSize
	if limit <= 0 {
		limit = defaultMaxResponseBytes
	}
	flushEvent := func() error {
		if len(event) == 0 {
			return nil
		}
		var output strings.Builder
		dataLines := make([]string, 0, len(event))
		for _, line := range event {
			if strings.HasPrefix(line, "data:") {
				dataLines = append(dataLines, strings.TrimPrefix(strings.TrimPrefix(line, "data:"), " "))
			}
		}
		if len(dataLines) > 0 {
			combined := []byte(strings.Join(dataLines, "\n"))
			inspected := inspectMCPResponse(requestID, combined)
			result.failed = result.failed || inspected.failed
			result.responded = result.responded || inspected.responded
			if inspected.rpcErrorCode != "" {
				result.rpcErrorCode = inspected.rpcErrorCode
			}
			if method == "initialize" || method == "tools/list" {
				filtered, err := s.filterJSON(method, combined)
				if err != nil {
					return err
				}
				for _, line := range event {
					if !strings.HasPrefix(line, "data:") {
						output.WriteString(line)
						output.WriteByte('\n')
					}
				}
				output.WriteString("data: ")
				output.Write(filtered)
				output.WriteString("\n\n")
			} else {
				for _, line := range event {
					output.WriteString(line)
					output.WriteByte('\n')
				}
				output.WriteByte('\n')
			}
		} else {
			for _, line := range event {
				output.WriteString(line)
				output.WriteByte('\n')
			}
			output.WriteByte('\n')
		}
		if _, err := io.WriteString(writer, output.String()); err != nil {
			return err
		}
		flusher.Flush()
		event = event[:0]
		eventBytes = 0
		return nil
	}

	for {
		line, readErr := readSSELine(reader, limit)
		if len(line) > 0 {
			line = strings.TrimSuffix(strings.TrimSuffix(line, "\n"), "\r")
			if line == "" {
				if err := flushEvent(); err != nil {
					return result, err
				}
			} else {
				eventBytes += len(line) + 1
				if int64(eventBytes) > limit {
					return result, errors.New("SSE event exceeds response limit")
				}
				event = append(event, line)
			}
		}
		if readErr != nil {
			if errors.Is(readErr, io.EOF) {
				flushErr := flushEvent()
				return result.finalize(method), flushErr
			}
			return result, readErr
		}
	}
}

func readSSELine(reader *bufio.Reader, limit int64) (string, error) {
	var line []byte
	for {
		fragment, err := reader.ReadSlice('\n')
		if len(fragment) > 0 {
			if int64(len(line)+len(fragment)) > limit {
				return "", errors.New("SSE line exceeds response limit")
			}
			line = append(line, fragment...)
		}
		switch {
		case err == nil:
			return string(line), nil
		case errors.Is(err, bufio.ErrBufferFull):
			continue
		default:
			return string(line), err
		}
	}
}

func inspectMCPResponse(requestID json.RawMessage, payload []byte) mcpOutcome {
	if len(bytes.TrimSpace(payload)) == 0 {
		return mcpOutcome{}
	}
	var object map[string]any
	decoder := json.NewDecoder(bytes.NewReader(payload))
	decoder.UseNumber()
	if err := decoder.Decode(&object); err != nil {
		return mcpOutcome{failed: true}
	}
	if decoder.Decode(&struct{}{}) != io.EOF || object["jsonrpc"] != "2.0" {
		return mcpOutcome{failed: true}
	}
	rawID, hasID := object["id"]
	if !hasID {
		if _, notification := object["method"]; notification {
			return mcpOutcome{}
		}
		return mcpOutcome{failed: true}
	}
	if len(bytes.TrimSpace(requestID)) > 0 && !sameJSONValue(rawID, requestID) {
		// A shared SSE stream can contain responses for other in-flight calls.
		// Ignore them until this request's response arrives.
		return mcpOutcome{}
	}
	outcome := mcpOutcome{}
	outcome.responded = true
	if rawError, exists := object["error"]; exists && rawError != nil {
		outcome.failed = true
		if errorObject, ok := rawError.(map[string]any); ok {
			outcome.rpcErrorCode = safeRPCErrorCode(errorObject["code"])
		}
	}
	result, hasResult := object["result"]
	if !hasResult {
		outcome.failed = true
	}
	if resultObject, ok := result.(map[string]any); ok {
		if isError, _ := resultObject["isError"].(bool); isError {
			outcome.failed = true
		}
	}
	return outcome
}

func (outcome mcpOutcome) finalize(method string) mcpOutcome {
	switch method {
	case "initialize", "tools/list", "tools/call", "ping":
		if !outcome.responded {
			outcome.failed = true
		}
	}
	return outcome
}

func sameJSONValue(left any, right json.RawMessage) bool {
	leftRaw, err := json.Marshal(left)
	if err != nil {
		return false
	}
	var compactLeft, compactRight bytes.Buffer
	if json.Compact(&compactLeft, leftRaw) != nil || json.Compact(&compactRight, right) != nil {
		return false
	}
	return bytes.Equal(compactLeft.Bytes(), compactRight.Bytes())
}

func safeRPCErrorCode(value any) string {
	var code int64
	switch typed := value.(type) {
	case json.Number:
		parsed, err := typed.Int64()
		if err != nil {
			return ""
		}
		code = parsed
	case float64:
		if typed < -32700 || typed > -32000 || typed != float64(int64(typed)) {
			return ""
		}
		code = int64(typed)
	default:
		return ""
	}
	if code < -32700 || code > -32000 {
		return ""
	}
	return strconv.FormatInt(code, 10)
}

func safeTraceID(value string) string {
	value = strings.TrimSpace(value)
	if !traceIDPattern.MatchString(value) {
		return ""
	}
	return value
}

func safeMethod(value string) string {
	switch value {
	case "initialize", "notifications/initialized", "ping", "tools/list", "tools/call":
		return value
	default:
		return ""
	}
}

func (s *Server) filterToolsObject(object map[string]any) (map[string]any, error) {
	result, ok := object["result"].(map[string]any)
	if !ok {
		return object, nil
	}
	rawTools, ok := result["tools"].([]any)
	if !ok {
		return object, nil
	}
	filtered := make([]any, 0, len(rawTools))
	for _, item := range rawTools {
		tool, ok := item.(map[string]any)
		if !ok {
			continue
		}
		name, _ := tool["name"].(string)
		if s.allowed[name] {
			filtered = append(filtered, tool)
		}
	}
	result["tools"] = filtered
	object["result"] = result
	return object, nil
}

func filterInitializeCapabilities(object map[string]any) {
	result, ok := object["result"].(map[string]any)
	if !ok {
		return
	}
	if _, exists := result["capabilities"]; exists {
		result["capabilities"] = map[string]any{"tools": map[string]any{}}
	}
	object["result"] = result
}

func isJSONContentType(value string) bool {
	mediaType, _, err := mime.ParseMediaType(value)
	if err != nil {
		return false
	}
	return strings.EqualFold(mediaType, "application/json")
}

func validProtocolVersion(value string) bool {
	if value == "" || len(value) > 32 {
		return false
	}
	for _, r := range value {
		if (r < '0' || r > '9') && r != '-' {
			return false
		}
	}
	return true
}

func validHeader(name, value string) bool {
	if name == "" || strings.ContainsAny(name, "\r\n:") || strings.ContainsAny(value, "\r\n") {
		return false
	}
	return !strings.EqualFold(name, "Host") &&
		!strings.EqualFold(name, "Content-Length") &&
		!strings.EqualFold(name, "Transfer-Encoding") &&
		!strings.EqualFold(name, "Mcp-Session-Id") &&
		!strings.EqualFold(name, "MCP-Protocol-Version")
}

func readLimited(reader io.Reader, limit int64) ([]byte, error) {
	payload, err := io.ReadAll(io.LimitReader(reader, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(payload)) > limit {
		return nil, errors.New("payload exceeds limit")
	}
	return payload, nil
}

func randomID() (string, error) {
	raw := make([]byte, 16)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return "sess_" + hex.EncodeToString(raw), nil
}

func writeProxyError(w http.ResponseWriter, status int, code, message string) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{
		"error": map[string]string{"code": code, "message": message},
	})
}

// AllowedTools returns a sorted copy for status rendering and tests.
func (s *Server) AllowedTools() []string {
	items := make([]string, 0, len(s.allowed))
	for item := range s.allowed {
		items = append(items, item)
	}
	sort.Strings(items)
	return items
}
