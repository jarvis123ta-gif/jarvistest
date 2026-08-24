/* graph.js — force-directed note graph on a canvas.
 *
 * Canvas, not SVG. SVG needs a DOM node per element and stalls somewhere
 * past ~1,500 nodes; this stays near-linear because repulsion only looks
 * at a 3x3 neighbourhood of a spatial grid and gives up past a cutoff.
 */

const Graph = (() => {
  'use strict';

  // -- tuning ------------------------------------------------------
  const CUTOFF     = 150;    // repulsion ignored past this, in world units
  const REPULSION  = 2050;
  const SPRING     = 0.010;
  const REST       = 52;
  const GRAVITY    = 0.0042;
  const DAMPING    = 0.86;
  const ALPHA_MIN  = 0.012;  // never fully zero — the graph keeps breathing
  const ALPHA_DECAY= 0.986;
  const PULSE_EVERY= 2600;   // ms between idle pulses
  const DIM        = 0.10;   // everything else drops to this on hover

  const LABEL_MIN_DEGREE = 8;   // below this a node stays unlabelled

  // Stable colours for the kinds of note that keep turning up, so the
  // graph reads the same way every session. Anything else falls through
  // to the spare palette in order of frequency.
  const BY_TYPE = {
    call: '#3aa0ff', note: '#d8dee6', concept: '#f2b53a', project: '#5f9dff',
    person: '#a97bff', client: '#3ecf7a', invoice: '#ff5a5a',
    proposal: '#ffb03a', sop: '#ff8a3d', brief: '#8ea2b8',
    campaign: '#ff5fa2', memory: '#2fd6c3',
  };
  const PALETTE = ['#2fd6c3', '#c6ff5f', '#5f7dff', '#ff5fa2', '#9fb0c4',
                   '#ffd166', '#7ee3ff', '#e08cff'];

  let cv, ctx, W = 0, H = 0, dpr = 1;
  let nodes = [], edges = [], byId = new Map(), adj = new Map();
  let colours = new Map(), hidden = new Set();
  let cam = { x: 0, y: 0, k: 1 };
  let alpha = 1, raf = 0;
  let hover = null, focus = null, pathIds = new Set(), pathEdges = new Set();
  let drag = null, panning = null, moved = false;
  let pulses = [], lastPulse = 0;
  let showLabels = true, showPulse = true;
  let hooks = {};

  // -- deterministic jitter so the first layout is the same every load
  let seed = 20240117;
  const rnd = () => ((seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff);

  // ---------------------------------------------------------------- setup

  function init(canvas, callbacks) {
    cv = canvas; ctx = cv.getContext('2d'); hooks = callbacks || {};
    resize();
    window.addEventListener('resize', resize);
    cv.addEventListener('mousemove', onMove);
    cv.addEventListener('mousedown', onDown);
    window.addEventListener('mouseup', onUp);
    cv.addEventListener('wheel', onWheel, { passive: false });
    cv.addEventListener('mouseleave', () => { hover = null; });
  }

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    W = window.innerWidth; H = window.innerHeight;
    cv.style.width = W + 'px'; cv.style.height = H + 'px';
    cv.width = W * dpr; cv.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function load(payload) {
    let spare = 0;
    for (const t of Object.keys(payload.counts || {})) {
      colours.set(t, BY_TYPE[t] || PALETTE[spare++ % PALETTE.length]);
    }

    const n = payload.nodes.length || 1;
    const R = Math.max(220, Math.sqrt(n) * 34);
    nodes = payload.nodes.map((d, i) => {
      const a = (i / n) * Math.PI * 2, rr = R * (0.35 + 0.65 * rnd());
      return Object.assign({}, d, {
        x: Math.cos(a) * rr + (rnd() - .5) * 40,
        y: Math.sin(a) * rr + (rnd() - .5) * 40,
        vx: 0, vy: 0,
        r: 3.4 + Math.sqrt(d.degree || 0) * 2.15,
        m: 1 + (d.degree || 0) * 0.28,
        c: colours.get(d.type) || '#8ea2b8'
      });
    });
    byId = new Map(nodes.map(d => [d.id, d]));
    adj = new Map(nodes.map(d => [d.id, []]));
    edges = payload.edges
      .map(e => ({ s: byId.get(e.s), t: byId.get(e.t) }))
      .filter(e => e.s && e.t);
    edges.forEach(e => { adj.get(e.s.id).push(e.t.id); adj.get(e.t.id).push(e.s.id); });

    alpha = 1;
    for (let i = 0; i < 260; i++) step();   // settle before the first paint
    fit();
    if (!raf) raf = requestAnimationFrame(frame);
  }

  // ---------------------------------------------------------------- forces

  function step() {
    const cell = CUTOFF, grid = new Map();
    for (const d of nodes) {
      const k = ((d.x / cell) | 0) + ',' + ((d.y / cell) | 0);
      (grid.get(k) || grid.set(k, []).get(k)).push(d);
    }

    for (const d of nodes) {
      const gx = (d.x / cell) | 0, gy = (d.y / cell) | 0;
      for (let ix = -1; ix <= 1; ix++) for (let iy = -1; iy <= 1; iy++) {
        const bucket = grid.get((gx + ix) + ',' + (gy + iy));
        if (!bucket) continue;
        for (const o of bucket) {
          if (o === d) continue;
          let dx = d.x - o.x, dy = d.y - o.y;
          let d2 = dx * dx + dy * dy;
          if (d2 > CUTOFF * CUTOFF) continue;
          if (d2 < 1) { dx = rnd() - .5; dy = rnd() - .5; d2 = 1; }
          const f = (REPULSION * d.m * o.m) / (d2 * Math.sqrt(d2));
          d.vx += dx * f; d.vy += dy * f;
        }
      }
      d.vx -= d.x * GRAVITY * (1 + alpha);
      d.vy -= d.y * GRAVITY * (1 + alpha);
    }

    for (const e of edges) {
      const dx = e.t.x - e.s.x, dy = e.t.y - e.s.y;
      const dist = Math.hypot(dx, dy) || 1;
      const f = (dist - REST) * SPRING;
      const fx = (dx / dist) * f, fy = (dy / dist) * f;
      e.s.vx += fx / e.s.m; e.s.vy += fy / e.s.m;
      e.t.vx -= fx / e.t.m; e.t.vy -= fy / e.t.m;
    }

    for (const d of nodes) {
      if (d === drag) { d.vx = d.vy = 0; continue; }
      d.vx *= DAMPING; d.vy *= DAMPING;
      const sp = Math.hypot(d.vx, d.vy);
      if (sp > 22) { d.vx *= 22 / sp; d.vy *= 22 / sp; }
      d.x += d.vx * alpha; d.y += d.vy * alpha;
    }
    alpha = Math.max(ALPHA_MIN, alpha * ALPHA_DECAY);
  }

  // ---------------------------------------------------------------- camera

  /* The four panels float over the canvas, so "centre" is the middle of
     what is actually visible, not the middle of the window. */
  const PAD = { l: 280, r: 296, t: 56, b: 96 };
  const CX = () => PAD.l + (W - PAD.l - PAD.r) / 2;
  const CY = () => PAD.t + (H - PAD.t - PAD.b) / 2;

  const toScreen = d => [(d.x - cam.x) * cam.k + CX(), (d.y - cam.y) * cam.k + CY()];
  const toWorld = (sx, sy) => [(sx - CX()) / cam.k + cam.x, (sy - CY()) / cam.k + cam.y];

  function fit() {
    if (!nodes.length) return;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const d of nodes) {
      x0 = Math.min(x0, d.x); x1 = Math.max(x1, d.x);
      y0 = Math.min(y0, d.y); y1 = Math.max(y1, d.y);
    }
    cam.x = (x0 + x1) / 2; cam.y = (y0 + y1) / 2;
    cam.k = Math.min((W - PAD.l - PAD.r - 70) / (x1 - x0 + 90),
                     (H - PAD.t - PAD.b - 70) / (y1 - y0 + 90), 2.4);
    cam.k = Math.max(cam.k, 0.16);
  }

  // ---------------------------------------------------------------- draw

  function visible(d) { return !hidden.has(d.type); }

  function frame(ts) {
    step();
    if (showPulse && ts - lastPulse > PULSE_EVERY && edges.length) {
      lastPulse = ts;
      pulses.push({ e: edges[(rnd() * edges.length) | 0], t: 0 });
    }
    draw();
    raf = requestAnimationFrame(frame);
  }

  function draw() {
    ctx.clearRect(0, 0, W, H);

    const lit = new Set();
    if (hover) { lit.add(hover.id); (adj.get(hover.id) || []).forEach(i => lit.add(i)); }
    const dimming = !!hover || pathIds.size > 0;
    const inFocus = id => !dimming || lit.has(id) || pathIds.has(id);

    // edges
    ctx.lineWidth = 1;
    for (const e of edges) {
      if (!visible(e.s) || !visible(e.t)) continue;
      const key = e.s.id + '|' + e.t.id, rev = e.t.id + '|' + e.s.id;
      const onPath = pathEdges.has(key) || pathEdges.has(rev);
      const touched = hover && (e.s.id === hover.id || e.t.id === hover.id);
      let a = 0.13, col = '140,168,200';
      if (onPath) { a = 0.95; col = '53,208,255'; }
      else if (touched) { a = 0.62; col = '53,208,255'; }
      else if (dimming) { a = 0.13 * DIM; }
      const [x1, y1] = toScreen(e.s), [x2, y2] = toScreen(e.t);
      ctx.strokeStyle = `rgba(${col},${a})`;
      ctx.lineWidth = onPath ? 1.6 : 1;
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
    }

    // idle pulses
    for (let i = pulses.length - 1; i >= 0; i--) {
      const p = pulses[i]; p.t += 0.014;
      if (p.t >= 1 || !visible(p.e.s) || !visible(p.e.t)) { pulses.splice(i, 1); continue; }
      const [x1, y1] = toScreen(p.e.s), [x2, y2] = toScreen(p.e.t);
      const x = x1 + (x2 - x1) * p.t, y = y1 + (y2 - y1) * p.t;
      const a = Math.sin(p.t * Math.PI) * 0.75;
      ctx.fillStyle = `rgba(53,208,255,${a})`;
      ctx.beginPath(); ctx.arc(x, y, 1.9, 0, 7); ctx.fill();
    }

    // nodes
    for (const d of nodes) {
      if (!visible(d)) continue;
      const [x, y] = toScreen(d);
      if (x < -60 || y < -60 || x > W + 60 || y > H + 60) continue;
      const isHover = hover === d, isFocus = focus === d;
      const a = inFocus(d.id) ? 1 : DIM;
      const r = (d.r + (isHover ? 3.2 : 0)) * Math.max(0.55, Math.min(cam.k, 1.5));

      ctx.globalAlpha = a;
      ctx.fillStyle = d.c;
      ctx.beginPath(); ctx.arc(x, y, r, 0, 7); ctx.fill();

      if (isHover || isFocus || pathIds.has(d.id)) {
        ctx.strokeStyle = isFocus || pathIds.has(d.id) ? '#35d0ff'
                                                       : 'rgba(255,255,255,.75)';
        ctx.lineWidth = 1.4;
        ctx.beginPath(); ctx.arc(x, y, r + 3.4, 0, 7); ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }

    if (showLabels) drawLabels(inFocus);
  }

  /* Draw most-connected first and refuse any label whose box collides with
     one already placed — otherwise the hub cluster turns to mush. */
  function drawLabels(inFocus) {
    /* Seed the occupied boxes with the dots themselves, so a label never
       lands on top of a node it does not belong to. */
    const placed = [];
    for (const d of nodes) {
      if (!visible(d) || d.degree < 4) continue;
      const [x, y] = toScreen(d);
      const r = d.r * Math.max(0.55, Math.min(cam.k, 1.5)) + 1.5;
      placed.push([x - r, y - r, x + r, y + r]);
    }
    ctx.font = '10.5px ui-sans-serif, -apple-system, Inter, sans-serif';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';

    const order = nodes.filter(visible).slice().sort((a, b) => {
      const pin = n => (n === hover ? 3 : n === focus ? 2 : pathIds.has(n.id) ? 1 : 0);
      return (pin(b) - pin(a)) || (b.degree - a.degree);
    });

    for (const d of order) {
      const pinned = d === hover || d === focus || pathIds.has(d.id);
      if (!pinned && (d.degree < LABEL_MIN_DEGREE || cam.k < 0.30)) continue;
      const [x, y] = toScreen(d);
      if (x < 0 || y < 0 || x > W || y > H) continue;

      const text = d.title.length > 30 ? d.title.slice(0, 29) + '…' : d.title;
      const w = ctx.measureText(text).width + 8;
      const h = 13;
      const ly = y - (d.r * Math.max(0.55, Math.min(cam.k, 1.5))) - 9;
      const box = [x - w / 2, ly - h / 2, x + w / 2, ly + h / 2];

      let clash = false;
      for (const b of placed) {
        if (box[0] < b[2] && box[2] > b[0] && box[1] < b[3] && box[3] > b[1]) {
          clash = true; break;
        }
      }
      if (clash && !pinned) continue;
      placed.push(box);

      ctx.globalAlpha = inFocus(d.id) ? (pinned ? 1 : 0.86) : DIM;
      ctx.fillStyle = 'rgba(0,0,0,.62)';
      ctx.fillText(text, x + 0.6, ly + 0.6);
      ctx.fillStyle = pinned ? '#ffffff' : '#c3ccd8';
      ctx.fillText(text, x, ly);
      ctx.globalAlpha = 1;
    }
  }

  // ---------------------------------------------------------------- input

  function pick(sx, sy) {
    const [wx, wy] = toWorld(sx, sy);
    let best = null, bestD = Infinity;
    for (const d of nodes) {
      if (!visible(d)) continue;
      const dd = (d.x - wx) ** 2 + (d.y - wy) ** 2;
      const rr = (d.r + 7 / cam.k) ** 2;
      if (dd < rr && dd < bestD) { best = d; bestD = dd; }
    }
    return best;
  }

  function onMove(ev) {
    const sx = ev.clientX, sy = ev.clientY;
    if (drag) {
      const [wx, wy] = toWorld(sx, sy);
      drag.x = wx; drag.y = wy; moved = true; alpha = Math.max(alpha, 0.55);
      return;
    }
    if (panning) {
      cam.x -= (sx - panning.x) / cam.k;
      cam.y -= (sy - panning.y) / cam.k;
      panning = { x: sx, y: sy }; moved = true;
      return;
    }
    const h = pick(sx, sy);
    if (h !== hover) { hover = h; cv.style.cursor = h ? 'pointer' : 'grab'; }
  }

  function onDown(ev) {
    const n = pick(ev.clientX, ev.clientY);
    moved = false;
    if (n && ev.altKey) { drag = n; }
    else if (n) { drag = n; }
    else { panning = { x: ev.clientX, y: ev.clientY }; cv.classList.add('dragging'); }
  }

  function onUp(ev) {
    const wasDrag = drag, wasMoved = moved;
    drag = null; panning = null; moved = false;
    cv.classList.remove('dragging');
    if (wasDrag && !wasMoved) select(wasDrag, ev.shiftKey);
  }

  function select(n, shift) {
    if (shift && focus && focus !== n) {
      hooks.onPath && hooks.onPath(focus.id, n.id);
      return;
    }
    focus = n; pathIds = new Set(); pathEdges = new Set();
    hooks.onFocus && hooks.onFocus(n.id);
  }

  function onWheel(ev) {
    ev.preventDefault();
    const [wx, wy] = toWorld(ev.clientX, ev.clientY);
    cam.k = Math.max(0.1, Math.min(4, cam.k * (ev.deltaY < 0 ? 1.11 : 0.9)));
    const [nx, ny] = toWorld(ev.clientX, ev.clientY);
    cam.x += wx - nx; cam.y += wy - ny;
  }

  // ---------------------------------------------------------------- api

  return {
    init, load, fit,
    colourOf: t => colours.get(t) || '#8ea2b8',
    focusById(id) {
      const n = byId.get(id); if (!n) return;
      focus = n; pathIds = new Set(); pathEdges = new Set();
      cam.x = n.x; cam.y = n.y; cam.k = Math.max(cam.k, 0.85);
      hooks.onFocus && hooks.onFocus(id);
    },
    highlight(ids) {
      const list = (ids || []).filter(i => byId.has(i));
      pathIds = new Set(list); pathEdges = new Set();
      for (let i = 0; i < list.length - 1; i++) pathEdges.add(list[i] + '|' + list[i + 1]);
      if (list.length) {
        const pts = list.map(i => byId.get(i));
        cam.x = pts.reduce((s, p) => s + p.x, 0) / pts.length;
        cam.y = pts.reduce((s, p) => s + p.y, 0) / pts.length;
      }
    },
    clearHighlight() { pathIds = new Set(); pathEdges = new Set(); },
    toggleType(t) { hidden.has(t) ? hidden.delete(t) : hidden.add(t); return !hidden.has(t); },
    setLabels(v) { showLabels = v; },
    setPulse(v) { showPulse = v; if (!v) pulses = []; },
    focusId: () => (focus ? focus.id : null),
    kick() { alpha = 0.8; }
  };
})();
