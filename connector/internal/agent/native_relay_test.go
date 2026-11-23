package agent

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"
	"github.com/sherkevin/open-claude-code/connector/internal/cloud"
	"github.com/sherkevin/open-claude-code/connector/internal/state"
)

func TestNativeConnectReconnectsWithoutFRPAndPauseStopsIt(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 12*time.Second)
	defer cancel()
	upstream := fakeMCPToolServer(t, `[{"name":"stock","inputSchema":{"type":"object"}}]`)
	defer upstream.Close()
	var mu sync.Mutex
	registered := map[string]cloud.DesiredSource{}
	var rejectRegistration, denyExport atomic.Bool
	const traceID = "kwtr_0123456789abcdef0123456789abcdef"
	const privateCanary = "SYNTHETIC_PRIVATE_EXPORT_TOKEN"
	peers := make(chan *websocket.Conn, 2)
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer device-token" || r.Header.Get("X-Kaiwu-Signature") == "" {
			t.Error("device credentials were absent from the cloud request")
		}
		switch r.URL.Path {
		case "/v1/connectors/conn_1/native-relay":
			peer, err := websocket.Accept(w, r, nil)
			if err != nil {
				t.Error(err)
				return
			}
			defer peer.CloseNow()
			if err := wsjson.Write(ctx, peer, map[string]string{"type": "ready"}); err != nil {
				t.Error(err)
				return
			}
			peers <- peer
			<-ctx.Done()
		case "/v1/sources":
			var source cloud.SourceRegistration
			if err := json.NewDecoder(r.Body).Decode(&source); err != nil {
				t.Error(err)
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			if rejectRegistration.Load() {
				w.WriteHeader(http.StatusConflict)
				_, _ = w.Write([]byte(`{"error":{"code":"SOURCE_CONFIG_CONFLICT","message":"` + privateCanary + `","trace_id":"` + traceID + `"}}`))
				return
			}
			mu.Lock()
			registered[source.SourceID] = cloud.DesiredSource{
				SourceID: source.SourceID, Transport: source.Transport, AllowedTools: source.AllowedTools,
				TargetBindingSHA256: source.TargetBindingSHA256, ToolSchemaSHA256: source.ToolSchemaSHA256,
			}
			mu.Unlock()
			_, _ = w.Write([]byte(`{"ok":true}`))
		case "/v1/connectors/conn_1/desired-state":
			mu.Lock()
			desired := cloud.DesiredState{Sources: make([]cloud.DesiredSource, 0, len(registered))}
			for _, item := range registered {
				desired.Sources = append(desired.Sources, item)
			}
			mu.Unlock()
			_ = json.NewEncoder(w).Encode(desired)
		case "/v1/connectors/conn_1/mcp-export":
			if denyExport.Load() {
				w.WriteHeader(http.StatusForbidden)
				_, _ = w.Write([]byte(`{"error":{"code":"DEVICE_EXPORT_DENIED","message":"` + privateCanary + `","trace_id":"` + traceID + `"}}`))
				return
			}
			_, _ = w.Write([]byte(`{"mcp_config":{"mcpServers":{"stock":{"type":"http","url":"https://example.com/mcp","headers":{"Authorization":"Bearer ` + privateCanary + `"}}}}}`))
		default:
			t.Errorf("unexpected cloud route %s", r.URL.Path)
			http.NotFound(w, r)
		}
	}))
	defer func() { cancel(); server.Close() }()

	instance, err := New(Options{
		DataDir: t.TempDir(), CloudBase: server.URL, EdgeBase: server.URL + "/edge", NativeRelay: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer instance.Close()
	instance.identity = cloud.Identity{ConnectorID: "conn_1", DeviceToken: "device-token"}
	instance.cloud, err = cloud.NewWithSigner(server.URL, instance.identity.DeviceToken, instance.deviceKey)
	if err != nil {
		t.Fatal(err)
	}
	instance.cloud.HTTP = server.Client()
	instance.state.Transition(state.Configuring, "test identity")
	result, err := instance.SaveSourceAndConnect(ctx, SourceInput{
		DisplayName: "库存", UpstreamURL: upstream.URL, Tools: []string{"stock"},
	})
	if err != nil || result.ConnectionError != "" {
		t.Fatalf("save/connect failed: %v / %s", err, result.ConnectionError)
	}
	status := instance.Status()
	if status.State != state.Ready || !status.TunnelConnected || status.FrpcRunning || status.TransportMode != "native" {
		t.Fatalf("wrong native status: %+v", status)
	}
	if _, err := os.Stat(instance.store.Path(fileFrpc)); !os.IsNotExist(err) {
		t.Fatalf("native mode created an FRP configuration: %v", err)
	}
	if _, err := instance.ExportPlatformMCP(ctx, result.Source.SourceID); err != nil {
		t.Fatal(err)
	}
	if event := requireOperationEvent(t, instance, "platform_export_succeeded"); event.SourceID != result.Source.SourceID {
		t.Fatalf("export event lost its source ID: %+v", event)
	}
	denyExport.Store(true)
	if _, err := instance.ExportPlatformMCP(ctx, result.Source.SourceID); err == nil {
		t.Fatal("test export must be rejected")
	}
	if event := requireOperationEvent(t, instance, "platform_export_failed"); event.HTTPStatus != http.StatusForbidden || event.TraceID != traceID {
		t.Fatalf("export failure event lost safe HTTP metadata: %+v", event)
	}

	readPeer := func() *websocket.Conn {
		t.Helper()
		select {
		case peer := <-peers:
			return peer
		case <-ctx.Done():
			t.Fatal("cloud peer did not connect")
			return nil
		}
	}
	call := func(peer *websocket.Conn, id, body string) (int, string) {
		t.Helper()
		if err := wsjson.Write(ctx, peer, map[string]any{
			"type": "request", "id": id, "source_id": result.Source.SourceID,
			"headers": map[string]string{"Content-Type": "application/json"}, "body": []byte(body),
		}); err != nil {
			t.Fatal(err)
		}
		var response strings.Builder
		var code int
		for {
			var frame struct {
				Type   string
				ID     string
				Status int
				Body   []byte
			}
			if err := wsjson.Read(ctx, peer, &frame); err != nil {
				t.Fatal(err)
			}
			if frame.ID != id {
				t.Fatalf("received a different request ID: %s", frame.ID)
			}
			switch frame.Type {
			case "response":
				code = frame.Status
			case "chunk":
				response.Write(frame.Body)
			case "end":
				return code, response.String()
			default:
				t.Fatalf("unexpected relay response %s", frame.Type)
			}
		}
	}

	first := readPeer()
	if code, response := call(first, "test-1", `{"jsonrpc":"2.0","id":1,"method":"tools/list"}`); code != http.StatusOK || !strings.Contains(response, `"stock"`) {
		t.Fatalf("wrong MCP list response: %d %s", code, response)
	}
	_ = first.CloseNow()
	second := readPeer()
	if code, response := call(second, "test-2", `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"unapproved","arguments":{}}}`); code != http.StatusForbidden || !strings.Contains(response, "POLICY_DENIED") {
		t.Fatalf("unapproved call should be rejected: %d %s", code, response)
	}
	status = instance.Status()
	if status.TunnelRestarts != 1 || !status.TunnelConnected {
		t.Fatalf("wrong reconnect status: %+v", status)
	}
	status, err = instance.Pause(ctx)
	if err != nil || status.TunnelConnected || status.State != state.Paused {
		t.Fatalf("pause did not stop the native relay: %+v / %v", status, err)
	}
	if _, _, err := second.Read(ctx); err == nil || ctx.Err() != nil {
		t.Fatalf("pause did not close the socket: %v", err)
	}

	rejectRegistration.Store(true)
	if _, err := instance.Connect(ctx); err == nil {
		t.Fatal("test registration must be rejected")
	}
	if event := requireOperationEvent(t, instance, "connection_source_conflict"); event.HTTPStatus != http.StatusConflict || event.TraceID != traceID || event.SourceID == "" {
		t.Fatalf("source conflict event lost safe metadata: %+v", event)
	}
	assertNoDiagnosticValues(t, instance, privateCanary, upstream.URL)
}
