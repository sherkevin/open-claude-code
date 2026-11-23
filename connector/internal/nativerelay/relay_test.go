package nativerelay

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"
)

func TestStreamingCancellationAndIndependentNextCall(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	peer := make(chan *websocket.Conn, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, nil)
		if err != nil {
			t.Error(err)
			return
		}
		defer conn.CloseNow()
		if err := wsjson.Write(ctx, conn, map[string]string{"type": "ready"}); err != nil {
			t.Error(err)
			return
		}
		peer <- conn
		<-ctx.Done()
	}))
	defer func() { cancel(); server.Close() }()
	conn, _, err := websocket.Dial(ctx, server.URL, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := AwaitReady(ctx, conn); err != nil {
		t.Fatal(err)
	}
	gateway := <-peer
	stopped := make(chan struct{})
	go Serve(ctx, conn, func(id string) http.Handler {
		if id != "local-approved" {
			return nil
		}
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodPost || r.URL.Path != "/mcp" || r.URL.RawQuery != "" {
				t.Errorf("wrong upstream request: %s %s", r.Method, r.URL)
			}
			body, err := io.ReadAll(r.Body)
			if err != nil {
				t.Error(err)
			}
			w.Header().Set("Content-Type", "text/event-stream")
			w.Header().Set("Mcp-Session-Id", "local-session")
			w.Header().Set("X-Secret", "must-not-leave")
			_, _ = fmt.Fprintf(w, "data: %s\n\n", body)
			w.(http.Flusher).Flush()
			if string(body) == "first" {
				<-r.Context().Done()
				close(stopped)
			}
			if string(body) == "incomplete" {
				w.(interface{ AbortMCPStream() }).AbortMCPStream()
			}
		})
	})
	write := func(msg inbound) {
		t.Helper()
		if err := wsjson.Write(ctx, gateway, msg); err != nil {
			t.Fatal(err)
		}
	}
	read := func(kind, id string) outbound {
		t.Helper()
		var msg outbound
		if err := wsjson.Read(ctx, gateway, &msg); err != nil {
			t.Fatal(err)
		}
		if msg.Type != kind || msg.ID != id {
			t.Fatalf("got %+v, want %s for %s", msg, kind, id)
		}
		return msg
	}
	write(inbound{Type: "request", ID: "test-1", SourceID: "local-approved", Body: []byte("first")})
	start := read("response", "test-1")
	if start.Status != http.StatusOK || len(start.Headers) != 2 || start.Headers["Mcp-Session-Id"] != "local-session" || start.Headers["Content-Type"] != "text/event-stream" {
		t.Fatalf("unexpected response metadata: %+v", start)
	}
	if got := string(read("chunk", "test-1").Body); got != "data: first\n\n" {
		t.Fatalf("stream chunk = %q", got)
	}
	write(inbound{Type: "cancel", ID: "test-1"})
	select {
	case <-ctx.Done():
		t.Fatal("local MCP handler was not canceled")
	case <-stopped:
	}
	write(inbound{Type: "request", ID: "unknown", SourceID: "https://foreign.invalid", Body: []byte("ignored")})
	read("error", "unknown")
	write(inbound{Type: "request", ID: "blocked-header", SourceID: "local-approved", Headers: map[string]string{"Authorization": "cloud-secret"}, Body: []byte("ignored")})
	read("error", "blocked-header")
	write(inbound{Type: "request", ID: "broken-stream", SourceID: "local-approved", Body: []byte("incomplete")})
	read("response", "broken-stream")
	read("chunk", "broken-stream")
	read("error", "broken-stream")
	write(inbound{Type: "request", ID: "test-2", SourceID: "local-approved", Body: []byte("next")})
	read("response", "test-2")
	if got := string(read("chunk", "test-2").Body); got != "data: next\n\n" {
		t.Fatalf("second call chunk = %q", got)
	}
	read("end", "test-2")
}
