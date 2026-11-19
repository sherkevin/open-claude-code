package web

// page 是本机配置界面的单页模板。__CONNECTOR_TOKEN__ 处写入本机会话令牌。
const page = `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>开物 · 内网连接器</title>
<style>
:root{--ink:#17223b;--muted:#5b6785;--line:#dfe4ef;--bg:#f5f7fb;--card:#fff;--brand:#1f4fd8;--brand-soft:#eef2ff;--ok:#0f7a4f;--warn:#a86a00;--err:#b3261e}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--ink);line-height:1.55}
header{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:16px 24px;background:var(--card);border-bottom:1px solid var(--line)}
.brand{font-weight:650;font-size:18px}.brand small{margin-left:8px;color:var(--muted);font-weight:400;font-size:12px}
main{max-width:920px;margin:0 auto;padding:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:14px}
.intro{border-left:4px solid var(--brand);background:linear-gradient(135deg,#fff 0%,#f8faff 100%)}
.intro h1{font-size:21px;margin:0 0 6px}.intro p{margin:0;color:var(--muted);font-size:13.5px}
.stage-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:14px}
.stage{border:1px solid var(--line);border-radius:10px;padding:12px;background:#fbfcff;min-width:0}
.stage b{display:block;font-size:13.5px}.stage small{display:block;margin-top:4px;font-size:12.5px;font-weight:600;color:var(--muted)}
.stage p{margin:5px 0 0;color:var(--muted);font-size:12px;line-height:1.55;word-break:break-word}
.stage.ready{border-color:#b8e1ca;background:#f4fbf7}.stage.ready small{color:var(--ok)}
.stage.warn{border-color:#ead39f;background:#fffaf0}.stage.warn small{color:var(--warn)}
.stage.error{border-color:#efc3bf;background:#fff7f6}.stage.error small{color:var(--err)}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
.row.end{justify-content:flex-end}.grow{flex:1 1 220px}
.pill{display:inline-block;padding:4px 10px;border-radius:999px;font-size:12px;background:var(--brand-soft);color:var(--brand);white-space:nowrap}
.pill.ok{background:#e6f4ec;color:var(--ok)}.pill.err{background:#fdeceb;color:var(--err)}.pill.warn{background:#fdf3e2;color:var(--warn)}
h2{font-size:16px;margin:0 0 5px}.section-hint{margin:0 0 14px;color:var(--muted);font-size:13px}
.field{display:block;min-width:0}.field span{display:block;margin-bottom:5px;font-size:13px;font-weight:600}.field em{font-style:normal;color:var(--muted);font-weight:400}
.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.span-2{grid-column:1/-1}
input,textarea,select{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;font:inherit;background:#fff;color:var(--ink)}
input:focus,textarea:focus,select:focus{outline:2px solid #c8d5ff;border-color:var(--brand)}
textarea{min-height:120px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
button{padding:9px 16px;border-radius:8px;border:1px solid var(--line);background:#fff;color:var(--ink);cursor:pointer;font:inherit}
button:hover:not(:disabled){border-color:#b6c3e7}button.primary{background:var(--brand);border-color:var(--brand);color:#fff}button.primary:hover:not(:disabled){background:#173fb3}
button.danger{color:var(--err);border-color:#efc3bf}button:disabled{opacity:.5;cursor:not-allowed}
.action-bar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:16px;padding-top:16px;border-top:1px solid var(--line)}
.action-bar .primary{min-width:160px;font-weight:650}.message{flex:1 1 260px;font-size:13px;color:var(--muted)}
.message.ok{color:var(--ok)}.message.warn{color:var(--warn)}.message.err{color:var(--err)}
.auth-box{margin-top:14px;border:1px dashed #cbd5ea;border-radius:10px;padding:14px;background:#fbfcff}
.auth-head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.check{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600}.check input{width:auto;margin:0}
.subtle{color:var(--muted);font-size:12.5px}.subtle code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.tools-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:8px}
.tools-head p{margin:3px 0 0;color:var(--muted);font-size:12.5px}.tool-list{display:grid;gap:8px}
.tool-empty{border:1px dashed var(--line);border-radius:8px;padding:12px;color:var(--muted);font-size:13px}
.tool-option{display:grid;grid-template-columns:auto minmax(0,1fr);gap:9px;align-items:start;padding:10px;border:1px solid var(--line);border-radius:8px;background:#fff}
.tool-option input{width:auto;margin-top:3px}.tool-option b{display:block;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;word-break:break-all}.tool-option p{margin:3px 0 0;color:var(--muted);font-size:12px}
pre{background:#0f172a;color:#e2e8f0;padding:14px;border-radius:10px;overflow:auto;font-size:12.5px;line-height:1.6;max-height:420px;white-space:pre-wrap;word-break:break-word}
details.card>summary{cursor:pointer;font-weight:650;font-size:15px;list-style:none}details.card>summary::-webkit-details-marker{display:none}details.card>summary:after{content:"展开";float:right;color:var(--muted);font-size:12px;font-weight:400}details[open].card>summary:after{content:"收起"}
.advanced-body{padding-top:18px;margin-top:16px;border-top:1px solid var(--line)}
.advanced-section{padding:15px 0;border-bottom:1px solid var(--line)}.advanced-section:first-child{padding-top:0}.advanced-section:last-child{border-bottom:0;padding-bottom:0}
.advanced-section h3{font-size:14px;margin:0 0 5px}.advanced-section p{margin:5px 0 10px;color:var(--muted);font-size:12.5px}
.warning{border-left:4px solid var(--warn);background:#fffaf0;padding:11px 12px;border-radius:8px;color:#71501a;font-size:12.5px;line-height:1.65;margin:8px 0 12px}
.danger-note{border-left-color:var(--err);background:#fff7f6;color:#7d2822}
ul.issues{margin:8px 0 0;padding-left:18px;font-size:13px;color:var(--muted)}ul.issues li.err{color:var(--err)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}.kv{font-size:12.5px;color:var(--muted)}.kv b{display:block;color:var(--ink);font-size:13.5px;margin-top:3px;word-break:break-all}
.hidden{display:none!important}
@media(max-width:720px){header{padding:14px 16px}main{padding:14px}.stage-grid,.form-grid{grid-template-columns:1fr}.span-2{grid-column:auto}.card{padding:15px}.tools-head{align-items:stretch;flex-direction:column}.action-bar{align-items:stretch}.action-bar .primary{width:100%}}
</style>
</head>
<body>
<header><div class="brand">开物<small>内网连接器</small></div><span class="pill" id="statePill">读取中…</span></header>
<main>
<section class="card intro">
  <h1>安装后填写本机 MCP 参数即可接入</h1>
  <p>不需要手工编写 JSON。填写服务地址和认证信息，读取并勾选允许使用的工具，再点击“保存并连接”。开物平台的企业集成凭据和来源路由由开物管理员完成。</p>
  <div class="stage-grid">
    <div class="stage" id="localStage"><b>内网 MCP 可访问</b><small id="localState">尚未检测</small><p id="localDetail">保存配置后会检查地址、协议和工具目录。</p></div>
    <div class="stage" id="tunnelStage"><b>公网隧道已连接</b><small id="tunnelState">尚未连接</small><p id="tunnelDetail">需要完成企业绑定后建立。</p></div>
    <div class="stage" id="platformStage"><b>开物平台接入</b><small id="platformState">等待管理员配置</small><p id="platformDetail">企业集成凭据 K 和来源路由由开物管理员配置。</p></div>
  </div>
</section>

<section class="card">
  <h2>配置内部 MCP 服务</h2>
  <p class="section-hint">首次安装请选择“新增 MCP 服务”。修改已有服务时，访问令牌和 HMAC 密钥留空表示保留本机已保存的值。</p>
  <div class="form-grid">
    <label class="field span-2"><span>当前配置对象</span><select id="sourceSelect"><option value="">＋ 新增 MCP 服务</option></select></label>
    <label class="field"><span>服务名称</span><input id="displayName" maxlength="80" placeholder="例如：库存查询" autocomplete="off"></label>
    <label class="field"><span>MCP HTTP 地址</span><input id="upstreamURL" placeholder="http://127.0.0.1:28765/mcp" autocomplete="off"></label>
    <label class="field span-2"><span>访问令牌 <em>可选；已有令牌留空保持原值</em></span><input id="bearerToken" type="password" placeholder="填写客户 MCP 要求的 Bearer Token；页面不会再次显示已保存的令牌" autocomplete="new-password"></label>
  </div>

  <div class="auth-box">
    <div class="auth-head">
      <label class="check"><input id="dynamicAuth" type="checkbox" checked>使用华微动态签名</label>
      <span class="subtle">动态签名只在调用工具时附加；密钥只保存在本机。</span>
    </div>
    <div class="form-grid" id="authFields" style="margin-top:12px">
      <label class="field"><span>appid</span><input id="authAppID" value="MCP" autocomplete="off"></label>
      <label class="field"><span>channel</span><input id="authChannel" placeholder="填写客户提供的渠道号" autocomplete="off"></label>
      <label class="field"><span>userid</span><input id="authUserID" placeholder="填写客户提供的读取身份" autocomplete="off"></label>
      <label class="field"><span>HMAC 密钥 <em>留空保持原值</em></span><input id="authHMACKey" type="password" placeholder="填写客户提供的 HMAC 密钥" autocomplete="new-password"></label>
    </div>
  </div>

  <div style="margin-top:16px">
    <div class="tools-head">
      <div><b style="font-size:13px">允许调用的工具</b><p>读取 MCP 初始化后的真实工具目录后勾选。不会默认允许未知工具，也不接受通配符。</p></div>
      <button id="readToolsBtn" type="button">读取工具列表</button>
    </div>
    <div class="tool-list" id="toolList"><div class="tool-empty">尚未读取工具。请先填写 MCP 地址和认证信息，再点击“读取工具列表”。</div></div>
  </div>

  <div class="action-bar">
    <button class="primary" id="saveConnectBtn" type="button">保存并连接</button>
    <div class="message" id="formMessage">保存成功后会立即检查内网 MCP；企业已绑定时继续启动公网隧道。</div>
  </div>
</section>

<details class="card" id="advancedPanel">
  <summary>高级选项与诊断详情</summary>
  <div class="advanced-body">
    <div class="advanced-section">
      <h3>原始 MCP JSON 导入（高级）</h3>
      <p>仅用于已有标准 MCP JSON 的维护场景。此操作会整体替换当前来源列表，不应与上面的普通表单同时使用。</p>
      <textarea id="mcpConfig" placeholder='{"mcpServers":{"库存查询":{"type":"http","url":"http://127.0.0.1:28765/mcp","headers":{"Authorization":"Bearer ..."}}}}'></textarea>
      <div class="row" style="margin-top:10px"><button id="importBtn" type="button">导入并替换全部来源</button></div>
      <ul class="issues" id="importIssues"></ul>
    </div>

    <div class="advanced-section">
      <h3>企业绑定</h3>
      <p id="bindHint">一次性接入码由开物管理员签发，只能使用一次。</p>
      <div class="row"><input class="grow" id="bindCode" placeholder="一次性接入码" autocomplete="off"><button id="bindBtn" type="button" disabled>绑定企业</button></div>
    </div>

    <div class="advanced-section">
      <h3>本地与隧道维护</h3>
      <p>诊断原始结果只用于排障。暂停或断开不会删除本机来源配置。</p>
      <div class="row"><button id="diagnoseBtn" type="button">运行详细诊断</button><button id="pauseBtn" type="button">暂停</button><button id="disconnectBtn" type="button">断开并回到待配置</button></div>
      <pre id="diagnoseOut" class="hidden" style="margin-top:12px"></pre>
    </div>

    <div class="advanced-section">
      <h3>Edge 地址预览</h3>
      <div class="warning">这里只预览本机生成的 Edge 路由地址，<strong>不包含 Authorization，也不是可以直接导入开物平台的完整 MCP JSON</strong>。企业集成凭据 K 必须由开物管理员签发并绑定来源。</div>
      <button id="exportBtn" type="button">查看 Edge 地址预览</button>
      <pre id="exportOut" class="hidden" style="margin-top:12px"></pre>
    </div>

    <div class="advanced-section">
      <h3>连接器技术状态</h3>
      <div class="grid">
        <div class="kv">企业<b id="tenant">—</b></div>
        <div class="kv">连接器<b id="connector">—</b></div>
        <div class="kv">公网中继进程<b id="frpc">—</b></div>
        <div class="kv">版本<b id="version">—</b></div>
        <div class="kv">控制面<b id="cloudEndpoint">—</b></div>
        <div class="kv">MCP Edge<b id="edgeEndpoint">—</b></div>
      </div>
      <p class="subtle" id="statusReason" style="margin-top:10px"></p>
    </div>

    <div class="advanced-section">
      <h3>撤销本机连接器</h3>
      <div class="warning danger-note">撤销会清除本机企业身份、访问令牌和动态签名配置，必须由开物管理员重新签发后才能再次接入。</div>
      <button class="danger" id="revokeBtn" type="button">撤销本机连接器</button>
    </div>
  </div>
</details>
</main>
<script>
const TOKEN = "__CONNECTOR_TOKEN__";
const $ = id => document.getElementById(id);
let currentStatus = null;
let diagnosisBySource = {};
let formDirty = false;
let busy = false;

function headers(){ return {"Content-Type":"application/json","X-Connector-Token":TOKEN}; }
async function api(path, body){
  const options = body === undefined
    ? {headers:{"X-Connector-Token":TOKEN}}
    : {method:"POST",headers:headers(),body:JSON.stringify(body)};
  const response = await fetch(path, options);
  const payload = await response.json().catch(()=>({ok:false,error:{message:"本机服务返回了无法解析的响应"}}));
  if(!payload.ok){ throw new Error((payload.error && payload.error.message) || "操作失败"); }
  return payload.data;
}

function setMessage(text, kind){
  const node = $("formMessage");
  node.textContent = text;
  node.className = "message" + (kind ? " " + kind : "");
}
function setStage(prefix, state, detail, kind){
  const box = $(prefix + "Stage");
  box.className = "stage" + (kind ? " " + kind : "");
  $(prefix + "State").textContent = state;
  $(prefix + "Detail").textContent = detail || "";
}
function stateClass(value){
  if(value === "READY") return "ok";
  if(value === "DEGRADED_UPSTREAM" || value === "DEGRADED_RELAY" || value === "REVOKED") return "err";
  if(value === "PAUSED" || value === "DRAINING" || value === "CONFIGURING" || value === "UNPAIRED") return "warn";
  return "";
}
function sourceByID(id){ return ((currentStatus && currentStatus.sources) || []).find(item=>item.source_id === id); }
function authByID(id){ return ((currentStatus && currentStatus.auth_profiles) || []).find(item=>item.source_id === id); }

function renderStageSummary(){
  if(!currentStatus){ return; }
  const sourceID = $("sourceSelect").value;
  const report = diagnosisBySource[sourceID];
  if(report){
    if(report.ok){ setStage("local", "可访问", "MCP 初始化、认证和工具目录检查通过。", "ready"); }
    else{
      const failure = (report.checks || []).find(item=>item.status === "fail");
      const detail = failure ? ((failure.label || failure.name) + "：" + (failure.detail || "检查失败")) : "内网 MCP 检查未通过。";
      setStage("local", "不可访问", detail, "error");
    }
  }else if(currentStatus.state === "DEGRADED_UPSTREAM"){
    setStage("local", "暂不可用", currentStatus.last_error || "内网 MCP 检查未通过。", "error");
  }else if(sourceID || (currentStatus.sources || []).length){
    setStage("local", "尚未检测", "点击“保存并连接”后执行本机检查。", "");
  }else{
    setStage("local", "尚未配置", "请新增一个内部 MCP 服务。", "");
  }

  if(currentStatus.tunnel_connected){
    setStage("tunnel", "已连接", currentStatus.tunnel_detail || "官方 frpc 来源代理已运行。", "ready");
  }else if(currentStatus.frpc_running){
    setStage("tunnel", "连接中", currentStatus.tunnel_detail || "frpc 进程已启动，但中继尚未就绪。", "warn");
  }else if(currentStatus.state === "DEGRADED_RELAY"){
    setStage("tunnel", "连接失败", currentStatus.last_error || currentStatus.tunnel_detail || "公网中继连接失败。", "error");
  }else{
    setStage("tunnel", "尚未连接", currentStatus.bound ? "保存并连接后启动公网隧道。" : "需要先完成企业绑定。", "");
  }

  if(currentStatus.state === "REVOKED"){
    setStage("platform", "已撤销", "本机身份已撤销，需要开物管理员重新签发。", "error");
  }else if(!currentStatus.bound){
    setStage("platform", "等待管理员配置", "企业绑定、企业集成凭据 K 和来源路由仍待开物管理员完成。", "warn");
  }else if(currentStatus.tunnel_connected){
    setStage("platform", "待管理员确认", "本机和公网隧道已经就绪；平台授权状态需要开物管理员确认，页面不会把本地成功当作平台已可使用。", "warn");
  }else{
    setStage("platform", "等待公网隧道", "隧道建立后仍需开物管理员确认企业集成凭据 K 和来源路由。", "");
  }
}

function renderStatus(status){
  currentStatus = status;
  const pill = $("statePill");
  pill.textContent = status.state_label || status.state || "未知状态";
  pill.className = "pill " + stateClass(status.state);
  $("tenant").textContent = status.tenant_name || "未绑定";
  $("connector").textContent = status.connector_id || "—";
  $("frpc").textContent = status.frpc_running
    ? (status.tunnel_connected ? "已连接" : "进程运行中，中继尚未就绪") + "，重启 " + (status.frpc_restarts || 0) + " 次"
    : "未运行";
  $("version").textContent = status.version || "—";
  $("cloudEndpoint").textContent = status.cloud_configured ? "已配置" : "未配置";
  $("edgeEndpoint").textContent = status.edge_configured ? (status.edge_base || "已配置") : "未配置";
  $("statusReason").textContent = status.last_error || status.reason || "";
  $("bindBtn").disabled = !!status.bound || !status.cloud_configured || busy;
  $("bindHint").textContent = status.bound
    ? "本机已经绑定企业，不需要重复使用接入码。"
    : (status.cloud_configured ? "一次性接入码由开物管理员签发，只能使用一次。" : "当前安装包未配置云端控制面，不能绑定企业。");
  renderSourceOptions(status);
  renderStageSummary();
}

function renderSourceOptions(status){
  const select = $("sourceSelect");
  const previous = select.value;
  select.innerHTML = "";
  const create = document.createElement("option");
  create.value = "";
  create.textContent = "＋ 新增 MCP 服务";
  select.appendChild(create);
  (status.sources || []).forEach(source=>{
    const option = document.createElement("option");
    option.value = source.source_id;
    option.textContent = source.display_name || source.source_id;
    select.appendChild(option);
  });
  if(previous && (status.sources || []).some(source=>source.source_id === previous)){
    select.value = previous;
  }else if(!formDirty){
    select.value = "";
  }
}

function clearToolList(message){
  $("toolList").innerHTML = "";
  const empty = document.createElement("div");
  empty.className = "tool-empty";
  empty.textContent = message;
  $("toolList").appendChild(empty);
}

function renderToolChoices(tools, selected){
  const toolList = $("toolList");
  toolList.innerHTML = "";
  if(!tools.length){ clearToolList("初始化成功，但 MCP 没有返回工具。请检查客户端 MCP 服务配置。"); return; }
  const selectedSet = new Set(selected || []);
  tools.forEach(tool=>{
    const label = document.createElement("label");
    label.className = "tool-option";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = tool.name;
    checkbox.checked = selectedSet.has(tool.name);
    const body = document.createElement("div");
    const name = document.createElement("b");
    name.textContent = tool.name;
    body.appendChild(name);
    const description = document.createElement("p");
    description.textContent = tool.title || tool.description || "上游未提供工具说明";
    body.appendChild(description);
    label.appendChild(checkbox);
    label.appendChild(body);
    toolList.appendChild(label);
  });
}

function selectedTools(){
  return Array.from(document.querySelectorAll("#toolList input[type=checkbox]:checked")).map(item=>item.value);
}
function payloadFromForm(){
  return {
    source_id: $("sourceSelect").value,
    display_name: $("displayName").value.trim(),
    upstream_url: $("upstreamURL").value.trim(),
    bearer_token: $("bearerToken").value,
    dynamic_auth: $("dynamicAuth").checked,
    app_id: $("authAppID").value.trim(),
    channel: $("authChannel").value.trim(),
    user_id: $("authUserID").value.trim(),
    hmac_key: $("authHMACKey").value,
    tools: selectedTools()
  };
}
function validateForm(payload, requireTools){
  if(!payload.display_name){ return "请填写服务名称。"; }
  if(!/^https?:\/\//i.test(payload.upstream_url)){ return "MCP HTTP 地址必须以 http:// 或 https:// 开头。"; }
  if(payload.dynamic_auth){
    if(!payload.app_id){ return "使用华微动态签名时请填写 appid。"; }
    if(!payload.channel){ return "使用华微动态签名时请填写 channel。"; }
    if(!payload.user_id){ return "使用华微动态签名时请填写 userid。"; }
  }
  if(requireTools !== false && !payload.tools.length){ return "请先读取工具列表，并至少勾选一个允许使用的只读工具。"; }
  return "";
}
function syncDynamicAuthFields(){
  $("authFields").style.opacity = $("dynamicAuth").checked ? "1" : ".55";
}
function applySourceSelection(){
  const sourceID = $("sourceSelect").value;
  $("bearerToken").value = "";
  $("authHMACKey").value = "";
  if(!sourceID){
    $("displayName").value = "";
    $("upstreamURL").value = "";
    $("dynamicAuth").checked = true;
    $("authAppID").value = "MCP";
    $("authChannel").value = "";
    $("authUserID").value = "";
    $("bearerToken").placeholder = "填写客户 MCP 要求的 Bearer Token";
    $("authHMACKey").placeholder = "填写客户提供的 HMAC 密钥";
    clearToolList("尚未读取工具。请先填写 MCP 地址和认证信息，再点击“读取工具列表”。");
    syncDynamicAuthFields();
    formDirty = false;
    renderStageSummary();
    return;
  }
  const source = sourceByID(sourceID);
  const auth = authByID(sourceID);
  if(!source){ return; }
  $("displayName").value = source.display_name || "";
  $("upstreamURL").value = source.upstream_url || "";
  const hasDynamic = !!(auth && (auth.configured || auth.app_id || auth.channel || auth.user_id));
  $("dynamicAuth").checked = hasDynamic;
  $("authAppID").value = (auth && auth.app_id) || "MCP";
  $("authChannel").value = (auth && auth.channel) || "";
  $("authUserID").value = (auth && auth.user_id) || "";
  $("bearerToken").placeholder = source.auth && source.auth.mode === "fixed_header"
    ? "已保存访问令牌；留空保持原值"
    : "该来源未保存访问令牌，可按客户要求填写";
  $("authHMACKey").placeholder = hasDynamic ? "已保存 HMAC 密钥；留空保持原值" : "填写客户提供的 HMAC 密钥";
  const savedTools = (source.tool_allowlist || []).slice().sort();
  renderToolChoices(savedTools.map(name=>({name:name, title:"本机已保存的允许工具；可重新读取目录确认"})), savedTools);
  syncDynamicAuthFields();
  formDirty = false;
  renderStageSummary();
}

async function refresh(initial){
  try{
    renderStatus(await api("/api/status"));
    if(initial){ applySourceSelection(); }
  }catch(error){
    $("statePill").textContent = "本机服务未就绪";
    $("statePill").className = "pill err";
    setMessage("无法读取本机连接器状态：" + error.message, "err");
    ["bindBtn","saveConnectBtn","readToolsBtn","diagnoseBtn"].forEach(id=>$(id).disabled=true);
  }
}

$("dynamicAuth").onchange = ()=>{
  syncDynamicAuthFields();
  formDirty = true;
};
["displayName","upstreamURL","bearerToken","authAppID","authChannel","authUserID","authHMACKey"].forEach(id=>{
  $(id).addEventListener("input", ()=>{ formDirty = true; });
});
$("sourceSelect").onchange = applySourceSelection;

$("readToolsBtn").onclick = async ()=>{
  const button = $("readToolsBtn");
  const payload = payloadFromForm();
  const wantedTools = selectedTools();
  const error = validateForm(payload, false);
  if(error){ setMessage(error, "err"); return; }
  button.disabled = true;
  busy = true;
  setMessage("正在连接 MCP 并读取工具目录，不会调用任何业务工具…", "warn");
  setStage("local", "检测中", "正在执行 MCP 初始化与 tools/list。", "warn");
  try{
    const tools = await api("/api/source/tools", payload);
    const savedSelected = (sourceByID(payload.source_id) && sourceByID(payload.source_id).tool_allowlist) || [];
    const available = new Set(tools.map(tool=>tool.name));
    const selection = (wantedTools.length ? wantedTools : savedSelected).filter(name=>available.has(name));
    renderToolChoices(tools, selection);
    setStage("local", "目录读取成功", "发现 " + tools.length + " 个工具，请勾选允许开物调用的工具后保存。", "ready");
    setMessage("工具目录读取成功。请勾选允许使用的只读工具，再点击“保存并连接”。", "ok");
  }catch(requestError){
    setStage("local", "读取失败", requestError.message, "error");
    setMessage("读取工具列表失败：" + requestError.message, "err");
  }finally{
    busy = false;
    button.disabled = false;
    await refresh(false);
  }
};

$("saveConnectBtn").onclick = async ()=>{
  const button = $("saveConnectBtn");
  const payload = payloadFromForm();
  const error = validateForm(payload);
  if(error){ setMessage(error, "err"); return; }
  button.disabled = true;
  busy = true;
  try{
    setMessage("正在保存本机 MCP 配置…", "warn");
    const statusBeforeSave = currentStatus || {};
    if(["READY","CONNECTING","DEGRADED_UPSTREAM","DEGRADED_RELAY","CONFIGURING"].includes(statusBeforeSave.state)){
      await api("/api/pause", {});
    }
    const saved = await api("/api/source/save", payload);
    $("sourceSelect").value = saved.source.source_id;
    $("bearerToken").value = "";
    $("authHMACKey").value = "";
    formDirty = false;
    setStage("local", "检测中", "配置已保存，正在检查内网 MCP。", "warn");
    setMessage("配置已保存。正在执行 MCP 初始化、认证和工具目录检查…", "warn");

    const reports = await api("/api/diagnose", {source_id:saved.source.source_id});
    const report = reports && reports[0];
    if(report){ diagnosisBySource[saved.source.source_id] = report; }
    if(!report || !report.ok){
      const failure = report && (report.checks || []).find(item=>item.status === "fail");
      const detail = failure ? ((failure.label || failure.name) + "：" + (failure.detail || "检查失败")) : "内网 MCP 检查未通过。";
      setStage("local", "不可访问", detail, "error");
      setStage("tunnel", "未建立", "内网 MCP 未通过，未继续启动公网隧道。", "");
      setStage("platform", "未到平台接入阶段", "请先修正本机 MCP 配置。", "");
      setMessage("内网 MCP 检查失败：" + detail + " 请根据诊断详情修正地址、访问令牌或动态签名后重试。", "err");
      $("diagnoseOut").textContent = JSON.stringify(reports, null, 2);
      $("diagnoseOut").classList.remove("hidden");
      $("advancedPanel").open = true;
      return;
    }
    setStage("local", "可访问", "MCP 初始化、认证和工具目录检查通过。", "ready");
    setMessage("内网 MCP 已通过检查。正在核对公网与企业接入状态…", "ok");
    let latest = await api("/api/status");
    renderStatus(latest);
    if(!latest.bound){
      setStage("tunnel", "尚未建立", "本机尚未绑定企业，未启动公网隧道。", "warn");
      setStage("platform", "等待管理员配置", "下一阶段由开物管理员完成企业绑定、企业集成凭据 K 和来源路由。", "warn");
      setMessage("内网 MCP 已验证。本机安装和配置已完成；公网与开物平台接入等待开物管理员完成企业绑定、企业集成凭据 K 和来源路由。", "warn");
      $("advancedPanel").open = true;
      return;
    }
    if(!latest.cloud_configured){
      setStage("tunnel", "无法连接", "当前安装包未配置云端控制面。", "error");
      setStage("platform", "等待管理员配置", "请使用开物管理员提供的正式安装包。", "warn");
      setMessage("内网 MCP 已验证，但当前安装包未配置云端控制面，无法建立公网隧道。", "err");
      return;
    }

    setStage("tunnel", "连接中", "正在启动官方 frpc 来源代理并等待中继状态。", "warn");
    setMessage("内网检查通过，正在建立公网隧道…", "warn");
    try{
      latest = await api("/api/connect", {});
    }catch(connectError){
      setStage("tunnel", "连接失败", connectError.message, "error");
      setStage("platform", "等待公网隧道与平台配置", "公网隧道未就绪，平台接入不能视为完成。", "warn");
      setMessage("公网隧道连接失败：" + connectError.message + " 如果提示来源路由或企业授权，请联系开物管理员。", "err");
      $("advancedPanel").open = true;
      return;
    }
    renderStatus(latest);
    if(latest.tunnel_connected){
      setMessage("内网 MCP 与公网隧道均已就绪。平台接入仍由开物管理员确认企业集成凭据 K 和来源路由，页面不会把这一步显示为平台已可使用。", "warn");
    }else{
      setMessage("内网 MCP 已验证，但公网隧道尚未就绪：" + (latest.tunnel_detail || latest.last_error || "请查看详细诊断。"), "warn");
    }
  }catch(operationError){
    setMessage("保存或检测失败：" + operationError.message, "err");
  }finally{
    busy = false;
    button.disabled = false;
    await refresh(false);
  }
};

$("bindBtn").onclick = async ()=>{
  try{
    busy = true;
    $("bindBtn").disabled = true;
    await api("/api/bind", {code:$("bindCode").value});
    $("bindCode").value = "";
    setMessage("企业绑定成功。请点击“保存并连接”继续检查内网和公网隧道。", "ok");
  }catch(error){
    setMessage("企业绑定失败：" + error.message, "err");
  }finally{
    busy = false;
    await refresh(false);
  }
};

$("importBtn").onclick = async ()=>{
  try{
    const report = await api("/api/import", {config:$("mcpConfig").value});
    const list = $("importIssues");
    list.innerHTML = "";
    (report.issues || []).forEach(item=>{
      const li = document.createElement("li");
      li.textContent = "[" + item.code + "] " + (item.path ? item.path + "：" : "") + item.message;
      if(item.severity === "error"){ li.className = "err"; }
      list.appendChild(li);
    });
    if(!(report.issues || []).length){
      const li = document.createElement("li");
      li.textContent = "JSON 配置校验通过；已替换来源列表，请重新读取工具并保存动态认证。";
      list.appendChild(li);
    }
    await refresh(false);
  }catch(error){
    setMessage("JSON 导入失败：" + error.message, "err");
  }
};

$("diagnoseBtn").onclick = async ()=>{
  try{
    const sourceID = $("sourceSelect").value;
    const reports = await api("/api/diagnose", {source_id:sourceID});
    (reports || []).forEach(report=>{ diagnosisBySource[report.source_id] = report; });
    $("diagnoseOut").textContent = JSON.stringify(reports, null, 2);
    $("diagnoseOut").classList.remove("hidden");
    renderStageSummary();
  }catch(error){
    setMessage("诊断失败：" + error.message, "err");
  }
};
$("pauseBtn").onclick = async ()=>{
  try{ await api("/api/pause", {}); setMessage("本机通道已暂停；来源配置和秘密均已保留。", "warn"); }
  catch(error){ setMessage("暂停失败：" + error.message, "err"); }
  finally{ await refresh(false); }
};
$("disconnectBtn").onclick = async ()=>{
  try{ await api("/api/disconnect", {}); setMessage("通道已断开并回到待配置状态；来源配置和秘密均已保留。", "warn"); }
  catch(error){ setMessage("断开失败：" + error.message, "err"); }
  finally{ await refresh(false); }
};
$("revokeBtn").onclick = async ()=>{
  if(!confirm("确认撤销本机连接器？本机企业身份、访问令牌和动态签名配置将被清除。")){ return; }
  try{ await api("/api/revoke", {}); setMessage("本机连接器已撤销，需要开物管理员重新签发后才能再次接入。", "warn"); }
  catch(error){ setMessage("撤销未完全完成：" + error.message, "err"); }
  finally{ await refresh(false); }
};
$("exportBtn").onclick = async ()=>{
  try{
    const response = await fetch("/api/export", {headers:{"X-Connector-Token":TOKEN}});
    const text = await response.text();
    if(!response.ok){ throw new Error(text || "Edge 地址预览失败"); }
    $("exportOut").textContent = text;
    $("exportOut").classList.remove("hidden");
  }catch(error){ setMessage("Edge 地址预览失败：" + error.message, "err"); }
};

refresh(true);
setInterval(()=>refresh(false), 5000);
</script>
</body>
</html>`
