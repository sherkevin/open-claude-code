package frpc

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func validConfig() Config {
	return Config{
		ServerAddr: "relay.example.com",
		ServerPort: 7443,
		User:       "tenant01",
		AuthToken:  "relay-token",
		TLSEnabled: true,
		Proxies: []Proxy{{
			Name: "src_01", SecretKey: "stcp-secret", LocalIP: "10.0.0.8", LocalPort: 8000,
			AllowUsers: []string{"tenant01"},
		}},
	}
}

func TestRenderContainsRestrictedProxy(t *testing.T) {
	text, err := validConfig().Render()
	if err != nil {
		t.Fatalf("渲染失败: %v", err)
	}
	for _, want := range []string{
		`serverAddr = "relay.example.com"`,
		`type = "stcp"`,
		`localIP = "10.0.0.8"`,
		`allowUsers = ["tenant01"]`,
		`transport.tls.enable = true`,
	} {
		if !strings.Contains(text, want) {
			t.Fatalf("配置缺少 %q:\n%s", want, text)
		}
	}
	if strings.Contains(text, "0.0.0.0") {
		t.Fatal("不应生成公网监听")
	}
}

func TestValidateRejectsMissingSecret(t *testing.T) {
	config := validConfig()
	config.Proxies[0].SecretKey = ""
	if err := config.Validate(); err == nil {
		t.Fatal("缺少 STCP 密钥应被拒绝")
	}
}

func TestValidateRejectsDuplicateNames(t *testing.T) {
	config := validConfig()
	config.Proxies = append(config.Proxies, config.Proxies[0])
	if err := config.Validate(); err == nil {
		t.Fatal("重复来源名应被拒绝")
	}
}

func TestWriteFileIsPrivate(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "frpc.toml")
	if err := validConfig().WriteFile(path); err != nil {
		t.Fatalf("写入失败: %v", err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("读取失败: %v", err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("配置文件权限应为 0600，实际 %o", info.Mode().Perm())
	}
}

func TestRenderEscapesQuotes(t *testing.T) {
	config := validConfig()
	config.Proxies[0].SecretKey = `a"b`
	text, err := config.Render()
	if err != nil {
		t.Fatalf("渲染失败: %v", err)
	}
	if !strings.Contains(text, `secretKey = "a\"b"`) {
		t.Fatalf("引号未转义: %s", text)
	}
}
