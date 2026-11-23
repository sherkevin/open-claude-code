//go:build !windows

package agent

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/cloud"
)

func TestAssistCommandIsExecutedAndReportedAutomatically(t *testing.T) {
	results := make(chan cloud.AssistResult, 1)
	var polls atomic.Int32
	var active atomic.Bool
	offer := cloud.AssistOffer{
		ID: testAssistID, Name: openAssistName, URL: "", Status: "active",
		ExpiresAt: time.Now().Add(time.Minute),
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/v1/connectors/conn_assist/assist/start":
			active.Store(true)
			_ = json.NewEncoder(w).Encode(offer)
		case "/v1/connectors/conn_assist/assist/poll":
			var job *cloud.AssistJob
			if active.Load() && polls.Add(1) == 1 {
				job = &cloud.AssistJob{ID: testAssistJobID, Action: "command", Command: "printf assist-ok", TimeoutSeconds: 10}
			}
			_ = json.NewEncoder(w).Encode(cloud.AssistPoll{Active: active.Load(), Job: job})
		case "/v1/connectors/conn_assist/assist/result":
			var result cloud.AssistResult
			if err := json.NewDecoder(r.Body).Decode(&result); err != nil {
				t.Error(err)
				http.Error(w, "bad result", http.StatusBadRequest)
				return
			}
			results <- result
			_, _ = w.Write([]byte("{}"))
		case "/v1/connectors/conn_assist/assist/stop":
			active.Store(false)
			_, _ = w.Write([]byte("{}"))
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)

	a, err := New(Options{DataDir: t.TempDir(), CloudBase: server.URL, NativeRelay: true})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(a.Close)
	client, err := cloud.NewWithSigner(server.URL, "existing-device-token", a.deviceKey)
	if err != nil {
		t.Fatal(err)
	}
	a.mu.Lock()
	a.cloud = client
	a.identity = cloud.Identity{ConnectorID: "conn_assist", DeviceToken: "existing-device-token"}
	a.mu.Unlock()

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	opened, err := a.OpenAssist(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if opened.ID != testAssistID || opened.URL != "" {
		t.Fatalf("unexpected offer: %+v", opened)
	}

	var result cloud.AssistResult
	select {
	case result = <-results:
	case <-ctx.Done():
		t.Fatal("command result was not reported")
	}
	if result.JobID != testAssistJobID || result.Status != "succeeded" || result.Code != "command_completed" {
		t.Fatalf("unexpected result: %+v", result)
	}
	if result.Output != "assist-ok" || result.ExitCode == nil || *result.ExitCode != 0 {
		t.Fatalf("unexpected output: %+v", result)
	}
	if result.DurationMS <= 0 {
		t.Fatalf("duration_ms = %d, want positive", result.DurationMS)
	}

	deadline := time.Now().Add(3 * time.Second)
	for {
		status, err := a.AssistStatus(ctx)
		if err != nil {
			t.Fatal(err)
		}
		if status.LastCommand != nil && status.LastCommand.ID == testAssistJobID && status.LastCommand.State == "succeeded" {
			if status.LastCommand.Output != "assist-ok" {
				t.Fatalf("local output = %q", status.LastCommand.Output)
			}
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("local command state was not updated: %+v", status.LastCommand)
		}
		time.Sleep(20 * time.Millisecond)
	}

	stopCtx, stopCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer stopCancel()
	if err := a.StopAssist(stopCtx, opened.ID); err != nil {
		t.Fatal(err)
	}
}
