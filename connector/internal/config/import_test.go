package config

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestParseMCPMergedConfig(t *testing.T) {
	raw := []byte(`{
	  "mcpServers": {
	    "库存查询": {
	      "type": "http",
	      "url": "http://10.0.0.8:8000/mcp",
	      "headers": {"Authorization": "Bearer secret-token"}
	    }
	  }
	}`)
	report, err := ParseHostConfig(raw, ImportOptions{})
	if err != nil {
		t.Fatalf("解析失败: %v", err)
	}
	if report.HasErrors() {
		t.Fatalf("不应有错误: %+v", report.Errors())
	}
	if len(report.Sources) != 1 {
		t.Fatalf("期望 1 个来源，得到 %d", len(report.Sources))
	}
	source := report.Sources[0]
	if source.UpstreamURL != "http://10.0.0.8:8000/mcp" {
		t.Fatalf("地址解析错误: %s", source.UpstreamURL)
	}
	if source.Auth.Mode != AuthModeFixedHeader || source.Auth.CredentialRef == "" {
		t.Fatalf("认证模式未转成凭据引用: %+v", source.Auth)
	}
	if got := report.Secrets[source.SourceID]["Authorization"]; got != "Bearer secret-token" {
		t.Fatalf("秘密未收集: %q", got)
	}
}

func TestLegacySourceConfigWithoutDisabledFieldRemainsEnabled(t *testing.T) {
	var connector Connector
	raw := []byte(`{
	  "schema_version": 1,
	  "sources": [{
	    "schema_version": 1,
	    "source_id": "src_00112233445566778899aabbccddeeff",
	    "display_name": "旧来源",
	    "transport": "streamable_http",
	    "upstream_url": "http://127.0.0.1:18765/mcp",
	    "auth": {"mode": "none", "credential_location": "local_vault"},
	    "tool_allowlist": ["lookup_order"]
	  }]
	}`)
	if err := json.Unmarshal(raw, &connector); err != nil {
		t.Fatalf("旧配置兼容解析失败: %v", err)
	}
	if len(connector.Sources) != 1 {
		t.Fatalf("旧配置来源数量不符: %+v", connector.Sources)
	}
	if connector.Sources[0].Disabled {
		t.Fatal("旧配置没有 disabled 字段时必须默认启用")
	}
}

func TestParseRejectsStdioWithoutConditional(t *testing.T) {
	raw := []byte(`{"mcpServers":{"本地工具":{"command":"node","args":["server.js"]}}}`)
	report, err := ParseHostConfig(raw, ImportOptions{})
	if err != nil {
		t.Fatalf("解析失败: %v", err)
	}
	if !report.HasErrors() {
		t.Fatal("标准输入输出型 MCP 首版应被拒绝")
	}
	if !strings.Contains(report.Errors()[0].Message, "条件适配") {
		t.Fatalf("错误信息不够明确: %s", report.Errors()[0].Message)
	}
}

func TestParseRejectsUnresolvedVariable(t *testing.T) {
	raw := []byte(`{"mcpServers":{"订单":{"type":"http","url":"http://10.0.0.9:9000/mcp","headers":{"Authorization":"Bearer ${TOKEN}"}}}}`)
	report, err := ParseHostConfig(raw, ImportOptions{})
	if err != nil {
		t.Fatalf("解析失败: %v", err)
	}
	if !report.HasErrors() {
		t.Fatal("未解析变量必须阻断导入")
	}
	if report.Errors()[0].Code != "CONFIG_UNRESOLVED_VARIABLE" {
		t.Fatalf("错误码不符: %s", report.Errors()[0].Code)
	}
}

func TestParseRejectsHostOutsideAllowlist(t *testing.T) {
	raw := []byte(`{"mcpServers":{"订单":{"type":"http","url":"http://10.0.0.9:9000/mcp"}}}`)
	report, err := ParseHostConfig(raw, ImportOptions{AllowedHosts: []string{"corp.example.com"}})
	if err != nil {
		t.Fatalf("解析失败: %v", err)
	}
	if !report.HasErrors() || report.Errors()[0].Code != "SOURCE_HOST_NOT_ALLOWED" {
		t.Fatalf("主机允许范围未生效: %+v", report.Issues)
	}
}

func TestParseRejectsReservedAndDuplicateHeaders(t *testing.T) {
	raw := []byte(`{
	  "mcpServers": {
	    "订单": {
	      "type": "http",
	      "url": "http://10.0.0.9:9000/mcp",
	      "headers": {
	        "Authorization": "Bearer ok",
	        "authorization": "Bearer duplicate",
	        "Host": "evil.example.com"
	      }
	    }
	  }
	}`)
	report, err := ParseHostConfig(raw, ImportOptions{})
	if err != nil {
		t.Fatalf("解析失败: %v", err)
	}
	codes := map[string]bool{}
	for _, issue := range report.Errors() {
		codes[issue.Code] = true
	}
	if !codes["CONFIG_DUPLICATE_HEADER"] || !codes["CONFIG_HEADER_NOT_ALLOWED"] {
		t.Fatalf("请求头约束未生效: %+v", report.Errors())
	}
}

func TestParseRejectsQueryAndFragmentInUpstreamURL(t *testing.T) {
	raw := []byte(`{"mcpServers":{"订单":{"type":"http","url":"http://10.0.0.9:9000/mcp?target=evil#fragment"}}}`)
	report, err := ParseHostConfig(raw, ImportOptions{})
	if err != nil {
		t.Fatalf("解析失败: %v", err)
	}
	if !report.HasErrors() {
		t.Fatal("上游查询串和片段必须拒绝")
	}
}

func TestBuildPlatformConfigMatchesContract(t *testing.T) {
	sources := []Source{{
		SchemaVersion: 1, SourceID: "src_inventory", DisplayName: "库存查询",
		Transport: TransportStreamableHTTP, UpstreamURL: "http://10.0.0.8:8000/mcp",
	}}
	raw, err := BuildPlatformConfig(sources, "https://mcp.example.com/", "")
	if err != nil {
		t.Fatalf("导出失败: %v", err)
	}
	var payload struct {
		MCPServers map[string]struct {
			Type    string            `json:"type"`
			URL     string            `json:"url"`
			Headers map[string]string `json:"headers"`
		} `json:"mcpServers"`
	}
	if err := json.Unmarshal(raw, &payload); err != nil {
		t.Fatalf("导出不是合法 JSON: %v", err)
	}
	entry, ok := payload.MCPServers["库存查询"]
	if !ok {
		t.Fatal("缺少来源条目")
	}
	if entry.Type != "http" {
		t.Fatalf("类型必须是 http: %s", entry.Type)
	}
	if entry.URL != "https://mcp.example.com/sources/src_inventory/mcp" {
		t.Fatalf("地址不符合 Edge 约定: %s", entry.URL)
	}
	if len(entry.Headers) != 0 {
		t.Fatalf("平台配置不得导出静态授权头: %+v", entry.Headers)
	}
}

func TestBuildPlatformConfigRejectsStaticBearer(t *testing.T) {
	sources := []Source{{SourceID: "s1", DisplayName: "库存", UpstreamURL: "http://10.0.0.1/mcp", Transport: TransportStreamableHTTP}}
	if _, err := BuildPlatformConfig(sources, "https://mcp.example.com", "static-token"); err == nil {
		t.Fatal("静态用户令牌必须被拒绝")
	}
}
