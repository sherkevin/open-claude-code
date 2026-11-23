package cloud

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"
)

type testSigner struct {
	method string
	path   string
	body   []byte
}

func (s *testSigner) KeyID() string { return "device-key-1" }

func (s *testSigner) SignRequest(method, path string, body []byte) (string, string, string, error) {
	s.method = method
	s.path = path
	s.body = append([]byte(nil), body...)
	return "1700000000", "synthetic-nonce-123", "synthetic-signature", nil
}

func TestRedeemAndDesiredState(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/enrollments/redeem":
			if r.Method != http.MethodPost {
				t.Errorf("兑换必须使用 POST")
			}
			w.Header().Set("Content-Type", "application/json")
			json.NewEncoder(w).Encode(map[string]any{
				"connector_id": "conn_1", "tenant_id": "tenant_1", "tenant_name": "示例企业",
				"device_token": "device-token",
				"relay":        map[string]any{"address": "relay.example.com", "port": 7443, "user": "tenant_1", "token": "relay-token"},
			})
		case "/v1/connectors/conn_1/desired-state":
			if got := r.Header.Get("Authorization"); got != "Bearer device-token" {
				t.Errorf("设备令牌未发送: %q", got)
			}
			json.NewEncoder(w).Encode(map[string]any{
				"revision": 3,
				"sources": []map[string]any{{
					"source_id": "src_1", "display_name": "库存", "stcp_secret": "s3",
				}},
			})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client, err := New(server.URL, "")
	if err != nil {
		t.Fatal(err)
	}
	identity, err := client.Redeem(context.Background(), "code-1", "")
	if err != nil {
		t.Fatalf("兑换失败: %v", err)
	}
	if identity.ConnectorID != "conn_1" || identity.DeviceToken != "device-token" {
		t.Fatalf("身份解析错误: %+v", identity)
	}

	authed, _ := New(server.URL, identity.DeviceToken)
	desired, err := authed.DesiredState(context.Background(), "conn_1")
	if err != nil {
		t.Fatalf("拉取期望状态失败: %v", err)
	}
	if desired.Revision != 3 || len(desired.Sources) != 1 || desired.Sources[0].SourceID != "src_1" {
		t.Fatalf("期望状态解析错误: %+v", desired)
	}
}

func TestAPIErrorParsing(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusConflict)
		json.NewEncoder(w).Encode(map[string]any{"error": map[string]any{
			"code": "SOURCE_CONFIG_CONFLICT", "message": "配置版本已变化", "trace_id": "trace_1", "retryable": false,
		}})
	}))
	defer server.Close()
	client, _ := New(server.URL, "token")
	err := client.Heartbeat(context.Background(), "conn_1", Heartbeat{State: "READY"})
	apiErr, ok := err.(*APIError)
	if !ok {
		t.Fatalf("应返回结构化错误: %T %v", err, err)
	}
	if apiErr.Code != "SOURCE_CONFIG_CONFLICT" || apiErr.TraceID != "trace_1" || apiErr.Status != 409 {
		t.Fatalf("错误字段不符: %+v", apiErr)
	}
}

func TestRejectsInvalidBaseURL(t *testing.T) {
	if _, err := New("not-a-url", ""); err == nil {
		t.Fatal("非法地址应被拒绝")
	}
}

func TestDeviceClientSignsExactMethodPathAndBody(t *testing.T) {
	signer := &testSigner{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("X-Kaiwu-Device-Key"); got != "device-key-1" {
			t.Errorf("设备密钥编号缺失: %q", got)
		}
		if got := r.Header.Get("X-Kaiwu-Timestamp"); got != "1700000000" {
			t.Errorf("时间戳缺失: %q", got)
		}
		if got := r.Header.Get("X-Kaiwu-Nonce"); got != "synthetic-nonce-123" {
			t.Errorf("nonce 缺失: %q", got)
		}
		if got := r.Header.Get("X-Kaiwu-Signature"); got != "synthetic-signature" {
			t.Errorf("签名缺失: %q", got)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"revision":1,"sources":[],"revoked":false,"draining":false}`))
	}))
	defer server.Close()

	client, err := NewWithSigner(server.URL+"/api/kaiwu", "device-token", signer)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.DesiredState(context.Background(), "conn_1"); err != nil {
		t.Fatal(err)
	}
	if signer.method != http.MethodGet {
		t.Fatalf("签名方法不符: %s", signer.method)
	}
	if signer.path != "/api/kaiwu/v1/connectors/conn_1/desired-state" {
		t.Fatalf("签名路径不符: %s", signer.path)
	}
	if len(signer.body) != 0 {
		t.Fatalf("GET 签名正文应为空: %q", signer.body)
	}
}

func TestNativeRelaySignsSecureUpgradeAndDoesNotFollowRedirects(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	signer := &testSigner{}
	const path = "/api/kaiwu/v1/connectors/conn_1/native-relay"
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/redirected" {
			t.Error("relay upgrade followed a redirect")
			return
		}
		if r.URL.Path != path || r.Method != http.MethodGet || r.Header.Get("Authorization") != "Bearer device-token" || r.Header.Get("Origin") != "" {
			t.Error("relay upgrade request did not use the expected secure endpoint and device identity")
		}
		if r.Header.Get("X-Kaiwu-Device-Key") != signer.KeyID() || r.Header.Get("X-Kaiwu-Signature") != "synthetic-signature" {
			t.Error("missing signature on relay upgrade")
		}
		if r.URL.RawQuery != "" {
			t.Error("relay credentials must not appear in the URL")
		}
		conn, err := websocket.Accept(w, r, nil)
		if err != nil {
			t.Error(err)
			return
		}
		defer conn.CloseNow()
		if err := wsjson.Write(ctx, conn, map[string]string{"type": "ready"}); err != nil {
			t.Error(err)
		}
	}))
	defer server.Close()
	client, err := NewWithSigner(server.URL+"/api/kaiwu", "device-token", signer)
	if err != nil {
		t.Fatal(err)
	}
	client.HTTP = server.Client()
	conn, err := client.DialNativeRelay(ctx, "conn_1")
	if err != nil {
		t.Fatal(err)
	}
	var ready map[string]string
	if err := wsjson.Read(ctx, conn, &ready); err != nil || ready["type"] != "ready" {
		t.Fatalf("expected ready from TLS relay: %v", err)
	}
	_ = conn.CloseNow()
	if signer.method != http.MethodGet || signer.path != path || len(signer.body) != 0 {
		t.Fatalf("wrong signed WebSocket request: %s %s", signer.method, signer.path)
	}

	redirect := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, server.URL+"/redirected", http.StatusTemporaryRedirect)
	}))
	defer redirect.Close()
	client.BaseURL, client.HTTP = redirect.URL+"/api/kaiwu", redirect.Client()
	if _, err := client.DialNativeRelay(ctx, "conn_1"); err == nil || !strings.Contains(err.Error(), "HTTP 307") {
		t.Fatalf("expected upgrade redirect to be rejected, got %v", err)
	}
	client.BaseURL = strings.Replace(server.URL, "https://", "http://", 1)
	if _, err := client.DialNativeRelay(ctx, "conn_1"); err == nil {
		t.Fatal("unencrypted relay upgrade was accepted")
	}
}

func TestDesiredStateStrictlyRejectsLocalTargetFields(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"revision":1,"sources":[{"source_id":"src_1","display_name":"源","local_host":"10.0.0.8","local_port":80}],"revoked":false,"draining":false}`))
	}))
	defer server.Close()
	client, _ := New(server.URL, "device-token")
	if _, err := client.DesiredState(context.Background(), "conn_1"); err == nil || !strings.Contains(err.Error(), "unknown field") {
		t.Fatalf("含内网目标字段的期望状态必须拒绝: %v", err)
	}
}

func TestHeartbeatKeepsExplicitEmptyFields(t *testing.T) {
	var payload map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatal(err)
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()
	client, _ := New(server.URL, "device-token")
	err := client.Heartbeat(context.Background(), "conn_1", Heartbeat{
		State:      "READY",
		Version:    "test",
		ObservedAt: time.Unix(1, 0).UTC(),
		SourceIDs:  []string{},
		LastError:  "",
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := payload["source_ids"]; !ok {
		t.Fatal("空 source_ids 必须显式上报")
	}
	if _, ok := payload["last_error"]; !ok {
		t.Fatal("空 last_error 必须显式上报")
	}
}

func TestRegisterSourceSendsMetadataOnlyAndRevokeUsesSelfRoute(t *testing.T) {
	var sourceBody map[string]any
	var revokePath string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/sources" {
			_ = json.NewDecoder(r.Body).Decode(&sourceBody)
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"ok":true}`))
			return
		}
		if strings.HasSuffix(r.URL.Path, "/self-revoke") {
			revokePath = r.URL.Path
			_, _ = w.Write([]byte(`{"revoked":true}`))
			return
		}
		http.NotFound(w, r)
	}))
	defer server.Close()
	client, _ := New(server.URL, "device-token")
	_, err := client.RegisterSource(context.Background(), SourceRegistration{
		SourceID:            "src_1",
		DisplayName:         "销售只读",
		AllowedTools:        []string{"query_orders"},
		ToolSchemaSHA256:    strings.Repeat("a", 64),
		TargetBindingSHA256: strings.Repeat("b", 64),
		Transport:           "streamable_http",
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, forbidden := range []string{"upstream_url", "host", "port", "path", "credential"} {
		if _, exists := sourceBody[forbidden]; exists {
			t.Fatalf("来源登记不得上传 %s: %+v", forbidden, sourceBody)
		}
	}
	if err := client.Revoke(context.Background(), "conn_1"); err != nil {
		t.Fatal(err)
	}
	if revokePath != "/v1/connectors/conn_1/self-revoke" {
		t.Fatalf("撤销路径不符合设备平面: %s", revokePath)
	}
}
