// Package config 定义连接器内部配置模型，以及宿主 MCP 配置的导入与平台配置导出。
//
// 设计依据：《企业内网 MCP 接入程序 开发全景文档 v1.0》第 07 章。
// 关键约束：
//   - 内部 schema 与宿主 schema 分离，导入时只接受已声明支持的格式；
//   - 秘密只保存引用，不写入普通配置与导出文件；
//   - 未知字段、未解析变量、相对路径一律明确报告，不做静默猜测。
package config

import "errors"

// 传输类型。首版只把 streamable_http 作为默认启用路径。
const (
	TransportStreamableHTTP = "streamable_http"
	TransportLegacySSE      = "legacy_sse"
	TransportStdio          = "stdio"
)

// 凭据模式。
const (
	AuthModeNone        = "none"
	AuthModeFixedHeader = "fixed_header"
)

// 凭据保存位置。
const (
	CredentialLocationLocal     = "local_vault"
	CredentialLocationCloud     = "cloud_vault"
	CredentialLocationReference = "reference"
)

// Limits 是单个来源的运行限制，默认收紧，按获批来源调整。
type Limits struct {
	Concurrency        int `json:"concurrency,omitempty"`
	ToolTimeoutSeconds int `json:"tool_timeout_seconds,omitempty"`
}

// Auth 描述如何向上游 MCP 认证。
type Auth struct {
	Mode               string `json:"mode"`
	CredentialLocation string `json:"credential_location"`
	CredentialRef      string `json:"credential_ref,omitempty"`
}

// Source 是连接器内部对一个 MCP 来源的描述（附录 A 的内部 schema）。
type Source struct {
	SchemaVersion   int      `json:"schema_version"`
	SourceID        string   `json:"source_id"`
	DisplayName     string   `json:"display_name"`
	Transport       string   `json:"transport"`
	UpstreamURL     string   `json:"upstream_url"`
	ProtocolProfile string   `json:"protocol_profile,omitempty"`
	Auth            Auth     `json:"auth"`
	ToolAllowlist   []string `json:"tool_allowlist,omitempty"`
	HeaderNames     []string `json:"header_names,omitempty"`
	Limits          Limits   `json:"limits,omitempty"`
	// Disabled 为 false 时来源参与诊断、登记和隧道启动。旧配置没有该字段，
	// 因而仍按启用处理；停用只改变连接范围，不删除来源、秘密或认证配置。
	Disabled bool `json:"disabled,omitempty"`
}

// Connector 是本机持久化的连接器配置。
type Connector struct {
	SchemaVersion int      `json:"schema_version"`
	ConnectorID   string   `json:"connector_id,omitempty"`
	TenantID      string   `json:"tenant_id,omitempty"`
	TenantName    string   `json:"tenant_name,omitempty"`
	PlatformHost  string   `json:"platform_host,omitempty"`
	CloudBaseURL  string   `json:"cloud_base_url,omitempty"`
	EdgeBaseURL   string   `json:"edge_base_url,omitempty"`
	Sources       []Source `json:"sources"`
	Revision      int      `json:"revision,omitempty"`
}

// CurrentSchemaVersion 是内部配置格式版本。
const CurrentSchemaVersion = 1

// 常见错误。
var (
	ErrUnsupported   = errors.New("CONFIG_UNSUPPORTED")
	ErrInvalid       = errors.New("CONFIG_INVALID")
	ErrUnresolvedVar = errors.New("CONFIG_UNRESOLVED_VARIABLE")
	ErrForbiddenHost = errors.New("SOURCE_HOST_NOT_ALLOWED")
)

// Issue 是一条导入或校验问题，Severity 为 "error" 或 "warning"。
type Issue struct {
	Code     string `json:"code"`
	Path     string `json:"path,omitempty"`
	Message  string `json:"message"`
	Severity string `json:"severity"`
}

// ImportReport 是配置导入的结果。
type ImportReport struct {
	Sources []Source `json:"sources"`
	// Secrets 以 source_id 为键，保存需要写入本机或云端秘密库的请求头原文。
	// 该字段不写入磁盘的普通配置，也不进入日志与导出文件。
	Secrets map[string]map[string]string `json:"-"`
	Issues  []Issue                      `json:"issues"`
	Format  string                       `json:"format"`
}

// HasErrors 表示导入结果中存在阻断项。
func (r ImportReport) HasErrors() bool {
	for _, issue := range r.Issues {
		if issue.Severity == "error" {
			return true
		}
	}
	return false
}

// Errors 返回所有阻断项，便于界面逐条展示。
func (r ImportReport) Errors() []Issue {
	var out []Issue
	for _, issue := range r.Issues {
		if issue.Severity == "error" {
			out = append(out, issue)
		}
	}
	return out
}
