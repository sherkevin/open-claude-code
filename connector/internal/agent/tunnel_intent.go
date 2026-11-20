package agent

import (
	"context"
	"errors"
	"fmt"
	"net/http"

	"github.com/sherkevin/open-claude-code/connector/internal/cloud"
	"github.com/sherkevin/open-claude-code/connector/internal/diagnostics"
	"github.com/sherkevin/open-claude-code/connector/internal/state"
)

type tunnelIntent string

const (
	tunnelConnected    tunnelIntent = "connected"
	tunnelPaused       tunnelIntent = "paused"
	tunnelDisconnected tunnelIntent = "disconnected"
)

// State keeps the record readable by older connectors. New connectors use the
// explicit choice across startups and derive the current health in memory.
type persistedState struct {
	State  state.State  `json:"state"`
	Intent tunnelIntent `json:"tunnel_intent,omitempty"`
}

func (s persistedState) tunnelIntent() tunnelIntent {
	switch s.Intent {
	case tunnelConnected, tunnelPaused, tunnelDisconnected:
		return s.Intent
	case "":
		switch s.State {
		case state.Ready, state.Connecting, state.DegradedRelay, state.DegradedUpstream:
			return tunnelConnected
		case state.Paused:
			return tunnelPaused
		}
	}
	return tunnelDisconnected
}

// saveTunnelChoice commits an explicit selection before acknowledging it.
// Runtime transitions, including startup, never overwrite this record.
func (a *Agent) saveTunnelChoice(intent tunnelIntent, legacyState state.State) error {
	if err := a.store.WriteJSON(fileState, persistedState{State: legacyState, Intent: intent}); err != nil {
		return fmt.Errorf("无法保存通道选择: %w", err)
	}
	return nil
}

func (a *Agent) shouldAutoConnectLocked() bool {
	return a.startupConnectPending && !a.closed && a.identity.DeviceToken != "" &&
		hasEnabledSource(a.config.Sources)
}

// ShouldAutoConnect reports whether a startup recovery attempt is pending.
func (a *Agent) ShouldAutoConnect() bool {
	a.mu.RLock()
	defer a.mu.RUnlock()
	return a.shouldAutoConnectLocked()
}

// AutoConnect attempts to restore a previously selected connection once.
func (a *Agent) AutoConnect() {
	ctx, cancel := context.WithTimeout(context.Background(), startupConnectTimeout)
	defer cancel()
	_, _ = a.runConnect(ctx, false, true)
}

var errTunnelSelectionSuperseded = errors.New("通道操作已被更新的用户选择取消")

// supersedeTunnelOperationLocked is called under mu before an explicit
// choice waits for connectMu. This lets Pause cancel a cloud request in flight,
// and prevents a queued startup worker from undoing the user's choice.
func (a *Agent) supersedeTunnelOperationLocked() {
	a.startupConnectPending = false
	a.tunnelRevision++
	if a.connectCancel != nil {
		a.connectCancel()
	}
}

// beginTunnelChoice serializes resource changes after announcing the choice.
func (a *Agent) beginTunnelChoice() (func(), error) {
	a.mu.Lock()
	a.supersedeTunnelOperationLocked()
	revision := a.tunnelRevision
	a.mu.Unlock()

	a.connectMu.Lock()
	a.mu.RLock()
	defer a.mu.RUnlock()
	if revision != a.tunnelRevision {
		a.connectMu.Unlock()
		return nil, errTunnelSelectionSuperseded
	}
	if a.closed {
		a.connectMu.Unlock()
		return nil, errAgentClosed
	}
	return a.connectMu.Unlock, nil
}

func (a *Agent) runConnect(ctx context.Context, demoOnly, automatic bool) (Status, error) {
	a.mu.Lock()
	switch {
	case automatic:
		if !a.shouldAutoConnectLocked() {
			status := a.statusLocked()
			a.mu.Unlock()
			return status, nil
		}
		a.startupConnectPending = false
	case !demoOnly:
		a.supersedeTunnelOperationLocked()
	}
	revision := a.tunnelRevision
	a.mu.Unlock()

	a.connectMu.Lock()
	defer a.connectMu.Unlock()
	ctx, release, err := a.beginRuntimeOperation(ctx)
	if err != nil {
		return a.statusSnapshot(), err
	}
	defer release()

	a.mu.Lock()
	if revision != a.tunnelRevision {
		status := a.statusLocked()
		a.mu.Unlock()
		return status, errTunnelSelectionSuperseded
	}
	a.connectCancel = release
	a.mu.Unlock()
	defer func() {
		a.mu.Lock()
		a.connectCancel = nil
		a.mu.Unlock()
	}()

	if err := ctx.Err(); err != nil {
		return a.statusSnapshot(), err
	}
	return a.establishTunnel(ctx, demoOnly, automatic)
}

// Pause immediately stops the tunnel and preserves the choice across startups.
func (a *Agent) Pause(ctx context.Context) (Status, error) {
	return a.stopByChoice(ctx, tunnelPaused, state.Paused, "本机暂停", "connection_paused")
}

// Disconnect stops the tunnel and returns to configuration.
func (a *Agent) Disconnect(ctx context.Context) (Status, error) {
	return a.stopByChoice(ctx, tunnelDisconnected, state.Configuring, "通道已断开", "connection_disconnected")
}

func (a *Agent) stopByChoice(ctx context.Context, intent tunnelIntent, target state.State, reason, event string) (Status, error) {
	unlock, err := a.beginTunnelChoice()
	if err != nil {
		return a.statusSnapshot(), err
	}
	defer unlock()

	current := a.state.Current()
	if current == state.Unpaired || current == state.Revoked {
		err := a.stopTunnel(ctx)
		return a.statusSnapshot(), err
	}
	saveErr := a.saveTunnelChoice(intent, target)
	stopErr := a.stopTunnel(ctx)
	// Moving from Ready or Degraded back to Configuring goes through Paused.
	a.state.Transition(state.Paused, reason)
	a.state.Transition(target, reason)
	a.recordDiagnostic(diagnostics.Record{EventCode: event})
	return a.statusSnapshot(), errors.Join(saveErr, stopErr)
}

// stopTunnel is called with connectMu held by the current tunnel operation.
func (a *Agent) stopTunnel(ctx context.Context) error {
	a.lifecycleMu.Lock()
	defer a.lifecycleMu.Unlock()

	a.mu.Lock()
	s := a.supervisor
	stopHeartbeat := a.heartbeat
	a.heartbeat = nil
	a.stoppingTunnels++
	a.frpcAdminPort = 0
	a.frpcAdminUser = ""
	a.frpcAdminPassword = ""
	a.frpcUser = ""
	a.frpcProxyCount = 0
	a.mu.Unlock()
	if stopHeartbeat != nil {
		stopHeartbeat()
	}
	var err error
	if s != nil {
		if stopErr := s.Stop(ctx); stopErr != nil {
			err = fmt.Errorf("通道停止尚未确认: %w", stopErr)
		}
	}
	a.stopAuthProxies()
	a.mu.Lock()
	if err == nil {
		a.supervisor = nil
	}
	a.stoppingTunnels--
	a.mu.Unlock()
	return err
}

func cloudRevokedDevice(err error) bool {
	var apiErr *cloud.APIError
	return errors.As(err, &apiErr) && apiErr.Status == http.StatusForbidden && apiErr.Code == "DEVICE_REVOKED"
}

func (a *Agent) revokeFromCloud(ctx context.Context, connectorID string) (Status, error) {
	err := a.revokeLocally(ctx, connectorID, "云端已撤销该连接器")
	return a.statusSnapshot(), errors.Join(errors.New("连接器已被撤销"), err)
}

func (a *Agent) drainTunnel(ctx context.Context) (Status, error) {
	err := a.stopTunnel(ctx)
	a.state.Transition(state.Draining, "云端已将该连接器排空")
	return a.statusSnapshot(), errors.Join(errors.New("云端已将该连接器排空，新的平台调用将被拒绝"), err)
}
