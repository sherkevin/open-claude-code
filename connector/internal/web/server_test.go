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
}
