package agent

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/diagnostics"
	"github.com/sherkevin/open-claude-code/connector/internal/state"
)

func TestAgentDiagnosticOperationsExcludeSecrets(t *testing.T) {
	instance, err := New(Options{
		DataDir:   t.TempDir(),
		CloudBase: "http://127.0.0.1:1",
		EdgeBase:  "https://mcp.example.com",
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(instance.Close)

	const (
		bearerCanary = "SYNTHETIC_BEARER_CANARY"
		hmacCanary   = "SYNTHETIC_HMAC_CANARY_0123456789"
		address      = "http://10.19.20.21/private-mcp"
	)
	if _, err := instance.SaveSource(SourceInput{
		DisplayName: "合成来源",
		UpstreamURL: address,
		BearerToken: bearerCanary,
		DynamicAuth: true,
		AppID:       "MCP",
		Channel:     "synthetic-channel",
		UserID:      "synthetic-user",
		HMACKey:     hmacCanary,
		Tools:       []string{"get_inventory"},
	}); err != nil {
		t.Fatal(err)
	}

	raw, err := json.Marshal(instance.DiagnosticLogs(diagnostics.Query{}))
	if err != nil {
		t.Fatal(err)
	}
	text := string(raw)
	for _, marker := range []string{bearerCanary, hmacCanary, address, "10.19.20.21", "synthetic-channel", "synthetic-user"} {
		if strings.Contains(text, marker) {
			t.Fatalf("sensitive marker reached the diagnostic log: %q", marker)
		}
	}
	if !strings.Contains(text, "MCP 服务配置已保存") {
		t.Fatalf("successful source operation was not recorded: %s", text)
	}
}

func TestSourceDiscoveryDiagnosticsCaptureStageAndSafeProtocolStatus(t *testing.T) {
	const remoteCanary = "SYNTHETIC_PRIVATE_MCP_RESPONSE"
	instance, err := New(Options{DataDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(instance.Close)

	unauthorized := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, remoteCanary, http.StatusUnauthorized)
	}))
	t.Cleanup(unauthorized.Close)
	if _, err := instance.DiscoverSourceTools(context.Background(), SourceInput{UpstreamURL: unauthorized.URL}); err == nil {
		t.Fatal("expected authentication failure")
	}
	event := requireOperationEvent(t, instance, "source_tools_initialize_failed")
	if event.HTTPStatus != http.StatusUnauthorized {
		t.Fatalf("initialize event did not record HTTP 401: %+v", event)
	}

	badList := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var request struct {
			Method string          `json:"method"`
			ID     json.RawMessage `json:"id"`
		}
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			http.Error(w, "invalid synthetic request", http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		switch request.Method {
		case "initialize":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"jsonrpc": "2.0", "id": request.ID,
				"result": map[string]string{"protocolVersion": "2025-06-18"},
			})
		case "notifications/initialized":
			w.WriteHeader(http.StatusAccepted)
		case "tools/list":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"jsonrpc": "2.0", "id": request.ID,
				"error": map[string]any{"code": -32601, "message": remoteCanary},
			})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(badList.Close)
	if _, err := instance.DiscoverSourceTools(context.Background(), SourceInput{UpstreamURL: badList.URL}); err == nil {
		t.Fatal("expected tools/list failure")
	}
	if event := requireOperationEvent(t, instance, "source_tools_list_failed"); event.RPCErrorCode != "-32601" {
		t.Fatalf("tool list event did not record the safe JSON-RPC code: %+v", event)
	}
	assertNoDiagnosticValues(t, instance, remoteCanary, unauthorized.URL, badList.URL)
}

func requireOperationEvent(t *testing.T, instance *Agent, code string) diagnostics.Event {
	t.Helper()
	for _, item := range instance.DiagnosticLogs(diagnostics.Query{}) {
		if item.EventCode == code {
			if item.OperationID == "" {
				t.Fatalf("event %s lacks an operation ID", code)
			}
			return item.Event
		}
	}
	t.Fatalf("event %s was not recorded", code)
	return diagnostics.Event{}
}

func assertNoDiagnosticValues(t *testing.T, instance *Agent, markers ...string) {
	t.Helper()
	raw, err := json.Marshal(instance.DiagnosticLogs(diagnostics.Query{}))
	if err != nil {
		t.Fatal(err)
	}
	for _, marker := range markers {
		if strings.Contains(string(raw), marker) {
			t.Fatal("connector diagnostics contained private test data")
		}
	}
}

func TestHeartbeatPayloadUsesBoundedStateCode(t *testing.T) {
	instance := &Agent{}
	const canary = "SYNTHETIC_STDERR http://10.19.20.21/private?credential=ABC"
	beat := instance.heartbeatPayload(Status{
		State:     state.DegradedRelay,
		LastError: canary,
	})
	if beat.LastError != "CONNECTION_DEGRADED_RELAY" {
		t.Fatalf("heartbeat must use a fixed classification, got %q", beat.LastError)
	}
	if strings.Contains(beat.LastError, "10.19.20.21") || strings.Contains(beat.LastError, "ABC") {
		t.Fatalf("raw error text reached heartbeat: %+v", beat)
	}
}

func TestDiagnosticsInitializationFailureIsVisible(t *testing.T) {
	dataDir := t.TempDir()
	instance, err := New(Options{
		DataDir:   dataDir,
		CloudBase: "http://127.0.0.1:1",
		EdgeBase:  "https://mcp.example.com",
		diagnosticsFactory: func(string) (*diagnostics.Manager, error) {
			return nil, errors.New("SYNTHETIC_DIAGNOSTICS_INIT_FAILURE")
		},
	})
	if err != nil {
		t.Fatalf("diagnostics failure must not block the MCP connector: %v", err)
	}
	t.Cleanup(instance.Close)

	status := instance.Status()
	if status.Diagnostics.StorageErrorCode != "LOCAL_STORAGE_FAILED" {
		t.Fatalf("diagnostics initialization failure was hidden: %+v", status.Diagnostics)
	}
	raw, _ := json.Marshal(status)
	if strings.Contains(string(raw), "SYNTHETIC_DIAGNOSTICS_INIT_FAILURE") || strings.Contains(string(raw), dataDir) {
		t.Fatalf("raw diagnostics initialization error leaked: %s", raw)
	}
	if _, err := instance.SaveSource(SourceInput{
		DisplayName: "初始化故障合成来源",
		UpstreamURL: "http://127.0.0.1:1/mcp",
		Tools:       []string{"get_inventory"},
	}); err != nil {
		t.Fatalf("diagnostics failure blocked source configuration: %v", err)
	}
}

func TestDiagnosticUploadSanitizesUnregisteredSourceThenCarriesRegisteredScope(t *testing.T) {
	type wireEvent struct {
		EventID     string `json:"event_id"`
		EventCode   string `json:"event_code"`
		OperationID string `json:"operation_id"`
		SourceID    string `json:"source_id"`
		ToolName    string `json:"tool_name"`
	}
	type uploadBatch struct {
		Events []wireEvent `json:"events"`
	}

	var upstreamDown atomic.Bool
	upstreamDown.Store(true)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if upstreamDown.Load() {
			http.Error(w, "synthetic unavailable", http.StatusServiceUnavailable)
			return
		}
		var request struct {
			ID     json.RawMessage `json:"id"`
			Method string          `json:"method"`
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
			_ = json.NewEncoder(w).Encode(map[string]any{
				"jsonrpc": "2.0",
				"id":      request.ID,
				"result": map[string]any{
					"protocolVersion": "2025-06-18",
					"serverInfo":      map[string]any{"name": "scope-test", "version": "1"},
				},
			})
		case "tools/list":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"jsonrpc": "2.0",
				"id":      request.ID,
				"result": map[string]any{
					"tools": []map[string]any{{"name": "get_inventory", "inputSchema": map[string]any{"type": "object"}}},
				},
			})
		default:
			http.NotFound(w, r)
		}
	}))
	defer upstream.Close()

	uploads := make(chan uploadBatch, 4)
	control := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.URL.Path == "/v1/enrollments/redeem":
			_, _ = w.Write([]byte(`{"connector_id":"conn_scope","tenant_id":"tenant_scope","tenant_name":"合成企业","device_token":"device-token","relay":{}}`))
		case r.URL.Path == "/v1/sources":
			_, _ = w.Write([]byte(`{}`))
		case r.URL.Path == "/v1/connectors/conn_scope/events":
			var batch uploadBatch
			if err := json.NewDecoder(r.Body).Decode(&batch); err != nil {
				http.Error(w, "bad request", http.StatusBadRequest)
				return
			}
			uploads <- batch
			_, _ = w.Write([]byte(`{"accepted":1}`))
		default:
			http.NotFound(w, r)
		}
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
	sourceID := saved.Source.SourceID
	if err := instance.startAuthProxies(); err != nil {
		t.Fatal(err)
	}
	defer instance.stopAuthProxies()

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	reports, err := instance.Diagnose(ctx, sourceID, "")
	if err != nil {
		t.Fatal(err)
	}
	if len(reports) != 1 || reports[0].OK {
		t.Fatalf("test precondition: first diagnosis must fail: %+v", reports)
	}
	if _, err := instance.diagMgr.UploadOnce(ctx); err != nil {
		t.Fatal(err)
	}
	first := <-uploads
	var firstFailure *wireEvent
	for index := range first.Events {
		event := &first.Events[index]
		if event.SourceID != "" || event.ToolName != "" {
			t.Fatalf("unregistered source scope reached the cloud batch: %+v", *event)
		}
		if event.EventCode == "diagnosis_failed" {
			firstFailure = event
		}
	}
	if firstFailure == nil || firstFailure.OperationID == "" {
		t.Fatalf("the first failed diagnosis was not uploaded without losing its operation id: %+v", first.Events)
	}

	upstreamDown.Store(false)
	if err := instance.registerSources(ctx, instance.runtimeSourcesLocked(), instance.proxies); err != nil {
		t.Fatal(err)
	}
	recorded := instance.recordDiagnostic(diagnostics.Record{
		EventCode:   "mcp_request_failed",
		OperationID: diagnostics.NewOperationID(),
		SourceID:    sourceID,
		ToolName:    "get_inventory",
	})
	if recorded.SourceID != sourceID || recorded.ToolName != "get_inventory" {
		t.Fatalf("approved local metadata was stripped too early: %+v", recorded)
	}
	if _, err := instance.diagMgr.UploadOnce(ctx); err != nil {
		t.Fatal(err)
	}
	second := <-uploads
	for _, event := range second.Events {
		if event.EventID == recorded.EventID {
			if event.SourceID != sourceID || event.ToolName != "get_inventory" {
				t.Fatalf("registered source scope was not preserved: %+v", event)
			}
			return
		}
	}
	t.Fatalf("registered event was not uploaded: %+v", second.Events)
}
