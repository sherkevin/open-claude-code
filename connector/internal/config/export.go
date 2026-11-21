package config

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/url"
	"strings"
)

// EdgeSourcePath 返回某个来源在云端 Edge 上的稳定路径。
func EdgeSourcePath(sourceID string) string {
	return "/sources/" + url.PathEscape(sourceID) + "/mcp"
}

// PlatformConnection 是平台导入器接受的单个连接。
type PlatformConnection struct {
	Type    string            `json:"type"`
	URL     string            `json:"url"`
	Headers map[string]string `json:"headers,omitempty"`
}

// BuildPlatformConfig 生成不含授权信息的 Edge 地址预览。
//
// 正式开物 JSON 由云端管理员签发企业集成凭据 K 后生成。本机导出器不持有
// K、设备令牌、客户 B 或 S，避免把地址预览误当成可直接使用的授权配置。
func BuildPlatformConfig(sources []Source, edgeBase, accessToken string) ([]byte, error) {
	edgeBase = strings.TrimRight(strings.TrimSpace(edgeBase), "/")
	if edgeBase == "" {
		return nil, fmt.Errorf("%w: 缺少云端 Edge 地址", ErrInvalid)
	}
	parsed, err := url.Parse(edgeBase)
	if err != nil || parsed.Host == "" {
		return nil, fmt.Errorf("%w: 云端 Edge 地址无效", ErrInvalid)
	}
	if strings.TrimSpace(accessToken) != "" {
		return nil, fmt.Errorf("%w: 地址预览不能包含凭据", ErrInvalid)
	}

	servers := map[string]PlatformConnection{}
	used := map[string]int{}
	for _, source := range sources {
		name := strings.TrimSpace(source.DisplayName)
		if name == "" {
			name = source.SourceID
		}
		used[name]++
		if used[name] > 1 {
			name = fmt.Sprintf("%s-%d", name, used[name])
		}
		servers[name] = PlatformConnection{
			Type: "http",
			URL:  edgeBase + EdgeSourcePath(source.SourceID),
		}
	}
	if len(servers) == 0 {
		return nil, fmt.Errorf("%w: 没有可导出的来源", ErrInvalid)
	}
	payload := map[string]any{"mcpServers": servers}
	buffer := &bytes.Buffer{}
	encoder := json.NewEncoder(buffer)
	encoder.SetEscapeHTML(false)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(payload); err != nil {
		return nil, err
	}
	return bytes.TrimRight(buffer.Bytes(), "\n"), nil
}

// ExportPreview 是给界面展示的地址预览，不包含任何授权信息。
func ExportPreview(sources []Source, edgeBase string) (map[string]any, error) {
	raw, err := BuildPlatformConfig(sources, edgeBase, "")
	if err != nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, err
	}
	return out, nil
}
