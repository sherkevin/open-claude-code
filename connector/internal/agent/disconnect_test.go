package agent

import (
	"context"
	"testing"

	"github.com/sherkevin/open-claude-code/connector/internal/state"
)

func TestDisconnectFromReadyReturnsToConfiguring(t *testing.T) {
	instance, err := New(Options{DataDir: t.TempDir()})
	if err != nil {
		t.Fatalf("创建连接器失败: %v", err)
	}
	instance.state = state.NewMachine(state.Ready)

	status, err := instance.Disconnect(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if status.State != state.Configuring {
		t.Fatalf("断开后应回到 CONFIGURING，实际 %s", status.State)
	}
}

func TestDisconnectLeavesUnpairedConnectorUnpaired(t *testing.T) {
	instance, err := New(Options{DataDir: t.TempDir()})
	if err != nil {
		t.Fatalf("创建连接器失败: %v", err)
	}

	status, err := instance.Disconnect(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if status.State != state.Unpaired {
		t.Fatalf("未绑定连接器断开后仍应为 UNPAIRED，实际 %s", status.State)
	}
}
