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
		ServerName: "relay.example.com",
		TrustedCA:  "/etc/kaiwu/relay-ca.pem",
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
		`transport.tls.serverName = "relay.example.com"`,
		`transport.tls.trustedCaFile = "/etc/kaiwu/relay-ca.pem"`,
		`loginFailExit = false`,
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

func TestRenderWithEnvSecretsKeepsSecretsOffDisk(t *testing.T) {
	config := validConfig()
	config.AdminPort = 39000
	config.AdminUser = "kaiwu"
	config.AdminPassword = "synthetic-admin-password"
	text, environment, err := config.RenderWithEnvSecrets()
	if err != nil {
		t.Fatal(err)
	}
	for _, secret := range []string{config.AuthToken, config.Proxies[0].SecretKey, config.AdminPassword} {
		if strings.Contains(text, secret) {
			t.Fatalf("磁盘配置不得包含明文秘密: %s", text)
		}
	}
	if !strings.Contains(text, "{{ .Envs.KAIWU_RELAY_TOKEN }}") ||
		!strings.Contains(text, "{{ .Envs.KAIWU_STCP_SRC_01 }}") ||
		!strings.Contains(text, "{{ .Envs.KAIWU_FRPC_ADMIN_PASSWORD }}") {
		t.Fatalf("frp 环境模板缺失: %s", text)
	}
	if environment["KAIWU_RELAY_TOKEN"] != config.AuthToken ||
		environment["KAIWU_STCP_SRC_01"] != config.Proxies[0].SecretKey ||
		environment["KAIWU_FRPC_ADMIN_PASSWORD"] != config.AdminPassword {
		t.Fatalf("子进程环境秘密不完整: %+v", environment)
	}
}
