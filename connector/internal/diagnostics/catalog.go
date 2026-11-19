package diagnostics

// EventSpec is a fixed event vocabulary entry. Message is the only text shown
// to customers for this event; callers cannot provide arbitrary strings.
type EventSpec struct {
	Level     Level
	Component Component
	Message   string
}

// Catalog is intentionally explicit. Adding an event requires code review and
// a corresponding allow-list update on the cloud receiver.
var Catalog = map[string]EventSpec{
	"connector_started":            {LevelInfo, ComponentConnector, "连接器已启动"},
	"connector_stopped":            {LevelInfo, ComponentConnector, "连接器已停止"},
	"version_loaded":               {LevelInfo, ComponentConnector, "连接器版本已加载"},
	"enrollment_succeeded":         {LevelInfo, ComponentConnector, "企业绑定成功"},
	"enrollment_failed":            {LevelError, ComponentConnector, "企业绑定失败"},
	"config_imported":              {LevelInfo, ComponentConfig, "MCP 配置已导入"},
	"config_import_failed":         {LevelError, ComponentConfig, "MCP 配置导入失败"},
	"source_saved":                 {LevelInfo, ComponentSource, "MCP 服务配置已保存"},
	"source_save_failed":           {LevelError, ComponentSource, "MCP 服务配置保存失败"},
	"source_tools_loaded":          {LevelInfo, ComponentSource, "MCP 工具目录读取成功"},
	"source_tools_failed":          {LevelError, ComponentSource, "MCP 工具目录读取失败"},
	"source_enabled":               {LevelInfo, ComponentSource, "MCP 服务已启用"},
	"source_disabled":              {LevelWarn, ComponentSource, "MCP 服务已停用"},
	"source_toggle_failed":         {LevelError, ComponentSource, "MCP 服务启停失败"},
	"auth_profile_saved":           {LevelInfo, ComponentAuth, "本机动态认证配置已保存"},
	"auth_profile_failed":          {LevelError, ComponentAuth, "本机动态认证配置保存失败"},
	"diagnosis_started":            {LevelInfo, ComponentDiagnosis, "开始检查 MCP 服务"},
	"diagnosis_succeeded":          {LevelInfo, ComponentDiagnosis, "MCP 服务检查通过"},
	"diagnosis_failed":             {LevelError, ComponentDiagnosis, "MCP 服务检查未通过"},
	"connection_started":           {LevelInfo, ComponentConnector, "开始建立公网连接"},
	"connection_succeeded":         {LevelInfo, ComponentConnector, "公网连接已就绪"},
	"connection_failed":            {LevelError, ComponentConnector, "公网连接建立失败"},
	"connection_paused":            {LevelWarn, ComponentConnector, "公网连接已暂停"},
	"connection_disconnected":      {LevelWarn, ComponentConnector, "公网连接已断开"},
	"connection_revoked":           {LevelWarn, ComponentConnector, "本机设备已撤销"},
	"frp_started":                  {LevelInfo, ComponentTunnel, "公网隧道进程已启动"},
	"frp_stopped":                  {LevelError, ComponentTunnel, "公网隧道进程已停止"},
	"frp_retry":                    {LevelWarn, ComponentTunnel, "公网隧道正在重试"},
	"frp_recovered":                {LevelInfo, ComponentTunnel, "公网隧道已恢复"},
	"mcp_request_started":          {LevelInfo, ComponentMCP, "收到 MCP 调用"},
	"mcp_request_succeeded":        {LevelInfo, ComponentMCP, "MCP 调用成功"},
	"mcp_request_failed":           {LevelError, ComponentMCP, "MCP 调用失败"},
	"mcp_tool_succeeded":           {LevelInfo, ComponentMCP, "工具调用成功"},
	"mcp_tool_failed":              {LevelError, ComponentMCP, "工具调用失败"},
	"platform_request_succeeded":   {LevelInfo, ComponentPlatform, "开物平台调用已到达并成功"},
	"platform_request_failed":      {LevelError, ComponentPlatform, "开物平台调用已到达但未成功"},
	"diagnostics_paused":           {LevelWarn, ComponentDiagnostics, "诊断上报已暂停"},
	"diagnostics_resumed":          {LevelInfo, ComponentDiagnostics, "诊断上报已恢复"},
	"diagnostics_upload_recovered": {LevelInfo, ComponentDiagnostics, "诊断上报已恢复连接"},
	"operation_failed":             {LevelError, ComponentConnector, "操作失败，请提供事件编号"},
}
