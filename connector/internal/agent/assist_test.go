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
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/cloud"
	"github.com/sherkevin/open-claude-code/connector/internal/huaweiauth"
	"github.com/sherkevin/open-claude-code/connector/internal/mcp"
)

const (
	testAssistID    = "asst_0123456789abcdef0123456789abcdef"
	testAssistJobID = "asjob_0123456789abcdef0123456789abcdef"
	testAssistToken = "synthetic-mcp-bearer"
)

type assistanceFixture struct {
	cloudURL  string
	active    atomic.Bool
	jobSent   atomic.Bool
	stopCalls atomic.Int32
	results   chan cloud.AssistResult
}

func newAssistanceFixture(t *testing.T, upstream string, rejectFirstStop bool) *assistanceFixture {
	t.Helper()
	f := &assistanceFixture{results: make(chan cloud.AssistResult, 2)}
	offer := cloud.AssistOffer{ID: testAssistID, Name: "销售合同", URL: upstream, ExpiresAt: time.Now().Add(time.Minute)}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer existing-device-token" || r.Header.Get("X-Kaiwu-Signature") == "" {
			http.Error(w, "expected signed device request", http.StatusUnauthorized)
			return
		}
		if r.Method != http.MethodGet {
			var data struct{ SessionID string }
			var wire map[string]json.RawMessage
			if err := json.NewDecoder(r.Body).Decode(&wire); err != nil ||
				json.Unmarshal(wire["session_id"], &data.SessionID) != nil || data.SessionID != offer.ID {
				t.Error("signed request is missing the approved session")
				http.Error(w, "invalid session", http.StatusBadRequest)
				return
			}
			if strings.HasSuffix(r.URL.Path, "/result") {
				for _, value := range wire {
					if strings.Contains(string(value), testAssistToken) || strings.Contains(string(value), "kept local") {
						t.Error("assistance returned a secret or raw upstream metadata")
					}
				}
				raw, _ := json.Marshal(wire)
				var result cloud.AssistResult
				if err := json.Unmarshal(raw, &result); err != nil {
					t.Error(err)
				}
				f.results <- result
			}
		}
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/v1/connectors/conn_assist/assist/offer":
			view := offer
			view.Status = "pending"
			if f.active.Load() {
				view.Status = "active"
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"offer": view})
		case "/v1/connectors/conn_assist/assist/start":
			f.active.Store(true)
			_, _ = w.Write([]byte("{}"))
		case "/v1/connectors/conn_assist/assist/poll":
			active := f.active.Load()
			var job *cloud.AssistJob
			if active && f.jobSent.CompareAndSwap(false, true) {
				job = &cloud.AssistJob{ID: testAssistJobID, Action: "discover", BearerToken: testAssistToken}
			}
			_ = json.NewEncoder(w).Encode(cloud.AssistPoll{Active: active, Job: job})
		case "/v1/connectors/conn_assist/assist/stop":
			if f.stopCalls.Add(1) == 1 && rejectFirstStop {
				w.WriteHeader(http.StatusBadGateway)
				return
			}
			f.active.Store(false)
			_, _ = w.Write([]byte("{}"))
		case "/v1/connectors/conn_assist/assist/result":
			_, _ = w.Write([]byte("{}"))
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	f.cloudURL = server.URL
	return f
}

func newAssistTestAgent(t *testing.T, f *assistanceFixture) *Agent {
	t.Helper()
	a, err := New(Options{DataDir: t.TempDir(), CloudBase: f.cloudURL, EdgeBase: "https://gateway.example.net", NativeRelay: true})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(a.Close)
	client, err := cloud.NewWithSigner(f.cloudURL, "existing-device-token", a.deviceKey)
	if err != nil {
		t.Fatal(err)
	}
	a.mu.Lock()
	a.cloud = client
	a.identity = cloud.Identity{ConnectorID: "conn_assist", DeviceToken: "existing-device-token"}
	a.mu.Unlock()
	return a
}

func TestAssistRequiresLocalConsentAndReportsBoundedDiscovery(t *testing.T) {
	var upstreamCalls atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstreamCalls.Add(1)
		if r.Header.Get("Authorization") != "Bearer "+testAssistToken || r.URL.Path != "/mcp" || r.Method != http.MethodPost {
			t.Error("unexpected request to the approved MCP service")
		}
		// The device should report the status and stage, without the upstream
		// body, URL credentials, or token in the job result.
		http.Error(w, "kept local", http.StatusUnauthorized)
	}))
	t.Cleanup(upstream.Close)
	f := newAssistanceFixture(t, upstream.URL+"/mcp", false)
	a := newAssistTestAgent(t, f)
	ctx := context.Background()
	if err := a.StartAssist(ctx, testAssistID); err == nil || f.active.Load() {
		t.Fatal("device accepted an offer that the local page never displayed")
	}
	offer, err := a.AssistOffer(ctx)
	if err != nil || offer == nil || offer.URL != upstream.URL+"/mcp" || offer.LocalActive || f.jobSent.Load() {
		t.Fatalf("unexpected pending offer: %+v %v", offer, err)
	}
	if err := a.StartAssist(ctx, testAssistID); err != nil {
		t.Fatal(err)
	}
	select {
	case got := <-f.results:
		if got.Status != "failed" || got.Code != "upstream_failed" || got.Stage != "initialize" ||
			got.HTTPStatus != http.StatusUnauthorized || got.Authorization == nil || !*got.Authorization ||
			upstreamCalls.Load() != 1 {
			t.Fatalf("unexpected bounded result: %+v", got)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("device did not post a discovery result")
	}
}

func TestAssistStopCancelsDiscoveryEvenWhenFirstNotificationFails(t *testing.T) {
	started := make(chan struct{})
	canceled := make(chan struct{})
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var prepare map[string]json.RawMessage
		_ = json.NewDecoder(r.Body).Decode(&prepare)
		close(started)
		<-r.Context().Done()
		close(canceled)
	}))
	t.Cleanup(upstream.Close)
	f := newAssistanceFixture(t, upstream.URL+"/mcp", true)
	a := newAssistTestAgent(t, f)
	ctx := context.Background()
	if _, err := a.AssistOffer(ctx); err != nil {
		t.Fatal(err)
	}
	if err := a.StartAssist(ctx, testAssistID); err != nil {
		t.Fatal(err)
	}
	select {
	case <-started:
	case <-time.After(5 * time.Second):
		t.Fatal("discovery did not start")
	}

	// Simulate the service restarting while the control plane still remembers
	// the previous grant. The replacement process must require fresh consent.
	reloaded := newAssistTestAgent(t, f)
	offer, err := reloaded.AssistOffer(ctx)
	if err != nil || offer == nil || offer.Status != "active" || offer.LocalActive ||
		reloaded.StartAssist(ctx, testAssistID) == nil {
		t.Fatal("a cloud grant silently resumed after restart")
	}
	if err := a.StopAssist(ctx, testAssistID); err == nil {
		t.Fatal("expected the first cloud stop notification to fail")
	}
	select {
	case <-canceled:
	case <-time.After(5 * time.Second):
		t.Fatal("device did not cancel the upstream request")
	}
	deadline := time.After(5 * time.Second)
	for f.active.Load() {
		select {
		case <-deadline:
			t.Fatal("device did not retry the failed stop notification")
		case <-time.After(10 * time.Millisecond):
		}
	}
	if offer, err := a.AssistOffer(ctx); err != nil || offer != nil {
		t.Fatalf("stopped session resurfaced: %+v %v", offer, err)
	}
	select {
	case result := <-f.results:
		t.Fatalf("stopped discovery published a result: %+v", result)
	default:
	}
}

func TestAssistRejectsUnapprovedToolSelectionAndAmbiguousTargets(t *testing.T) {
	if !validAssistSelection([]string{"get_order"}, []string{"get_order", "list_contracts"}) ||
		validAssistSelection([]string{"delete_order"}, []string{"get_order", "list_contracts"}) ||
		validAssistSelection([]string{"get_order", "get_order"}, []string{"get_order", "list_contracts"}) {
		t.Fatal("tool selection was not restricted to unique discovered names")
	}
	now := time.Now()
	offer := cloud.AssistOffer{ID: testAssistID, Name: "销售合同", ExpiresAt: now.Add(time.Minute)}
	for _, tc := range []struct {
		url   string
		valid bool
	}{
		{"https://fixture.example/mcp/sales", true},
		{"http://127.0.0.1:8091/mcp", true},
		{"http://10.192.0.9/mcp", false},
		{"https://fixture.example/mcp/sales?token=fixture", false},
		{"https://fixture.example/mcp/sales#", false},
		{"https://fixture.example/", false},
		{"https://fake-user@fixture.example/mcp", false},
		{"https://169.254.169.254/metadata", false},
	} {
		offer.URL = tc.url
		if got := validAssistOffer(offer, now); got != tc.valid {
			t.Errorf("offer validation for %q = %t, want %t", tc.url, got, tc.valid)
		}
	}
}

func TestAssistDynamicSigningStaysWithTheApprovedSource(t *testing.T) {
	const key = "synthetic-signing-secret"
	var calls atomic.Int32
	service := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			ID     int    `json:"id"`
			Method string `json:"method"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			t.Error(err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		if r.URL.Path != "/mcp/sales" || r.Header.Get("Authorization") != "Bearer "+testAssistToken {
			t.Error("request sent outside the approved service or without the service Bearer")
		}
		if req.Method != "tools/call" && r.Header.Get("sign") != "" {
			t.Error("discovery or session setup received a business-call signature")
		}
		w.Header().Set("Content-Type", "application/json")
		switch req.Method {
		case "initialize":
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{"protocolVersion":"2025-06-18","serverInfo":{"name":"synthetic"}}}`, req.ID)
		case "notifications/initialized":
			w.WriteHeader(http.StatusAccepted)
		case "tools/list":
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{"tools":[{"name":"read_contracts"}]}}`, req.ID)
		case "tools/call":
			calls.Add(1)
			if r.Header.Get("appid") != "MCP" || r.Header.Get("channel") != "sales" ||
				r.Header.Get("userid") != "reader" || r.Header.Get("requestId") == "" ||
				r.Header.Get("timestamp") == "" || len(r.Header.Get("sign")) != 64 {
				t.Error("business call did not receive the locally generated signature headers")
				w.WriteHeader(http.StatusUnauthorized)
				return
			}
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{"content":[{"type":"text","text":"ok"}]}}`, req.ID)
		default:
			w.WriteHeader(http.StatusBadRequest)
		}
	}))
	t.Cleanup(service.Close)
	a, err := New(Options{DataDir: t.TempDir(), NativeRelay: true})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(a.Close)
	ctx := context.Background()
	offer := cloud.AssistOffer{ID: testAssistID, Name: "销售合同", URL: service.URL + "/mcp/sales"}
	var draft *SourceInput
	var tools []string
	profile := &huaweiauth.Profile{Channel: " sales ", UserID: " reader ", HMACKey: key}
	result := a.runAssistJob(ctx, offer, cloud.AssistJob{
		ID: testAssistJobID, Action: "discover", BearerToken: testAssistToken, DynamicAuth: profile,
	}, &draft, &tools)
	if result.Code != "tools_loaded" || draft == nil || !draft.DynamicAuth || draft.AppID != "MCP" ||
		draft.Channel != "sales" || draft.UserID != "reader" || draft.HMACKey != key {
		t.Fatal("validated dynamic authentication was not kept with the discovery draft")
	}
	wire, err := json.Marshal(result)
	if err != nil || strings.Contains(string(wire), key) || strings.Contains(string(wire), testAssistToken) {
		t.Fatal("diagnostic response contained authentication material")
	}
	badConnect := a.runAssistJob(ctx, offer, cloud.AssistJob{
		ID: testAssistJobID, Action: "connect", Tools: tools, DynamicAuth: profile,
	}, &draft, &tools)
	if badConnect.Code != "connect_failed" || draft == nil || len(a.Status().Sources) != 0 {
		t.Fatal("connect unexpectedly allowed new signing credentials")
	}

	// Save the verified draft through the same source writer used by assisted
	// connect, then use that source's real signing proxy in a loopback test.
	draft.Tools = tools
	source, err := a.SaveSource(*draft)
	if err != nil {
		t.Fatal(err)
	}
	var profiles map[string]huaweiauth.Profile
	if err := a.store.ReadProtectedJSON(fileAuth, &profiles); err != nil {
		t.Fatal(err)
	}
	stored, found := profiles[source.Source.SourceID]
	if !found || stored.HMACKey != key {
		t.Fatal("the approved source's signing key was not stored with its protected local profile")
	}
	a.mu.RLock()
	headers := cloneHeaders(a.secrets[source.Source.SourceID])
	a.mu.RUnlock()
	signingProxy, err := newSourceAuthProxy(source.Source, headers, stored, true, nil)
	if err != nil {
		t.Fatal(err)
	}
	proxy := httptest.NewServer(signingProxy)
	t.Cleanup(proxy.Close)
	client := &mcp.Client{Endpoint: proxy.URL + "/mcp"}
	if _, err := client.Initialize(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := client.CallTool(ctx, "read_contracts", map[string]any{}); err != nil || calls.Load() != 1 {
		t.Fatalf("approved signed tool call failed: %v", err)
	}
}
