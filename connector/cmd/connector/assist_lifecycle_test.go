package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/agent"
	"github.com/sherkevin/open-claude-code/connector/internal/diagnostics"
)

const cliTestOfferID = "asst_0123456789abcdef0123456789abcdef"

type cliPersistedEvent struct {
	EventCode   string `json:"event_code"`
	Component   string `json:"component"`
	ReasonCode  string `json:"reason_code"`
	OperationID string `json:"operation_id"`
}

type cliPersistedDiagnostics struct {
	Logs   []cliPersistedEvent `json:"logs"`
	Outbox []json.RawMessage   `json:"outbox"`
}

func assistEventSeen(instance *agent.Agent, code string) bool {
	for _, view := range instance.DiagnosticLogs(diagnostics.Query{
		Component: diagnostics.ComponentAssist, Limit: 1000,
	}) {
		if view.EventCode == code {
			return true
		}
	}
	return false
}

// TestAssistDirectCLIPersistsLocalLifecycle exercises the exact initialization
// used by `connector assist --direct` / `--interactive`: exclusive diagnostics
// lock, local-only persistence, loopback control plane, and an orderly close.
// It is the non-root NPX acceptance point for this task.
func TestAssistDirectCLIPersistsLocalLifecycle(t *testing.T) {
	dir := t.TempDir()
	var uploads atomic.Int32
	control := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/events") {
			uploads.Add(1)
		}
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.URL.Path == "/v1/enrollments/redeem":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"connector_id": "conn_0123456789abcdef0123456789abcdef",
				"tenant_id":    "tenant_cli",
				"tenant_name":  "CLI",
				"device_token": "cli-device-token",
			})
		case strings.HasSuffix(r.URL.Path, "/assist/start"):
			_ = json.NewEncoder(w).Encode(map[string]any{
				"id":         cliTestOfferID,
				"name":       "内网接入协助",
				"url":        "",
				"status":     "active",
				"expires_at": time.Now().Add(time.Minute).UTC(),
			})
		case strings.HasSuffix(r.URL.Path, "/assist/poll"):
			_ = json.NewEncoder(w).Encode(map[string]any{"active": false})
		case strings.HasSuffix(r.URL.Path, "/assist/offer"):
			_ = json.NewEncoder(w).Encode(map[string]any{"enabled": false})
		case strings.HasSuffix(r.URL.Path, "/assist/stop"):
			_, _ = w.Write([]byte("{}"))
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(control.Close)

	instance, err := newAssistCLIAgent(agent.Options{DataDir: dir, CloudBase: control.URL})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := instance.Bind(context.Background(), "cli-code"); err != nil {
		t.Fatalf("bind: %v", err)
	}
	offer, err := instance.OpenAssist(context.Background())
	if err != nil || offer == nil {
		t.Fatalf("open assist: %+v %v", offer, err)
	}

	deadline := time.Now().Add(10 * time.Second)
	for !assistEventSeen(instance, "assist_session_inactive") {
		if time.Now().After(deadline) {
			t.Fatal("assistance did not record its server-inactive end")
		}
		time.Sleep(20 * time.Millisecond)
	}
	instance.Close()

	raw, err := os.ReadFile(filepath.Join(dir, "diagnostics.json"))
	if err != nil {
		t.Fatalf("read diagnostics after close: %v", err)
	}
	var saved cliPersistedDiagnostics
	if err := json.Unmarshal(raw, &saved); err != nil {
		t.Fatal(err)
	}
	seenStarted, seenInactive := false, false
	for _, event := range saved.Logs {
		switch event.EventCode {
		case "assist_session_started":
			seenStarted = true
		case "assist_session_inactive":
			seenInactive = true
		}
	}
	if !seenStarted || !seenInactive {
		t.Fatalf("persisted log is missing the assistance lifecycle: %+v", saved.Logs)
	}
	for _, rawEvent := range saved.Outbox {
		var item struct {
			Event cliPersistedEvent `json:"event"`
		}
		if err := json.Unmarshal(rawEvent, &item); err != nil {
			t.Fatal(err)
		}
		event := item.Event
		if event.Component == "assist" || strings.HasPrefix(event.EventCode, "assist_") || event.ReasonCode != "" {
			t.Fatalf("assist event entered the upload outbox: %s", rawEvent)
		}
	}
	if uploads.Load() != 0 {
		t.Fatalf("direct CLI assistance uploaded %d diagnostic batches", uploads.Load())
	}
}
