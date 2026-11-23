//go:build windows

// Command assistprobe is a Windows-only acceptance probe for the remote CMD
// channel. It starts a loopback control-plane fake, binds a temporary agent,
// opens assistance, and verifies that a command executes and is reported.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync/atomic"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/agent"
)

const (
	offerID = "asst_0123456789abcdef0123456789abcdef"
	jobOne  = "asjob_0123456789abcdef0123456789abcdef"
	jobTwo  = "asjob_fedcba9876543210fedcba9876543210"
)

func main() {
	if err := run(); err != nil {
		fmt.Println("FAIL:", err)
		os.Exit(1)
	}
	fmt.Println("PASS: Windows command execution and automatic report")
}

func run() error {
	var polls atomic.Int32
	var active atomic.Bool
	results := make(chan map[string]any, 2)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/v1/enrollments/redeem":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"connector_id": "conn_probe", "tenant_id": "tenant_probe",
				"tenant_name": "Probe", "device_token": "probe-device-token",
			})
		case "/v1/connectors/conn_probe/assist/start":
			active.Store(true)
			_ = json.NewEncoder(w).Encode(map[string]any{
				"id": offerID, "name": "内网接入协助", "url": "", "status": "active",
				"expires_at": time.Now().Add(time.Minute).UTC().Format(time.RFC3339),
			})
		case "/v1/connectors/conn_probe/assist/poll":
			var job any
			switch polls.Add(1) {
			case 1:
				job = map[string]any{"id": jobOne, "action": "command", "command": "echo assist-windows-ok", "timeout_seconds": 10}
			case 3:
				job = map[string]any{"id": jobTwo, "action": "command", "command": "ping -n 10 127.0.0.1 >NUL", "timeout_seconds": 1}
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"active": active.Load(), "job": job})
		case "/v1/connectors/conn_probe/assist/result":
			var result map[string]any
			if err := json.NewDecoder(r.Body).Decode(&result); err != nil {
				http.Error(w, "bad result", http.StatusBadRequest)
				return
			}
			results <- result
			_, _ = w.Write([]byte("{}"))
		case "/v1/connectors/conn_probe/assist/stop":
			active.Store(false)
			_, _ = w.Write([]byte("{}"))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	dir, err := os.MkdirTemp("", "kaiwu-assist-probe-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(dir)
	instance, err := agent.New(agent.Options{DataDir: dir, CloudBase: server.URL, NativeRelay: true})
	if err != nil {
		return err
	}
	defer instance.Close()

	start := time.Now()
	step := func(name string) {
		fmt.Printf("step %s at +%s\n", name, time.Since(start).Round(time.Millisecond))
	}
	bindCtx, bindCancel := context.WithTimeout(context.Background(), 300*time.Second)
	defer bindCancel()
	step("bind")
	if _, err := instance.Bind(bindCtx, "probe-code"); err != nil {
		return fmt.Errorf("bind: %w", err)
	}
	step("open-assist")
	offer, err := instance.OpenAssist(bindCtx)
	if err != nil {
		return fmt.Errorf("open assist: %w", err)
	}
	if offer.ID != offerID {
		return fmt.Errorf("offer id = %q", offer.ID)
	}

	waitCtx, waitCancel := context.WithTimeout(context.Background(), 480*time.Second)
	defer waitCancel()
	first, err := waitResult(waitCtx, results)
	if err != nil {
		return err
	}
	step("first-result")
	if first["status"] != "succeeded" || first["code"] != "command_completed" ||
		first["exit_code"] != float64(0) || !strings.Contains(fmt.Sprint(first["output"]), "assist-windows-ok") {
		return fmt.Errorf("first result = %#v", first)
	}
	fmt.Println("ok: command output was captured and reported automatically")

	second, err := waitResult(waitCtx, results)
	if err != nil {
		return err
	}
	step("second-result")
	if second["status"] != "failed" || second["code"] != "command_timeout" {
		return fmt.Errorf("timeout result = %#v", second)
	}
	fmt.Println("ok: command timeout stopped the process and reported command_timeout")

	stopCtx, stopCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer stopCancel()
	if err := instance.StopAssist(stopCtx, offer.ID); err != nil {
		return fmt.Errorf("stop: %w", err)
	}
	return nil
}

func waitResult(ctx context.Context, results <-chan map[string]any) (map[string]any, error) {
	select {
	case result := <-results:
		return result, nil
	case <-ctx.Done():
		return nil, fmt.Errorf("timed out waiting for command result")
	}
}
