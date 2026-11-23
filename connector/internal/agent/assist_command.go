package agent

import (
	"bytes"
	"context"
	"errors"
	"os/exec"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

const (
	assistCommandOutputLimit    = 64 * 1024
	assistCommandDefaultTimeout = 60 * time.Second
	assistCommandMaxTimeout     = 120 * time.Second
)

// assistCommandExecution is the result of one command the control plane sent.
type assistCommandExecution struct {
	status    string
	code      string
	state     string
	output    string
	exitCode  *int
	truncated bool
	duration  time.Duration
}

// runAssistCommand executes one command with a bounded timeout and output size.
// The command is chosen by the Kaiwu engineer inside an assistance session the
// device owner started; the local UI and the terminal always show it.
func (a *Agent) runAssistCommand(parent context.Context, command string, timeoutSeconds int) assistCommandExecution {
	result := assistCommandExecution{status: "failed", code: "command_failed", state: "failed"}
	command = strings.TrimSpace(command)
	if command == "" || utf8.RuneCountInString(command) > 4000 || strings.IndexByte(command, 0) >= 0 {
		result.output = "命令为空或格式无效"
		return result
	}
	timeout := time.Duration(timeoutSeconds) * time.Second
	if timeout <= 0 {
		timeout = assistCommandDefaultTimeout
	}
	if timeout > assistCommandMaxTimeout {
		timeout = assistCommandMaxTimeout
	}
	ctx, cancel := context.WithTimeout(parent, timeout)
	defer cancel()

	writer := &assistOutputBuffer{limit: assistCommandOutputLimit}
	process := newAssistCommand(ctx, command)
	process.Stdout, process.Stderr = writer, writer
	started := time.Now()
	err := process.Run()
	result.duration = time.Since(started)
	result.output = decodeAssistOutput(writer.Bytes())
	result.truncated = writer.Truncated()

	switch {
	case parent.Err() != nil:
		result.state, result.code = "canceled", "canceled"
		return result
	case ctx.Err() == context.DeadlineExceeded:
		result.state, result.code = "timeout", "command_timeout"
		return result
	case err != nil:
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			code := exitErr.ExitCode()
			result.exitCode = &code
		} else if result.output == "" {
			result.output = err.Error()
		}
		return result
	}
	zero := 0
	result.exitCode = &zero
	result.status, result.code, result.state = "succeeded", "command_completed", "succeeded"
	return result
}

type assistOutputBuffer struct {
	mu        sync.Mutex
	buffer    bytes.Buffer
	limit     int
	truncated bool
}

func (b *assistOutputBuffer) Write(data []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	remaining := b.limit - b.buffer.Len()
	if remaining > 0 {
		if len(data) <= remaining {
			_, _ = b.buffer.Write(data)
		} else {
			_, _ = b.buffer.Write(data[:remaining])
			b.truncated = true
		}
	} else if len(data) > 0 {
		b.truncated = true
	}
	// Always report the full write so the child process is never blocked.
	return len(data), nil
}

func (b *assistOutputBuffer) Bytes() []byte {
	b.mu.Lock()
	defer b.mu.Unlock()
	return append([]byte(nil), b.buffer.Bytes()...)
}

func (b *assistOutputBuffer) Truncated() bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.truncated
}
