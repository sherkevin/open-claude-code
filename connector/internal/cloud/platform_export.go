package cloud

import (
	"context"
	"encoding/json"
	"net/http"
	"net/url"
)

// ExportPlatformMCP requests the standard, source-scoped MCP configuration.
func (c *Client) ExportPlatformMCP(ctx context.Context, connectorID, sourceID string) (json.RawMessage, error) {
	var result struct {
		Config json.RawMessage `json:"mcp_config"`
	}
	path := "/v1/connectors/" + url.PathEscape(connectorID) + "/mcp-export"
	err := c.doStrict(ctx, http.MethodPost, path, map[string]string{"source_id": sourceID}, &result)
	return result.Config, err
}

// CreatePlatformHandoff obtains a one-time code for an authorized user to
// confirm the MCP configuration on the platform. It contains no credentials.
func (c *Client) CreatePlatformHandoff(ctx context.Context, connectorID, sourceID string) (string, error) {
	var result struct {
		Code string `json:"handoff_code"`
	}
	path := "/v1/connectors/" + url.PathEscape(connectorID) + "/platform-handoff"
	err := c.doStrict(ctx, http.MethodPost, path, map[string]string{"source_id": sourceID}, &result)
	return result.Code, err
}
