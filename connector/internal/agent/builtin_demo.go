package agent

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"reflect"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/config"
	"github.com/sherkevin/open-claude-code/connector/internal/demomcp"
)

const (
	fileBuiltinDemo            = "builtin_demo.json"
	builtinDemoProtocolProfile = "kaiwu_builtin_demo_v1"
)

type builtinDemoState struct {
	SourceID string `json:"builtin_demo_source_id,omitempty"`
	Disabled bool   `json:"disabled,omitempty"`
}

// BuiltinDemoStatus is the non-secret runtime view shown by the local UI.
type BuiltinDemoStatus struct {
	Enabled   bool     `json:"enabled"`
	Running   bool     `json:"running"`
	SourceID  string   `json:"source_id,omitempty"`
	Tools     []string `json:"tools"`
	ErrorCode string   `json:"error_code,omitempty"`
}

func builtinDemoSourceID(connectorID string) string {
	sum := sha256.Sum256([]byte("kaiwu_builtin_demo_v1:" + connectorID))
	return "src_" + hex.EncodeToString(sum[:])[:32]
}

func (a *Agent) builtinDemoSourceIndexLocked() int {
	for index, source := range a.config.Sources {
		if a.demoState.SourceID != "" && source.SourceID == a.demoState.SourceID {
			return index
		}
	}
	return -1
}

func (a *Agent) isTrackedBuiltinDemoSourceID(sourceID string) bool {
	if a == nil || sourceID == "" {
		return false
	}
	a.mu.RLock()
	defer a.mu.RUnlock()
	return a.demoState.SourceID != "" && sourceID == a.demoState.SourceID
}

func (a *Agent) builtinDemoSource() (config.Source, error) {
	a.mu.RLock()
	defer a.mu.RUnlock()
	index := a.builtinDemoSourceIndexLocked()
	if index < 0 {
		if a.demoErrorCode == "BUILTIN_SOURCE_CONFLICT" {
			return config.Source{}, errors.New("内置测试来源的固定编号已被其他来源占用")
		}
		return config.Source{}, errors.New("内置测试来源尚未准备好")
	}
	return a.config.Sources[index], nil
}

func (a *Agent) businessSourcesLocked() []config.Source {
	out := make([]config.Source, 0, len(a.config.Sources))
	for _, source := range a.config.Sources {
		if a.demoState.SourceID != "" && source.SourceID == a.demoState.SourceID {
			continue
		}
		out = append(out, source)
	}
	return out
}

func (a *Agent) validateBuiltinDemoIdentity(connectorID string) error {
	a.mu.RLock()
	defer a.mu.RUnlock()
	if a.demoState.SourceID == "" {
		return nil
	}
	targetID := builtinDemoSourceID(connectorID)
	trackedExists := false
	for _, source := range a.config.Sources {
		if source.SourceID == a.demoState.SourceID {
			trackedExists = true
		}
		if source.SourceID != targetID {
			continue
		}
		if source.SourceID != a.demoState.SourceID {
			return errors.New("内置测试来源的固定编号已被其他来源占用")
		}
		return nil
	}
	if !trackedExists && len(a.config.Sources) >= 16 {
		return errors.New("来源数量已达到上限，无法迁移内置测试来源")
	}
	return nil
}

// StartBuiltinDemo starts the bundled loopback MCP unless the operator
// previously turned it off. Failure is reflected in Status but never closes
// the customer's MCP paths.
func (a *Agent) StartBuiltinDemo() error {
	if a == nil {
		return errors.New("connector is nil")
	}
	a.lifecycleMu.Lock()
	defer a.lifecycleMu.Unlock()
	if a.isClosed() {
		return errAgentClosed
	}
	a.mu.RLock()
	disabled := a.demoState.Disabled
	connectorID := a.identity.ConnectorID
	server := a.demoServer
	a.mu.RUnlock()
	if disabled {
		return nil
	}
	if server == nil {
		server = demomcp.New()
		a.mu.Lock()
		if a.demoServer == nil {
			a.demoServer = server
		} else {
			server = a.demoServer
		}
		a.mu.Unlock()
	}
	if err := server.Start(); err != nil {
		a.setBuiltinDemoError("LOCAL_LISTENER_FAILED")
		return err
	}
	if a.isClosed() {
		a.stopBuiltinDemoServer()
		return errAgentClosed
	}
	if err := a.syncBuiltinDemoSource(connectorID, server.Endpoint()); err != nil {
		return err
	}
	a.setBuiltinDemoError("")
	return nil
}

// SetBuiltinDemoEnabled persists the operator choice independently from all
// customer sources. Disabling the demo does not pause or edit any business
// source and does not stop an already running tunnel.
func (a *Agent) SetBuiltinDemoEnabled(enabled bool) (Status, error) {
	if a == nil {
		return Status{}, errors.New("connector is nil")
	}
	if a.isClosed() {
		return a.statusSnapshot(), errAgentClosed
	}
	a.mu.Lock()
	previous := a.demoState.Disabled
	a.demoState.Disabled = !enabled
	connectorID := a.identity.ConnectorID
	a.mu.Unlock()

	if enabled {
		if err := a.StartBuiltinDemo(); err != nil {
			a.mu.Lock()
			a.demoState.Disabled = previous
			a.mu.Unlock()
			return a.Status(), err
		}
	} else {
		a.mu.RLock()
		server := a.demoServer
		hasSource := a.builtinDemoSourceIndexLocked() >= 0
		a.mu.RUnlock()
		endpoint := ""
		if server != nil {
			endpoint = server.Endpoint()
		}
		if hasSource {
			if err := a.syncBuiltinDemoSource(connectorID, endpoint); err != nil {
				a.mu.Lock()
				a.demoState.Disabled = previous
				a.mu.Unlock()
				return a.Status(), err
			}
		}
	}
	if err := a.persistBuiltinDemoState(); err != nil {
		a.mu.Lock()
		a.demoState.Disabled = previous
		a.mu.Unlock()
		_ = a.syncBuiltinDemoSource(connectorID, a.currentDemoEndpoint())
		if previous {
			a.stopBuiltinDemoServer()
		}
		return a.Status(), err
	}
	if !enabled {
		a.stopBuiltinDemoServer()
	}
	return a.Status(), nil
}

func (a *Agent) currentDemoEndpoint() string {
	a.mu.RLock()
	server := a.demoServer
	a.mu.RUnlock()
	if server == nil {
		return ""
	}
	return server.Endpoint()
}

func (a *Agent) StopBuiltinDemo() {
	if a == nil {
		return
	}
	a.stopBuiltinDemoServer()
}

func (a *Agent) stopBuiltinDemoServer() {
	a.mu.RLock()
	server := a.demoServer
	a.mu.RUnlock()
	if server == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	_ = server.Stop(ctx)
}

func (a *Agent) builtinDemoStatusLocked() BuiltinDemoStatus {
	status := BuiltinDemoStatus{
		Enabled:   !a.demoState.Disabled,
		SourceID:  a.demoState.SourceID,
		Tools:     append([]string(nil), demomcp.Tools...),
		ErrorCode: a.demoErrorCode,
	}
	if index := a.builtinDemoSourceIndexLocked(); index >= 0 {
		status.SourceID = a.config.Sources[index].SourceID
	}
	if a.demoServer != nil {
		status.Running = a.demoServer.Running()
	}
	return status
}

func (a *Agent) builtinDemoRuntimeReadyLocked() bool {
	return !a.demoState.Disabled && a.demoServer != nil && a.demoServer.Running()
}

func (a *Agent) runtimeSourcesLocked() []config.Source {
	sources := enabledSources(a.config.Sources)
	if a.builtinDemoRuntimeReadyLocked() {
		return sources
	}
	out := make([]config.Source, 0, len(sources))
	for _, source := range sources {
		if a.demoState.SourceID != "" && source.SourceID == a.demoState.SourceID {
			continue
		}
		out = append(out, source)
	}
	return out
}

func (a *Agent) setBuiltinDemoError(code string) {
	a.mu.Lock()
	a.demoErrorCode = safeDemoErrorCode(code)
	a.mu.Unlock()
}

func safeDemoErrorCode(code string) string {
	switch code {
	case "", "LOCAL_LISTENER_FAILED", "BUILTIN_SOURCE_FAILED", "BUILTIN_SOURCE_CONFLICT", "SOURCE_LIMIT_REACHED":
		return code
	default:
		return "BUILTIN_DEMO_FAILED"
	}
}

func (a *Agent) persistBuiltinDemoState() error {
	a.mu.RLock()
	state := a.demoState
	a.mu.RUnlock()
	return a.store.WriteJSON(fileBuiltinDemo, state)
}

// syncBuiltinDemoSource keeps exactly one built-in source in the local config.
// It preserves every business source and refuses to overwrite a source whose
// fixed ID is not the already tracked demo source.
func (a *Agent) syncBuiltinDemoSource(connectorID, endpoint string) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.closed {
		return errAgentClosed
	}

	targetID := ""
	switch {
	case connectorID != "":
		targetID = builtinDemoSourceID(connectorID)
	case a.demoState.SourceID != "":
		targetID = a.demoState.SourceID
	default:
		generated, err := newSourceID()
		if err != nil {
			a.demoErrorCode = "BUILTIN_SOURCE_FAILED"
			return err
		}
		targetID = generated
	}

	currentIndex := -1
	for index, source := range a.config.Sources {
		if a.demoState.SourceID != "" && source.SourceID == a.demoState.SourceID {
			currentIndex = index
			break
		}
	}

	targetIndex := -1
	for index, source := range a.config.Sources {
		if source.SourceID == targetID {
			targetIndex = index
			break
		}
	}
	if targetIndex >= 0 && targetIndex != currentIndex {
		a.demoErrorCode = "BUILTIN_SOURCE_CONFLICT"
		return errors.New("内置测试来源的固定编号已被其他来源占用")
	}
	if currentIndex < 0 && len(a.config.Sources) >= 16 {
		a.demoErrorCode = "SOURCE_LIMIT_REACHED"
		return errors.New("来源数量已达到上限，无法追加内置测试来源")
	}

	nextSources := append([]config.Source(nil), a.config.Sources...)
	demo := config.Source{
		SchemaVersion:   config.CurrentSchemaVersion,
		SourceID:        targetID,
		DisplayName:     demomcp.SourceName,
		Transport:       config.TransportStreamableHTTP,
		UpstreamURL:     endpoint,
		ProtocolProfile: builtinDemoProtocolProfile,
		Auth: config.Auth{
			Mode:               config.AuthModeNone,
			CredentialLocation: config.CredentialLocationLocal,
		},
		ToolAllowlist: append([]string(nil), demomcp.Tools...),
		Disabled:      a.demoState.Disabled,
	}
	if currentIndex >= 0 {
		existing := nextSources[currentIndex]
		if demo.UpstreamURL == "" {
			demo.UpstreamURL = existing.UpstreamURL
		}
		nextSources[currentIndex] = demo
	} else {
		if demo.UpstreamURL == "" {
			a.demoErrorCode = "BUILTIN_SOURCE_FAILED"
			return errors.New("内置测试服务尚未启动")
		}
		nextSources = append(nextSources, demo)
	}
	oldID := a.demoState.SourceID
	configChanged := !reflect.DeepEqual(a.config.Sources, nextSources)
	if configChanged {
		next := a.config
		next.Revision++
		next.Sources = nextSources
		if err := a.store.WriteJSON(fileConfig, next); err != nil {
			a.demoErrorCode = "BUILTIN_SOURCE_FAILED"
			return err
		}
		a.config = next
	}
	if oldID != "" && oldID != targetID {
		if tools, ok := a.registeredSources[oldID]; ok {
			delete(a.registeredSources, oldID)
			a.registeredSources[targetID] = tools
		}
		delete(a.secrets, oldID)
		delete(a.profiles, oldID)
	}
	a.demoState.SourceID = targetID
	if oldID != targetID {
		if err := a.store.WriteJSON(fileBuiltinDemo, a.demoState); err != nil {
			a.demoErrorCode = "BUILTIN_SOURCE_FAILED"
			return err
		}
	}
	a.demoErrorCode = ""
	return nil
}

// migrateBuiltinDemoForIdentity updates only the tracked demo source ID. It
// deliberately fails instead of taking over an unrelated source at the
// deterministic ID.
func (a *Agent) migrateBuiltinDemoForIdentity(connectorID string) error {
	a.mu.RLock()
	server := a.demoServer
	state := a.demoState
	a.mu.RUnlock()
	if state.SourceID == "" {
		return nil
	}
	endpoint := ""
	if server != nil {
		endpoint = server.Endpoint()
	}
	return a.syncBuiltinDemoSource(connectorID, endpoint)
}
