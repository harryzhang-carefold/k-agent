/* config.js — "模型节点" 配置页（TASK-023 / F3 前端；TASK-029 需求1 改造）
 * 复用 app.js 全局 api()/esc()/$ 与 RBAC 守卫。
 *
 * LLM 区（TASK-029）：
 *   1. "系统默认 endpoint" 卡片（原单 LLM 卡片，保留）——编辑 S.LLM_* 单值，
 *      保存后后端自动同步 system-default 行（兜底/默认选中项）。
 *   2. "Endpoint 列表" 面板（新增）——多 endpoint 维护：
 *      新建 / 编辑 / 删除 / 测试连接 / 启用停用 / 设为默认，
 *      api_key 只回 key_set + 末 4 位（永不回明文，DECISION-004）。
 * Embedding 卡片不动（TASK-023 原样）。
 *
 * 权限：GET 登录即可；写操作（POST/PUT/DELETE/set-default/test）需 system:admin
 * （无权限时写入/测试入口禁用，后端同样 403）。
 */
(function () {
  'use strict';

  function canAdmin() { return !!(me && me.permissions && me.permissions.includes('system:admin')); }
  let _cfg = {};        // GET /api/system/config 结果
  let _eps = [];        // GET /api/llm-endpoints 结果
  let _saved = '';      // 保存状态提示
  let _editing = null;  // 正在编辑的 endpoint name（null = 新建）

  async function loadConfig() {
    const [cfg, epj] = await Promise.all([
      api('/api/system/config'),
      api('/api/llm-endpoints').catch(() => ({ endpoints: [], default_name: 'system-default' })),
    ]);
    _cfg = cfg;
    _eps = epj.endpoints || [];
    _saved = '';
    _editing = null;
    const A = canAdmin();
    const s = _cfg.settings || {};
    $('#page-model').innerHTML =
      '<h2>模型节点（LLM / Embedding 底座维护）</h2>' +
      '<div class="k" style="margin-bottom:14px">' +
        (A
          ? '✓ 你有 <span class="tag acc">system:admin</span> 权限：可保存（热更+落库，重启不丢）与测试连接。'
          : '只读模式：你当前没有 <span class="tag warn">system:admin</span> 权限，保存/测试入口已禁用（后端同样 403）。' +
            ' api_key 永不回传明文，仅显示是否已配置 + 末 4 位。') +
      '</div>' +
      '<div class="row">' +
        _llmCard(s, A) +
        _embCard(s, A) +
      '</div>' +
      _endpointList(A) +
      (A ? '<div class="panel" style="margin-top:16px">' +
        '<div class="row" style="align-items:center">' +
          '<button class="primary" style="width:auto;padding:11px 26px" onclick="cfgSave()">💾 保存（热更新 + 落库持久化）</button>' +
          '<span class="k">保存后即时生效；重启不丢（settings 表，DB&gt;.env 优先级）。保存 LLM 字段会同步"系统默认 endpoint"。</span>' +
          '<span id="cfg-saved" class="k"></span></div>' +
        '<div id="cfg-saved-msg" class="err"></div></div>' : '') +
      '<div class="panel"><h3>说明</h3>' +
      '<div class="k">· api_key 写库但 GET 永不回传明文（只回 key_set + 末 4 位）。</div>' +
      '<div class="k">· 留空 api_key 输入框 = 不改动已存 key；填写新值 = 覆盖。</div>' +
      '<div class="k">· "测试连接"用当前表单值（留空则用已保存值）发起真实连通请求，失败不 500。</div>' +
      '<div class="k">· embedding 留空 base_url 时走本地确定性哈希向量（DECISION-003）。</div>' +
      '<div class="k">· Agent 构建器"模型"下拉列出已启用（is_active）的 endpoint；' +
        '旧 agent 的自由文本模型匹配不到 endpoint 时回退系统默认（不报错）。</div>' +
      '<div class="k">· 系统默认 endpoint（system-default）= S.LLM_* 单值快照，兜底/默认选中，不可删除。</div></div>';
  }

  function _keyInd(k) {
    // k = {key_set, key_tail}
    const set = k && k.key_set;
    return set
      ? '<span class="tag ok">✓ 已配置 · 末4位 ' + esc(k.key_tail || '') + '</span>'
      : '<span class="tag warn">✗ 未配置</span>';
  }

  function _llmCard(s, A) {
    return '<div class="panel" style="flex:1;min-width:320px"><h3>LLM 底座（OpenAI 兼容 · 系统默认）</h3>' +
      '<div class="k" style="margin-bottom:8px">此卡片维护 <span class="tag acc">系统默认 endpoint</span>（system-default，' +
      'S.LLM_* 单值）：保存后同步到 endpoint 列表，Agent 未匹配到其它 endpoint 时回退它。</div>' +
      '<label>base_url</label>' +
      '<input id="cfg-llm-base" class="mono" value="' + esc(s.llm_base_url || '') + '">' +
      '<label>model</label>' +
      '<input id="cfg-llm-model" value="' + esc(s.llm_model || '') + '">' +
      '<label>api_key（' + _keyInd(s.llm_api_key) + '）</label>' +
      '<input id="cfg-llm-key" type="password" placeholder="' + (A ? '留空 = 不改动；填写 = 覆盖' : '（只读）') +
        '" ' + (A ? '' : 'disabled') + ' autocomplete="new-password">' +
      '<div class="row">' +
        '<div><label>timeout（秒）</label><input id="cfg-llm-timeout" type="number" step="0.1" value="' + (s.llm_timeout != null ? s.llm_timeout : 90) + '"></div>' +
        '<div><label>retries</label><input id="cfg-llm-retries" type="number" value="' + (s.llm_retries != null ? s.llm_retries : 3) + '"></div>' +
      '</div>' +
      (A ? '<button class="small" onclick="cfgTest(\'llm\')">▶ 测试连接</button>' : '') +
      '<div id="cfg-test-llm" class="k" style="margin-top:8px"></div></div>';
  }

  function _embCard(s, A) {
    return '<div class="panel" style="flex:1;min-width:320px"><h3>Embedding 底座（OpenAI 兼容，可空 → 本地哈希）</h3>' +
      '<label>base_url</label>' +
      '<input id="cfg-emb-base" class="mono" value="' + esc(s.embedding_base_url || '') + '"><div class="k">留空 = 本地确定性哈希向量（dim 仍生效）</div>' +
      '<label>model</label>' +
      '<input id="cfg-emb-model" value="' + esc(s.embedding_model || '') + '">' +
      '<label>api_key（' + _keyInd(s.embedding_api_key) + '）</label>' +
      '<input id="cfg-emb-key" type="password" placeholder="' + (A ? '留空 = 不改动；填写 = 覆盖' : '（只读）') +
        '" ' + (A ? '' : 'disabled') + ' autocomplete="new-password">' +
      '<label>dim（向量维度）</label>' +
      '<input id="cfg-emb-dim" type="number" value="' + (s.embedding_dim != null ? s.embedding_dim : 512) + '">' +
      (A ? '<button class="small" onclick="cfgTest(\'embedding\')">▶ 测试连接</button>' : '') +
      '<div id="cfg-test-emb" class="k" style="margin-top:8px"></div></div>';
  }

  // ---------------- endpoint 列表（TASK-029 需求1） ----------------
  function _endpointList(A) {
    const rows = _eps.map(e =>
      '<tr>' +
      '<td>' + esc(e.name) +
        (e.is_system_default ? '<div class="k"><span class="tag acc">系统默认</span></div>' : '') +
        (e.is_active ? '<div class="k"><span class="tag ok">启用</span></div>' : '<div class="k"><span class="tag warn">停用</span></div>') +
      '</td>' +
      '<td class="mono">' + esc(e.base_url) + '</td>' +
      '<td class="mono">' + esc(e.model) + '</td>' +
      '<td>' + _keyInd(e) + '</td>' +
      '<td class="k">t=' + e.timeout + ' · r=' + e.retries + '</td>' +
      '<td>' +
        (A
          ? '<button class="small" onclick="epEdit(\'' + esc(e.name) + '\')">编辑</button> ' +
            '<button class="small" onclick="epToggle(\'' + esc(e.name) + '\')">' + (e.is_active ? '停用' : '启用') + '</button> ' +
            '<button class="small" onclick="epTest(\'' + esc(e.name) + '\')">测试</button>' +
            (e.is_system_default
              ? ''
              : '<button class="small" onclick="epSetDefault(\'' + esc(e.name) + '\')">设为默认</button> ' +
                '<button class="small" onclick="epDel(\'' + esc(e.name) + '\')">删除</button>')
          : '<span class="k">只读</span>') +
      '</td></tr>').join('');
    return '<div class="panel"><h3>LLM Endpoint 列表（Agent 模型下拉来源）</h3>' +
      (A
        ? '<div style="margin-bottom:10px"><button class="small" onclick="epNew()">＋ 新建 endpoint</button>' +
          '<span class="k" style="margin-left:10px">启用（is_active）的 endpoint 才会出现在 Agent 构建器"模型"下拉</span></div>'
        : '<div class="k" style="margin-bottom:10px">只读模式（无 system:admin 权限）</div>') +
      '<table><tr><th>名称</th><th>base_url</th><th>model</th><th>api_key</th><th>参数</th><th>操作</th></tr>' +
      (rows || '<tr><td colspan="6" class="k">（暂无 endpoint）</td></tr>') + '</table>' +
      '<div id="ep-form"></div>' +
      '<div id="ep-test-out" class="k" style="margin-top:8px"></div>' +
      '<div id="ep-msg" class="err" style="margin-top:8px"></div></div>';
  }

  function _epForm(e, isNew) {
    const A = canAdmin();
    const v = e || {};
    return '<div style="margin-top:12px;border-top:1px dashed #999;padding-top:12px">' +
      '<h4>' + (isNew ? '新建 endpoint' : '编辑 endpoint：' + esc(v.name || '')) + '</h4>' +
      '<div class="row">' +
      '<div><label>名称（唯一，Agent 绑定标识）</label>' +
      '<input id="ep-name" class="mono" value="' + esc(v.name || '') + '" ' + (isNew ? '' : 'disabled') +
      ' placeholder="如 gpt-4o / vllm-local">' + '</div>' +
      '<div><label>base_url</label>' +
      '<input id="ep-base" class="mono" value="' + esc(v.base_url || '') + '" placeholder="http://host:port/v1"></div>' +
      '<div><label>model</label>' +
      '<input id="ep-model" value="' + esc(v.model || '') + '" placeholder="模型标识"></div>' +
      '<div><label>api_key（' + _keyInd(v) + '）</label>' +
      '<input id="ep-key" type="password" placeholder="' + (isNew ? '可选（留空 = 用共享 key）' : '留空 = 不改动；填写 = 覆盖') +
      '" autocomplete="new-password"></div></div>' +
      '<div class="row">' +
      '<div><label>timeout（秒）</label><input id="ep-timeout" type="number" step="0.1" value="' + (v.timeout != null ? v.timeout : 90) + '"></div>' +
      '<div><label>retries</label><input id="ep-retries" type="number" value="' + (v.retries != null ? v.retries : 3) + '"></div>' +
      '<div><label>is_active（启用 = 进入 Agent 下拉）</label>' +
      '<input id="ep-active" type="checkbox" ' + (v.is_active ? 'checked' : '') + '></div></div>' +
      '<div style="margin-top:10px">' +
      '<button class="primary small" style="padding:8px 20px" onclick="epSave()">保存</button> ' +
      '<button class="small" onclick="epTestForm()">▶ 测试连接（当前表单值）</button> ' +
      '<button class="small" onclick="epCancel()">取消</button></div></div>';
  }

  function epNew() {
    _editing = null;
    $('#ep-form').innerHTML = _epForm(null, true);
    $('#ep-msg').textContent = '';
  }
  function epEdit(name) {
    const e = _eps.find(x => x.name === name);
    if (!e) return;
    _editing = name;
    $('#ep-form').innerHTML = _epForm(e, false);
    $('#ep-msg').textContent = '';
  }
  function epCancel() { _editing = null; $('#ep-form').innerHTML = ''; }

  async function epSave() {
    const body = {
      base_url: $('#ep-base').value.trim(),
      model: $('#ep-model').value.trim(),
      timeout: parseFloat($('#ep-timeout').value),
      retries: parseInt($('#ep-retries').value),
      is_active: $('#ep-active').checked,
    };
    const key = $('#ep-key').value;
    if (key !== '') body.api_key = key;
    if (_editing) {
      try {
        await api('/api/llm-endpoints/' + encodeURIComponent(_editing),
                  { method: 'PUT', body: JSON.stringify(body) });
      } catch (e) { $('#ep-msg').textContent = e.message; return; }
    } else {
      if (!$('#ep-name').value.trim()) { $('#ep-msg').textContent = '名称必填'; return; }
      body.name = $('#ep-name').value.trim();
      try {
        await api('/api/llm-endpoints', { method: 'POST', body: JSON.stringify(body) });
      } catch (e) { $('#ep-msg').textContent = e.message; return; }
    }
    _editing = null;
    await loadConfig();
  }

  async function epDel(name) {
    if (!confirm('删除 endpoint ' + name + '？（引用它的 Agent 将回退系统默认）')) return;
    try {
      await api('/api/llm-endpoints/' + encodeURIComponent(name), { method: 'DELETE' });
      await loadConfig();
    } catch (e) { $('#ep-msg').textContent = e.message; }
  }

  async function epToggle(name) {
    const e = _eps.find(x => x.name === name);
    if (!e) return;
    try {
      await api('/api/llm-endpoints/' + encodeURIComponent(name),
                { method: 'PUT', body: JSON.stringify({ is_active: !e.is_active }) });
      await loadConfig();
    } catch (err) { $('#ep-msg').textContent = err.message; }
  }

  async function epSetDefault(name) {
    if (!confirm('将 ' + name + ' 设为系统默认？\n' +
      '其 base_url/model/api_key/timeout/retries 将同步到 S.LLM_*（热更+落库），' +
      'system-default 行同步更新，Agent 未匹配到 endpoint 时回退它。')) return;
    try {
      const r = await api('/api/llm-endpoints/' + encodeURIComponent(name) + '/set-default',
                          { method: 'POST' });
      alert('已设为默认：' + (r && r.llm ? r.llm.base_url + ' / ' + r.llm.model : ''));
      await loadConfig();
    } catch (e) { $('#ep-msg').textContent = e.message; }
  }

  async function epTest(name) {
    const out = $('#ep-test-out');
    out.innerHTML = '<span class="k">测试中…（' + esc(name) + '，真实连通请求，上限 15s）</span>';
    try {
      const r = await api('/api/llm-endpoints/test',
                          { method: 'POST', body: JSON.stringify({ name }) });
      out.innerHTML = epTestHtml(r);
    } catch (e) {
      out.innerHTML = '<span class="tag warn">✗ 测试异常</span> <div class="err">' + esc(e.message) + '</div>';
    }
  }

  async function epTestForm() {
    const out = $('#ep-test-out');
    const base = $('#ep-base').value.trim();
    const key = $('#ep-key').value;
    if (!base) { out.innerHTML = '<span class="tag warn">✗ 先填 base_url</span>'; return; }
    out.innerHTML = '<span class="k">测试中…（当前表单值，上限 15s）</span>';
    const body = { base_url: base };
    if (key) body.api_key = key;
    try {
      const r = await api('/api/llm-endpoints/test',
                          { method: 'POST', body: JSON.stringify(body) });
      out.innerHTML = epTestHtml(r);
    } catch (e) {
      out.innerHTML = '<span class="tag warn">✗ 测试异常</span> <div class="err">' + esc(e.message) + '</div>';
    }
  }

  function epTestHtml(r) {
    return r.ok
      ? '<span class="tag ok">✓ 连接成功</span> <span class="k">延迟 ' + r.latency_ms + ' ms</span> ' +
        '<div class="k" style="margin-top:4px">' + esc(r.detail || '') + '</div>'
      : '<span class="tag warn">✗ 连接失败</span> <span class="k">延迟 ' + r.latency_ms + ' ms</span> ' +
        '<div class="err" style="margin-top:4px">' + esc(r.detail || '') + '</div>';
  }

  async function cfgTest(target) {
    const out = $(target === 'llm' ? '#cfg-test-llm' : '#cfg-test-emb');
    out.innerHTML = '<span class="k">测试中…（真实连通请求，上限 15s）</span>';
    const body = { target };
    if (target === 'llm') {
      const b = $('#cfg-llm-base').value.trim();
      const m = $('#cfg-llm-model').value.trim();
      const k = $('#cfg-llm-key').value;
      if (b) body.base_url = b;
      if (m) body.model = m;
      if (k) body.api_key = k;
    } else {
      const b = $('#cfg-emb-base').value.trim();
      const m = $('#cfg-emb-model').value.trim();
      const k = $('#cfg-emb-key').value;
      if (b) body.base_url = b;
      if (m) body.model = m;
      if (k) body.api_key = k;
    }
    try {
      const r = await api('/api/system/config/test', { method: 'POST', body: JSON.stringify(body) });
      out.innerHTML = r.ok
        ? '<span class="tag ok">✓ 连接成功</span> <span class="k">延迟 ' + r.latency_ms + ' ms</span> ' +
          '<div class="k" style="margin-top:4px">' + esc(r.detail || '') + '</div>'
        : '<span class="tag warn">✗ 连接失败</span> <span class="k">延迟 ' + r.latency_ms + ' ms</span> ' +
          '<div class="err" style="margin-top:4px">' + esc(r.detail || '') + '</div>';
    } catch (e) {
      out.innerHTML = '<span class="tag warn">✗ 测试异常</span> <div class="err" style="margin-top:4px">' + esc(e.message) + '</div>';
    }
  }

  async function cfgSave() {
    const s = (_cfg && _cfg.settings) || {};
    const body = {};
    const orig = {
      llm_base_url: s.llm_base_url || '', llm_model: s.llm_model || '',
      llm_timeout: s.llm_timeout, llm_retries: s.llm_retries,
      embedding_base_url: s.embedding_base_url || '', embedding_model: s.embedding_model || '',
      embedding_dim: s.embedding_dim,
    };
    // LLM：仅发送与已存值不同的字段（api_key 留空 = 不改动）
    const lb = $('#cfg-llm-base').value.trim();
    const lm = $('#cfg-llm-model').value.trim();
    const lk = $('#cfg-llm-key').value;
    const lt = parseFloat($('#cfg-llm-timeout').value);
    const lr = parseInt($('#cfg-llm-retries').value);
    if (lb !== orig.llm_base_url) body.llm_base_url = lb;
    if (lm !== orig.llm_model) body.llm_model = lm;
    if (lk !== '') body.llm_api_key = lk;
    if (!isNaN(lt) && lt > 0 && lt !== orig.llm_timeout) body.llm_timeout = lt;
    if (!isNaN(lr) && lr > 0 && lr !== orig.llm_retries) body.llm_retries = lr;
    // Embedding：允许清空 base_url（= 本地哈希），按"是否改变"判断
    const eb = $('#cfg-emb-base').value.trim();
    const em = $('#cfg-emb-model').value.trim();
    const ek = $('#cfg-emb-key').value;
    const ed = parseInt($('#cfg-emb-dim').value);
    if (eb !== orig.embedding_base_url) body.embedding_base_url = eb;
    if (em !== orig.embedding_model) body.embedding_model = em;
    if (ek !== '') body.embedding_api_key = ek;
    if (!isNaN(ed) && ed > 0 && ed !== orig.embedding_dim) body.embedding_dim = ed;

    if (Object.keys(body).length === 0) {
      $('#cfg-saved-msg').textContent = '没有可保存的变更（字段与已存值相同，api_key 留空=不改动）';
      return;
    }
    try {
      const r = await api('/api/system/config', { method: 'POST', body: JSON.stringify(body) });
      $('#cfg-saved').textContent = '✓ 已保存：' + (r.updated || []).join(', ');
      $('#cfg-saved-msg').textContent = '';
      // 刷新脱敏回显（key_set/末4位 + 各字段现值 + endpoint 列表同步）
      await loadConfig();
    } catch (e) {
      $('#cfg-saved-msg').textContent = e.message;
    }
  }

  // 暴露给 onclick
  window.loadConfig = loadConfig;
  window.cfgTest = cfgTest;
  window.cfgSave = cfgSave;
  window.epNew = epNew;
  window.epEdit = epEdit;
  window.epSave = epSave;
  window.epCancel = epCancel;
  window.epDel = epDel;
  window.epToggle = epToggle;
  window.epSetDefault = epSetDefault;
  window.epTest = epTest;
  window.epTestForm = epTestForm;
})();
