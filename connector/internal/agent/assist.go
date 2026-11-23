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
)

var (
	assistSessionID = regexp.MustCompile("^asst_[0-9a-f]{32}$")
	assistJobID     = regexp.MustCompile("^asjob_[0-9a-f]{32}$")
	assistToolName  = regexp.MustCompile("^[A-Za-z0-9_.:/-]{1,128}$")
	assistBearer    = regexp.MustCompile("^[A-Za-z0-9._~+/-]+=*$")
)

type assistSession struct {
	offer       cloud.AssistOffer
	client      *cloud.Client
	connectorID string
	cancel      context.CancelFunc
}

// AssistOffer returns only the service awaiting approval or the current local
// session. An offline device owner can still see and end local consent.
func (a *Agent) AssistOffer(ctx context.Context) (*cloud.AssistOffer, error) {
	a.mu.RLock()
	if active := a.assistActive; active != nil {
		view := active.offer
		view.Status = "active"
		view.LocalActive = true
		a.mu.RUnlock()
		return &view, nil
	}
	client, connectorID := a.cloud, a.identity.ConnectorID
	available := !a.closed && a.opts.NativeRelay && a.identity.DeviceToken != ""
	a.mu.RUnlock()
	if !available || client == nil || connectorID == "" {
		return nil, nil
	}
	ctx, cancel := context.WithTimeout(ctx, assistControlTimeout)
	defer cancel()
	offer, err := client.AssistOffer(ctx, connectorID)
	if err != nil {
		var apiErr *cloud.APIError
		if errors.As(err, &apiErr) && apiErr.Status == http.StatusNotFound {
			return nil, nil
		}
		return nil, errors.New("暂时无法读取协助请求")
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.closed || client != a.cloud || a.assistActive != nil {
		return nil, nil
	}
	a.assistSeen = nil
	if offer == nil || (offer.Status != "pending" && offer.Status != "active") || offer.ID == a.assistStopped {
		return nil, nil
	}
	if !validAssistOffer(*offer, time.Now()) {
		return nil, errors.New("协助请求的 MCP 服务地址无效或已过期")
	}
	offer.LocalActive = false
	seen := *offer
	a.assistSeen = &seen
	return offer, nil
}

// StartAssist requires the local user to approve exactly the offer the UI read.
// Consent lives only in this process and is never restored on service restart.
func (a *Agent) StartAssist(ctx context.Context, sessionID string) error {
	a.mu.Lock()
	offer := a.assistSeen
	if a.closed || !a.opts.NativeRelay || a.cloud == nil || a.identity.ConnectorID == "" || a.identity.DeviceToken == "" ||
		a.state.Current() == state.Revoked || a.state.Current() == state.Draining ||
		a.assistActive != nil || offer == nil || offer.ID != sessionID || sessionID == a.assistStopped ||
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
		request, done := context.WithTimeout(ctx, assistControlTimeout)
		poll, err := session.client.PollAssist(request, session.connectorID, session.offer.ID)
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
				completed <- a.runAssistJob(ctx, session.offer, *job, &draft, &verifiedTools)
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

func (a *Agent) runAssistJob(ctx context.Context, offer cloud.AssistOffer, job cloud.AssistJob, draft **SourceInput, verifiedTools *[]string) cloud.AssistResult {
	result := cloud.AssistResult{SessionID: offer.ID, JobID: job.ID, Status: "failed", Code: "canceled"}
	if ctx.Err() != nil {
		return result
	}
	switch job.Action {
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
	if !assistSessionID.MatchString(offer.ID) || offer.Name == "" ||
		len([]rune(offer.Name)) > 100 || strings.IndexFunc(offer.Name, unicode.IsControl) >= 0 ||
		!offer.ExpiresAt.After(now) || len(offer.URL) > 2048 || strings.ContainsAny(offer.URL, "?#\\") ||
		strings.IndexFunc(offer.URL, func(r rune) bool { return unicode.IsSpace(r) || unicode.IsControl(r) }) >= 0 {
		return false
	}
	target, err := url.Parse(offer.URL)
	if err != nil || target.Opaque != "" || target.User != nil || target.Hostname() == "" ||
		target.Path == "" || target.Path == "/" || target.String() != offer.URL {
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
