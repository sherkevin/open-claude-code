package mcp

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newServer(t *testing.T) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var request struct {
			ID     int64           `json:"id"`
			Method string          `json:"method"`
			Params json.RawMessage `json:"params"`
		}
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		if request.Method == "notifications/initialized" || r.Header.Get("Mcp-Session-Id") == "session-1" {
			if version := r.Header.Get("MCP-Protocol-Version"); version != "2025-03-26" {
				t.Errorf("%s used protocol version %q, want negotiated version 2025-03-26", request.Method, version)
			}
		}
		if request.Method == "notifications/initialized" {
			w.WriteHeader(http.StatusAccepted)
			return
		}
		w.Header().Set("Mcp-Session-Id", "session-1")
		switch request.Method {
		case "initialize":
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{"protocolVersion":"2025-03-26","capabilities":{},"serverInfo":{"name":"demo","version":"1.0"}}}`, request.ID)
		case "tools/list":
			w.Header().Set("Content-Type", "text/event-stream")
			fmt.Fprintf(w, "event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":%d,\"result\":{\"tools\":[{\"name\":\"get_inventory\",\"description\":\"查询库存\"}]}}\n\n", request.ID)
		case "tools/call":
			var params struct {
				Name string `json:"name"`
			}
			json.Unmarshal(request.Params, &params)
			w.Header().Set("Content-Type", "application/json")
			if params.Name != "get_inventory" {
				fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"error":{"code":-32601,"message":"unknown tool"}}`, request.ID)
				return
			}
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{"content":[{"type":"text","text":"ok"}]}}`, request.ID)
		default:
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"error":{"code":-32601,"message":"unknown method"}}`, request.ID)
		}
	}))
}

func TestClientHandlesJSONAndSSE(t *testing.T) {
	server := newServer(t)
	defer server.Close()
	client := &Client{Endpoint: server.URL + "/mcp"}
	ctx := context.Background()

	info, err := client.Initialize(ctx)
	if err != nil {
		t.Fatalf("初始化失败: %v", err)
	}
	if info.ServerInfo.Name != "demo" {
		t.Fatalf("服务端信息错误: %+v", info)
	}
	tools, err := client.ListTools(ctx)
	if err != nil {
		t.Fatalf("列工具失败: %v", err)
	}
	if len(tools) != 1 || tools[0].Name != "get_inventory" {
		t.Fatalf("工具解析错误: %+v", tools)
	}
	if len(tools[0].Raw) == 0 || !strings.Contains(string(tools[0].Raw), `"description":"查询库存"`) {
		t.Fatalf("未保留完整工具定义: %s", tools[0].Raw)
	}
	if _, err := client.CallTool(ctx, "get_inventory", map[string]any{}); err != nil {
		t.Fatalf("调用工具失败: %v", err)
	}
}

func TestClientReportsRPCError(t *testing.T) {
	server := newServer(t)
	defer server.Close()
	client := &Client{Endpoint: server.URL + "/mcp"}
	_, err := client.CallTool(context.Background(), "missing", map[string]any{})
	if err == nil || !strings.Contains(err.Error(), "-32601") {
		t.Fatalf("应返回 JSON-RPC 错误: %v", err)
	}
}

func TestClientReportsAuthFailure(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "no", http.StatusUnauthorized)
	}))
	defer server.Close()
	client := &Client{Endpoint: server.URL}
	_, err := client.Initialize(context.Background())
	if err == nil {
		t.Fatal("401 必须报错")
	}
}
