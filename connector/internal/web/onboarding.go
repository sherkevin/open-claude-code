package web

// onboardingPage is the local connector's regular MCP setup flow.
const onboardingPage = `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>接入 MCP · 开物内网连接器</title>
<link rel="icon" href="/flintworks-mark.png" type="image/png">
<style>
:root{color-scheme:light;--text:#17223b;--muted:#55627a;--line:#dce3ee;--blue:#2453d5;--pale:#eff4ff;--error:#a62620;--ok:#196443}
*{box-sizing:border-box} [hidden]{display:none!important}
body{margin:0;background:#f4f7fb;color:var(--text);font:15px/1.6 "Segoe UI","Microsoft YaHei",sans-serif}
header{background:#fff;border-bottom:1px solid var(--line)}
.bar{max-width:1060px;margin:auto;min-height:68px;padding:12px 24px;display:flex;gap:16px;align-items:center;justify-content:space-between}
.brand{display:flex;align-items:center;gap:11px;font-weight:700}.brand img{width:28px;height:28px}
.bar a{font-size:13px;text-decoration:none}.bar a:hover{text-decoration:underline}
main{max-width:720px;margin:52px auto;padding:0 20px 44px}
h1{margin:0;font-size:34px;line-height:1.25;letter-spacing:-.02em}h2{margin:0 0 6px;font-size:22px;line-height:1.35}p{margin:0 0 16px}
.lead{margin-top:8px;color:var(--muted)}.muted{color:var(--muted);font-size:13px}a{color:var(--blue)}
.panel{background:#fff;border:1px solid var(--line);border-radius:16px;padding:28px;box-shadow:0 12px 32px #253d7410}
.identity{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin:0 0 18px;color:var(--muted);font-size:13px}
.dot{width:8px;height:8px;border-radius:50%;background:#7b879d}.dot.online{background:var(--ok)}
.identity button{padding:4px 9px;font-size:13px}
.steps{list-style:none;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin:0 0 26px;padding:0;font-size:13px}
.steps li{border-top:3px solid var(--line);padding:8px 0 0;color:var(--muted)}.steps li[aria-current=step]{border-color:var(--blue);color:var(--blue);font-weight:700}.steps li.done{border-color:#7799f3;color:var(--text)}
.field{display:block;margin:17px 0 0;font-weight:650}.field small{font-size:12px;color:var(--muted);font-weight:400}
input:not([type=checkbox]),textarea{width:100%;display:block;margin-top:5px;padding:10px 12px;border:1px solid #cbd5e5;border-radius:8px;background:#fff;color:var(--text);font:inherit}
textarea{resize:vertical;min-height:160px;font:12px/1.55 Consolas,monospace;overflow-wrap:anywhere}
button,.button-link{min-height:38px;padding:9px 15px;border:1px solid #cbd5e5;border-radius:8px;background:#fff;color:var(--text);font:inherit;text-decoration:none;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;text-align:center}
button:hover:not(:disabled),.button-link:hover{background:var(--pale)}button:disabled{opacity:.53;cursor:not-allowed}
button.primary{background:var(--blue);color:#fff;border-color:var(--blue);font-weight:650}button.primary:hover:not(:disabled){background:#1a42b9}
:focus-visible{outline:3px solid #85a8ff;outline-offset:2px}
.actions{margin-top:20px;display:flex;flex-wrap:wrap;gap:9px;align-items:center}
details{margin-top:15px}summary{cursor:pointer;color:var(--muted);font-size:13px}
.credentials{border:1px solid var(--line);border-radius:9px;padding:0 14px 14px;margin-top:9px}
.check{display:flex;align-items:flex-start;gap:9px;cursor:pointer}.check input{width:17px;height:17px;margin:4px 0 0;accent-color:var(--blue);flex:0 0 auto}
.check code{display:block;overflow-wrap:anywhere;font:13px/1.5 Consolas,monospace}.check small{display:block;font-size:12px;margin-top:3px;color:var(--muted);overflow-wrap:anywhere}
.tools{display:grid;gap:7px;margin-top:12px;max-height:300px;overflow:auto;padding:3px}.tools .check{padding:9px 11px;border:1px solid var(--line);border-radius:8px}.tools .check:has(:checked){border-color:#9cbbff;background:var(--pale)}
.found{margin:0 0 3px;color:var(--muted);font-size:13px}.found strong{color:var(--text)}
.notice{margin:16px 0 0;color:var(--muted);font-size:13px}.notice:empty{display:none}.notice.error{color:var(--error)}.notice.ok{color:var(--ok)}
.saved{margin:18px 3px 0}.saved summary{font-size:14px}.saved ul{padding:0;list-style:none;margin:8px 0 0;display:grid;gap:6px}.saved button{width:100%;justify-content:space-between;gap:10px;overflow-wrap:anywhere;text-align:left}
.result-pill{display:inline-block;background:#e8f5ef;color:var(--ok);padding:2px 8px;margin:0 0 9px;border-radius:5px;font-size:12px;font-weight:650}
.assistance{margin-bottom:18px}.assistance code{display:block;margin:3px 0 9px;overflow-wrap:anywhere}.assistance .actions{margin-top:12px}
.assist-console{margin:14px 0 0;padding:12px 14px;border-radius:8px;background:#0f1b2d;color:#dce7ff;font:12px/1.55 Consolas,"Cascadia Mono",monospace;white-space:pre-wrap;overflow-wrap:anywhere;max-height:260px;overflow:auto}
.assist-command{margin:12px 0 0}.assist-command h3{margin:0 0 6px;font-size:15px}.assist-command pre{margin:8px 0}.assist-command .muted{margin:6px 0 0}
.terminal-help{margin:0 0 18px}.terminal-help p{margin:0}.terminal-help a{margin-top:10px}
@media(max-width:560px){main{margin:30px auto;padding:0 14px 30px}.bar{padding:12px 14px;gap:10px}.brand{gap:7px;font-size:14px}h1{font-size:28px}.panel{padding:20px}.actions button.primary{flex-grow:1}}
</style>
</head>
<body>
<header><div class="bar">
  <div class="brand"><img src="/flintworks-mark.png" width="28" height="28" alt="">开物内网连接器</div>
  <a href="/support">连接测试与诊断</a>
</div></header>
<main>
  <h1>连接业务系统</h1>
  <p class="lead">将已经授权的业务服务接入开物。</p>
  <div class="identity"><span id="connectionDot" class="dot" aria-hidden="true"></span><span id="connectionState" role="status">正在读取连接状态…</span><button id="reloadStatusBtn" type="button" hidden>重试</button><button id="resumeBtn" type="button" hidden>恢复连接</button></div>
  <section id="assistPanel" class="panel assistance" aria-labelledby="assistTitle" hidden>
    <h2 id="assistTitle">开物技术协助（远程 CMD 协助）</h2>
    <strong id="assistService"></strong>
    <code id="assistAddress"></code>
    <p id="assistState" class="muted" role="status"></p>
    <p id="assistExpiry" class="muted" role="status" hidden></p>
    <p class="muted">仅在企业管理员批准后开启。协助期间，工程师可以在本机执行诊断及维护命令（包括读取或修改文件）；你可以随时停止。七天授权按固定期限计算，不会因为连接器轮询而延长；连接器进程重启后本机授权立即失效，需要重新确认。</p>
    <div class="actions">
      <button id="assistStart" class="primary" type="button" hidden>开始协助</button>
      <button id="assistTarget" class="primary" type="button" hidden>确认这个业务服务</button>
      <button id="assistStop" type="button" hidden>停止协助</button>
    </div>
    <section id="assistCommandPanel" class="assist-command" aria-label="开物工程师下发的命令" hidden>
      <h3>开物工程师的命令</h3>
      <p class="muted">连接器自动执行并把输出回传开物。你随时可以点「停止协助」。</p>
      <pre id="assistCommand" class="assist-console" aria-label="命令内容"></pre>
      <p id="assistCommandState" class="muted" role="status"></p>
      <details id="assistOutputDetails" hidden>
        <summary>查看命令输出</summary>
        <pre id="assistOutput" class="assist-console" aria-label="命令输出"></pre>
      </details>
    </section>
    <p id="assistNotice" class="notice" role="status"></p>
  </section>
  __ATTENDED_SUPPORT_ENTRY__
  <section class="panel" aria-label="接入本机 MCP">
    <form id="bindForm" hidden>
      <h2>绑定企业</h2>
      <p class="muted">第一次使用时，填写开物为你签发的企业绑定码。</p>
      <label class="field" for="bindCode">企业绑定码</label>
      <input id="bindCode" type="password" autocomplete="off" required>
      <div class="actions"><button id="bindBtn" class="primary" type="submit">绑定企业</button></div>
    </form>
    <div id="workflow" hidden>
      <ol class="steps" aria-label="接入步骤"><li id="addressStep" aria-current="step">1. 本机服务</li><li id="toolsStep">2. 选择工具</li><li id="resultStep">3. 导入开物</li></ol>
      <form id="addressForm">
        <h2>填写 MCP 地址</h2>
        <label class="field" for="upstreamAddress">本机端口或完整 HTTP 地址</label>
        <input id="upstreamAddress" placeholder="例如 3000" maxlength="2048" autocomplete="off" spellcheck="false" aria-describedby="addressHelp" required>
        <p class="muted" id="addressHelp">填端口 3000 会使用 <code>http://127.0.0.1:3000/mcp</code>。路径不同时，填完整地址。</p>
        <details id="sourceOptions">
          <summary>服务名称和认证（选填）</summary>
          <div class="credentials">
            <label class="field" for="sourceName">服务名称</label>
            <input id="sourceName" maxlength="80" autocomplete="off">
            <label class="field" for="bearerToken">Bearer 访问令牌 <small id="bearerHint">服务要求认证时，填写该服务的令牌</small></label>
            <input id="bearerToken" type="password" autocomplete="new-password">
            <label class="check" style="margin-top:14px"><input id="dynamicAuth" type="checkbox">使用华微动态签名</label>
            <div id="authFields" hidden>
              <label class="field" for="authAppID">应用标识（appid）</label><input id="authAppID" value="MCP" autocomplete="off">
              <label class="field" for="authChannel">渠道编号（channel）</label><input id="authChannel" autocomplete="off">
              <label class="field" for="authUserID">读取身份（userid）</label><input id="authUserID" autocomplete="off">
              <label class="field" for="authHMACKey">签名密钥 <small>留空保留已有值</small></label><input id="authHMACKey" type="password" autocomplete="new-password">
            </div>
          </div>
        </details>
        <div class="actions"><button id="readToolsBtn" class="primary" type="submit">读取工具</button></div>
      </form>
      <form id="toolForm" hidden>
        <h2>选择允许开物调用的工具</h2>
        <p class="found">已读取 <strong id="foundName"></strong> 的工具。勾选需要开放的工具，首次接入可先选一个只读工具。</p>
        <div id="toolsList" class="tools"></div>
        <div class="actions"><button id="generateBtn" class="primary" type="submit" disabled>生成 MCP JSON</button><button id="retryExportBtn" type="button" hidden>重试生成 JSON</button><button id="editAddressBtn" type="button">返回修改地址</button></div>
      </form>
      <section id="resultPanel" hidden>
        <p class="result-pill">配置已生成</p>
        <h2>复制配置到开物</h2>
        <p>复制 MCP JSON，在开物点击「MCP → 导入配置」，粘贴后点「测试连接」。</p>
        <div class="actions"><button id="copyJSONBtn" class="primary" type="button">复制 MCP JSON</button><a id="openPlatform" class="button-link" href="https://flintw.com/project/kaiwu/" target="_blank" rel="noopener noreferrer">打开开物</a></div>
        <details id="jsonDetails"><summary>查看配置</summary><textarea id="mcpJSON" readonly aria-label="MCP JSON" spellcheck="false"></textarea></details>
        <p class="muted" style="margin:12px 0 0">这份配置含开物访问凭据，请只粘贴到获准使用的平台。</p>
        <div class="actions"><button id="addAnotherBtn" type="button">接入另一个 MCP</button></div>
      </section>
    </div>
    <p id="notice" class="notice" role="status" aria-live="polite"></p>
    <section id="exportHelp" class="notice" aria-label="开物接入协助" hidden>
      <p>不用重新填写地址或访问凭据。请联系开物技术支持处理权限，处理后可直接重试。</p>
      <div class="actions"><button id="exportHelpBtn" type="button" hidden>查看协助方式</button><a href="/support">连接测试与诊断</a></div>
    </section>
  </section>
  <details class="saved" id="savedSources" hidden><summary>已保存的 MCP 服务</summary><ul id="savedSourcesList"></ul></details>
</main>
<script>
const TOKEN = "__CONNECTOR_TOKEN__";
const $ = id => document.getElementById(id);
let status;
let phase = "address";
let busy = false;
let sourceID = "";
let draft;
let pendingExport = "";
let exportNeedsSupport = false;
let assistOffer;
let assistEnabled = false;
let assistAvailability = "checking";
let assistTargetAfterStart = false;
let assistWeekConsent = false;
let assistRefreshing = false;
let assistAction = "";
let assistChange = 0;
let assistCommand;

async function api(path, body){
  const headers = {"X-Connector-Token":TOKEN};
  const options = {headers};
  if(body !== undefined){
    headers["Content-Type"] = "application/json";
    options.method = "POST";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const payload = await response.json().catch(()=>({}));
  if(!response.ok || !payload.ok){
    const error = new Error(payload.error?.message || "未能读取连接器的响应，请重试。");
    error.code = payload.error?.code;
    throw error;
  }
  return payload.data;
}

function notice(text, kind = ""){
  $("notice").textContent = text;
  $("notice").className = "notice" + (kind ? " " + kind : "");
}

function renderAssist(){
  const eligible = status?.bound && status.state !== "REVOKED" && assistEnabled;
  $("assistPanel").hidden = (!status?.bound || status.state === "REVOKED") && !assistOffer && !$("assistNotice").textContent;
  const pending = assistOffer?.status === "pending";
  const active = assistOffer?.status === "active";
  const local = !!assistOffer?.local_active;
  const awaitingTarget = local && assistTargetAfterStart && !assistOffer.url;
  let target = assistOffer?.url ? assistOffer : undefined;
  const needsTarget = awaitingTarget || (eligible && !active && !assistTargetAfterStart);
  if(needsTarget && !target && $("upstreamAddress").value.trim()){
    try{ target = localServiceTarget(); }catch(_error){}
  }
  $("assistService").textContent = target?.name || "";
  $("assistAddress").textContent = target?.url || "";
  $("assistAddress").hidden = !target;
  $("assistState").textContent = awaitingTarget ? "已开启命令行协助。排查本机已保存的服务无需重新填写地址或凭据；若需新增业务服务，再在下方确认该服务。" :
    local ? "已开始协助。开物工程师下发的命令会由连接器自动执行，并把输出回传开物。你随时可以停止。" :
    active ? "本机协助已停止。请点「停止协助」通知开物。" :
    !eligible && !pending ? (assistAvailability === "checking" ? "正在核对本机协助权限…" :
      assistAvailability === "unreachable" ? "暂时无法读取协助权限，请检查连接或联系开物技术支持。" :
      "此设备尚未开通协助权限，请联系开物技术支持。无需重新安装。") :
    assistTargetAfterStart ? (assistWeekConsent
      ? "点击「开始协助」后，本次授权自本机确认起固定 7 天有效；到期自动停止，不能续期。"
      : "点击「开始协助」后，开物工程师可在授权期间排查本机问题（旧版授权按 24 小时上限、最近在线后 30 分钟到期）。") :
    target ? "点击「开始协助」，允许开物工程师读取并协助接入这个 MCP 服务。" :
    "先在下方填写 MCP 地址，再点击「开始协助」。";
  // Show the actually negotiated policy and deadline, not a hard-coded promise:
  // an older control plane keeps the legacy 24h/30min lease.
  const consentDeadline = assistOffer?.consent_expires_at || assistOffer?.expires_at;
  const weekConsent = assistOffer?.consent_policy === "week_168h";
  $("assistExpiry").hidden = !active || !consentDeadline;
  $("assistExpiry").textContent = active && consentDeadline ? (weekConsent
    ? "本次授权自本机确认起固定 7 天有效，到期时间 " + new Date(consentDeadline).toLocaleString("zh-CN", {hour12:false}) + "；到期或停止后不能恢复。"
    : "旧版授权：最近一次连接器在线后 30 分钟到期，且不超过开始后 24 小时。到期时间 " + new Date(consentDeadline).toLocaleString("zh-CN", {hour12:false}) + "。") : "";
  $("assistStart").textContent = assistWeekConsent ? "开始协助（固定7天）" : "开始协助";
  $("assistStart").hidden = active || (!eligible && !pending);
  $("assistTarget").hidden = !awaitingTarget;
  $("assistStop").hidden = !active;
  $("assistStart").disabled = !!assistAction;
  $("assistTarget").disabled = !!assistAction;
  $("assistStop").disabled = !!assistAction && assistAction !== "target";
  $("exportHelpBtn").hidden = !eligible && !local;
  const command = local ? assistCommand : undefined;
  $("assistCommandPanel").hidden = !command;
  if(command){
    $("assistCommand").textContent = command.command || "";
    $("assistCommandState").textContent = command.state === "running" ? "正在本机执行，完成后会自动回传开物。" :
      command.state === "succeeded" ? "执行完成，输出已自动回传开物。" :
      command.state === "timeout" ? "执行超时，结果已自动回传开物。" :
      command.state === "canceled" ? "命令已取消。" :
      "执行失败，结果已自动回传开物。";
    $("assistOutputDetails").hidden = !command.output;
    $("assistOutput").textContent = command.output || "";
  }
}

async function refreshAssist(){
  if(!status?.bound || status.state === "REVOKED" || assistAction || assistRefreshing){ return; }
  assistRefreshing = true;
  const revision = assistChange;
  try{
    const {enabled, offer, target_after_start, week_consent, last_command} = await api("/api/assist/offer");
    if(assistAction || revision !== assistChange){ return; }
    assistEnabled = enabled;
    assistAvailability = enabled ? "enabled" : "disabled";
    assistTargetAfterStart = !!target_after_start;
    assistWeekConsent = !!week_consent;
    assistCommand = last_command;
    assistOffer = offer?.status === "active" || (!assistEnabled && offer?.status === "pending") ? offer : undefined;
    renderAssist();
  }catch(_error){
    // Keep an active local consent and its Stop action available even offline.
    if(!assistAction && revision === assistChange && !assistOffer?.local_active){
      assistEnabled = false;
      assistAvailability = "unreachable";
      renderAssist();
    }
  }finally{
    assistRefreshing = false;
  }
}

async function changeAssist(action){
  if(assistAction && !(assistAction === "target" && action === "stop")){ return; }
  let request;
  if(action === "stop" || (!assistEnabled && assistOffer?.status === "pending")){
    if(!assistOffer){ return; }
    request = {session_id:assistOffer.id};
  }else if(action === "start" && assistTargetAfterStart){
    if(!assistEnabled || assistOffer?.status === "active"){ return; }
    request = {};
  }else{
    const confirmsTarget = action === "target";
    if(confirmsTarget){
      if(!assistTargetAfterStart || !assistOffer?.local_active || assistOffer.url){ return; }
    }else if(!assistEnabled || assistOffer?.status === "active"){ return; }
    try{
      if(!$("upstreamAddress").value.trim()){ throw new Error("请先在下方填写 MCP 地址。"); }
      request = localServiceTarget();
      if(confirmsTarget){ request.session_id = assistOffer.id; }
    }catch(error){
      $("assistNotice").textContent = error.message;
      if(phase !== "address"){ phase = "address"; render(); }
      $("upstreamAddress").focus();
      return;
    }
  }
  if(action === "start" && assistWeekConsent){ request.consent_policy = "week_168h"; }
  assistAction = action;
  const revision = ++assistChange;
  $("assistNotice").textContent = "";
  renderAssist();
  try{
    const offer = await api("/api/assist/" + action, request);
    if(revision !== assistChange){ return; }
    if(action === "stop"){
      assistOffer = undefined;
      assistCommand = undefined;
      $("assistNotice").textContent = "协助已停止。";
    }else{
      assistOffer = offer || { ...assistOffer, status:"active", local_active:true };
    }
  }catch(error){
    if(revision !== assistChange){ return; }
    if(action === "stop"){ assistOffer = { ...assistOffer, local_active:false }; }
    $("assistNotice").textContent = error.message;
  }finally{
    if(revision === assistChange){
      assistAction = "";
      renderAssist();
      void refreshAssist();
    }
  }
}

function selectedTools(){
  return Array.from($("toolsList").querySelectorAll("input:checked"), input=>input.value);
}

function render(){
  renderAssist();
  const bound = status?.bound && status.state !== "REVOKED";
  $("bindForm").hidden = !status || status.bound || !status.cloud_configured;
  $("workflow").hidden = !bound;
  $("addressForm").hidden = phase !== "address";
  $("toolForm").hidden = phase !== "tools";
  $("resultPanel").hidden = phase !== "result";
  const steps = ["address", "tools", "result"];
  steps.forEach((step, index)=>{
    const item = $(step + "Step");
    if(step === phase){ item.setAttribute("aria-current", "step"); }
    else{ item.removeAttribute("aria-current"); }
    item.classList.toggle("done", index < steps.indexOf(phase));
  });
  const connected = bound && !!status.tunnel_connected;
  let connectionText = "正在读取连接状态…";
  if(status){
    if(status.state === "REVOKED"){ connectionText = "企业绑定已撤销"; }
    else if(!bound){ connectionText = "待绑定企业"; }
    else{
      const label = status.state === "PAUSED" ? "连接已暂停" : (connected ? "已连接开物" : "已绑定企业");
      connectionText = status.tenant_name ? status.tenant_name + " · " + label : label;
    }
  }
  $("connectionState").textContent = connectionText;
  $("connectionDot").classList.toggle("online", connected);
  $("resumeBtn").hidden = !bound || status.state !== "PAUSED";
  $("resumeBtn").disabled = busy;
  $("bindBtn").disabled = busy;
  $("readToolsBtn").disabled = busy;
  $("generateBtn").hidden = !!pendingExport;
  $("generateBtn").disabled = busy || !selectedTools().length;
  $("retryExportBtn").hidden = !pendingExport;
  $("retryExportBtn").disabled = busy;
  $("exportHelp").hidden = !exportNeedsSupport;
  $("editAddressBtn").disabled = busy;
  $("authFields").hidden = !$("dynamicAuth").checked;
  for(const input of $("toolsList").querySelectorAll("input")){ input.disabled = busy || !!pendingExport; }
}

function updateSources(){
  const sources = status?.sources || [];
  $("savedSources").hidden = !status?.bound || !sources.length;
  $("savedSourcesList").replaceChildren(...sources.map(source=>{
    const row = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = (source.display_name || "MCP 服务") + (source.disabled ? "（已停用）" : "");
    button.onclick = ()=>{ if(!busy){ editSource(source); } };
    row.append(button);
    return row;
  }));
}

function editSource(source){
  sourceID = source?.source_id || "";
  const profile = (status?.auth_profiles || []).find(item=>item.source_id === sourceID);
  $("upstreamAddress").value = source?.upstream_url || "";
  $("sourceName").value = source?.display_name || "";
  $("bearerToken").value = "";
  $("bearerHint").textContent = sourceID ? "留空保留已有值" : "服务要求认证时，填写该服务的令牌";
  $("dynamicAuth").checked = !!profile?.configured;
  $("authAppID").value = profile?.app_id || "MCP";
  $("authChannel").value = profile?.channel || "";
  $("authUserID").value = profile?.user_id || "";
  $("authHMACKey").value = "";
  $("sourceOptions").open = false;
  $("toolsList").replaceChildren();
  $("mcpJSON").value = "";
  $("jsonDetails").open = false;
  draft = undefined;
  pendingExport = "";
  exportNeedsSupport = false;
  phase = "address";
  render();
  notice(source?.disabled ? "这个服务已停用。请在连接测试与诊断中启用它，再生成配置。" : "");
  $("upstreamAddress").focus();
}

function localURL(value){
  const address = value.trim();
  if(/^\d+$/.test(address)){
    const port = Number(address);
    if(port < 1 || port > 65535){ throw new Error("端口必须在 1 到 65535 之间。"); }
    return new URL("http://127.0.0.1:" + port + "/mcp");
  }
  let url;
  try{ url = new URL(address); }catch(_error){ throw new Error("请填写本机端口或完整的 HTTP / HTTPS 地址。"); }
  if(!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.hash){
    throw new Error("请填写 HTTP / HTTPS 地址。访问凭据请放在下方认证设置中。");
  }
  return url;
}

function localServiceTarget(){
  const endpoint = localURL($("upstreamAddress").value);
  return {name:$("sourceName").value.trim() || (endpoint.port ? "MCP " + endpoint.port : "本机 MCP"), url:endpoint.href};
}

function sourceInput(){
  const target = localServiceTarget();
  const dynamicAuth = $("dynamicAuth").checked;
  return {
    source_id: sourceID,
    display_name: target.name,
    upstream_url: target.url,
    bearer_token: $("bearerToken").value.trim(),
    dynamic_auth: dynamicAuth,
    app_id: $("authAppID").value.trim(), channel: $("authChannel").value.trim(), user_id: $("authUserID").value.trim(),
    hmac_key: $("authHMACKey").value.trim()
  };
}

async function operate(text, work){
  if(busy){ return; }
  busy = true;
  render();
  notice(text);
  try{ await work(); }catch(error){
    notice(error.message, "error");
    if(error.code === "DEVICE_EXPORT_DENIED" && pendingExport){ exportNeedsSupport = true; }
    if(error.code === "UPSTREAM_UNAUTHORIZED"){
      $("sourceOptions").open = true;
      $("bearerToken").focus();
    }
  }
  finally{ busy = false; render(); }
}

async function loadStatus(){
  $("reloadStatusBtn").hidden = true;
  try{
    status = await api("/api/status");
    updateSources();
    const revoked = status.state === "REVOKED";
    if(revoked || !status.bound){
      assistChange++;
      assistOffer = undefined;
      assistEnabled = false;
      assistAvailability = "checking";
      assistTargetAfterStart = false;
      assistWeekConsent = false;
      assistAction = "";
      assistCommand = undefined;
      $("assistNotice").textContent = "";
      renderAssist();
    }
    notice(revoked && status.bound ? "企业绑定已撤销，请联系开物技术支持。" :
      (!status.bound && !status.cloud_configured ? "当前安装包未配置开物服务，请联系技术支持。" : ""));
  }catch(error){
    $("reloadStatusBtn").hidden = false;
    notice(error.message, "error");
  }
  render();
  void refreshAssist();
}

async function readTools(event){
  event.preventDefault();
  await operate("正在读取本机工具…", async()=>{
    const input = sourceInput();
    const source = status.sources?.find(item=>item.source_id === sourceID);
    if(source?.disabled){ throw new Error("请先在连接测试与诊断中启用这个服务。"); }
    const discovered = await api("/api/source/tools", input);
    if(!discovered.length){ throw new Error("这个 MCP 服务没有返回可用工具，请核对地址和访问凭据。"); }
    const previous = new Set(source?.tool_allowlist || []);
    $("toolsList").replaceChildren(...discovered.map(tool=>{
      const label = document.createElement("label");
      label.className = "check";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = tool.name;
      checkbox.checked = previous.has(tool.name);
      checkbox.onchange = render;
      const info = document.createElement("span");
      const name = document.createElement("code");
      name.textContent = tool.title && tool.title !== tool.name ? tool.title + " · " + tool.name : tool.name;
      info.append(name);
      if(tool.description){
        const description = document.createElement("small");
        description.textContent = tool.description;
        info.append(description);
      }
      label.append(checkbox, info);
      return label;
    }));
    draft = input;
    pendingExport = "";
    exportNeedsSupport = false;
    $("foundName").textContent = input.display_name;
    phase = "tools";
    notice("");
  });
  if(phase === "tools"){ $("toolsList").querySelector("input")?.focus(); }
}

async function exportJSON(){
  exportNeedsSupport = false;
  const result = await api("/api/mcp-export", {source_id:pendingExport});
  if(!result.mcp_config?.mcpServers){ throw new Error("开物未返回完整的 MCP JSON，请重试。"); }
  $("mcpJSON").value = JSON.stringify(result.mcp_config, null, 2);
  if(result.platform_url){ $("openPlatform").href = result.platform_url; }
  pendingExport = "";
  phase = "result";
  notice("");
}

async function generateJSON(event){
  event.preventDefault();
  const tools = selectedTools();
  if(!draft || !tools.length || pendingExport){ return; }
  await operate("正在连接开物并生成配置…", async()=>{
    if(status.state === "PAUSED"){ throw new Error("请先点击页面上方的「恢复连接」。"); }
    const saved = await api("/api/source/save-and-connect", {...draft, source_id:sourceID, tools});
    sourceID = saved.source.source_id;
    status = saved.status;
    draft.bearer_token = "";
    draft.hmac_key = "";
    $("bearerToken").value = "";
    $("authHMACKey").value = "";
    updateSources();
    if(saved.connection_error || !status.tunnel_connected){
      throw new Error("服务已保存，连接开物未完成。" + (saved.connection_error || "请恢复连接后重试。"));
    }
    pendingExport = sourceID;
    await exportJSON();
  });
  if(phase === "result"){ $("copyJSONBtn").focus(); }
}

async function copyJSON(){
  const field = $("mcpJSON");
  if(!field.value){ return; }
  try{
    await navigator.clipboard.writeText(field.value);
    notice("已复制。请到开物的「导入配置」粘贴。", "ok");
  }catch(_error){
    $("jsonDetails").open = true;
    field.focus();
    field.select();
    notice("请选择 Ctrl+C 复制选中的配置。");
  }
}

$("bindForm").onsubmit = event=>{
  event.preventDefault();
  return operate("正在绑定企业…", async()=>{
    const code = $("bindCode").value.trim();
    if(!code){ throw new Error("请输入企业绑定码。"); }
    status = await api("/api/bind", {code});
    $("bindCode").value = "";
    updateSources();
    notice("");
  }).then(()=>{ if(status?.bound){ $("upstreamAddress").focus(); void refreshAssist(); } });
};
$("addressForm").onsubmit = readTools;
$("toolForm").onsubmit = generateJSON;
$("retryExportBtn").onclick = async()=>{
  if(!pendingExport){ return; }
  await operate("正在生成 MCP JSON…", exportJSON);
  if(phase === "result"){ $("copyJSONBtn").focus(); }
};
$("copyJSONBtn").onclick = copyJSON;
$("editAddressBtn").onclick = ()=>{
  pendingExport = "";
  exportNeedsSupport = false;
  phase = "address";
  render();
  notice("");
  $("upstreamAddress").focus();
};
$("addAnotherBtn").onclick = ()=>editSource();
$("exportHelpBtn").onclick = ()=>{
  // Navigation only: the customer must still explicitly start assistance.
  if($("assistPanel").hidden){ return; }
  $("assistPanel").scrollIntoView?.({behavior:"smooth",block:"start"});
  if(!$("assistStart").hidden){ $("assistStart").focus(); }
  else if(!$("assistStop").hidden){ $("assistStop").focus(); }
};
$("assistStart").onclick = ()=>changeAssist("start");
$("assistTarget").onclick = ()=>changeAssist("target");
$("assistStop").onclick = ()=>changeAssist("stop");
$("upstreamAddress").oninput = renderAssist;
$("sourceName").oninput = renderAssist;
$("dynamicAuth").onchange = render;
$("reloadStatusBtn").onclick = loadStatus;
$("resumeBtn").onclick = ()=>operate("正在恢复连接…", async()=>{
  status = await api("/api/connect", {});
  updateSources();
  notice(status.tunnel_connected ? "已恢复连接。" : (status.last_error || "还未连到开物，请查看诊断。"), status.tunnel_connected ? "ok" : "error");
});

renderAssist();
loadStatus();
let assistRefreshTimer;
async function scheduleAssistRefresh(){
  clearTimeout(assistRefreshTimer);
  await refreshAssist();
  assistRefreshTimer = setTimeout(scheduleAssistRefresh, assistOffer?.local_active ? 1500 : 5000);
}
void scheduleAssistRefresh();
</script>
</body>
</html>`
