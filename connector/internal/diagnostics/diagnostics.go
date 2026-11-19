// Package diagnostics implements the connector's privacy-bounded operational
// event catalog, local log, durable outbox and background uploader.
package diagnostics

import (
	"context"
	"crypto/rand"
	"crypto/tls"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"math/big"
	"net"
	"net/http"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/store"
)

const (
	SchemaVersion = 1

	fileDiagnostics = "diagnostics.json"

	DefaultMaxQueueEvents = 5000
	DefaultMaxQueueBytes  = 5 << 20
	DefaultMaxLogEvents   = 10000
	DefaultMaxLogBytes    = 10 << 20
	DefaultBatchSize      = 100
	defaultFlushInterval  = 2 * time.Second
	defaultUploadTimeout  = 3 * time.Second
	defaultPersistDelay   = 20 * time.Millisecond
	maxRetryDelay         = 60 * time.Second
)

var (
	eventIDRE     = regexp.MustCompile(`^evt_[0-9a-f]{32}$`)
	operationIDRE = regexp.MustCompile(`^op_[0-9a-f]{16,64}$`)
	traceIDRE     = regexp.MustCompile(`^kwtr_[0-9a-f]{24,64}$`)
	sourceIDRE    = regexp.MustCompile(`^src_[0-9a-f]{32}$`)
	toolNameRE    = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,128}$`)
)

// Level is the fixed severity vocabulary accepted by the local and cloud APIs.
type Level string

const (
	LevelInfo  Level = "info"
	LevelWarn  Level = "warn"
	LevelError Level = "error"
)

// Component identifies the connector subsystem that emitted an event.
type Component string

const (
	ComponentConnector   Component = "connector"
	ComponentConfig      Component = "config"
	ComponentSource      Component = "source"
	ComponentAuth        Component = "auth"
	ComponentDiagnosis   Component = "diagnosis"
	ComponentTunnel      Component = "tunnel"
	ComponentMCP         Component = "mcp"
	ComponentPlatform    Component = "platform"
	ComponentDiagnostics Component = "diagnostics"
)

// Event is the complete wire and persistence contract. There is deliberately
// no free-form details/message/error field.
type Event struct {
	SchemaVersion int       `json:"schema_version"`
	EventID       string    `json:"event_id"`
	OccurredAt    time.Time `json:"occurred_at"`
	Level         Level     `json:"level"`
	Component     Component `json:"component"`
	EventCode     string    `json:"event_code"`
	MessageCode   string    `json:"message_code"`
	OperationID   string    `json:"operation_id,omitempty"`
	TraceID       string    `json:"trace_id,omitempty"`
	SourceID      string    `json:"source_id,omitempty"`
	ToolName      string    `json:"tool_name,omitempty"`
	HTTPStatus    int       `json:"http_status,omitempty"`
	RPCErrorCode  string    `json:"rpc_error_code,omitempty"`
	DurationMS    int64     `json:"duration_ms,omitempty"`
	Attempt       int       `json:"attempt,omitempty"`
}

// View is returned to local callers with catalog-controlled text only.
type View struct {
	Event
	Message string `json:"message"`
}

// Record is the internal input after sensitive values have been discarded.
type Record struct {
	Level        Level
	Component    Component
	EventCode    string
	OperationID  string
	TraceID      string
	SourceID     string
	ToolName     string
	HTTPStatus   int
	RPCErrorCode string
	DurationMS   int64
	Attempt      int
}

// Query only narrows an already-authorized local log view.
type Query struct {
	Level     Level
	Component Component
	SourceID  string
	TraceID   string
	Limit     int
}

// Stats is the non-secret health view shown in the local page.
type Stats struct {
	Paused               bool       `json:"paused"`
	LastSuccessAt        *time.Time `json:"last_success_at,omitempty"`
	LastErrorCode        string     `json:"last_error_code,omitempty"`
	StorageErrorCode     string     `json:"storage_error_code,omitempty"`
	PendingEvents        int        `json:"pending_events"`
	PendingBytes         int        `json:"pending_bytes"`
	PendingDurableEvents int        `json:"pending_durable_events"`
	DroppedEvents        int64      `json:"dropped_events"`
	LocalEventCount      int        `json:"local_event_count"`
	LocalEventBytes      int        `json:"local_event_bytes"`
}

type queuedEvent struct {
	OwnerConnectorID string `json:"owner_connector_id"`
	OwnerTenantID    string `json:"owner_tenant_id"`
	Event            Event  `json:"event"`
}

type persisted struct {
	SchemaVersion    int           `json:"schema_version"`
	OwnerConnectorID string        `json:"owner_connector_id,omitempty"`
	OwnerTenantID    string        `json:"owner_tenant_id,omitempty"`
	Logs             []Event       `json:"logs"`
	Outbox           []queuedEvent `json:"outbox"`
	DroppedEvents    int64         `json:"dropped_events"`
	Paused           bool          `json:"paused"`
	LastSuccessAt    *time.Time    `json:"last_success_at,omitempty"`
	LastErrorCode    string        `json:"last_error_code,omitempty"`
	StorageErrorCode string        `json:"storage_error_code,omitempty"`
}

// Uploader sends one authenticated batch through the existing device signer.
type Uploader func(ctx context.Context, connectorID string, events []Event) error

// Manager is safe for concurrent use by MCP, web and background goroutines.
type Manager struct {
	mu          sync.Mutex
	persistMu   sync.Mutex
	dir         string
	ownerID     string
	ownerTenant string
	logs        []Event
	logSizes    []int
	logBytes    int
	outbox      []queuedEvent
	outboxSize  int
	dropped     int64
	paused      bool
	lastOK      *time.Time
	lastCode    string
	storageCode string
	uploader    Uploader
	approved    map[string]map[string]bool
	durableIDs  map[string]bool

	maxQueueEvents int
	maxQueueBytes  int
	maxLogEvents   int
	maxLogBytes    int
	batchSize      int
	flushInterval  time.Duration
	uploadTimeout  time.Duration
	persistDelay   time.Duration

	wake        chan struct{}
	persistWake chan struct{}
	stop        chan struct{}
	stopOnce    sync.Once
	started     bool
	nextAttempt time.Time
	retryDelay  time.Duration
	wg          sync.WaitGroup
}

// New creates a manager rooted under the existing connector data directory.
func New(dataDir string) (*Manager, error) {
	st, err := store.New(dataDir)
	if err != nil {
		return nil, err
	}
	m := &Manager{
		dir:            st.Dir,
		maxQueueEvents: DefaultMaxQueueEvents,
		maxQueueBytes:  DefaultMaxQueueBytes,
		maxLogEvents:   DefaultMaxLogEvents,
		maxLogBytes:    DefaultMaxLogBytes,
		batchSize:      DefaultBatchSize,
		flushInterval:  defaultFlushInterval,
		uploadTimeout:  defaultUploadTimeout,
		persistDelay:   defaultPersistDelay,
		approved:       map[string]map[string]bool{},
		durableIDs:     map[string]bool{},
		wake:           make(chan struct{}, 1),
		persistWake:    make(chan struct{}, 1),
		stop:           make(chan struct{}),
	}
	if err := m.load(st); err != nil {
		return nil, err
	}
	return m, nil
}

func (m *Manager) load(st *store.Store) error {
	var saved persisted
	if err := st.ReadJSON(fileDiagnostics, &saved); err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil
		}
		_ = st.Remove(fileDiagnostics)
		return nil
	}
	m.ownerID = saved.OwnerConnectorID
	m.ownerTenant = saved.OwnerTenantID
	m.dropped = maxInt64(0, saved.DroppedEvents)
	m.paused = saved.Paused
	m.lastOK = saved.LastSuccessAt
	m.lastCode = safeCode(saved.LastErrorCode)
	m.storageCode = safeCode(saved.StorageErrorCode)
	for _, event := range saved.Logs {
		// Tool approvals are restored by Agent after construction. Persisted
		// tool metadata stays untrusted until that binding exists again.
		event.ToolName = ""
		if Validate(event, nil) != nil {
			m.dropped++
			continue
		}
		raw, _ := json.Marshal(event)
		m.logs = append(m.logs, event)
		m.logSizes = append(m.logSizes, len(raw))
		m.logBytes += len(raw)
		m.durableIDs[event.EventID] = true
	}
	for _, item := range saved.Outbox {
		if item.OwnerConnectorID != m.ownerID || item.OwnerTenantID != m.ownerTenant {
			m.dropped++
			continue
		}
		item.Event.ToolName = ""
		if Validate(item.Event, nil) != nil {
			m.dropped++
			continue
		}
		raw, _ := json.Marshal(item.Event)
		m.outbox = append(m.outbox, item)
		m.outboxSize += len(raw)
		m.durableIDs[item.Event.EventID] = true
	}
	m.trimLogsLocked()
	m.trimQueueLocked()
	return nil
}

// SetApprovedTools binds one source to the exact tool names approved locally.
func (m *Manager) SetApprovedTools(sourceID string, tools []string) {
	sourceID = strings.TrimSpace(sourceID)
	if !sourceIDRE.MatchString(sourceID) {
		return
	}
	allowed := make(map[string]bool, len(tools))
	for _, tool := range tools {
		tool = safeToolName(tool)
		if tool != "" {
			allowed[tool] = true
		}
	}
	m.mu.Lock()
	m.approved[sourceID] = allowed
	changed := false
	for index := range m.logs {
		if m.logs[index].ToolName == "" {
			continue
		}
		if m.logs[index].SourceID != sourceID || !allowed[m.logs[index].ToolName] {
			m.logs[index].ToolName = ""
			changed = true
		}
	}
	for index := range m.outbox {
		if m.outbox[index].Event.ToolName == "" {
			continue
		}
		if m.outbox[index].Event.SourceID != sourceID || !allowed[m.outbox[index].Event.ToolName] {
			m.outbox[index].Event.ToolName = ""
			changed = true
		}
	}
	if changed {
		m.recountLocked()
	}
	m.mu.Unlock()
	if changed {
		m.signalPersist()
	}
}

// SetOwner binds the pending queue and local log to one connector identity.
func (m *Manager) SetOwner(connectorID, tenantID string) {
	connectorID = strings.TrimSpace(connectorID)
	tenantID = strings.TrimSpace(tenantID)
	if connectorID == "" || tenantID == "" {
		return
	}
	m.mu.Lock()
	if m.ownerID != connectorID || m.ownerTenant != tenantID {
		wasBound := m.ownerID != "" || m.ownerTenant != ""
		if len(m.outbox) > 0 {
			m.dropped += int64(len(m.outbox))
		}
		m.outbox = nil
		m.outboxSize = 0
		m.durableIDs = map[string]bool{}
		m.lastOK = nil
		m.lastCode = ""
		m.nextAttempt = time.Time{}
		m.retryDelay = 0
		if wasBound {
			if len(m.logs) > 0 {
				m.dropped += int64(len(m.logs))
			}
			m.logs = nil
			m.logSizes = nil
			m.logBytes = 0
		}
		m.ownerID = connectorID
		m.ownerTenant = tenantID
	}
	m.mu.Unlock()
	m.signalPersist()
	m.signal()
}

// ClearOwner drops the current connector's pending queue and local history so
// a revoked or re-enrolled device never sends the previous tenant's events.
func (m *Manager) ClearOwner() {
	m.mu.Lock()
	if len(m.outbox) > 0 {
		m.dropped += int64(len(m.outbox))
	}
	if len(m.logs) > 0 {
		m.dropped += int64(len(m.logs))
	}
	m.ownerID = ""
	m.ownerTenant = ""
	m.logs = nil
	m.logSizes = nil
	m.logBytes = 0
	m.outbox = nil
	m.outboxSize = 0
	m.durableIDs = map[string]bool{}
	m.lastOK = nil
	m.lastCode = ""
	m.nextAttempt = time.Time{}
	m.retryDelay = 0
	m.mu.Unlock()
	m.signalPersist()
	m.signal()
}

// SetUploader installs the signed control-plane upload function.
func (m *Manager) SetUploader(uploader Uploader) {
	m.mu.Lock()
	m.uploader = uploader
	m.nextAttempt = time.Time{}
	m.retryDelay = 0
	m.mu.Unlock()
	m.signal()
}

// SetPaused controls remote diagnostics only. Local logs and MCP continue.
func (m *Manager) SetPaused(paused bool) {
	m.mu.Lock()
	if m.paused != paused {
		m.paused = paused
		if paused {
			m.nextAttempt = time.Time{}
			m.retryDelay = 0
		}
	}
	m.mu.Unlock()
	m.signalPersist()
	m.signal()
}

// Record validates and stores one event without waiting for disk writes.
func (m *Manager) Record(input Record) Event {
	event := Event{
		SchemaVersion: SchemaVersion,
		EventID:       newID("evt_", 16),
		OccurredAt:    time.Now().UTC(),
		Level:         input.Level,
		Component:     input.Component,
		EventCode:     strings.TrimSpace(input.EventCode),
		OperationID:   safeIdentifier(input.OperationID, operationIDRE),
		TraceID:       safeIdentifier(input.TraceID, traceIDRE),
		SourceID:      safeIdentifier(input.SourceID, sourceIDRE),
		ToolName:      safeToolName(input.ToolName),
		HTTPStatus:    safeHTTPStatus(input.HTTPStatus),
		RPCErrorCode:  safeRPCErrorCode(input.RPCErrorCode),
		DurationMS:    clampInt64(input.DurationMS, 0, 3_600_000),
		Attempt:       int(clampInt64(int64(input.Attempt), 0, 100)),
	}
	if spec, ok := Catalog[event.EventCode]; ok {
		if event.Level == "" {
			event.Level = spec.Level
		}
		if event.Component == "" {
			event.Component = spec.Component
		}
	} else {
		event.Level = LevelError
		event.Component = ComponentConnector
		event.EventCode = "operation_failed"
	}
	event.MessageCode = event.EventCode

	m.mu.Lock()
	if event.ToolName != "" {
		allowed := m.approved[event.SourceID]
		if allowed == nil || !allowed[event.ToolName] {
			event.ToolName = ""
		}
	}
	if err := Validate(event, m.approved[event.SourceID]); err != nil {
		event = Event{
			SchemaVersion: SchemaVersion,
			EventID:       newID("evt_", 16),
			OccurredAt:    time.Now().UTC(),
			Level:         LevelError,
			Component:     ComponentConnector,
			EventCode:     "operation_failed",
			MessageCode:   "operation_failed",
		}
	}
	raw, _ := json.Marshal(event)
	m.logs = append(m.logs, event)
	m.logSizes = append(m.logSizes, len(raw))
	m.logBytes += len(raw)
	m.trimLogsLocked()
	if m.ownerID != "" && m.ownerTenant != "" {
		if len(m.outbox) >= m.maxQueueEvents || m.outboxSize+len(raw) > m.maxQueueBytes {
			m.dropped++
		} else {
			m.outbox = append(m.outbox, queuedEvent{
				OwnerConnectorID: m.ownerID,
				OwnerTenantID:    m.ownerTenant,
				Event:            event,
			})
			m.outboxSize += len(raw)
		}
	}
	m.mu.Unlock()
	m.signalPersist()
	m.signal()
	return event
}

// Logs returns the latest matching local events in chronological order.
func (m *Manager) Logs(q Query) []View {
	if q.Limit <= 0 || q.Limit > DefaultMaxLogEvents {
		q.Limit = 200
	}
	if q.TraceID != "" && !traceIDRE.MatchString(q.TraceID) {
		return nil
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]View, 0, q.Limit)
	for i := len(m.logs) - 1; i >= 0 && len(out) < q.Limit; i-- {
		event := m.logs[i]
		if q.Level != "" && event.Level != q.Level {
			continue
		}
		if q.Component != "" && event.Component != q.Component {
			continue
		}
		if q.SourceID != "" && event.SourceID != q.SourceID {
			continue
		}
		if q.TraceID != "" && event.TraceID != q.TraceID {
			continue
		}
		out = append(out, View{Event: event, Message: Message(event.MessageCode)})
	}
	for left, right := 0, len(out)-1; left < right; left, right = left+1, right-1 {
		out[left], out[right] = out[right], out[left]
	}
	return out
}

// Stats returns queue and uploader health without exposing errors or payloads.
func (m *Manager) Stats() Stats {
	m.mu.Lock()
	defer m.mu.Unlock()
	var last *time.Time
	if m.lastOK != nil {
		copyValue := *m.lastOK
		last = &copyValue
	}
	durable := 0
	for _, item := range m.outbox {
		if m.durableIDs[item.Event.EventID] {
			durable++
		}
	}
	return Stats{
		Paused:               m.paused,
		LastSuccessAt:        last,
		LastErrorCode:        m.lastCode,
		StorageErrorCode:     m.storageCode,
		PendingEvents:        len(m.outbox),
		PendingBytes:         m.outboxSize,
		PendingDurableEvents: durable,
		DroppedEvents:        m.dropped,
		LocalEventCount:      len(m.logs),
		LocalEventBytes:      m.logBytes,
	}
}

// Start launches one uploader and one coalescing persistence worker.
func (m *Manager) Start() {
	m.mu.Lock()
	if m.started {
		m.mu.Unlock()
		return
	}
	m.started = true
	m.wg.Add(2)
	m.mu.Unlock()
	go m.loop()
	go m.persistLoop()
}

// Close stops background work and makes one final persistence attempt.
func (m *Manager) Close() {
	m.stopOnce.Do(func() { close(m.stop) })
	m.wg.Wait()
}

func (m *Manager) loop() {
	defer m.wg.Done()
	for {
		wait, ready := m.uploadSchedule()
		if ready {
			sent, err := m.UploadOnce(context.Background())
			m.mu.Lock()
			pending := len(m.outbox)
			uploaderReady := m.uploader != nil && m.ownerID != "" && !m.paused
			if err != nil && pending > 0 && uploaderReady {
				if m.retryDelay <= 0 {
					m.retryDelay = m.flushInterval
				} else {
					m.retryDelay *= 2
					if m.retryDelay > maxRetryDelay {
						m.retryDelay = maxRetryDelay
					}
				}
				m.nextAttempt = time.Now().Add(delayWithJitter(m.retryDelay))
			} else if pending > 0 && uploaderReady {
				m.retryDelay = 0
				m.nextAttempt = time.Now().Add(10 * time.Millisecond)
			} else {
				m.retryDelay = 0
				m.nextAttempt = time.Time{}
			}
			m.mu.Unlock()
			_ = sent
			continue
		}
		if wait <= 0 {
			wait = time.Hour
		}
		timer := time.NewTimer(wait)
		select {
		case <-m.stop:
			if !timer.Stop() {
				<-timer.C
			}
			return
		case <-m.wake:
			if !timer.Stop() {
				<-timer.C
			}
		case <-timer.C:
		}
	}
}

// uploadSchedule computes one wake-up deadline. New events cannot bypass an
// existing retry deadline.
func (m *Manager) uploadSchedule() (time.Duration, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.paused || m.uploader == nil || m.ownerID == "" || len(m.outbox) == 0 {
		return 0, false
	}
	if m.nextAttempt.IsZero() {
		return 0, true
	}
	wait := time.Until(m.nextAttempt)
	if wait <= 0 {
		return 0, true
	}
	return wait, false
}

// UploadOnce is exported for deterministic tests and operational smoke tests.
func (m *Manager) UploadOnce(ctx context.Context) (int, error) {
	m.mu.Lock()
	uploader := m.uploader
	ownerID := m.ownerID
	paused := m.paused
	batch := make([]Event, 0, m.batchSize)
	if !paused && uploader != nil && ownerID != "" {
		for i := 0; i < len(m.outbox) && len(batch) < m.batchSize; i++ {
			batch = append(batch, m.outbox[i].Event)
		}
	}
	timeout := m.uploadTimeout
	m.mu.Unlock()
	if len(batch) == 0 {
		return 0, nil
	}
	uploadCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	if err := uploader(uploadCtx, ownerID, batch); err != nil {
		m.mu.Lock()
		m.lastCode = CodeForError(err)
		m.mu.Unlock()
		m.signalPersist()
		return 0, err
	}
	m.removeUploaded(batch, ownerID)
	return len(batch), nil
}

func (m *Manager) removeUploaded(batch []Event, ownerID string) {
	ids := make(map[string]bool, len(batch))
	for _, event := range batch {
		ids[event.EventID] = true
	}
	nowValue := time.Now().UTC()
	m.mu.Lock()
	if m.ownerID != ownerID {
		m.mu.Unlock()
		return
	}
	kept := m.outbox[:0]
	newSize := 0
	for _, item := range m.outbox {
		if ids[item.Event.EventID] {
			delete(m.durableIDs, item.Event.EventID)
			continue
		}
		kept = append(kept, item)
		raw, _ := json.Marshal(item.Event)
		newSize += len(raw)
	}
	m.outbox = kept
	m.outboxSize = newSize
	m.lastOK = &nowValue
	m.lastCode = ""
	m.mu.Unlock()
	m.signalPersist()
}

func (m *Manager) trimLogsLocked() {
	for len(m.logs) > m.maxLogEvents || m.logBytes > m.maxLogBytes {
		if len(m.logs) == 0 {
			break
		}
		m.logs = m.logs[1:]
		removed := 0
		if len(m.logSizes) > 0 {
			removed = m.logSizes[0]
			m.logSizes = m.logSizes[1:]
		}
		m.logBytes -= removed
		m.dropped++
	}
	if m.logBytes < 0 {
		m.logBytes = eventBytes(m.logs)
	}
}

func (m *Manager) trimQueueLocked() {
	for len(m.outbox) > m.maxQueueEvents || m.outboxSize > m.maxQueueBytes {
		if len(m.outbox) == 0 {
			break
		}
		raw, _ := json.Marshal(m.outbox[0].Event)
		delete(m.durableIDs, m.outbox[0].Event.EventID)
		m.outboxSize -= len(raw)
		m.outbox = m.outbox[1:]
		m.dropped++
	}
	if m.outboxSize < 0 {
		m.outboxSize = queueBytes(m.outbox)
	}
}

func (m *Manager) recountLocked() {
	m.logBytes = 0
	m.logSizes = m.logSizes[:0]
	for _, event := range m.logs {
		raw, _ := json.Marshal(event)
		m.logSizes = append(m.logSizes, len(raw))
		m.logBytes += len(raw)
	}
	m.outboxSize = queueBytes(m.outbox)
}

func (m *Manager) signalPersist() {
	select {
	case m.persistWake <- struct{}{}:
	default:
	}
}

func (m *Manager) persistLoop() {
	defer m.wg.Done()
	for {
		select {
		case <-m.stop:
			m.persistNow()
			return
		case <-m.persistWake:
			timer := time.NewTimer(m.persistDelay)
			select {
			case <-m.stop:
				if !timer.Stop() {
					<-timer.C
				}
				m.persistNow()
				return
			case <-timer.C:
			}
			if !m.persistNow() {
				select {
				case <-m.stop:
					return
				case <-time.After(time.Second):
					m.signalPersist()
				}
			}
		}
	}
}

func (m *Manager) persistNow() bool {
	m.persistMu.Lock()
	defer m.persistMu.Unlock()
	m.mu.Lock()
	snapshot := persisted{
		SchemaVersion:    SchemaVersion,
		OwnerConnectorID: m.ownerID,
		OwnerTenantID:    m.ownerTenant,
		Logs:             append([]Event(nil), m.logs...),
		Outbox:           append([]queuedEvent(nil), m.outbox...),
		DroppedEvents:    m.dropped,
		Paused:           m.paused,
		LastSuccessAt:    cloneTime(m.lastOK),
		LastErrorCode:    m.lastCode,
		StorageErrorCode: m.storageCode,
	}
	m.mu.Unlock()
	st, err := store.New(m.dir)
	if err == nil {
		err = st.WriteJSON(fileDiagnostics, snapshot)
	}
	m.mu.Lock()
	if err != nil {
		m.storageCode = "LOCAL_STORAGE_FAILED"
		m.mu.Unlock()
		return false
	}
	snapshotIDs := make(map[string]bool, len(snapshot.Outbox))
	for _, item := range snapshot.Outbox {
		snapshotIDs[item.Event.EventID] = true
	}
	durable := make(map[string]bool, len(m.outbox))
	for _, item := range m.outbox {
		if snapshotIDs[item.Event.EventID] {
			durable[item.Event.EventID] = true
		}
	}
	m.durableIDs = durable
	m.storageCode = ""
	m.mu.Unlock()
	return true
}

func (m *Manager) signal() {
	select {
	case m.wake <- struct{}{}:
	default:
	}
}

// Validate rejects unknown fields and unsafe identifiers. ToolName must match
// the exact approved tool set for its source whenever present.
func Validate(event Event, approvedTools map[string]bool) error {
	if event.SchemaVersion != SchemaVersion {
		return errors.New("unsupported schema_version")
	}
	if !eventIDRE.MatchString(event.EventID) {
		return errors.New("invalid event_id")
	}
	if event.OccurredAt.IsZero() || event.OccurredAt.After(time.Now().Add(5*time.Minute)) {
		return errors.New("invalid occurred_at")
	}
	if !validLevel(event.Level) {
		return errors.New("invalid level")
	}
	if !validComponent(event.Component) {
		return errors.New("invalid component")
	}
	if event.EventCode == "" || event.EventCode != event.MessageCode {
		return errors.New("invalid message_code")
	}
	if _, ok := Catalog[event.EventCode]; !ok {
		return errors.New("unknown event_code")
	}
	if event.OperationID != "" && !operationIDRE.MatchString(event.OperationID) {
		return errors.New("invalid operation_id")
	}
	if event.TraceID != "" && !traceIDRE.MatchString(event.TraceID) {
		return errors.New("invalid trace_id")
	}
	if event.SourceID != "" && !sourceIDRE.MatchString(event.SourceID) {
		return errors.New("invalid source_id")
	}
	if event.ToolName != "" {
		if safeToolName(event.ToolName) == "" {
			return errors.New("invalid tool_name")
		}
		if event.SourceID == "" || approvedTools == nil || !approvedTools[event.ToolName] {
			return errors.New("tool_name is not approved")
		}
	}
	if event.HTTPStatus < 0 || event.HTTPStatus > 599 {
		return errors.New("invalid http_status")
	}
	if event.RPCErrorCode != "" && safeRPCErrorCode(event.RPCErrorCode) == "" {
		return errors.New("invalid rpc_error_code")
	}
	if event.DurationMS < 0 || event.DurationMS > 3_600_000 || event.Attempt < 0 || event.Attempt > 100 {
		return errors.New("invalid numeric field")
	}
	return nil
}

// Message returns only catalog-controlled Chinese text.
func Message(code string) string {
	if spec, ok := Catalog[code]; ok {
		return spec.Message
	}
	return "操作失败，请提供事件编号"
}

// NewOperationID creates a non-secret correlation identifier for one operation.
func NewOperationID() string { return newID("op_", 12) }

// NewTraceID is used by Edge and tests. Edge overwrites any external value.
func NewTraceID() string { return newID("kwtr_", 16) }

// CodeForError converts an internal error into a bounded classification.
func CodeForError(err error) string {
	if err == nil {
		return ""
	}
	var statusErr interface{ HTTPStatusCode() int }
	if errors.As(err, &statusErr) {
		switch statusErr.HTTPStatusCode() {
		case http.StatusUnauthorized:
			return "UPLOAD_UNAUTHORIZED"
		case http.StatusForbidden:
			return "UPLOAD_FORBIDDEN"
		case http.StatusNotFound:
			return "UPLOAD_ENDPOINT_NOT_FOUND"
		case http.StatusRequestEntityTooLarge:
			return "UPLOAD_BATCH_TOO_LARGE"
		}
		if statusErr.HTTPStatusCode() >= 500 {
			return "UPLOAD_SERVER_ERROR"
		}
		return "UPLOAD_REJECTED"
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return "UPSTREAM_TIMEOUT"
	}
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return "UPSTREAM_TIMEOUT"
	}
	var certErr *tls.CertificateVerificationError
	if errors.As(err, &certErr) {
		return "TLS_VERIFY_FAILED"
	}
	var dnsErr *net.DNSError
	if errors.As(err, &dnsErr) {
		return "UPSTREAM_DNS_FAILED"
	}
	lower := strings.ToLower(err.Error())
	switch {
	case strings.Contains(lower, "401"), strings.Contains(lower, "unauthorized"):
		return "UPSTREAM_AUTH_FAILED"
	case strings.Contains(lower, "403"), strings.Contains(lower, "forbidden"):
		return "UPSTREAM_FORBIDDEN"
	case strings.Contains(lower, "timeout"), strings.Contains(lower, "timed out"):
		return "UPSTREAM_TIMEOUT"
	case strings.Contains(lower, "refused"), strings.Contains(lower, "unreachable"):
		return "UPSTREAM_UNREACHABLE"
	case strings.Contains(lower, "tls"), strings.Contains(lower, "certificate"):
		return "TLS_VERIFY_FAILED"
	case strings.Contains(lower, "protocol"), strings.Contains(lower, "json-rpc"):
		return "UPSTREAM_PROTOCOL_ERROR"
	default:
		return "OPERATION_FAILED"
	}
}

// SafeDetail returns a fixed customer-facing instruction for a code.
func SafeDetail(code string) string {
	switch code {
	case "UPSTREAM_AUTH_FAILED", "UPSTREAM_FORBIDDEN":
		return "MCP 服务未接受当前访问凭据，请检查访问令牌或动态签名配置"
	case "UPSTREAM_TIMEOUT":
		return "MCP 服务响应超时，请确认服务正在运行且内网网络稳定"
	case "UPSTREAM_UNREACHABLE", "UPSTREAM_DNS_FAILED":
		return "无法连接 MCP 服务，请检查地址、端口和内网网络"
	case "TLS_VERIFY_FAILED":
		return "MCP 服务证书校验失败，请联系开物技术支持检查证书链"
	case "UPSTREAM_PROTOCOL_ERROR":
		return "MCP 服务返回的协议内容无效，请确认该地址是 MCP HTTP 服务"
	case "TOOL_ALLOWLIST_MISMATCH":
		return "已选择的工具与 MCP 服务当前目录不一致，请重新读取工具"
	case "AUTH_PROFILE_INVALID":
		return "本机动态签名配置不完整，请检查服务要求的字段"
	case "POLICY_DENIED":
		return "当前请求不在已允许的工具范围内"
	case "RPC_TOOL_ERROR":
		return "MCP 服务返回了工具执行错误，请查看 MCP 服务日志"
	case "LOCAL_STORAGE_FAILED":
		return "本机诊断日志暂时无法写入磁盘，请联系开物技术支持检查数据目录权限"
	default:
		return "操作失败，请提供事件编号"
	}
}

func safeIdentifier(value string, pattern *regexp.Regexp) string {
	value = strings.TrimSpace(value)
	if value == "" || len(value) > 256 || !pattern.MatchString(value) {
		return ""
	}
	return value
}

func safeToolName(value string) string {
	value = strings.TrimSpace(value)
	if value == "" || !toolNameRE.MatchString(value) {
		return ""
	}
	return value
}

func safeRPCErrorCode(value string) string {
	value = strings.TrimSpace(value)
	if value == "" || len(value) > 6 {
		return ""
	}
	number, err := strconv.Atoi(value)
	if err != nil || number < -32700 || number > -32000 || strconv.Itoa(number) != value {
		return ""
	}
	return value
}

func safeHTTPStatus(value int) int {
	if value < 0 || value > 599 {
		return 0
	}
	return value
}

func safeCode(value string) string {
	if value == "" || len(value) > 64 {
		return ""
	}
	for _, char := range value {
		if (char < 'A' || char > 'Z') && (char < '0' || char > '9') && char != '_' {
			return ""
		}
	}
	return value
}

func validLevel(value Level) bool {
	return value == LevelInfo || value == LevelWarn || value == LevelError
}

func validComponent(value Component) bool {
	switch value {
	case ComponentConnector, ComponentConfig, ComponentSource, ComponentAuth,
		ComponentDiagnosis, ComponentTunnel, ComponentMCP, ComponentPlatform,
		ComponentDiagnostics:
		return true
	default:
		return false
	}
}

func newID(prefix string, bytesLen int) string {
	raw := make([]byte, bytesLen)
	if _, err := rand.Read(raw); err != nil {
		panic(fmt.Sprintf("secure random unavailable: %v", err))
	}
	return prefix + hex.EncodeToString(raw)
}

func eventBytes(events []Event) int {
	total := 0
	for _, event := range events {
		raw, _ := json.Marshal(event)
		total += len(raw)
	}
	return total
}

func queueBytes(items []queuedEvent) int {
	total := 0
	for _, item := range items {
		raw, _ := json.Marshal(item.Event)
		total += len(raw)
	}
	return total
}

func delayWithJitter(base time.Duration) time.Duration {
	if base <= 0 {
		base = time.Second
	}
	next := base
	if next < maxRetryDelay {
		next *= 2
		if next > maxRetryDelay {
			next = maxRetryDelay
		}
	}
	maxJitter := int64(next / 4)
	if maxJitter <= 0 {
		return next
	}
	value, err := rand.Int(rand.Reader, big.NewInt(maxJitter))
	if err != nil {
		return next
	}
	return next + time.Duration(value.Int64())
}

func clampInt64(value, low, high int64) int64 {
	if value < low {
		return low
	}
	if value > high {
		return high
	}
	return value
}

func maxInt64(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}

func cloneTime(value *time.Time) *time.Time {
	if value == nil {
		return nil
	}
	copyValue := *value
	return &copyValue
}

// SortedComponents returns the accepted component vocabulary for docs/tests.
func SortedComponents() []string {
	values := []string{
		string(ComponentAuth), string(ComponentConfig), string(ComponentConnector),
		string(ComponentDiagnostics), string(ComponentDiagnosis), string(ComponentMCP),
		string(ComponentPlatform), string(ComponentSource), string(ComponentTunnel),
	}
	sort.Strings(values)
	return values
}
