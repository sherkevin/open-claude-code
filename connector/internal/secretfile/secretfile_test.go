package secretfile

import (
	"os"
	"path/filepath"
	"testing"
)

func TestProtectedJSONRoundTripAndPrivateMode(t *testing.T) {
	path := filepath.Join(t.TempDir(), "secrets.json")
	value := map[string]string{"secret": "synthetic-value"}
	if err := WriteJSON(path, value); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("秘密文件权限应为 0600: %o", info.Mode().Perm())
	}
	var loaded map[string]string
	if _, err := ReadJSON(path, &loaded); err != nil {
		t.Fatal(err)
	}
	if loaded["secret"] != value["secret"] {
		t.Fatalf("读取结果不符: %+v", loaded)
	}
}

func TestLegacyPlaintextJSONIsReadable(t *testing.T) {
	path := filepath.Join(t.TempDir(), "identity.json")
	if err := os.WriteFile(path, []byte(`{"device_token":"synthetic"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	var loaded map[string]string
	encrypted, err := ReadJSON(path, &loaded)
	if err != nil {
		t.Fatal(err)
	}
	if loaded["device_token"] != "synthetic" {
		t.Fatalf("旧明文秘密未读取: %+v", loaded)
	}
	if encrypted != EncryptionSupported {
		t.Fatalf("迁移状态不符: encrypted=%v supported=%v", encrypted, EncryptionSupported)
	}
}
