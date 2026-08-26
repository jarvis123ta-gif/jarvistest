/* app.js — wiring: state, voice, cards.
 *
 * Speech never touches the browser's Web Speech API. Raw PCM is captured
 * through Web Audio, encoded as 16 kHz mono WAV here, and transcribed
 * server-side — which works in every browser instead of only Chrome, fails
 * loudly instead of silently, and hands local whisper.cpp a format it can
 * read without ffmpeg.
 */

'use strict';

/* ---- tuning: the numbers worth touching ------------------------- */
const SILENCE_MS      = 900;    // quiet for this long ends the turn
const LEVEL_THRESHOLD = 0.055;  // RMS above this counts as speech
const LEVEL_TICK_MS   = 60;     // setInterval, NOT rAF — see below
const MIN_SPEECH_MS   = 350;    // ignore a cough

/* rAF is throttled to nothing in a backgrounded tab, which would leave
   the mic silently deaf mid-sentence. setInterval keeps running. */

const $  = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));

const TARGET_RATE = 16000;   // what whisper.cpp and every cloud API want

const S = {
  session: 'web-' + Math.random().toString(36).slice(2, 9),
  state: 'idle',            // idle | listening | thinking | speaking
  micOn: false, muted: false,
  stream: null, ctx: null, analyser: null, buf: null,
  source: null, proc: null, pcm: [], capturing: false,
  levelTimer: null,
  lastVoice: 0, speechStart: 0, heardSpeech: false,
  audio: null, level: 0, status: null,
  armed: true, acting: false,
};

/* ================================================================ state */

function setState(s) {
  S.state = s;
  $('#reactorState').textContent = s;
  $('#bars').classList.toggle('live', s === 'listening');
  $('#micBtn').classList.toggle('on', S.micOn);
}

function alertLoud(msg, kind) {
  const el = document.createElement('div');
  el.className = 'alert' + (kind === 'warn' ? ' warn' : '');
  el.textContent = msg;
  $('#alerts').appendChild(el);
  return el;
}

function caption(t) { $('#caption').textContent = t || ''; }

/* ================================================================ boot */

async function boot() {
  Graph.init($('#graph'), {
    onFocus: openNote,
    onPath: tracePath,
  });

  await loadStatus();
  await loadGraph();
  wire();
  rotateExample();
  drawReactor();
}

async function loadStatus() {
  try {
    S.status = await (await fetch('/api/status')).json();
  } catch (e) {
    alertLoud('Server unreachable — nothing will work until it is back.');
    return;
  }
  const st = S.status;
  const badge = $('#modeBadge');
  badge.textContent = st.mode;
  badge.classList.toggle('live', st.mode === 'live');

  $('#alerts').innerHTML = '';
  if (!st.model.ok) {
    alertLoud('No model: ' + st.model.reason, 'warn');
  }
  if (!st.voice.ok) {
    alertLoud('Voice off (' + st.voice.provider + '): ' + st.voice.reason, 'warn');
  }
  if (st.roots.missing.length) {
    alertLoud('Folder not found: ' + st.roots.missing.join(', '));
  }
  if (!st.roots.configured) {
    alertLoud('No folders configured — set them in agent/data.py');
  }
  const off = (st.connectors || []).filter(c => !c.connected);
  if (off.length) {
    alertLoud('Not connected: ' + off.map(c => c.label).join(', ') +
              ' — JARVIS will say so rather than guess.', 'warn');
  }

  if (st.timezone && st.timezone.fallback) {
    alertLoud('Clock: ' + st.timezone.fallback, 'warn');
  }

  const ctl = st.control || {};
  paintControl(ctl.armed !== false);
  if (ctl.browser && !ctl.browser.connected) {
    alertLoud('Chrome not attached — ' + ctl.browser.reason.slice(0, 150), 'warn');
  }
  if (ctl.desktop && !ctl.desktop.connected) {
    alertLoud('Desktop control off — ' + ctl.desktop.reason, 'warn');
  }
}

/* ---- control: armed, halted, acting ---------------------------- */

function paintControl(armed) {
  S.armed = armed;
  const b = $('#controlBadge');
  b.textContent = armed ? (S.acting ? 'acting' : 'armed') : 'halted';
  b.classList.toggle('halted', !armed);
  b.classList.toggle('acting', armed && S.acting);
  const f = $('#actingFrame');
  f.classList.toggle('on', armed && S.acting);
  f.classList.toggle('halted', !armed);
  $('#haltBtn').textContent = armed ? 'Halt' : 'Re-arm';
}

async function setArmed(armed, reason) {
  try {
    const r = await (await fetch('/api/control', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ armed, reason: reason || 'from the interface' }),
    })).json();
    paintControl(r.armed !== false);
    $('#spoken').classList.remove('hint');
    $('#spoken').textContent = armed
      ? 'Re-armed, Sir. I can act again.'
      : 'Stopped, Sir. Hands off everything until you re-arm.';
  } catch (e) {
    alertLoud('Could not reach the kill switch: ' + e.message);
  }
}

async function loadGraph() {
  const g = await (await fetch('/api/graph')).json();
  Graph.load(g);
  renderHubs(g.hubs);
  renderDomains(g.domains || {});
  renderFilter(g.counts);
}

/* ================================================================ panels */

function renderHubs(hubs) {
  $('#hubs').innerHTML = hubs.map(h =>
    `<li data-id="${h.id}">
       <span class="dot" style="background:${Graph.colourOf(h.type)}"></span>
       <span class="name">${esc(h.title)}</span>
       <span class="num">${h.degree}</span>
     </li>`).join('');
  $$('#hubs li').forEach(li =>
    li.onclick = () => Graph.focusById(li.dataset.id));
}

function renderDomains(domains) {
  const LABEL = { school: 'School', business: 'Shopify', deca: 'DECA',
                  unsorted: 'Unsorted' };
  $('#domains').innerHTML = Object.entries(domains).map(([d, c]) =>
    `<li data-domain="${d}">
       <span class="dot" style="background:${Graph.domainColour(d)}"></span>
       <span class="name">${esc(LABEL[d] || d)}</span>
       <span class="num">${c}</span>
     </li>`).join('');
  $$('#domains li').forEach(li => li.onclick = () => {
    const on = Graph.toggleDomain(li.dataset.domain);
    li.classList.toggle('off', !on);
  });
}

function renderFilter(counts) {
  $('#filter').innerHTML = Object.entries(counts).map(([t, c]) =>
    `<li data-type="${t}">
       <span class="dot" style="background:${Graph.colourOf(t)}"></span>
       <span class="name">${esc(t)}</span>
       <span class="num">${c}</span>
     </li>`).join('');
  $$('#filter li').forEach(li => li.onclick = () => {
    const on = Graph.toggleType(li.dataset.type);
    li.classList.toggle('off', !on);
  });
}

async function openNote(id) {
  const n = await (await fetch('/api/note?id=' + encodeURIComponent(id))).json();
  if (n.error) return;
  const meta = Object.entries(n.meta || {})
    .filter(([k]) => k !== 'title')
    .map(([k, v]) => `<div class="crow"><span class="k">${esc(k)}</span>
                      <span class="v">${esc(String(v))}</span></div>`).join('');
  const links = (n.links || []).concat(n.backlinks || [])
    .slice(0, 14)
    .map(l => `<span class="chip" data-id="${l.id}">${esc(l.title)}</span>`).join('');

  $('#inspector').classList.remove('hint');
  $('#inspector').innerHTML =
    `<div class="title">${esc(n.title)}</div>
     <div class="sub">${esc(n.domain || '—')} · ${esc(n.type)} · ${n.degree} links · ${esc(n.file)}</div>
     ${meta}
     ${n.unreadable ? '<div class="flag">This PDF has no extractable text. Nothing was guessed from it.</div>' : ''}
     <div class="body">${esc((n.text || '').slice(0, 1400))}</div>
     <div class="chips">${links}</div>`;
  $$('#inspector .chip').forEach(c =>
    c.onclick = () => Graph.focusById(c.dataset.id));
}

async function tracePath(a, b) {
  const r = await (await fetch(`/api/path?a=${a}&b=${b}`)).json();
  if (!r.path || !r.path.length) {
    caption('No path between those two.');
    setTimeout(() => caption(''), 2200);
    return;
  }
  Graph.highlight(r.path);
  caption(`${r.path.length} steps between them.`);
  setTimeout(() => caption(''), 3000);
}

/* ================================================================ ask */

async function ask(text) {
  text = (text || '').trim();
  if (!text) return;
  $('#ask').value = '';
  caption('');
  $('#spoken').classList.remove('hint');
  $('#spoken').textContent = '…';
  $('#card').innerHTML = '';
  setState('thinking');
  S.acting = true; paintControl(S.armed);

  let r;
  try {
    r = await (await fetch('/api/ask', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ text, session: S.session }),
    })).json();
  } catch (e) {
    $('#spoken').textContent = 'The server did not answer, Sir.';
    S.acting = false; paintControl(S.armed);
    setState('idle');
    return;
  }
  S.acting = false;
  if (r.card && r.card.kind === 'halted') paintControl(false);
  else paintControl(S.armed);

  $('#spoken').textContent = r.spoken || '(nothing said)';
  const cards = r.cards && r.cards.length ? r.cards : (r.card ? [r.card] : []);
  $('#card').innerHTML = cards.map(renderCard).join('') +
    (r.badge ? `<div class="flag">${esc(r.badge)}${
      r.routed_by ? ' · ' + esc(r.routed_by) : ''}</div>` : '');
  $$('#card .file').forEach(f => f.onclick = () => {
    if (f.dataset.id) Graph.focusById(f.dataset.id);
  });

  await speak(r.spoken);
}

/* ================================================================ cards */

const esc = s => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

const row = (k, v) => `<div class="crow"><span class="k">${esc(k)}</span>
                       <span class="v">${v}</span></div>`;

function renderCard(c) {
  if (!c) return '';
  switch (c.kind) {
    case 'search':   return cardSearch(c);
    case 'deadlines':return cardDeadlines(c);
    case 'browser':  return cardBrowser(c);
    case 'desktop':  return cardDesktop(c);
    case 'halted':   return cardHalted(c);
    case 'refused':  return cardRefused(c);
    case 'actions':  return cardActions(c);
    case 'store':    return cardStore(c);
    case 'research': return cardResearch(c);
    case 'inbox':    return cardInbox(c);
    case 'brief':    return cardBrief(c);
    case 'plan':     return cardPlan(c);
    case 'memory':   return cardMemory(c);
    case 'memories': return cardMemories(c);
    case 'error':    return `<div class="flag">${esc(c.error)}</div>`;
    default:         return '';
  }
}

function cardSearch(c) {
  if (c.empty) return row('searched', `${c.searched} notes, nothing matched`);
  const hits = c.hits.map(h => row(h.type,
    `<em class="file" data-id="${h.id}">${esc(h.file)}</em>
     <span class="muted">${esc(h.domain || '')}</span><br>
     <span class="muted">${esc(h.snippet)}</span>
     ${h.due ? `<br><span class="muted">${esc(h.due)}</span>` : ''}
     ${h.status ? `<br><span class="muted">status: ${esc(h.status)}</span>` : ''}
     ${h.demo_only ? '<br><span class="muted">demo figures only</span>' : ''}`)).join('');
  const mem = (c.memory || []).map(m =>
    row('memory', `<em>${esc(m.fact)}</em><br><span class="muted">${esc(m.file)}</span>`)).join('');
  const warn = (c.warnings || []).map(w =>
    `<div class="flag">In ${esc(w.where)}: “${esc(w.quote)}” — ${esc(w.handling)}</div>`).join('');
  return hits + mem + warn;
}

function cardResearch(c) {
  const web = (c.results || []).map(r =>
    row('web', `<em>${esc(r.title)}</em><br><span class="muted">${esc(r.snippet)}</span>`)).join('');
  const yours = (c.your_numbers || []).map(y =>
    row('yours', `<em>${esc(y.price)}</em> — ${esc(y.title)}
                  ${y.demo ? '<br><span class="muted">demo figure</span>' : ''}`)).join('');
  const off = c.store && !c.store.connected
    ? `<div class="flag">${esc(c.store.reason)}</div>` : '';
  return (c.error ? `<div class="flag">${esc(c.error)}</div>` : '') +
         off + yours + web + `<div class="qual">${esc(c.note)}</div>`;
}

function cardInbox(c) {
  if (c.error) return `<div class="flag">${esc(c.error)}<br>${esc(c.hint || '')}</div>`;
  const msgs = c.messages.map(m => row(
    m.unread ? 'unread' : 'read',
    `<em>${esc(m.from)}</em> — ${esc(m.subject)}
     <span class="muted">${esc(m.domain || '')}</span>
     <br><span class="muted">${esc(m.body.slice(0, 120))}</span>
     <br><span class="muted">${m.in_your_files
        ? 'in your files: ' + m.matched.map(x => esc(x.file)).join(', ')
        : 'no record of them'}</span>`)).join('');
  const flags = (c.flags || []).map(f =>
    `<div class="flag">${esc(f.where)}: “${esc(f.quote)}” — ${esc(f.handling)}</div>`).join('');
  return msgs + flags + `<div class="qual">${esc(c.note)}</div>`;
}

function cardDeadlines(c) {
  if (!c.items.length) return row('window', `nothing in ${c.window_days} days`);
  const it = c.items.map(i => row(
    i.days < 0 ? 'overdue' : i.domain,
    `<em class="file" data-id="${i.id}">${esc(i.what)}</em>
     <br><span class="muted">${esc(i.when)} · ${esc(i.due)} · ${esc(i.status)}</span>`)).join('');
  return it + `<div class="qual">${c.overdue} overdue of ${c.total}. ${esc(c.ordering || '')}. ${esc(c.note || '')}</div>`;
}

function cardBrowser(c) {
  if (c.connected === false) return `<div class="flag">${esc(c.note)}</div>`;
  if (c.image) return `<img src="${c.image}" alt="Screenshot of the tab"
      style="width:100%;border-radius:4px;border:1px solid var(--line)">`;
  if (c.tabs) {
    return c.tabs.map(t => row('tab',
      `<em>${esc(t.title || 'untitled')}</em><br><span class="muted">${esc(t.url)}</span>`)).join('');
  }
  if (c.title || c.url) {
    const links = (c.links || []).map(l => row('link',
      `${esc(l.text)}<br><span class="muted">${esc(l.href)}</span>`)).join('');
    const fields = (c.fields || []).map(f => row('field',
      `<em>${esc(f.label || f.name || f.id || f.tag)}</em>
       <span class="muted">${esc(f.type || f.tag)}</span>
       ${f.value ? `<br><span class="muted">${esc(f.value)}</span>` : ''}`)).join('');
    const buttons = (c.buttons || []).map(b => row('button', esc(b.text))).join('');
    const warn = (c.warnings || []).map(w =>
      `<div class="flag">This page contains an instruction aimed at me:
        “${esc(w.quote)}” — ${esc(w.handling)}</div>`).join('');
    return row('page', `<em>${esc(c.title)}</em><br><span class="muted">${esc(c.url)}</span>`) +
      (c.text ? row('text', `<span class="muted">${esc(c.text.slice(0, 700))}</span>`) : '') +
      fields + buttons + links + warn;
  }
  return row(c.action || 'browser', c.ok ? esc(JSON.stringify(c).slice(0, 300))
                                         : `<span class="muted">${esc(c.reason || '')}</span>`);
}

function cardDesktop(c) {
  if (c.connected === false) return `<div class="flag">${esc(c.note)}</div>`;
  if (c.image) return `<img src="${c.image}" alt="Screenshot of the screen"
      style="width:100%;border-radius:4px;border:1px solid var(--line)">`;
  if (c.windows) {
    return c.windows.map(w => row(w.focused ? 'focused' : 'window',
      esc(w.title))).join('');
  }
  return row(c.action || 'desktop', c.ok
    ? esc(JSON.stringify({ ...c, kind: undefined }).slice(0, 240))
    : `<span class="muted">${esc(c.reason || '')}</span>`);
}

function cardHalted(c) {
  const s = c.control || {};
  return `<div class="flag">Halted — ${esc(s.reason || 'stopped')}.
    Nothing will be clicked or typed until you re-arm.</div>` +
    row('actions so far', esc(s.actions || 0)) + row('log', esc(s.log || ''));
}

function cardRefused(c) {
  return `<div class="flag">Refused on the ${esc(c.surface)}: ${esc(c.why)}</div>`;
}

function cardActions(c) {
  if (!c.actions || !c.actions.length) return row('log', 'nothing done yet, Sir');
  return c.actions.map(a =>
    `<div class="actionrow${a.ok ? '' : ' refused'}">
       <span class="when">${esc((a.at || '').slice(11, 19))}</span>
       <span class="what">${esc(a.surface)} · ${esc(a.action)}</span>
       <span class="from">${esc(a.origin)}</span>
     </div>`).join('');
}

function cardStore(c) {
  if (!c.connected) {
    return `<div class="flag">${esc(c.note)}</div>`;
  }
  if (c.error) return `<div class="flag">${esc(c.error)}</div>`;
  const k = c.counts || {};
  const head = row('orders', `<em>${k.orders}</em> total · ${k.unfulfilled} unfulfilled · ${k.backlog} past window`);
  const os = (c.orders || []).slice(0, 6).map(o => row(
    o.fulfilment === 'fulfilled' ? 'done' : 'open',
    `<em>${esc(o.name)}</em> ${esc(o.currency || '')} ${esc(o.total)}
     <br><span class="muted">${esc(o.customer || 'no customer')} · ${esc(o.items.join(', '))}</span>`)).join('');
  const low = (c.low_stock || []).map(p => row('low stock',
    `<em>${esc(p.title)}</em> — ${esc(p.inventory)} left`)).join('');
  return head + os + low +
    `<div class="qual">${esc(c.qualifier || '')} ${esc(c.note || '')}</div>`;
}

function cardBrief(c) {
  const od = (c.overdue || []).map(o =>
    row('overdue', `<em>${esc(o.what)}</em> <span class="muted">${esc(o.domain)} · ${esc(o.when)}</span>`)).join('');
  const soon = (c.soon || []).map(o =>
    row('soon', `<em>${esc(o.what)}</em> <span class="muted">${esc(o.domain)} · ${esc(o.when)}</span>`)).join('');
  const ev = (c.events || []).map(e =>
    row('diary', `<em>${esc(e.title)}</em> <span class="muted">${esc((e.start || '').slice(11, 16))}${e.minutes ? ' · ' + e.minutes + 'm' : ''}</span>`)).join('');
  const sl = (c.slipped || []).map(s =>
    row('slipped', `${esc(s.title)} <span class="muted">due ${esc(s.due)}</span>`)).join('');
  const un = (c.unread || []).map(u =>
    row('unread', `<em>${esc(u.from)}</em> — ${esc(u.subject)} <span class="muted">${esc(u.domain || '')}</span>`)).join('');
  const st = c.store || {};
  const store = st.connected
    ? row('store', `${(st.counts || {}).unfulfilled} unfulfilled of ${(st.counts || {}).orders}${st.demo ? ' <span class="muted">(demo)</span>' : ''}`)
    : row('store', '<span class="muted">not connected</span>');
  const off = (c.connectors || []).filter(x => !x.connected)
    .map(x => `<div class="flag">${esc(x.label)}: ${esc(x.reason)}</div>`).join('');
  return od + soon + ev + sl + un + store + off +
    `<div class="qual">${esc(c.timezone || '')}. ${esc(c.caveat || '')}</div>`;
}

function cardPlan(c) {
  const it = (c.items || []).map(i =>
    row(String(i.rank), `<em>${esc(i.what)}</em><br><span class="muted">${esc(i.why)}</span>
      ${i.qualifier ? `<br><span class="muted">${esc(i.qualifier)}</span>` : ''}`)).join('');
  return it + `<div class="qual">${esc(c.considered)} candidates, ${esc(c.ordering)}. ${esc(c.note)}</div>`;
}

function cardMemory(c) {
  if (c.error) return `<div class="flag">${esc(c.error)}</div>`;
  return row('written', `<em>${esc(c.fact)}</em>`) +
         row('file', esc(c.file)) + row('total', esc(c.total)) +
         `<div class="qual">${esc(c.note)}</div>`;
}

function cardMemories(c) {
  if (!c.facts.length) return row('memory', 'nothing remembered yet');
  return c.facts.map(f =>
    row(f.date.slice(0, 10), `<em>${esc(f.fact)}</em><br><span class="muted">${esc(f.file)}</span>`)).join('');
}

/* ================================================================ voice out */

async function speak(text) {
  if (!text) { setState('idle'); return resumeListening(); }
  if (S.muted || !(S.status && S.status.voice.ok)) {
    setState('idle');
    return resumeListening();
  }
  setState('speaking');
  stopRecording();                 // deaf while speaking — no self-transcription

  try {
    const res = await fetch('/api/speak', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    if (!res.ok) {
      const e = await res.json().catch(() => ({}));
      alertLoud('Could not speak: ' + (e.error || res.status), 'warn');
      setState('idle');
      return resumeListening();
    }
    const url = URL.createObjectURL(await res.blob());
    await new Promise(done => {
      S.audio = new Audio(url);
      S.audio.onended = S.audio.onerror = () => {
        URL.revokeObjectURL(url); S.audio = null; done();
      };
      S.audio.play().catch(() => done());
    });
  } catch (e) {
    alertLoud('Speech failed: ' + e.message, 'warn');
  }
  setState('idle');
  resumeListening();
}

function bargeIn() {
  if (S.audio) { S.audio.pause(); S.audio = null; }
  setState('idle');
}

/* ================================================================ voice in */

async function micToggle() {
  if (S.state === 'speaking') { bargeIn(); }
  if (S.micOn) { micOff(); return; }
  if (!(S.status && S.status.voice.ok)) {
    alertLoud('Mic is pointless right now: ' + S.status.voice.reason, 'warn');
    return;
  }
  try {
    S.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true },
    });
  } catch (e) {
    /* A blocked mic that produces no error is the most confusing failure
       in this whole build, so say it plainly. */
    alertLoud('Microphone blocked or unavailable: ' + e.name +
              '. Check the padlock in the address bar.');
    return;
  }
  S.ctx = new (window.AudioContext || window.webkitAudioContext)();
  S.source = S.ctx.createMediaStreamSource(S.stream);
  S.analyser = S.ctx.createAnalyser();
  S.analyser.fftSize = 1024;
  S.buf = new Uint8Array(S.analyser.fftSize);
  S.source.connect(S.analyser);

  S.proc = S.ctx.createScriptProcessor(4096, 1, 1);
  S.proc.onaudioprocess = e => {
    if (!S.capturing) return;                 // genuinely deaf while speaking
    S.pcm.push(new Float32Array(e.inputBuffer.getChannelData(0)));
  };
  S.source.connect(S.proc);
  // A zero-gain sink: some browsers will not run the processor unless the
  // graph reaches a destination, but nothing should actually be audible.
  const mute = S.ctx.createGain();
  mute.gain.value = 0;
  S.proc.connect(mute);
  mute.connect(S.ctx.destination);

  S.micOn = true;
  S.levelTimer = setInterval(levelTick, LEVEL_TICK_MS);
  startRecording();
}

function micOff() {
  S.micOn = false;
  stopRecording();
  clearInterval(S.levelTimer); S.levelTimer = null;
  if (S.proc) { S.proc.onaudioprocess = null; S.proc.disconnect(); }
  if (S.source) S.source.disconnect();
  if (S.stream) S.stream.getTracks().forEach(t => t.stop());
  if (S.ctx) S.ctx.close();
  S.stream = S.ctx = S.analyser = S.source = S.proc = null;
  S.level = 0; paintBars(0);
  caption('');
  setState('idle');
}

/* Capture raw PCM rather than MediaRecorder's WebM/Opus. Local whisper
   cannot read Opus without ffmpeg, and every cloud API accepts WAV, so one
   format serves every tier with nothing to install. The audio callback also
   keeps running in a backgrounded tab, which MediaRecorder timeslices do
   not reliably do. */

function startRecording() {
  if (!S.micOn || !S.ctx || S.capturing) return;
  S.pcm = [];
  S.capturing = true;
  S.heardSpeech = false;
  S.speechStart = 0;
  S.lastVoice = performance.now();
  setState('listening');
  caption('listening…');
}

function stopRecording() {
  S.capturing = false;
  S.pcm = [];
}

function resumeListening() {
  if (S.micOn && !S.capturing) setTimeout(startRecording, 220);
}

function endTurn() {
  if (!S.capturing) return;
  S.capturing = false;
  caption('transcribing…');
  setState('thinking');
  onTurnEnd(flushWav());
}

function flushWav() {
  const total = S.pcm.reduce((n, c) => n + c.length, 0);
  const merged = new Float32Array(total);
  let at = 0;
  for (const c of S.pcm) { merged.set(c, at); at += c.length; }
  S.pcm = [];
  return encodeWav(downsample(merged, S.ctx.sampleRate, TARGET_RATE), TARGET_RATE);
}

function downsample(input, from, to) {
  if (to >= from) return input;
  const ratio = from / to;
  const out = new Float32Array(Math.floor(input.length / ratio));
  for (let i = 0; i < out.length; i++) {
    const start = Math.floor(i * ratio), end = Math.min(Math.floor((i + 1) * ratio), input.length);
    let sum = 0;
    for (let j = start; j < end; j++) sum += input[j];
    out[i] = sum / Math.max(end - start, 1);
  }
  return out;
}

function encodeWav(samples, rate) {
  const buf = new ArrayBuffer(44 + samples.length * 2);
  const v = new DataView(buf);
  const str = (off, s) => { for (let i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i)); };
  str(0, 'RIFF'); v.setUint32(4, 36 + samples.length * 2, true); str(8, 'WAVE');
  str(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true);
  v.setUint16(22, 1, true); v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  str(36, 'data'); v.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
  }
  return new Blob([buf], { type: 'audio/wav' });
}

function levelTick() {
  if (!S.analyser) return;
  S.analyser.getByteTimeDomainData(S.buf);
  let sum = 0;
  for (let i = 0; i < S.buf.length; i++) {
    const v = (S.buf[i] - 128) / 128;
    sum += v * v;
  }
  S.level = Math.sqrt(sum / S.buf.length);
  paintBars(S.level);

  if (S.state !== 'listening' || !S.capturing) return;
  const now = performance.now();
  if (S.level > LEVEL_THRESHOLD) {
    S.lastVoice = now;
    if (!S.heardSpeech) { S.heardSpeech = true; S.speechStart = now; caption('…'); }
  } else if (S.heardSpeech &&
             now - S.lastVoice > SILENCE_MS &&
             now - S.speechStart > MIN_SPEECH_MS) {
    endTurn();
  }
}

async function onTurnEnd(blob) {
  if (!blob || blob.size < 2000) { caption(''); resumeListening(); return; }
  let out;
  try {
    const res = await fetch('/api/listen', {
      method: 'POST', headers: { 'content-type': 'audio/wav' }, body: blob,
    });
    out = await res.json();
  } catch (e) {
    alertLoud('Transcriber unreachable: ' + e.message);
    caption(''); resumeListening(); return;
  }
  if (!out.ok) {
    alertLoud('Could not transcribe: ' + out.error);
    caption(''); resumeListening(); return;
  }
  const text = (out.text || '').trim();
  caption(text || '(nothing heard)');
  if (!text) { resumeListening(); return; }
  await ask(text);
}

function paintBars(level) {
  const bars = $$('#bars i');
  const n = bars.length;
  for (let i = 0; i < n; i++) {
    const centre = 1 - Math.abs(i - (n - 1) / 2) / ((n - 1) / 2);
    const h = 2 + Math.min(1, level * 7) * (3 + centre * 13);
    bars[i].style.height = h.toFixed(1) + 'px';
  }
}

/* ================================================================ reactor */

function drawReactor() {
  const cv = $('#reactor'), g = cv.getContext('2d');
  const cx = 66, cy = 66;
  let t = 0;

  const COL = { idle: '#4a5765', listening: '#35d0ff',
                thinking: '#f2b53a', speaking: '#3ecf7a' };

  setInterval(() => {
    t += 0.04;
    const col = COL[S.state] || COL.idle;
    const amp = S.state === 'listening' ? Math.min(1, S.level * 8)
              : S.state === 'speaking' ? 0.55 + Math.sin(t * 5) * 0.3
              : S.state === 'thinking' ? 0.4 : 0.12;

    g.clearRect(0, 0, 132, 132);

    g.strokeStyle = 'rgba(255,255,255,.055)';
    g.lineWidth = 1;
    [26, 38, 50].forEach(r => {
      g.beginPath(); g.arc(cx, cy, r, 0, 7); g.stroke();
    });

    // tick ring
    g.strokeStyle = 'rgba(255,255,255,.10)';
    for (let i = 0; i < 60; i++) {
      const a = (i / 60) * Math.PI * 2;
      const r0 = 54, r1 = 54 + (i % 5 === 0 ? 5 : 2.5);
      g.beginPath();
      g.moveTo(cx + Math.cos(a) * r0, cy + Math.sin(a) * r0);
      g.lineTo(cx + Math.cos(a) * r1, cy + Math.sin(a) * r1);
      g.stroke();
    }

    // live arcs
    g.strokeStyle = col; g.lineWidth = 2;
    g.globalAlpha = 0.9;
    g.beginPath(); g.arc(cx, cy, 44, t, t + 1.1 + amp * 1.4); g.stroke();
    g.beginPath(); g.arc(cx, cy, 33, -t * 1.6, -t * 1.6 + 0.7 + amp); g.stroke();
    g.globalAlpha = 0.35;
    g.beginPath(); g.arc(cx, cy, 55, t * 0.5, t * 0.5 + 2.2); g.stroke();

    // core
    g.globalAlpha = 1;
    g.fillStyle = col;
    g.beginPath(); g.arc(cx, cy, 5 + amp * 9, 0, 7); g.fill();
    g.globalAlpha = 0.16;
    g.beginPath(); g.arc(cx, cy, 12 + amp * 16, 0, 7); g.fill();
    g.globalAlpha = 1;
  }, 55);
}

/* ================================================================ wiring */

const EXAMPLES = [
  'what is due this week?',
  'brief me',
  'what needs my attention first?',
  'how is the store doing?',
  'when is the DECA registration deadline?',
  'what did Ms. Whitfield want?',
  'remember that the chemistry makeup lab is during tutorial',
  'how should I study for the calculus test?',
];

function rotateExample() {
  let i = 0;
  setInterval(() => {
    if (document.activeElement === $('#ask') || $('#ask').value) return;
    i = (i + 1) % EXAMPLES.length;
    $('#ask').placeholder = EXAMPLES[i];
  }, 5200);
}

function wire() {
  $('#ask').addEventListener('keydown', e => {
    if (e.key === 'Enter') ask($('#ask').value);
  });
  $('#micBtn').onclick = micToggle;
  $('#muteBtn').onclick = () => {
    S.muted = !S.muted;
    $('#muteBtn').classList.toggle('muted', S.muted);
    $('#muteBtn').textContent = S.muted ? 'Muted' : 'Mute';
    if (S.muted) bargeIn();
  };
  $('#haltBtn').onclick = () => setArmed(!S.armed);
  $('#controlBadge').onclick = () => setArmed(!S.armed);
  $('#memBtn').oncontextmenu = async e => {
    e.preventDefault();
    const a = await (await fetch('/api/actions')).json();
    $('#spoken').classList.remove('hint');
    $('#spoken').textContent = `${a.actions.length} actions logged, Sir.`;
    $('#card').innerHTML = renderCard({ kind: 'actions', actions: a.actions });
  };
  $('#memBtn').onclick = async () => {
    const m = await (await fetch('/api/memory')).json();
    $('#spoken').classList.remove('hint');
    $('#spoken').textContent = `${m.count} facts remembered, Sir.`;
    $('#card').innerHTML = renderCard({ kind: 'memories', facts: m.facts });
  };
  $$('[data-say]').forEach(b => b.onclick = () => ask(b.dataset.say));

  $$('#toolbar .pill').forEach(p => p.onclick = () => {
    const act = p.dataset.act;
    if (act === 'fit') { Graph.fit(); Graph.kick(); return; }
    p.classList.toggle('on');
    const on = p.classList.contains('on');
    if (act === 'labels') Graph.setLabels(on);
    if (act === 'pulse') Graph.setPulse(on);
  });

  document.addEventListener('keydown', e => {
    if (e.target === $('#ask')) return;
    if (e.code === 'Space') { e.preventDefault(); micToggle(); }
    if (e.key === 'Escape') {
      // Esc is the panic key in the page: it stops the voice AND the hands.
      bargeIn(); Graph.clearHighlight();
      if (S.micOn) micOff();
      if (S.armed) setArmed(false, 'Esc pressed');
    }
  });

  setInterval(loadStatus, 30000);
}

boot();
