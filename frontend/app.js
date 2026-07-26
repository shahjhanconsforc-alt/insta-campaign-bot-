/* insta-bot console — vanilla JS, no build step. */

const $ = sel => document.querySelector(sel);
const $$ = sel => [...document.querySelectorAll(sel)];
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function api(path, { method = 'GET', body } = {}) {
  const response = await fetch(`/api${path}`, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || data.error || `HTTP ${response.status}`);
  return data;
}

function toast(message, bad = false) {
  const el = $('#toast');
  el.textContent = message;
  el.className = `toast show${bad ? ' bad' : ''}`;
  clearTimeout(toast._t);
  // errors get longer on screen — 2.8s isn't enough to read why something failed
  toast._t = setTimeout(() => (el.className = 'toast'), bad ? 7000 : 2800);
}

function show(el, data, isError = false) {
  el.classList.toggle('err', isError);
  el.textContent = typeof data === 'string' ? data : JSON.stringify(data, null, 2);
  if (isError) openTerminal(el);   // an error nobody can see is no error message
  else markUnread(el);
}

/* ── Terminal drawers ────────────────────────────────────────────
   Every panel holding a .out box gets a `>_` button on the right of its
   head. Clicking opens the output as a drawer down the LEFT side of that
   panel, with its own close button. Built here rather than in the markup so
   the six panels can't drift apart — and any future .out gets it free. */
function buildTerminal(panel) {
  const out = panel.querySelector('.out');
  const head = panel.querySelector('.panel-head');
  const body = panel.querySelector('.panel-body');
  if (!out || !head || !body) return;

  // Head actions (Close browser) belong beside the >_ button, not swept into
  // the title box with the <h2>/<p> — lift them out before the sweep.
  const actions = [...head.querySelectorAll('[data-head-action]')];
  actions.forEach(el => el.remove());

  // push the existing <h2>/<p> into their own box so the buttons can sit right
  const title = document.createElement('div');
  while (head.firstChild) title.appendChild(head.firstChild);

  // One cluster on the right: the head is space-between, so a third child
  // would strand the actions in the middle of the bar.
  const tools = document.createElement('div');
  tools.className = 'head-tools';
  tools.append(...actions);
  head.append(title, tools);

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'term-btn';
  btn.textContent = '>_';
  btn.title = 'Show output';
  btn.setAttribute('aria-label', 'Show output');
  btn.setAttribute('aria-expanded', 'false');
  tools.append(btn);                     // >_ stays rightmost

  const term = document.createElement('aside');
  term.className = 'term';
  term.hidden = true;

  const bar = document.createElement('div');
  bar.className = 'term-head';
  bar.innerHTML = '<span class="term-title">&gt;_ output</span>';

  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'term-x';
  close.textContent = '✕';
  close.title = 'Close output';
  close.setAttribute('aria-label', 'Close output');
  bar.append(close);

  term.append(bar, out);                 // moves .out out of the body

  const main = document.createElement('div');
  main.className = 'panel-main';
  panel.insertBefore(main, body);
  main.append(term, body);               // drawer first = left side

  const toggle = open => {
    term.hidden = !open;
    btn.classList.toggle('on', open);
    btn.setAttribute('aria-expanded', String(open));
    btn.title = open ? 'Hide output' : 'Show output';
    if (open) btn.classList.remove('dot');
  };
  btn.addEventListener('click', () => toggle(term.hidden));
  close.addEventListener('click', () => toggle(false));
}

/** Dot the button when output lands while the drawer is shut — otherwise the
 *  result of a click would vanish with no trace. */
function markUnread(out) {
  const term = out.closest('.term');
  if (!term || !term.hidden) return;
  term.closest('.panel')?.querySelector('.term-btn')?.classList.add('dot');
}

/** Force the drawer open. Errors must never land in a panel nobody can see —
 *  a failed step would otherwise show only a toast that fades in 3 seconds. */
function openTerminal(out) {
  const term = out.closest('.term');
  if (!term) return;
  term.hidden = false;
  const btn = term.closest('.panel')?.querySelector('.term-btn');
  btn?.classList.add('on');
  btn?.classList.remove('dot');
  btn?.setAttribute('aria-expanded', 'true');
}

/* Panel-level notes (the explanatory <p class="hint"> sitting directly in a
   body) belong at the foot of the panel, not floating between the controls.
   Inline hints — <span class="hint"> inside a .row — stay where they are,
   since they caption the control next to them. */
function buildNote(panel) {
  const body = panel.querySelector('.panel-body');
  if (!body) return;
  const notes = [...body.children].filter(el => el.matches('p.hint'));
  if (!notes.length) return;

  const foot = document.createElement('div');
  foot.className = 'panel-note';
  foot.append(...notes);
  panel.append(foot);            // very bottom, under the add-row where there is one
}

$$('.panel').forEach(panel => {
  buildTerminal(panel);
  buildNote(panel);
});

/** Wrap a click handler: disables the button, shows errors consistently. */
function bind(sel, outSel, handler) {
  const button = $(sel);
  button.addEventListener('click', async () => {
    const out = outSel ? $(outSel) : null;
    const label = button.textContent;
    button.disabled = true;
    button.textContent = 'Working…';
    if (out) show(out, 'Working…');
    try {
      await handler(out);
    } catch (error) {
      if (out) show(out, `❌ ${error.message}`, true);
      toast(error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = label;
      refreshHealth();
    }
  });
}

const esc = s => String(s ?? '').replace(/[&<>"]/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);

/* Show a UTC timestamp in Pakistan time (PKT, UTC+5, no DST). The 'sv-SE'
   locale formats as ISO-ish "YYYY-MM-DD HH:MM", and timeZone does the +5 shift
   correctly regardless of the viewer's own clock. */
const PKT_FMT = new Intl.DateTimeFormat('sv-SE', {
  timeZone: 'Asia/Karachi',
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hour12: false
});
function pktTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return isNaN(d) ? esc(iso) : `${PKT_FMT.format(d)} PKT`;
}
const stamp = pktTime;   // account session dates use the same PKT format

/* ── Panel switching ─────────────────────────────────────────── */
function openPanel(name) {
  hidePanelInfo(true);                      // never leave a pinned ⓘ over the new panel
  $$('.panel').forEach(p => (p.hidden = p.id !== `panel-${name}`));
  $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.panel === name));
  $('#empty').hidden = true;
  location.hash = name;
  ({ tools: loadTools, data: loadRecords,
     automation: loadBrowserStatus, accounts: loadAccounts,
     config: loadConfig, activity: loadActivity, campaign: loadCampaignList,
     scrapping: loadScrapAccounts, actions: loadAccountPickers })[name]?.();
}

$$('.tab').forEach(tab => tab.addEventListener('click', () => openPanel(tab.dataset.panel)));

/* ── Panel ⓘ help ────────────────────────────────────────────────
   The blurb that used to sit under each panel title, moved into a small circled
   ⓘ in front of the name — the header stays one line and the explanation is a
   hover (or keyboard focus / tap) away. Keyed by data-panel-info, so any panel
   can opt in by adding the button and an entry here.

   Values are trusted literals from this file, so the light markup below is
   rendered rather than escaped — never put user or API data in here. */
const PANEL_HELP = {
  scrapping:
    'Collects usernames from one or more open windows at once, straight into the '
    + 'Data panel. Each account uses its own technique — <b>Scroll</b>, <b>Search</b>, '
    + 'or <b>Both</b> (default) — picked under its name. 🧲 Scrapping runs every '
    + 'ticked account by its chosen method at once.',
};

// A click PINS the popover open; hover alone doesn't. This is what makes touch
// work: a tap fires mouseover → focusin → click → **mouseout**, in that order, so
// an unpinned popover would be hidden by the mouseout that trails its own tap.
// Pinned, it survives until a click elsewhere (or a panel switch) clears it.
let panelInfoPinned = false;

function showPanelInfo(key, anchor) {
  const pop = $('#panel-info-pop');
  if (!pop || !PANEL_HELP[key]) return;
  pop.innerHTML = PANEL_HELP[key];
  pop.hidden = false;                                    // unhide first so we can measure it
  const r = anchor.getBoundingClientRect();
  // Below the icon, nudged left so a wide popover never runs off the right edge.
  pop.style.top = `${r.bottom + 8}px`;
  pop.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - pop.offsetWidth - 12))}px`;
}
function hidePanelInfo(force = false) {
  if (panelInfoPinned && !force) return;                 // hover-out mustn't close a pinned one
  panelInfoPinned = false;
  const p = $('#panel-info-pop');
  if (p) p.hidden = true;
}

/* Delegated on the document: panels are re-rendered often, and this survives it.
   Hover for the mouse, focus for the keyboard, click/tap to pin. */
document.addEventListener('mouseover', e => {
  const info = e.target.closest?.('.panel-info');
  if (info) showPanelInfo(info.dataset.panelInfo, info);
});
document.addEventListener('mouseout', e => {
  if (e.target.closest?.('.panel-info')) hidePanelInfo();
});
document.addEventListener('focusin', e => {
  const info = e.target.closest?.('.panel-info');
  if (info) showPanelInfo(info.dataset.panelInfo, info);
});
document.addEventListener('focusout', e => {
  if (e.target.closest?.('.panel-info')) hidePanelInfo();
});
document.addEventListener('click', e => {
  const info = e.target.closest?.('.panel-info');
  if (!info) { hidePanelInfo(true); return; }            // a click anywhere else dismisses it
  e.preventDefault();
  panelInfoPinned = true;
  showPanelInfo(info.dataset.panelInfo, info);
});
document.addEventListener('keydown', e => {             // Esc closes a pinned popover
  if (e.key === 'Escape') hidePanelInfo(true);
});

/* ── Enable/disable nav panels (Config → 🧭 Panels) ──────────────
   Which panel buttons are usable is a per-browser UI preference kept in localStorage
   (like activeAccount). A disabled button stays VISIBLE but is greyed out and can't
   be clicked. Config is deliberately absent from the list — it can never be disabled,
   so the switches are always reachable. */
const NAV_PANELS = [
  { name: 'campaign',   label: '📣 Campaign' },
  { name: 'automation', label: '🌐 Account Connections' },
  { name: 'accounts',   label: '👥 Account Registry' },
  { name: 'scrapping',  label: '🧲 Scrapping' },
  { name: 'data',       label: '🗄️ Data' },
  { name: 'agent',      label: '⚡ Agent' },
  { name: 'actions',    label: '🎬 Actions' },
  { name: 'tools',      label: '🧰 Tools' },
  { name: 'activity',   label: '📡 Activity' },
];
const DISABLED_PANELS_KEY = 'disabledPanels';
const loadDisabledPanels = () => {
  try { return new Set(JSON.parse(localStorage.getItem(DISABLED_PANELS_KEY) || '[]')); }
  catch { return new Set(); }
};
const saveDisabledPanels = set => localStorage.setItem(DISABLED_PANELS_KEY, JSON.stringify([...set]));

/* Grey out and disable the chosen nav buttons (they stay visible), and if the panel
   currently on screen was just disabled, fall back to Config so nobody is stranded. */
function applyPanelState() {
  const off = loadDisabledPanels();
  $$('.tab').forEach(t => {
    if (t.dataset.panel === 'config') return;          // config can never be disabled
    const disabled = off.has(t.dataset.panel);
    t.disabled = disabled;                              // native: an unclickable <button>
    t.classList.toggle('tab-off', disabled);
  });
  const openSection = $$('.panel').find(p => !p.hidden);
  const openName = openSection ? openSection.id.replace('panel-', '') : null;
  if (openName && openName !== 'config' && off.has(openName)) openPanel('config');
}

/* The checkbox grid inside Config: ticked = the panel's button is enabled. */
function renderPanelToggles() {
  const box = $('#cfg-panels');
  if (!box) return;
  const off = loadDisabledPanels();
  box.innerHTML = NAV_PANELS.map(p => `
    <label class="cfg-panel-item ${off.has(p.name) ? '' : 'on'}">
      <input type="checkbox" data-panel-toggle="${p.name}" ${off.has(p.name) ? '' : 'checked'} />
      <span class="cfg-panel-name">${p.label}</span>
    </label>`).join('');
  box.querySelectorAll('[data-panel-toggle]').forEach(cb =>
    cb.addEventListener('change', () => {
      const set = loadDisabledPanels();
      if (cb.checked) set.delete(cb.dataset.panelToggle);
      else set.add(cb.dataset.panelToggle);
      saveDisabledPanels(set);
      cb.closest('.cfg-panel-item')?.classList.toggle('on', cb.checked);
      applyPanelState();
    }));
}

/* ── Actions: click one Instagram menu item ──────────────────────
   Each button maps to a sidebar menu item; clicking it clicks that item in
   Instagram on the chosen window. */
async function clickMenuItem(item, btn) {
  const acct = pickedAccount('#actions-menu-acct');
  const note = $('#actions-menu-note');
  if (!acct) { toast('Pick an open window to run on.', true); return; }

  const label = btn.textContent;
  btn.disabled = true; btn.textContent = '…';
  if (note) { note.className = 'banner'; note.textContent = `Clicking “${item}” in the Instagram menu…`; }
  try {
    await api('/automation/click-menu', { method: 'POST', body: { account_id: acct, item } });
    if (note) { note.className = 'banner good'; note.textContent = `Clicked “${item}” in the menu.`; }
    toast(`Clicked ${item}`);
  } catch (error) {
    if (note) { note.className = 'banner bad'; note.textContent = error.message; }
    toast(error.message, true);
  }
  btn.disabled = false; btn.textContent = label;
}

/* The Search button does more than click the menu item: it types two random
   letters into Instagram's Search and opens the first result's profile. */
async function searchAndOpenFirst(btn) {
  const acct = pickedAccount('#actions-menu-acct');
  const note = $('#actions-menu-note');
  if (!acct) { toast('Pick an open window to run on.', true); return; }

  const label = btn.textContent;
  btn.disabled = true; btn.textContent = '…';
  if (note) { note.className = 'banner'; note.textContent = 'Searching two random letters, opening the first result, and following…'; }
  try {
    const r = await api('/automation/open-search', { method: 'POST', body: { account_id: acct, click_first: true, follow: true } });
    const opened = r.opened_first;
    const seed = (r.seeds || []).join('');
    const FOLLOW_MSG = { 'followed': 'followed', 'already-following': 'already following', 'no-button': 'no Follow button' };
    if (note) {
      note.className = opened ? 'banner good' : 'banner';
      note.textContent = opened
        ? `Searched “${seed}” → opened @${opened} → ${FOLLOW_MSG[r.followed] || 'opened'}`
        : `Searched “${seed}” — no result to open.`;
    }
    toast(opened ? (r.followed === 'followed' ? `Followed @${opened}` : `Opened @${opened}`) : 'No result to open');
  } catch (error) {
    if (note) { note.className = 'banner bad'; note.textContent = error.message; }
    toast(error.message, true);
  }
  btn.disabled = false; btn.textContent = label;
}

/* Each Reel button opens Reels and clicks ONE action (Like, Comment, Repost,
   Share, Save, More). Comment types a random comment and posts it with Enter. */
async function clickReelAction(action, btn) {
  const acct = pickedAccount('#actions-menu-acct');
  const note = $('#actions-menu-note');
  if (!acct) { toast('Pick an open window to run on.', true); return; }

  const label = btn.textContent;
  btn.disabled = true; btn.textContent = '…';
  if (note) { note.className = 'banner'; note.textContent = `Reels → ${action}…`; }
  try {
    const r = await api('/automation/reel-action', { method: 'POST', body: { account_id: acct, item: action } });
    if (note) {
      note.className = 'banner good';
      note.textContent = r.commented
        ? `Reel: commented “${r.comment}”`
        : `Reel: clicked ${r.action}`;
    }
    toast(r.commented ? `Commented “${r.comment}”` : `Reel: ${r.action}`);
  } catch (error) {
    if (note) { note.className = 'banner bad'; note.textContent = error.message; }
    toast(error.message, true);
  }
  btn.disabled = false; btn.textContent = label;
}

/* The Notifications button opens Notifications, then clicks each filter tab
   (All, People you follow, Comments, Follows, Tags & mentions, Verified). */
async function clickNotificationFilters(btn) {
  const acct = pickedAccount('#actions-menu-acct');
  const note = $('#actions-menu-note');
  if (!acct) { toast('Pick an open window to run on.', true); return; }

  const label = btn.textContent;
  btn.disabled = true; btn.textContent = '…';
  if (note) { note.className = 'banner'; note.textContent = 'Opening Notifications and clicking each filter tab…'; }
  try {
    const r = await api('/automation/notification-filters', { method: 'POST', body: { account_id: acct } });
    const done = r.clicked || [];
    if (note) {
      note.className = done.length ? 'banner good' : 'banner';
      note.textContent = (done.length
        ? `Notifications: clicked ${done.join(', ')}`
        : 'No notification tabs were clicked.')
        + (r.closed ? ' · panel closed' : '')
        + (r.failed?.length ? ` · skipped ${r.failed.join(', ')}` : '');
    }
    toast(done.length ? `Clicked ${done.length} filter tab(s)` : 'No tabs clicked');
  } catch (error) {
    if (note) { note.className = 'banner bad'; note.textContent = error.message; }
    toast(error.message, true);
  }
  btn.disabled = false; btn.textContent = label;
}

/* The Home button opens Home, scrolls a little, clicks a username, then clicks
   each tab (Posts, Reels, Reposts, Tagged) on the opened profile. */
async function clickHomeActions(btn) {
  const acct = pickedAccount('#actions-menu-acct');
  const note = $('#actions-menu-note');
  if (!acct) { toast('Pick an open window to run on.', true); return; }

  const label = btn.textContent;
  btn.disabled = true; btn.textContent = '…';
  if (note) { note.className = 'banner'; note.textContent = 'Home: scrolling, opening a username, clicking its tabs…'; }
  try {
    const r = await api('/automation/home-actions', { method: 'POST', body: { account_id: acct } });
    const tabs = r.tabs || [];
    if (note) {
      note.className = 'banner good';
      note.textContent = `Home: opened @${r.username} → ${tabs.length ? 'tabs: ' + tabs.join(', ') : 'no tabs found'}`;
    }
    toast(`Opened @${r.username}`);
  } catch (error) {
    if (note) { note.className = 'banner bad'; note.textContent = error.message; }
    toast(error.message, true);
  }
  btn.disabled = false; btn.textContent = label;
}

/* A Settings sub-option (Settings, Your activity, Saved, Switch appearance,
   Report a problem): open the Settings menu, click it, then close. */
async function clickSettingsOption(option, btn) {
  const acct = pickedAccount('#actions-menu-acct');
  const note = $('#actions-menu-note');
  if (!acct) { toast('Pick an open window to run on.', true); return; }

  const label = btn.textContent;
  btn.disabled = true; btn.textContent = '…';
  if (note) { note.className = 'banner'; note.textContent = `Settings → “${option}” → close…`; }
  try {
    await api('/automation/settings-option', { method: 'POST', body: { account_id: acct, item: option } });
    if (note) { note.className = 'banner good'; note.textContent = `Settings → “${option}” → closed.`; }
    toast(`Settings: ${option}`);
  } catch (error) {
    if (note) { note.className = 'banner bad'; note.textContent = error.message; }
    toast(error.message, true);
  }
  btn.disabled = false; btn.textContent = label;
}

$('#actions-menu-btns')?.addEventListener('click', e => {
  const reelBtn = e.target.closest('[data-reel]');
  if (reelBtn) { clickReelAction(reelBtn.dataset.reel, reelBtn); return; }
  const settingBtn = e.target.closest('[data-setting]');
  if (settingBtn) { clickSettingsOption(settingBtn.dataset.setting, settingBtn); return; }
  const btn = e.target.closest('[data-menu]');
  if (!btn) return;
  if (btn.dataset.menu === 'Home') clickHomeActions(btn);
  else if (btn.dataset.menu === 'Search') searchAndOpenFirst(btn);
  else if (btn.dataset.menu === 'Notifications') clickNotificationFilters(btn);
  else clickMenuItem(btn.dataset.menu, btn);
});

/* ── Health pills ────────────────────────────────────────────── */
async function refreshHealth() {
  try {
    const s = await api('/status');
    $('#pill-server').className = 'pill up';
    $('#pill-browser').className = `pill ${s.browser?.open ? 'up' : 'down'}`;
  } catch {
    $('#pill-server').className = 'pill down';
  }
  // MinIO on its own request: it may be down while the rest is fine, and its
  // check shouldn't hold up (or be held up by) the others.
  try {
    const m = await api('/minio/status');
    const pill = $('#pill-minio');
    pill.className = `pill pill-btn ${m.up ? 'up' : 'down'}`;
    pill.title = m.up
      ? `MinIO running at ${m.endpoint} · ${m.count ?? '?'} usernames · click for logs`
      : `MinIO stopped (${m.endpoint}) · click for logs`;
  } catch {
    $('#pill-minio').className = 'pill pill-btn down';
  }
}

/* ── MinIO logs terminal ──────────────────────────────────────────
   Opens from the MinIO pill. Shows the live running/stopped state and streams
   just the MinIO log lines, polling for new ones while it's open. */
const minioTerm = (() => {
  const esc = s => String(s ?? '').replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
  let lastId = 0;      // highest log id shown, so each poll pulls only new lines
  let timer = null;

  function paintState(up) {
    const el = $('#minio-term-state');
    if (!el) return;
    el.className = `mio-state ${up ? 'up' : 'down'}`;
    el.textContent = up ? 'running' : 'stopped';
  }

  function append(lines) {
    const out = $('#minio-term-out');
    if (!out || !lines.length) return;
    // First real batch clears the placeholder.
    if (lastId === 0) out.innerHTML = '';
    const html = lines.map(e => {
      const t = new Date(e.at).toLocaleTimeString();
      return `<span class="mio-line ${esc(e.level)}"><span class="mio-t">${esc(t)}</span>${esc(e.message)}</span>`;
    }).join('\n');
    out.insertAdjacentHTML('beforeend', (out.textContent ? '\n' : '') + html);
    lastId = lines[lines.length - 1].id;
    out.scrollTop = out.scrollHeight;      // stick to the newest line
  }

  async function poll() {
    try {
      const r = await api(`/minio/logs?after=${lastId}`);
      $('#minio-term-endpoint').textContent = `${r.endpoint} · ${r.bucket}`;
      paintState(r.up);
      append(r.logs || []);
    } catch {
      paintState(false);
    }
  }

  function open() {
    const dlg = $('#minio-term');
    if (!dlg) return;
    lastId = 0;
    $('#minio-term-out').textContent = 'Waiting for MinIO logs…';
    dlg.showModal();
    poll();                                // fill immediately
    timer = setInterval(poll, 1200);       // then keep it live while open
  }

  function close() {
    const dlg = $('#minio-term');
    if (timer) { clearInterval(timer); timer = null; }
    dlg?.close();
  }

  return { open, close };
})();

$('#pill-minio')?.addEventListener('click', () => minioTerm.open());
$('#btn-minio-term-close')?.addEventListener('click', () => minioTerm.close());
// Backdrop click closes it, and stops the poll timer with it.
$('#minio-term')?.addEventListener('click', e => {
  if (e.target === $('#minio-term')) minioTerm.close();
});

/* ── Run controls (stop / pause / start fresh) ───────────────── */
/* These act on JOBS, never on Chrome: a stopped or reset console leaves every
   window open and logged in, because the sessions are the expensive part. */
/* Header run-status: blinks "Stopped Working" / "Paused" with the time it began
   and a live elapsed counter. Driven by the control state polled below. */
let runStoppedAt = null, runPausedAt = null;
function fmtClock(d) {
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}
function fmtElapsed(secs) {
  const s = Math.max(0, secs);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60), r = s % 60;
  if (m < 60) return `${m}m ${String(r).padStart(2, '0')}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${String(m % 60).padStart(2, '0')}m`;
}
/* HH:MM:SS for the next-batch countdown (matches "00:01:32"). */
function fmtHMS(secs) {
  const s = Math.max(0, Math.round(secs));
  const p = n => String(n).padStart(2, '0');
  return `${p(Math.floor(s / 3600))}:${p(Math.floor((s % 3600) / 60))}:${p(s % 60)}`;
}
function renderRunStatus() {
  const el = $('#run-status'), txt = $('#run-status-text');
  if (!el || !txt) return;
  const since = runStoppedAt || runPausedAt;
  if (!since) { el.hidden = true; return; }
  const stopped = !!runStoppedAt;
  el.hidden = false;
  el.classList.toggle('paused', !stopped);
  const elapsed = fmtElapsed(Math.floor((Date.now() - since.getTime()) / 1000));
  txt.textContent = `${stopped ? '⏹ Stopped Working' : '⏸ Paused'} · ${fmtClock(since)} · ${elapsed}`;
}
function updateRunStatus(s) {
  if (s && s.stopping)      { if (!runStoppedAt) runStoppedAt = new Date(); runPausedAt = null; }
  else if (s && s.paused)   { if (!runPausedAt) runPausedAt = new Date();  runStoppedAt = null; }
  else                      { runStoppedAt = null; runPausedAt = null; }
  renderRunStatus();
}
setInterval(renderRunStatus, 1000);   // tick the elapsed counter while it's shown

async function refreshControls() {
  try {
    const s = await api('/automation/control');
    updateRunStatus(s);
    const busy = Object.keys(s.running || {}).length > 0;
    const pause = $('#btn-pause');
    const stop = $('#btn-stop');
    if (pause) {
      pause.setAttribute('aria-pressed', String(!!s.paused));
      pause.textContent = s.paused ? '▶' : '⏸';
      pause.title = s.paused
        ? 'Resume the paused jobs.'
        : 'Pause every running job at its next step.';
      pause.disabled = !busy && !s.paused;
    }
    if (stop) {
      stop.disabled = !busy;
      stop.classList.toggle('live', busy);
      stop.title = busy
        ? `Stop ${Object.values(s.running).join(', ')}. Asks first; Chrome stays open and your panel data is kept.`
        : 'Nothing is running.';
    }
    return s;
  } catch {
    return null;                     // the server pill already reports an outage
  }
}

/* Stop everything: every scrape, every campaign, and the Chrome windows with
   them. Destructive enough to confirm first — but only to the RUN, never to the
   panels: the username queue, SMS drafts, staged media, scrape results and the
   Campaign view all stay exactly as they are (see /control/stop-all, which
   deliberately skips the progress-clearing that /control/reset does). */
$('#btn-stop')?.addEventListener('click', async () => {
  const button = $('#btn-stop');
  let jobs = [];
  try {
    const now = await api('/automation/control');
    jobs = [...new Set(Object.values(now.running || {}))];
  } catch { /* offline — still let them try to stop */ }

  const what = jobs.length ? jobs.join(', ') : 'anything still running';
  if (!confirm(
    `Stop ${what}?\n\n` +
    `• every running scrape and campaign is stopped\n` +
    `• Chrome stays open and logged in — its windows are just refreshed\n` +
    `• nothing is cleared — your names, drafts, media and results stay\n\n` +
    `Jobs stop at their next step, so this can take a few seconds.`
  )) return;

  const label = button.textContent;
  button.disabled = true;
  button.textContent = '…';
  try {
    const s = await api('/automation/control/stop-all', { method: 'POST' });
    updateRunStatus(s);              // blink "Stopped Working" instantly
    const left = Object.values(s.running || {});
    const fresh = (s.windows_refreshed || []).length;
    toast(left.length
      ? `${[...new Set(left)].join(', ')} still finishing — press ⏹ again in a moment`
      : `Stopped ${s.stopped?.join(', ') || 'everything'} — ${fresh} window(s) refreshed, Chrome still open`,
      left.length > 0);
  } catch (error) { toast(error.message, true); }
  button.textContent = label;
  refreshControls();                 // re-enables from live state, not blindly
});

$('#btn-pause')?.addEventListener('click', async () => {
  const paused = $('#btn-pause').getAttribute('aria-pressed') === 'true';
  try {
    const s = await api(`/automation/control/${paused ? 'resume' : 'pause'}`, { method: 'POST' });
    updateRunStatus(s);              // reflect Paused / cleared instantly
    toast(s.paused ? 'Paused — jobs park at their next step' : 'Resumed');
  } catch (error) { toast(error.message, true); }
  refreshControls();
});

$('#btn-restart')?.addEventListener('click', async () => {
  try {
    await api('/automation/control/reset', { method: 'POST' });
    toast('Fresh start — reloading');
    setTimeout(() => location.reload(), 400);   // let the toast land
  } catch (error) {
    // 409 = something is still running. Reloading would hide it, not fix it.
    toast(error.message, true);
    refreshControls();
  }
});

/* ── Agent ───────────────────────────────────────────────────── */
bind('#btn-run', '#out-agent', async out => {
  const goal = $('#goal').value.trim();
  if (!goal) throw new Error('Type a goal first.');
  const data = await api('/agent/run', { method: 'POST', body: { goal } });
  show(out, data);
  toast(data.run.ok ? '✅ Run finished' : '⚠️ Run finished with errors', !data.run.ok);
});

bind('#btn-plan', '#out-agent', async out => {
  const goal = $('#goal').value.trim();
  if (!goal) throw new Error('Type a goal first.');
  show(out, await api('/agent/plan', { method: 'POST', body: { goal } }));
});

$$('#goal-samples .chip').forEach(chip =>
  chip.addEventListener('click', () => { $('#goal').value = chip.textContent; $('#goal').focus(); })
);

/* ── Account pickers ─────────────────────────────────────────── */
/* Each panel picks the account it drives. Two accounts can run at once — the
   backend keys every window by that account's Chrome profile — so the id has to
   travel with every request; without it the server falls back to "whichever
   window was touched last", which is exactly what goes wrong under concurrency. */
const PICKERS = {
  // Automation drives an on-screen Chrome window, so only accounts whose window
  // is open right now. "Ticked in Accounts" stays as the multi-account login
  // flow — that one OPENS windows, so it isn't gated on one already being open.
  '#auto-account': { queueOption: true, openOnly: true },
  // Actions panel's menu-walk runs on one open window.
  '#actions-menu-acct': { openOnly: true }
  // Scrapping and Campaign have their own chip pickers — they list open windows
  // only, so a job always has a real window to drive.
};

const isOpen = a => !!a.window_open;

function accountName(a) {
  return a.account_name || a.label || a.login || `Account ${a.id}`;
}

/* null = no explicit account (the ticked queue, or the default profile). */
function pickedAccount(sel) {
  const value = $(sel)?.value || '';
  return value ? Number(value) : null;
}

function fillAccountPickers(accounts) {
  for (const [sel, opts] of Object.entries(PICKERS)) {
    const el = $(sel);
    if (!el) continue;
    const keep = el.value;
    const shown = opts.openOnly ? accounts.filter(isOpen) : accounts;
    const options = shown.map(a =>
      `<option value="${a.id}">${esc(accountName(a))}${a.window_open ? ' — window open' : ''}</option>`);
    if (opts.queueOption) {
      const n = queued().length;
      options.unshift(`<option value="">Ticked in Accounts${n ? ` (${n})` : ''}</option>`);
    }
    const empty = opts.openOnly ? 'No open windows — open one in Accounts' : 'No accounts saved';
    el.innerHTML = options.length ? options.join('') : `<option value="">${empty}</option>`;
    el.disabled = !options.length;
    // keep the user's choice across a refresh
    if ([...el.options].some(o => o.value === keep)) el.value = keep;
  }
}

async function loadAccountPickers() {
  try {
    const { accounts } = await api('/accounts');
    accountCache = accounts;
    fillAccountPickers(accounts);
  } catch {
    /* the panel's own banner reports failures; a stale picker is not fatal */
  }
}

/* ── Campaign ────────────────────────────────────────────────── */
function campaignNamesFromRecords(records) {
  // Data's order is newest-first, which for a Visit insta batch restores the
  // order the names appeared in the feed. Keep it.
  const seen = new Set();
  const names = [];
  for (const record of records || []) {
    if (record.type !== 'instagram-name') continue;
    const name = String(record.title || '').trim().replace(/^@/, '');
    if (!name) continue;
    const key = name.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    names.push(name);
  }
  return names;
}

/* The names the next run will use. Filled from Data — this list IS the queue,
   so Start Campaign reads it rather than any editable box. */
let campaignNames = [];

/* An optional 1-based inclusive slice of campaignNames chosen in the Full
   preview modal, e.g. {from:1, to:500}. null = run every name. */
let campaignRange = null;

/* The names the run will actually use, after applying any selected range. */
function campaignPool() {
  return campaignRange
    ? campaignNames.slice(campaignRange.from - 1, campaignRange.to)
    : campaignNames;
}

/* Paint the Usernames list with the current pool (the range-selected slice, or
   every name). This is the queue the next Start Campaign sends to. */
function showCampaignQueue() {
  renderCampaignTargets(campaignPool().map(name => ({ query: name, pending: true })));
}

/* Which open-window accounts are ticked for the campaign. A Set so a refresh
   can preserve it — same idea as scrapPicked. The names loaded from Data are
   split across whatever is ticked here when Start Campaign runs. */
const campaignPicked = new Set();

/* The campaign's own open-window picker — mirrors loadScrapAccounts. Only
   accounts whose Chrome window is open show up, because search-campaign drives
   an on-screen window and a closed one has nothing to run against. */
function loadCampaignAccounts() {
  const box = $('#campaign-accounts');
  if (!box) return Promise.resolve();
  return api('/accounts').then(({ accounts }) => {
    accountCache = accounts;
    const open = accounts.filter(isOpen);
    [...campaignPicked].forEach(id => open.some(a => a.id === id) || campaignPicked.delete(id));

    box.innerHTML = open.length
      ? open.map((a, i) =>
          `<button type="button" class="acct-chip ${campaignPicked.has(a.id) ? 'on' : ''}"
                   data-acct="${a.id}" aria-pressed="${campaignPicked.has(a.id)}">
             <span class="acct-num">${i + 1}</span>${esc(accountName(a))}
           </button>`).join('')
      : '<span class="hint">No open windows. Open accounts first (Accounts → Check Login Status).</span>';

    box.querySelectorAll('.acct-chip').forEach(chip => {
      chip.addEventListener('click', () => {
        const id = Number(chip.dataset.acct);
        const on = !campaignPicked.has(id);
        if (on) campaignPicked.add(id); else campaignPicked.delete(id);
        chip.classList.toggle('on', on);
        chip.setAttribute('aria-pressed', String(on));
      });
    });
  }).catch(() => { /* the panel's own banner reports load failures */ });
}

$('#btn-campaign-all')?.addEventListener('click', () => {
  const chips = $$('#campaign-accounts .acct-chip');
  const turnOn = chips.some(chip => !chip.classList.contains('on'));  // any off -> all; else clear
  chips.forEach(chip => {
    const id = Number(chip.dataset.acct);
    if (turnOn) campaignPicked.add(id); else campaignPicked.delete(id);
    chip.classList.toggle('on', turnOn);
    chip.setAttribute('aria-pressed', String(turnOn));
  });
});

// Close every open Chrome window, then refresh the picker — closed windows
// drop out of the open-only list. Same endpoint the Automation panel uses.
bind('#btn-campaign-close', null, async () => {
  const data = await api('/automation/close', { method: 'POST' });
  toast(data.windows_closed?.length
    ? `${data.windows_closed.length} window(s) closed`
    : 'No windows were open');
  await loadCampaignAccounts();
});

/* ── Campaign compose: SMS drafts (tabs) + media picker ────────────
   Several message drafts live behind the sms1/sms2/… tabs. With more than one
   non-empty draft, they're handed out to the ticked accounts round-robin — so
   account 1 writes draft 1, account 2 draft 2, and so on (blank drafts fall
   back to the backend's "Hello"). The backend already takes a per-account
   `message`, so this needs no backend change.

   Media is staged and previewed on the right, but attaching it to a DM needs a
   browser upload step the backend doesn't have yet, so it's shown, not sent. */
const campaignSms = [{ text: '' }];   // at least one draft
let activeSms = 0;
const SMS_MAX = 8;

/* The tabs live in TWO surfaces sharing one `campaignSms` store: the Campaign
   panel (#sms-tabs / #campaign-message / #sms-menu) and the Setup wizard
   (#setup-sms-tabs / #setup-message / #setup-sms-menu). Whichever surface is
   showing is the one the tabs act on — the wizard when its dialog is open. */
function smsWizardOpen() { return !!$('#campaign-setup')?.open; }
function activeSmsBox()  { return smsWizardOpen() ? $('#setup-message') : $('#campaign-message'); }
function smsMenuFor()    { return smsWizardOpen() ? $('#setup-sms-menu') : $('#sms-menu'); }

/* A tab's display name: the user's custom name, else the positional sms1/sms2/… */
const smsLabel = i => (campaignSms[i]?.name || '').trim() || `sms${i + 1}`;

/* One tab bar's markup: sms1 ✎ · Promo ✎ · … · + · 💾 (add/save use data-attrs so
   both bars can coexist without duplicate ids). */
function smsBarHtml() {
  return campaignSms.map((draft, i) => {
    const on = i === activeSms;
    const label = smsLabel(i);
    return `<div class="sms-tab ${on ? 'on' : ''}" data-sms="${i}" role="tab"
                 tabindex="0" aria-selected="${on}">
              <span class="sms-label">${esc(label)}</span>
              <button type="button" class="sms-edit" data-sms-edit="${i}"
                      title="Edit, rename, update or delete" aria-label="Options for ${esc(label)}">✎</button>
            </div>`;
  }).join('') +
    (campaignSms.length < SMS_MAX
      ? '<button type="button" class="sms-add" data-sms-add title="Add another message">+</button>'
      : '') +
    `<button type="button" class="icon-btn sms-save" data-sms-save title="Save all messages">
       <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
         <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/>
         <polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/>
       </svg>
     </button>`;
}

function wireSmsBar(bar) {
  bar.querySelectorAll('.sms-tab').forEach(tab =>
    tab.addEventListener('click', event => {
      if (event.target.closest('.sms-edit')) return;   // the ✎ handles itself
      switchSms(Number(tab.dataset.sms));
      setSmsEditable(false);                            // navigating only — view, don't edit
    }));
  bar.querySelectorAll('.sms-edit').forEach(btn =>
    btn.addEventListener('click', event => {
      event.stopPropagation();               // don't also switch to the tab
      const i = Number(btn.dataset.smsEdit);
      const menu = smsMenuFor();
      if (menu && !menu.hidden && smsMenuTarget === i) closeSmsMenu();  // toggle off
      else openSmsMenu(i, btn);
    }));
  bar.querySelector('[data-sms-add]')?.addEventListener('click', addSms);
  bar.querySelector('[data-sms-save]')?.addEventListener('click', event => saveSmsDrafts(event.currentTarget));
}

/* Paint the bar in every surface that has one (panel + wizard). */
function renderSmsTabs() {
  closeSmsMenu();                            // a re-render invalidates any open menu
  [$('#sms-tabs'), $('#setup-sms-tabs')].filter(Boolean).forEach(bar => {
    bar.innerHTML = smsBarHtml();
    wireSmsBar(bar);
  });
}

/* Per-tab ✎ menu: Edit (jump in and type), Update (save to JSON), Delete. */
let smsMenuTarget = null;

function closeSmsMenu() {
  [$('#sms-menu'), $('#setup-sms-menu')].forEach(menu => { if (menu) menu.hidden = true; });
  smsMenuTarget = null;
}

function openSmsMenu(index, anchor) {
  const menu = smsMenuFor();
  if (!menu) return;
  smsMenuTarget = index;
  const del = menu.querySelector('[data-sms-act="delete"]');
  if (del) del.disabled = campaignSms.length <= 1;   // can't delete the last draft
  menu.hidden = false;
  const r = anchor.getBoundingClientRect();
  menu.style.top = `${r.bottom + 4}px`;
  menu.style.left = `${Math.min(r.left, window.innerWidth - menu.offsetWidth - 8)}px`;
}

// The message box is read-only until Edit is chosen, so a saved draft can't be
// changed by accident just by clicking around.
function setSmsEditable(on) {
  const box = activeSmsBox();
  if (!box) return;
  box.readOnly = !on;
  box.classList.toggle('editing', on);
}

// Wire the static menus (panel + wizard) once. Each action reads the tab the ✎
// was opened from.
$$('#sms-menu [data-sms-act], #setup-sms-menu [data-sms-act]').forEach(btn =>
  btn.addEventListener('click', () => {
    const i = smsMenuTarget;
    const act = btn.dataset.smsAct;
    closeSmsMenu();
    if (i == null) return;
    if (act === 'edit') {
      switchSms(i);
      setSmsEditable(true);                 // unlock so this draft can be typed
      $('#campaign-message')?.focus();
    } else if (act === 'rename') {
      const next = prompt(`Rename ${smsLabel(i)} — leave blank to reset to sms${i + 1}:`,
                          (campaignSms[i].name || '').trim());
      if (next === null) return;             // cancelled
      campaignSms[i].name = next.trim().slice(0, 40);   // "" resets to the default label
      renderSmsTabs();                       // the tab shows the new name
      saveSmsDrafts();                        // persist the name to MinIO
    } else if (act === 'update') {
      switchSms(i);
      saveSmsDrafts();                       // persist, then lock again
      setSmsEditable(false);
    } else if (act === 'delete') {
      if (!confirm(`Delete ${smsLabel(i)}? This can't be undone.`)) return;
      removeSms(i);
      saveSmsDrafts();                       // persist the removal
      setSmsEditable(false);
    }
  }));

// Dismiss the menu on an outside click or Escape.
document.addEventListener('click', event => {
  const open = [$('#sms-menu'), $('#setup-sms-menu')].some(m => m && !m.hidden);
  if (!open) return;
  if (event.target.closest('.sms-menu') || event.target.closest('.sms-edit')) return;
  closeSmsMenu();
});
document.addEventListener('keydown', event => { if (event.key === 'Escape') closeSmsMenu(); });

/* Persist the drafts to the JSON store, each tagged with its sms id, and load
   them back when the app starts so they survive a restart. */
async function saveSmsDrafts(btn) {
  syncActiveDraft();
  const drafts = campaignSms.map((s, i) => ({ id: `sms${i + 1}`, text: s.text, name: (s.name || '').trim() }));
  try {
    await api('/campaign/sms', { method: 'PUT', body: { drafts } });
    toast('Messages saved');
    if (btn) { btn.classList.add('ok'); setTimeout(() => btn.classList.remove('ok'), 900); }
  } catch (error) {
    toast(`Could not save messages: ${error.message}`, true);
  }
}

/* Pull the drafts back from the store (MinIO-backed). Drafts may come back as
   {id,text} objects OR as plain strings (older saves) — read the text out of
   either so the message body always shows what was saved. */
function draftText(d) {
  if (typeof d === 'string') return d;
  if (d && typeof d === 'object') return d.text ?? d.message ?? d.body ?? d.value ?? '';
  return d == null ? '' : String(d);
}
async function loadSmsDrafts() {
  try {
    const { drafts } = await api('/campaign/sms');
    if (Array.isArray(drafts) && drafts.length) {
      campaignSms.length = 0;
      drafts.forEach(d => campaignSms.push({
        text: String(draftText(d)),
        name: (d && typeof d === 'object' && d.name) ? String(d.name) : '',
      }));
      activeSms = 0;
      showActiveDraft();
      renderSmsTabs();
    }
  } catch { /* no saved drafts yet — keep the default single empty tab */ }
}

function syncActiveDraft() {
  const box = activeSmsBox();
  if (box) campaignSms[activeSms].text = box.value;
}

function showActiveDraft() {
  // Fill BOTH editors (panel + wizard) so whichever surface is showing always
  // has the active draft's text — the drafts load once at startup (wizard still
  // closed) but must appear when the wizard's Messages step opens later.
  const text = campaignSms[activeSms]?.text || '';
  [$('#campaign-message'), $('#setup-message')].forEach(box => { if (box) box.value = text; });
}

function switchSms(i) {
  if (i === activeSms) return;
  syncActiveDraft();
  activeSms = i;
  showActiveDraft();
  renderSmsTabs();
}

function addSms() {
  if (campaignSms.length >= SMS_MAX) return;
  syncActiveDraft();
  campaignSms.push({ text: '' });
  activeSms = campaignSms.length - 1;
  showActiveDraft();
  renderSmsTabs();
  setSmsEditable(true);                 // a fresh, empty draft is ready to type
  $('#campaign-message')?.focus();
}

function removeSms(i) {
  if (campaignSms.length <= 1) return;
  campaignSms.splice(i, 1);
  if (activeSms >= campaignSms.length) activeSms = campaignSms.length - 1;
  else if (i < activeSms) activeSms--;
  showActiveDraft();
  renderSmsTabs();
}

// Keep the active draft in sync as the user types.
$('#campaign-message')?.addEventListener('input', () => syncActiveDraft());

renderSmsTabs();   // paint the initial single tab

/* Media: the images in the server-side store, previewed as thumbnails on the
   right. The store is the single source of truth — picking or pasting uploads
   straight away and re-reads the list, so the preview shows what will actually
   be sent and survives a reload. It also means a name is only ever stored once:
   re-uploading pic1.png replaces it instead of adding pic1-1.png (the old
   upload-at-Start-Campaign path re-sent every staged file on every run, which
   is how the folder filled with byte-identical copies). */
let campaignMedia = [];

const mediaSrc = m => `/api/campaign/media/${encodeURIComponent(m.name)}?v=${m.at || 0}`;

/* One stored image as a thumbnail (with a remove ✕). */
function mediaThumbHtml(item, i) {
  return `<div class="media-thumb" title="${esc(item.name)}">
            <img src="${mediaSrc(item)}" alt="${esc(item.name)}" loading="lazy">
            <span class="m-cap">${esc(item.name)}</span>
            <button type="button" class="m-del" data-media-del="${esc(item.name)}" aria-label="Remove ${esc(item.name)}">✕</button>
          </div>`;
}

function renderCampaignMedia() {
  const box = $('#campaign-media-list');
  if (!box) return;
  box.innerHTML = campaignMedia.length
    ? campaignMedia.map(mediaThumbHtml).join('')
    : '<p class="hint">Preview — pick images to attach</p>';
  box.querySelectorAll('[data-media-del]').forEach(btn =>
    btn.addEventListener('click', async () => {
      const name = btn.dataset.mediaDel;
      try {
        await api(`/campaign/media/${encodeURIComponent(name)}`, { method: 'DELETE' });
        toast(`Removed ${name}`);
      } catch (error) { toast(error.message, true); }
      await loadCampaignMedia();
    }));
}

/* Read the store and repaint the preview. */
async function loadCampaignMedia() {
  try {
    const { media } = await api('/campaign/media');
    campaignMedia = media || [];
  } catch { campaignMedia = []; }
  renderCampaignMedia();
}

/* Add image files from ANY source (picker, paste, drop): upload them to the
   store, then re-read it. Same filename overwrites, so re-adding an image the
   store already holds refreshes it rather than piling up another copy. */
async function addCampaignFiles(fileList) {
  const all = [...(fileList || [])];
  const picked = all.filter(isImageFile);
  const dropped = all.length - picked.length;
  if (dropped) toast(`${dropped} item(s) skipped — only JPG, JPEG and PNG images can be sent.`, true);
  if (!picked.length) return 0;
  try {
    const files = await Promise.all(picked.map(async f => ({
      name: f.name, type: f.type, data: await fileToBase64(f),
    })));
    await api('/campaign/media', { method: 'POST', body: { files } });
  } catch (error) {
    toast(error.message, true);
    return 0;
  }
  await loadCampaignMedia();
  return picked.length;
}

$('#campaign-media')?.addEventListener('change', async event => {
  // Snapshot the File objects BEFORE clearing the input: `input.value = ''`
  // empties the very FileList this reference points at, so reading it after
  // the reset yields nothing and the upload silently does nothing.
  const files = [...event.target.files];
  event.target.value = '';          // let the same file be re-picked after removal
  const n = await addCampaignFiles(files);
  if (n) toast(`${n} image(s) ready to send`);
});

/* Paste (Ctrl+V) image files anywhere on the campaign panel while media/both is
   selected — they're staged and shown in the right-side preview. */
function onCampaignPaste(event) {
  const dt = event.clipboardData;
  if (!dt) return;
  let files = [...(dt.files || [])].filter(isImageFile);
  if (!files.length) {
    // Some browsers surface a pasted image only via items, not files.
    files = [...(dt.items || [])]
      .filter(it => it.kind === 'file' && /^image\//i.test(it.type))
      .map(it => it.getAsFile()).filter(Boolean);
  }
  if (!files.length) return;         // plain-text paste → let it be
  event.preventDefault();
  addCampaignFiles(files).then(n => { if (n) toast(`Pasted ${n} image(s) to send`); });
}
document.addEventListener('paste', event => {
  const panel = $('#panel-campaign');
  if (!panel || panel.hidden) return;
  if (campaignMode !== 'media' && campaignMode !== 'both') return;
  const ae = document.activeElement;
  if (ae && ae.id === 'campaign-message' && !ae.readOnly) return;
  onCampaignPaste(event);
});

/* What the campaign delivers: 'text' | 'media' | 'both'. Set by the Send
   segmented control. Images are attached with ⬆️ Upload (or Ctrl+V) on the right. */
let campaignMode = 'text';
const CAMPAIGN_MODE_HINTS = {
  text:  'Text — write the assigned message only.',
  media: 'Media — attach image(s) with ⬆️ Upload (or Ctrl+V); no text.',
  both:  'Both — write the message and attach image(s); sent together.',
};

$('#campaign-mode')?.addEventListener('click', event => {
  const btn = event.target.closest('.seg-b');
  if (!btn) return;
  campaignMode = btn.dataset.mode || 'text';
  $$('#campaign-mode .seg-b').forEach(b => b.classList.toggle('on', b === btn));
  const hint = $('#campaign-mode-hint');
  if (hint) hint.textContent = CAMPAIGN_MODE_HINTS[campaignMode] || '';
});

/* Only JPG/JPEG/PNG count as attachable images. */
function isImageFile(file) {
  const type = (file.type || '').toLowerCase();
  if (type === 'image/jpeg' || type === 'image/jpg' || type === 'image/png') return true;
  return /\.(jpe?g|png)$/i.test(file.name || '');
}

/* Read a File as bare base64 (no data: prefix) for the JSON upload. */
function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(`Could not read ${file.name}`));
    reader.onload = () => {
      const result = String(reader.result || '');
      const comma = result.indexOf(',');
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.readAsDataURL(file);
  });
}

/* The stored filenames /automation/search-campaign attaches in each DM. The
   images are already in the store — they went up when they were picked — so a
   run just names them. Re-uploading here is what used to duplicate the folder. */
function uploadCampaignMedia() {
  return campaignMedia.filter(isImageFile).map(m => m.stored || m.name);
}

/* Loads the saved Instagram names from Data and shows them above the button.
   Runs every time the panel opens, so the list is always what Data holds now. */
async function loadCampaignList() {
  const note = $('#campaign-note');
  if (!$('#campaign-accounts')) return;   // only run when the Campaign panel is present
  if (note) {
    note.className = 'banner';
    note.textContent = 'Loading saved Instagram names from Data...';
  }
  loadCampaignAccounts();
  loadCampaignMedia();               // the store's images, so the preview persists
  try {
    const { usernames } = await api('/data/usernames');       // from MinIO (source of truth)
    const fresh = (usernames || []).map(n => String(n).trim().replace(/^@/, '')).filter(Boolean);
    // Only drop the range when the underlying list actually moved — otherwise
    // merely reopening the panel (or switching tabs, which reloads it) threw the
    // selection away and put all 2861 names back on screen. Same names in the
    // same order = the positions the range refers to still mean what they did.
    const samePositions =
      fresh.length === campaignNames.length &&
      fresh.every((name, i) => name === campaignNames[i]);
    if (!samePositions) campaignRange = null;
    campaignNames = fresh;
    if (campaignRange) {                                      // re-clamp, just in case
      campaignRange.from = Math.max(1, Math.min(campaignRange.from, campaignNames.length));
      campaignRange.to = Math.max(campaignRange.from, Math.min(campaignRange.to, campaignNames.length));
    }
    showCampaignQueue();
    if (note) {
      note.className = campaignNames.length ? 'banner good' : 'banner';
      note.textContent = campaignNames.length
        ? `${campaignNames.length} name(s) loaded from Data. Run to open each conversation.`
        : 'No saved Instagram names in Data yet — run Visit insta first.';
    }
  } catch (error) {
    campaignNames = [];
    renderCampaignTargets([]);
    if (note) {
      note.className = 'banner bad';
      note.textContent = `Could not load names from Data: ${error.message}`;
    }
  }
}

$('#btn-campaign-reload')?.addEventListener('click', async event => {
  const icon = event.currentTarget;
  icon.classList.remove('spinning');
  void icon.offsetWidth;              // restart the animation if clicked again mid-spin
  icon.classList.add('spinning');
  icon.addEventListener('animationend', () => icon.classList.remove('spinning'), { once: true });
  await loadCampaignList();
});

/* Names searched AND written to ("Hello" sent) — the ticked ones. Persisted in
   localStorage so the green marks survive a page refresh: a reload no longer
   re-sends to a profile that already got its message. "Repeat sending" is the
   deliberate reset that makes them runnable again. */
const CAMPAIGN_DONE_KEY = 'campaignDone';
const campaignDone = (() => {
  try { return new Set(JSON.parse(localStorage.getItem(CAMPAIGN_DONE_KEY) || '[]')); }
  catch { return new Set(); }
})();
const isDone = name => campaignDone.has(String(name).toLowerCase());
function saveCampaignDone() {
  localStorage.setItem(CAMPAIGN_DONE_KEY, JSON.stringify([...campaignDone]));
}
function markDone(name) {
  campaignDone.add(String(name).toLowerCase());
  saveCampaignDone();
}
function clearCampaignDone() {
  campaignDone.clear();
  localStorage.removeItem(CAMPAIGN_DONE_KEY);
}
const CAMPAIGN_POLL_MS = 1000;

/* The one campaign list. Before a run it shows the queue loaded from Data;
   after a run the same names carry their outcome — a name that opened nothing
   stays put, struck through, so a miss is visible rather than silently gone.
   A ✓ means searched and "Hello" written; those are skipped next run. */
/* Kept so the Full-preview modal can re-render every entry on demand. */
let campaignEntries = [];

/* The current/last campaign run, for the live "Campaign view" monitor. Set when
   Start Campaign fires; holds the per-name assignment + a reference to the live
   `runs` the poller updates, plus click-time status timestamps. Null = the view
   button stays disabled. */
let campaignRun = null;

/* Record the moment a name's status changes, so the table can show WHEN it moved
   to processing / sent rather than just the label. */
function stampCampaignStatus(name, status) {
  const stamps = campaignRun.stamps || (campaignRun.stamps = {});
  if (!stamps[name] || stamps[name].status !== status) {
    stamps[name] = { status, at: new Date() };
  }
  return stamps[name].at;
}

/* One table row per assigned name: its account, sms#, media, live status + time.
   Status: pending → processing (its account is on it now) → sent / staged
   (composed, Press ENTER off) / failed (opened nothing). */
function computeCampaignRows() {
  if (!campaignRun) return [];
  const { assign, runs, mode } = campaignRun;
  return assign.map(a => {
    const run = runs[a.accountId];
    const entry = run?.results?.find(r => r.query === a.name);
    let status = 'pending';
    if (entry) {
      if (!entry.opened) status = 'failed';
      else if (entry.message?.sent) status = 'sent';
      else if (entry.message?.written) status = 'staged';
      else status = 'opened';
    } else if (run?.current === a.name) {
      status = 'processing';
    }
    const at = stampCampaignStatus(a.name, status);
    // The one image this name was given, named so the row says which.
    const mediaLabel = mode === 'text' || a.imgNum == null
      ? '—'
      : `img${a.imgNum}${a.media ? ` · ${a.media}` : ''}`;
    // Which batch this name is in. The split is decided up front, so the
    // backend's batch_map gives every name its batch number from the start;
    // entry.batch_no confirms it once reached.
    const batches = entry?.batches ?? run?.batches ?? null;
    const batchNo = entry?.batch_no ?? run?.batchMap?.[a.name] ?? null;
    const batchLabel = !batches || batches < 2
      ? '—'
      : batchNo ? `${batchNo}/${batches}` : `?/${batches}`;
    return {
      name: a.name,
      accountId: a.accountId,
      accountNum: a.accountNum,
      who: run?.who || '',
      account: `${a.accountNum} · ${run?.who || ''}`,
      sms: mode === 'media' || a.smsNum == null ? '—' : (a.smsLabel || `sms${a.smsNum}`),
      media: mediaLabel,
      batch: batchLabel,
      batchNo,
      waiting: !!run?.waiting,
      status,
      time: (status === 'pending') ? '' : fmtClock(at),
    };
  });
}

const CV_STATUS_LABEL = {
  pending: 'Pending', processing: 'Processing', sent: 'Sent',
  staged: 'Staged', opened: 'Opened', failed: 'Failed',
};

/* One tab per account (index -> accountId). Which one is showing. */
let activeCampaignTab = null;

/* 8-colour cycle so consecutive batches are visibly different. */
function batchColorClass(batchNo) {
  if (!batchNo) return '';
  return `cv-b${((batchNo - 1) % 8) + 1}`;
}

function renderCampaignViewTable() {
  const body = $('#campaign-view-rows');
  const summary = $('#campaign-view-summary');
  const tabsEl = $('#campaign-view-tabs');
  if (!body) return;
  const rows = computeCampaignRows();

  // Accounts in this run, in assignment order, each becoming a tab.
  const accounts = [];
  const seenAcct = new Set();
  rows.forEach(r => {
    if (seenAcct.has(r.accountId)) return;
    seenAcct.add(r.accountId);
    accounts.push({ id: r.accountId, num: r.accountNum, who: r.who });
  });
  if (activeCampaignTab == null || !seenAcct.has(activeCampaignTab)) {
    activeCampaignTab = accounts[0]?.id ?? null;
  }

  if (tabsEl) {
    tabsEl.innerHTML = accounts.map(a => {
      const acctRows = rows.filter(r => r.accountId === a.id);
      const done = acctRows.filter(r => r.status === 'sent' || r.status === 'staged').length;
      const paused = acctRows.some(r => r.waiting);
      return `<button class="cv-tab ${a.id === activeCampaignTab ? 'on' : ''}" data-acct="${a.id}">
                ${esc(a.who || ('#' + a.id))}
                <span class="cv-tab-count">${done}/${acctRows.length}</span>
                ${paused ? '<span class="cv-tab-pause" title="Paused between batches">⏸</span>' : ''}
              </button>`;
    }).join('');
  }

  // Only the active account's rows, renumbered within the tab.
  let shown = rows.filter(r => r.accountId === activeCampaignTab);
  // Order the rows into their batches, in sequence — all of batch 1, then all of
  // batch 2, … — rather than the mixed assignment order. batch_map's key order
  // IS the run order (batch 1's names first, then batch 2's), so sorting by a
  // name's position in it groups the batches AND matches the processing order,
  // which keeps the interleaved action rows in their right place.
  const bmap = campaignRun?.runs?.[activeCampaignTab]?.batchMap;
  if (bmap && Object.keys(bmap).length) {
    const pos = new Map(Object.keys(bmap).map((name, i) => [name, i]));
    shown = shown.slice().sort((a, b) => (pos.get(a.name) ?? 1e9) - (pos.get(b.name) ?? 1e9));
  }
  // Random Actions run between rows for this account, grouped by the row they
  // followed (seq = number of rows done when it ran), so an "action row" can be
  // inserted right after that row.
  const acctActions = (campaignRun?.runs?.[activeCampaignTab]?.actions) || [];
  const actionsAfter = {};
  acctActions.forEach(a => { (actionsAfter[a.seq] ||= []).push(a); });
  const actionRow = a => {
    const st = a.status || 'pending';
    const cls = st === 'failed' ? 'cv-action-failed' : st === 'pending' ? 'cv-action-pending' : '';
    const mark = st === 'failed' ? '⚠️' : st === 'done' ? '✓' : '⏳';
    const suffix = st === 'failed' ? ' (failed)' : st === 'pending' ? ' (queued)' : '';
    return `<tr class="cv-action-row ${cls}">
       <td class="cv-num">⚡</td>
       <td class="cv-action-cell" colspan="6">Action — ${esc(a.action)}${suffix}</td>
       <td class="cv-time">${mark}</td>
     </tr>`;
  };

  body.innerHTML = shown.map((r, i) => {
    const cls = [batchColorClass(r.batchNo),
                 r.status === 'processing' ? 'cv-row-processing' : ''].filter(Boolean).join(' ');
    const nameRow = `<tr class="${cls}">
       <td class="cv-num">${i + 1}</td>
       <td class="cv-user">${esc(r.name)}</td>
       <td class="cv-mono">${esc(r.account)}</td>
       <td class="cv-mono">${esc(r.sms)}</td>
       <td class="cv-mono">${esc(r.media)}</td>
       <td class="cv-mono"><span class="cv-batch-tag ${batchColorClass(r.batchNo)}">${esc(r.batch)}</span></td>
       <td><span class="cv-status ${r.status}">${CV_STATUS_LABEL[r.status] || r.status}</span></td>
       <td class="cv-time">${esc(r.time)}</td>
     </tr>`;
    // action rows that fired after this row's position
    const acts = (actionsAfter[i + 1] || []).map(actionRow).join('');
    return nameRow + acts;
  }).join('');

  if (summary) {
    const n = rows.length;
    const by = s => rows.filter(r => r.status === s).length;
    const sent = by('sent'), staged = by('staged'), failed = by('failed');
    const processing = by('processing'), pending = by('pending');
    summary.className = 'banner banner-slim cv-summary' + (campaignRun?.running ? '' : ' good');
    // Batch total across the running accounts, so the header says how many
    // batches the configured size split this run into.
    const batchTotal = Object.values(campaignRun?.runs || {})
      .reduce((n, r) => n + (r.batches > 1 ? r.batches : 0), 0);
    const actionTotal = Object.values(campaignRun?.runs || {})
      .reduce((n, r) => n + (r.actions?.length || 0), 0);
    summary.textContent =
      `${campaignRun?.mode || ''} mode · ${n} name(s)` +
      (batchTotal ? ` · ${batchTotal} batch(es)` : '') + ' — ' +
      `⏳ ${pending} pending · ▶ ${processing} processing · ✅ ${sent} sent` +
      (staged ? ` · 📝 ${staged} staged` : '') +
      (failed ? ` · ⚠️ ${failed} failed` : '') +
      (actionTotal ? ` · ⚡ ${actionTotal} action(s)` : '') +
      (campaignRun?.running ? '' : ' · finished');
  }

  renderBatchCountdown();
}

/* The next-batch countdown for the account whose tab is OPEN — shown only on
   that tab, not shared across all. Each account's pause appears on its own tab. */
function renderBatchCountdown() {
  const el = $('#campaign-view-batch');
  const txt = $('#campaign-view-batch-text');
  if (!el || !txt) return;
  const r = campaignRun?.runs?.[activeCampaignTab];
  const left = (r && r.waiting && r.batchDeadline) ? (r.batchDeadline - Date.now()) / 1000 : 0;
  if (!left || left <= 0) { el.hidden = true; return; }   // this account isn't pausing
  el.hidden = false;
  const batchLabel = (r.batchNo && r.batches)
    ? ` (batch ${r.batchNo}/${r.batches}${r.who ? ' · ' + r.who : ''})` : '';
  txt.textContent = `Remaining time for next batch start ${fmtHMS(left)}${batchLabel}`;
}
// Tick the countdown every second while the Campaign view is open, so it counts
// down smoothly between the 1s progress polls.
setInterval(() => { if ($('#campaign-view')?.open) renderBatchCountdown(); }, 1000);

function openCampaignView() {
  if (!campaignRun) { toast('Press Start Campaign first.'); return; }
  const title = $('#campaign-view-title');
  if (title) title.textContent = `Campaign run — ${campaignRun.mode} mode`;
  renderCampaignViewTable();
  $('#campaign-view')?.showModal();
}

/* Enable/disable the (static) Campaign view button — it opens the live run table,
   so it's only usable once Start Campaign has created a run. */
function syncCampaignViewBtn() {
  const btn = $('#btn-campaign-view');
  if (!btn) return;
  btn.disabled = !campaignRun;
  btn.title = campaignRun
    ? 'Live table of the running / last campaign'
    : 'Available once you press Start Campaign';
}

$('#btn-campaign-view')?.addEventListener('click', openCampaignView);
$('#btn-campaign-view-close')?.addEventListener('click', () => $('#campaign-view')?.close());

/* Switch which account's campaign list the table shows. */
$('#campaign-view-tabs')?.addEventListener('click', e => {
  const tab = e.target.closest('.cv-tab');
  if (!tab) return;
  activeCampaignTab = Number(tab.dataset.acct);
  renderCampaignViewTable();
});

/* Campaign settings ⚙️ (left of the >_ terminal button): the same campaign keys
   as Config → Campaign batching / actions, in a dialog on the Campaign panel.
   Both edit one shared store, so a save in either place updates the other. */
const CAMP_SET_FIELDS = {
  batch_size_min: '#camp-batch-size-min', batch_size_max: '#camp-batch-size-max',
  batch_delay_min: '#camp-batch-delay-min', batch_delay_max: '#camp-batch-delay-max',
  campaign_action_min: '#camp-action-min', campaign_action_max: '#camp-action-max',
};
function readCampSet() {
  const cfg = {};
  for (const [k, sel] of Object.entries(CAMP_SET_FIELDS)) cfg[k] = Number($(sel).value);
  cfg.batch_enabled = $('#camp-batch-enabled').checked;
  cfg.press_enter = $('#camp-press-enter').checked;
  return cfg;
}
function writeCampSet(s) {
  for (const [k, sel] of Object.entries(CAMP_SET_FIELDS)) if ($(sel) && s[k] !== undefined) $(sel).value = s[k];
  $('#camp-batch-enabled').checked = !!s.batch_enabled;
  $('#camp-press-enter').checked = s.press_enter === undefined ? true : !!s.press_enter;
}
async function loadCampSet() {
  try {
    const { settings } = await api('/settings');
    writeCampSet(settings);
    if (typeof writeCfg === 'function') writeCfg(settings);   // mirror into Config panel
  } catch (error) {
    const note = $('#camp-set-note');
    if (note) note.textContent = error.message;
  }
}

$('#btn-campaign-settings')?.addEventListener('click', async () => {
  const note = $('#camp-set-note');
  if (note) note.textContent = '';
  await loadCampSet();                 // fresh every open — never a stale form
  $('#campaign-settings')?.showModal();
});

$('#btn-camp-set-save')?.addEventListener('click', async event => {
  event.preventDefault();              // don't let the form close before the PUT
  const note = $('#camp-set-note');
  try {
    const { settings } = await api('/settings', { method: 'PUT', body: readCampSet() });
    writeCampSet(settings);            // echo back the stored (clamped/ordered) values
    if (typeof writeCfg === 'function') writeCfg(settings);   // keep Config panel in sync
    toast('Campaign settings saved');
    $('#campaign-settings')?.close();
  } catch (error) {
    if (note) note.textContent = error.message;
    toast(error.message, true);
  }
});

/* Backdrop click closes the campaign-settings dialog (same guard as ⚙️ Scrapping). */
(() => {
  const dialog = $('#campaign-settings');
  if (!dialog) return;
  let downOnBackdrop = false;
  dialog.addEventListener('mousedown', e => { downOnBackdrop = e.target === dialog; });
  dialog.addEventListener('click', e => { if (e.target === dialog && downOnBackdrop) dialog.close(); });
})();

/* ── Setup Campaign wizard ─────────────────────────────────────────
   A stepwise pop-up (auto-opens on refresh): Login → Users → Messages →
   Start, then hands off to the Campaign panel in a running state. */
const setup = { step: 1, accountId: null, loggedIn: false,
                allData: [], usernames: [], drafts: [], savedMedia: [], mode: 'text',
                catRows: [], catHidden: new Set(), catRange: null, usedFilter: null };   // Users step: categorized DB view state
let setupCatView = null;   // the mounted handles view, so "Use range" can re-render it

/* A row's campaign-usage count vs the used filter: null = any, else an exact match. */
function matchUsed(n, filter) {
  if (filter == null) return true;
  return (Number(n) || 0) === Number(filter);
}
/* The pool the range slices: category-ticked, then narrowed by the used filter. */
function setupCatPool() {
  let rows = setup.catRows.filter(r => !setup.catHidden.has(String(r.category || '').trim()));
  if (setup.usedFilter != null) rows = rows.filter(r => matchUsed(r.used_count, setup.usedFilter));
  return rows;
}

/* Accordion sections for the Users step — Used-in-campaigns and Range. Both are
   STAGED: they just hold their values (from setup state), and the global Apply
   button reads them (setupApplyFilters). */
function setupUsedSection() {
  return {
    id: 'used', title: 'Used in campaigns', open: false,
    build: (body) => {
      body.innerHTML = `<label class="cat-used-row">
          <input type="checkbox" class="cat-used-on"> used exactly
          <input type="number" class="cat-used-n" min="0" step="1" value="1"> time(s)
        </label>
        <span class="hint">Off = any. Tick and set the count, then Apply.</span>`;
      body.querySelector('.cat-used-on').checked = setup.usedFilter != null;
      if (setup.usedFilter != null) body.querySelector('.cat-used-n').value = setup.usedFilter;
    },
  };
}
function setupRangeSection() {
  return {
    id: 'range', title: 'Range', open: false,
    build: (body) => {
      const from = setup.catRange ? setup.catRange.from : 1;
      const to = setup.catRange ? setup.catRange.to : setup.catRows.length;
      body.innerHTML = `<div class="cat-range">
          <div class="cat-range-inputs">
            <label>from <input type="number" min="1" step="1" class="cat-range-from" value="${from}"></label>
            <label>to <input type="number" min="1" step="1" class="cat-range-to" value="${to}"></label>
          </div>
          <span class="hint">Positions in the filtered list. Applied with Apply.</span>
        </div>`;
    },
  };
}

/* The global Apply: read every staged filter (used + range; the search box is
   read by the view) into setup state, then let the view re-render + push the
   selection. Wired via the component's onApply, which hands us its apply(). */
function setupApplyFilters(apply) {
  const box = $('#setup-users-preview');
  const on = box?.querySelector('.cat-used-on')?.checked;
  const n = Math.max(0, Math.floor(Number(box?.querySelector('.cat-used-n')?.value) || 0));
  setup.usedFilter = on ? n : null;
  const poolN = setupCatPool().length;                     // category-ticked + used
  const from = Math.max(1, Math.floor(Number(box?.querySelector('.cat-range-from')?.value) || 1));
  const to = Math.min(poolN, Math.max(from, Math.floor(Number(box?.querySelector('.cat-range-to')?.value) || poolN)));
  setup.catRange = (from <= 1 && to >= poolN) ? null : { from, to };   // full span = no slice
  apply();
}

function setupGoto(step) {
  setup.step = Math.max(1, Math.min(5, step));
  $$('#wiz-steps .wiz-step').forEach(li => {
    const n = Number(li.dataset.step);
    li.classList.toggle('on', n <= setup.step);
    li.classList.toggle('current', n === setup.step);
  });
  $$('#campaign-setup .wiz-pane').forEach(p =>
    p.classList.toggle('on', Number(p.dataset.pane) === setup.step));
  $('#btn-setup-back').disabled = setup.step === 1;
  $('#btn-setup-next').hidden = setup.step === 5;   // step 5 (Start) has its own button
  if (setup.step === 2) renderSetupUsers();          // empty state until file/Database load
  if (setup.step === 3) { renderSmsTabs(); showActiveDraft(); }   // sms1 ✎ · sms2 ✎ · … · + · 💾
  // step 4 (Media) starts EMPTY — load only on the 🗄️ Database click.
  if (setup.step === 4) renderSetupMediaPreview();   // shows the big-icon upload state
  if (setup.step === 5) renderSetupSummary();
  updateWizProgress();                               // header stepper fills to the current step
  updateSetupGate();                                 // skip-media visibility + Continue gate
}
/* Fill the header progress stepper's circles/lines up to the current step. */
function updateWizProgress() {
  const box = $('#wiz-progress'); if (!box) return;
  box.querySelectorAll('.wp-node').forEach(el => {
    const n = Number(el.dataset.n);
    el.classList.toggle('on', n <= setup.step);
    el.classList.toggle('current', n === setup.step);
  });
  box.querySelectorAll('.wp-line').forEach(el => {
    el.classList.toggle('on', Number(el.dataset.after) < setup.step);   // filled once past that node
  });
}
/* Move the Setup Campaign wizard INTO the Campaign panel and render it inline
   (non-modal), so it's a real panel section rather than a pop-up. Kept as a
   <dialog> opened with .show() so `.open` stays true (the sms tabs read it). */
function mountSetupInline() {
  const wiz = $('#campaign-setup');
  const host = $('#panel-campaign .panel-body');
  if (!wiz || !host || wiz.classList.contains('wiz-inline')) return;
  wiz.classList.add('wiz-inline');
  host.prepend(wiz);                        // first child of the panel body
  try { if (!wiz.open) wiz.show(); } catch { wiz.setAttribute('open', ''); }
}

// Reset to step 1 (and make sure the inline wizard is showing).
function openCampaignSetup() {
  mountSetupInline();
  if (!$$('#setup-accounts .setup-acct-row').length) addSetupAcctRow();   // start with one
  const wiz = $('#campaign-setup');
  try { if (wiz && !wiz.open) wiz.show(); } catch { wiz?.setAttribute('open', ''); }
  setupGoto(1);
}

$('#btn-setup-next')?.addEventListener('click', () => setupGoto(setup.step + 1));
$('#btn-setup-back')?.addEventListener('click', () => setupGoto(setup.step - 1));
$('#btn-campaign-setup')?.addEventListener('click', openCampaignSetup);   // head: restart at step 1
$('#btn-campaign-open-view')?.addEventListener('click', openCampaignView);
/* Per-step help shown by each step's ⓘ icon. */
const STEP_HELP = {
  1: 'Add one or more Instagram accounts (username / email / phone + password) and log each in. At least one account must be logged in to continue.',
  2: 'Upload a .txt / .csv of usernames, or load them from 🗄️ Database. Use the from–to range to pick how many. At least one user is required.',
  3: 'Write a message, or load your saved drafts from 🗄️ Database into the sms tabs — each user gets a random one. Tick “Skip messages” to send no text.',
  4: 'Optionally attach images — upload, or load from 🗄️ Database. A message OR media is required. Tick “Skip media” to send no image.',
  5: 'A final checklist of steps 1–4. When every item shows a blue tick, press 🚀 Start Campaign to run and open the live Campaign view.',
};
function showStepInfo(step, anchor) {
  const pop = $('#wiz-info-pop'); if (!pop) return;
  pop.textContent = STEP_HELP[step] || '';
  pop.hidden = false;                                    // unhide first so we can measure it
  const r = anchor.getBoundingClientRect();
  pop.style.top = `${r.bottom + 6}px`;
  pop.style.left = `${Math.max(8, Math.min(r.left - 8, window.innerWidth - pop.offsetWidth - 12))}px`;
}
function hideStepInfo() { const p = $('#wiz-info-pop'); if (p) p.hidden = true; }
// HOVER the ⓘ to open its help panel (mouseover/out bubble, so delegate here).
$('#wiz-steps')?.addEventListener('mouseover', e => {
  const info = e.target.closest('.wiz-info'); if (info) showStepInfo(Number(info.dataset.info), info);
});
$('#wiz-steps')?.addEventListener('mouseout', e => {
  if (e.target.closest('.wiz-info')) hideStepInfo();
});
$('#wiz-steps')?.addEventListener('click', e => {
  if (e.target.closest('.wiz-info')) { e.stopPropagation(); return; }   // ⓘ is hover-only, never navigates
  const li = e.target.closest('.wiz-step'); if (li) setupGoto(Number(li.dataset.step));
});

/* Step 1 — one or more account rows; each creates the account then logs it in
   (reuses the tested login flow). */
function addSetupAcctRow() {
  const box = $('#setup-accounts');
  if (!box) return;
  const row = document.createElement('div');
  row.className = 'setup-acct-row';
  row.innerHTML =
    '<input class="mini acct-label" placeholder="optional">' +
    '<input class="mini acct-login" placeholder="username / email / phone">' +
    '<input class="mini acct-pass" type="password" placeholder="password">' +
    '<button class="btn btn-sm acct-login-btn" type="button">Login</button>' +
    '<button class="icon-btn acct-close" type="button" aria-label="Close this account browser" title="Close this account’s browser window">⏻</button>' +
    '<span class="acct-status hint"></span>' +
    '<button class="icon-btn acct-remove" type="button" aria-label="Remove account">✕</button>';
  box.appendChild(row);
}
const IG_LOGIN_URL = 'https://www.instagram.com/accounts/login/';

/* Drive the SAME cursor login sequence as Account Connections — open Chrome, new
   tab, type the URL, type credentials, then press Log in with the real cursor
   (falling back to the DOM click) — but silently: no visible steps, only a small
   status. Each endpoint runs the actual Playwright/cursor work at the backend. */
async function silentCursorLogin(accountId, status) {
  const acct = { account_id: accountId };
  const set = t => { if (status) { status.className = 'acct-status hint'; status.textContent = t; } };
  set('opening…');      await api('/automation/open', { method: 'POST', body: acct });
  set('new tab…');      await api('/automation/new-tab', { method: 'POST', body: acct });
  set('typing URL…');   await api('/automation/cursor-url', { method: 'POST', body: { url: IG_LOGIN_URL, account_id: accountId } });
  set('typing creds…'); await api('/automation/type-credentials', { method: 'POST', body: acct });
  // Click Login the way a person does — cursor first (retype + retry a few
  // times), then the DOM click as a fallback.
  for (const method of ['cursor', 'cursor', 'cursor', 'inspect', 'inspect']) {
    set(`login (${method})…`);
    let r = null;
    try { r = await api('/automation/click-login', { method: 'POST', body: { account_id: accountId, method } }); }
    catch { r = null; }
    if (r?.logged_in) return r;
    if (r && ['2fa_required', 'challenge'].includes(r.action)) return r;   // needs the user
    await api('/automation/type-credentials', { method: 'POST', body: acct }).catch(() => {});
  }
  return { logged_in: false, detail: 'Login did not complete after retries.' };
}

async function loginSetupRow(row) {
  const login = row.querySelector('.acct-login').value.trim();
  const password = row.querySelector('.acct-pass').value;
  const label = row.querySelector('.acct-label').value.trim();
  const status = row.querySelector('.acct-status');
  if (!login || !password) { status.className = 'acct-status hint bad'; status.textContent = 'need username + password'; return; }
  const set = (cls, txt) => { status.className = 'acct-status hint' + (cls ? ' ' + cls : ''); status.textContent = txt; };
  set('', 'starting…');
  try {
    let accountId;
    try {
      const { account } = await api('/accounts', { method: 'POST', body: { label, login, password } });
      accountId = account.id;
    } catch (e) {
      // "<login> is already saved." — don't fail: reuse that account and open its
      // stored session window directly (it's usually already logged in).
      if (!/already saved/i.test(e.message || '')) throw e;
      const { accounts } = await api('/accounts');
      const existing = (accounts || []).find(a => (a.login || '').toLowerCase() === login.toLowerCase());
      if (!existing) throw e;
      row.dataset.accountId = existing.id;
      set('', 'opening saved session…');
      await api(`/accounts/${existing.id}/open`, { method: 'POST' });   // open its window
      if (existing.logged_in === false) { set('', '🌐 opened — not logged in'); }
      else { row.dataset.loggedIn = '1'; set('good', '✅ opened saved session'); }
      return;
    }
    row.dataset.accountId = accountId;
    const r = await silentCursorLogin(accountId, status);              // same cursor steps, hidden
    if (r.logged_in) { row.dataset.loggedIn = '1'; set('good', '✅ logged in'); }
    else if (['2fa_required', 'challenge'].includes(r.action)) { set('', '⚠️ finish 2FA / verify in Chrome'); }
    else { set('bad', r.detail || "didn't complete"); }
  } catch (e) { set('bad', e.message); }
  finally { updateSetupGate(); }                 // ≥1 logged-in account unlocks Continue
}
/* The account ids that logged in — the campaign fans out across them. */
function loggedInAccountIds() {
  return $$('#setup-accounts .setup-acct-row')
    .filter(r => r.dataset.loggedIn === '1')
    .map(r => Number(r.dataset.accountId));
}
/* ⏻ — close just this account's Chrome window (its saved session stays on disk).
   The window is what the campaign drives, so a closed one is no longer ready:
   clear its logged-in flag and re-check the gate. Log in again to reopen it. */
async function closeSetupRow(row) {
  const id = Number(row.dataset.accountId);
  const status = row.querySelector('.acct-status');
  const set = (cls, txt) => { status.className = 'acct-status hint' + (cls ? ' ' + cls : ''); status.textContent = txt; };
  if (!id) { set('', 'not opened yet'); return; }
  set('', 'closing…');
  try {
    await api(`/accounts/${id}/session/close`, { method: 'POST' });
    delete row.dataset.loggedIn;          // window closed → not ready for the campaign
    set('', '⏻ browser closed');
  } catch (e) { set('bad', e.message); }
  finally { updateSetupGate(); }
}
$('#setup-accounts')?.addEventListener('click', e => {
  const row = e.target.closest('.setup-acct-row'); if (!row) return;
  if (e.target.closest('.acct-login-btn')) loginSetupRow(row);
  else if (e.target.closest('.acct-close')) closeSetupRow(row);
  else if (e.target.closest('.acct-remove')) {
    if ($$('#setup-accounts .setup-acct-row').length > 1) row.remove();
  }
});
$('#btn-setup-add-acct')?.addEventListener('click', addSetupAcctRow);
$('#btn-setup-login-all')?.addEventListener('click', async () => {
  for (const row of $$('#setup-accounts .setup-acct-row')) await loginSetupRow(row);
});

/* Step 2 — Users: an upload icon + Database. Database loads the SAME categorized
   view as the Data panel (username · bio · category table + tickable category
   list); ticking categories picks which handles the campaign targets. A file
   upload keeps the simple chip list + range. */
function renderSetupUsers() {
  const box = $('#setup-users-preview'); if (!box) return;
  const range = $('#setup-users-range');

  // Database source → the shared categorized view with an ACCORDION side panel:
  // Filter · Used · Range · Categories, each a collapsible row. The ticks + used
  // filter + range together drive which handles the campaign targets.
  if (setup.catRows && setup.catRows.length) {
    if (!setup.catHidden) setup.catHidden = new Set();
    if (range) range.hidden = true;                       // DB filters live in the accordion
    setupCatView = mountHandlesView(box, setup.catRows, {
      categoryList: true,
      hidden: setup.catHidden,
      accordion: true,
      manual: true,                                   // filters stage; Apply applies them
      // Positional filter: narrow by the used count, then to the range slice.
      rowFilter: rows => {
        let r = rows;
        if (setup.usedFilter != null) r = r.filter(row => matchUsed(row.used_count, setup.usedFilter));
        if (setup.catRange) r = r.slice(setup.catRange.from - 1, setup.catRange.to);
        return r;
      },
      onChange: applySetupCatSelection,               // fires on Apply (and initial mount)
      onApply: setupApplyFilters,
      sideSections: [setupUsedSection(), setupRangeSection()],
    });
    return;
  }
  setupCatView = null;

  if (!setup.allData || !setup.allData.length) {          // empty state
    if (range) range.hidden = true;
    box.innerHTML = `<div class="wiz-media-empty">
        <img src="data/upload.png" class="wiz-upload-big" data-users-upload alt="Upload file" title="Upload file">
        <button type="button" class="btn btn-sm" data-users-upload>Upload file (.txt, .csv, .xlsx)</button>
        <span class="hint">or <img src="data/db.png" class="db-ico" alt=""> Database to load categorized users</span>
      </div>`;
    return;
  }
  if (range) range.hidden = false;                        // range shows after a file load
  const shown = (setup.usernames || []).slice(0, 300);
  box.innerHTML = shown.map(u => `<span class="chip">@${esc(u)}</span>`).join('') +
    ((setup.usernames || []).length > 300 ? ` <span class="hint">…and ${setup.usernames.length - 300} more</span>` : '');
}

/* The categorized view calls this whenever the ticks OR the range change, with the
   ACTIVE rows (category-filtered, then used-filtered, then range-sliced). Their
   handles are the campaign targets. Updates the note + gate directly (no re-render,
   so the view isn't torn down under a click). */
function applySetupCatSelection(rows) {
  setup.usernames = (rows || []).map(r => r.username).filter(Boolean);   // what's shown = what's used
  setup.allData = setupCatPool().map(r => r.username).filter(Boolean);   // category + used pool (for "of N")
  updateSetupUsersNote();
}
/* Note + gate for the categorized (DB) source, without re-mounting the view. */
function updateSetupUsersNote() {
  const pool = setup.allData.length, sel = setup.usernames.length;
  const hiddenN = setup.catHidden ? setup.catHidden.size : 0;
  const note = $('#setup-users-note');
  if (note) note.textContent = sel
    ? `${sel} user(s) selected` + (sel !== pool ? ` (of ${pool})` : '')
      + (hiddenN ? ` · ${hiddenN} categor${hiddenN === 1 ? 'y' : 'ies'} unticked` : ' · all categories')
    : 'No users selected — tick a category on the right.';
  updateSetupGate();
}
function setUsersNote() {
  $('#setup-users-note').textContent = setup.usernames.length
    ? `${setup.usernames.length} user(s) selected (of ${setup.allData.length}).` : 'No users selected yet.';
  renderSetupUsers();
  updateSetupGate();                             // ≥1 selected user unlocks Continue
}
/* A source was loaded (file or DB): default to the whole list + show the range. */
function setSetupUsersSource(all, label) {
  setup.allData = all || [];
  setup.usernames = setup.allData.slice();               // all selected by default
  $('#setup-data-count').textContent = `${setup.allData.length} ${label}`;
  $('#setup-range-from').value = 1;
  $('#setup-range-to').value = setup.allData.length;
  setUsersNote();
}
async function loadSetupUsersFromDb() {
  try {
    // The categorized store (username · bio · category, + used_count joined from the
    // bio store) — the same source the Data panel's Categorized tab shows.
    const { records } = await api('/data/usernames-categories');
    setup.catRows = records || [];
    setup.catHidden = new Set();                        // all categories ticked by default
    setup.catRange = null;                              // fresh load → no range applied
    setup.usedFilter = null;                              // …and no used-count filter
    renderSetupUsers();                                 // mounts the view; onChange sets the selection
  } catch (e) { toast(e.message, true); }
}
/* 🗄️ Database toggles: click loads the categorized users, click again clears. */
$('#btn-setup-load-data')?.addEventListener('click', async () => {
  const btn = $('#btn-setup-load-data');
  if (btn.classList.contains('on')) {                 // clear the fetched users
    setup.catRows = []; setup.catHidden = new Set(); setup.catRange = null; setup.usedFilter = null;
    setup.allData = []; setup.usernames = [];
    setUsersNote();                                    // back to the empty state, range hidden
    btn.classList.remove('on');
  } else {
    await loadSetupUsersFromDb();
    if (setup.catRows && setup.catRows.length) btn.classList.add('on');
  }
});
/* Head-row "Use range" is the FILE source only now (the DB view has its own Range
   row inside the accordion). Slice the plain list and re-render the chip preview. */
$('#btn-setup-apply-range')?.addEventListener('click', () => {
  const from = Math.max(1, Number($('#setup-range-from').value) || 1);
  const to = Math.min(setup.allData.length, Number($('#setup-range-to').value) || setup.allData.length);
  setup.usernames = setup.allData.slice(from - 1, to);
  setUsersNote();
});
/* "Used: …" selector — narrows the pool to handles used that many times, then
   re-renders (the range resets, since the pool changed). */
/* The big Upload icon / button opens the file picker. */
$('#setup-users-preview')?.addEventListener('click', e => {
  if (e.target.closest('[data-users-upload]')) $('#setup-user-fileinput')?.click();
});
$('#setup-user-fileinput')?.addEventListener('change', async e => {
  const file = e.target.files[0]; e.target.value = ''; if (!file) return;
  if (file.name.toLowerCase().endsWith('.xlsx')) {
    toast('XLSX needs the backend parser — use txt/csv or Database for now.', true); return;
  }
  const text = await file.text();
  const names = text.split(/[\r\n,;]+/).map(s => s.trim().replace(/^@/, '')).filter(Boolean);
  setup.catRows = []; setup.catRange = null; setup.usedFilter = null;   // switching to a file source
  setSetupUsersSource(names, `name(s) from ${file.name}`);
  $('#btn-setup-load-data')?.classList.remove('on');   // source is now the file, not Database
});

/* Mode is INFERRED from what's filled AND not skipped: message + media = both;
   only a message = text; only media = media (default text when neither). */
function inferSetupMode() {
  const hasMsg = setupMessagePool().length > 0;
  const hasMedia = !$('#setup-skip-media')?.checked && !!(setup.media && setup.media.length);
  if (hasMsg && hasMedia) return 'both';
  if (hasMedia) return 'media';
  return 'text';
}
/* The non-empty drafts as {text, label} pairs, in tab order — `label` is the tab's
   display name (a rename like "travelling", else sms1/sms2/…), so an assignment
   can show which draft it picked by its real name rather than a pool position. */
function setupMessagePairs() {
  if ($('#setup-skip-msg')?.checked) return [];       // messages skipped -> none
  syncActiveDraft();                                  // capture the in-progress edit
  return campaignSms
    .map((s, i) => ({ text: (s.text || '').trim(), label: smsLabel(i) }))
    .filter(p => p.text);
}
/* Just the texts (gate checks, pool size). */
function setupMessagePool() {
  return setupMessagePairs().map(p => p.text);
}
/* When there's NO message, media becomes required: hide "Skip media" and block
   Continue on the Media step until an image is attached (can't send nothing). */
function updateSetupGate() {
  const hasMsg = setupMessagePool().length > 0;
  const skipLabel = $('#campaign-setup .wiz-pane[data-pane="4"] .wiz-skip');
  const skipMedia = $('#setup-skip-media');
  if (!hasMsg) {                                  // no message -> media mandatory
    if (skipMedia) skipMedia.checked = false;
    if (skipLabel) skipLabel.style.display = 'none';
  } else if (skipLabel) {
    skipLabel.style.display = '';
  }
  const hasMedia = !skipMedia?.checked && !!(setup.media && setup.media.length);
  // Per-step Continue gate: can't advance until the step's requirement is met.
  const next = $('#btn-setup-next');
  if (next) {
    let blocked = false;
    if (setup.step === 1) blocked = loggedInAccountIds().length === 0;         // need ≥1 logged-in account
    else if (setup.step === 2) blocked = (setup.usernames || []).length === 0; // need ≥1 selected user
    else if (setup.step === 4) blocked = !hasMsg && !hasMedia;                 // need a message or media
    next.disabled = blocked;
    next.title = blocked
      ? (setup.step === 1 ? 'Log in at least one account to continue'
        : setup.step === 2 ? 'Select or upload at least one user to continue'
        : 'Add a message or media to continue')
      : '';
  }
}
/* Load saved media from Database — tag them fromDb so a second click can clear
   just the fetched ones (uploads stay). */
async function loadSetupMedia() {
  try {
    const { media } = await api('/campaign/media');
    const fetched = (media || []).map(m => ({ ...m, fromDb: true }));
    const uploaded = (setup.savedMedia || []).filter(m => !m.fromDb);
    const have = new Set(uploaded.map(m => m.name));
    setup.savedMedia = [...uploaded, ...fetched.filter(m => !have.has(m.name))];
    // All loaded images are CHECKED by default (untick any to leave out).
    setup.media = setup.media || [];
    setup.savedMedia.forEach(m => { if (!setup.media.includes(m.name)) setup.media.push(m.name); });
    renderSetupMediaPreview();
  } catch (e) { toast(e.message, true); }
}

/* 🗄️ Database toggle: load the saved message drafts INTO the sms tabs (sms1 ✎
   sms2 ✎ …), click again to clear back to a single empty tab. Shares the same
   `campaignSms` store as the Campaign panel, so a save here saves everywhere. */
$('#btn-setup-load-sms')?.addEventListener('click', async () => {
  const btn = $('#btn-setup-load-sms');
  if (btn.classList.contains('on')) {                 // clear -> one empty draft tab
    campaignSms.length = 0; campaignSms.push({ text: '' });
    activeSms = 0; showActiveDraft(); renderSmsTabs();
    btn.classList.remove('on');
  } else {
    await loadSmsDrafts();                             // pull saved drafts into the tabs
    renderSmsTabs(); showActiveDraft();
    if (campaignSms.some(s => (s.text || '').trim())) btn.classList.add('on');
  }
  updateSetupGate();
});
$('#btn-setup-load-media')?.addEventListener('click', async () => {
  const btn = $('#btn-setup-load-media');
  if (btn.classList.contains('on')) {                 // clear the fetched (db) media, keep uploads
    setup.savedMedia = (setup.savedMedia || []).filter(m => !m.fromDb);
    const names = new Set(setup.savedMedia.map(m => m.name));
    setup.media = (setup.media || []).filter(n => names.has(n));
    renderSetupMediaPreview();
    btn.classList.remove('on');
  } else { await loadSetupMedia(); btn.classList.add('on'); }
  updateSetupGate();
});

/* Thumbnail preview — each stored image; click to attach/detach. Empty = a big
   icon with an Upload button in the middle. */
function renderSetupMediaPreview() {
  const box = $('#setup-media-preview'); if (!box) return;
  if (!(setup.savedMedia && setup.savedMedia.length)) {
    box.innerHTML = `<div class="wiz-media-empty">
        <img src="data/upload.png" class="wiz-upload-big" data-media-upload alt="Upload media" title="Upload media">
        <button type="button" class="btn btn-sm" data-media-upload>Upload media</button>
        <span class="hint">or <img src="data/db.png" class="db-ico" alt=""> Database to load saved images</span>
      </div>`;
    return;
  }
  const sel = new Set(setup.media || []);
  box.innerHTML = setup.savedMedia.map(m => `
      <label class="wiz-thumb ${sel.has(m.name) ? 'on' : ''}" data-media="${esc(m.name)}" title="${esc(m.name)}">
        <img src="/api/campaign/media/${encodeURIComponent(m.name)}?v=${m.at || 0}" alt="${esc(m.name)}" loading="lazy">
        <span class="m-cap">${esc(m.name)}</span>
      </label>`).join('');
}
/* Click a thumbnail to toggle attach; the center button opens the file picker. */
$('#setup-media-preview')?.addEventListener('click', e => {
  if (e.target.closest('[data-media-upload]')) { $('#setup-media-file')?.click(); return; }
  const t = e.target.closest('.wiz-thumb'); if (!t) return;
  e.preventDefault();
  const name = t.dataset.media;
  setup.media = setup.media || [];
  if (t.classList.toggle('on')) { if (!setup.media.includes(name)) setup.media.push(name); }
  else setup.media = setup.media.filter(n => n !== name);
  updateSetupGate();                          // media selection changed
});
/* Keep the gate live as messages / media / skip change. Typing edits the active
   draft (the box is the sms tabs' editor), so sync it too. */
$('#setup-message')?.addEventListener('input', () => { syncActiveDraft(); updateSetupGate(); });
$('#setup-skip-media')?.addEventListener('change', updateSetupGate);
$('#setup-skip-msg')?.addEventListener('change', e => {
  // grey out the sms tabs + editor while skipped, then re-run the gate
  const off = e.target.checked;
  const msgPane = $('#campaign-setup .wiz-pane[data-pane="3"]');
  msgPane?.querySelectorAll('#setup-sms-tabs, #setup-message').forEach(el => el.classList.toggle('is-skipped', off));
  updateSetupGate();
});
$('#setup-media-file')?.addEventListener('change', async e => {
  const files = [...e.target.files]; e.target.value = ''; if (!files.length) return;
  const n = await addCampaignFiles(files);          // reuse the tested upload path
  if (n) {
    // Show ONLY the just-uploaded images in the preview (storage isn't pulled in
    // unless "From storage" is clicked), and auto-attach them.
    const now = Date.now();
    setup.savedMedia = setup.savedMedia || [];
    setup.media = setup.media || [];
    files.filter(isImageFile).forEach(f => {
      if (!setup.savedMedia.some(m => m.name === f.name)) setup.savedMedia.push({ name: f.name, at: now });
      if (!setup.media.includes(f.name)) setup.media.push(f.name);
    });
    renderSetupMediaPreview();
    updateSetupGate();
    toast(`${n} image(s) uploaded`);
  }
});

/* Step 5 — a checklist of the first four steps. Every item must pass before the
   Start Campaign button enables:
     • at least one account logged in (step 1)
     • at least one username selected (step 2)
     • a message OR media to send (steps 3 / 4) */
function renderSetupSummary() {
  setup.media = setup.media || [];          // kept in sync by the thumbnail clicks
  const nAcct = loggedInAccountIds().length;
  const nUsers = setup.usernames.length;
  const pool = setupMessagePool();
  const nMedia = $('#setup-skip-media')?.checked ? 0 : setup.media.length;
  const hasMsg = pool.length > 0, hasMedia = nMedia > 0;

  const checks = [
    { ok: nAcct >= 1, label: 'Logged in on at least one account', step: 'Step 1' },
    { ok: nUsers >= 1, label: 'At least one username selected', step: 'Step 2' },
    { ok: hasMsg || hasMedia, label: 'A message or media to send', step: 'Step 3–4' },
  ];
  const allOk = checks.every(c => c.ok);

  const box = $('#setup-summary');
  const start = $('#btn-setup-start');
  if (start) { start.disabled = true; start.title = 'Checking…'; }   // gated while verifying
  if (!box) return;

  // Each row starts as a spinning circle (label left, mark right), then resolves
  // one by one to a tick (blue) or cross (red).
  box.innerHTML = checks.map(c =>
    `<div class="wiz-check loading" data-ok="${c.ok ? 1 : 0}">
       <span class="wiz-check-step">${esc(c.step)}</span>
       <span class="wiz-check-label">${esc(c.label)}</span>
       <span class="wiz-check-mark"><span class="wiz-check-spin" aria-hidden="true"></span></span>
     </div>`).join('') +
    `<div class="wiz-check-final" hidden></div>`;

  const rows = [...box.querySelectorAll('.wiz-check')];
  rows.forEach((row, i) => {
    setTimeout(() => {
      if (!row.isConnected || setup.step !== 5) return;      // navigated away / re-rendered
      const ok = row.dataset.ok === '1';
      row.classList.remove('loading');
      row.classList.add(ok ? 'done' : 'todo');
      row.querySelector('.wiz-check-mark').innerHTML = ok ? '✓' : '✗';
      if (i === rows.length - 1) {                            // last one resolved
        const final = box.querySelector('.wiz-check-final');
        if (final) {
          final.hidden = false;
          final.textContent = allOk ? 'All steps complete — ready to start.'
                                     : 'Complete the steps above to start.';
        }
        if (start) { start.disabled = !allOk; start.title = allOk ? '' : 'Complete the checklist to start the campaign'; }
      }
    }, 400 + i * 350);                                        // staggered reveal
  });
}
$('#btn-setup-start')?.addEventListener('click', () => {
  renderSetupSummary();
  const ids = loggedInAccountIds();
  if (!ids.length) { toast('Log at least one account in (step 1) first.', true); return; }
  if (!setup.usernames.length) { toast('Add users (step 2) first.', true); return; }
  const mode = inferSetupMode();          // both if both filled, else whichever one
  const skipMedia = $('#setup-skip-media')?.checked;
  const media = skipMedia ? [] : (setup.media || []);
  const pairs = setupMessagePairs();      // the ticked messages (+ any written), with their tab names
  const smsPool = pairs.length ? pairs.map(p => p.text) : ['Hello'];
  const smsLabels = pairs.length ? pairs.map(p => p.label) : ['sms1'];

  // The wizard is now the Campaign panel itself — no close/switch. Hand the run to
  // the SAME shared runner the panel uses, so it opens the live Campaign view with
  // tabs, coloured batches and interleaved Actions instead of just a note.
  toast('Campaign started');
  const note = $('#campaign-note');
  if (note) { note.className = 'banner good';
    note.textContent = `▶ Campaign starting on ${ids.length} account(s), ${setup.usernames.length} user(s) (from Setup Campaign)…`; }

  // runCampaign sets `campaignRun` synchronously (before its first await), so the
  // view has data the moment we open it; the returned promise runs the poll loop.
  const running = runCampaign({
    ids, usernames: setup.usernames.slice(), smsPool, smsLabels, mode,
    mediaStored: media, mediaNames: media, out: null,
  });
  openCampaignView();                     // the batches + Actions run view, open now
  running.catch(() => {});                // long-running; errors surface in the note
});

/* Mount the Setup Campaign wizard inline in the Campaign panel on load. */
try { mountSetupInline(); openCampaignSetup(); } catch {}

/* One chip's <li>, numbered. Shared by the compact list and the full-preview
   modal. `i` is the 0-based position; the shown number is 1-based. */
function campaignChipHTML(entry, i, extraCls = '') {
  const hit = entry.result;
  const name = hit ? (hit.username || hit.name || entry.query) : entry.query;
  const done = isDone(entry.query);
  const miss = !entry.pending && !entry.opened;
  const title = done
    ? `${entry.query}: searched, "Hello" written — won't re-send until "Repeat sending"`
    : entry.pending
      ? `${entry.query} — copy`
      : (entry.opened
          ? `${hit.name}${hit.verified ? ' (verified)' : ''} — copy ${name}`
          : `${entry.query}: ${entry.error || 'not opened'}`);
  // the ✓ itself is CSS (.name-chip-done::after) — see style.css
  const cls = `name-chip${miss ? ' name-chip-miss' : ''}${done ? ' name-chip-done' : ''}`;
  // Assignment badge: which account, sms and/or image this name was given.
  // Always emitted — empty when the name has no assignment yet. The queue lays
  // its rows out on one shared grid, so every row needs the same cell count or
  // an untagged name would slide into the tag's column. Empty renders invisible.
  let label = '';
  if (entry.an != null) {
    const bits = [`Acc ${entry.an}`];
    if (entry.sn != null) bits.push(entry.sl || `sms ${entry.sn}`);
    if (entry.img != null) bits.push(`img ${entry.img}`);
    label = bits.join(' - ');
  }
  const tag = `<span class="assign-tag" title="${esc(label)}">${esc(label)}</span>`;
  // Tag leads the name: the assignment is what you scan the list for before
  // starting a run, so it reads "Acc 1 - sms 2 - img 5  @username".
  return `<li class="name-li ${extraCls}"><span class="name-num">${i + 1}</span>${tag}` +
         `<button class="${cls}" data-copy-target="${esc(name)}" title="${esc(title)}">${esc(name)}</button></li>`;
}

/* Copy-on-click for every name chip inside `root`. */
function wireCopyChips(root) {
  $$(`${root} [data-copy-target]`).forEach(button => {
    button.addEventListener('click', async () => {
      const name = button.dataset.copyTarget || '';
      if (!name) return;
      try { await navigator.clipboard.writeText(name); toast(`Copied ${name}`); }
      catch { toast('Could not copy the name.', true); }
    });
  });
}

function renderCampaignTargets(entries) {
  campaignEntries = entries;                 // track even when the list isn't shown
  syncCampaignViewBtn();                      // keep the 📊 Campaign view button in sync
  const list = $('#campaign-targets');
  if (!list) return;                          // Usernames list was removed from the panel
  const pending = entries.some(entry => entry.pending);
  const opened = entries.filter(entry => entry.opened).length;
  const ticked = entries.filter(entry => isDone(entry.query)).length;
  const rangeNote = campaignRange ? ` · range ${campaignRange.from}–${campaignRange.to}` : '';
  const heading = pending
    ? `${entries.length} name${entries.length === 1 ? '' : 's'}${campaignRange ? '' : ' from Data'}${rangeNote}` +
      (ticked ? ` · ✓ ${ticked} done, ${entries.length - ticked} left` : '')
    : `Opened ${opened} of ${entries.length}` + (ticked ? ` · ✓ ${ticked} done` : '');

  // "Repeat sending" appears only once something is ticked — it clears the ✓
  // marks so those profiles get their message again on the next run.
  const repeatBtn = ticked
    ? `<button type="button" class="btn btn-sm" id="btn-campaign-repeat"
              title="Clear the ✓ marks so every name can be sent again">🔁 Repeat sending</button>`
    : '';
  // Full preview always opens the WHOLE list (with the range picker), even when
  // the queue above shows only a selected slice — so its count is the total.
  const previewBtn =
    `<button type="button" class="btn btn-sm" id="btn-campaign-preview">Full preview (${campaignNames.length})</button>`;

  // Heading on its own line; the action buttons sit parallel beneath it.
  list.innerHTML = entries.length
    ? `<div class="name-count"><span class="nc-heading">${heading}</span>` +
      `<div class="nc-actions">${repeatBtn}${previewBtn}</div></div>` +
      // Wrap the callback so map's 3rd arg (the whole array) can't land in
      // campaignChipHTML's extraCls — that made each chip embed a stringified
      // copy of every entry, an O(n²) blow-up that overflowed the max string
      // length past ~5600 names.
      '<ol class="name-list name-list--scroll">' + entries.map((e, i) => campaignChipHTML(e, i)).join('') + '</ol>'
    : '<p class="hint">No saved Instagram names in Data — run Visit insta to collect some.</p>';

  wireCopyChips('#campaign-targets');
  $('#btn-campaign-preview')?.addEventListener('click', openCampaignPreview);
  $('#btn-campaign-repeat')?.addEventListener('click', () => {
    clearCampaignDone();
    renderCampaignTargets(campaignEntries);      // re-render: marks gone, all runnable
    const note = $('#campaign-note');
    if (note) { note.className = 'banner'; note.textContent = 'Cleared ✓ marks — press Start Campaign to send Hello again.'; }
    toast('Repeat: every name can be sent again');
  });
}

/* Full-preview modal: every target, scrollable, plus a range picker to run only
   a slice of the list (e.g. profiles 1–500). Out-of-range rows are dimmed. */
function renderCampaignPreview() {
  const body = $('#campaign-preview-body');
  const title = $('#campaign-preview-title');
  const total = campaignNames.length;                // the modal always shows ALL names
  const from = campaignRange ? campaignRange.from : 1;
  const to = campaignRange ? campaignRange.to : total;
  if (title) title.textContent = `All targets (${total})`;
  if (body) {
    body.innerHTML = total
      ? '<ol class="name-list">' + campaignNames.map((name, i) =>
          campaignChipHTML({ query: name, pending: true }, i,
            (i + 1 < from || i + 1 > to) ? 'out-of-range' : '')).join('') + '</ol>'
      : '<p class="hint">No names loaded.</p>';
    wireCopyChips('#campaign-preview-body');
  }
  const count = $('#range-count');
  if (count) {
    const n = Math.max(0, Math.min(to, total) - Math.max(from, 1) + 1);
    count.textContent = campaignRange
      ? `${n} of ${total} selected (profiles ${from}–${to})`
      : `All ${total} selected`;
  }
}

function openCampaignPreview() {
  const total = campaignNames.length;
  const fromInput = $('#range-from');
  const toInput = $('#range-to');
  if (fromInput) { fromInput.max = total || 1; fromInput.value = campaignRange ? campaignRange.from : 1; }
  if (toInput) { toInput.max = total || 1; toInput.value = campaignRange ? campaignRange.to : total; }
  renderCampaignPreview();
  $('#campaign-preview')?.showModal();
}

$('#btn-range-apply')?.addEventListener('click', () => {
  const total = campaignNames.length;
  if (!total) return;
  let from = Math.round(Number($('#range-from')?.value) || 1);
  let to = Math.round(Number($('#range-to')?.value) || total);
  from = Math.max(1, Math.min(from, total));
  to = Math.max(from, Math.min(to, total));          // to >= from, both within bounds
  $('#range-from').value = from;
  $('#range-to').value = to;
  campaignRange = (from === 1 && to === total) ? null : { from, to };
  showCampaignQueue();                               // push the selected slice to the Usernames list
  reflectCampaignRange();
  toast(campaignRange ? `${campaignPool().length} profile(s) sent to the list (${from}–${to})`
                      : 'All profiles sent to the list');
  $('#campaign-preview')?.close();                   // committed — show the updated list
});

$('#btn-range-clear')?.addEventListener('click', () => {
  campaignRange = null;
  const total = campaignNames.length;
  if ($('#range-from')) $('#range-from').value = 1;
  if ($('#range-to')) $('#range-to').value = total;
  renderCampaignPreview();
  showCampaignQueue();
  reflectCampaignRange();
  toast('Selected all profiles');
});

/* Mirror the chosen range into the panel's banner so it's visible after the
   modal closes — the next Start Campaign will honour it. */
function reflectCampaignRange() {
  const note = $('#campaign-note');
  if (!note) return;
  const pool = campaignPool().filter(name => !isDone(name));
  note.className = 'banner banner-slim';
  note.textContent = campaignRange
    ? `Range profiles ${campaignRange.from}–${campaignRange.to}: ${pool.length} name(s) will run.`
    : `All ${campaignNames.length} profile(s) selected — ${pool.length} will run.`;
}

$('#btn-campaign-preview-close')?.addEventListener('click', () => $('#campaign-preview')?.close());
// Click the backdrop (not a child) to close — for both campaign dialogs.
['#campaign-preview', '#campaign-view'].forEach(sel => {
  const dialog = $(sel);
  if (!dialog) return;
  let downOnBackdrop = false;
  dialog.addEventListener('mousedown', e => { downOnBackdrop = e.target === dialog; });
  dialog.addEventListener('click', e => { if (e.target === dialog && downOnBackdrop) dialog.close(); });
});

/* Split a list into `parts` contiguous chunks, the front chunks taking the
   remainder when it doesn't divide evenly (500 / 3 -> 167, 167, 166). Contiguous
   (not round-robin) so each account works a clean run of the Data order. */
function splitContiguous(items, parts) {
  const out = [];
  const base = Math.floor(items.length / parts);
  let rem = items.length % parts;
  let idx = 0;
  for (let p = 0; p < parts; p++) {
    const size = base + (rem > 0 ? 1 : 0);
    if (rem > 0) rem--;
    out.push(items.slice(idx, idx + size));
    idx += size;
  }
  return out;
}

/* Shared campaign runner — builds the per-name assignment, publishes the live run
   to the Campaign view (tabs · coloured batches · interleaved Actions), fires each
   account and polls its progress. Used by BOTH the Campaign panel's Start Campaign
   and the Setup Campaign wizard's Start, so both open the exact same live view. */
async function runCampaign({ ids, usernames, smsPool, smsLabels = [], mode, mediaStored = [], mediaNames = [], out = null, skipping = 0 }) {
  // Whether each name gets the shared media pool, and whether it gets text.
  const useMedia = mode === 'media' || mode === 'both';
  const useText  = mode === 'text'  || mode === 'both';

  // Assign every name a random account, a random SMS (text/both) and ONE random
  // image (media/both) — the same way a draft is picked, so a name gets one
  // picture rather than the whole pool. The backend takes these per-name images
  // as `medias`, parallel to the usernames.
  const assign = usernames.map(name => {
    const ai = Math.floor(Math.random() * ids.length);
    const a = { name, accountId: ids[ai], accountNum: ai + 1 };
    if (useText) {
      const si = Math.floor(Math.random() * smsPool.length);
      a.smsNum = si + 1;
      a.smsLabel = smsLabels[si] || `sms${si + 1}`;   // the draft's tab name (rename or smsN)
      a.message = smsPool[si];
    } else {
      a.message = '';                       // media mode delivers no text
    }
    if (useMedia) {
      const mi = Math.floor(Math.random() * mediaStored.length);
      a.imgNum = mi + 1;                    // 1-based, matches the preview order
      a.media = mediaStored[mi];
    }
    return a;
  });
  const assignByName = new Map(assign.map(a => [a.name, a]));

  // Group the assignments by account: each account gets its own names and the
  // matching per-name messages. Media is the same shared pool for everyone.
  const runs = {};
  ids.forEach((id, i) => {
    runs[id] = {
      who: accountById(id)?.account_name || `#${id}`, accountNum: i + 1,
      queue: [], msgs: [], medias: [], results: [], done: 0, total: 0,
      written: 0, current: null, error: null, finished: false,
    };
  });
  assign.forEach(a => {
    runs[a.accountId].queue.push(a.name);
    runs[a.accountId].msgs.push(a.message);
    // The name's own image. Pushing null here was what made every DM get the
    // whole shared pool — null means "fall back to `media`" on the backend.
    if (useMedia) runs[a.accountId].medias.push(a.media || null);
  });
  ids.forEach(id => { runs[id].total = runs[id].queue.length; });

  const active = ids.filter(id => runs[id].total > 0);
  active.forEach(id => $(`#campaign-accounts .acct-chip[data-acct="${id}"]`)?.classList.add('running'));

  // Publish this run to the live "Campaign view" monitor: keep a reference to the
  // same `runs` the poller mutates, plus the original image names for the table.
  campaignRun = { runs, assign, assignByName, mode, mediaNames, running: true };
  renderCampaignTargets(campaignEntries);   // repaint the list so 📊 Campaign view enables

  // One assignment line per name: account, plus sms#/img# for whatever the mode
  // delivers.
  const assignLine = a => `@${a.name} → account ${a.accountNum}` +
    (a.smsNum ? ` · ${a.smsLabel || `sms${a.smsNum}`}` : '') +
    (a.imgNum ? ` · img${a.imgNum}` : '');
  const poolNote = [
    useText  ? `${smsPool.length} message(s)` : null,
    useMedia ? `${mediaStored.length} image(s)` : null,
  ].filter(Boolean).join(' + ') || 'no content';

  if (out) show(out, `Assigned ${usernames.length} name(s) to ${active.length} account(s) and ` +
            `${poolNote} (${mode} mode)` +
            (skipping ? `, skipping ${skipping} already ticked` : '') + '. Sending…\n\n' +
            assign.slice(0, 15).map(assignLine).join('\n') +
            (assign.length > 15 ? `\n…and ${assign.length - 15} more` : ''));

  // Merge every account's live results into the ONE queue list, tagging each
  // name with the account+sms it was assigned. Each name belongs to exactly one
  // account, so concatenating can't collide.
  const paint = () => {
    const all = active.flatMap(id => runs[id].results);
    all.forEach(entry => {
      if (entry.opened && entry.message?.written) markDone(entry.query);
    });
    renderCampaignTargets(campaignPool().map(name => {
      const hit = all.find(entry => entry.query === name);
      const a = assignByName.get(name);
      const base = hit || { query: name, pending: true };
      return a ? { ...base, an: a.accountNum, sn: a.smsNum, sl: a.smsLabel, img: a.imgNum } : base;
    }));
    if ($('#campaign-view')?.open) renderCampaignViewTable();
  };

  // The POSTs don't return for minutes, so poll each account's progress and
  // paint each name the moment its "Hello" lands.
  const note = $('#campaign-note');
  const poll = setInterval(async () => {
    await Promise.all(active.map(async id => {
      if (runs[id].finished) return;
      try {
        const p = await api(`/automation/campaign-progress?account_id=${id}`);
        runs[id].results = p.results || [];
        runs[id].done = p.done ?? runs[id].done;
        runs[id].written = p.written ?? runs[id].written;
        runs[id].current = p.current;
        // Track the batch split on EVERY poll, not just while waiting — the
        // table's Batch column needs it from the first row, and the first wait
        // only happens after a whole batch has already run.
        if (p.batches != null) runs[id].batches = p.batches;
        if (p.batch_no != null) runs[id].batchNo = p.batch_no;
        if (p.batch_map) runs[id].batchMap = p.batch_map;   // name -> batch #, known at start
        if (p.actions) runs[id].actions = p.actions;        // random Actions run between rows
        // Batching countdown: stamp a client-side deadline so it ticks smoothly
        // between polls, re-synced each poll.
        if (p.waiting && p.next_batch_in > 0) {
          runs[id].waiting = true;
          runs[id].batchDeadline = Date.now() + p.next_batch_in * 1000;
        } else {
          runs[id].waiting = false;
          runs[id].batchDeadline = null;
        }
      } catch { /* a dropped poll is harmless — the next one catches up */ }
    }));
    paint();
    if (note) {
      const done = active.reduce((n, id) => n + runs[id].done, 0);
      const written = active.reduce((n, id) => n + runs[id].written, 0);
      const onNow = active.filter(id => runs[id].current).map(id => `${runs[id].who}→@${runs[id].current}`);
      note.className = 'banner';
      note.textContent = `${done} of ${usernames.length} searched, ✓ ${written} written` +
                         (onNow.length ? ` — ${onNow.join(', ')}` : '') + '…';
    }
  }, CAMPAIGN_POLL_MS);

  // Fire every account at once with its names + per-name messages; one failing
  // must not sink the others.
  const settled = await Promise.all(active.map(id =>
    api('/automation/search-campaign',
        { method: 'POST', body: { usernames: runs[id].queue, account_id: id, messages: runs[id].msgs, mode, media: mediaStored, medias: runs[id].medias } })
      .then(data => ({ id, ok: true, data }))
      .catch(err => ({ id, ok: false, err }))
  ));
  clearInterval(poll);

  let totalOpened = 0, totalWritten = 0, totalSent = 0, totalCount = 0, failures = 0;
  let anySend = false;
  const lines = [];
  for (const res of settled) {
    const id = res.id;
    const chip = $(`#campaign-accounts .acct-chip[data-acct="${id}"]`);
    runs[id].finished = true;
    if (res.ok) {
      const data = res.data;
      runs[id].results = data.results || [];
      totalOpened += data.opened || 0;
      totalWritten += data.written || 0;
      totalSent += data.sent || 0;
      totalCount += data.count || 0;
      if (data.send) anySend = true;
      chip?.classList.remove('running'); chip?.classList.add('done');
      lines.push(`${runs[id].who} (acct ${runs[id].accountNum}, ${data.count} name(s)): ` +
                 `opened ${data.opened}, composed ${data.written}` +
                 (data.send ? `, sent ${data.sent}` : ' (staged, not sent)') +
                 (data.missed ? `, ${data.missed} missed` : '') + '.');
    } else {
      failures++;
      runs[id].error = (res.err && res.err.message) || 'failed';
      chip?.classList.remove('running'); chip?.classList.add('failed');
      lines.push(`${runs[id].who}: ${runs[id].error}`);
    }
  }
  if (campaignRun) campaignRun.running = false;   // monitor now reads "finished"
  paint();
  if ($('#campaign-view')?.open) renderCampaignViewTable();

  // Enter is the master send-switch (Config → Press ENTER). Report honestly:
  // "sent" when it was on, "staged, nothing sent" when it was off.
  const sendTail = anySend
    ? `sent ${totalSent}.`
    : 'nothing sent (Press ENTER is off — messages left in the composer).';

  if (note) {
    const left = campaignNames.filter(name => !isDone(name)).length;
    note.className = failures ? 'banner' : 'banner good';
    note.textContent =
      `${active.length} account(s), ${mode} mode: opened ${totalOpened} of ${totalCount}, ` +
      `composed ${totalWritten}, ${sendTail} ` +
      (skipping ? `Skipped ${skipping} already ticked. ` : '') +
      (failures ? `${failures} account(s) failed. ` : '') +
      `✓ ${campaignDone.size} ticked, ${left} left — 🔁 Repeat sending resets them.`;
  }

  if (out) show(out, `Campaign across ${active.length} account(s) in ${mode} mode, random account+sms per name.\n\n` +
            lines.join('\n') +
            `\n\nTotal: opened ${totalOpened} of ${totalCount}, composed ${totalWritten}, ${sendTail}`);
  toast(`Campaign: opened ${totalOpened}, ${anySend ? `sent ${totalSent}` : `staged ${totalWritten}`}`);
  return { active: active.length, totalOpened, totalWritten, totalSent, anySend, failures };
}

bind('#btn-campaign', '#out-campaign', async out => {
  // Give every username a RANDOM account and a RANDOM SMS drawn from all ticked
  // accounts and all existing drafts, then group by account and send — each
  // account writes each of its names that name's own assigned draft. Every
  // account runs at once. Nothing is sent: the message is left in the composer.
  if (!campaignNames.length) {
    throw new Error('No names loaded from Data — run Visit insta first, then Reload from Data.');
  }
  const ids = [...campaignPicked];
  if (!ids.length) throw new Error('Tick at least one open-window account to run the campaign.');

  // The SMS pool: every non-empty draft (with its tab name), or "Hello" when empty.
  syncActiveDraft();
  const pairs = campaignSms
    .map((s, i) => ({ text: s.text.trim(), label: smsLabel(i) }))
    .filter(p => p.text);
  const smsPool = pairs.length ? pairs.map(p => p.text) : ['Hello'];
  const smsLabels = pairs.length ? pairs.map(p => p.label) : ['sms1'];

  // Mode decides what gets delivered. media/both need at least one image; they
  // are already in the store, so the run just reuses their names.
  const mode = campaignMode;
  let mediaStored = [];
  if (mode === 'media' || mode === 'both') {
    const images = campaignMedia.filter(isImageFile);
    if (!images.length) {
      throw new Error(`"${mode}" mode needs at least one JPG/JPEG/PNG image — press ⬆️ Upload.`);
    }
    mediaStored = uploadCampaignMedia();
    if (!mediaStored.length) throw new Error('No stored images to send — press ⬆️ Upload.');
  }

  // Only the chosen range of profiles (Full preview → range picker), then drop
  // ticked names — those already have their message and would just re-open.
  const pool = campaignPool();
  const usernames = pool.filter(name => !isDone(name));
  const skipping = pool.length - usernames.length;
  if (!usernames.length) {
    const scope = campaignRange ? ` in range ${campaignRange.from}–${campaignRange.to}` : '';
    throw new Error(
      `All ${pool.length} name(s)${scope} are ticked — already sent. ` +
      'Press 🔁 Repeat sending to run them again.'
    );
  }

  // Hand off to the shared runner (assign → live view → poll → report).
  const mediaNames = campaignMedia.filter(isImageFile).map(f => f.name);
  await runCampaign({ ids, usernames, smsPool, smsLabels, mode, mediaStored, mediaNames, out, skipping });
});

/* ── Scrapping settings (⚙️ pop-up) ──────────────────────────── */
/* Duration and scroll pace, saved server-side with everything else. The panel
   holds no copy of its own: the dialog reads /settings when it opens and the
   scrape reads them again when it runs, so there's one source of truth. */
const SET_FIELDS = {
  scroll_pause_min_ms: '#set-pause-min',
  scroll_pause_max_ms: '#set-pause-max',
  scroll_step_min_pct: '#set-step-min',
  scroll_step_max_pct: '#set-step-max',
  search_rounds: '#set-search-rounds',
  scrape_batch_min: '#set-batch-min',
  scrape_batch_max: '#set-batch-max',
  scrape_batch_delay_min: '#set-batch-delay-min',
  scrape_batch_delay_max: '#set-batch-delay-max'
};
/* Mirrors the scrape keys in backend/settings.py — used only by Reset defaults.
   scroll_duration_s is intentionally omitted: scraping now stops by count. */
const SCRAP_DEFAULTS = {
  scroll_pause_min_ms: 2800, scroll_pause_max_ms: 4200,
  scroll_step_min_pct: 45, scroll_step_max_pct: 75,
  search_rounds: 100,
  scrape_batch_min: 10, scrape_batch_max: 20,
  scrape_batch_delay_min: 3, scrape_batch_delay_max: 8
};
let scrapSettings = null;

function writeScrapSettings(s) {
  scrapSettings = s;
  for (const [key, sel] of Object.entries(SET_FIELDS)) {
    if ($(sel) && s[key] !== undefined) $(sel).value = s[key];
  }
  const summary = $('#scrap-summary');
  if (summary) {
    summary.textContent =
      `pause ${s.scroll_pause_min_ms}-${s.scroll_pause_max_ms}ms · ` +
      `step ${s.scroll_step_min_pct}-${s.scroll_step_max_pct}% · ` +
      `batch ${s.scrape_batch_min ?? 10}-${s.scrape_batch_max ?? 20}` +
      `, pause ${s.scrape_batch_delay_min ?? 3}-${s.scrape_batch_delay_max ?? 8}s`;
  }
}

async function loadScrapSettings() {
  try {
    const { settings } = await api('/settings');
    writeScrapSettings(settings);
    return settings;
  } catch (error) {
    const note = $('#set-note');
    if (note) note.textContent = `Could not load settings: ${error.message}`;
    return null;
  }
}

$('#btn-scrap-settings')?.addEventListener('click', async () => {
  const note = $('#set-note');
  if (note) note.textContent = '';
  await loadScrapSettings();          // fresh every open — never a stale form
  $('#scrap-settings')?.showModal();
});

/* Click the dark backdrop to close. A native dialog treats a backdrop click as
   a click on the dialog element itself, so target === dialog means "outside the
   box". The box (.modal-box) fills the dialog, so clicks on it don't match.
   mousedown is checked too: a drag that starts inside and releases on the
   backdrop (selecting text) must NOT close. */
(() => {
  const dialog = $('#scrap-settings');
  if (!dialog) return;
  let downOnBackdrop = false;
  dialog.addEventListener('mousedown', e => { downOnBackdrop = e.target === dialog; });
  dialog.addEventListener('click', e => {
    if (e.target === dialog && downOnBackdrop) dialog.close();
  });
})();

$('#btn-set-save')?.addEventListener('click', async event => {
  event.preventDefault();             // don't let the form close before the PUT
  const body = {};
  for (const [key, sel] of Object.entries(SET_FIELDS)) body[key] = Number($(sel).value);
  const note = $('#set-note');
  try {
    const { settings } = await api('/settings', { method: 'PUT', body });
    // Echo back what was STORED, not what was typed: the server clamps and
    // swaps reversed ranges, so the form must show the values that will be used.
    writeScrapSettings(settings);
    toast('Scrapping settings saved');
    $('#scrap-settings')?.close();
  } catch (error) {
    if (note) note.textContent = error.message;
    toast(error.message, true);
  }
});

$('#btn-set-reset')?.addEventListener('click', async () => {
  const note = $('#set-note');
  try {
    // Only the scroll keys — /settings/reset would take typing back too.
    const { settings } = await api('/settings', { method: 'PUT', body: SCRAP_DEFAULTS });
    writeScrapSettings(settings);
    if (note) note.textContent = 'Back to defaults — Save to keep.';
  } catch (error) {
    if (note) note.textContent = error.message;
  }
});

/* ── Scrapping ───────────────────────────────────────────────── */
/* Scrolls the home feed of one or more logged-in windows and records the
   usernames each sees. Several accounts run AT ONCE — the backend keys every
   window and its progress by account, so the scrapes don't blur together. The
   POSTs don't return for a minute or two, so a poller reads each run's live
   progress alongside them and lists names per account as they land. */
const SCRAP_POLL_MS = 1200;

/* Which open-window accounts are ticked. A Set so a refresh can preserve it. */
const scrapPicked = new Set();

/* Per-account collection technique: 'scroll' | 'search' | 'both'. Default 'both'
   so an account collects by scrolling AND searching unless narrowed. */
const scrapTech = new Map();
const techOf = id => scrapTech.get(Number(id)) || 'both';

/* Scrapping prefs persisted in the store file (🎯 target + per-account technique),
   so a save sticks across restart/refresh — loaded once, saved on every change. */
let scrapPrefs = null;
/* 🎯 0 means unlimited — say so next to the box instead of leaving a bare 0. */
function renderScrapTargetHint() {
  const hint = $('#scrap-target-hint');
  if (!hint) return;
  const t = Math.max(0, Number($('#scrap-target')?.value) || 0);
  hint.textContent = t ? '/account' : 'unlimited';
}
async function loadScrapPrefs() {
  if (scrapPrefs) return scrapPrefs;
  try {
    scrapPrefs = await api('/automation/scrape-prefs');
    const t = $('#scrap-target');
    // `?? 0`, not `if (target)` — a saved 0 IS a real value (unlimited) and must
    // not fall through to the input's default.
    if (t) t.value = scrapPrefs.target ?? 0;
    for (const [k, v] of Object.entries(scrapPrefs.techniques || {})) scrapTech.set(Number(k), v);
  } catch { scrapPrefs = { target: 0, techniques: {} }; }
  renderScrapTargetHint();
  return scrapPrefs;
}
async function saveScrapPrefs(changes) {
  try { scrapPrefs = await api('/automation/scrape-prefs', { method: 'PUT', body: changes }); }
  catch { /* non-fatal — a failed save just isn't remembered */ }
}

function loadScrapAccounts() {
  const box = $('#scrap-accounts');
  const note = $('#scrap-note');
  if (!box) return Promise.resolve();
  loadScrapSettings();               // keeps the ⚙️ summary line honest
  // Load saved prefs first so each card shows its remembered technique + 🎯 target.
  return loadScrapPrefs().then(() => api('/accounts')).then(({ accounts }) => {
    accountCache = accounts;
    // Only accounts whose Chrome window is open right now — a scrape scrolls an
    // on-screen window, so a closed one has nothing to drive.
    const open = accounts.filter(isOpen);
    // drop ticks + techniques for windows that have since closed
    [...scrapPicked].forEach(id => open.some(a => a.id === id) || scrapPicked.delete(id));
    [...scrapTech.keys()].forEach(id => open.some(a => a.id === id) || scrapTech.delete(id));

    box.innerHTML = open.length
      ? open.map(a => {
          const on = scrapPicked.has(a.id);
          const t = techOf(a.id);
          const opt = (val, label) =>
            `<button type="button" class="tech-opt ${t === val ? 'on' : ''}"
                     data-acct="${a.id}" data-tech="${val}" aria-pressed="${t === val}"
                     title="${label} for this account">${label}</button>`;
          return `<div class="scrap-acct-pick ${on ? 'on' : ''}" data-acct="${a.id}">
                    <button type="button" class="acct-chip ${on ? 'on' : ''}"
                            data-acct="${a.id}" aria-pressed="${on}">${esc(accountName(a))}</button>
                    <div class="tech-seg" role="group" aria-label="Collection technique for ${esc(accountName(a))}">
                      ${opt('scroll', 'Scroll')}${opt('search', 'Search')}${opt('both', 'Both')}
                    </div>
                  </div>`;
        }).join('')
      : '<span class="hint">No open windows. Open accounts first (Accounts → Check Login Status).</span>';

    // Name = include/exclude toggle.
    box.querySelectorAll('.acct-chip').forEach(chip => {
      chip.addEventListener('click', () => {
        const id = Number(chip.dataset.acct);
        const on = !scrapPicked.has(id);
        if (on) scrapPicked.add(id); else scrapPicked.delete(id);
        chip.classList.toggle('on', on);
        chip.setAttribute('aria-pressed', String(on));
        chip.closest('.scrap-acct-pick')?.classList.toggle('on', on);
      });
    });
    // Technique segment = pick one of Scroll / Search / Both for that account.
    box.querySelectorAll('.tech-opt').forEach(opt => {
      opt.addEventListener('click', () => {
        const id = Number(opt.dataset.acct);
        scrapTech.set(id, opt.dataset.tech);
        saveScrapPrefs({ techniques: { [id]: opt.dataset.tech } });   // remembered permanently
        opt.parentElement.querySelectorAll('.tech-opt').forEach(o => {
          const sel = o === opt;
          o.classList.toggle('on', sel);
          o.setAttribute('aria-pressed', String(sel));
        });
      });
    });

    if (note) {
      note.className = open.length ? 'banner good' : 'banner';
      note.textContent = open.length
        ? `${open.length} open window(s). Tick the ones to scrape and pick each one's technique — they run together.`
        : 'No open windows. Open an account first (Accounts → Check Login Status).';
    }
  }).catch(error => {
    if (note) { note.className = 'banner bad'; note.textContent = `Could not load accounts: ${error.message}`; }
  });
}

$('#btn-scrap-all')?.addEventListener('click', () => {
  const chips = $$('#scrap-accounts .acct-chip');
  const turnOn = chips.some(chip => !chip.classList.contains('on'));  // any off -> all; else clear
  chips.forEach(chip => {
    const id = Number(chip.dataset.acct);
    if (turnOn) scrapPicked.add(id); else scrapPicked.delete(id);
    chip.classList.toggle('on', turnOn);
    chip.setAttribute('aria-pressed', String(turnOn));
    chip.closest('.scrap-acct-pick')?.classList.toggle('on', turnOn);
  });
});

/* 🎯 target is remembered permanently — persisted the moment it changes.
   0 = unlimited, so the floor is 0 and there is no ceiling. */
$('#scrap-target')?.addEventListener('change', () => {
  const t = Math.max(0, Math.floor(Number($('#scrap-target').value) || 0));
  $('#scrap-target').value = t;
  renderScrapTargetHint();
  saveScrapPrefs({ target: t });
});

/* Merge a poll's [{username, bio}] into an account's username→bio map. The bio
   scrape fills these in as it opens each profile, so a name can show the moment
   it's found and its bio the moment it's read. */
function mergeBios(map, profiles) {
  (profiles || []).forEach(p => {
    const n = p && (p.username || p.title);
    if (n && p.bio) map[n.toLowerCase()] = p.bio;
  });
}

/* One live-list row: the copyable @name, with its bio underneath on a single
   gray line once the bio scrape has read it. Newlines and runs of whitespace are
   collapsed so a multi-line bio stays on the one line; the full text is the
   title, so hovering shows a bio too long to fit. */
function nameRowHTML(n, bioMap) {
  const bio = bioMap && bioMap[n.toLowerCase()];
  const oneLine = bio ? String(bio).replace(/\s+/g, ' ').trim() : '';
  return `<li><button class="name-chip" data-copy-target="${esc(n)}" title="Copy ${esc(n)}">${esc(n)}</button>` +
         (oneLine ? `<div class="name-bio" title="${esc(oneLine)}">${esc(oneLine)}</div>` : '') +
         '</li>';
}

/* Copy-on-click for any name chip inside the live list. */
function wireScrapCopy() {
  $$('#scrap-list [data-copy-target]').forEach(button => {
    button.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(button.dataset.copyTarget || '');
        toast(`Copied ${button.dataset.copyTarget}`);
      } catch { toast('Could not copy the name.', true); }
    });
  });
}

/* One column per account in the live list. `runs` is a map id -> {who, tech,
   phase, subTech, batchTarget, pauseLeft, target, names, error}. Each card shows
   the account's technique tag + a live line with the current type, batch size,
   pause countdown, and count/target. Rebuilt each poll — cheap for a handful. */
const TECH_LABEL = { scroll: 'Scroll', search: 'Search', both: 'Both' };

function renderScrapRuns(runs) {
  const list = $('#scrap-list');
  if (!list) return;
  const ids = Object.keys(runs);
  if (!ids.length) { list.innerHTML = '<p class="hint">Usernames will appear here as they are found.</p>'; return; }

  list.innerHTML = ids.map(id => {
    const r = runs[id];
    const found = r.names.length;
    const tgt = r.target ? `${found}/${r.target}` : `${found}`;
    let state, empty;
    if (r.error)                 { state = `failed — ${esc(r.error)}`; empty = '—'; }
    else if (r.phase === 'done') { state = `${tgt} found`; empty = 'No usernames were captured.'; }
    else if (r.subTech === 'pause') {
      const p = Number.isFinite(r.pauseLeft) ? Math.max(0, Math.round(r.pauseLeft)) : null;
      state = `⏸ paused${p != null ? ` · ${p}s` : ''}… · ${tgt}`;
      empty = 'Paused…';
    }
    else {
      const sub = r.subTech === 'search' ? 'searching' : 'scrolling';
      const bt = r.batchTarget ? ` · batch ${r.batchTarget}` : '';
      state = `${sub}${bt}… · ${tgt}`;
      empty = 'Collecting…';
    }
    const tag = TECH_LABEL[r.tech] || r.tech;
    const chips = found
      ? '<ol class="name-list name-list--scroll">' +
          r.names.map(n => nameRowHTML(n, r.bioMap)).join('') + '</ol>'
      : `<p class="hint">${empty}</p>`;
    return `<div class="scrap-acct" data-acct="${id}">
              <div class="scrap-acct-head">${esc(r.who)} <span class="tech-tag">${tag}</span>
                <span class="sub">· ${state}</span></div>
              ${chips}
            </div>`;
  }).join('');
  wireScrapCopy();
}

/* Search-Scrapping: on each ticked window, click the sidebar Search and run 5
   rounds of a fresh 2-random-letter query, collecting the accounts each round
   suggests. Every account runs on its OWN window with its OWN random letters, so
   several run at once without sharing a query — and each window's block fills in
   iteration by iteration as its rounds land. Read only: nothing clicked or sent.
   Found names go to Data, the same as the scroll scrape. */
bind('#btn-search-scrap', '#out-scrap', async out => {
  const ids = [...scrapPicked];
  if (!ids.length) throw new Error('Tick at least one open-window account first.');

  // The configured round count (⚙️ → Search rounds, 100 by default). Just for
  // the display — the backend reads the same setting when the run has no rounds.
  const cfg = scrapSettings || await loadScrapSettings();
  const rounds = Number(cfg?.search_rounds) || 100;

  const note = $('#scrap-note');
  if (note) {
    note.className = 'banner';
    note.textContent = `Search-Scrapping ${ids.length} window(s)…`;
  }
  show(out, `Searching on ${ids.length} window(s), ${rounds} rounds each…`);

  const runs = {};
  ids.forEach(id => {
    runs[id] = { who: accountById(id)?.account_name || `#${id}`, names: [],
                 seeds: [], round: 0, rounds, saved: 0, bios: 0, bioMap: {},
                 done: false, error: null };
    $(`#scrap-accounts .acct-chip[data-acct="${id}"]`)?.classList.add('running');
  });
  renderSearchRuns(runs);

  // Poll each account's live progress so the panel shows "round 3/5" and that
  // round's names the moment they land, rather than nothing until the run ends.
  const poll = setInterval(async () => {
    await Promise.all(ids.map(async id => {
      if (runs[id].done) return;
      try {
        const p = await api(`/automation/search-progress?account_id=${id}`);
        runs[id].names = p.names || runs[id].names;
        mergeBios(runs[id].bioMap, p.profiles);   // bios land as each profile is read
        runs[id].seeds = p.seeds || runs[id].seeds;
        runs[id].round = p.round || runs[id].round;
        runs[id].rounds = p.rounds || runs[id].rounds;
      } catch { /* a dropped poll is harmless — the next catches up */ }
    }));
    renderSearchRuns(runs);
  }, 700);

  // Each account is its own Chrome window driven independently, so they run
  // concurrently — the same way the scroll scrape fans out across windows.
  await Promise.all(ids.map(async id => {
    const chip = $(`#scrap-accounts .acct-chip[data-acct="${id}"]`);
    try {
      const r = await api('/automation/open-search', { method: 'POST', body: { account_id: id } });
      runs[id].names = r.usernames || [];
      mergeBios(runs[id].bioMap, r.profiles);   // catch any bio read after the last poll
      runs[id].seeds = r.seeds || [];
      runs[id].round = r.rounds || runs[id].round;
      runs[id].saved = r.saved_records || 0;
      runs[id].bios = r.bios_saved || 0;
      runs[id].done = true;
      chip?.classList.remove('running'); chip?.classList.add('done');
    } catch (error) {
      runs[id].error = error.message;
      runs[id].done = true;
      chip?.classList.remove('running'); chip?.classList.add('failed');
    }
    renderSearchRuns(runs);
  }));

  clearInterval(poll);
  renderSearchRuns(runs);

  const ok = ids.filter(id => !runs[id].error);
  const bad = ids.filter(id => runs[id].error);
  const total = ok.reduce((n, id) => n + runs[id].names.length, 0);
  const saved = ok.reduce((n, id) => n + runs[id].saved, 0);
  const bios = ok.reduce((n, id) => n + (runs[id].bios || 0), 0);

  show(out, [
    ...ok.map(id => `${runs[id].who}: ${runs[id].names.length} username(s) from ` +
                    `${runs[id].seeds.length} search(es) [${runs[id].seeds.join(', ')}]` +
                    (runs[id].bios ? ` · ${runs[id].bios} bios` : '') +
                    (runs[id].names.length ? `\n  ${runs[id].names.join(', ')}` : '')),
    ...bad.map(id => `${runs[id].who}: ${runs[id].error}`),
    `\n${total} name(s) found, ${saved} new saved to Data, ${bios} bios saved to usernames_bio.json.`,
  ].join('\n'), bad.length > 0);

  if (note) {
    note.className = bad.length ? 'banner bad' : 'banner good';
    note.textContent = bad.length
      ? `${total} name(s) found · ${saved} new saved · ${bios} bios · ${bad.length} window(s) failed.`
      : `${total} name(s) found across ${ok.length} window(s) · ${saved} new saved to Data · ${bios} bios → usernames_bio.json.`;
  }
  toast(bad.length ? `${bad.length} window(s) failed` : `${total} found · ${saved} new saved · ${bios} bios`,
        bad.length > 0);
  if (saved || bios) loadRecords();           // the Data panel's list is now stale
});

/* Live per-account Search-Scrapping display: one block per window with its
   current iteration, the seeds tried, and the names found so far. */
function renderSearchRuns(runs) {
  const list = $('#scrap-list');
  if (!list) return;
  const ids = Object.keys(runs);
  if (!ids.length) { list.innerHTML = '<p class="hint">Usernames will appear here as they are found.</p>'; return; }

  list.innerHTML = ids.map(id => {
    const r = runs[id];
    const seedNote = r.seeds.length ? ` · ${r.seeds.map(esc).join(', ')}` : '';
    const state = r.error ? `failed — ${esc(r.error)}`
                : r.done  ? `${r.names.length} found in ${r.rounds} search(es)${seedNote}`
                          : `round ${r.round}/${r.rounds} · ${r.names.length} found so far${seedNote}`;
    const chips = r.names.length
      ? '<ol class="name-list name-list--scroll">' +
          r.names.map(n => nameRowHTML(n, r.bioMap)).join('') + '</ol>'
      : `<p class="hint">${r.error ? '—' : r.done ? 'No usernames found.' : 'Searching…'}</p>`;
    return `<div class="scrap-acct" data-acct="${id}">
              <div class="scrap-acct-head">${esc(r.who)} <span class="sub">· ${state}</span></div>
              ${chips}
            </div>`;
  }).join('');
  wireScrapCopy();
}

bind('#btn-scrap', '#out-scrap', async out => {
  const ids = [...scrapPicked];
  if (!ids.length) throw new Error('Tick at least one open-window account to scrape.');

  // Target: stop each account after this many usernames (no time limit).
  // 0 = unlimited — the backend then runs until the feed/search dry up or Stop.
  const target = Math.max(0, Math.floor(Number($('#scrap-target')?.value) || 0));
  const targetWord = target ? `${target}` : 'unlimited';
  saveScrapPrefs({ target });        // remember it even if 'change' didn't fire

  // Per-account state. `tech` is the chosen technique; `subTech` is what's live
  // right now (scroll|search|pause) as the backend batches. Names accumulate
  // into a Set so each account shows one merged, de-duped list.
  const runs = {};
  ids.forEach(id => {
    runs[id] = { who: accountById(id)?.account_name || `#${id}`, tech: techOf(id),
                 phase: 'run', names: [], nameSet: new Set(), target,
                 subTech: techOf(id) === 'search' ? 'search' : 'scroll',
                 batchTarget: 0, pauseLeft: null, saved: 0, bios: 0, bioMap: {}, error: null };
    $(`#scrap-accounts .acct-chip[data-acct="${id}"]`)?.classList.add('running');
  });
  const addNames = (id, arr) => (arr || []).forEach(n => {
    if (!runs[id].nameSet.has(n)) { runs[id].nameSet.add(n); runs[id].names.push(n); }
  });

  const note = $('#scrap-note');
  if (note) {
    note.className = 'banner';
    note.textContent = target
      ? `Collecting up to ${target}/account on ${ids.length} window(s)…`
      : `Collecting every handle ${ids.length} window(s) will give up — no limit…`;
  }
  show(out, target
    ? `Collecting up to ${target} username(s) per account on ${ids.length} window(s)…`
    : `Collecting usernames with NO limit on ${ids.length} window(s) — runs until the feed and search dry up, or you press Stop…`);
  renderScrapRuns(runs);

  // One poller for all runs — every technique now reports through visit-progress
  // (technique = scroll|search|pause, batch_target = current random batch size,
  // pause_left = pause countdown, target = the stop count).
  const poll = setInterval(async () => {
    await Promise.all(ids.map(async id => {
      const r = runs[id];
      if (r.phase !== 'run') return;
      try {
        const p = await api(`/automation/visit-progress?account_id=${id}`);
        addNames(id, p.names);
        mergeBios(r.bioMap, p.profiles);                      // bios land as each profile is read
        if (p.technique) r.subTech = p.technique;             // scroll|search|pause
        if (p.batch_target != null) r.batchTarget = p.batch_target;
        r.pauseLeft = p.pause_left;
        if (p.target) r.target = p.target;
      } catch { /* a dropped poll is harmless — the next catches up */ }
    }));
    renderScrapRuns(runs);
  }, SCRAP_POLL_MS);

  // Every account runs concurrently through the one count-bounded endpoint; the
  // backend does the batching, pausing, and (for 'both') technique switching.
  const runAccount = async id => {
    const r = runs[id];
    const chip = $(`#scrap-accounts .acct-chip[data-acct="${id}"]`);
    try {
      const d = await api('/automation/scrape-batched',
        { method: 'POST', body: { account_id: id, technique: r.tech, target_count: target, scrape_bio: true } });
      addNames(id, d.usernames || d.names || []);
      mergeBios(r.bioMap, d.profiles);   // catch any bio read after the last poll
      r.saved += d.saved_records ?? 0;
      r.bios += d.bios_saved ?? 0;
      r.phase = 'done';
      chip?.classList.remove('running'); chip?.classList.add('done');
    } catch (error) {
      r.error = error.message; r.phase = 'done';
      chip?.classList.remove('running'); chip?.classList.add('failed');
    }
    renderScrapRuns(runs);
  };

  await Promise.all(ids.map(runAccount));
  clearInterval(poll);
  renderScrapRuns(runs);

  const bad = ids.filter(id => runs[id].error);
  const totalFound = ids.filter(id => !runs[id].error).reduce((n, id) => n + runs[id].names.length, 0);
  const totalSaved = ids.reduce((n, id) => n + runs[id].saved, 0);
  const totalBios = ids.reduce((n, id) => n + (runs[id].bios || 0), 0);
  const techWord = { scroll: 'scroll', search: 'search', both: 'scroll+search' };
  const lines = ids.map(id => {
    const r = runs[id];
    const found = r.target ? `${r.names.length}/${r.target}` : `${r.names.length}`;
    return r.error ? `${r.who} (${techWord[r.tech]}): ${r.error}`
                   : `${r.who} (${techWord[r.tech]}): ${found} found, ${r.saved} new, ${r.bios || 0} bios.`;
  });

  if (note) {
    note.className = bad.length ? 'banner' : 'banner good';
    note.textContent = `${ids.length} window(s): found ${totalFound}, added ${totalSaved} new to Data, ${totalBios} bios → usernames_bio.json` +
                       (bad.length ? `. ${bad.length} failed.` : '.');
  }
  show(out, `Scraped ${ids.length} window(s), target ${targetWord}/account.\n\n` + lines.join('\n') +
            `\n\nTotal: ${totalFound} found, ${totalSaved} new records added to Data, ${totalBios} bios saved to usernames_bio.json.`, bad.length > 0);
  toast(`Scraped ${ids.length} window(s): +${totalSaved} names, ${totalBios} bios`, bad.length > 0);
  loadRecords();                   // the new names land in Data
});

/* ── Automation ──────────────────────────────────────────────── */
/* The sequence is driven here, one step at a time, rather than in one backend
   call — a single call only returns once everything is finished, which cannot
   drive a live spinner. */
/* How many goes a step gets before the sequence gives up on it, shown as the
   .tries circle counting down. Step 1 has no entry: it runs once, since it's
   what the retries below fall BACK on — it can't also be the thing retried.
   Login isn't here — it has two methods with their own budgets (see STEPS). */
const ATTEMPTS = { newtab: 5, type: 5, creds: 5 };

/* Long enough for whatever wasn't ready yet to become ready — retrying a UI
   race instantly just fails again at the same speed. */
const RETRY_PAUSE_MS = 1200;

const STEPS = [
  { key: 'open',   n: 1, check: '#step-open',   label: 'Open Chrome',
    // the account's own profile, so its session is saved against it and not
    // into the shared default one
    run: ctx => api('/automation/open', { method: 'POST', body: { account_id: ctx.accountId } }) },
  // `back`: the step to redo before retrying this one. A step rarely fails on
  // its own account — the tab never appeared, the page navigated away, the form
  // reset — so repeating it against the same broken state just fails the same
  // way. Redoing the step before it rebuilds what this one needs: no tab, open
  // Chrome again; login won't submit, retype the credentials and press again.
  { key: 'newtab', n: 2, check: '#step-newtab', label: 'New Tab', back: 'open',
    run: ctx => api('/automation/new-tab', { method: 'POST', body: { account_id: ctx.accountId } }) },
  { key: 'type',   n: 3, check: '#step-type',   label: 'Type in address bar', back: 'newtab',
    run: ctx => api('/automation/cursor-url',
      { method: 'POST', body: { url: ctx.url, account_id: ctx.accountId } }) },
  { key: 'creds',  n: 4, check: '#step-creds',  label: 'Type credentials', back: 'type',
    run: ctx => api('/automation/type-credentials', { method: 'POST', body: { account_id: ctx.accountId } }) },
  // Two ways to press Log in, tried in order. The real cursor first — that's
  // what a person's click looks like — but it can miss (window covered, moved).
  // Once it's missed all 5 goes, fall back to the DOM click, which can't miss
  // but is a synthetic event. Each attempt of either retypes the credentials.
  { key: 'login',  n: 5, check: '#step-login',  label: 'Click Login', back: 'creds',
    methods: [
      { id: 'cursor',  label: 'via Cursor',  tries: 5 },
      { id: 'inspect', label: 'via Inspect', tries: 3 }
    ],
    // The click returns 200 whether or not it logged in — a rejection is an
    // answer, not a transport error. So "the request came back" is NOT success:
    // step 5 is done only when Instagram actually reached the feed (logged_in).
    run: async (ctx, method) => {
      const r = await api('/automation/click-login',
        { method: 'POST', body: { account_id: ctx.accountId, method } });
      if (r.logged_in) return r;                 // main page — the real success
      // Not logged in — throw so this attempt fails. A plain failure is
      // retryable: the loop redoes step 4 first (back: 'creds'), so the
      // credentials are RETYPED fresh before each retry — which fixes a login
      // that failed because a keystroke dropped or the field held stale text.
      // 2FA / challenge are different: the password stage already passed and
      // Instagram now wants something only you can give in Chrome, so retyping
      // the password would be nonsense — those stop instead of looping.
      const err = new Error(r.detail || 'Login did not complete — the feed never loaded.');
      err.fatal = ['2fa_required', 'challenge'].includes(r.action);
      throw err;
    } }
];

/* The attempt budget(s) of a step, as a uniform list. A retrying step with no
   explicit methods runs one nameless method off ATTEMPTS; a step with neither
   (Open Chrome) runs once. Both the runner and the circle markup read this, so
   they can't disagree about how many goes a step gets. */
function stepMethods(step) {
  if (step.methods) return step.methods;
  const tries = ATTEMPTS[step.key];
  return tries ? [{ id: '_', tries }] : [];
}

/* The attempt-circle markup for a step: one bare circle for a single budget,
   or a labelled chip per method. `cls` differs between the big steps (.tries)
   and the per-account strips (.q-tries). */
function triesHTML(step, cls) {
  const methods = stepMethods(step);
  if (!methods.length) return '';
  if (methods.length === 1 && methods[0].id === '_') {
    return `<i class="${cls}" title="Attempts left">${methods[0].tries}</i>`;
  }
  return '<span class="methods">' + methods.map(m =>
    `<span class="method${m.id === methods[0].id ? ' on' : ''}" data-method="${m.id}">` +
    `<em>${esc(m.label)}</em>` +
    `<i class="${cls}" data-method="${m.id}" title="${esc(m.label)} attempts left">${m.tries}</i>` +
    '</span>').join('');
}

/* The short caption under each step, reused by the per-account strips. */
const STEP_NOTE = {
  open: 'empty window', newtab: "clicks Chrome's +",
  type: 'real cursor', creds: 'with corrections', login: 'submits the form'
};

/** Paint every attempt circle inside `scope` for `step`.
 *  `prog` is the runtime progress: { active, left } where `left` maps a method
 *  id to its remaining count. prog null/undefined = not started, so each circle
 *  shows its full budget and the first method is the active one. */
function renderTries(scope, step, prog) {
  const methods = stepMethods(step);
  scope.querySelectorAll('.tries, .q-tries').forEach(circle => {
    const id = circle.dataset.method || '_';
    const budget = methods.find(m => m.id === id)?.tries ?? 0;
    const remaining = prog?.left?.[id];
    const n = remaining == null ? budget : remaining;
    circle.textContent = n;
    circle.classList.toggle('spent', n === 0);
    circle.classList.toggle('low', n > 0 && n < budget);

    // highlight the method in play; dim the ones already exhausted
    const chip = circle.closest('.method');
    if (chip) {
      const active = (prog?.active || methods[0]?.id) === id;
      chip.classList.toggle('on', active);
      chip.classList.toggle('used', n === 0 && !active);
    }
  });
}

function setStep(step, state, accountId = null, prog = null) {
  const box = $(`#box-${step.key}`);
  box.className = `step ${state}`;
  const badge = box.querySelector('.badge');
  badge.textContent = state === 'done' ? '✓' : state === 'failed' ? '✕'
                    : state === 'running' ? '' : step.n;   // empty = CSS spinner
  renderTries(box, step, prog);

  // mirror onto that account's own strip, and remember it: the queue re-renders
  // whenever the active account changes, which would otherwise wipe the history
  if (accountId != null) {
    stepState[accountId] = { ...(stepState[accountId] || {}), [step.key]: { state, prog } };
    paintStep(accountId, step, state, prog);
  }
}

const stepState = {};   // accountId -> { stepKey: { state, prog } }

function paintStep(accountId, step, state, prog = null) {
  const el = $(`#auto-detail [data-qsteps="${accountId}"] [data-qstep="${step.key}"]`);
  if (!el) return;
  el.className = `q-step ${state}`;
  el.querySelector('.q-badge').textContent =
    state === 'done' ? '✓' : state === 'failed' ? '✕' : state === 'running' ? '' : step.n;
  renderTries(el, step, prog);
}

/** Re-apply every account's step history after the queue re-renders. */
function applyStepState() {
  for (const [accountId, steps] of Object.entries(stepState)) {
    for (const [key, { state, prog }] of Object.entries(steps)) {
      const step = STEPS.find(s => s.key === key);
      if (step) paintStep(accountId, step, state, prog);
    }
  }
}

function resetSteps(accountId = null) {
  STEPS.forEach(s => setStep(s, $(s.check).checked ? 'idle' : 'skip', accountId));
}

/* Ticking a step re-arms every queued account's strip too — the checkboxes are
   the plan, the per-account strips are what happened. */
STEPS.forEach(s => $(s.check).addEventListener('change', () => {
  resetSteps();
  Object.keys(stepState).forEach(k => delete stepState[k]);
  syncAutoAccount();
}));

async function loadCredsAccounts() {
  try {
    const { accounts } = await api('/accounts');
    accountCache = accounts;
    fillAccountPickers(accounts);
  } catch { /* the banner reports it */ }
  syncAutoAccount();
}

/* The accounts handed over from the Accounts panel. The Automation panel has
   no picker of its own — one source of truth, so the two can't disagree about
   who they're driving. Nothing here launches anything: the queue is only run
   when Start Processing is pressed. */
let autoQueue = [];

const queued = () => (autoQueue.length ? autoQueue : (active.id ? [active.id] : []));

function syncAutoAccount() {
  const banner = $('#auto-session');
  const detail = $('#auto-detail');
  const step4 = $('#step-creds');
  const step5 = $('#step-login');
  if (!banner || !detail) return;

  const ids = queued();
  const list = ids.map(accountById).filter(Boolean);
  detail.hidden = !list.length;

  if (!list.length) {
    banner.className = 'banner';
    banner.textContent =
      'No account queued — pick one in the Accounts panel. Step 4 falls back to ' +
      'IG_USERNAME/IG_PASSWORD from .env.';
    return;
  }

  detail.innerHTML = list.map((a, i) => `
    <div class="q-item" data-qitem="${a.id}">
      <div class="q-row ${a.id === active.id ? 'on' : ''}" data-qid="${a.id}">
        <span class="q-n">${i + 1}</span>
        <span class="q-name">${esc(a.account_name)}${a.locked ? ' 🔒' : ''}</span>
        <span class="q-mail" title="${esc(a.login)}">${esc(a.login)}</span>
        <code class="q-pw" data-pw-id="${a.id}">${'•'.repeat(Math.min(a.password_len || 8, 12))}</code>
        <button class="icon-btn" data-qeye="${a.id}" title="Show password" aria-label="Show password">👁</button>
        <span class="q-sess ${a.logged_in ? 'ok' : 'off'}">${esc(a.status)}</span>
        <button class="icon-btn danger" data-qdel="${a.id}" title="Remove from the queue"
                aria-label="Remove from queue">✕</button>
      </div>
      <!-- this account's own four steps, so with several queued you can see
           each one's progress instead of only the account running now -->
      <div class="q-steps" data-qsteps="${a.id}">
        ${STEPS.map(s => `
          <span class="q-step ${$(s.check).checked ? 'idle' : 'skip'}" data-qstep="${s.key}">
            <b class="q-badge">${s.n}</b>${esc(s.label)}${s.methods ? '' : `<em>${esc(STEP_NOTE[s.key])}</em>`}
            ${triesHTML(s, 'q-tries')}
          </span>`).join('<span class="q-arrow">→</span>')}
      </div>
    </div>`).join('');

  wireQueueRow();
  applyQueueState();      // re-render just wiped the running/done marks
  applyStepState();       // …and each account's own step history

  const needLogin = list.filter(a => !a.logged_in);
  // steps 4 and 5 are the login pair: typing credentials nobody submits is
  // half a job, and submitting a form nobody filled in is the other half
  const want = checked => {
    let changed = false;
    for (const box of [step4, step5]) {
      if (box && box.checked !== checked) { box.checked = checked; changed = true; }
    }
    if (changed) resetSteps();
  };

  if (!needLogin.length) {
    // every queued account already has a session, so steps 4-5 have nothing to do
    banner.className = 'banner good';
    banner.textContent = list.length === 1
      ? `${list[0].account_name} has a valid session — login will be skipped.`
      : `All ${list.length} queued accounts have valid sessions — login will be skipped.`;
    want(false);
  } else {
    banner.className = 'banner bad';
    banner.textContent =
      `${needLogin.length} of ${list.length} queued account(s) need a login — ` +
      `step 4 types their credentials and step 5 submits them. Press Start ` +
      `Processing when ready; nothing runs until you do.`;
    want(true);
  }
}

function wireQueueRow() {
  // eye — fetch the secret only when asked for
  $$('#auto-detail [data-qeye]').forEach(button => {
    button.addEventListener('click', async () => {
      const id = button.dataset.qeye;
      const cell = $(`#auto-detail [data-pw-id="${id}"]`);
      if (cell.dataset.shown === '1') {
        const a = accountById(id);
        cell.textContent = '•'.repeat(Math.min(a?.password_len || 8, 12));
        cell.dataset.shown = '0';
        button.textContent = '👁';
        return;
      }
      try {
        const { password } = await api(`/accounts/${id}/secret`);
        cell.textContent = password;
        cell.dataset.shown = '1';
        button.textContent = '🙈';
      } catch (error) { toast(error.message, true); }
    });
  });

  $$('#auto-detail [data-qdel]').forEach(button => {
    button.addEventListener('click', () => {
      const id = Number(button.dataset.qdel);
      autoQueue = autoQueue.filter(x => x !== id);
      if (active.id === id) active.id = autoQueue[0] || null;
      syncAutoAccount();
    });
  });

  $$('#auto-detail .q-row').forEach(row => {
    row.addEventListener('click', event => {
      if (event.target.closest('button')) return;
      active.id = Number(row.dataset.qid);
      syncAutoAccount();
    });
  });
}

/* Make sure this account's Chrome is open AND on screen. Called before every
   retry so a step never re-fails simply because its window was closed, behind
   another, or minimised — the whole point of "reprocess on a visible window". */
async function ensureChrome(ctx, out, log) {
  try {
    const r = await api('/automation/ensure', { method: 'POST', body: { account_id: ctx.accountId } });
    if (r.opened) { log.push('🪟 Chrome was closed — reopened it'); show(out, log.join('\n')); }
  } catch (error) {
    // not fatal: the retry itself will report a genuinely dead browser
    log.push(`🪟 couldn't confirm Chrome on screen: ${error.message}`);
  }
}

/* Redo the step before a failing one, to rebuild the state it needs. Its own
   failure is not fatal — the retry is about to run anyway and will report the
   real problem — so this only logs and moves on. Always ensures Chrome is
   visible first. */
async function stepBack(step, ctx, out, log, accountId) {
  await ensureChrome(ctx, out, log);

  const back = STEPS.find(s => s.key === step.back);
  if (!back) return;

  const prog = stepState[accountId]?.[back.key]?.prog;
  setStep(back, 'running', accountId, prog);
  show(out, `${log.join('\n')}\n↺ redoing ${back.n}. ${back.label} first…`.trim());
  try {
    await back.run(ctx);
    setStep(back, 'done', accountId, prog);
    log.push(`↺ redid ${back.n}. ${back.label}`);
  } catch (error) {
    setStep(back, 'failed', accountId, prog);
    log.push(`↺ ${back.n}. ${back.label} would not redo either: ${error.message}`);
  }
}

/* ── Live address-bar mirror (step 3) ──────────────────────────────
   While the cursor types the URL, poll the backend a few times a second and
   show each character landing in Chrome's real address bar, matched against
   the URL in the UI. Green when the two agree — which is the cue the sequence
   uses to move on. */
function bareUrl(s) {
  return (s || '').trim().toLowerCase().replace(/^https?:\/\//, '');
}

/** Highlight the leading run of `chrome` that already matches the UI URL. */
function markMatch(ui, chrome) {
  const u = bareUrl(ui), c = bareUrl(chrome);
  let n = 0;
  while (n < c.length && n < u.length && c[n] === u[n]) n++;
  // map the matched length (measured on the bare form) back onto the raw text
  const lead = chrome.length - bareUrl(chrome).length;   // scheme chars, if shown
  const cut = lead + n;
  return `<b class="hit">${esc(chrome.slice(0, cut))}</b>${esc(chrome.slice(cut))}`;
}

function renderUrlCheck(uiUrl, p) {
  const box = $('#url-check');
  if (!box) return;
  const chrome = p.chrome || '';
  $('#uc-chrome').innerHTML = chrome ? markMatch(uiUrl, chrome) : '—';
  const pct = p.total ? Math.min(100, Math.round((p.done / p.total) * 100)) : 0;
  $('#uc-fill').style.width = `${pct}%`;

  const matched = !!p.matched;
  const diverged = p.diverged_at != null && !matched;
  box.classList.toggle('ok', matched);
  box.classList.toggle('warn', diverged);
  $('#uc-note').textContent = matched
    ? '✓ match — UI and Chrome agree, moving on'
    : diverged
      ? `character ${p.diverged_at} didn't land — correcting…`
      : `typing… ${p.done}/${p.total || '?'} characters`;
}

/** Show the readout and poll it until stopped. Returns an async stop(). */
function watchTyping() {
  const box = $('#url-check');
  const uiUrl = ($('#auto-url').value || '').trim();
  if (!box) return async () => {};
  box.hidden = false;
  box.classList.remove('ok', 'warn');
  $('#uc-ui').textContent = uiUrl || '(from .env)';
  $('#uc-chrome').innerHTML = '—';
  $('#uc-fill').style.width = '0%';
  $('#uc-note').textContent = 'starting…';

  const tick = async () => {
    try { renderUrlCheck(uiUrl, await api('/automation/type-progress')); } catch { /* transient */ }
  };
  const id = setInterval(tick, 300);
  tick();
  return async () => { clearInterval(id); await tick(); };   // one last read to catch the final state
}

/** Run a step; for step 3 wrap it in the live address-bar mirror. */
async function runStep(step, ctx, methodId) {
  if (step.key !== 'type') return step.run(ctx, methodId);
  const stop = watchTyping();
  try {
    return await step.run(ctx, methodId);
  } finally {
    await stop();
    // Show the mirror only WHILE step 3 types. Hide it the moment it's done, so
    // it never lingers through the login steps or after the run finishes.
    const uc = $('#url-check');
    if (uc) uc.hidden = true;
  }
}

/* Run one step, working through its method(s) in order. Each method gets its
   own budget of attempts; every failed attempt redoes the step before it (see
   `back`) so the next try starts from a rebuilt state — the circle reaching 0
   means real goes, not identical pokes at a wedged page. When a method runs
   out, the next one takes over (cursor → inspect for Log in). Returns the first
   success, or throws the last failure once every method is spent. */
async function attemptStep(step, ctx, out, log, accountId) {
  const methods = stepMethods(step);
  const plan = methods.length ? methods : [{ id: '_', tries: 1 }];   // Open Chrome: one shot
  const left = Object.fromEntries(plan.map(m => [m.id, m.tries]));
  let last;

  outer:
  for (const [mi, method] of plan.entries()) {
    const named = method.id !== '_';
    const via = named ? ` ${method.label}` : '';

    for (let a = method.tries; a >= 1; a--) {
      left[method.id] = a;
      setStep(step, 'running', accountId, { active: method.id, left: { ...left } });
      const note = method.tries > 1 ? ` · attempt ${method.tries - a + 1}/${method.tries}` : '';
      show(out, `${log.join('\n')}\n▶ ${step.n}. ${step.label}${via}${note}…`.trim());
      try {
        const result = await runStep(step, ctx, named ? method.id : undefined);
        setStep(step, 'done', accountId, { active: method.id, left: { ...left } });
        return result;
      } catch (error) {
        last = error;
        if (error.fatal) {                   // wrong password / 2FA — retrying can't help and is risky
          left[method.id] = 0;
          log.push(`✕ ${step.n}. ${step.label}${via} — ${error.message}`);
          break outer;
        }
        if (a > 1) {                         // more goes at this method
          log.push(`↻ ${step.n}. ${step.label}${via} — attempt ${method.tries - a + 1}/` +
                   `${method.tries} failed: ${error.message} · ${a - 1} ` +
                   `${a - 1 === 1 ? 'try' : 'tries'} left`);
        } else {                             // this method is spent
          left[method.id] = 0;
          const next = plan[mi + 1];
          if (!next) break;                  // no fallback left — caller reports it
          log.push(`↦ ${step.label}${via} failed all ${method.tries} attempts — ` +
                   `switching to ${next.label} (${next.tries} tries)`);
        }
        setStep(step, 'running', accountId, { active: method.id, left: { ...left } });
        show(out, log.join('\n'));
        await sleep(RETRY_PAUSE_MS);         // a beat: some of these pass on their own
        await stepBack(step, ctx, out, log, accountId);
      }
    }
  }

  setStep(step, 'failed', accountId, { active: plan[plan.length - 1].id, left });
  const tried = plan.map(m => m.id === '_' ? null : `${m.label} ×${m.tries}`).filter(Boolean);
  throw new Error(tried.length
    ? `${last.message} (tried ${tried.join(', then ')})`
    : last.message);
}

async function runSequence(out, accountId = active.id, prefix = '') {
  const url = $('#auto-url').value.trim();
  const queue = STEPS.filter(s => $(s.check).checked);
  if (!queue.length) throw new Error('Tick at least one step.');
  if (queue.some(s => s.key === 'type') && !url) {
    throw new Error('Enter a URL to type into the address bar.');
  }

  resetSteps(accountId);
  const uc = $('#url-check');
  if (uc) uc.hidden = true;               // fresh start; watchTyping reshows it at step 3
  const log = prefix ? [prefix] : [];
  const ctx = { url, accountId };
  for (const step of queue) {
    try {
      const result = await attemptStep(step, ctx, out, log, accountId);
      log.push(`✓ ${step.n}. ${step.label}\n${JSON.stringify(result, null, 2)}`);
    } catch (error) {
      // leave the rest idle — they never ran
      queue.slice(queue.indexOf(step) + 1).forEach(s => setStep(s, 'idle', accountId));
      show(out, `${log.join('\n')}\n✕ ${step.n}. ${step.label}\n${error.message}`.trim(), true);
      throw new Error(`Step ${step.n} (${step.label}): ${error.message}`);
    }
  }
  show(out, log.join('\n\n'));
  return log;
}

/* Start Processing — the only thing that launches Chrome. Works through every
   queued account in turn; a profile is locked to one Chrome process, so they
   can't overlap. */
bind('#btn-cursor', '#out-automation', async out => {
  // The top steps row is the PLAN — you edit it before a run. Once processing
  // starts it would just repeat itself for every account, so fold it away and
  // let each account's own strip carry the live progress. Restored in finally,
  // so it comes back whether the run finishes or throws.
  const stepsRow = $('#panel-automation .steps');
  stepsRow?.setAttribute('hidden', '');
  try {
    await processQueue(out);
  } finally {
    stepsRow?.removeAttribute('hidden');
  }
});

async function processQueue(out) {
  // The panel's picker wins when it names an account; "Ticked in Accounts"
  // leaves it empty and the ticked queue drives the run as before.
  const picked = pickedAccount('#auto-account');
  const ids = picked ? [picked] : queued();
  if (!ids.length) {                       // no account: .env credentials
    await runSequence(out, null);
    toast('✅ Done');
    return;
  }

  const done = [];
  const failed = [];
  for (const [index, id] of ids.entries()) {
    const a = accountById(id);
    const who = a ? a.account_name : `#${id}`;

    active.id = id;
    syncAutoAccount();
    setQueueRow(id, 'running');

    if (index) {
      // wipe the steps back to idle between accounts and hold a beat, so the
      // restart is something you can actually see rather than a flicker
      resetSteps();
      show(out, `${done.join('\n')}\n\n⟳ resetting steps for ${who} (${index + 1}/${ids.length})…`);
      await sleep(900);
    }
    if (ids.length > 1) toast(`Account ${index + 1}/${ids.length}: ${who}`);

    try {
      const log = await runSequence(out, id, ids.length > 1 ? `━━ ${index + 1}/${ids.length} ${who} ━━` : '');
      setQueueRow(id, 'done');
      done.push(...log);
    } catch (error) {
      // One account failing must not strand the rest — and its window stays
      // open, which is exactly the one you want to look at.
      setQueueRow(id, 'failed');
      failed.push(who);
      done.push(`━━ ${index + 1}/${ids.length} ${who} ━━\n✕ ${error.message}`);
      show(out, done.join('\n\n'), true);
    }
  }

  show(out,
    `${done.join('\n\n')}\n\n` +
    `${ids.length - failed.length}/${ids.length} succeeded` +
    `${failed.length ? ` · failed: ${failed.join(', ')}` : ''}\n` +
    `Every window is still open — close them with Close browser.`,
    failed.length > 0);
  toast(failed.length
    ? `⚠️ ${failed.length}/${ids.length} account(s) failed`
    : `✅ ${ids.length} account(s) processed`, failed.length > 0);
  loadAccounts();
}

/** Mark a queued account's row, so with several accounts you can see which one
 *  is live and which are finished.
 *  Kept in a map, not just on the element: syncAutoAccount() rewrites the
 *  queue's innerHTML whenever the active account changes, which would wipe the
 *  ✓ off every account already finished. */
const queueState = {};

function setQueueRow(id, state) {
  queueState[id] = state;
  applyQueueState();
}

function applyQueueState() {
  $$('#auto-detail .q-row').forEach(row => {
    const state = queueState[row.dataset.qid];
    row.classList.remove('running', 'done', 'failed');
    if (state) row.classList.add(state);
  });
}

/* ── Accounts → Automation handoff ───────────────────────────────
   "Log in selected" and "New Login" don't log in behind the scenes any more:
   they hand the account over to the Automation panel — details on screen —
   and run the sequence there, so you can watch it happen. */
async function handoff(ids, { fresh = false } = {}) {
  if (!ids.length) throw new Error('Select at least one account first.');

  if (fresh) {
    // New Login: drop the stored sessions now, so Start Processing does a real
    // fresh login rather than skipping past a live one
    for (const id of ids) await api(`/accounts/${id}/session`, { method: 'DELETE' });
    toast(`🔄 ${ids.length} session(s) cleared`);
  }

  autoQueue = [...ids];
  // a new run starts clean — no ✓ left over from last time
  Object.keys(queueState).forEach(k => delete queueState[k]);
  Object.keys(stepState).forEach(k => delete stepState[k]);
  active.id = ids[0];
  openPanel('automation');
  await loadCredsAccounts();          // refreshes the cache, then renders the queue
  $('#auto-url').value = 'www.instagram.com';
  syncAutoAccount();
  loadAccounts();
  // deliberately does NOT run: the sequence seizes the real mouse and keyboard,
  // so it only ever starts on an explicit press of Start Processing
  toast(`${ids.length} account(s) queued — press Start Processing`);
}

/* Closes every open window — with several accounts up, there are several. */
bind('#btn-close', '#out-automation', async out => {
  const data = await api('/automation/close', { method: 'POST' });
  show(out, data);
  toast(data.windows_closed?.length
    ? `${data.windows_closed.length} window(s) closed`
    : 'No windows were open');
});

async function loadBrowserStatus() {
  loadCredsAccounts();
  resetSteps();
  try { show($('#out-automation'), await api('/automation/status')); } catch { /* pill covers it */ }
}

/* ── Activity ────────────────────────────────────────────────── */
const activity = { events: [], src: 'all', errorsOnly: false, follow: true, es: null, max: 600 };

function actMatches(e) {
  if (activity.errorsOnly && !['warn', 'error'].includes(e.level)) return false;
  return activity.src === 'all' || e.source === activity.src;
}

function actRow(e) {
  const time = (e.at || '').slice(11, 19);
  const dur = e.ms != null ? `${e.ms}ms` : '';
  return `<div class="log-row ${esc(e.level)}">
    <span class="t">${esc(time)}</span>
    <span class="s">${esc(e.source)}</span>
    <span class="m">${esc(e.message)}</span>
    <span class="d">${esc(dur)}</span>
  </div>`;
}

function renderActivity() {
  const box = $('#activity-log');
  if (!box) return;
  const shown = activity.events.filter(actMatches);
  box.innerHTML = shown.length
    ? shown.map(actRow).join('')
    : '<p class="log-empty">Nothing yet. Use another panel and watch it land here.</p>';
  $('#act-count').textContent = `${shown.length} shown · ${activity.events.length} total`;
  if (activity.follow) box.scrollTop = box.scrollHeight;
}

function pushEvent(e) {
  activity.events.push(e);
  if (activity.events.length > activity.max) activity.events.shift();
  if (e?.kind === 'media-copy' || e?.kind === 'media-paste') {
    toast(e.message, e.level === 'warn' || e.level === 'error');
  }
  if (!$('#panel-activity').hidden) renderActivity();
}

function liveState(on, text) {
  $('#live-dot').className = `live ${on ? 'on' : 'off'}`;
  $('#live-text').textContent = text;
}

function connectActivity() {
  if (activity.es) return;                       // already streaming
  const since = activity.events.at(-1)?.id || 0;
  api(`/activity?since=${since}`)
    .then(data => { data.events.forEach(e => activity.events.push(e)); renderActivity(); })
    .catch(() => {});

  const es = new EventSource('/api/activity/stream');
  activity.es = es;
  es.onopen = () => liveState(true, 'live');
  es.onmessage = event => { try { pushEvent(JSON.parse(event.data)); } catch {} };
  es.onerror = () => liveState(false, 'reconnecting…');   // EventSource retries by itself
}

function loadActivity() {
  connectActivity();
  renderActivity();
}

$$('#act-filters .seg-b').forEach(button => {
  button.addEventListener('click', () => {
    activity.src = button.dataset.src;
    $$('#act-filters .seg-b').forEach(b => b.classList.toggle('on', b === button));
    renderActivity();
  });
});
$('#act-errors').addEventListener('change', e => { activity.errorsOnly = e.target.checked; renderActivity(); });
$('#act-follow').addEventListener('change', e => { activity.follow = e.target.checked; renderActivity(); });

bind('#btn-act-clear', null, async () => {
  await api('/activity/clear', { method: 'POST' });
  activity.events = [];
  renderActivity();
  toast('Activity cleared');
});

/* ── Config ──────────────────────────────────────────────────── */
const CFG_FIELDS = {
  speed_ms: '#cfg-speed', jitter_ms: '#cfg-jitter', remove_every: '#cfg-every',
  remove_min: '#cfg-min', remove_max: '#cfg-max',
  // Scrolling — its own section, nothing to do with typing. Same stored values
  // as Scrapping → ⚙️ Settings, so editing either place moves both.
  scroll_pause_min_ms: '#cfg-pause-min', scroll_pause_max_ms: '#cfg-pause-max',
  scroll_step_min_pct: '#cfg-step-min', scroll_step_max_pct: '#cfg-step-max',
  scroll_duration_s: '#cfg-duration',
  // Campaign batching
  batch_size_min: '#cfg-batch-size-min', batch_size_max: '#cfg-batch-size-max',
  batch_delay_min: '#cfg-batch-delay-min', batch_delay_max: '#cfg-batch-delay-max',
  // Campaign actions — rows between each interleaved action
  campaign_action_min: '#cfg-action-min', campaign_action_max: '#cfg-action-max',
};

function readCfg() {
  const cfg = {};
  for (const [key, sel] of Object.entries(CFG_FIELDS)) cfg[key] = Number($(sel).value);
  cfg.remove_enabled = $('#cfg-enabled').checked;
  cfg.press_enter = $('#cfg-press-enter').checked;
  cfg.batch_enabled = $('#cfg-batch-enabled').checked;
  return cfg;
}

function writeCfg(cfg) {
  for (const [key, sel] of Object.entries(CFG_FIELDS)) if (cfg[key] !== undefined) $(sel).value = cfg[key];
  $('#cfg-enabled').checked = !!cfg.remove_enabled;
  // Default ON when the store has never held it (older saved settings).
  $('#cfg-press-enter').checked = cfg.press_enter === undefined ? true : !!cfg.press_enter;
  $('#cfg-batch-enabled').checked = !!cfg.batch_enabled;
}

async function loadConfig() {
  renderPanelToggles();          // the 🧭 Panels switches (localStorage, independent of /settings)
  try {
    const { settings } = await api('/settings');
    writeCfg(settings);
    $('#cfg-summary').className = 'banner';
    $('#cfg-summary').textContent = 'Preview to see what a login would look like.';
  } catch (error) {
    $('#cfg-summary').className = 'banner bad';
    $('#cfg-summary').textContent = error.message;
  }
}

async function previewCfg() {
  const body = { ...readCfg(), text: $('#cfg-text').value.trim() || 'test.user@example.com' };
  const data = await api('/settings/preview', { method: 'POST', body });
  const banner = $('#cfg-summary');
  banner.className = `banner ${data.correct ? 'good' : 'bad'}`;
  banner.textContent = data.correct
    ? `${data.chars} chars → ${data.keystrokes} keystrokes (${data.backspaces} backspaces) · ~${data.estimated_s}s · types correctly`
    : `Broken: types "${data.result}" instead of "${body.text}"`;
  show($('#out-config'),
    `trace  ${data.trace}\n       (${data.trace_legend})\n\n` +
    `result ${data.result}\nmatches input: ${data.correct}`);
  return data;
}

bind('#btn-cfg-preview', null, previewCfg);

bind('#btn-cfg-save', null, async () => {
  const { settings } = await api('/settings', { method: 'PUT', body: readCfg() });
  writeCfg(settings);          // echo back what was actually stored, post-clamp
  toast('Settings saved');
  await previewCfg();
});

bind('#btn-cfg-reset', null, async () => {
  const { settings } = await api('/settings/reset', { method: 'POST' });
  writeCfg(settings);
  toast('Reset to defaults');
  await previewCfg();
});

/* ── Accounts ────────────────────────────────────────────────── */
const selected = new Set();

/* The one account both panels act on. Picking a card in Accounts sets it; the
   Automation panel reads the same value, so the two can't disagree about who
   they're driving. Survives reloads. */
const active = {
  get id() { return Number(localStorage.getItem('activeAccount')) || null; },
  set id(v) {
    v ? localStorage.setItem('activeAccount', v) : localStorage.removeItem('activeAccount');
    document.dispatchEvent(new CustomEvent('active-account'));
  }
};

let accountCache = [];
const accountById = id => accountCache.find(a => a.id === Number(id)) || null;

function syncCount() {
  const n = selected.size;
  $('#acc-count').textContent = `${n} selected`;
  $('#btn-acc-login').disabled = n === 0;
  const a = accountById(active.id);
  // Check Login Status sweeps every stored session, so it never needs a
  // selection. New Login acts on one account, so it does.
  $('#btn-acc-newlogin').disabled = !a;

  const banner = $('#acc-active');
  if (!a) {
    banner.className = 'banner';
    banner.textContent = 'No active account — click a card to pick one.';
  } else {
    banner.className = `banner ${a.logged_in ? 'good' : ''}`;
    banner.textContent =
      `Active: ${a.account_name} (${a.login}) · ${a.platform} · ${a.status}` +
      `${a.locked ? ' 🔒 locked' : ''} · ${a.reason}`;
  }
}

document.addEventListener('active-account', () => { syncCount(); syncAutoAccount(); });

async function loadAccounts() {
  const grid = $('#accounts-grid');
  try {
    const { accounts } = await api('/accounts');
    accountCache = accounts;
    fillAccountPickers(accounts);   // adding/removing an account updates the pickers
    // drop selections for accounts that no longer exist
    [...selected].forEach(id => accounts.some(a => a.id === id) || selected.delete(id));
    // …and for any that turn out to be logged in: they don't need a login, so
    // they must not sit in the queue. Covers an account that was ticked before
    // Check Login Status found its session.
    accounts.filter(a => a.logged_in).forEach(a => selected.delete(a.id));
    if (active.id && !accounts.some(a => a.id === active.id)) active.id = null;

    grid.innerHTML = accounts.length
      ? accounts.map(a => `
        <article class="acc ${selected.has(a.id) ? 'sel' : ''} ${a.id === active.id ? 'active' : ''}
                        ${a.logged_in ? 'logged-in' : ''}"
                 data-id="${a.id}" data-live="${a.logged_in ? 1 : 0}"
                 role="button" tabindex="0" aria-pressed="${selected.has(a.id)}"
                 title="${a.logged_in ? 'Valid stored session — no login needed' : ''}">
          <span class="tick">✓</span>
          <div class="acc-top">
            <span class="acc-state" title="${a.status}">${a.logged_in ? '🟡' : '⚪'}</span>
            <span class="acc-name">${esc(a.account_name)}</span>
            ${a.id === active.id ? '<span class="acc-badge">active</span>' : ''}
            ${a.window_open ? '<span class="lock" title="Its window is open right now">🪟</span>' : ''}
          </div>
          ${a.label ? `<div class="acc-login" title="${esc(a.login)}">${esc(a.login)}</div>` : ''}
          <div class="acc-pass">
            <code data-pw="${a.id}">${'•'.repeat(Math.min(a.password_len || 8, 12))}</code>
            <button class="icon-btn" data-eye="${a.id}" title="Show password"
                    aria-label="Show password">👁</button>
          </div>
          <dl class="acc-sess">
            <dt>platform</dt><dd>${esc(a.platform)}</dd>
            <dt>session</dt><dd class="${a.logged_in ? 'ok' : 'off'}">${esc(a.status)}</dd>
            <dt>created</dt><dd>${stamp(a.session_created_at)}</dd>
            <dt>last used</dt><dd>${stamp(a.session_last_used)}</dd>
            <dt>expires</dt><dd>${stamp(a.session_expires_at)}</dd>
            <dt>path</dt><dd title="${esc(a.session_path)}">${esc(a.session_path.split(/[\\/]/).pop())}</dd>
          </dl>
          <div class="acc-foot">
            <span class="acc-meta" title="${esc(a.reason || '')}">${esc((a.reason || '').slice(0, 22))}</span>
            ${a.logged_in
              ? `<button class="icon-btn ok" data-open="${a.id}"
                         title="${a.window_open ? 'Its window is already open' : 'Open this stored session in Chrome'}"
                         aria-label="Open stored session"${a.window_open ? ' disabled' : ''}>🌐</button>`
              : ''}
            ${a.logged_in || a.window_open
              ? `<button class="icon-btn warn" data-logout="${a.id}" title="Log out — closes any window and deletes the stored session">⏻</button>`
              : ''}
            <button class="icon-btn danger" data-del="${a.id}" title="Remove account"
                    aria-label="Remove account">🗑</button>
          </div>
        </article>`).join('')
      : '<p class="hint">No accounts yet — add one below.</p>';

    // select / deselect — selecting also makes it the active account, which is
    // what the Automation panel drives
    $$('#accounts-grid .acc').forEach(card => {
      const toggle = () => {
        const id = Number(card.dataset.id);
        // An account with a valid stored session doesn't need a login, so it
        // can't be queued for one. Log out (⏻) to drop the session first.
        if (card.dataset.live === '1' && !selected.has(id)) {
          toast('Already logged in — log out (⏻) first to log in again.', true);
          return;
        }
        selected.has(id) ? selected.delete(id) : selected.add(id);
        card.classList.toggle('sel', selected.has(id));
        card.setAttribute('aria-pressed', selected.has(id));
        if (selected.has(id)) {
          active.id = id;
          $$('#accounts-grid .acc').forEach(c => c.classList.toggle('active', +c.dataset.id === id));
        } else if (active.id === id) {
          active.id = [...selected][0] || null;
          $$('#accounts-grid .acc').forEach(c => c.classList.toggle('active', +c.dataset.id === active.id));
        }
        syncCount();
      };
      card.addEventListener('click', event => {
        if (event.target.closest('button')) return;   // let the icons do their thing
        toggle();
      });
      card.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); toggle(); }
      });
    });

    // eye — fetch the secret only when actually asked for
    $$('#accounts-grid [data-eye]').forEach(button => {
      button.addEventListener('click', async () => {
        const id = button.dataset.eye;
        const cell = $(`[data-pw="${id}"]`);
        if (cell.dataset.shown === '1') {
          cell.textContent = '•'.repeat(Number(cell.dataset.len || 8));
          cell.dataset.shown = '0';
          button.textContent = '👁';
          button.title = 'Show password';
          return;
        }
        const { password } = await api(`/accounts/${id}/secret`);
        cell.dataset.len = Math.min(password.length, 12);
        cell.textContent = password;
        cell.dataset.shown = '1';
        button.textContent = '🙈';
        button.title = 'Hide password';
      });
    });

    // log out — closes the window and drops the stored session
    // open this account's stored session in its own Chrome window — the yellow
    // (logged-in) cards only, so you open exactly the session you want
    $$('#accounts-grid [data-open]').forEach(button => {
      button.addEventListener('click', async () => {
        const id = button.dataset.open;
        const who = button.closest('.acc').querySelector('.acc-name').textContent.trim();
        button.disabled = true;
        try {
          await api(`/accounts/${id}/open`, { method: 'POST' });
          toast(`🌐 Opened ${who}'s session`);
        } catch (error) {
          toast(error.message, true);
          button.disabled = false;
        }
        loadAccounts();       // refresh so the 🪟 window indicator shows
      });
    });

    $$('#accounts-grid [data-logout]').forEach(button => {
      button.addEventListener('click', async () => {
        const id = button.dataset.logout;
        const who = button.closest('.acc').querySelector('.acc-name').textContent.trim();
        if (!confirm(`Log ${who} out?\n\nCloses its window and deletes the stored session — it will need a fresh login.`)) return;
        try {
          await api(`/accounts/${id}/logout`, { method: 'POST' });
          toast(`⏻ ${who} logged out`);
        } catch (error) { toast(error.message, true); }
        loadAccounts();
      });
    });

    // remove
    $$('#accounts-grid [data-del]').forEach(button => {
      button.addEventListener('click', async () => {
        const id = button.dataset.del;
        const card = button.closest('.acc');
        const who = card.querySelector('.acc-name').textContent.trim();
        if (!confirm(`Remove ${who}? Its saved Instagram session is deleted too.`)) return;
        await api(`/accounts/${id}`, { method: 'DELETE' });
        selected.delete(Number(id));
        toast('Account removed');
        loadAccounts();
      });
    });

    syncCount();
    showDefaultProfile();
  } catch (error) {
    grid.innerHTML = `<p class="hint">Could not load accounts: ${esc(error.message)}</p>`;
  }
}

$('#add-account').addEventListener('submit', async event => {
  event.preventDefault();
  const body = {
    label: $('#acc-label').value.trim(),
    login: $('#acc-login').value.trim(),
    password: $('#acc-pass').value
  };
  try {
    await api('/accounts', { method: 'POST', body });
    toast('Account added');
    loadAccounts();
  } catch (error) {
    toast(error.message, true);
  } finally {
    // always empty, even when the add failed — the toast says what went wrong
    $('#acc-label').value = $('#acc-login').value = $('#acc-pass').value = '';
    $('#acc-label').focus();
  }
});

bind('#btn-acc-refresh', null, loadAccounts);

/* Close a session window by hand and the card should stop being yellow without
   you having to hit Refresh. Only polls while the panel is actually on screen. */
setInterval(() => {
  if (!$('#panel-accounts').hidden) loadAccounts();
}, 4000);

/* Hands the ticked accounts to the Automation panel and runs the sequence
   there — the credentials get typed in step 4, in front of you. */
bind('#btn-acc-login', '#out-accounts', async () => {
  await handoff([...selected]);
});

/* New Login — drop the stored session, then hand over to Automation for a
   fresh login you can watch. */
bind('#btn-acc-newlogin', '#out-accounts', async () => {
  const id = active.id;
  if (!id) throw new Error('Pick an account first.');
  const a = accountById(id);
  if (!confirm(`New login for ${a.account_name}?\n\nDeletes its stored session, then runs the login sequence in the Automation panel.`)) return;
  await handoff([id], { fresh: true });
});

/* Check Login Status — two phases:
   1. Read EVERY stored session straight off disk (no browser, no login) to
      find which profiles hold a valid session. Yellow cards are logged in.
   2. For each account found logged in, OPEN its Chrome profile and load
      instagram.com. The stored session comes up automatically — no credentials
      typed — so you SEE it's really logged in. Logged-out accounts get no
      window, so this never opens one browser per account just to check. */
const IG_HOME = 'https://www.instagram.com/';

bind('#btn-acc-check', '#out-accounts', async out => {
  show(out, 'Reading every stored session…');
  const data = await api('/accounts/sessions/check', { method: 'POST' });
  const rows = data.accounts.map(a =>
    `${a.logged_in ? '🟡' : '⚪'} ${a.account_name.padEnd(16)} ${a.status.padEnd(11)} ${a.reason}`);

  const live = data.accounts.filter(a => a.logged_in);
  const head = `${data.logged_in}/${data.checked} accounts have a valid stored session.\n\n${rows.join('\n')}`;

  if (!live.length) {
    show(out, `${head}\n\nRead from the session files — nothing logged in, so no window opened.`);
    toast(`0/${data.checked} logged in`);
    loadAccounts();
    return;
  }

  const log = [head, '',
    `Opening ${live.length} logged-in session${live.length > 1 ? 's' : ''} at instagram.com ` +
    `— the stored session loads itself, no login:`];
  show(out, log.join('\n'));

  // One profile locks to one Chrome process, so open them one at a time.
  const opened = [];
  for (const a of live) {
    try {
      const r = await api('/automation/open', { method: 'POST', body: { account_id: a.id, url: IG_HOME } });
      opened.push(a.account_name);
      log.push(`✓ ${a.account_name}: ${r.reused ? 'already open' : 'opened'} — loaded ${IG_HOME}`);
    } catch (error) {
      log.push(`✕ ${a.account_name}: ${error.message}`);
    }
    show(out, log.join('\n'), opened.length < live.length);
  }

  show(out, `${log.join('\n')}\n\nEach open window is on instagram.com, logged in from its stored session.`);
  toast(`${data.logged_in}/${data.checked} logged in · ${opened.length} opened`);
  loadAccounts();
});

/* Clear All Sessions — wipes every session file, including the default
   profile that "Open Chrome" uses when no account is active. */
bind('#btn-acc-clearsess', '#out-accounts', async out => {
  if (!confirm(
    'Delete every stored session?\n\n' +
    '• every account profile\n' +
    '• the default profile "Open Chrome" uses\n\n' +
    'Chrome will close. Accounts and passwords are kept — only the logins go.'
  )) return;
  const data = await api('/accounts/sessions/clear', { method: 'POST' });
  show(out,
    `${data.detail}\n\n` +
    data.results.map(r =>
      `${r.removed ? '✓' : '✕'} ${r.who}\n    ${r.path}` +
      `${r.existed ? '' : '  (was already empty)'}${r.error ? `\n    ${r.error}` : ''}`
    ).join('\n'));
  toast(`🧹 ${data.cleared} session(s) deleted`);
  loadAccounts();
});

/* The default profile isn't an account, so it has no card — but it does hold a
   login, and an invisible one is exactly what made "cleared" look broken. */
async function showDefaultProfile() {
  const el = $('#acc-default');
  if (!el) return;
  try {
    const { default_profile: d } = await api('/accounts/sessions');
    el.className = `banner ${d.logged_in ? 'bad' : ''}`;
    el.textContent = d.logged_in
      ? `⚠ Default profile is LOGGED IN (${d.session_path}) — "Open Chrome" with no active account ` +
        `opens this session. Clear All Sessions removes it.`
      : `Default profile (${d.session_path}): Logged Out.`;
  } catch { el.textContent = ''; }
}

/* ── Tools ───────────────────────────────────────────────────── */
async function loadTools() {
  const grid = $('#tools-grid');
  try {
    const { tools } = await api('/agent/tools');
    grid.innerHTML = tools.map(t => `
      <article class="card">
        <h3>${esc(t.name)}</h3>
        <p>${esc(t.description)}</p>
        <code>${esc(t.id)}</code>
      </article>`).join('');
  } catch (error) {
    grid.innerHTML = `<p class="hint">Could not load tools: ${esc(error.message)}</p>`;
  }
}

/* ── Data ────────────────────────────────────────────────────── */
/* ── Data panel: two views — the username · bio list every scrape fills
   (usernames_bio.json), and the categorized set (usernames_categories.json), a
   username · bio · category table you segment by ticking categories. Both read
   MinIO, their source of truth. The categorized object is READ-ONLY: the pipeline
   that wrote it is gone, so this view segments existing data, it doesn't make more. ── */
let dataTab = 'usernames';
let dataCatRows = [];              // cached category rows, so filtering doesn't refetch
let dataCatHidden = new Set();     // categories unticked in the right panel ("" = uncategorized);
                                   // empty = every category ticked, so all rows show

function setDataTab(tab) {
  dataTab = tab;
  $$('.data-tab').forEach(b => b.classList.toggle('on', b.dataset.dtab === tab));
  loadRecords();
}
$$('.data-tab').forEach(b => b.addEventListener('click', () => setDataTab(b.dataset.dtab)));

/* Copy-to-clipboard for any @name chip inside the Data panel. */
function wireDataCopy() {
  $$('#records [data-copy-target]').forEach(button =>
    button.addEventListener('click', async () => {
      const name = button.dataset.copyTarget || '';
      if (!name) return;
      try { await navigator.clipboard.writeText(name); toast(`Copied ${name}`); }
      catch { toast('Could not copy the name.', true); }
    }));
}

async function loadRecords() {
  return dataTab === 'categorized' ? loadCategorized() : loadUsernames();
}

/* usersBio: scraped handles from usernames_bio.json that are NOT yet categorized —
   a handle drops off this list as soon as it appears in usernames_categories.json,
   so the two Data views partition the set instead of showing the same handle twice. */
async function loadUsernames() {
  const list = $('#records');
  try {
    const { records, total } = await api('/data/usernames-bio?exclude_categorized=true');
    const rows = records || [];
    const done = Math.max(0, (total ?? rows.length) - rows.length);
    list.innerHTML = rows.length
      ? `<section class="data-section">
          <div class="name-count">Users with bio (not yet categorized) - ${rows.length}${
            done ? ` <span class="nc-sub">· ${done} categorized (see “Categorized data”)</span>` : ''}</div>
          <ol class="usersbio-list">
            ${rows.map((r, i) => {
              const name = String(r.username || '').trim() || '(untitled)';
              const bio = String(r.bio ?? '').replace(/\s+/g, ' ').trim();
              const hasBio = bio && bio.toLowerCase() !== 'unknown';
              return `<li class="ub-row">
                  <span class="name-num">${i + 1}</span>
                  <button type="button" class="name-chip ub-chip" data-copy-target="${esc(name)}" title="Copy ${esc(name)}">${esc(name)}</button>
                  <span class="ub-bio${hasBio ? '' : ' ub-bio-empty'}">${hasBio ? esc(bio) : 'no bio scraped yet'}</span>
                  <button type="button" class="name-del" data-del-name="${esc(name)}" aria-label="Delete ${esc(name)}" title="Delete ${esc(name)}">X</button>
                </li>`;
            }).join('')}
          </ol>
        </section>`
      : `<p class="hint">${done
            ? `No uncategorized bios left — all ${done} handle(s) have a category, so they show under “Categorized data”.`
            : 'No bios yet — scrape profiles in the Scrapping panel.'}</p>`;

    $$('#records [data-del-name]').forEach(button =>
      button.addEventListener('click', async () => {
        await api(`/data/usernames-bio/${encodeURIComponent(button.dataset.delName)}`, { method: 'DELETE' });
        toast('Removed');
        loadRecords();
      }));
    wireDataCopy();
  } catch (error) {
    list.innerHTML = `<p class="hint">Could not load users with bio: ${esc(error.message)}</p>`;
  }
}

/* Categorized view: the whole usernames_categories.json set, segmented by ticking
   categories. Read-only — there is no "categorize" button any more, this reads the
   dataset the removed pipeline left behind. */
async function loadCategorized() {
  const list = $('#records');
  list.innerHTML = '<p class="hint">Loading…</p>';
  try {
    const { records } = await api('/data/usernames-categories');
    dataCatRows = records || [];
    renderCategorized();
  } catch (error) {
    list.innerHTML = `<p class="hint">Could not load categorized data: ${esc(error.message)}</p>`;
  }
}

function renderCategorized() {
  mountHandlesView($('#records'), dataCatRows, {
    categoryList: true,
    hidden: dataCatHidden,
    empty: 'No categorized data in usernames_categories.json yet.',
  });
}

/* ── Reusable handles view ─────────────────────────────────────────
   A filterable username·bio table (≈80%) beside a side panel (≈20%). Used by the
   Data panel's Categorized view AND the Campaign wizard's Users step — so it's
   scoped to `container` with CLASSES (not ids), letting two mounts coexist without
   colliding.

   opts.categoryList : add the Category column and a searchable, tickable category
                 list built from the rows themselves (unticking one removes its
                 rows; all ticked by default). The categories are whatever the data
                 holds — the closed taxonomy left with the pipeline that wrote it.
   opts.hidden : the Set of unticked category keys the caller owns, so the choice
                 survives a re-mount.
   opts.onChange(rows) fires with the active rows whenever the filters change.
   opts.empty : the no-data message. */
function mountHandlesView(container, rows, opts = {}) {
  if (!container) return;
  const onChange = opts.onChange || (() => {});
  const rowsAll = rows || [];
  const q = sel => container.querySelector(sel);
  const externalSearch = opts.searchInput || null;   // when given, the text filter lives outside the box
  const rowFilter = opts.rowFilter || (r => r);      // extra positional filter (e.g. the wizard's range)
  const withCats = opts.categoryList === true;
  const hidden = opts.hidden || new Set();

  if (!rowsAll.length) {
    container.innerHTML = `<p class="hint">${esc(opts.empty || 'No data yet.')}</p>`;
    onChange([]);
    return { rerender: () => {} };
  }

  const catKeyOf = r => String(r.category || '').trim();   // "" for an empty category
  const catLabelOf = key => key || '(uncategorized)';
  const catCounts = () => {
    const m = new Map();
    rowsAll.forEach(r => { const k = catKeyOf(r); m.set(k, (m.get(k) || 0) + 1); });
    return [...m.entries()].map(([key, n]) => ({ key, label: catLabelOf(key), n }))
      .sort((a, b) => b.n - a.n || a.label.localeCompare(b.label));
  };
  // Ticked categories, then the external row filter (used count + range) → the
  // active set shown & selected.
  const catFiltered = () => (withCats ? rowsAll.filter(r => !hidden.has(catKeyOf(r))) : rowsAll);
  const activeSet = () => rowFilter(catFiltered());

  // Accordion side (opt-in): single-open collapsible rows gathering the filters.
  // `manual` stages them so they apply only when the Apply button is pressed.
  const accordion = opts.accordion === true;
  const manual = opts.manual === true;
  const sideSections = opts.sideSections || [];
  const sec = (id, title, open, body) =>
    `<div class="dcv-sec ${open ? 'open' : ''}" data-sec="${esc(id)}">
       <button type="button" class="dcv-sec-head">${esc(title)}<span class="dcv-chev" aria-hidden="true"></span></button>
       <div class="dcv-sec-body">${body}</div>
     </div>`;
  const applyBtn = (accordion && manual)
    ? '<button type="button" class="dcv-apply">Apply filters</button>' : '';
  const searchPlaceholder = withCats ? 'Filter handle, bio or category…' : 'Filter handle or bio…';
  const catActions = `<div class="data-cat-side-actions">
      <button type="button" class="dcv-all" title="Tick every category">All</button>
      <button type="button" class="dcv-none" title="Untick every category">None</button>
    </div>`;
  const catSearch = '<input type="search" class="data-cat-list-search" placeholder="Search categories…" />';
  const catList = '<div class="data-cat-list"></div>';
  // Categories inside the accordion: All / None sit inline in FRONT of the category
  // search, then the list. Collapsed by default — expand it to pick categories.
  const catSection = withCats
    ? sec('categories', 'Categories', false,
          `<div class="cat-list-top">${catActions}${catSearch}</div>${catList}`)
    : '';
  const sideHtml = accordion
    ? sec('filter', 'Filter', false,
          `<input type="search" class="data-cat-search" placeholder="${searchPlaceholder}" />`)
      + sideSections.map(s => sec(s.id, s.title, s.open !== false, '')).join('')
      + catSection
      + applyBtn
    : (withCats
        ? `<div class="data-cat-side-head"><span>Categories</span>${catActions}</div>${catSearch}${catList}`
        : '');

  container.innerHTML = `
    <div class="data-cat-layout${sideHtml ? '' : ' data-cat-layout--full'}">
      <div class="data-cat-main">
        ${(accordion || externalSearch) ? '' : `<input type="search" class="data-cat-search" placeholder="Filter rows by ${withCats ? 'handle, bio or category' : 'handle or bio'}…" />`}
        <div class="name-count data-cat-count"></div>
        <div class="data-cat-wrap"></div>
      </div>
      ${sideHtml ? `<aside class="data-cat-side ${accordion ? 'dcv-accordion' : ''}">${sideHtml}</aside>` : ''}
    </div>`;

  const searchEl = externalSearch || q('.data-cat-search');
  // What the table shows: the active set (ticks + range/used) narrowed by the text
  // box. The selection is the same — what you see is what gets used.
  const viewSet = () => {
    const query = ((searchEl && searchEl.value) || '').trim().toLowerCase();
    const base = activeSet();
    const fields = withCats ? ['username', 'bio', 'category'] : ['username', 'bio'];
    return query
      ? base.filter(r => fields.some(f => String(r[f] || '').toLowerCase().includes(query)))
      : base;
  };
  const emitSelection = () => onChange(viewSet());

  /* The tickable category list, built from the rows themselves. */
  function renderList() {
    const box = q('.data-cat-list');
    if (!box) return;
    const query = (q('.data-cat-list-search')?.value || '').trim().toLowerCase();
    const items = catCounts().filter(c => !query || c.label.toLowerCase().includes(query));
    box.innerHTML = items.length
      ? items.map(c => {
          const on = !hidden.has(c.key);                    // ticked unless explicitly hidden
          return `<label class="data-cat-item ${on ? 'on' : ''}">
            <input type="checkbox" data-cat-key="${esc(c.key)}" ${on ? 'checked' : ''} />
            <span class="dc-name" title="${esc(c.label)}">${esc(c.label)}</span>
            <span class="dc-n">${c.n}</span>
          </label>`;
        }).join('')
      : '<div class="data-cat-empty-list">No categories match.</div>';
    box.querySelectorAll('input[data-cat-key]').forEach(cb =>
      cb.addEventListener('change', () => {
        const key = cb.getAttribute('data-cat-key');
        if (cb.checked) hidden.delete(key); else hidden.add(key);
        cb.closest('.data-cat-item')?.classList.toggle('on', cb.checked);
        if (!manual) { renderTable(); emitSelection(); }    // live: reflect the tick at once
      }));
  }

  function renderTable() {
    const wrap = q('.data-cat-wrap');
    const countEl = q('.data-cat-count');
    const view = viewSet();                                 // ticks + range/used + text
    const cell = v => esc(String(v ?? '').replace(/\s+/g, ' ').trim());
    if (countEl) {
      const h = withCats ? hidden.size : 0;
      const note = h ? ` · ${h} categor${h === 1 ? 'y' : 'ies'} hidden` : '';
      countEl.textContent = `${view.length} of ${rowsAll.length} handle${rowsAll.length === 1 ? '' : 's'}${note}`;
    }
    wrap.innerHTML = view.length
      ? `<table class="data-cat-table">
          <thead><tr><th>#</th><th>Username</th><th>Bio</th>${withCats ? '<th>Category</th>' : ''}</tr></thead>
          <tbody>
            ${view.map((r, i) => `<tr>
              <td class="dc-num">${i + 1}</td>
              <td class="dc-user"><button type="button" class="name-chip" data-copy-target="${esc(r.username || '')}" title="Copy ${esc(r.username || '')}">${esc(r.username || '')}</button>${
                Number(r.used_count) > 0 ? `<span class="dc-used" title="Used ${Number(r.used_count)}× in campaigns (staged/sent)">×${Number(r.used_count)}</span>` : ''}</td>
              <td class="dc-bio" title="${cell(r.bio)}">${r.bio ? cell(r.bio) : '<span class="dc-empty">—</span>'}</td>
              ${withCats ? `<td class="dc-cat">${r.category ? `<span class="dc-cat-tag">${cell(r.category)}</span>` : '<span class="dc-empty">—</span>'}</td>` : ''}
            </tr>`).join('')}
          </tbody>
        </table>`
      : `<p class="hint">${withCats ? 'No handles to show — tick a category on the right.' : 'No handles match the current filters.'}</p>`;
    container.querySelectorAll('.data-cat-wrap [data-copy-target]').forEach(b =>
      b.addEventListener('click', async () => {
        const name = b.dataset.copyTarget || '';
        if (!name) return;
        try { await navigator.clipboard.writeText(name); toast(`Copied ${name}`); }
        catch { toast('Could not copy the name.', true); }
      }));
  }

  const apply = () => { renderTable(); emitSelection(); };   // render the table + push the selection

  // The row filter is view-only. In manual mode it's staged (applied on Apply);
  // otherwise live. `oninput` (not addEventListener) so an EXTERNAL input that
  // survives re-mounts keeps just one handler.
  if (searchEl && !manual) searchEl.oninput = renderTable;
  q('.data-cat-list-search')?.addEventListener('input', renderList);   // list filter is always live
  q('.dcv-all')?.addEventListener('click', () => { hidden.clear(); renderList(); if (!manual) apply(); });
  q('.dcv-none')?.addEventListener('click', () => { catCounts().forEach(c => hidden.add(c.key)); renderList(); if (!manual) apply(); });

  // Accordion: let each caller section fill its body (with a rerender hook), make
  // headers SINGLE-open (opening one closes the others), and wire the Apply button.
  if (accordion) {
    const api = { rerender: apply };
    sideSections.forEach(s => {
      const body = q(`.dcv-sec[data-sec="${s.id}"] .dcv-sec-body`);
      if (body && typeof s.build === 'function') s.build(body, api);
    });
    container.querySelectorAll('.dcv-sec-head').forEach(head =>
      head.addEventListener('click', () => {
        const target = head.closest('.dcv-sec');
        const wasOpen = target.classList.contains('open');
        container.querySelectorAll('.dcv-sec').forEach(s => s.classList.remove('open'));
        if (!wasOpen) target.classList.add('open');
      }));
    q('.dcv-apply')?.addEventListener('click', () => (opts.onApply ? opts.onApply(apply) : apply()));
  }

  renderList();
  renderTable();
  emitSelection();
  return { rerender: apply, apply };
}

bind('#btn-refresh', null, loadRecords);


/* ── Boot ────────────────────────────────────────────────────── */
refreshHealth();
setInterval(refreshHealth, 15000);
// Messages start with ONE empty draft tab — saved drafts load only on the
// 🗄️ Database click (Setup Campaign → Messages), not automatically on boot.
// Faster than the health poll: these buttons must track a job that starts or
// ends in another panel, not lag 15s behind it.
refreshControls();
setInterval(refreshControls, 2000);
applyPanelState();               // grey out any panels disabled in Config → 🧭 Panels
const initial = location.hash.slice(1);
if ($(`#panel-${initial}`) && !loadDisabledPanels().has(initial)) openPanel(initial);
