//go:build linux || darwin || freebsd || openbsd || netbsd || dragonfly

package diagnostics

import (
	"os"

	"golang.org/x/sys/unix"
)

func lockReportingFile(file *os.File) error {
	return unix.Flock(int(file.Fd()), unix.LOCK_EX|unix.LOCK_NB)
}
