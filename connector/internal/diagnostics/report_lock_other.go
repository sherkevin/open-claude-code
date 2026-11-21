//go:build !windows && !linux && !darwin && !freebsd && !openbsd && !netbsd && !dragonfly

package diagnostics

import "os"

// The installation journal reader is unavailable on these platforms.
func lockReportingFile(*os.File) error { return nil }
