//go:build windows

package agent

import (
	"context"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"

	"golang.org/x/sys/windows"
)

const assistCreateNoWindow = 0x08000000

func newAssistCommand(ctx context.Context, command string) *exec.Cmd {
	// chcp keeps CMD built-ins and most console programs in UTF-8. Programs that
	// still emit GBK are decoded in decodeAssistOutput.
	payload := "chcp 65001 >NUL & " + command
	process := exec.CommandContext(ctx, "cmd.exe")
	process.Args = []string{"cmd.exe", "/d", "/s", "/c", payload}
	process.SysProcAttr = &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: assistCreateNoWindow,
		CmdLine:       `cmd.exe /d /s /c "` + payload + `"`,
	}
	process.Cancel = func() error {
		if process.Process == nil {
			return nil
		}
		kill := exec.Command("taskkill", "/T", "/F", "/PID", strconv.Itoa(process.Process.Pid))
		kill.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: assistCreateNoWindow}
		_ = kill.Run()
		return process.Process.Kill()
	}
	process.WaitDelay = 5 * time.Second
	return process
}

func decodeAssistOutput(raw []byte) string {
	if len(raw) == 0 || utf8.Valid(raw) {
		return string(raw)
	}
	if len(raw) > 128*1024 {
		raw = raw[:128*1024]
	}
	count, err := windows.MultiByteToWideChar(936, 0, &raw[0], int32(len(raw)), nil, 0)
	if err != nil || count <= 0 {
		return strings.ToValidUTF8(string(raw), "�")
	}
	wide := make([]uint16, count)
	if _, err := windows.MultiByteToWideChar(936, 0, &raw[0], int32(len(raw)), &wide[0], count); err != nil {
		return strings.ToValidUTF8(string(raw), "�")
	}
	return windows.UTF16ToString(wide)
}
