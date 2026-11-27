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
	cloudURL   string
	active     atomic.Bool
	jobSent    atomic.Bool
	offerCalls atomic.Int32
	stopCalls  atomic.Int32
	results    chan cloud.AssistResult
}

func newAssistanceFixture(t *testing.T, upstream string, rejectFirstStop, deviceFirst bool) *assistanceFixture {
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
			if err := json.NewDecoder(r.Body).Decode(&wire); err != nil {
				t.Error("signed request has no JSON body")
				http.Error(w, "invalid request", http.StatusBadRequest)
				return
			}
			if deviceFirst && strings.HasSuffix(r.URL.Path, "/start") {
				var target cloud.AssistTarget
				if len(wire) != 2 || json.Unmarshal(wire["name"], &target.Name) != nil || target.Name != offer.Name ||
					json.Unmarshal(wire["url"], &target.URL) != nil || target.URL != offer.URL {
					t.Error("device-first start should send only the approved name and address")
					http.Error(w, "invalid target", http.StatusBadRequest)
					return
				}
			} else if json.Unmarshal(wire["session_id"], &data.SessionID) != nil || data.SessionID != offer.ID {
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
			f.offerCalls.Add(1)
			view := offer
			view.Status = "pending"
			if f.active.Load() {
				view.Status = "active"
			}
			if deviceFirst {
				if r.URL.Query().Get("device_first") != "1" {
					t.Error("missing device-first capability query")
				}
				status := cloud.AssistStatus{Enabled: true}
				if f.active.Load() {
					status.Offer = &view
				}
				_ = json.NewEncoder(w).Encode(status)
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"offer": view})
		case "/v1/connectors/conn_assist/assist/start":
			f.active.Store(true)
			if deviceFirst {
				view := offer
				view.Status = "active"
				_ = json.NewEncoder(w).Encode(view)
				return
			}
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
	f := newAssistanceFixture(t, upstream.URL+"/mcp", false, false)
	a := newAssistTestAgent(t, f)
	ctx := context.Background()
	if err := a.StartAssist(ctx, testAssistID); err == nil || f.active.Load() {
		t.Fatal("device accepted an offer that the local page never displayed")
	}
	assist, err := a.AssistStatus(ctx)
	if err != nil || assist.Enabled || assist.Offer == nil || assist.Offer.URL != upstream.URL+"/mcp" || assist.Offer.LocalActive || f.jobSent.Load() {
		t.Fatalf("unexpected pending offer: %+v %v", assist, err)
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

func TestDeviceStartsAssistForLocalTargetWithoutPendingOffer(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/mcp" || r.Method != http.MethodPost ||
			r.Header.Get("Authorization") != "Bearer "+testAssistToken {
			t.Error("unexpected request to the locally chosen service")
		}
		http.Error(w, "kept local", http.StatusUnauthorized)
	}))
	t.Cleanup(upstream.Close)
	target := cloud.AssistTarget{Name: "销售合同", URL: upstream.URL + "/mcp"}
	f := newAssistanceFixture(t, target.URL, false, true)
	a := newAssistTestAgent(t, f)
	ctx := context.Background()

	assist, err := a.AssistStatus(ctx)
	if err != nil || !assist.Enabled || assist.Offer != nil || f.offerCalls.Load() != 1 || f.active.Load() {
		t.Fatalf("expected a device-first idle state: %+v %v", assist, err)
	}
	offer, err := a.StartAssistForTarget(ctx, target)
	if err != nil || offer == nil || offer.URL != target.URL || !offer.LocalActive {
		t.Fatalf("could not start from the local target: %+v %v", offer, err)
	}
	select {
	case result := <-f.results:
		if result.HTTPStatus != http.StatusUnauthorized || result.SessionID != offer.ID {
			t.Fatalf("unexpected result for the locally approved target: %+v", result)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("device did not send the discovery result")
	}
	if err := a.StopAssist(ctx, offer.ID); err != nil {
		t.Fatal(err)
	}
	assist, err = a.AssistStatus(ctx)
	if err != nil || !assist.Enabled || assist.Offer != nil || f.active.Load() {
		t.Fatalf("expected a device-first idle state after stopping: %+v %v", assist, err)
	}
}

func TestDeviceOpensAssistBeforeConfirmingMCP(t *testing.T) {
	var upstreamCalls atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstreamCalls.Add(1)
		if r.URL.Path != "/mcp" || r.Header.Get("Authorization") != "Bearer "+testAssistToken {
			t.Error("request escaped the approved MCP target")
		}
		http.Error(w, "kept local", http.StatusUnauthorized)
	}))
	t.Cleanup(upstream.Close)
	target := cloud.AssistTarget{Name: "销售合同", URL: upstream.URL + "/mcp"}
	offer := cloud.AssistOffer{ID: testAssistID, Name: openAssistName, Status: "active", ExpiresAt: time.Now().Add(time.Minute)}
	presence := make(chan struct{}, 2)
	f := &assistanceFixture{results: make(chan cloud.AssistResult, 1)}
	var confirmations atomic.Int32
	var targetReady atomic.Bool
	control := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		respond := json.NewEncoder(w).Encode
		switch r.URL.Path {
		case "/v1/connectors/conn_assist/assist/offer":
			if r.URL.Query().Get("device_first") != "1" || r.URL.Query().Get("target_after_start") != "1" {
				t.Error("device did not negotiate the new flow")
			}
			_ = respond(cloud.AssistStatus{Enabled: true, TargetAfterStart: true})
		case "/v1/connectors/conn_assist/assist/start":
			var body map[string]any
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body == nil || len(body) != 0 {
				t.Error("opening assistance must send an empty JSON object")
			}
			_ = respond(offer)
		case "/v1/connectors/conn_assist/assist/target":
			var body struct {
				SessionID string `json:"session_id"`
				cloud.AssistTarget
			}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.SessionID != offer.ID || body.AssistTarget != target {
				t.Error("device did not send the approved session and target")
			}
			view := offer
			view.Name, view.URL = target.Name, target.URL
			if confirmations.Add(1) == 1 {
				view.URL += "/substituted"
			}
			_ = respond(view)
		case "/v1/connectors/conn_assist/assist/poll":
			var body struct {
				SessionID  string `json:"session_id"`
				AcceptJobs *bool  `json:"accept_jobs"`
			}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.SessionID != offer.ID {
				t.Error("unexpected poll body")
			}
			view := cloud.AssistPoll{Active: true}
			if body.AcceptJobs != nil && !*body.AcceptJobs {
				t.Error("device must accept command jobs without an MCP target")
			}
			switch {
			case targetReady.Load() && f.jobSent.CompareAndSwap(false, true):
				view.Job = &cloud.AssistJob{ID: testAssistJobID, Action: "discover", BearerToken: testAssistToken}
			case !targetReady.Load():
				select {
				case presence <- struct{}{}:
				default:
				}
			}
			_ = respond(view)
		case "/v1/connectors/conn_assist/assist/result":
			var result cloud.AssistResult
			if err := json.NewDecoder(r.Body).Decode(&result); err != nil {
				t.Error(err)
			}
			f.results <- result
			_ = respond(struct{}{})
		case "/v1/connectors/conn_assist/assist/stop":
			_ = respond(struct{}{})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(control.Close)
	f.cloudURL = control.URL
	a := newAssistTestAgent(t, f)
	ctx := context.Background()

	status, err := a.AssistStatus(ctx)
	if err != nil || !status.Enabled || !status.TargetAfterStart || status.Offer != nil {
		t.Fatalf("expected independent assistance capability: %+v %v", status, err)
	}
	started, err := a.OpenAssist(ctx)
	if err != nil || started == nil || !started.LocalActive || started.URL != "" {
		t.Fatalf("could not open assistance without a target: %+v %v", started, err)
	}
	waitForPresence := func() {
		t.Helper()
		select {
		case <-presence:
		case <-time.After(6 * time.Second):
			t.Fatal("device did not poll without accepting jobs")
		}
	}
	waitForPresence()
	if _, err := a.SetAssistTarget(ctx, offer.ID, target); err == nil {
		t.Fatal("device accepted a substituted target from the server")
	}
	waitForPresence()
	different := cloud.AssistTarget{Name: target.Name, URL: target.URL + "/another"}
	if _, err := a.SetAssistTarget(ctx, offer.ID, different); err == nil || confirmations.Load() != 1 || upstreamCalls.Load() != 0 {
		t.Fatal("ambiguous confirmation allowed a different target or an upstream call")
	}
	confirmed, err := a.SetAssistTarget(ctx, offer.ID, target)
	if err != nil || confirmed == nil || confirmed.URL != target.URL || !confirmed.LocalActive {
		t.Fatalf("could not confirm the original target: %+v %v", confirmed, err)
	}
	targetReady.Store(true)
	select {
	case result := <-f.results:
		if result.HTTPStatus != http.StatusUnauthorized || result.SessionID != offer.ID || upstreamCalls.Load() != 1 {
			t.Fatalf("unexpected result for confirmed target: %+v", result)
		}
	case <-time.After(6 * time.Second):
		t.Fatal("device never ran the job for the confirmed target")
	}
	if err := a.StopAssist(ctx, offer.ID); err != nil {
		t.Fatal(err)
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
	f := newAssistanceFixture(t, upstream.URL+"/mcp", true, false)
	a := newAssistTestAgent(t, f)
	ctx := context.Background()
	if _, err := a.AssistStatus(ctx); err != nil {
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
	assist, err := reloaded.AssistStatus(ctx)
	if err != nil || assist.Offer == nil || assist.Offer.Status != "active" || assist.Offer.LocalActive ||
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
	if assist, err := a.AssistStatus(ctx); err != nil || assist.Offer != nil {
		t.Fatalf("stopped session resurfaced: %+v %v", assist, err)
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
	session := &assistSession{offer: offer}
	var draft *SourceInput
	var tools []string
	profile := &huaweiauth.Profile{Channel: " sales ", UserID: " reader ", HMACKey: key}
	result := a.runAssistJob(ctx, session, cloud.AssistJob{
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
	badConnect := a.runAssistJob(ctx, session, cloud.AssistJob{
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

func TestAssistWeekConsentIsFixedAndNeverSlides(t *testing.T) {
	deadline := time.Now().Add(assistWeekLifetime).UTC().Truncate(time.Millisecond)
	var polls atomic.Int32
	control := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		respond := json.NewEncoder(w).Encode
		switch r.URL.Path {
		case "/v1/connectors/conn_assist/assist/offer":
			if r.URL.Query().Get("consent") != cloud.AssistConsentWeek {
				t.Error("connector did not opt in to the week consent capability")
			}
			_ = respond(cloud.AssistStatus{Enabled: true, WeekConsent: true})
		case "/v1/connectors/conn_assist/assist/start":
			var body struct {
				ConsentPolicy string `json:"consent_policy"`
			}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.ConsentPolicy != cloud.AssistConsentWeek {
				t.Errorf("start did not confirm the negotiated week policy: %+v %v", body, err)
			}
			_ = respond(cloud.AssistOffer{
				ID: testAssistID, Name: openAssistName, Status: "active",
				ExpiresAt: deadline, ConsentPolicy: cloud.AssistConsentWeek, ConsentExpiresAt: &deadline,
			})
		case "/v1/connectors/conn_assist/assist/poll":
			polls.Add(1)
			_ = respond(cloud.AssistPoll{Active: true})
		case "/v1/connectors/conn_assist/assist/stop":
			_ = respond(struct{}{})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(control.Close)
	f := &assistanceFixture{results: make(chan cloud.AssistResult, 1), cloudURL: control.URL}
	a := newAssistTestAgent(t, f)
	ctx := context.Background()

	status, err := a.AssistStatus(ctx)
	if err != nil || !status.WeekConsent {
		t.Fatalf("week capability was not negotiated: %+v %v", status, err)
	}
	offer, err := a.OpenAssist(ctx)
	if err != nil || offer == nil || offer.ConsentPolicy != cloud.AssistConsentWeek ||
		offer.ConsentExpiresAt == nil || !offer.ConsentExpiresAt.Equal(deadline) {
		t.Fatalf("week consent was not kept: %+v %v", offer, err)
	}
	// Several polls must not move the fixed absolute deadline.
	wait := time.Now().Add(6 * time.Second)
	for polls.Load() < 2 && time.Now().Before(wait) {
		time.Sleep(20 * time.Millisecond)
	}
	local, err := a.AssistStatus(ctx)
	if err != nil || local.Offer == nil || !local.Offer.ExpiresAt.Equal(deadline) ||
		!local.Offer.ConsentDeadline().Equal(deadline) {
		t.Fatalf("polling moved the fixed week deadline: %+v %v", local, err)
	}
	if got := assistSessionDeadline(*offer, time.Now()); !got.Equal(deadline) {
		t.Fatalf("local safety deadline = %v, want %v", got, deadline)
	}
	if err := a.StopAssist(ctx, offer.ID); err != nil {
		t.Fatal(err)
	}
}

func TestAssistKeepsLegacyPolicyWhenControlPlaneDoesNotAdvertiseWeek(t *testing.T) {
	legacyExpiry := time.Now().Add(assistLifetime).UTC()
	control := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		respond := json.NewEncoder(w).Encode
		switch r.URL.Path {
		case "/v1/connectors/conn_assist/assist/offer":
			// An older control plane ignores the opt-in query and never
			// advertises week support.
			_ = respond(cloud.AssistStatus{Enabled: true})
		case "/v1/connectors/conn_assist/assist/start":
			var body map[string]json.RawMessage
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil || len(body) != 0 {
				t.Errorf("legacy start must stay empty, got %+v %v", body, err)
			}
			_ = respond(cloud.AssistOffer{
				ID: testAssistID, Name: openAssistName, Status: "active", ExpiresAt: legacyExpiry,
			})
		case "/v1/connectors/conn_assist/assist/poll":
			_ = respond(cloud.AssistPoll{Active: true})
		case "/v1/connectors/conn_assist/assist/stop":
			_ = respond(struct{}{})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(control.Close)
	f := &assistanceFixture{results: make(chan cloud.AssistResult, 1), cloudURL: control.URL}
	a := newAssistTestAgent(t, f)
	ctx := context.Background()

	status, err := a.AssistStatus(ctx)
	if err != nil || status.WeekConsent {
		t.Fatalf("legacy control plane must not advertise week consent: %+v %v", status, err)
	}
	offer, err := a.OpenAssist(ctx)
	if err != nil || offer == nil || offer.ConsentPolicy != "" || offer.ConsentExpiresAt != nil {
		t.Fatalf("legacy offer changed shape: %+v %v", offer, err)
	}
	if got := assistSessionDeadline(*offer, time.Now()); got.Before(time.Now().Add(assistMaxLifetime - time.Minute)) {
		t.Fatalf("legacy local deadline = %v, want about 24h", got)
	}
	if err := a.StopAssist(ctx, offer.ID); err != nil {
		t.Fatal(err)
	}
}

func TestAssistOutlivesTransientPollFailure(t *testing.T) {
	offer := cloud.AssistOffer{ID: testAssistID, Name: openAssistName, Status: "active", ExpiresAt: time.Now().Add(time.Minute)}
	var polls, stops atomic.Int32
	recovered := make(chan struct{}, 1)
	control := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		respond := json.NewEncoder(w).Encode
		switch r.URL.Path {
		case "/v1/connectors/conn_assist/assist/start":
			_ = respond(offer)
		case "/v1/connectors/conn_assist/assist/poll":
			if polls.Add(1) == 1 {
				// A control-plane blip: the first poll after opening fails.
				http.Error(w, "temporarily unavailable", http.StatusBadGateway)
				return
			}
			select {
			case recovered <- struct{}{}:
			default:
			}
			_ = respond(cloud.AssistPoll{Active: true})
		case "/v1/connectors/conn_assist/assist/stop":
			stops.Add(1)
			_ = respond(struct{}{})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(control.Close)
	f := &assistanceFixture{results: make(chan cloud.AssistResult, 1), cloudURL: control.URL}
	a := newAssistTestAgent(t, f)
	ctx := context.Background()
	started, err := a.OpenAssist(ctx)
	if err != nil || started == nil || !started.LocalActive {
		t.Fatalf("could not open assistance: %+v %v", started, err)
	}
	select {
	case <-recovered:
	case <-time.After(10 * time.Second):
		t.Fatal("device ended the session after a transient poll failure")
	}
	// Every successful poll renews the local lease alongside the server's,
	// so the status page and target confirmation do not see a stale expiry.
	deadline := time.Now().Add(5 * time.Second)
	for {
		status, err := a.AssistStatus(ctx)
		if err == nil && status.Offer != nil && status.Offer.ExpiresAt.After(offer.ExpiresAt) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("local lease did not follow the server's renewal: %+v %v", status, err)
		}
		time.Sleep(100 * time.Millisecond)
	}
	if stops.Load() != 0 {
		t.Fatal("device stopped the session after a transient failure")
	}
	if err := a.StopAssist(ctx, offer.ID); err != nil {
		t.Fatal(err)
	}
}
