//go:build !windows && !linux

package diagnostics

import (
	"os"
	"time"
)

const installReadFlags = os.O_RDONLY

// The installer writes journals on Windows. Linux supplies a local test
// implementation. Other systems lack a verified source of file creation time.
func installCreated(*os.File) time.Time { return time.Time{} }

func installDirectoryTrusted(*os.File, os.FileInfo) bool { return false }
