package web

import (
	"context"
	"encoding/json"
	"fmt"
	"image/png"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/agent"
)

const loopbackHost = "127.0.0.1:17872"

func newTestServer(t *testing.T) *Server {
	t.Helper()
	instance, err := agent.New(agent.Options{DataDir: t.TempDir(), CloudBase: "http://127.0.0.1:1", EdgeBase: "https://mcp.example.com"})
	if err != nil {
		t.Fatalf("创建连接器失败: %v", err)
	}
	t.Cleanup(instance.Close)
	server, err := New(instance, loopbackHost)
	if err != nil {
		t.Fatalf("创建界面失败: %v", err)
	}
	return server
}

func newUnconfiguredTestServer(t *testing.T) *Server {
	t.Helper()
	instance, err := agent.New(agent.Options{DataDir: t.TempDir()})
	if err != nil {
		t.Fatalf("创建连接器失败: %v", err)
	}
	t.Cleanup(instance.Close)
	server, err := New(instance, loopbackHost)
	if err != nil {
		t.Fatalf("创建界面失败: %v", err)
	}
	return server
}

func TestIndexSetsSessionCookie(t *testing.T) {
	server := newTestServer(t)
	request := httptest.NewRequest(http.MethodGet, "/", nil)
	request.Host = loopbackHost
	recorder := httptest.NewRecorder()
	server.Handler().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("状态码 %d", recorder.Code)
	}
	cookies := recorder.Result().Cookies()
	if len(cookies) == 0 || cookies[0].Name != "kconn" || cookies[0].Value != server.Token {
		t.Fatalf("会话令牌未下发: %+v", cookies)
	}
	if !cookies[0].HttpOnly || cookies[0].SameSite != http.SameSiteStrictMode {
		t.Fatal("本机页面会话 Cookie 必须开启 HttpOnly 和 SameSite=Strict")
	}
	if recorder.Header().Get("Cache-Control") != "no-store" {
		t.Fatal("包含本机会话令牌的页面禁止缓存")
	}
	body := recorder.Body.String()
	if !strings.Contains(body, "内网连接器") || !strings.Contains(body, `rel="icon" href="/flintworks-mark.png"`) {
		t.Fatal("页面内容不完整")
	}
}

func TestHomeShowsMCPSetupAndLinksToSupport(t *testing.T) {
	server := newTestServer(t)
	request := httptest.NewRequest(http.MethodGet, "/", nil)
	request.Host = loopbackHost
	response := httptest.NewRecorder()
	server.Handler().ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("GET / returned %d", response.Code)
	}
	body := response.Body.String()
	previous := -1
	for _, id := range []string{"bindForm", "addressForm", "toolForm", "resultPanel"} {
		index := strings.Index(body, `id="`+id+`"`)
		if index <= previous {
			t.Fatalf("homepage missing %s or showing it out of workflow order", id)
		}
		previous = index
	}
	for _, expected := range []string{`id="upstreamAddress"`, `id="copyJSONBtn"`, `href="/support"`} {
		if !strings.Contains(body, expected) {
			t.Fatalf("homepage missing %s", expected)
		}
	}
	for _, id := range []string{"builtinDemoPanel", "logsPanel", "advancedPanel"} {
		if strings.Contains(body, `id="`+id+`"`) {
			t.Fatalf("troubleshooting panel %s should live on /support", id)
		}
	}
}

func TestSupportPageKeepsSelfTestAndLocalAccess(t *testing.T) {
	handler := newTestServer(t).Handler()
	request := httptest.NewRequest(http.MethodGet, "/support", nil)
	request.Host = "external.example"
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusForbidden {
		t.Fatalf("nonlocal Host may not access troubleshooting, got %d", response.Code)
	}

	request.Host = loopbackHost
	response = httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusOK || response.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("support response: %d, cache-control %q", response.Code, response.Header().Get("Cache-Control"))
	}
	for _, id := range []string{"selfTestBtn", "logsPanel", "advancedPanel"} {
		if !strings.Contains(response.Body.String(), `id="`+id+`"`) {
			t.Fatalf("support page missing %s", id)
		}
	}
}

func TestFlintworksMarkIsServedLocally(t *testing.T) {
	handler := newTestServer(t).Handler()
	request := httptest.NewRequest(http.MethodGet, "/flintworks-mark.png", nil)
	request.Host = loopbackHost
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK || recorder.Header().Get("Content-Type") != "image/png" {
		t.Fatalf("brand image response: %d %q", recorder.Code, recorder.Header().Get("Content-Type"))
	}
	decoded, err := png.DecodeConfig(recorder.Body)
	if err != nil || decoded.Width != 128 || decoded.Height != 128 {
		t.Fatalf("brand image has unexpected format or dimensions: %+v, %v", decoded, err)
	}

	request.Host = "external.example"
	recorder = httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusForbidden {
		t.Fatalf("nonlocal Host may not fetch the image: %d", recorder.Code)
	}
}

func TestRejectsForeignHost(t *testing.T) {
	server := newTestServer(t)
	request := httptest.NewRequest(http.MethodGet, "/", nil)
	request.Host = "evil.example.com"
	recorder := httptest.NewRecorder()
	server.Handler().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusForbidden {
		t.Fatalf("非本机 Host 必须拒绝，实际 %d", recorder.Code)
	}
}

func TestRejectsForeignOriginOnPost(t *testing.T) {
	server := newTestServer(t)
	request := httptest.NewRequest(http.MethodPost, "/api/pause", strings.NewReader("{}"))
	request.Host = loopbackHost
	request.Header.Set("X-Connector-Token", server.Token)
	request.Header.Set("Origin", "https://evil.example.com")
	recorder := httptest.NewRecorder()
	server.Handler().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusForbidden {
		t.Fatalf("外部来源必须拒绝，实际 %d", recorder.Code)
	}
}

func TestPostRequiresToken(t *testing.T) {
	server := newTestServer(t)
	request := httptest.NewRequest(http.MethodPost, "/api/import", strings.NewReader(`{"config":"{}"}`))
	request.Host = loopbackHost
	recorder := httptest.NewRecorder()
	server.Handler().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusForbidden {
		t.Fatalf("缺少令牌必须拒绝，实际 %d", recorder.Code)
	}
}

func TestAssistOfferRequiresLocalSessionToken(t *testing.T) {
	server := newTestServer(t)
	request := httptest.NewRequest(http.MethodGet, "/api/assist/offer", nil)
	request.Host = loopbackHost
	unauthorized := httptest.NewRecorder()
	server.Handler().ServeHTTP(unauthorized, request)
	if unauthorized.Code != http.StatusForbidden {
		t.Fatalf("assistance offer must require the local token: %d", unauthorized.Code)
	}

	request.Header.Set("X-Connector-Token", server.Token)
	authorized := httptest.NewRecorder()
	server.Handler().ServeHTTP(authorized, request)
	if authorized.Code != http.StatusOK || !strings.Contains(authorized.Body.String(), `"data":{"enabled":false,"offer":null}`) {
		t.Fatalf("an unbound device should show no offer: %d %s", authorized.Code, authorized.Body)
	}
}

func TestStateChangingEndpointsRejectNonPostBeforeHandler(t *testing.T) {
	server := &Server{Token: "test-token", Addr: loopbackHost}
	paths := []string{
		"/api/bind",
		"/api/import",
		"/api/source/save",
		"/api/source/save-and-connect",
		"/api/source/tools",
		"/api/source/enabled",
		"/api/builtin-demo/enabled",
		"/api/self-test",
		"/api/auth-profile",
		"/api/diagnose",
		"/api/connect",
		"/api/pause",
		"/api/disconnect",
		"/api/revoke",
		"/api/mcp-export",
		"/api/assist/start",
		"/api/assist/stop",
		"/api/diagnostics/settings",
	}
	for _, path := range paths {
		for _, method := range []string{http.MethodGet, http.MethodHead} {
			for _, token := range []string{"", server.Token} {
				request := httptest.NewRequest(method, path, nil)
				request.Host = loopbackHost
				if token != "" {
					request.Header.Set("X-Connector-Token", token)
				}
				recorder := httptest.NewRecorder()
				server.Handler().ServeHTTP(recorder, request)
				if recorder.Code != http.StatusMethodNotAllowed {
					t.Fatalf("%s %s 必须返回 405，实际 %d", method, path, recorder.Code)
				}
				if allow := recorder.Header().Get("Allow"); allow != http.MethodPost {
					t.Fatalf("%s %s 的 Allow 应为 POST，实际 %q", method, path, allow)
				}
			}
		}
	}
}

func TestStateChangingEndpointsRejectPostWithoutToken(t *testing.T) {
	server := &Server{Token: "test-token", Addr: loopbackHost}
	paths := []string{
		"/api/bind",
		"/api/import",
		"/api/source/save",
		"/api/source/save-and-connect",
		"/api/source/tools",
		"/api/source/enabled",
		"/api/builtin-demo/enabled",
		"/api/self-test",
		"/api/auth-profile",
		"/api/diagnose",
		"/api/connect",
		"/api/pause",
		"/api/disconnect",
		"/api/revoke",
		"/api/mcp-export",
		"/api/assist/start",
		"/api/assist/stop",
		"/api/diagnostics/settings",
	}
	for _, path := range paths {
		request := httptest.NewRequest(http.MethodPost, path, strings.NewReader("{}"))
		request.Host = loopbackHost
		recorder := httptest.NewRecorder()
		server.Handler().ServeHTTP(recorder, request)
		if recorder.Code != http.StatusForbidden {
			t.Fatalf("POST %s 缺少令牌必须返回 403，实际 %d", path, recorder.Code)
		}
	}
}

func TestImportWithToken(t *testing.T) {
	server := newTestServer(t)
	body := `{"config":"{\"mcpServers\":{\"库存\":{\"type\":\"http\",\"url\":\"http://10.0.0.8:8000/mcp\"}}}"}`
	request := httptest.NewRequest(http.MethodPost, "/api/import", strings.NewReader(body))
	request.Host = loopbackHost
	request.Header.Set("X-Connector-Token", server.Token)
	recorder := httptest.NewRecorder()
	server.Handler().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("导入失败: %d %s", recorder.Code, recorder.Body.String())
	}
	var payload struct {
		OK   bool `json:"ok"`
		Data struct {
			Sources []struct {
				SourceID string `json:"source_id"`
			} `json:"sources"`
		} `json:"data"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &payload); err != nil {
		t.Fatalf("响应不是 JSON: %v", err)
	}
	if !payload.OK || len(payload.Data.Sources) != 1 {
		t.Fatalf("导入结果不符: %s", recorder.Body.String())
	}
}

func TestSourceSaveWithTokenCreatesAndUpdatesOneSource(t *testing.T) {
	server := newTestServer(t)
	body, _ := json.Marshal(map[string]any{
		"display_name": "库存查询",
		"upstream_url": "http://10.0.0.8:8000/mcp",
		"bearer_token": "synthetic-access-token",
		"dynamic_auth": false,
		"tools":        []string{"get_inventory"},
	})
	request := httptest.NewRequest(http.MethodPost, "/api/source/save", strings.NewReader(string(body)))
	request.Host = loopbackHost
	request.Header.Set("X-Connector-Token", server.Token)
	recorder := httptest.NewRecorder()
	server.Handler().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("保存普通表单来源失败: %d %s", recorder.Code, recorder.Body.String())
	}
	if strings.Contains(recorder.Body.String(), "synthetic-access-token") {
		t.Fatal("来源保存响应泄漏了访问令牌")
	}
	var created struct {
		OK   bool `json:"ok"`
		Data struct {
			Created bool `json:"created"`
			Source  struct {
				SourceID string `json:"source_id"`
			} `json:"source"`
			Status struct {
				Sources []struct {
					SourceID string `json:"source_id"`
				} `json:"sources"`
			} `json:"status"`
		} `json:"data"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &created); err != nil {
		t.Fatalf("保存响应不是 JSON: %v", err)
	}
	if !created.OK || !created.Data.Created || created.Data.Source.SourceID == "" || len(created.Data.Status.Sources) != 1 {
		t.Fatalf("新建来源结果不符: %s", recorder.Body.String())
	}

	update, _ := json.Marshal(map[string]any{
		"source_id":    created.Data.Source.SourceID,
		"display_name": "库存查询（维护中）",
		"upstream_url": "http://10.0.0.8:8000/mcp",
		"dynamic_auth": false,
		"tools":        []string{"get_inventory"},
	})
	request = httptest.NewRequest(http.MethodPost, "/api/source/save", strings.NewReader(string(update)))
	request.Host = loopbackHost
	request.Header.Set("X-Connector-Token", server.Token)
	recorder = httptest.NewRecorder()
	server.Handler().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("更新普通表单来源失败: %d %s", recorder.Code, recorder.Body.String())
	}
	var updated struct {
		Data struct {
			Created bool `json:"created"`
			Status  struct {
				Sources []struct {
					DisplayName string `json:"display_name"`
				} `json:"sources"`
			} `json:"status"`
		} `json:"data"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &updated); err != nil {
		t.Fatalf("更新响应不是 JSON: %v", err)
	}
	if updated.Data.Created || len(updated.Data.Status.Sources) != 1 || updated.Data.Status.Sources[0].DisplayName != "库存查询（维护中）" {
		t.Fatalf("更新来源时不应删除或替换其他来源: %s", recorder.Body.String())
	}
}

func TestSourceEnabledEndpointTogglesWithoutDeletingSource(t *testing.T) {
	server := newTestServer(t)
	result, err := server.Agent.SaveSource(agent.SourceInput{
		DisplayName: "旧合成来源", UpstreamURL: "http://127.0.0.1:18765/mcp", Tools: []string{"lookup_order"},
	})
	if err != nil {
		t.Fatalf("准备来源失败: %v", err)
	}

	body, _ := json.Marshal(map[string]any{"source_id": result.Source.SourceID, "enabled": false})
	request := httptest.NewRequest(http.MethodPost, "/api/source/enabled", strings.NewReader(string(body)))
	request.Host = loopbackHost
	request.Header.Set("X-Connector-Token", server.Token)
	recorder := httptest.NewRecorder()
	server.Handler().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("停用来源失败: %d %s", recorder.Code, recorder.Body.String())
	}
	var payload struct {
		OK   bool `json:"ok"`
		Data struct {
			Source struct {
				SourceID string `json:"source_id"`
				Disabled bool   `json:"disabled"`
			} `json:"source"`
		} `json:"data"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &payload); err != nil {
		t.Fatalf("停用响应不是 JSON: %v", err)
	}
	if !payload.OK || payload.Data.Source.SourceID != result.Source.SourceID || !payload.Data.Source.Disabled {
		t.Fatalf("停用响应不符: %s", recorder.Body.String())
	}
	status := server.Agent.Status()
	if len(status.Sources) != 1 || !status.Sources[0].Disabled {
		t.Fatalf("停用必须保留来源并标记状态: %+v", status.Sources)
	}

	missing, _ := json.Marshal(map[string]any{"source_id": result.Source.SourceID})
	request = httptest.NewRequest(http.MethodPost, "/api/source/enabled", strings.NewReader(string(missing)))
	request.Host = loopbackHost
	request.Header.Set("X-Connector-Token", server.Token)
	recorder = httptest.NewRecorder()
	server.Handler().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusBadRequest || !strings.Contains(recorder.Body.String(), "明确指定") {
		t.Fatalf("缺少 enabled 时必须拒绝：%d %s", recorder.Code, recorder.Body.String())
	}
}

func TestSourceToolsWithTokenDiscoversOnlySupportedTools(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Authorization"); got != "Bearer synthetic-access-token" {
			http.Error(w, "authorization mismatch", http.StatusUnauthorized)
			return
		}
		var request struct {
			ID     int64  `json:"id"`
			Method string `json:"method"`
		}
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		if request.Method == "notifications/initialized" {
			w.WriteHeader(http.StatusAccepted)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		switch request.Method {
		case "initialize":
			_, _ = fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{"protocolVersion":"2025-06-18","serverInfo":{"name":"synthetic","version":"1"}}}`, request.ID)
		case "tools/list":
			_, _ = fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{"tools":[{"name":"get_inventory","title":"读取库存"},{"name":"lookup_order","description":"查询订单"}]}}`, request.ID)
		default:
			http.Error(w, "unexpected method", http.StatusBadRequest)
		}
	}))
	defer upstream.Close()

	server := newTestServer(t)
	body, _ := json.Marshal(map[string]any{
		"display_name": "库存查询",
		"upstream_url": upstream.URL,
		"bearer_token": "synthetic-access-token",
		"dynamic_auth": false,
	})
	missing := httptest.NewRequest(http.MethodPost, "/api/source/tools", strings.NewReader(
		fmt.Sprintf(`{"upstream_url":%q}`, upstream.URL)))
	missing.Host = loopbackHost
	missing.Header.Set("X-Connector-Token", server.Token)
	rejected := httptest.NewRecorder()
	server.Handler().ServeHTTP(rejected, missing)
	var failure struct{ Error struct{ Code string } }
	if json.Unmarshal(rejected.Body.Bytes(), &failure) != nil || failure.Error.Code != "UPSTREAM_UNAUTHORIZED" {
		t.Fatalf("missing upstream token should point to the local credential field: %s", rejected.Body)
	}
	request := httptest.NewRequest(http.MethodPost, "/api/source/tools", strings.NewReader(string(body)))
	request.Host = loopbackHost
	request.Header.Set("X-Connector-Token", server.Token)
	recorder := httptest.NewRecorder()
	server.Handler().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("读取工具列表失败: %d %s", recorder.Code, recorder.Body.String())
	}
	var payload struct {
		OK   bool `json:"ok"`
		Data []struct {
			Name string `json:"name"`
		} `json:"data"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &payload); err != nil {
		t.Fatalf("工具列表响应不是 JSON: %v", err)
	}
	if !payload.OK || len(payload.Data) != 2 || payload.Data[0].Name != "get_inventory" || payload.Data[1].Name != "lookup_order" {
		t.Fatalf("工具列表结果不符: %s", recorder.Body.String())
	}
	if got := len(server.Agent.Status().Sources); got != 0 {
		t.Fatalf("读取工具列表不得持久化草稿，实际 %d 个来源", got)
	}
}

func TestStatusReportsUnpaired(t *testing.T) {
	server := newTestServer(t)
	request := httptest.NewRequest(http.MethodGet, "/api/status", nil)
	request.Host = loopbackHost
	recorder := httptest.NewRecorder()
	server.Handler().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("状态码 %d", recorder.Code)
	}
	if !strings.Contains(recorder.Body.String(), "UNPAIRED") {
		t.Fatalf("初始状态应为未绑定: %s", recorder.Body.String())
	}
	if !strings.Contains(recorder.Body.String(), `"cloud_configured":true`) || !strings.Contains(recorder.Body.String(), `"edge_configured":true`) {
		t.Fatalf("状态应标明测试云端端点已配置: %s", recorder.Body.String())
	}
}

func TestStatusReportsUnconfiguredTrial(t *testing.T) {
	server := newUnconfiguredTestServer(t)
	request := httptest.NewRequest(http.MethodGet, "/api/status", nil)
	request.Host = loopbackHost
	recorder := httptest.NewRecorder()
	server.Handler().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("状态码 %d", recorder.Code)
	}
	if !strings.Contains(recorder.Body.String(), `"cloud_configured":false`) || !strings.Contains(recorder.Body.String(), `"edge_configured":false`) {
		t.Fatalf("状态应标明未配置云端端点: %s", recorder.Body.String())
	}
}

func TestConnectFailsClosedWithoutCloudRelay(t *testing.T) {
	server := newUnconfiguredTestServer(t)
	request := httptest.NewRequest(http.MethodPost, "/api/connect", strings.NewReader("{}"))
	request.Host = loopbackHost
	request.Header.Set("X-Connector-Token", server.Token)
	recorder := httptest.NewRecorder()
	server.Handler().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusBadRequest || !strings.Contains(recorder.Body.String(), "未配置云端中继") {
		t.Fatalf("未配置中继时连接必须失败关闭: %d %s", recorder.Code, recorder.Body.String())
	}
}

func TestAuthProfileEndpointDoesNotReturnSecrets(t *testing.T) {
	server := newTestServer(t)
	importBody := `{"config":"{\"mcpServers\":{\"库存\":{\"type\":\"http\",\"url\":\"http://10.0.0.8:8000/mcp\",\"headers\":{\"Authorization\":\"Bearer customer-secret\"}}}}"}`
	importRequest := httptest.NewRequest(http.MethodPost, "/api/import", strings.NewReader(importBody))
	importRequest.Host = loopbackHost
	importRequest.Header.Set("X-Connector-Token", server.Token)
	importRecorder := httptest.NewRecorder()
	server.Handler().ServeHTTP(importRecorder, importRequest)
	if importRecorder.Code != http.StatusOK {
		t.Fatalf("导入失败: %d %s", importRecorder.Code, importRecorder.Body.String())
	}
	var imported struct {
		Data struct {
			Sources []struct {
				SourceID string `json:"source_id"`
			} `json:"sources"`
		} `json:"data"`
	}
	if err := json.Unmarshal(importRecorder.Body.Bytes(), &imported); err != nil || len(imported.Data.Sources) != 1 {
		t.Fatalf("导入响应无效: %v %s", err, importRecorder.Body.String())
	}
	body, _ := json.Marshal(map[string]any{
		"source_id": imported.Data.Sources[0].SourceID,
		"app_id":    "app", "channel": "channel", "user_id": "user",
		"hmac_key": "0123456789abcdef0123456789abcdef",
		"tools":    "manual_search",
	})
	request := httptest.NewRequest(http.MethodPost, "/api/auth-profile", strings.NewReader(string(body)))
	request.Host = loopbackHost
	request.Header.Set("X-Connector-Token", server.Token)
	recorder := httptest.NewRecorder()
	server.Handler().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("认证配置失败: %d %s", recorder.Code, recorder.Body.String())
	}
	if strings.Contains(recorder.Body.String(), "0123456789abcdef") || strings.Contains(recorder.Body.String(), "customer-secret") {
		t.Fatal("接口响应泄漏秘密")
	}
}

func TestLocalLogsSettingsAndDownloadAreRedacted(t *testing.T) {
	server := newTestServer(t)
	const bearer = "SYNTHETIC_BEARER_CANARY"
	if _, err := server.Agent.SaveSource(agent.SourceInput{
		DisplayName: "合成库存",
		UpstreamURL: "http://10.19.20.21/private-mcp",
		BearerToken: bearer,
		Tools:       []string{"get_inventory"},
	}); err != nil {
		t.Fatal(err)
	}

	request := httptest.NewRequest(http.MethodGet, "/api/logs?level=info&limit=200", nil)
	request.Host = loopbackHost
	recorder := httptest.NewRecorder()
	server.Handler().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("连接器日志读取失败: %d %s", recorder.Code, recorder.Body.String())
	}
	body := recorder.Body.String()
	if !strings.Contains(body, "MCP 服务配置已保存") {
		t.Fatalf("连接器日志缺少成功事件: %s", body)
	}
	for _, marker := range []string{bearer, "10.19.20.21", "private-mcp"} {
		if strings.Contains(body, marker) {
			t.Fatalf("连接器日志泄漏标记 %q: %s", marker, body)
		}
	}

	pausedBody := strings.NewReader(`{"paused":true}`)
	request = httptest.NewRequest(http.MethodPost, "/api/diagnostics/settings", pausedBody)
	request.Host = loopbackHost
	request.Header.Set("X-Connector-Token", server.Token)
	recorder = httptest.NewRecorder()
	server.Handler().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK || !strings.Contains(recorder.Body.String(), `"paused":true`) {
		t.Fatalf("暂停诊断上报失败: %d %s", recorder.Code, recorder.Body.String())
	}

	request = httptest.NewRequest(http.MethodGet, "/api/logs/download?level=info", nil)
	request.Host = loopbackHost
	recorder = httptest.NewRecorder()
	server.Handler().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("下载脱敏日志失败: %d %s", recorder.Code, recorder.Body.String())
	}
	if disposition := recorder.Header().Get("Content-Disposition"); !strings.Contains(disposition, "attachment") {
		t.Fatalf("下载响应缺少 attachment: %q", disposition)
	}
	download := recorder.Body.String()
	if !strings.Contains(download, "connector_events") || !strings.Contains(download, "diagnostics") {
		t.Fatalf("下载内容缺少双日志结构: %s", download)
	}
	if strings.Contains(download, bearer) || strings.Contains(download, "10.19.20.21") {
		t.Fatal("下载脱敏日志泄漏凭据或内部地址")
	}
}

func TestPlatformLogsStayBehindLocalDeviceCredential(t *testing.T) {
	const (
		connectorID  = "conn_33333333333333333333333333333333"
		deviceSecret = "SYNTHETIC_DEVICE_SECRET_CANARY"
	)
	authorization := make(chan string, 1)
	control := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.URL.Path == "/v1/enrollments/redeem":
			_, _ = fmt.Fprintf(w, `{"connector_id":%q,"tenant_id":"tenant-one","tenant_name":"测试企业","device_token":%q,"relay":{}}`, connectorID, deviceSecret)
		case r.URL.Path == "/v1/connectors/"+connectorID+"/platform-events":
			authorization <- r.Header.Get("Authorization")
			_, _ = fmt.Fprintf(w, `{"events":[{"schema_version":1,"event_id":"evt_44444444444444444444444444444444","occurred_at":%q,"level":"error","component":"platform","event_code":"platform_request_failed","message_code":"platform_request_failed","trace_id":"kwtr_55555555555555555555555555555555","http_status":502,"message":"<img src=x onerror=alert(1)>"}]}`, time.Now().UTC().Format(time.RFC3339Nano))
		default:
			http.NotFound(w, r)
		}
	}))
	defer control.Close()

	instance, err := agent.New(agent.Options{
		DataDir:   t.TempDir(),
		CloudBase: control.URL,
		EdgeBase:  "https://mcp.example.com",
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(instance.Close)
	if _, err := instance.Bind(context.Background(), "synthetic-code"); err != nil {
		t.Fatal(err)
	}
	server, err := New(instance, loopbackHost)
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodGet, "/api/platform-logs?limit=200", nil)
	request.Host = loopbackHost
	recorder := httptest.NewRecorder()
	server.Handler().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("开物调用日志读取失败: %d %s", recorder.Code, recorder.Body.String())
	}
	body := recorder.Body.String()
	select {
	case got := <-authorization:
		if got != "Bearer "+deviceSecret {
			t.Fatalf("后端没有使用设备身份查询: %q", got)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("平台日志接口没有被调用")
	}
	if !strings.Contains(body, "开物平台调用已到达但未成功") || !strings.Contains(body, `"available":true`) {
		t.Fatalf("平台日志未按固定目录展示: %s", body)
	}
	for _, marker := range []string{deviceSecret, "<img", "onerror", "tenant-one"} {
		if strings.Contains(body, marker) {
			t.Fatalf("浏览器响应泄漏或透传了不允许的值 %q: %s", marker, body)
		}
	}
}

func TestLocalLogEndpointsRejectForeignOriginAndWrongMethods(t *testing.T) {
	server := newTestServer(t)
	for _, path := range []string{"/api/logs", "/api/platform-logs", "/api/logs/download"} {
		request := httptest.NewRequest(http.MethodGet, path, nil)
		request.Host = loopbackHost
		request.Header.Set("Origin", "https://evil.example.com")
		recorder := httptest.NewRecorder()
		server.Handler().ServeHTTP(recorder, request)
		if recorder.Code != http.StatusForbidden {
			t.Fatalf("%s 外部 Origin 必须拒绝，实际 %d", path, recorder.Code)
		}

		request = httptest.NewRequest(http.MethodPost, path, strings.NewReader("{}"))
		request.Host = loopbackHost
		request.Header.Set("X-Connector-Token", server.Token)
		recorder = httptest.NewRecorder()
		server.Handler().ServeHTTP(recorder, request)
		if recorder.Code != http.StatusMethodNotAllowed {
			t.Fatalf("%s 非 GET 必须返回 405，实际 %d", path, recorder.Code)
		}
	}
	request := httptest.NewRequest(http.MethodGet, "/api/diagnostics/settings", nil)
	request.Host = loopbackHost
	request.Header.Set("X-Connector-Token", server.Token)
	recorder := httptest.NewRecorder()
	server.Handler().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusMethodNotAllowed {
		t.Fatalf("诊断设置非 POST 必须返回 405，实际 %d", recorder.Code)
	}
}

func TestSelfTestRunsLocalBuiltinMCPWithoutBinding(t *testing.T) {
	server := newTestServer(t)
	request := httptest.NewRequest(http.MethodPost, "/api/self-test", strings.NewReader("{}"))
	request.Host = loopbackHost
	request.Header.Set("X-Connector-Token", server.Token)
	recorder := httptest.NewRecorder()
	server.Handler().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("self-test status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	var response struct {
		OK   bool `json:"ok"`
		Data struct {
			OK     bool   `json:"ok"`
			Source string `json:"source_id"`
			Stages []struct {
				ID     string `json:"id"`
				Status string `json:"status"`
			} `json:"stages"`
		} `json:"data"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if !response.OK || response.Data.OK || response.Data.Source == "" {
		t.Fatalf("unbound self-test must return a layered non-green result: %s", recorder.Body.String())
	}
	statuses := map[string]string{}
	for _, stage := range response.Data.Stages {
		statuses[stage.ID] = stage.Status
	}
	if statuses["local_mcp"] != "passed" || statuses["enterprise_binding"] != "skipped" ||
		statuses["tunnel"] != "skipped" {
		t.Fatalf("unexpected layered result: %s", recorder.Body.String())
	}
}

func TestBuiltinDemoToggleIsIndependent(t *testing.T) {
	server := newTestServer(t)
	request := httptest.NewRequest(http.MethodPost, "/api/builtin-demo/enabled", strings.NewReader(`{"enabled":false}`))
	request.Host = loopbackHost
	request.Header.Set("X-Connector-Token", server.Token)
	recorder := httptest.NewRecorder()
	server.Handler().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("toggle status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	status := server.Agent.Status()
	if status.BuiltinDemo.Enabled || status.BuiltinDemo.Running {
		t.Fatalf("built-in demo did not stop independently: %+v", status.BuiltinDemo)
	}
}
