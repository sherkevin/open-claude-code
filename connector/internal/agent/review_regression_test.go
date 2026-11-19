package agent

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/state"
	"github.com/sherkevin/open-claude-code/connector/internal/supervisor"
)

func TestConnectReplacingRunningTunnelDoesNotDeadlockOnStopCallback(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("synthetic frpc process uses a POSIX shell script")
	}

	upstream := fakeMCPToolServer(t, `[{"name":"get_inventory","inputSchema":{"type":"object"}}]`)
	defer upstream.Close()

	var sourceMu sync.RWMutex
	var localSourceID string
	control := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.URL.Path == "/v1/enrollments/redeem":
			_, _ = w.Write([]byte(`{"connector_id":"conn_reconnect","tenant_id":"tenant_reconnect","tenant_name":"合成企业","device_token":"device-token","relay":{"address":"relay.example.com","port":7443,"user":"tenant_reconnect","token":"relay-token","protocol":"websocket"}}`))
		case r.URL.Path == "/v1/sources":
			_, _ = w.Write([]byte(`{}`))
		case r.URL.Path == "/v1/connectors/conn_reconnect/desired-state" ||
			r.URL.Path == "/v1/connectors/conn_reconnect/transport-secrets":
			sourceMu.RLock()
			sourceID := localSourceID
			sourceMu.RUnlock()
			_ = json.NewEncoder(w).Encode(map[string]any{
				"revision": 1,
				"relay": map[string]any{
					"address": "relay.example.com", "port": 7443,
					"user": "tenant_reconnect", "token": "relay-token", "protocol": "websocket",
				},
				"sources": []map[string]any{{
					"source_id": sourceID, "display_name": "范围测试来源",
					"stcp_secret": "synthetic-stcp", "allow_users": []string{"kaiwu"},
				}},
			})
		default:
			http.NotFound(w, r)
		}
	}))
	defer control.Close()

	dir := t.TempDir()
	binary := filepath.Join(dir, "frpc")
	if err := os.WriteFile(binary, []byte("#!/bin/sh\ntrap 'exit 0' INT TERM\nwhile :; do sleep 1; done\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	oldConfig := filepath.Join(dir, "old-frpc.toml")
	if err := os.WriteFile(oldConfig, []byte("synthetic = true\n"), 0o600); err != nil {
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
	t.Cleanup(instance.Close)
	if _, err := instance.Bind(context.Background(), "code-1"); err != nil {
		t.Fatal(err)
	}
	saved, err := instance.SaveSource(SourceInput{
		DisplayName: "范围测试来源",
		UpstreamURL: upstream.URL + "/mcp",
		Tools:       []string{"get_inventory"},
	})
	if err != nil {
		t.Fatal(err)
	}
	sourceMu.Lock()
	localSourceID = saved.Source.SourceID
	sourceMu.Unlock()
	if !instance.state.Transition(state.Connecting, "回归测试开始建立旧通道") ||
		!instance.state.Transition(state.Ready, "回归测试旧通道已就绪") {
		t.Fatalf("unexpected initial state: %s", instance.state.Current())
	}

	oldCtx, oldCancel := context.WithCancel(context.Background())
	defer oldCancel()
	old := supervisor.New(binary, oldConfig)
	old.OnChange = instance.onFrpChange
	instance.mu.Lock()
	instance.supervisor = old
	instance.mu.Unlock()
	if err := old.Start(oldCtx); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(2 * time.Second)
	for !old.Running() && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if !old.Running() {
		t.Fatal("synthetic old tunnel did not start")
	}

	t.Cleanup(func() {
		instance.mu.RLock()
		current := instance.supervisor
		stopHeartbeat := instance.heartbeat
		instance.mu.RUnlock()
		if stopHeartbeat != nil {
			stopHeartbeat()
		}
		if current != nil {
			_ = stopSupervisor(current)
		}
	})

	connectCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	result := make(chan error, 1)
	go func() {
		_, connectErr := instance.Connect(connectCtx)
		result <- connectErr
	}()
	select {
	case connectErr := <-result:
		if connectErr == nil || !strings.Contains(connectErr.Error(), "公网隧道尚未就绪") {
			t.Fatalf("reconnect did not complete after stopping the old callback: %v", connectErr)
		}
	case <-time.After(6 * time.Second):
		t.Fatal("Connect deadlocked while replacing a running tunnel")
	}
	if old.Running() {
		t.Fatal("the previous tunnel was not stopped")
	}
}
