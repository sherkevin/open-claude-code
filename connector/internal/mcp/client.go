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
	"errors"
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
	// HeaderProvider can add request-specific authentication headers. The
	// connector uses it for HwMain's per-call dynamic identity and HMAC.
	HeaderProvider func(method, tool string) (map[string]string, error)

	nextID    atomic.Int64
	sessionID string
}

// Tool 是服务端返回的工具描述。
type Tool struct {
	Name         string          `json:"name"`
	Title        string          `json:"title,omitempty"`
	Description  string          `json:"description,omitempty"`
	InputSchema  json.RawMessage `json:"inputSchema,omitempty"`
	OutputSchema json.RawMessage `json:"outputSchema,omitempty"`
	Annotations  json.RawMessage `json:"annotations,omitempty"`
	// Raw preserves the complete server definition so callers can compute a
	// deterministic digest without silently dropping schema extensions.
	Raw json.RawMessage `json:"-"`
}

// UnmarshalJSON keeps both the typed view used by callers and the exact tool
// object returned by the server.
func (t *Tool) UnmarshalJSON(data []byte) error {
	type toolAlias Tool
	var decoded toolAlias
	if err := json.Unmarshal(data, &decoded); err != nil {
		return err
	}
	*t = Tool(decoded)
	t.Raw = append(t.Raw[:0], data...)
	return nil
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
	var base *http.Client
	if c.HTTP != nil {
		base = c.HTTP
	} else {
		base = &http.Client{Timeout: 30 * time.Second}
	}
	clone := *base
	clone.CheckRedirect = func(_ *http.Request, _ []*http.Request) error {
		return http.ErrUseLastResponse
	}
	return &clone
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
	if err := c.call(ctx, "initialize", "", params, &info); err != nil {
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
	if err := c.call(ctx, "tools/list", "", map[string]any{}, &result); err != nil {
		return nil, err
	}
	return result.Tools, nil
}

// CallTool 调用一个工具并返回原始结果。
func (c *Client) CallTool(ctx context.Context, name string, arguments any) (json.RawMessage, error) {
	var result json.RawMessage
	if err := c.call(ctx, "tools/call", name, map[string]any{"name": name, "arguments": arguments}, &result); err != nil {
		return nil, err
	}
	return result, nil
}

func (c *Client) call(ctx context.Context, method, tool string, params any, out any) error {
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
	if err := c.applyHeaderProvider(req, method, tool); err != nil {
		return err
	}

	resp, err := c.httpClient().Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if sid := resp.Header.Get("Mcp-Session-Id"); sid != "" {
		c.sessionID = sid
	}
	if resp.StatusCode >= 300 && resp.StatusCode < 400 {
		return fmt.Errorf("上游返回重定向 HTTP %d，连接器拒绝跟随", resp.StatusCode)
	}
	if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
		return fmt.Errorf("上游认证失败: HTTP %d", resp.StatusCode)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("上游返回 HTTP %d", resp.StatusCode)
	}

	contentType := resp.Header.Get("Content-Type")
	if strings.Contains(contentType, "text/event-stream") {
		return c.readSSE(resp.Body, id, out)
	}
	if contentType != "" && !strings.Contains(contentType, "application/json") && !strings.Contains(contentType, "+json") {
		return fmt.Errorf("上游返回了不支持的 Content-Type: %s", contentType)
	}
	payload, err := readLimited(resp.Body, 4<<20)
	if err != nil {
		return err
	}
	return decodeResponse(payload, id, out)
}

func (c *Client) readSSE(body io.Reader, id int64, out any) error {
	scanner := bufio.NewScanner(body)
	scanner.Buffer(make([]byte, 0, 64<<10), 256<<10)
	var data strings.Builder
	var total int64
	events := 0
	for scanner.Scan() {
		line := scanner.Text()
		total += int64(len(line)) + 1
		if total > 4<<20 {
			return errors.New("上游事件流超过 4 MiB 限制")
		}
		switch {
		case strings.HasPrefix(line, "data:"):
			value := strings.TrimPrefix(line, "data:")
			value = strings.TrimPrefix(value, " ")
			if data.Len()+len(value) > 256<<10 {
				return errors.New("单个上游事件超过 256 KiB 限制")
			}
			data.WriteString(value)
		case line == "":
			if data.Len() == 0 {
				continue
			}
			events++
			if events > 1000 {
				return errors.New("上游事件数量超过 1000 条限制")
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

func readLimited(reader io.Reader, limit int64) ([]byte, error) {
	payload, err := io.ReadAll(io.LimitReader(reader, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(payload)) > limit {
		return nil, fmt.Errorf("上游响应超过 %d 字节限制", limit)
	}
	return payload, nil
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
	if err := c.applyHeaderProvider(req, method, ""); err != nil {
		return
	}
	resp, err := c.httpClient().Do(req)
	if err == nil {
		resp.Body.Close()
	}
}

func (c *Client) applyHeaderProvider(req *http.Request, method, tool string) error {
	if c.HeaderProvider == nil {
		return nil
	}
	headers, err := c.HeaderProvider(method, tool)
	if err != nil {
		return err
	}
	for key, value := range headers {
		if key == "" || strings.ContainsAny(key, "\r\n:") || strings.ContainsAny(value, "\r\n") {
			return errors.New("认证头包含非法字符")
		}
		req.Header.Set(key, value)
	}
	return nil
}
