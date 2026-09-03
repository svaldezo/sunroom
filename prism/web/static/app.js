/* Sunroom — client.
   One rule runs through the whole interface: nothing is shown without a way
   back to where it came from. Everything else is in service of that being
   pleasant rather than forensic. */

const $  = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct = v => Math.round((v || 0) * 100) + '%';
const icon = (id, cls = '') =>
  `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true"><use href="#${id}"/></svg>`;

/* ── session ─────────────────────────────────────────────────────
   Auth is Supabase magic links. The access token lives in memory and in
   localStorage under supabase-js's own key; every API call carries it as a
   bearer. A 401 means the session lapsed, so the app returns to the sign-in
   screen rather than showing an empty library and letting someone conclude
   their work is gone. */

const S = {view: 'library', doc: null, docs: [], collection: null, formats: [],
           deliverable: null, spans: [], thread: [], due: [], idx: 0,
           revealed: false, activePart: null,
           me: null, config: null, jobs: [], polling: null};

class ApiError extends Error {
  constructor(message, status, code) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  const token = auth.token();
  if (token) headers.Authorization = 'Bearer ' + token;
  const r = await fetch('/api' + path, Object.assign({}, opts, {headers}));
  if (r.status === 401) { auth.expired(); throw new ApiError('Signed out', 401); }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new ApiError(body.detail || r.statusText, r.status, body.code);
  return body;
}
const post = (p, b) => api(p, {
  method: 'POST', headers: {'Content-Type': 'application/json'},
  body: JSON.stringify(b === undefined ? null : b),
});
const put = (p, b) => api(p, {
  method: 'PUT', headers: {'Content-Type': 'application/json'},
  body: JSON.stringify(b === undefined ? null : b),
});
const del = (p) => api(p, {method: 'DELETE'});

let toastT;
function toast(msg) {
  const t = $('#toast');
  t.textContent = msg; t.classList.add('on');
  clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove('on'), 2400);
}
function copy(text) {
  navigator.clipboard?.writeText(text).then(() => toast('Copied'),
    () => toast("Your browser blocked the copy"));
}
window.copy = copy;

/* Formats emit light markdown; render the inline subset rather than showing
   raw asterisks to a person. */
function md(text) {
  const inline = t => t
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|\s)\*([^*\s][^*]*)\*/g, '$1<em>$2</em>')
    .replace(/_{4,}/g, '<span class="blank"></span>');

  // Blank lines separate paragraphs; a single newline is just where the
  // source happened to wrap, so consecutive lines are joined rather than
  // each becoming its own paragraph.
  return esc(text).split(/\n\s*\n/).map(block => {
    const lines = block.split('\n').filter(l => l.trim());
    if (!lines.length) return '';
    const listy = lines.filter(l => /^\s*([-*]|\d+\.)\s+/.test(l)).length;
    if (listy >= Math.ceil(lines.length / 2)) {
      return '<ul>' + lines.map(l => {
        const m = l.match(/^\s*(?:[-*]|(\d+)\.)\s+(.*)$/);
        return `<li>${m ? (m[1] ? `<b>${m[1]}.</b> ` : '') + inline(m[2]) : inline(l)}</li>`;
      }).join('') + '</ul>';
    }
    return `<p>${inline(lines.join(' '))}</p>`;
  }).join('');
}

const HAS_MERMAID = typeof mermaid !== 'undefined';

/* Mermaid's own palette is loud enough to fight the page for attention, and
   mind maps in particular reach past primaryColor into cScale0..n. Build the
   theme from the live CSS variables so a diagram is the same document in dark
   mode as in light. */
function mermaidTheme() {
  const v = getComputedStyle(document.documentElement);
  const c = n => v.getPropertyValue(n).trim();
  const scale = [c('--sun-wash'), c('--leaf-wash'), c('--sunken'), c('--rose-wash')];
  const vars = {
    background: 'transparent',
    primaryColor: c('--sun-wash'), primaryBorderColor: c('--sun-deep'),
    primaryTextColor: c('--ink'), secondaryColor: c('--leaf-wash'),
    tertiaryColor: c('--sunken'),
    lineColor: c('--line-2'), textColor: c('--ink'),
    fontFamily: 'Karla, sans-serif', fontSize: '13px',
    nodeBorder: c('--line-2'), edgeLabelBackground: c('--surface'),
  };
  for (let i = 0; i < 12; i++) {
    vars['cScale' + i] = scale[i % scale.length];
    vars['cScaleInv' + i] = c('--ink');
    vars['cScaleLabel' + i] = c('--ink');
    vars['cScalePeer' + i] = c('--line-2');
  }
  return vars;
}
function initMermaid() {
  if (HAS_MERMAID) mermaid.initialize({startOnLoad: false, theme: 'base',
                                       themeVariables: mermaidTheme()});
}
initMermaid();
matchMedia('(prefers-color-scheme: dark)').addEventListener?.('change', initMermaid);

const MEDIUM_ICON = {pdf: 'i-doc', markdown: 'i-doc', text: 'i-doc',
  html: 'i-web', audio: 'i-audio', video: 'i-audio'};
const FORMAT_ICON = {brief: 'i-doc', guide: 'i-library', podcast: 'i-audio',
  explainer: 'i-make', activity: 'i-practice', tutor: 'i-ask', lesson: 'i-library'};
const KIND_COLOR = {concept:'#8F6819', definition:'#2E6B4F', claim:'#4C554F',
  process:'#7A5EA8', step:'#3E7EA6', quantity:'#B5722C', event:'#A8556A',
  example:'#4E8B5E', question:'#7A837C'};

/* ── shell ───────────────────────────────────────────────────── */
function setCount(id, n, tone) {
  // A "0" chip beside a nav item reads as an empty state, not a count. Show a
  // number only when there is something there, and colour it when it's asking
  // for attention.
  const el = $(id);
  el.textContent = n;
  el.className = 'ct' + (n ? (tone ? ' ' + tone : '') : ' zero');
}
function setChrome(title, sub = '', actions = '') {
  $('#title').textContent = title;
  $('#subtitle').textContent = sub;
  $('#topactions').innerHTML = actions;
}
function go(view) {
  S.view = view;
  $$('#nav a').forEach(a => a.classList.toggle('on', a.dataset.view === view));
  $('#composer').style.display = view === 'ask' ? '' : 'none';
  $('#aside').style.display = (view === 'read' || view === 'make') ? '' : 'none';
  $('#scroll').scrollTop = 0;
  ({library: viewLibrary, read: viewRead, make: viewMake,
    ask: viewAsk, practice: viewPractice, checks: viewChecks}[view])();
}
$$('#nav a').forEach(a => a.onclick = () => {
  if (['read', 'make', 'ask'].includes(a.dataset.view) && !S.doc)
    return toast('Open something from your library first');
  go(a.dataset.view); closeRail();
});

/* ── off-canvas rail (narrow screens) ── */
function openRail() {
  $('#rail').classList.add('open');
  $('#railscrim').classList.add('on');
  $('#menu').setAttribute('aria-expanded', 'true');
}
function closeRail() {
  $('#rail').classList.remove('open');
  $('#railscrim').classList.remove('on');
  $('#menu').setAttribute('aria-expanded', 'false');
}
$('#menu').onclick = () =>
  $('#rail').classList.contains('open') ? closeRail() : openRail();
$('#railscrim').onclick = closeRail;

document.addEventListener('keydown', e => {
  if (e.target.matches('input,textarea')) { if (e.key === 'Escape') e.target.blur(); return; }
  if (e.key === '/') { e.preventDefault(); $('#search').focus(); }
  if (e.key === 'Escape') { closeRail(); closeSheet(); }
  if (e.key === 'n') { e.preventDefault(); openSheet(); }
  if (S.view === 'practice') {
    if (e.key === ' ') { e.preventDefault(); reveal(); }
    if (e.key === 'j') grade(false);
    if (e.key === 'k') grade(true);
  }
});

/* ── library ─────────────────────────────────────────────────── */
async function loadDocs() {
  S.docs = await api('/documents' +
    (S.collection ? `?collection=${encodeURIComponent(S.collection)}` : ''));
  setCount('#ct-docs', S.docs.length);
}

async function paintColls() {
  const colls = await api('/collections');
  $('#colls').innerHTML =
    `<div class="coll ${!S.collection ? 'on' : ''}" data-c=""><span class="dot"></span>
      Everything<span class="n">${S.docs.length}</span></div>` +
    colls.map(c => `<div class="coll ${S.collection === c.name ? 'on' : ''}"
      data-c="${esc(c.name)}"><span class="dot"></span>${esc(c.name)}
      <span class="n">${c.documents}</span></div>`).join('');
  $('#colls-list').innerHTML = colls.map(c => `<option value="${esc(c.name)}">`).join('');
  $$('#colls .coll').forEach(el => el.onclick = async () => {
    S.collection = el.dataset.c || null;
    closeRail();
    await loadDocs(); paintColls(); if (S.view === 'library') viewLibrary();
  });
}

async function viewLibrary() {
  await loadDocs(); paintColls();
  setChrome(S.collection || 'Library',
    `${S.docs.length} ${S.docs.length === 1 ? 'source' : 'sources'}`);
  if (!S.docs.length) {
    $('#view').innerHTML = `<div class="page"><div class="empty rise-in">
      <svg class="art" viewBox="0 0 32 32"><rect width="32" height="32" rx="7.4" fill="var(--sunken)"/>
        <circle cx="16" cy="30" r="12.5" fill="var(--line-2)"/></svg>
      <h3>Nothing in here yet</h3>
      <p>Add a lecture recording, a PDF, an article — anything you're working
         through. Sunroom reads it once and can give it back to you as a brief,
         a podcast, a practice set, or a tutor to ask.</p>
      <div style="margin-top:1.3rem"><button class="btn sun" onclick="openSheet()">
        Add your first source</button></div>
    </div></div>`;
    return;
  }
  $('#view').innerHTML = `<div class="page"><div class="cards rise-in">
    ${S.docs.map(d => `
      <div class="doccard" data-id="${d.id}" role="button" tabindex="0">
        <h3>${esc(d.title)}</h3>
        <div class="meta">
          <span class="kind">${icon(MEDIUM_ICON[d.medium] || 'i-doc')}${esc(d.medium)}</span>
          <span>${d.nodes} passages</span>
          ${d.collection ? `<span class="coll-tag">${esc(d.collection)}</span>` : ''}
        </div>
        <button class="rm" data-rm="${d.id}" aria-label="Remove ${esc(d.title)}"
          title="Remove from library">${icon('i-trash')}</button>
        <div class="confirm">
          <p>Remove this and everything made from it?</p>
          <div class="chips">
            <button class="btn quiet sm" data-cancel>Keep it</button>
            <button class="btn danger sm" data-yes="${d.id}">Remove</button>
          </div>
        </div>
      </div>`).join('')}
  </div></div>`;
  $$('.doccard').forEach(c => {
    c.onclick = e => {
      if (e.target.closest('.rm') || e.target.closest('.confirm')) return;
      openDoc(c.dataset.id, 'read');
    };
    c.onkeydown = e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openDoc(c.dataset.id, 'read'); }
    };
  });
  // An in-card confirm rather than window.confirm: a browser modal drops you
  // out of the app, and deleting a source is not reversible here.
  $$('.doccard .rm').forEach(b => b.onclick = () => {
    $$('.doccard').forEach(c => c.classList.remove('confirming'));
    b.closest('.doccard').classList.add('confirming');
  });
  $$('.doccard [data-cancel]').forEach(b => b.onclick = () =>
    b.closest('.doccard').classList.remove('confirming'));
  $$('.doccard [data-yes]').forEach(b => b.onclick = async () => {
    b.disabled = true; b.textContent = 'Removing…';
    try {
      await api('/documents/' + b.dataset.yes, {method: 'DELETE'});
      if (S.doc && S.doc.id === b.dataset.yes) S.doc = null;
      toast('Removed');
      viewLibrary();
    } catch (e) { toast(e.message); b.disabled = false; b.textContent = 'Remove'; }
  });
}

/* ── open a document ─────────────────────────────────────────── */
async function openDoc(id, view) {
  setChrome('Opening…', '');
  $('#view').innerHTML = `<div class="page"><div class="empty">
    <div class="spinner" style="margin:0 auto"></div></div></div>`;
  S.doc = await api('/documents/' + id);
  S.spans = await api(`/documents/${id}/spans`);
  S.deliverable = null; S.thread = []; S.activePart = null;
  go(view || 'read');
}

/* ── read ────────────────────────────────────────────────────── */
function viewRead() {
  const d = S.doc;
  setChrome(d.title, `${d.medium} · ${S.spans.filter(s => s.node_count).length} cited passages`,
    `<button class="btn quiet sm" onclick="openOriginal()">
       ${icon('i-ext')}Open original</button>`);

  const marks = [];
  const claimed = [];
  for (const s of [...S.spans].filter(s => s.node_count > 0 && !s.is_section)
        .sort((a, b) => (a.length - b.length) || (a.start - b.start))) {
    if (claimed.some(c => s.start < c.end && s.end > c.start)) continue;
    claimed.push(s); marks.push(s);
  }
  marks.sort((a, b) => a.start - b.start);

  // Where a logical section starts, its title occupies the first line. Without
  // this the heading runs straight into the first sentence, because a single
  // newline is otherwise just where the extractor wrapped.
  const heads = new Map();
  for (const sec of d.sections || []) {
    if (sec.physical || !sec.title) continue;
    // The heading line may carry its own markup. A markdown source keeps the
    // "## " in the text while the section title does not, so matching the
    // title against the raw line failed for every markdown document -- which
    // is most of them.
    const window_ = d.text.slice(sec.start, sec.start + sec.title.length + 12);
    const lead = (window_.match(/^(#{1,6}\s+|\s*[-*]\s+)/) || [''])[0];
    if (window_.slice(lead.length).startsWith(sec.title)) {
      // Consume the marker but do not print it. Skipping characters is safe:
      // the offsets that matter are the ones we advance `i` by, not the length
      // of what comes out.
      heads.set(sec.start, {text: sec.start + lead.length,
                            end: sec.start + lead.length + sec.title.length});
    }
  }

  // `at` is the absolute offset of this slice, so heading positions computed
  // against the whole document still line up inside a mark.
  const flow = (t, at) => {
    let out = '', i = 0;
    while (i < t.length) {
      const head = heads.get(at + i);
      if (head && head.end > at + i) {
        const skip = head.text - (at + i);          // the "## ", unprinted
        const n = head.end - head.text;
        out += `<b class="hd">${esc(t.slice(i + skip, i + skip + n))}</b>`;
        i += skip + n;
        while (t[i] === '\n') i++;      // the break is the element, not the newline
        continue;
      }
      const ch = t[i];
      if (ch === '\n') {
        let j = i;
        while (t[j] === '\n') j++;
        out += (j - i >= 2) ? '<span class="para"></span>' : ' ';
        i = j;
        continue;
      }
      out += esc(ch);
      i++;
    }
    return out;
  };

  let html = '', cur = 0;
  for (const s of marks) {
    if (s.start < cur) continue;
    html += flow(d.text.slice(cur, s.start), cur);
    html += `<mark data-span="${s.span_id}" title="${esc(s.locator)}">${
      flow(d.text.slice(s.start, s.end), s.start)}</mark>`;
    cur = s.end;
  }
  html += flow(d.text.slice(cur), cur);

  $('#view').innerHTML = `<div class="page rise-in"><div class="reader">
    <div class="src">${html}</div></div></div>`;
  $$('.src mark').forEach(m => m.onclick = () => traceSpan(m.dataset.span, m));

  asideIntro('Every highlighted passage is one Sunroom can cite. Tap one to see '
    + 'what it understood and what it has made from it.');
}
window.openOriginal = () => {
  const a = S.spans.find(s => s.anchor)?.anchor;
  a ? window.open(a, '_blank') : toast('This source has no external file to open');
};

function asideIntro(text) {
  $('#aside-body').innerHTML = `<p class="small muted">${esc(text)}</p>`;
}

async function traceSpan(spanId, el) {
  $$('.src mark').forEach(m => m.classList.remove('on'));
  el?.classList.add('on');
  $('#aside-body').innerHTML = `<div class="spinner"></div>`;
  const t = await api(`/documents/${S.doc.id}/trace?span_id=${spanId}`);
  const by = {};
  t.outputs.forEach(o => (by[o.renderer] ||= []).push(o));
  $('#aside-body').innerHTML = `<div class="rise-in">
    ${citeCard(t.spans[0])}
    <div class="eyebrow" style="margin:1.4rem 0 .5rem">What Sunroom understood</div>
    ${t.nodes.length ? t.nodes.map(n => `<div class="kv">
        <span style="color:${KIND_COLOR[n.kind] || 'inherit'}">${esc(n.kind)}</span>
        <b>${esc(n.label)}</b></div>`).join('')
      : '<p class="small muted">Nothing was extracted from this passage.</p>'}
    <div class="eyebrow" style="margin:1.4rem 0 .5rem">
      Used in ${t.outputs.length} ${t.outputs.length === 1 ? 'place' : 'places'}</div>
    ${t.outputs.length ? Object.entries(by).map(([r, list]) => `
        <div style="margin-bottom:.9rem">
          <div class="chip plain" style="margin-bottom:.4rem">${esc(r)} · ${list.length}</div>
          ${list.slice(0, 3).map(o => `<div class="part" style="cursor:default;margin-bottom:.4rem">
            <div class="bd small">${esc(o.content)}</div></div>`).join('')}
        </div>`).join('')
      : `<p class="small muted">Nothing made from this yet. Head to
         <strong>Make</strong> and it will show up here.</p>`}
  </div>`;
}

function citeCard(c) {
  if (!c) return '';
  const note = {pdf: 'opens the PDF at this page', textfragment: 'scrolls to this sentence',
    media: 'opens at this timestamp', line: 'opens the file at this line',
    internal: 'in-app reference'}[c.anchor_kind] || '';
  return `<div class="cite">
    <div class="loc"><span>${esc(c.locator)}</span>
      <span class="badge">${esc(c.anchor_kind === 'textfragment' ? 'web' : c.anchor_kind)}</span></div>
    <blockquote>“${esc((c.quote || '').slice(0, 380))}”</blockquote>
    <div class="acts">
      <button onclick="window.open('${esc(c.anchor)}','_blank')" title="${esc(note)}">Open source</button>
      <button onclick="copy(${JSON.stringify(c.anchor)})">Copy link</button>
      <button onclick="copy(${JSON.stringify((c.quote || '').slice(0, 380))})">Copy quote</button>
    </div></div>`;
}

/* ── make ────────────────────────────────────────────────────── */
function viewMake() {
  setChrome(S.doc.title, 'Choose what to turn it into');
  $('#view').innerHTML = `<div class="page">
    <div class="formats rise-in" id="fmts">
      ${S.formats.map(f => `<button class="fmt" data-f="${f.name}">
        ${icon(FORMAT_ICON[f.name] || 'i-doc', 'ic')}
        <span class="nm">${esc(f.label)}</span>
        <span class="job">${esc(f.job)}</span>
        ${f.tier !== 'production' ? `<span class="tier">${esc(f.tier)}</span>` : ''}
      </button>`).join('')}
    </div>
    <div id="out" style="margin-top:1.8rem"></div>
  </div>`;
  $$('#fmts .fmt').forEach(b => b.onclick = () => makeFormat(b.dataset.f));
  asideIntro('Pick a format. Every part of what comes out will point back to the '
    + 'passage it came from.');
  if (S.deliverable) {
    $$('#fmts .fmt').forEach(b =>
      b.classList.toggle('on', b.dataset.f === S.deliverable.format));
    drawDeliverable(S.deliverable);
  }
}

async function makeFormat(name) {
  $$('#fmts .fmt').forEach(b => b.classList.toggle('on', b.dataset.f === name));
  $('#out').innerHTML = `<div class="empty"><div class="spinner" style="margin:0 auto .8rem"></div>
    <p class="small">Reading your source and writing it…</p></div>`;
  try {
    S.deliverable = await post(`/documents/${S.doc.id}/format/${name}`);
  } catch (e) {
    S.deliverable = null;
    $('#out').innerHTML = `<div class="notice rise-in">
      <strong>${esc(name)} isn't a good fit for this source.</strong><br>
      ${esc(e.message)}</div>`;
    return;
  }
  drawDeliverable(S.deliverable);
}

function drawDeliverable(d) {
  const f = d.fidelity;
  const clean = f.passed && !f.findings.length;
  $('#out').innerHTML = `<div class="rise-in">
    <div class="chips" style="margin-bottom:1.1rem">
      <span class="chip ${clean ? 'ok' : 'warn'}">
        ${icon('i-tick')}${clean ? 'Verified' : 'Verified with notes'}</span>
      <span class="chip plain">${d.citations.length} sources</span>
      <span class="chip plain">covers ${pct(f.coverage)} of the source</span>
      ${d.tier !== 'production' ? `<span class="chip warn">${esc(d.tier)}</span>` : ''}
      <div class="sp" style="flex:1"></div>
      <button class="btn quiet sm" onclick="downloadDeliverable()">Download</button>
    </div>
    <p class="small muted" style="margin:-.4rem 0 1.3rem">
      ${clean
        ? 'Every line below traces back to a passage in your source.'
        : esc(f.findings.map(x => x.message).join(' '))}
    </p>
    <div id="parts"></div>
    <details style="margin-top:1.4rem">
      <summary class="small muted" style="cursor:pointer">The plain text version</summary>
      <pre class="raw" style="margin-top:.7rem">${esc(d.artifact)}</pre>
    </details>
  </div>`;

  $('#parts').innerHTML = d.parts.map((p, i) => `
    <div class="part ${p.asserts ? '' : 'instruction'}" data-i="${i}">
      <div class="role"><span>${esc(p.role.replace(/_/g, ' '))}</span>
        ${p.asserts ? '' : '<span>instruction</span>'}
        ${footnotes(p.footnotes)}
      </div>
      ${p.title ? `<h4>${esc(p.title)}</h4>` : ''}
      <div class="bd" id="pb-${i}"></div>
      ${extras(p)}
      <div class="srcchips">${p.spans.slice(0, 4).map(s =>
        `<button class="srcchip" data-anchor="${esc(s.citation.anchor)}">${esc(s.locator)}</button>`
      ).join('')}</div>
    </div>`).join('') || '<div class="empty"><p>Nothing to show.</p></div>';

  d.parts.forEach((p, i) => {
    const el = document.getElementById('pb-' + i);
    if (!el) return;
    if (p.meta?.language === 'mermaid') drawDiagram(el, p.body);
    else if (isScript(p.body)) el.innerHTML = script(p.body);
    else el.innerHTML = md(p.body);
  });

  $$('#parts .part').forEach(n => n.onclick = e => {
    if (e.target.classList.contains('srcchip'))
      return window.open(e.target.dataset.anchor, '_blank');
    $$('#parts .part').forEach(x => x.classList.remove('on'));
    n.classList.add('on');
    showSources(d.parts[+n.dataset.i]);
  });
}

/* A narration script is dialogue, not prose. Running the turns together into a
   paragraph -- which is what the markdown path does, correctly, for everything
   else -- makes a two-voice podcast unreadable. */
const SPEAKER = /^([A-Z][A-Z0-9 ._-]{1,18}):\s*(.*)$/;
function isScript(body) {
  const lines = String(body || '').split('\n').filter(l => l.trim());
  return lines.length > 1 && lines.filter(l => SPEAKER.test(l)).length >= lines.length - 1;
}
function script(body) {
  return String(body).split('\n').filter(l => l.trim()).map(line => {
    const m = line.match(SPEAKER);
    if (!m) return `<p class="turn"><span class="line">${esc(line)}</span></p>`;
    return `<p class="turn"><span class="who">${esc(m[1].toLowerCase())}</span>
      <span class="line">${md(m[2]).replace(/^<p>|<\/p>$/g, '')}</span></p>`;
  }).join('');
}

/* A part can derive from a great many passages, and printing every marker
   turns the header into a wall of numbers taller than the text under it.
   Show enough to be checkable, count the rest -- the full set is one click
   away in the panel, which is where someone verifying an output actually
   looks. */
const FOOTNOTE_CAP = 8;

function footnotes(list) {
  if (!list || !list.length) return '';
  const shown = list.slice(0, FOOTNOTE_CAP).map(n => '[' + n + ']').join('');
  const rest = list.length - FOOTNOTE_CAP;
  return `<span class="fn">${shown}${rest > 0
    ? `<span class="fn-more" title="${list.length} passages in all">+${rest}</span>`
    : ''}</span>`;
}

function extras(p) {
  const m = p.meta || {}; let h = '';
  if (m.caption) h += `<p class="small muted" style="margin-top:.6rem">${esc(m.caption)}</p>`;
  if (m.brief) h += `<p class="small muted" style="margin-top:.6rem">Illustration: ${esc(m.brief)}</p>`;
  if (m.teacher) h += `<p class="small" style="margin-top:.7rem"><strong>You say:</strong> ${esc(m.teacher)}</p>`;
  if (m.learner) h += `<p class="small"><strong>They produce:</strong> ${esc(m.learner)}</p>`;
  if (m.scrambled) h += `<ul style="margin-top:.6rem">${m.scrambled.map(x => `<li>${esc(x)}</li>`).join('')}</ul>`;
  if (m.categories) h += `<div class="chips" style="margin-top:.6rem">${
    m.categories.map(c => `<span class="chip plain">${esc(c)}</span>`).join('')}</div>`;
  if (m.decisions) h += m.decisions.map((d, i) => `<div style="margin-top:.9rem">
    <strong class="small">Decision ${i + 1}</strong>
    <p class="small" style="margin:.25rem 0">${esc(d.prompt)}</p>
    <ul>${(d.options || []).map(o => `<li>${esc(o)}</li>`).join('')}</ul></div>`).join('');
  if (m.rubric) h += `<p class="small" style="margin-top:.7rem"><strong>Looking for</strong></p>
    <ul>${m.rubric.map(r => `<li>${esc(r)}</li>`).join('')}</ul>`;
  if (m.answer != null && !m.decisions)
    h += `<div class="answer">${esc(Array.isArray(m.answer) ? m.answer.join(' → ') : m.answer)}</div>`;
  return h;
}

async function drawDiagram(el, source) {
  if (!HAS_MERMAID) { el.innerHTML = `<pre class="raw">${esc(source)}</pre>`; return; }
  try {
    await mermaid.parse(source);
    const {svg} = await mermaid.render('g' + Date.now(), source);
    el.innerHTML = `<div class="mermaid">${svg}</div>`;
  } catch {
    el.innerHTML = `<pre class="raw">${esc(source)}</pre>`;
    $$('body > svg[id^="d"], body > .mermaid-error').forEach(n => n.remove());
  }
}

function showSources(p) {
  $('#aside-body').innerHTML = `<div class="rise-in">
    <p class="small muted" style="margin-bottom:.9rem">
      This ${esc(p.role.replace(/_/g, ' '))} was built from
      ${p.spans.length} ${p.spans.length === 1 ? 'passage' : 'passages'}.</p>
    ${p.spans.map(s => citeCard(s.citation)).join('') ||
      '<p class="small muted">This part cites nothing — the check will flag it.</p>'}
    <button class="btn quiet sm" style="margin-top:.6rem" id="jump">Show me in the source</button>
  </div>`;
  const j = $('#jump');
  if (j) j.onclick = () => {
    const first = p.spans[0]; if (!first) return;
    go('read');
    requestAnimationFrame(() => {
      const m = $(`.src mark[data-span="${first.id}"]`);
      if (m) { m.scrollIntoView({block: 'center', behavior: 'smooth'}); traceSpan(first.id, m); }
    });
  };
}
window.downloadDeliverable = () => {
  const d = S.deliverable; if (!d) return;
  const ext = d.artifact_format === 'json' ? 'json' : 'md';
  const blob = new Blob([d.artifact], {type: 'text/plain'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${S.doc.title.replace(/\W+/g, '-').toLowerCase()}-${d.format}.${ext}`;
  a.click(); toast('Downloaded');
};

/* ── ask ─────────────────────────────────────────────────────── */
function viewAsk() {
  setChrome(S.doc.title, 'Ask anything — answers cite the source');
  const seeds = S.doc.nodes.filter(n => n.kind === 'definition' && !n.section).slice(0, 4)
    .map(n => `What does ${n.label} mean?`);
  $('#view').innerHTML = `<div class="thread" id="thread"></div>`;
  paintThread(seeds);
}

function paintThread(seeds = []) {
  const el = $('#thread');
  if (!S.thread.length) {
    el.innerHTML = `<div class="empty rise-in" style="padding-top:2.5rem">
      <h3>Ask this source anything</h3>
      <p>Answers are put together from what's actually in it, with the page or
         timestamp attached. If it can't answer, it says so instead of guessing.</p>
      <div class="chips" style="justify-content:center;margin-top:1.3rem">
        ${seeds.map(q => `<button class="chip plain seed">${esc(q)}</button>`).join('')}
      </div></div>`;
    $$('.seed').forEach(b => b.onclick = () => ask(b.textContent));
    return;
  }
  el.innerHTML = S.thread.map(m => `
    <div class="qa ${m.covered === false ? 'uncovered' : ''} rise-in">
      <div class="q">${esc(m.question)}</div>
      <div class="a">
        ${m.pending ? '<div class="spinner"></div>' : md(m.answer)}
        ${!m.pending && m.covered === false ? `<p class="small muted" style="margin:.7rem 0 0">
          Answering anyway would be guessing.</p>` : ''}
        ${(m.citations || []).map(citeCard).join('')}
      </div></div>`).join('');
  $('#scroll').scrollTop = $('#scroll').scrollHeight;
}

async function ask(q) {
  const question = (q || $('#ask-input').value).trim();
  if (!question) return;
  if (!S.doc) return toast('Open something from your library first');
  $('#ask-input').value = '';
  S.thread.push({question, pending: true});
  paintThread();
  const slot = S.thread[S.thread.length - 1];
  try {
    const a = await post(`/documents/${S.doc.id}/ask`, {question});
    Object.assign(slot, {pending: false, answer: a.answer,
                         covered: a.covered, citations: a.citations});
  } catch (e) {
    Object.assign(slot, {pending: false, answer: 'Something went wrong: ' + e.message,
                         covered: false});
  }
  paintThread();
}
$('#ask-send').onclick = () => ask();
$('#ask-input').addEventListener('keydown', e => { if (e.key === 'Enter') ask(); });

/* ── practice ────────────────────────────────────────────────── */
async function viewPractice() {
  setChrome('Practice', '');
  S.due = await api('/review');
  setCount('#ct-due', S.due.length, 'due');
  S.idx = 0; S.revealed = false;
  paintCard();
}
function paintCard() {
  if (!S.due.length) {
    setChrome('Practice', 'Nothing due');
    $('#view').innerHTML = `<div class="page"><div class="empty rise-in">
      <svg class="art" viewBox="0 0 24 24" style="color:var(--line-2)">
        <use href="#i-practice"/></svg>
      <h3>All caught up</h3>
      <p>Make a <strong>practice set</strong> from something in your library and
         the cards will show up here, spaced out over time so they stick.</p>
      <div style="margin-top:1.3rem"><button class="btn sun" id="topractice">
        ${S.doc ? 'Make a practice set' : 'Pick something to practise'}</button></div>
    </div></div>`;
    $('#topractice').onclick = () => {
      if (!S.doc) return go('library');
      go('make'); setTimeout(() => { const b = $('.fmt[data-f=activity]'); if (b) b.click(); }, 40);
    };
    return;
  }
  const c = S.due[S.idx];
  setChrome('Practice', `${S.idx + 1} of ${S.due.length}`);
  $('#view').innerHTML = `<div class="page narrow"><div class="card-stage rise-in">
    <div class="progress"><i style="width:${(S.idx / S.due.length) * 100}%"></i></div>
    <div class="flash">
      <div class="eyebrow" style="margin-bottom:1rem">${esc(c.kind)} · ${esc(c.document)}</div>
      <div class="prompt">${md(c.prompt)}</div>
      ${S.revealed ? `<div class="reveal">${esc(c.answer)}</div>` : ''}
    </div>
    ${S.revealed ? `
      <div class="grades">
        <button class="btn quiet" id="miss">Missed it</button>
        <button class="btn sun" id="got">Got it</button></div>
      ${(c.citations || []).length ? `<div style="margin-top:1.3rem">
        ${c.citations.map(citeCard).join('')}</div>` : ''}
      `: `<div class="grades"><button class="btn quiet" id="rev">Show me</button></div>`}
  </div></div>`;
  if ($('#rev')) $('#rev').onclick = reveal;
  if ($('#got')) $('#got').onclick = () => grade(true);
  if ($('#miss')) $('#miss').onclick = () => grade(false);
}
function reveal() { if (S.due.length) { S.revealed = true; paintCard(); } }
async function grade(ok) {
  if (!S.due.length || !S.revealed) return;
  await post('/review', {node_id: S.due[S.idx].node_id, collection: null, correct: ok});
  toast(ok ? "Nice — you'll see it again later" : "No problem, it'll come back soon");
  S.revealed = false; S.idx += 1;
  if (S.idx >= S.due.length) return viewPractice();
  paintCard();
}

/* ── checks ──────────────────────────────────────────────────── */
async function viewChecks() {
  setChrome('Checks', 'Everything Sunroom has made for you');
  $('#view').innerHTML = `<div class="page"><div class="empty">
    <div class="spinner" style="margin:0 auto"></div></div></div>`;
  const a = await api('/audit');
  setCount('#ct-prob', a.problems.length, 'bad');
  if (!a.total) {
    $('#view').innerHTML = `<div class="page"><div class="empty rise-in">
      <h3>Nothing to check yet</h3>
      <p>Once you've made a brief, a podcast or a practice set, Sunroom
         re-checks each one against your source and reports anything that
         drifted.</p></div></div>`;
    return;
  }
  if (!a.problems.length) {
    $('#view').innerHTML = `<div class="page narrow rise-in">
      <div class="empty" style="padding-bottom:1.6rem">
        <div class="chip ok" style="margin-bottom:1rem">${icon('i-tick')}All clear</div>
        <h3>All ${a.total} ${a.total === 1 ? 'piece checks' : 'pieces check'} out</h3>
        <p>Every line in everything you've made traces back to your sources, and
           nothing has gone stale against a source you've since changed.</p>
      </div>
      ${auditList(a.clean_items || [])}</div>`;
    bindAudit();
    return;
  }
  $('#view').innerHTML = `<div class="page rise-in">
    <p class="small muted" style="margin-bottom:1.2rem">
      ${a.clean} of ${a.total} are clean. These need a look.</p>
    <table><thead><tr>
      <th>Source</th><th>Made as</th><th>Covers</th><th>Status</th><th>Why</th>
    </tr></thead><tbody>
    ${a.problems.map(p => `<tr>
      <td class="ink">${esc(p.document)}</td>
      <td>${esc(p.renderer)}</td>
      <td>${pct(p.coverage)}</td>
      <td>${p.stale ? '<span class="chip warn">source changed</span>'
            : p.passed ? '<span class="chip warn">worth a look</span>'
                       : '<span class="chip bad">failed</span>'}</td>
      <td>${esc(p.findings.map(f => f.message).join(' ')) || '—'}</td>
    </tr>`).join('')}</tbody></table>
    ${auditList(a.clean_items || [])}</div>`;
  bindAudit();
}

const fmtLabel = name =>
  (S.formats.find(f => f.name === name) || {}).label || name;

function auditList(items) {
  if (!items.length) return '';
  return `<div style="margin-top:2rem">
    <div class="eyebrow" style="margin-bottom:.8rem">Checked and clean</div>
    <div class="rowlist">${items.map(it => `
      <button class="rowitem" data-id="${esc(it.understanding)}">
        <span class="ico-wrap">${icon(FORMAT_ICON[it.renderer] || 'i-doc')}</span>
        <span class="rl-main"><span class="rl-title">${esc(fmtLabel(it.renderer))}</span>
          <span class="rl-sub">${esc(it.document)} · covers ${pct(it.coverage)}</span></span>
        <span class="chip ok">${icon('i-tick')}clean</span>
      </button>`).join('')}</div></div>`;
}
function bindAudit() {
  $$('#view .rowitem').forEach(el => el.onclick = () => openDoc(el.dataset.id, 'make'));
}

/* ── search ──────────────────────────────────────────────────── */
let searchT;
$('#search').oninput = e => {
  clearTimeout(searchT);
  const q = e.target.value.trim();
  searchT = setTimeout(async () => {
    if (q.length < 2) { if (S.view === 'library') viewLibrary(); return; }
    const hits = await api('/search?q=' + encodeURIComponent(q));
    // Not go('library'): viewLibrary is async, so its own setChrome and
    // innerHTML land *after* these and overwrite the results with an empty
    // library. Search is its own view; paint it directly.
    S.view = 'search';
    $$('#nav a').forEach(a => a.classList.remove('on'));
    $('#composer').style.display = 'none';
    $('#aside').style.display = 'none';
    $('#scroll').scrollTop = 0;
    setChrome('Search', `${hits.length} ${hits.length === 1 ? 'result' : 'results'} for “${q}”`);
    $('#view').innerHTML = `<div class="page rise-in">
      ${hits.length ? hits.map(h => `<button class="part" data-id="${h.understanding}"
          style="width:100%;text-align:left">
          <div class="role"><span style="color:${KIND_COLOR[h.kind] || ''}">${esc(h.kind)}</span>
            <span>${esc(h.title)}</span></div>
          <div class="bd">${esc(h.body || h.label)}</div></button>`).join('')
        : '<div class="empty"><h3>No matches</h3><p>Try a different word.</p></div>'}
    </div>`;
    $$('#view .part').forEach(n => n.onclick = () => openDoc(n.dataset.id, 'read'));
  }, 220);
};

/* ── add sheet ───────────────────────────────────────────────── */
let pending = null;   // a File the person picked or dropped

function openSheet() { $('#sheet').classList.add('on'); setTimeout(() => $('#src').focus(), 60); }
function closeSheet() { $('#sheet').classList.remove('on'); }
function resetSheet() {
  pending = null; $('#file').value = '';
  $('#src').value = ''; $('#ttl').value = '';
  $('#addmsg').textContent = '';
  paintDrop();
}
function paintDrop() {
  const d = $('#drop');
  d.classList.toggle('has', !!pending);
  // One obvious path at a time: with a file in hand, the paste box recedes
  // rather than sitting there as a competing option.
  $('#sheet').classList.toggle('filepicked', !!pending);
  d.querySelector('p').innerHTML = pending
    ? `<strong>${esc(pending.name)}</strong> — <button type="button" class="linkish"
         id="unpick">remove</button>`
    : `<button type="button" class="linkish" id="pick">Choose a file</button>
       or drop one here`;
  d.querySelector('.small').textContent = pending
    ? readableSize(pending.size)
    : 'PDF, audio, video, a web page you saved, plain text';
  const pick = $('#pick'); if (pick) pick.onclick = () => $('#file').click();
  const un = $('#unpick'); if (un) un.onclick = () => { pending = null; $('#file').value = ''; paintDrop(); };
}
const readableSize = n => n < 1024 ? n + ' B'
  : n < 1048576 ? (n / 1024).toFixed(0) + ' KB' : (n / 1048576).toFixed(1) + ' MB';

window.openSheet = openSheet;
$('#add-open').onclick = openSheet;
$('#add-cancel').onclick = closeSheet;
$('#sheet').onclick = e => { if (e.target.id === 'sheet') closeSheet(); };
$('#file').onchange = e => { pending = e.target.files[0] || null; paintDrop(); estimateSoon(); };

let estT;
function estimateSoon() {
  clearTimeout(estT);
  estT = setTimeout(async () => {
    const chars = pending ? pending.size : $('#src').value.length;
    const el = $('#addmsg');
    // Below a few pages the number is noise; nobody needs to be warned about
    // an article.
    if (!chars || chars < 20000) { if (el.dataset.est) { el.textContent = ''; delete el.dataset.est; } return; }
    try {
      const r = await post('/estimate', {chars});
      el.dataset.est = '1';
      el.className = 'small muted' + (r.affordable ? '' : ' bad');
      el.textContent = r.affordable
        ? `About ${fmtTokens(r.estimate.total_tokens)} tokens to read this`
          + (r.usage.byo ? '.' : ` — roughly ${Math.round(r.estimate.total_tokens / r.usage.budget * 100)}% of your month.`)
        : `This needs about ${fmtTokens(r.estimate.total_tokens)} tokens and `
          + `you have ${fmtTokens(r.usage.remaining)} left. Add your own key in `
          + `your account settings to go past the limit.`;
    } catch (e) { /* an estimate is a nicety, not a gate */ }
  }, 350);
}
$('#src').addEventListener('input', estimateSoon);
paintDrop();

// Drag and drop, guarded against the browser's default "open the file" behaviour
// firing when someone misses the target.
['dragenter', 'dragover'].forEach(ev => $('#drop').addEventListener(ev, e => {
  e.preventDefault(); $('#drop').classList.add('over');
}));
['dragleave', 'drop'].forEach(ev => $('#drop').addEventListener(ev, e => {
  e.preventDefault(); $('#drop').classList.remove('over');
}));
$('#drop').addEventListener('drop', e => {
  const f = e.dataTransfer?.files?.[0];
  if (f) { pending = f; $('#file').value = ''; paintDrop(); estimateSoon(); }
});
window.addEventListener('dragover', e => e.preventDefault());
window.addEventListener('drop', e => e.preventDefault());

/* Uploading goes straight to storage, then we hand the server a key. The
   bytes never pass through the API: a 90 MB recording would blow the request
   body limit and the function's whole time budget before any reading started. */
async function uploadPending(file, onProgress) {
  const ticket = await post('/uploads/sign',
                            {filename: file.name, size: file.size});
  await new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(ticket.method || 'PUT', ticket.url, true);
    // The signed URL carries its own auth; our bearer only belongs on our API.
    if (ticket.url.startsWith('/api/')) {
      const t = auth.token();
      if (t) xhr.setRequestHeader('Authorization', 'Bearer ' + t);
    }
    for (const [k, v] of Object.entries(ticket.headers || {})) {
      xhr.setRequestHeader(k, v);
    }
    if (ticket.token) {
      xhr.setRequestHeader('Authorization', 'Bearer ' + ticket.token);
    }
    xhr.upload.onprogress = e => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => (xhr.status >= 200 && xhr.status < 300)
      ? resolve() : reject(new Error(`Upload failed (${xhr.status})`));
    xhr.onerror = () => reject(new Error('Upload failed. Check your connection.'));
    xhr.send(file);
  });
  return ticket.key;
}

$('#add-go').onclick = async () => {
  const source = $('#src').value.trim();
  if (!pending && !source) return toast('Choose a file, or paste something in');
  const btn = $('#add-go');
  btn.disabled = true;
  $('#addmsg').textContent = '';
  $('#addmsg').className = 'small muted';

  try {
    let payload;
    if (pending) {
      btn.textContent = 'Uploading…';
      const key = await uploadPending(pending, f => {
        btn.textContent = `Uploading ${Math.round(f * 100)}%`;
      });
      payload = {source: key, kind: 'storage', filename: pending.name};
    } else {
      payload = {source,
                 kind: /^https?:\/\//i.test(source) ? 'url' : 'text'};
    }
    payload.title = $('#ttl').value || null;
    payload.collection = $('#coll').value || null;

    btn.textContent = 'Adding…';
    const r = await post('/documents', payload);
    resetSheet();
    closeSheet();
    S.jobs = [r.job, ...S.jobs.filter(j => j.id !== r.job.id)];
    paintJobs();
    pollJobs();
    toast('Reading it now — this can take a minute');
  } catch (e) {
    $('#addmsg').textContent = e.code === 'quota'
      ? e.message + ' You can add your own key in your account settings.'
      : e.message;
    $('#addmsg').className = 'small muted bad';
  }
  btn.disabled = false; btn.textContent = 'Add to library';
};

/* ── the account, the meter, the settings sheet ──────────────── */

const fmtTokens = n =>
  n >= 1e6 ? (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M'
  : n >= 1e3 ? Math.round(n / 1e3) + 'k' : String(n);

function paintAccount() {
  const btn = $('#account');
  if (!S.me) { btn.hidden = true; return; }
  btn.hidden = false;
  $('#account-email').textContent = S.me.email || 'Your account';
  const u = S.me.usage || {};
  const meter = $('#account-meter');
  if (u.byo) {
    meter.hidden = true;
    $('#account-usage').textContent = 'Your own key · no limit';
    return;
  }
  meter.hidden = false;
  meter.classList.toggle('high', (u.fraction || 0) > 0.85);
  meter.querySelector('i').style.width = Math.round((u.fraction || 0) * 100) + '%';
  $('#account-usage').textContent =
    `${fmtTokens(u.used || 0)} of ${fmtTokens(u.budget || 0)} this month`;
}

async function refreshMe() {
  try { S.me = await api('/me'); paintAccount(); } catch (e) { /* gate handles it */ }
}

function openSettings() {
  $('#settings').classList.add('on');
  const u = (S.me && S.me.usage) || {};
  $('#set-email').textContent = (S.me && S.me.email) || '';
  $('#set-usage').innerHTML = u.byo
    ? `<div class="row"><span class="k">Billing</span>
         <span class="v">Your own Anthropic key ····${esc((S.me && S.me.byo_key_hint) || '')}</span></div>
       <div class="row"><span class="k">Used this month</span>
         <span class="v">${fmtTokens(u.total || 0)} tokens</span></div>`
    : `<div class="row"><span class="k">Used this month</span>
         <span class="v">${fmtTokens(u.used || 0)} of ${fmtTokens(u.budget || 0)}</span></div>
       <div class="row"><span class="k">Model calls</span>
         <span class="v">${(u.calls || 0).toLocaleString()}</span></div>
       <span class="meter ${(u.fraction || 0) > 0.85 ? 'high' : ''}">
         <i style="width:${Math.round((u.fraction || 0) * 100)}%"></i></span>`;
  $('#set-clear').hidden = !(S.me && S.me.byo_key);
  // Signing out of a single-user deployment does nothing you would want.
  $('#set-signout').closest('.signout-row').hidden = !auth.multiUser;
  $('#set-key').value = '';
  $('#set-msg').textContent = '';
}

$('#account').onclick = openSettings;
$('#set-close').onclick = () => $('#settings').classList.remove('on');
$('#settings').onclick = e => {
  if (e.target.id === 'settings') $('#settings').classList.remove('on');
};
$('#set-save').onclick = async () => {
  const key = $('#set-key').value.trim();
  if (!key) return;
  const btn = $('#set-save');
  btn.disabled = true; btn.textContent = 'Saving…';
  try {
    await put('/settings/api-key', {api_key: key});
    await refreshMe();
    openSettings();
    toast('Key saved — no monthly limit now');
  } catch (e) { $('#set-msg').textContent = e.message; }
  btn.disabled = false; btn.textContent = 'Save key';
};
$('#set-clear').onclick = async () => {
  await del('/settings/api-key');
  await refreshMe();
  openSettings();
  toast('Key removed');
};
$('#set-signout').onclick = async () => { await auth.signOut(); };

/* ── work in progress ────────────────────────────────────────────
   Ingest is a job now, so the interface has to show one. The tray is the
   honest version of a spinner: it names the source, says which section is
   being read, and stays out of the way while you do something else. */

function paintJobs() {
  const tray = $('#tray');
  const live = S.jobs.filter(j => j.status !== 'done' || j._justDone);
  tray.innerHTML = live.map(j => {
    const bad = j.status === 'failed';
    const done = j.status === 'done';
    const pct = Math.round((j.progress || 0) * 100);
    return `<div class="jobcard ${bad ? 'bad' : done ? 'ok' : ''}" data-id="${esc(j.id)}">
      <div class="top"><span class="nm">${esc(j.title || 'New source')}</span>
        <button class="x" data-dismiss="${esc(j.id)}">${done || bad ? 'Dismiss' : 'Cancel'}</button></div>
      <div class="msg">${esc(bad ? j.error || 'Something went wrong'
                              : done ? j.message || 'Ready' : j.message || 'Queued')}</div>
      ${done || bad ? '' :
        `<span class="meter"><i style="width:${pct}%"></i></span>`}
    </div>`;
  }).join('');

  $$('#tray [data-dismiss]').forEach(b => b.onclick = async () => {
    const id = b.dataset.dismiss;
    const job = S.jobs.find(j => j.id === id);
    if (job && (job.status === 'queued' || job.status === 'running')) {
      try { await del('/jobs/' + id); } catch (e) { /* already gone */ }
    }
    S.jobs = S.jobs.filter(j => j.id !== id);
    paintJobs();
  });
}

async function pollJobs() {
  if (!auth.signedIn()) return;
  let active;
  try { active = await api('/jobs?active=true'); } catch (e) { return; }

  const finished = S.jobs.filter(
    old => old.status === 'queued' || old.status === 'running')
    .filter(old => !active.some(j => j.id === old.id));

  for (const old of finished) {
    let job;
    try { job = await api('/jobs/' + old.id); } catch (e) { continue; }
    job._justDone = true;
    active.push(job);
    if (job.status === 'done' && job.understanding) {
      toast(`${job.title || 'Your source'} is ready`);
      await loadDocs(); paintColls();
      if (S.view === 'library') viewLibrary();
      // Clear the card after a moment; the document is in the library now.
      setTimeout(() => {
        S.jobs = S.jobs.filter(j => j.id !== job.id);
        paintJobs();
      }, 6000);
    }
    refreshMe();
  }

  // Keep failures on screen until dismissed: an error nobody sees is an error
  // that gets reported as "it just didn't work".
  const keep = S.jobs.filter(j => j.status === 'failed'
                             && !active.some(a => a.id === j.id));
  S.jobs = [...active, ...keep];
  paintJobs();

  const busy = S.jobs.some(j => j.status === 'queued' || j.status === 'running');
  clearTimeout(S.polling);
  if (busy) S.polling = setTimeout(pollJobs, 1500);
}

/* ── boot ────────────────────────────────────────────────────── */

function showGate(on, message) {
  $('#gate').hidden = !on;
  document.body.style.overflow = on ? 'hidden' : '';
  if (!message) return;
  const el = $('#gate-msg');
  el.textContent = message;
  el.className = 'small muted bad';
  // A form that cannot work should not invite someone to fill it in and find
  // that out after typing their address.
  $('#gate-email').disabled = true;
  $('#gate-go').disabled = true;
  $('#gate-go').textContent = 'Unavailable';
}

$('#gate-form').onsubmit = async e => {
  e.preventDefault();
  const email = $('#gate-email').value.trim();
  const msg = $('#gate-msg');
  const btn = $('#gate-go');
  if (!email || !email.includes('@')) {
    msg.textContent = 'Enter the email address you want the link sent to.';
    msg.className = 'small muted bad';
    return;
  }
  btn.disabled = true; btn.textContent = 'Sending…';
  try {
    await auth.sendLink(email);
    msg.textContent = `Sent. Open the link in the email to ${email} — it signs `
      + `you in on this device.`;
    msg.className = 'small muted sent';
    $('#gate-form').reset();
  } catch (err) {
    msg.textContent = err.message || 'That did not send. Try again in a moment.';
    msg.className = 'small muted bad';
  }
  btn.disabled = false; btn.textContent = 'Email me a link';
};

async function start() {
  try {
    const m = await api('/media');
    S.formats = m.formats || [];
    await refreshMe();
    await loadDocs(); await paintColls();
    const due = await api('/review');
    setCount('#ct-due', due.length, 'due');
    go('library');
    pollJobs();
  } catch (e) {
    if (e.status === 401) return;          // the gate is already up
    $('#view').innerHTML = `<div class="page"><div class="notice">
      Sunroom can't reach its engine. ${esc(e.message)}</div></div>`;
  }
}

(async () => {
  const state = await auth.ready(signedIn => {
    if (signedIn) { showGate(false); start(); }
    else { S.me = null; paintAccount(); showGate(true); }
  });

  if (state.mode === 'broken') { showGate(true, state.error); return; }
  if (auth.signedIn()) { showGate(false); start(); }
  else showGate(true);
})();
