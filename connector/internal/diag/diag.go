// Package diag 实现本机分层诊断。
//
// 依据文档第 04、12 章：先检查网络与 TLS，再检查协议与认证，支持指定一条测试查询，
// 不会遍历执行所有工具；诊断结果不包含业务正文和凭据。
package diag

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/config"
	"github.com/sherkevin/open-claude-code/connector/internal/mcp"
)

// 检查状态。
const (
	StatusPass = "pass"
	StatusWarn = "warn"
	StatusFail = "fail"
	StatusSkip = "skip"
)

// Check 是单项检查结果。
type Check struct {
	Name         string `json:"name"`
	Label        string `json:"label"`
	Status       string `json:"status"`
	Code         string `json:"code,omitempty"`
	Detail       string `json:"detail,omitempty"`
	DurationMS   int64  `json:"duration_ms"`
	HTTPStatus   int    `json:"http_status,omitempty"`
	UpstreamCode string `json:"upstream_code,omitempty"`
	RequestID    string `json:"request_id,omitempty"`
}

// Report 是一次诊断的完整结果。
type Report struct {
	SourceID  string    `json:"source_id"`
	StartedAt time.Time `json:"started_at"`
	OK        bool      `json:"ok"`
	Checks    []Check   `json:"checks"`
}

// Options 控制诊断行为。
type Options struct {
	// HeaderValues 是上游认证头的真实值，只在本次诊断的内存中使用。
	HeaderValues map[string]string
	// Timeout 是单项检查的超时，默认 5 秒。
	Timeout time.Duration
	// ToolProbe 指定要试调用的一条只读工具；为空则只做工具目录检查。
	ToolProbe string
	// ToolArguments 是试调用参数，默认空对象。
	ToolArguments any
	// ReplayProbe aligns transport headers with the installed authentication
	// proxy for a fresh direct session and emits only bounded diagnostic metadata.
	ReplayProbe bool
	// HeaderProvider injects request-specific authentication headers. It is
	// only used by local diagnosis and must not be sourced from browser input.
	HeaderProvider func(method, tool string) (map[string]string, error)
}

func (o Options) timeout() time.Duration {
	if o.Timeout <= 0 {
		return 5 * time.Second
	}
	return o.Timeout
}

// Run 对单个来源执行完整诊断。
func Run(ctx context.Context, source config.Source, opts Options) Report {
	report := Report{SourceID: source.SourceID, StartedAt: time.Now().UTC()}

	report.Checks = append(report.Checks, checkConfig(source))
	if report.Checks[len(report.Checks)-1].Status == StatusFail {
		report.OK = false
		return report
	}

	parsed, err := url.Parse(source.UpstreamURL)
	if err != nil || parsed.Host == "" {
		report.Checks = append(report.Checks, Check{Name: "network", Label: "内网连通性", Status: StatusFail, Code: "UPSTREAM_UNREACHABLE", Detail: "地址无法解析"})
		return report
	}

	report.Checks = append(report.Checks, checkDial(ctx, parsed, opts.timeout()))
	if report.Checks[len(report.Checks)-1].Status == StatusFail {
		return report
	}

	if parsed.Scheme == "https" {
		report.Checks = append(report.Checks, checkTLS(ctx, parsed, opts.timeout()))
		if report.Checks[len(report.Checks)-1].Status == StatusFail {
			return report
		}
	} else {
		report.Checks = append(report.Checks, Check{Name: "tls", Label: "证书校验", Status: StatusSkip, Detail: "来源使用明文 HTTP，按您已批准的内网范围执行"})
	}

	client := &mcp.Client{
		Endpoint:       source.UpstreamURL,
		Headers:        opts.HeaderValues,
		HTTP:           newHTTPClient(opts.timeout()),
		HeaderProvider: opts.HeaderProvider,
	}
	if opts.ReplayProbe {
		transport := http.DefaultTransport.(*http.Transport).Clone()
		transport.Proxy = nil
		transport.DisableCompression = true
		transport.ResponseHeaderTimeout = 30 * time.Second
		transport.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12}
		defer transport.CloseIdleConnections()
		client.HTTP = &http.Client{Transport: transport, Timeout: opts.timeout()}
		client.UserAgent = "kaiwu-intranet-connector-authproxy/1"
		client.ProtocolVersion = "2025-11-25"
	}

	initCtx, cancel := context.WithTimeout(ctx, opts.timeout())
	info, initErr := client.Initialize(initCtx)
	cancel()
	if initErr != nil {
		code := classify(initErr)
		detail := initErr.Error()
		if opts.ReplayProbe {
			detail = safeProbeFailure(initErr, code)
		}
		report.Checks = append(report.Checks, Check{Name: "protocol", Label: "协议握手", Status: StatusFail, Code: code, Detail: detail, HTTPStatus: client.LastHTTPStatus()})
		return report
	}
	report.Checks = append(report.Checks, Check{
		Name: "protocol", Label: "协议握手", Status: StatusPass,
		Detail: fmt.Sprintf("服务端 %s %s，协议版本 %s", info.ServerInfo.Name, info.ServerInfo.Version, info.ProtocolVersion),
	})

	toolsCtx, cancel := context.WithTimeout(ctx, opts.timeout())
	tools, toolsErr := client.ListTools(toolsCtx)
	cancel()
	if toolsErr != nil {
		detail := toolsErr.Error()
		if opts.ReplayProbe {
			detail = safeProbeFailure(toolsErr, classify(toolsErr))
		}
		report.Checks = append(report.Checks, Check{Name: "auth", Label: "认证与工具目录", Status: StatusFail, Code: classify(toolsErr), Detail: detail, HTTPStatus: client.LastHTTPStatus()})
		return report
	}
	check := Check{Name: "auth", Label: "认证与工具目录", Status: StatusPass, Detail: fmt.Sprintf("可用工具 %d 个", len(tools))}
	if len(source.ToolAllowlist) > 0 {
		available := map[string]bool{}
		for _, tool := range tools {
			available[tool.Name] = true
		}
		var missing []string
		for _, name := range source.ToolAllowlist {
			if !available[name] {
				missing = append(missing, name)
			}
		}
		if len(missing) > 0 {
			check.Status = StatusFail
			check.Code = "TOOL_ALLOWLIST_MISMATCH"
			check.Detail += "；允许列表中的工具不存在: " + strings.Join(missing, ", ")
		}
	}
	report.Checks = append(report.Checks, check)
	if check.Status == StatusFail {
		return report
	}

	if opts.ToolProbe != "" {
		args := opts.ToolArguments
		if args == nil {
			args = map[string]any{}
		}
		callCtx, cancel := context.WithTimeout(ctx, opts.timeout())
		started := time.Now()
		raw, callErr := client.CallTool(callCtx, opts.ToolProbe, args)
		cancel()
		result := Check{Name: "tool", Label: "测试查询", Status: StatusPass, DurationMS: time.Since(started).Milliseconds(), Detail: "调用成功: " + opts.ToolProbe, HTTPStatus: client.LastHTTPStatus()}
		if callErr != nil {
			result.Status = StatusFail
			result.Code = classify(callErr)
			result.Detail = safeProbeFailure(callErr, result.Code)
		} else {
			inspectToolResult(raw, &result)
		}
		report.Checks = append(report.Checks, result)
		if result.Status == StatusFail {
			return report
		}
	}

	report.OK = true
	return report
}

// Tool errors are returned within a successful JSON-RPC result. HTTP 200 is
// not proof that the business tool succeeded. Never echo arbitrary remote
// text, record content, or credential-bearing headers into diagnostics.
var diagnosticRequestID = regexp.MustCompile(`(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

func inspectToolResult(raw json.RawMessage, check *Check) {
	var result struct {
		IsError bool `json:"isError"`
		Content []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		} `json:"content"`
		StructuredContent json.RawMessage `json:"structuredContent"`
	}
	if err := json.Unmarshal(raw, &result); err != nil || (result.Content == nil && len(result.StructuredContent) == 0) {
		check.Status, check.Code, check.Detail = StatusFail, "MCP_TOOL_RESPONSE_INVALID", "上游工具返回格式无效"
		return
	}
	if !result.IsError {
		return
	}
	check.Status, check.Code, check.Detail = StatusFail, "MCP_TOOL_ERROR", "上游返回 isError=true，业务工具执行失败"
	inspect := func(data []byte) {
		var item struct {
			Code      string `json:"code"`
			RequestID string `json:"request_id"`
		}
		if len(data) > 16<<10 || json.Unmarshal(data, &item) != nil {
			return
		}
		// Recognize the observed upstream diagnostic code, rather than
		// transferring arbitrary error text or untrusted field contents.
		if item.Code == "QUERY_EXECUTION_FAILED" {
			check.UpstreamCode = item.Code
			if diagnosticRequestID.MatchString(item.RequestID) {
				check.RequestID = item.RequestID
			}
		}
	}
	inspect(result.StructuredContent)
	for _, item := range result.Content {
		if item.Type == "text" {
			inspect([]byte(item.Text))
		}
	}
}

func safeProbeFailure(err error, code string) string {
	var status interface{ HTTPStatusCode() int }
	var rpc *mcp.RPCError
	if errors.As(err, &status) {
		return fmt.Sprintf("上游 HTTP %d", status.HTTPStatusCode())
	}
	if errors.As(err, &rpc) {
		return fmt.Sprintf("上游 JSON-RPC 错误 %d", rpc.Code)
	}
	return "MCP 请求未完成：" + code
}

func checkConfig(source config.Source) Check {
	started := time.Now()
	check := Check{Name: "config", Label: "配置校验", Status: StatusPass}
	parsed, err := url.Parse(source.UpstreamURL)
	switch {
	case source.UpstreamURL == "" || err != nil || parsed.Host == "":
		check.Status = StatusFail
		check.Code = "CONFIG_INVALID"
		check.Detail = "上游地址无效"
	case source.Transport != config.TransportStreamableHTTP && source.Transport != config.TransportLegacySSE:
		check.Status = StatusFail
		check.Code = "CONFIG_UNSUPPORTED"
		check.Detail = "传输类型不在首版范围: " + source.Transport
	case parsed.Scheme != "http" && parsed.Scheme != "https":
		check.Status = StatusFail
		check.Code = "CONFIG_INVALID"
		check.Detail = "只支持 http 或 https 地址"
	default:
		check.Detail = "地址、传输与认证模式通过检查"
	}
	check.DurationMS = time.Since(started).Milliseconds()
	return check
}

func checkDial(ctx context.Context, parsed *url.URL, timeout time.Duration) Check {
	started := time.Now()
	host := parsed.Host
	if !strings.Contains(host, ":") {
		if parsed.Scheme == "https" {
			host += ":443"
		} else {
			host += ":80"
		}
	}
	dialer := &net.Dialer{Timeout: timeout}
	dialCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	conn, err := dialer.DialContext(dialCtx, "tcp", host)
	check := Check{Name: "network", Label: "内网连通性", Status: StatusPass, DurationMS: time.Since(started).Milliseconds()}
	if err != nil {
		check.Status = StatusFail
		check.Code = "UPSTREAM_UNREACHABLE"
		check.Detail = err.Error()
		return check
	}
	conn.Close()
	check.Detail = "可以连接 " + host
	return check
}

func checkTLS(ctx context.Context, parsed *url.URL, timeout time.Duration) Check {
	started := time.Now()
	host := parsed.Hostname()
	port := parsed.Port()
	if port == "" {
		port = "443"
	}
	dialer := &net.Dialer{Timeout: timeout}
	conn, err := tls.DialWithDialer(dialer, "tcp", net.JoinHostPort(host, port), &tls.Config{ServerName: host, MinVersion: tls.VersionTLS12})
	check := Check{Name: "tls", Label: "证书校验", Status: StatusPass, DurationMS: time.Since(started).Milliseconds()}
	if err != nil {
		check.Status = StatusFail
		check.Code = "CLOUD_TLS_FAILED"
		check.Detail = err.Error()
		return check
	}
	defer conn.Close()
	state := conn.ConnectionState()
	if len(state.PeerCertificates) == 0 {
		check.Status = StatusFail
		check.Code = "CLOUD_TLS_FAILED"
		check.Detail = "服务端未提供证书"
		return check
	}
	cert := state.PeerCertificates[0]
	days := int(time.Until(cert.NotAfter).Hours() / 24)
	check.Detail = fmt.Sprintf("证书主体 %s，剩余 %d 天", cert.Subject.CommonName, days)
	if days < 0 {
		check.Status = StatusFail
		check.Code = "CLOUD_TLS_FAILED"
		check.Detail = "证书已过期"
	} else if days < 30 {
		check.Status = StatusWarn
		check.Detail += "，建议尽快更换证书"
	}
	return check
}

func newHTTPClient(timeout time.Duration) *http.Client {
	return &http.Client{Timeout: timeout}
}

func classify(err error) string {
	message := strings.ToLower(err.Error())
	switch {
	case strings.Contains(message, "认证"), strings.Contains(message, "401"), strings.Contains(message, "403"):
		return "UPSTREAM_AUTH_FAILED"
	case strings.Contains(message, "超时"), strings.Contains(message, "timeout"), strings.Contains(message, "deadline"):
		return "TOOL_TIMEOUT"
	case strings.Contains(message, "证书"), strings.Contains(message, "tls"), strings.Contains(message, "x509"):
		return "CLOUD_TLS_FAILED"
	default:
		return "MCP_INCOMPATIBLE"
	}
}
