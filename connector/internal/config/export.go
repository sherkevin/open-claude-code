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
	Headers map[string]string `json:"headers"`
}

// BuildPlatformConfig 生成平台可直接导入的 MCP 配置。
//
// 平台导入器只接受 mcpServers 容器、http 类型和唯一的 Authorization 请求头，
// 因此这里严格按该格式输出；地址指向云端 Edge，凭据为平台签发的调用凭据。
func BuildPlatformConfig(sources []Source, edgeBase, accessToken string) ([]byte, error) {
	edgeBase = strings.TrimRight(strings.TrimSpace(edgeBase), "/")
	if edgeBase == "" {
		return nil, fmt.Errorf("%w: 缺少云端 Edge 地址", ErrInvalid)
	}
	parsed, err := url.Parse(edgeBase)
	if err != nil || parsed.Host == "" {
		return nil, fmt.Errorf("%w: 云端 Edge 地址无效", ErrInvalid)
	}
	token := strings.TrimSpace(accessToken)
	if token == "" {
		token = "<EDGE_ACCESS_TOKEN>"
	}
	if !strings.HasPrefix(strings.ToLower(token), "bearer ") {
		token = "Bearer " + token
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
			Type:    "http",
			URL:     edgeBase + EdgeSourcePath(source.SourceID),
			Headers: map[string]string{"Authorization": token},
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

// ExportPreview 是给界面展示的导出预览，凭据只显示占位符。
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
