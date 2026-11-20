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
.self-test-stages{grid-template-columns:repeat(4,minmax(0,1fr))}
.self-test-answer{border:1px solid #d8e2fa;border-radius:10px;background:#f8faff;padding:12px;margin-top:12px;font-size:12.5px}
.self-test-answer b{display:block;margin-bottom:4px}.self-test-list{margin:8px 0 0;padding-left:18px;color:var(--muted);font-size:12.5px}
.first-run{border-left:3px solid var(--brand);border-radius:8px;background:#f8faff;padding:12px}
.first-run p{margin:5px 0 10px}
.bind-panel{margin-top:12px}.bind-panel p{margin:0 0 6px}.bind-panel form{align-items:flex-end}
.bind-panel form .field{max-width:420px}
.self-test-result{margin-top:12px}.self-test-result code{overflow-wrap:anywhere}
a{color:var(--brand)}
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
#saveConnectBtn{min-width:160px;font-weight:650}.message{flex:1 1 260px;font-size:13px;color:var(--muted)}
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
.log-toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:12px 0}
.log-tabs{display:flex;padding:3px;border:1px solid var(--line);border-radius:9px;background:#f8faff}
.log-tab{border:0;background:transparent;padding:7px 13px;border-radius:7px;color:var(--muted)}
.log-tab.active{background:#fff;color:var(--brand);box-shadow:0 1px 4px rgba(31,79,216,.12)}
.log-toolbar select{width:auto;min-width:105px;padding:8px 10px}
.log-actions{display:flex;gap:8px;flex-wrap:wrap;margin-left:auto}
.log-summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin:10px 0 12px}
.log-summary .kv{padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:#fbfcff}
.log-notice{font-size:12.5px;color:var(--muted);margin:6px 0 10px;min-height:20px}
.log-notice.err{color:var(--err)}.log-notice.warn{color:var(--warn)}
.log-scroll{max-height:440px;overflow:auto;border:1px solid var(--line);border-radius:9px;background:#fff}
.log-table{width:100%;border-collapse:collapse;font-size:12.5px}
.log-table th{position:sticky;top:0;z-index:1;background:#f5f7fb;color:var(--muted);text-align:left;font-weight:600;white-space:nowrap}
.log-table th,.log-table td{padding:9px 10px;border-bottom:1px solid #edf0f6;vertical-align:top}
.log-table tr:last-child td{border-bottom:0}
.log-time{white-space:nowrap;color:var(--muted)}
.log-message{min-width:180px}.log-meta{margin-top:3px;color:var(--muted);font-size:11.5px;word-break:break-all}
.log-duration{white-space:nowrap;color:var(--muted)}
.log-level{display:inline-block;padding:2px 7px;border-radius:999px;font-size:11px;font-weight:650}
.log-level.info{background:#eaf1ff;color:#2450bd}.log-level.warn{background:#fff3df;color:#936000}.log-level.error{background:#fdecea;color:#a62a22}
.copy-button{padding:3px 8px;font-size:11px;border-radius:6px}
.log-empty{text-align:center;color:var(--muted);padding:24px 12px!important}
.hidden{display:none!important}
@media(max-width:720px){header{padding:14px 16px}main{padding:14px}.stage-grid,.form-grid{grid-template-columns:1fr}.span-2{grid-column:auto}.card{padding:15px}.tools-head{align-items:stretch;flex-direction:column}.action-bar{align-items:stretch}.action-bar .primary{width:100%}.log-actions{width:100%;margin-left:0}.log-toolbar{align-items:stretch}.log-tab{flex:1}.log-scroll{max-height:360px}}
</style>
</head>
<body>
<header><div class="brand">开物<small>内网连接器</small></div><span class="pill" id="statePill">读取中…</span></header>
<main>
<section class="card intro">
  <h1>连接器首次使用</h1>
  <p>先检查本机示例，绑定企业后再验证公网，最后接入业务服务并到开物试问。</p>
</section>

<section class="card" id="builtinDemoPanel">
  <div class="row" style="justify-content:space-between;align-items:flex-start">
    <div>
      <h2>内置测试服务（示例数据）</h2>
      <p class="section-hint">使用随安装启动的固定示例检查连接，无需填写业务地址或凭据。</p>
    </div>
    <span class="pill" id="builtinDemoPill">读取中…</span>
  </div>
  <div class="first-run">
    <strong id="nextStepTitle">先检查本机示例</strong>
    <p class="subtle" id="nextStepDetail">正在读取连接器状态…</p>
    <div class="row">
      <button class="primary" id="selfTestBtn" type="button" disabled>运行连接测试</button>
      <button class="primary hidden" id="nextStepBtn" type="button"></button>
      <button class="hidden" id="copySelfTestTraceBtn" type="button">复制诊断编号</button>
    </div>
    <div class="bind-panel">
      <p class="subtle" id="bindHint"></p>
      <form class="row hidden" id="bindForm">
        <label class="field grow"><span>一次性企业绑定码</span><input id="bindCode" type="password" autocomplete="off" required></label>
        <button id="bindBtn" type="submit" disabled>绑定企业</button>
      </form>
      <div class="message" id="firstRunNotice" role="status"></div>
    </div>
  </div>
  <div class="self-test-result row">
    <strong class="message" id="selfTestSummary" role="status">尚未运行连接测试。</strong>
    <span class="subtle" id="selfTestTime"></span>
  </div>
  <div class="stage-grid self-test-stages" style="margin-top:14px">
    <div class="stage" id="selfLocalStage"><b>本机示例 MCP</b><small id="selfLocalState">尚未检测</small><p id="selfLocalDetail">检查本机测试服务运行状态和固定示例数据。</p></div>
    <div class="stage" id="selfBindingStage"><b>企业绑定</b><small id="selfBindingState">尚未检测</small><p id="selfBindingDetail">未绑定时只执行本机阶段。</p></div>
    <div class="stage" id="selfTunnelStage"><b>公网隧道</b><small id="selfTunnelState">尚未检测</small><p id="selfTunnelDetail">绑定后使用独立示例来源建立路由。</p></div>
    <div class="stage" id="selfEdgeStage"><b>开物公网验证</b><small id="selfEdgeState">尚未检测</small><p id="selfEdgeDetail">检查开物能否通过公网取回本机示例结果。</p></div>
  </div>
  <div class="row subtle self-test-result hidden" id="selfTestTraceWrap">
    <span>诊断编号 <code id="selfTestTrace"></code></span><span id="selfTestTraceNotice" role="status"></span>
  </div>
  <div class="self-test-answer hidden" id="selfTestAnswer">
    <b>技术支持确认接入后，在开物试问示例</b>
    <p class="subtle">请开物技术支持确认示例服务已授权给哪个助手，并领取试用账号。收到确认后，<a href="https://flintw.com/project/kaiwu/" target="_blank" rel="noopener noreferrer">登录开物</a>，选择该助手，发送下面的问题。</p>
    <div id="selfTestQuestion">请用内置测试服务查询订单数和 KAIWU-DEMO-001 的库存。</div>
    <p class="subtle" id="selfTestExpected">预期结果为示例订单数 12、KAIWU-DEMO-001 示例库存 128。示例数据与华微业务数据无关。</p>
    <button id="copyTestQuestionBtn" type="button">复制测试问题</button>
  </div>
</section>

<section class="card" id="businessPanel">
  <h2 tabindex="-1" id="businessHeading">配置业务 MCP 服务</h2>
  <p class="section-hint">请向业务服务维护人员确认服务名称、完整 MCP HTTP 地址、允许使用的查询工具，以及服务要求的访问令牌或华微动态签名参数。访问令牌和签名密钥只在本机填写。</p>
  <p class="section-hint">首次安装请选择“新增 MCP 服务”。升级后可选中暂时不用的服务并点击“停用此来源”；停用不会删除旧配置、访问令牌或签名密钥。</p>
  <div class="form-grid">
    <label class="field span-2"><span>当前配置对象</span><select id="sourceSelect"><option value="">＋ 新增 MCP 服务</option></select></label>
    <div class="row span-2"><button id="toggleSourceBtn" type="button" disabled>停用此来源</button><span class="subtle" id="sourceEnabledHint">新增服务保存后默认启用；已有来源可选择后停用。</span></div>
    <label class="field"><span>服务名称</span><input id="displayName" maxlength="80" placeholder="例如：库存查询" autocomplete="off"></label>
    <label class="field"><span>MCP HTTP 地址</span><input id="upstreamURL" placeholder="http://127.0.0.1:28765/mcp" autocomplete="off"></label>
    <label class="field span-2"><span>访问令牌 <em>可选；已有令牌留空保持原值</em></span><input id="bearerToken" type="password" placeholder="填写您当前服务器 MCP 要求的 Bearer Token；页面不会再次显示已保存的令牌" autocomplete="new-password"></label>
  </div>

  <div class="auth-box">
    <div class="auth-head">
      <label class="check"><input id="dynamicAuth" type="checkbox" checked>使用华微动态签名</label>
      <span class="subtle">动态签名只在调用工具时附加；密钥只保存在当前服务器。</span>
    </div>
    <div class="form-grid" id="authFields" style="margin-top:12px">
      <label class="field"><span>应用标识（appid）</span><input id="authAppID" value="MCP" autocomplete="off"></label>
      <label class="field"><span>渠道编号（channel）</span><input id="authChannel" placeholder="填写您当前服务器的渠道号" autocomplete="off"></label>
      <label class="field"><span>读取身份（userid）</span><input id="authUserID" placeholder="填写您当前服务器的读取身份" autocomplete="off"></label>
      <label class="field"><span>签名密钥 <em>留空保持原值</em></span><input id="authHMACKey" type="password" placeholder="填写您当前服务器的签名密钥" autocomplete="new-password"></label>
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
    <button id="saveConnectBtn" type="button">保存并连接</button>
    <div class="message" id="formMessage" role="status">保存后立即检查业务服务。企业已绑定时继续建立公网隧道。</div>
  </div>
  <div class="stage-grid">
    <div class="stage" id="localStage"><b>业务服务本机检查</b><small id="localState">尚未检测</small><p id="localDetail">保存配置后会检查地址、协议和工具目录。</p></div>
    <div class="stage" id="tunnelStage"><b>连接器公网隧道</b><small id="tunnelState">尚未连接</small><p id="tunnelDetail">需要完成企业绑定后建立。</p></div>
    <div class="stage" id="platformStage"><b>开物业务接入</b><small id="platformState">等待管理员配置</small><p id="platformDetail">联系开物技术支持确认业务来源和助手授权。</p></div>
  </div>
  <p class="subtle">业务服务连接后，请开物技术支持确认业务来源和助手授权。收到确认后，使用已分配的账号<a href="https://flintw.com/project/kaiwu/" target="_blank" rel="noopener noreferrer">登录开物</a>，选择指定助手，试问一条已知正确结果的业务问题，并与原业务系统核对。</p>
</section>

<section class="card" id="logsPanel">
  <h2 tabindex="-1" id="logsHeading">运行日志</h2>
  <p class="section-hint">为便于开物技术支持排查连接问题，连接状态、操作结果和错误代码会自动上报至开物服务，不包含访问密钥、查询参数或业务数据。</p>
  <div class="log-toolbar">
    <div class="log-tabs" role="tablist">
      <button class="log-tab active" id="connectorLogTab" type="button">连接器日志</button>
      <button class="log-tab" id="platformLogTab" type="button">开物调用日志</button>
    </div>
    <select id="logLevel" aria-label="日志级别">
      <option value="">全部级别</option>
      <option value="info">信息</option>
      <option value="warn">警告</option>
      <option value="error">错误</option>
    </select>
    <label class="check"><input id="logAutoRefresh" type="checkbox" checked>自动刷新</label>
    <div class="log-actions">
      <button id="logRefreshBtn" type="button">刷新</button>
      <button id="logDownloadBtn" type="button">下载脱敏日志</button>
      <button id="diagPauseBtn" type="button">暂停诊断上报</button>
    </div>
  </div>
  <div class="log-summary">
    <div class="kv">最近同步<b id="diagLastSync">尚未同步</b></div>
    <div class="kv">待上报<b id="diagPending">0 条</b></div>
    <div class="kv">已丢弃<b id="diagDropped">0 条</b></div>
    <div class="kv">当前服务器日志存储<b id="diagStorage">正常</b></div>
  </div>
  <div class="log-notice" id="logNotice"></div>
  <div class="log-scroll" id="logScroll">
    <table class="log-table">
      <thead><tr><th>时间</th><th>级别</th><th>动作</th><th>结果</th><th>耗时</th><th>错误与关联编号</th></tr></thead>
      <tbody id="logRows"><tr><td class="log-empty" colspan="6">正在读取日志…</td></tr></tbody>
    </table>
  </div>
</section>

<details class="card" id="advancedPanel">
  <summary>高级选项与诊断详情</summary>
  <div class="advanced-body">
    <div class="advanced-section">
      <h3>原始 MCP JSON 导入（高级）</h3>
      <p>粘贴您已有的 MCP 配置，支持 mcpServers 格式或单个 HTTP MCP 对象。连接所需的访问凭据会加密保存在当前服务器，不会写入导出的配置或诊断日志。</p>
      <p class="warning">此操作会整体替换当前来源列表，不应与上面的普通表单同时使用。</p>
      <textarea id="mcpConfig" placeholder='{"mcpServers":{"库存查询":{"type":"http","url":"http://127.0.0.1:28765/mcp","headers":{"Authorization":"Bearer ..."}}}}'></textarea>
      <div class="row" style="margin-top:10px"><button id="importBtn" type="button">导入并替换全部来源</button></div>
      <ul class="issues" id="importIssues"></ul>
    </div>

    <div class="advanced-section hidden" id="selfTestCloudWrap">
      <h3>公网自检分层明细</h3>
      <ul class="self-test-list" id="selfTestCloudStages"></ul>
    </div>

    <div class="advanced-section">
      <h3>本地与隧道维护</h3>
      <p>诊断原始结果只用于排障。暂停或断开不会删除当前服务器的来源配置。</p>
      <div class="row"><button id="diagnoseBtn" type="button">运行详细诊断</button><button id="pauseBtn" type="button">暂停</button><button id="disconnectBtn" type="button">断开并回到待配置</button><button id="demoToggleBtn" type="button" disabled>停用内置测试服务</button></div>
      <pre id="diagnoseOut" class="hidden" style="margin-top:12px"></pre>
    </div>

    <div class="advanced-section">
      <h3>Edge 地址预览</h3>
      <div class="warning">这里只预览当前服务器生成的 Edge 路由地址，<strong>不包含 Authorization，也不是可以直接导入开物平台的完整 MCP JSON</strong>。企业接入配置必须由开物管理员签发并绑定来源。</div>
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
      <h3>撤销当前服务器连接器</h3>
      <div class="warning danger-note">撤销会清除当前服务器的企业身份、访问令牌和动态签名配置，必须由开物管理员重新签发后才能再次接入。</div>
      <button class="danger" id="revokeBtn" type="button">撤销当前服务器连接器</button>
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
let statusUnavailable = false;
let statusRequest = 0;
let firstRunOperation = "";
let selfTestResult = null;
let selfTestError = "";
let logTab = "connector";
let logSignature = "";

function headers(){ return {"Content-Type":"application/json","X-Connector-Token":TOKEN}; }
async function api(path, body){
  const options = body === undefined
    ? {headers:{"X-Connector-Token":TOKEN}}
    : {method:"POST",headers:headers(),body:JSON.stringify(body)};
  const response = await fetch(path, options);
  const payload = await response.json().catch(()=>({ok:false,error:{message:"当前服务器服务返回了无法解析的响应"}}));
  if(!payload.ok){ throw new Error((payload.error && payload.error.message) || "操作失败"); }
  return payload.data;
}

function setMessage(text, kind){
  const node = $("formMessage");
  node.textContent = text;
  node.className = "message" + (kind ? " " + kind : "");
}
function setFirstRunNotice(text, kind){
  const node = $("firstRunNotice");
  node.textContent = text;
  node.className = "message" + (kind ? " " + kind : "");
}
function setStage(prefix, state, detail, kind){
  const box = $(prefix + "Stage");
  box.className = "stage" + (kind ? " " + kind : "");
  $(prefix + "State").textContent = state;
  $(prefix + "Detail").textContent = detail || "";
}
function selfTestKind(status){
  if(status === "passed"){ return "ready"; }
  if(status === "failed"){ return "error"; }
  if(status === "skipped"){ return "warn"; }
  return "";
}
function selfTestLabel(status){
  if(status === "passed"){ return "通过"; }
  if(status === "failed"){ return "失败"; }
  if(status === "skipped"){ return "未执行"; }
  return "等待";
}
function setSelfTestStage(prefix, stage, fallback){
  if(stage){
    setStage(prefix, selfTestLabel(stage.status), stage.message, selfTestKind(stage.status));
  }else{
    setStage(prefix, "尚未检测", fallback, "");
  }
}
function renderBuiltinDemo(demo){
  const pill = $("builtinDemoPill");
  const toggle = $("demoToggleBtn");
  toggle.disabled = busy || !demo;
  if(!demo){
    pill.textContent = statusUnavailable ? "状态不可用" : "读取中…";
    pill.className = "pill";
  }else if(!demo.enabled){
    pill.textContent = "已关闭";
    pill.className = "pill warn";
  }else if(demo.error_code || !demo.running){
    pill.textContent = demo.error_code === "BUILTIN_SOURCE_CONFLICT" ? "来源冲突" : "启动异常";
    pill.className = "pill err";
  }else{
    pill.textContent = "运行中";
    pill.className = "pill ok";
  }
  toggle.textContent = demo && !demo.enabled ? "启用内置测试服务" : "停用内置测试服务";
}
function selfTestTraceID(result){
  const trace = result && result.trace_id;
  return typeof trace === "string" && /^kwtr_[0-9a-f]{24,64}$/.test(trace) ? trace : "";
}
function selfTestOutcome(result, stages){
  if(!result){ return "pending"; }
  if(stages.local_mcp?.status !== "passed"){ return "local-failed"; }
  if(!stages.enterprise_binding || stages.enterprise_binding.status === "skipped"){ return "local-passed"; }
  if(stages.tunnel?.status === "failed"){ return "tunnel-failed"; }
  if(result.ok === true && stages.enterprise_binding.status === "passed" &&
     stages.tunnel?.status === "passed" && stages.edge_connect?.status === "passed" &&
     Object.values(stages).every(stage=>stage.status === "passed")){
    return "public-passed";
  }
  return "public-failed";
}
function renderSelfTest(status, stages, outcome){
  const localPassed = stages.local_mcp?.status === "passed";
  const revoked = status?.state === "REVOKED";
  const bound = status?.bound && !revoked;
  const publicPassed = bound && outcome === "public-passed";
  setSelfTestStage("selfLocal", stages.local_mcp, "运行连接测试，检查本机示例服务。");
  if(revoked){
    setStage("selfBinding", "已撤销", "请开物技术支持确认服务器身份并重新签发接入码。", "error");
  }else if(status){
    setStage("selfBinding", bound ? "已绑定" : "待绑定", bound ? (status.tenant_name || "已加载企业身份。") : "需要一次性企业绑定码。", bound ? "ready" : "warn");
  }else{
    setStage("selfBinding", "待确认", "读取连接器状态后确认企业绑定。", "");
  }
  setSelfTestStage("selfTunnel", stages.tunnel, "企业绑定后运行连接测试。");
  const cloudStages = Object.values(stages).filter(stage=>!["local_mcp", "enterprise_binding", "tunnel"].includes(stage.id));
  if(outcome === "public-failed"){
    const failure = cloudStages.find(stage=>stage.status === "failed");
    setStage("selfEdge", "未通过", failure?.message || "未能验证本次公网返回的示例结果。", "error");
  }else{
    setSelfTestStage("selfEdge", stages.edge_connect, "公网隧道建立后检查。");
  }
  if(localPassed && outcome === "local-passed" && !revoked){
    const label = bound ? "待验证" : "待绑定";
    const detail = bound ? "重新运行连接测试以验证公网。" : "完成企业绑定后再验证公网。";
    setStage("selfTunnel", label, detail, "warn");
    setStage("selfEdge", label, detail, "warn");
  }
  const list = $("selfTestCloudStages");
  list.replaceChildren();
  cloudStages.forEach(item=>{
    const li = document.createElement("li");
    li.textContent = selfTestLabel(item.status) + " · " + item.id + " · " + item.message;
    list.appendChild(li);
  });
  $("selfTestCloudWrap").classList.toggle("hidden", cloudStages.length === 0);
  const trace = selfTestTraceID(selfTestResult);
  if($("selfTestTrace").textContent !== trace){ $("selfTestTraceNotice").textContent = ""; }
  $("selfTestTrace").textContent = trace;
  $("selfTestTraceWrap").classList.toggle("hidden", !trace);
  $("copySelfTestTraceBtn").classList.toggle("hidden", !trace);
  $("selfTestAnswer").classList.toggle("hidden", !publicPassed);
  const testTime = formatTime(selfTestResult?.generated_at);
  $("selfTestTime").textContent = testTime === "—" ? "" : testTime;

  let summary = "尚未运行连接测试。";
  let kind = "";
  if(firstRunOperation === "test"){
    summary = "正在运行连接测试…";
  }else if(selfTestError){
    summary = "无法完成连接测试：" + selfTestError;
    kind = "err";
  }else if(outcome === "local-failed"){
    summary = "本机示例检查未通过。";
    kind = "err";
  }else if(outcome === "tunnel-failed" || outcome === "public-failed"){
    summary = outcome === "tunnel-failed" ? "本机示例已通过，公网隧道未通过。" : "本机示例已通过，公网验证未通过。";
    kind = "err";
  }else if(publicPassed){
    summary = "本机示例和公网验证已通过，助手授权待确认。";
    kind = "ok";
  }else if(localPassed){
    summary = "本机示例已通过，" + (!status || revoked ? "企业状态待确认。" : (bound ? "公网待验证。" : "公网待绑定企业。"));
    kind = "warn";
  }
  if($("selfTestSummary").textContent !== summary){ $("selfTestSummary").textContent = summary; }
  $("selfTestSummary").className = "message" + (kind ? " " + kind : "");
}
function firstRunStep(status, demo, stages, outcome){
  if(statusUnavailable){
    return ["连接器状态暂时不可用", "请重试。若仍无法读取，请联系开物技术支持检查本机连接器。", "refresh", "重新读取状态"];
  }
  if(!status){ return ["先检查本机示例", "正在读取连接器状态…", "test", "运行连接测试"]; }
  if(firstRunOperation === "test"){
    return ["正在检查连接", status.bound ? "正在检查本机示例和公网连接。" : "正在检查本机示例。绑定企业后可继续验证公网。", "test", "正在测试…"];
  }
  if(firstRunOperation === "bind"){
    return ["正在绑定企业", "请等待绑定结果。", "bind"];
  }
  if(!demo?.enabled){
    return ["先启用内置测试服务", "启用后即可检查本机示例。", "enable", "启用内置测试服务"];
  }
  if(demo.error_code){
    const detail = demo.error_code === "BUILTIN_SOURCE_CONFLICT"
      ? "内置示例与现有来源冲突。请把页面提示和发生时间提供给开物技术支持。"
      : "内置示例启动异常。请把页面提示和发生时间提供给开物技术支持。";
    return ["请开物技术支持检查示例服务", detail, "logs", "查看运行日志"];
  }
  const hasTrace = !!selfTestTraceID(selfTestResult);
  const diagnosticHint = hasTrace
    ? "联系开物技术支持时，请提供诊断编号、失败项和发生时间。"
    : "联系开物技术支持时，请提供失败项和发生时间，或下载对应的脱敏日志。";
  if(status.state === "REVOKED" && status.bound){
    return ["请技术支持检查企业绑定", "当前服务器身份已撤销。请联系开物技术支持处理原绑定并签发新的接入码。", "logs", "查看运行日志"];
  }
  if(selfTestError || outcome === "local-failed"){
    return ["重新检查本机示例", "确认连接器在本机运行后再试。若仍未通过，" + diagnosticHint, "test", "重新运行连接测试"];
  }
  if(outcome === "pending"){
    return ["先检查本机示例", "点击运行连接测试。" + (status.bound ? "已绑定企业，本次也会验证公网。" : "可先检查本机，之后再绑定企业。"), "test", "运行连接测试"];
  }
  if(!status.bound){
    return status.cloud_configured
      ? [status.state === "REVOKED" ? "重新绑定企业" : "绑定企业以验证公网", "本机示例已通过。请在下方填写一次性企业绑定码。", "bind"]
      : ["请技术支持提供接入安装包", "本机示例已通过。当前安装包未配置企业绑定，请联系开物技术支持获取接入安装包。"];
  }
  if(outcome === "local-passed"){
    return ["验证公网连接", "企业已绑定。请重新运行连接测试，验证开物能否经公网取回本机示例结果。", "test", "验证公网连接"];
  }
  if(outcome === "tunnel-failed"){
    if(stages.tunnel.message?.includes("测试通道尚未就绪，请在方便时重新连接")){
      return ["请维护人员安排重连", "请让维护人员在合适的时间用业务配置中的“保存并连接”重新连接，再运行连接测试。重连会短暂中断正在使用的业务连接。", "logs", "查看运行日志"];
    }
    if(stages.tunnel.message?.includes("云端尚未批准")){
      return ["请技术支持确认示例来源授权", "示例路由尚未获准。请开物技术支持确认来源授权后重新运行连接测试。", "logs", "查看运行日志"];
    }
    return ["排查公网隧道后重试", "确认本机可以访问公网，再运行连接测试。若仍未通过，" + diagnosticHint, "test", "重新运行连接测试"];
  }
  if(outcome === "public-failed"){
    return ["请技术支持排查公网验证", diagnosticHint, hasTrace ? "copy" : "logs", "查看运行日志"];
  }
  if((status.sources || []).some(source=>!source.disabled)){
    return ["查看业务接入状态", "已有启用的业务配置。请查看业务连接结果，收到技术支持的助手授权确认后，再登录开物试问。", "business", "查看业务配置"];
  }
  return ["填写业务服务配置", "请准备业务服务的地址、认证信息和允许查询的工具。示例试问需先由技术支持确认助手授权。", "business", "填写业务配置"];
}
function renderFirstRun(){
  const status = statusUnavailable ? null : currentStatus;
  const demo = status?.builtin_demo;
  const stages = Object.fromEntries((selfTestResult?.stages || []).map(stage=>[stage.id, stage]));
  const outcome = selfTestOutcome(selfTestResult, stages);
  renderBuiltinDemo(demo);
  renderSelfTest(status, stages, outcome);

  const [title, detail, action, label] = firstRunStep(status, demo, stages, outcome);
  $("nextStepTitle").textContent = title;
  $("nextStepDetail").textContent = detail;
  const test = $("selfTestBtn");
  const canTest = !!demo?.enabled && !demo.error_code && !(status?.bound && status.state === "REVOKED");
  test.disabled = busy || !canTest;
  test.classList.toggle("hidden", !canTest && !!action && action !== "test");
  test.classList.toggle("primary", action === "test");
  test.textContent = action === "test" ? label : "重新运行连接测试";
  $("copySelfTestTraceBtn").classList.toggle("primary", action === "copy");

  const next = $("nextStepBtn");
  const actions = {
    refresh:()=>refresh(!currentStatus),
    enable:()=>$("demoToggleBtn").click(),
    logs:()=>$("logsHeading").focus(),
    business:()=>$("businessHeading").focus()
  };
  next.classList.toggle("hidden", !actions[action]);
  next.textContent = actions[action] ? label : "";
  next.onclick = actions[action] || null;
  next.disabled = busy;

  const canBind = !!status && !status.bound && status.cloud_configured;
  $("bindForm").classList.toggle("hidden", !canBind);
  $("bindCode").disabled = busy || !canBind;
  $("bindBtn").disabled = busy || !canBind;
  $("bindBtn").classList.toggle("primary", action === "bind");
  $("bindHint").textContent = canBind
    ? "接入码由开物技术支持签发，只能使用一次。缺少、过期或已使用时请联系技术支持。"
    : (!status || status.bound ? "" : "当前安装包未配置企业绑定，请联系开物技术支持。");
}
function stateClass(value){
  if(value === "READY") return "ok";
  if(value === "DEGRADED_UPSTREAM" || value === "DEGRADED_RELAY" || value === "REVOKED") return "err";
  if(value === "PAUSED" || value === "DRAINING" || value === "CONFIGURING" || value === "UNPAIRED") return "warn";
  return "";
}
function sourceByID(id){ return ((currentStatus && currentStatus.sources) || []).find(item=>item.source_id === id); }
function authByID(id){ return ((currentStatus && currentStatus.auth_profiles) || []).find(item=>item.source_id === id); }

const levelLabels = {info:"信息", warn:"警告", error:"错误"};
const resultLabels = {info:"成功", warn:"注意", error:"失败"};
function formatTime(value){
  if(!value){ return "—"; }
  const date = new Date(value);
  if(Number.isNaN(date.getTime())){ return "—"; }
  return new Intl.DateTimeFormat("zh-CN", {
    year:"numeric", month:"2-digit", day:"2-digit",
    hour:"2-digit", minute:"2-digit", second:"2-digit",
    hour12:false, timeZoneName:"short"
  }).format(date);
}
function formatDuration(value){
  if(value === undefined || value === null || value === ""){ return "—"; }
  if(value < 1000){ return value + " 毫秒"; }
  return (value / 1000).toFixed(value < 10000 ? 1 : 0) + " 秒";
}
function storageText(code){
  if(!code){ return "正常"; }
  if(code === "LOCAL_STORAGE_FAILED"){ return "暂时无法写入，请联系开物技术支持"; }
  return "异常，请联系开物技术支持";
}
function renderDiagnosticSummary(stats){
  const value = stats || {};
  $("diagLastSync").textContent = value.last_success_at ? formatTime(value.last_success_at) : "尚未同步";
  $("diagPending").textContent = (value.pending_events || 0) + " 条";
  $("diagDropped").textContent = (value.dropped_events || 0) + " 条";
  $("diagStorage").textContent = storageText(value.storage_error_code);
  const button = $("diagPauseBtn");
  button.textContent = value.paused ? "恢复诊断上报" : "暂停诊断上报";
  button.className = value.paused ? "primary" : "";
}
function platformErrorText(code){
  if(code === "NOT_BOUND"){ return "当前服务器尚未绑定企业"; }
  if(code === "UPLOAD_ENDPOINT_NOT_FOUND"){ return "当前云端尚未启用调用日志查询"; }
  if(code === "UPSTREAM_DNS_FAILED" || code === "UPSTREAM_UNREACHABLE"){ return "当前服务器无法连接开物服务"; }
  if(code === "UPSTREAM_TIMEOUT"){ return "开物服务响应超时"; }
  if(code === "UPLOAD_UNAUTHORIZED" || code === "UPLOAD_FORBIDDEN"){ return "当前设备身份没有查询权限"; }
  return "查询暂时未成功";
}
function setLogNotice(text, kind){
  const node = $("logNotice");
  node.textContent = text || "";
  node.className = "log-notice" + (kind ? " " + kind : "");
}
function emptyLogRows(text){
  const tbody = $("logRows");
  tbody.replaceChildren();
  const row = document.createElement("tr");
  const cell = document.createElement("td");
  cell.colSpan = 6;
  cell.className = "log-empty";
  cell.textContent = text;
  row.appendChild(cell);
  tbody.appendChild(row);
}
function logCell(text, className){
  const cell = document.createElement("td");
  if(className){ cell.className = className; }
  cell.textContent = text || "";
  return cell;
}
async function copyText(value){
  if(!value){ return false; }
  try{
    await navigator.clipboard.writeText(value);
    return true;
  }catch(_error){
    const area = document.createElement("textarea");
    area.value = value;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    try{
      area.select();
      return document.execCommand("copy");
    }catch(_fallbackError){
      return false;
    }finally{
      area.remove();
    }
  }
}
function renderLogRows(events){
  const tbody = $("logRows");
  const scroll = $("logScroll");
  const scrollTop = scroll ? scroll.scrollTop : 0;
  tbody.replaceChildren();
  if(!events.length){
    emptyLogRows(logTab === "connector" ? "当前没有符合条件的连接器日志。" : "当前没有符合条件的开物调用日志。");
    return;
  }
  events.forEach(event=>{
    const row = document.createElement("tr");
    row.appendChild(logCell(formatTime(event.occurred_at), "log-time"));
    const levelCell = document.createElement("td");
    const level = event.level || "info";
    const badge = document.createElement("span");
    badge.className = "log-level " + level;
    badge.textContent = levelLabels[level] || level;
    levelCell.appendChild(badge);
    row.appendChild(levelCell);

    const messageCell = document.createElement("td");
    messageCell.className = "log-message";
    const message = document.createElement("div");
    message.textContent = event.message || "操作失败，请提供事件编号";
    messageCell.appendChild(message);
    if(event.tool_name){
      const tool = document.createElement("div");
      tool.className = "log-meta";
      tool.textContent = "工具 " + event.tool_name;
      messageCell.appendChild(tool);
    }
    row.appendChild(messageCell);
    row.appendChild(logCell(resultLabels[level] || "—"));
    row.appendChild(logCell(formatDuration(event.duration_ms), "log-duration"));

    const referenceCell = document.createElement("td");
    const references = [];
    if(event.rpc_error_code){ references.push("错误码 " + event.rpc_error_code); }
    if(event.trace_id){ references.push("关联编号 " + event.trace_id); }
    if(!references.length && event.event_id){ references.push("事件编号 " + event.event_id); }
    const reference = document.createElement("div");
    reference.className = "log-meta";
    reference.textContent = references.join("；");
    referenceCell.appendChild(reference);
    const copyValue = event.trace_id || event.event_id || "";
    if(copyValue){
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "copy-button";
      copy.textContent = "复制编号";
      copy.addEventListener("click", ()=>copyText(copyValue));
      referenceCell.appendChild(copy);
    }
    row.appendChild(referenceCell);
    tbody.appendChild(row);
  });
  if(scroll){ scroll.scrollTop = scrollTop; }
}
async function loadLogs(force){
  const level = $("logLevel").value;
  const query = "?limit=200" + (level ? "&level=" + encodeURIComponent(level) : "");
  try{
    const data = logTab === "connector"
      ? await api("/api/logs" + query)
      : await api("/api/platform-logs" + query);
    renderDiagnosticSummary(data.stats);
    const events = data.events || [];
    if(logTab === "platform" && data.available === false){
      setLogNotice("开物调用日志暂时无法读取：" + platformErrorText(data.error_code) + "。连接器本地日志不受影响。", "warn");
    }else{
      setLogNotice("");
    }
    const signature = logTab + "|" + level + "|" + events.map(item=>item.event_id + ":" + item.level).join(",");
    if(!force && signature === logSignature){ return; }
    logSignature = signature;
    renderLogRows(events);
  }catch(error){
    setLogNotice("读取日志失败：" + error.message, "err");
  }
}
function switchLogTab(next){
  logTab = next;
  logSignature = "";
  const connector = $("connectorLogTab");
  const platform = $("platformLogTab");
  connector.className = "log-tab" + (next === "connector" ? " active" : "");
  platform.className = "log-tab" + (next === "platform" ? " active" : "");
  emptyLogRows("正在读取日志…");
  loadLogs(true);
}

function renderStageSummary(){
  if(!currentStatus){ return; }
  const sourceID = $("sourceSelect").value;
  const report = diagnosisBySource[sourceID];
  if(sourceByID(sourceID)?.disabled){
    setStage("local", "已停用", "此来源当前不参与连接。", "warn");
  }else if(report){
    if(report.ok){ setStage("local", "可访问", "MCP 初始化、认证和工具目录检查通过。", "ready"); }
    else{
      const failure = (report.checks || []).find(item=>item.status === "fail");
      const detail = failure ? ((failure.label || failure.name) + "：" + (failure.detail || "检查失败")) : "内网 MCP 检查未通过。";
      setStage("local", "不可访问", detail, "error");
    }
  }else if(currentStatus.state === "DEGRADED_UPSTREAM"){
    setStage("local", "暂不可用", currentStatus.last_error || "内网 MCP 检查未通过。", "error");
  }else if(sourceID || (currentStatus.sources || []).length){
    setStage("local", "尚未检测", "点击“保存并连接”后执行检查。", "");
  }else{
    setStage("local", "尚未配置", "请新增一个内部 MCP 服务。", "");
  }

  if(currentStatus.tunnel_connected){
    setStage("tunnel", "已连接", currentStatus.tunnel_detail || "官方 frpc 来源代理已运行。", "ready");
  }else if(currentStatus.state === "DEGRADED_RELAY"){
    setStage("tunnel", "连接失败", currentStatus.last_error || currentStatus.tunnel_detail || "公网中继连接失败。", "error");
  }else if(currentStatus.frpc_running){
    setStage("tunnel", "连接中", currentStatus.tunnel_detail || "frpc 进程已启动，但中继尚未就绪。", "warn");
  }else if(currentStatus.state === "PAUSED"){
    setStage("tunnel", "已暂停", "确认可以恢复连接后，点击“保存并连接”。", "warn");
  }else{
    setStage("tunnel", "尚未连接", currentStatus.bound ? "保存并连接后启动公网隧道。" : "需要先完成企业绑定。", "");
  }

  if(currentStatus.state === "REVOKED"){
    setStage("platform", "已撤销", "当前服务器身份已撤销，需要开物管理员重新签发。", "error");
  }else if(!currentStatus.bound){
    setStage("platform", "等待企业绑定", "请在页面上方完成企业绑定。", "warn");
  }else if(!(currentStatus.sources || []).some(source=>!source.disabled)){
    setStage("platform", "等待业务配置", "请添加并启用业务服务。", "");
  }else if(currentStatus.tunnel_connected){
    setStage("platform", "待管理员确认", "请开物技术支持确认业务来源和助手授权。", "warn");
  }else{
    setStage("platform", "等待公网隧道", "隧道建立后仍需开物管理员确认企业接入配置和来源路由。", "");
  }
}

function keepLocalSelfTest(){
  if(!selfTestResult){ return; }
  selfTestResult = {
    generated_at: selfTestResult.generated_at,
    stages: (selfTestResult.stages || []).filter(stage=>stage.id === "local_mcp")
  };
}
function renderStatus(status){
  const previous = currentStatus;
  const demo = status.builtin_demo;
  const localPassed = selfTestResult?.stages?.some(stage=>stage.id === "local_mcp" && stage.status === "passed");
  if(!demo?.enabled || (localPassed && (!demo.running || demo.error_code))){
    selfTestResult = null;
    selfTestError = "";
  }else if((previous?.bound && (!status.bound || previous.connector_id !== status.connector_id)) ||
           (status.state === "REVOKED" && previous?.state !== "REVOKED") ||
           (selfTestResult?.ok && (!status.frpc_running ||
             (previous?.frpc_running && previous.frpc_restarts !== status.frpc_restarts)))){
    // The live status reports the whole tunnel, not the exact demo route.
    // Discard the public result only when the identity or process changed.
    keepLocalSelfTest();
  }
  if(previous?.bound && !status.bound){ setFirstRunNotice("", ""); }
  if(statusUnavailable){
    ["saveConnectBtn","readToolsBtn","diagnoseBtn"].forEach(id=>$(id).disabled = false);
  }
  statusUnavailable = false;
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
  renderDiagnosticSummary(status.diagnostics);
  renderFirstRun();
  renderSourceOptions(status);
  syncSourceEnabledControl();
  renderStageSummary();
}

function renderSourceOptions(status){
  const select = $("sourceSelect");
  const previous = select.value;
  const demoID = status.builtin_demo && status.builtin_demo.source_id;
  select.innerHTML = "";
  const create = document.createElement("option");
  create.value = "";
  create.textContent = "＋ 新增 MCP 服务";
  select.appendChild(create);
  (status.sources || []).filter(source=>source.source_id !== demoID).forEach(source=>{
    const option = document.createElement("option");
    option.value = source.source_id;
    option.textContent = (source.display_name || source.source_id) + (source.disabled ? "（已停用，不参与连接）" : "");
    select.appendChild(option);
  });
  if(previous && previous !== demoID && (status.sources || []).some(source=>source.source_id === previous)){
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
  if(!tools.length){ clearToolList("初始化成功，但 MCP 没有返回工具。请检查当前服务器的 MCP 服务配置。"); return; }
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
function selectedSource(){
  return sourceByID($("sourceSelect").value);
}
function syncSourceEnabledControl(){
  const source = selectedSource();
  const button = $("toggleSourceBtn");
  const hint = $("sourceEnabledHint");
  button.disabled = !source || busy;
  if(!source){
    button.textContent = "停用此来源";
    hint.textContent = "新增服务保存后默认启用；已有来源可选择后停用。";
    return;
  }
  button.textContent = source.disabled ? "启用此来源" : "停用此来源";
  hint.textContent = source.disabled
    ? "当前已停用：不会参与诊断、来源登记或公网隧道启动。"
    : "当前已启用：会参与诊断、来源登记和公网隧道启动。";
}
function validateForm(payload, requireTools){
  if(!payload.display_name){ return "请填写服务名称。"; }
  if(!/^https?:\/\//i.test(payload.upstream_url)){ return "MCP HTTP 地址必须以 http:// 或 https:// 开头。"; }
  if(payload.dynamic_auth){
    if(!payload.app_id){ return "使用华微动态签名时请填写应用标识。"; }
    if(!payload.channel){ return "使用华微动态签名时请填写渠道编号。"; }
    if(!payload.user_id){ return "使用华微动态签名时请填写读取身份。"; }
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
    $("bearerToken").placeholder = "填写您当前服务器 MCP 要求的 Bearer Token";
    $("authHMACKey").placeholder = "填写您当前服务器的签名密钥";
    clearToolList("尚未读取工具。请先填写 MCP 地址和认证信息，再点击“读取工具列表”。");
    syncDynamicAuthFields();
    formDirty = false;
    renderStageSummary();
    syncSourceEnabledControl();
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
    : "该来源未保存访问令牌，可按 MCP 服务要求填写";
  $("authHMACKey").placeholder = hasDynamic ? "已保存签名密钥；留空保持原值" : "填写您当前服务器的签名密钥";
  const savedTools = (source.tool_allowlist || []).slice().sort();
  renderToolChoices(savedTools.map(name=>({name:name, title:"当前服务器已保存的允许工具；可重新读取目录确认"})), savedTools);
  syncDynamicAuthFields();
  formDirty = false;
  renderStageSummary();
  syncSourceEnabledControl();
}

async function refresh(initial){
  const request = ++statusRequest;
  try{
    const status = await api("/api/status");
    if(request !== statusRequest || busy){ return; }
    const firstStatus = !currentStatus;
    renderStatus(status);
    if(initial || firstStatus){ applySourceSelection(); }
  }catch(error){
    if(request !== statusRequest || busy){ return; }
    statusUnavailable = true;
    $("statePill").textContent = "当前服务器服务未就绪";
    $("statePill").className = "pill err";
    setMessage("无法读取当前服务器连接器状态：" + error.message, "err");
    ["saveConnectBtn","readToolsBtn","diagnoseBtn","toggleSourceBtn"].forEach(id=>$(id).disabled=true);
    renderFirstRun();
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

$("toggleSourceBtn").onclick = async ()=>{
  if(busy){ return; }
  const source = selectedSource();
  if(!source){ return; }
  const enabling = !!source.disabled;
  const button = $("toggleSourceBtn");
  button.disabled = true;
  busy = true;
  try{
    const statusBefore = currentStatus || {};
    if(["READY","CONNECTING","DEGRADED_UPSTREAM","DEGRADED_RELAY"].includes(statusBefore.state)){
      await api("/api/pause", {});
    }
    await api("/api/source/enabled", {source_id: source.source_id, enabled: enabling});
    setMessage(enabling
      ? "来源已启用。请确认地址、访问令牌、动态签名和允许工具后点击“保存并连接”。"
      : "来源已停用，不会参与诊断、来源登记或公网隧道启动；原配置和秘密仍保留。", "ok");
  }catch(error){
    setMessage("启用或停用来源失败：" + error.message, "err");
  }finally{
    busy = false;
    await refresh(false);
  }
};

$("readToolsBtn").onclick = async ()=>{
  if(busy){ return; }
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
  if(busy){ return; }
  const button = $("saveConnectBtn");
  const payload = payloadFromForm();
  const error = validateForm(payload);
  if(error){ setMessage(error, "err"); return; }
  button.disabled = true;
  busy = true;
  try{
    setMessage("正在保存当前服务器的 MCP 配置…", "warn");
    const statusBeforeSave = currentStatus || {};
    if(["READY","CONNECTING","DEGRADED_UPSTREAM","DEGRADED_RELAY","CONFIGURING"].includes(statusBeforeSave.state)){
      await api("/api/pause", {});
    }
    const saved = await api("/api/source/save", payload);
    delete diagnosisBySource[saved.source.source_id];
    renderStatus(saved.status);
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
      setStage("platform", "未到平台接入阶段", "请先修正当前服务器的 MCP 配置。", "");
      setMessage("内网 MCP 检查失败：" + detail + " 请核对地址、访问令牌或动态签名后重试。详细诊断在高级选项。", "err");
      $("diagnoseOut").textContent = JSON.stringify(reports, null, 2);
      $("diagnoseOut").classList.remove("hidden");
      return;
    }
    setStage("local", "可访问", "MCP 初始化、认证和工具目录检查通过。", "ready");
    setMessage("内网 MCP 已通过检查。正在核对公网与企业接入状态…", "ok");
    let latest = await api("/api/status");
    renderStatus(latest);
    if(!latest.cloud_configured){
      setStage("tunnel", "无法连接", "当前安装包未配置云端控制面。", "error");
      setStage("platform", "等待管理员配置", "请使用开物管理员提供的正式安装包。", "warn");
      setMessage("内网 MCP 已验证，但当前安装包未配置云端控制面，无法建立公网隧道。", "err");
      return;
    }
    if(!latest.bound){
      setStage("tunnel", "尚未建立", "当前服务器尚未绑定企业。", "warn");
      setMessage("业务服务本机检查已通过。请在页面上方绑定企业，再点击“保存并连接”。", "warn");
      return;
    }

    setStage("tunnel", "连接中", "正在启动官方 frpc 来源代理并等待中继状态。", "warn");
    setMessage("内网检查通过，正在建立公网隧道…", "warn");
    try{
      latest = await api("/api/connect", {});
    }catch(connectError){
      setStage("tunnel", "连接失败", connectError.message, "error");
      setStage("platform", "等待公网隧道与平台配置", "公网隧道未就绪，平台接入不能视为完成。", "warn");
      setMessage("公网隧道连接失败：" + connectError.message + " 请核对公网连接后重试。如提示来源授权或仍失败，请联系开物技术支持。", "err");
      return;
    }
    renderStatus(latest);
    if(latest.tunnel_connected){
      setMessage("业务服务本机检查已通过，连接器公网隧道已建立。请开物技术支持确认业务来源和助手授权，再登录开物试问。", "warn");
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

$("bindForm").onsubmit = async event=>{
  event.preventDefault();
  if(busy || $("bindBtn").disabled){ return; }
  const code = $("bindCode").value.trim();
  if(!code){
    setFirstRunNotice("请填写一次性企业绑定码。", "err");
    return;
  }
  busy = true;
  firstRunOperation = "bind";
  setFirstRunNotice("", "");
  renderFirstRun();
  try{
    const status = await api("/api/bind", {code:code});
    $("bindCode").value = "";
    renderStatus(status);
    setFirstRunNotice("企业绑定成功。请运行连接测试验证公网。", "ok");
  }catch(error){
    setFirstRunNotice("企业绑定失败：" + error.message + " 请核对接入码，过期或已使用时请联系开物技术支持。", "err");
  }finally{
    busy = false;
    firstRunOperation = "";
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
  try{ await api("/api/pause", {}); setMessage("当前服务器通道已暂停；来源配置和秘密均已保留。", "warn"); }
  catch(error){ setMessage("暂停失败：" + error.message, "err"); }
  finally{ await refresh(false); }
};
$("disconnectBtn").onclick = async ()=>{
  try{ await api("/api/disconnect", {}); setMessage("通道已断开并回到待配置状态；来源配置和秘密均已保留。", "warn"); }
  catch(error){ setMessage("断开失败：" + error.message, "err"); }
  finally{ await refresh(false); }
};
$("revokeBtn").onclick = async ()=>{
  if(!confirm("确认撤销当前服务器连接器？当前服务器的企业身份、访问令牌和动态签名配置将被清除。")){ return; }
  try{ await api("/api/revoke", {}); setMessage("当前服务器连接器已撤销，需要开物管理员重新签发后才能再次接入。", "warn"); }
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

$("demoToggleBtn").onclick = async ()=>{
  if(busy || statusUnavailable){ return; }
  const demo = (currentStatus && currentStatus.builtin_demo) || {};
  const enabled = !demo.enabled;
  busy = true;
  renderFirstRun();
  try{
    const status = await api("/api/builtin-demo/enabled", {enabled:enabled});
    renderStatus(status);
    setFirstRunNotice(enabled ? "内置测试服务已启用，可运行连接测试。" : "内置测试服务已关闭。", "ok");
  }catch(error){
    setFirstRunNotice("切换内置测试服务失败：" + error.message + " 请联系开物技术支持。", "err");
  }finally{
    busy = false;
    renderFirstRun();
    await refresh(false);
  }
};

$("selfTestBtn").onclick = async ()=>{
  if(busy || $("selfTestBtn").disabled){ return; }
  busy = true;
  firstRunOperation = "test";
  selfTestResult = null;
  selfTestError = "";
  setFirstRunNotice("", "");
  renderFirstRun();
  try{
    selfTestResult = await api("/api/self-test", {});
  }catch(error){
    selfTestError = error.message;
  }finally{
    busy = false;
    firstRunOperation = "";
    await refresh(false);
  }
};

$("copyTestQuestionBtn").onclick = ()=>copyText($("selfTestQuestion").textContent);
$("copySelfTestTraceBtn").onclick = async ()=>{
  const trace = selfTestTraceID(selfTestResult);
  if(!trace){ return; }
  const copied = await copyText(trace);
  if(trace === selfTestTraceID(selfTestResult)){
    $("selfTestTraceNotice").textContent = copied ? "已复制诊断编号。" : "未能自动复制，请选中编号手动复制。";
  }
};

$("connectorLogTab").onclick = ()=>switchLogTab("connector");
$("platformLogTab").onclick = ()=>switchLogTab("platform");
$("logLevel").onchange = ()=>{ logSignature = ""; loadLogs(true); };
$("logRefreshBtn").onclick = ()=>loadLogs(true);
$("diagPauseBtn").onclick = async ()=>{
  const paused = !!(currentStatus && currentStatus.diagnostics && currentStatus.diagnostics.paused);
  try{
    await api("/api/diagnostics/settings", {paused:!paused});
    setMessage(paused
      ? "诊断上报已恢复。连接和工具调用未受影响。"
      : "诊断上报已暂停。连接器本地日志和工具调用仍会继续，恢复后可按队列补报。", "warn");
  }catch(error){
    setMessage("切换诊断上报状态失败：" + error.message, "err");
  }finally{
    await refresh(false);
    await loadLogs(true);
  }
};
$("logDownloadBtn").onclick = async ()=>{
  const level = $("logLevel").value;
  const query = level ? "?level=" + encodeURIComponent(level) : "";
  try{
    const response = await fetch("/api/logs/download" + query, {headers:{"X-Connector-Token":TOKEN}});
    if(!response.ok){ throw new Error("当前服务器服务未能生成脱敏日志"); }
    const blob = await response.blob();
    const link = document.createElement("a");
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    link.href = URL.createObjectURL(blob);
    link.download = "kaiwu-diagnostics-" + stamp + ".json";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(()=>URL.revokeObjectURL(link.href), 1000);
  }catch(error){
    setLogNotice("下载脱敏日志失败：" + error.message, "err");
  }
};

refresh(true).then(()=>loadLogs(true));
setInterval(()=>{ if(!busy){ refresh(false); } }, 5000);
setInterval(()=>{
  if($("logAutoRefresh").checked && document.visibilityState === "visible"){ loadLogs(false); }
}, 3000);
</script>
</body>
</html>`
