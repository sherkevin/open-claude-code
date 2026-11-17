package devicekey

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"os"
	"path/filepath"
	"testing"
)

func TestLoadOrCreatePersistsAndSigns(t *testing.T) {
	path := filepath.Join(t.TempDir(), "device_key.json")
	first, created, err := LoadOrCreate(path)
	if err != nil {
		t.Fatal(err)
	}
	if !created {
		t.Fatal("首次加载应创建设备密钥")
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("设备密钥文件权限应为 0600: %o", info.Mode().Perm())
	}

	second, created, err := LoadOrCreate(path)
	if err != nil {
		t.Fatal(err)
	}
	if created {
		t.Fatal("已有设备密钥不应重新生成")
	}
	if first.PublicKey() != second.PublicKey() || first.KeyID() != second.KeyID() {
		t.Fatal("重启后设备身份发生变化")
	}

	body := []byte(`{"synthetic":true}`)
	timestamp, nonce, signatureText, err := first.SignRequest("POST", "/api/kaiwu/v1/sources", body)
	if err != nil {
		t.Fatal(err)
	}
	publicRaw, err := base64.RawURLEncoding.DecodeString(first.PublicKey())
	if err != nil {
		t.Fatal(err)
	}
	signature, err := base64.RawURLEncoding.DecodeString(signatureText)
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(body)
	message := []byte(timestamp + "\n" + nonce + "\nPOST\n/api/kaiwu/v1/sources\n" +
		fmtDigest(digest))
	if !ed25519.Verify(ed25519.PublicKey(publicRaw), message, signature) {
		t.Fatal("设备签名无法验证")
	}

	_, secondNonce, _, err := first.SignRequest("POST", "/api/kaiwu/v1/sources", body)
	if err != nil {
		t.Fatal(err)
	}
	if secondNonce == nonce {
		t.Fatal("每次签名必须使用新 nonce")
	}
}

func fmtDigest(value [32]byte) string {
	const digits = "0123456789abcdef"
	out := make([]byte, len(value)*2)
	for index, item := range value {
		out[index*2] = digits[item>>4]
		out[index*2+1] = digits[item&0x0f]
	}
	return string(out)
}
