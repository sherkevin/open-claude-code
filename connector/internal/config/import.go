package config

import (
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/url"
	"regexp"
	"sort"
	"strings"
)

// ImportOptions 控制导入时允许的格式与地址范围。
type ImportOptions struct {
	// AllowedHosts 为空表示不限制主机，只做协议与格式校验。
	AllowedHosts []string
	// AllowStdio 只有在明确进入条件分支时才置为 true。
	AllowStdio bool
	// SourceIDPrefix 用于给导入条目生成稳定的来源编号。
	SourceIDPrefix string
}

var variablePattern = regexp.MustCompile(`\$\{[^}]*\}`)

var knownHostKeys = map[string]bool{
	"mcpServers": true,
}

var knownServerKeys = map[string]bool{
	"type": true, "transport": true, "url": true, "headers": true,
	"command": true, "args": true, "cwd": true, "env": true,
	"envFile": true, "headersHelper": true, "disabled": true,
	"name": true, "description": true,
}

// ParseHostConfig 解析并校验宿主 MCP 配置。
//
// 支持的输入：
//  1. 容器格式 {"mcpServers": {"name": {...}}}；
//  2. 单个服务器对象 {"type":"http","url":"..."}；
//  3. 本连接器的内部来源对象（含 schema_version 与 source_id）。
func ParseHostConfig(raw []byte, opts ImportOptions) (ImportReport, error) {
	report := ImportReport{Secrets: map[string]map[string]string{}}
	if len(strings.TrimSpace(string(raw))) == 0 {
		return report, fmt.Errorf("%w: 配置文件为空", ErrInvalid)
	}

	var top map[string]json.RawMessage
	if err := json.Unmarshal(raw, &top); err != nil {
		return report, fmt.Errorf("%w: %v", ErrInvalid, err)
	}

	switch {
	case hasKey(top, "schema_version") && hasKey(top, "source_id"):
		report.Format = "internal"
		source, secrets, issues := parseInternalSource(top)
		report.Sources = append(report.Sources, source)
		report.Issues = append(report.Issues, issues...)
		if len(secrets) > 0 {
			report.Secrets[source.SourceID] = secrets
		}
	case hasKey(top, "mcpServers"):
		report.Format = "mcpServers"
		for key, value := range top {
			if key != "mcpServers" && !knownHostKeys[key] {
				report.Issues = append(report.Issues, Issue{
					Code: "CONFIG_UNSUPPORTED", Path: key,
					Message:  "顶层字段暂不支持，已忽略；如为必需配置请拆分后单独导入",
					Severity: "warning",
				})
			}
			_ = value
		}
		var servers map[string]json.RawMessage
		if err := json.Unmarshal(top["mcpServers"], &servers); err != nil {
			return report, fmt.Errorf("%w: mcpServers 必须是对象", ErrInvalid)
		}
		names := make([]string, 0, len(servers))
		for name := range servers {
			names = append(names, name)
		}
		sort.Strings(names)
		for _, name := range names {
			source, secrets, issues := parseServerEntry(name, servers[name], opts)
			report.Issues = append(report.Issues, issues...)
			if source != nil {
				report.Sources = append(report.Sources, *source)
				if len(secrets) > 0 {
					report.Secrets[source.SourceID] = secrets
				}
			}
		}
	case hasKey(top, "url") || hasKey(top, "command"):
		report.Format = "single"
		source, secrets, issues := parseServerEntry("mcp", raw, opts)
		report.Issues = append(report.Issues, issues...)
		if source != nil {
			report.Sources = append(report.Sources, *source)
			if len(secrets) > 0 {
				report.Secrets[source.SourceID] = secrets
			}
		}
	default:
		return report, fmt.Errorf("%w: 未识别的配置格式，请提供 mcpServers 容器或单个 HTTP MCP 配置", ErrUnsupported)
	}

	report.Sources = normalizeSources(report.Sources)
	return report, nil
}

func parseInternalSource(top map[string]json.RawMessage) (Source, map[string]string, []Issue) {
	var source Source
	var issues []Issue
	body, _ := json.Marshal(top)
	if err := json.Unmarshal(body, &source); err != nil {
		issues = append(issues, errorIssue("CONFIG_INVALID", "", "内部配置无法解析: "+err.Error()))
		return source, nil, issues
	}
	if source.SchemaVersion == 0 {
		source.SchemaVersion = CurrentSchemaVersion
	}
	if source.Transport == "" {
		source.Transport = TransportStreamableHTTP
	}
	issues = append(issues, validateSource(&source, ImportOptions{})...)
	return source, nil, issues
}

func parseServerEntry(name string, raw json.RawMessage, opts ImportOptions) (*Source, map[string]string, []Issue) {
	var issues []Issue
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil {
		return nil, nil, []Issue{errorIssue("CONFIG_INVALID", name, "条目必须是对象")}
	}

	for key := range fields {
		if !knownServerKeys[key] {
			issues = append(issues, Issue{
				Code: "CONFIG_UNSUPPORTED", Path: name + "." + key,
				Message:  "字段暂不支持，已从导入结果中忽略",
				Severity: "warning",
			})
		}
	}

	source := Source{
		SchemaVersion: CurrentSchemaVersion,
		SourceID:      sourceID(opts.SourceIDPrefix, name),
		DisplayName:   displayName(name),
		Transport:     TransportStreamableHTTP,
		Auth:          Auth{Mode: AuthModeNone, CredentialLocation: CredentialLocationLocal},
	}

	if rawType, ok := fields["type"]; ok {
		var value string
		if err := json.Unmarshal(rawType, &value); err == nil {
			switch strings.ToLower(strings.TrimSpace(value)) {
			case "http", "streamable-http", "streamable_http", "streamablehttp":
				source.Transport = TransportStreamableHTTP
			case "sse", "legacy-sse", "legacy_sse":
				source.Transport = TransportLegacySSE
				issues = append(issues, Issue{
					Code: "PROTOCOL_LEGACY_SSE", Path: name + ".type",
					Message:  "旧版 SSE 传输需要按条件分支单独验收，首版默认不启用",
					Severity: "warning",
				})
			case "stdio":
				source.Transport = TransportStdio
			default:
				issues = append(issues, errorIssue("CONFIG_UNSUPPORTED", name+".type", "不支持的传输类型: "+value))
			}
		}
	}
	if rawTransport, ok := fields["transport"]; ok {
		var value string
		if err := json.Unmarshal(rawTransport, &value); err == nil && value != "" {
			switch strings.ToLower(strings.TrimSpace(value)) {
			case "http", "streamable-http", "streamable_http", "streamablehttp":
				source.Transport = TransportStreamableHTTP
			case "sse", "legacy-sse", "legacy_sse":
				source.Transport = TransportLegacySSE
			case "stdio":
				source.Transport = TransportStdio
			}
		}
	}

	if _, ok := fields["command"]; ok {
		source.Transport = TransportStdio
	}
	if source.Transport == TransportStdio {
		if opts.AllowStdio {
			issues = append(issues, Issue{
				Code: "STDIO_CONDITIONAL", Path: name,
				Message:  "该来源只能在本地进程方式运行，需要启用条件适配模块",
				Severity: "warning",
			})
		} else {
			issues = append(issues, errorIssue("CONFIG_UNSUPPORTED", name+".command", "标准输入输出型 MCP 需要单独的条件适配交付，首版不启用"))
		}
	}

	if rawURL, ok := fields["url"]; ok {
		var value string
		if err := json.Unmarshal(rawURL, &value); err == nil {
			source.UpstreamURL = strings.TrimSpace(value)
		}
	}
	if source.UpstreamURL == "" {
		issues = append(issues, errorIssue("CONFIG_INVALID", name+".url", "缺少 HTTP MCP 地址"))
	}

	secrets := map[string]string{}
	if rawHeaders, ok := fields["headers"]; ok {
		var headers map[string]string
		if err := json.Unmarshal(rawHeaders, &headers); err == nil {
			headerNames := make([]string, 0, len(headers))
			for header := range headers {
				headerNames = append(headerNames, header)
			}
			sort.Strings(headerNames)
			for _, header := range headerNames {
				value := headers[header]
				header = strings.TrimSpace(header)
				if header == "" {
					continue
				}
				source.HeaderNames = append(source.HeaderNames, header)
				secrets[header] = value
				if variablePattern.MatchString(value) {
					issues = append(issues, errorIssue("CONFIG_UNRESOLVED_VARIABLE", name+".headers."+header, "请求头中包含未解析变量: "+variablePattern.FindString(value)))
				}
			}
		} else {
			issues = append(issues, errorIssue("CONFIG_INVALID", name+".headers", "headers 必须是字符串键值对"))
		}
	}
	if len(secrets) > 0 {
		source.Auth = Auth{
			Mode:               AuthModeFixedHeader,
			CredentialLocation: CredentialLocationLocal,
			CredentialRef:      "cred_" + source.SourceID,
		}
	}
	if _, ok := fields["envFile"]; ok {
		issues = append(issues, Issue{
			Code: "CONFIG_UNSUPPORTED", Path: name + ".envFile",
			Message:  "环境变量文件不会被自动读取，请在明确的本机适配步骤中处理",
			Severity: "warning",
		})
	}
	if _, ok := fields["headersHelper"]; ok {
		issues = append(issues, Issue{
			Code: "CONFIG_UNSUPPORTED", Path: name + ".headersHelper",
			Message:  "动态认证脚本不会被自动执行，请改用固定请求头或单独适配",
			Severity: "warning",
		})
	}

	issues = append(issues, validateSource(&source, opts)...)
	return &source, secrets, dedupeIssues(issues)
}

func validateSource(source *Source, opts ImportOptions) []Issue {
	var issues []Issue
	if variablePattern.MatchString(source.UpstreamURL) {
		issues = append(issues, errorIssue("CONFIG_UNRESOLVED_VARIABLE", source.DisplayName+".url", "地址中包含未解析变量: "+variablePattern.FindString(source.UpstreamURL)))
	}
	if source.UpstreamURL != "" && !variablePattern.MatchString(source.UpstreamURL) {
		parsed, err := url.Parse(source.UpstreamURL)
		if err != nil || parsed.Host == "" {
			issues = append(issues, errorIssue("CONFIG_INVALID", source.DisplayName+".url", "地址无法解析"))
		} else {
			if parsed.User != nil {
				issues = append(issues, errorIssue("CONFIG_INVALID", source.DisplayName+".url", "地址中不能包含用户名密码"))
			}
			if parsed.Scheme != "http" && parsed.Scheme != "https" {
				issues = append(issues, errorIssue("CONFIG_INVALID", source.DisplayName+".url", "只支持 http 或 https 地址"))
			}
			if len(opts.AllowedHosts) > 0 && !hostAllowed(parsed.Hostname(), opts.AllowedHosts) {
				issues = append(issues, errorIssue("SOURCE_HOST_NOT_ALLOWED", source.DisplayName+".url", "主机不在本机允许范围内: "+parsed.Hostname()))
			}
		}
	}
	if source.Transport == TransportLegacySSE {
		issues = append(issues, Issue{
			Code: "PROTOCOL_LEGACY_SSE", Path: source.DisplayName,
			Message:  "旧版 SSE 与新协议行为不同，需按已冻结的协议档案执行",
			Severity: "warning",
		})
	}
	return issues
}

func normalizeSources(sources []Source) []Source {
	seen := map[string]int{}
	for i := range sources {
		base := sources[i].SourceID
		if base == "" {
			base = "src"
		}
		seen[base]++
		if seen[base] > 1 {
			sources[i].SourceID = fmt.Sprintf("%s_%d", base, seen[base])
		}
		if sources[i].DisplayName == "" {
			sources[i].DisplayName = sources[i].SourceID
		}
		if sources[i].SchemaVersion == 0 {
			sources[i].SchemaVersion = CurrentSchemaVersion
		}
	}
	return sources
}

func sourceID(prefix, name string) string {
	slug := strings.ToLower(strings.TrimSpace(name))
	var builder strings.Builder
	for _, r := range slug {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9':
			builder.WriteRune(r)
		case r == '-' || r == '_':
			builder.WriteRune('_')
		}
	}
	value := strings.Trim(builder.String(), "_")
	if value == "" {
		// 纯中文等非 ASCII 名称：用稳定摘要兜底，保证多次导入得到同一编号。
		sum := sha1.Sum([]byte(name))
		value = hex.EncodeToString(sum[:4])
	}
	if prefix != "" {
		value = prefix + "_" + value
	}
	return value
}

func displayName(name string) string {
	name = strings.TrimSpace(name)
	if name == "" {
		return "未命名来源"
	}
	return name
}

func hasKey(m map[string]json.RawMessage, key string) bool {
	_, ok := m[key]
	return ok
}

func hostAllowed(host string, allowed []string) bool {
	host = strings.ToLower(host)
	for _, item := range allowed {
		item = strings.ToLower(strings.TrimSpace(item))
		if item == "" {
			continue
		}
		if host == item || strings.HasSuffix(host, "."+item) {
			return true
		}
	}
	return false
}

func errorIssue(code, path, message string) Issue {
	return Issue{Code: code, Path: path, Message: message, Severity: "error"}
}

func dedupeIssues(issues []Issue) []Issue {
	seen := map[string]bool{}
	var out []Issue
	for _, issue := range issues {
		key := issue.Code + "|" + issue.Path + "|" + issue.Message
		if seen[key] {
			continue
		}
		seen[key] = true
		out = append(out, issue)
	}
	return out
}
