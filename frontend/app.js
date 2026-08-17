/* Зерно — клиент. Ванильный JS: снимок рынка приходит одним запросом и рисуется
   сразу, разборы локальной модели догружаются по одному и подставляются на месте
   заглушек. */

const UP = 'var(--color-accent-2-700)';
const DOWN = 'var(--color-accent-700)';
const MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля',
  'августа', 'сентября', 'октября', 'ноября', 'декабря'];
const WEEKDAYS = ['Воскресенье', 'Понедельник', 'Вторник', 'Среда', 'Четверг',
  'Пятница', 'Суббота'];

const state = {
  snapshot: null,
  selected: null,
  view: 'grid',
  filter: '',
  pending: new Set(),
  busy: false,
};

const $ = (id) => document.getElementById(id);

// ── сеть ────────────────────────────────────────────────────────────────────
async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* пустой ответ */ }
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `Запрос не удался (${res.status})`);
  }
  return data;
}

let toastTimer = null;
function toast(message) {
  let el = document.querySelector('.toast');
  if (!el) {
    el = document.createElement('div');
    el.className = 'toast';
    el.setAttribute('role', 'status');
    document.body.appendChild(el);
  }
  el.textContent = message;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.remove(), 5000);
}

// ── утилиты вывода ──────────────────────────────────────────────────────────
const esc = (value) => String(value ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const color = (token) => (token.up ? UP : DOWN);
const skeleton = (width) => `<span class="skeleton"${width ? ` style="width:${width}"` : ''}>&nbsp;</span>`;

function fmtPrice(value) {
  if (value == null) return '—';
  const nbsp = ' ';
  if (value >= 1000) return `$${Math.round(value).toLocaleString('ru-RU').replace(/\s/g, nbsp)}`;
  if (value >= 1) return `$${value.toFixed(2)}`;
  if (value >= 0.01) return `$${value.toFixed(4)}`;
  return `$${value.toFixed(6)}`;
}

function dateLabel() {
  const now = new Date();
  return `${WEEKDAYS[now.getDay()]}, ${now.getDate()} ${MONTHS[now.getMonth()]}`;
}

function sparkSvg(token, extraClass = '') {
  if (!token.spark) {
    return `<div class="spark ${extraClass} no-spark">${esc(historyNote(token))}</div>`;
  }
  return `<svg class="spark ${extraClass}" viewBox="0 0 120 34" preserveAspectRatio="none" aria-hidden="true">
    <polyline points="${esc(token.spark)}" fill="none" stroke="${color(token)}" stroke-width="2.75"
      stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke"></polyline>
  </svg>`;
}

function historyNote(token) {
  if (token.history_source) return '';
  return 'график появится, когда наберётся история';
}

function historyBanner() {
  const caps = (state.snapshot && state.snapshot.capabilities) || {};
  if (caps.history !== false) return '';
  return 'Тариф FreeCryptoAPI не отдаёт историю цен: недельные изменения и графики '
    + 'считаются из котировок, которые сервис накапливает сам.';
}

const tokens = () => (state.snapshot ? state.snapshot.tokens : []);
const current = () => tokens().find((t) => t.symbol === state.selected) || tokens()[0] || null;
const visible = () => tokens().filter((t) => !state.filter
  || t.symbol.includes(state.filter) || t.name.toUpperCase().includes(state.filter));

// ── отрисовка ───────────────────────────────────────────────────────────────
function renderAll() {
  const snap = state.snapshot;
  if (!snap) return;
  $('dateLabel').textContent = dateLabel();
  $('countLabel').textContent = `${snap.tokens.length} ${plural(snap.tokens.length, 'токен', 'токена', 'токенов')}`;
  $('fCount').textContent = $('countLabel').textContent;
  $('updatedNote').textContent = snap.tokens.length
    ? `Котировки обновились в ${snap.updated_text}.`
    : 'Список пуст — добавьте тикер.';

  const warn = $('warnBox');
  const notes = [];
  if (snap.warnings && snap.warnings.length) {
    notes.push(`Часть данных не пришла: ${snap.warnings.slice(0, 3).join('; ')}`);
  }
  const banner = historyBanner();
  if (banner) notes.push(banner);
  warn.classList.toggle('hidden', notes.length === 0);
  warn.textContent = notes.join(' ');

  renderSummary();
  renderSideList();
  renderCards();
  renderDetail();
  renderFocus();
  renderNotes();
}

function plural(n, one, few, many) {
  const mod10 = n % 10; const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return few;
  return many;
}

function renderSummary() {
  const snap = state.snapshot;
  const p = snap.portfolio || {};
  const analysis = snap.analysis;
  $('moodValue').textContent = p.mood || '—';
  $('riskValue').textContent = p.risk || '—';
  const fg = snap.fear_greed;
  const fgAvailable = fg && fg.index != null;
  $('fgBlock').classList.toggle('hidden', !fgAvailable);
  if (fgAvailable) $('fgValue').textContent = `${fg.index} · ${emotionRu(fg.emotion)}`;

  if (analysis) {
    $('headline').textContent = analysis.headline;
    $('summaryText').innerHTML = `${esc(analysis.summary)} ${sourceNote(analysis)}`;
  } else if (state.pending.has('portfolio')) {
    $('headline').innerHTML = skeleton('14ch');
    $('summaryText').innerHTML = `${skeleton()}<span class="text-muted" style="font-size:13px">Модель пишет сводку…</span>`;
  } else {
    $('headline').textContent = p.mood ? `${p.up} из ${p.count} в плюсе` : 'Нет данных';
    $('summaryText').innerHTML = '<span class="text-muted">Разбор ещё не собран — нажмите «Обновить разбор».</span>';
  }
}

function emotionRu(value) {
  return { fear: 'страх', greed: 'жадность', neutral: 'нейтрально' }[value] || (value || '—');
}

function sourceNote(analysis) {
  if (!analysis || !analysis._source) return '';
  const label = analysis._source === 'llm'
    ? `модель ${esc(analysis._model || '')}`
    : 'текст по правилам, модель была недоступна';
  return `<span class="text-muted" style="font-size:12.5px"> · ${label}</span>`;
}

function renderSideList() {
  $('sideList').innerHTML = visible().map((t) => `
    <button class="pill-row" data-symbol="${esc(t.symbol)}" aria-current="${t.symbol === state.selected}">
      <span class="sym">${esc(t.symbol.slice(0, 4))}</span>
      <span class="name">${esc(t.name)}</span>
      <span class="chg" style="color:${color(t)}">${esc(t.change_24h_text)}</span>
    </button>`).join('') || '<div class="text-muted" style="font-size:13px">Ничего не найдено</div>';
}

function renderCards() {
  $('cards').innerHTML = visible().map((t) => {
    const a = t.analysis;
    const short = a ? esc(a.short)
      : (state.pending.has(t.symbol) ? skeleton() : `<span class="text-muted">${esc(t.trend)}</span>`);
    return `
    <article class="token-card" data-symbol="${esc(t.symbol)}" aria-current="${t.symbol === state.selected}" tabindex="0">
      <div class="head">
        <span class="sym">${esc(t.symbol.slice(0, 4))}</span>
        <div style="flex:1;min-width:0">
          <div class="title">${esc(t.name)}</div>
          <div class="price">${esc(t.price_text)}</div>
        </div>
        <div style="text-align:right">
          <div class="chg" style="color:${color(t)}">${esc(t.change_24h_text)}</div>
          <div class="chg-note">24 часа</div>
        </div>
      </div>
      ${sparkSvg(t)}
      <p>${short}</p>
      <div class="foot">
        <span class="tag tag-neutral">риск: ${esc(t.risk.toLowerCase())}</span>
        ${t.change_7d != null ? `<span class="tag tag-neutral">неделя: ${esc(t.change_7d_text)}</span>` : ''}
      </div>
    </article>`;
  }).join('');
}

function renderDetail() {
  const t = current();
  const box = $('detail');
  if (!t) { box.innerHTML = '<div class="text-muted">Список пуст.</div>'; return; }
  const a = t.analysis;
  const pending = state.pending.has(t.symbol);
  box.innerHTML = `
    <div class="detail-head">
      <span class="sym">${esc(t.symbol.slice(0, 4))}</span>
      <div style="flex:1;min-width:0">
        <div class="title">${esc(t.name)}</div>
        <div class="meta">${[
          esc(t.price_text),
          `${esc(t.change_24h_text)} за сутки`,
          t.change_7d != null ? `${esc(t.change_7d_text)} за неделю` : null,
          t.volatility != null ? `колебания ${esc(t.volatility_label.toLowerCase())}` : null,
        ].filter(Boolean).join(' · ')}</div>
      </div>
      <span class="tag tag-accent">риск: ${esc(t.risk.toLowerCase())}</span>
      <button class="btn btn-ghost" data-remove="${esc(t.symbol)}" type="button">Убрать из списка</button>
    </div>
    <p style="margin:0;font-size:15.5px;line-height:1.62;text-wrap:pretty">
      ${a ? `${esc(a.summary)} ${sourceNote(a)}` : (pending ? skeleton() : '<span class="text-muted">Разбор не собран.</span>')}
    </p>
    <div class="two-col">
      <div>
        <div class="kicker kicker-sage">Что за</div>
        <p style="margin-top:7px">${a ? esc(a.pro) : (pending ? skeleton() : '—')}</p>
      </div>
      <div>
        <div class="kicker kicker-accent">Что против</div>
        <p style="margin-top:7px">${a ? esc(a.contra) : (pending ? skeleton() : '—')}</p>
      </div>
    </div>`;
}

function renderFocus() {
  const t = current();
  if (!t) return;
  const a = t.analysis;
  const pending = state.pending.has(t.symbol);
  $('fSym').textContent = t.symbol.slice(0, 4);
  $('fName').textContent = t.name;
  $('fKicker').textContent = `Разбор на ${dateLabel().split(', ')[1]}`;
  $('fPrice').textContent = t.price_text;
  $('fChange').textContent = `${t.change_24h_text} за сутки`;
  $('fChange').style.color = color(t);

  const line = $('fSpark').querySelector('polyline');
  line.setAttribute('points', t.spark || '');
  line.setAttribute('stroke', color(t));
  $('fSpark').classList.toggle('hidden', !t.spark);
  $('fNoSpark').classList.toggle('hidden', !!t.spark);

  // Показываем только те метрики, по которым есть данные: тариф провайдера
  // может не отдавать ни объём, ни историю.
  const metrics = [];
  if (t.change_7d != null) {
    metrics.push(['За неделю', t.change_7d_text, t.change_7d >= 0 ? UP : DOWN]);
  }
  if (t.day_low && t.day_high) {
    metrics.push(['Диапазон за сутки', `${fmtPrice(t.day_low)} – ${fmtPrice(t.day_high)}`, '']);
  }
  if (t.volume) metrics.push(['Объём за сутки', t.volume_text, '']);
  if (t.volatility != null) metrics.push(['Колебания', t.volatility_label, '']);
  metrics.push(['Риск', t.risk, 'var(--color-accent-700)']);
  $('fMetrics').innerHTML = metrics.map(([label, value, col]) => `<div>
      <div class="label">${esc(label)}</div>
      <div class="value"${col ? ` style="color:${col}"` : ''}>${esc(value)}</div>
    </div>`).join('');
  $('fMetrics').style.gridTemplateColumns = `repeat(${Math.min(metrics.length, 4)}, minmax(0, 1fr))`;

  $('fSummary').innerHTML = a ? `${esc(a.summary)} ${sourceNote(a)}`
    : (pending ? skeleton() : '<span class="text-muted">Разбор не собран.</span>');
  $('fPro').innerHTML = a ? esc(a.pro) : (pending ? skeleton() : '—');
  $('fContra').innerHTML = a ? esc(a.contra) : (pending ? skeleton() : '—');

  $('miniList').innerHTML = tokens().filter((x) => x.symbol !== t.symbol).map((x) => `
    <button class="mini-card" data-symbol="${esc(x.symbol)}">
      <div class="head">
        <span class="sym">${esc(x.symbol.slice(0, 4))}</span>
        <span class="name">${esc(x.name)}</span>
        <span class="chg" style="color:${color(x)}">${esc(x.change_24h_text)}</span>
      </div>
      <div style="display:flex;align-items:center;gap:11px">
        ${sparkSvg(x, 'spark-sm')}
        <span style="font-size:12px;color:var(--color-neutral-600);white-space:nowrap">${esc(x.price_text)}</span>
      </div>
      <div class="note">${x.analysis ? esc(x.analysis.short) : esc(x.trend)}</div>
    </button>`).join('');
}

function renderNotes() {
  const items = (state.snapshot && state.snapshot.notes) || [];
  $('notesCount').textContent = items.length
    ? `${items.length} ${plural(items.length, 'запись', 'записи', 'записей')}` : 'пусто';
  $('notes').innerHTML = items.map((n) => `
    <div class="note">
      <div class="grow">
        <div>${esc(n.text)}</div>
        <div class="meta">${esc(n.source || 'вручную')} · ${new Date(n.created_at * 1000).toLocaleString('ru-RU')}</div>
      </div>
      <button class="btn btn-ghost" data-note="${n.id}" type="button" aria-label="Удалить">✕</button>
    </div>`).join('') || '<div class="text-muted" style="font-size:13px">Пока ничего не прислано.</div>';
}

// ── загрузка данных ─────────────────────────────────────────────────────────
async function loadSnapshot({ refresh = false } = {}) {
  const data = await api(`/api/snapshot?scope=web${refresh ? '&refresh=1' : ''}`);
  state.snapshot = data;
  if (!state.selected || !data.tokens.some((t) => t.symbol === state.selected)) {
    state.selected = data.tokens.length ? data.tokens[0].symbol : null;
  }
  renderAll();
  return data;
}

async function loadAnalyses({ force = false } = {}) {
  if (state.busy) { toast('Модель ещё считает предыдущий разбор.'); return; }
  state.busy = true;
  setBusy(true);
  try {
    // сводка первой — она в шапке
    if (force || !state.snapshot.analysis) {
      state.pending.add('portfolio');
      renderSummary();
      try {
        const res = await api('/api/analysis', { method: 'POST', body: { scope: 'web', kind: 'portfolio', force } });
        state.snapshot.analysis = res.analysis;
        state.snapshot.portfolio = res.portfolio || state.snapshot.portfolio;
      } catch (err) {
        toast(err.message);
      } finally {
        state.pending.delete('portfolio');
        renderSummary();
      }
    }

    // затем токены: выбранный вперёд, остальные по очереди
    const queue = [...tokens()].sort((a, b) => (a.symbol === state.selected ? -1 : 0)
      - (b.symbol === state.selected ? -1 : 0));
    for (const token of queue) {
      if (!force && token.analysis) continue;
      state.pending.add(token.symbol);
      renderCards(); renderDetail(); renderFocus();
      try {
        const res = await api('/api/analysis', {
          method: 'POST',
          body: { scope: 'web', kind: 'token', symbol: token.symbol, force },
        });
        token.analysis = res.analysis;
      } catch (err) {
        toast(`${token.symbol}: ${err.message}`);
      } finally {
        state.pending.delete(token.symbol);
        renderCards(); renderDetail(); renderFocus();
      }
    }
  } finally {
    state.busy = false;
    setBusy(false);
  }
}

function setBusy(busy) {
  $('analyzeBtn').disabled = busy;
  $('analyzeBtn').textContent = busy ? 'Модель думает…' : 'Обновить разбор';
}

async function loadHealth() {
  try {
    const h = await api('/api/health');
    const source = $('sourceBadge');
    source.querySelector('span').textContent = h.data_source === 'demo'
      ? 'демо-данные (нет ключа FreeCryptoAPI)' : 'FreeCryptoAPI';
    source.classList.toggle('is-off', h.data_source === 'demo');
    const llm = $('llmBadge');
    llm.querySelector('span').textContent = h.ollama.up
      ? `Ollama · ${h.ollama.model}${h.ollama.ready ? '' : ' (модель не скачана)'}`
      : 'Ollama недоступна — тексты по правилам';
    llm.classList.toggle('is-off', !h.ollama.up || !h.ollama.ready);
  } catch (err) {
    toast(err.message);
  }
}

// ── события ─────────────────────────────────────────────────────────────────
function select(symbol) {
  if (!symbol || symbol === state.selected) return;
  state.selected = symbol;
  renderSideList(); renderCards(); renderDetail(); renderFocus();
}

document.addEventListener('click', async (event) => {
  const pick = event.target.closest('[data-symbol]');
  if (pick && !event.target.closest('[data-remove]')) { select(pick.dataset.symbol); return; }

  const remove = event.target.closest('[data-remove]');
  if (remove) {
    const symbol = remove.dataset.remove;
    try {
      await api(`/api/watchlist/${encodeURIComponent(symbol)}?scope=web`, { method: 'DELETE' });
      state.selected = null;
      await loadSnapshot();
      toast(`${symbol} убран из списка.`);
    } catch (err) { toast(err.message); }
    return;
  }

  const note = event.target.closest('[data-note]');
  if (note) {
    try {
      await api(`/api/notes/${note.dataset.note}?scope=web`, { method: 'DELETE' });
      const res = await api('/api/notes?scope=web');
      state.snapshot.notes = res.items.slice(0, 5);
      renderNotes();
    } catch (err) { toast(err.message); }
  }
});

document.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  const card = event.target.closest('.token-card');
  if (card) { event.preventDefault(); select(card.dataset.symbol); }
});

$('filter').addEventListener('input', (event) => {
  state.filter = event.target.value.trim().toUpperCase();
  renderSideList(); renderCards();
});

document.querySelectorAll('input[name="view"]').forEach((radio) => {
  radio.addEventListener('change', () => {
    state.view = radio.value;
    $('viewGrid').classList.toggle('hidden', state.view !== 'grid');
    $('viewFocus').classList.toggle('hidden', state.view !== 'focus');
    if (state.view === 'focus') renderFocus(); else { renderCards(); renderDetail(); }
  });
});

$('refreshBtn').addEventListener('click', async () => {
  $('refreshBtn').disabled = true;
  try {
    await loadSnapshot({ refresh: true });
    toast('Котировки обновлены.');
  } catch (err) { toast(err.message); } finally { $('refreshBtn').disabled = false; }
});

$('analyzeBtn').addEventListener('click', () => loadAnalyses({ force: true }));

$('addForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = $('addSymbol');
  const symbol = input.value.trim().toUpperCase();
  if (!symbol) return;
  $('addBtn').disabled = true;
  try {
    const res = await api('/api/watchlist', { method: 'POST', body: { scope: 'web', symbol } });
    input.value = '';
    await loadSnapshot();
    select(symbol);
    toast(res.added ? `${symbol} добавлен.` : `${symbol} уже в списке.`);
    loadAnalyses();
  } catch (err) { toast(err.message); } finally { $('addBtn').disabled = false; }
});

$('noteForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const text = $('noteText').value.trim();
  if (text.length < 10) { toast('Слишком коротко.'); return; }
  try {
    const res = await api('/api/notes', { method: 'POST', body: { scope: 'web', text } });
    $('noteText').value = '';
    state.snapshot.notes = res.items.slice(0, 5);
    renderNotes();
    toast('Добавлено в контекст. Разбор учтёт это при следующем обновлении.');
  } catch (err) { toast(err.message); }
});

// ── старт ───────────────────────────────────────────────────────────────────
(async function start() {
  loadHealth();
  try {
    await loadSnapshot();
    await loadAnalyses();
  } catch (err) {
    toast(err.message);
  }
  setInterval(() => loadSnapshot().catch(() => {}), 5 * 60 * 1000);
}());
