package agent

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"sort"
	"strings"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/cloud"
	"github.com/sherkevin/open-claude-code/connector/internal/demomcp"
	"github.com/sherkevin/open-claude-code/connector/internal/diagnostics"
	"github.com/sherkevin/open-claude-code/connector/internal/mcp"
)

const (
	SelfTestStatusPassed  = "passed"
	SelfTestStatusFailed  = "failed"
	SelfTestStatusSkipped = "skipped"

	SelfTestQuestion = "请用内置测试服务查询订单数和 KAIWU-DEMO-001 的库存。"
	SelfTestAnswer   = "示例订单数 12；KAIWU-DEMO-001 示例库存 128。两者都不是华微真实业务数据。"
)

type SelfTestStage struct {
	ID      string `json:"id"`
	Status  string `json:"status"`
	Message string `json:"message"`
}

type SelfTestResult struct {
	OK             bool                 `json:"ok"`
	TraceID        string               `json:"trace_id,omitempty"`
	SourceID       string               `json:"source_id,omitempty"`
	Stages         []SelfTestStage      `json:"stages"`
	Probe          *cloud.SelfTestProbe `json:"probe,omitempty"`
	TestQuestion   string               `json:"test_question"`
	ExpectedAnswer string               `json:"expected_answer"`
	GeneratedAt    time.Time            `json:"generated_at"`
}

// SelfTest performs a real local MCP handshake, then asks the cloud to run the
// fixed demo tool through the public Edge and existing FRP route. Customer
// sources are not disabled, deleted or required to be healthy.
func (a *Agent) SelfTest(ctx context.Context) (result SelfTestResult) {
	started := time.Now()
	operationID := diagnostics.NewOperationID()
	result = SelfTestResult{
		Stages:         make([]SelfTestStage, 0, 8),
		TestQuestion:   SelfTestQuestion,
		ExpectedAnswer: SelfTestAnswer,
		GeneratedAt:    time.Now().UTC(),
	}
	ctx, release, operationErr := a.beginRuntimeOperation(ctx)
	if operationErr != nil {
		result.Stages = append(result.Stages, SelfTestStage{
			ID: "local_mcp", Status: SelfTestStatusFailed,
			Message: "连接器正在关闭，本次自检已取消",
		})
		appendSelfTestSkipped(&result, "enterprise_binding", "tunnel", "edge_connect")
		return result
	}
	defer release()
	a.recordDiagnostic(diagnostics.Record{EventCode: "diagnosis_started", OperationID: operationID})
	defer func() {
		code := "diagnosis_succeeded"
		if !result.OK {
			code = "diagnosis_failed"
		}
		a.recordDiagnostic(diagnostics.Record{
			EventCode:   code,
			OperationID: operationID,
			SourceID:    result.SourceID,
			TraceID:     result.TraceID,
			DurationMS:  diagnosticDuration(started),
		})
	}()

	if err := a.StartBuiltinDemo(); err != nil {
		result.Stages = append(result.Stages, SelfTestStage{
			ID: "local_mcp", Status: SelfTestStatusFailed,
			Message: "内置测试服务未能启动，请查看页面顶部状态或联系开物技术支持",
		})
		appendSelfTestSkipped(&result, "enterprise_binding", "tunnel", "edge_connect")
		return result
	}
	status := a.Status()
	if !status.BuiltinDemo.Enabled {
		result.Stages = append(result.Stages, SelfTestStage{
			ID: "local_mcp", Status: SelfTestStatusSkipped,
			Message: "内置测试服务已由用户关闭",
		})
		appendSelfTestSkipped(&result, "enterprise_binding", "tunnel", "edge_connect")
		return result
	}
	if !status.BuiltinDemo.Running {
		result.Stages = append(result.Stages, SelfTestStage{
			ID: "local_mcp", Status: SelfTestStatusFailed,
			Message: "内置测试服务当前没有运行",
		})
		appendSelfTestSkipped(&result, "enterprise_binding", "tunnel", "edge_connect")
		return result
	}
	result.SourceID = status.BuiltinDemo.SourceID
	if result.SourceID == "" {
		result.Stages = append(result.Stages, SelfTestStage{
			ID: "local_mcp", Status: SelfTestStatusFailed,
			Message: "内置测试来源尚未建立，请检查是否与其他来源编号冲突",
		})
		appendSelfTestSkipped(&result, "enterprise_binding", "tunnel", "edge_connect")
		return result
	}

	nonce, err := randomHex(16)
	if err != nil {
		result.Stages = append(result.Stages, SelfTestStage{
			ID: "local_mcp", Status: SelfTestStatusFailed,
			Message: "无法生成本次测试随机值",
		})
		appendSelfTestSkipped(&result, "enterprise_binding", "tunnel", "edge_connect")
		return result
	}
	endpoint := ""
	a.mu.RLock()
	if a.demoServer != nil {
		endpoint = a.demoServer.Endpoint()
	}
	a.mu.RUnlock()
	localProbe, err := runLocalDemoProbe(ctx, endpoint, nonce)
	if err != nil {
		result.Stages = append(result.Stages, SelfTestStage{
			ID: "local_mcp", Status: SelfTestStatusFailed,
			Message: "本机内置测试 MCP 的协议握手、工具目录或测试调用未通过",
		})
		appendSelfTestSkipped(&result, "enterprise_binding", "tunnel", "edge_connect")
		return result
	}
	result.Probe = &localProbe
	result.Stages = append(result.Stages, SelfTestStage{
		ID: "local_mcp", Status: SelfTestStatusPassed,
		Message: "本机 MCP 初始化、工具目录和 nonce 往返通过",
	})

	if !status.Bound {
		result.Stages = append(result.Stages, SelfTestStage{
			ID: "enterprise_binding", Status: SelfTestStatusSkipped,
			Message: "当前尚未绑定企业，本机示例服务已通过，公网链路未执行",
		})
		appendSelfTestSkipped(&result, "tunnel", "edge_connect")
		return result
	}
	result.Stages = append(result.Stages, SelfTestStage{
		ID: "enterprise_binding", Status: SelfTestStatusPassed,
		Message: "企业绑定信息已加载",
	})

	_, connectErr := a.connect(ctx, true)
	if connectErr != nil {
		result.Stages = append(result.Stages, SelfTestStage{
			ID: "tunnel", Status: SelfTestStatusFailed,
			Message: selfTestTunnelMessage(connectErr),
		})
		appendSelfTestSkipped(&result, "edge_connect")
		return result
	}
	result.Stages = append(result.Stages, SelfTestStage{
		ID: "tunnel", Status: SelfTestStatusPassed,
		Message: "公网隧道和独立示例来源路由已就绪",
	})

	a.mu.RLock()
	client := a.cloud
	connectorID := a.identity.ConnectorID
	a.mu.RUnlock()
	if client == nil || connectorID == "" {
		result.Stages = append(result.Stages, SelfTestStage{
			ID: "edge_connect", Status: SelfTestStatusFailed,
			Message: "企业身份或云端客户端不可用",
		})
		return result
	}
	cloudResult, cloudErr := client.SelfTest(ctx, connectorID, result.SourceID, nonce)
	result.TraceID = cloudResult.TraceID
	if len(cloudResult.Stages) == 0 {
		message := "云端自检接口暂不可用或尚未部署"
		if cloudErr != nil {
			message = selfTestCloudMessage(cloudErr)
		}
		result.Stages = append(result.Stages, SelfTestStage{
			ID: "edge_connect", Status: SelfTestStatusFailed, Message: message,
		})
		return result
	}
	for _, stage := range cloudResult.Stages {
		result.Stages = append(result.Stages, SelfTestStage{
			ID: stage.ID, Status: normalizeSelfTestStatus(stage.Status), Message: stage.Message,
		})
	}
	if cloudResult.Probe.Marker != "" || cloudResult.Probe.InstanceID != "" {
		probe := cloudResult.Probe
		result.Probe = &probe
	}
	if cloudErr != nil {
		return result
	}
	if !cloudResult.OK || cloudResult.SourceID != result.SourceID ||
		!cloudResult.Probe.Demo || cloudResult.Probe.Marker != demomcp.Marker ||
		cloudResult.Probe.Nonce != nonce {
		result.Stages = appendOrReplaceStage(result.Stages, SelfTestStage{
			ID: "probe_validation", Status: SelfTestStatusFailed,
			Message: "公网返回的示例标记、来源编号或 nonce 与本次测试不一致",
		})
		return result
	}
	result.OK = selfTestStagesPassed(result.Stages)
	return result
}

func runLocalDemoProbe(ctx context.Context, endpoint, nonce string) (cloud.SelfTestProbe, error) {
	if !strings.HasPrefix(endpoint, "http://127.0.0.1:") {
		return cloud.SelfTestProbe{}, errors.New("内置测试服务没有监听本机回环地址")
	}
	client := &mcp.Client{
		Endpoint: endpoint,
		HTTP:     &http.Client{Timeout: 8 * time.Second},
	}
	probeCtx, cancel := context.WithTimeout(ctx, 12*time.Second)
	defer cancel()
	if _, err := client.Initialize(probeCtx); err != nil {
		return cloud.SelfTestProbe{}, err
	}
	tools, err := client.ListTools(probeCtx)
	if err != nil {
		return cloud.SelfTestProbe{}, err
	}
	names := make([]string, 0, len(tools))
	for _, tool := range tools {
		names = append(names, tool.Name)
	}
	want := append([]string(nil), demomcp.Tools...)
	sort.Strings(names)
	sort.Strings(want)
	if strings.Join(names, ",") != strings.Join(want, ",") {
		return cloud.SelfTestProbe{}, errors.New("内置测试工具目录不符合固定契约")
	}
	raw, err := client.CallTool(probeCtx, demomcp.ToolPing, map[string]any{"nonce": nonce})
	if err != nil {
		return cloud.SelfTestProbe{}, err
	}
	var envelope struct {
		StructuredContent json.RawMessage `json:"structuredContent"`
		IsError           bool            `json:"isError"`
	}
	if err := json.Unmarshal(raw, &envelope); err != nil || envelope.IsError {
		return cloud.SelfTestProbe{}, errors.New("内置测试 ping 返回无效结果")
	}
	var probe cloud.SelfTestProbe
	if err := json.Unmarshal(envelope.StructuredContent, &probe); err != nil {
		return cloud.SelfTestProbe{}, err
	}
	if !probe.Demo || probe.Marker != demomcp.Marker || probe.Nonce != nonce || probe.InstanceID == "" {
		return cloud.SelfTestProbe{}, errors.New("内置测试 ping 探针校验失败")
	}
	observed, err := time.Parse(time.RFC3339Nano, probe.ServerTimeUTC)
	if err != nil || time.Since(observed).Abs() > 5*time.Minute {
		return cloud.SelfTestProbe{}, errors.New("内置测试服务时间无效")
	}
	return probe, nil
}

func appendSelfTestSkipped(result *SelfTestResult, ids ...string) {
	for _, id := range ids {
		result.Stages = appendOrReplaceStage(result.Stages, SelfTestStage{
			ID: id, Status: SelfTestStatusSkipped, Message: "前序阶段未通过，本阶段未执行",
		})
	}
}

func appendOrReplaceStage(stages []SelfTestStage, stage SelfTestStage) []SelfTestStage {
	for index := range stages {
		if stages[index].ID == stage.ID {
			stages[index] = stage
			return stages
		}
	}
	return append(stages, stage)
}

func normalizeSelfTestStatus(status string) string {
	switch status {
	case SelfTestStatusPassed, SelfTestStatusFailed, SelfTestStatusSkipped:
		return status
	default:
		return SelfTestStatusFailed
	}
}

func selfTestStagesPassed(stages []SelfTestStage) bool {
	if len(stages) == 0 {
		return false
	}
	for _, stage := range stages {
		if stage.Status != SelfTestStatusPassed {
			return false
		}
	}
	return true
}

func selfTestTunnelMessage(err error) string {
	if err == nil {
		return "公网隧道没有进入就绪状态"
	}
	message := err.Error()
	switch {
	case strings.Contains(message, "测试通道尚未就绪"):
		return demoRouteReconnectMessage
	case strings.Contains(message, "内置测试来源诊断"):
		return "内置测试来源诊断未通过"
	case strings.Contains(message, "尚未批准"):
		return "云端尚未批准内置测试来源"
	case strings.Contains(message, "绑定"):
		return "请先完成企业绑定"
	case strings.Contains(message, "隧道"):
		return "公网隧道连接未通过"
	default:
		return "公网隧道或独立示例来源准备失败"
	}
}

func selfTestCloudMessage(err error) string {
	var apiErr *cloud.APIError
	if errors.As(err, &apiErr) {
		switch apiErr.Code {
		case "SELF_TEST_SOURCE_DENIED":
			return "云端拒绝该内置测试来源，请由管理员重新确认来源登记和三个固定工具授权"
		case "DEVICE_SCOPE_DENIED", "DEVICE_REVOKED":
			return "当前设备身份无权执行该连接器自检"
		case "HTTP_404", "HTTP_405":
			return "当前云端尚未部署自检接口"
		}
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return "云端自检等待超时"
	}
	return "云端自检请求未成功"
}
