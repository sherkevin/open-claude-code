package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/authproxy"
	"github.com/sherkevin/open-claude-code/connector/internal/cloud"
	"github.com/sherkevin/open-claude-code/connector/internal/config"
	"github.com/sherkevin/open-claude-code/connector/internal/demomcp"
	"github.com/sherkevin/open-claude-code/connector/internal/frpc"
	"github.com/sherkevin/open-claude-code/connector/internal/state"
	"github.com/sherkevin/open-claude-code/connector/internal/supervisor"
)

type demoReuseFixture struct {
	instance         *Agent
	supervisor       *supervisor.Supervisor
	demoProxy        *authproxy.Server
	customerProxy    *authproxy.Server
	demoPort         int
	customerPort     int
	frpcConfigPath   string
	frpcConfigBefore string
	adminReloads     atomic.Int32
	heartbeatStops   atomic.Int32
}

func newDemoReuseFixture(t *testing.T, withDemoRoute bool, controlURL string) *demoReuseFixture {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("synthetic frpc process uses a POSIX shell script")
	}
	if controlURL == "" {
		controlURL = "https://cloud.example.net"
	}
	customerUpstream := fakeMCPToolServer(t, `[{"name":"get_inventory","inputSchema":{"type":"object"}}]`)
	t.Cleanup(customerUpstream.Close)

	dir := t.TempDir()
	instance, err := New(Options{
		DataDir:    dir,
		CloudBase:  controlURL,
		EdgeBase:   "https://edge.example.net",
		FrpcBinary: filepath.Join(dir, "frpc"),
	})
	if err != nil {
		t.Fatal(err)
	}
	customer := config.Source{
		SchemaVersion: config.CurrentSchemaVersion,
		SourceID:      "src_cccccccccccccccccccccccccccccccc",
		DisplayName:   "客户业务来源",
		Transport:     config.TransportStreamableHTTP,
		UpstreamURL:   customerUpstream.URL + "/mcp",
		Auth:          config.Auth{Mode: config.AuthModeNone, CredentialLocation: config.CredentialLocationLocal},
		ToolAllowlist: []string{"get_inventory"},
	}
	client, err := cloud.New(controlURL, "device-token")
	if err != nil {
		t.Fatal(err)
	}
	instance.mu.Lock()
	instance.config.Sources = []config.Source{customer}
	instance.identity = cloud.Identity{ConnectorID: "connector-test-1", DeviceToken: "device-token"}
	instance.cloud = client
	instance.mu.Unlock()
	instance.state = state.NewMachine(state.DegradedRelay)
	if err := instance.StartBuiltinDemo(); err != nil {
		t.Fatal(err)
	}

	customerProxy, err := authproxy.New(authproxy.Options{
		SourceID:      customer.SourceID,
		UpstreamURL:   customer.UpstreamURL,
		ToolAllowlist: customer.ToolAllowlist,
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := customerProxy.Start(); err != nil {
		t.Fatal(err)
	}
	instance.mu.Lock()
	instance.proxies[customer.SourceID] = customerProxy
	instance.mu.Unlock()
	if _, err := instance.startAuthProxiesForDemo(); err != nil {
		t.Fatal(err)
	}
	instance.mu.RLock()
	demoID := instance.demoState.SourceID
	demoProxy := instance.proxies[demoID]
	instance.mu.RUnlock()
	if demoProxy == nil {
		t.Fatal("demo proxy did not start")
	}
	demoHost, demoPort, err := demoProxy.Endpoint()
	if err != nil {
		t.Fatal(err)
	}
	_, customerPort, err := customerProxy.Endpoint()
	if err != nil {
		t.Fatal(err)
	}

	fixture := &demoReuseFixture{
		instance:       instance,
		demoProxy:      demoProxy,
		customerProxy:  customerProxy,
		demoPort:       demoPort,
		customerPort:   customerPort,
		frpcConfigPath: instance.store.Path(fileFrpc),
	}
	commonUser := "tenant-demo"
	actualName := frpcProxyName(demoID, commonUser)
	localAddr := net.JoinHostPort(demoHost, strconv.Itoa(demoPort))
	decoyLocalAddr := net.JoinHostPort(demoHost, "1")
	proxyCount := 3
	if withDemoRoute {
		proxyCount++
	}
	admin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		user, password, ok := r.BasicAuth()
		if !ok || user != "kaiwu" || password != "synthetic-admin" {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		if r.URL.Path == "/api/reload" {
			fixture.adminReloads.Add(1)
			http.Error(w, "reload must not be called", http.StatusInternalServerError)
			return
		}
		if r.URL.Path != "/api/status" {
			http.NotFound(w, r)
			return
		}
		statuses := []frpcProxyStatus{
			{Name: demoID, Type: "stcp", Status: "running", LocalAddr: decoyLocalAddr},
			{Name: "other-user." + demoID, Type: "stcp", Status: "running", LocalAddr: decoyLocalAddr},
			{Name: "customer-business", Type: "stcp", Status: "wait start", Err: "customer route unavailable"},
		}
		if withDemoRoute {
			statuses = append(statuses, frpcProxyStatus{
				Name: actualName, Type: "stcp", Status: "running", LocalAddr: localAddr,
			})
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string][]frpcProxyStatus{"stcp": statuses})
	}))
	t.Cleanup(admin.Close)

	binary := filepath.Join(dir, "frpc")
	if err := os.WriteFile(binary, []byte("#!/bin/sh\ntrap 'exit 0' INT TERM\nwhile :; do sleep 1; done\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	fixture.frpcConfigBefore = "synthetic = true\n"
	if err := os.WriteFile(fixture.frpcConfigPath, []byte(fixture.frpcConfigBefore), 0o600); err != nil {
		t.Fatal(err)
	}
	s := supervisor.New(binary, fixture.frpcConfigPath)
	fixture.supervisor = s
	instance.mu.Lock()
	instance.supervisor = s
	instance.frpcAdminPort = admin.Listener.Addr().(*net.TCPAddr).Port
	instance.frpcAdminUser = "kaiwu"
	instance.frpcAdminPassword = "synthetic-admin"
	instance.frpcUser = commonUser
	instance.frpcProxyCount = proxyCount
	instance.heartbeat = func() { fixture.heartbeatStops.Add(1) }
	instance.mu.Unlock()
	if err := s.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(2 * time.Second)
	for !s.Running() && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if !s.Running() {
		t.Fatal("synthetic customer tunnel did not start")
	}
	t.Cleanup(instance.Close)
	return fixture
}

func (f *demoReuseFixture) assertCustomerTunnelUnchanged(t *testing.T) {
	t.Helper()
	f.instance.mu.RLock()
	currentSupervisor := f.instance.supervisor
	currentDemo := f.instance.proxies[f.instance.demoState.SourceID]
	f.instance.mu.RUnlock()
	if currentSupervisor != f.supervisor || !currentSupervisor.Running() {
		t.Fatal("demo self-test replaced or stopped the customer supervisor")
	}
	if currentDemo != f.demoProxy {
		t.Fatal("demo self-test replaced the demo authproxy listener")
	}
	demoHost, demoPort, err := currentDemo.Endpoint()
	if err != nil || demoPort != f.demoPort {
		t.Fatalf("demo listener changed: host=%s port=%d want=%d err=%v", demoHost, demoPort, f.demoPort, err)
	}
	_, customerPort, err := f.customerProxy.Endpoint()
	if err != nil || customerPort != f.customerPort {
		t.Fatalf("customer listener changed: port=%d want=%d err=%v", customerPort, f.customerPort, err)
	}
	contents, err := os.ReadFile(f.frpcConfigPath)
	if err != nil || string(contents) != f.frpcConfigBefore {
		t.Fatalf("frpc config changed: contents=%q err=%v", contents, err)
	}
	if got := f.adminReloads.Load(); got != 0 {
		t.Fatalf("frpc reload endpoint was called %d times", got)
	}
	if got := f.heartbeatStops.Load(); got != 0 {
		t.Fatalf("heartbeat was replaced %d times", got)
	}
}

func (f *demoReuseFixture) runDemoProxyProbe(ctx context.Context, nonce string) (cloud.SelfTestProbe, error) {
	return runLocalDemoProbe(ctx, fmt.Sprintf("http://127.0.0.1:%d/mcp", f.demoPort), nonce)
}

func TestDemoSelfTestUsesExactRunningRouteDespiteCustomerError(t *testing.T) {
	var cloudCalls atomic.Int32
	control := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		cloudCalls.Add(1)
		if r.URL.Path != "/v1/connectors/connector-test-1/self-test" {
			http.NotFound(w, r)
			return
		}
		var request struct {
			SourceID string `json:"source_id"`
			Nonce    string `json:"nonce"`
		}
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprintf(w, `{"data":{"ok":true,"trace_id":"kwtr_aaaaaaaaaaaaaaaaaaaaaaaa","source_id":%q,"stages":[{"id":"edge_connect","status":"passed","message":"ok"},{"id":"mcp_initialize","status":"passed","message":"ok"},{"id":"tools_list","status":"passed","message":"ok"},{"id":"tools_call","status":"passed","message":"ok"},{"id":"probe_validation","status":"passed","message":"ok"}],"probe":{"demo":true,"marker":%q,"nonce":%q,"server_time_utc":"2026-11-19T00:00:00Z","instance_id":"demo_public_probe"}}}`, request.SourceID, demomcp.Marker, request.Nonce)
	}))
	t.Cleanup(control.Close)
	fixture := newDemoReuseFixture(t, true, control.URL)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	result := fixture.instance.SelfTest(ctx)
	if !result.OK {
		t.Fatalf("demo self-test failed because of customer route health: %+v", result)
	}
	if got := fixture.instance.state.Current(); got != state.DegradedRelay {
		t.Fatalf("self-test should not rewrite the global customer health state: %s", got)
	}
	if cloudCalls.Load() != 1 {
		t.Fatalf("expected exactly one cloud self-test request, got %d", cloudCalls.Load())
	}
	fixture.assertCustomerTunnelUnchanged(t)
}

func TestFRPCProxyNameUsesActualCommonUser(t *testing.T) {
	demoID := "src_403571374c41e5c2d9f793ad5e334053"
	if got := frpcProxyName(demoID, ""); got != demoID {
		t.Fatalf("empty common user must use the source ID directly, got %q", got)
	}
	if got := frpcProxyName(demoID, " tenant-demo "); got != "tenant-demo."+demoID {
		t.Fatalf("non-empty common user must prefix the source ID, got %q", got)
	}
}

func TestDemoNewTunnelQueryOnlyChecksExactRouteAfterSupervisorExit(t *testing.T) {
	fixture := newDemoReuseFixture(t, true, "")
	stopCtx, stopCancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer stopCancel()
	if err := fixture.supervisor.Stop(stopCtx); err != nil {
		t.Fatal(err)
	}
	if fixture.supervisor.Running() {
		t.Fatal("synthetic old supervisor did not exit")
	}

	fixture.instance.mu.RLock()
	adminPort := fixture.instance.frpcAdminPort
	commonUser := fixture.instance.frpcUser
	demoID := fixture.instance.demoState.SourceID
	fixture.instance.mu.RUnlock()
	demoHost, demoPort, err := fixture.demoProxy.Endpoint()
	if err != nil {
		t.Fatal(err)
	}
	generated := frpc.Config{
		AdminUser:     "kaiwu",
		AdminPassword: "synthetic-admin",
		User:          commonUser,
		Proxies: []frpc.Proxy{{
			Name: demoID, LocalIP: demoHost, LocalPort: demoPort,
		}},
	}
	connected, detail := queryConnectTunnel(adminPort, generated, true, demoID)
	if !connected {
		t.Fatalf("demo-only new tunnel query was blocked by customer route error: %s", detail)
	}
	fullConnected, fullDetail := queryConnectTunnel(adminPort, generated, false, demoID)
	if fullConnected || !strings.Contains(fullDetail, "customer-business") {
		t.Fatalf("ordinary Connect must keep the full route health check: connected=%v detail=%s", fullConnected, fullDetail)
	}
}

func TestDemoSelfTestWithoutExactRouteKeepsCustomerTunnel(t *testing.T) {
	fixture := newDemoReuseFixture(t, false, "")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_, err := fixture.instance.connect(ctx, true)
	if err == nil || !strings.Contains(err.Error(), "测试通道尚未就绪，请在方便时重新连接") {
		t.Fatalf("missing exact demo route must request reconnect, got %v", err)
	}
	if got := fixture.instance.state.Current(); got != state.DegradedRelay {
		t.Fatalf("demo-only failure rewrote customer state: %s", got)
	}
	fixture.assertCustomerTunnelUnchanged(t)
}

func TestDemoOffOnKeepsProxyListenerAndUsesNewInstance(t *testing.T) {
	fixture := newDemoReuseFixture(t, true, "")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	before, err := fixture.runDemoProxyProbe(ctx, "before-toggle")
	if err != nil {
		t.Fatalf("initial demo probe through authproxy failed: %v", err)
	}
	if _, err := fixture.instance.SetBuiltinDemoEnabled(false); err != nil {
		t.Fatal(err)
	}
	if _, _, err := fixture.demoProxy.Endpoint(); err != nil {
		t.Fatalf("disabling demo must keep its authproxy listener: %v", err)
	}
	closedCtx, closedCancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer closedCancel()
	if _, err := fixture.runDemoProxyProbe(closedCtx, "while-disabled"); err == nil {
		t.Fatal("disabled demo still returned data through the old proxy")
	}
	if _, err := fixture.instance.SetBuiltinDemoEnabled(true); err != nil {
		t.Fatal(err)
	}
	if _, err := fixture.instance.connect(ctx, true); err != nil {
		t.Fatalf("demo-only reconnect after re-enable failed: %v", err)
	}
	after, err := fixture.runDemoProxyProbe(ctx, "after-toggle")
	if err != nil {
		t.Fatalf("re-enabled demo probe through old authproxy failed: %v", err)
	}
	if before.Nonce != "before-toggle" || after.Nonce != "after-toggle" {
		t.Fatalf("demo nonce validation failed: before=%+v after=%+v", before, after)
	}
	if before.InstanceID == "" || after.InstanceID == "" || before.InstanceID == after.InstanceID {
		t.Fatalf("demo listener did not expose a new instance: before=%q after=%q", before.InstanceID, after.InstanceID)
	}
	fixture.assertCustomerTunnelUnchanged(t)
}

func TestCloseCancelsInFlightAutoConnectBeforeResourcesStart(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("synthetic frpc process uses a POSIX shell script")
	}
	blocked := make(chan struct{})
	release := make(chan struct{})
	var blockOnce, releaseOnce sync.Once
	unblock := func() { releaseOnce.Do(func() { close(release) }) }
	control := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/sources":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{}`))
		case "/v1/connectors/connector-test-1/desired-state":
			blockOnce.Do(func() { close(blocked) })
			<-release
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{}`))
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(control.Close)
	t.Cleanup(unblock)

	dir := t.TempDir()
	marker := filepath.Join(dir, "frpc-started")
	binary := filepath.Join(dir, "frpc")
	script := "#!/bin/sh\necho started > " + marker + "\ntrap 'exit 0' INT TERM\nwhile :; do sleep 1; done\n"
	if err := os.WriteFile(binary, []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	instance, err := New(Options{
		DataDir:    dir,
		CloudBase:  control.URL,
		EdgeBase:   "https://edge.example.net",
		FrpcBinary: binary,
	})
	if err != nil {
		t.Fatal(err)
	}
	client, err := cloud.New(control.URL, "device-token")
	if err != nil {
		t.Fatal(err)
	}
	instance.mu.Lock()
	instance.identity = cloud.Identity{ConnectorID: "connector-test-1", DeviceToken: "device-token"}
	instance.cloud = client
	instance.startupConnectPending = true
	instance.mu.Unlock()
	if err := instance.StartBuiltinDemo(); err != nil {
		t.Fatal(err)
	}
	instance.mu.RLock()
	demoID := instance.demoState.SourceID
	demoServer := instance.demoServer
	instance.mu.RUnlock()

	done := make(chan struct{})
	go func() {
		instance.AutoConnect()
		close(done)
	}()
	select {
	case <-blocked:
	case <-time.After(5 * time.Second):
		t.Fatal("AutoConnect did not reach the blocked cloud request")
	}
	instance.mu.RLock()
	demoProxy := instance.preparedProxies[demoID]
	instance.mu.RUnlock()
	if demoProxy == nil {
		t.Fatal("blocked connect did not create the expected local demo proxy")
	}
	closeCtx, closeCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer closeCancel()
	instance.CloseContext(closeCtx)
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		unblock()
		t.Fatal("Close did not cancel the in-flight AutoConnect")
	}
	unblock()

	if !instance.isClosed() {
		t.Fatal("agent did not remain closed")
	}
	instance.mu.RLock()
	currentSupervisor := instance.supervisor
	currentHeartbeat := instance.heartbeat
	proxyCount := len(instance.proxies) + len(instance.preparedProxies)
	instance.mu.RUnlock()
	if currentSupervisor != nil || currentHeartbeat != nil || proxyCount != 0 {
		t.Fatalf("Close left runtime resources: supervisor=%v heartbeat=%v proxies=%d", currentSupervisor, currentHeartbeat, proxyCount)
	}
	if _, _, err := demoProxy.Endpoint(); err == nil {
		t.Fatal("demo authproxy listener remained open after Close")
	}
	if demoServer.Running() {
		t.Fatal("demo MCP listener remained open after Close")
	}
	if _, err := os.Stat(instance.store.Path(fileFrpc)); !os.IsNotExist(err) {
		t.Fatalf("Close race wrote an frpc config: %v", err)
	}
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatalf("Close race started frpc: %v", err)
	}
	if err := instance.StartBuiltinDemo(); !errorsIsClosed(err) {
		t.Fatalf("StartBuiltinDemo after Close must fail with closed error, got %v", err)
	}
}

func errorsIsClosed(err error) bool {
	return err != nil && strings.Contains(err.Error(), "连接器正在关闭")
}
