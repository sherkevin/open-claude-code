package agent

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func fakeCloud(t *testing.T) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/v1/enrollments/redeem":
			json.NewEncoder(w).Encode(map[string]any{
				"connector_id": "conn_1", "tenant_id": "tenant_1", "tenant_name": "示例企业",
				"device_token": "device-token",
				"relay":        map[string]any{"address": "relay.example.com", "port": 7443, "user": "tenant_1", "token": "relay-token"},
			})
		case "/v1/connectors/conn_1/desired-state":
			json.NewEncoder(w).Encode(map[string]any{
				"revision": 1,
				"relay":    map[string]any{"address": "relay.example.com", "port": 7443, "user": "tenant_1", "token": "relay-token"},
				"sources": []map[string]any{{
					"source_id": "mcp", "display_name": "库存", "local_host": "10.0.0.8", "local_port": 8000, "stcp_secret": "s1",
				}},
			})
		default:
			http.NotFound(w, r)
		}
	}))
}

func TestBindImportExport(t *testing.T) {
	server := fakeCloud(t)
	defer server.Close()

	instance, err := New(Options{DataDir: t.TempDir(), CloudBase: server.URL, EdgeBase: "https://mcp.example.com"})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()

	status, err := instance.Bind(ctx, "code-1")
	if err != nil {
		t.Fatalf("绑定失败: %v", err)
	}
	if !status.Bound || status.ConnectorID != "conn_1" || status.State != "CONFIGURING" {
		t.Fatalf("绑定后状态不符: %+v", status)
	}

	raw := []byte(`{"mcpServers":{"inventory":{"type":"http","url":"http://10.0.0.8:8000/mcp"}}}`)
	report, err := instance.ImportConfig(ctx, raw)
	if err != nil {
		t.Fatalf("导入失败: %v", err)
	}
	if report.HasErrors() || len(report.Sources) != 1 {
		t.Fatalf("导入结果不符: %+v", report)
	}

	exported, err := instance.Export()
	if err != nil {
		t.Fatalf("导出失败: %v", err)
	}
	if !strings.Contains(string(exported), "https://mcp.example.com/sources/inventory/mcp") {
		t.Fatalf("导出地址不符: %s", exported)
	}

	// 重新载入后应保留绑定与来源。
	reloaded, err := New(Options{DataDir: instance.opts.DataDir, CloudBase: server.URL, EdgeBase: "https://mcp.example.com"})
	if err != nil {
		t.Fatal(err)
	}
	snapshot := reloaded.Status()
	if !snapshot.Bound || len(snapshot.Sources) != 1 {
		t.Fatalf("重启后状态丢失: %+v", snapshot)
	}
}

func TestAutoConnectIntentPersistence(t *testing.T) {
	server := fakeCloud(t)
	defer server.Close()
	dir := t.TempDir()
	instance, err := New(Options{DataDir: dir, CloudBase: server.URL})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if _, err := instance.Bind(ctx, "code-1"); err != nil {
		t.Fatal(err)
	}
	if _, err := instance.ImportConfig(ctx, []byte(`{"mcpServers":{"inventory":{"type":"http","url":"http://10.0.0.8:8000/mcp"}}}`)); err != nil {
		t.Fatal(err)
	}
	if instance.ShouldAutoConnect() {
		t.Fatal("首次配置后不应自动连接")
	}

	savedState := filepath.Join(dir, "state.json")
	if err := os.WriteFile(savedState, []byte(`{"state":"READY"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	reloaded, err := New(Options{DataDir: dir, CloudBase: server.URL})
	if err != nil {
		t.Fatal(err)
	}
	if !reloaded.ShouldAutoConnect() {
		t.Fatal("上次为就绪状态时应自动恢复通道")
	}

	if err := os.WriteFile(savedState, []byte(`{"state":"PAUSED"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	paused, err := New(Options{DataDir: dir, CloudBase: server.URL})
	if err != nil {
		t.Fatal(err)
	}
	if paused.ShouldAutoConnect() {
		t.Fatal("人工暂停后重启不应自动连接")
	}
}

func TestConnectFailsWithoutFrpc(t *testing.T) {
	server := fakeCloud(t)
	defer server.Close()
	instance, err := New(Options{DataDir: t.TempDir(), CloudBase: server.URL, EdgeBase: "https://mcp.example.com"})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if _, err := instance.Bind(ctx, "code-1"); err != nil {
		t.Fatal(err)
	}
	if _, err := instance.ImportConfig(ctx, []byte(`{"mcpServers":{"库存":{"type":"http","url":"http://10.0.0.8:8000/mcp"}}}`)); err != nil {
		t.Fatal(err)
	}
	if _, err := instance.Connect(ctx); err == nil {
		t.Fatal("缺少 frpc 时应失败")
	}
	if got := instance.Status().State; got != "DEGRADED_RELAY" {
		t.Fatalf("应进入公网通道不可用状态，实际 %s", got)
	}
}
