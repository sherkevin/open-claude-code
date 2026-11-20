package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/cloud"
	"github.com/sherkevin/open-claude-code/connector/internal/config"
	"github.com/sherkevin/open-claude-code/connector/internal/huaweiauth"
	"github.com/sherkevin/open-claude-code/connector/internal/mcp"
	"github.com/sherkevin/open-claude-code/connector/internal/state"
	"github.com/sherkevin/open-claude-code/connector/internal/supervisor"
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

func fakeMCPToolServer(t *testing.T, toolsJSON string) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
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
			_, _ = fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{"protocolVersion":"2025-06-18","serverInfo":{"name":"synthetic-mcp","version":"1.0"}}}`, request.ID)
		case "tools/list":
			_, _ = fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{"tools":%s}}`, request.ID, toolsJSON)
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

func TestReimportPreservesApprovedToolAllowlist(t *testing.T) {
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
	first, err := instance.ImportConfig(context.Background(), raw)
	if err != nil || first.HasErrors() {
		t.Fatalf("首次导入失败: %v %+v", err, first)
	}
	sourceID := first.Sources[0].SourceID
	if _, err := instance.SetAuthProfile(sourceID, huaweiauth.Profile{
		AppID: "app", Channel: "channel", UserID: "user", HMACKey: "0123456789abcdef0123456789abcdef",
	}, []string{"manual_search"}); err != nil {
		t.Fatal(err)
	}

	second, err := instance.ImportConfig(context.Background(), raw)
	if err != nil || second.HasErrors() {
		t.Fatalf("重复导入失败: %v %+v", err, second)
	}
	if second.Sources[0].SourceID != sourceID {
		t.Fatalf("同一上游 URL 的来源编号发生变化: got=%s want=%s", second.Sources[0].SourceID, sourceID)
	}
	if got := second.Sources[0].ToolAllowlist; len(got) != 1 || got[0] != "manual_search" {
		t.Fatalf("重复导入不得清空工具允许列表: %+v", got)
	}
	status := instance.Status()
	if len(status.AuthProfiles) != 1 || !status.AuthProfiles[0].Configured ||
		len(status.AuthProfiles[0].AllowedTools) != 1 || status.AuthProfiles[0].AllowedTools[0] != "manual_search" {
		t.Fatalf("重复导入后认证状态不一致: %+v", status.AuthProfiles)
	}

	reloaded, err := New(Options{DataDir: dir, CloudBase: "http://127.0.0.1:1", EdgeBase: "https://mcp.example.com"})
	if err != nil {
		t.Fatal(err)
	}
	reloadedStatus := reloaded.Status()
	if len(reloadedStatus.AuthProfiles) != 1 ||
		len(reloadedStatus.AuthProfiles[0].AllowedTools) != 1 ||
		reloadedStatus.AuthProfiles[0].AllowedTools[0] != "manual_search" {
		t.Fatalf("重启后工具允许列表丢失: %+v", reloadedStatus.AuthProfiles)
	}
}

func TestCorrectAuthorizationSurvivesReimportAndRestart(t *testing.T) {
	dir := t.TempDir()
	instance, err := New(Options{DataDir: dir, CloudBase: "http://127.0.0.1:1", EdgeBase: "https://mcp.example.com"})
	if err != nil {
		t.Fatal(err)
	}
	correct := []byte(`{"mcpServers":{"销售查询":{"type":"http","url":"http://127.0.0.1:28765/mcp","headers":{"Authorization":"Bearer customer-secret"}}}}`)
	wrong := []byte(`{"mcpServers":{"销售查询":{"type":"http","url":"http://127.0.0.1:28765/mcp","headers":{"Authorization":"Bearer wrong-token"}}}}`)
	for index, raw := range [][]byte{correct, wrong, correct} {
		report, err := instance.ImportConfig(context.Background(), raw)
		if err != nil || report.HasErrors() {
			t.Fatalf("第 %d 次导入失败: %v %+v", index+1, err, report)
		}
	}

	reloaded, err := New(Options{DataDir: dir, CloudBase: "http://127.0.0.1:1", EdgeBase: "https://mcp.example.com"})
	if err != nil {
		t.Fatal(err)
	}
	reloaded.mu.RLock()
	sourceID := reloaded.config.Sources[0].SourceID
	got := reloaded.secrets[sourceID]["Authorization"]
	reloaded.mu.RUnlock()
	if got != "Bearer customer-secret" {
		t.Fatalf("重启后 Authorization 不是最后一次导入的正确值: %q", got)
	}
}

func TestRevokeIsLocalFirstAndTombstoneSurvivesCloudFailure(t *testing.T) {
	var revokeCalls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/v1/enrollments/redeem":
			_, _ = w.Write([]byte(`{"connector_id":"conn_revoke","tenant_id":"tenant_1","tenant_name":"合成企业","device_token":"device-token","device_key_id":"key_1","relay":{"address":"relay.example.com","port":7443,"user":"conn_revoke","token":"relay-token"}}`))
		case "/v1/connectors/conn_revoke/self-revoke":
			revokeCalls.Add(1)
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte(`{"error":{"code":"CONTROL_UNAVAILABLE","message":"合成故障"}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	dir := t.TempDir()
	instance, err := New(Options{
		DataDir: dir, CloudBase: server.URL, EdgeBase: "https://edge.example.net",
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := instance.Bind(context.Background(), "code-1"); err != nil {
		t.Fatal(err)
	}
	status, err := instance.Revoke(context.Background())
	if err == nil || !strings.Contains(err.Error(), "本机已撤销") {
		t.Fatalf("云端通知失败应明确返回，但不能回滚本机撤销: %v", err)
	}
	if revokeCalls.Load() != 1 {
		t.Fatalf("应尝试通知云端一次: %d", revokeCalls.Load())
	}
	if status.State != "REVOKED" || status.Bound {
		t.Fatalf("本机撤销状态不符: %+v", status)
	}
	if _, err := os.Stat(filepath.Join(dir, fileIdentity)); !os.IsNotExist(err) {
		t.Fatal("设备身份文件必须删除")
	}
	if _, err := os.Stat(filepath.Join(dir, fileRevoked)); err != nil {
		t.Fatalf("撤销墓碑必须持久化: %v", err)
	}

	reloaded, err := New(Options{
		DataDir: dir, CloudBase: server.URL, EdgeBase: "https://edge.example.net",
	})
	if err != nil {
		t.Fatal(err)
	}
	if reloaded.Status().State != "REVOKED" || reloaded.Status().Bound {
		t.Fatalf("重启后不得复活设备身份: %+v", reloaded.Status())
	}
}

func TestConnectRegistersMetadataOnlyAndHonorsDraining(t *testing.T) {
	upstream := fakeMCPToolServer(t, `[{"name":"query_orders","description":"查询合成销售订单","inputSchema":{"type":"object","properties":{"limit":{"type":"integer"}}}}]`)
	defer upstream.Close()
	var registrations []map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.URL.Path == "/v1/enrollments/redeem":
			_, _ = w.Write([]byte(`{"connector_id":"conn_drain","tenant_id":"tenant_1","tenant_name":"合成企业","device_token":"device-token","device_key_id":"key_1","relay":{"address":"relay.example.com","port":7443,"user":"conn_drain","token":"relay-token"}}`))
		case r.URL.Path == "/v1/sources":
			raw, _ := io.ReadAll(r.Body)
			var item map[string]any
			_ = json.Unmarshal(raw, &item)
			registrations = append(registrations, item)
			_, _ = w.Write([]byte(`{"revision":2}`))
		case r.URL.Path == "/v1/connectors/conn_drain/desired-state":
			_, _ = w.Write([]byte(`{"revision":2,"sources":[],"revoked":false,"draining":true}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	instance, err := New(Options{
		DataDir: t.TempDir(), CloudBase: server.URL, EdgeBase: "https://edge.example.net",
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := instance.Bind(context.Background(), "code-1"); err != nil {
		t.Fatal(err)
	}
	report, err := instance.ImportConfig(context.Background(), []byte(
		`{"mcpServers":{"销售查询":{"type":"http","url":"`+upstream.URL+`/mcp"}}}`,
	))
	if err != nil || report.HasErrors() {
		t.Fatalf("导入失败: %v %+v", err, report)
	}
	instance.config.Sources[0].ToolAllowlist = []string{"query_orders"}

	status, err := instance.Connect(context.Background())
	if err == nil || !strings.Contains(err.Error(), "排空") {
		t.Fatalf("云端排空必须阻止建立新通道: %v", err)
	}
	if status.State != state.Draining {
		t.Fatalf("排空后状态应为 DRAINING: %s", status.State)
	}
	sourceID := instance.config.Sources[0].SourceID
	var registered map[string]any
	for _, item := range registrations {
		if item["source_id"] == sourceID {
			registered = item
			break
		}
	}
	if registered == nil {
		t.Fatal("来源登记未被调用")
	}
	if registered["source_id"] != sourceID {
		t.Fatalf("来源编号不符: %+v", registered)
	}
	info := mcp.ServerInfo{ProtocolVersion: "2025-06-18"}
	info.ServerInfo.Name = "synthetic-mcp"
	info.ServerInfo.Version = "1.0"
	expectedDigest, err := digestToolSchema(
		info,
		[]mcp.Tool{{
			Name:        "query_orders",
			Description: "查询合成销售订单",
			Raw:         json.RawMessage(`{"name":"query_orders","description":"查询合成销售订单","inputSchema":{"type":"object","properties":{"limit":{"type":"integer"}}}}`),
		}},
		[]string{"query_orders"},
	)
	if err != nil {
		t.Fatal(err)
	}
	if registered["tool_schema_sha256"] != expectedDigest {
		t.Fatalf("工具 schema 摘要不是真实目录摘要: got=%v want=%s", registered["tool_schema_sha256"], expectedDigest)
	}
	raw, _ := json.Marshal(registered)
	for _, forbidden := range []string{"upstream_url", "local_host", "local_port", "Authorization", "10.0.0.8"} {
		if strings.Contains(string(raw), forbidden) {
			t.Fatalf("来源登记泄漏本地目标或凭据 %s: %s", forbidden, raw)
		}
	}
}

func TestConnectRejectsCloudSuppliedLocalTargetFields(t *testing.T) {
	upstream := fakeMCPToolServer(t, `[{"name":"query_orders","inputSchema":{"type":"object"}}]`)
	defer upstream.Close()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.URL.Path == "/v1/enrollments/redeem":
			_, _ = w.Write([]byte(`{"connector_id":"conn_bad","tenant_id":"tenant_1","tenant_name":"合成企业","device_token":"device-token","device_key_id":"key_1","relay":{"address":"relay.example.com","port":7443,"user":"conn_bad","token":"relay-token"}}`))
		case r.URL.Path == "/v1/sources":
			_, _ = w.Write([]byte(`{"revision":2}`))
		case r.URL.Path == "/v1/connectors/conn_bad/desired-state":
			_, _ = w.Write([]byte(`{"revision":2,"sources":[{"source_id":"src_00112233445566778899aabbccddeeff","display_name":"销售","local_host":"10.0.0.8","local_port":80}],"revoked":false,"draining":false}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	instance, err := New(Options{
		DataDir: t.TempDir(), CloudBase: server.URL, EdgeBase: "https://edge.example.net",
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := instance.Bind(context.Background(), "code-1"); err != nil {
		t.Fatal(err)
	}
	report, err := instance.ImportConfig(context.Background(), []byte(
		`{"mcpServers":{"销售查询":{"type":"http","url":"`+upstream.URL+`/mcp"}}}`,
	))
	if err != nil || report.HasErrors() {
		t.Fatalf("导入失败: %v %+v", err, report)
	}
	instance.config.Sources[0].ToolAllowlist = []string{"query_orders"}
	status, err := instance.Connect(context.Background())
	if err == nil {
		t.Fatal("含 local_host/local_port 的期望状态必须拒绝")
	}
	if status.State != "DEGRADED_RELAY" {
		t.Fatalf("拒绝恶意期望状态后应降级: %s", status.State)
	}
}

func TestToolSchemaDigestIsCanonicalAndSchemaSensitive(t *testing.T) {
	info := mcp.ServerInfo{ProtocolVersion: "2025-06-18"}
	info.ServerInfo.Name = "inventory"
	info.ServerInfo.Version = "1.0"
	allowlist := []string{"query_orders"}
	first := []mcp.Tool{{
		Name: "query_orders",
		Raw:  json.RawMessage(`{"name":"query_orders","description":"查询订单","inputSchema":{"type":"object","properties":{"limit":{"type":"integer"}}},"annotations":{"readOnlyHint":true}}`),
	}}
	sameSchemaDifferentOrder := []mcp.Tool{{
		Name: "query_orders",
		Raw:  json.RawMessage(`{"inputSchema":{"properties":{"limit":{"type":"integer"}},"type":"object"},"annotations":{"readOnlyHint":true},"description":"查询订单","name":"query_orders"}`),
	}}
	changedSchema := []mcp.Tool{{
		Name: "query_orders",
		Raw:  json.RawMessage(`{"name":"query_orders","description":"查询订单","inputSchema":{"type":"object","properties":{"limit":{"type":"string"}}},"annotations":{"readOnlyHint":true}}`),
	}}

	firstDigest, err := digestToolSchema(info, first, allowlist)
	if err != nil {
		t.Fatal(err)
	}
	reorderedDigest, err := digestToolSchema(info, sameSchemaDifferentOrder, allowlist)
	if err != nil {
		t.Fatal(err)
	}
	changedDigest, err := digestToolSchema(info, changedSchema, allowlist)
	if err != nil {
		t.Fatal(err)
	}
	if firstDigest != reorderedDigest {
		t.Fatalf("JSON 键顺序变化不应改变摘要: %s != %s", firstDigest, reorderedDigest)
	}
	if firstDigest == changedDigest {
		t.Fatal("输入 schema 变化必须改变摘要")
	}
	if _, err := digestToolSchema(info, first, []string{"missing_tool"}); err == nil || !strings.Contains(err.Error(), "不存在") {
		t.Fatalf("缺少允许工具时必须拒绝: %v", err)
	}
	unlistedChange := append([]mcp.Tool(nil), first...)
	unlistedChange = append(unlistedChange, mcp.Tool{
		Name: "internal_admin", Raw: json.RawMessage(`{"name":"internal_admin","inputSchema":{"type":"object"}}`),
	})
	withoutUnlistedDigest, err := digestToolSchema(info, unlistedChange, allowlist)
	if err != nil {
		t.Fatal(err)
	}
	if withoutUnlistedDigest != firstDigest {
		t.Fatal("未获准工具的 schema 不得影响已批准摘要")
	}
}

func TestBuildFrpcConfigStartsOnlyCloudApprovedSources(t *testing.T) {
	instance, err := New(Options{DataDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	instance.config.Sources = []config.Source{{
		SchemaVersion: 1,
		SourceID:      "src_00112233445566778899aabbccddeeff",
		DisplayName:   "销售只读",
		Transport:     config.TransportStreamableHTTP,
		UpstreamURL:   "http://10.0.0.8:8000/mcp",
		ToolAllowlist: []string{"query_orders"},
	}}
	if _, err := instance.buildFrpcConfig(cloud.DesiredState{Revision: 1}, instance.proxies); err == nil ||
		!strings.Contains(err.Error(), "尚未批准任何可用来源") {
		t.Fatalf("未获云端批准的来源不能启动通道: %v", err)
	}
}

func TestQueryFrpcTunnelRequiresRunningProxyPhase(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		user, password, ok := r.BasicAuth()
		if !ok || user != "kaiwu" || password != "synthetic-admin" {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"stcp":[{"name":"src_synthetic","status":"running"}]}`))
	}))
	defer upstream.Close()
	port := upstream.Listener.Addr().(*net.TCPAddr).Port
	connected, detail := queryFrpcTunnel(port, "kaiwu", "synthetic-admin", 1)
	if !connected || !strings.Contains(detail, "1") {
		t.Fatalf("running 代理应判定为已连通: connected=%v detail=%s", connected, detail)
	}

	waiting := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"stcp":[{"name":"src_synthetic","status":"wait start"}]}`))
	}))
	defer waiting.Close()
	port = waiting.Listener.Addr().(*net.TCPAddr).Port
	connected, detail = queryFrpcTunnel(port, "kaiwu", "synthetic-admin", 1)
	if connected || !strings.Contains(detail, "0/1") {
		t.Fatalf("wait start 不能被误报为已连通: connected=%v detail=%s", connected, detail)
	}
}

func TestRecoverTunnelRequiresRunningFrpcProxies(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("synthetic frpc process uses a POSIX shell script")
	}

	upstream := fakeMCPToolServer(t, `[]`)
	defer upstream.Close()

	var proxyRunning atomic.Bool
	admin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		user, password, ok := r.BasicAuth()
		if !ok || user != "kaiwu" || password != "synthetic-admin" {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		status := "wait start"
		if proxyRunning.Load() {
			status = "running"
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprintf(w, `{"stcp":[{"name":"src_synthetic","status":%q}]}`, status)
	}))
	defer admin.Close()

	dir := t.TempDir()
	binary := filepath.Join(dir, "frpc")
	if err := os.WriteFile(binary, []byte("#!/bin/sh\ntrap 'exit 0' INT TERM\nwhile :; do sleep 1; done\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(dir, "frpc.toml")
	if err := os.WriteFile(configPath, []byte("synthetic = true\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	instance, err := New(Options{DataDir: dir})
	if err != nil {
		t.Fatal(err)
	}
	instance.config.Sources = []config.Source{{
		SchemaVersion: 1,
		SourceID:      "src_synthetic",
		DisplayName:   "合成来源",
		Transport:     config.TransportStreamableHTTP,
		UpstreamURL:   upstream.URL + "/mcp",
	}}
	instance.state = state.NewMachine(state.DegradedRelay)
	instance.supervisor = supervisor.New(binary, configPath)
	instance.frpcAdminPort = admin.Listener.Addr().(*net.TCPAddr).Port
	instance.frpcAdminUser = "kaiwu"
	instance.frpcAdminPassword = "synthetic-admin"
	instance.frpcProxyCount = 1

	supCtx, supCancel := context.WithCancel(context.Background())
	defer supCancel()
	if err := instance.supervisor.Start(supCtx); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		stopCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = instance.supervisor.Stop(stopCtx)
	})
	deadline := time.Now().Add(2 * time.Second)
	for !instance.supervisor.Running() && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if !instance.supervisor.Running() {
		t.Fatal("synthetic frpc process did not start")
	}

	instance.recoverTunnel()
	if got := instance.state.Current(); got != state.DegradedRelay {
		t.Fatalf("wait start proxy must remain degraded, got %s", got)
	}

	proxyRunning.Store(true)
	instance.recoverTunnel()
	if got := instance.state.Current(); got != state.Ready {
		t.Fatalf("running proxy should recover to ready, got %s", got)
	}
	instance.mu.RLock()
	stopHeartbeat := instance.heartbeat
	instance.mu.RUnlock()
	if stopHeartbeat != nil {
		stopHeartbeat()
	}
}

func TestDisabledSourceDoesNotBlockEnabledSource(t *testing.T) {
	upstream := fakeMCPToolServer(t, `[{"name":"get_inventory","description":"读取库存"}]`)
	defer upstream.Close()
	instance, err := New(Options{DataDir: t.TempDir(), CloudBase: "http://127.0.0.1:1", EdgeBase: "https://mcp.example.com"})
	if err != nil {
		t.Fatal(err)
	}

	legacy, err := instance.SaveSource(SourceInput{
		DisplayName: "旧合成来源", UpstreamURL: "http://127.0.0.1:1/mcp", Tools: []string{"lookup_order"},
	})
	if err != nil {
		t.Fatal(err)
	}
	current, err := instance.SaveSource(SourceInput{
		DisplayName: "文件库存来源", UpstreamURL: upstream.URL, Tools: []string{"get_inventory"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := instance.SetSourceEnabled(legacy.Source.SourceID, false); err != nil {
		t.Fatal(err)
	}

	reports, err := instance.Diagnose(context.Background(), "", "")
	if err != nil {
		t.Fatalf("停用旧来源后诊断不应被阻断: %v", err)
	}
	if len(reports) != 1 || reports[0].SourceID != current.Source.SourceID || !reports[0].OK {
		t.Fatalf("诊断必须只覆盖启用的当前来源: %+v", reports)
	}

	if err := instance.startAuthProxies(); err != nil {
		t.Fatalf("启动启用来源代理失败: %v", err)
	}
	defer instance.stopAuthProxies()
	instance.mu.RLock()
	proxyIDs := make([]string, 0, len(instance.proxies))
	for sourceID := range instance.proxies {
		proxyIDs = append(proxyIDs, sourceID)
	}
	instance.mu.RUnlock()
	if len(proxyIDs) != 1 || proxyIDs[0] != current.Source.SourceID {
		t.Fatalf("只能为启用的当前来源启动本机代理: %v", proxyIDs)
	}

	registrations := make(chan cloud.SourceRegistration, 2)
	controlPlane := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/sources" || r.Method != http.MethodPost {
			http.NotFound(w, r)
			return
		}
		var registration cloud.SourceRegistration
		if err := json.NewDecoder(r.Body).Decode(&registration); err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		registrations <- registration
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer controlPlane.Close()
	client, err := cloud.NewWithSigner(controlPlane.URL, "device-token", instance.deviceKey)
	if err != nil {
		t.Fatal(err)
	}
	instance.cloud = client
	if err := instance.registerSources(context.Background(), instance.runtimeSourcesLocked(), instance.proxies); err != nil {
		t.Fatalf("云端来源登记不应被停用来源阻断: %v", err)
	}
	if got := len(registrations); got != 1 {
		t.Fatalf("只能登记启用的当前来源，实际 %d 条", got)
	}
	registration := <-registrations
	if registration.SourceID != current.Source.SourceID {
		t.Fatalf("登记了错误来源: %+v", registration)
	}

	desired := cloud.DesiredState{
		Revision: 1,
		Relay: cloud.Relay{
			Address: "relay.example.com", Port: 7443, User: "tenant", Token: "relay-token", Protocol: "websocket",
		},
		Sources: []cloud.DesiredSource{
			{SourceID: legacy.Source.SourceID, STCPSecret: "legacy-stcp"},
			{SourceID: current.Source.SourceID, STCPSecret: "current-stcp", AllowUsers: []string{"kaiwu"}},
		},
	}
	generated, err := instance.buildFrpcConfig(desired, instance.proxies)
	if err != nil {
		t.Fatalf("生成专属 FRP 配置失败: %v", err)
	}
	if len(generated.Proxies) != 1 || generated.Proxies[0].Name != current.Source.SourceID {
		t.Fatalf("FRP 只能包含启用的当前来源: %+v", generated.Proxies)
	}
}

func TestConnectRejectsWhenAllSourcesDisabled(t *testing.T) {
	server := fakeCloud(t)
	defer server.Close()
	instance, err := New(Options{DataDir: t.TempDir(), CloudBase: server.URL, EdgeBase: "https://mcp.example.com"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := instance.Bind(context.Background(), "code-1"); err != nil {
		t.Fatal(err)
	}
	source, err := instance.SaveSource(SourceInput{
		DisplayName: "已停用来源", UpstreamURL: "http://127.0.0.1:1/mcp", Tools: []string{"get_inventory"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := instance.SetSourceEnabled(source.Source.SourceID, false); err != nil {
		t.Fatal(err)
	}
	status, err := instance.Connect(context.Background())
	if err == nil || !strings.Contains(err.Error(), "至少启用一个") {
		t.Fatalf("全部来源停用时必须拒绝连接: status=%+v err=%v", status, err)
	}
	if status.State == state.Ready {
		t.Fatal("全部来源停用时绝不能显示已连接")
	}
}

func TestConnectFailsWhenEnabledSourceCannotBeRegistered(t *testing.T) {
	server := fakeCloud(t)
	defer server.Close()
	instance, err := New(Options{DataDir: t.TempDir(), CloudBase: server.URL, EdgeBase: "https://mcp.example.com"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := instance.Bind(context.Background(), "code-1"); err != nil {
		t.Fatal(err)
	}
	instance.config.Sources = []config.Source{{
		SchemaVersion: 1,
		SourceID:      "mcp",
		DisplayName:   "不可达来源",
		Transport:     config.TransportStreamableHTTP,
		UpstreamURL:   "http://127.0.0.1:1/mcp",
		Auth:          config.Auth{Mode: config.AuthModeNone, CredentialLocation: config.CredentialLocationLocal},
		ToolAllowlist: []string{"get_inventory"},
	}}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	status, err := instance.Connect(ctx)
	if err == nil {
		t.Fatalf("启用来源失败时不能返回连接成功: %+v", status)
	}
	if status.State == state.Ready {
		t.Fatal("启用来源失败时绝不能显示已连接")
	}
}

func (a *Agent) startAuthProxies() error {
	proxies, err := a.prepareAuthProxies(a.runtimeSourcesLocked())
	if err != nil {
		return err
	}
	a.mu.Lock()
	a.proxies = proxies
	a.preparedProxies = nil
	a.mu.Unlock()
	return nil
}
