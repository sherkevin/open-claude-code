//go:build linux || darwin || freebsd || openbsd || netbsd || dragonfly

package diagnostics

import (
	"errors"
	"os"

	"golang.org/x/sys/unix"
)

func lockReportingFile(file *os.File) error {
	err := unix.Flock(int(file.Fd()), unix.LOCK_EX|unix.LOCK_NB)
	if errors.Is(err, unix.EWOULDBLOCK) {
		return ErrReportingBusy
	}
	return err
}
