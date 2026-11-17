// Package agent 把配置、凭据、诊断、通道和状态串联成连接器的运行主体。
package agent

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"net"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/authproxy"
	"github.com/sherkevin/open-claude-code/connector/internal/cloud"
	"github.com/sherkevin/open-claude-code/connector/internal/config"
	"github.com/sherkevin/open-claude-code/connector/internal/diag"
	"github.com/sherkevin/open-claude-code/connector/internal/frpc"
	"github.com/sherkevin/open-claude-code/connector/internal/huaweiauth"
	"github.com/sherkevin/open-claude-code/connector/internal/state"
	"github.com/sherkevin/open-claude-code/connector/internal/store"
	"github.com/sherkevin/open-claude-code/connector/internal/supervisor"
	"github.com/sherkevin/open-claude-code/connector/internal/version"
)

// 本机文件名。
const (
	fileIdentity = "identity.json"
	fileConfig   = "config.json"
	fileSecrets  = "secrets.json"
	fileAuth     = "auth_profiles.json"
	fileFrpc     = "frpc.toml"
	fileState    = "state.json"
)

// Options 是连接器启动参数。
type Options struct {
	DataDir    string
	CloudBase  string
	EdgeBase   string
	FrpcBinary string
	ListenAddr string
	AllowStdio bool
}

// Agent 是连接器运行主体。
type Agent struct {
	opts  Options
	store *store.Store
	state *state.Machine
	cloud *cloud.Client

	mu        sync.RWMutex
	identity  cloud.Identity
	config    config.Connector
	secrets   map[string]map[string]string
	profiles  map[string]huaweiauth.Profile
	lastError string

	supervisor *supervisor.Supervisor
	proxies    map[string]*authproxy.Server
	heartbeat  context.CancelFunc

	persistMu   sync.Mutex
	autoConnect bool
}

// persistedState 记录上一次稳定状态，供服务重启后判断是否需要自动恢复通道。
type persistedState struct {
	State state.State `json:"state"`
}

// Status 是给本机界面和心跳使用的快照。
type Status struct {
	State        state.State     `json:"state"`
	StateLabel   string          `json:"state_label"`
	Reason       string          `json:"reason,omitempty"`
	Bound        bool            `json:"bound"`
	ConnectorID  string          `json:"connector_id,omitempty"`
	TenantName   string          `json:"tenant_name,omitempty"`
	TenantID     string          `json:"tenant_id,omitempty"`
	Sources      []config.Source `json:"sources"`
	FrpcRunning  bool            `json:"frpc_running"`
	FrpcRestarts int             `json:"frpc_restarts"`
	LastError    string          `json:"last_error,omitempty"`
	Version      string          `json:"version"`
	EdgeBase     string          `json:"edge_base,omitempty"`
	AuthProfiles []SourceAuth    `json:"auth_profiles"`
	UpdatedAt    time.Time       `json:"updated_at"`
}

// SourceAuth is a non-secret view of the local dynamic authentication state.
type SourceAuth struct {
	SourceID     string   `json:"source_id"`
	Configured   bool     `json:"configured"`
	AllowedTools []string `json:"allowed_tools"`
}

// New 载入本机状态并返回连接器主体。
func New(opts Options) (*Agent, error) {
	if opts.CloudBase == "" {
		opts.CloudBase = "https://api.example.com"
	}
	st, err := store.New(opts.DataDir)
	if err != nil {
		return nil, err
	}
	agent := &Agent{
		opts:     opts,
		store:    st,
		state:    state.NewMachine(state.Unpaired),
		secrets:  map[string]map[string]string{},
		profiles: map[string]huaweiauth.Profile{},
		proxies:  map[string]*authproxy.Server{},
		config:   config.Connector{SchemaVersion: config.CurrentSchemaVersion},
	}
	agent.state.SetObserver(func(from, to state.State, reason string) {
		agent.mu.Lock()
		if to == state.Ready || to == state.Connecting {
			agent.lastError = ""
		}
		if reason != "" && (to == state.DegradedUpstream || to == state.DegradedRelay) {
			agent.lastError = reason
		}
		agent.mu.Unlock()
		agent.persistState(to)
	})

	// 先读上一次稳定状态，再载入身份；载入身份会触发一次状态迁移并写回文件。
	var saved persistedState
	if err := st.ReadJSON(fileState, &saved); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return nil, err
	}
	if err := st.ReadJSON(fileIdentity, &agent.identity); err == nil && agent.identity.DeviceToken != "" {
		agent.state.Transition(state.Configuring, "已载入设备身份")
	}
	if err := st.ReadJSON(fileConfig, &agent.config); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return nil, err
	}
	if err := st.ReadJSON(fileSecrets, &agent.secrets); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return nil, err
	}
	if err := st.ReadJSON(fileAuth, &agent.profiles); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return nil, err
	}
	if agent.secrets == nil {
		agent.secrets = map[string]map[string]string{}
	}
	if agent.profiles == nil {
		agent.profiles = map[string]huaweiauth.Profile{}
	}
	// 未绑定但已经导入来源时，重启后应回到“待配置”，而不是退回“尚未绑定”。
	if agent.identity.DeviceToken == "" && len(agent.config.Sources) > 0 && saved.State != state.Revoked {
		agent.state.Transition(state.Configuring, "已载入来源配置")
	}
	if agent.identity.DeviceToken != "" && len(agent.config.Sources) > 0 {
		switch saved.State {
		case state.Ready, state.Connecting, state.DegradedRelay, state.DegradedUpstream:
			agent.autoConnect = true
		}
	}
	if agent.identity.DeviceToken != "" {
		client, err := cloud.New(agent.cloudBase(), agent.identity.DeviceToken)
		if err != nil {
			return nil, err
		}
		agent.cloud = client
	}
	agent.reapStaleFrpc()
	return agent, nil
}

// reapStaleFrpc 清理上次进程异常退出后遗留的 frpc，避免新进程注册同名代理被拒绝。
// 只处理 PID 文件记录且进程名确认为 frpc 的进程，不误杀其他程序。
func (a *Agent) reapStaleFrpc() {
	raw, err := os.ReadFile(a.store.Path("frpc.pid"))
	if err != nil {
		return
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(raw)))
	if err != nil || pid <= 0 || pid == os.Getpid() || !processLooksLikeFrpc(pid) {
		_ = a.store.Remove("frpc.pid")
		return
	}
	if process, err := os.FindProcess(pid); err == nil {
		_ = process.Kill()
		time.Sleep(300 * time.Millisecond)
	}
	_ = a.store.Remove("frpc.pid")
}

func processLooksLikeFrpc(pid int) bool {
	switch runtime.GOOS {
	case "linux":
		raw, err := os.ReadFile(fmt.Sprintf("/proc/%d/comm", pid))
		return err == nil && strings.Contains(strings.ToLower(strings.TrimSpace(string(raw))), "frpc")
	case "windows":
		out, err := exec.Command("tasklist", "/FI", fmt.Sprintf("PID eq %d", pid), "/FO", "CSV", "/NH").Output()
		return err == nil && strings.Contains(strings.ToLower(string(out)), "frpc.exe")
	default:
		out, err := exec.Command("ps", "-p", strconv.Itoa(pid), "-o", "comm=").Output()
		return err == nil && strings.Contains(strings.ToLower(string(out)), "frpc")
	}
}

// ShouldAutoConnect 表示上次退出时通道处于工作状态，服务启动后应自动恢复。
func (a *Agent) ShouldAutoConnect() bool {
	return a.autoConnect
}

// AutoConnect 在服务启动后按上次状态恢复通道。
func (a *Agent) AutoConnect() {
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	_, _ = a.Connect(ctx)
}

// recoverTunnel 在 frpc 被守护进程重新拉起后，确认内网来源健康再恢复就绪状态。
// 只跟着 frpc 的启动事件走，避免把仍在失败的代理误报为可用。
func (a *Agent) recoverTunnel() {
	time.Sleep(1500 * time.Millisecond)
	if a.state.Current() != state.DegradedRelay {
		return
	}
	a.mu.RLock()
	s := a.supervisor
	a.mu.RUnlock()
	if s == nil || !s.Running() {
		return
	}
	if detail := strings.ToLower(s.LastError()); strings.Contains(detail, "error") || strings.Contains(detail, "failed") {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	reports, err := a.Diagnose(ctx, "", "")
	if err != nil || !allOK(reports) {
		return
	}
	if a.state.Transition(state.Ready, "通道已恢复") {
		a.startHeartbeat()
	}
}

// persistState 记录稳定状态，供下次启动判断是否需要自动恢复通道。
func (a *Agent) persistState(to state.State) {
	switch to {
	case state.Ready, state.Paused, state.Configuring, state.DegradedUpstream, state.DegradedRelay, state.Revoked:
	default:
		return
	}
	a.persistMu.Lock()
	defer a.persistMu.Unlock()
	_ = a.store.WriteJSON(fileState, persistedState{State: to})
}

func (a *Agent) cloudBase() string {
	if a.config.CloudBaseURL != "" {
		return a.config.CloudBaseURL
	}
	return a.opts.CloudBase
}

func (a *Agent) edgeBase() string {
	if a.config.EdgeBaseURL != "" {
		return a.config.EdgeBaseURL
	}
	return a.opts.EdgeBase
}

// Status 返回当前快照。
func (a *Agent) Status() Status {
	a.mu.RLock()
	defer a.mu.RUnlock()
	return a.statusLocked()
}

// Bind 用一次性接入码兑换设备身份。
func (a *Agent) Bind(ctx context.Context, code string) (Status, error) {
	code = strings.TrimSpace(code)
	if code == "" {
		return a.Status(), errors.New("请输入接入码")
	}
	if a.cloud == nil {
		client, err := cloud.New(a.cloudBase(), "")
		if err != nil {
			return a.Status(), err
		}
		a.cloud = client
	}
	identity, err := a.cloud.Redeem(ctx, code, "")
	if err != nil {
		return a.Status(), err
	}
	if identity.DeviceToken == "" || identity.ConnectorID == "" {
		return a.Status(), errors.New("云端未返回有效的设备身份")
	}
	a.mu.Lock()
	a.identity = identity
	a.mu.Unlock()
	if err := a.store.WriteJSON(fileIdentity, identity); err != nil {
		return a.Status(), err
	}
	client, err := cloud.New(a.cloudBase(), identity.DeviceToken)
	if err == nil {
		a.cloud = client
	}
	a.state.Transition(state.Configuring, "设备已绑定")
	return a.Status(), nil
}

// ImportConfig 解析宿主 MCP 配置并保存来源与秘密。
func (a *Agent) ImportConfig(ctx context.Context, raw []byte) (config.ImportReport, error) {
	report, err := config.ParseHostConfig(raw, config.ImportOptions{AllowStdio: a.opts.AllowStdio})
	if err != nil {
		return report, err
	}
	if report.HasErrors() {
		return report, nil
	}
	if len(report.Sources) > 16 {
		return report, errors.New("单个连接器最多允许 16 个来源")
	}
	a.mu.Lock()
	report.Sources, report.Secrets, err = assignSourceIDs(report.Sources, report.Secrets, a.config.Sources)
	if err != nil {
		a.mu.Unlock()
		return report, err
	}
	a.config.Sources = report.Sources
	a.secrets = map[string]map[string]string{}
	for sourceID, headerValues := range report.Secrets {
		a.secrets[sourceID] = headerValues
	}
	activeSources := map[string]bool{}
	for _, source := range a.config.Sources {
		activeSources[source.SourceID] = true
	}
	for sourceID := range a.profiles {
		if !activeSources[sourceID] {
			delete(a.profiles, sourceID)
		}
	}
	a.mu.Unlock()
	if err := a.store.WriteJSON(fileConfig, a.config); err != nil {
		return report, err
	}
	if err := a.store.WriteJSON(fileSecrets, a.secrets); err != nil {
		return report, err
	}
	if err := a.store.WriteJSON(fileAuth, a.profiles); err != nil {
		return report, err
	}
	if a.state.Current() == state.Unpaired || a.state.Current() == state.Configuring {
		a.state.Transition(state.Configuring, "已导入来源配置")
	}
	return report, nil
}

// assignSourceIDs treats the upstream URL as the stable identity. A display
// name may change without rotating the opaque source ID. When several existing
// sources share the same URL, the match is ambiguous and the incoming source
// receives a new ID rather than risking cross-source authorization reuse.
func assignSourceIDs(sources []config.Source, secrets map[string]map[string]string, existing []config.Source) ([]config.Source, map[string]map[string]string, error) {
	existingIDsByURL := map[string][]string{}
	for _, source := range existing {
		if isOpaqueSourceID(source.SourceID) {
			existingIDsByURL[source.UpstreamURL] = append(existingIDsByURL[source.UpstreamURL], source.SourceID)
		}
	}
	used := map[string]bool{}
	assignedSecrets := map[string]map[string]string{}
	for index := range sources {
		oldID := sources[index].SourceID
		nextID := ""
		if candidates := existingIDsByURL[sources[index].UpstreamURL]; len(candidates) == 1 {
			nextID = candidates[0]
		}
		if nextID == "" || used[nextID] {
			var err error
			nextID, err = newSourceID()
			if err != nil {
				return nil, nil, err
			}
		}
		for used[nextID] {
			var err error
			nextID, err = newSourceID()
			if err != nil {
				return nil, nil, err
			}
		}
		used[nextID] = true
		sources[index].SourceID = nextID
		if headers := secrets[oldID]; len(headers) > 0 {
			assignedSecrets[nextID] = headers
		}
	}
	return sources, assignedSecrets, nil
}

func newSourceID() (string, error) {
	raw := make([]byte, 16)
	if _, err := rand.Read(raw); err != nil {
		return "", fmt.Errorf("生成来源编号失败: %w", err)
	}
	return "src_" + hex.EncodeToString(raw), nil
}

func isOpaqueSourceID(value string) bool {
	if len(value) != len("src_")+32 || !strings.HasPrefix(value, "src_") {
		return false
	}
	_, err := hex.DecodeString(strings.TrimPrefix(value, "src_"))
	return err == nil
}

// Diagnose 对指定来源执行分层诊断；sourceID 为空表示全部来源。
func (a *Agent) Diagnose(ctx context.Context, sourceID, probe string) ([]diag.Report, error) {
	a.mu.RLock()
	sources := append([]config.Source(nil), a.config.Sources...)
	secrets := map[string]map[string]string{}
	for key, value := range a.secrets {
		secrets[key] = value
	}
	a.mu.RUnlock()
	if len(sources) == 0 {
		return nil, errors.New("还没有导入来源")
	}
	var reports []diag.Report
	for _, source := range sources {
		if sourceID != "" && source.SourceID != sourceID {
			continue
		}
		a.mu.RLock()
		profile, hasProfile := a.profiles[source.SourceID]
		a.mu.RUnlock()
		if probe != "" {
			if !hasProfile {
				return nil, errors.New("试调用前请先导入本机动态认证配置")
			}
			if !containsTool(source.ToolAllowlist, probe) {
				return nil, errors.New("试调用工具不在客户允许列表内")
			}
		}
		options := diag.Options{HeaderValues: secrets[source.SourceID], ToolProbe: probe}
		if hasProfile {
			options.HeaderProvider = func(method, tool string) (map[string]string, error) {
				if method != "tools/call" {
					return nil, nil
				}
				return huaweiauth.SignedHeaders(profile, time.Now(), rand.Reader)
			}
		}
		report := diag.Run(ctx, source, options)
		reports = append(reports, report)
	}
	if len(reports) == 0 {
		return nil, fmt.Errorf("未找到来源 %s", sourceID)
	}
	return reports, nil
}

func containsTool(tools []string, wanted string) bool {
	for _, tool := range tools {
		if tool == wanted {
			return true
		}
	}
	return false
}

// Connect 拉取期望状态、生成通道配置并启动 frpc。
func (a *Agent) Connect(ctx context.Context) (Status, error) {
	if a.cloud == nil || a.identity.DeviceToken == "" {
		return a.Status(), errors.New("请先绑定企业")
	}
	a.mu.RLock()
	sourceCount := len(a.config.Sources)
	a.mu.RUnlock()
	if sourceCount == 0 {
		return a.Status(), errors.New("请先导入 MCP 配置")
	}
	if current := a.state.Current(); current != state.Connecting {
		if !state.CanTransition(current, state.Connecting) {
			a.state.Transition(state.Configuring, "回到待配置")
		}
		a.state.Transition(state.Connecting, "开始建立通道")
	}

	desired, err := a.cloud.DesiredState(ctx, a.identity.ConnectorID)
	if err != nil {
		a.state.Transition(state.DegradedRelay, "获取云端状态失败: "+err.Error())
		return a.Status(), err
	}
	if desired.Revoked {
		a.state.Transition(state.Revoked, "云端已撤销该连接器")
		return a.Status(), errors.New("连接器已被撤销")
	}
	secretsState, err := a.cloud.TransportSecrets(ctx, a.identity.ConnectorID)
	if err != nil {
		a.state.Transition(state.DegradedRelay, "获取通道密钥失败")
		return a.Status(), fmt.Errorf("获取通道密钥失败: %w", err)
	}
	desired = mergeSecrets(desired, secretsState)

	if err := a.startAuthProxies(); err != nil {
		a.state.Transition(state.DegradedRelay, "启动本机认证代理失败: "+err.Error())
		return a.Status(), err
	}
	generated, err := a.buildFrpcConfig(desired)
	if err != nil {
		a.stopAuthProxies()
		a.state.Transition(state.DegradedRelay, "通道配置无效: "+err.Error())
		return a.Status(), err
	}
	configPath := a.store.Path(fileFrpc)
	if err := generated.WriteFile(configPath); err != nil {
		a.stopAuthProxies()
		a.state.Transition(state.DegradedRelay, "写入通道配置失败: "+err.Error())
		return a.Status(), err
	}

	binary := a.frpcBinary()
	if _, err := os.Stat(binary); err != nil {
		a.stopAuthProxies()
		a.state.Transition(state.DegradedRelay, "找不到 frpc 程序")
		return a.Status(), fmt.Errorf("找不到 frpc 程序: %s", binary)
	}

	a.mu.Lock()
	if a.supervisor != nil {
		_ = a.supervisor.Stop(context.Background())
	}
	a.supervisor = supervisor.New(binary, configPath)
	a.supervisor.PIDFile = a.store.Path("frpc.pid")
	s := a.supervisor
	stopHeartbeat := a.heartbeat
	a.mu.Unlock()
	if stopHeartbeat != nil {
		stopHeartbeat()
	}
	s.OnChange = func(running bool, detail string) {
		if !running && a.state.Current() == state.Ready {
			a.state.Transition(state.DegradedRelay, detail)
			return
		}
		if running && a.state.Current() == state.DegradedRelay {
			go a.recoverTunnel()
		}
	}
	if err := s.Start(ctx); err != nil {
		a.stopAuthProxies()
		a.state.Transition(state.DegradedRelay, err.Error())
		return a.Status(), err
	}

	time.Sleep(800 * time.Millisecond)
	if !s.Running() {
		a.stopAuthProxies()
		a.state.Transition(state.DegradedRelay, "通道未启动: "+s.LastError())
		return a.Status(), errors.New("通道未启动")
	}
	if a.state.Current() == state.Paused {
		_ = s.Stop(context.Background())
		return a.Status(), errors.New("连接已取消")
	}

	reports, diagErr := a.Diagnose(ctx, "", "")
	if diagErr == nil && !allOK(reports) {
		a.state.Transition(state.DegradedUpstream, "内网来源检查未通过")
	} else {
		a.state.Transition(state.Ready, "通道已建立")
	}
	a.startHeartbeat()
	return a.Status(), nil
}

// Pause 立即停止访问，不等待在途请求。
func (a *Agent) Pause(ctx context.Context) Status {
	a.mu.Lock()
	s := a.supervisor
	stopHeartbeat := a.heartbeat
	a.heartbeat = nil
	a.mu.Unlock()
	if stopHeartbeat != nil {
		stopHeartbeat()
	}
	if s != nil {
		_ = s.Stop(ctx)
	}
	a.stopAuthProxies()
	a.state.Transition(state.Paused, "本机暂停")
	return a.Status()
}

// Disconnect 停止通道并回到待配置状态。
func (a *Agent) Disconnect(ctx context.Context) Status {
	a.Pause(ctx)
	a.state.Transition(state.Configuring, "通道已断开")
	return a.Status()
}

// Revoke 撤销本机设备。
func (a *Agent) Revoke(ctx context.Context) (Status, error) {
	if a.cloud != nil && a.identity.ConnectorID != "" {
		if err := a.cloud.Revoke(ctx, a.identity.ConnectorID); err != nil {
			return a.Status(), err
		}
	}
	a.Pause(ctx)
	a.mu.Lock()
	a.identity = cloud.Identity{}
	a.mu.Unlock()
	_ = a.store.Remove(fileIdentity)
	a.state.Transition(state.Revoked, "设备已撤销")
	return a.Status(), nil
}

// Export 生成平台可导入的 MCP 配置。
func (a *Agent) Export() ([]byte, error) {
	a.mu.RLock()
	sources := append([]config.Source(nil), a.config.Sources...)
	a.mu.RUnlock()
	return config.BuildPlatformConfig(sources, a.edgeBase(), "")
}

func (a *Agent) buildFrpcConfig(desired cloud.DesiredState) (frpc.Config, error) {
	a.mu.RLock()
	sources := append([]config.Source(nil), a.config.Sources...)
	proxies := make(map[string]*authproxy.Server, len(a.proxies))
	for sourceID, proxy := range a.proxies {
		proxies[sourceID] = proxy
	}
	a.mu.RUnlock()

	relay := desired.Relay
	if relay.Address == "" {
		relay = a.identity.Relay
	}
	generated := frpc.Config{
		ServerAddr: relay.Address,
		ServerPort: relay.Port,
		User:       relay.User,
		AuthToken:  relay.Token,
		Protocol:   relay.Protocol,
		TLSEnabled: relay.TLS.Enabled,
		TrustedCA:  relay.TLS.TrustedCA,
		CertFile:   relay.TLS.CertFile,
		KeyFile:    relay.TLS.KeyFile,
	}
	if generated.AuthToken == "" {
		generated.AuthToken = a.identity.Relay.Token
	}
	if generated.User == "" {
		generated.User = a.identity.Relay.User
	}

	byID := map[string]cloud.DesiredSource{}
	for _, item := range desired.Sources {
		byID[item.SourceID] = item
	}
	for _, source := range sources {
		item, ok := byID[source.SourceID]
		proxy := proxies[source.SourceID]
		if proxy == nil {
			return generated, fmt.Errorf("来源 %s 缺少本机认证代理", source.DisplayName)
		}
		host, port, err := proxy.Endpoint()
		if err != nil {
			return generated, err
		}
		secret := ""
		if ok {
			secret = item.STCPSecret
		}
		generated.Proxies = append(generated.Proxies, frpc.Proxy{
			Name:       source.SourceID,
			SecretKey:  secret,
			LocalIP:    host,
			LocalPort:  port,
			AllowUsers: item.AllowUsers,
		})
	}
	if err := generated.Validate(); err != nil {
		return generated, err
	}
	return generated, nil
}

func (a *Agent) frpcBinary() string {
	if a.opts.FrpcBinary != "" {
		return a.opts.FrpcBinary
	}
	exe, err := os.Executable()
	if err == nil {
		candidate := filepath.Join(filepath.Dir(exe), frpcBinaryName())
		if _, statErr := os.Stat(candidate); statErr == nil {
			return candidate
		}
	}
	return filepath.Join(a.opts.DataDir, "bin", frpcBinaryName())
}

func (a *Agent) startHeartbeat() {
	ctx, cancel := context.WithCancel(context.Background())
	a.mu.Lock()
	previous := a.heartbeat
	a.heartbeat = cancel
	a.mu.Unlock()
	if previous != nil {
		previous()
	}
	go func() {
		ticker := time.NewTicker(30 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if a.cloud == nil || a.identity.ConnectorID == "" {
					continue
				}
				status := a.Status()
				beat := cloud.Heartbeat{
					State:      string(status.State),
					Version:    version.String(),
					ObservedAt: time.Now().UTC(),
					LastError:  status.LastError,
				}
				for _, source := range status.Sources {
					beat.SourceIDs = append(beat.SourceIDs, source.SourceID)
				}
				beatCtx, beatCancel := context.WithTimeout(ctx, 10*time.Second)
				_ = a.cloud.Heartbeat(beatCtx, a.identity.ConnectorID, beat)
				beatCancel()
			}
		}
	}()
}

func mergeSecrets(base, secrets cloud.DesiredState) cloud.DesiredState {
	if secrets.Relay.Token != "" {
		base.Relay.Token = secrets.Relay.Token
	}
	if secrets.Relay.User != "" {
		base.Relay.User = secrets.Relay.User
	}
	if secrets.Relay.Address != "" {
		base.Relay.Address = secrets.Relay.Address
	}
	if secrets.Relay.Port > 0 {
		base.Relay.Port = secrets.Relay.Port
	}
	if len(secrets.Sources) == 0 {
		return base
	}
	byID := map[string]cloud.DesiredSource{}
	for _, item := range base.Sources {
		byID[item.SourceID] = item
	}
	for _, item := range secrets.Sources {
		existing, ok := byID[item.SourceID]
		if !ok {
			base.Sources = append(base.Sources, item)
			continue
		}
		if item.STCPSecret != "" {
			existing.STCPSecret = item.STCPSecret
		}
		if len(item.AllowUsers) > 0 {
			existing.AllowUsers = item.AllowUsers
		}
		byID[item.SourceID] = existing
	}
	merged := make([]cloud.DesiredSource, 0, len(byID))
	for _, item := range byID {
		merged = append(merged, item)
	}
	base.Sources = merged
	return base
}

func localTarget(rawURL string) (string, int) {
	parsed, err := url.Parse(rawURL)
	if err != nil || parsed.Host == "" {
		return "127.0.0.1", 80
	}
	host := parsed.Hostname()
	port := 80
	if parsed.Scheme == "https" {
		port = 443
	}
	if value := parsed.Port(); value != "" {
		if parsedPort, err := net.LookupPort("tcp", value); err == nil {
			port = parsedPort
		}
	}
	return host, port
}

func allOK(reports []diag.Report) bool {
	for _, report := range reports {
		if !report.OK {
			return false
		}
	}
	return true
}

func frpcBinaryName() string {
	if runtime.GOOS == "windows" {
		return "frpc.exe"
	}
	return "frpc"
}

// Snapshot 返回可直接序列化的状态，便于测试。
func (a *Agent) Snapshot() json.RawMessage {
	raw, _ := json.Marshal(a.Status())
	return raw
}
