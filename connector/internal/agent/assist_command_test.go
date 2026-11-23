//go:build !windows

package agent

import (
	"context"
	"strings"
	"testing"
	"time"
)

func TestRunAssistCommandCapturesOutputAndExitCode(t *testing.T) {
	result := (&Agent{}).runAssistCommand(context.Background(), "printf 'hello\\n'; printf 'oops\\n' >&2", 10)
	if result.status != "succeeded" || result.code != "command_completed" || result.state != "succeeded" {
		t.Fatalf("unexpected result: %+v", result)
	}
	if !strings.Contains(result.output, "hello") || !strings.Contains(result.output, "oops") {
		t.Fatalf("stdout/stderr were not captured: %q", result.output)
	}
	if result.exitCode == nil || *result.exitCode != 0 {
		t.Fatalf("exit code = %v, want 0", result.exitCode)
	}
	if result.duration <= 0 {
		t.Fatalf("duration = %v, want positive", result.duration)
	}
}

func TestRunAssistCommandReportsFailureExitCode(t *testing.T) {
	result := (&Agent{}).runAssistCommand(context.Background(), "printf 'bad'; exit 7", 10)
	if result.status != "failed" || result.code != "command_failed" || result.state != "failed" {
		t.Fatalf("unexpected result: %+v", result)
	}
	if result.exitCode == nil || *result.exitCode != 7 {
		t.Fatalf("exit code = %v, want 7", result.exitCode)
	}
	if result.output != "bad" {
		t.Fatalf("output = %q, want bad", result.output)
	}
}

func TestRunAssistCommandTimeoutKillsProcess(t *testing.T) {
	started := time.Now()
	result := (&Agent{}).runAssistCommand(context.Background(), "sleep 10", 1)
	if elapsed := time.Since(started); elapsed > 5*time.Second {
		t.Fatalf("timeout took %v", elapsed)
	}
	if result.status != "failed" || result.code != "command_timeout" || result.state != "timeout" {
		t.Fatalf("unexpected result: %+v", result)
	}
	if result.exitCode != nil {
		t.Fatalf("timeout exit code = %v, want nil", result.exitCode)
	}
}

func TestRunAssistCommandCanceledBySession(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(100 * time.Millisecond)
		cancel()
	}()
	result := (&Agent{}).runAssistCommand(ctx, "sleep 5", 10)
	if result.status != "failed" || result.code != "canceled" || result.state != "canceled" {
		t.Fatalf("unexpected result: %+v", result)
	}
}

func TestRunAssistCommandTruncatesOutput(t *testing.T) {
	result := (&Agent{}).runAssistCommand(context.Background(), "head -c 100000 /dev/zero | tr '\\0' x", 10)
	if !result.truncated {
		t.Fatal("expected truncated output")
	}
	if len(result.output) != assistCommandOutputLimit {
		t.Fatalf("output length = %d, want %d", len(result.output), assistCommandOutputLimit)
	}
}

func TestRunAssistCommandRejectsInvalidInput(t *testing.T) {
	for _, command := range []string{"", "   ", "echo bad\x00value", strings.Repeat("x", 4001)} {
		result := (&Agent{}).runAssistCommand(context.Background(), command, 10)
		if result.status != "failed" || result.code != "command_failed" || result.output == "" {
			t.Fatalf("command %q: unexpected result: %+v", command, result)
		}
	}
}
