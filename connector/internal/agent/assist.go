package agent

import (
	"context"
	"errors"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode"

	"github.com/sherkevin/open-claude-code/connector/internal/cloud"
	"github.com/sherkevin/open-claude-code/connector/internal/config"
	"github.com/sherkevin/open-claude-code/connector/internal/huaweiauth"
	"github.com/sherkevin/open-claude-code/connector/internal/state"
)

const (
	assistLifetime       = 30 * time.Minute
	assistPollInterval   = 3 * time.Second
	assistControlTimeout = 3 * time.Second
	openAssistName       = "内网接入协助"
)

var (
	assistSessionID = regexp.MustCompile("^asst_[0-9a-f]{32}$")
	assistJobID     = regexp.MustCompile("^asjob_[0-9a-f]{32}$")
	assistToolName  = regexp.MustCompile("^[A-Za-z0-9_.:/-]{1,128}$")
	assistBearer    = regexp.MustCompile("^[A-Za-z0-9._~+/-]+=*$")
)

type assistSession struct {
	offer            cloud.AssistOffer
	client           *cloud.Client
	connectorID      string
	cancel           context.CancelFunc
	targetAfterStart bool

	// Guarded by Agent.mu. Keep an attempted target pinned if the server's
	// response was lost, and allow jobs only after checking the response.
	target          cloud.AssistTarget
	targetSetting   bool
	targetConfirmed bool

	// command is the local view of the last command job. It is never restored
	// after a service restart and disappears when assistance stops.
	command *cloud.AssistCommand
}

// AssistStatus returns the current local grant or the server's availability and
// any pending offer. An offline device owner can still end local consent.
func (a *Agent) AssistStatus(ctx context.Context) (cloud.AssistStatus, error) {
	a.mu.RLock()
	if active := a.assistActive; active != nil {
		status := active.localStatus()
		a.mu.RUnlock()
		return status, nil
	}
	client, connectorID := a.cloud, a.identity.ConnectorID
	available := a.canAssistLocked()
	a.mu.RUnlock()
	if !available {
		return cloud.AssistStatus{}, nil
	}
	ctx, cancel := context.WithTimeout(ctx, assistControlTimeout)
	defer cancel()
	status, err := client.AssistStatus(ctx, connectorID)
	if err != nil {
		var apiErr *cloud.APIError
		if errors.As(err, &apiErr) && (apiErr.Status == http.StatusNotFound ||
			apiErr.Code == "ASSIST_DISABLED" || apiErr.Code == "ASSIST_DEVICE_DENIED") {
			return cloud.AssistStatus{}, nil
		}
		return cloud.AssistStatus{}, errors.New("暂时无法读取协助状态")
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if !a.canAssistLocked() || client != a.cloud || connectorID != a.identity.ConnectorID {
		return cloud.AssistStatus{}, nil
	}
	if active := a.assistActive; active != nil {
		return active.localStatus(), nil
	}
	a.assistSeen = nil
	offer := status.Offer
	if offer == nil || (offer.Status != "pending" && offer.Status != "active") || offer.ID == a.assistStopped {
		status.Offer = nil
		return status, nil
	}
	if !validAssistOffer(*offer, time.Now()) {
		return cloud.AssistStatus{}, errors.New("协助请求的 MCP 服务地址无效或已过期")
	}
	offer.LocalActive = false
	seen := *offer
	a.assistSeen = &seen
	return status, nil
}

func (s *assistSession) localOffer() cloud.AssistOffer {
	offer := s.offer
	if s.targetConfirmed {
		offer.Name, offer.URL = s.target.Name, s.target.URL
	}
	offer.Status = "active"
	offer.LocalActive = true
	return offer
}

func (s *assistSession) localStatus() cloud.AssistStatus {
	offer := s.localOffer()
	return cloud.AssistStatus{
		Enabled: true, Offer: &offer, TargetAfterStart: s.targetAfterStart,
		LastCommand: copyAssistCommand(s.command),
	}
}

func copyAssistCommand(command *cloud.AssistCommand) *cloud.AssistCommand {
	if command == nil {
		return nil
	}
	copied := *command
	if command.ExitCode != nil {
		value := *command.ExitCode
		copied.ExitCode = &value
	}
	if command.FinishedAt != nil {
		value := *command.FinishedAt
		copied.FinishedAt = &value
	}
	return &copied
}

func (a *Agent) canAssistLocked() bool {
	current := a.state.Current()
	return !a.closed && a.opts.NativeRelay && a.cloud != nil && a.identity.ConnectorID != "" &&
		a.identity.DeviceToken != "" && current != state.Revoked && current != state.Draining
}

// StartAssistForTarget supports control servers that still require the
// assistance session and the locally chosen MCP address in the same request.
func (a *Agent) StartAssistForTarget(ctx context.Context, target cloud.AssistTarget) (*cloud.AssistOffer, error) {
	target.Name, target.URL = strings.TrimSpace(target.Name), strings.TrimSpace(target.URL)
	if !validAssistTarget(target.Name, target.URL) {
		return nil, errors.New("请填写有效的 MCP 服务名称和地址。内网服务使用 HTTPS，本机服务也可填写端口")
	}
	return a.openAssist(ctx, &target)
}

func (a *Agent) OpenAssist(ctx context.Context) (*cloud.AssistOffer, error) {
	return a.openAssist(ctx, nil)
}

// Local consent is never restored after a service restart.
func (a *Agent) openAssist(ctx context.Context, target *cloud.AssistTarget) (*cloud.AssistOffer, error) {
	a.mu.Lock()
	if !a.canAssistLocked() || a.assistActive != nil || a.assistStarting {
		a.mu.Unlock()
		return nil, errors.New("当前无法开始协助，请确认设备已绑定且没有正在进行的协助")
	}
	client, connectorID := a.cloud, a.identity.ConnectorID
	a.assistStarting = true
	a.mu.Unlock()

	request, done := context.WithTimeout(a.runtimeCtx, assistControlTimeout)
	stopRequest := context.AfterFunc(ctx, done)
	offer, err := client.OpenAssist(request, connectorID, target)
	stopRequest()
	done()

	a.mu.Lock()
	a.assistStarting = false
	if err != nil {
		a.mu.Unlock()
		return nil, assistStartError(err)
	}
	expected := cloud.AssistTarget{Name: openAssistName}
	if target != nil {
		expected = *target
	}
	if offer.Status != "active" || offer.Name != expected.Name || offer.URL != expected.URL ||
		!validAssistOffer(offer, time.Now()) {
		a.mu.Unlock()
		return nil, errors.New("开物返回的协助内容与本机填写的服务不一致，请刷新后重试")
	}
	session := &assistSession{offer: offer, client: client, connectorID: connectorID, targetAfterStart: target == nil}
	if !a.canAssistLocked() || client != a.cloud || connectorID != a.identity.ConnectorID ||
		ctx.Err() != nil || offer.ID == a.assistStopped {
		a.mu.Unlock()
		go a.notifyAssistStop(session)
		return nil, errors.New("开始协助时本机连接状态已变化，请刷新后重试")
	}
	deadline := offer.ExpiresAt
	if localDeadline := time.Now().Add(assistLifetime); localDeadline.Before(deadline) {
		deadline = localDeadline
	}
	sessionCtx, cancel := context.WithDeadline(a.runtimeCtx, deadline)
	session.cancel = cancel
	a.assistSeen = nil
	a.assistActive = session
	view := session.localOffer()
	a.mu.Unlock()
	go a.runAssist(sessionCtx, session)
	return &view, nil
}

// SetAssistTarget grants access to exactly one local MCP service after Start.
func (a *Agent) SetAssistTarget(ctx context.Context, sessionID string, target cloud.AssistTarget) (*cloud.AssistOffer, error) {
	target.Name, target.URL = strings.TrimSpace(target.Name), strings.TrimSpace(target.URL)
	if !validAssistTarget(target.Name, target.URL) {
		return nil, errors.New("请填写有效的 MCP 服务名称和地址。内网服务使用 HTTPS，本机服务也可填写端口")
	}
	a.mu.Lock()
	session := a.assistActive
	if !a.canAssistLocked() || session == nil || session.offer.ID != sessionID ||
		!session.targetAfterStart || !session.offer.ExpiresAt.After(time.Now()) {
		a.mu.Unlock()
		return nil, errors.New("请先在本机开始协助，再确认 MCP")
	}
	if session.target.URL != "" && session.target != target {
		a.mu.Unlock()
		return nil, errors.New("本次协助只能确认一个 MCP。如需换地址，请停止后重新开始协助")
	}
	if session.targetConfirmed {
		view := session.localOffer()
		a.mu.Unlock()
		return &view, nil
	}
	if session.targetSetting {
		a.mu.Unlock()
		return nil, errors.New("正在确认此 MCP，请稍候")
	}
	session.target = target
	session.targetSetting = true
	a.mu.Unlock()

	request, done := context.WithTimeout(a.runtimeCtx, assistControlTimeout)
	stopRequest := context.AfterFunc(ctx, done)
	offer, err := session.client.SetAssistTarget(request, session.connectorID, sessionID, target)
	stopRequest()
	done()

	a.mu.Lock()
	defer a.mu.Unlock()
	session.targetSetting = false
	if a.assistActive != session || !a.canAssistLocked() || a.cloud != session.client ||
		a.identity.ConnectorID != session.connectorID {
		return nil, errors.New("本次协助已结束")
	}
	if err != nil || ctx.Err() != nil {
		return nil, errors.New("开物未确认此 MCP，请检查连接后用同一地址重试")
	}
	if offer.ID != sessionID || offer.Status != "active" || offer.Name != target.Name || offer.URL != target.URL ||
		!offer.ExpiresAt.Equal(session.offer.ExpiresAt) || !validAssistOffer(offer, time.Now()) {
		return nil, errors.New("开物返回的服务与本机所选 MCP 不一致，请核对地址后重试")
	}
	session.targetConfirmed = true
	view := session.localOffer()
	return &view, nil
}

func assistStartError(err error) error {
	var apiErr *cloud.APIError
	if errors.As(err, &apiErr) {
		switch apiErr.Code {
		case "ASSIST_DISABLED", "ASSIST_DEVICE_DENIED":
			return errors.New("这台设备尚未开通协助，请联系开物技术支持")
		}
		if apiErr.Status == http.StatusConflict {
			return errors.New("已有一项协助正在进行，请先停止后重试")
		}
	}
	return errors.New("开物未确认开始协助，请检查连接后重试")
}

// StartAssist requires the local user to approve exactly the offer the UI read.
// Consent lives only in this process and is never restored on service restart.
func (a *Agent) StartAssist(ctx context.Context, sessionID string) error {
	a.mu.Lock()
	offer := a.assistSeen
	if !a.canAssistLocked() || a.assistActive != nil || a.assistStarting ||
		offer == nil || offer.ID != sessionID || sessionID == a.assistStopped ||
		offer.Status != "pending" || !validAssistOffer(*offer, time.Now()) {
		a.mu.Unlock()
		return errors.New("这项协助请求已更新或无法开始，请刷新后重试")
	}
	sessionCtx, cancel := context.WithTimeout(a.runtimeCtx, assistLifetime)
	session := &assistSession{offer: *offer, client: a.cloud, connectorID: a.identity.ConnectorID, cancel: cancel}
	session.offer.ExpiresAt = time.Now().Add(assistLifetime)
	a.assistActive = session
	a.mu.Unlock()

	request, done := context.WithTimeout(sessionCtx, assistControlTimeout)
	stopRequest := context.AfterFunc(ctx, done)
	err := session.client.StartAssist(request, session.connectorID, sessionID)
	stopRequest()
	done()
	if err != nil || sessionCtx.Err() != nil {
		if a.clearAssist(session) {
			a.notifyAssistStop(session)
		}
		return errors.New("开始协助未得到确认，请让开物工程师重新发起")
	}
	go a.runAssist(sessionCtx, session)
	return nil
}

// StopAssist cancels local operations before attempting the cloud notification.
func (a *Agent) StopAssist(ctx context.Context, sessionID string) error {
	a.mu.Lock()
	active := a.assistActive
	var session *assistSession
	switch {
	case active != nil && active.offer.ID == sessionID:
		session = active
		active.cancel()
		a.assistActive = nil
	case a.assistSeen != nil && a.assistSeen.ID == sessionID && a.assistSeen.Status == "active":
		session = &assistSession{offer: *a.assistSeen, client: a.cloud, connectorID: a.identity.ConnectorID}
	case a.assistStopped == sessionID && assistSessionID.MatchString(sessionID):
		a.mu.Unlock()
		return nil
	default:
		a.mu.Unlock()
		return errors.New("没有这项正在进行的协助")
	}
	a.assistSeen = nil
	a.assistStopped = sessionID
	a.mu.Unlock()

	request, cancel := context.WithTimeout(ctx, assistControlTimeout)
	defer cancel()
	if err := session.client.StopAssist(request, session.connectorID, sessionID); err != nil {
		go a.notifyAssistStop(session)
		return errors.New("本机已停止协助，正在重试通知开物")
	}
	return nil
}

func (a *Agent) clearAssist(session *assistSession) bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.assistActive != session {
		return false
	}
	session.cancel()
	a.assistActive = nil
	a.assistSeen = nil
	a.assistStopped = session.offer.ID
	return true
}

func (a *Agent) notifyAssistStop(session *assistSession) {
	request, cancel := context.WithTimeout(a.runtimeCtx, assistControlTimeout)
	defer cancel()
	_ = session.client.StopAssist(request, session.connectorID, session.offer.ID)
}

func (a *Agent) runAssist(ctx context.Context, session *assistSession) {
	defer func() {
		if a.clearAssist(session) {
			a.notifyAssistStop(session)
		}
	}()
	ticker := time.NewTicker(assistPollInterval)
	defer ticker.Stop()
	var draft *SourceInput
	var verifiedTools []string
	completed := make(chan cloud.AssistResult, 1)
	busy := false
	for ctx.Err() == nil {
		a.mu.RLock()
		offer := session.localOffer()
		a.mu.RUnlock()
		request, done := context.WithTimeout(ctx, assistControlTimeout)
		poll, err := session.client.PollAssist(request, session.connectorID, offer.ID, true)
		done()
		if err != nil || !poll.Active || ctx.Err() != nil {
			return
		}
		if job := poll.Job; job != nil {
			if busy || !assistJobID.MatchString(job.ID) {
				return
			}
			busy = true
			go func() {
				completed <- a.runAssistJob(ctx, session, *job, &draft, &verifiedTools)
			}()
		}
		select {
		case <-ctx.Done():
			return
		case result := <-completed:
			busy = false
			request, done := context.WithTimeout(ctx, assistControlTimeout)
			err := session.client.ReportAssist(request, session.connectorID, result)
			done()
			if err != nil {
				return
			}
		case <-ticker.C:
		}
	}
}

func (a *Agent) runAssistJob(ctx context.Context, session *assistSession, job cloud.AssistJob, draft **SourceInput, verifiedTools *[]string) cloud.AssistResult {
	offer := session.offer
	result := cloud.AssistResult{SessionID: offer.ID, JobID: job.ID, Status: "failed", Code: "canceled"}
	if ctx.Err() != nil {
		return result
	}
	switch job.Action {
	case "command":
		result.Code = "command_failed"
		a.setAssistCommand(session, &cloud.AssistCommand{
			ID: job.ID, Command: job.Command, State: "running", StartedAt: time.Now().UTC(),
		})
		executed := a.runAssistCommand(ctx, job.Command, job.TimeoutSeconds)
		finished := time.Now().UTC()
		a.setAssistCommand(session, &cloud.AssistCommand{
			ID: job.ID, Command: job.Command, State: executed.state, Output: executed.output,
			ExitCode: executed.exitCode, Truncated: executed.truncated,
			DurationMS: executed.duration.Milliseconds(), StartedAt: finished.Add(-executed.duration),
			FinishedAt: &finished,
		})
		result.Status, result.Code = executed.status, executed.code
		result.Output, result.ExitCode, result.Truncated = executed.output, executed.exitCode, executed.truncated
		result.DurationMS = executed.duration.Milliseconds()
	case "discover":
		*draft = nil
		*verifiedTools = nil
		result.Code = "upstream_failed"
		result.Stage = "initialize"
		input, err := a.assistSourceInput(offer, job.BearerToken, job.DynamicAuth)
		if err != nil || len(job.Tools) != 0 {
			return result
		}
		var stage sourceDiscovery
		directory, err := a.discoverSourceTools(ctx, input, &stage)
		result.Authorization = stage.authorization
		if stage.stage != "" {
			result.Stage = stage.stage
		}
		if err != nil {
			var upstream interface{ HTTPStatusCode() int }
			if errors.As(err, &upstream) {
				code := upstream.HTTPStatusCode()
				if code >= 300 && code <= 599 {
					result.HTTPStatus = code
				}
			}
			return result
		}
		if len(directory) == 0 {
			result.Code = "no_tools"
			return result
		}
		tools := make([]string, 0, len(directory))
		for _, tool := range directory {
			if !assistToolName.MatchString(tool.Name) {
				return result
			}
			tools = append(tools, tool.Name)
		}
		*draft, *verifiedTools = &input, tools
		result.Code, result.Status, result.Tools = "tools_loaded", "succeeded", tools
	case "connect":
		result.Code, result.Stage = "connect_failed", "connect"
		if *draft == nil || job.BearerToken != "" || job.DynamicAuth != nil || !validAssistSelection(job.Tools, *verifiedTools) ||
			a.state.Current() == state.Paused {
			return result
		}
		input := **draft
		input.Tools = job.Tools
		saved, err := a.SaveSourceAndConnect(ctx, input)
		if err != nil || saved.ConnectionError != "" || !saved.Status.TunnelConnected ||
			saved.Source.UpstreamURL != offer.URL || !isOpaqueSourceID(saved.Source.SourceID) {
			return result
		}
		*draft, *verifiedTools = nil, nil
		result.Code, result.Status, result.SourceID = "connected", "succeeded", saved.Source.SourceID
	}
	return result
}

func (a *Agent) setAssistCommand(session *assistSession, command *cloud.AssistCommand) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.assistActive == session {
		session.command = command
	}
}

func (a *Agent) assistSourceInput(offer cloud.AssistOffer, bearer string, dynamic *huaweiauth.Profile) (SourceInput, error) {
	if len(bearer) > 8192 || (bearer != "" && !assistBearer.MatchString(bearer)) {
		return SourceInput{}, errors.New("访问令牌格式无效")
	}
	a.mu.RLock()
	defer a.mu.RUnlock()
	revision := a.config.Revision
	input := SourceInput{
		DisplayName:    offer.Name,
		UpstreamURL:    offer.URL,
		BearerToken:    bearer,
		assistRevision: &revision,
	}
	for _, source := range a.config.Sources {
		if source.SourceID == a.demoState.SourceID || source.UpstreamURL != offer.URL ||
			source.Transport != config.TransportStreamableHTTP {
			continue
		}
		if input.SourceID != "" || source.Disabled {
			return SourceInput{}, errors.New("本机 MCP 来源有冲突")
		}
		for header := range a.secrets[source.SourceID] {
			if header != "Authorization" {
				return SourceInput{}, errors.New("本机 MCP 来源使用了其他认证头")
			}
		}
		input.SourceID = source.SourceID
		_, input.DynamicAuth = a.profiles[source.SourceID]
	}
	if dynamic != nil {
		profile := *dynamic
		profile.AppID = strings.TrimSpace(profile.AppID)
		if profile.AppID == "" {
			profile.AppID = "MCP"
		}
		profile.Channel = strings.TrimSpace(profile.Channel)
		profile.UserID = strings.TrimSpace(profile.UserID)
		if err := huaweiauth.Validate(profile); err != nil {
			return SourceInput{}, err
		}
		input.DynamicAuth = true
		input.AppID = profile.AppID
		input.Channel = profile.Channel
		input.UserID = profile.UserID
		input.HMACKey = profile.HMACKey
	}
	return input, nil
}

func validAssistSelection(selected, available []string) bool {
	if len(selected) == 0 || len(selected) > 64 {
		return false
	}
	known := make(map[string]bool, len(available))
	for _, tool := range available {
		known[tool] = true
	}
	for _, tool := range selected {
		if !known[tool] {
			return false
		}
		delete(known, tool)
	}
	return true
}

func validAssistOffer(offer cloud.AssistOffer, now time.Time) bool {
	if !assistSessionID.MatchString(offer.ID) || !offer.ExpiresAt.After(now) {
		return false
	}
	return validAssistTarget(offer.Name, offer.URL) ||
		(offer.Status == "active" && offer.Name == openAssistName && offer.URL == "")
}

func validAssistTarget(name, address string) bool {
	if name == "" || len([]rune(name)) > 100 ||
		strings.IndexFunc(name, func(r rune) bool { return unicode.IsControl(r) || unicode.Is(unicode.Cf, r) }) >= 0 ||
		len(address) > 2048 || strings.ContainsAny(address, "?#\\") ||
		strings.IndexFunc(address, func(r rune) bool { return unicode.IsSpace(r) || unicode.IsControl(r) }) >= 0 {
		return false
	}
	target, err := url.Parse(address)
	if err != nil || target.Opaque != "" || target.User != nil || target.Hostname() == "" ||
		target.Path == "" || target.Path == "/" || target.String() != address {
		return false
	}
	if port := target.Port(); port != "" {
		value, err := strconv.Atoi(port)
		if err != nil || value < 1 || value > 65535 {
			return false
		}
	}
	host := strings.ToLower(target.Hostname())
	if ip := net.ParseIP(host); ip != nil && (ip.IsUnspecified() || ip.IsMulticast() || ip.IsLinkLocalUnicast()) {
		return false
	}
	return target.Scheme == "https" || (target.Scheme == "http" &&
		(host == "localhost" || host == "127.0.0.1" || host == "::1"))
}
