package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync"
	"testing"
)

func TestSaveSourcePreservesOtherSourcesAndStoredSecrets(t *testing.T) {
	dir := t.TempDir()
	instance, err := New(Options{DataDir: dir, CloudBase: "http://127.0.0.1:1", EdgeBase: "https://mcp.example.com"})
	if err != nil {
		t.Fatal(err)
	}

	first, err := instance.SaveSource(SourceInput{
		DisplayName: "库存查询",
		UpstreamURL: "http://10.0.0.8:8000/mcp",
		BearerToken: "first-secret",
		DynamicAuth: true,
		AppID:       "MCP",
		Channel:     "channel-one",
		UserID:      "user-one",
		HMACKey:     "short-hmac-one",
		Tools:       []string{"get_inventory", "lookup_order", "get_inventory"},
	})
	if err != nil {
		t.Fatalf("保存第一个来源失败: %v", err)
	}
	if !first.Created || first.Source.SourceID == "" {
		t.Fatalf("第一个来源创建结果不符: %+v", first)
	}
	if got, want := first.Source.ToolAllowlist, []string{"get_inventory", "lookup_order"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("工具允许列表未排序去重: got=%v want=%v", got, want)
	}

	second, err := instance.SaveSource(SourceInput{
		DisplayName: "订单查询",
		UpstreamURL: "http://10.0.0.9:8000/mcp",
		BearerToken: "second-secret",
		DynamicAuth: true,
		AppID:       "MCP",
		Channel:     "channel-two",
		UserID:      "user-two",
		HMACKey:     "short-hmac-two",
		Tools:       []string{"lookup_order"},
	})
	if err != nil {
		t.Fatalf("保存第二个来源失败: %v", err)
	}

	updated, err := instance.SaveSource(SourceInput{
		SourceID:    first.Source.SourceID,
		DisplayName: "库存查询（维护中）",
		UpstreamURL: "http://10.0.0.8:8000/mcp",
		DynamicAuth: true,
		AppID:       "MCP",
		Channel:     "channel-one",
		UserID:      "user-one",
		Tools:       []string{"lookup_order"},
	})
	if err != nil {
		t.Fatalf("更新第一个来源失败: %v", err)
	}
	if updated.Created {
		t.Fatal("更新现有来源不应标记为新建")
	}

	instance.mu.RLock()
	if len(instance.config.Sources) != 2 {
		instance.mu.RUnlock()
		t.Fatalf("更新单个来源不应删除其他来源: %+v", instance.config.Sources)
	}
	firstSecret := cloneHeaders(instance.secrets[first.Source.SourceID])
	secondSecret := cloneHeaders(instance.secrets[second.Source.SourceID])
	firstProfile := instance.profiles[first.Source.SourceID]
	secondProfile := instance.profiles[second.Source.SourceID]
	instance.mu.RUnlock()

	if firstSecret["Authorization"] != "Bearer first-secret" {
		t.Fatalf("留空访问令牌时必须保留原值: %q", firstSecret["Authorization"])
	}
	if secondSecret["Authorization"] != "Bearer second-secret" {
		t.Fatalf("更新其他来源时不得改变第二个来源令牌: %q", secondSecret["Authorization"])
	}
	if firstProfile.HMACKey != "short-hmac-one" || secondProfile.HMACKey != "short-hmac-two" {
		t.Fatalf("留空 HMAC 时必须只保留当前来源的密钥: first=%+v second=%+v", firstProfile, secondProfile)
	}

	statusRaw, err := json.Marshal(instance.Status())
	if err != nil {
		t.Fatal(err)
	}
	for _, secret := range []string{"first-secret", "second-secret", "short-hmac-one", "short-hmac-two"} {
		if strings.Contains(string(statusRaw), secret) {
			t.Fatalf("状态接口泄漏秘密 %q: %s", secret, statusRaw)
		}
	}

	reloaded, err := New(Options{DataDir: dir, CloudBase: "http://127.0.0.1:1", EdgeBase: "https://mcp.example.com"})
	if err != nil {
		t.Fatal(err)
	}
	reloaded.mu.RLock()
	defer reloaded.mu.RUnlock()
	if len(reloaded.config.Sources) != 2 ||
		reloaded.secrets[first.Source.SourceID]["Authorization"] != "Bearer first-secret" ||
		reloaded.secrets[second.Source.SourceID]["Authorization"] != "Bearer second-secret" ||
		reloaded.profiles[first.Source.SourceID].HMACKey != "short-hmac-one" ||
		reloaded.profiles[second.Source.SourceID].HMACKey != "short-hmac-two" {
		t.Fatalf("重启后来源或秘密未完整保留: sources=%+v secrets=%+v profiles=%+v", reloaded.config.Sources, reloaded.secrets, reloaded.profiles)
	}
}

func TestSaveSourceRejectsUnsafeOrIncompleteOnboarding(t *testing.T) {
	instance, err := New(Options{DataDir: t.TempDir(), CloudBase: "http://127.0.0.1:1", EdgeBase: "https://mcp.example.com"})
	if err != nil {
		t.Fatal(err)
	}

	cases := []struct {
		name string
		in   SourceInput
		want string
	}{
		{
			name: "缺少工具允许列表",
			in:   SourceInput{DisplayName: "库存", UpstreamURL: "http://10.0.0.8:8000/mcp"},
			want: "至少勾选一个",
		},
		{
			name: "通配符绕过允许列表",
			in:   SourceInput{DisplayName: "库存", UpstreamURL: "http://10.0.0.8:8000/mcp", Tools: []string{"*"}},
			want: "工具名称格式无效",
		},
		{
			name: "动态认证字段不完整",
			in: SourceInput{
				DisplayName: "库存", UpstreamURL: "http://10.0.0.8:8000/mcp",
				DynamicAuth: true, AppID: "MCP", UserID: "user-one", HMACKey: "short-hmac", Tools: []string{"get_inventory"},
			},
			want: "channel",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := instance.SaveSource(tc.in); err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("应拒绝无效配置并包含 %q，实际 %v", tc.want, err)
			}
			if got := len(instance.Status().Sources); got != 0 {
				t.Fatalf("失败保存不得写入来源，实际 %d 个", got)
			}
		})
	}
}

func TestSaveSourceCanRemoveOnlyCurrentDynamicProfile(t *testing.T) {
	instance, err := New(Options{DataDir: t.TempDir(), CloudBase: "http://127.0.0.1:1", EdgeBase: "https://mcp.example.com"})
	if err != nil {
		t.Fatal(err)
	}
	first, err := instance.SaveSource(SourceInput{
		DisplayName: "库存", UpstreamURL: "http://10.0.0.8:8000/mcp", BearerToken: "one",
		DynamicAuth: true, AppID: "MCP", Channel: "c1", UserID: "u1", HMACKey: "h1", Tools: []string{"get_inventory"},
	})
	if err != nil {
		t.Fatal(err)
	}
	second, err := instance.SaveSource(SourceInput{
		DisplayName: "订单", UpstreamURL: "http://10.0.0.9:8000/mcp", BearerToken: "two",
		DynamicAuth: true, AppID: "MCP", Channel: "c2", UserID: "u2", HMACKey: "h2", Tools: []string{"lookup_order"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := instance.SaveSource(SourceInput{
		SourceID: first.Source.SourceID, DisplayName: first.Source.DisplayName, UpstreamURL: first.Source.UpstreamURL,
		Tools: []string{"get_inventory"},
	}); err != nil {
		t.Fatal(err)
	}
	instance.mu.RLock()
	_, firstExists := instance.profiles[first.Source.SourceID]
	secondProfile, secondExists := instance.profiles[second.Source.SourceID]
	instance.mu.RUnlock()
	if firstExists || !secondExists || secondProfile.HMACKey != "h2" {
		t.Fatalf("取消当前来源动态认证时不得影响其他来源: first=%v second=%+v", firstExists, secondProfile)
	}
}

func TestDiscoverSourceToolsOnlyInitializesAndListsTools(t *testing.T) {
	var mu sync.Mutex
	var methods []string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Authorization"); got != "Bearer synthetic-token" {
			http.Error(w, "authorization mismatch", http.StatusUnauthorized)
			return
		}
		var request struct {
			ID     int64  `json:"id"`
			Method string `json:"method"`
		}
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		mu.Lock()
		methods = append(methods, request.Method)
		mu.Unlock()
		if request.Method == "notifications/initialized" {
			w.WriteHeader(http.StatusAccepted)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		switch request.Method {
		case "initialize":
			_, _ = fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{"protocolVersion":"2025-06-18","serverInfo":{"name":"synthetic","version":"1"}}}`, request.ID)
		case "tools/list":
			_, _ = fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{"tools":[{"name":"zeta","description":"Z"},{"name":"alpha","title":"Alpha"}]}}`, request.ID)
		default:
			http.Error(w, "unexpected method", http.StatusBadRequest)
		}
	}))
	defer upstream.Close()

	instance, err := New(Options{DataDir: t.TempDir(), CloudBase: "http://127.0.0.1:1", EdgeBase: "https://mcp.example.com"})
	if err != nil {
		t.Fatal(err)
	}
	tools, err := instance.DiscoverSourceTools(context.Background(), SourceInput{
		DisplayName: "库存", UpstreamURL: upstream.URL, BearerToken: "synthetic-token",
		DynamicAuth: true, AppID: "MCP", Channel: "synthetic-channel", UserID: "synthetic-user", HMACKey: "x",
	})
	if err != nil {
		t.Fatalf("读取工具列表失败: %v", err)
	}
	if got, want := []string{tools[0].Name, tools[1].Name}, []string{"alpha", "zeta"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("工具名称未排序: got=%v want=%v", got, want)
	}
	mu.Lock()
	gotMethods := append([]string(nil), methods...)
	mu.Unlock()
	for _, method := range gotMethods {
		if method == "tools/call" {
			t.Fatalf("工具发现不得调用业务工具: %v", gotMethods)
		}
	}
	if len(instance.Status().Sources) != 0 {
		t.Fatal("读取工具列表不得持久化草稿")
	}
}
