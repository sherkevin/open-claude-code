package diag

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/sherkevin/open-claude-code/connector/internal/config"
)

func fakeMCP(t *testing.T, requireAuth bool) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if requireAuth && r.Header.Get("Authorization") != "Bearer good" {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		var request struct {
			ID     int64  `json:"id"`
			Method string `json:"method"`
		}
		json.NewDecoder(r.Body).Decode(&request)
		if request.Method == "notifications/initialized" {
			w.WriteHeader(http.StatusAccepted)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		switch request.Method {
		case "initialize":
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{"protocolVersion":"2025-06-18","serverInfo":{"name":"fake","version":"1"}}}`, request.ID)
		case "tools/list":
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{"tools":[{"name":"get_inventory"},{"name":"search_knowledge"}]}}`, request.ID)
		case "tools/call":
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{"content":[]}}`, request.ID)
		}
	}))
}

func TestRunPassesAgainstLocalMCP(t *testing.T) {
	server := fakeMCP(t, false)
	defer server.Close()
	source := config.Source{
		SchemaVersion: 1, SourceID: "s1", DisplayName: "库存",
		Transport: config.TransportStreamableHTTP, UpstreamURL: server.URL + "/mcp",
		ToolAllowlist: []string{"get_inventory"},
	}
	report := Run(context.Background(), source, Options{ToolProbe: "get_inventory"})
	if !report.OK {
		t.Fatalf("诊断应通过: %+v", report.Checks)
	}
	if len(report.Checks) < 5 {
		t.Fatalf("检查项不足: %+v", report.Checks)
	}
}

func TestRunFailsOnAuth(t *testing.T) {
	server := fakeMCP(t, true)
	defer server.Close()
	source := config.Source{
		SchemaVersion: 1, SourceID: "s1", DisplayName: "库存",
		Transport: config.TransportStreamableHTTP, UpstreamURL: server.URL + "/mcp",
	}
	report := Run(context.Background(), source, Options{})
	if report.OK {
		t.Fatal("缺少凭据时诊断必须失败")
	}
	last := report.Checks[len(report.Checks)-1]
	if last.Code != "UPSTREAM_AUTH_FAILED" {
		t.Fatalf("错误码不符: %+v", last)
	}
}

func TestRunFailsOnAllowlistMismatch(t *testing.T) {
	server := fakeMCP(t, false)
	defer server.Close()
	source := config.Source{
		SchemaVersion: 1, SourceID: "s1", DisplayName: "库存",
		Transport: config.TransportStreamableHTTP, UpstreamURL: server.URL + "/mcp",
		ToolAllowlist: []string{"not_exists"},
	}
	report := Run(context.Background(), source, Options{})
	if report.OK {
		t.Fatal("允许列表与目录不一致时必须失败")
	}
}

func TestRunFailsOnUnreachableHost(t *testing.T) {
	source := config.Source{
		SchemaVersion: 1, SourceID: "s1", DisplayName: "库存",
		Transport: config.TransportStreamableHTTP, UpstreamURL: "http://127.0.0.1:1/mcp",
	}
	report := Run(context.Background(), source, Options{Timeout: 300000000})
	if report.OK {
		t.Fatal("不可达地址必须失败")
	}
	if report.Checks[len(report.Checks)-1].Code != "UPSTREAM_UNREACHABLE" {
		t.Fatalf("错误码不符: %+v", report.Checks)
	}
}
