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

// AssistOffer binds a customer's approval to one specific MCP service.
type AssistOffer struct {
	ID          string    `json:"id"`
	Name        string    `json:"name"`
	URL         string    `json:"url"`
	Status      string    `json:"status"`
	ExpiresAt   time.Time `json:"expires_at"`
	LocalActive bool      `json:"local_active,omitempty"`
}

type AssistJob struct {
	ID          string              `json:"id"`
	Action      string              `json:"action"`
	BearerToken string              `json:"bearer_token,omitempty"`
	DynamicAuth *huaweiauth.Profile `json:"dynamic_auth,omitempty"`
	Tools       []string            `json:"tools,omitempty"`
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
}

func (c *Client) AssistOffer(ctx context.Context, connectorID string) (*AssistOffer, error) {
	var data struct {
		Offer *AssistOffer `json:"offer"`
	}
	err := c.assistRequest(ctx, http.MethodGet, connectorID, "offer", nil, &data)
	return data.Offer, err
}

func (c *Client) StartAssist(ctx context.Context, connectorID, sessionID string) error {
	return c.assistRequest(ctx, http.MethodPost, connectorID, "start", assistSessionRequest{sessionID}, nil)
}

func (c *Client) PollAssist(ctx context.Context, connectorID, sessionID string) (AssistPoll, error) {
	var data AssistPoll
	err := c.assistRequest(ctx, http.MethodPost, connectorID, "poll", assistSessionRequest{sessionID}, &data)
	return data, err
}

func (c *Client) StopAssist(ctx context.Context, connectorID, sessionID string) error {
	return c.assistRequest(ctx, http.MethodPost, connectorID, "stop", assistSessionRequest{sessionID}, nil)
}

func (c *Client) ReportAssist(ctx context.Context, connectorID string, data AssistResult) error {
	return c.assistRequest(ctx, http.MethodPost, connectorID, "result", data, nil)
}

type assistSessionRequest struct {
	SessionID string `json:"session_id"`
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
