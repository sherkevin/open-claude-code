//go:build linux || windows

package agent

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/diagnostics"
)

func TestContendedDiagnosticsServingLifecycle(t *testing.T) {
	for _, releaseBeforeClose := range []bool{true, false} {
		name := "close_while_lock_is_held"
		if releaseBeforeClose {
			name = "release_then_close"
		}
		t.Run(name, func(t *testing.T) {
			dir := t.TempDir()
			holder, err := diagnostics.LockReporting(dir)
			if err != nil {
				t.Fatal(err)
			}
			defer holder.Close()
			a, err := New(Options{DataDir: dir, ExclusiveDiagnostics: true})
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(a.Close)
			a.StartRuntime()
			if got := a.Status().Diagnostics.StorageErrorCode; got != "LOCAL_STORAGE_FAILED" {
				t.Fatalf("diagnostics initialized without the lock: %q", got)
			}
			if releaseBeforeClose {
				if err := holder.Close(); err != nil {
					t.Fatal(err)
				}
				select {
				case <-a.diagnosticsRetryDone:
				case <-time.After(5 * time.Second):
					t.Fatal("serving runtime did not recover diagnostics")
				}
				if got := a.Status().Diagnostics; got.StorageErrorCode != "" || got.LocalEventCount != 1 {
					t.Fatalf("expected exactly one startup event: %+v", got)
				}
			}

			ctx, cancel := context.WithTimeout(t.Context(), time.Second)
			defer cancel()
			a.CloseContext(ctx)
			select {
			case <-a.diagnosticsRetryDone:
			default:
				t.Fatal("diagnostic recovery worker outlived closing")
			}
			if !releaseBeforeClose {
				if _, err := os.Stat(filepath.Join(dir, "diagnostics.json")); !errors.Is(err, os.ErrNotExist) {
					t.Fatalf("closing a waiter wrote a queue without the lock: %v", err)
				}
				if err := holder.Close(); err != nil {
					t.Fatal(err)
				}
			}
			lock, err := diagnostics.LockReporting(dir)
			if err != nil {
				t.Fatalf("diagnostics lock remained held after shutdown: %v", err)
			}
			lock.Close()
		})
	}
}
