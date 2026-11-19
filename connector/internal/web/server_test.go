package web

import (
	"encoding/json"
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
	if !strings.Contains(recorder.Body.String(), "企业统一入口候选版本") || !strings.Contains(recorder.Body.String(), "公网是否可用必须以云端 Edge") {
		t.Fatal("页面未明确展示企业桥接验收边界")
	}
	if strings.Contains(recorder.Body.String(), "启动加密通道并与云端完成端到端检测") {
		t.Fatal("页面不得把候选连接器描述成已完成端到端联调")
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
