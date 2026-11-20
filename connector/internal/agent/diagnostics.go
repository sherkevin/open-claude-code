package agent

import (
	"context"
	"errors"
	"io/fs"
	"os"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/authproxy"
	"github.com/sherkevin/open-claude-code/connector/internal/cloud"
	"github.com/sherkevin/open-claude-code/connector/internal/config"
	"github.com/sherkevin/open-claude-code/connector/internal/devicekey"
	"github.com/sherkevin/open-claude-code/connector/internal/diagnostics"
	"github.com/sherkevin/open-claude-code/connector/internal/state"
	"github.com/sherkevin/open-claude-code/connector/internal/store"
)

func (a *Agent) recordDiagnostic(record diagnostics.Record) diagnostics.Event {
	if a == nil || a.diagMgr == nil {
		return diagnostics.Event{}
	}
	return a.diagMgr.Record(record)
}

// StartDiagnostics starts durable persistence and background upload. It is
// separate from Agent construction so CLI and unit-test callers control it.
func (a *Agent) StartDiagnostics() {
	if a != nil && a.diagMgr != nil {
		a.diagMgr.Start()
	}
}

// RecordStartup writes the connector lifecycle event for the serving process.
func (a *Agent) RecordStartup() {
	a.recordDiagnostic(diagnostics.Record{EventCode: "connector_started"})
}

func (a *Agent) syncDiagnosticApprovals() {
	a.mu.RLock()
	sources := append([]config.Source(nil), a.config.Sources...)
	a.mu.RUnlock()
	a.applyDiagnosticApprovals(sources)
}

func (a *Agent) applyDiagnosticApprovals(sources []config.Source) {
	if a == nil || a.diagMgr == nil {
		return
	}
	for _, source := range sources {
		a.diagMgr.SetApprovedTools(source.SourceID, source.ToolAllowlist)
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
	if a == nil || a.diagMgr == nil {
		return nil
	}
	return a.diagMgr.Logs(query)
}

// DiagnosticStats exposes queue and storage health without error text.
func (a *Agent) DiagnosticStats() diagnostics.Stats {
	if a == nil {
		return diagnostics.Stats{}
	}
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
	if a.diagMgr == nil {
		return a.DiagnosticStats()
	}
	before := a.diagMgr.Stats()
	a.diagMgr.SetPaused(paused)
	if before.Paused != paused {
		code := "diagnostics_resumed"
		if paused {
			code = "diagnostics_paused"
		}
		a.recordDiagnostic(diagnostics.Record{EventCode: code})
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

func (a *Agent) bindDiagnosticsOwner(identity cloud.Identity) {
	if a == nil || a.diagMgr == nil {
		return
	}
	a.diagMgr.SetOwnerSince(identity.ConnectorID, identity.TenantID, diagnosticIdentityTime(a.store))
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

func (a *Agent) clearDiagnosticsOwner() {
	if a == nil || a.diagMgr == nil {
		return
	}
	a.diagMgr.ClearOwner()
}

func diagnosticDuration(start time.Time) int64 {
	if start.IsZero() {
		return 0
	}
	return time.Since(start).Milliseconds()
}

func (a *Agent) reportAuthProxyCall(observation authproxy.Observation) {
	if a == nil || a.diagMgr == nil {
		return
	}
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
