//go:build !windows

package agent

import (
	"context"
	"os/exec"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"
)

func newAssistCommand(ctx context.Context, command string) *exec.Cmd {
	process := exec.CommandContext(ctx, "/bin/sh", "-c", command)
	process.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	process.Cancel = func() error {
		if process.Process == nil {
			return nil
		}
		_ = syscall.Kill(-process.Process.Pid, syscall.SIGKILL)
		return process.Process.Kill()
	}
	process.WaitDelay = 5 * time.Second
	return process
}

func decodeAssistOutput(raw []byte) string {
	if utf8.Valid(raw) {
		return string(raw)
	}
	return strings.ToValidUTF8(string(raw), "�")
}
