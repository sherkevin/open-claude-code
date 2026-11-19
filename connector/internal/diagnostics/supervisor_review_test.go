// 总控审查回归用例：验证诊断补传、入队前隐私过滤、企业隔离及写盘故障边界。
// 本文件独立于工程师实现；所有凭据/工具/地址标记均为人工构造的测试值。
package diagnostics

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const (
	supervisorOwnerA  = "conn_11111111111111111111111111111111"
	supervisorOwnerB  = "conn_22222222222222222222222222222222"
	supervisorSourceA = "src_11111111111111111111111111111111"
	supervisorSourceB = "src_22222222222222222222222222222222"
)

func supervisorReviewManager(t *testing.T) *Manager {
	t.Helper()
	m, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(m.Close)
	return m
}

func supervisorReviewEventually(t *testing.T, timeout time.Duration, condition func() bool, message string) {
	t.Helper()
	if condition() {
		return
	}
	ticker := time.NewTicker(20 * time.Millisecond)
	defer ticker.Stop()
	deadline := time.NewTimer(timeout)
	defer deadline.Stop()
	for {
		select {
		case <-ticker.C:
			if condition() {
				return
			}
		case <-deadline.C:
			t.Fatal(message)
		}
	}
}

// Only the original event wakes the uploader. A transient first failure must
// arrange its own retry; no Record/SetUploader/SetOwner is called after Start.
func TestSupervisorReviewRetriesWithoutNewEvents(t *testing.T) {
	m := supervisorReviewManager(t)
	m.flushInterval = 25 * time.Millisecond
	m.uploadTimeout = time.Second
	m.SetOwner(supervisorOwnerA, "supervisor-tenant-a")

	firstAttempt := make(chan struct{}, 1)
	retryAttempt := make(chan []Event, 1)
	var attempts atomic.Int32
	m.SetUploader(func(_ context.Context, owner string, events []Event) error {
		if owner != supervisorOwnerA {
			return errors.New("unexpected test owner")
		}
		if attempts.Add(1) == 1 {
			firstAttempt <- struct{}{}
			return errors.New("synthetic temporary network failure")
		}
		select {
		case retryAttempt <- append([]Event(nil), events...):
		default:
		}
		return nil
	})
	event := m.Record(Record{EventCode: "connector_started"})
	m.Start()

	select {
	case <-firstAttempt:
	case <-time.After(3 * time.Second):
		t.Fatal("the initial event was not uploaded")
	}

	select {
	case batch := <-retryAttempt:
		if len(batch) != 1 || batch[0].EventID != event.EventID {
			t.Fatalf("retry must preserve the original event identity; batch size=%d", len(batch))
		}
	case <-time.After(3 * time.Second):
		t.Fatal("upload did not retry after a transient failure without a new Record or configuration wake")
	}
	supervisorReviewEventually(t, time.Second, func() bool {
		return m.Stats().PendingEvents == 0
	}, "acknowledged retry remained in the pending outbox")
}

// Syntax-valid strings are not necessarily safe metadata. No local source/tool
// approval has been registered in these cases, so the tool values are untrusted.
func TestSupervisorReviewFiltersUntrustedFieldsBeforeStorageAndUpload(t *testing.T) {
	cases := []struct {
		name   string
		marker string
		input  Record
	}{
		{
			name:   "arbitrary_rpc_code",
			marker: "SUPERVISOR_HMAC_SECRET_ABC123",
			input:  Record{EventCode: "mcp_request_failed", SourceID: supervisorSourceA, RPCErrorCode: "SUPERVISOR_HMAC_SECRET_ABC123"},
		},
		{
			name:   "url_in_tool_name",
			marker: "http://10.19.20.21/private-mcp",
			input:  Record{EventCode: "mcp_request_failed", SourceID: supervisorSourceA, ToolName: "http://10.19.20.21/private-mcp"},
		},
		{
			name:   "syntactically_valid_but_unapproved_tool",
			marker: "SUPERVISOR_PRIVATE_TOOL_ABC123",
			input:  Record{EventCode: "mcp_request_failed", SourceID: supervisorSourceA, ToolName: "SUPERVISOR_PRIVATE_TOOL_ABC123"},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			m := supervisorReviewManager(t)
			m.SetOwner(supervisorOwnerA, "supervisor-tenant-a")
			var wire []Event
			m.SetUploader(func(_ context.Context, _ string, events []Event) error {
				wire = append(wire, events...)
				return nil
			})
			m.Record(tc.input)
			local, err := json.Marshal(m.Logs(Query{}))
			if err != nil {
				t.Fatal(err)
			}
			if strings.Contains(string(local), tc.marker) {
				t.Error("untrusted marker reached the local diagnostic log")
			}
			if _, err := m.UploadOnce(context.Background()); err != nil {
				t.Fatal(err)
			}
			encoded, err := json.Marshal(wire)
			if err != nil {
				t.Fatal(err)
			}
			if strings.Contains(string(encoded), tc.marker) {
				t.Error("untrusted marker reached the upload batch; server-side rejection would be too late")
			}
		})
	}
}

func TestSupervisorReviewOwnerChangeIsolatesLogsAndSyncStatus(t *testing.T) {
	m := supervisorReviewManager(t)
	m.SetOwner(supervisorOwnerA, "supervisor-tenant-a")
	m.SetUploader(func(context.Context, string, []Event) error { return nil })
	old := m.Record(Record{EventCode: "source_saved", SourceID: supervisorSourceA})
	if _, err := m.UploadOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	if m.Stats().LastSuccessAt == nil {
		t.Fatal("test precondition: owner A must have a confirmed successful upload")
	}

	m.SetOwner(supervisorOwnerB, "supervisor-tenant-b")
	for _, view := range m.Logs(Query{}) {
		if view.EventID == old.EventID || view.SourceID == supervisorSourceA {
			t.Error("owner B's current local log contains owner A's event metadata")
		}
	}
	if m.Stats().LastSuccessAt != nil {
		t.Error("owner B inherited owner A's successful synchronization timestamp")
	}

	current := m.Record(Record{EventCode: "source_saved", SourceID: supervisorSourceB})
	var delivered []Event
	m.SetUploader(func(_ context.Context, owner string, events []Event) error {
		if owner != supervisorOwnerB {
			return errors.New("upload after owner switch used the previous connector")
		}
		delivered = append(delivered, events...)
		return nil
	})
	if _, err := m.UploadOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	foundCurrent := false
	for _, event := range delivered {
		if event.EventID == old.EventID || event.SourceID == supervisorSourceA {
			t.Error("owner A's event was uploaded after switching to owner B")
		}
		if event.EventID == current.EventID {
			foundCurrent = true
		}
	}
	if !foundCurrent {
		t.Error("owner B's new event did not reach its upload batch")
	}
}

// Replacing the log directory with a regular file produces a deterministic
// storage error even when tests run with elevated filesystem privileges.
func TestSupervisorReviewStorageFailureIsVisible(t *testing.T) {
	root := t.TempDir()
	dir := filepath.Join(root, "connector-data")
	m, err := New(dir)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(m.Close)
	m.SetOwner(supervisorOwnerA, "supervisor-tenant-a")
	backup := dir + "-before-fault"
	if err := os.Rename(dir, backup); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(dir, []byte("synthetic unavailable log directory"), 0600); err != nil {
		t.Fatal(err)
	}
	m.Start()
	m.Record(Record{EventCode: "connector_started"})
	supervisorReviewEventually(t, 2*time.Second, func() bool {
		// Accept the fixed storage failure code on a current or added Stats
		// field; do not require one specific presentation field.
		raw, err := json.Marshal(m.Stats())
		return err == nil && strings.Contains(string(raw), "LOCAL_STORAGE_FAILED")
	}, "a failed durable outbox write was not exposed as LOCAL_STORAGE_FAILED in diagnostic health")
}

// Holding the persistence serialization lock models a busy/slow existing disk
// writer. Recording a new operation must remain independent of that writer.
func TestSupervisorReviewRecordDoesNotWaitForSlowPersistence(t *testing.T) {
	m := supervisorReviewManager(t)
	m.Start()
	m.persistMu.Lock()
	done := make(chan struct{})
	go func() {
		m.Record(Record{EventCode: "connector_started"})
		close(done)
	}()

	select {
	case <-done:
		m.persistMu.Unlock()
	case <-time.After(time.Second):
		m.persistMu.Unlock()
		select {
		case <-done:
		case <-time.After(3 * time.Second):
			t.Fatal("Record failed to return even after releasing the simulated slow persistence writer")
		}
		t.Error("Record blocked on persistence; logging can stall a business operation")
	}
}
