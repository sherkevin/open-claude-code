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

	"github.com/sherkevin/open-claude-code/connector/internal/config"
	"github.com/sherkevin/open-claude-code/connector/internal/huaweiauth"
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
	sourceID := report.Sources[0].SourceID
	if !isOpaqueSourceID(sourceID) {
		t.Fatalf("来源编号不是 128 位不透明编号: %s", sourceID)
	}

	exported, err := instance.Export()
	if err != nil {
		t.Fatalf("导出失败: %v", err)
	}
	if !strings.Contains(string(exported), "https://mcp.example.com/sources/"+sourceID+"/mcp") {
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

func TestAssignSourceIDsPreservesIDAcrossDisplayNameChange(t *testing.T) {
	existing := []config.Source{{
		SourceID: "src_00112233445566778899aabbccddeeff", DisplayName: "库存旧名称",
		UpstreamURL: "https://mcp.example.com/inventory",
	}}
	incoming := []config.Source{{
		SourceID: "inventory", DisplayName: "库存新名称",
		UpstreamURL: "https://mcp.example.com/inventory",
	}}
	secrets := map[string]map[string]string{
		"inventory": {"Authorization": "Bearer synthetic-token"},
	}

	got, gotSecrets, err := assignSourceIDs(incoming, secrets, existing)
	if err != nil {
		t.Fatal(err)
	}
	if got[0].SourceID != existing[0].SourceID {
		t.Fatalf("显示名变化不应改变来源编号: got=%s want=%s", got[0].SourceID, existing[0].SourceID)
	}
	if gotSecrets[existing[0].SourceID]["Authorization"] != "Bearer synthetic-token" {
		t.Fatal("来源秘密未迁移到保留后的来源编号")
	}
}

func TestAssignSourceIDsRotatesIDWhenUpstreamChanges(t *testing.T) {
	existing := []config.Source{{
		SourceID: "src_00112233445566778899aabbccddeeff", DisplayName: "库存",
		UpstreamURL: "https://mcp.example.com/inventory",
	}}
	incoming := []config.Source{{
		SourceID: "inventory", DisplayName: "库存",
		UpstreamURL: "https://mcp.example.com/inventory-v2",
	}}

	got, _, err := assignSourceIDs(incoming, nil, existing)
	if err != nil {
		t.Fatal(err)
	}
	if got[0].SourceID == existing[0].SourceID {
		t.Fatal("上游地址变化必须生成新的来源编号")
	}
	if !isOpaqueSourceID(got[0].SourceID) {
		t.Fatalf("新来源编号无效: %s", got[0].SourceID)
	}
}

func TestAssignSourceIDsDoesNotGuessForDuplicateURLs(t *testing.T) {
	existing := []config.Source{
		{SourceID: "src_00112233445566778899aabbccddeeff", DisplayName: "库存甲", UpstreamURL: "https://mcp.example.com/shared"},
		{SourceID: "src_ffeeddccbbaa99887766554433221100", DisplayName: "库存乙", UpstreamURL: "https://mcp.example.com/shared"},
	}
	incoming := []config.Source{{
		SourceID: "inventory", DisplayName: "库存丙", UpstreamURL: "https://mcp.example.com/shared",
	}}

	got, _, err := assignSourceIDs(incoming, nil, existing)
	if err != nil {
		t.Fatal(err)
	}
	for _, source := range existing {
		if got[0].SourceID == source.SourceID {
			t.Fatal("同一地址存在多个旧来源时不能猜测并复用其中任意编号")
		}
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

func TestCloudActionsFailClosedWithoutConfiguredRelay(t *testing.T) {
	instance, err := New(Options{DataDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	status := instance.Status()
	if status.CloudConfigured || status.EdgeConfigured || instance.CloudReadyForTrial() {
		t.Fatalf("默认地址不得启用云端试用操作: %+v", status)
	}
	if _, err := instance.Bind(context.Background(), "code-1"); err == nil || !strings.Contains(err.Error(), "未配置云端控制面") {
		t.Fatalf("未配置控制面时应拒绝绑定，实际 %v", err)
	}
	if _, err := instance.Connect(context.Background()); err == nil || !strings.Contains(err.Error(), "未配置云端中继") {
		t.Fatalf("未配置中继时应拒绝连接，实际 %v", err)
	}
}

func TestCloudActionsEnabledOnlyWithRealControlPlaneAndEdge(t *testing.T) {
	instance, err := New(Options{
		DataDir:   t.TempDir(),
		CloudBase: "https://control.example.net",
		EdgeBase:  "https://edge.example.net",
	})
	if err != nil {
		t.Fatal(err)
	}
	status := instance.Status()
	if !status.CloudConfigured || !status.EdgeConfigured || !instance.CloudReadyForTrial() {
		t.Fatalf("有效 HTTPS 端点应启用云端试用操作: %+v", status)
	}

	insecure, err := New(Options{
		DataDir:   t.TempDir(),
		CloudBase: "http://control.example.net",
		EdgeBase:  "http://edge.example.net",
	})
	if err != nil {
		t.Fatal(err)
	}
	status = insecure.Status()
	if status.CloudConfigured || status.EdgeConfigured || insecure.CloudReadyForTrial() {
		t.Fatalf("非回环明文端点不得启用云端试用操作: %+v", status)
	}
}

func TestAuthProfilePersistsWithoutExposingSecret(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"jsonrpc":"2.0","id":1,"result":{}}`))
	}))
	defer upstream.Close()
	dir := t.TempDir()
	instance, err := New(Options{DataDir: dir, CloudBase: "http://127.0.0.1:1", EdgeBase: "https://mcp.example.com"})
	if err != nil {
		t.Fatal(err)
	}
	raw := []byte(`{"mcpServers":{"销售查询":{"type":"http","url":"` + upstream.URL + `/mcp","headers":{"Authorization":"Bearer customer-secret"}}}}`)
	report, err := instance.ImportConfig(context.Background(), raw)
	if err != nil || report.HasErrors() {
		t.Fatalf("导入失败: %v %+v", err, report)
	}
	sourceID := report.Sources[0].SourceID
	secret := "0123456789abcdef0123456789abcdef"
	status, err := instance.SetAuthProfile(sourceID, huaweiauth.Profile{
		AppID: "app", Channel: "channel", UserID: "user", HMACKey: secret,
	}, []string{"manual_search"})
	if err != nil {
		t.Fatal(err)
	}
	if len(status.AuthProfiles) != 1 || !status.AuthProfiles[0].Configured ||
		len(status.AuthProfiles[0].AllowedTools) != 1 || status.AuthProfiles[0].AllowedTools[0] != "manual_search" {
		t.Fatalf("认证状态不符: %+v", status.AuthProfiles)
	}
	statusJSON, _ := json.Marshal(status)
	if strings.Contains(string(statusJSON), secret) || strings.Contains(string(statusJSON), "customer-secret") {
		t.Fatal("状态响应不能包含客户秘密")
	}

	reloaded, err := New(Options{DataDir: dir, CloudBase: "http://127.0.0.1:1", EdgeBase: "https://mcp.example.com"})
	if err != nil {
		t.Fatal(err)
	}
	reloadedStatus := reloaded.Status()
	if len(reloadedStatus.AuthProfiles) != 1 || !reloadedStatus.AuthProfiles[0].Configured {
		t.Fatalf("重启后认证配置丢失: %+v", reloadedStatus.AuthProfiles)
	}
	if err := reloaded.startAuthProxies(); err != nil {
		t.Fatalf("启动来源代理失败: %v", err)
	}
	defer reloaded.stopAuthProxies()
	reloaded.mu.RLock()
	proxy := reloaded.proxies[sourceID]
	reloaded.mu.RUnlock()
	if proxy == nil {
		t.Fatal("来源代理未创建")
	}
	host, port, err := proxy.Endpoint()
	if err != nil || host != "127.0.0.1" || port <= 0 {
		t.Fatalf("来源代理不是回环地址: host=%s port=%d err=%v", host, port, err)
	}
}
