package demomcp

import (
	"context"
	"encoding/json"
	"net/http"
	"sort"
	"strings"
	"testing"
	"time"

	connectormcp "github.com/sherkevin/open-claude-code/connector/internal/mcp"
	"github.com/sherkevin/open-claude-code/connector/internal/version"
)

func TestServerImplementsFixedMCPTools(t *testing.T) {
	server := New()
	if err := server.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		if err := server.Stop(ctx); err != nil {
			t.Errorf("stop demo MCP: %v", err)
		}
	})
	if !strings.HasPrefix(server.Endpoint(), "http://127.0.0.1:") {
		t.Fatalf("demo MCP must use loopback: %s", server.Endpoint())
	}

	client := &connectormcp.Client{
		Endpoint: server.Endpoint(),
		HTTP:     &http.Client{Timeout: 5 * time.Second},
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	info, err := client.Initialize(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if got := info.ServerInfo.Version; got != version.Version {
		t.Fatalf("demo handshake version = %q, want connector build version %q", got, version.Version)
	}
	tools, err := client.ListTools(ctx)
	if err != nil {
		t.Fatal(err)
	}
	names := make([]string, 0, len(tools))
	for _, tool := range tools {
		names = append(names, tool.Name)
	}
	sort.Strings(names)
	want := append([]string(nil), Tools...)
	sort.Strings(want)
	if strings.Join(names, ",") != strings.Join(want, ",") {
		t.Fatalf("unexpected demo tools: got=%v want=%v", names, want)
	}

	const nonce = "nonce-roundtrip-456"
	ping := callDemoTool[PingOutput](t, ctx, client, ToolPing, map[string]any{"nonce": nonce})
	if !ping.Demo || ping.Marker != Marker || ping.Nonce != nonce || ping.InstanceID != server.InstanceID() {
		t.Fatalf("invalid ping output: %+v", ping)
	}
	observed, err := time.Parse(time.RFC3339Nano, ping.ServerTimeUTC)
	if err != nil || time.Since(observed) > time.Minute || time.Until(observed) > time.Minute {
		t.Fatalf("invalid ping time %q: %v", ping.ServerTimeUTC, err)
	}

	orders := callDemoTool[OrderSummaryOutput](t, ctx, client, ToolOrderSummary, map[string]any{})
	if !orders.Demo || orders.OrderCount != 12 || !strings.Contains(orders.Notice, "示例数据") {
		t.Fatalf("invalid order summary: %+v", orders)
	}
	inventory := callDemoTool[InventoryOutput](t, ctx, client, ToolInventory, map[string]any{})
	if !inventory.Demo || inventory.ProductCode != "KAIWU-DEMO-001" || inventory.Stock != 128 {
		t.Fatalf("invalid inventory: %+v", inventory)
	}
}

func callDemoTool[T any](t *testing.T, ctx context.Context, client *connectormcp.Client, name string, arguments any) T {
	t.Helper()
	raw, err := client.CallTool(ctx, name, arguments)
	if err != nil {
		t.Fatalf("%s: %v", name, err)
	}
	var result struct {
		StructuredContent json.RawMessage `json:"structuredContent"`
		IsError           bool            `json:"isError"`
	}
	if err := json.Unmarshal(raw, &result); err != nil {
		t.Fatalf("%s response: %v", name, err)
	}
	if result.IsError {
		t.Fatalf("%s returned isError=true: %s", name, raw)
	}
	var out T
	if err := json.Unmarshal(result.StructuredContent, &out); err != nil {
		t.Fatalf("%s structuredContent: %v (%s)", name, err, raw)
	}
	return out
}
