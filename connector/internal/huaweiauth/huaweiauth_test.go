package huaweiauth

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"strconv"
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

func TestOfflineVectorAndShortProtocolValidKey(t *testing.T) {
	profile := Profile{
		AppID: "MCP", Channel: "demo", UserID: "u01", HMACKey: "demo-key-only",
	}
	requestID := "00000000-0000-4000-8000-000000000001"
	canonical := CanonicalString(profile, requestID, 1700000000)
	if canonical != "appid=MCP&channel=demo&userid=u01&requestId=00000000-0000-4000-8000-000000000001&timestamp=1700000000" {
		t.Fatalf("固定向量原文不符: %s", canonical)
	}
	mac := hmac.New(sha256.New, []byte(profile.HMACKey))
	_, _ = mac.Write([]byte(canonical))
	if got := hex.EncodeToString(mac.Sum(nil)); got != "650d825ca0684ad7cf57ca0ca26a44536b6a4bf263fbf944e79c115b72bf9e51" {
		t.Fatalf("固定向量摘要不符: %s", got)
	}
	if err := Validate(profile); err != nil {
		t.Fatalf("协议有效的短密钥不应被拒绝: %v", err)
	}
	if _, err := SignedHeaders(profile, time.Unix(1700000000, 0), bytes.NewReader(make([]byte, 16))); err != nil {
		t.Fatalf("短密钥签名失败: %v", err)
	}
}

// verifyOffline deliberately rebuilds the canonical string instead of calling
// CanonicalString, so the negative tests do not trust the signer's own helper.
func verifyOffline(profile Profile, headers map[string]string, now time.Time, window time.Duration) error {
	for name, want := range map[string]string{
		"appid": profile.AppID, "channel": profile.Channel, "userid": profile.UserID,
	} {
		if headers[name] != want {
			return fmt.Errorf("identity header %s does not match", name)
		}
	}
	requestID := headers["requestId"]
	timestampRaw := headers["timestamp"]
	if requestID == "" || timestampRaw == "" || headers["sign"] == "" {
		return errors.New("missing signed header")
	}
	timestamp, err := strconv.ParseInt(timestampRaw, 10, 64)
	if err != nil {
		return errors.New("invalid timestamp")
	}
	if timestamp < now.Add(-window).Unix() || timestamp > now.Add(window).Unix() {
		return errors.New("timestamp outside accepted window")
	}
	canonical := "appid=" + profile.AppID +
		"&channel=" + profile.Channel +
		"&userid=" + profile.UserID +
		"&requestId=" + requestID +
		"&timestamp=" + timestampRaw
	expected := hmac.New(sha256.New, []byte(profile.HMACKey))
	_, _ = expected.Write([]byte(canonical))
	if !hmac.Equal([]byte(headers["sign"]), []byte(hex.EncodeToString(expected.Sum(nil)))) {
		return errors.New("signature mismatch")
	}
	return nil
}

func TestOfflineRejectsWrongKeyExpiredWindowAndForgedIdentity(t *testing.T) {
	profile := Profile{
		AppID: "MCP", Channel: "demo", UserID: "u01", HMACKey: "demo-key-only",
	}
	signedAt := time.Unix(1700000000, 0)
	headers, err := SignedHeaders(profile, signedAt, bytes.NewReader(make([]byte, 16)))
	if err != nil {
		t.Fatal(err)
	}
	window := 5 * time.Minute
	if err := verifyOffline(profile, headers, signedAt, window); err != nil {
		t.Fatalf("fresh correctly signed headers should pass: %v", err)
	}

	wrongKey := profile
	wrongKey.HMACKey = "different-key"
	if err := verifyOffline(wrongKey, headers, signedAt, window); err == nil {
		t.Fatal("wrong HMAC key must be rejected")
	}

	stale := signedAt.Add(window + time.Second)
	if err := verifyOffline(profile, headers, stale, window); err == nil {
		t.Fatal("stale timestamp must be rejected")
	}
	future := signedAt.Add(-window - time.Second)
	if err := verifyOffline(profile, headers, future, window); err == nil {
		t.Fatal("future timestamp must be rejected")
	}

	forged := map[string]string{}
	for key, value := range headers {
		forged[key] = value
	}
	forged["channel"] = "attacker-channel"
	if err := verifyOffline(profile, forged, signedAt, window); err == nil {
		t.Fatal("forged identity header must be rejected")
	}
}
