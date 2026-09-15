/* graph3d.js — 纯 JS 零框架 3D 力导向立体图引擎（DECISION-001，离线可用，无 CDN）
 *
 * 用 canvas 2D 把节点 3D 坐标透视投影到 2D 平面渲染。力导向布局在 3D 空间做
 * （Fruchterman-Reingold：节点互斥 + 边弹簧 + 中心引力 + 温度退火）。
 *
 * 交互：
 *   - 拖拽 → 旋转（绕 Y/X 轴）
 *   - 滚轮 → 缩放
 *   - 悬停 → 高亮节点及其邻接边
 *   - 点击 → onNodeClick(node)
 *
 * 用法：
 *   const g = new Graph3D(containerEl, {
 *     nodes: [{id,label,props}, ...],
 *     edges: [{src,dst,type,props}, ...],
 *     height: 520,
 *     onNodeClick: (n) => {...},
 *     onHover: (nOrNull) => {...},
 *   });
 *   g.destroy();
 *   g.setNodes(nodes, edges);   // 重新布局（可选）
 */
(function () {
  'use strict';

  function rand(a, b) { return a + Math.random() * (b - a); }

  // 按 label 分配稳定颜色（不同 label 不同色，便于医疗图区分 Patient/Disease/...）
  const LABEL_COLORS = {};
  const PALETTE = ['#4f8cff', '#38c172', '#e6b422', '#e05c5c', '#b07cf0',
                   '#4fd0c8', '#f0934f', '#7c9cf0', '#5fc98a', '#d07cb0'];
  function colorForLabel(label) {
    if (!(label in LABEL_COLORS)) {
      let h = 0;
      for (let i = 0; i < label.length; i++) h = (h * 31 + label.charCodeAt(i)) >>> 0;
      LABEL_COLORS[label] = PALETTE[h % PALETTE.length];
    }
    return LABEL_COLORS[label];
  }

  class Graph3D {
    constructor(container, opts) {
      this.container = container;
      this.height = opts.height || 520;
      this.onNodeClick = opts.onNodeClick || function () {};
      this.onHover = opts.onHover || function () {};
      this.edgeLabel = !!opts.edgeLabel; // 是否在边上画 type

      // DOM：canvas + 说明条
      container.innerHTML =
        '<div class="g3d-wrap">' +
        '<canvas class="g3d-canvas"></canvas>' +
        '<div class="g3d-hint">拖拽旋转 · 滚轮缩放 · 悬停高亮 · 点击看子图</div>' +
        '</div>';
      this.canvas = container.querySelector('.g3d-canvas');
      this.ctx = this.canvas.getContext('2d');

      this.dpr = window.devicePixelRatio || 1;
      this.rotX = 0.35; this.rotY = 0.6;      // 初始视角
      this.zoom = 1; this.fov = 900;           // 透视焦距
      this.camDist = 0;
      this.running = true;
      this.alpha = 1.0;                        // 温度（退火）
      this.hover = null; this.selected = null;

      this._drag = null; this._moved = 0;
      this._bind();
      this.setNodes(opts.nodes || [], opts.edges || []);
      this._loop();
    }

    _size() {
      const w = Math.max(320, this.container.clientWidth || 720);
      const h = this.height;
      this.canvas.width = w * this.dpr;
      this.canvas.height = h * this.dpr;
      this.canvas.style.width = w + 'px';
      this.canvas.style.height = h + 'px';
      this.W = w; this.H = h;
    }

    _bind() {
      const c = this.canvas;
      c.addEventListener('mousedown', (e) => {
        this._drag = { x: e.clientX, y: e.clientY, rx: this.rotX, ry: this.rotY };
        this._moved = 0;
        e.preventDefault();
      });
      window.addEventListener('mousemove', (e) => { this._onMove(e); });
      window.addEventListener('mouseup', (e) => { this._onUp(e); });
      c.addEventListener('wheel', (e) => {
        e.preventDefault();
        const f = e.deltaY < 0 ? 1.1 : 0.9;
        this.zoom = Math.min(4, Math.max(0.3, this.zoom * f));
      }, { passive: false });
      c.addEventListener('mouseleave', () => { this._setHover(null); });
      window.addEventListener('resize', () => { this._size(); });
    }

    _onMove(e) {
      if (this._drag) {
        const dx = e.clientX - this._drag.x, dy = e.clientY - this._drag.y;
        this._moved += Math.abs(dx) + Math.abs(dy);
        this.rotY = this._drag.ry + dx * 0.008;
        this.rotX = this._drag.rx + dy * 0.008;
        this.rotX = Math.min(1.5, Math.max(-1.5, this.rotX));
      } else {
        const rect = this.canvas.getBoundingClientRect();
        this._setHover(this._pick(e.clientX - rect.left, e.clientY - rect.top));
      }
    }

    _onUp(e) {
      if (this._drag) {
        const wasDrag = this._moved > 6;
        this._drag = null;
        if (!wasDrag) {
          const rect = this.canvas.getBoundingClientRect();
          const n = this._pick(e.clientX - rect.left, e.clientY - rect.top);
          if (n) { this.selected = n.id; this.onNodeClick(n); }
        }
      }
    }

    _setHover(n) {
      const id = n ? n.id : null;
      if ((this.hover && this.hover.id) !== id) {
        this.hover = n;
        this.canvas.style.cursor = n ? 'pointer' : 'grab';
        this.onHover(n);
      }
    }

    // 3D -> 屏幕投影（返回 {sx,sy,scale,z}，z 为相机深度用于排序）
    _project(p) {
      // 绕 Y 轴
      const cy = Math.cos(this.rotY), sy = Math.sin(this.rotY);
      let x = p.x * cy - p.z * sy, z = p.x * sy + p.z * cy, y = p.y;
      // 绕 X 轴
      const cx = Math.cos(this.rotX), sx = Math.sin(this.rotX);
      let y2 = y * cx - z * sx, z2 = y * sx + z * cx;
      const depth = z2 + this.fov;               // 相机在 -z 方向
      const scale = (this.fov / Math.max(1, depth)) * this.zoom;
      return { sx: this.W / 2 + x * scale, sy: this.H / 2 + y2 * scale,
               scale, z: z2 };
    }

    _pick(sx, sy) {
      let best = null, bestZ = Infinity;
      for (const n of this._screenNodes) {
        const dx = sx - n.sx, dy = sy - n.sy;
        // 与绘制一致的屏幕半径（见 _draw 节点循环）+ 4px 命中余量
        const r = Math.max(6, (n.node.r || 6) * n.scale) + 4;
        if (dx * dx + dy * dy <= r * r && n.z < bestZ) { best = n.node; bestZ = n.z; }
      }
      return best;
    }

    setNodes(nodes, edges) {
      this.nodes = nodes.map((n) => ({
        id: n.id, label: n.label, props: n.props || {},
        x: rand(-60, 60), y: rand(-60, 60), z: rand(-60, 60),
        vx: 0, vy: 0, vz: 0, r: 9,
      }));
      this._byId = {};
      this.nodes.forEach((n) => { this._byId[n.id] = n; });
      this.edges = (edges || []).filter((e) => this._byId[e.src] && this._byId[e.dst])
        .map((e) => ({ src: e.src, dst: e.dst, type: e.type, props: e.props || {} }));
      // 邻接表（用于高亮 + 度数）
      this._adj = {};
      this.nodes.forEach((n) => { this._adj[n.id] = new Set(); });
      this.edges.forEach((e) => {
        this._adj[e.src].add(e.dst); this._adj[e.dst].add(e.src);
      });
      this._degree = {};
      this.nodes.forEach((n) => { this._degree[n.id] = this._adj[n.id].size; });
      // 节点半径按度数
      this.nodes.forEach((n) => { n.r = 8 + Math.min(10, this._degree[n.id] * 1.6); });
      // 理想边长
      const area = 220 * 220;
      this._k = Math.sqrt(area / Math.max(1, this.nodes.length));
      this.alpha = 1.0;
      this._size();
    }

    _step() {
      if (this.alpha < 0.02) return;            // 已收敛，停止模拟
      const nodes = this.nodes, edges = this.edges, k = this._k;
      const disp = {}; nodes.forEach((n) => { disp[n.id] = { x: 0, y: 0, z: 0 }; });
      // 节点互斥（O(n^2)，小图足够）
      for (let i = 0; i < nodes.length; i++) {
        for (let j = i + 1; j < nodes.length; j++) {
          const a = nodes[i], b = nodes[j];
          let dx = a.x - b.x, dy = a.y - b.y, dz = a.z - b.z;
          let dist = Math.sqrt(dx * dx + dy * dy + dz * dz) || 0.01;
          const f = (k * k) / dist;
          const ux = dx / dist, uy = dy / dist, uz = dz / dist;
          disp[a.id].x += ux * f; disp[a.id].y += uy * f; disp[a.id].z += uz * f;
          disp[b.id].x -= ux * f; disp[b.id].y -= uy * f; disp[b.id].z -= uz * f;
        }
      }
      // 边弹簧（理想长度 k）
      for (const e of edges) {
        const a = this._byId[e.src], b = this._byId[e.dst];
        let dx = a.x - b.x, dy = a.y - b.y, dz = a.z - b.z;
        let dist = Math.sqrt(dx * dx + dy * dy + dz * dz) || 0.01;
        const f = (dist * dist) / k;
        const ux = dx / dist, uy = dy / dist, uz = dz / dist;
        disp[a.id].x -= ux * f; disp[a.id].y -= uy * f; disp[a.id].z -= uz * f;
        disp[b.id].x += ux * f; disp[b.id].y += uy * f; disp[b.id].z += uz * f;
      }
      // 中心引力
      for (const n of nodes) {
        disp[n.id].x -= n.x * 0.04;
        disp[n.id].y -= n.y * 0.04;
        disp[n.id].z -= n.z * 0.04;
      }
      // 应用位移（限幅 = 温度），退火
      for (const n of nodes) {
        const d = disp[n.id];
        const len = Math.sqrt(d.x * d.x + d.y * d.y + d.z * d.z) || 0.01;
        const max = this.alpha * k;
        const s = Math.min(1, max / len);
        n.x += d.x * s; n.y += d.y * s; n.z += d.z * s;
      }
      this.alpha *= 0.97;
    }

    _draw() {
      const ctx = this.ctx;
      ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      ctx.clearRect(0, 0, this.W, this.H);
      // 背景
      ctx.fillStyle = '#0b0f18';
      ctx.fillRect(0, 0, this.W, this.H);

      // 投影所有节点
      const screen = [];
      for (const n of this.nodes) {
        const p = this._project(n);
        screen.push({ node: n, sx: p.sx, sy: p.sy, scale: p.scale, z: p.z });
      }
      this._screenNodes = screen;
      // 深度排序（远的先画）
      screen.sort((a, b) => a.z - b.z);

      const focus = (this.hover || { id: this.selected });
      const focusId = focus ? focus.id : null;
      const hl = focusId ? this._adj[focusId] : null;

      // 边
      for (const e of this.edges) {
        const a = this._byId[e.src], b = this._byId[e.dst];
        const pa = this._project(a), pb = this._project(b);
        const incident = focusId && (e.src === focusId || e.dst === focusId);
        const dimmed = focusId && !incident;
        ctx.beginPath();
        ctx.moveTo(pa.sx, pa.sy); ctx.lineTo(pb.sx, pb.sy);
        ctx.lineWidth = incident ? 2.4 : 1.2;
        ctx.strokeStyle = incident ? '#ffd54f' : (dimmed ? 'rgba(120,135,165,0.14)' : 'rgba(120,135,165,0.5)');
        ctx.stroke();
        if (this.edgeLabel && !dimmed) {
          const mx = (pa.sx + pb.sx) / 2, my = (pa.sy + pb.sy) / 2;
          ctx.font = '10px ui-monospace,monospace';
          ctx.fillStyle = incident ? '#ffd54f' : 'rgba(139,151,176,0.8)';
          ctx.fillText(e.type, mx + 3, my - 3);
        }
      }

      // 节点 + 标签
      for (const s of screen) {
        const n = s.node;
        const r = Math.max(4, n.r * s.scale);
        const isFocus = focusId === n.id;
        const isNeighbor = hl && hl.has(n.id);
        const dimmed = focusId && !isFocus && !isNeighbor;
        const color = colorForLabel(n.label);
        ctx.globalAlpha = dimmed ? 0.28 : 1;
        // 光晕
        if (isFocus) {
          ctx.beginPath(); ctx.arc(s.sx, s.sy, r + 6, 0, Math.PI * 2);
          ctx.fillStyle = color + '55'; ctx.fill();
        }
        ctx.beginPath(); ctx.arc(s.sx, s.sy, r, 0, Math.PI * 2);
        ctx.fillStyle = color; ctx.fill();
        ctx.lineWidth = isFocus ? 2 : 1;
        ctx.strokeStyle = isFocus ? '#fff' : 'rgba(255,255,255,0.35)';
        ctx.stroke();
        // 标签
        const name = n.label === n.id ? n.label : (n.props && n.props.name ? n.props.name : n.id);
        ctx.font = (isFocus ? 'bold ' : '') + '12px -apple-system,"PingFang SC",sans-serif';
        ctx.fillStyle = dimmed ? 'rgba(230,235,245,0.3)' : '#e6ebf5';
        const tw = ctx.measureText(name).width;
        ctx.fillText(name, s.sx - tw / 2, s.sy + r + 13);
        ctx.globalAlpha = 1;
      }
    }

    _loop() {
      if (!this.running) return;
      this._step();
      this._draw();
      requestAnimationFrame(() => this._loop());
    }

    destroy() {
      this.running = false;
      this.canvas.remove();
      if (this.container) this.container.innerHTML = '';
    }

    // 对外：度数
    degree(id) { return this._degree[id] || 0; }
    neighbors(id) { return this._adj[id] ? [...this._adj[id]] : []; }
  }

  window.Graph3D = Graph3D;
})();
