// Package huaweiauth builds the per-call authentication headers required by
// HwMain MCP endpoints.
package huaweiauth

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"strconv"
	"strings"
	"time"
)

// Profile is the local-only identity and signing material for one source.
type Profile struct {
	AppID   string `json:"app_id"`
	Channel string `json:"channel"`
	UserID  string `json:"user_id"`
	HMACKey string `json:"hmac_key"`
}

// Validate checks values that are inserted into HTTP headers or the HMAC
// canonical string. It deliberately does not log any supplied value.
func Validate(profile Profile) error {
	for name, value := range map[string]string{
		"appid":   profile.AppID,
		"channel": profile.Channel,
		"userid":  profile.UserID,
	} {
		if strings.TrimSpace(value) == "" {
			return fmt.Errorf("缺少华微渠道字段 %s", name)
		}
		if len(value) > 256 || strings.ContainsAny(value, "\r\n&") {
			return fmt.Errorf("华微渠道字段 %s 格式无效", name)
		}
	}
	if strings.TrimSpace(profile.HMACKey) == "" || len(profile.HMACKey) > 4096 || strings.ContainsAny(profile.HMACKey, "\r\n") {
		return errors.New("HMAC 密钥不能为空、超长或包含换行符")
	}
	return nil
}

// SignedHeaders generates the six headers required for a tools/call request.
// A fresh UUID and timestamp are generated for every forwarded request.
func SignedHeaders(profile Profile, now time.Time, entropy io.Reader) (map[string]string, error) {
	if err := Validate(profile); err != nil {
		return nil, err
	}
	if entropy == nil {
		entropy = rand.Reader
	}
	requestID, err := newUUIDv4(entropy)
	if err != nil {
		return nil, fmt.Errorf("生成 requestId 失败: %w", err)
	}
	timestamp := now.UTC().Unix()
	canonical := CanonicalString(profile, requestID, timestamp)
	mac := hmac.New(sha256.New, []byte(profile.HMACKey))
	_, _ = mac.Write([]byte(canonical))
	return map[string]string{
		"appid":     profile.AppID,
		"channel":   profile.Channel,
		"userid":    profile.UserID,
		"requestId": requestID,
		"timestamp": strconv.FormatInt(timestamp, 10),
		"sign":      hex.EncodeToString(mac.Sum(nil)),
	}, nil
}

// CanonicalString returns the exact canonical value defined by the current
// HwMain integration contract.
func CanonicalString(profile Profile, requestID string, timestamp int64) string {
	return "appid=" + profile.AppID +
		"&channel=" + profile.Channel +
		"&userid=" + profile.UserID +
		"&requestId=" + requestID +
		"&timestamp=" + strconv.FormatInt(timestamp, 10)
}

func newUUIDv4(reader io.Reader) (string, error) {
	raw := make([]byte, 16)
	if _, err := io.ReadFull(reader, raw); err != nil {
		return "", err
	}
	raw[6] = (raw[6] & 0x0f) | 0x40
	raw[8] = (raw[8] & 0x3f) | 0x80
	return fmt.Sprintf("%s-%s-%s-%s-%s",
		hex.EncodeToString(raw[0:4]),
		hex.EncodeToString(raw[4:6]),
		hex.EncodeToString(raw[6:8]),
		hex.EncodeToString(raw[8:10]),
		hex.EncodeToString(raw[10:16]),
	), nil
}
