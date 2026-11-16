package cloud

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

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
					"source_id": "src_1", "display_name": "库存", "local_host": "10.0.0.8", "local_port": 8000, "stcp_secret": "s3",
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
	if desired.Revision != 3 || len(desired.Sources) != 1 || desired.Sources[0].LocalPort != 8000 {
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
