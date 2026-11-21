package diagnostics

import (
	"context"
	"path/filepath"
	"strings"
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
