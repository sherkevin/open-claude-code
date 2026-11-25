package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/sherkevin/open-claude-code/connector/internal/diag"
)

func TestSavedSourceReplayReusesAuthAndRecognizesToolError(t *testing.T) {
	const fixtureToken = "local-test-fixture-only"
	const requestID = "8000041b-0000-e200-b63f-84710c7967bb"
	var toolCalls atomic.Int32
	var fail atomic.Bool
	fail.Store(true)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer "+fixtureToken ||
			r.Header.Get("User-Agent") != "kaiwu-intranet-connector-authproxy/1" ||
			r.Header.Get("Accept") != "application/json, text/event-stream" ||
			r.Header.Get("Content-Type") != "application/json" {
			t.Errorf("direct probe did not reuse saved auth or match proxy headers")
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		var req struct {
			ID     int64           `json:"id"`
			Method string          `json:"method"`
			Params json.RawMessage `json:"params"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			t.Error("invalid JSON request")
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		if req.Method != "initialize" && (r.Header.Get("Mcp-Session-Id") != "test-session" || r.Header.Get("MCP-Protocol-Version") != "2025-11-25") {
			t.Error("session or negotiated protocol not preserved")
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		var result any
		switch req.Method {
		case "initialize":
			w.Header().Set("Mcp-Session-Id", "test-session")
			result = map[string]any{"protocolVersion": "2025-11-25", "serverInfo": map[string]string{"name": "fixture", "version": "1"}}
		case "notifications/initialized":
			w.WriteHeader(http.StatusAccepted)
			return
		case "tools/list":
			result = map[string]any{"tools": []map[string]string{{"name": "query"}, {"name": "describe"}, {"name": "call"}}}
		case "tools/call":
			toolCalls.Add(1)
			var call struct {
				Name      string          `json:"name"`
				Arguments json.RawMessage `json:"arguments"`
			}
			if err := json.Unmarshal(req.Params, &call); err != nil ||
				call.Name != "query" ||
				string(call.Arguments) != `{"dataset":"erp_pur_order_type","select":[{"field":"Type_Name"}],"rows":1}` {
				t.Error("probe arguments changed")
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			result = map[string]any{"isError": fail.Load(), "content": []map[string]string{{"type": "text", "text": fmt.Sprintf(`{"code":"QUERY_EXECUTION_FAILED","message":"not for logs %s","request_id":"%s","data":["sensitive fixture"]}`, fixtureToken, requestID)}}}
		default:
			t.Errorf("unexpected method %q", req.Method)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"jsonrpc": "2.0", "id": req.ID, "result": result})
	}))
	defer upstream.Close()
	dir := t.TempDir()
	opts := Options{DataDir: dir, NativeRelay: true, ExclusiveDiagnostics: true}
	first, err := New(opts)
	if err != nil {
		t.Fatal(err)
	}
	saved, err := first.SaveSource(SourceInput{DisplayName: "test", UpstreamURL: upstream.URL + "/mcp", BearerToken: fixtureToken, Tools: []string{"query", "describe", "call"}})
	if err != nil {
		t.Fatal(err)
	}
	first.Close()
	reopened, err := New(opts)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	id := saved.Source.SourceID
	args := json.RawMessage(`{"dataset":"erp_pur_order_type","select":[{"field":"Type_Name"}],"rows":1}`)
	for _, tc := range []struct {
		source, tool string
		args         json.RawMessage
	}{
		{"", "query", args}, {id, "call", args}, {id, "query", json.RawMessage(`[]`)}, {id, "query", json.RawMessage(`null`)},
		{id, "query", json.RawMessage(strings.Repeat(" ", 17<<10))},
		{"src_00000000000000000000000000000000", "query", args},
	} {
		if _, err := reopened.DiagnoseWithArguments(context.Background(), tc.source, tc.tool, tc.args); err == nil {
			t.Fatal("invalid replay accepted")
		}
	}
	if toolCalls.Load() != 0 {
		t.Fatal("invalid requests must never execute a tool")
	}
	reports, err := reopened.DiagnoseWithArguments(context.Background(), id, "query", args)
	if err != nil || len(reports) != 1 {
		t.Fatalf("diagnostic failed: %v", err)
	}
	r := reports[0]
	last := r.Checks[len(r.Checks)-1]
	if r.OK || last.Status != diag.StatusFail || last.HTTPStatus != 200 || last.Code != "MCP_TOOL_ERROR" || last.UpstreamCode != "QUERY_EXECUTION_FAILED" || last.RequestID != requestID {
		t.Fatalf("HTTP 200 tool error must remain a failure: %+v", last)
	}
	serialized, _ := json.Marshal(reports)
	if strings.Contains(string(serialized), fixtureToken) || strings.Contains(string(serialized), "sensitive fixture") || strings.Contains(string(serialized), "not for logs") {
		t.Fatal("diagnostics leaked credential or business body")
	}
	fail.Store(false)
	reports, err = reopened.DiagnoseWithArguments(context.Background(), id, "query", args)
	if err != nil || len(reports) != 1 || !reports[0].OK || toolCalls.Load() != 2 {
		t.Fatalf("successful direct replay not recognized: %v", err)
	}
	if _, err := reopened.SetSourceEnabled(id, false); err != nil {
		t.Fatal(err)
	}
	if _, err := reopened.DiagnoseWithArguments(context.Background(), id, "query", args); err == nil {
		t.Fatal("disabled source executed")
	}
	if toolCalls.Load() != 2 {
		t.Fatal("disabled source must not execute")
	}
}
