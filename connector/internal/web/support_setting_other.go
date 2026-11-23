//go:build !windows

package web

import "os"

func machineSupportPortalSetting() string {
	return os.Getenv("KAIWU_SUPPORT_PORTAL_URL")
}
