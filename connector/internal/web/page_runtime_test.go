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

func TestPageSelfTestRendererRunsInJavaScript(t *testing.T) {
	functions := pageScriptFragment(t, "function setStage(prefix, state, detail, kind){", "\nfunction stateClass")
	script := functions + `
const elements = new Map();
function element(id){
  return {
    id, textContent:"", className:"", children:[], checked:false, disabled:false,
    classList:{add(){},remove(){},toggle(){}},
    replaceChildren(){ this.children = []; },
    appendChild(child){ this.children.push(child); }
  };
}
function $(id){
  if(!elements.has(id)){ elements.set(id, element(id)); }
  return elements.get(id);
}
const document = {createElement(tag){ return element(tag); }};
const busy = false;
renderSelfTest({
  ok:false,
  trace_id:"kwtr_11111111111111111111111111111111",
  stages:[
    {id:"local_mcp",status:"passed",message:"local ok"},
    {id:"enterprise_binding",status:"skipped",message:"not bound"},
    {id:"tunnel",status:"skipped",message:"not run"},
    {id:"edge_connect",status:"failed",message:"edge failed"},
    {id:"mcp_initialize",status:"skipped",message:"not run"},
    {id:"cleanup",status:"passed",message:"credential removed"}
  ]
});
if($("selfLocalState").textContent !== "通过"){ throw new Error("local stage not rendered"); }
if($("selfBindingState").textContent !== "未执行"){ throw new Error("binding stage not rendered"); }
if($("selfEdgeState").textContent !== "失败"){ throw new Error("edge stage not rendered"); }
if($("selfTestCloudStages").children.length !== 3){ throw new Error("all cloud stages, including cleanup, must be shown"); }
if(!$("selfTestTrace").textContent.includes("kwtr_")){ throw new Error("trace id not rendered"); }
renderBuiltinDemo({builtin_demo:{enabled:true,running:true,error_code:"BUILTIN_SOURCE_CONFLICT",tools:["kaiwu_demo_ping"]}});
if($("builtinDemoPill").textContent !== "来源冲突"){ throw new Error("source conflict was hidden by running state"); }
if(!$("selfTestBtn").disabled){ throw new Error("self-test must stay disabled while source identity conflicts"); }
process.stdout.write("self-test-render-ok");
`
	output := runNodeScript(t, script)
	if !strings.Contains(output, "self-test-render-ok") {
		t.Fatalf("self-test renderer did not complete: %s", output)
	}
}
