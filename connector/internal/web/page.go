package web

// page 是本机配置界面的单页模板。%s 处写入本机会话令牌。
const page = `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>开物 · 内网连接器</title>
<style>
:root{--ink:#17223b;--muted:#5b6785;--line:#dfe4ef;--bg:#f5f7fb;--card:#fff;--brand:#1f4fd8;--ok:#0f7a4f;--warn:#a86a00;--err:#b3261e}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--ink)}
header{display:flex;align-items:center;justify-content:space-between;padding:16px 24px;background:var(--card);border-bottom:1px solid var(--line)}
.brand{font-weight:600;font-size:18px}
.brand small{margin-left:8px;color:var(--muted);font-weight:400;font-size:12px}
main{max-width:880px;margin:0 auto;padding:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:14px}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
.pill{display:inline-block;padding:4px 10px;border-radius:999px;font-size:12px;background:#eef2ff;color:var(--brand)}
.pill.ok{background:#e6f4ec;color:var(--ok)}.pill.err{background:#fdeceb;color:var(--err)}.pill.warn{background:#fdf3e2;color:var(--warn)}
h2{font-size:15px;margin:0 0 12px}
p.hint{color:var(--muted);font-size:13px;margin:6px 0 12px;line-height:1.6}
input,textarea{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;font:inherit;background:#fff}
textarea{min-height:120px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px}
button{padding:9px 16px;border-radius:8px;border:1px solid var(--line);background:#fff;color:var(--ink);cursor:pointer;font:inherit}
button.primary{background:var(--brand);border-color:var(--brand);color:#fff}
button:disabled{opacity:.5;cursor:not-allowed}
pre{background:#0f172a;color:#e2e8f0;padding:14px;border-radius:10px;overflow:auto;font-size:12.5px;line-height:1.6}
ul.issues{margin:8px 0 0;padding-left:18px;font-size:13px;color:var(--muted)}
ul.issues li.err{color:var(--err)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}
.kv{font-size:13px;color:var(--muted)}.kv b{display:block;color:var(--ink);font-size:14px;margin-top:4px;word-break:break-all}
.steps{font-size:12px;color:var(--muted);margin-bottom:6px}
</style>
</head>
<body>
<header><div class="brand">开物<small>内网连接器</small></div><span class="pill" id="statePill">读取中…</span></header>
<main>
<section class="card">
  <div class="grid">
    <div class="kv">企业<b id="tenant">—</b></div>
    <div class="kv">连接器<b id="connector">—</b></div>
    <div class="kv">通道<b id="frpc">—</b></div>
    <div class="kv">版本<b id="version">—</b></div>
  </div>
  <p class="hint" id="reason"></p>
</section>

<section class="card">
  <div class="steps">第 1 步</div><h2>绑定企业</h2>
  <p class="hint">向交付人员索取一次性接入码，输入后完成绑定。接入码只能使用一次。</p>
  <div class="row"><input id="code" placeholder="一次性接入码" autocomplete="off"><button class="primary" id="bindBtn">绑定</button></div>
</section>

<section class="card">
  <div class="steps">第 2 步</div><h2>导入现有 MCP 配置</h2>
  <p class="hint">粘贴客户的 MCP 配置（mcpServers 格式或单个 HTTP MCP 对象）。请求头里的凭据只会保存在本机，不写入导出文件。</p>
  <textarea id="mcpConfig" placeholder='{"mcpServers":{"库存查询":{"type":"http","url":"http://10.0.0.8:8000/mcp","headers":{"Authorization":"Bearer ..."}}}}'></textarea>
  <div class="row" style="margin-top:10px"><button class="primary" id="importBtn">导入并校验</button></div>
  <ul class="issues" id="issues"></ul>
</section>

<section class="card">
  <div class="steps">第 3 步</div><h2>检测内网</h2>
  <p class="hint">按网络、证书、协议、认证的顺序检查，不会遍历执行所有工具。</p>
  <div class="row"><button id="diagBtn">开始检测</button></div>
  <pre id="diagOut" hidden></pre>
</section>

<section class="card">
  <div class="steps">第 4 步</div><h2>连接云端</h2>
  <p class="hint">启动加密通道并与云端完成端到端检测。</p>
  <div class="row"><button class="primary" id="connectBtn">连接</button><button id="pauseBtn">暂停</button><button id="disconnectBtn">断开</button></div>
</section>

<section class="card">
  <div class="steps">第 5 步</div><h2>导出平台配置</h2>
  <p class="hint">把下面的配置交给平台管理员导入。凭据占位符会在平台创建授权时替换。</p>
  <div class="row"><button id="exportBtn">生成配置</button></div>
  <pre id="exportOut" hidden></pre>
</section>

<section class="card">
  <div class="row" style="justify-content:space-between">
    <span class="hint" style="margin:0">撤销后本机身份立即失效，需要重新接入。</span>
    <button id="revokeBtn">撤销本机</button>
  </div>
</section>
</main>
<script>
const TOKEN = "__CONNECTOR_TOKEN__";
const $ = (id) => document.getElementById(id);
function headers(){return {"Content-Type":"application/json","X-Connector-Token":TOKEN};}
async function api(path, body){
  const options = body === undefined ? {headers:{"X-Connector-Token":TOKEN}} : {method:"POST",headers:headers(),body:JSON.stringify(body)};
  const resp = await fetch(path, options);
  const data = await resp.json().catch(()=>({ok:false,error:{message:"响应无法解析"}}));
  if(!data.ok){ throw new Error((data.error && data.error.message) || "操作失败"); }
  return data.data;
}
function stateClass(s){ if(s==="READY") return "ok"; if(s==="DEGRADED_UPSTREAM"||s==="DEGRADED_RELAY"||s==="REVOKED") return "err"; if(s==="PAUSED"||s==="DRAINING") return "warn"; return ""; }
function render(status){
  const pill = $("statePill");
  pill.textContent = status.state_label || status.state;
  pill.className = "pill " + stateClass(status.state);
  $("tenant").textContent = status.tenant_name || "未绑定";
  $("connector").textContent = status.connector_id || "—";
  $("frpc").textContent = status.frpc_running ? ("运行中，重启 " + status.frpc_restarts + " 次") : "未运行";
  $("version").textContent = status.version || "—";
  $("reason").textContent = status.last_error || "";
}
async function refresh(){ try{ render(await api("/api/status")); }catch(e){ $("statePill").textContent = "本机服务未就绪"; } }
async function act(button, fn){
  button.disabled = true;
  try{ await fn(); }catch(e){ alert(e.message); } finally { button.disabled = false; refresh(); }
}
$("bindBtn").onclick = () => act($("bindBtn"), async ()=>{ await api("/api/bind",{code:$("code").value}); });
$("importBtn").onclick = () => act($("importBtn"), async ()=>{
  const report = await api("/api/import",{config:$("mcpConfig").value});
  const list = $("issues"); list.innerHTML = "";
  (report.issues||[]).forEach(item=>{ const li=document.createElement("li"); li.textContent = "["+item.code+"] "+(item.path?item.path+"：":"")+item.message; if(item.severity==="error") li.className="err"; list.appendChild(li); });
  if(!(report.issues||[]).length){ const li=document.createElement("li"); li.textContent="配置校验通过"; list.appendChild(li); }
});
$("diagBtn").onclick = () => act($("diagBtn"), async ()=>{
  const reports = await api("/api/diagnose",{});
  const out = $("diagOut"); out.hidden=false; out.textContent = JSON.stringify(reports,null,2);
});
$("connectBtn").onclick = () => act($("connectBtn"), async ()=>{ await api("/api/connect",{}); });
$("pauseBtn").onclick = () => act($("pauseBtn"), async ()=>{ await api("/api/pause",{}); });
$("disconnectBtn").onclick = () => act($("disconnectBtn"), async ()=>{ await api("/api/disconnect",{}); });
$("revokeBtn").onclick = () => { if(!confirm("确认撤销本机连接器？")) return; act($("revokeBtn"), async ()=>{ await api("/api/revoke",{}); }); };
$("exportBtn").onclick = () => act($("exportBtn"), async ()=>{
  const resp = await fetch("/api/export",{headers:{"X-Connector-Token":TOKEN}});
  const out = $("exportOut"); out.hidden=false; out.textContent = await resp.text();
});
refresh(); setInterval(refresh, 3000);
</script>
</body>
</html>`
