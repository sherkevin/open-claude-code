package agent

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/diagnostics"
	"github.com/sherkevin/open-claude-code/connector/internal/state"
)

func TestUpgradeLegacyIntentAcrossTwoInitializations(t *testing.T) {
	control := fakeCloud(t)
	defer control.Close()

	tests := []struct {
		name      string
		saved     state.State
		bound     bool
		source    bool
		tombstone bool
		want      state.State
		auto      bool
	}{
		{"ready", state.Ready, true, true, false, state.Configuring, true},
		{"connecting", state.Connecting, true, true, false, state.Configuring, true},
		{"relay-degraded", state.DegradedRelay, true, true, false, state.Configuring, true},
		{"upstream-degraded", state.DegradedUpstream, true, true, false, state.Configuring, true},
		{"paused", state.Paused, true, true, false, state.Paused, false},
		{"configuring", state.Configuring, true, true, false, state.Configuring, false},
		{"bound-without-a-source", state.Ready, true, false, false, state.Configuring, false},
		{"unpaired", state.Unpaired, false, false, false, state.Unpaired, false},
		{"unpaired-with-source", state.Ready, false, true, false, state.Configuring, false},
		{"revoked-state", state.Revoked, true, true, false, state.Revoked, false},
		{"revoked-tombstone", state.Ready, true, true, true, state.Revoked, false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			opts := Options{DataDir: t.TempDir(), CloudBase: control.URL, EdgeBase: control.URL}
			provisionUpgradeIntent(t, opts, tc.bound, tc.source)
			if tc.tombstone {
				writeUpgradeIntentFile(t, opts, fileRevoked, []byte("{}"))
			}
			writeUpgradeIntentFile(t, opts, fileState, []byte(`{"state":"`+tc.saved+`"}`))

			for boot := 1; boot <= 2; boot++ {
				instance := newUpgradeIntentAgent(t, opts)
				status := instance.Status()
				wantBound := tc.bound && tc.want != state.Revoked
				if status.State != tc.want || status.Bound != wantBound || instance.ShouldAutoConnect() != tc.auto {
					t.Errorf("boot %d: state=%s bound=%t automatic=%t; want %s, %t, %t",
						boot, status.State, status.Bound, instance.ShouldAutoConnect(), tc.want, wantBound, tc.auto)
				}
				if boot == 1 && tc.name == "bound-without-a-source" {
					// The automatically provisioned demo must not become a
					// business source for interpreting a legacy record.
					if err := instance.StartBuiltinDemo(); err != nil {
						t.Fatal(err)
					}
				}
				instance.Close()
			}
		})
	}
}

func TestUpgradeRuntimeRetriesAfterInterruptedStartup(t *testing.T) {
	control := newUpgradeIntentControl(t)
	opts := control.options(t)
	provisionUpgradeIntent(t, opts, true, true)
	writeUpgradeIntentFile(t, opts, fileState, []byte(`{"state":"READY"}`))

	for boot := 1; boot <= 2; boot++ {
		instance := newUpgradeIntentAgent(t, opts)
		instance.StartRuntime()
		select {
		case <-control.desired:
		case <-time.After(6 * time.Second):
			t.Fatalf("boot %d: StartRuntime did not attempt to recover the tunnel", boot)
		}
		if !instance.Status().BuiltinDemo.Running {
			t.Fatal("local demo did not start independently")
		}
		interruptUpgradeStartup(t, opts, instance)
	}
}

func TestUpgradeExplicitChoiceSupersedesStartup(t *testing.T) {
	for _, choice := range []string{"pause", "disconnect", "revoke", "connect"} {
		for _, timing := range []string{"before", "during"} {
			t.Run(choice+"/"+timing, func(t *testing.T) {
				control := newUpgradeIntentControl(t)
				opts := control.options(t)
				provisionUpgradeIntent(t, opts, true, true)
				writeUpgradeIntentFile(t, opts, fileState, []byte(`{"state":"DEGRADED_RELAY"}`))
				instance := newUpgradeIntentAgent(t, opts)

				if timing == "during" {
					instance.StartRuntime()
					select {
					case <-control.desired:
					case <-time.After(6 * time.Second):
						t.Fatal("startup did not reach the held control-plane request")
					}
				}

				ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
				defer cancel()
				selected := make(chan struct{})
				go func() {
					defer close(selected)
					switch choice {
					case "pause":
						instance.Pause(ctx)
					case "disconnect":
						instance.Disconnect(ctx)
					case "revoke":
						_, _ = instance.Revoke(ctx)
					case "connect":
						_, _ = instance.Connect(ctx)
					}
				}()
				if timing == "during" {
					select {
					case <-control.aborted:
					case <-time.After(4 * time.Second):
						t.Error("the explicit action did not cancel the held startup request")
					}
				}
				control.unblock()
				select {
				case <-selected:
				case <-time.After(5 * time.Second):
					t.Fatal("the explicit action did not return")
				}

				if timing == "before" {
					instance.StartRuntime()
					// Invoke the same startup worker again synchronously so a
					// delayed worker cannot be mistaken for no work.
					instance.AutoConnect()
				}
				wantState := state.Paused
				wantNext := state.Paused
				wantCalls := int32(0)
				if timing == "during" {
					wantCalls++
				}
				switch choice {
				case "disconnect":
					wantState, wantNext = state.Configuring, state.Configuring
				case "revoke":
					wantState, wantNext = state.Revoked, state.Revoked
				case "connect":
					// The synthetic control plane declines the request.
					wantState, wantNext = state.DegradedRelay, state.Configuring
					wantCalls++
				}
				status := instance.Status()
				if status.State != wantState || status.FrpcRunning || status.TunnelConnected {
					t.Errorf("after %s: state=%s running=%t connected=%t",
						choice, status.State, status.FrpcRunning, status.TunnelConnected)
				}
				instance.Close()
				// Close joins resource shutdown. Any late startup attempt is
				// also forced through the synchronous AutoConnect above.
				if got := control.calls.Load(); got != wantCalls {
					t.Errorf("desired-state requests=%d, want %d", got, wantCalls)
				}
				restarted := newUpgradeIntentAgent(t, opts)
				if got := restarted.Status().State; got != wantNext || restarted.ShouldAutoConnect() != (choice == "connect") {
					t.Errorf("after restart: state=%s automatic=%t", got, restarted.ShouldAutoConnect())
				}
			})
		}
	}
}

func TestUpgradeCloudDrainAndRevocation(t *testing.T) {
	for _, action := range []string{"draining", "revoked", "device-revoked"} {
		t.Run(action, func(t *testing.T) {
			control := newUpgradeIntentControl(t)
			if action == "device-revoked" {
				control.revoked.Store(true)
			} else {
				control.response.Store(`{"` + action + `":true}`)
			}
			control.unblock()
			opts := control.options(t)
			provisionUpgradeIntent(t, opts, true, true)
			instance := newUpgradeIntentAgent(t, opts)
			if _, err := instance.Connect(context.Background()); err == nil {
				t.Fatalf("cloud %s should prevent connection", action)
			}
			instance.Close()
			restarted := newUpgradeIntentAgent(t, opts)
			if action == "draining" {
				if !restarted.ShouldAutoConnect() || !restarted.Status().Bound {
					t.Fatal("cloud drain became a local pause and suppressed the next startup attempt")
				}
			} else if got := restarted.Status(); got.State != state.Revoked || got.Bound || restarted.ShouldAutoConnect() {
				t.Fatalf("cloud revocation did not survive restart: state=%s bound=%t", got.State, got.Bound)
			}
		})
	}
}

func TestUpgradeRuntimeRestoresRelay(t *testing.T) {
	frpcPath := os.Getenv("HUAWI_INTENT_FRPC")
	frpsPath := os.Getenv("HUAWI_INTENT_FRPS")
	if frpcPath == "" || frpsPath == "" {
		t.Skip("set both relay test binary paths to run the real loopback relay case")
	}
	port := startUpgradeIntentRelay(t, frpsPath)
	control := newUpgradeIntentControl(t)
	opts := control.options(t)
	opts.FrpcBinary = frpcPath
	sourceID := provisionUpgradeIntent(t, opts, true, true)
	control.response.Store(fmt.Sprintf(`{"revision":1,"relay":{"address":"127.0.0.1","port":%d,"user":"synthetic","token":"intent-relay"},"sources":[{"source_id":%q,"stcp_secret":"intent-stcp"}]}`, port, sourceID))
	writeUpgradeIntentFile(t, opts, fileState, []byte(`{"state":"READY"}`))

	first := newUpgradeIntentAgent(t, opts)
	first.StartRuntime()
	select {
	case <-control.desired:
	case <-time.After(5 * time.Second):
		t.Fatal("first startup did not reach the control plane")
	}
	interruptUpgradeStartup(t, opts, first)
	select {
	case <-control.aborted:
	case <-time.After(5 * time.Second):
		t.Fatal("closing the first runtime did not interrupt the held request")
	}

	control.unblock()
	second := newUpgradeIntentAgent(t, opts)
	second.StartRuntime()
	waitForUpgradeIntent(t, "second startup did not recover the relay", func() bool {
		status := second.Status()
		return status.State == state.Ready && status.FrpcRunning && status.TunnelConnected
	})
	pidPath := filepath.Join(opts.DataDir, "frpc.pid")
	if _, err := os.Stat(pidPath); err != nil {
		t.Fatalf("no managed tunnel process: %v", err)
	}
	second.Pause(context.Background())
	if status := second.Status(); status.State != state.Paused || status.FrpcRunning || status.TunnelConnected {
		t.Fatalf("pause did not stop the running relay: %+v", status)
	}
	if _, err := os.Stat(pidPath); !os.IsNotExist(err) {
		t.Fatalf("managed relay still has a process record: %v", err)
	}
	second.Close()

	third := newUpgradeIntentAgent(t, opts)
	third.StartRuntime()
	third.AutoConnect()
	if status := third.Status(); status.State != state.Paused || status.FrpcRunning || !status.BuiltinDemo.Running {
		t.Fatalf("paused startup did not preserve pause and the local demo: %+v", status)
	}
	if got := control.calls.Load(); got != 2 {
		t.Fatalf("paused restart requested the tunnel: total requests=%d", got)
	}
	if status, err := third.Connect(context.Background()); err != nil || status.State != state.Ready || !status.TunnelConnected {
		t.Fatalf("explicit connect did not resume the paused relay: state=%s connected=%t err=%v", status.State, status.TunnelConnected, err)
	}
}

func startUpgradeIntentRelay(t *testing.T, binary string) int {
	t.Helper()
	port, err := freeLoopbackPort()
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "frps.toml")
	config := fmt.Sprintf("bindAddr = %q\nbindPort = %d\nauth.method = %q\nauth.token = %q\n", "127.0.0.1", port, "token", "intent-relay")
	if err := os.WriteFile(path, []byte(config), 0o600); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cmd := exec.CommandContext(ctx, binary, "-c", path)
	if err := cmd.Start(); err != nil {
		cancel()
		t.Fatal(err)
	}
	t.Cleanup(func() {
		cancel()
		_ = cmd.Wait()
	})
	waitForUpgradeIntent(t, "loopback relay did not start", func() bool {
		conn, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", port), 100*time.Millisecond)
		if err != nil {
			return false
		}
		_ = conn.Close()
		return true
	})
	return port
}

func waitForUpgradeIntent(t *testing.T, message string, ready func() bool) {
	t.Helper()
	deadline := time.Now().Add(8 * time.Second)
	for time.Now().Before(deadline) {
		if ready() {
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatal(message)
}

func interruptUpgradeStartup(t *testing.T, opts Options, instance *Agent) {
	t.Helper()
	// Capture what a sudden process exit would leave before Close can run.
	snapshot, err := os.ReadFile(filepath.Join(opts.DataDir, fileState))
	if err != nil {
		t.Fatal(err)
	}
	instance.Close()
	instance.connectMu.Lock()
	instance.connectMu.Unlock()
	writeUpgradeIntentFile(t, opts, fileState, snapshot)
}

func newUpgradeIntentAgent(t *testing.T, opts Options) *Agent {
	t.Helper()
	opts.diagnosticsFactory = func(string) (*diagnostics.Manager, error) {
		return nil, errors.New("diagnostics is outside this tunnel fixture")
	}
	instance, err := New(opts)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(instance.Close)
	return instance
}

func provisionUpgradeIntent(t *testing.T, opts Options, bound, source bool) string {
	t.Helper()
	instance := newUpgradeIntentAgent(t, opts)
	ctx := context.Background()
	if bound {
		if _, err := instance.Bind(ctx, "synthetic"); err != nil {
			t.Fatal(err)
		}
	}
	var sourceID string
	if source {
		saved, err := instance.SaveSource(SourceInput{
			DisplayName: "synthetic", UpstreamURL: opts.CloudBase + "/mcp", Tools: []string{"test"},
		})
		if err != nil {
			t.Fatal(err)
		}
		sourceID = saved.Source.SourceID
	}
	instance.Close()
	return sourceID
}

func writeUpgradeIntentFile(t *testing.T, opts Options, name string, body []byte) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(opts.DataDir, name), body, 0o600); err != nil {
		t.Fatal(err)
	}
}

type upgradeIntentControl struct {
	server   *httptest.Server
	desired  chan struct{}
	aborted  chan struct{}
	release  chan struct{}
	once     sync.Once
	calls    atomic.Int32
	response atomic.Value
	revoked  atomic.Bool
}

func newUpgradeIntentControl(t *testing.T) *upgradeIntentControl {
	t.Helper()
	control := &upgradeIntentControl{
		desired: make(chan struct{}, 8), aborted: make(chan struct{}, 8), release: make(chan struct{}),
	}
	control.response.Store("")
	control.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/v1/enrollments/redeem":
			_, _ = io.WriteString(w, `{"connector_id":"conn_1","tenant_id":"tenant_1","device_token":"synthetic"}`)
		case "/v1/sources":
			if control.revoked.Load() {
				w.WriteHeader(http.StatusForbidden)
				_, _ = io.WriteString(w, `{"error":{"code":"DEVICE_REVOKED","message":"synthetic revoked device"}}`)
				return
			}
			_, _ = io.WriteString(w, `{}`)
		case "/v1/connectors/conn_1/revoke", "/v1/connectors/conn_1/events":
			_, _ = io.WriteString(w, `{}`)
		case "/v1/connectors/conn_1/desired-state":
			control.calls.Add(1)
			control.desired <- struct{}{}
			select {
			case <-r.Context().Done():
				control.aborted <- struct{}{}
				return
			case <-control.release:
			}
			if body := control.response.Load().(string); body != "" {
				_, _ = io.WriteString(w, body)
			} else {
				http.Error(w, "synthetic relay unavailable", http.StatusServiceUnavailable)
			}
		case "/v1/connectors/conn_1/transport-secrets":
			_, _ = io.WriteString(w, control.response.Load().(string))
		case "/mcp":
			var request struct {
				ID     json.RawMessage `json:"id"`
				Method string          `json:"method"`
			}
			if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
				http.Error(w, "invalid request", http.StatusBadRequest)
				return
			}
			switch request.Method {
			case "initialize":
				_, _ = io.WriteString(w, `{"jsonrpc":"2.0","id":`+string(request.ID)+`,"result":{"protocolVersion":"2025-06-18","capabilities":{"tools":{}},"serverInfo":{"name":"synthetic","version":"1"}}}`)
			case "notifications/initialized":
				w.WriteHeader(http.StatusAccepted)
			case "tools/list":
				_, _ = io.WriteString(w, `{"jsonrpc":"2.0","id":`+string(request.ID)+`,"result":{"tools":[{"name":"test","inputSchema":{"type":"object"}}]}}`)
			default:
				http.NotFound(w, r)
			}
		default:
			if strings.HasPrefix(r.URL.Path, "/v1/connectors/conn_1/") {
				_, _ = io.WriteString(w, `{}`)
				return
			}
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(control.server.Close)
	t.Cleanup(control.unblock)
	return control
}

func (c *upgradeIntentControl) options(t *testing.T) Options {
	return Options{DataDir: t.TempDir(), CloudBase: c.server.URL, EdgeBase: c.server.URL}
}

func (c *upgradeIntentControl) unblock() {
	c.once.Do(func() { close(c.release) })
}
