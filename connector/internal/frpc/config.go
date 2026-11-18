// Package frpc 生成并校验 frpc 运行配置。
//
// 设计依据：《企业内网 MCP 接入程序 开发全景文档 v1.0》第 03、05、10 章。
// 连接器只生成受限配置：每个来源一个 STCP 代理，目标固定为客户本机地址，
// 不生成任意端口映射、TCP 公网入口或 frp 管理控制台。
package frpc

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// Proxy 是一个受限的 STCP 代理。
type Proxy struct {
	Name       string
	SecretKey  string
	LocalIP    string
	LocalPort  int
	AllowUsers []string
}

// Config 是 frpc 客户端运行配置。
type Config struct {
	ServerAddr string
	ServerPort int
	User       string
	AuthToken  string
	Protocol   string // tcp / websocket / wss，空值按 tcp
	TLSEnabled bool
	ServerName string
	TrustedCA  string
	CertFile   string
	KeyFile    string
	Proxies    []Proxy
	// AdminPort exposes frpc's loopback status API. The process may be running
	// while a proxy is still waiting for relay login; callers must inspect the
	// proxy phase instead of treating process liveness as data-plane readiness.
	AdminPort     int
	AdminUser     string
	AdminPassword string
	LogLevel      string
	LogMaxDays    int
}

// Validate 检查生成前的最低要求，避免把空值写进运行配置。
func (c Config) Validate() error {
	if strings.TrimSpace(c.ServerAddr) == "" {
		return fmt.Errorf("缺少中继地址")
	}
	if c.ServerPort <= 0 || c.ServerPort > 65535 {
		return fmt.Errorf("中继端口无效: %d", c.ServerPort)
	}
	if strings.TrimSpace(c.AuthToken) == "" {
		return fmt.Errorf("缺少通道令牌")
	}
	if len(c.Proxies) == 0 {
		return fmt.Errorf("没有可运行的来源")
	}
	if c.AdminPort < 0 || c.AdminPort > 65535 {
		return fmt.Errorf("frpc 本机管理端口无效: %d", c.AdminPort)
	}
	if c.AdminPort > 0 && (strings.TrimSpace(c.AdminUser) == "" || strings.TrimSpace(c.AdminPassword) == "") {
		return fmt.Errorf("frpc 本机管理接口缺少认证信息")
	}
	seen := map[string]bool{}
	for _, proxy := range c.Proxies {
		if strings.TrimSpace(proxy.Name) == "" {
			return fmt.Errorf("来源名称为空")
		}
		if seen[proxy.Name] {
			return fmt.Errorf("来源名称重复: %s", proxy.Name)
		}
		seen[proxy.Name] = true
		if strings.TrimSpace(proxy.SecretKey) == "" {
			return fmt.Errorf("来源 %s 缺少 STCP 密钥", proxy.Name)
		}
		if proxy.LocalPort <= 0 || proxy.LocalPort > 65535 {
			return fmt.Errorf("来源 %s 的本地端口无效", proxy.Name)
		}
		if strings.TrimSpace(proxy.LocalIP) == "" {
			return fmt.Errorf("来源 %s 缺少本地地址", proxy.Name)
		}
	}
	switch c.Protocol {
	case "", "tcp", "quic", "websocket", "wss":
	default:
		return fmt.Errorf("不支持的传输协议: %s", c.Protocol)
	}
	return nil
}

// Render 输出 TOML 文本。
func (c Config) Render() (string, error) {
	text, _, err := c.render(false)
	return text, err
}

// RenderWithEnvSecrets emits an on-disk config that contains no relay or STCP
// secret. The returned environment values are passed only to the frpc child
// process; frp expands the official `.Envs` templates at startup.
func (c Config) RenderWithEnvSecrets() (string, map[string]string, error) {
	return c.render(true)
}

func (c Config) render(useEnvSecrets bool) (string, map[string]string, error) {
	if err := c.Validate(); err != nil {
		return "", nil, err
	}
	protocol := c.Protocol
	if protocol == "" {
		protocol = "tcp"
	}
	environment := map[string]string{}
	var b strings.Builder
	fmt.Fprintf(&b, "serverAddr = %s\n", quote(c.ServerAddr))
	fmt.Fprintf(&b, "serverPort = %d\n", c.ServerPort)
	if c.User != "" {
		fmt.Fprintf(&b, "user = %s\n", quote(c.User))
	}
	fmt.Fprintf(&b, "\nauth.method = \"token\"\n")
	authToken := c.AuthToken
	if useEnvSecrets {
		authToken = "{{ .Envs.KAIWU_RELAY_TOKEN }}"
		environment["KAIWU_RELAY_TOKEN"] = c.AuthToken
	}
	fmt.Fprintf(&b, "auth.token = %s\n", quote(authToken))
	// The Windows client is intentionally allowed to start before the
	// dedicated frps/visitor provisioning step completes. A transient initial
	// login failure must not terminate the supervised process.
	fmt.Fprintf(&b, "loginFailExit = false\n")
	fmt.Fprintf(&b, "\ntransport.protocol = %s\n", quote(protocol))
	fmt.Fprintf(&b, "transport.tls.enable = %t\n", c.TLSEnabled)
	if c.ServerName != "" {
		fmt.Fprintf(&b, "transport.tls.serverName = %s\n", quote(c.ServerName))
	}
	if c.TrustedCA != "" {
		fmt.Fprintf(&b, "transport.tls.trustedCaFile = %s\n", quote(c.TrustedCA))
	}
	if c.CertFile != "" {
		fmt.Fprintf(&b, "transport.tls.certFile = %s\n", quote(c.CertFile))
	}
	if c.KeyFile != "" {
		fmt.Fprintf(&b, "transport.tls.keyFile = %s\n", quote(c.KeyFile))
	}
	if c.AdminPort > 0 {
		password := c.AdminPassword
		if useEnvSecrets {
			password = "{{ .Envs.KAIWU_FRPC_ADMIN_PASSWORD }}"
			environment["KAIWU_FRPC_ADMIN_PASSWORD"] = c.AdminPassword
		}
		fmt.Fprintf(&b, "\nwebServer.addr = \"127.0.0.1\"\n")
		fmt.Fprintf(&b, "webServer.port = %d\n", c.AdminPort)
		fmt.Fprintf(&b, "webServer.user = %s\n", quote(c.AdminUser))
		fmt.Fprintf(&b, "webServer.password = %s\n", quote(password))
	}
	if c.LogLevel != "" {
		fmt.Fprintf(&b, "\nlog.to = \"/dev/null\"\nlog.level = %s\n", quote(c.LogLevel))
	}
	if c.LogMaxDays > 0 {
		fmt.Fprintf(&b, "log.maxDays = %d\n", c.LogMaxDays)
	}
	for _, proxy := range c.Proxies {
		secretKey := proxy.SecretKey
		if useEnvSecrets {
			envName := secretEnvironmentName(proxy.Name)
			secretKey = "{{ .Envs." + envName + " }}"
			environment[envName] = proxy.SecretKey
		}
		fmt.Fprintf(&b, "\n[[proxies]]\n")
		fmt.Fprintf(&b, "name = %s\n", quote(proxy.Name))
		fmt.Fprintf(&b, "type = \"stcp\"\n")
		fmt.Fprintf(&b, "secretKey = %s\n", quote(secretKey))
		fmt.Fprintf(&b, "localIP = %s\n", quote(proxy.LocalIP))
		fmt.Fprintf(&b, "localPort = %d\n", proxy.LocalPort)
		if len(proxy.AllowUsers) > 0 {
			quoted := make([]string, 0, len(proxy.AllowUsers))
			for _, user := range proxy.AllowUsers {
				quoted = append(quoted, quote(user))
			}
			fmt.Fprintf(&b, "allowUsers = [%s]\n", strings.Join(quoted, ", "))
		}
	}
	return b.String(), environment, nil
}

// WriteFile 以 0600 权限原子写入配置文件。
func (c Config) WriteFile(path string) error {
	text, err := c.Render()
	if err != nil {
		return err
	}
	return writeText(path, text)
}

// WriteEnvFile writes a secret-free config and returns the child environment
// that supplies the actual relay and STCP values at process startup.
func (c Config) WriteEnvFile(path string) (map[string]string, error) {
	text, environment, err := c.RenderWithEnvSecrets()
	if err != nil {
		return nil, err
	}
	if err := writeText(path, text); err != nil {
		return nil, err
	}
	return environment, nil
}

func writeText(path, text string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, []byte(text), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func secretEnvironmentName(sourceID string) string {
	var b strings.Builder
	b.WriteString("KAIWU_STCP_")
	for _, r := range strings.ToUpper(sourceID) {
		switch {
		case r >= 'A' && r <= 'Z', r >= '0' && r <= '9':
			b.WriteRune(r)
		default:
			b.WriteByte('_')
		}
	}
	return b.String()
}

func quote(value string) string {
	replacer := strings.NewReplacer("\\", "\\\\", "\"", "\\\"", "\n", "\\n", "\r", "\\r", "\t", "\\t")
	return "\"" + replacer.Replace(value) + "\""
}
