/* memory.js — 记忆页（TASK-023 / F2 前端）
 * 复用 app.js 全局 api()/esc()/$ 与 RBAC 守卫；3D 用 graph3d.js 的 Graph3D（纯 JS，离线可用）。
 *
 * 三个视图（tab）+ 保留多跳遍历文本：
 *   1) 列表视图  L0/L1/L2 各自表格化 + 前端关键字过滤 + L0 limit 分页
 *   2) 3D 立体图 L2 属性图 nodes+edges → 可交互 3D 力导向图（拖拽旋转/滚轮缩放/悬停高亮/点击拉 subgraph）
 *   3) B+ 树分桶  选节点 + 维度(date/entity/topic/process) → 有序桶记录
 *   4) 多跳遍历    保留现有 walkGraph / bucketDemo 文本
 */
(function () {
  'use strict';

  let _be = {};              // 记忆后端信息
  let _adapters = {};        // 可插拔适配器
  let _nodes = [];           // L2 节点
  let _edges = [];           // L2 边
  let _l0 = [], _l1 = [], _l0limit = 15, _l0page = 0, _events = [];
  let _tab = 'list';
  let _listQ = '';
  let _g3d = null;           // Graph3D 实例（仅 3D tab 激活时存在）
  let _bucketNode = 'P001';

  async function loadMemory() {
    const [l0, l1, nodes, edges, events, be] = await Promise.all([
      api('/api/memory/l0?limit=15'), api('/api/memory/l1'),
      api('/api/memory/l2/nodes'), api('/api/memory/l2/edges'),
      api('/api/memory/l2/events'), api('/api/memory/backend'),
    ]);
    _l0 = l0.l0 || []; _l1 = l1.l1 || []; _nodes = nodes.nodes || [];
    _edges = edges.edges || []; _events = (events.events || []);
    _be = be.current || {}; _adapters = be.adapters || {};
    _tab = 'list'; _listQ = ''; _l0limit = 15; _l0page = 0;
    _bucketNode = (_nodes[0] && _nodes[0].id) || 'P001';
    _renderMemoryShell();
    _renderTab(_tab);
  }

  function _renderMemoryShell() {
    const b = _be;
    // 顶部：后端信息 + tab 栏
    $('#page-memory').innerHTML =
      '<h2>记忆（三层插件，与 agent 解耦）</h2>' +
      '<div class="grid" style="margin-bottom:16px">' +
        '<div class="card"><div class="k">当前后端</div><div class="v">' + esc(b.name || '') +
          '</div><div class="k">' + (b.stub ? '适配位存根' : '真实 local') + ' · 节点 ' +
          (b.graph_nodes || 0) + ' · 边 ' + (b.graph_edges || 0) + '</div></div>' +
        '<div class="card"><div class="k">可插拔适配器</div>' +
          '<div class="k">' + Object.entries(_adapters)
            .filter(([k]) => k !== 'current')
            .map(([k, v]) => '<span class="tag ' + (v.stub ? 'warn' : 'ok') + '">' + k +
            (v.stub ? '·存根' : '') + '</span>').join(' ') + '</div>' +
          '<div class="k">切换: .env MEMORY_BACKEND=local|redis|milvus|neo4j|hermes</div></div>' +
      '</div>' +
      '<div class="mem-tabs">' +
        _tabBtn('list', '列表视图') +
        _tabBtn('g3d', '3D 立体图') +
        _tabBtn('btree', 'B+ 树分桶') +
        _tabBtn('walk', '多跳遍历') +
      '</div>' +
      '<div id="mem-view"></div>';
  }

  function _tabBtn(id, label) {
    return '<button class="mem-tab' + (_tab === id ? ' active' : '') +
      '" data-tab="' + id + '" onclick="memTab(\'' + id + '\')">' + label + '</button>';
  }

  function _syncTabs() {
    document.querySelectorAll('#page-memory .mem-tab').forEach(b =>
      b.classList.toggle('active', b.dataset.tab === _tab));
  }

  function memTab(t) {
    if (_tab === t) return;
    _destroyG3D();            // 切换离开 3D 时释放 canvas
    _tab = t;
    _syncTabs();
    _renderTab(t);
  }

  function _renderTab(t) {
    const v = $('#mem-view');
    if (t === 'list') v.innerHTML = _listViewHTML();
    else if (t === 'g3d') v.innerHTML = _g3dViewHTML();
    else if (t === 'btree') v.innerHTML = _btreeViewHTML();
    else if (t === 'walk') v.innerHTML = _walkViewHTML();
    if (t === 'g3d') _initG3D();
  }

  // ================= 1) 列表视图 =================
  function _listViewHTML() {
    const q = _listQ.toLowerCase();
    // L0 分页
    const total = _l0.length;
    const pages = Math.max(1, Math.ceil(total / _l0limit));
    const page = Math.min(_l0page, pages - 1);
    const l0rows = _l0.slice(page * _l0limit, (page + 1) * _l0limit).filter(x =>
      !q || String(x.input || '').toLowerCase().includes(q) ||
            String(x.output || '').toLowerCase().includes(q) ||
            String(x.agent_id).toLowerCase().includes(q));
    const l1rows = _l1.filter(x =>
      !q || (x.text || '').toLowerCase().includes(q) ||
            (x.answer || '').toLowerCase().includes(q) ||
            (x.agent_key || '').toLowerCase().includes(q));
    const nodeDeg = _degMap();
    const l2rows = _nodes.filter(n =>
      !q || (n.id || '').toLowerCase().includes(q) ||
            (n.label || '').toLowerCase().includes(q) ||
            JSON.stringify(n.props || {}).toLowerCase().includes(q));

    return '<div class="panel">' +
      '<div class="row">' +
        '<div style="flex:0 0 280px"><label>关键字过滤（作用于 L0/L1/L2）</label>' +
          '<input id="mem-q" placeholder="输入关键字…" value="' + esc(_listQ) + '" oninput="memFilterList()"></div>' +
        '<div style="align-self:flex-end" class="k">匹配: L0 ' + l0rows.length +
          ' · L1 ' + l1rows.length + ' · L2 ' + l2rows.length + '</div></div></div>' +
      '<div class="panel"><h3>L0 原始行为记录（不降噪，共 ' + total + ' 条，每页 ' + _l0limit + '）</h3>' +
        (l0rows.length ? _table(
          ['时间', 'agent', '输入摘要', '输出摘要'],
          l0rows.map(x => [esc(x.ts || ''), 'agent' + x.agent_id,
            _trunc(x.input, 80), _trunc(x.output, 80)]))
          : '<div class="k">（无）</div>') +
        '<div class="row" style="margin-top:8px;align-items:center">' +
          '<div style="flex:0 0 auto">' +
            (page > 0 ? '<button class="small" onclick="memL0Page(' + (page - 1) + ')">上一页</button>' : '') +
            '<button class="small" disabled>' + (page + 1) + ' / ' + pages + '</button>' +
            (page < pages - 1 ? '<button class="small" onclick="memL0Page(' + (page + 1) + ')">下一页</button>' : '') +
          '</div>' +
          '<div><label style="display:inline-block">每页条数</label> ' +
            [10, 15, 30, 50].map(n =>
              '<button class="small' + (n === _l0limit ? '' : '') + '" style="' +
              (n === _l0limit ? 'border-color:var(--acc);color:var(--acc)' : '') +
              '" onclick="memL0Limit(' + n + ')">' + n + '</button>').join(' ') +
          '</div></div></div>' +
      '<div class="panel"><h3>L1 语义缓存（' + l1rows.length + ' 条）</h3>' +
        (l1rows.length ? _table(
          ['agent_key', '文本 text', 'answer'],
          l1rows.map(x => [esc(x.agent_key), _trunc(x.text, 60), _trunc(x.answer, 80)]))
          : '<div class="k">（无）</div>') + '</div>' +
      '<div class="panel"><h3>L2 属性图节点（' + l2rows.length + ' 个）</h3>' +
        (l2rows.length ? _table(
          ['节点 id', '类型 label', '属性 props', '度数', '操作'],
          l2rows.map(n => [
            '<span class="mono">' + esc(n.id) + '</span>',
            '<span class="tag acc">' + esc(n.label) + '</span>',
            _trunc(JSON.stringify(n.props || {}), 90),
            String(nodeDeg[n.id] || 0),
            '<button class="small" onclick="memJumpNode(\'' + esc(n.id).replace(/'/g, "\\'") + '\')">B+ 桶</button>'
          ]))
          : '<div class="k">（无）</div>') + '</div>';
  }

  function _table(headers, rows) {
    return '<table><tr>' + headers.map(h => '<th>' + h + '</th>').join('') +
      '</tr>' + rows.map(r => '<tr>' + r.map(c => '<td>' + c + '</td>').join('') +
      '</tr>').join('') + '</table>';
  }
  function _trunc(s, n) {
    s = String(s == null ? '' : s);
    return s.length > n ? s.slice(0, n) + '…' : s;
  }
  function _degMap() {
    const d = {};
    _nodes.forEach(n => { d[n.id] = 0; });
    _edges.forEach(e => { d[e.src] = (d[e.src] || 0) + 1; d[e.dst] = (d[e.dst] || 0) + 1; });
    return d;
  }

  function memFilterList() {
    _listQ = $('#mem-q').value;
    const v = $('#mem-view');
    if (_tab === 'list') v.innerHTML = _listViewHTML();
  }
  function memL0Page(p) { _l0page = p; if (_tab === 'list') $('#mem-view').innerHTML = _listViewHTML(); }
  function memL0Limit(n) { _l0limit = n; _l0page = 0; if (_tab === 'list') $('#mem-view').innerHTML = _listViewHTML(); }

  // 从 L2 列表跳到 B+ 树分桶（预填节点）
  function memJumpNode(id) {
    _destroyG3D();
    _bucketNode = id;
    _tab = 'btree';
    _syncTabs();
    _renderTab('btree');
  }

  // ================= 2) 3D 立体图 =================
  function _g3dViewHTML() {
    return '<div class="panel"><h3>L2 属性图 · 3D 力导向立体图（纯 JS canvas 2D 投影，离线可用）</h3>' +
      '<div class="k" style="margin-bottom:8px">节点 ' + _nodes.length + ' · 边 ' + _edges.length +
      '。按 label 着色；节点大小 = 度数。悬停高亮邻接，点击拉取子图详情。</div>' +
      '<div id="g3d-mount"></div>' +
      '<div id="g3d-detail" class="hidden"></div></div>';
  }

  function _initG3D() {
    const mount = $('#g3d-mount');
    if (!mount) return;
    _g3d = new Graph3D(mount, {
      nodes: _nodes.map(n => ({ id: n.id, label: n.label, props: n.props })),
      edges: _edges.map(e => ({ src: e.src, dst: e.dst, type: e.type, props: e.props })),
      height: 480,
      edgeLabel: true,
      onHover: (n) => { /* 详情面板只在点击时拉，悬停仅高亮 */ },
      onNodeClick: (n) => _showSubgraph(n.id),
    });
    // 调试/测试钩子：暴露实例（读取节点屏幕坐标、度数等），不影响渲染
    window.__g3d = _g3d;
  }

  async function _showSubgraph(id) {
    const d = $('#g3d-detail');
    d.classList.remove('hidden');
    d.innerHTML = '<div class="k">拉取 ' + esc(id) + ' 的 2 跳子图…</div>';
    try {
      const r = await api('/api/memory/l2/subgraph?start=' +
        encodeURIComponent(id) + '&hops=2');
      d.innerHTML =
        '<div class="panel" style="background:var(--panel2);margin-top:12px">' +
        '<h4>子图详情：' + esc(id) + '（节点 ' + r.nodes.length + ' · 边 ' + r.edges.length + '）</h4>' +
        '<div class="row">' +
          '<div><label>子图节点</label><pre>' + esc(
            r.nodes.map(n => n.id + ' [' + n.label + '] ' + JSON.stringify(n.props)).join('\n') || '(空)') +
            '</pre></div>' +
          '<div><label>子图边</label><pre>' + esc(
            r.edges.map(e => '(' + e.src + ')-[:' + e.type + ' ' + JSON.stringify(e.props) + ']->(' + e.dst + ')').join('\n') || '(空)') +
            '</pre></div></div>' +
        '<label>子图摘要（最终问题只查摘要，不回顾原文）</label><pre>' + esc(r.summary || '') + '</pre>' +
        '<button class="small" onclick="document.getElementById(\'g3d-detail\').classList.add(\'hidden\')">关闭</button>' +
        '</div>';
    } catch (e) {
      d.innerHTML = '<div class="err">' + esc(e.message) + '</div>';
    }
  }

  function _destroyG3D() {
    if (_g3d) { try { _g3d.destroy(); } catch (e) {} _g3d = null; }
  }

  // ================= 3) B+ 树分桶 =================
  function _btreeViewHTML() {
    const dims = ['date', 'entity', 'topic', 'process'];
    const opts = _nodes.map(n =>
      '<option value="' + esc(n.id).replace(/"/g, '&quot;') + '"' +
      (n.id === _bucketNode ? ' selected' : '') + '>' + esc(n.id) + ' [' + esc(n.label) + ']</option>').join('');
    return '<div class="panel"><h3>B+ 树分桶可视化（有序桶记录）</h3>' +
      '<div class="k" style="margin-bottom:10px">选节点 + 维度，复用 <span class="mono">/api/memory/l2/bucket</span>。' +
      '桶按 key 有序（中序遍历有序），支持精确 key 检索。</div>' +
      '<div class="row">' +
        '<div><label>节点</label><select id="bt-node" onchange="memBucketChange()">' + (opts || '') + '</select></div>' +
        '<div><label>维度 dim</label><select id="bt-dim" onchange="memBucketChange()">' +
          dims.map(d => '<option value="' + d + '">' + d + '</option>').join('') + '</select></div>' +
        '<div><label>精确 key（可选）</label><input id="bt-key" placeholder="如 2026-09-09"></div>' +
      '</div>' +
      '<div id="bt-out" style="margin-top:10px"></div></div>';
  }

  async function memBucketChange() {
    const node = $('#bt-node').value;
    const dim = $('#bt-dim').value;
    const key = $('#bt-key').value.trim();
    _bucketNode = node;
    const out = $('#bt-out');
    out.innerHTML = '<div class="k">读取 ' + esc(node) + ' / ' + esc(dim) + ' 桶…</div>';
    try {
      const u = '/api/memory/l2/bucket?node=' + encodeURIComponent(node) +
        '&dim=' + encodeURIComponent(dim) + (key ? '&key=' + encodeURIComponent(key) : '');
      const r = await api(u);
      const recs = r.records || [];
      out.innerHTML =
        '<div class="k" style="margin-bottom:6px">' + (key ? '精确检索 ' + esc(key) + '：' : '全桶有序遍历：') +
        ' 共 ' + recs.length + ' 条</div>' +
        (recs.length
          ? '<div class="bt-tree">' + recs.map((rec, i) =>
              '<div class="bt-node"><span class="bt-idx">' + (i + 1) + '</span>' +
              '<span class="bt-rec">' + esc(JSON.stringify(rec)) + '</span></div>').join('') +
            '</div>'
          : '<div class="k">（该桶为空）</div>');
    } catch (e) { out.innerHTML = '<div class="err">' + esc(e.message) + '</div>'; }
  }

  // ================= 4) 多跳遍历（保留原文本功能） =================
  function _walkViewHTML() {
    return '<div class="panel"><h3>L2 立体图（双向属性图 · 免索引邻接）— 多跳遍历</h3>' +
      '<div class="row">' +
      '<div><label>多跳遍历起点</label><input id="g-start" value="' + esc(_bucketNode) + '"></div>' +
      '<div><label>跳数</label><input id="g-hops" type="number" value="2"></div>' +
      '<div style="align-self:flex-end">' +
        '<button class="small" onclick="memWalkGraph()">遍历</button>' +
        '<button class="small" onclick="memBucketDemo()">B+ 树分桶演示</button></div></div>' +
      '<div id="g-out"></div>' +
      '<div class="k" style="margin-top:10px">节点 / 边（四要素：起/止/类型/属性，节点 ' + _nodes.length + ' · 边 ' + _edges.length + '）</div>' +
      '<pre>' + esc(_edges.map(e => '(' + e.src + ')-[:' + e.type + ' ' +
        JSON.stringify(e.props) + ']->(' + e.dst + ')').join('\n') || '(空)') + '</pre></div>' +
      '<div class="panel"><h3>事件节点（Visit 多对多降维）</h3><pre>' +
      esc(JSON.stringify(_events, null, 2)) + '</pre></div>';
  }

  async function memWalkGraph() {
    try {
      const r = await api('/api/memory/l2/subgraph?start=' +
        encodeURIComponent($('#g-start').value) + '&hops=' + $('#g-hops').value);
      $('#g-out').innerHTML = '<h4>路径: ' + esc(r.path.join(' → ')) +
        '</h4><pre>' + esc(r.bfs.map(e => '(' + e.src + ')-[:' + e.type + ' ' +
        JSON.stringify(e.props) + ']->(' + e.dst + ')').join('\n')) + '</pre>' +
        '<h4>子图摘要（最终问题只查摘要，不回顾原文）</h4><pre>' + esc(r.summary) + '</pre>';
    } catch (e) { $('#g-out').innerHTML = '<div class="err">' + esc(e.message) + '</div>'; }
  }

  async function memBucketDemo() {
    try {
      const dims = ['date', 'entity', 'topic', 'process'];
      let out = '';
      for (const d of dims) {
        const r = await api('/api/memory/l2/bucket?node=' + encodeURIComponent(_bucketNode) + '&dim=' + d);
        out += '【' + d + ' 桶】' + JSON.stringify(r.records) + '\n';
      }
      $('#g-out').innerHTML = '<pre>' + esc(out) +
        '\nB+ 树正确性: 按 key 有序插入 → 中序遍历有序（tests 断言）；按日期 2026-09-09 检索只返回当日条目。</pre>';
    } catch (e) { $('#g-out').innerHTML = '<div class="err">' + esc(e.message) + '</div>'; }
  }

  // 暴露给 onclick
  window.loadMemory = loadMemory;
  window.memTab = memTab; window.memFilterList = memFilterList;
  window.memL0Page = memL0Page; window.memL0Limit = memL0Limit;
  window.memJumpNode = memJumpNode;
  window.memBucketChange = memBucketChange;
  window.memWalkGraph = memWalkGraph; window.memBucketDemo = memBucketDemo;
})();
