/* AI Agent Platform — 纯静态 SPA（原生 JS，无构建步骤，DECISION-001）
   7 页面: Dashboard / Agent 构建器 / 对话 / RAG 管理 / 用户权限 / 记忆 / MCP-Skills
   前端 RBAC 路由守卫 + WS 流式对话 + 页脚 "AI Agent Platform · dev-team 构建" */
'use strict';

const TOKEN_KEY = 'agp_token';
let me = null;
let agentsCache = [];
let curAgent = null;
let curConv = null;

async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) }
  const t = localStorage.getItem(TOKEN_KEY)
  if (t) headers['Authorization'] = 'Bearer ' + t
  // 子目录部署适配: 页面从 /agent/ 下加载时, API 请求自动加 /agent 前缀
  let p = path
  const m = location.pathname.match(/^(.*\/)agent\//)
  if (m) p = m[1] + 'agent' + path
  const r = await fetch(p, { ...opts, headers })
  if (r.status === 401) { doLogout(); throw new Error('未认证'); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.message || ('HTTP ' + r.status));
  return j;
}
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const show = (el) => el.classList.remove('hidden');
const hide = (el) => el.classList.add('hidden');

const PAGE_PERMS = {
  dashboard: [], agents: [], chat: [],
  rag: ['rag:read'], users: ['role:manage', 'user:manage'],
  memory: ['memory:read'], ext: ['ext:manage', 'agent:read'],
  model: [],  // 模型节点页：登录即可查看（只读摘要）；保存/测试由页面内 system:admin 守卫
};
function can(page) {
  if (!me) return false;
  const perms = PAGE_PERMS[page] || [];
  return perms.length === 0 || perms.some(p => me.permissions.includes(p));
}

async function doLogin() {
  const u = $('#login-user').value.trim(), p = $('#login-pass').value;
  $('#login-err').textContent = '';
  if (!u || !p) { $('#login-err').textContent = '请输入用户名和密码'; return; }
  try {
    const j = await api('/api/auth/login', { method: 'POST', body: JSON.stringify({ username: u, password: p }) });
    localStorage.setItem(TOKEN_KEY, j.token);
    me = j.user;
    enterMain();
  } catch (e) { $('#login-err').textContent = e.message; }
}
function doLogout() {
  localStorage.removeItem(TOKEN_KEY);
  me = null;
  show($('#login-view')); hide($('#main-view'));
}
async function enterMain() {
  try { me = await api('/api/auth/me'); } catch { return; }
  hide($('#login-view')); show($('#main-view'));
  $('#whoami').innerHTML = esc(me.username) + ' · ' +
    me.roles.map(r => '<span class="tag acc">' + esc(r) + '</span>').join('');
  document.querySelectorAll('#nav a').forEach(a => {
    a.classList.toggle('forbidden', !can(a.dataset.page));
  });
  goto('dashboard');
}

const LOADERS = {
  dashboard: loadDashboard, agents: loadAgents, chat: loadChat,
  rag: loadRag, users: loadUsers,
  // memory/ext/model 由独立模块 memory.js / ext.js / config.js 提供（window 全局）
  memory: loadMemory, ext: loadExt, model: loadConfig,
};
function goto(page) {
  if (!can(page)) return;
  document.querySelectorAll('#nav a').forEach(a =>
    a.classList.toggle('active', a.dataset.page === page));
  document.querySelectorAll('.page').forEach(p => hide(p));
  show($('#page-' + page));
  (LOADERS[page] || (() => {}))().catch(e =>
    $('#page-' + page).insertAdjacentHTML('beforeend', '<div class="err">' + esc(e.message) + '</div>'));
}

async function loadDashboard() {
  const h = await api('/healthz');
  const cfg = await api('/api/system/config');
  const cnt = h.counters || {};
  $('#page-dashboard').innerHTML =
    '<h2>Dashboard</h2><div class="grid">' +
    '<div class="card"><div class="k">服务</div><div class="v ok">' + esc(h.status) + '</div>' +
    '<div class="k">端口 ' + h.port + ' · 记忆后端 ' + esc((h.memory_backend||{}).name||'') + '</div></div>' +
    '<div class="card"><div class="k">LLM 底座</div><div class="v">' + esc(cfg.llm.model) + '</div>' +
    '<div class="k mono">' + esc(cfg.llm.base_url) + '</div>' +
    '<div class="k">key 已配置: ' + (cfg.llm.key_set ? '✓' : '✗') + '</div></div>' +
    '<div class="card"><div class="k">Embedding</div>' +
    '<div class="v">' + (cfg.embedding.model ? esc(cfg.embedding.model) : 'local 哈希向量') + '</div>' +
    '<div class="k">维度 ' + cfg.embedding.dim + ' · 语义阈值 ' + cfg.semantic_cache_threshold + '</div></div>' +
    '<div class="card"><div class="k">可观测计数</div>' +
    '<div class="v">' + (cnt.llm_calls||0) + ' <span class="k">LLM 调用</span></div>' +
    '<div class="k">MD5 命中 ' + (cnt.cache_md5_hits||0) + ' · 语义命中 ' + (cnt.cache_semantic_hits||0) + '</div>' +
    '<div class="k">混合拆分 ' + (cnt.split_mixed||0) + ' · 复杂拆分 ' + (cnt.split_complex||0) + '</div></div>' +
    '</div><h3>角色权限矩阵（13 权限）</h3><div class="panel" id="matrix"></div>';
  try {
    const roles = await api('/api/roles');
    if (roles.roles && roles.roles.length) {
      $('#matrix').innerHTML = '<table><tr><th>角色</th><th>权限数</th><th>权限</th></tr>' +
        roles.roles.map(r => '<tr><td><span class="tag acc">' + esc(r.name) + '</span></td>' +
          '<td>' + r.permissions.length + '</td>' +
          '<td>' + r.permissions.map(p => '<span class="tag">' + esc(p) + '</span>').join('') + '</td></tr>').join('') +
        '</table>';
    }
  } catch {
    $('#matrix').innerHTML = '<div class="k">当前角色无 role:manage 权限，矩阵不可见（后端 403 同样生效）</div>';
  }
}
// ---- Agent 构建器 ----
async function loadAgents() {
  const j = await api('/api/agents');
  agentsCache = j.agents;
  window._skills = (await api('/api/ext/skills').catch(() => ({ skills: [] }))).skills;
  window._mcps = (await api('/api/ext/mcp').catch(() => ({ mcp_servers: [] }))).mcp_servers;
  window._kbs = (await api('/api/rag/knowledge').catch(() => ({ knowledge: [] }))).knowledge;
  $('#page-agents').innerHTML =
    '<h2>Agent 构建器</h2><div class="panel" id="agent-form"></div>' +
    '<h3>Agent 列表（' + agentsCache.length + '）</h3>' +
    '<table><tr><th>ID</th><th>名称</th><th>模型</th><th>参数</th><th>绑定</th><th>操作</th></tr>' +
    agentsCache.map(a => '<tr><td>' + a.id + '</td><td>' + esc(a.name) +
      '<div class="k">' + esc(a.description||'') + '</div></td>' +
      '<td class="mono">' + esc(a.model||'') + '</td>' +
      '<td class="k">t=' + a.temperature + ' · max=' + a.max_tokens + '</td>' +
      '<td>' + (a.bindings||[]).map(b => '<span class="tag">' + esc(b.type) + ':' + esc(b.ref_id) + '</span>').join('') + '</td>' +
      '<td><button class="small" onclick="editAgent(' + a.id + ')">编辑</button>' +
      '<button class="small" onclick="viewPrompt(' + a.id + ')">Prompt</button>' +
      '<button class="danger" onclick="delAgent(' + a.id + ')">删除</button></td></tr>').join('') +
    '</table>';
  renderAgentForm(null);
}
function hasB(a, type, ref) { return (a.bindings||[]).some(b => b.type===type && String(b.ref_id)===String(ref)); }
function renderAgentForm(a) {
  const skills = (window._skills || []), mcps = (window._mcps || []), kb = (window._kbs || []);
  const sel = (v, arr, type, id) => arr.map(s =>
    '<option value="' + type + ':' + s.id + '"' + (a && hasB(a, type, s.id) ? ' selected' : '') + '>' + esc(s.name) + '</option>').join('');
  $('#agent-form').innerHTML =
    '<div class="row">' +
    '<div><label>名称</label><input id="ag-name" value="' + (a?esc(a.name):'') + '"></div>' +
    '<div><label>描述</label><input id="ag-desc" value="' + (a?esc(a.description||''):
      '') + '"></div>' +
    '<div><label>模型</label><input id="ag-model" value="' + (a?esc(a.model||''):'') +
      '" placeholder="vllm-qwen3.8-27b"></div></div>' +
    '<label>system_prompt</label>' +
    '<textarea id="ag-sys" rows="4">' + (a?esc(a.system_prompt):'你是一名医疗助手。') + '</textarea>' +
    '<div class="row">' +
    '<div><label>temperature</label><input id="ag-t" type="number" step="0.1" value="' + (a?a.temperature:0.2) + '"></div>' +
    '<div><label>max_tokens</label><input id="ag-max" type="number" value="' + (a?a.max_tokens:1024) + '"></div>' +
    '<div><label>top_p</label><input id="ag-p" type="number" step="0.05" value="' + (a?a.top_p:0.9) + '"></div></div>' +
    '<label>绑定（多选）</label><div class="row">' +
    '<div><label>Skills</label><select id="ag-sk" multiple size="3">' + sel('skill', skills, 'skill', 0) + '</select></div>' +
    '<div><label>MCP</label><select id="ag-mc" multiple size="3">' + sel('mcp', mcps, 'mcp', 0) + '</select></div>' +
    '<div><label>RAG</label><select id="ag-rk" multiple size="3">' + sel('rag', kb, 'rag', 0) + '</select></div>' +
    '<div><label>Plugins</label><select id="ag-pl" multiple size="3">' +
    ['get_time','mcp_call','echo'].map(p => '<option value="plugin:' + p + '"' +
      (a && hasB(a, 'plugin', p) ? ' selected' : '') + '>' + p + '</option>').join('') + '</select></div></div>' +
    '<div style="margin-top:14px">' +
    '<button class="primary" style="width:auto;padding:10px 26px" onclick="saveAgent(' + (a?a.id:'null') + ')">' +
    (a?'保存':'创建 Agent') + '</button>' +
    (a?'<button class="small" onclick="loadAgents()">取消</button>':'') +
    ' <span id="ag-err" class="err"></span></div>';
}
async function editAgent(id) {
  renderAgentForm(agentsCache.find(x => x.id === id));
  $('#agent-form').scrollIntoView({ behavior: 'smooth' });
}
async function saveAgent(id) {
  const body = {
    name: $('#ag-name').value.trim(),
    description: $('#ag-desc').value.trim(),
    system_prompt: $('#ag-sys').value,
    model: $('#ag-model').value.trim() || null,
    temperature: parseFloat($('#ag-t').value) || 0.2,
    max_tokens: parseInt($('#ag-max').value) || 1024,
    top_p: parseFloat($('#ag-p').value) || 0.9,
    bindings: [...$('#ag-sk').selectedOptions, ...$('#ag-mc').selectedOptions,
              ...$('#ag-rk').selectedOptions, ...$('#ag-pl').selectedOptions]
      .map(o => { const [type, ref_id] = o.value.split(':'); return { type, ref_id }; }),
  };
  if (!body.name || !body.system_prompt) { $('#ag-err').textContent = '名称和 system_prompt 必填'; return; }
  try {
    if (id) await api('/api/agents/' + id, { method: 'PUT', body: JSON.stringify(body) });
    else await api('/api/agents', { method: 'POST', body: JSON.stringify(body) });
    $('#ag-err').textContent = '已保存（落库）';
    await loadAgents();
  } catch (e) { $('#ag-err').textContent = e.message; }
}
async function delAgent(id) {
  if (!confirm('删除该 Agent？')) return;
  try { await api('/api/agents/' + id, { method: 'DELETE' }); await loadAgents(); }
  catch (e) { alert(e.message); }
}
async function viewPrompt(id) {
  try {
    const j = await api('/api/agents/' + id + '/prompt?text=' +
      encodeURIComponent('患者 IgE 394 KU/L，FEV1 改善 240ml，哮喘控制情况如何？'));
    const sys = (j.messages[0]||{}).content || '';
    $('#agent-form').insertAdjacentHTML('beforeend',
      '<h3>组装 Prompt（prefix caching 布局）</h3><pre>' + esc(sys) + '</pre>' +
      '<div class="k">布局: [system_prompt + skills 指令 + 工具schema + RAG 上下文] 固定最左 → 记忆(左) → 历史 → 用户请求(最右)</div>');
  } catch (e) { alert(e.message); }
}
// ---- 对话（WS 流式）----
async function loadChat() {
  if (!agentsCache.length) await loadAgents();
  $('#page-chat').innerHTML =
    '<h2>对话</h2>' +
    '<div class="row" style="margin-bottom:12px">' +
    '<div style="flex:0 0 220px"><label>Agent</label><select id="chat-agent" class="ag-sel">' +
    agentsCache.map(a => '<option value="' + a.id + '">' + esc(a.name) + '</option>').join('') +
    '</select></div>' +
    '<div class="k" style="align-self:center">WS /ws/chat/{agent_id}/{conv_id} · 流式 token · 缓存命中直返</div></div>' +
    '<div class="chat-box"><div class="chat-msgs" id="chat-msgs"></div>' +
    '<div class="chat-input"><input id="chat-in" placeholder="输入问题（例：现在几点？/ 患者 IgE 多少？）">' +
    '<button onclick="sendChat()">发送</button></div></div>';
  curAgent = parseInt($('#chat-agent').value);
  curConv = 'c' + Date.now().toString(36);
  addMsg('assistant', '你好，我是医疗助手。可以问我：患者 IgE 结果？FEV1 改善量？现在几点？');
  $('#chat-agent').onchange = () => {
    curAgent = parseInt($('#chat-agent').value);
    curConv = 'c' + Date.now().toString(36);
    addMsg('assistant', '已切换到 Agent #' + curAgent + '（新会话）');
  };
}
function addMsg(role, content, badge) {
  const box = $('#chat-msgs');
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  div.innerHTML = '<div class="meta">' + (role === 'user' ? '我' : 'Agent') +
    (badge ? ' <span class="cache-badge">' + esc(badge) + '</span>' : '') + '</div>' +
    '<div class="bubble">' + esc(content) + '</div>';
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  return div;
}
function sendChat() {
  const inp = $('#chat-in');
  const text = inp.value.trim();
  if (!text || !curAgent) return;
  inp.value = '';
  addMsg('user', text);
  const msgDiv = addMsg('assistant', '…');
  const bubble = msgDiv.querySelector('.bubble');
  let acc = '';
  const t = localStorage.getItem(TOKEN_KEY);
  const m = location.pathname.match(/^(.*\/)agent\//)
  const url = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + (m ? m[1] + 'agent' : '') + '/ws/chat/' + curAgent + '/' + curConv + '?token=' +
    encodeURIComponent(t);
  const ws = new WebSocket(url);
  ws.onopen = () => ws.send(JSON.stringify({ message: text }));
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === 'token') {
      acc += m.content;
      bubble.textContent = acc;
      $('#chat-msgs').scrollTop = $('#chat-msgs').scrollHeight;
    } else if (m.type === 'cache_hit') {
      bubble.textContent = m.content;
      msgDiv.querySelector('.meta').innerHTML +=
        ' <span class="cache-badge">cache_hit:' + esc(m.cache_hit) + '</span>';
    } else if (m.type === 'done') {
      if (!acc && m.content) bubble.textContent = m.content;
      let meta = ' · LLM调用 ' + (m.llm_calls||0);
      if (m.cache_hit) meta = ' <span class="cache-badge">cache_hit:' + esc(m.cache_hit) + '</span>';
      msgDiv.querySelector('.meta').innerHTML += meta +
        ((m.route_events||[]).length ? ' · ' + esc(m.route_events.join(',')) : '');
      ws.close();
    } else if (m.type === 'error') {
      bubble.textContent = '[错误] ' + m.content;
      ws.close();
    }
  };
  ws.onerror = () => { bubble.textContent = '[WS 连接失败] 请检查服务与 token'; };
  ws.onclose = () => {
    if (bubble.textContent === '…') bubble.textContent = '[WS 已断开]';
  };
}

// ---- RAG 管理 ----
async function loadRag() {
  const j = await api('/api/rag/knowledge');
  const kbs = j.knowledge;
  $('#page-rag').innerHTML =
    '<h2>RAG 管理</h2>' +
    '<div class="panel"><div class="row">' +
    '<div><label>新知识库名称</label><input id="kb-name" placeholder="医疗知识库2"></div>' +
    '<div style="align-self:flex-end"><button class="primary" style="width:auto;padding:10px 22px" onclick="createKb()">创建</button></div>' +
    '</div><div id="kb-err" class="err"></div></div>' +
    kbs.map(k => '<div class="panel"><div class="row" style="align-items:center">' +
      '<div style="flex:1"><strong>' + esc(k.name) + '</strong> <span class="tag">#' + k.id +
      '</span> <span class="tag">分块 ' + k.chunks + '</span></div>' +
      '<div><button class="small" onclick="addDoc(' + k.id + ')">上传文档</button>' +
      '<button class="small" onclick="searchKb(' + k.id + ')">检索</button>' +
      '<button class="small" onclick="listChunks(' + k.id + ')">分块</button>' +
      '<button class="danger" onclick="delKb(' + k.id + ')">删除</button></div></div>' +
      '<div id="kb-' + k.id + '-out"></div></div>').join('') || '<div class="k">暂无知识库</div>';
}
async function createKb() {
  const name = $('#kb-name').value.trim();
  if (!name) return;
  try { await api('/api/rag/knowledge', { method: 'POST', body: JSON.stringify({ name }) }); await loadRag(); }
  catch (e) { $('#kb-err').textContent = e.message; }
}
async function delKb(id) {
  if (!confirm('删除知识库？')) return;
  try { await api('/api/rag/knowledge/' + id, { method: 'DELETE' }); await loadRag(); }
  catch (e) { alert(e.message); }
}
function addDoc(id) {
  const out = $('#kb-' + id + '-out');
  const taId = 'doc-ta-' + id;
  out.innerHTML = '<div class="panel"><label>文档文本（医疗场景示例）</label>' +
    '<textarea id="' + taId + '" rows="5">' +
    '患者李某某，女，48岁，反复咳嗽3年。2026-08-20 肺功能：FEV1 1.95 L，FEV1/FVC 61%。' +
    '总IgE 210.50 KU/L。诊断：支气管哮喘（中度）。予吸入性糖皮质激素联合长效β2受体激动剂。' +
    '2026-09-05 复测 FEV1 2.02 L，改善 70 ml。尘螨特异性IgE阳性。' +
    '</textarea><button class="small" onclick="doAddDoc(' + id + ')">分块入库（滑窗 重叠15%）</button>' +
    '<div id="doc-' + id + '-res"></div></div>';
}
async function doAddDoc(id) {
  const text = document.getElementById('doc-ta-' + id).value;
  try {
    const r = await api('/api/rag/knowledge/' + id + '/documents',
      { method: 'POST', body: JSON.stringify({ text }) });
    $('#doc-' + id + '-res').innerHTML = '<div class="ok">分块 ' + r.chunks +
      ' 个（窗口 ' + r.chunk_chars + ' 字符，重叠 ' + Math.round(r.overlap*100) + '%）</div>';
    await loadRag();
  } catch (e) { $('#doc-' + id + '-res').innerHTML = '<div class="err">' + esc(e.message) + '</div>'; }
}
async function listChunks(id) {
  try {
    const r = await api('/api/rag/knowledge/' + id + '/chunks');
    $('#kb-' + id + '-out').innerHTML = '<pre>' + esc(r.chunks.map(c =>
      '[' + c.seq + '](' + c.token_est + 'tok) ' + c.text).join('\n---\n')) + '</pre>';
  } catch (e) { alert(e.message); }
}
async function searchKb(id) {
  const q = prompt('检索 query（例：IgE 结果是多少？）', 'IgE 结果是多少？');
  if (!q) return;
  try {
    const r = await api('/api/rag/knowledge/' + id + '/search',
      { method: 'POST', body: JSON.stringify({ query: q, top_k: 3 }) });
    $('#kb-' + id + '-out').innerHTML = '<h3>检索 top-' + r.top_k +
      '</h3>' + r.results.map(x => '<div class="card" style="margin-bottom:8px">' +
      '<div class="k">seq ' + x.seq + ' · 余弦 ' + x.score + '</div>' +
      '<div style="font-size:13px">' + esc(x.text) + '</div></div>').join('');
  } catch (e) { alert(e.message); }
}
// ---- 用户权限 ----
async function loadUsers() {
  const [u, r] = await Promise.all([
    api('/api/users').catch(() => ({ users: [] })),
    api('/api/roles').catch(() => ({ roles: [] })),
  ]);
  $('#page-users').innerHTML =
    '<h2>用户权限</h2>' +
    '<div class="panel"><div class="row">' +
    '<div><label>用户名</label><input id="nu-name"></div>' +
    '<div><label>密码</label><input id="nu-pass" type="password"></div>' +
    '<div><label>角色</label><select id="nu-role">' +
    (r.roles||[]).map(x => '<option value="' + esc(x.name) + '">' + esc(x.name) + '</option>').join('') +
    '</select></div>' +
    '<div style="align-self:flex-end"><button class="primary" style="width:auto;padding:10px 22px" onclick="createUser()">创建用户</button></div>' +
    '</div><div id="nu-err" class="err"></div></div>' +
    '<h3>用户列表</h3><table><tr><th>ID</th><th>用户名</th><th>角色</th><th>创建时间</th><th>操作</th></tr>' +
    (u.users||[]).map(x => '<tr><td>' + x.id + '</td><td>' + esc(x.username) + '</td>' +
      '<td>' + x.roles.map(z => '<span class="tag acc">' + esc(z) + '</span>').join('') + '</td>' +
      '<td class="k">' + esc(x.created_at||'') + '</td>' +
      '<td><button class="danger" onclick="delUser(' + x.id + ')">删除</button></td></tr>').join('') +
    '</table>' +
    '<h3>角色权限矩阵</h3><table><tr><th>角色</th><th>权限</th><th>操作</th></tr>' +
    (r.roles||[]).map(x => '<tr><td><span class="tag acc">' + esc(x.name) + '</span></td>' +
      '<td>' + x.permissions.map(p => '<span class="tag">' + esc(p) + '</span>').join('') + '</td>' +
      '<td><button class="small" onclick="editRole(' + x.id + ', ' +
      JSON.stringify(x.permissions).replace(/"/g, '&quot;') + ')">调整</button></td></tr>').join('') +
    '</table>';
}
async function createUser() {
  try {
    await api('/api/users', { method: 'POST', body: JSON.stringify({
      username: $('#nu-name').value.trim(), password: $('#nu-pass').value,
      role: $('#nu-role').value }) });
    $('#nu-err').textContent = '已创建'; await loadUsers();
  } catch (e) { $('#nu-err').textContent = e.message; }
}
async function delUser(id) {
  if (!confirm('删除用户？')) return;
  try { await api('/api/users/' + id, { method: 'DELETE' }); await loadUsers(); }
  catch (e) { alert(e.message); }
}
async function editRole(id, perms) {
  const all = ['agent:read','agent:create','agent:update','agent:delete','rag:read','rag:write',
    'rag:delete','memory:read','memory:write','ext:manage','user:manage','role:manage','system:admin'];
  const cur = new Set(perms);
  const html = all.map(p => '<label style="display:inline-block;margin:4px 10px 4px 0">' +
    '<input type="checkbox" class="role-cb" value="' + p + '"' +
    (cur.has(p) ? ' checked' : '') + '> ' + p + '</label>').join('');
  const r = prompt('角色 ' + id + ' 权限（本框为演示，实际请走 API）：\n' +
    'POST 方式: PUT /api/roles/' + id + '/permissions {"permissions":[...]}\n' +
    '输入要授予的权限码（逗号分隔）：', perms.join(','));
  if (r === null) return;
  const list = r.split(',').map(s => s.trim()).filter(Boolean);
  try {
    await api('/api/roles/' + id + '/permissions',
      { method: 'PUT', body: JSON.stringify({ permissions: list }) });
    alert('已更新 ' + list.length + ' 项权限');
    await loadUsers();
  } catch (e) { alert(e.message); }
}

// ---- 记忆：由 memory.js 提供（loadMemory + 列表/3D/B+树/多跳 四视图）----
// 此处不再内联实现，避免与 memory.js 的 window 全局重复。

// ---- MCP-Skills-Plugins：由 ext.js 提供（loadExt + Skills/MCP 管理 + Plugins/长文本）----
// 此处不再内联实现，避免与 ext.js 的 window 全局重复（callPlugin/mcpTools/mcpCall/ltRun 等）。

// ---- 启动 ----
document.addEventListener('DOMContentLoaded', () => {
  $('#login-btn').onclick = doLogin;
  $('#login-pass').addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });
  $('#logout-btn').onclick = doLogout;
  document.querySelectorAll('#nav a').forEach(a => {
    a.onclick = (e) => { e.preventDefault(); goto(a.dataset.page); };
  });
  const t = localStorage.getItem(TOKEN_KEY);
  if (t) enterMain();
});
