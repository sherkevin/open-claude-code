package diagnostics

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"
)

const (
	installJournalDir = "logs/install-attempts"
	maxInstallFiles   = 128
	maxInstallBytes   = 4096
	maxInstallLine    = 256
	maxInstallSeq     = 48
	installScanPeriod = 5 * time.Second
)

var (
	installIDRE       = regexp.MustCompile(`^[0-9a-f]{32}$`)
	installEventCodes = map[string]string{
		"started": "install_stage_started",
		"ok":      "install_stage_succeeded",
		"failed":  "install_stage_failed",
	}
	installStages = map[string]bool{
		"started": true, "precheck": true, "extract": true, "lock": true, "backup": true,
		"replace": true, "service": true, "verify": true, "rollback": true, "finish": true,
	}
	installCodes = map[string]bool{
		"ok": true, "unknown": true, "unsupported_host": true, "busy": true,
		"file_missing": true, "checksum_mismatch": true, "access_denied": true,
		"service_registration": true, "service_start": true, "local_page_unready": true,
		"write_failed": true, "restored": true, "recovery_unconfirmed": true,
	}
)

var (
	errInstallIneligible       = errors.New("找不到可归属当前设备的安装记录")
	errInstallQueueUnavailable = errors.New("本机诊断队列暂时无法接收该安装记录，请稍后重试")
)

type installCheckpoint struct {
	Created time.Time `json:"created"`
	Seq     int       `json:"seq"`
}

type installLine struct {
	version int
	seq     int
	stage   string
	outcome string
	code    string
	ts      string
}

func parseInstallLine(raw []byte) (installLine, bool) {
	var line installLine
	if len(raw) > maxInstallLine {
		return line, false
	}
	for _, b := range raw {
		if b > 127 {
			return line, false
		}
	}

	fields := map[string]any{
		"v": &line.version, "seq": &line.seq, "stage": &line.stage,
		"outcome": &line.outcome, "code": &line.code, "ts": &line.ts,
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	if open, err := decoder.Token(); err != nil || open != json.Delim('{') {
		return line, false
	}
	for decoder.More() {
		key, err := decoder.Token()
		if err != nil {
			return line, false
		}
		name, ok := key.(string)
		target := fields[name]
		if !ok || target == nil || decoder.Decode(target) != nil {
			return line, false
		}
		delete(fields, name)
	}
	if closeToken, err := decoder.Token(); err != nil || closeToken != json.Delim('}') {
		return line, false
	}
	if _, err := decoder.Token(); err != io.EOF {
		return line, false
	}
	if _, omitted := fields["ts"]; !omitted {
		if _, err := time.Parse(time.RFC3339Nano, line.ts); err != nil || !strings.HasSuffix(line.ts, "Z") {
			return line, false
		}
	}
	return line, line.version == 1 && line.seq > 0 && line.seq <= maxInstallSeq &&
		installStages[line.stage] && installEventCodes[line.outcome] != "" && installCodes[line.code]
}

func installEventID(id string, seq int) string {
	// Replaying a journal yields the same receiver idempotency key.
	digest := sha256.Sum256([]byte("kaiwu-install-v1/" + id + "/" + strconv.Itoa(seq)))
	return "evt_" + hex.EncodeToString(digest[:16])
}

func (line installLine) event(id string, created time.Time) Event {
	code := installEventCodes[line.outcome]
	spec := Catalog[code]
	occurred := created
	if ts, err := time.Parse(time.RFC3339Nano, line.ts); err == nil &&
		!ts.Before(created) && !ts.After(time.Now().Add(5*time.Minute)) {
		occurred = ts
	}
	return Event{
		SchemaVersion: SchemaVersion, EventID: installEventID(id, line.seq),
		OccurredAt: occurred.UTC(), Level: spec.Level, Component: spec.Component,
		EventCode: code, MessageCode: code, InstallAttemptID: id, InstallSeq: line.seq,
		InstallStage: line.stage, InstallOutcome: line.outcome, InstallCode: line.code,
	}
}

func validateInstallEvent(event Event) bool {
	spec := Catalog[event.EventCode]
	if spec.Component != ComponentInstall && event.Component != ComponentInstall {
		return event.InstallAttemptID == "" && event.InstallSeq == 0 &&
			event.InstallStage == "" && event.InstallOutcome == "" && event.InstallCode == ""
	}
	return event.Component == spec.Component && event.Level == spec.Level &&
		installIDRE.MatchString(event.InstallAttemptID) && event.InstallSeq > 0 &&
		event.InstallSeq <= maxInstallSeq && event.EventID == installEventID(event.InstallAttemptID, event.InstallSeq) &&
		installStages[event.InstallStage] &&
		installEventCodes[event.InstallOutcome] == event.EventCode && installCodes[event.InstallCode] &&
		event.OperationID == "" && event.TraceID == "" && event.SourceID == "" && event.ToolName == "" &&
		event.HTTPStatus == 0 && event.RPCErrorCode == "" && event.DurationMS == 0 && event.Attempt == 0
}

type installOwner struct {
	connector string
	tenant    string
	since     time.Time
}

func (m *Manager) installOwnerLocked() installOwner {
	return installOwner{m.ownerID, m.ownerTenant, m.installSince}
}

func (m *Manager) scanInstallJournals() {
	m.mu.Lock()
	owner := m.installOwnerLocked()
	m.mu.Unlock()
	if owner.since.IsZero() {
		return
	}
	journals, err := openInstallDirectory(m.dir)
	if err != nil {
		return
	}
	defer journals.Close()
	dir, err := journals.Open(".")
	if err != nil {
		return
	}
	defer dir.Close()
	entries, err := dir.ReadDir(maxInstallFiles + 1)
	if (err != nil && !errors.Is(err, io.EOF)) || len(entries) > maxInstallFiles {
		return
	}

	// A removed journal no longer needs a checkpoint. An open or read error
	// for a still-present name cannot reset its sequence.
	present := make(map[string]bool, len(entries))
	for _, entry := range entries {
		id, ok := strings.CutSuffix(entry.Name(), ".jsonl")
		if ok && installIDRE.MatchString(id) {
			present[id] = true
		}
	}
	m.mu.Lock()
	if m.installOwnerLocked() == owner {
		for id := range m.installCheckpoints {
			if !present[id] {
				delete(m.installCheckpoints, id)
				m.signalPersist()
			}
		}
	}
	m.mu.Unlock()

	for _, entry := range entries {
		select {
		case <-m.stop:
			return
		default:
		}
		id, _ := strings.CutSuffix(entry.Name(), ".jsonl")
		if present[id] && entry.Type().IsRegular() {
			m.readInstallJournal(journals, entry.Name(), id, owner)
		}
	}
}

func openInstallDirectory(dataDir string) (*os.Root, error) {
	root, err := os.OpenRoot(dataDir)
	if err != nil {
		return nil, err
	}
	defer root.Close()
	for _, name := range []string{".", "logs", installJournalDir} {
		info, err := root.Lstat(name)
		if err != nil || info.Mode().Type() != fs.ModeDir {
			return nil, errors.New("unsafe installation journal directory")
		}
		handle, err := root.Open(name)
		if err != nil {
			return nil, err
		}
		verified := installDirectoryTrusted(handle, info)
		handle.Close()
		if !verified {
			return nil, errors.New("unsafe installation journal directory")
		}
	}
	return root.OpenRoot(installJournalDir)
}

func (m *Manager) readInstallJournal(root *os.Root, name, id string, owner installOwner) error {
	before, err := root.Lstat(name)
	if err != nil || !before.Mode().IsRegular() {
		return errInstallIneligible
	}
	file, err := root.OpenFile(name, installReadFlags, 0)
	if err != nil {
		return errInstallIneligible
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() || !os.SameFile(before, info) {
		return errInstallIneligible
	}
	created := installCreated(file).UTC()
	if !created.After(owner.since) || created.After(time.Now()) {
		return errInstallIneligible
	}
	raw, err := io.ReadAll(io.LimitReader(file, maxInstallBytes))
	if err != nil {
		return errInstallIneligible
	}
	// A writer may be appending the final line. Only newline-terminated
	// records can advance the persistent cursor.
	for len(raw) != 0 {
		line, remainder, complete := bytes.Cut(raw, []byte("\n"))
		if !complete {
			break
		}
		raw = remainder
		if parsed, ok := parseInstallLine(line); ok && !m.enqueueInstallEvent(parsed.event(id, created), created, owner) {
			return errInstallQueueUnavailable
		}
	}
	return nil
}

func (m *Manager) enqueueInstallEvent(event Event, created time.Time, owner installOwner) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.installOwnerLocked() != owner {
		return false
	}
	saved, known := m.installCheckpoints[event.InstallAttemptID]
	if known && !saved.Created.Equal(created) {
		return false
	}
	if !known && len(m.installCheckpoints) >= maxInstallFiles {
		return false
	}
	if event.InstallSeq <= saved.Seq {
		return true
	}
	raw, _ := json.Marshal(event)
	if len(m.outbox) >= m.maxQueueEvents || m.outboxSize+len(raw) > m.maxQueueBytes {
		return false // Leave the cursor at the last enqueued event for retry.
	}
	m.appendEventLocked(event, len(raw))
	m.installCheckpoints[event.InstallAttemptID] = installCheckpoint{Created: created, Seq: event.InstallSeq}
	m.signalPersist()
	return true
}

func (m *Manager) installJournalLoop() {
	defer m.wg.Done()
	ticker := time.NewTicker(installScanPeriod)
	defer ticker.Stop()
	for {
		m.scanInstallJournals()
		select {
		case <-m.stop:
			return
		case <-m.installWake:
		case <-ticker.C:
		}
	}
}
