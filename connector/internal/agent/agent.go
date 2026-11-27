// Package agent 把配置、凭据、诊断、通道和状态串联成连接器的运行主体。
package agent

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/authproxy"
	"github.com/sherkevin/open-claude-code/connector/internal/cloud"
	"github.com/sherkevin/open-claude-code/connector/internal/config"
	"github.com/sherkevin/open-claude-code/connector/internal/demomcp"
	"github.com/sherkevin/open-claude-code/connector/internal/devicekey"
	"github.com/sherkevin/open-claude-code/connector/internal/diag"
	"github.com/sherkevin/open-claude-code/connector/internal/diagnostics"
	"github.com/sherkevin/open-claude-code/connector/internal/frpc"
	"github.com/sherkevin/open-claude-code/connector/internal/huaweiauth"
	"github.com/sherkevin/open-claude-code/connector/internal/mcp"
	"github.com/sherkevin/open-claude-code/connector/internal/state"
	"github.com/sherkevin/open-claude-code/connector/internal/store"
	"github.com/sherkevin/open-claude-code/connector/internal/supervisor"
	"github.com/sherkevin/open-claude-code/connector/internal/version"
)

// 本机文件名。
const (
	fileIdentity  = "identity.json"
	fileConfig    = "config.json"
	fileSecrets   = "secrets.json"
	fileAuth      = "auth_profiles.json"
	fileFrpc      = "frpc.toml"
	fileState     = "state.json"
	fileDeviceKey = "device_key.json"
	fileRevoked   = "revoked.json"

	supervisorStopTimeout = 5 * time.Second
	startupConnectTimeout = 90 * time.Second
)

// Options 是连接器启动参数。
type Options struct {
	PlatformURL string
	DataDir     string
	CloudBase   string
	EdgeBase    string
	FrpcBinary  string
	ListenAddr  string
	AllowStdio  bool
	NativeRelay bool

	ExclusiveDiagnostics bool

	diagnosticsFactory func(string) (*diagnostics.Manager, error)
}

// Agent 是连接器运行主体。
type Agent struct {
	opts          Options
	store         *store.Store
	state         *state.Machine
	cloud         *cloud.Client
	deviceKey     *devicekey.Key
	demoServer    *demomcp.Server
	demoState     builtinDemoState
	demoErrorCode string

	// mu guards publication and shutdown of the diagnostic queue writer.
	diagMgr              *diagnostics.Manager
	diagnosticsLock      *os.File
	diagnosticsBusy      bool
	diagnosticsRetryDone chan struct{}

	mu            sync.RWMutex
	connectMu     sync.Mutex
	lifecycleMu   sync.Mutex
	runtimeOnce   sync.Once
	closeOnce     sync.Once
	runtimeCtx    context.Context
	runtimeCancel context.CancelFunc
	closed        bool
	identity      cloud.Identity
	config        config.Connector
	secrets       map[string]map[string]string
	profiles      map[string]huaweiauth.Profile
	lastError     string

	supervisor      *supervisor.Supervisor
	native          *nativeRelay
	proxies         map[string]*authproxy.Server
	preparedProxies map[string]*authproxy.Server
	heartbeat       context.CancelFunc

	frpcAdminPort     int
	frpcAdminUser     string
	frpcAdminPassword string
	frpcUser          string
	frpcProxyCount    int
	stoppingTunnels   int
	registeredSources map[string]map[string]bool
	assistSeen        *cloud.AssistOffer
	assistActive      *assistSession
	assistStarting    bool
	assistStopped     string
	// assistWeekConsent records the control plane's capability advertisement.
	// The week policy is only sent after this is true.
	assistWeekConsent bool

	// mu guards the startup worker and cancellation of the active attempt.
	startupConnectPending bool
	tunnelRevision        uint64
	connectCancel         context.CancelFunc
}

// Status 是给本机界面和心跳使用的快照。
type Status struct {
	State           state.State       `json:"state"`
	StateLabel      string            `json:"state_label"`
	Reason          string            `json:"reason,omitempty"`
	Bound           bool              `json:"bound"`
	CloudConfigured bool              `json:"cloud_configured"`
	EdgeConfigured  bool              `json:"edge_configured"`
	ConnectorID     string            `json:"connector_id,omitempty"`
	TenantName      string            `json:"tenant_name,omitempty"`
	TenantID        string            `json:"tenant_id,omitempty"`
	Sources         []config.Source   `json:"sources"`
	FrpcRunning     bool              `json:"frpc_running"`
	FrpcRestarts    int               `json:"frpc_restarts"`
	TransportMode   string            `json:"transport_mode"`
	TunnelRestarts  int               `json:"tunnel_restarts"`
	TunnelConnected bool              `json:"tunnel_connected"`
	TunnelDetail    string            `json:"tunnel_detail,omitempty"`
	LastError       string            `json:"last_error,omitempty"`
	Version         string            `json:"version"`
	EdgeBase        string            `json:"edge_base,omitempty"`
	AuthProfiles    []SourceAuth      `json:"auth_profiles"`
	Diagnostics     diagnostics.Stats `json:"diagnostics"`
	BuiltinDemo     BuiltinDemoStatus `json:"builtin_demo"`
	UpdatedAt       time.Time         `json:"updated_at"`
}

// SourceAuth is a non-secret view of the local dynamic authentication state.
type SourceAuth struct {
	SourceID     string   `json:"source_id"`
	Configured   bool     `json:"configured"`
	AllowedTools []string `json:"allowed_tools"`
	AppID        string   `json:"app_id,omitempty"`
	Channel      string   `json:"channel,omitempty"`
	UserID       string   `json:"user_id,omitempty"`
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
	var diagnosticsLock *os.File
	var lockErr error
	if opts.ExclusiveDiagnostics {
		diagnosticsLock, lockErr = diagnostics.LockReporting(st.Dir)
		defer func() {
			if diagnosticsLock != nil {
				diagnosticsLock.Close()
			}
		}()
	}
	runtimeCtx, runtimeCancel := context.WithCancel(context.Background())
	agent := &Agent{
		opts:              opts,
		store:             st,
		state:             state.NewMachine(state.Unpaired),
		secrets:           map[string]map[string]string{},
		profiles:          map[string]huaweiauth.Profile{},
		proxies:           map[string]*authproxy.Server{},
		demoServer:        demomcp.New(),
		config:            config.Connector{SchemaVersion: config.CurrentSchemaVersion},
		registeredSources: map[string]map[string]bool{},
		runtimeCtx:        runtimeCtx,
		runtimeCancel:     runtimeCancel,
	}
	deviceKey, _, err := devicekey.LoadOrCreate(st.Path(fileDeviceKey))
	if err != nil {
		return nil, err
	}
	agent.deviceKey = deviceKey
	// Restore the user's choice without rewriting the file during startup.
	var saved persistedState
	if err := st.ReadJSON(fileState, &saved); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return nil, err
	}
	revoked := st.Exists(fileRevoked) || saved.State == state.Revoked
	if !revoked {
		if err := st.ReadProtectedJSON(fileIdentity, &agent.identity); err != nil {
			agent.identity = cloud.Identity{}
		}
	}
	if err := st.ReadJSON(fileConfig, &agent.config); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return nil, err
	}
	if err := st.ReadProtectedJSON(fileSecrets, &agent.secrets); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return nil, err
	}
	if err := st.ReadProtectedJSON(fileAuth, &agent.profiles); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return nil, err
	}
	if err := st.ReadJSON(fileBuiltinDemo, &agent.demoState); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return nil, err
	}
	if agent.demoState.SourceID != "" && !isOpaqueSourceID(agent.demoState.SourceID) {
		agent.demoState.SourceID = ""
	}
	if agent.secrets == nil {
		agent.secrets = map[string]map[string]string{}
	}
	if agent.profiles == nil {
		agent.profiles = map[string]huaweiauth.Profile{}
	}
	bound := agent.identity.DeviceToken != ""
	configured := bound || hasEnabledSource(agent.businessSourcesLocked())
	intent := saved.tunnelIntent()
	switch {
	case revoked:
		agent.state.Transition(state.Revoked, "已载入撤销记录")
	case configured:
		agent.state.Transition(state.Configuring, "已载入本机配置")
		if intent == tunnelPaused {
			agent.state.Transition(state.Paused, "已载入本机暂停选择")
		}
		startupSources := agent.config.Sources
		if saved.Intent == "" {
			// An older demo-only self-test could also write READY.
			startupSources = agent.businessSourcesLocked()
		}
		agent.startupConnectPending = bound && intent == tunnelConnected && hasEnabledSource(startupSources)
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
	})
	if agent.identity.DeviceToken != "" {
		client, err := cloud.NewWithSigner(agent.cloudBase(), agent.identity.DeviceToken, agent.deviceKey)
		if err != nil {
			return nil, err
		}
		agent.cloud = client
	}
	// Diagnostics may be temporarily owned by the offline reporting command.
	// The serving runtime retries contention without delaying the local UI.
	agent.diagnosticsBusy = errors.Is(lockErr, diagnostics.ErrReportingBusy)
	if lockErr == nil {
		if manager, err := agent.openDiagnostics(); err == nil {
			agent.installDiagnosticsLocked(manager)
			agent.diagnosticsLock, diagnosticsLock = diagnosticsLock, nil
		}
	}
	if !opts.NativeRelay {
		agent.reapStaleFrpc()
	}
	return agent, nil
}

// StartRuntime starts the shared serving lifecycle used by both CLI serve and
// the Windows Service Control Manager path.
func (a *Agent) StartRuntime() {
	if a == nil {
		return
	}
	if a.isClosed() {
		return
	}
	a.runtimeOnce.Do(func() {
		a.startDiagnostics()
		if err := a.StartBuiltinDemo(); err != nil {
			a.recordDiagnostic(diagnostics.Record{EventCode: "operation_failed"})
		}
		if a.ShouldAutoConnect() {
			go a.AutoConnect()
		}
	})
}

// Close stops background work and flushes the durable diagnostics queue.
func (a *Agent) Close() {
	ctx, cancel := context.WithTimeout(context.Background(), supervisorStopTimeout)
	defer cancel()
	a.CloseContext(ctx)
}

// CloseContext is the bounded shutdown used by the CLI and Windows SCM.
func (a *Agent) CloseContext(ctx context.Context) {
	if a == nil {
		return
	}
	a.closeOnce.Do(func() {
		a.lifecycleMu.Lock()
		defer a.lifecycleMu.Unlock()
		a.mu.Lock()
		a.closed = true
		if a.runtimeCancel != nil {
			a.runtimeCancel()
		}
		a.assistActive = nil
		a.assistSeen = nil
		s := a.supervisor
		native := a.native
		stopHeartbeat := a.heartbeat
		a.supervisor = nil
		a.native = nil
		a.heartbeat = nil
		a.frpcAdminPort = 0
		a.frpcAdminUser = ""
		a.frpcAdminPassword = ""
		a.frpcUser = ""
		a.frpcProxyCount = 0
		a.stoppingTunnels++
		a.mu.Unlock()
		if stopHeartbeat != nil {
			stopHeartbeat()
		}
		if native != nil {
			native.cancel()
		}
		if s != nil {
			_ = s.Stop(ctx)
		}
		a.stopAuthProxies()
		a.stopPreparedAuthProxies()
		a.stopBuiltinDemoServer()
		a.closeDiagnostics(ctx)
		a.mu.Lock()
		a.stoppingTunnels--
		a.mu.Unlock()
	})
}

var errAgentClosed = errors.New("连接器正在关闭，已取消本次操作")

func (a *Agent) isClosed() bool {
	if a == nil {
		return true
	}
	a.mu.RLock()
	defer a.mu.RUnlock()
	return a.closed
}

// beginRuntimeOperation ties a request or self-test to the connector's serving
// lifecycle. Close cancels this context even when the caller used background.
func (a *Agent) beginRuntimeOperation(parent context.Context) (context.Context, func(), error) {
	if a == nil {
		return nil, func() {}, errAgentClosed
	}
	a.mu.RLock()
	closed := a.closed
	runtimeCtx := a.runtimeCtx
	a.mu.RUnlock()
	if closed || runtimeCtx == nil {
		return nil, func() {}, errAgentClosed
	}
	if parent == nil {
		parent = context.Background()
	}
	ctx, cancel := context.WithCancel(parent)
	stop := context.AfterFunc(runtimeCtx, cancel)
	return ctx, func() {
		stop()
		cancel()
	}, nil
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

// recoverTunnel 在 frpc 被守护进程重新拉起后，确认内网来源健康再恢复就绪状态。
// 只跟着 frpc 的启动事件走，避免把仍在失败的代理误报为可用。
func (a *Agent) recoverTunnel() {
	a.mu.RLock()
	s := a.supervisor
	adminPort := a.frpcAdminPort
	adminUser := a.frpcAdminUser
	adminPassword := a.frpcAdminPassword
	proxyCount := a.frpcProxyCount
	a.mu.RUnlock()
	time.Sleep(1500 * time.Millisecond)
	if a.state.Current() != state.DegradedRelay || s == nil || !s.Running() {
		return
	}
	if detail := strings.ToLower(s.LastError()); strings.Contains(detail, "error") || strings.Contains(detail, "failed") {
		return
	}
	// A live frpc process is not enough: it may still be waiting for relay
	// login or for every approved STCP proxy to enter the running phase.
	if connected, _ := queryFrpcTunnel(adminPort, adminUser, adminPassword, proxyCount); !connected {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	reports, err := a.Diagnose(ctx, "", "")
	if err != nil || !allOK(reports) {
		return
	}
	a.connectMu.Lock()
	defer a.connectMu.Unlock()
	a.mu.RLock()
	sameTunnel := !a.closed && a.supervisor == s && a.frpcAdminPort == adminPort
	a.mu.RUnlock()
	if !sameTunnel || a.state.Current() != state.DegradedRelay || !s.Running() {
		return
	}
	if a.state.Transition(state.Ready, "通道已恢复") {
		a.recordDiagnostic(diagnostics.Record{EventCode: "frp_recovered"})
		a.recordDiagnostic(diagnostics.Record{EventCode: "connection_succeeded"})
		a.startHeartbeat(s)
	}
}

// onFrpChange keeps tunnel lifecycle diagnostics independent from the
// Connect/Pause call that controls the supervisor. Detail text is deliberately
// not logged because it may contain an upstream address or local error output.
func (a *Agent) onFrpChange(running bool, detail string) {
	a.mu.RLock()
	stopping := a.stoppingTunnels > 0
	a.mu.RUnlock()
	if stopping {
		return
	}
	current := a.state.Current()
	if running {
		a.recordDiagnostic(diagnostics.Record{EventCode: "frp_started"})
		if current == state.DegradedRelay {
			go a.recoverTunnel()
		}
		return
	}
	if current == state.Paused || current == state.Configuring || current == state.Revoked {
		return
	}
	a.recordDiagnostic(diagnostics.Record{EventCode: "frp_stopped"})
	a.recordDiagnostic(diagnostics.Record{EventCode: "frp_retry"})
	if current == state.Ready {
		a.state.Transition(state.DegradedRelay, detail)
	}
}

func stopSupervisor(s *supervisor.Supervisor) error {
	ctx, cancel := context.WithTimeout(context.Background(), supervisorStopTimeout)
	defer cancel()
	return s.Stop(ctx)
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

func endpointConfigured(raw string) bool {
	raw = strings.TrimRight(strings.TrimSpace(raw), "/")
	if raw == "" {
		return false
	}
	parsed, err := url.Parse(raw)
	if err != nil || parsed.Host == "" {
		return false
	}
	host := strings.ToLower(parsed.Hostname())
	switch strings.ToLower(parsed.Scheme) {
	case "https":
		return host != "api.example.com"
	case "http":
		ip := net.ParseIP(host)
		return host == "localhost" || (ip != nil && ip.IsLoopback())
	default:
		return false
	}
}

type frpcProxyStatus struct {
	Name      string `json:"name"`
	Type      string `json:"type"`
	Status    string `json:"status"`
	Err       string `json:"err"`
	LocalAddr string `json:"local_addr"`
}

func queryFrpcStatus(port int, user, password string) (map[string][]frpcProxyStatus, string, bool) {
	if port <= 0 {
		return nil, "frpc 状态接口不可用", false
	}
	ctx, cancel := context.WithTimeout(context.Background(), 900*time.Millisecond)
	defer cancel()
	request, err := http.NewRequestWithContext(
		ctx,
		http.MethodGet,
		fmt.Sprintf("http://127.0.0.1:%d/api/status", port),
		nil,
	)
	if err != nil {
		return nil, "frpc 状态接口不可用", false
	}
	request.SetBasicAuth(user, password)
	response, err := (&http.Client{Timeout: time.Second}).Do(request)
	if err != nil {
		return nil, "frpc 尚未完成中继登录", false
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, "frpc 状态接口返回异常", false
	}
	payload, err := io.ReadAll(io.LimitReader(response.Body, (1<<20)+1))
	if err != nil || len(payload) > 1<<20 {
		return nil, "frpc 状态响应无效", false
	}
	var groups map[string][]frpcProxyStatus
	if err := json.Unmarshal(payload, &groups); err != nil {
		return nil, "frpc 状态响应不是有效 JSON", false
	}
	return groups, "", true
}

func queryFrpcTunnel(port int, user, password string, expected int) (bool, string) {
	if expected <= 0 {
		return false, "frpc 尚未配置可检查的来源代理"
	}
	groups, detail, ok := queryFrpcStatus(port, user, password)
	if !ok {
		return false, detail
	}
	running := 0
	for _, statuses := range groups {
		for _, item := range statuses {
			if item.Status == "running" {
				running++
			} else if item.Err != "" {
				return false, "来源代理 " + item.Name + " 启动失败"
			}
		}
	}
	if running < expected {
		return false, fmt.Sprintf("来源代理尚未全部运行（%d/%d）", running, expected)
	}
	return true, fmt.Sprintf("%d 个来源代理已运行", running)
}

func queryFrpcProxyStatus(port int, user, password, proxyName string) (frpcProxyStatus, bool, string) {
	if strings.TrimSpace(proxyName) == "" {
		return frpcProxyStatus{}, false, "示例路由名称为空"
	}
	groups, detail, ok := queryFrpcStatus(port, user, password)
	if !ok {
		return frpcProxyStatus{}, false, detail
	}
	for _, item := range groups["stcp"] {
		if item.Name != proxyName {
			continue
		}
		if item.Status != "running" || item.Err != "" {
			return item, false, "示例 STCP 路由尚未运行"
		}
		return item, true, "示例 STCP 路由已运行"
	}
	return frpcProxyStatus{}, false, "示例 STCP 路由尚未创建"
}

// queryFrpcDemoRoute checks only the exact bundled-demo route. Customer
// route errors are intentionally ignored so a demo-only self-test is not
// coupled to the health of unrelated business sources.
func queryFrpcDemoRoute(port int, user, password, demoID, commonUser, expectedLocalAddr string) (bool, string) {
	item, ok, detail := queryFrpcProxyStatus(port, user, password, frpcProxyName(demoID, commonUser))
	if !ok {
		return false, detail
	}
	if item.LocalAddr != expectedLocalAddr {
		return false, "示例 STCP 路由本机地址或端口不一致"
	}
	return true, "示例 STCP 路由已运行"
}

// queryConnectTunnel keeps ordinary Connect on the existing all-route health
// check, while demo-only Connect checks the exact bundled route and ignores
// unrelated customer route failures.
func queryConnectTunnel(port int, generated frpc.Config, demoOnly bool, demoID string) (bool, string) {
	if !demoOnly {
		return queryFrpcTunnel(port, generated.AdminUser, generated.AdminPassword, len(generated.Proxies))
	}
	for _, proxy := range generated.Proxies {
		if proxy.Name != demoID {
			continue
		}
		expectedLocalAddr := net.JoinHostPort(proxy.LocalIP, strconv.Itoa(proxy.LocalPort))
		return queryFrpcDemoRoute(port, generated.AdminUser, generated.AdminPassword, demoID, generated.User, expectedLocalAddr)
	}
	return false, "内置示例代理未进入当前通道配置"
}

func frpcProxyName(sourceID, commonUser string) string {
	commonUser = strings.TrimSpace(commonUser)
	if commonUser == "" {
		return sourceID
	}
	return commonUser + "." + sourceID
}

// CloudReadyForTrial reports whether this build has a real control-plane and
// Edge endpoint configured. Development example endpoints never enable cloud
// actions in the trial UI.
func (a *Agent) CloudReadyForTrial() bool {
	a.mu.RLock()
	defer a.mu.RUnlock()
	return endpointConfigured(a.cloudBase()) && endpointConfigured(a.edgeBase())
}

func (a *Agent) cloudConfigured() bool {
	a.mu.RLock()
	defer a.mu.RUnlock()
	return endpointConfigured(a.cloudBase())
}

// Status 返回当前快照。
func (a *Agent) Status() Status {
	a.mu.RLock()
	status := a.statusLocked()
	s := a.supervisor
	adminPort := a.frpcAdminPort
	adminUser := a.frpcAdminUser
	adminPassword := a.frpcAdminPassword
	proxyCount := a.frpcProxyCount
	a.mu.RUnlock()
	if !status.FrpcRunning || adminPort <= 0 {
		return status
	}
	connected, detail := queryFrpcTunnel(adminPort, adminUser, adminPassword, proxyCount)
	a.mu.RLock()
	sameTunnel := a.supervisor == s && a.frpcAdminPort == adminPort
	a.mu.RUnlock()
	if !sameTunnel {
		return a.statusSnapshot()
	}
	if connected && a.state.Current() == state.DegradedRelay {
		a.state.Transition(state.Ready, "中继代理已进入运行状态")
	} else if !connected && a.state.Current() == state.Ready {
		a.state.Transition(state.DegradedRelay, detail)
	}
	a.mu.RLock()
	defer a.mu.RUnlock()
	status = a.statusLocked()
	if a.supervisor == s && a.frpcAdminPort == adminPort && status.FrpcRunning {
		status.TunnelConnected = connected
		status.TunnelDetail = detail
	}
	return status
}

// statusSnapshot returns the stored status without probing FRP. Demo-only
// failure paths use it so a customer proxy error cannot be reclassified as a
// new failure caused by the demo test itself.
func (a *Agent) statusSnapshot() Status {
	a.mu.RLock()
	defer a.mu.RUnlock()
	return a.statusLocked()
}

// Bind 用一次性接入码兑换设备身份。
func (a *Agent) Bind(ctx context.Context, code string) (Status, error) {
	a.connectMu.Lock()
	defer a.connectMu.Unlock()
	started := time.Now()
	operationID := diagnostics.NewOperationID()
	code = strings.TrimSpace(code)
	if code == "" {
		a.recordDiagnostic(diagnostics.Record{EventCode: "enrollment_failed", OperationID: operationID})
		return a.Status(), errors.New("请输入接入码")
	}
	if !a.cloudConfigured() {
		a.recordDiagnostic(diagnostics.Record{EventCode: "enrollment_failed", OperationID: operationID})
		return a.Status(), errors.New("本机环境未配置云端控制面，当前诊断试用包不能绑定企业")
	}
	if a.cloud == nil {
		client, err := cloud.NewWithSigner(a.cloudBase(), "", a.deviceKey)
		if err != nil {
			a.recordDiagnostic(diagnostics.Record{EventCode: "enrollment_failed", OperationID: operationID})
			return a.Status(), err
		}
		a.cloud = client
	}
	identity, err := a.cloud.Redeem(ctx, code, a.deviceKey.PublicKey())
	if err != nil {
		a.recordDiagnostic(diagnostics.Record{EventCode: "enrollment_failed", OperationID: operationID, DurationMS: diagnosticDuration(started)})
		return a.Status(), err
	}
	if identity.DeviceToken == "" || identity.ConnectorID == "" {
		a.recordDiagnostic(diagnostics.Record{EventCode: "enrollment_failed", OperationID: operationID, DurationMS: diagnosticDuration(started)})
		return a.Status(), errors.New("云端未返回有效的设备身份")
	}
	if err := a.validateBuiltinDemoIdentity(identity.ConnectorID); err != nil {
		a.recordDiagnostic(diagnostics.Record{EventCode: "enrollment_failed", OperationID: operationID, DurationMS: diagnosticDuration(started)})
		return a.Status(), err
	}
	a.mu.Lock()
	a.identity = identity
	a.registeredSources = map[string]map[string]bool{}
	a.mu.Unlock()
	if err := a.store.WriteProtectedJSON(fileIdentity, identity); err != nil {
		a.recordDiagnostic(diagnostics.Record{EventCode: "enrollment_failed", OperationID: operationID, DurationMS: diagnosticDuration(started)})
		return a.Status(), err
	}
	if err := a.migrateBuiltinDemoForIdentity(identity.ConnectorID); err != nil {
		a.recordDiagnostic(diagnostics.Record{EventCode: "enrollment_failed", OperationID: operationID, DurationMS: diagnosticDuration(started)})
		return a.Status(), err
	}
	client, err := cloud.NewWithSigner(a.cloudBase(), identity.DeviceToken, a.deviceKey)
	if err != nil {
		a.recordDiagnostic(diagnostics.Record{EventCode: "enrollment_failed", OperationID: operationID, DurationMS: diagnosticDuration(started)})
		return a.Status(), err
	}
	a.mu.Lock()
	a.cloud = client
	if a.diagMgr != nil {
		a.diagMgr.SetOwnerSince(identity.ConnectorID, identity.TenantID, diagnosticIdentityTime(a.store))
		applyDiagnosticApprovals(a.diagMgr, a.config.Sources)
	}
	a.mu.Unlock()
	a.state.Transition(state.Configuring, "设备已绑定")
	a.recordDiagnostic(diagnostics.Record{
		EventCode:   "enrollment_succeeded",
		OperationID: operationID,
		DurationMS:  diagnosticDuration(started),
	})
	return a.Status(), nil
}

// ImportConfig 解析宿主 MCP 配置并保存来源与秘密。
func (a *Agent) ImportConfig(ctx context.Context, raw []byte) (config.ImportReport, error) {
	started := time.Now()
	operationID := diagnostics.NewOperationID()
	report, err := config.ParseHostConfig(raw, config.ImportOptions{AllowStdio: a.opts.AllowStdio})
	if err != nil {
		a.recordDiagnostic(diagnostics.Record{EventCode: "config_import_failed", OperationID: operationID})
		return report, err
	}
	if report.HasErrors() {
		a.recordDiagnostic(diagnostics.Record{EventCode: "config_import_failed", OperationID: operationID, DurationMS: diagnosticDuration(started)})
		return report, nil
	}
	if len(report.Sources) > maxBusinessSources {
		a.recordDiagnostic(diagnostics.Record{EventCode: "config_import_failed", OperationID: operationID, DurationMS: diagnosticDuration(started)})
		return report, errors.New("单个连接器最多允许 16 个业务来源")
	}
	a.connectMu.Lock()
	defer a.connectMu.Unlock()
	a.mu.Lock()
	report.Sources, report.Secrets, err = assignSourceIDs(report.Sources, report.Secrets, a.businessSourcesLocked())
	if err != nil {
		a.mu.Unlock()
		a.recordDiagnostic(diagnostics.Record{EventCode: "config_import_failed", OperationID: operationID, DurationMS: diagnosticDuration(started)})
		return report, err
	}
	a.config.Sources = report.Sources
	a.secrets = map[string]map[string]string{}
	for sourceID, headerValues := range report.Secrets {
		a.secrets[sourceID] = headerValues
	}
	activeSources := map[string]bool{}
	for _, source := range a.config.Sources {
		// An auth profile without an approved tool list is unusable and must
		// not remain presented as a valid local authorization.
		activeSources[source.SourceID] = len(source.ToolAllowlist) > 0
	}
	for sourceID := range a.profiles {
		if !activeSources[sourceID] {
			delete(a.profiles, sourceID)
		}
	}
	a.mu.Unlock()
	if err := a.store.WriteJSON(fileConfig, a.config); err != nil {
		a.recordDiagnostic(diagnostics.Record{EventCode: "config_import_failed", OperationID: operationID, DurationMS: diagnosticDuration(started)})
		return report, err
	}
	if err := a.store.WriteProtectedJSON(fileSecrets, a.secrets); err != nil {
		a.recordDiagnostic(diagnostics.Record{EventCode: "config_import_failed", OperationID: operationID, DurationMS: diagnosticDuration(started)})
		return report, err
	}
	if err := a.store.WriteProtectedJSON(fileAuth, a.profiles); err != nil {
		a.recordDiagnostic(diagnostics.Record{EventCode: "config_import_failed", OperationID: operationID, DurationMS: diagnosticDuration(started)})
		return report, err
	}
	a.syncDiagnosticApprovals()
	if a.state.Current() == state.Unpaired || a.state.Current() == state.Configuring {
		a.state.Transition(state.Configuring, "已导入来源配置")
	}
	a.mu.RLock()
	demoDisabled := a.demoState.Disabled
	a.mu.RUnlock()
	if !demoDisabled {
		// Import replaces business sources but the bundled test source remains
		// independent. A conflict is surfaced in BuiltinDemo without blocking
		// the customer's successfully validated import.
		_ = a.StartBuiltinDemo()
	}
	a.recordDiagnostic(diagnostics.Record{
		EventCode:   "config_imported",
		OperationID: operationID,
		DurationMS:  diagnosticDuration(started),
	})
	return report, nil
}

// assignSourceIDs treats the upstream URL as the stable identity. A display
// name may change without rotating the opaque source ID. When several existing
// sources share the same URL, the match is ambiguous and the incoming source
// receives a new ID rather than risking cross-source authorization reuse.
func assignSourceIDs(sources []config.Source, secrets map[string]map[string]string, existing []config.Source) ([]config.Source, map[string]map[string]string, error) {
	existingIDsByURL := map[string][]string{}
	existingByID := map[string]config.Source{}
	for _, source := range existing {
		if isOpaqueSourceID(source.SourceID) {
			existingIDsByURL[source.UpstreamURL] = append(existingIDsByURL[source.UpstreamURL], source.SourceID)
			existingByID[source.SourceID] = source
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
		if previous, ok := existingByID[nextID]; ok &&
			previous.UpstreamURL == sources[index].UpstreamURL &&
			previous.Transport == sources[index].Transport {
			if len(sources[index].ToolAllowlist) == 0 {
				// Re-importing the same MCP endpoint must not silently revoke the
				// administrator-approved tool allowlist.
				sources[index].ToolAllowlist = append([]string(nil), previous.ToolAllowlist...)
			}
			// Enablement is local maintenance state. Re-importing the endpoint
			// must not accidentally reconnect a source the operator disabled.
			sources[index].Disabled = previous.Disabled
		}
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

func freeLoopbackPort() (int, error) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	defer listener.Close()
	address, ok := listener.Addr().(*net.TCPAddr)
	if !ok {
		return 0, errors.New("本机管理端口地址无效")
	}
	return address.Port, nil
}

func randomHex(bytesLen int) (string, error) {
	raw := make([]byte, bytesLen)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return hex.EncodeToString(raw), nil
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
	return a.diagnose(ctx, sourceID, probe, nil, false)
}

// DiagnoseWithArguments reproduces one explicitly selected, authorized query
// directly against the saved upstream, without returning credentials or business
// records. This is a local operator diagnostic, not a new cloud tool.
func (a *Agent) DiagnoseWithArguments(ctx context.Context, sourceID, probe string, raw json.RawMessage) ([]diag.Report, error) {
	if !isOpaqueSourceID(strings.TrimSpace(sourceID)) || (probe != "query" && probe != "describe") {
		return nil, errors.New("参数诊断必须指定已保存的来源和经批准的 query/describe 只读工具")
	}
	var args map[string]any
	if len(raw) == 0 || len(raw) > 16<<10 || json.Unmarshal(raw, &args) != nil || args == nil {
		return nil, errors.New("诊断参数必须为不超过 16 KiB 的 JSON 对象")
	}
	if a.state.Current() == state.Revoked || a.store.Exists(fileRevoked) {
		return nil, errors.New("企业绑定已撤销")
	}
	return a.diagnose(ctx, sourceID, probe, raw, true)
}

func (a *Agent) diagnose(ctx context.Context, sourceID, probe string, arguments any, replay bool) (reports []diag.Report, err error) {
	sourceID = strings.TrimSpace(sourceID)
	started := time.Now()
	operationID := diagnostics.NewOperationID()
	a.recordDiagnostic(diagnostics.Record{
		EventCode:   "diagnosis_started",
		OperationID: operationID,
		SourceID:    sourceID,
	})
	defer func() {
		code := "diagnosis_succeeded"
		if err != nil || len(reports) == 0 || !allOK(reports) {
			code = "diagnosis_failed"
		}
		a.recordDiagnostic(diagnostics.Record{
			EventCode:   code,
			OperationID: operationID,
			SourceID:    sourceID,
			DurationMS:  diagnosticDuration(started),
		})
	}()
	a.mu.RLock()
	sources := append([]config.Source(nil), a.config.Sources...)
	secrets := cloneSourceSecrets(a.secrets)
	profiles := cloneProfiles(a.profiles)
	a.mu.RUnlock()
	if sourceID == "" {
		sources = enabledSources(sources)
	}
	if len(sources) == 0 {
		return nil, errors.New("还没有启用的来源")
	}
	for _, source := range sources {
		if sourceID != "" && source.SourceID != sourceID {
			continue
		}
		if source.Disabled {
			return nil, fmt.Errorf("来源 %s 已停用，请先启用后再诊断", source.DisplayName)
		}
		profile, hasProfile := profiles[source.SourceID]
		if probe != "" {
			if !containsTool(source.ToolAllowlist, probe) {
				return nil, errors.New("试调用工具不在当前已批准列表内")
			}
		}
		options := diag.Options{HeaderValues: secrets[source.SourceID], ToolProbe: probe, ToolArguments: arguments, ReplayProbe: replay}
		if replay {
			options.Timeout = 20 * time.Second
		}
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

type sourceRegistrationError struct {
	sourceID string
	cause    error
}

func (err *sourceRegistrationError) Error() string { return err.cause.Error() }
func (err *sourceRegistrationError) Unwrap() error { return err.cause }

// registerSources publishes only opaque source metadata to the control plane.
// Customer URLs, credentials and local proxy targets remain local.
func (a *Agent) registerSources(ctx context.Context, sources []config.Source, proxies map[string]*authproxy.Server) error {
	a.mu.RLock()
	demoID := a.demoState.SourceID
	a.mu.RUnlock()
	if a.cloud == nil {
		return errors.New("请先绑定企业")
	}
	if len(sources) == 0 {
		return errors.New("没有启用的 MCP 服务，请至少启用一个来源")
	}
	for _, source := range sources {
		allowedTools, err := normalizeTools(source.ToolAllowlist)
		if err != nil {
			return fmt.Errorf("来源 %s 的工具允许列表无效: %w", source.DisplayName, err)
		}
		if len(allowedTools) == 0 {
			return fmt.Errorf("来源 %s 未设置当前服务器的只读工具允许列表", source.DisplayName)
		}
		proxy := proxies[source.SourceID]
		if proxy == nil {
			return fmt.Errorf("来源 %s 缺少本机认证代理", source.DisplayName)
		}
		schemaDigest, err := discoverToolSchemaDigest(ctx, proxy, allowedTools)
		if err != nil {
			return fmt.Errorf("来源 %s 的工具 schema 发现失败: %w", source.DisplayName, err)
		}
		if _, fail := a.cloud.RegisterSource(ctx, cloud.SourceRegistration{
			SourceID:            source.SourceID,
			DisplayName:         source.DisplayName,
			AllowedTools:        allowedTools,
			ToolSchemaSHA256:    schemaDigest,
			TargetBindingSHA256: sourceBindingDigest(source, demoID),
			Transport:           source.Transport,
		}); fail != nil {
			return &sourceRegistrationError{
				sourceID: source.SourceID,
				cause:    fmt.Errorf("登记来源 %s 失败: %w", source.DisplayName, fail),
			}
		}
		a.markSourceRegistered(source.SourceID, allowedTools)
	}
	return nil
}

func sourceBindingDigest(source config.Source, demoID string) string {
	if source.SourceID == demoID {
		// Each process chooses a new loopback port for the same bundled service.
		return digestString(builtinDemoProtocolProfile + ":" + demoID)
	}
	return digestString(strings.TrimRight(source.UpstreamURL, "/"))
}

func discoverToolSchemaDigest(ctx context.Context, proxy *authproxy.Server, allowlist []string) (string, error) {
	host, port, err := proxy.Endpoint()
	if err != nil {
		return "", err
	}
	client := &mcp.Client{
		Endpoint: fmt.Sprintf("http://%s:%d/mcp", host, port),
		HTTP:     &http.Client{Timeout: 15 * time.Second},
	}
	discoveryCtx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	info, err := client.Initialize(discoveryCtx)
	if err != nil {
		return "", fmt.Errorf("MCP 初始化失败: %w", err)
	}
	tools, err := client.ListTools(discoveryCtx)
	if err != nil {
		return "", fmt.Errorf("读取工具目录失败: %w", err)
	}
	return digestToolSchema(info, tools, allowlist)
}

// digestToolSchema hashes only the approved tools and the schema-bearing
// fields that affect call semantics. Map keys are sorted by encoding/json, so
// cosmetic ordering changes do not rotate the digest.
func digestToolSchema(info mcp.ServerInfo, tools []mcp.Tool, allowlist []string) (string, error) {
	allowed := make(map[string]bool, len(allowlist))
	for _, name := range allowlist {
		allowed[name] = true
	}
	byName := make(map[string]mcp.Tool, len(tools))
	for _, tool := range tools {
		if tool.Name == "" {
			return "", errors.New("工具目录包含空名称")
		}
		if _, exists := byName[tool.Name]; exists {
			return "", fmt.Errorf("工具目录包含重复名称: %s", tool.Name)
		}
		byName[tool.Name] = tool
	}
	names := make([]string, 0, len(allowlist))
	for _, name := range allowlist {
		if _, exists := byName[name]; !exists {
			return "", fmt.Errorf("允许列表中的工具不存在: %s", name)
		}
		names = append(names, name)
	}
	sort.Strings(names)

	canonicalTools := make([]any, 0, len(names))
	for _, name := range names {
		tool := byName[name]
		raw := tool.Raw
		if len(raw) == 0 {
			encoded, err := json.Marshal(tool)
			if err != nil {
				return "", err
			}
			raw = encoded
		}
		var object map[string]any
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.UseNumber()
		if err := decoder.Decode(&object); err != nil {
			return "", fmt.Errorf("工具 %s 的定义不是 JSON 对象: %w", name, err)
		}
		canonical := map[string]any{"name": name}
		for _, key := range []string{"title", "description", "inputSchema", "outputSchema", "annotations"} {
			if value, exists := object[key]; exists {
				canonical[key] = value
			}
		}
		canonicalTools = append(canonicalTools, canonical)
	}
	manifest := map[string]any{
		"digest_schema_version": 1,
		"protocol_version":      info.ProtocolVersion,
		"server_name":           info.ServerInfo.Name,
		"server_version":        info.ServerInfo.Version,
		"tools":                 canonicalTools,
	}
	payload, err := json.Marshal(manifest)
	if err != nil {
		return "", err
	}
	return digestString(string(payload)), nil
}

func digestString(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}

// Connect 拉取期望状态、生成通道配置并启动 frpc。
func (a *Agent) Connect(ctx context.Context) (Status, error) {
	return a.connect(ctx, false)
}

func (a *Agent) connect(ctx context.Context, demoOnly bool) (Status, error) {
	return a.runConnect(ctx, demoOnly, false)
}

func (a *Agent) establishTunnel(ctx context.Context, demoOnly, automatic bool) (status Status, err error) {
	if demoOnly && a.state.Current() == state.Paused {
		return a.statusSnapshot(), errTunnelPaused
	}
	started := time.Now()
	operationID := diagnostics.NewOperationID()
	a.recordDiagnostic(diagnostics.Record{EventCode: "connection_started", OperationID: operationID})
	defer func() {
		code := "connection_succeeded"
		sourceID := ""
		if err != nil || status.State != state.Ready {
			code = "connection_failed"
		}
		var registrationErr *sourceRegistrationError
		if errors.As(err, &registrationErr) {
			sourceID = registrationErr.sourceID
			var apiErr *cloud.APIError
			if errors.As(registrationErr, &apiErr) && apiErr.Code == "SOURCE_CONFIG_CONFLICT" {
				code = "connection_source_conflict"
			}
		}
		a.recordOperationDiagnostic(code, operationID, sourceID, started, err)
	}()
	if !a.CloudReadyForTrial() {
		return a.Status(), errors.New("本机环境未配置云端中继，当前诊断试用包不能建立连接")
	}
	if a.state.Current() == state.Revoked {
		return a.statusSnapshot(), errors.New("连接器已被撤销")
	}
	a.mu.RLock()
	client := a.cloud
	identity := a.identity
	sources := a.runtimeSourcesLocked()
	hasSources := len(sources) > 0
	demoID := a.demoState.SourceID
	demoSourceIndex := a.builtinDemoSourceIndexLocked()
	demoRunning := a.builtinDemoRuntimeReadyLocked()
	a.mu.RUnlock()
	if client == nil || identity.DeviceToken == "" {
		return a.statusSnapshot(), errors.New("请先绑定企业")
	}
	if demoOnly {
		hasSources = demoSourceIndex >= 0 && demoID != "" && demoRunning
	}
	if !hasSources {
		if demoOnly {
			return a.Status(), errors.New("内置测试服务尚未启动，请先在页面中启用内置测试服务")
		}
		return a.Status(), errors.New("请至少启用一个 MCP 服务后再连接")
	}
	if !demoOnly && !automatic {
		if err := a.saveTunnelChoice(tunnelConnected, state.Connecting); err != nil {
			return a.statusSnapshot(), err
		}
	}
	if current := a.state.Current(); !demoOnly && current != state.Connecting {
		if !state.CanTransition(current, state.Connecting) {
			a.state.Transition(state.Configuring, "回到待配置")
		}
		a.state.Transition(state.Connecting, "开始建立通道")
	}

	var proxies, demoCreatedProxies map[string]*authproxy.Server
	if demoOnly {
		demoCreatedProxies, err = a.startAuthProxiesForDemo()
		a.mu.RLock()
		proxies = a.proxies
		a.mu.RUnlock()
	} else {
		proxies, err = a.prepareAuthProxies(sources)
	}
	if err != nil {
		if !demoOnly {
			a.state.Transition(state.DegradedRelay, "启动本机认证代理失败: "+err.Error())
		}
		return a.Status(), err
	}
	if demoOnly && !a.opts.NativeRelay {
		reused, reuseErr := a.reuseRunningDemoTunnel(ctx)
		if reused {
			if reuseErr != nil {
				a.stopNewAuthProxies(demoCreatedProxies)
				return a.statusSnapshot(), reuseErr
			}
			return a.statusSnapshot(), nil
		}
	}
	cleanupProxies := func() {
		if demoOnly {
			a.stopNewAuthProxies(demoCreatedProxies)
			return
		}
		a.stopPreparedAuthProxies()
	}
	if a.opts.NativeRelay {
		return a.establishNativeRelay(ctx, client, identity.ConnectorID, sources, proxies, demoOnly, cleanupProxies)
	}
	registrationErr := error(nil)
	if demoOnly {
		source, sourceErr := a.builtinDemoSource()
		if sourceErr != nil {
			registrationErr = sourceErr
		} else {
			registrationErr = a.registerSources(ctx, []config.Source{source}, proxies)
		}
	} else {
		registrationErr = a.registerSources(ctx, sources, proxies)
	}
	if registrationErr != nil {
		if cloudRevokedDevice(registrationErr) {
			cleanupProxies()
			return a.revokeFromCloud(ctx, identity.ConnectorID)
		}
		cleanupProxies()
		if !demoOnly {
			a.state.Transition(state.DegradedRelay, registrationErr.Error())
		}
		return a.Status(), registrationErr
	}
	desired, err := client.DesiredState(ctx, identity.ConnectorID)
	if err != nil {
		if cloudRevokedDevice(err) {
			cleanupProxies()
			return a.revokeFromCloud(ctx, identity.ConnectorID)
		}
		cleanupProxies()
		if !demoOnly {
			a.state.Transition(state.DegradedRelay, "获取云端状态失败: "+err.Error())
		}
		return a.Status(), err
	}
	if desired.Revoked {
		cleanupProxies()
		return a.revokeFromCloud(ctx, identity.ConnectorID)
	}
	if desired.Draining {
		cleanupProxies()
		return a.drainTunnel(ctx)
	}
	secretsState, err := client.TransportSecrets(ctx, identity.ConnectorID)
	if err != nil {
		if cloudRevokedDevice(err) {
			cleanupProxies()
			return a.revokeFromCloud(ctx, identity.ConnectorID)
		}
		cleanupProxies()
		if !demoOnly {
			a.state.Transition(state.DegradedRelay, "获取通道密钥失败")
		}
		return a.Status(), fmt.Errorf("获取通道密钥失败: %w", err)
	}
	if secretsState.Revoked {
		cleanupProxies()
		return a.revokeFromCloud(ctx, identity.ConnectorID)
	}
	if secretsState.Draining {
		cleanupProxies()
		return a.drainTunnel(ctx)
	}
	desired = mergeSecrets(desired, secretsState)

	generated, err := a.buildFrpcConfig(desired, proxies)
	if err != nil {
		cleanupProxies()
		if !demoOnly {
			a.state.Transition(state.DegradedRelay, "通道配置无效: "+err.Error())
		}
		return a.Status(), err
	}
	if demoOnly {
		demoApproved := false
		for _, proxy := range generated.Proxies {
			if proxy.Name == demoID {
				demoApproved = true
				break
			}
		}
		if !demoApproved {
			cleanupProxies()
			return a.Status(), errors.New("云端尚未批准内置测试来源，请先由开物管理员完成来源登记和授权")
		}
	}
	if err := ctx.Err(); err != nil {
		cleanupProxies()
		return a.statusSnapshot(), err
	}
	adminPort, err := freeLoopbackPort()
	if err != nil {
		cleanupProxies()
		if !demoOnly {
			a.state.Transition(state.DegradedRelay, "分配 frpc 本机管理端口失败")
		}
		return a.Status(), err
	}
	adminPassword, err := randomHex(24)
	if err != nil {
		cleanupProxies()
		if !demoOnly {
			a.state.Transition(state.DegradedRelay, "生成 frpc 本机管理凭据失败")
		}
		return a.Status(), err
	}
	generated.AdminPort = adminPort
	generated.AdminUser = "kaiwu"
	generated.AdminPassword = adminPassword
	binary := a.frpcBinary()
	if _, err := os.Stat(binary); err != nil {
		cleanupProxies()
		if !demoOnly {
			a.state.Transition(state.DegradedRelay, "找不到 frpc 程序")
		}
		return a.Status(), fmt.Errorf("找不到 frpc 程序: %s", binary)
	}

	configPath := a.store.Path(fileFrpc)
	configText, frpcEnvironment, err := generated.RenderWithEnvSecrets()
	if err != nil {
		cleanupProxies()
		return a.statusSnapshot(), err
	}

	a.lifecycleMu.Lock()
	if err := ctx.Err(); err != nil {
		a.lifecycleMu.Unlock()
		cleanupProxies()
		return a.statusSnapshot(), err
	}
	if a.isClosed() {
		a.lifecycleMu.Unlock()
		cleanupProxies()
		return a.statusSnapshot(), errAgentClosed
	}
	a.mu.Lock()
	previousSupervisor := a.supervisor
	previousProxies := a.proxies
	a.stoppingTunnels++
	a.mu.Unlock()
	if previousSupervisor != nil {
		err = stopSupervisor(previousSupervisor)
	}
	if err == nil {
		// A restarting previous supervisor must not read the new file with
		// its old environment. Replace the file after that supervisor stops.
		if writeErr := a.store.WriteFile(fileFrpc, []byte(configText)); writeErr != nil {
			err = fmt.Errorf("写入通道配置失败: %w", writeErr)
			if previousSupervisor != nil {
				err = errors.Join(err, previousSupervisor.Start(ctx))
			}
		}
	}
	a.mu.Lock()
	a.stoppingTunnels--
	if err != nil {
		a.mu.Unlock()
		a.lifecycleMu.Unlock()
		cleanupProxies()
		if !demoOnly {
			a.state.Transition(state.DegradedRelay, err.Error())
		}
		return a.Status(), err
	}
	a.frpcAdminPort = adminPort
	a.frpcAdminUser = generated.AdminUser
	a.frpcAdminPassword = adminPassword
	a.frpcUser = generated.User
	a.frpcProxyCount = len(generated.Proxies)
	s := supervisor.New(binary, configPath)
	s.PIDFile = a.store.Path("frpc.pid")
	s.Environment = frpcEnvironment
	s.OnChange = a.onFrpChange
	a.supervisor = s
	stopHeartbeat := a.heartbeat
	if !demoOnly {
		a.proxies = proxies
		a.preparedProxies = nil
	}
	a.mu.Unlock()
	if !demoOnly {
		closeProxies(previousProxies)
		cleanupProxies = a.stopAuthProxies
	}
	if stopHeartbeat != nil {
		stopHeartbeat()
	}
	if err := s.Start(ctx); err != nil {
		a.lifecycleMu.Unlock()
		cleanupProxies()
		a.state.Transition(state.DegradedRelay, err.Error())
		return a.Status(), err
	}
	a.lifecycleMu.Unlock()
	defer func() {
		if ctx.Err() != nil {
			if err == nil {
				err = ctx.Err()
			}
			stopCtx, cancel := context.WithTimeout(context.Background(), supervisorStopTimeout)
			defer cancel()
			err = errors.Join(err, a.stopTunnel(stopCtx))
			status = a.statusSnapshot()
		}
	}()

	select {
	case <-ctx.Done():
		return a.statusSnapshot(), ctx.Err()
	case <-time.After(800 * time.Millisecond):
	}
	if !s.Running() {
		cleanupProxies()
		a.state.Transition(state.DegradedRelay, "通道未启动: "+s.LastError())
		return a.Status(), errors.New("通道未启动")
	}
	tunnelConnected := false
	tunnelDetail := "frpc 尚未完成中继登录"
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		tunnelConnected, tunnelDetail = queryConnectTunnel(adminPort, generated, demoOnly, demoID)
		if tunnelConnected {
			break
		}
		select {
		case <-ctx.Done():
			return a.statusSnapshot(), ctx.Err()
		case <-time.After(250 * time.Millisecond):
		}
	}
	diagnoseSourceID := ""
	if demoOnly {
		diagnoseSourceID = demoID
	}
	reports, diagErr := a.Diagnose(ctx, diagnoseSourceID, "")
	if err := ctx.Err(); err != nil {
		return a.statusSnapshot(), err
	}
	if diagErr != nil {
		a.state.Transition(state.DegradedUpstream, "内网来源检查失败: "+diagErr.Error())
		a.startHeartbeat(s)
		return a.Status(), fmt.Errorf("内网来源检查失败: %w", diagErr)
	}
	if !allOK(reports) {
		a.state.Transition(state.DegradedUpstream, "内网来源检查未通过")
	} else if !tunnelConnected {
		a.state.Transition(state.DegradedRelay, tunnelDetail)
	} else {
		if demoOnly && a.state.Current() != state.Ready && !state.CanTransition(a.state.Current(), state.Ready) {
			if state.CanTransition(a.state.Current(), state.Connecting) {
				a.state.Transition(state.Connecting, "开始建立通道")
			}
		}
		a.state.Transition(state.Ready, "通道已建立")
		a.startHeartbeat(s)
		return a.Status(), nil
	}
	a.startHeartbeat(s)
	if a.state.Current() == state.DegradedUpstream {
		return a.Status(), errors.New("内网来源检查未通过")
	}
	return a.Status(), fmt.Errorf("公网隧道尚未就绪: %s", tunnelDetail)
}

const demoRouteReconnectMessage = "测试通道尚未就绪，请在方便时重新连接"

// reuseRunningDemoTunnel never rewrites FRP configuration or replaces the
// running supervisor. It accepts only the exact demo STCP route and local
// listener that belong to this connector process.
func (a *Agent) reuseRunningDemoTunnel(ctx context.Context) (bool, error) {
	a.mu.RLock()
	s := a.supervisor
	demoID := a.demoState.SourceID
	demoServer := a.demoServer
	demoProxy := a.proxies[demoID]
	adminPort := a.frpcAdminPort
	adminUser := a.frpcAdminUser
	adminPassword := a.frpcAdminPassword
	commonUser := a.frpcUser
	a.mu.RUnlock()
	if s == nil || !s.Running() {
		return false, nil
	}
	if demoID == "" || demoServer == nil || demoProxy == nil {
		return true, errors.New(demoRouteReconnectMessage)
	}
	host, port, err := demoProxy.Endpoint()
	if err != nil {
		return true, errors.New(demoRouteReconnectMessage)
	}
	demoEndpoint := demoServer.Endpoint()
	if demoEndpoint == "" || demoProxy.UpstreamURL() != demoEndpoint {
		return true, errors.New(demoRouteReconnectMessage)
	}
	expectedLocalAddr := net.JoinHostPort(host, strconv.Itoa(port))
	proxyName := frpcProxyName(demoID, commonUser)
	item, ok, _ := queryFrpcProxyStatus(adminPort, adminUser, adminPassword, proxyName)
	if !ok || item.LocalAddr != expectedLocalAddr {
		return true, errors.New(demoRouteReconnectMessage)
	}
	reports, err := a.Diagnose(ctx, demoID, "")
	if err != nil {
		return true, fmt.Errorf("内置测试来源诊断未通过: %w", err)
	}
	if !allOK(reports) {
		return true, errors.New("内置测试来源诊断未通过")
	}
	return true, nil
}

// Revoke 撤销本机设备。
func (a *Agent) Revoke(ctx context.Context) (Status, error) {
	unlock, err := a.beginTunnelChoice()
	if err != nil {
		return a.statusSnapshot(), err
	}

	a.mu.RLock()
	client := a.cloud
	connectorID := a.identity.ConnectorID
	a.mu.RUnlock()
	localErr := a.revokeLocally(ctx, connectorID, "设备已撤销")
	unlock()
	if client != nil && connectorID != "" {
		if err := client.Revoke(ctx, connectorID); err != nil {
			return a.statusSnapshot(), errors.Join(localErr, fmt.Errorf("本机已撤销，云端通知未完成: %w", err))
		}
	}
	return a.statusSnapshot(), localErr
}

// revokeLocally is shared by explicit and cloud revocation. connectMu is held.
func (a *Agent) revokeLocally(ctx context.Context, connectorID, reason string) error {
	a.mu.Lock()
	if a.assistActive != nil {
		a.assistActive.cancel()
		a.assistActive = nil
	}
	a.assistSeen = nil
	a.mu.Unlock()
	tombstoneErr := a.store.WriteJSON(fileRevoked, map[string]string{
		"connector_id": connectorID,
		"revoked_at":   time.Now().UTC().Format(time.RFC3339Nano),
	})
	if tombstoneErr != nil {
		tombstoneErr = fmt.Errorf("无法保存设备撤销记录: %w", tombstoneErr)
	}
	saveErr := a.saveTunnelChoice(tunnelDisconnected, state.Revoked)
	stopErr := a.stopTunnel(ctx)
	a.mu.Lock()
	if a.diagMgr != nil {
		a.diagMgr.ClearOwner()
	}
	if a.assistActive != nil {
		a.assistActive.cancel()
		a.assistActive = nil
	}
	a.assistSeen = nil
	a.identity = cloud.Identity{}
	a.cloud = nil
	a.secrets = map[string]map[string]string{}
	a.profiles = map[string]huaweiauth.Profile{}
	a.registeredSources = map[string]map[string]bool{}
	a.startupConnectPending = false
	a.mu.Unlock()
	removeErr := errors.Join(
		a.store.Remove(fileIdentity),
		a.store.Remove(fileSecrets),
		a.store.Remove(fileAuth),
		a.store.Remove(fileFrpc),
	)
	a.state.Transition(state.Revoked, reason)
	a.recordDiagnostic(diagnostics.Record{EventCode: "connection_revoked"})
	return errors.Join(tombstoneErr, saveErr, stopErr, removeErr)
}

// Export 生成平台可导入的 MCP 配置。
func (a *Agent) Export() ([]byte, error) {
	a.mu.RLock()
	sources := enabledSources(a.config.Sources)
	demoID := a.demoState.SourceID
	edgeBase := a.edgeBase()
	a.mu.RUnlock()
	business := make([]config.Source, 0, len(sources))
	for _, source := range sources {
		if demoID != "" && source.SourceID == demoID {
			continue
		}
		business = append(business, source)
	}
	return config.BuildPlatformConfig(business, edgeBase, "")
}

func (a *Agent) buildFrpcConfig(desired cloud.DesiredState, proxies map[string]*authproxy.Server) (frpc.Config, error) {
	a.mu.RLock()
	sources := append([]config.Source(nil), a.config.Sources...)
	demoID := a.demoState.SourceID
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
		ServerName: relay.TLS.ServerName,
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
	localByID := map[string]config.Source{}
	for _, source := range sources {
		localByID[source.SourceID] = source
	}
	for _, item := range desired.Sources {
		source, ok := localByID[item.SourceID]
		if !ok {
			if item.SourceID != demoID {
				continue
			}
			return generated, fmt.Errorf("云端引用了本机未导入的来源 %s", item.SourceID)
		}
		if source.Disabled {
			continue
		}
		proxy := proxies[source.SourceID]
		if proxy == nil {
			if source.SourceID != demoID {
				continue
			}
			return generated, fmt.Errorf("来源 %s 缺少本机认证代理", source.DisplayName)
		}
		host, port, err := proxy.Endpoint()
		if err != nil {
			return generated, err
		}
		generated.Proxies = append(generated.Proxies, frpc.Proxy{
			Name:       source.SourceID,
			SecretKey:  item.STCPSecret,
			LocalIP:    host,
			LocalPort:  port,
			AllowUsers: item.AllowUsers,
		})
	}
	if len(generated.Proxies) == 0 {
		return generated, errors.New("云端尚未批准任何可用来源")
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

func (a *Agent) startHeartbeat(s *supervisor.Supervisor) {
	a.mu.Lock()
	frpStarted := s != nil && a.supervisor == s && a.frpcAdminPort != 0
	if a.closed || a.runtimeCtx == nil || (!frpStarted && a.native == nil) {
		a.mu.Unlock()
		return
	}
	ctx, cancel := context.WithCancel(a.runtimeCtx)
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
				a.mu.RLock()
				client := a.cloud
				connectorID := a.identity.ConnectorID
				a.mu.RUnlock()
				if client == nil || connectorID == "" {
					continue
				}
				status := a.Status()
				beat := a.heartbeatPayload(status)
				beatCtx, beatCancel := context.WithTimeout(ctx, 10*time.Second)
				_ = client.Heartbeat(beatCtx, connectorID, beat)
				beatCancel()
			}
		}
	}()
}

func (a *Agent) heartbeatPayload(status Status) cloud.Heartbeat {
	a.mu.RLock()
	activeSources := make([]string, 0, len(a.proxies))
	for sourceID := range a.proxies {
		activeSources = append(activeSources, sourceID)
	}
	a.mu.RUnlock()
	sort.Strings(activeSources)
	return cloud.Heartbeat{
		State:      string(status.State),
		Version:    version.String(),
		ObservedAt: time.Now().UTC(),
		LastError:  heartbeatErrorCode(status),
		SourceIDs:  activeSources,
	}
}

func heartbeatErrorCode(status Status) string {
	switch status.State {
	case state.DegradedUpstream:
		return "CONNECTION_DEGRADED_UPSTREAM"
	case state.DegradedRelay:
		return "CONNECTION_DEGRADED_RELAY"
	case state.Revoked:
		return "CONNECTION_REVOKED"
	default:
		return ""
	}
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
