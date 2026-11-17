//go:build !windows

package main

// runServiceIfNeeded 在非 Windows 平台不启用服务模式。
func runServiceIfNeeded() bool {
	return false
}
