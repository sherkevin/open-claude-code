package web

import (
	"strings"
	"testing"
)

func TestOnboardingCopiesImportableJSONAfterExplicitToolSelection(t *testing.T) {
	_, after, found := strings.Cut(onboardingPage, "<script>")
	pageScript, _, closed := strings.Cut(after, "</script>")
	if !found || !closed || pageScript == "" {
		t.Fatal("onboarding page has no script")
	}
	script := `
const assert = require("node:assert/strict");
const elements = new Map();
function element(tagName = "div"){
  const classes = new Set();
  return {
    tagName, textContent:"", value:"", children:[], dataset:{}, style:{},
    hidden:false, disabled:false, checked:false,
    classList:{
      contains(name){ return classes.has(name); },
      add(name){ classes.add(name); },
      remove(name){ classes.delete(name); },
      toggle(name, force){
        const enabled = force === undefined ? !classes.has(name) : force;
        if(enabled){ classes.add(name); }else{ classes.delete(name); }
        return enabled;
      }
    },
    addEventListener(name, callback){ this["on" + name] = callback; },
    focus(){ document.activeElement = this; }, select(){},
    setAttribute(name, value){ this[name] = value; },
    hasAttribute(name){ return Object.hasOwn(this, name); },
    removeAttribute(name){ delete this[name]; },
    appendChild(child){ this.children.push(child); return child; },
    append(...items){ this.children.push(...items); },
    replaceChildren(...items){ this.children = items; },
    querySelector(selector){ return this.querySelectorAll(selector)[0] || null; },
    querySelectorAll(selector){
      const descendants = this.children.flatMap(child => typeof child === "object"
        ? [child, ...child.querySelectorAll("*")] : []);
      if(selector === "*"){ return descendants; }
      return descendants.filter(child => child.tagName === "input" && child.type === "checkbox" &&
        (!selector.includes(":checked") || child.checked));
    }
  };
}
const document = {
  visibilityState:"visible",
  getElementById(id){
    if(!elements.has(id)){ elements.set(id, element()); }
    return elements.get(id);
  },
  createElement:element,
  createTextNode:value=>value,
  querySelectorAll(selector){
    assert.match(selector, /#toolsList\s+/);
    return this.getElementById("toolsList").querySelectorAll(selector);
  },
  addEventListener(){}
};
const location = new URL("http://127.0.0.1:17872/");
const copiedJSON = [];
const navigator = {clipboard:{writeText:async value=>{ copiedJSON.push(value); }}};
const window = {location, navigator, addEventListener(){}};
const setInterval = ()=>0;
const setTimeout = ()=>0;
const clearTimeout = ()=>0;
const control = {bound:false, cloud_configured:true, edge_configured:true,
  state:"UNPAIRED", transport_mode:"native", sources:[], auth_profiles:[]};
const calls = [];
const mcpConfig = {mcpServers:{"只读示例":{
  type:"http", url:"https://edge.example/sources/public/mcp", headers:{Authorization:"Bearer test-integration"}
}}};
let exportAttempts = 0;
const assistTarget = {name:"销售合同",url:"https://customer.example/mcp/sales"};
let assistance = null;
let allowedAssist = false;
let failAssist = false;
const fetch = async (path, options) => {
  const body = options && options.body ? JSON.parse(options.body) : undefined;
  calls.push({path, body});
  let data;
  if(path === "/api/status"){
    data = control;
  }else if(path === "/api/assist/offer"){
    assert.ok(options.headers["X-Connector-Token"]);
    if(failAssist){ throw new Error("control unavailable"); }
    data = {enabled:control.bound && allowedAssist,offer:assistance,target_after_start:control.bound && allowedAssist,
      last_command:assistance?.last_command};
  }else if(path === "/api/assist/start"){
    assert.deepEqual(body, {});
    assistance = {id:"as_fixture",name:"内网接入协助",url:"",status:"active",local_active:true};
    data = assistance;
  }else if(path === "/api/assist/target"){
    assert.deepEqual(body, {...assistTarget,session_id:"as_fixture"});
    assistance = {...assistance,...assistTarget};
    data = assistance;
  }else if(path === "/api/assist/stop"){
    assert.deepEqual(body, {session_id:"as_fixture"});
    assistance = null;
  }else if(path === "/api/bind"){
    assert.deepEqual(body, {code:"kwp_fixture"});
    Object.assign(control, {bound:true,state:"CONFIGURING",tenant_name:"试测企业"});
    data = control;
  }else if(path === "/api/source/tools"){
    if (!body.bearer_token) {
      return {ok:false,json:async()=>({ok:false,error:{code:"UPSTREAM_UNAUTHORIZED",message:"HTTP 401"}})};
    }
    data = [{name:"read_orders",title:"订单查询"},{name:"update_order",title:"修改订单"}];
  }else if(path === "/api/source/save-and-connect"){
    const source = {source_id:"source-test",display_name:"只读示例",upstream_url:body.upstream_url,tool_allowlist:body.tools};
    Object.assign(control, {state:"READY",tunnel_connected:true,sources:[source]});
    data = {source, status:control};
  }else if(path === "/api/mcp-export"){
    assert.deepEqual(body, {source_id:"source-test"});
    exportAttempts++;
    if(exportAttempts === 1){
      return {ok:false,status:403,json:async()=>({ok:false,error:{code:"DEVICE_EXPORT_DENIED",message:"业务服务已保存，开物平台尚未开通配置权限。"}})};
    }
    data = {mcp_config:mcpConfig,platform_url:"https://flintw.com/project/kaiwu/"};
  }else{
    assert.fail("Unexpected onboarding API call: " + path);
  }
  if(!["/api/status", "/api/assist/offer"].includes(path)){
    assert.equal(options.method, "POST");
    assert.ok(options.headers["X-Connector-Token"]);
  }
  return {ok:true,status:200,json:async()=>({ok:true,data})};
};
` + pageScript + `
async function verifyWorkflow(){
  await new Promise(resolve=>setImmediate(resolve));
  const ui = id=>document.getElementById(id);
  const submit = () => ({preventDefault(){}});
  assert.equal(ui("assistPanel").hidden, true);
  ui("bindCode").value = "  kwp_fixture  ";
  await ui("bindForm").onsubmit(submit());
  assert.equal(calls.filter(call=>call.path === "/api/bind").length, 1);
  assert.equal(ui("bindCode").value, "");
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(ui("assistPanel").hidden, false);
  assert.equal(ui("assistStart").hidden, true);
  assert.match(ui("assistState").textContent, /尚未开通协助权限/);
  assert.equal(calls.filter(call=>call.path === "/api/assist/start").length, 0);
  allowedAssist = true;
  await refreshAssist();
  assert.equal(ui("assistStart").hidden, false);
  failAssist = true;
  await refreshAssist();
  assert.equal(ui("assistStart").hidden, true);
  assert.match(ui("assistState").textContent, /暂时无法读取/);
  failAssist = false;
  await refreshAssist();
  assert.equal(ui("assistStart").hidden, false);
  assert.equal(ui("assistTarget").hidden, true);
  assert.equal(ui("assistStop").hidden, true);
  assert.equal(ui("assistAddress").textContent, "");
  await ui("assistStart").onclick();
  assert.equal(calls.filter(call=>call.path === "/api/assist/start").length, 1);
  assert.equal(ui("assistStart").hidden, true);
  assert.equal(ui("assistTarget").hidden, false);
  assert.equal(ui("assistStop").hidden, false);
  assert.equal(ui("assistAddress").textContent, "");
  for(let i = 0; i < 20 && assistRefreshing; i++){ await new Promise(resolve=>setImmediate(resolve)); }
  assistance.last_command = {id:"asjob_fixture",command:"ipconfig /all",state:"succeeded",output:"Windows IP Configuration"};
  await refreshAssist();
  assert.equal(ui("assistCommandPanel").hidden, false);
  assert.equal(ui("assistCommand").textContent, "ipconfig /all");
  assert.match(ui("assistCommandState").textContent, /执行完成/);
  assert.equal(ui("assistOutputDetails").hidden, false);
  assert.equal(ui("assistOutput").textContent, "Windows IP Configuration");
  await ui("assistTarget").onclick();
  assert.equal(calls.filter(call=>call.path === "/api/assist/target").length, 0);
  assert.equal(document.activeElement, ui("upstreamAddress"));
  ui("upstreamAddress").value = assistTarget.url;
  ui("sourceName").value = assistTarget.name;
  ui("bearerToken").value = "kept-on-device";
  ui("upstreamAddress").oninput();
  assert.equal(ui("assistAddress").textContent, assistTarget.url);
  await ui("assistTarget").onclick();
  assert.equal(ui("assistStart").hidden, true);
  assert.equal(ui("assistTarget").hidden, true);
  assert.equal(ui("assistStop").hidden, false);
  assert.equal(ui("assistAddress").textContent, assistTarget.url);
  await ui("assistStop").onclick();
  assert.equal(ui("assistStart").hidden, false);
  assert.equal(ui("assistStop").hidden, true);
  assert.equal(ui("assistCommandPanel").hidden, true);
  assert.equal(ui("assistNotice").textContent, "协助已停止。");
  await ui("assistStart").onclick();
  assert.equal(ui("assistAddress").textContent, assistTarget.url);
  assert.equal(ui("assistTarget").hidden, false, "a prefilled address still needs explicit confirmation");
  await ui("assistStop").onclick();
  assert.equal(calls.filter(call=>call.path === "/api/assist/target").length, 1);
  ui("sourceName").value = "";
  ui("bearerToken").value = "";

  ui("upstreamAddress").value = "65536";
  await readTools(submit());
  assert.equal(calls.filter(call=>call.path === "/api/source/tools").length, 0);
  ui("upstreamAddress").value = "http://127.0.0.1:3800/custom/mcp";
  await readTools(submit());
  assert.equal(ui("sourceOptions").open, true);
  assert.equal(document.activeElement, ui("bearerToken"));
  assert.match(ui("notice").textContent, /401/);
  assert.equal(ui("toolForm").hidden, true);
  ui("bearerToken").value = "synthetic-access-token";
  await readTools(submit());
  assert.equal(calls.filter(call=>call.path === "/api/source/tools").at(-1).body.upstream_url,
    "http://127.0.0.1:3800/custom/mcp");
  ui("upstreamAddress").value = "3000";
  await readTools(submit());
  assert.equal(calls.filter(call=>call.path === "/api/source/tools").at(-1).body.upstream_url,
    "http://127.0.0.1:3000/mcp");
  const tools = ui("toolsList").querySelectorAll("input[type=checkbox]");
  assert.deepEqual(tools.map(tool=>tool.value), ["read_orders", "update_order"]);
  assert.ok(tools.every(tool=>!tool.checked), "new tools require explicit consent");
  await generateJSON(submit());
  assert.equal(calls.filter(call=>call.path === "/api/source/save-and-connect").length, 0);

  tools[0].checked = true;
  const change = {target:tools[0]};
  tools[0].onchange?.(change);
  ui("toolsList").onchange?.(change);
  await generateJSON(submit());
  const saved = calls.filter(call=>call.path === "/api/source/save-and-connect");
  assert.equal(saved.length, 1);
  assert.equal(saved[0].body.upstream_url, "http://127.0.0.1:3000/mcp");
  assert.deepEqual(saved[0].body.tools, ["read_orders"]);
  assert.notEqual(saved[0].body.dynamic_auth, true);
  assert.equal(ui("mcpJSON").value, "", "failed export must not expose a partial JSON");
  assert.equal(exportAttempts, 1);
  assert.equal(ui("exportHelp").hidden, false);
  assert.equal(ui("exportHelpBtn").hidden, false);
  assert.match(ui("notice").textContent, /开物平台尚未开通配置权限/);
  const startsBeforeHelp = calls.filter(call=>call.path === "/api/assist/start").length;
  ui("exportHelpBtn").onclick();
  assert.equal(document.activeElement, ui("assistStart"));
  assert.equal(calls.filter(call=>call.path === "/api/assist/start").length, startsBeforeHelp,
    "help navigation must not start remote assistance without an explicit user action");
  await ui("retryExportBtn").onclick();
  assert.equal(calls.filter(call=>call.path === "/api/source/save-and-connect").length, 1,
    "export retry must not create or reconnect another source");
  assert.equal(exportAttempts, 2);
  assert.equal(ui("exportHelp").hidden, true);
  assert.deepEqual(JSON.parse(ui("mcpJSON").value), mcpConfig);
  await copyJSON();
  assert.deepEqual(copiedJSON.map(value=>JSON.parse(value)), [mcpConfig]);
}
verifyWorkflow().catch(error=>{ console.error(error); process.exitCode = 1; });
`
	runNodeScript(t, script)
}
