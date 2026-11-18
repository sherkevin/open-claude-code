//go:build windows

package secretfile

import (
	"bytes"
	"testing"
)

func TestDPAPIRoundTrip(t *testing.T) {
	raw := []byte("synthetic Windows DPAPI material")
	sealed, err := protect(raw)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(sealed, raw) {
		t.Fatal("DPAPI 密文仍包含明文")
	}
	opened, err := unprotect(sealed)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(opened, raw) {
		t.Fatalf("DPAPI 往返不一致: %q", opened)
	}
}
