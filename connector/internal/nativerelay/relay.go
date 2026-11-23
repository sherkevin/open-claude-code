// Package nativerelay carries approved MCP calls over an authenticated outbound
// WebSocket. It dispatches only to handlers already registered by the agent.
package nativerelay

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"mime"
	"net/http"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"
)

const (
	maxRequestBytes  = 4 << 20
	maxResponseBytes = 8 << 20
	chunkBytes       = 16 << 10
	maxInFlight      = 20
	writeTimeout     = 5 * time.Second
	requestTimeout   = 60 * time.Second
)

var messageID = regexp.MustCompile(`^[A-Za-z0-9_-]{1,96}$`)

type inbound struct {
	Type     string            `json:"type"`
	ID       string            `json:"id"`
	SourceID string            `json:"source_id"`
	Headers  map[string]string `json:"headers"`
	Body     []byte            `json:"body"`
}

type outbound struct {
	Type    string            `json:"type"`
	ID      string            `json:"id"`
	Status  int               `json:"status,omitempty"`
	Headers map[string]string `json:"headers,omitempty"`
	Body    []byte            `json:"body,omitempty"`
}

// AwaitReady checks that the signed upgrade reached the MCP relay endpoint.
func AwaitReady(ctx context.Context, conn *websocket.Conn) error {
	conn.SetReadLimit(int64(base64.StdEncoding.EncodedLen(maxRequestBytes) + (16 << 10)))
	msg, err := readMessage(ctx, conn)
	if err != nil || msg.Type != "ready" || !msg.controlOnly() || msg.ID != "" {
		return errors.New("原生中继未确认连接")
	}
	return nil
}

// Serve handles one WebSocket connection until its context or transport ends.
// The supplied lookup returns only a source-bound local MCP handler.
func Serve(ctx context.Context, conn *websocket.Conn, lookup func(string) http.Handler) error {
	ctx, cancel := context.WithCancel(ctx)
	stopClose := context.AfterFunc(ctx, func() { _ = conn.CloseNow() })
	defer func() {
		cancel()
		stopClose()
		_ = conn.CloseNow()
	}()
	go keepalive(ctx, conn, cancel)
	var mu sync.Mutex
	inFlight := make(map[string]context.CancelFunc)
	for {
		msg, err := readMessage(ctx, conn)
		if err != nil {
			return err
		}
		if !messageID.MatchString(msg.ID) {
			return errors.New("原生中继请求编号无效")
		}
		switch msg.Type {
		case "cancel":
			if !msg.controlOnly() {
				return errors.New("原生中继取消消息无效")
			}
			mu.Lock()
			stop := inFlight[msg.ID]
			mu.Unlock()
			if stop != nil {
				stop()
			}
		case "request":
			if len(msg.Body) == 0 || len(msg.Body) > maxRequestBytes || len(msg.Headers) > 5 {
				return errors.New("原生中继请求超过允许范围")
			}
			handler := lookup(msg.SourceID)
			mu.Lock()
			if inFlight[msg.ID] != nil {
				mu.Unlock()
				return errors.New("原生中继请求编号重复")
			}
			if handler == nil || len(inFlight) >= maxInFlight {
				mu.Unlock()
				if err := writeMessage(ctx, conn, outbound{Type: "error", ID: msg.ID}); err != nil {
					return err
				}
				continue
			}
			requestCtx, stop := context.WithTimeout(ctx, requestTimeout)
			inFlight[msg.ID] = stop
			mu.Unlock()
			go func() {
				defer func() {
					stop()
					mu.Lock()
					delete(inFlight, msg.ID)
					mu.Unlock()
				}()
				serveRequest(ctx, requestCtx, conn, msg, handler)
			}()
		default:
			return errors.New("原生中继消息类型无效")
		}
	}
}

func (msg inbound) controlOnly() bool {
	return msg.SourceID == "" && len(msg.Headers) == 0 && len(msg.Body) == 0
}

func readMessage(ctx context.Context, conn *websocket.Conn) (inbound, error) {
	var msg inbound
	messageType, raw, err := conn.Read(ctx)
	if err != nil {
		return msg, err
	}
	if messageType != websocket.MessageText {
		return msg, errors.New("原生中继只接收文本消息")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&msg); err != nil {
		return msg, errors.New("原生中继消息格式无效")
	}
	if err := decoder.Decode(new(json.RawMessage)); err != io.EOF {
		return msg, errors.New("原生中继消息格式无效")
	}
	return msg, nil
}

func writeMessage(ctx context.Context, conn *websocket.Conn, msg outbound) error {
	writeCtx, cancel := context.WithTimeout(ctx, writeTimeout)
	defer cancel()
	return wsjson.Write(writeCtx, conn, msg)
}

func keepalive(ctx context.Context, conn *websocket.Conn, stop context.CancelFunc) {
	ticker := time.NewTicker(20 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			pingCtx, cancel := context.WithTimeout(ctx, writeTimeout)
			err := conn.Ping(pingCtx)
			cancel()
			if err != nil {
				stop()
				return
			}
		}
	}
}

func serveRequest(relayCtx, requestCtx context.Context, conn *websocket.Conn, msg inbound, handler http.Handler) {
	failed := true
	defer func() {
		panicked := recover() != nil
		if (panicked || failed) && requestCtx.Err() != context.Canceled {
			_ = writeMessage(relayCtx, conn, outbound{Type: "error", ID: msg.ID})
		}
	}()
	request, err := http.NewRequestWithContext(requestCtx, http.MethodPost, "http://localhost/mcp", bytes.NewReader(msg.Body))
	if err != nil {
		return
	}
	for name, value := range msg.Headers {
		canonical := http.CanonicalHeaderKey(name)
		switch canonical {
		case "Content-Type", "Accept", "Mcp-Protocol-Version", "Mcp-Session-Id", "X-Kaiwu-Trace-Id":
			if _, duplicate := request.Header[canonical]; duplicate || len(value) > 512 || strings.ContainsAny(value, "\r\n\x00") {
				return
			}
			request.Header.Set(canonical, value)
		default:
			return
		}
	}
	writer := &responseWriter{
		id: msg.ID, relayCtx: relayCtx, requestCtx: requestCtx, conn: conn, header: make(http.Header),
	}
	handler.ServeHTTP(writer, request)
	writer.Flush()
	if writer.err != nil || requestCtx.Err() != nil {
		return
	}
	failed = writer.send(outbound{Type: "end", ID: msg.ID}) != nil
}

type responseWriter struct {
	relayCtx   context.Context
	requestCtx context.Context
	conn       *websocket.Conn
	id         string
	header     http.Header
	statusSent bool
	written    int
	err        error
}

func (w *responseWriter) Header() http.Header { return w.header }

func (w *responseWriter) AbortMCPStream() {
	if w.err == nil {
		w.err = errors.New("MCP 响应流意外中断")
	}
}

func (w *responseWriter) send(msg outbound) error {
	if err := w.requestCtx.Err(); err != nil {
		return err
	}
	// Canceling one MCP call must not interrupt a WebSocket write on the
	// shared connection. A late chunk can be ignored by its request ID.
	return writeMessage(w.relayCtx, w.conn, msg)
}

func (w *responseWriter) WriteHeader(status int) {
	if w.statusSent {
		return
	}
	w.statusSent = true
	headers := make(map[string]string)
	if value := w.header.Get("Content-Type"); value != "" {
		mediaType, _, err := mime.ParseMediaType(value)
		switch {
		case err == nil && mediaType == "text/event-stream":
			headers["Content-Type"] = "text/event-stream"
		case err == nil && (mediaType == "application/json" || strings.HasSuffix(mediaType, "+json")):
			headers["Content-Type"] = "application/json"
		default:
			w.err = errors.New("原生中继不支持此 MCP 响应类型")
			return
		}
	}
	if sessionID := w.header.Get("Mcp-Session-Id"); sessionID != "" {
		headers["Mcp-Session-Id"] = sessionID
	}
	w.err = w.send(outbound{Type: "response", ID: w.id, Status: status, Headers: headers})
}

func (w *responseWriter) Flush() { w.WriteHeader(http.StatusOK) }

func (w *responseWriter) Write(body []byte) (int, error) {
	w.Flush()
	if w.err != nil {
		return 0, w.err
	}
	if len(body) > maxResponseBytes-w.written {
		w.err = errors.New("MCP 响应超过大小限制")
		return 0, w.err
	}
	for start := 0; start < len(body); start += chunkBytes {
		end := min(start+chunkBytes, len(body))
		w.err = w.send(outbound{Type: "chunk", ID: w.id, Body: body[start:end]})
		if w.err != nil {
			return start, w.err
		}
	}
	w.written += len(body)
	return len(body), nil
}
