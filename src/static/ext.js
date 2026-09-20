/* ext.js — MCP-Skills-Plugins 页面（TASK-023 / F1 前端）
 * 复用 app.js 的全局 api()/esc()/$ 与 RBAC 守卫。
 *
 * 职责：
 *   - Skills：列表(名称/描述/长度/更新时间, 可搜索) + 新建/编辑 + 删除(确认) + 内容查看/编辑 + 上传导入(.md/.txt/zip)
 *   - MCP   ：列表(传输标记 stdio/http + enabled 开关) + 新建/编辑(传输两态表单：
 *             stdio=command/args/env；http=url/headers) + 删除 + 保留 tools/list 与 call 测试
 *   - Plugins / 长文本4策略 / HITL：保持原有只读能力
 *   - TASK-058 迭代5：链路追踪（trace）面板（页面底部，仅 system:admin 有数据）：
 *       会话列表（时间/agent/模型/token 总量/工具数/状态，可按 agent 过滤）
 *       + 会话详情时间线（每个 span 一行：序号/类型图标/名称/时间/耗时/token/状态，
 *         展开看 input/output；rag_search 展开可见命中 chunk 列表）
 *       + 三态（加载态/空态/错误态，沿用现有 UI 规范）
 *
 * 权限：无 ext:manage 时写入入口禁用（canManage() 守卫）。
 *
 * TASK-054 迭代4：MCP 表单增加传输类型两态（stdio / HTTP，Streamable HTTP），
 *   http 态 = URL + headers 键值对；stdio 态 = 原有 command/args/env 零回归。
 *   列表行增加传输标记（stdio 灰 / http 蓝 badge）。tools/list 与 call 按钮
 *   对两种传输通用（后端按行 transport 分流，/api/ext/mcp/{id}/tools 无需前端区分）。
 */
(function () {
  'use strict';

  function canManage() { return !!(me && me.permissions && me.permissions.includes('ext:manage')); }

  let _skills = [];   // 当前 skills 缓存
  let _mcps = [];     // 当前 mcp 缓存
  let _skillQ = '';

  // 表单暂存（编辑中的 skill/mcp 对象）
  let _skillEditId = null;
  let _mcpEditId = null;

  // ---------- TASK-058: 链路追踪（trace）面板状态 ----------
  let _trcConvs = [];   // 当前会话聚合列表（GET /api/trace/conversations）
  let _trcSpans = [];   // 当前详情视图的 spans（按 seq 升序）
  let _trcAgentQ = null; // 按 agent 过滤（agent id 或 null=全部）

  // ---------- TASK-030: info tooltip（纯 CSS hover，原生实现无依赖） ----------
  function _tipBox(content) {
    return '<span class="tip-wrap" tabindex="0">' +
      '<span class="tip-icon" aria-label="说明">i</span>' +
      '<span class="tip-box" role="tooltip">' + esc(content) + '</span></span>';
  }

  // 文案对齐 src/mcp/mcp_client.py（stdio + Streamable HTTP 双传输）+
  // src/mcp/mcp_server_demo.py（容器内 /app/mcp/mcp_server_demo.py，工具
  // get_time/get_patient_demo；脚本不解析任何 CLI 参数，故不举 --verbose）。
  // TASK-054 迭代4：http 传输（MCP Streamable HTTP：JSON-RPC over POST 单端点，
  // 响应 JSON 或 SSE 流，Mcp-Session-Id 会话头）与 stdio 并存。
  const MCP_TIP = [
    'MCP Server 两种传输（可并存）：',
    '· stdio：本地可执行命令，平台 spawn 子进程用 LSP Content-Length 帧 JSON-RPC 通信。',
    '· http（Streamable HTTP）：远程/容器化 MCP server 的 HTTP 端点，JSON-RPC over POST 单端点（响应可为 JSON 或 SSE 流），自动处理 Mcp-Session-Id 会话头；无需本地可执行程序。',
    '',
    '配置步骤（stdio）：① 唯一名称 → ② 传输=stdio → ③ command（可执行程序，如 python3 / node / 绝对路径）→ ④ args（脚本路径+参数）→ ⑤ 可选 env 键值对（注入子进程环境变量）→ ⑥ 勾 enabled → 保存',
    '配置步骤（http）：① 唯一名称 → ② 传输=http → ③ URL（http(s) 端点地址，如 http://host:port/mcp）→ ④ 可选 headers 键值对（自定义请求头，如 Authorization: Bearer xxx）→ ⑤ 勾 enabled → 保存',
    '',
    '真实示例（stdio，容器内可跑，demo 为仓库内置）：',
    '· 名称 demo，command python3，args /app/mcp/mcp_server_demo.py —— 即当前已存在的内置 demo server，保存后点 tools/list 可见 get_time / get_patient_demo 两个工具',
    'http 示例（远程 server，真实端点地址）：',
    '· 名称 remote-weather，URL http://10.0.0.5:8900/mcp，headers Authorization=Bearer <token> —— 接入远程 MCP server（如容器内起的 Streamable HTTP server）',
    '',
    '提示：保存后点 tools/list 验证连接（stdio 会真实启动子进程；http 会真实 POST 到端点，超时 10s）；被 agent 引用的 server 删除会 409，需先解绑。',
  ].join('\n');

  // 文案逐字对齐 src/services/longtext.py 头部 docstring（1-11 行）
  const LT_TIP = [
    '长文本 4 策略（services/longtext.py）：处理超长临床文本。策略 1/2/3 真实调用 LLM，策略 4 为纯 pandas 确定性处理。',
    '1. Map-Reduce：2000-3000 token 滑窗分块（重叠 10-20%）→ 逐块 LLM 提取检验指标 → LLM 聚合去重/时间线排序/冲突标"需人工复核"；平台侧兜底：同名指标单位不一致强制标"需人工复核"。',
    '2. 增量图构建：流式逐段 → LLM 仅输出标准三元组 JSON → MERGE 写入 L2 图（幂等）→ 最终只查子图摘要，不回顾原文。',
    '3. critique-refine：pydantic QAItem 校验（question/answer/source_field、数值非负）→ 失败带具体错误重喂 LLM ≤5 次 → 达上限转 HITL 人工待办队列（不硬失败）。',
    '4. pandas 确定性预处理：散乱检验记录 CSV → 清洗/按 patient_id+date 聚合 → 结构化 JSON（不依赖 LLM）。',
  ].join('\n');

  async function loadExt() {
    const [pl, sk, mc, lt] = await Promise.all([
      api('/api/ext/plugins').catch(() => ({ plugins: [] })),
      api('/api/ext/skills').catch(() => ({ skills: [] })),
      api('/api/ext/mcp').catch(() => ({ mcp_servers: [] })),
      api('/api/longtext/hitl').catch(() => ({ hitl: [], count: 0 })),
    ]);
    _skills = sk.skills || [];
    _mcps = mc.mcp_servers || [];
    _skillQ = '';
    const M = canManage();
    $('#page-ext').innerHTML =
      '<h2>MCP-Skills-Plugins</h2>' +
      '<div class="k" style="margin-bottom:14px">' + (M
        ? '✓ 你有 <span class="tag acc">ext:manage</span> 权限，可增删改。'
        : '只读模式：你当前没有 <span class="tag warn">ext:manage</span> 权限，写入入口已禁用（后端同样 403）。') + '</div>' +
      '<div class="panel">' + renderSkillsPanel(M) + '</div>' +
      '<div class="panel"><h3>MCP Servers（stdio JSON-RPC / HTTP Streamable 双传输 · 内置 demo 真实进程）' + _tipBox(MCP_TIP) + '</h3>' +
        renderMcpPanel(M) + '</div>' +
      '<div class="panel"><h3>Plugins（内置可调用能力 · 只读）</h3>' +
        (pl.plugins || []).map(p =>
          '<div class="card" style="margin-bottom:8px"><strong>' + esc(p.name) +
          '</strong> <div class="k">' + esc(p.description) + '</div>' +
          '<button class="small" onclick="extCallPlugin(\'' + p.name + '\')">调用</button>' +
          '<pre id="pl-' + p.name + '"></pre></div>').join('') + '</div>' +
      renderLongtextPanel(lt) +
      // TASK-058: 链路追踪面板（独立 #trc-panel，局部刷新不重绘其他 panel）
      '<div id="trc-panel">' + renderTracePanel() + '</div>';
    // 非 admin 不需要拉数据（面板内已给出无权限提示）；admin 才加载会话列表（拉取失败 → 错误态）
    if (me && me.permissions && me.permissions.includes('system:admin')) {
      trcLoad();
    }
  }

  // ---------- TASK-058: 链路追踪（trace）面板 ----------
  // 数据来源：GET /api/trace/conversations?agent_id=&limit=&offset=（仅 system:admin）。
  // 权限守卫：非 admin 直接走 403 → 错误态（沿用现有 UI 规范，不渲染任何数据）。
  // 三态：
  //   加载态 → _trcState = 'loading'（"加载中…"）
  //   空态   → _trcState = 'empty'（"暂无 trace 数据"）
  //   错误态 → _trcState = 'error'（显示后端错误信息，如 403 无权限）
  //   正常态 → _trcState = 'ok'（渲染会话列表 + 可选详情时间线）
  let _trcState = 'idle';   // idle | loading | ok | empty | error
  let _trcErr = '';
  let _trcDetailId = null;  // 当前展开详情的 conv_id（null=仅列表）
  let _trcRetain = null;    // 保留天数（来自 list 接口的 retention_days）

  // span_type → 图标/标签文案（对齐任务书 span_type 枚举）
  const _TRC_ICON = {
    llm_call: '🧠', tool_call: '🔧', mcp_call: '🔌', rag_search: '📚',
    skill_inject: '🧩', file_op: '📄', cache_hit: '⚡', error: '⚠️',
  };
  const _TRC_LABEL = {
    llm_call: 'LLM 调用', tool_call: '工具', mcp_call: 'MCP', rag_search: 'RAG 检索',
    skill_inject: 'Skill 注入', file_op: '文件', cache_hit: '缓存命中', error: '错误',
  };

  function _trcIcon(st) { return _TRC_ICON[st] || '•'; }
  function _trcLabel(st) { return _TRC_LABEL[st] || esc(st || ''); }
  function _trcStatusBadge(status) {
    const s = (status || '').toLowerCase();
    if (s === 'ok') return '<span class="tag ok">ok</span>';
    if (s === 'error') return '<span class="tag warn">error</span>';
    if (s === 'degraded') return '<span class="tag warn">degraded</span>';
    return status ? '<span class="tag">' + esc(status) + '</span>' : '—';
  }

  // 会话列表行（时间/agent/模型/token 总量/工具数/状态，可点进详情）
  function _trcListHTML() {
    if (!_trcConvs.length) {
      return '<div class="trc-empty">（暂无 trace 数据 —— 尚无已记录的会话，或已超出保留期被清理）</div>';
    }
    const rows = _trcConvs.map(c => {
      const tools = (c.tools_called || []);
      const models = (c.models || []);
      const isDetail = c.id === _trcDetailId;
      return '<tr class="' + (isDetail ? 'trc-act' : '') + '" style="cursor:pointer" onclick="trcOpen(\'' +
        esc(c.id) + '\')">' +
        '<td class="k mono">' + esc(c.started_at || '') + '</td>' +
        '<td><strong>' + esc(c.agent_name || ('agent#' + c.agent_id)) + '</strong>' +
          ' <span class="k mono">#' + esc(c.agent_id) + '</span>' +
          ' ' + (c.backend ? '<span class="tag ' + (c.backend === 'hermes' ? 'herm' : '') + '">' + esc(c.backend) + '</span>' : '') + '</td>' +
        '<td class="k mono">' + (models.length ? models.map(m => '<span class="tag">' + esc(m) + '</span>').join('') : '—') + '</td>' +
        '<td class="k">' + (c.total_tokens_in || 0) + ' / ' + (c.total_tokens_out || 0) +
          ' <span class="k">(' + (c.total_llm_calls || 0) + ' 次)</span></td>' +
        '<td class="k">' + tools.length + (tools.length ? '<div class="k mono">' + tools.map(t => '<span class="tag">' + esc(t) + '</span>').join('') + '</div>' : '') + '</td>' +
        '<td>' + _trcStatusBadge(c.status) + (c.error ? '<div class="k err" style="margin-top:2px">' + esc(c.error) + '</div>' : '') + '</td>' +
      '</tr>';
    }).join('');
    return '<table><tr><th>开始时间</th><th>Agent</th><th>模型</th><th>token (in/out · LLM 次数)</th><th>工具数</th><th>状态</th></tr>' +
      rows + '</table>';
  }

  // 详情时间线：每个 span 一行（序号/类型图标/名称/时间/耗时/token/状态），可展开 input/output；
  // rag_search 展开可见命中 chunk 列表（具体到 chunk）。
  function _trcDetailHTML(conv) {
    const head = '<div class="panel trc-detail-head" style="background:var(--panel2);margin-top:10px">' +
      '<div class="row" style="align-items:center">' +
        '<button class="small" onclick="trcBack()">← 返回列表</button>' +
        '<div style="flex:1"><strong>' + esc(conv.agent_name || ('agent#' + conv.agent_id)) + '</strong>' +
          ' <span class="k mono">conv ' + esc(conv.id) + '</span> ' + _trcStatusBadge(conv.status) + '</div>' +
      '</div>' +
      '<div class="k" style="margin-top:8px">' +
        '开始 ' + esc(conv.started_at || '—') + ' · 结束 ' + esc(conv.ended_at || '—') + ' · 总耗时 ' +
        (conv.duration_ms != null ? conv.duration_ms + ' ms' : '—') +
        ' · LLM 次数 ' + (conv.total_llm_calls || 0) + ' · token ' +
        (conv.total_tokens_in || 0) + '/' + (conv.total_tokens_out || 0) +
        (_trcErr ? ' <span class="err">' + esc(_trcErr) + '</span>' : '') +
      '</div></div>';

    if (!(_trcSpans && _trcSpans.length)) {
      return head + '<div class="trc-empty">（该会话无 span 明细）</div>';
    }

    const rows = _trcSpans.map(s => {
      const ti = (s.tokens_in != null ? s.tokens_in : '—') + ' / ' + (s.tokens_out != null ? s.tokens_out : '—');
      const dur = s.duration_ms != null ? (s.duration_ms + ' ms') : '—';
      const hasBody = (s.input || s.output || (s.rag_chunks && s.rag_chunks.length));
      const toggle = hasBody
        ? '<button class="small" style="flex:0 0 auto" onclick="trcToggle(' + s.id + ')">展开</button>'
        : '';
      // 展开体：input/output（JSON 友好）+ rag_search 的 chunk 列表
      let body = '';
      if (hasBody) {
        body = '<div class="trc-body" id="trc-body-' + s.id + '" style="display:none">';
        if (s.rag_chunks && s.rag_chunks.length) {
          body += '<div class="k">📚 命中 chunk（' + s.rag_chunks.length + '）</div>' +
            '<div class="trc-chunks">' +
            s.rag_chunks.map((c, i) =>
              '<div class="trc-chunk">' +
              '<span class="tag acc">chunk ' + (c.chunk_seq != null ? c.chunk_seq : (i + 1)) + '</span>' +
              (c.knowledge_id != null ? ' <span class="k mono">kb#' + esc(c.knowledge_id) + '</span>' : '') +
              (c.score != null ? ' <span class="k">score ' + esc(c.score) + '</span>' : '') +
              '<div class="trc-chunk-txt">' + esc(c.preview || c.text || '') + '</div></div>').join('') +
            '</div>';
        }
        if (s.input) {
          body += '<div class="k">输入 input</div><pre class="trc-io">' + esc(_trcPretty(s.input)) + '</pre>';
        }
        if (s.output) {
          body += '<div class="k">输出 output</div><pre class="trc-io">' + esc(_trcPretty(s.output)) + '</pre>';
        }
        if (s.error) {
          body += '<div class="k">错误</div><pre class="trc-io">' + esc(s.error) + '</pre>';
        }
        body += '</div>';
      }
      return '<div class="trc-row">' +
        '<div class="trc-line">' +
          '<span class="trc-idx">' + (s.seq != null ? s.seq : '·') + '</span>' +
          '<span class="trc-icon" title="' + esc(s.span_type || '') + '">' + _trcIcon(s.span_type) + '</span>' +
          '<span class="trc-name">' + _trcLabel(s.span_type) + ' ' +
            (s.name ? '<span class="mono">' + esc(s.name) + '</span>' : '') + '</span>' +
          '<span class="k mono trc-ts">' + esc(s.ts || '') + '</span>' +
          '<span class="k">' + dur + '</span>' +
          (s.span_type === 'llm_call'
            ? '<span class="k">tok ' + ti + (s.model ? ' · ' + esc(s.model) : '') + '</span>'
            : (s.tokens_in != null || s.tokens_out != null ? '<span class="k">tok ' + ti + '</span>' : '')) +
          _trcStatusBadge(s.status) +
          toggle +
        '</div>' + body + '</div>';
    }).join('');
    return head + '<div class="trc-timeline">' + rows + '</div>';
  }

  // input/output 可能是 JSON 字符串或纯文本 → 尝试美化 JSON，失败原样
  function _trcPretty(v) {
    try { return JSON.stringify(JSON.parse(v), null, 2); }
    catch (e) { return String(v); }
  }

  function renderTracePanel() {
    const isAdmin = !!(me && me.permissions && me.permissions.includes('system:admin'));
    // 非 admin：不请求数据，直接给出只读/无权限提示（后端同样 403，不泄露数据）
    if (!isAdmin) {
      return '<div class="panel"><h3>链路追踪' + _tipBox(
        '全链路 trace 记录：每次对话记录 LLM/工具/MCP/RAG(到 chunk)/skill/文件/缓存/错误 各步骤。\n' +
        '仅 system:admin 可查询（安全优先，不跨 agent 泄露）。当前账号无 system:admin 权限。') + '</h3>' +
        '<div class="k">你当前没有 <span class="tag warn">system:admin</span> 权限，trace 查询接口（/api/trace/*）会返回 403。</div></div>';
    }

    // 视图二选一：列表 或 详情
    if (_trcDetailId) {
      const conv = _trcConvs.find(c => c.id === _trcDetailId);
      const body = _trcState === 'loading'
        ? '<div class="trc-loading">加载 span 明细中…</div>'
        : _trcState === 'error'
          ? '<div class="err">加载失败：' + esc(_trcErr) + ' <button class="small" onclick="trcOpen(\'' + esc(_trcDetailId) + '\')">重试</button></div>'
          : (conv ? _trcDetailHTML(conv) : '<div class="err">会话不存在</div>');
      return '<div class="panel"><h3>链路追踪 · 会话详情</h3>' + body + '</div>';
    }

    // 列表态
    const body =
      _trcState === 'loading'
        ? '<div class="trc-loading">加载 trace 会话列表…</div>'
        : _trcState === 'error'
          ? '<div class="err">加载失败：' + esc(_trcErr) + ' <button class="small" onclick="trcLoad()">重试</button></div>'
          : _trcState === 'empty'
            ? '<div class="trc-empty">（暂无 trace 数据）</div>'
            : _trcListHTML();
    return '<div class="panel"><h3>链路追踪' + _tipBox(
      '每次对话的完整执行链路（迭代5 全链路 trace）。\n' +
      '· 会话列表：时间/agent/模型/token 总量/工具数/状态，点行进入详情。\n' +
      '· 详情时间线：每个 span 一行（LLM/工具/MCP/RAG/skill/文件/缓存/错误），可展开看 input/output。\n' +
      '· rag_search 展开可见命中 chunk 列表（knowledge_id/chunk_seq/score/preview）。\n' +
      '仅 system:admin 可查（/api/trace/*，不跨 agent 泄露）。保留 ' +
      (_trcRetain != null ? _trcRetain + ' 天' : 'N 天（默认 30，settings 表可配）') + '。') + '</h3>' +
      '<div class="row" style="margin-bottom:10px">' +
        '<div style="flex:0 0 240px"><label>按 Agent 过滤</label>' +
        '<select id="trc-agent" onchange="trcFilter(this.value)">' +
          '<option value=""' + (_trcAgentQ == null ? ' selected' : '') + '>全部 agent</option>' +
          (agentsCache || []).map(a =>
            '<option value="' + a.id + '"' + (_trcAgentQ != null && String(_trcAgentQ) === String(a.id) ? ' selected' : '') + '>' +
            esc(a.name) + ' (#' + a.id + ')</option>').join('') +
        '</select></div>' +
        '<div style="flex:0 0 auto;align-self:flex-end"><button class="small" onclick="trcLoad()">刷新</button></div>' +
      '</div>' + body + '</div>';
  }

  async function trcLoad() {
    _trcState = 'loading'; _trcErr = '';
    // 确保 agent 下拉有数据（agentsCache 可能还没被 loadAgents 填充）
    if (!agentsCache.length) {
      try { agentsCache = (await api('/api/agents')).agents || agentsCache; }
      catch (e) { /* 下拉留空，不影响 trace 列表本体 */ }
    }
    const pg = renderTracePanel();
    const el = $('#trc-panel');
    if (el) el.innerHTML = pg;
    try {
      let p = '/api/trace/conversations?limit=100';
      if (_trcAgentQ != null) p += '&agent_id=' + _trcAgentQ;
      const j = await api(p);
      _trcConvs = j.conversations || [];
      _trcRetain = (j.retention_days != null) ? j.retention_days : _trcRetain;
      _trcState = _trcConvs.length ? 'ok' : 'empty';
    } catch (e) {
      _trcState = 'error'; _trcErr = e.message;
    }
    const el2 = $('#trc-panel');
    if (el2) el2.innerHTML = renderTracePanel();
  }

  function trcFilter(v) {
    _trcAgentQ = v === '' ? null : parseInt(v, 10);
    _trcDetailId = null;
    trcLoad();
  }

  async function trcOpen(convId) {
    _trcDetailId = convId; _trcState = 'loading'; _trcErr = '';
    const el = $('#trc-panel');
    if (el) el.innerHTML = renderTracePanel();
    try {
      const j = await api('/api/trace/conversations/' + encodeURIComponent(convId));
      _trcSpans = j.spans || [];
      // 若列表缓存里没有该 conv（如分页外），补一条最小聚合
      if (!(_trcConvs || []).some(c => c.id === convId)) _trcConvs.unshift(j.conversation || {});
      _trcState = 'ok';
    } catch (e) {
      _trcState = 'error'; _trcErr = e.message;
    }
    const el2 = $('#trc-panel');
    if (el2) el2.innerHTML = renderTracePanel();
  }

  function trcBack() {
    _trcDetailId = null; _trcSpans = [];
    const el = $('#trc-panel');
    if (el) el.innerHTML = renderTracePanel();
  }

  function trcToggle(id) {
    const b = $('#trc-body-' + id);
    if (!b) return;
    const open = b.style.display !== 'none';
    b.style.display = open ? 'none' : 'block';
    const btn = b.closest('.trc-row').querySelector('button');
    if (btn) btn.textContent = open ? '展开' : '收起';
  }

  // ---------- Skills ----------
  function _skillListHTML(M) {
    const q = _skillQ.toLowerCase();
    const rows = _skills.filter(s =>
      !q || (s.name || '').toLowerCase().includes(q) ||
            (s.description || '').toLowerCase().includes(q) ||
            (s.content || '').toLowerCase().includes(q));
    return '<div class="k" style="margin:8px 0 6px">共 ' + _skills.length +
      ' 个 · 当前显示 ' + rows.length + ' 个</div>' +
      (rows.length
        ? '<table><tr><th>名称</th><th>描述</th><th>内容长度</th><th>更新时间</th><th>操作</th></tr>' +
          rows.map(s =>
            '<tr><td><strong>' + esc(s.name) + '</strong><div class="k mono">#' + s.id + '</div></td>' +
            '<td>' + esc(s.description || '') + '</td>' +
            '<td class="k">' + (s.content || '').length + ' 字符</td>' +
            '<td class="k">' + esc(s.updated_at || '') + '</td>' +
            '<td>' +
              '<button class="small" onclick="extViewSkill(' + s.id + ')">查看</button>' +
              (M ? '<button class="small" onclick="extEditSkill(' + s.id + ')">编辑</button>' : '') +
              (M ? '<button class="danger" onclick="extDelSkill(' + s.id + ')">删除</button>' : '') +
            '</td></tr>').join('') +
          '</table>'
        : '<div class="k">（无匹配的 skill）</div>');
  }

  function renderSkillsPanel(M) {
    return '<h3>Skills（指令包 · 注入 prompt 固定左侧）</h3>' +
      '<div class="row" style="margin-bottom:10px">' +
        '<div style="flex:0 0 260px"><label>搜索（名称/描述/内容）</label>' +
          '<input id="sk-q" placeholder="输入关键字过滤…" value="' + esc(_skillQ) + '" oninput="extFilterSkills()"></div>' +
        '<div style="flex:0 0 auto;align-self:flex-end">' +
          (M ? '<button class="primary" style="width:auto;padding:10px 20px" onclick="extNewSkill()">＋ 新建 Skill</button>' : '') +
          (M ? '<button class="small" onclick="extUploadPick()">⬆ 上传导入</button>' : '') +
          '</div></div>' +
      (M ?
        '<div id="sk-form" class="hidden"></div>' +
        '<div id="sk-upload" class="hidden"></div>' :
        '<div id="sk-upload" class="hidden"></div>') +
      '<div id="sk-list">' + _skillListHTML(M) + '</div>';
  }

  function extFilterSkills() {
    _skillQ = $('#sk-q').value;
    // 只重绘结果列表（#sk-list），不动搜索框/表单/上传区，避免抢焦点
    const list = $('#sk-list');
    if (list) list.innerHTML = _skillListHTML(canManage());
  }

  function extNewSkill() { _skillEditId = null; _showSkillForm(); }
  function extEditSkill(id) {
    const s = _skills.find(x => x.id === id);
    if (!s) return;
    _skillEditId = id; _showSkillForm();
  }

  function _showSkillForm() {
    const s = _skillEditId ? _skills.find(x => x.id === _skillEditId) : null;
    const f = $('#sk-form');
    f.classList.remove('hidden');
    f.innerHTML =
      '<div class="panel" style="background:var(--panel2);margin-top:10px">' +
      '<h4>' + (s ? '编辑 Skill #' + s.id : '新建 Skill') + '</h4>' +
      '<div class="row">' +
        '<div><label>名称（必填，唯一）</label><input id="skf-name" value="' + (s ? esc(s.name) : '') + '"></div>' +
        '<div><label>描述</label><input id="skf-desc" value="' + (s ? esc(s.description || '') : '') + '"></div>' +
      '</div>' +
      '<label>内容（注入 prompt 的指令文本，必填）</label>' +
      '<textarea id="skf-content" rows="6">' + (s ? esc(s.content) : '') + '</textarea>' +
      '<div style="margin-top:10px">' +
        '<button class="primary" style="width:auto;padding:9px 22px" onclick="extSaveSkill()">' + (s ? '保存' : '创建') + '</button>' +
        '<button class="small" onclick="extCloseSkillForm()">取消</button>' +
        '<span id="skf-err" class="err"></span></div></div>';
    f.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function extCloseSkillForm() { $('#sk-form').classList.add('hidden'); $('#sk-form').innerHTML = ''; }

  async function extSaveSkill() {
    const body = {
      name: $('#skf-name').value.trim(),
      description: $('#skf-desc').value.trim(),
      content: $('#skf-content').value,
    };
    if (!body.name) { $('#skf-err').textContent = '名称必填'; return; }
    if (!body.content.trim()) { $('#skf-err').textContent = '内容必填（不能为空）'; return; }
    try {
      if (_skillEditId) await api('/api/ext/skills/' + _skillEditId, { method: 'PUT', body: JSON.stringify(body) });
      else await api('/api/ext/skills', { method: 'POST', body: JSON.stringify(body) });
      extCloseSkillForm();
      await loadExt();
    } catch (e) { $('#skf-err').textContent = e.message; }
  }

  async function extDelSkill(id) {
    const s = _skills.find(x => x.id === id);
    if (!confirm('删除 Skill "' + (s ? s.name : id) + '"？（不可恢复）')) return;
    try { await api('/api/ext/skills/' + id, { method: 'DELETE' }); await loadExt(); }
    catch (e) { alert(e.message); }
  }

  function extViewSkill(id) {
    const s = _skills.find(x => x.id === id);
    if (!s) return;
    alert('Skill: ' + s.name + '\n描述: ' + (s.description || '(无)') +
      '\n更新时间: ' + (s.updated_at || '(无)') + '\n\n' +
      '===== 内容 =====\n' + s.content);
  }

  // ---- 上传导入 ----
  function extUploadPick() {
    const u = $('#sk-upload');
    u.classList.remove('hidden');
    u.innerHTML =
      '<div class="panel" style="background:var(--panel2);margin-top:10px">' +
      '<h4>上传导入（单文件 .md/.txt 或 zip 目录，解析 SKILL.md frontmatter）</h4>' +
      '<input type="file" id="sk-file" multiple accept=".md,.txt,.zip">' +
      '<div style="margin-top:8px">' +
        '<button class="primary" style="width:auto;padding:9px 20px" onclick="extDoUpload()">开始导入</button>' +
        '<button class="small" onclick="extCloseUpload()">取消</button></div>' +
      '<div id="sk-up-res"></div></div>';
  }
  function extCloseUpload() { $('#sk-upload').classList.add('hidden'); $('#sk-upload').innerHTML = ''; }

  async function extDoUpload() {
    const fileInput = $('#sk-file');
    const files = fileInput.files;
    if (!files || !files.length) { $('#sk-up-res').innerHTML = '<div class="err">请选择至少一个文件</div>'; return; }
    const fd = new FormData();
    for (const f of files) fd.append('files', f);
    const res = $('#sk-up-res');
    res.innerHTML = '<div class="k">导入中…</div>';
    // api() 默认带 Content-Type: application/json，multipart 需要去掉（浏览器自动加 boundary）
    const headers = { 'Authorization': 'Bearer ' + (localStorage.getItem(TOKEN_KEY) || '') };
    let p = '/api/ext/skills/upload';
    const m = location.pathname.match(/^(.*\/)agent\//);
    if (m) p = m[1] + 'agent' + p;
    let r;
    try {
      r = await fetch(p, { method: 'POST', body: fd, headers });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.message || ('HTTP ' + r.status));
      _renderUploadRes(j);
      // BUG-006: 导入成功后只刷新 skills 列表（#sk-list），不整块重渲染 #page-ext。
      // 原实现 await loadExt() 会重生成一个空的 hidden #sk-upload，把刚渲染进
      // #sk-up-res 的导入结果面板整体清空 → 用户永远看不到 新增/跳过/失败 汇总。
      // 改法（修复要求选项 1 的实现：结果落在不会被重渲染的节点内）：
      //   保留已渲染的结果面板（#sk-upload/#sk-up-res），仅用局部刷新同步列表，
      //   导入结果因此稳定可见，列表也同步新增（含重名/失败场景的完整明细）。
      _skillQ = '';
      const sk = await api('/api/ext/skills').catch(() => ({ skills: [] }));
      _skills = sk.skills || [];
      const qInput = $('#sk-q');
      if (qInput) qInput.value = '';
      const list = $('#sk-list');
      if (list) list.innerHTML = _skillListHTML(canManage());
    } catch (e) {
      res.innerHTML = '<div class="err">导入失败: ' + esc(e.message) + '</div>';
    }
  }

  function _renderUploadRes(j) {
    const c = j.counts || {};
    let html = '<div class="panel" style="background:#12251a;border-color:var(--ok);margin-top:10px">' +
      '<div style="color:var(--ok)">✓ 完成：新增 ' + (c.created || 0) +
      ' · 跳过 ' + (c.skipped || 0) + ' · 失败 ' + (c.failed || 0) + '</div></div>' +
      '<div id="sk-up-detail"></div>';
    const det = $('#sk-up-res');
    det.innerHTML = html;
    const d = det.querySelector('#sk-up-detail');
    const part = (title, arr, color) => {
      if (!arr || !arr.length) return '';
      return '<div class="k" style="margin-top:8px">' + title + '（' + arr.length + '）</div>' +
        arr.map(x => {
          const txt = (x.name ? '「' + x.name + '」' : (x.file ? '「' + x.file + '」' : '')) +
            ' — ' + (x.reason || x.source || '');
          return '<div class="' + (color || 'k') + '">' + esc(txt) + '</div>';
        }).join('');
    };
    d.innerHTML = part('新增成功', j.created, 'ok') +
      part('跳过（重名/空内容）', j.skipped, 'warn') +
      part('失败（类型/大小）', j.failed, 'err');
  }

  // ---------- MCP ----------
  // TASK-054 迭代4：传输标记 badge（列表行内，紧跟名称）
  function _mcpTransportBadge(transport) {
    const t = (transport || 'stdio').toLowerCase();
    return '<span class="tag ' + (t === 'http' ? 'acc' : '') + '">' +
      (t === 'http' ? 'http' : 'stdio') + '</span>';
  }

  function renderMcpPanel(M) {
    return (M ?
      '<div id="mcp-form" class="hidden"></div>' +
      '<div class="k" style="margin:6px 0">共 ' + _mcps.length + ' 个 server</div>' :
      '<div class="k" style="margin:6px 0">共 ' + _mcps.length + ' 个 server（只读）</div>') +
      (_mcps.length
        ? '<table><tr><th>名称 / 传输</th><th>端点 / 命令</th><th>env / headers</th><th>状态</th><th>操作</th><th>tools/call 输出</th></tr>' +
          _mcps.map(s => {
            const t = (s.transport || 'stdio').toLowerCase();
            // stdio：显示 command args；http：显示 url（+ headers 项数）
            const endpoint = (t === 'http')
              ? esc(s.url || '')
              : esc(s.command || '') + ((s.args && s.args.length) ? ' ' + esc(s.args.join(' ')) : '');
            // env（stdio）/ headers（http）计数
            const kv = (t === 'http') ? (s.headers || {}) : (s.env || {});
            const kvLabel = (t === 'http') ? 'headers' : 'env';
            return '<tr><td><strong>' + esc(s.name) + '</strong> ' + _mcpTransportBadge(t) +
              '<div class="k mono">#' + s.id + '</div></td>' +
              '<td class="k mono" style="word-break:break-all">' + endpoint + '</td>' +
              '<td class="k">' + (Object.keys(kv).length ? kvLabel + ' ' + Object.keys(kv).length + ' 项' : '—') + '</td>' +
              '<td>' + (M
                ? '<label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer">' +
                  '<input type="checkbox" class="mcp-toggle" data-id="' + s.id + '" ' + (s.enabled ? 'checked' : '') + ' onchange="extToggleMcp(' + s.id + ', this.checked)">' +
                  '<span class="tag ' + (s.enabled ? 'ok' : 'warn') + '">' + (s.enabled ? 'enabled' : 'disabled') + '</span></label>'
                : '<span class="tag ' + (s.enabled ? 'ok' : 'warn') + '">' + (s.enabled ? 'enabled' : 'disabled') + '</span>') + '</td>' +
              '<td>' +
                '<button class="small" onclick="extMcpTools(' + s.id + ')">tools/list</button>' +
                '<button class="small" onclick="extMcpCall(' + s.id + ',\'get_time\')">call get_time</button>' +
                (M ? '<button class="small" onclick="extEditMcp(' + s.id + ')">编辑</button>' : '') +
                (M ? '<button class="danger" onclick="extDelMcp(' + s.id + ')">删除</button>' : '') +
              '</td><td id="mcp-out-' + s.id + '"></td></tr>';
          }).join('') +
          '</table><div class="k">说明：MCP 测试按钮会真实启动 stdio 子进程（内置 demo）或真实 POST 到 http 端点（Streamable HTTP，超时 10s）。' + (M ? ' 编辑表单需回传完整 name/transport/url/headers/command/args/env/enabled（后端全量覆盖）。' : '') + '</div>'
        : '<div class="k">（暂无 MCP server）</div>') +
      (M ? '<button class="small" onclick="extNewMcp()">＋ 新建 MCP Server</button>' : '');
  }

  function extMcpTools(id) {
    api('/api/ext/mcp/' + id + '/tools').then(r => {
      document.getElementById('mcp-out-' + id).innerHTML =
        '<pre>' + esc(JSON.stringify({ tools: r.tools }, null, 2)) + '</pre>';
    }).catch(e => {
      document.getElementById('mcp-out-' + id).innerHTML = '<div class="err">' + esc(e.message) + '</div>';
    });
  }
  function extMcpCall(id, tool) {
    api('/api/ext/mcp/' + id + '/tools/' + tool + '/call',
      { method: 'POST', body: JSON.stringify({ arguments: {} }) }).then(r => {
        document.getElementById('mcp-out-' + id).innerHTML =
          '<pre>' + esc(JSON.stringify(r.result, null, 2)) + '</pre>';
      }).catch(e => {
        document.getElementById('mcp-out-' + id).innerHTML = '<div class="err">' + esc(e.message) + '</div>';
      });
  }

  async function extToggleMcp(id, enabled) {
    const s = _mcps.find(x => x.id === id);
    if (!s) return;
    // PUT 是全量覆盖，需回传完整对象（含 transport/url/headers）
    const t = (s.transport || 'stdio').toLowerCase();
    const body = {
      name: s.name,
      command: t === 'http' ? '' : (s.command || ''),
      args: (s.args || []),
      env: (s.env || {}),
      transport: t,
      url: t === 'http' ? (s.url || '') : '',
      headers: t === 'http' ? (s.headers || {}) : {},
      enabled,
    };
    try {
      await api('/api/ext/mcp/' + id, { method: 'PUT', body: JSON.stringify(body) });
      await loadExt();
    } catch (e) {
      alert(e.message);
      await loadExt();
    }
  }

  function extNewMcp() { _mcpEditId = null; _showMcpForm(); }
  function extEditMcp(id) {
    const s = _mcps.find(x => x.id === id);
    if (!s) return;
    _mcpEditId = id; _showMcpForm();
  }

  // TASK-054 迭代4：stdio 传输区内容（command/args/env 键值对，原样零回归）。
  // 返回纯内容串，外层由 _showMcpForm 的 #mcp-stdio-wrap 包裹并按 transport 显隐。
  function _mcpStdioSection(s) {
    const env = s ? (s.env || {}) : {};
    const envRows = Object.entries(env).map(([k, v]) => _envRow(k, v)).join('') || _envRow('', '');
    return '<div class="row">' +
      '<div><label>命令 command（stdio 必填）</label><input id="mcpf-cmd" value="' + (s ? esc(s.command || '') : '') + '" placeholder="python 或 node 或绝对路径"></div>' +
    '</div>' +
    '<label>args（空格分隔，或 JSON 数组）</label>' +
    '<input id="mcpf-args" value="' + (s ? esc((s.args || []).join(' ')) : '') + '" placeholder="脚本路径 --flag（多个空格分隔）">' +
    '<label>env（键值对，动态增删，注入子进程环境变量）</label>' +
    '<div id="mcpf-env">' + envRows + '</div>' +
    '<button class="small" onclick="extAddEnvRow()">＋ 加一行 env</button>';
  }

  // TASK-054 迭代4：http 传输区内容（URL + headers 键值对，Streamable HTTP 端点）。
  // 返回纯内容串，外层由 _showMcpForm 的 #mcp-http-wrap 包裹并按 transport 显隐。
  function _mcpHttpSection(s) {
    const headers = s ? (s.headers || {}) : {};
    const headerRows = Object.entries(headers).map(([k, v]) => _hdrRow(k, v)).join('') || _hdrRow('', '');
    return '<label>URL（http 必填，Streamable HTTP 端点地址）</label>' +
      '<input id="mcpf-url" value="' + (s ? esc(s.url || '') : '') + '" placeholder="http://host:port/mcp 或 https://...（需 http/https + host）">' +
      '<label>headers（键值对，可选自定义请求头，如 Authorization）</label>' +
      '<div id="mcpf-headers">' + headerRows + '</div>' +
      '<button class="small" onclick="extAddHdrRow()">＋ 加一行 header</button>' +
      '<div class="k" style="margin-top:6px">提示：http 传输无需本地 command；headers 值原样发送（如 Authorization: Bearer &lt;token&gt;）。超时默认 10s（connect 5s）。</div>';
  }

  function _envRow(k, v) {
    return '<div class="row mcpf-env-row" style="margin-bottom:6px">' +
      '<div><input class="mcpf-env-k" placeholder="KEY" value="' + esc(k) + '"></div>' +
      '<div><input class="mcpf-env-v" placeholder="value" value="' + esc(v) + '"></div>' +
      '<div style="flex:0 0 auto;align-self:flex-end"><button class="small" onclick="this.closest(\'.mcpf-env-row\').remove()">删</button></div>' +
      '</div>';
  }
  function extAddEnvRow() { const e = $('#mcpf-env'); if (e) e.insertAdjacentHTML('beforeend', _envRow('', '')); }

  function _hdrRow(k, v) {
    return '<div class="row mcpf-hdr-row" style="margin-bottom:6px">' +
      '<div><input class="mcpf-hdr-k" placeholder="Header-Name" value="' + esc(k) + '"></div>' +
      '<div><input class="mcpf-hdr-v" placeholder="value（如 Bearer xxx）" value="' + esc(v) + '"></div>' +
      '<div style="flex:0 0 auto;align-self:flex-end"><button class="small" onclick="this.closest(\'.mcpf-hdr-row\').remove()">删</button></div>' +
      '</div>';
  }
  function extAddHdrRow() { const h = $('#mcpf-headers'); if (h) h.insertAdjacentHTML('beforeend', _hdrRow('', '')); }

  // TASK-054 迭代4：传输切换（stdio / http 两态表单）。
  // 切到 http → 隐藏 stdio 区、显示 http 区（URL+headers）；
  // 切到 stdio → 隐藏 http 区、显示 stdio 区（command/args/env）。
  // 仅切显示，不清空已输入值（用户来回切换不丢数据）。
  function extSwitchMcpTransport() {
    const sel = $('#mcpf-transport');
    const t = sel ? sel.value : 'stdio';
    const stdio = $('#mcp-stdio-wrap');
    const http = $('#mcp-http-wrap');
    if (stdio) stdio.classList.toggle('hidden', t !== 'stdio');
    if (http) http.classList.toggle('hidden', t !== 'http');
  }

  function _showMcpForm() {
    const s = _mcpEditId ? _mcps.find(x => x.id === _mcpEditId) : null;
    const t = s ? (s.transport || 'stdio').toLowerCase() : 'stdio';
    const f = $('#mcp-form');
    f.classList.remove('hidden');
    // 两态区：按当前 transport 决定哪个可见（stdio 默认）
    const stdioVisible = t === 'stdio';
    f.innerHTML =
      '<div class="panel" style="background:var(--panel2);margin-top:10px">' +
      '<h4>' + (s ? '编辑 MCP Server #' + s.id : '新建 MCP Server') + '</h4>' +
      '<div class="row">' +
        '<div><label>名称（必填，唯一）</label><input id="mcpf-name" value="' + (s ? esc(s.name) : '') + '"></div>' +
        '<div style="flex:0 0 220px"><label>传输类型</label>' +
          '<select id="mcpf-transport" onchange="extSwitchMcpTransport()">' +
            '<option value="stdio"' + (t === 'stdio' ? ' selected' : '') + '>stdio（本地命令）</option>' +
            '<option value="http"' + (t === 'http' ? ' selected' : '') + '>http（Streamable HTTP 端点）</option>' +
          '</select></div>' +
      '</div>' +
      // stdio 区（command/args/env）— 按 transport 显隐
      '<div' + (stdioVisible ? '' : ' class="hidden"') + ' id="mcp-stdio-wrap">' + _mcpStdioSection(s) + '</div>' +
      // http 区（url/headers）— 按 transport 显隐
      '<div' + (stdioVisible ? ' class="hidden"' : '') + ' id="mcp-http-wrap">' + _mcpHttpSection(s) + '</div>' +
      '<div class="row" style="margin-top:10px;align-items:center">' +
        '<div style="flex:0 0 auto"><label style="display:inline-flex;gap:6px;align-items:center;margin:0">' +
          '<input type="checkbox" id="mcpf-enabled" ' + (s && !s.enabled ? '' : 'checked') + '> enabled</label></div></div>' +
      '<div style="margin-top:10px">' +
        '<button class="primary" style="width:auto;padding:9px 22px" onclick="extSaveMcp()">' + (s ? '保存' : '创建') + '</button>' +
        '<button class="small" onclick="extCloseMcpForm()">取消</button>' +
        '<span id="mcpf-err" class="err"></span></div></div>';
    f.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function extCloseMcpForm() { $('#mcp-form').classList.add('hidden'); $('#mcp-form').innerHTML = ''; }

  async function extSaveMcp() {
    const name = $('#mcpf-name').value.trim();
    const transport = ($('#mcpf-transport') ? $('#mcpf-transport').value : 'stdio').trim().toLowerCase();
    const enabled = $('#mcpf-enabled').checked;
    if (!name) { $('#mcpf-err').textContent = '名称必填'; return; }

    let body;
    if (transport === 'http') {
      // http 态：url 必填；command/args/env 留空（后端按 http 行 command 存空串）
      const url = ($('#mcpf-url') ? $('#mcpf-url').value.trim() : '');
      if (!url) { $('#mcpf-err').textContent = 'URL 必填（http 传输需端点地址）'; return; }
      const headers = {};
      document.querySelectorAll('#mcpf-headers .mcpf-hdr-row').forEach(row => {
        const k = row.querySelector('.mcpf-hdr-k').value.trim();
        const v = row.querySelector('.mcpf-hdr-v').value;
        if (k) headers[k] = v;
      });
      body = { name, command: '', args: [], env: {}, transport, url, headers, enabled };
    } else {
      // stdio 态：command 必填；url/headers 留空
      const command = ($('#mcpf-cmd') ? $('#mcpf-cmd').value.trim() : '');
      if (!command) { $('#mcpf-err').textContent = '命令 command 必填（stdio 传输）'; return; }
      const argsRaw = ($('#mcpf-args') ? $('#mcpf-args').value.trim() : '');
      let args;
      if (argsRaw) {
        try {
          const p = JSON.parse(argsRaw);
          args = Array.isArray(p) ? p.map(String) : [argsRaw];
        } catch (e) {
          args = argsRaw.split(/\s+/);
        }
      } else args = [];
      const env = {};
      document.querySelectorAll('#mcpf-env .mcpf-env-row').forEach(row => {
        const k = row.querySelector('.mcpf-env-k').value.trim();
        const v = row.querySelector('.mcpf-env-v').value;
        if (k) env[k] = v;
      });
      body = { name, command, args, env, transport, url: '', headers: {}, enabled };
    }
    try {
      if (_mcpEditId) await api('/api/ext/mcp/' + _mcpEditId, { method: 'PUT', body: JSON.stringify(body) });
      else await api('/api/ext/mcp', { method: 'POST', body: JSON.stringify(body) });
      extCloseMcpForm();
      await loadExt();
    } catch (e) { $('#mcpf-err').textContent = e.message; }
  }

  async function extDelMcp(id) {
    const s = _mcps.find(x => x.id === id);
    if (!confirm('删除 MCP Server "' + (s ? s.name : id) + '"？\n（若仍被 agent 引用，后端会返回 409 提示先解绑）')) return;
    try { await api('/api/ext/mcp/' + id, { method: 'DELETE' }); await loadExt(); }
    catch (e) { alert(e.message); }
  }

  // ---------- Plugins 调用 ----------
  function extCallPlugin(name) {
    api('/api/ext/plugins/' + name + '/call',
      { method: 'POST', body: JSON.stringify({ arguments: {} }) }).then(r => {
        document.getElementById('pl-' + name).textContent = JSON.stringify(r, null, 2);
      }).catch(e => { document.getElementById('pl-' + name).textContent = e.message; });
  }

  // ---------- 长文本 4 策略（保持原有） ----------
  function renderLongtextPanel(lt) {
    return '<div class="panel"><h3>长文本 4 策略' + _tipBox(LT_TIP) + '</h3>' +
      '<div class="row"><div><label>长文本（策略1/2 输入）</label><textarea id="lt-text" rows="5">' +
      '患者王建国，52岁。2026-07-14 门诊：FEV1 2.10 L，IgE 410.20 KU/L，控制不佳，加用孟鲁司特。' +
      '2026-09-09 门诊：支气管舒张试验阳性，FEV1 改善 240 ml，IgE 394.00 KU/L，尘螨皮试阳性。' +
      '医嘱：布地奈德/福莫特罗吸入，氯雷他定口服。' +
      '</textarea></div></div>' +
      '<button class="small" onclick="extLtRun(\'map-reduce\')">策略1 Map-Reduce</button>' +
      '<button class="small" onclick="extLtRun(\'incremental-graph\')">策略2 增量图构建</button>' +
      '<button class="small" onclick="extLtRun(\'critique-refine\')">策略3 critique-refine</button>' +
      '<button class="small" onclick="extLtPreprocess()">策略4 pandas 预处理</button>' +
      '<pre id="lt-out"></pre>' +
      '<h4>HITL 人工待办队列（' + (lt.count || 0) + '）</h4><pre>' +
      esc((lt.hitl || []).map(h => '[' + h.status + '] ' + h.task + ' — ' + h.reason).join('\n') || '(空)') +
      '</pre></div>';
  }
  async function extLtRun(kind) {
    const out = $('#lt-out');
    out.textContent = '执行中…（真实调用 LLM，约 10-60s）';
    try {
      const body = { text: $('#lt-text').value };
      if (kind === 'incremental-graph') body.center = '王建国';
      const r = await api('/api/longtext/' + kind, { method: 'POST', body: JSON.stringify(body) });
      out.textContent = JSON.stringify(r, null, 2).slice(0, 3000);
    } catch (e) { out.textContent = e.message; }
  }
  async function extLtPreprocess() {
    const out = $('#lt-out');
    out.textContent = '执行中…';
    const csv = 'patient_id,date,indicator,value,unit\n' +
      'P001,2026-07-14,FEV1,2.10,L\nP001,2026-07-14,IgE,410.20,KU/L\nP001,2026-09-09,FEV1改善量,240,ml\nP001,2026-09-09,IgE,394.00,KU/L\n' +
      'P002,2026-08-20,FEV1,1.95,L\nP002,2026-09-05,FEV1,2.02,L\nP002,2026-08-20,IgE,210.50,KU/L\n';
    try {
      const r = await api('/api/longtext/preprocess', { method: 'POST', body: JSON.stringify({ csv }) });
      out.textContent = JSON.stringify(r, null, 2).slice(0, 3000);
    } catch (e) { out.textContent = e.message; }
  }

  // 暴露给 onclick
  window.loadExt = loadExt;
  window.extNewSkill = extNewSkill; window.extEditSkill = extEditSkill;
  window.extSaveSkill = extSaveSkill; window.extDelSkill = extDelSkill;
  window.extViewSkill = extViewSkill; window.extCloseSkillForm = extCloseSkillForm;
  window.extFilterSkills = extFilterSkills;
  window.extUploadPick = extUploadPick; window.extDoUpload = extDoUpload; window.extCloseUpload = extCloseUpload;
  window.extMcpTools = extMcpTools; window.extMcpCall = extMcpCall; window.extToggleMcp = extToggleMcp;
  window.extNewMcp = extNewMcp; window.extEditMcp = extEditMcp;
  window.extSaveMcp = extSaveMcp; window.extDelMcp = extDelMcp; window.extAddEnvRow = extAddEnvRow;
  window.extAddHdrRow = extAddHdrRow; window.extSwitchMcpTransport = extSwitchMcpTransport;
  window.extCloseMcpForm = extCloseMcpForm;
  window.extCallPlugin = extCallPlugin; window.extLtRun = extLtRun; window.extLtPreprocess = extLtPreprocess;
  // TASK-058: 链路追踪（trace）面板
  window.trcLoad = trcLoad; window.trcFilter = trcFilter; window.trcOpen = trcOpen;
  window.trcBack = trcBack; window.trcToggle = trcToggle;
})();
