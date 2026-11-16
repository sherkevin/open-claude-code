package supervisor

import (
	"context"
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"
)

// 回归：界面上的连接请求返回后，HTTP 请求上下文会被取消，
// frpc 不能因此被连带杀掉，否则用户会看到「刚连上就断开」。
func TestStartDetachesFromParentCancellation(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("用 shell 脚本模拟 frpc，仅在类 Unix 上运行")
	}
	dir := t.TempDir()
	binary := filepath.Join(dir, "fake-frpc")
	if err := os.WriteFile(binary, []byte("#!/bin/sh\nsleep 30\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	config := filepath.Join(dir, "frpc.toml")
	if err := os.WriteFile(config, []byte(""), 0o600); err != nil {
		t.Fatal(err)
	}

	instance := New(binary, config)
	parent, cancelParent := context.WithCancel(context.Background())
	if err := instance.Start(parent); err != nil {
		t.Fatal(err)
	}
	cancelParent()
	time.Sleep(300 * time.Millisecond)
	if !instance.Running() {
		t.Fatal("父上下文取消后 frpc 不应被停止")
	}

	stopCtx, cancelStop := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancelStop()
	if err := instance.Stop(stopCtx); err != nil {
		t.Fatal(err)
	}
	if instance.Running() {
		t.Fatal("显式停止后 frpc 应退出")
	}
}
