package agent

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/cloud"
	"github.com/sherkevin/open-claude-code/connector/internal/config"
	"github.com/sherkevin/open-claude-code/connector/internal/demomcp"
	"github.com/sherkevin/open-claude-code/connector/internal/state"
)

func TestBuiltinDemoSourceIDMatchesContract(t *testing.T) {
	const connectorID = "connector-test-1"
	const want = "src_403571374c41e5c2d9f793ad5e334053"
	if got := builtinDemoSourceID(connectorID); got != want {
		t.Fatalf("built-in source ID mismatch: got=%s want=%s", got, want)
	}
	if !isOpaqueSourceID(want) {
		t.Fatal("the contract source ID must remain in the existing src_32hex format")
	}
}

func TestBuiltinDemoDoesNotClaimSameNameBusinessSource(t *testing.T) {
	instance, err := New(Options{DataDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(instance.Close)
	business := config.Source{
		SchemaVersion:   config.CurrentSchemaVersion,
		SourceID:        "src_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		DisplayName:     demomcp.SourceName,
		Transport:       config.TransportStreamableHTTP,
		UpstreamURL:     "http://10.0.0.8:9000/mcp",
		ProtocolProfile: builtinDemoProtocolProfile,
		Auth:            config.Auth{Mode: config.AuthModeNone, CredentialLocation: config.CredentialLocationLocal},
		ToolAllowlist:   append([]string(nil), demomcp.Tools...),
	}
	instance.config.Sources = []config.Source{business}

	if err := instance.StartBuiltinDemo(); err != nil {
		t.Fatal(err)
	}
	if len(instance.config.Sources) != 2 {
		t.Fatalf("built-in source must be added separately: %+v", instance.config.Sources)
	}
	if got := instance.config.Sources[0]; got.SourceID != business.SourceID || got.UpstreamURL != business.UpstreamURL {
		t.Fatalf("same-name business source was overwritten: %+v", got)
	}
	if instance.demoState.SourceID == business.SourceID {
		t.Fatal("editable display name and tool names must not claim the business source")
	}
}

func TestBuiltinDemoDerivedIDConflictDoesNotOverwriteBusinessSource(t *testing.T) {
	instance, err := New(Options{DataDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(instance.Close)
	const connectorID = "connector-test-1"
	business := config.Source{
		SchemaVersion:   config.CurrentSchemaVersion,
		SourceID:        builtinDemoSourceID(connectorID),
		DisplayName:     demomcp.SourceName,
		Transport:       config.TransportStreamableHTTP,
		UpstreamURL:     "http://10.0.0.9:9000/mcp",
		ProtocolProfile: builtinDemoProtocolProfile,
		Auth:            config.Auth{Mode: config.AuthModeNone, CredentialLocation: config.CredentialLocationLocal},
		ToolAllowlist:   append([]string(nil), demomcp.Tools...),
	}
	instance.config.Sources = []config.Source{business}
	instance.identity.ConnectorID = connectorID

	err = instance.StartBuiltinDemo()
	if err == nil || !strings.Contains(err.Error(), "已被其他来源占用") {
		t.Fatalf("occupied derived ID must report a conflict: %v", err)
	}
	if got := instance.config.Sources[0]; got.SourceID != business.SourceID || got.UpstreamURL != business.UpstreamURL {
		t.Fatalf("business source at derived ID was modified: %+v", got)
	}
	if instance.Status().BuiltinDemo.ErrorCode != "BUILTIN_SOURCE_CONFLICT" {
		t.Fatalf("conflict must be visible in status: %+v", instance.Status().BuiltinDemo)
	}
}

func TestBuiltinDemoMigratesOnlyTrackedSource(t *testing.T) {
	instance, err := New(Options{DataDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(instance.Close)
	business := config.Source{
		SchemaVersion: config.CurrentSchemaVersion,
		SourceID:      "src_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
		DisplayName:   demomcp.SourceName,
		Transport:     config.TransportStreamableHTTP,
		UpstreamURL:   "http://10.0.0.10:9000/mcp",
		Auth:          config.Auth{Mode: config.AuthModeNone, CredentialLocation: config.CredentialLocationLocal},
		ToolAllowlist: append([]string(nil), demomcp.Tools...),
	}
	instance.config.Sources = []config.Source{business}
	if err := instance.StartBuiltinDemo(); err != nil {
		t.Fatal(err)
	}
	oldDemoID := instance.demoState.SourceID
	if oldDemoID == "" {
		t.Fatal("unbound demo source ID was not persisted")
	}
	if err := instance.migrateBuiltinDemoForIdentity("connector-test-1"); err != nil {
		t.Fatal(err)
	}
	if got := instance.demoState.SourceID; got != builtinDemoSourceID("connector-test-1") {
		t.Fatalf("tracked demo source was not migrated: %s", got)
	}
	var sameName *config.Source
	for index := range instance.config.Sources {
		source := &instance.config.Sources[index]
		if source.SourceID == business.SourceID {
			sameName = source
		}
	}
	if sameName == nil || sameName.UpstreamURL != business.UpstreamURL {
		t.Fatalf("same-name business source changed during migration: %+v", instance.config.Sources)
	}
}

func TestBadCustomerSourceDoesNotBlockBuiltinProxy(t *testing.T) {
	instance, err := New(Options{DataDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(instance.Close)
	if err := instance.StartBuiltinDemo(); err != nil {
		t.Fatal(err)
	}
	customer := config.Source{
		SchemaVersion: config.CurrentSchemaVersion,
		SourceID:      "src_cccccccccccccccccccccccccccccccc",
		DisplayName:   "坏的客户来源",
		Transport:     config.TransportStreamableHTTP,
		UpstreamURL:   "://invalid-upstream",
		Auth:          config.Auth{Mode: config.AuthModeNone, CredentialLocation: config.CredentialLocationLocal},
		ToolAllowlist: []string{"lookup"},
	}
	instance.config.Sources = append(instance.config.Sources, customer)
	if err := instance.startAuthProxies(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(instance.stopAuthProxies)
	instance.mu.RLock()
	_, demoProxy := instance.proxies[instance.demoState.SourceID]
	_, customerProxy := instance.proxies[customer.SourceID]
	instance.mu.RUnlock()
	if !demoProxy || customerProxy {
		t.Fatalf("bad customer source must not block or be silently proxied: demo=%v customer=%v", demoProxy, customerProxy)
	}
}

func TestBuiltinDemoToggleDoesNotChangeBusinessSource(t *testing.T) {
	instance, err := New(Options{DataDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(instance.Close)
	business := config.Source{
		SchemaVersion: config.CurrentSchemaVersion,
		SourceID:      "src_dddddddddddddddddddddddddddddddd",
		DisplayName:   "客户业务来源",
		Transport:     config.TransportStreamableHTTP,
		UpstreamURL:   "http://10.0.0.11:9000/mcp",
		Auth:          config.Auth{Mode: config.AuthModeNone, CredentialLocation: config.CredentialLocationLocal},
		ToolAllowlist: []string{"lookup"},
	}
	instance.config.Sources = []config.Source{business}
	if err := instance.StartBuiltinDemo(); err != nil {
		t.Fatal(err)
	}
	if _, err := instance.SetBuiltinDemoEnabled(false); err != nil {
		t.Fatal(err)
	}
	instance.mu.RLock()
	businessAfter := instance.config.Sources[0]
	demoIndex := instance.builtinDemoSourceIndexLocked()
	demoDisabled := demoIndex >= 0 && instance.config.Sources[demoIndex].Disabled
	instance.mu.RUnlock()
	if businessAfter.Disabled || businessAfter.UpstreamURL != business.UpstreamURL {
		t.Fatalf("disabling demo changed the business source: %+v", businessAfter)
	}
	if !demoDisabled || instance.Status().BuiltinDemo.Running {
		t.Fatalf("demo did not stop independently: %+v", instance.Status().BuiltinDemo)
	}
}

func TestBuiltinDemoConnectFailureKeepsCustomerProxyAndState(t *testing.T) {
	upstream := fakeMCPToolServer(t, `[{"name":"get_inventory","inputSchema":{"type":"object"}}]`)
	defer upstream.Close()
	control := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path == "/v1/sources" {
			http.Error(w, `{"code":"SYNTHETIC_REGISTER_FAILED","message":"synthetic failure"}`, http.StatusServiceUnavailable)
			return
		}
		http.NotFound(w, r)
	}))
	defer control.Close()

	instance, err := New(Options{
		DataDir:   t.TempDir(),
		CloudBase: control.URL,
		EdgeBase:  "https://edge.example.net",
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(instance.Close)
	if err := instance.StartBuiltinDemo(); err != nil {
		t.Fatal(err)
	}
	customer := config.Source{
		SchemaVersion:   config.CurrentSchemaVersion,
		SourceID:        "src_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
		DisplayName:     "客户业务来源",
		Transport:       config.TransportStreamableHTTP,
		UpstreamURL:     upstream.URL + "/mcp",
		Auth:            config.Auth{Mode: config.AuthModeNone, CredentialLocation: config.CredentialLocationLocal},
		ToolAllowlist:   []string{"get_inventory"},
		ProtocolProfile: "synthetic",
	}
	instance.mu.Lock()
	instance.config.Sources = append(instance.config.Sources, customer)
	instance.identity = cloud.Identity{ConnectorID: "conn_demo_isolated", DeviceToken: "device-token"}
	instance.cloud = &cloud.Client{BaseURL: control.URL, DeviceToken: "device-token", HTTP: control.Client()}
	instance.mu.Unlock()
	if !instance.state.Transition(state.Configuring, "测试准备") ||
		!instance.state.Transition(state.Connecting, "测试准备") ||
		!instance.state.Transition(state.Ready, "测试准备") {
		t.Fatalf("could not prepare READY state: %s", instance.state.Current())
	}
	if err := instance.startAuthProxies(); err != nil {
		t.Fatal(err)
	}
	instance.mu.RLock()
	customerProxy := instance.proxies[customer.SourceID]
	instance.mu.RUnlock()
	if customerProxy == nil {
		t.Fatal("customer proxy did not start")
	}
	_, beforePort, err := customerProxy.Endpoint()
	if err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, err := instance.connect(ctx, true); err == nil {
		t.Fatal("demo-only connect unexpectedly succeeded without cloud registration")
	}
	instance.mu.RLock()
	customerAfter := instance.proxies[customer.SourceID]
	instance.mu.RUnlock()
	if customerAfter != customerProxy {
		t.Fatal("demo-only failure replaced the existing customer proxy")
	}
	_, afterPort, err := customerAfter.Endpoint()
	if err != nil || afterPort != beforePort {
		t.Fatalf("existing customer proxy was restarted: before=%d after=%d err=%v", beforePort, afterPort, err)
	}
	if got := instance.state.Current(); got != state.Ready {
		t.Fatalf("demo-only failure changed customer connection state: %s", got)
	}
}

func TestStartRuntimeAndCloseAreIdempotent(t *testing.T) {
	instance, err := New(Options{DataDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	instance.StartRuntime()
	instance.mu.RLock()
	server := instance.demoServer
	instance.mu.RUnlock()
	if server == nil || !server.Running() {
		t.Fatal("runtime did not start the bundled demo MCP")
	}
	endpoint := server.Endpoint()
	instance.StartRuntime()
	instance.mu.RLock()
	serverAfter := instance.demoServer
	instance.mu.RUnlock()
	if serverAfter != server || serverAfter.Endpoint() != endpoint {
		t.Fatal("repeated StartRuntime created a second demo instance")
	}
	instance.Close()
	instance.Close()
	if server.Running() {
		t.Fatal("Close left the bundled demo listener running")
	}
}
