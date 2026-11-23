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
	"github.com/sherkevin/open-claude-code/connector/internal/diagnostics"
	"github.com/sherkevin/open-claude-code/connector/internal/huaweiauth"
	"github.com/sherkevin/open-claude-code/connector/internal/state"
	"github.com/sherkevin/open-claude-code/connector/internal/version"
)

// SetAuthProfile stores the local-only dynamic authentication profile and the
// exact tools this source may call. The HMAC key is never returned by Status.
func (a *Agent) SetAuthProfile(sourceID string, profile huaweiauth.Profile, tools []string) (status Status, err error) {
	sourceID = strings.TrimSpace(sourceID)
	started := time.Now()
	operationID := diagnostics.NewOperationID()
	defer func() {
		code := "auth_profile_saved"
		if err != nil {
			code = "auth_profile_failed"
		}
		a.recordDiagnostic(diagnostics.Record{
			EventCode:   code,
			OperationID: operationID,
			SourceID:    sourceID,
			DurationMS:  diagnosticDuration(started),
		})
	}()
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

	a.connectMu.Lock()
	defer a.connectMu.Unlock()
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
	if err := a.store.WriteProtectedJSON(fileAuth, a.profiles); err != nil {
		a.mu.Unlock()
		return a.Status(), err
	}
	status = a.statusLocked()
	a.mu.Unlock()
	a.syncDiagnosticApprovals()
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
		if len(item) > 128 || strings.ContainsAny(item, "\r\n,* \t") {
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

// prepareAuthProxies leaves the current tunnel's listeners in service while
// the candidate routes are validated with the control plane.
func (a *Agent) prepareAuthProxies(sourceList []config.Source) (map[string]*authproxy.Server, error) {
	a.lifecycleMu.Lock()
	defer a.lifecycleMu.Unlock()
	if a.isClosed() {
		return nil, errAgentClosed
	}
	a.mu.RLock()
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
	failures := make([]string, 0)
	for _, source := range sourceList {
		if a.isClosed() {
			closeProxies(created)
			return nil, errAgentClosed
		}
		options := authproxy.Options{
			SourceID:      source.SourceID,
			UpstreamURL:   source.UpstreamURL,
			Headers:       cloneHeaders(secrets[source.SourceID]),
			ToolAllowlist: append([]string(nil), source.ToolAllowlist...),
			OnMCPCall:     a.reportAuthProxyCall,
		}
		if profile, ok := profiles[source.SourceID]; ok {
			copy := profile
			options.Signing = &copy
		}
		proxy, err := authproxy.New(options)
		if err != nil {
			failures = append(failures, fmt.Sprintf("%s: %v", source.DisplayName, err))
			continue
		}
		if err := proxy.Start(); err != nil {
			failures = append(failures, fmt.Sprintf("%s: %v", source.DisplayName, err))
			continue
		}
		created[source.SourceID] = proxy
	}
	if len(created) == 0 {
		if len(failures) > 0 {
			return nil, fmt.Errorf("没有可启动的来源代理: %s", strings.Join(failures, "；"))
		}
		return nil, errors.New("没有可启动的来源代理")
	}
	a.mu.Lock()
	a.preparedProxies = created
	a.mu.Unlock()
	return created, nil
}

func (a *Agent) stopPreparedAuthProxies() {
	a.mu.Lock()
	proxies := a.preparedProxies
	a.preparedProxies = nil
	a.mu.Unlock()
	closeProxies(proxies)
}

// startAuthProxiesForDemo touches only the bundled demo proxy. If that listener
// already exists it updates only its upstream target; customer proxies and
// tunnel local ports remain untouched.
func (a *Agent) startAuthProxiesForDemo() (map[string]*authproxy.Server, error) {
	a.lifecycleMu.Lock()
	defer a.lifecycleMu.Unlock()
	if a.isClosed() {
		return nil, errAgentClosed
	}
	a.mu.RLock()
	demoID := a.demoState.SourceID
	var demoSource config.Source
	foundDemo := false
	for _, source := range a.runtimeSourcesLocked() {
		if source.SourceID == demoID {
			demoSource = source
			foundDemo = true
			break
		}
	}
	secrets := make(map[string]map[string]string, len(a.secrets))
	for sourceID, headers := range a.secrets {
		secrets[sourceID] = cloneHeaders(headers)
	}
	profiles := make(map[string]huaweiauth.Profile, len(a.profiles))
	for sourceID, profile := range a.profiles {
		profiles[sourceID] = profile
	}
	existing := a.proxies[demoID]
	a.mu.RUnlock()
	if !foundDemo {
		return nil, errors.New("内置测试来源尚未启用")
	}

	if existing != nil {
		if _, _, err := existing.Endpoint(); err != nil {
			return nil, fmt.Errorf("内置测试来源代理监听不可用: %w", err)
		}
		if existing.UpstreamURL() != demoSource.UpstreamURL {
			if err := existing.UpdateUpstream(demoSource.UpstreamURL); err != nil {
				return nil, fmt.Errorf("更新内置测试来源代理上游失败: %w", err)
			}
		}
		return nil, nil
	}

	profile, hasProfile := profiles[demoID]
	proxy, err := newSourceAuthProxy(demoSource, secrets[demoID], profile, hasProfile, a.reportAuthProxyCall)
	if err != nil {
		return nil, fmt.Errorf("内置测试来源代理未启动: %w", err)
	}
	if err := proxy.Start(); err != nil {
		return nil, fmt.Errorf("内置测试来源代理未启动: %w", err)
	}
	created := map[string]*authproxy.Server{demoID: proxy}

	a.mu.Lock()
	if a.closed {
		a.mu.Unlock()
		closeProxies(created)
		return nil, errAgentClosed
	}
	if current := a.proxies[demoID]; current != nil {
		a.mu.Unlock()
		closeProxies(created)
		if current.UpstreamURL() != demoSource.UpstreamURL {
			if err := current.UpdateUpstream(demoSource.UpstreamURL); err != nil {
				return nil, fmt.Errorf("更新内置测试来源代理上游失败: %w", err)
			}
		}
		return nil, nil
	}
	merged := make(map[string]*authproxy.Server, len(a.proxies)+1)
	for sourceID, proxy := range a.proxies {
		merged[sourceID] = proxy
	}
	merged[demoID] = proxy
	a.proxies = merged
	a.mu.Unlock()
	return created, nil
}

func newSourceAuthProxy(source config.Source, headers map[string]string, profile huaweiauth.Profile, hasProfile bool, observer func(authproxy.Observation)) (*authproxy.Server, error) {
	options := authproxy.Options{
		SourceID:      source.SourceID,
		UpstreamURL:   source.UpstreamURL,
		Headers:       cloneHeaders(headers),
		ToolAllowlist: append([]string(nil), source.ToolAllowlist...),
		OnMCPCall:     observer,
	}
	if hasProfile {
		copy := profile
		options.Signing = &copy
	}
	return authproxy.New(options)
}

func (a *Agent) stopNewAuthProxies(proxies map[string]*authproxy.Server) {
	if len(proxies) == 0 {
		return
	}
	a.mu.Lock()
	for sourceID, proxy := range proxies {
		if a.proxies[sourceID] == proxy {
			delete(a.proxies, sourceID)
		}
	}
	a.mu.Unlock()
	closeProxies(proxies)
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
		if a.demoState.SourceID != "" && source.SourceID == a.demoState.SourceID {
			continue
		}
		profile, hasProfile := a.profiles[source.SourceID]
		allowed := append([]string(nil), source.ToolAllowlist...)
		sort.Strings(allowed)
		items = append(items, SourceAuth{
			SourceID:     source.SourceID,
			Configured:   hasProfile && len(allowed) > 0,
			AllowedTools: allowed,
			AppID:        profile.AppID,
			Channel:      profile.Channel,
			UserID:       profile.UserID,
		})
	}
	return items
}

func (a *Agent) statusLocked() Status {
	status := Status{
		State:           a.state.Current(),
		StateLabel:      a.state.Current().Label(),
		Reason:          a.state.Reason(),
		Bound:           a.identity.DeviceToken != "",
		CloudConfigured: endpointConfigured(a.cloudBase()),
		EdgeConfigured:  endpointConfigured(a.edgeBase()),
		ConnectorID:     a.identity.ConnectorID,
		TenantName:      a.identity.TenantName,
		TenantID:        a.identity.TenantID,
		Sources:         a.businessSourcesLocked(),
		TransportMode:   "frp",
		LastError:       a.lastError,
		Version:         version.String(),
		EdgeBase:        a.edgeBase(),
		AuthProfiles:    a.authStatusLocked(),
		Diagnostics:     a.diagnosticStatsLocked(),
		BuiltinDemo:     a.builtinDemoStatusLocked(),
		UpdatedAt:       time.Now().UTC(),
	}
	if a.supervisor != nil {
		status.FrpcRunning = a.supervisor.Running()
		status.FrpcRestarts = a.supervisor.Restarts()
		status.TunnelRestarts = status.FrpcRestarts
	}
	if a.opts.NativeRelay {
		status.TransportMode = "native"
		if a.native != nil {
			status.TunnelConnected = a.native.connected
			status.TunnelRestarts = a.native.restarts
			if status.TunnelConnected {
				status.TunnelDetail = "原生中继已连接"
			} else {
				status.TunnelDetail = "原生中继正在重连"
			}
		}
	}
	return status
}
