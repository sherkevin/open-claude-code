package state

import "testing"

func TestHappyPathTransitions(t *testing.T) {
	machine := NewMachine(Unpaired)
	for _, next := range []State{Configuring, Connecting, Ready, Draining, Connecting, Ready, Paused, Revoked} {
		if !machine.Transition(next, "测试") {
			t.Fatalf("迁移到 %s 失败，当前 %s", next, machine.Current())
		}
	}
}

func TestRejectsIllegalTransition(t *testing.T) {
	machine := NewMachine(Unpaired)
	if machine.Transition(Ready, "跳过绑定") {
		t.Fatal("未绑定不能直接进入已连接")
	}
	if machine.Current() != Unpaired {
		t.Fatalf("非法迁移不应改变状态: %s", machine.Current())
	}
}

func TestRevokedIsTerminal(t *testing.T) {
	machine := NewMachine(Unpaired)
	machine.Transition(Revoked, "撤销")
	if machine.Transition(Ready, "恢复") {
		t.Fatal("已撤销不能直接恢复")
	}
}

func TestObserverReceivesChange(t *testing.T) {
	machine := NewMachine(Unpaired)
	var from, to State
	machine.SetObserver(func(f, t State, reason string) {
		from, to = f, t
	})
	machine.Transition(Configuring, "已绑定")
	if from != Unpaired || to != Configuring {
		t.Fatalf("回调收到 %s -> %s", from, to)
	}
}
