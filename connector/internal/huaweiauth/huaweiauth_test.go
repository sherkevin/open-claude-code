package huaweiauth

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"strings"
	"testing"
	"time"
)

func TestSignedHeadersCanonicalStringAndUUID(t *testing.T) {
	profile := Profile{
		AppID:   "app-demo",
		Channel: "channel-demo",
		UserID:  "user-demo",
		HMACKey: "0123456789abcdef0123456789abcdef",
	}
	now := time.Unix(1700000000, 0)
	headers, err := SignedHeaders(profile, now, bytes.NewReader(make([]byte, 16)))
	if err != nil {
		t.Fatal(err)
	}
	if got := headers["requestId"]; got != "00000000-0000-4000-8000-000000000000" {
		t.Fatalf("requestId 不是 UUID v4: %s", got)
	}
	if got := headers["timestamp"]; got != "1700000000" {
		t.Fatalf("timestamp 不符: %s", got)
	}
	canonical := CanonicalString(profile, headers["requestId"], 1700000000)
	if canonical != "appid=app-demo&channel=channel-demo&userid=user-demo&requestId=00000000-0000-4000-8000-000000000000&timestamp=1700000000" {
		t.Fatalf("签名原文顺序不符: %s", canonical)
	}
	mac := hmac.New(sha256.New, []byte(profile.HMACKey))
	_, _ = mac.Write([]byte(canonical))
	if got, want := headers["sign"], hex.EncodeToString(mac.Sum(nil)); got != want {
		t.Fatalf("签名不符: got %s want %s", got, want)
	}
}

func TestValidateRejectsHeaderInjection(t *testing.T) {
	err := Validate(Profile{
		AppID: "app\r\nInjected: value", Channel: "channel", UserID: "user",
		HMACKey: "0123456789abcdef",
	})
	if err == nil || !strings.Contains(err.Error(), "appid") {
		t.Fatalf("应拒绝渠道字段注入: %v", err)
	}
}
