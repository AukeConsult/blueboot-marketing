/* crm-common.js -- shared helpers for the Blueboot CRM pages.
 * Loaded as a plain <script>; everything below is global (window-scoped).
 *   <script src="js/crm-common.js"></script>
 */

// Base URL of the CRM API (crmApi Cloud Function).
const BASE = 'https://us-central1-blueboot-market.cloudfunctions.net/crmApi';

// HTML-escape a string for safe interpolation into innerHTML.
function escapeHtml(s){
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// Human-readable byte size.
function fmtSize(b){
  b = Number(b);
  if(!b && b !== 0) return '';
  if(b < 1024) return b + ' B';
  if(b < 1048576) return (b/1024).toFixed(1) + ' KB';
  return (b/1048576).toFixed(1) + ' MB';
}

// ISO timestamp -> "YYYY-MM-DD HH:MM".
function fmtDateTime(iso){ return iso ? iso.replace('T',' ').slice(0,16) : ''; }

// Set a status line on an element by id. kind: 'ok' | 'err' | falsy (muted).
function setStatusEl(id, msg, kind, html){
  const el = document.getElementById(id);
  if(!el) return;
  el.className = 'small mt-2 ' + (kind === 'err' ? 'text-danger' : kind === 'ok' ? 'text-success' : '');
  el.style.color = kind ? '' : 'var(--bb-muted)';
  if(html){ el.innerHTML = msg; } else { el.textContent = msg; }
}

// fetch with a hard timeout (rejects if the API is unreachable).
async function fetchWithTimeout(url, options = {}, ms = 8000){
  const timeout = new Promise((_, reject) =>
    setTimeout(() => reject(new Error('Request timed out — API may be unreachable')), ms));
  return Promise.race([fetch(url, options), timeout]);
}

// fetch + parse JSON; throws Error on HTTP error or {status:'error'}.
async function fetchJSON(url, options = {}){
  const r = await fetch(url, options);
  let d = {};
  try { d = await r.json(); } catch(_){ /* non-JSON */ }
  if(!r.ok || d.status === 'error') throw new Error(d.message || ('HTTP ' + r.status));
  return d;
}

// Poll a CRM job until done/error. Calls onDone(result)/onError(message).
async function pollJob(jobId, { onDone, onError, intervalMs = 2000, tries = 60 } = {}){
  for(let i = 0; i < tries; i++){
    await new Promise(res => setTimeout(res, intervalMs));
    try{
      const r = await fetch(BASE + '/api/crm/status/' + jobId);
      const j = await r.json();
      if(j.status === 'done'){ onDone && onDone(j.result || {}, j); return; }
      if(j.status === 'error'){ onError && onError(j.error || 'unknown', j); return; }
    }catch(_){ /* keep polling */ }
  }
  onError && onError('timed out waiting for job ' + jobId);
}

// --- date/time formatters (named variants; pages differ in what they show) ---

// time only, e.g. "14:05:09"  (em-dash for empty)
function fmtTime(iso){
  if(!iso) return '—';
  return new Date(iso).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
}

// short date + time, e.g. "6/5/26, 2:05 PM"
function fmtDateTimeShort(iso){
  if(!iso) return '—';
  return new Date(iso).toLocaleString([], {dateStyle:'short', timeStyle:'short'});
}

// medium date, e.g. "Jun 5, 2026"
function fmtDateMedium(iso){
  if(!iso) return '—';
  return new Date(iso).toLocaleDateString([], {dateStyle:'medium'});
}

// elapsed between two ISO timestamps, e.g. "12s" or "3m 4s"
function elapsed(a, b){
  if(!a || !b) return '';
  const s = Math.round((new Date(b) - new Date(a)) / 1000);
  return s < 60 ? s + 's' : Math.floor(s/60) + 'm ' + (s % 60) + 's';
}

// Turn a raw API/Google error string into a short, friendly HTML snippet.
function prettyError(msg){
  const m = String(msg || 'Unknown error');
  if(/has not been used in project|accessNotConfigured|drive\.googleapis\.com/i.test(m)){
    const url = (m.match(/https?:\/\/console\.developers\.google\.com[^\s"'\]]+/) || [])[0]
             || 'https://console.developers.google.com/apis/api/drive.googleapis.com';
    return '<strong>Google Drive API is not enabled.</strong> '
         + 'Enable it, wait a minute, then refresh — '
         + '<a href="' + url + '" target="_blank">enable Drive API</a>.';
  }
  if(/insufficientPermissions|caller does not have permission|\bpermission\b/i.test(m)){
    return '<strong>Permission denied.</strong> Share the Drive folder (Editor) with the '
         + 'backend service account — click <em>Check access</em> to see which account that is.';
  }
  if(/No .*folder configured/i.test(m)){
    return 'No Drive folder configured yet — set it on the <a href="settings.html">Settings</a> page.';
  }
  if(/file not found/i.test(m)){
    return '<strong>Folder not found or not shared.</strong> Check the folder ID, and share the '
         + 'Drive folder (Editor) with the backend service account — use <em>Check access</em> '
         + 'to see which account and confirm read/write.';
  }
  if(/\bnot found\b|\b404\b/i.test(m)) return 'Not found.';
  if(/timed out|unreachable/i.test(m)) return 'The API did not respond — check your connection and try again.';
  return escapeHtml(m.length > 300 ? m.slice(0, 300) + '…' : m);
}

// --- shared top navigation (single source of truth) -------------------------
// Pages include  <div id="nav"></div>  and load this file; the nav renders
// automatically with the active link highlighted from the current URL.
const NAV_LINKS = [
  { href: 'campaigns.html',     icon: 'ti-speakerphone',      label: 'Campaigns',
    match: ['campaigns.html', 'campaign.html', 'campaign-edit.html'] },
  { href: 'crm-bp.html',        icon: 'ti-server-2',          label: 'CRM' },
  { href: 'jobs.html',          icon: 'ti-list-check',        label: 'Jobs' },
  { dropdown: 'data-sources',   icon: 'ti-database',          label: 'Data collect',
    children: [
      { href: 'statistics.html',    icon: 'ti-chart-bar', label: 'Statistics' },
      { href: 'filter-facets.html', icon: 'ti-filter',    label: 'Filter facets' },
    ]},
  { href: 'gdisk.html',         icon: 'ti-brand-google-drive',label: 'Drive Folder' },
  { href: 'mailbox.html',       icon: 'ti-inbox',             label: 'Mailbox' },
  { dropdown: 'docs',  match: ['doc-viewer.html'],           icon: 'ti-book',              label: 'Documentation',
    children: [
      { href: 'doc-viewer.html?doc=installation',        icon: 'ti-download',      label: 'Installation' },
      { href: 'doc-viewer.html?doc=user-guide',          icon: 'ti-user', label: 'User guide' },
      { href: 'doc-viewer.html?doc=system-architecture', icon: 'ti-topology-star-3', label: 'System architecture' },
      { href: 'doc-viewer.html?doc=backend-functions',   icon: 'ti-terminal',      label: 'Backend functions' },
      { href: 'doc-viewer.html?doc=pipeline-config',     icon: 'ti-settings-2',    label: 'Pipeline config' },
      { href: 'doc-viewer.html?doc=ai-assistance',       icon: 'ti-brain',         label: 'AI assistance' },
    ]},
  { href: 'settings.html',      icon: 'ti-settings',          label: 'Settings' },
];

function renderNav(targetId){
  const el = document.getElementById(targetId || 'nav');
  if(!el) return;
  const cur = (location.pathname.split('/').pop() || 'index.html') || 'index.html';
  const links = NAV_LINKS.map(l => {
    if (l.dropdown) {
      // Dropdown group
      const childActive = l.children.some(c => (c.match || [c.href]).includes(cur));
      const items = l.children.map(c => {
        const a = (c.match || [c.href]).includes(cur) ? ' active' : '';
        return '<a href="' + c.href + '" class="nav-dropdown-item' + a + '">'
             + '<i class="ti ' + c.icon + '"></i>' + c.label + '</a>';
      }).join('');
      return '<div class="nav-dropdown' + (childActive ? ' active' : '') + '">'
           + '<button class="nav-link nav-dropdown-toggle" onclick="this.parentElement.classList.toggle(&quot;open&quot;)">'
           + '<i class="ti ' + l.icon + '"></i>' + l.label
           + '<i class="ti ti-chevron-down" style="font-size:.7rem;margin-left:.2rem"></i></button>'
           + '<div class="nav-dropdown-menu">' + items + '</div></div>';
    }
    const active = (l.match || [l.href]).includes(cur) ? ' active' : '';
    return '<a href="' + l.href + '" class="nav-link' + active + '">'
         + '<i class="ti ' + l.icon + '"></i>' + l.label + '</a>';
  }).join('');
  el.outerHTML = '<nav class="bb-nav">'
    + '<a href="index.html" class="brand"><i class="ti ti-bolt"></i>Blueboot CRM</a>'
    + '<div class="nav-links">' + links + '</div></nav>';
  // Close dropdown when clicking outside
  document.addEventListener('click', e => {
    document.querySelectorAll('.nav-dropdown.open').forEach(d => {
      if (!d.contains(e.target)) d.classList.remove('open');
    });
  }, { once: false, capture: true });
}

// auto-render on any page that has a #nav placeholder
(function(){
  function go(){ if(document.getElementById('nav')) renderNav(); }
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', go);
  else go();
})();

// Back button: go to the previous page if there is history, otherwise let the
// link's href act as a fallback. Use as: <a href="index.html" onclick="return goBack()">
function goBack(){
  if(history.length > 1){ history.back(); return false; }
  return true;
}
