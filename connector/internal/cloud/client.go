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

	"github.com/coder/websocket"
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
	DeviceKeyID string `json:"device_key_id,omitempty"`
	Relay       Relay  `json:"relay"`
}

// DesiredSource 是云端下发的来源期望状态。
type DesiredSource struct {
	SourceID            string   `json:"source_id"`
	DisplayName         string   `json:"display_name"`
	AllowedTools        []string `json:"allowed_tools,omitempty"`
	ToolSchemaSHA256    string   `json:"tool_schema_sha256,omitempty"`
	TargetBindingSHA256 string   `json:"target_binding_sha256,omitempty"`
	Transport           string   `json:"transport,omitempty"`
	STCPSecret          string   `json:"stcp_secret,omitempty"`
	AllowUsers          []string `json:"allow_users,omitempty"`
	Revision            int      `json:"revision,omitempty"`
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
	SourceIDs  []string  `json:"source_ids"`
	LastError  string    `json:"last_error"`
}

// SourceRegistration is the metadata-only source contract. It never contains
// the customer's upstream URL, host, port or credentials.
type SourceRegistration struct {
	SourceID            string   `json:"source_id"`
	DisplayName         string   `json:"display_name"`
	AllowedTools        []string `json:"allowed_tools"`
	ToolSchemaSHA256    string   `json:"tool_schema_sha256"`
	TargetBindingSHA256 string   `json:"target_binding_sha256"`
	Transport           string   `json:"transport"`
}

// SelfTestStage is one bounded result returned by the cloud end-to-end probe.
type SelfTestStage struct {
	ID      string `json:"id"`
	Status  string `json:"status"`
	Message string `json:"message"`
}

// SelfTestProbe is the fixed kaiwu_demo_ping result. It contains no customer
// business data or machine identity.
type SelfTestProbe struct {
	Demo          bool   `json:"demo"`
	Marker        string `json:"marker"`
	Nonce         string `json:"nonce"`
	ServerTimeUTC string `json:"server_time_utc"`
	InstanceID    string `json:"instance_id"`
}

// SelfTestResult is the control-plane response envelope payload.
type SelfTestResult struct {
	OK         bool            `json:"ok"`
	TraceID    string          `json:"trace_id"`
	SourceID   string          `json:"source_id"`
	Stages     []SelfTestStage `json:"stages"`
	Probe      SelfTestProbe   `json:"probe"`
	HTTPStatus int             `json:"-"`
}

// RequestSigner signs the exact control-plane request.
type RequestSigner interface {
	KeyID() string
	SignRequest(method, path string, body []byte) (timestamp, nonce, signature string, err error)
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

// HTTPStatusCode exposes only the bounded status for privacy-safe diagnostics
// classification; it does not expose the response body or customer payload.
func (e *APIError) HTTPStatusCode() int { return e.Status }

// Client 是云端控制面客户端。
type Client struct {
	BaseURL     string
	DeviceToken string
	Signer      RequestSigner
	HTTP        *http.Client
	UserAgent   string
}

// New 创建客户端并校验地址。
func New(baseURL, deviceToken string) (*Client, error) {
	return NewWithSigner(baseURL, deviceToken, nil)
}

// NewWithSigner creates a device client. Device control requests must use this
// constructor so method, path and body are signed with the local private key.
func NewWithSigner(baseURL, deviceToken string, signer RequestSigner) (*Client, error) {
	baseURL = strings.TrimRight(strings.TrimSpace(baseURL), "/")
	parsed, err := url.Parse(baseURL)
	if err != nil || parsed.Host == "" {
		return nil, fmt.Errorf("云端地址无效")
	}
	return &Client{
		BaseURL:     baseURL,
		DeviceToken: deviceToken,
		Signer:      signer,
		HTTP:        &http.Client{Timeout: 20 * time.Second},
		UserAgent:   "kaiwu-intranet-connector",
	}, nil
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
	err := c.doStrict(ctx, http.MethodGet, "/v1/connectors/"+url.PathEscape(connectorID)+"/desired-state", nil, &state)
	return state, err
}

// TransportSecrets 拉取通道材料（中继令牌与每个来源的 STCP 密钥）。
func (c *Client) TransportSecrets(ctx context.Context, connectorID string) (DesiredState, error) {
	var state DesiredState
	err := c.doStrict(ctx, http.MethodGet, "/v1/connectors/"+url.PathEscape(connectorID)+"/transport-secrets", nil, &state)
	return state, err
}

// DialNativeRelay signs the WebSocket upgrade with the same device identity as
// the control plane. The connection does not inherit the HTTP API deadline.
func (c *Client) DialNativeRelay(ctx context.Context, connectorID string) (*websocket.Conn, error) {
	if c.Signer == nil || c.DeviceToken == "" {
		return nil, fmt.Errorf("中继需要已绑定的设备签名身份")
	}
	target, err := url.Parse(c.BaseURL + "/v1/connectors/" + url.PathEscape(connectorID) + "/native-relay")
	if err != nil || target.Scheme != "https" || target.User != nil || target.RawQuery != "" || target.ForceQuery || target.Fragment != "" {
		return nil, fmt.Errorf("原生中继只接受 HTTPS 云端地址")
	}
	request := &http.Request{URL: target, Header: http.Header{}, Method: http.MethodGet}
	if err := c.applyIdentityHeaders(request, nil); err != nil {
		return nil, err
	}

	client := &http.Client{}
	if c.HTTP != nil {
		*client = *c.HTTP
	}
	client.Timeout = 0
	client.Jar = nil
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	if client.Transport == nil {
		// The default HTTP transport honours the standard proxy environment.
		client.Transport = http.DefaultTransport
	}
	conn, response, err := websocket.Dial(ctx, target.String(), &websocket.DialOptions{
		HTTPClient: client, HTTPHeader: request.Header,
	})
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		if response != nil {
			return nil, fmt.Errorf("原生中继握手失败（HTTP %d）", response.StatusCode)
		}
		return nil, fmt.Errorf("无法建立原生中继连接")
	}
	return conn, nil
}

// RegisterSource registers only opaque metadata and a local target digest.
func (c *Client) RegisterSource(ctx context.Context, source SourceRegistration) (map[string]any, error) {
	var result map[string]any
	err := c.do(ctx, http.MethodPost, "/v1/sources", source, &result)
	return result, err
}

// Heartbeat 上报运行状态。
func (c *Client) Heartbeat(ctx context.Context, connectorID string, beat Heartbeat) error {
	return c.do(ctx, http.MethodPost, "/v1/connectors/"+url.PathEscape(connectorID)+"/heartbeat", beat, nil)
}

// UploadDiagnosticEvents sends one already-redacted batch through the same
// device Bearer and Ed25519 request signature used by the rest of the control
// plane. The concrete event type lives in diagnostics to avoid a package cycle.
func (c *Client) UploadDiagnosticEvents(ctx context.Context, connectorID string, events any) error {
	body := map[string]any{"events": events}
	return c.do(ctx, http.MethodPost, "/v1/connectors/"+url.PathEscape(connectorID)+"/events", body, nil)
}

// PlatformDiagnosticEvents returns only edge/platform events scoped to the
// authenticated connector. Query parameters do not establish authorization.
func (c *Client) PlatformDiagnosticEvents(ctx context.Context, connectorID string, limit int) ([]map[string]any, error) {
	if limit <= 0 || limit > 500 {
		limit = 200
	}
	path := "/v1/connectors/" + url.PathEscape(connectorID) + "/platform-events?limit=" + url.QueryEscape(fmt.Sprint(limit))
	var result struct {
		Events []map[string]any `json:"events"`
	}
	if err := c.doStrict(ctx, http.MethodGet, path, nil, &result); err != nil {
		return nil, err
	}
	return result.Events, nil
}

// SelfTest asks the control plane to perform the fixed demo probe through the
// public Edge and the connector's FRP route. Local data is never sent besides
// the opaque source ID and a short nonce.
func (c *Client) SelfTest(ctx context.Context, connectorID, sourceID, nonce string) (SelfTestResult, error) {
	path := "/v1/connectors/" + url.PathEscape(connectorID) + "/self-test"
	status, payload, err := c.rawRequest(ctx, http.MethodPost, path, map[string]string{
		"source_id": sourceID,
		"nonce":     nonce,
	})
	if err != nil {
		return SelfTestResult{}, err
	}
	var envelope struct {
		Data SelfTestResult `json:"data"`
	}
	decoder := json.NewDecoder(bytes.NewReader(payload))
	decoder.DisallowUnknownFields()
	if decodeErr := decoder.Decode(&envelope); decodeErr == nil {
		result := envelope.Data
		result.HTTPStatus = status
		return result, nil
	}
	return SelfTestResult{HTTPStatus: status}, apiErrorFromResponse(status, payload)
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
	return c.do(ctx, http.MethodPost, "/v1/connectors/"+url.PathEscape(connectorID)+"/self-revoke", map[string]any{}, nil)
}

func (c *Client) do(ctx context.Context, method, path string, body any, out any) error {
	return c.request(ctx, method, path, body, out, false)
}

func (c *Client) doStrict(ctx context.Context, method, path string, body any, out any) error {
	return c.request(ctx, method, path, body, out, true)
}

func (c *Client) request(ctx context.Context, method, path string, body any, out any, strict bool) error {
	status, payload, err := c.rawRequest(ctx, method, path, body)
	if err != nil {
		return err
	}
	if status < 200 || status >= 300 {
		return apiErrorFromResponse(status, payload)
	}
	if out == nil || len(payload) == 0 {
		return nil
	}
	if strict {
		decoder := json.NewDecoder(bytes.NewReader(payload))
		decoder.DisallowUnknownFields()
		return decoder.Decode(out)
	}
	return json.Unmarshal(payload, out)
}

func (c *Client) rawRequest(ctx context.Context, method, path string, body any) (int, []byte, error) {
	var rawBody []byte
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return 0, nil, err
		}
		rawBody = raw
	}
	var reader io.Reader
	if rawBody != nil {
		reader = bytes.NewReader(rawBody)
	}
	target := c.BaseURL + path
	req, err := http.NewRequestWithContext(ctx, method, target, reader)
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if err := c.applyIdentityHeaders(req, rawBody); err != nil {
		return 0, nil, err
	}
	client := c.HTTP
	if client == nil {
		client = &http.Client{Timeout: 20 * time.Second}
	}
	resp, err := client.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	payload, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return 0, nil, err
	}
	return resp.StatusCode, payload, nil
}

func (c *Client) applyIdentityHeaders(req *http.Request, rawBody []byte) error {
	if c.DeviceToken != "" {
		req.Header.Set("Authorization", "Bearer "+c.DeviceToken)
	}
	if c.Signer != nil && c.DeviceToken != "" {
		timestamp, nonce, signature, err := c.Signer.SignRequest(req.Method, req.URL.EscapedPath(), rawBody)
		if err != nil {
			return err
		}
		req.Header.Set("X-Kaiwu-Device-Key", c.Signer.KeyID())
		req.Header.Set("X-Kaiwu-Timestamp", timestamp)
		req.Header.Set("X-Kaiwu-Nonce", nonce)
		req.Header.Set("X-Kaiwu-Signature", signature)
	}
	if c.UserAgent != "" {
		req.Header.Set("User-Agent", c.UserAgent)
	}
	return nil
}

func apiErrorFromResponse(status int, payload []byte) error {
	apiErr := &APIError{Status: status}
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
		apiErr.Code = fmt.Sprintf("HTTP_%d", status)
		// Never carry an arbitrary proxy/HTML response body into logs or UI.
		// Known structured API errors were parsed above.
		apiErr.Message = "云端请求失败"
	}
	return apiErr
}
