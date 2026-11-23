//go:build windows

package web

import (
	"os"

	"golang.org/x/sys/windows/registry"
	"golang.org/x/sys/windows/svc"
)

func machineSupportPortalSetting() string {
	const name = "KAIWU_SUPPORT_PORTAL_URL"
	service, err := svc.IsWindowsService()
	if err != nil {
		return ""
	}
	if !service {
		return os.Getenv(name)
	}

	// SCM can retain an older process environment across service restarts.
	// Read the current machine value, including an administrator's removal.
	key, err := registry.OpenKey(registry.LOCAL_MACHINE, `SYSTEM\CurrentControlSet\Control\Session Manager\Environment`, registry.QUERY_VALUE)
	if err != nil {
		return ""
	}
	defer key.Close()
	value, _, err := key.GetStringValue(name)
	if err != nil {
		return ""
	}
	return value
}
