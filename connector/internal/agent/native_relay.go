package agent

import (
	"context"
	"errors"
	"fmt"
	"math/rand/v2"
	"net/http"
	"slices"
	"time"

	"github.com/coder/websocket"
	"github.com/sherkevin/open-claude-code/connector/internal/authproxy"
	"github.com/sherkevin/open-claude-code/connector/internal/cloud"
	"github.com/sherkevin/open-claude-code/connector/internal/config"
	"github.com/sherkevin/open-claude-code/connector/internal/diagnostics"
	"github.com/sherkevin/open-claude-code/connector/internal/nativerelay"
	"github.com/sherkevin/open-claude-code/connector/internal/state"
)

const nativeRelayHandshakeTimeout = 10 * time.Second

// All fields except cancel are guarded by Agent.mu. A session owns its local
// approved routes across transport reconnects, but never replays a request.
type nativeRelay struct {
	cancel    context.CancelFunc
	connected bool
	restarts  int
	routes    map[string]*authproxy.Server
	// Empty means the normal connection with all enabled sources.
	demoID string
}

func (a *Agent) establishNativeRelay(ctx context.Context, client *cloud.Client, connectorID string, sources []config.Source, proxies map[string]*authproxy.Server, demoOnly bool, cleanup func()) (Status, error) {
	success := false
	defer func() {
		if !success {
			cleanup()
		}
	}()

	demoID := ""
	if demoOnly {
		demo, err := a.builtinDemoSource()
		if err != nil {
			return a.statusSnapshot(), err
		}
		demoID = demo.SourceID
		sources = []config.Source{demo}
	}
	a.mu.RLock()
	previous := a.native
	reuse := demoOnly && previous != nil && previous.connected
	builtinID := a.demoState.SourceID
	a.mu.RUnlock()
	if demoOnly && previous != nil && !reuse {
		return a.statusSnapshot(), errors.New("原生中继正在重连，请稍后重试")
	}

	var socket *websocket.Conn
	if !reuse {
		a.retireNativeRelay()
		var err error
		socket, err = openNativeRelay(ctx, client, connectorID)
		if err != nil {
			if disposition := queryNativeDisposition(ctx, client, connectorID); disposition != "" {
				return a.applyNativeDisposition(ctx, connectorID, disposition)
			}
			return a.nativeRelayFailure(demoOnly, state.DegradedRelay, err)
		}
		defer func() {
			if !success {
				_ = socket.CloseNow()
			}
		}()
	}

	// The signed upgrade marks this server-side device as native before source
	// registration can allocate any legacy FRP visitor ports.
	if err := a.registerSources(ctx, sources, proxies); err != nil {
		if cloudRevokedDevice(err) {
			return a.revokeFromCloud(ctx, connectorID)
		}
		return a.nativeRelayFailure(demoOnly, state.DegradedRelay, err)
	}
	desired, err := client.DesiredState(ctx, connectorID)
	if disposition := nativeDisposition(desired, err); disposition != "" {
		return a.applyNativeDisposition(ctx, connectorID, disposition)
	}
	if err != nil {
		return a.nativeRelayFailure(demoOnly, state.DegradedRelay, errors.New("获取原生中继来源状态失败"))
	}
	routes, err := nativeRoutes(sources, proxies, desired, builtinID)
	if err != nil {
		return a.nativeRelayFailure(demoOnly, state.DegradedRelay, err)
	}
	reports, err := a.Diagnose(ctx, demoID, "")
	if err != nil || !allOK(reports) {
		return a.nativeRelayFailure(demoOnly, state.DegradedUpstream, errors.New("内网来源检查未通过"))
	}

	a.lifecycleMu.Lock()
	defer a.lifecycleMu.Unlock()
	if err := ctx.Err(); err != nil {
		return a.statusSnapshot(), err
	}
	a.mu.Lock()
	if a.closed {
		a.mu.Unlock()
		return a.statusSnapshot(), errAgentClosed
	}
	if reuse {
		if a.native != previous || !previous.connected {
			a.mu.Unlock()
			return a.statusSnapshot(), errors.New("原生中继正在重连，请稍后重试")
		}
		previous.routes[demoID] = routes[demoID]
		a.mu.Unlock()
		success = true
		return a.statusSnapshot(), nil
	}
	runtimeCtx, stop := context.WithCancel(a.runtimeCtx)
	session := &nativeRelay{cancel: stop, connected: true, routes: routes, demoID: demoID}
	a.native = session
	var previousProxies map[string]*authproxy.Server
	if !demoOnly {
		previousProxies = a.proxies
		a.proxies = proxies
		a.preparedProxies = nil
	}
	a.mu.Unlock()
	if demoOnly && !state.CanTransition(a.state.Current(), state.Ready) && state.CanTransition(a.state.Current(), state.Connecting) {
		a.state.Transition(state.Connecting, "开始建立原生中继")
	}
	a.state.Transition(state.Ready, "原生中继已建立")
	go a.runNativeRelay(runtimeCtx, session, client, connectorID, socket)
	a.startHeartbeat(nil)
	closeProxies(previousProxies)
	success = true
	return a.statusSnapshot(), nil
}

func (a *Agent) nativeRelayFailure(demoOnly bool, target state.State, err error) (Status, error) {
	if !demoOnly {
		a.state.Transition(target, err.Error())
	}
	return a.statusSnapshot(), err
}

func nativeRoutes(sources []config.Source, proxies map[string]*authproxy.Server, desired cloud.DesiredState, demoID string) (map[string]*authproxy.Server, error) {
	byID := make(map[string]cloud.DesiredSource, len(desired.Sources))
	for _, item := range desired.Sources {
		byID[item.SourceID] = item
	}
	routes := make(map[string]*authproxy.Server, len(sources))
	for _, source := range sources {
		approved := byID[source.SourceID]
		proxy := proxies[source.SourceID]
		if !nativeApprovalMatches(source, approved, proxy, demoID) {
			return nil, fmt.Errorf("来源 %s 的云端授权与本机配置不一致", source.DisplayName)
		}
		routes[source.SourceID] = proxy
	}
	return routes, nil
}

func nativeApprovalMatches(source config.Source, approved cloud.DesiredSource, proxy *authproxy.Server, demoID string) bool {
	if source.Disabled || proxy == nil || approved.SourceID != source.SourceID || source.Transport != config.TransportStreamableHTTP || approved.Transport != source.Transport || approved.TargetBindingSHA256 != sourceBindingDigest(source, demoID) {
		return false
	}
	tools, err := normalizeTools(approved.AllowedTools)
	return err == nil && slices.Equal(tools, proxy.AllowedTools())
}

func openNativeRelay(ctx context.Context, client *cloud.Client, connectorID string) (*websocket.Conn, error) {
	ctx, cancel := context.WithTimeout(ctx, nativeRelayHandshakeTimeout)
	defer cancel()
	socket, err := client.DialNativeRelay(ctx, connectorID)
	if err != nil {
		return nil, err
	}
	if err := nativerelay.AwaitReady(ctx, socket); err != nil {
		_ = socket.CloseNow()
		return nil, err
	}
	return socket, nil
}

func (a *Agent) retireNativeRelay() {
	a.lifecycleMu.Lock()
	defer a.lifecycleMu.Unlock()
	a.mu.Lock()
	old := a.native
	stopHeartbeat := a.heartbeat
	a.native = nil
	a.heartbeat = nil
	a.mu.Unlock()
	if old != nil {
		old.cancel()
	}
	if stopHeartbeat != nil {
		stopHeartbeat()
	}
}

// Persisted connected intent is retried across temporary boot-time network
// outages. A new explicit selection always supersedes these retries.
func (a *Agent) autoConnectNativeRelay() {
	a.mu.RLock()
	ctx, revision := a.runtimeCtx, a.tunnelRevision
	a.mu.RUnlock()
	for delay := time.Second; ; delay = min(delay*2, 20*time.Second) {
		attemptCtx, cancel := context.WithTimeout(ctx, startupConnectTimeout)
		status, err := a.runConnect(attemptCtx, false, true)
		cancel()
		if err == nil || ctx.Err() != nil || status.State == state.Draining || status.State == state.Revoked {
			return
		}
		a.mu.Lock()
		if a.tunnelRevision != revision || a.closed {
			a.mu.Unlock()
			return
		}
		a.startupConnectPending = true
		a.mu.Unlock()
		if !waitNativeRetry(ctx, delay) {
			return
		}
	}
}

func (a *Agent) runNativeRelay(ctx context.Context, session *nativeRelay, client *cloud.Client, connectorID string, socket *websocket.Conn) {
	lookup := func(sourceID string) http.Handler {
		a.mu.RLock()
		defer a.mu.RUnlock()
		if a.native != session || !session.connected || (sourceID == a.demoState.SourceID && !a.builtinDemoRuntimeReadyLocked()) {
			return nil
		}
		if proxy := session.routes[sourceID]; proxy != nil {
			return proxy
		}
		return nil
	}
	for {
		_ = nativerelay.Serve(ctx, socket, lookup)
		if ctx.Err() != nil || !a.setNativeConnectivity(session, false) {
			return
		}
		delay := time.Second
		for {
			if !waitNativeRetry(ctx, delay) {
				return
			}
			delay = min(delay*2, 20*time.Second)
			attemptCtx, cancel := context.WithTimeout(ctx, nativeRelayHandshakeTimeout)
			desired, err := client.DesiredState(attemptCtx, connectorID)
			if disposition := nativeDisposition(desired, err); disposition != "" {
				cancel()
				a.finishNativeDisposition(ctx, session, connectorID, disposition)
				return
			}
			if err == nil && a.nativeRoutesStillApproved(session, desired) {
				socket, err = openNativeRelay(attemptCtx, client, connectorID)
				if err == nil {
					cancel()
					if ctx.Err() != nil || !a.setNativeConnectivity(session, true) {
						_ = socket.CloseNow()
						return
					}
					break
				}
			}
			cancel()
		}
	}
}

func waitNativeRetry(ctx context.Context, delay time.Duration) bool {
	jitter := time.Duration(rand.Int64N(int64(delay / 4)))
	timer := time.NewTimer(delay + jitter)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}

func (a *Agent) nativeRoutesStillApproved(session *nativeRelay, desired cloud.DesiredState) bool {
	a.mu.RLock()
	defer a.mu.RUnlock()
	if a.native != session {
		return false
	}
	for id, proxy := range session.routes {
		position := slices.IndexFunc(a.config.Sources, func(item config.Source) bool { return item.SourceID == id })
		if position < 0 {
			return false
		}
		source := a.config.Sources[position]
		if source.Disabled && id == a.demoState.SourceID {
			continue
		}
		index := slices.IndexFunc(desired.Sources, func(item cloud.DesiredSource) bool { return item.SourceID == id })
		if index < 0 || !nativeApprovalMatches(source, desired.Sources[index], proxy, a.demoState.SourceID) {
			return false
		}
	}
	return true
}

func (a *Agent) setNativeConnectivity(session *nativeRelay, isConnected bool) bool {
	a.lifecycleMu.Lock()
	defer a.lifecycleMu.Unlock()
	a.mu.Lock()
	if a.native != session || a.closed {
		a.mu.Unlock()
		return false
	}
	session.connected = isConnected
	if isConnected {
		session.restarts++
	}
	a.mu.Unlock()
	if isConnected {
		a.state.Transition(state.Ready, "原生中继已恢复")
		a.recordDiagnostic(diagnostics.Record{EventCode: "connection_succeeded"})
	} else {
		a.state.Transition(state.DegradedRelay, "原生中继已断开，正在重连")
		a.recordDiagnostic(diagnostics.Record{EventCode: "connection_failed"})
	}
	return true
}

func nativeDisposition(desired cloud.DesiredState, err error) state.State {
	if cloudRevokedDevice(err) || desired.Revoked {
		return state.Revoked
	}
	if desired.Draining {
		return state.Draining
	}
	return ""
}

func queryNativeDisposition(ctx context.Context, client *cloud.Client, connectorID string) state.State {
	queryCtx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	desired, err := client.DesiredState(queryCtx, connectorID)
	return nativeDisposition(desired, err)
}

func (a *Agent) applyNativeDisposition(ctx context.Context, connectorID string, disposition state.State) (Status, error) {
	if disposition == state.Revoked {
		return a.revokeFromCloud(ctx, connectorID)
	}
	return a.drainTunnel(ctx)
}

func (a *Agent) finishNativeDisposition(ctx context.Context, session *nativeRelay, connectorID string, disposition state.State) {
	a.connectMu.Lock()
	defer a.connectMu.Unlock()
	a.mu.RLock()
	current := a.native == session && !a.closed
	a.mu.RUnlock()
	if current && ctx.Err() == nil {
		_, _ = a.applyNativeDisposition(ctx, connectorID, disposition)
	}
}
