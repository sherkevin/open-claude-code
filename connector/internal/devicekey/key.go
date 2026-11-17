// Package devicekey owns the connector's Ed25519 device identity.
//
// The private key is local-only and is never sent to the cloud. Every device
// control-plane request is signed over the exact method, path and body digest.
// Persistence uses an atomic 0600 file; platform-specific DPAPI wrapping remains
// a separate Windows acceptance requirement.
package devicekey

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"time"
)

type document struct {
	SchemaVersion int    `json:"schema_version"`
	PrivateKey    string `json:"private_key_pkcs8"`
}

// Key is a loaded device signing identity.
type Key struct {
	private ed25519.PrivateKey
	public  ed25519.PublicKey
	keyID   string
}

// LoadOrCreate loads an existing private key or creates one atomically.
func LoadOrCreate(path string) (*Key, bool, error) {
	var stored document
	raw, err := os.ReadFile(path)
	switch {
	case err == nil:
		if err := json.Unmarshal(raw, &stored); err != nil {
			return nil, false, fmt.Errorf("读取设备密钥失败: %w", err)
		}
		encoded, err := base64.RawURLEncoding.DecodeString(stored.PrivateKey)
		if err != nil {
			return nil, false, fmt.Errorf("设备密钥编码无效: %w", err)
		}
		parsed, err := x509.ParsePKCS8PrivateKey(encoded)
		if err != nil {
			return nil, false, fmt.Errorf("设备密钥格式无效: %w", err)
		}
		private, ok := parsed.(ed25519.PrivateKey)
		if !ok {
			return nil, false, errors.New("设备密钥不是 Ed25519")
		}
		return fromPrivate(private), false, nil
	case errors.Is(err, fs.ErrNotExist):
		// Continue below and create a new identity.
	default:
		return nil, false, err
	}

	_, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return nil, false, err
	}
	encoded, err := x509.MarshalPKCS8PrivateKey(private)
	if err != nil {
		return nil, false, err
	}
	stored = document{
		SchemaVersion: 1,
		PrivateKey:    base64.RawURLEncoding.EncodeToString(encoded),
	}
	raw, err = json.MarshalIndent(stored, "", "  ")
	if err != nil {
		return nil, false, err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, false, err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o600); err != nil {
		return nil, false, err
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return nil, false, err
	}
	return fromPrivate(private), true, nil
}

func fromPrivate(private ed25519.PrivateKey) *Key {
	public := append(ed25519.PublicKey(nil), private.Public().(ed25519.PublicKey)...)
	digest := sha256.Sum256(public)
	return &Key{
		private: append(ed25519.PrivateKey(nil), private...),
		public:  public,
		keyID:   base64.RawURLEncoding.EncodeToString(digest[:]),
	}
}

// PublicKey returns the canonical base64url raw public key.
func (k *Key) PublicKey() string {
	return base64.RawURLEncoding.EncodeToString(k.public)
}

// KeyID returns the SHA-256 thumbprint of the raw public key.
func (k *Key) KeyID() string {
	return k.keyID
}

// SignRequest signs timestamp, nonce, method, exact path and body digest.
func (k *Key) SignRequest(method, path string, body []byte) (timestamp, nonce, signature string, err error) {
	nonceRaw := make([]byte, 24)
	if _, err := rand.Read(nonceRaw); err != nil {
		return "", "", "", err
	}
	nonce = base64.RawURLEncoding.EncodeToString(nonceRaw)
	timestamp = fmt.Sprintf("%d", time.Now().UTC().Unix())
	bodyDigest := sha256.Sum256(body)
	message := []byte(timestamp + "\n" + nonce + "\n" + method + "\n" + path + "\n" +
		fmt.Sprintf("%x", bodyDigest))
	signature = base64.RawURLEncoding.EncodeToString(ed25519.Sign(k.private, message))
	return timestamp, nonce, signature, nil
}
