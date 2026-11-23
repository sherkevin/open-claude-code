package agent

import (
	"context"
	"errors"
	"io/fs"
	"os"
	"strconv"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/authproxy"
	"github.com/sherkevin/open-claude-code/connector/internal/cloud"
	"github.com/sherkevin/open-claude-code/connector/internal/config"
	"github.com/sherkevin/open-claude-code/connector/internal/devicekey"
	"github.com/sherkevin/open-claude-code/connector/internal/diagnostics"
	"github.com/sherkevin/open-claude-code/connector/internal/mcp"
	"github.com/sherkevin/open-claude-code/connector/internal/state"
	"github.com/sherkevin/open-claude-code/connector/internal/store"
)

func (a *Agent) openDiagnostics() (*diagnostics.Manager, error) {
	if a.opts.diagnosticsFactory != nil {
		return a.opts.diagnosticsFactory(a.store.Dir)
	}
	return diagnostics.New(a.store.Dir)
}

func (a *Agent) installDiagnosticsLocked(manager *diagnostics.Manager) {
	manager.SetUploader(a.uploadDiagnostics)
	if a.identity.DeviceToken != "" {
		manager.SetOwnerSince(a.identity.ConnectorID, a.identity.TenantID, diagnosticIdentityTime(a.store))
	} else if a.state.Current() == state.Revoked {
		manager.ClearOwner()
	}
	applyDiagnosticApprovals(manager, a.config.Sources)
	a.diagMgr = manager
}

func (a *Agent) startDiagnostics() {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.closed {
		return
	}
	if a.diagMgr != nil {
		a.startDiagnosticManagerLocked()
	} else if a.diagnosticsBusy {
		a.diagnosticsRetryDone = make(chan struct{})
		go a.recoverDiagnostics(a.runtimeCtx, a.diagnosticsRetryDone)
	}
}

func (a *Agent) startDiagnosticManagerLocked() {
	a.diagMgr.Record(diagnostics.Record{EventCode: "connector_started"})
	a.diagMgr.Start()
}

// recoverDiagnostics retests a nonblocking lock with bounded backoff. A
// second serving process can hold the lock indefinitely without blocking
// this process's startup, local requests, or shutdown.
func (a *Agent) recoverDiagnostics(ctx context.Context, done chan<- struct{}) {
	defer close(done)
	const maxBackoff = 2 * time.Second
	for delay := 50 * time.Millisecond; ctx.Err() == nil; delay = min(delay*2, maxBackoff) {
		timer := time.NewTimer(delay)
		select {
		case <-ctx.Done():
			timer.Stop()
			return
		case <-timer.C:
		}
		lock, err := diagnostics.LockReporting(a.store.Dir)
		if errors.Is(err, diagnostics.ErrReportingBusy) {
			continue
		}
		if err != nil {
			return
		}
		// A binding or revocation that ran while the lock was busy must
		// finish before we pick the owner of the reloaded queue.
		if !a.connectMu.TryLock() {
			lock.Close()
			continue
		}
		a.restoreDiagnostics(lock)
		a.connectMu.Unlock()
		return
	}
}

func (a *Agent) restoreDiagnostics(lock *os.File) {
	defer func() {
		if lock != nil {
			lock.Close()
		}
	}()
	if a.isClosed() || !a.diagnosticIdentityStillCurrent() {
		return
	}
	manager, err := a.openDiagnostics()
	if err != nil {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.closed {
		return
	}
	a.installDiagnosticsLocked(manager)
	a.diagnosticsLock, lock = lock, nil
	a.startDiagnosticManagerLocked()
}

// A long-running lock holder might itself be another service. A waiting
// instance must never take over its queue using a stale local binding.
// The caller holds connectMu so binding changes in this instance are complete.
func (a *Agent) diagnosticIdentityStillCurrent() bool {
	a.mu.RLock()
	identity := a.identity
	revoked := a.state.Current() == state.Revoked
	a.mu.RUnlock()
	if revoked {
		return identity.DeviceToken == ""
	}
	if a.store.Exists(fileRevoked) {
		return false
	}
	var saved cloud.Identity
	err := a.store.ReadProtectedJSON(fileIdentity, &saved)
	if errors.Is(err, fs.ErrNotExist) {
		return identity.DeviceToken == ""
	}
	return err == nil && saved.DeviceToken == identity.DeviceToken &&
		saved.ConnectorID == identity.ConnectorID && saved.TenantID == identity.TenantID &&
		saved.DeviceKeyID == identity.DeviceKeyID
}

func (a *Agent) closeDiagnostics(ctx context.Context) {
	a.mu.Lock()
	done := a.diagnosticsRetryDone
	manager, lock := a.diagMgr, a.diagnosticsLock
	a.diagMgr, a.diagnosticsLock = nil, nil
	a.mu.Unlock()
	if done != nil {
		select {
		case <-done:
		case <-ctx.Done():
		}
	}
	if manager != nil {
		manager.Record(diagnostics.Record{EventCode: "connector_stopped"})
		manager.Close()
	}
	if lock != nil {
		lock.Close()
	}
}

func (a *Agent) recordDiagnostic(record diagnostics.Record) diagnostics.Event {
	if a == nil {
		return diagnostics.Event{}
	}
	a.mu.RLock()
	defer a.mu.RUnlock()
	if a.diagMgr == nil {
		return diagnostics.Event{}
	}
	return a.diagMgr.Record(record)
}

// recordOperationDiagnostic keeps only metadata allowed by the event catalog.
// Error strings may contain the local URL, network details, or MCP content.
func (a *Agent) recordOperationDiagnostic(code, operationID, sourceID string, started time.Time, err error) {
	record := diagnostics.Record{
		EventCode:   code,
		OperationID: operationID,
		SourceID:    sourceID,
		DurationMS:  diagnosticDuration(started),
	}
	var httpErr interface{ HTTPStatusCode() int }
	if errors.As(err, &httpErr) {
		record.HTTPStatus = httpErr.HTTPStatusCode()
	}
	var cloudErr *cloud.APIError
	if errors.As(err, &cloudErr) {
		record.TraceID = cloudErr.TraceID
	}
	var rpcErr *mcp.RPCError
	if errors.As(err, &rpcErr) {
		record.RPCErrorCode = strconv.Itoa(rpcErr.Code)
	}
	a.recordDiagnostic(record)
}

func (a *Agent) syncDiagnosticApprovals() {
	a.mu.RLock()
	defer a.mu.RUnlock()
	if a.diagMgr != nil {
		applyDiagnosticApprovals(a.diagMgr, a.config.Sources)
	}
}

func applyDiagnosticApprovals(manager *diagnostics.Manager, sources []config.Source) {
	for _, source := range sources {
		manager.SetApprovedTools(source.SourceID, source.ToolAllowlist)
	}
}

func (a *Agent) uploadDiagnostics(ctx context.Context, ownerID string, events []diagnostics.Event) error {
	a.mu.RLock()
	client := a.cloud
	connectorID := a.identity.ConnectorID
	hasIdentity := a.identity.DeviceToken != ""
	registered := make(map[string]map[string]bool, len(a.registeredSources))
	for sourceID, tools := range a.registeredSources {
		registered[sourceID] = tools
	}
	a.mu.RUnlock()
	if client == nil || !hasIdentity || connectorID == "" {
		return errors.New("diagnostic upload is not configured")
	}
	if connectorID != ownerID {
		return errors.New("diagnostic owner changed")
	}
	for index := range events {
		tools, ok := registered[events[index].SourceID]
		if !ok {
			events[index].SourceID = ""
			events[index].ToolName = ""
			continue
		}
		if events[index].ToolName != "" && !tools[events[index].ToolName] {
			events[index].ToolName = ""
		}
	}
	return client.UploadDiagnosticEvents(ctx, ownerID, events)
}

func (a *Agent) markSourceRegistered(sourceID string, tools []string) {
	allowed := make(map[string]bool, len(tools))
	for _, tool := range tools {
		if tool != "" {
			allowed[tool] = true
		}
	}
	a.mu.Lock()
	if a.registeredSources == nil {
		a.registeredSources = map[string]map[string]bool{}
	}
	a.registeredSources[sourceID] = allowed
	a.mu.Unlock()
}

// DiagnosticLogs returns only local catalog-controlled connector events.
func (a *Agent) DiagnosticLogs(query diagnostics.Query) []diagnostics.View {
	if a == nil {
		return nil
	}
	a.mu.RLock()
	defer a.mu.RUnlock()
	if a.diagMgr == nil {
		return nil
	}
	return a.diagMgr.Logs(query)
}

// DiagnosticStats exposes queue and storage health without error text.
func (a *Agent) DiagnosticStats() diagnostics.Stats {
	if a == nil {
		return diagnostics.Stats{}
	}
	a.mu.RLock()
	defer a.mu.RUnlock()
	return a.diagnosticStatsLocked()
}

func (a *Agent) diagnosticStatsLocked() diagnostics.Stats {
	if a.diagMgr == nil {
		return diagnostics.Stats{StorageErrorCode: "LOCAL_STORAGE_FAILED"}
	}
	return a.diagMgr.Stats()
}

// SetDiagnosticsPaused changes remote reporting only. Local record and every
// MCP path continue independently.
func (a *Agent) SetDiagnosticsPaused(paused bool) diagnostics.Stats {
	if a == nil {
		return diagnostics.Stats{}
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.diagMgr == nil || a.closed {
		return a.diagnosticStatsLocked()
	}
	before := a.diagMgr.Stats()
	a.diagMgr.SetPaused(paused)
	if before.Paused != paused {
		code := "diagnostics_resumed"
		if paused {
			code = "diagnostics_paused"
		}
		a.diagMgr.Record(diagnostics.Record{EventCode: code})
	}
	return a.diagMgr.Stats()
}

// PlatformDiagnosticLogs queries the authenticated cloud endpoint on behalf of
// the local browser. Device credentials never leave the connector process.
func (a *Agent) PlatformDiagnosticLogs(ctx context.Context, limit int) ([]diagnostics.PlatformView, string) {
	a.mu.RLock()
	client := a.cloud
	connectorID := a.identity.ConnectorID
	hasIdentity := a.identity.DeviceToken != ""
	a.mu.RUnlock()
	if client == nil || !hasIdentity || connectorID == "" {
		return nil, "NOT_BOUND"
	}
	items, err := client.PlatformDiagnosticEvents(ctx, connectorID, limit)
	if err != nil {
		return nil, diagnostics.CodeForError(err)
	}
	return diagnostics.SafePlatformViews(items), ""
}

func diagnosticIdentityTime(st *store.Store) time.Time {
	if info, err := os.Lstat(st.Path(fileIdentity)); err == nil && info.Mode().IsRegular() {
		return info.ModTime().UTC()
	}
	return time.Time{}
}

// ReportInstall works with a stopped service and an existing, active
// device identity. It doesn't start the tunnel or enroll a device.
func ReportInstall(ctx context.Context, opts Options, attemptID string) (int, error) {
	if attemptID == "" {
		return 0, errors.New("请用 --attempt 指定安装编号")
	}
	st := &store.Store{Dir: opts.DataDir}
	lock, err := diagnostics.LockReporting(st.Dir)
	if err != nil {
		return 0, err
	}
	defer lock.Close()

	if _, err := os.Lstat(st.Path(fileRevoked)); !errors.Is(err, fs.ErrNotExist) {
		return 0, errors.New("设备已撤销或无法核对本机撤销记录")
	}
	var saved persistedState
	if err := st.ReadJSON(fileState, &saved); (err != nil && !errors.Is(err, fs.ErrNotExist)) || saved.State == state.Revoked {
		return 0, errors.New("设备已撤销或无法核对本机状态")
	}
	var identity cloud.Identity
	if err := st.ReadProtectedJSON(fileIdentity, &identity); err != nil ||
		identity.DeviceToken == "" || identity.ConnectorID == "" || identity.TenantID == "" {
		return 0, errors.New("本机没有已绑定的设备身份，请将安装编号和脱敏记录交给技术支持")
	}
	key, err := devicekey.Load(st.Path(fileDeviceKey))
	if err != nil || identity.DeviceKeyID != key.KeyID() {
		return 0, errors.New("本机设备签名密钥不可用")
	}
	var cfg config.Connector
	if err := st.ReadJSON(fileConfig, &cfg); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return 0, errors.New("无法读取现有连接器配置")
	}
	base := cfg.CloudBaseURL
	if base == "" {
		base = opts.CloudBase
	}
	if !endpointConfigured(base) {
		return 0, errors.New("云端控制面地址未配置")
	}
	client, err := cloud.NewWithSigner(base, identity.DeviceToken, key)
	if err != nil {
		return 0, err
	}
	return diagnostics.ReportInstall(ctx, st.Dir, attemptID, identity.ConnectorID, identity.TenantID,
		diagnosticIdentityTime(st), func(ctx context.Context, connectorID string, events []diagnostics.Event) error {
			return client.UploadDiagnosticEvents(ctx, connectorID, events)
		})
}

func diagnosticDuration(start time.Time) int64 {
	if start.IsZero() {
		return 0
	}
	return time.Since(start).Milliseconds()
}

func (a *Agent) reportAuthProxyCall(observation authproxy.Observation) {
	code := "platform_request_succeeded"
	if observation.Failed {
		code = "platform_request_failed"
	}
	if observation.Platform {
		a.recordDiagnostic(diagnostics.Record{
			EventCode:    code,
			TraceID:      observation.TraceID,
			SourceID:     observation.SourceID,
			ToolName:     observation.ToolName,
			HTTPStatus:   observation.HTTPStatus,
			RPCErrorCode: observation.RPCErrorCode,
			DurationMS:   diagnosticDuration(observation.StartedAt),
		})
	}
	if observation.Method == "tools/call" {
		mcpCode := "mcp_request_succeeded"
		if observation.Failed {
			mcpCode = "mcp_request_failed"
		}
		a.recordDiagnostic(diagnostics.Record{
			EventCode:    mcpCode,
			TraceID:      observation.TraceID,
			SourceID:     observation.SourceID,
			ToolName:     observation.ToolName,
			HTTPStatus:   observation.HTTPStatus,
			RPCErrorCode: observation.RPCErrorCode,
			DurationMS:   diagnosticDuration(observation.StartedAt),
		})
	}
}
