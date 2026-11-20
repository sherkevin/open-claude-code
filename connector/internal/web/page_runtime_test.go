package web

import (
	"os"
	"os/exec"
	"strings"
	"testing"
)

func pageScriptFragment(t *testing.T, startMarker, endMarker string) string {
	t.Helper()
	source, err := os.ReadFile("page.go")
	if err != nil {
		t.Fatal(err)
	}
	text := string(source)
	start := strings.Index(text, startMarker)
	if start < 0 {
		t.Fatalf("JavaScript start marker not found: %s", startMarker)
	}
	endOffset := strings.Index(text[start:], endMarker)
	if endOffset < 0 {
		t.Fatalf("JavaScript end marker not found: %s", endMarker)
	}
	return text[start : start+endOffset]
}

func runNodeScript(t *testing.T, script string) string {
	t.Helper()
	node, err := exec.LookPath("node")
	if err != nil {
		t.Skip("node is not installed; JavaScript runtime verification skipped")
	}
	cmd := exec.Command(node, "-e", script)
	cmd.Env = append(os.Environ(), "TZ=Asia/Shanghai")
	output, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("JavaScript runtime check failed: %v\n%s", err, output)
	}
	return string(output)
}

// TestPageFormatTimeRunsInJavaScript catches Intl option combinations that are
// syntactically valid but throw at runtime when the first log row is rendered.
func TestPageFormatTimeRunsInJavaScript(t *testing.T) {
	function := pageScriptFragment(t, "function formatTime(value){", "\nfunction formatDuration")
	script := function + `
const value = formatTime("2026-11-19T03:04:05Z");
if (!value || value === "—") { throw new Error("formatTime returned no value"); }
if (!value.includes("11:04:05")) { throw new Error("local time was not rendered: " + value); }
process.stdout.write(value);
`
	output := runNodeScript(t, script)
	if !strings.Contains(output, "11:04:05") {
		t.Fatalf("formatTime did not show the customer's local time: %s", output)
	}
}

func TestPageFirstRunUsesOnlyAvailableEvidence(t *testing.T) {
	functions := pageScriptFragment(t, `const TOKEN =`, "\nrefresh(true).then(")
	script := `
const assert = require("node:assert/strict");
const elements = new Map();
function element(){
  const classes = new Set();
  return {
    textContent:"", className:"", value:"", style:{}, children:[],
    classList:{
      contains(name){ return classes.has(name); },
      add(name){ classes.add(name); },
      remove(name){ classes.delete(name); },
      toggle(name, force){ force ? classes.add(name) : classes.delete(name); }
    },
    addEventListener(){},
    replaceChildren(){ this.children = []; },
    appendChild(child){ this.children.push(child); }
  };
}
const document = {
  getElementById(id){
    if(!elements.has(id)){ elements.set(id, element()); }
    return elements.get(id);
  },
  createElement:element
};
const hidden = id => $(id).classList.contains("hidden");
const primary = id => $(id).classList.contains("primary");
const copied = [];
const navigator = {clipboard:{writeText:async value=>{ copied.push(value); }}};
const calls = [];
const unbound = {
  state:"UNPAIRED", bound:false, cloud_configured:true,
  builtin_demo:{enabled:true,running:true,source_id:"local-demo"}
};
const bound = {...unbound, state:"CONFIGURING", bound:true, connector_id:"connector",
  builtin_demo:{...unbound.builtin_demo, source_id:"bound-demo"}};
let status = unbound;
let testResult;
const fetch = async (path, options) => {
  calls.push(path);
  let data;
  if(path === "/api/status"){ data = status; }
  else if(path === "/api/self-test"){
    if(!testResult){ throw new Error("请求超时"); }
    data = testResult;
  }else if(path === "/api/bind"){
    if(JSON.parse(options.body).code === "expired"){
      return {json:async ()=>({ok:false,error:{message:"过期"}})};
    }
    assert.equal(JSON.parse(options.body).code, "valid");
    status = bound;
    data = status;
  }else{ assert.fail("Unexpected API call: " + path); }
  return {json:async ()=>({ok:true,data})};
};
` + functions + `
function stage(id,status,message = id){ return {id,status,message}; }
const localPassed = stage("local_mcp","passed");
const bindingPassed = stage("enterprise_binding","passed");
const tunnelPassed = stage("tunnel","passed");
const edgePassed = stage("edge_connect","passed");
const trace = "kwtr_11111111111111111111111111111111";
const submitBinding = ()=>$("bindForm").onsubmit({preventDefault(){}});
async function check(){
  await refresh(false);
  assert.equal($("nextStepTitle").textContent, "先检查本机示例");
  assert.ok(primary("selfTestBtn"));
  assert.ok(!hidden("bindForm"));
  assert.ok(hidden("selfTestAnswer"));
  assert.equal($("selfLocalState").textContent, "尚未检测");
  $("bindCode").value = "   ";
  await submitBinding();
  assert.ok(!calls.includes("/api/bind"));

  testResult = {ok:false, stages:[stage("local_mcp","failed"),stage("enterprise_binding","skipped")]};
  await $("selfTestBtn").onclick();
  assert.match($("selfTestSummary").textContent, /本机示例检查未通过/);
  assert.ok(hidden("selfTestTraceWrap"));

  testResult = {ok:false, stages:[
    localPassed, stage("enterprise_binding","skipped"), stage("tunnel","skipped"), stage("edge_connect","skipped")
  ]};
  await $("selfTestBtn").onclick();
  await refresh(false);
  assert.match($("selfTestSummary").textContent, /本机示例已通过，公网待绑定企业/);
  assert.equal($("selfLocalState").textContent, "通过");
  assert.equal($("selfBindingState").textContent, "待绑定");
  assert.equal($("selfEdgeState").textContent, "待绑定");
  assert.ok(primary("bindBtn") && !primary("selfTestBtn"));
  assert.ok(hidden("selfTestAnswer") && hidden("selfTestTraceWrap"));

  status = {...unbound, cloud_configured:false};
  await refresh(false);
  assert.ok(hidden("bindForm"));
  assert.match($("nextStepDetail").textContent, /获取接入安装包/);
  status = unbound;
  await refresh(false);

  $("bindCode").value = "expired";
  await submitBinding();
  assert.match($("firstRunNotice").textContent, /企业绑定失败.*过期/);
  assert.ok(!hidden("bindForm") && !$("bindBtn").disabled);
  $("bindCode").value = " valid ";
  await submitBinding();
  assert.equal($("bindCode").value, "");
  assert.ok(hidden("bindForm"));
  assert.match($("firstRunNotice").textContent, /企业绑定成功/);
  assert.equal($("selfBindingState").textContent, "已绑定");
  assert.equal($("selfEdgeState").textContent, "待验证");
  assert.equal($("nextStepBtn").textContent, "");
  assert.equal($("selfTestBtn").textContent, "验证公网连接");

  testResult = {ok:false, stages:[localPassed, bindingPassed, stage("tunnel","failed","隧道异常")]};
  await $("selfTestBtn").onclick();
  assert.match($("selfTestSummary").textContent, /公网隧道未通过/);
  assert.ok(hidden("selfTestTraceWrap"));
  testResult.stages[2].message = "测试通道尚未就绪，请在方便时重新连接";
  await $("selfTestBtn").onclick();
  assert.match($("nextStepDetail").textContent, /重连会短暂中断/);
  assert.equal($("nextStepBtn").textContent, "查看运行日志");
  assert.ok(!calls.includes("/api/connect"));

  status = {...bound, state:"READY", frpc_running:true, tunnel_connected:true};
  testResult = {ok:false, trace_id:trace, stages:[
    localPassed, bindingPassed, tunnelPassed, edgePassed, stage("tools_call","failed"), stage("cleanup","passed")
  ]};
  await $("selfTestBtn").onclick();
  assert.equal($("selfEdgeState").textContent, "未通过");
  assert.match($("selfTestSummary").textContent, /公网验证未通过/);
  assert.equal($("selfTestCloudStages").children.length, 3);
  assert.ok(!hidden("selfTestTraceWrap") && hidden("selfTestAnswer") && primary("copySelfTestTraceBtn"));
  await $("copySelfTestTraceBtn").onclick();
  assert.deepEqual(copied, [trace]);
  assert.match($("selfTestTraceNotice").textContent, /已复制/);

  testResult = {...testResult, trace_id:trace + " token=secret"};
  await $("selfTestBtn").onclick();
  assert.ok(hidden("selfTestTraceWrap"));
  await $("copySelfTestTraceBtn").onclick();
  assert.deepEqual(copied, [trace]);
  assert.equal($("selfTestTraceNotice").textContent, "");

  testResult = {ok:true, stages:[localPassed, bindingPassed, tunnelPassed]};
  await $("selfTestBtn").onclick();
  assert.ok(hidden("selfTestAnswer"));

  testResult = {ok:true, stages:[localPassed, bindingPassed, tunnelPassed, edgePassed, stage("tools_call","passed")]};
  await $("selfTestBtn").onclick();
  assert.match($("selfTestSummary").textContent, /公网验证已通过，助手授权待确认/);
  assert.ok(!hidden("selfTestAnswer"));
  assert.equal($("nextStepBtn").textContent, "填写业务配置");
  status = {...status, sources:[{source_id:"business"}]};
  await refresh(false);
  assert.equal($("nextStepBtn").textContent, "查看业务配置");

  status = {...bound, state:"PAUSED", frpc_running:false};
  await refresh(false);
  assert.equal($("selfEdgeState").textContent, "待验证");
  assert.ok(hidden("selfTestAnswer"));
  status = {...unbound, state:"REVOKED"};
  await refresh(false);
  assert.equal($("selfBindingState").textContent, "已撤销");
  assert.equal($("nextStepTitle").textContent, "重新绑定企业");
  assert.ok(!hidden("bindForm"));

  testResult = null;
  await $("selfTestBtn").onclick();
  assert.match($("selfTestSummary").textContent, /无法完成连接测试.*请求超时/);
  assert.ok(hidden("selfTestTraceWrap"));
  status = {...unbound, builtin_demo:{...unbound.builtin_demo, error_code:"BUILTIN_SOURCE_CONFLICT"}};
  await refresh(false);
  assert.equal($("builtinDemoPill").textContent, "来源冲突");
  assert.ok($("selfTestBtn").disabled);
}
check().catch(error=>{ console.error(error); process.exitCode = 1; });
`
	runNodeScript(t, script)
}
