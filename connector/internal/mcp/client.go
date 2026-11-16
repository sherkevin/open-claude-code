// Package mcp 实现连接器需要的最小 MCP 客户端。
//
// 只覆盖首版验收用到的三件事：初始化、列工具、调用工具。
// 传输按 Streamable HTTP 处理：POST JSON-RPC，响应可能是 JSON，也可能是 SSE 事件流。
package mcp

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync/atomic"
	"time"
)

// Client 是单个 MCP 端点的客户端。
type Client struct {
	Endpoint        string
	Headers         map[string]string
	HTTP            *http.Client
	ProtocolVersion string
	ClientName      string
	ClientVersion   string

	nextID    atomic.Int64
	sessionID string
}

// Tool 是服务端返回的工具描述。
type Tool struct {
	Name        string          `json:"name"`
	Description string          `json:"description,omitempty"`
	InputSchema json.RawMessage `json:"inputSchema,omitempty"`
}

// ServerInfo 是初始化结果中的服务端信息。
type ServerInfo struct {
	ProtocolVersion string          `json:"protocolVersion"`
	Capabilities    json.RawMessage `json:"capabilities"`
	ServerInfo      struct {
		Name    string `json:"name"`
		Version string `json:"version"`
	} `json:"serverInfo"`
}

// RPCError 是 MCP 服务端返回的 JSON-RPC 错误。
type RPCError struct {
	Code    int             `json:"code"`
	Message string          `json:"message"`
	Data    json.RawMessage `json:"data,omitempty"`
}

func (e *RPCError) Error() string {
	return fmt.Sprintf("MCP 错误 %d: %s", e.Code, e.Message)
}

type rpcRequest struct {
	JSONRPC string `json:"jsonrpc"`
	ID      int64  `json:"id"`
	Method  string `json:"method"`
	Params  any    `json:"params,omitempty"`
}

type rpcResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Result  json.RawMessage `json:"result"`
	Error   *RPCError       `json:"error"`
}

func (c *Client) httpClient() *http.Client {
	if c.HTTP != nil {
		return c.HTTP
	}
	return &http.Client{Timeout: 30 * time.Second}
}

// Initialize 执行 MCP 初始化握手。
func (c *Client) Initialize(ctx context.Context) (ServerInfo, error) {
	version := c.ProtocolVersion
	if version == "" {
		version = "2025-06-18"
	}
	name := c.ClientName
	if name == "" {
		name = "kaiwu-intranet-connector"
	}
	params := map[string]any{
		"protocolVersion": version,
		"capabilities":    map[string]any{},
		"clientInfo":      map[string]any{"name": name, "version": c.ClientVersion},
	}
	var info ServerInfo
	if err := c.call(ctx, "initialize", params, &info); err != nil {
		return info, err
	}
	c.notify(ctx, "notifications/initialized")
	return info, nil
}

// ListTools 获取工具目录。
func (c *Client) ListTools(ctx context.Context) ([]Tool, error) {
	var result struct {
		Tools []Tool `json:"tools"`
	}
	if err := c.call(ctx, "tools/list", map[string]any{}, &result); err != nil {
		return nil, err
	}
	return result.Tools, nil
}

// CallTool 调用一个工具并返回原始结果。
func (c *Client) CallTool(ctx context.Context, name string, arguments any) (json.RawMessage, error) {
	var result json.RawMessage
	if err := c.call(ctx, "tools/call", map[string]any{"name": name, "arguments": arguments}, &result); err != nil {
		return nil, err
	}
	return result, nil
}

func (c *Client) call(ctx context.Context, method string, params any, out any) error {
	id := c.nextID.Add(1)
	body, err := json.Marshal(rpcRequest{JSONRPC: "2.0", ID: id, Method: method, Params: params})
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.Endpoint, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json, text/event-stream")
	if c.ProtocolVersion != "" {
		req.Header.Set("MCP-Protocol-Version", c.ProtocolVersion)
	}
	if c.sessionID != "" {
		req.Header.Set("Mcp-Session-Id", c.sessionID)
	}
	for key, value := range c.Headers {
		req.Header.Set(key, value)
	}

	resp, err := c.httpClient().Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if sid := resp.Header.Get("Mcp-Session-Id"); sid != "" {
		c.sessionID = sid
	}
	if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
		return fmt.Errorf("上游认证失败: HTTP %d", resp.StatusCode)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		payload, _ := io.ReadAll(io.LimitReader(resp.Body, 2048))
		return fmt.Errorf("上游返回 HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(payload)))
	}

	contentType := resp.Header.Get("Content-Type")
	if strings.Contains(contentType, "text/event-stream") {
		return c.readSSE(resp.Body, id, out)
	}
	payload, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return err
	}
	return decodeResponse(payload, id, out)
}

func (c *Client) readSSE(body io.Reader, id int64, out any) error {
	scanner := bufio.NewScanner(body)
	scanner.Buffer(make([]byte, 0, 64<<10), 4<<20)
	var data strings.Builder
	for scanner.Scan() {
		line := scanner.Text()
		switch {
		case strings.HasPrefix(line, "data:"):
			data.WriteString(strings.TrimSpace(strings.TrimPrefix(line, "data:")))
		case line == "":
			if data.Len() == 0 {
				continue
			}
			if matched := matchID([]byte(data.String()), id); matched {
				return decodeResponse([]byte(data.String()), id, out)
			}
			data.Reset()
		}
	}
	if err := scanner.Err(); err != nil {
		return err
	}
	if data.Len() > 0 && matchID([]byte(data.String()), id) {
		return decodeResponse([]byte(data.String()), id, out)
	}
	return fmt.Errorf("事件流结束前未收到响应 id=%d", id)
}

func matchID(payload []byte, id int64) bool {
	var probe struct {
		ID json.RawMessage `json:"id"`
	}
	if err := json.Unmarshal(payload, &probe); err != nil || len(probe.ID) == 0 {
		return false
	}
	var got int64
	if err := json.Unmarshal(probe.ID, &got); err != nil {
		return false
	}
	return got == id
}

func decodeResponse(payload []byte, id int64, out any) error {
	var resp rpcResponse
	if err := json.Unmarshal(payload, &resp); err != nil {
		return fmt.Errorf("响应不是有效的 JSON-RPC: %w", err)
	}
	if resp.Error != nil {
		return resp.Error
	}
	if len(resp.Result) == 0 {
		return fmt.Errorf("响应缺少 result")
	}
	if out == nil {
		return nil
	}
	return json.Unmarshal(resp.Result, out)
}

// notify 发送初始化完成通知，失败不影响主流程。
func (c *Client) notify(ctx context.Context, method string) {
	body, err := json.Marshal(map[string]any{"jsonrpc": "2.0", "method": method})
	if err != nil {
		return
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.Endpoint, bytes.NewReader(body))
	if err != nil {
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json, text/event-stream")
	if c.sessionID != "" {
		req.Header.Set("Mcp-Session-Id", c.sessionID)
	}
	for key, value := range c.Headers {
		req.Header.Set(key, value)
	}
	resp, err := c.httpClient().Do(req)
	if err == nil {
		resp.Body.Close()
	}
}
