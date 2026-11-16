// Package supervisor 管理固定版本 frpc 子进程。
//
// 依据文档第 04、10、11 章：
//   - frpc 作为独立进程运行，连接器负责启动、守护、停止和重启；
//   - 网络故障采用带随机抖动的指数退避（1 秒起，上限 30 秒）；
//   - 身份或配置错误不无限重试，交由人工修复后重新检测。
package supervisor

import (
	"context"
	"errors"
	"fmt"
	"math/rand"
	"os"
	"os/exec"
	"sync"
	"time"
)

// Supervisor 守护一个 frpc 进程。
type Supervisor struct {
	BinaryPath string
	ConfigPath string
	// OnChange 在进程启动或退出时回调，running 表示当前是否有进程在跑。
	OnChange func(running bool, detail string)

	mu       sync.Mutex
	cmd      *exec.Cmd
	running  bool
	restarts int
	lastErr  string
	stopping bool
	cancel   context.CancelFunc
	doneCh   chan struct{}
}

// New 创建守护器。
func New(binaryPath, configPath string) *Supervisor {
	return &Supervisor{BinaryPath: binaryPath, ConfigPath: configPath}
}

// Start 启动守护循环；重复调用无副作用。
func (s *Supervisor) Start(parent context.Context) error {
	if _, err := os.Stat(s.BinaryPath); err != nil {
		return fmt.Errorf("找不到 frpc 程序: %s", s.BinaryPath)
	}
	if _, err := os.Stat(s.ConfigPath); err != nil {
		return fmt.Errorf("找不到 frpc 配置: %s", s.ConfigPath)
	}

	s.mu.Lock()
	if s.running {
		s.mu.Unlock()
		return nil
	}
	ctx, cancel := context.WithCancel(parent)
	s.cancel = cancel
	s.stopping = false
	s.doneCh = make(chan struct{})
	done := s.doneCh
	s.mu.Unlock()

	go s.loop(ctx, done)
	return nil
}

func (s *Supervisor) loop(ctx context.Context, done chan struct{}) {
	defer close(done)
	backoff := time.Second
	for {
		if ctx.Err() != nil {
			return
		}
		started := time.Now()
		cmd := exec.CommandContext(ctx, s.BinaryPath, "-c", s.ConfigPath)
		cmd.Env = minimalEnv()
		stderr, err := cmd.StderrPipe()
		if err != nil {
			s.fail(err)
			return
		}
		if err := cmd.Start(); err != nil {
			s.fail(err)
			if !s.sleep(ctx, backoff) {
				return
			}
			backoff = nextBackoff(backoff)
			continue
		}

		s.mu.Lock()
		s.cmd = cmd
		s.running = true
		s.mu.Unlock()
		s.notify(true, "frpc 已启动")

		go drainStderr(stderr, s)

		waitErr := cmd.Wait()

		s.mu.Lock()
		s.running = false
		s.cmd = nil
		s.mu.Unlock()

		if ctx.Err() != nil || s.isStopping() {
			s.notify(false, "frpc 已停止")
			return
		}

		detail := "frpc 进程退出"
		if waitErr != nil {
			detail += ": " + waitErr.Error()
		}
		s.mu.Lock()
		s.restarts++
		s.lastErr = detail
		s.mu.Unlock()
		s.notify(false, detail)

		// 稳定运行超过 30 秒后重置退避，避免偶发断线越退越久。
		if time.Since(started) > 30*time.Second {
			backoff = time.Second
		}
		if !s.sleep(ctx, backoff) {
			return
		}
		backoff = nextBackoff(backoff)
	}
}

// Stop 优雅停止；超时后强制结束。
func (s *Supervisor) Stop(ctx context.Context) error {
	s.mu.Lock()
	if s.doneCh == nil {
		s.mu.Unlock()
		return nil
	}
	s.stopping = true
	cancel := s.cancel
	done := s.doneCh
	cmd := s.cmd
	s.mu.Unlock()

	if cancel != nil {
		cancel()
	}
	if cmd != nil && cmd.Process != nil {
		_ = cmd.Process.Signal(os.Interrupt)
	}

	select {
	case <-done:
		return nil
	case <-ctx.Done():
		if cmd != nil && cmd.Process != nil {
			_ = cmd.Process.Kill()
		}
		return ctx.Err()
	}
}

// Running 返回当前是否有 frpc 在跑。
func (s *Supervisor) Running() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.running
}

// Restarts 返回累计重启次数。
func (s *Supervisor) Restarts() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.restarts
}

// LastError 返回最近一次退出原因。
func (s *Supervisor) LastError() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.lastErr
}

func (s *Supervisor) isStopping() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.stopping
}

func (s *Supervisor) notify(running bool, detail string) {
	if s.OnChange != nil {
		s.OnChange(running, detail)
	}
}

func (s *Supervisor) fail(err error) {
	s.mu.Lock()
	s.lastErr = err.Error()
	s.mu.Unlock()
	s.notify(false, err.Error())
}

func (s *Supervisor) sleep(ctx context.Context, d time.Duration) bool {
	jitter := time.Duration(rand.Int63n(int64(d/4) + 1))
	select {
	case <-time.After(d + jitter):
		return true
	case <-ctx.Done():
		return false
	}
}

func nextBackoff(current time.Duration) time.Duration {
	next := current * 2
	if next > 30*time.Second {
		next = 30 * time.Second
	}
	return next
}

func drainStderr(reader interface{ Read([]byte) (int, error) }, s *Supervisor) {
	buf := make([]byte, 4096)
	for {
		n, err := reader.Read(buf)
		if n > 0 {
			s.mu.Lock()
			s.lastErr = trim(string(buf[:n]))
			s.mu.Unlock()
		}
		if err != nil {
			return
		}
	}
}

func trim(value string) string {
	if len(value) > 400 {
		return value[:400]
	}
	return value
}

func minimalEnv() []string {
	env := []string{"PATH=" + os.Getenv("PATH")}
	for _, key := range []string{"SYSTEMROOT", "WINDIR", "TEMP", "TMP"} {
		if value := os.Getenv(key); value != "" {
			env = append(env, key+"="+value)
		}
	}
	return env
}

// ErrStopped 表示守护循环已结束。
var ErrStopped = errors.New("frpc 守护已停止")
