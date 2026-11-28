package diagnostics

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestQueueReplaysAfterRestart(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "connector-data")
	first, err := New(dir)
	if err != nil {
		t.Fatal(err)
	}
	first.SetOwner("conn_11111111111111111111111111111111", "tenant-one")
	event := first.Record(Record{EventCode: "connector_started"})
	if !first.persistNow() {
		t.Fatal("test event was not persisted")
	}
	first.Close()

	reloaded, err := New(dir)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(reloaded.Close)
	if reloaded.Stats().PendingEvents != 1 || reloaded.Stats().PendingDurableEvents != 1 {
		t.Fatalf("restart did not restore a durable pending event: %+v", reloaded.Stats())
	}
	var sent []Event
	reloaded.SetUploader(func(_ context.Context, _ string, events []Event) error {
		sent = append(sent, events...)
		return nil
	})
	count, err := reloaded.UploadOnce(context.Background())
	if err != nil || count != 1 || len(sent) != 1 || sent[0].EventID != event.EventID {
		t.Fatalf("restart replay mismatch: count=%d sent=%+v err=%v", count, sent, err)
	}
}

func TestQueueIsBoundedAndPausedEventsResume(t *testing.T) {
	manager, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(manager.Close)
	manager.SetOwner("conn_11111111111111111111111111111111", "tenant-one")
	manager.maxQueueEvents = 2
	manager.Record(Record{EventCode: "connector_started"})
	manager.Record(Record{EventCode: "version_loaded"})
	manager.Record(Record{EventCode: "enrollment_succeeded"})
	stats := manager.Stats()
	if stats.PendingEvents != 2 || stats.DroppedEvents != 1 {
		t.Fatalf("queue bound mismatch: %+v", stats)
	}

	manager.SetPaused(true)
	manager.Record(Record{EventCode: "connection_paused"})
	if manager.Stats().LocalEventCount != 4 || manager.Stats().PendingEvents != 2 {
		t.Fatalf("paused mode must keep local logs and bounded queue: %+v", manager.Stats())
	}
	var delivered []Event
	manager.SetUploader(func(_ context.Context, _ string, events []Event) error {
		delivered = append(delivered, events...)
		return nil
	})
	if count, err := manager.UploadOnce(context.Background()); err != nil || count != 0 {
		t.Fatalf("paused manager must not upload: count=%d err=%v", count, err)
	}
	manager.SetPaused(false)
	if count, err := manager.UploadOnce(context.Background()); err != nil || count != 2 {
		t.Fatalf("resume must upload queued events: count=%d err=%v", count, err)
	}
	for _, event := range delivered {
		if strings.Contains(event.EventCode, "secret") {
			t.Fatal("unexpected free-form event code")
		}
	}
}

func TestSafePlatformViewsDropsUnknownAndUntrustedText(t *testing.T) {
	now := time.Now().UTC()
	valid := map[string]any{
		"schema_version": 1,
		"event_id":       "evt_11111111111111111111111111111111",
		"occurred_at":    now.Format(time.RFC3339Nano),
		"level":          "error",
		"component":      "platform",
		"event_code":     "platform_request_failed",
		"message_code":   "platform_request_failed",
		"trace_id":       "kwtr_11111111111111111111111111111111",
		"source_id":      "src_11111111111111111111111111111111",
		"tool_name":      "get_inventory",
		"message":        "<script>untrusted</script>",
	}
	unknown := cloneMap(valid)
	unknown["details"] = "synthetic-secret-canary"
	tenantField := cloneMap(valid)
	tenantField["tenant_id"] = "tenant-must-not-be-accepted"
	originField := cloneMap(valid)
	originField["origin"] = "connector"
	unsafeTool := cloneMap(valid)
	unsafeTool["tool_name"] = "private tool with spaces"
	wrongComponent := cloneMap(valid)
	wrongComponent["component"] = "connector"
	wrongCode := cloneMap(valid)
	wrongCode["event_code"] = "connection_succeeded"
	wrongCode["message_code"] = "connection_succeeded"
	views := SafePlatformViews([]map[string]any{
		valid,
		unknown,
		tenantField,
		originField,
		unsafeTool,
		wrongComponent,
		wrongCode,
	})
	if len(views) != 1 {
		t.Fatalf("unknown fields must be rejected, got %d views", len(views))
	}
	if views[0].Message != Catalog["platform_request_failed"].Message {
		t.Fatalf("server message must be replaced by the fixed catalog: %+v", views[0])
	}
	if views[0].ToolName != "get_inventory" {
		t.Fatalf("safe tool metadata was not preserved: %+v", views[0])
	}
}

func TestRPCErrorCodeIsDecimalJSONRPCNumberOnly(t *testing.T) {
	for _, value := range []string{"-32700", "-32500", "-32000"} {
		if got := safeRPCErrorCode(value); got != value {
			t.Fatalf("valid JSON-RPC code %q was rejected: %q", value, got)
		}
	}
	for _, value := range []string{
		"TIMEOUT",
		"TOOL_ERROR",
		"JSON_RPC_ERROR",
		"-32999",
		"-31999",
		"-032000",
		"+32000",
		"32000",
	} {
		if got := safeRPCErrorCode(value); got != "" {
			t.Fatalf("non-contract RPC code %q was accepted: %q", value, got)
		}
	}
}

func cloneMap(source map[string]any) map[string]any {
	out := make(map[string]any, len(source))
	for key, value := range source {
		out[key] = value
	}
	return out
}

func TestRecordLocalNeverEntersOutboxAndSurvivesRestart(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "connector-data")
	manager, err := New(dir)
	if err != nil {
		t.Fatal(err)
	}
	manager.SetOwner("conn_11111111111111111111111111111111", "tenant-one")
	queued := manager.Record(Record{EventCode: "connector_started"})
	local := manager.RecordLocal(Record{
		EventCode:   "assist_poll_failed",
		OperationID: "op_0011223344556677",
		ReasonCode:  "SERVER_ERROR",
		HTTPStatus:  502,
		Attempt:     3,
	})
	if local.Component != ComponentAssist || local.Level != LevelWarn || local.ReasonCode != "SERVER_ERROR" {
		t.Fatalf("local assist event was not normalized from the catalog: %+v", local)
	}
	stats := manager.Stats()
	if stats.PendingEvents != 1 || stats.LocalEventCount != 2 {
		t.Fatalf("local-only event reached the outbox: %+v", stats)
	}
	if !manager.persistNow() {
		t.Fatal("could not persist diagnostics")
	}
	manager.Close()

	reloaded, err := New(dir)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(reloaded.Close)
	stats = reloaded.Stats()
	if stats.PendingEvents != 1 || stats.PendingDurableEvents != 1 || stats.LocalEventCount != 2 {
		t.Fatalf("restart changed local/outbox separation: %+v", stats)
	}
	foundLocal := false
	for _, view := range reloaded.Logs(Query{Limit: 10}) {
		if view.EventID == local.EventID {
			foundLocal = view.OperationID == "op_0011223344556677" && view.ReasonCode == "SERVER_ERROR" && view.HTTPStatus == 502
		}
	}
	if !foundLocal {
		t.Fatal("local assist event was not restored with its bounded fields")
	}
	var sent []Event
	reloaded.SetUploader(func(_ context.Context, _ string, events []Event) error {
		sent = append(sent, events...)
		return nil
	})
	count, err := reloaded.UploadOnce(context.Background())
	if err != nil || count != 1 || len(sent) != 1 || sent[0].EventID != queued.EventID {
		t.Fatalf("upload selected a local-only event: count=%d sent=%+v err=%v", count, sent, err)
	}
}

func TestRecordLocalSanitizesUnknownCodesAndReasons(t *testing.T) {
	manager, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(manager.Close)
	manager.SetOwner("conn_11111111111111111111111111111111", "tenant-one")
	event := manager.RecordLocal(Record{
		EventCode:   "assist_unknown_synthetic",
		OperationID: "http://10.19.20.21/private",
		ReasonCode:  "http://10.19.20.21/private",
	})
	if event.EventCode != "operation_failed" || event.OperationID != "" || event.ReasonCode != "" {
		t.Fatalf("unbounded local input was not discarded: %+v", event)
	}
	stats := manager.Stats()
	if stats.PendingEvents != 0 || stats.LocalEventCount != 1 {
		t.Fatalf("rejected local event entered the outbox: %+v", stats)
	}
}

func TestRecordLocalIsConcurrentWithQueuedEvents(t *testing.T) {
	manager, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(manager.Close)
	manager.SetOwner("conn_11111111111111111111111111111111", "tenant-one")
	const workers = 20
	var group sync.WaitGroup
	group.Add(workers * 2)
	for index := 0; index < workers; index++ {
		go func() {
			defer group.Done()
			manager.RecordLocal(Record{EventCode: "assist_poll_succeeded", OperationID: "op_0011223344556677"})
		}()
		go func() {
			defer group.Done()
			manager.Record(Record{EventCode: "connector_started"})
		}()
	}
	group.Wait()
	stats := manager.Stats()
	if stats.LocalEventCount != workers*2 || stats.PendingEvents != workers {
		t.Fatalf("concurrent local/queued recording lost separation: %+v", stats)
	}
}

// The interactive NPX assistance path constructs a manager but does not call
// StartRuntime. The explicit local-only lifecycle must persist evidence while
// the process is alive without starting the uploader or install journal.
func TestRecordLocalPersistsWithoutServingRuntime(t *testing.T) {
	dir := t.TempDir()
	manager, err := New(dir)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(manager.Close)
	manager.SetOwner("conn_11111111111111111111111111111111", "tenant-one")
	manager.Record(Record{EventCode: "connector_started"})
	manager.StartLocalOnly()
	manager.Record(Record{EventCode: "enrollment_succeeded"})
	var uploads atomic.Int32
	manager.SetUploader(func(_ context.Context, _ string, _ []Event) error {
		uploads.Add(1)
		return nil
	})
	event := manager.RecordLocal(Record{EventCode: "assist_poll_failed", ReasonCode: "TIMEOUT"})
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		raw, err := os.ReadFile(filepath.Join(dir, fileDiagnostics))
		if err == nil {
			var saved persisted
			if json.Unmarshal(raw, &saved) == nil {
				for _, item := range saved.Logs {
					if item.EventID == event.EventID {
						if item.ReasonCode != "TIMEOUT" || len(saved.Outbox) != 1 || saved.Outbox[0].Event.EventID == event.EventID || uploads.Load() != 0 {
							t.Fatalf("local-only persistence started upload or lost metadata")
						}
						return
					}
				}
			}
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("interactive local diagnostic did not persist while the process was still alive")
}

// Close must make the final write even when the caller records and immediately
// exits, which is the normal Ctrl+C path for direct/interactive assistance.
func TestCloseFlushesLastLocalEvent(t *testing.T) {
	dir := t.TempDir()
	manager, err := New(dir)
	if err != nil {
		t.Fatal(err)
	}
	manager.SetOwner("conn_11111111111111111111111111111111", "tenant-one")
	manager.StartLocalOnly()
	event := manager.RecordLocal(Record{EventCode: "assist_session_canceled"})
	manager.Close()

	raw, err := os.ReadFile(filepath.Join(dir, fileDiagnostics))
	if err != nil {
		t.Fatalf("close did not flush the local log: %v", err)
	}
	var saved persisted
	if err := json.Unmarshal(raw, &saved); err != nil {
		t.Fatal(err)
	}
	found := false
	for _, item := range saved.Logs {
		if item.EventID == event.EventID {
			found = true
		}
	}
	if !found {
		t.Fatal("last local event was lost on orderly shutdown")
	}
	if len(saved.Outbox) != 0 {
		t.Fatalf("local-only event entered the outbox: %+v", saved.Outbox)
	}
}

// StartLocalOnly must never launch the uploader, even when a regular event is
// queued for an owner. Otherwise direct/interactive assistance would generate
// remote telemetry.
func TestStartLocalOnlyNeverUploads(t *testing.T) {
	manager, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(manager.Close)
	manager.SetOwner("conn_11111111111111111111111111111111", "tenant-one")
	var uploads atomic.Int32
	manager.SetUploader(func(_ context.Context, _ string, _ []Event) error {
		uploads.Add(1)
		return nil
	})
	manager.StartLocalOnly()
	manager.Record(Record{EventCode: "connector_started"})
	manager.RecordLocal(Record{EventCode: "assist_poll_succeeded"})
	time.Sleep(150 * time.Millisecond)
	if uploads.Load() != 0 {
		t.Fatalf("local-only lifecycle uploaded %d batches", uploads.Load())
	}
}

// Start after StartLocalOnly must not create a second persistence worker or
// panic on shutdown.
func TestStartAfterStartLocalOnlyIsIdempotent(t *testing.T) {
	manager, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	manager.StartLocalOnly()
	manager.StartLocalOnly()
	manager.Start()
	manager.Start()
	manager.RecordLocal(Record{EventCode: "assist_session_started"})
	manager.Close()
}

func TestAssistCatalogCannotEnterCloudOutbox(t *testing.T) {
	dir := t.TempDir()
	manager, err := New(dir)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(manager.Close)
	manager.SetOwner("conn_11111111111111111111111111111111", "tenant-one")
	local := manager.Record(Record{EventCode: "assist_poll_failed", ReasonCode: "TIMEOUT"})
	ordinary := manager.Record(Record{EventCode: "connector_started", ReasonCode: "TIMEOUT"})
	if ordinary.ReasonCode != "" {
		t.Fatal("local-only reason reached cloud event")
	}
	if manager.Stats().PendingEvents != 1 {
		t.Fatal("assist event entered outbox")
	}
	if !manager.persistNow() {
		t.Fatal("explicit synthetic queue persistence failed")
	}
	manager.Close()
	raw, err := os.ReadFile(filepath.Join(dir, fileDiagnostics))
	if err != nil {
		t.Fatal(err)
	}
	var saved persisted
	if err = json.Unmarshal(raw, &saved); err != nil {
		t.Fatal(err)
	}
	saved.Outbox = append(saved.Outbox, queuedEvent{OwnerConnectorID: saved.OwnerConnectorID, OwnerTenantID: saved.OwnerTenantID, Event: local})
	raw, err = json.Marshal(saved)
	if err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(filepath.Join(dir, fileDiagnostics), raw, 0600); err != nil {
		t.Fatal(err)
	}
	restored, err := New(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer restored.Close()
	if restored.Stats().PendingEvents != 1 {
		t.Fatal("local-only assist event was restored to outbox")
	}
	if event := manager.RecordLocal(Record{EventCode: "assist_poll_succeeded"}); event.EventID != "" {
		t.Fatal("closed manager accepted local event")
	}
}

func TestLocalOnlyPreservesOldQueueAndCannotUploadOrEnqueue(t *testing.T) {
	m, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer m.Close()
	m.SetOwner("conn_11111111111111111111111111111111", "tenant-one")
	m.Record(Record{EventCode: "connector_started"})
	var uploads atomic.Int32
	m.SetUploader(func(context.Context, string, []Event) error { uploads.Add(1); return nil })
	m.StartLocalOnly()
	m.Start()
	m.Record(Record{EventCode: "enrollment_succeeded"})
	m.RecordLocal(Record{EventCode: "assist_session_started"})
	if n, err := m.UploadOnce(context.Background()); n != 0 || err != nil {
		t.Fatalf("local-only upload: %d %v", n, err)
	}
	if m.Stats().PendingEvents != 1 || uploads.Load() != 0 {
		t.Fatal("local-only changed preexisting queue or uploaded")
	}
}
func TestUnstartedCloseDoesNotWrite(t *testing.T) {
	dir := t.TempDir()
	m, err := New(dir)
	if err != nil {
		t.Fatal(err)
	}
	m.Record(Record{EventCode: "connector_started"})
	m.Close()
	if _, err := os.Stat(filepath.Join(dir, fileDiagnostics)); !os.IsNotExist(err) {
		t.Fatalf("unstarted close wrote diagnostics: %v", err)
	}
}
