package agent

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"sort"
	"strings"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/config"
	"github.com/sherkevin/open-claude-code/connector/internal/huaweiauth"
	"github.com/sherkevin/open-claude-code/connector/internal/mcp"
	"github.com/sherkevin/open-claude-code/connector/internal/state"
)

// SourceInput is the non-JSON local onboarding form for one MCP source. Empty
// secret fields on an existing source mean "keep the stored value".
type SourceInput struct {
	SourceID    string   `json:"source_id,omitempty"`
	DisplayName string   `json:"display_name"`
	UpstreamURL string   `json:"upstream_url"`
	BearerToken string   `json:"bearer_token,omitempty"`
	DynamicAuth bool     `json:"dynamic_auth"`
	AppID       string   `json:"app_id,omitempty"`
	Channel     string   `json:"channel,omitempty"`
	UserID      string   `json:"user_id,omitempty"`
	HMACKey     string   `json:"hmac_key,omitempty"`
	Tools       []string `json:"tools,omitempty"`
}

// SourceSaveResult contains only non-secret source metadata.
type SourceSaveResult struct {
	Created bool          `json:"created"`
	Source  config.Source `json:"source"`
	Status  Status        `json:"status"`
}

// ToolSummary is the safe subset returned to the local configuration page.
type ToolSummary struct {
	Name        string `json:"name"`
	Title       string `json:"title,omitempty"`
	Description string `json:"description,omitempty"`
}

type preparedSource struct {
	source     config.Source
	secret     map[string]string
	profile    huaweiauth.Profile
	hasProfile bool
	index      int
	created    bool
}

// SaveSource creates or updates exactly one source. Unlike the advanced
// whole-import path, it never replaces other sources or their secrets.
func (a *Agent) SaveSource(input SourceInput) (SourceSaveResult, error) {
	a.mu.Lock()
	switch a.state.Current() {
	case state.Ready, state.Connecting, state.DegradedRelay, state.DegradedUpstream:
		a.mu.Unlock()
		return SourceSaveResult{}, errors.New("请先暂停连接，再修改 MCP 服务配置")
	}

	prepared, err := a.prepareSourceLocked(input)
	if err != nil {
		a.mu.Unlock()
		return SourceSaveResult{}, err
	}
	if len(prepared.source.ToolAllowlist) == 0 {
		a.mu.Unlock()
		return SourceSaveResult{}, errors.New("请先读取工具列表，并至少勾选一个允许使用的只读工具")
	}

	nextConfig := a.config
	nextConfig.Revision++
	nextConfig.Sources = append([]config.Source(nil), a.config.Sources...)
	if prepared.created {
		if len(nextConfig.Sources) >= 16 {
			a.mu.Unlock()
			return SourceSaveResult{}, errors.New("单个连接器最多允许 16 个来源")
		}
		nextConfig.Sources = append(nextConfig.Sources, prepared.source)
	} else {
		nextConfig.Sources[prepared.index] = prepared.source
	}

	nextSecrets := cloneSourceSecrets(a.secrets)
	if len(prepared.secret) == 0 {
		delete(nextSecrets, prepared.source.SourceID)
	} else {
		nextSecrets[prepared.source.SourceID] = cloneHeaders(prepared.secret)
	}

	nextProfiles := cloneProfiles(a.profiles)
	if prepared.hasProfile {
		nextProfiles[prepared.source.SourceID] = prepared.profile
	} else {
		delete(nextProfiles, prepared.source.SourceID)
	}

	if err := a.store.WriteJSON(fileConfig, nextConfig); err != nil {
		a.mu.Unlock()
		return SourceSaveResult{}, err
	}
	if err := a.store.WriteProtectedJSON(fileSecrets, nextSecrets); err != nil {
		a.mu.Unlock()
		return SourceSaveResult{}, err
	}
	if err := a.store.WriteProtectedJSON(fileAuth, nextProfiles); err != nil {
		a.mu.Unlock()
		return SourceSaveResult{}, err
	}

	a.config = nextConfig
	a.secrets = nextSecrets
	a.profiles = nextProfiles
	status := a.statusLocked()
	a.mu.Unlock()

	if status.State == state.Unpaired {
		a.state.Transition(state.Configuring, "已保存 MCP 服务配置")
		status = a.Status()
	}
	return SourceSaveResult{Created: prepared.created, Source: prepared.source, Status: status}, nil
}

// DiscoverSourceTools validates the current form values and performs the same
// MCP initialize + tools/list calls used by diagnosis. It does not persist the
// draft or call any business tool.
func (a *Agent) DiscoverSourceTools(ctx context.Context, input SourceInput) ([]ToolSummary, error) {
	a.mu.RLock()
	prepared, err := a.prepareSourceLocked(input)
	a.mu.RUnlock()
	if err != nil {
		return nil, err
	}

	client := &mcp.Client{
		Endpoint: prepared.source.UpstreamURL,
		Headers:  cloneHeaders(prepared.secret),
		HTTP:     &http.Client{Timeout: 15 * time.Second},
	}
	if prepared.hasProfile {
		profile := prepared.profile
		client.HeaderProvider = func(method, _ string) (map[string]string, error) {
			if method != "tools/call" {
				return nil, nil
			}
			return huaweiauth.SignedHeaders(profile, time.Now(), nil)
		}
	}

	discoveryCtx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	if _, err := client.Initialize(discoveryCtx); err != nil {
		return nil, fmt.Errorf("MCP 初始化失败，请检查 MCP 地址和访问令牌: %w", err)
	}
	tools, err := client.ListTools(discoveryCtx)
	if err != nil {
		return nil, fmt.Errorf("工具列表读取失败，请检查访问令牌和动态签名配置: %w", err)
	}
	if len(tools) > 256 {
		return nil, errors.New("工具数量超过 256 个，请联系开物管理员调整来源")
	}

	out := make([]ToolSummary, 0, len(tools))
	seen := map[string]bool{}
	for _, tool := range tools {
		name := strings.TrimSpace(tool.Name)
		if name == "" || strings.ContainsAny(name, "\r\n,* \t") {
			return nil, fmt.Errorf("上游返回了无效工具名称: %q", tool.Name)
		}
		if seen[name] {
			return nil, fmt.Errorf("上游返回了重复工具名称: %s", name)
		}
		seen[name] = true
		out = append(out, ToolSummary{Name: name, Title: strings.TrimSpace(tool.Title), Description: strings.TrimSpace(tool.Description)})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out, nil
}

// prepareSourceLocked expects a.mu to be held. It validates all input through
// the existing host-config parser and merges blank secrets from the selected
// existing source without touching any other source.
func (a *Agent) prepareSourceLocked(input SourceInput) (preparedSource, error) {
	displayName := strings.TrimSpace(input.DisplayName)
	upstreamURL := strings.TrimSpace(input.UpstreamURL)
	index := -1
	var existing config.Source
	if sourceID := strings.TrimSpace(input.SourceID); sourceID != "" {
		for i := range a.config.Sources {
			if a.config.Sources[i].SourceID == sourceID {
				index = i
				existing = a.config.Sources[i]
				break
			}
		}
		if index < 0 {
			return preparedSource{}, errors.New("要编辑的 MCP 服务不存在，请刷新页面后重试")
		}
		if displayName == "" {
			displayName = existing.DisplayName
		}
		if upstreamURL == "" {
			upstreamURL = existing.UpstreamURL
		}
	}
	if displayName == "" {
		displayName = "MCP 服务"
	}
	if upstreamURL == "" {
		return preparedSource{}, errors.New("请填写 MCP HTTP 地址")
	}

	existingSecret := map[string]string{}
	if index >= 0 {
		existingSecret = cloneHeaders(a.secrets[existing.SourceID])
	}
	authorization := strings.TrimSpace(existingSecret["Authorization"])
	if strings.TrimSpace(input.BearerToken) != "" {
		value, err := normalizeAuthorization(input.BearerToken)
		if err != nil {
			return preparedSource{}, err
		}
		authorization = value
	}

	entry := map[string]any{"type": "http", "url": upstreamURL}
	if authorization != "" {
		entry["headers"] = map[string]string{"Authorization": authorization}
	}
	raw, err := encodeSingleSource(displayName, entry)
	if err != nil {
		return preparedSource{}, err
	}
	report, err := config.ParseHostConfig(raw, config.ImportOptions{AllowStdio: a.opts.AllowStdio})
	if err != nil {
		return preparedSource{}, err
	}
	if len(report.Sources) != 1 {
		return preparedSource{}, errors.New("MCP 服务配置没有生成唯一来源")
	}
	if report.HasErrors() {
		return preparedSource{}, errors.New(formatSourceIssues(report.Errors()))
	}

	source := report.Sources[0]
	if index >= 0 {
		source.SourceID = existing.SourceID
	} else {
		source.SourceID, err = newSourceID()
		if err != nil {
			return preparedSource{}, err
		}
	}
	source.DisplayName = displayName
	source.SchemaVersion = config.CurrentSchemaVersion
	source.Transport = config.TransportStreamableHTTP
	source.Limits = existing.Limits
	allowedTools, err := normalizeTools(input.Tools)
	if err != nil {
		return preparedSource{}, err
	}
	source.ToolAllowlist = allowedTools
	if authorization == "" {
		source.Auth = config.Auth{Mode: config.AuthModeNone, CredentialLocation: config.CredentialLocationLocal}
		source.HeaderNames = nil
	} else {
		source.Auth = config.Auth{
			Mode:               config.AuthModeFixedHeader,
			CredentialLocation: config.CredentialLocationLocal,
			CredentialRef:      "cred_" + source.SourceID,
		}
		source.HeaderNames = []string{"Authorization"}
	}

	prepared := preparedSource{
		source:  source,
		secret:  cloneHeaders(report.Secrets[report.Sources[0].SourceID]),
		index:   index,
		created: index < 0,
	}
	if authorization != "" {
		prepared.secret["Authorization"] = authorization
	}

	if input.DynamicAuth {
		profile := huaweiauth.Profile{}
		if index >= 0 {
			profile = a.profiles[existing.SourceID]
		}
		if value := strings.TrimSpace(input.AppID); value != "" {
			profile.AppID = value
		}
		if value := strings.TrimSpace(input.Channel); value != "" {
			profile.Channel = value
		}
		if value := strings.TrimSpace(input.UserID); value != "" {
			profile.UserID = value
		}
		if profile.AppID == "" {
			profile.AppID = "MCP"
		}
		if input.HMACKey != "" {
			profile.HMACKey = input.HMACKey
		}
		if err := huaweiauth.Validate(profile); err != nil {
			return preparedSource{}, err
		}
		prepared.profile = profile
		prepared.hasProfile = true
	}
	return prepared, nil
}

func normalizeAuthorization(value string) (string, error) {
	value = strings.TrimSpace(value)
	if value == "" {
		return "", nil
	}
	if strings.ContainsAny(value, "\r\n") {
		return "", errors.New("访问令牌不能包含换行符")
	}
	if len(value) > 8192 {
		return "", errors.New("访问令牌超过本机允许长度")
	}
	if !strings.ContainsAny(value, " \t") {
		return "Bearer " + value, nil
	}
	return value, nil
}

func encodeSingleSource(name string, entry map[string]any) ([]byte, error) {
	return jsonMarshalNoEscape(map[string]any{
		"mcpServers": map[string]any{name: entry},
	})
}

func jsonMarshalNoEscape(value any) ([]byte, error) {
	// json.RawMessage through the standard encoder is sufficient here; the
	// helper keeps the call site readable and avoids hand-built JSON strings.
	return json.Marshal(value)
}

func formatSourceIssues(issues []config.Issue) string {
	parts := make([]string, 0, len(issues))
	for _, issue := range issues {
		message := strings.TrimSpace(issue.Message)
		if issue.Path != "" {
			message = issue.Path + "：" + message
		}
		parts = append(parts, message)
	}
	if len(parts) == 0 {
		return "MCP 服务配置无效"
	}
	return strings.Join(parts, "；")
}

func cloneSourceSecrets(input map[string]map[string]string) map[string]map[string]string {
	out := make(map[string]map[string]string, len(input))
	for sourceID, headers := range input {
		out[sourceID] = cloneHeaders(headers)
	}
	return out
}

func cloneProfiles(input map[string]huaweiauth.Profile) map[string]huaweiauth.Profile {
	out := make(map[string]huaweiauth.Profile, len(input))
	for sourceID, profile := range input {
		out[sourceID] = profile
	}
	return out
}
