package agent

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/cloud"
	"github.com/sherkevin/open-claude-code/connector/internal/diagnostics"
)

func assistTestOffer() cloud.AssistOffer {
	return cloud.AssistOffer{ID: testAssistID, Name: openAssistName, Status: "active", ExpiresAt: time.Now().Add(time.Minute)}
}

func assistEvents(instance *Agent, code string) []diagnostics.Event {
	var out []diagnostics.Event
	for _, view := range instance.DiagnosticLogs(diagnostics.Query{
		Component: diagnostics.ComponentAssist, Limit: 1000,
	}) {
		if view.EventCode == code {
			out = append(out, view.Event)
		}
	}
	return out
}

func waitForAssistEvent(t *testing.T, instance *Agent, code string, within time.Duration) diagnostics.Event {
	t.Helper()
	deadline := time.Now().Add(within)
	for {
		if events := assistEvents(instance, code); len(events) > 0 {
			return events[0]
		}
		if time.Now().After(deadline) {
			t.Fatalf("assistance event %s was not recorded", code)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func TestAssistObservabilityRecordsPollOutageAndRecovery(t *testing.T) {
	var polls atomic.Int32
	control := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.HasSuffix(r.URL.Path, "/start"):
			_ = json.NewEncoder(w).Encode(assistTestOffer())
		case strings.HasSuffix(r.URL.Path, "/poll"):
			if polls.Add(1) == 1 {
				http.Error(w, "synthetic poll outage", http.StatusBadGateway)
				return
			}
			// The control plane ended the session as soon as polling recovered.
			_ = json.NewEncoder(w).Encode(cloud.AssistPoll{Active: false})
		case strings.HasSuffix(r.URL.Path, "/stop"):
			_, _ = w.Write([]byte("{}"))
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(control.Close)
	f := &assistanceFixture{results: make(chan cloud.AssistResult, 1), cloudURL: control.URL}
	a := newAssistTestAgent(t, f)
	a.diagMgr.SetOwner("conn_assist", "tenant-one")

	started, err := a.OpenAssist(context.Background())
	if err != nil || started == nil || !started.LocalActive {
		t.Fatalf("could not open assistance: %+v %v", started, err)
	}
	waitForAssistEvent(t, a, "assist_session_inactive", 10*time.Second)

	if count := len(assistEvents(a, "assist_session_started")); count != 1 {
		t.Fatalf("start event count = %d, want 1", count)
	}
	failures := assistEvents(a, "assist_poll_failed")
	if len(failures) != 1 {
		t.Fatalf("first poll failure produced %d events, want 1", len(failures))
	}
	if failures[0].ReasonCode != "SERVER_ERROR" || failures[0].HTTPStatus != http.StatusBadGateway || failures[0].Attempt != 1 {
		t.Fatalf("poll failure was not classified with bounded fields: %+v", failures[0])
	}
	if count := len(assistEvents(a, "assist_poll_succeeded")); count != 1 {
		t.Fatalf("first successful poll count = %d, want 1", count)
	}
	recovered := assistEvents(a, "assist_poll_recovered")
	if len(recovered) != 1 || recovered[0].Attempt != 1 || recovered[0].DurationMS <= 0 {
		t.Fatalf("poll recovery was not recorded: %+v", recovered)
	}
	if count := len(assistEvents(a, "assist_session_inactive")); count != 1 {
		t.Fatalf("inactive end event count = %d, want 1", count)
	}

	operationID := failures[0].OperationID
	if !strings.HasPrefix(operationID, "op_") {
		t.Fatalf("missing correlation id: %+v", failures[0])
	}
	for _, event := range a.DiagnosticLogs(diagnostics.Query{Component: diagnostics.ComponentAssist, Limit: 1000}) {
		if event.OperationID != operationID {
			t.Fatalf("assistance lifecycle used a different correlation id: %+v", event)
		}
	}
	raw, err := json.Marshal(a.DiagnosticLogs(diagnostics.Query{Component: diagnostics.ComponentAssist, Limit: 1000}))
	if err != nil {
		t.Fatal(err)
	}
	for _, marker := range []string{"synthetic poll outage", control.URL, "127.0.0.1", testAssistID, testAssistToken} {
		if strings.Contains(string(raw), marker) {
			t.Fatalf("assistance diagnostics contained private data %q: %s", marker, raw)
		}
	}
	if stats := a.DiagnosticStats(); stats.PendingEvents != 0 {
		t.Fatalf("local-only assistance events entered the outbox: %+v", stats)
	}
}

func TestAssistObservabilityDistinguishesLocalStop(t *testing.T) {
	control := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.HasSuffix(r.URL.Path, "/start"):
			_ = json.NewEncoder(w).Encode(assistTestOffer())
		case strings.HasSuffix(r.URL.Path, "/poll"):
			_ = json.NewEncoder(w).Encode(cloud.AssistPoll{Active: true})
		case strings.HasSuffix(r.URL.Path, "/stop"):
			_, _ = w.Write([]byte("{}"))
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(control.Close)
	f := &assistanceFixture{results: make(chan cloud.AssistResult, 1), cloudURL: control.URL}
	a := newAssistTestAgent(t, f)

	started, err := a.OpenAssist(context.Background())
	if err != nil || started == nil {
		t.Fatalf("could not open assistance: %+v %v", started, err)
	}
	waitForAssistEvent(t, a, "assist_poll_succeeded", 5*time.Second)
	if err := a.StopAssist(context.Background(), started.ID); err != nil {
		t.Fatal(err)
	}
	waitForAssistEvent(t, a, "assist_session_stopped", 5*time.Second)
	if events := assistEvents(a, "assist_session_inactive"); len(events) != 0 {
		t.Fatalf("local stop was misreported as a server-inactive end: %+v", events)
	}
}

func TestAssistObservabilityRecordsProtocolViolation(t *testing.T) {
	control := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.HasSuffix(r.URL.Path, "/start"):
			_ = json.NewEncoder(w).Encode(assistTestOffer())
		case strings.HasSuffix(r.URL.Path, "/poll"):
			// A non-command job before any MCP target is a protocol violation.
			_ = json.NewEncoder(w).Encode(cloud.AssistPoll{
				Active: true,
				Job:    &cloud.AssistJob{ID: testAssistJobID, Action: "discover"},
			})
		case strings.HasSuffix(r.URL.Path, "/stop"):
			_, _ = w.Write([]byte("{}"))
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(control.Close)
	f := &assistanceFixture{results: make(chan cloud.AssistResult, 1), cloudURL: control.URL}
	a := newAssistTestAgent(t, f)

	if _, err := a.OpenAssist(context.Background()); err != nil {
		t.Fatal(err)
	}
	waitForAssistEvent(t, a, "assist_session_protocol_error", 5*time.Second)
}

func TestAssistObservabilityJobAndConfirmedReport(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "kept local", http.StatusUnauthorized)
	}))
	t.Cleanup(upstream.Close)
	target := cloud.AssistTarget{Name: "销售合同", URL: upstream.URL + "/mcp"}
	var jobSent atomic.Bool
	control := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.HasSuffix(r.URL.Path, "/start"):
			offer := assistTestOffer()
			offer.Name, offer.URL = target.Name, target.URL
			_ = json.NewEncoder(w).Encode(offer)
		case strings.HasSuffix(r.URL.Path, "/poll"):
			poll := cloud.AssistPoll{Active: true}
			if jobSent.CompareAndSwap(false, true) {
				poll.Job = &cloud.AssistJob{ID: testAssistJobID, Action: "discover", BearerToken: testAssistToken}
			}
			_ = json.NewEncoder(w).Encode(poll)
		case strings.HasSuffix(r.URL.Path, "/result"):
			_, _ = w.Write([]byte("{}"))
		case strings.HasSuffix(r.URL.Path, "/stop"):
			_, _ = w.Write([]byte("{}"))
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(control.Close)
	f := &assistanceFixture{results: make(chan cloud.AssistResult, 1), cloudURL: control.URL}
	a := newAssistTestAgent(t, f)

	started, err := a.StartAssistForTarget(context.Background(), target)
	if err != nil || started == nil {
		t.Fatalf("could not start assistance: %+v %v", started, err)
	}
	waitForAssistEvent(t, a, "assist_result_reported", 10*time.Second)
	if len(assistEvents(a, "assist_job_claimed")) != 1 || len(assistEvents(a, "assist_job_failed")) != 1 {
		t.Fatalf("job lifecycle was not recorded: %+v", a.DiagnosticLogs(diagnostics.Query{Component: diagnostics.ComponentAssist, Limit: 1000}))
	}
	claimed := assistEvents(a, "assist_job_claimed")[0]
	reported := assistEvents(a, "assist_result_reported")[0]
	if claimed.OperationID == "" || claimed.OperationID != reported.OperationID || reported.Attempt != 1 {
		t.Fatalf("job and report did not share one correlation id: %+v %+v", claimed, reported)
	}
	raw, err := json.Marshal(a.DiagnosticLogs(diagnostics.Query{Component: diagnostics.ComponentAssist, Limit: 1000}))
	if err != nil {
		t.Fatal(err)
	}
	for _, marker := range []string{testAssistToken, upstream.URL, "kept local", target.Name} {
		if strings.Contains(string(raw), marker) {
			t.Fatalf("job diagnostics contained private data %q: %s", marker, raw)
		}
	}
	if err := a.StopAssist(context.Background(), started.ID); err != nil {
		t.Fatal(err)
	}
}

func TestAssistObservabilityReportFailureIsNotSuccess(t *testing.T) {
	control := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte("{}"))
	}))
	t.Cleanup(control.Close)
	f := &assistanceFixture{results: make(chan cloud.AssistResult, 1), cloudURL: control.URL}
	a := newAssistTestAgent(t, f)
	session := &assistSession{
		client: a.cloud, connectorID: "conn_assist", operationID: "op_0011223344556677",
	}
	result := cloud.AssistResult{SessionID: testAssistID, JobID: testAssistJobID, Status: "succeeded"}

	confirmedCtx := context.Background()
	a.reportAndRecordAssistResult(confirmedCtx, session, result)
	reported := waitForAssistEvent(t, a, "assist_result_reported", 5*time.Second)
	if reported.Attempt != 1 || reported.ReasonCode != "" {
		t.Fatalf("confirmed report was misclassified: %+v", reported)
	}

	canceledCtx, cancel := context.WithCancel(context.Background())
	cancel()
	a.reportAndRecordAssistResult(canceledCtx, session, result)
	failed := waitForAssistEvent(t, a, "assist_result_report_failed", 5*time.Second)
	if failed.ReasonCode != "CANCELED" || failed.Attempt != 1 || failed.OperationID != session.operationID {
		t.Fatalf("unconfirmed receipt was not recorded as unknown: %+v", failed)
	}
}

func TestAssistWorksWithoutDiagnosticsManager(t *testing.T) {
	control := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.HasSuffix(r.URL.Path, "/start"):
			_ = json.NewEncoder(w).Encode(assistTestOffer())
		case strings.HasSuffix(r.URL.Path, "/poll"):
			_ = json.NewEncoder(w).Encode(cloud.AssistPoll{Active: false})
		case strings.HasSuffix(r.URL.Path, "/stop"):
			_, _ = w.Write([]byte("{}"))
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(control.Close)
	a, err := New(Options{
		DataDir: t.TempDir(), CloudBase: control.URL, NativeRelay: true,
		diagnosticsFactory: func(string) (*diagnostics.Manager, error) {
			return nil, errors.New("synthetic diagnostics init failure")
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(a.Close)
	client, err := cloud.NewWithSigner(control.URL, "existing-device-token", a.deviceKey)
	if err != nil {
		t.Fatal(err)
	}
	a.mu.Lock()
	a.cloud = client
	a.identity = cloud.Identity{ConnectorID: "conn_assist", DeviceToken: "existing-device-token"}
	a.mu.Unlock()

	started, err := a.OpenAssist(context.Background())
	if err != nil || started == nil || !started.LocalActive {
		t.Fatalf("missing diagnostics blocked assistance: %+v %v", started, err)
	}
	if logs := a.DiagnosticLogs(diagnostics.Query{Component: diagnostics.ComponentAssist}); logs != nil {
		t.Fatal("unexpected diagnostics without a manager")
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		status, statusErr := a.AssistStatus(context.Background())
		if statusErr == nil && status.Offer == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("assistance did not end without a diagnostics manager")
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func TestAssistObservabilityRecordsExpiryAndCancellation(t *testing.T) {
	a, err := New(Options{DataDir: t.TempDir(), NativeRelay: true})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(a.Close)

	expiredCtx, expiredCancel := context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
	defer expiredCancel()
	a.runAssist(expiredCtx, &assistSession{offer: assistTestOffer(), operationID: "op_0011223344556677"})
	waitForAssistEvent(t, a, "assist_session_expired", time.Second)

	canceledCtx, canceledCancel := context.WithCancel(context.Background())
	canceledCancel()
	a.runAssist(canceledCtx, &assistSession{offer: assistTestOffer(), operationID: "op_0011223344556677"})
	waitForAssistEvent(t, a, "assist_session_canceled", time.Second)

	if len(assistEvents(a, "assist_session_inactive")) != 0 ||
		len(assistEvents(a, "assist_session_protocol_error")) != 0 {
		t.Fatal("deadline or cancellation was misreported as a different end cause")
	}
}

func TestAssistPollOutageThrottleIsBounded(t *testing.T) {
	var outage assistPollOutage
	start := time.Now()
	logged := 0
	const days = 7
	step := 30 * time.Second
	for elapsed := time.Duration(0); elapsed < days*24*time.Hour; elapsed += step {
		if outage.failed(start.Add(elapsed)) {
			logged++
		}
	}
	if logged < 2 || logged > 200 {
		t.Fatalf("week-long outage produced %d events, want a bounded trace", logged)
	}
	failures, duration := outage.recovered(start.Add(days * 24 * time.Hour))
	if failures <= 0 || duration != days*24*time.Hour {
		t.Fatalf("recovery did not carry the outage duration/count: %d %v", failures, duration)
	}
}

func TestAssistJobEventCodeIsBounded(t *testing.T) {
	for _, tc := range []struct {
		result cloud.AssistResult
		want   string
	}{
		{cloud.AssistResult{Status: "succeeded"}, "assist_job_succeeded"},
		{cloud.AssistResult{Status: "failed", Code: "command_timeout"}, "assist_job_timeout"},
		{cloud.AssistResult{Status: "failed", Code: "canceled"}, "assist_job_canceled"},
		{cloud.AssistResult{Status: "failed", Code: "upstream_failed"}, "assist_job_failed"},
	} {
		if got := assistJobEventCode(tc.result); got != tc.want {
			t.Fatalf("job event code = %s, want %s", got, tc.want)
		}
	}
}

// persistedDiagnostics is the on-disk contract read by these tests. It mirrors
// the diagnostics package's private snapshot type.
type persistedDiagnostics struct {
	Logs   []diagnostics.Event `json:"logs"`
	Outbox []json.RawMessage   `json:"outbox"`
}

func readPersistedDiagnostics(t *testing.T, dir string) persistedDiagnostics {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(dir, "diagnostics.json"))
	if err != nil {
		t.Fatalf("read diagnostics: %v", err)
	}
	var saved persistedDiagnostics
	if err := json.Unmarshal(raw, &saved); err != nil {
		t.Fatalf("decode diagnostics: %v", err)
	}
	return saved
}

func waitForPersistedEvent(t *testing.T, dir, eventID string, within time.Duration) diagnostics.Event {
	t.Helper()
	deadline := time.Now().Add(within)
	for {
		if raw, err := os.ReadFile(filepath.Join(dir, "diagnostics.json")); err == nil {
			var saved persistedDiagnostics
			if json.Unmarshal(raw, &saved) == nil {
				for _, item := range saved.Logs {
					if item.EventID == eventID {
						return item
					}
				}
			}
		}
		if time.Now().After(deadline) {
			t.Fatalf("event %s was not persisted while the process was running", eventID)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func assertNoAssistInOutbox(t *testing.T, saved persistedDiagnostics) {
	t.Helper()
	for _, raw := range saved.Outbox {
		var item struct {
			Event diagnostics.Event `json:"event"`
		}
		if err := json.Unmarshal(raw, &item); err != nil {
			t.Fatal(err)
		}
		event := item.Event
		if event.Component == diagnostics.ComponentAssist || strings.HasPrefix(event.EventCode, "assist_") || event.ReasonCode != "" {
			t.Fatalf("local assistance event reached the upload outbox: %+v", event)
		}
	}
}

// This mirrors the real direct/interactive CLI lifecycle: exclusive diagnostics
// lock, local persistence started without the serving runtime, evidence on
// disk while the process is alive, and a final event flushed on Ctrl+C/Close.
func TestAssistLocalLifecyclePersistsWhileRunningAndOnClose(t *testing.T) {
	var uploads atomic.Int32
	control := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/events") {
			uploads.Add(1)
		}
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.HasSuffix(r.URL.Path, "/start"):
			_ = json.NewEncoder(w).Encode(assistTestOffer())
		case strings.HasSuffix(r.URL.Path, "/poll"):
			_ = json.NewEncoder(w).Encode(cloud.AssistPoll{Active: true})
		case strings.HasSuffix(r.URL.Path, "/stop"):
			_, _ = w.Write([]byte("{}"))
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(control.Close)
	dir := t.TempDir()
	f := &assistanceFixture{results: make(chan cloud.AssistResult, 1), cloudURL: control.URL}
	a := newAssistTestAgentDir(t, f, dir)

	started, err := a.OpenAssist(context.Background())
	if err != nil || started == nil {
		t.Fatalf("could not open assistance: %+v %v", started, err)
	}
	running := waitForAssistEvent(t, a, "assist_poll_succeeded", 5*time.Second)
	waitForPersistedEvent(t, dir, running.EventID, 3*time.Second)

	if err := a.StopAssist(context.Background(), started.ID); err != nil {
		t.Fatal(err)
	}
	stopped := waitForAssistEvent(t, a, "assist_session_stopped", 5*time.Second)
	a.Close()

	saved := readPersistedDiagnostics(t, dir)
	found := false
	for _, item := range saved.Logs {
		if item.EventID == stopped.EventID {
			found = true
		}
	}
	if !found {
		t.Fatal("the final stop event was not flushed on orderly shutdown")
	}
	assertNoAssistInOutbox(t, saved)
	if uploads.Load() != 0 {
		t.Fatalf("local-only assistance performed %d diagnostic uploads", uploads.Load())
	}
}

// A second direct/interactive process must not overwrite the store owned by a
// running service. It degrades to no local log while assistance keeps working.
func TestAssistLocalPersistenceDegradesWhenLockIsHeld(t *testing.T) {
	dir := t.TempDir()
	sentinel := []byte(`{"schema_version":1,"logs":[],"outbox":[]}`)
	if err := os.WriteFile(filepath.Join(dir, "diagnostics.json"), sentinel, 0o600); err != nil {
		t.Fatal(err)
	}
	holder, err := diagnostics.LockReporting(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer holder.Close()

	instance, err := New(Options{DataDir: dir, NativeRelay: true, ExclusiveDiagnostics: true})
	if err != nil {
		t.Fatal(err)
	}
	if !instance.diagnosticsBusy || instance.diagMgr != nil {
		t.Fatalf("contended agent initialized a writer: busy=%v", instance.diagnosticsBusy)
	}
	instance.StartLocalDiagnostics()
	if event := instance.recordLocalDiagnostic(diagnostics.Record{EventCode: "assist_session_started"}); event.EventID != "" {
		t.Fatal("contended agent recorded into the active store")
	}
	instance.Close()

	after, err := os.ReadFile(filepath.Join(dir, "diagnostics.json"))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(after, sentinel) {
		t.Fatalf("contended assistance overwrote the active store: %s", after)
	}
}

// Restart must restore the local assistance trace with its bounded fields while
// keeping every assistance event out of the durable upload outbox.
func TestAssistLocalLogSurvivesRestartWithoutOutbox(t *testing.T) {
	dir := t.TempDir()
	f := &assistanceFixture{results: make(chan cloud.AssistResult, 1), cloudURL: "http://127.0.0.1:1"}
	first := newAssistTestAgentDir(t, f, dir)
	event := first.recordLocalDiagnostic(diagnostics.Record{
		EventCode:   "assist_poll_recovered",
		OperationID: "op_0011223344556677",
		ReasonCode:  "TIMEOUT",
		DurationMS:  5,
		Attempt:     2,
	})
	if event.EventID == "" {
		t.Fatal("could not record a local assist event")
	}
	first.Close()

	reloaded, err := New(Options{DataDir: dir, NativeRelay: true, ExclusiveDiagnostics: true})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(reloaded.Close)
	if stats := reloaded.DiagnosticStats(); stats.PendingEvents != 0 {
		t.Fatalf("assist event entered the outbox on restart: %+v", stats)
	}
	saved := readPersistedDiagnostics(t, dir)
	assertNoAssistInOutbox(t, saved)
	for _, view := range reloaded.DiagnosticLogs(diagnostics.Query{Component: diagnostics.ComponentAssist, Limit: 10}) {
		if view.EventID == event.EventID {
			if view.ReasonCode != "TIMEOUT" || view.Attempt != 2 || view.DurationMS != 5 {
				t.Fatalf("bounded fields were not restored: %+v", view)
			}
			return
		}
	}
	t.Fatal("local assist event did not survive restart")
}
