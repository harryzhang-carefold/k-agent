/* ext.js — MCP-Skills-Plugins 页面（TASK-023 / F1 前端）
 * 复用 app.js 的全局 api()/esc()/$ 与 RBAC 守卫。
 *
 * 职责：
 *   - Skills：列表(名称/描述/长度/更新时间, 可搜索) + 新建/编辑 + 删除(确认) + 内容查看/编辑 + 上传导入(.md/.txt/zip)
 *   - MCP   ：列表(enabled 开关) + 新建/编辑(command/args/env 键值对) + 删除 + 保留 tools/list 与 call 测试
 *   - Plugins / 长文本4策略 / HITL：保持原有只读能力
 *
 * 权限：无 ext:manage 时写入入口禁用（canManage() 守卫）。
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
      '<div class="panel"><h3>MCP Servers（stdio JSON-RPC · 内置 demo 真实进程）</h3>' +
        renderMcpPanel(M) + '</div>' +
      '<div class="panel"><h3>Plugins（内置可调用能力 · 只读）</h3>' +
        (pl.plugins || []).map(p =>
          '<div class="card" style="margin-bottom:8px"><strong>' + esc(p.name) +
          '</strong> <div class="k">' + esc(p.description) + '</div>' +
          '<button class="small" onclick="extCallPlugin(\'' + p.name + '\')">调用</button>' +
          '<pre id="pl-' + p.name + '"></pre></div>').join('') + '</div>' +
      renderLongtextPanel(lt);
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
      '<div id="sk-upload-res"></div></div>';
  }
  function extCloseUpload() { $('#sk-upload').classList.add('hidden'); $('#sk-upload').innerHTML = ''; }

  async function extDoUpload() {
    const fileInput = $('#sk-file');
    const files = fileInput.files;
    if (!files || !files.length) { $('#sk-upload-res').innerHTML = '<div class="err">请选择至少一个文件</div>'; return; }
    const fd = new FormData();
    for (const f of files) fd.append('files', f);
    const res = $('#sk-upload-res');
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
    const det = $('#sk-upload-res');
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
  function renderMcpPanel(M) {
    return (M ?
      '<div id="mcp-form" class="hidden"></div>' +
      '<div class="k" style="margin:6px 0">共 ' + _mcps.length + ' 个 server</div>' :
      '<div class="k" style="margin:6px 0">共 ' + _mcps.length + ' 个 server（只读）</div>') +
      (_mcps.length
        ? '<table><tr><th>名称</th><th>命令</th><th>env</th><th>状态</th><th>操作</th><th>tools/call 输出</th></tr>' +
          _mcps.map(s => {
            const args = (s.args || []).join(' ');
            return '<tr><td><strong>' + esc(s.name) + '</strong><div class="k mono">#' + s.id + '</div></td>' +
              '<td class="k mono">' + esc(s.command) + (args ? ' ' + esc(args) : '') + '</td>' +
              '<td class="k">' + ((s.env && Object.keys(s.env).length) ? Object.keys(s.env).length + ' 项' : '—') + '</td>' +
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
          '</table><div class="k">说明：MCP 测试按钮会真实启动 stdio 子进程（内置 demo）。' + (M ? ' 编辑表单需回传完整 name/command/args/env/enabled（后端全量覆盖）。' : '') + '</div>'
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
    // PUT 是全量覆盖，需回传完整对象（仅改 enabled）
    const body = { name: s.name, command: s.command, args: s.args || [], env: s.env || {}, enabled };
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

  function _showMcpForm() {
    const s = _mcpEditId ? _mcps.find(x => x.id === _mcpEditId) : null;
    const f = $('#mcp-form');
    f.classList.remove('hidden');
    // env 键值对行
    const env = s ? (s.env || {}) : {};
    const envRows = Object.entries(env).map(([k, v], i) =>
      _envRow(k, v)).join('') || _envRow('', '');
    f.innerHTML =
      '<div class="panel" style="background:var(--panel2);margin-top:10px">' +
      '<h4>' + (s ? '编辑 MCP Server #' + s.id : '新建 MCP Server') + '</h4>' +
      '<div class="row">' +
        '<div><label>名称（必填，唯一）</label><input id="mcpf-name" value="' + (s ? esc(s.name) : '') + '"></div>' +
        '<div><label>命令 command（必填）</label><input id="mcpf-cmd" value="' + (s ? esc(s.command) : '') + '" placeholder="python 或 node"></div>' +
      '</div>' +
      '<label>args（空格分隔，或 JSON 数组）</label>' +
      '<input id="mcpf-args" value="' + (s ? esc((s.args || []).join(' ')) : '') + '" placeholder="脚本路径 --flag（多个空格分隔）">' +
      '<label>env（键值对，动态增删）</label>' +
      '<div id="mcpf-env">' + envRows + '</div>' +
      '<button class="small" onclick="extAddEnvRow()">＋ 加一行 env</button>' +
      '<div class="row" style="margin-top:10px;align-items:center">' +
        '<div style="flex:0 0 auto"><label style="display:inline-flex;gap:6px;align-items:center;margin:0">' +
          '<input type="checkbox" id="mcpf-enabled" ' + (s && !s.enabled ? '' : 'checked') + '> enabled</label></div></div>' +
      '<div style="margin-top:10px">' +
        '<button class="primary" style="width:auto;padding:9px 22px" onclick="extSaveMcp()">' + (s ? '保存' : '创建') + '</button>' +
        '<button class="small" onclick="extCloseMcpForm()">取消</button>' +
        '<span id="mcpf-err" class="err"></span></div></div>';
    f.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function _envRow(k, v) {
    return '<div class="row mcpf-env-row" style="margin-bottom:6px">' +
      '<div><input class="mcpf-env-k" placeholder="KEY" value="' + esc(k) + '"></div>' +
      '<div><input class="mcpf-env-v" placeholder="value" value="' + esc(v) + '"></div>' +
      '<div style="flex:0 0 auto;align-self:flex-end"><button class="small" onclick="this.closest(\'.mcpf-env-row\').remove()">删</button></div>' +
      '</div>';
  }
  function extAddEnvRow() { $('#mcpf-env').insertAdjacentHTML('beforeend', _envRow('', '')); }

  function extCloseMcpForm() { $('#mcp-form').classList.add('hidden'); $('#mcp-form').innerHTML = ''; }

  async function extSaveMcp() {
    const name = $('#mcpf-name').value.trim();
    const command = $('#mcpf-cmd').value.trim();
    const argsRaw = $('#mcpf-args').value.trim();
    let args;
    if (argsRaw) {
      // 支持 JSON 数组或空格分隔
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
    const enabled = $('#mcpf-enabled').checked;
    if (!name) { $('#mcpf-err').textContent = '名称必填'; return; }
    if (!command) { $('#mcpf-err').textContent = '命令 command 必填'; return; }
    const body = { name, command, args, env, enabled };
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
    return '<div class="panel"><h3>长文本 4 策略</h3>' +
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
  window.extCloseMcpForm = extCloseMcpForm;
  window.extCallPlugin = extCallPlugin; window.extLtRun = extLtRun; window.extLtPreprocess = extLtPreprocess;
})();
