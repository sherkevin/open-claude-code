// Package secretfile stores local connector secrets with Windows DPAPI when
// available. Cross-platform builds keep the same atomic-write contract for
// tests and diagnostics, but production Windows packages use machine-scoped
// DPAPI plus directory ACLs.
package secretfile

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
)

const protectedPrefix = "KAIWU-DPAPI-V1\n"

// EncryptionSupported reports whether this build protects file contents with
// the operating-system secret facility.
var EncryptionSupported = platformEncryptionSupported

// Read returns plaintext payload and whether the file was encrypted.
func Read(path string) ([]byte, bool, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, false, err
	}
	if !bytes.HasPrefix(raw, []byte(protectedPrefix)) {
		return raw, false, nil
	}
	payload, err := unprotect(raw[len(protectedPrefix):])
	if err != nil {
		return nil, true, fmt.Errorf("解密本机秘密失败: %w", err)
	}
	return payload, true, nil
}

// Write atomically replaces a file with its protected representation.
func Write(path string, raw []byte) error {
	payload, err := protect(raw)
	if err != nil {
		return fmt.Errorf("加密本机秘密失败: %w", err)
	}
	content := payload
	if EncryptionSupported {
		content = append([]byte(protectedPrefix), payload...)
	}
	return writeAtomic(path, content)
}

// ReadJSON loads a sensitive JSON document and migrates an existing plaintext
// file atomically when platform encryption is available.
func ReadJSON(path string, target any) (bool, error) {
	raw, encrypted, err := Read(path)
	if err != nil {
		return false, err
	}
	if err := json.Unmarshal(raw, target); err != nil {
		return encrypted, err
	}
	if !encrypted && EncryptionSupported {
		if err := Write(path, raw); err != nil {
			return false, err
		}
		return true, nil
	}
	return false, nil
}

// WriteJSON writes an indented sensitive JSON document atomically.
func WriteJSON(path string, value any) error {
	raw, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	return Write(path, raw)
}

func writeAtomic(path string, content []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	tmp := path + ".tmp"
	file, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	removeTemp := true
	defer func() {
		_ = file.Close()
		if removeTemp {
			_ = os.Remove(tmp)
		}
	}()
	if _, err := file.Write(content); err != nil {
		return err
	}
	if err := file.Sync(); err != nil {
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		return err
	}
	removeTemp = false
	return nil
}

// ErrUnsupported is returned when a Windows-protected file is opened by a
// build that cannot access DPAPI.
var ErrUnsupported = errors.New("当前平台不支持本机秘密解密")

// ReadOptionalJSON returns (false, nil) when the file does not exist.
func ReadOptionalJSON(path string, target any) (bool, error) {
	encrypted, err := ReadJSON(path, target)
	if errors.Is(err, fs.ErrNotExist) {
		return false, nil
	}
	return encrypted, err
}
