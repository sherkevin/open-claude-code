package web

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/sherkevin/open-claude-code/connector/internal/agent"
)

const loopbackHost = "127.0.0.1:17872"

func newTestServer(t *testing.T) *Server {
	t.Helper()
	instance, err := agent.New(agent.Options{DataDir: t.TempDir(), CloudBase: "http://127.0.0.1:1", EdgeBase: "https://mcp.example.com"})
	if err != nil {
		t.Fatalf("创建连接器失败: %v", err)
	}
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
	if !strings.Contains(recorder.Body.String(), "内网连接器") {
		t.Fatal("页面内容不完整")
	}
	for _, expected := range []string{
		"安装后填写本机 MCP 参数即可接入",
		"保存并连接",
		"读取工具列表",
		"企业集成凭据 K",
		"不包含 Authorization，也不是可以直接导入开物平台的完整 MCP JSON",
	} {
		if !strings.Contains(recorder.Body.String(), expected) {
			t.Fatalf("页面缺少普通接入流程或交付边界文案: %s", expected)
		}
	}
	if strings.Contains(recorder.Body.String(), "第 1 步") {
		t.Fatal("页面不应继续把本机配置展示成六步技术向导")
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

func TestStateChangingEndpointsRejectNonPostBeforeHandler(t *testing.T) {
	server := &Server{Token: "test-token", Addr: loopbackHost}
	paths := []string{
		"/api/bind",
		"/api/import",
		"/api/source/save",
		"/api/source/tools",
		"/api/auth-profile",
		"/api/diagnose",
		"/api/connect",
		"/api/pause",
		"/api/disconnect",
		"/api/revoke",
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
		"/api/source/tools",
		"/api/auth-profile",
		"/api/diagnose",
		"/api/connect",
		"/api/pause",
		"/api/disconnect",
		"/api/revoke",
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
