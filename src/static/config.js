/* config.js — "模型节点" 配置页（TASK-023 / F3 前端）
 * 复用 app.js 全局 api()/esc()/$ 与 RBAC 守卫。
 *
 * LLM 卡片 + Embedding 卡片，字段表单（base_url/model/api_key/timeout/dim/retries）
 * + key_set 指示（只回 key_set + 末 4 位）+ "测试连接"按钮（调 /api/system/config/test，
 * 实时显示延迟/错误）+ 保存（POST /api/system/config）。
 *
 * 权限：GET 登录即可；POST 写 与 test 需 system:admin（无权限时写入/测试入口禁用）。
 */
(function () {
  'use strict';

  function canAdmin() { return !!(me && me.permissions && me.permissions.includes('system:admin')); }
  let _cfg = {};        // GET /api/system/config 结果
  let _saved = '';      // 保存状态提示

  async function loadConfig() {
    _cfg = await api('/api/system/config');
    _saved = '';
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
      (A ? '<div class="panel" style="margin-top:16px">' +
        '<div class="row" style="align-items:center">' +
          '<button class="primary" style="width:auto;padding:11px 26px" onclick="cfgSave()">💾 保存（热更新 + 落库持久化）</button>' +
          '<span class="k">保存后即时生效；重启不丢（settings 表，DB&gt;.env 优先级）</span>' +
          '<span id="cfg-saved" class="k"></span></div>' +
        '<div id="cfg-saved-msg" class="err"></div></div>' : '') +
      '<div class="panel"><h3>说明</h3>' +
      '<div class="k">· api_key 写库但 GET 永不回传明文（只回 key_set + 末 4 位）。</div>' +
      '<div class="k">· 留空 api_key 输入框 = 不改动已存 key；填写新值 = 覆盖。</div>' +
      '<div class="k">· "测试连接"用当前表单值（留空则用已保存值）发起真实连通请求，失败不 500。</div>' +
      '<div class="k">· embedding 留空 base_url 时走本地确定性哈希向量（DECISION-003）。</div></div>';
  }

  function _keyInd(k) {
    // k = {key_set, key_tail}
    const set = k && k.key_set;
    return set
      ? '<span class="tag ok">✓ 已配置 · 末4位 ' + esc(k.key_tail || '') + '</span>'
      : '<span class="tag warn">✗ 未配置</span>';
  }

  function _llmCard(s, A) {
    return '<div class="panel" style="flex:1;min-width:320px"><h3>LLM 底座（OpenAI 兼容）</h3>' +
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
      // 刷新脱敏回显（key_set/末4位 + 各字段现值）
      await loadConfig();
    } catch (e) {
      $('#cfg-saved-msg').textContent = e.message;
    }
  }

  // 暴露给 onclick
  window.loadConfig = loadConfig;
  window.cfgTest = cfgTest;
  window.cfgSave = cfgSave;
})();
