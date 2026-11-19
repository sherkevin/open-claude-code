package authproxy

import (
	"bufio"
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/huaweiauth"
)

func TestProxyFiltersToolsAndSignsOnlyAllowedCalls(t *testing.T) {
	profile := huaweiauth.Profile{
		AppID:   "app-demo",
		Channel: "channel-demo",
		UserID:  "user-demo",
		HMACKey: "0123456789abcdef0123456789abcdef",
	}
	var mu sync.Mutex
	upstreamCalls := map[string]int{}
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var request struct {
			ID     int64           `json:"id"`
			Method string          `json:"method"`
			Params json.RawMessage `json:"params"`
		}
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		if got := r.Header.Get("Authorization"); got != "Bearer customer-secret" {
			t.Errorf("客户 Bearer 未由本机代理注入: %q", got)
		}
		if r.Header.Get("X-Forwarded-For") != "" || r.Header.Get("X-Edge-Token") != "" {
			t.Errorf("入口认证头必须被剥离")
		}
		mu.Lock()
		upstreamCalls[request.Method]++
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		switch request.Method {
		case "initialize":
			w.Header().Set("Mcp-Session-Id", "upstream-session")
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{"protocolVersion":"2025-06-18","capabilities":{"tools":{},"prompts":{},"resources":{}},"serverInfo":{"name":"demo","version":"1"}}}`, request.ID)
		case "tools/list":
			if got := r.Header.Get("Mcp-Session-Id"); got != "upstream-session" {
				t.Errorf("上游会话映射错误: %q", got)
			}
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{"tools":[{"name":"manual_search"},{"name":"dangerous_write"}]}}`, request.ID)
		case "tools/call":
			if r.Header.Get("appid") != profile.AppID ||
				r.Header.Get("channel") != profile.Channel ||
				r.Header.Get("userid") != profile.UserID {
				t.Errorf("渠道身份头不完整")
			}
			requestID := r.Header.Get("requestId")
			if len(requestID) != 36 {
				t.Errorf("requestId 格式无效: %q", requestID)
			}
			timestamp, err := strconv.ParseInt(r.Header.Get("timestamp"), 10, 64)
			if err != nil || time.Since(time.Unix(timestamp, 0)) > time.Minute {
				t.Errorf("timestamp 无效: %q", r.Header.Get("timestamp"))
			}
			canonical := huaweiauth.CanonicalString(profile, requestID, timestamp)
			mac := hmac.New(sha256.New, []byte(profile.HMACKey))
			_, _ = mac.Write([]byte(canonical))
			if got, want := r.Header.Get("sign"), hex.EncodeToString(mac.Sum(nil)); got != want {
				t.Errorf("HMAC 不符: got %s want %s", got, want)
			}
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{"content":[{"type":"text","text":"ok"}]}}`, request.ID)
		default:
			http.Error(w, "unexpected method", http.StatusBadRequest)
		}
	}))
	defer upstream.Close()

	proxy, baseURL := startTestProxy(t, Options{
		SourceID:      "src_demo",
		UpstreamURL:   upstream.URL + "/mcp",
		Headers:       map[string]string{"Authorization": "Bearer customer-secret"},
		Signing:       &profile,
		ToolAllowlist: []string{"manual_search"},
	})
	defer proxy.Close(testContext(t))

	status, session, body := postMCP(t, baseURL, "", "initialize", map[string]any{})
	if status != http.StatusOK || session == "" || session == "upstream-session" {
		t.Fatalf("初始化或会话映射失败: status=%d session=%q body=%s", status, session, body)
	}
	if !strings.Contains(body, `"tools"`) || strings.Contains(body, `"prompts"`) {
		t.Fatalf("initialize 能力未收窄: %s", body)
	}

	status, _, body = postMCP(t, baseURL, session, "tools/list", map[string]any{})
	if status != http.StatusOK || !strings.Contains(body, "manual_search") || strings.Contains(body, "dangerous_write") {
		t.Fatalf("工具目录未按允许列表过滤: status=%d body=%s", status, body)
	}

	status, _, body = postMCP(t, baseURL, session, "tools/call", map[string]any{
		"name": "manual_search", "arguments": map[string]any{"q": "synthetic"},
	})
	if status != http.StatusOK || !strings.Contains(body, `"text":"ok"`) {
		t.Fatalf("允许工具调用失败: status=%d body=%s", status, body)
	}

	before := countCalls(&mu, upstreamCalls, "tools/call")
	status, _, _ = postMCP(t, baseURL, session, "tools/call", map[string]any{
		"name": "dangerous_write", "arguments": map[string]any{},
	})
	if status != http.StatusForbidden || countCalls(&mu, upstreamCalls, "tools/call") != before {
		t.Fatalf("不允许工具必须在本地拒绝: status=%d upstream=%d", status, countCalls(&mu, upstreamCalls, "tools/call"))
	}

	status, _, _ = postMCP(t, baseURL, session, "resources/read", map[string]any{})
	if status != http.StatusForbidden {
		t.Fatalf("未知方法必须拒绝: %d", status)
	}
}

func TestProxyFiltersSSEToolList(t *testing.T) {
	profile := huaweiauth.Profile{
		AppID: "app", Channel: "channel", UserID: "user",
		HMACKey: "0123456789abcdef0123456789abcdef",
	}
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var request struct {
			ID     int64  `json:"id"`
			Method string `json:"method"`
		}
		_ = json.NewDecoder(r.Body).Decode(&request)
		if request.Method == "initialize" {
			w.Header().Set("Content-Type", "application/json")
			w.Header().Set("Mcp-Session-Id", "upstream-sse")
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{"protocolVersion":"2025-06-18","capabilities":{"tools":{}},"serverInfo":{"name":"demo","version":"1"}}}`, request.ID)
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprintf(w, "event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":%d,\"result\":{\"tools\":[{\"name\":\"manual_search\"},{\"name\":\"dangerous_write\"}]}}\n\n", request.ID)
	}))
	defer upstream.Close()

	proxy, baseURL := startTestProxy(t, Options{
		SourceID: "src_sse", UpstreamURL: upstream.URL,
		Signing: &profile, ToolAllowlist: []string{"manual_search"},
	})
	defer proxy.Close(testContext(t))
	status, session, body := postMCP(t, baseURL, "", "initialize", map[string]any{})
	if status != http.StatusOK {
		t.Fatalf("初始化失败: %d %s", status, body)
	}
	status, _, body = postMCP(t, baseURL, session, "tools/list", map[string]any{})
	if status != http.StatusOK || !strings.Contains(body, "manual_search") || strings.Contains(body, "dangerous_write") {
		t.Fatalf("SSE 工具目录未过滤: %d %s", status, body)
	}
}

func TestProxyStreamsFirstSSEEventBeforeUpstreamClose(t *testing.T) {
	release := make(chan struct{})
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		flusher := w.(http.Flusher)
		flusher.Flush()
		_, _ = io.WriteString(w, "event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{}}\n\n")
		flusher.Flush()
		<-release
	}))
	defer func() {
		close(release)
		upstream.Close()
	}()

	proxy, baseURL := startTestProxy(t, Options{SourceID: "src_stream", UpstreamURL: upstream.URL})
	defer proxy.Close(testContext(t))
	payload := []byte(`{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}`)
	request, err := http.NewRequest(http.MethodPost, baseURL, bytes.NewReader(payload))
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Content-Type", "application/json")
	type result struct {
		response *http.Response
		err      error
	}
	results := make(chan result, 1)
	go func() {
		response, requestErr := http.DefaultClient.Do(request)
		results <- result{response: response, err: requestErr}
	}()
	var response *http.Response
	select {
	case got := <-results:
		if got.err != nil {
			t.Fatal(got.err)
		}
		response = got.response
	case <-time.After(2 * time.Second):
		t.Fatal("代理在客户端取得首个事件前缓冲了整个 SSE 响应")
	}
	defer response.Body.Close()
	reader := bufio.NewReader(response.Body)
	first, err := reader.ReadString('\n')
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(first, "event: message") {
		t.Fatalf("首个事件不完整: %q", first)
	}
	second, err := reader.ReadString('\n')
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(second, `"result":{}`) {
		t.Fatalf("首个数据事件未及时转发: %q", second)
	}
}

func TestProxyMergesMultilineSSEDataOnce(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w,
			"event: message\nid: 7\ndata: {\"jsonrpc\":\"2.0\",\"id\":7,\n"+
				"data: \"result\":{\"tools\":[{\"name\":\"manual_search\"},{\"name\":\"dangerous_write\"}]}}\n\n",
		)
	}))
	defer upstream.Close()
	proxy, baseURL := startTestProxy(t, Options{
		SourceID: "src_multiline", UpstreamURL: upstream.URL,
		ToolAllowlist: []string{"manual_search"},
	})
	defer proxy.Close(testContext(t))
	status, _, body := postMCP(t, baseURL, "", "tools/list", map[string]any{})
	if status != http.StatusOK {
		t.Fatalf("SSE 工具目录请求失败: %d %s", status, body)
	}
	if strings.Count(body, "data:") != 1 {
		t.Fatalf("多行 data 必须合并为一个有效载荷: %s", body)
	}
	if !strings.Contains(body, "id: 7") || !strings.Contains(body, "manual_search") || strings.Contains(body, "dangerous_write") {
		t.Fatalf("多行 SSE 过滤结果不符: %s", body)
	}
}

func TestProxyRejectsRedirectWithoutFollowing(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "http://127.0.0.1:1/secret", http.StatusTemporaryRedirect)
	}))
	defer upstream.Close()
	proxy, baseURL := startTestProxy(t, Options{SourceID: "src_redirect", UpstreamURL: upstream.URL})
	defer proxy.Close(testContext(t))
	status, _, body := postMCP(t, baseURL, "", "ping", map[string]any{})
	if status != http.StatusBadGateway || !strings.Contains(body, "UPSTREAM_REDIRECT_DENIED") {
		t.Fatalf("重定向必须失败关闭: %d %s", status, body)
	}
}

func TestProxyReportsTraceAndDetectsToolIsError(t *testing.T) {
	profile := huaweiauth.Profile{
		AppID: "app", Channel: "channel", UserID: "user",
		HMACKey: "0123456789abcdef0123456789abcdef",
	}
	var upstreamTrace atomic.Value
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstreamTrace.Store(r.Header.Get("X-Kaiwu-Trace-ID"))
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"synthetic failure"}],"isError":true}}`))
	}))
	defer upstream.Close()

	observations := make(chan Observation, 1)
	proxy, baseURL := startTestProxy(t, Options{
		SourceID:      "src_trace",
		UpstreamURL:   upstream.URL,
		Signing:       &profile,
		ToolAllowlist: []string{"manual_search"},
		OnMCPCall:     func(observation Observation) { observations <- observation },
	})
	defer proxy.Close(testContext(t))

	traceID := "kwtr_11111111111111111111111111111111"
	status, _, body := postMCPWithHeaders(t, baseURL, "", "tools/call", map[string]any{
		"name": "manual_search", "arguments": map[string]any{"q": "synthetic"},
	}, map[string]string{"X-Kaiwu-Trace-ID": traceID})
	if status != http.StatusOK || !strings.Contains(body, `"isError":true`) {
		t.Fatalf("proxy must preserve the upstream tool result: status=%d body=%s", status, body)
	}
	if got, _ := upstreamTrace.Load().(string); got != "" {
		t.Fatalf("diagnostic trace header must not reach the customer MCP: %q", got)
	}
	select {
	case observation := <-observations:
		if observation.TraceID != traceID || !observation.Platform || observation.SourceID != "src_trace" ||
			observation.ToolName != "manual_search" || !observation.Failed || observation.HTTPStatus != http.StatusOK {
			t.Fatalf("unexpected diagnostic observation: %+v", observation)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("authproxy did not report trace metadata")
	}
}

func TestProxyReportsBoundedRPCErrorCode(t *testing.T) {
	profile := huaweiauth.Profile{
		AppID: "app", Channel: "channel", UserID: "user",
		HMACKey: "0123456789abcdef0123456789abcdef",
	}
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"jsonrpc":"2.0","id":1,"error":{"code":-32603,"message":"SYNTHETIC_SECRET_CANARY"}}`))
	}))
	defer upstream.Close()

	observations := make(chan Observation, 1)
	proxy, baseURL := startTestProxy(t, Options{
		SourceID:      "src_rpc_error",
		UpstreamURL:   upstream.URL,
		Signing:       &profile,
		ToolAllowlist: []string{"manual_search"},
		OnMCPCall:     func(observation Observation) { observations <- observation },
	})
	defer proxy.Close(testContext(t))
	status, _, _ := postMCPWithHeaders(t, baseURL, "", "tools/call", map[string]any{
		"name": "manual_search", "arguments": map[string]any{},
	}, map[string]string{"X-Kaiwu-Trace-ID": "kwtr_22222222222222222222222222222222"})
	if status != http.StatusOK {
		t.Fatalf("unexpected proxy status: %d", status)
	}
	select {
	case observation := <-observations:
		if !observation.Failed || observation.RPCErrorCode != "-32603" {
			t.Fatalf("unexpected RPC classification: %+v", observation)
		}
		if strings.Contains(observation.RPCErrorCode, "SYNTHETIC") {
			t.Fatal("free-form upstream error text reached the diagnostic metadata")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("authproxy did not report the RPC error")
	}
}

func TestProxyMarksHTTP200WithoutResultAsFailure(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{}`))
	}))
	defer upstream.Close()

	observations := make(chan Observation, 1)
	proxy, baseURL := startTestProxy(t, Options{
		SourceID:    "src_missing_result",
		UpstreamURL: upstream.URL,
		OnMCPCall:   func(observation Observation) { observations <- observation },
	})
	defer proxy.Close(testContext(t))
	status, _, body := postMCPWithHeaders(t, baseURL, "", "ping", map[string]any{},
		map[string]string{"X-Kaiwu-Trace-ID": "kwtr_33333333333333333333333333333333"})
	if status != http.StatusOK || !strings.Contains(body, `{}`) {
		t.Fatalf("response forwarding changed: status=%d body=%s", status, body)
	}
	select {
	case observation := <-observations:
		if !observation.Failed {
			t.Fatalf("HTTP 200 without a JSON-RPC result was reported as success: %+v", observation)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("authproxy did not report the missing result")
	}
}

func TestInspectMCPResponseRequiresMatchingResult(t *testing.T) {
	requestID := json.RawMessage(`1`)
	missing := inspectMCPResponse(requestID, []byte(`{"jsonrpc":"2.0","id":1}`)).finalize("tools/call")
	if !missing.failed {
		t.Fatal("a response without result or error must fail")
	}
	valid := inspectMCPResponse(requestID, []byte(`{"jsonrpc":"2.0","id":1,"result":{}}`)).finalize("ping")
	if valid.failed || !valid.responded {
		t.Fatalf("valid ping response was rejected: %+v", valid)
	}
	otherID := inspectMCPResponse(requestID, []byte(`{"jsonrpc":"2.0","id":2,"result":{}}`)).finalize("ping")
	if !otherID.failed || otherID.responded {
		t.Fatalf("unrelated response satisfied the request: %+v", otherID)
	}
	progress := inspectMCPResponse(requestID, []byte(`{"jsonrpc":"2.0","method":"notifications/progress","params":{}}`))
	if progress.failed || progress.responded {
		t.Fatalf("progress notification is not a response: %+v", progress)
	}
	if !progress.finalize("tools/call").failed {
		t.Fatal("a stream ending with only progress notifications must fail")
	}
	notificationACK := inspectMCPResponse(nil, nil).finalize("notifications/initialized")
	if notificationACK.failed {
		t.Fatalf("empty notification ACK must succeed: %+v", notificationACK)
	}
	if got := safeRPCErrorCode("TOOL_ERROR"); got != "" {
		t.Fatalf("enum text must not enter rpc_error_code: %q", got)
	}
	if got := safeRPCErrorCode(json.Number("-32999")); got != "" {
		t.Fatalf("out-of-range RPC code was accepted: %q", got)
	}
	if got := safeRPCErrorCode(json.Number("-32500")); got != "-32500" {
		t.Fatalf("valid in-range RPC code was rejected: %q", got)
	}
}

func TestPolicyAllowsApprovedToolWithoutDynamicSigning(t *testing.T) {
	server := &Server{allowed: map[string]bool{"kaiwu_demo_ping": true}}
	name, err := server.policy(rpcEnvelope{
		Method: "tools/call",
		Params: json.RawMessage(`{"name":"kaiwu_demo_ping","arguments":{"nonce":"n-1"}}`),
	})
	if err != nil || name != "kaiwu_demo_ping" {
		t.Fatalf("source without dynamic signing must be callable: name=%q err=%v", name, err)
	}
	if _, err := server.policy(rpcEnvelope{
		Method: "tools/call",
		Params: json.RawMessage(`{"name":"not_approved"}`),
	}); err == nil {
		t.Fatal("unapproved tool must still be rejected")
	}
}

func TestStreamSSEFlushesTailOutcomeBeforeReturn(t *testing.T) {
	writer := httptest.NewRecorder()
	outcome, err := (&Server{}).streamSSE(
		strings.NewReader(`data: {"jsonrpc":"2.0","id":1,"error":{"code":-32603}}`),
		writer,
		writer,
		"tools/call",
		json.RawMessage(`1`),
	)
	if err != nil {
		t.Fatal(err)
	}
	if !outcome.failed || outcome.rpcErrorCode != "-32603" {
		t.Fatalf("the final event without a blank line was not inspected: %+v", outcome)
	}
}

func TestStreamSSEIgnoresProgressUntilMatchingResponse(t *testing.T) {
	progressThenResult := strings.NewReader(
		"data: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\",\"params\":{}}\n\n" +
			"data: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{}}",
	)
	writer := httptest.NewRecorder()
	outcome, err := (&Server{}).streamSSE(progressThenResult, writer, writer, "tools/call", json.RawMessage(`1`))
	if err != nil {
		t.Fatal(err)
	}
	if outcome.failed || !outcome.responded {
		t.Fatalf("progress notification prevented matching result detection: %+v", outcome)
	}

	onlyProgress := strings.NewReader("data: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\",\"params\":{}}\n\n")
	writer = httptest.NewRecorder()
	outcome, err = (&Server{}).streamSSE(onlyProgress, writer, writer, "tools/call", json.RawMessage(`1`))
	if err != nil {
		t.Fatal(err)
	}
	if !outcome.failed {
		t.Fatalf("SSE without the matching response was reported as success: %+v", outcome)
	}
}

func startTestProxy(t *testing.T, options Options) (*Server, string) {
	t.Helper()
	proxy, err := New(options)
	if err != nil {
		t.Fatal(err)
	}
	if err := proxy.Start(); err != nil {
		t.Fatal(err)
	}
	host, port, err := proxy.Endpoint()
	if err != nil {
		t.Fatal(err)
	}
	return proxy, fmt.Sprintf("http://%s:%d/sources/%s/mcp", host, port, options.SourceID)
}

func postMCP(t *testing.T, endpoint, session, method string, params any) (int, string, string) {
	return postMCPWithHeaders(t, endpoint, session, method, params, nil)
}

func postMCPWithHeaders(t *testing.T, endpoint, session, method string, params any, extra map[string]string) (int, string, string) {
	t.Helper()
	payload, err := json.Marshal(map[string]any{
		"jsonrpc": "2.0", "id": 1, "method": method, "params": params,
	})
	if err != nil {
		t.Fatal(err)
	}
	request, err := http.NewRequest(http.MethodPost, endpoint, bytes.NewReader(payload))
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer edge-only-token")
	request.Header.Set("X-Edge-Token", "must-be-removed")
	request.Header.Set("appid", "forged-app")
	request.Header.Set("channel", "forged-channel")
	request.Header.Set("userid", "forged-user")
	request.Header.Set("requestId", "forged-request")
	request.Header.Set("timestamp", "1")
	request.Header.Set("sign", "forged-signature")
	if session != "" {
		request.Header.Set("Mcp-Session-Id", session)
	}
	for name, value := range extra {
		request.Header.Set(name, value)
	}
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	body, _ := io.ReadAll(response.Body)
	return response.StatusCode, response.Header.Get("Mcp-Session-Id"), string(body)
}

func countCalls(mu *sync.Mutex, counts map[string]int, method string) int {
	mu.Lock()
	defer mu.Unlock()
	return counts[method]
}

func testContext(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	t.Cleanup(cancel)
	return ctx
}
