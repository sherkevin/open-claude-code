// Package state 实现连接器的运行状态机。
//
// 状态定义与进入/退出条件见《企业内网 MCP 接入程序 开发全景文档 v1.0》第 10 章。
// 单纯端口连通、网页返回 200 或 frpc 进程存在，都不能直接进入 READY。
package state

import "sync"

// State 是连接器的运行状态。
type State string

const (
	Unpaired         State = "UNPAIRED"
	Configuring      State = "CONFIGURING"
	Connecting       State = "CONNECTING"
	Ready            State = "READY"
	DegradedUpstream State = "DEGRADED_UPSTREAM"
	DegradedRelay    State = "DEGRADED_RELAY"
	Paused           State = "PAUSED"
	Draining         State = "DRAINING"
	Revoked          State = "REVOKED"
)

var labels = map[State]string{
	Unpaired:         "尚未绑定",
	Configuring:      "待配置",
	Connecting:       "正在连接",
	Ready:            "已连接可使用",
	DegradedUpstream: "内网 MCP 暂不可用",
	DegradedRelay:    "公网连接暂不可用",
	Paused:           "已暂停",
	Draining:         "准备维护",
	Revoked:          "已撤销",
}

// Label 返回给客户看的中文提示。
func (s State) Label() string {
	if label, ok := labels[s]; ok {
		return label
	}
	return string(s)
}

// allowed 是允许的状态迁移表。
var allowed = map[State][]State{
	Unpaired:         {Configuring, Revoked},
	Configuring:      {Connecting, Unpaired, Revoked, Paused, Draining},
	Connecting:       {Ready, DegradedUpstream, DegradedRelay, Configuring, Paused, Draining, Revoked},
	Ready:            {DegradedUpstream, DegradedRelay, Connecting, Paused, Draining, Revoked},
	DegradedUpstream: {Ready, Connecting, DegradedRelay, Paused, Draining, Revoked},
	DegradedRelay:    {Ready, Connecting, DegradedUpstream, Paused, Draining, Revoked},
	Paused:           {Configuring, Connecting, Unpaired, Revoked, Draining},
	Draining:         {Configuring, Connecting, Paused, Revoked},
	Revoked:          {},
}

// CanTransition 判断两状态之间是否允许迁移。
func CanTransition(from, to State) bool {
	for _, candidate := range allowed[from] {
		if candidate == to {
			return true
		}
	}
	return false
}

// Machine 是并发安全的状态机。
type Machine struct {
	mu       sync.RWMutex
	current  State
	reason   string
	observer func(from, to State, reason string)
}

// NewMachine 创建状态机；初始状态默认是 UNPAIRED。
func NewMachine(initial State) *Machine {
	if initial == "" {
		initial = Unpaired
	}
	return &Machine{current: initial}
}

// SetObserver 注册状态变化回调，用于界面或心跳上报。
func (m *Machine) SetObserver(fn func(from, to State, reason string)) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.observer = fn
}

// Current 返回当前状态。
func (m *Machine) Current() State {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.current
}

// Reason 返回最近一次状态变化的原因。
func (m *Machine) Reason() string {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.reason
}

// Transition 执行一次状态迁移；不允许的迁移返回 false，不改变状态。
func (m *Machine) Transition(to State, reason string) bool {
	m.mu.Lock()
	from := m.current
	if from == to {
		m.reason = reason
		observer := m.observer
		m.mu.Unlock()
		if observer != nil {
			observer(from, to, reason)
		}
		return true
	}
	if !CanTransition(from, to) {
		m.mu.Unlock()
		return false
	}
	m.current = to
	m.reason = reason
	observer := m.observer
	m.mu.Unlock()
	if observer != nil {
		observer(from, to, reason)
	}
	return true
}
