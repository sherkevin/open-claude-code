package agent

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/authproxy"
	"github.com/sherkevin/open-claude-code/connector/internal/config"
	"github.com/sherkevin/open-claude-code/connector/internal/huaweiauth"
	"github.com/sherkevin/open-claude-code/connector/internal/state"
	"github.com/sherkevin/open-claude-code/connector/internal/version"
)

// SetAuthProfile stores the local-only dynamic authentication profile and the
// exact tools this source may call. The HMAC key is never returned by Status.
func (a *Agent) SetAuthProfile(sourceID string, profile huaweiauth.Profile, tools []string) (Status, error) {
	sourceID = strings.TrimSpace(sourceID)
	if sourceID == "" {
		return a.Status(), errors.New("请选择来源")
	}
	if err := huaweiauth.Validate(profile); err != nil {
		return a.Status(), err
	}
	allowed, err := normalizeTools(tools)
	if err != nil {
		return a.Status(), err
	}
	if len(allowed) == 0 {
		return a.Status(), errors.New("至少允许一个只读工具后才能建立动态认证")
	}

	a.mu.Lock()
	switch a.state.Current() {
	case state.Ready, state.Connecting, state.DegradedRelay, state.DegradedUpstream:
		a.mu.Unlock()
		return a.Status(), errors.New("请先暂停连接，再修改本机认证配置")
	}
	index := -1
	for i := range a.config.Sources {
		if a.config.Sources[i].SourceID == sourceID {
			index = i
			break
		}
	}
	if index < 0 {
		a.mu.Unlock()
		return a.Status(), errors.New("来源不存在，请重新导入 MCP 配置")
	}
	if strings.TrimSpace(a.secrets[sourceID]["Authorization"]) == "" {
		a.mu.Unlock()
		return a.Status(), errors.New("请先在 MCP 配置中导入 Authorization 固定 Bearer")
	}

	a.config.Sources[index].ToolAllowlist = allowed
	a.profiles[sourceID] = profile
	if err := a.store.WriteJSON(fileConfig, a.config); err != nil {
		a.mu.Unlock()
		return a.Status(), err
	}
	if err := a.store.WriteJSON(fileAuth, a.profiles); err != nil {
		a.mu.Unlock()
		return a.Status(), err
	}
	status := a.statusLocked()
	a.mu.Unlock()
	return status, nil
}

func normalizeTools(tools []string) ([]string, error) {
	seen := map[string]bool{}
	out := make([]string, 0, len(tools))
	for _, item := range tools {
		item = strings.TrimSpace(item)
		if item == "" {
			continue
		}
		if len(item) > 128 || strings.ContainsAny(item, "\r\n, ") {
			return nil, fmt.Errorf("工具名称格式无效: %s", item)
		}
		if !seen[item] {
			seen[item] = true
			out = append(out, item)
		}
	}
	sort.Strings(out)
	return out, nil
}

func (a *Agent) startAuthProxies() error {
	a.stopAuthProxies()

	a.mu.RLock()
	sourceList := append([]config.Source(nil), a.config.Sources...)
	secrets := make(map[string]map[string]string, len(a.secrets))
	for sourceID, headers := range a.secrets {
		secrets[sourceID] = cloneHeaders(headers)
	}
	profiles := make(map[string]huaweiauth.Profile, len(a.profiles))
	for sourceID, profile := range a.profiles {
		profiles[sourceID] = profile
	}
	a.mu.RUnlock()

	created := map[string]*authproxy.Server{}
	for _, source := range sourceList {
		options := authproxy.Options{
			SourceID:      source.SourceID,
			UpstreamURL:   source.UpstreamURL,
			Headers:       cloneHeaders(secrets[source.SourceID]),
			ToolAllowlist: append([]string(nil), source.ToolAllowlist...),
		}
		if profile, ok := profiles[source.SourceID]; ok {
			copy := profile
			options.Signing = &copy
		}
		proxy, err := authproxy.New(options)
		if err != nil {
			closeProxies(created)
			return fmt.Errorf("来源 %s: %w", source.DisplayName, err)
		}
		if err := proxy.Start(); err != nil {
			closeProxies(created)
			return fmt.Errorf("来源 %s: %w", source.DisplayName, err)
		}
		created[source.SourceID] = proxy
	}
	if len(created) == 0 {
		return errors.New("没有可启动的来源代理")
	}
	a.mu.Lock()
	a.proxies = created
	a.mu.Unlock()
	return nil
}

func (a *Agent) stopAuthProxies() {
	a.mu.Lock()
	proxies := a.proxies
	a.proxies = map[string]*authproxy.Server{}
	a.mu.Unlock()
	closeProxies(proxies)
}

func closeProxies(proxies map[string]*authproxy.Server) {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	for _, proxy := range proxies {
		_ = proxy.Close(ctx)
	}
}

func cloneHeaders(input map[string]string) map[string]string {
	if len(input) == 0 {
		return map[string]string{}
	}
	out := make(map[string]string, len(input))
	for key, value := range input {
		out[key] = value
	}
	return out
}

func (a *Agent) authStatusLocked() []SourceAuth {
	items := make([]SourceAuth, 0, len(a.config.Sources))
	for _, source := range a.config.Sources {
		_, configured := a.profiles[source.SourceID]
		allowed := append([]string(nil), source.ToolAllowlist...)
		sort.Strings(allowed)
		items = append(items, SourceAuth{
			SourceID:     source.SourceID,
			Configured:   configured,
			AllowedTools: allowed,
		})
	}
	return items
}

func (a *Agent) statusLocked() Status {
	status := Status{
		State:        a.state.Current(),
		StateLabel:   a.state.Current().Label(),
		Reason:       a.state.Reason(),
		Bound:        a.identity.DeviceToken != "",
		ConnectorID:  a.identity.ConnectorID,
		TenantName:   a.identity.TenantName,
		TenantID:     a.identity.TenantID,
		Sources:      append([]config.Source(nil), a.config.Sources...),
		LastError:    a.lastError,
		Version:      version.String(),
		EdgeBase:     a.edgeBase(),
		AuthProfiles: a.authStatusLocked(),
		UpdatedAt:    time.Now().UTC(),
	}
	if a.supervisor != nil {
		status.FrpcRunning = a.supervisor.Running()
		status.FrpcRestarts = a.supervisor.Restarts()
	}
	return status
}
