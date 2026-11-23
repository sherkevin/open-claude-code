package agent

import (
	"context"
	"encoding/json"
	"errors"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/cloud"
	"github.com/sherkevin/open-claude-code/connector/internal/config"
	"github.com/sherkevin/open-claude-code/connector/internal/diagnostics"
)

const defaultPlatformURL = "https://flintw.com/project/kaiwu/"

var platformHandoffCode = regexp.MustCompile(`^kw_link_[A-Za-z0-9_-]{43}$`)

// CreatePlatformHandoff only returns a short-lived link. The platform decides
// which signed-in administrator can use it and whether to show the MCP JSON.
func (a *Agent) CreatePlatformHandoff(ctx context.Context, sourceID string) (string, error) {
	client, id, err := a.platformBusinessClient(sourceID)
	if err != nil {
		return "", err
	}
	link, err := a.platformURL()
	if err != nil {
		return "", err
	}
	code, err := client.CreatePlatformHandoff(ctx, id, sourceID)
	if err != nil {
		return "", err
	}
	if !platformHandoffCode.MatchString(code) {
		return "", errors.New("开物返回的导入链接无效")
	}
	link.Fragment = "kaiwu-mcp=" + code
	return link.String(), nil
}

type PlatformMCPExport struct {
	Config      json.RawMessage `json:"mcp_config"`
	PlatformURL string          `json:"platform_url"`
}

// ExportPlatformMCP provides the local operator with a pasteable configuration
// for one connected business source. The cloud authorizes and issues the key.
func (a *Agent) ExportPlatformMCP(ctx context.Context, sourceID string) (_ PlatformMCPExport, err error) {
	started := time.Now()
	operationID := diagnostics.NewOperationID()
	defer func() {
		code := "platform_export_succeeded"
		if err != nil {
			code = "platform_export_failed"
		}
		a.recordOperationDiagnostic(code, operationID, sourceID, started, err)
	}()
	client, id, err := a.platformBusinessClient(sourceID)
	if err != nil {
		return PlatformMCPExport{}, err
	}
	link, err := a.platformURL()
	if err != nil {
		return PlatformMCPExport{}, err
	}
	raw, err := client.ExportPlatformMCP(ctx, id, sourceID)
	if err != nil {
		return PlatformMCPExport{}, err
	}
	var config struct {
		Servers map[string]json.RawMessage `json:"mcpServers"`
	}
	if err := json.Unmarshal(raw, &config); err != nil || len(config.Servers) != 1 {
		return PlatformMCPExport{}, errors.New("开物返回的 MCP 配置无效")
	}
	return PlatformMCPExport{Config: raw, PlatformURL: link.String()}, nil
}

func (a *Agent) platformBusinessClient(sourceID string) (*cloud.Client, string, error) {
	status := a.Status()
	if !a.opts.NativeRelay || !status.TunnelConnected {
		return nil, "", errors.New("请先建立开物连接")
	}
	var allowed bool
	for _, source := range status.Sources {
		if source.SourceID == sourceID && source.SourceID != status.BuiltinDemo.SourceID &&
			!source.Disabled && source.Transport == config.TransportStreamableHTTP {
			allowed = true
			break
		}
	}
	if !allowed {
		return nil, "", errors.New("请选择正在连接的业务 MCP")
	}
	a.mu.RLock()
	client, currentID := a.cloud, a.identity.ConnectorID
	a.mu.RUnlock()
	if client == nil || currentID != status.ConnectorID || currentID == "" {
		return nil, "", errors.New("请先绑定企业")
	}
	return client, currentID, nil
}

func (a *Agent) platformURL() (*url.URL, error) {
	base := strings.TrimSpace(a.opts.PlatformURL)
	if base == "" {
		base = defaultPlatformURL
	}
	link, err := url.Parse(base)
	if err != nil || link.Scheme != "https" || link.Hostname() == "" || link.User != nil || link.RawQuery != "" ||
		link.ForceQuery || link.Fragment != "" || strings.Contains(base, "#") {
		return nil, errors.New("开物网页地址配置无效")
	}
	return link, nil
}
