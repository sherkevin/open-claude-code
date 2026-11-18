//go:build !windows

package secretfile

const platformEncryptionSupported = false

func protect(raw []byte) ([]byte, error) {
	return raw, nil
}

func unprotect(raw []byte) ([]byte, error) {
	return nil, ErrUnsupported
}
