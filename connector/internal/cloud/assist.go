package cloud

import (
	"context"
	"errors"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/huaweiauth"
)

// AssistConsentWeek is the explicit opt-in for one fixed 168h authorization
// counted from the customer's local confirmation. A connector must only send it
// after the control plane advertises SupportWeekConsent; older control planes
// keep the original 24h absolute + 30 minute sliding policy.
const AssistConsentWeek = "week_168h"

// AssistOffer describes the current grant. URL is empty until an MCP service
// has been approved on the device.
type AssistOffer struct {
	ID          string    `json:"id"`
	Name        string    `json:"name"`
	URL         string    `json:"url"`
	Status      string    `json:"status"`
	ExpiresAt   time.Time `json:"expires_at"`
	LocalActive bool      `json:"local_active,omitempty"`

	// ConsentPolicy is "week_168h" only when both sides negotiated the fixed
	// week consent. ConsentExpiresAt is then the authoritative absolute
	// deadline; it never moves when the connector polls.
	ConsentPolicy    string     `json:"consent_policy,omitempty"`
	ConsentExpiresAt *time.Time `json:"consent_expires_at,omitempty"`
}

// ConsentDeadline returns the absolute end of a negotiated week grant.
func (o AssistOffer) ConsentDeadline() time.Time {
	if o.ConsentExpiresAt != nil {
		return *o.ConsentExpiresAt
	}
	return o.ExpiresAt
}

// AssistCommand is the local-only view of a command the control plane sent to
// this device. It is never sent by the control plane.
type AssistCommand struct {
	ID         string     `json:"id"`
	Command    string     `json:"command"`
	State      string     `json:"state"`
	Output     string     `json:"output,omitempty"`
	ExitCode   *int       `json:"exit_code,omitempty"`
	Truncated  bool       `json:"truncated,omitempty"`
	DurationMS int64      `json:"duration_ms,omitempty"`
	StartedAt  time.Time  `json:"started_at,omitempty"`
	FinishedAt *time.Time `json:"finished_at,omitempty"`
}

type AssistStatus struct {
	Enabled          bool           `json:"enabled"`
	Offer            *AssistOffer   `json:"offer"`
	TargetAfterStart bool           `json:"target_after_start,omitempty"`
	LastCommand      *AssistCommand `json:"last_command,omitempty"`
	// WeekConsent is only sent by a control plane that supports the fixed
	// 168h policy and only after this connector opts in on the request.
	WeekConsent bool `json:"week_consent,omitempty"`
}

type AssistTarget struct {
	Name string `json:"name"`
	URL  string `json:"url"`
}

type AssistJob struct {
	ID             string              `json:"id"`
	Action         string              `json:"action"`
	BearerToken    string              `json:"bearer_token,omitempty"`
	DynamicAuth    *huaweiauth.Profile `json:"dynamic_auth,omitempty"`
	Tools          []string            `json:"tools,omitempty"`
	Command        string              `json:"command,omitempty"`
	TimeoutSeconds int                 `json:"timeout_seconds,omitempty"`
}

type AssistPoll struct {
	Active bool       `json:"active"`
	Job    *AssistJob `json:"job"`
}

type AssistResult struct {
	SessionID     string   `json:"session_id"`
	JobID         string   `json:"job_id"`
	Status        string   `json:"status"`
	Code          string   `json:"code"`
	Stage         string   `json:"stage,omitempty"`
	HTTPStatus    int      `json:"http_status,omitempty"`
	Authorization *bool    `json:"authorization,omitempty"`
	Tools         []string `json:"tools,omitempty"`
	SourceID      string   `json:"source_id,omitempty"`
	Output        string   `json:"output,omitempty"`
	ExitCode      *int     `json:"exit_code,omitempty"`
	Truncated     bool     `json:"truncated,omitempty"`
	DurationMS    int64    `json:"duration_ms,omitempty"`
}

func (c *Client) AssistStatus(ctx context.Context, connectorID string) (AssistStatus, error) {
	var data AssistStatus
	// The opt-in query only asks the control plane to advertise the week
	// policy; it never grants anything by itself. The actual policy is
	// confirmed in the signed start body.
	err := c.assistRequest(
		ctx, http.MethodGet, connectorID,
		"offer?device_first=1&target_after_start=1&consent="+AssistConsentWeek, nil, &data,
	)
	return data, err
}

func (c *Client) StartAssist(ctx context.Context, connectorID, sessionID, consentPolicy string) (AssistOffer, error) {
	var offer AssistOffer
	body := assistSessionRequest{SessionID: sessionID, ConsentPolicy: consentPolicy}
	err := c.assistRequest(ctx, http.MethodPost, connectorID, "start", body, &offer)
	return offer, err
}

func (c *Client) OpenAssist(
	ctx context.Context, connectorID string, target *AssistTarget, consentPolicy string,
) (AssistOffer, error) {
	var body any = struct{}{}
	if target != nil {
		body = assistTargetRequest{AssistTarget: *target, ConsentPolicy: consentPolicy}
	} else if consentPolicy != "" {
		body = assistConsentRequest{ConsentPolicy: consentPolicy}
	}
	var offer AssistOffer
	err := c.assistRequest(ctx, http.MethodPost, connectorID, "start", body, &offer)
	return offer, err
}

func (c *Client) SetAssistTarget(ctx context.Context, connectorID, sessionID string, target AssistTarget) (AssistOffer, error) {
	body := struct {
		assistSessionRequest
		AssistTarget
	}{assistSessionRequest: assistSessionRequest{SessionID: sessionID}, AssistTarget: target}
	var offer AssistOffer
	err := c.assistRequest(ctx, http.MethodPost, connectorID, "target", body, &offer)
	return offer, err
}

func (c *Client) PollAssist(ctx context.Context, connectorID, sessionID string, acceptJobs bool) (AssistPoll, error) {
	body := struct {
		assistSessionRequest
		AcceptJobs *bool `json:"accept_jobs,omitempty"`
	}{assistSessionRequest: assistSessionRequest{SessionID: sessionID}}
	if !acceptJobs {
		body.AcceptJobs = &acceptJobs
	}
	var data AssistPoll
	err := c.assistRequest(ctx, http.MethodPost, connectorID, "poll", body, &data)
	return data, err
}

func (c *Client) StopAssist(ctx context.Context, connectorID, sessionID string) error {
	return c.assistRequest(
		ctx, http.MethodPost, connectorID, "stop", assistSessionRequest{SessionID: sessionID}, nil,
	)
}

func (c *Client) ReportAssist(ctx context.Context, connectorID string, data AssistResult) error {
	return c.assistRequest(ctx, http.MethodPost, connectorID, "result", data, nil)
}

type assistSessionRequest struct {
	SessionID     string `json:"session_id"`
	ConsentPolicy string `json:"consent_policy,omitempty"`
}

type assistTargetRequest struct {
	AssistTarget
	ConsentPolicy string `json:"consent_policy,omitempty"`
}

type assistConsentRequest struct {
	ConsentPolicy string `json:"consent_policy,omitempty"`
}

func (c *Client) assistRequest(ctx context.Context, method, connectorID, action string, body, out any) error {
	base, err := url.Parse(c.BaseURL)
	if err != nil || base.User != nil || base.RawQuery != "" || base.ForceQuery || base.Fragment != "" {
		return errors.New("协助需要受保护的开物控制面地址")
	}
	host := strings.ToLower(base.Hostname())
	if base.Scheme != "https" && !(base.Scheme == "http" && (host == "localhost" || host == "127.0.0.1" || host == "::1")) {
		return errors.New("协助需要受保护的开物控制面地址")
	}
	client := *c
	httpClient := c.HTTP
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 5 * time.Second}
	}
	transport := *httpClient
	transport.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	client.HTTP = &transport
	return client.doStrict(ctx, method, "/v1/connectors/"+url.PathEscape(connectorID)+"/assist/"+action, body, out)
}
