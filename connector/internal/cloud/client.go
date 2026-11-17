// Package cloud 封装连接器与云端控制面的最小接口。
//
// 接口命名与用途见《企业内网 MCP 接入程序 开发全景文档 v1.0》第 08 章。
// 连接器只使用设备身份调用这些接口，不接触平台调用凭据或客户业务凭据。
package cloud

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// TLSConfig 描述中继的 TLS 材料位置。
type TLSConfig struct {
	Enabled    bool   `json:"enabled"`
	ServerName string `json:"server_name,omitempty"`
	TrustedCA  string `json:"trusted_ca_file,omitempty"`
	CertFile   string `json:"cert_file,omitempty"`
	KeyFile    string `json:"key_file,omitempty"`
}

// Relay 描述中继地址与通道身份。
type Relay struct {
	Address  string    `json:"address"`
	Port     int       `json:"port"`
	User     string    `json:"user,omitempty"`
	Token    string    `json:"token,omitempty"`
	Protocol string    `json:"protocol,omitempty"`
	TLS      TLSConfig `json:"tls,omitempty"`
}

// Identity 是兑换接入码后得到的设备身份。
type Identity struct {
	ConnectorID string `json:"connector_id"`
	TenantID    string `json:"tenant_id"`
	TenantName  string `json:"tenant_name"`
	DeviceToken string `json:"device_token"`
	Relay       Relay  `json:"relay"`
}

// DesiredSource 是云端下发的来源期望状态。
type DesiredSource struct {
	SourceID    string   `json:"source_id"`
	DisplayName string   `json:"display_name"`
	STCPSecret  string   `json:"stcp_secret,omitempty"`
	AllowUsers  []string `json:"allow_users,omitempty"`
}

// DesiredState 是连接器的期望状态。
type DesiredState struct {
	Revision int             `json:"revision"`
	Relay    Relay           `json:"relay"`
	Sources  []DesiredSource `json:"sources"`
	Revoked  bool            `json:"revoked"`
	Draining bool            `json:"draining"`
}

// Heartbeat 是上报给云端的状态。
type Heartbeat struct {
	State      string    `json:"state"`
	Version    string    `json:"version"`
	ObservedAt time.Time `json:"observed_at"`
	SourceIDs  []string  `json:"source_ids,omitempty"`
	LastError  string    `json:"last_error,omitempty"`
}

// APIError 是云端返回的结构化错误。
type APIError struct {
	Status    int
	Code      string `json:"code"`
	Message   string `json:"message"`
	TraceID   string `json:"trace_id"`
	Retryable bool   `json:"retryable"`
}

func (e *APIError) Error() string {
	if e.TraceID != "" {
		return fmt.Sprintf("%s: %s (trace %s)", e.Code, e.Message, e.TraceID)
	}
	return fmt.Sprintf("%s: %s", e.Code, e.Message)
}

// Client 是云端控制面客户端。
type Client struct {
	BaseURL     string
	DeviceToken string
	HTTP        *http.Client
	UserAgent   string
}

// New 创建客户端并校验地址。
func New(baseURL, deviceToken string) (*Client, error) {
	baseURL = strings.TrimRight(strings.TrimSpace(baseURL), "/")
	parsed, err := url.Parse(baseURL)
	if err != nil || parsed.Host == "" {
		return nil, fmt.Errorf("云端地址无效")
	}
	return &Client{BaseURL: baseURL, DeviceToken: deviceToken, HTTP: &http.Client{Timeout: 20 * time.Second}, UserAgent: "kaiwu-intranet-connector"}, nil
}

// Redeem 用一次性接入码兑换设备身份。
func (c *Client) Redeem(ctx context.Context, code string, devicePublicKey string) (Identity, error) {
	var identity Identity
	body := map[string]any{"code": strings.TrimSpace(code)}
	if devicePublicKey != "" {
		body["device_public_key"] = devicePublicKey
	}
	err := c.do(ctx, http.MethodPost, "/v1/enrollments/redeem", body, &identity)
	return identity, err
}

// DesiredState 拉取期望状态。
func (c *Client) DesiredState(ctx context.Context, connectorID string) (DesiredState, error) {
	var state DesiredState
	err := c.do(ctx, http.MethodGet, "/v1/connectors/"+url.PathEscape(connectorID)+"/desired-state", nil, &state)
	return state, err
}

// TransportSecrets 拉取通道材料（中继令牌与每个来源的 STCP 密钥）。
func (c *Client) TransportSecrets(ctx context.Context, connectorID string) (DesiredState, error) {
	var state DesiredState
	err := c.do(ctx, http.MethodGet, "/v1/connectors/"+url.PathEscape(connectorID)+"/transport-secrets", nil, &state)
	return state, err
}

// Heartbeat 上报运行状态。
func (c *Client) Heartbeat(ctx context.Context, connectorID string, beat Heartbeat) error {
	return c.do(ctx, http.MethodPost, "/v1/connectors/"+url.PathEscape(connectorID)+"/heartbeat", beat, nil)
}

// Drain 请求进入排空状态。
func (c *Client) Drain(ctx context.Context, connectorID string) error {
	return c.do(ctx, http.MethodPost, "/v1/connectors/"+url.PathEscape(connectorID)+"/drain", map[string]any{}, nil)
}

// Resume 请求恢复。
func (c *Client) Resume(ctx context.Context, connectorID string) error {
	return c.do(ctx, http.MethodPost, "/v1/connectors/"+url.PathEscape(connectorID)+"/resume", map[string]any{}, nil)
}

// Revoke 请求撤销本设备。
func (c *Client) Revoke(ctx context.Context, connectorID string) error {
	return c.do(ctx, http.MethodPost, "/v1/connectors/"+url.PathEscape(connectorID)+"/revoke", map[string]any{}, nil)
}

func (c *Client) do(ctx context.Context, method, path string, body any, out any) error {
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.BaseURL+path, reader)
	if err != nil {
		return err
	}
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if c.DeviceToken != "" {
		req.Header.Set("Authorization", "Bearer "+c.DeviceToken)
	}
	if c.UserAgent != "" {
		req.Header.Set("User-Agent", c.UserAgent)
	}
	client := c.HTTP
	if client == nil {
		client = &http.Client{Timeout: 20 * time.Second}
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	payload, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		apiErr := &APIError{Status: resp.StatusCode}
		var wrapper struct {
			Error APIError `json:"error"`
		}
		if err := json.Unmarshal(payload, &wrapper); err == nil && wrapper.Error.Code != "" {
			apiErr.Code = wrapper.Error.Code
			apiErr.Message = wrapper.Error.Message
			apiErr.TraceID = wrapper.Error.TraceID
			apiErr.Retryable = wrapper.Error.Retryable
		} else {
			var flat APIError
			if err := json.Unmarshal(payload, &flat); err == nil && flat.Code != "" {
				apiErr.Code = flat.Code
				apiErr.Message = flat.Message
				apiErr.TraceID = flat.TraceID
				apiErr.Retryable = flat.Retryable
			}
		}
		if apiErr.Code == "" {
			apiErr.Code = fmt.Sprintf("HTTP_%d", resp.StatusCode)
			apiErr.Message = strings.TrimSpace(string(payload))
		}
		return apiErr
	}
	if out == nil || len(payload) == 0 {
		return nil
	}
	return json.Unmarshal(payload, out)
}
