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
