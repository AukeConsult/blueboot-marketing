'use strict';
// ── Activity tab: contacts with their last two activities shown in full ──────
// Uses the contacts already filtered by the header filters (_visibleRows), newest
// activity first. An "activity" is a comment_history entry (comment, status change,
// e-mail in / out).

let _actPage = 0;

function _actPageSize() {
  const v = parseInt(document.getElementById('act-page-size')?.value, 10);
  return v > 0 ? v : 10;
}

function _actLast(r, n) {
  // Owner / importance / follow-up date / status changes are bookkeeping (already visible on the card), not activity: skip them
  return [...(r.comment_history || [])]
    .filter(h => !['OWNER', 'IMPORTANCE', 'FOLLOWUP', 'STATUS'].includes(String(h.type || '').toUpperCase()))
    .sort((a, b) => new Date(b.date || 0) - new Date(a.date || 0))
    .slice(0, n);
}

function _actBodyText(h) {
  if (h.body_text) return String(h.body_text).trim();
  if (h.body_html) {
    const el = document.createElement('div');
    el.innerHTML = String(h.body_html).replace(/<(br|\/p|\/div|\/li)\s*\/?>/gi, '\n');
    return (el.textContent || '').trim();
  }
  return '';
}

function _actEntryHtml(h) {
  const d = h.date ? new Date(h.date).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : '—';
  const isIn = h.type === 'EMAIL_IN', isOut = h.type === 'EMAIL_OUT', isSent = h.type === 'MAIL_SENT';
  const badge = isIn ? '<span class="hist-type-badge hist-type-in">IN</span>'
              : isOut ? '<span class="hist-type-badge hist-type-out">OUT</span>'
              : isSent ? '<span class="hist-type-badge hist-type-out">SENT</span>'
              : `<span class="hist-type-badge act-type-other">${escapeHtml((h.type || 'NOTE').toString())}</span>`;
  const meta = (isIn || isOut)
    ? [h.from ? `<span class="hist-detail-lbl">From</span> ${escapeHtml(h.from)}` : '',
       h.to   ? `<span class="hist-detail-lbl">To</span> ${escapeHtml(h.to)}` : ''].filter(Boolean).join(' &nbsp; ')
    : (h.user ? `<span class="hist-detail-lbl">By</span> ${escapeHtml(h.user)}` : '');
  const isMail = isIn || isOut || isSent;
  const body = _actBodyText(h);
  const foldable = (isOut || isSent) && !!body;      // sent mail: subject only, click to show the body
  const txt = escapeHtml(h.text || '');
  return `<div class="act-entry ${isIn ? 'hist-entry-email-in' : isOut ? 'hist-entry-email-out' : isSent ? 'hist-entry-email-out' : ''}">
    <div class="act-entry-head${foldable ? ' act-fold' : ''}"${foldable ? ' onclick="actToggleBody(this)" title="Click to show / hide the mail"' : ''}>${badge}<span class="hist-entry-date">${escapeHtml(d)}</span>
      ${isSent && h.user ? `<span class="small text-muted act-entry-by">${escapeHtml(h.user)}</span>` : ''}
      ${isMail ? `<span class="act-entry-subject" title="${txt}">${txt}</span>` : ''}${foldable ? '<i class="ti ti-chevron-down act-chev"></i>' : ''}</div>
    ${meta && !isSent ? `<div class="small text-muted">${meta}</div>` : ''}
    ${isMail ? '' : `<div class="act-entry-text">${txt}</div>`}
    ${body ? `<div class="act-entry-body"${foldable ? ' style="display:none"' : ''}>${escapeHtml(body)}</div>` : ''}
  </div>`;
}

function renderActivity() {
  const host = document.getElementById('act-list');
  if (!host) return;
  const hideEmpty = document.getElementById('act-hide-empty')?.checked;
  let rows = (typeof _visibleRows !== 'undefined' ? _visibleRows : []).map(r => ({ r, last: _actLast(r, 2) }));
  if (hideEmpty) rows = rows.filter(x => x.last.length);
  rows.sort((a, b) => new Date(b.last[0]?.date || 0) - new Date(a.last[0]?.date || 0));

  const size = _actPageSize();
  const pages = Math.max(1, Math.ceil(rows.length / size));
  if (_actPage >= pages) _actPage = pages - 1;
  const from = _actPage * size;
  const slice = rows.slice(from, from + size);

  document.getElementById('act-tab-count').textContent = rows.length || '';
  document.getElementById('act-count-label').textContent = rows.length
    ? `${from + 1}–${from + slice.length} of ${rows.length} contacts` : '';
  document.getElementById('act-prev').disabled = _actPage <= 0;
  document.getElementById('act-next').disabled = _actPage >= pages - 1;
  document.getElementById('act-page-label').textContent = `${_actPage + 1} / ${pages}`;

  if (!slice.length) {
    host.innerHTML = '<div class="text-center py-4 small text-muted">No contacts with activity match the current filters.</div>';
    return;
  }
  host.innerHTML = slice.map(({ r, last }) => _actCardHtml(r, last)).join('');
}

function _actCardHtml(r, last) {
  const gidx = allRows.indexOf(r);
  const st = r.followup_status || '';
  const radios = FU_STATUSES.map((o, i) => {
    const id = `act-st-${gidx}-${i}`;
    return `<input type="radio" class="btn-check" name="act-st-${gidx}" id="${id}" value="${escapeHtml(o.value)}"
              data-act-gidx="${gidx}"${o.value === st ? ' checked' : ''} onchange="actSetStatus(this)">
            <label class="btn btn-outline-secondary act-st-btn" for="${id}">${o.value ? escapeHtml(o.label) : 'None'}</label>`;
  }).join('');
  const due = r.followup_date
    ? `<span class="small ${dueDateClass(r.followup_date)}">Follow-up ${escapeHtml(r.followup_date)}</span>` : '';
  return `<div class="act-card" id="act-card-${gidx}">
    <div class="act-card-head">
      <div>
        <span class="fw-600">${escapeHtml(r.name || r.email || '—')}</span>
        ${r.title ? `<span class="small text-muted ms-1">${escapeHtml(r.title)}</span>` : ''}
        <div class="small text-muted">${escapeHtml([r.company, r.email].filter(Boolean).join(' · '))}</div>
      </div>
      <div class="ms-auto d-flex align-items-center gap-2 flex-wrap">
        ${due}
        <button class="btn btn-sm btn-outline-secondary py-0" onclick="openActivityContact(${gidx})">
          <i class="ti ti-external-link me-1"></i>Open</button>
      </div>
    </div>
    <div class="btn-group btn-group-sm act-st-group" role="group" aria-label="Follow-up status">${radios}</div>
    ${last.length ? last.map(_actEntryHtml).join('')
                  : '<div class="small text-muted py-1">No activity yet.</div>'}
  </div>`;
}

// Save through the same path as the Contacts tab, then refresh only this card in place
// (the new status entry appears as the latest activity without the list reshuffling).
async function actSetStatus(el) {
  const gidx = parseInt(el.dataset.actGidx, 10);
  const group = document.querySelectorAll(`input[name="act-st-${gidx}"]`);
  group.forEach(r => { r.disabled = true; });
  // saveField reads dataset + value and syncs the Contacts tab / side panel inputs itself
  const proxy = { dataset: { gidx: String(gidx), field: 'followup_status' }, value: el.value, closest: () => null };
  try { await saveField(proxy); } finally { group.forEach(r => { r.disabled = false; }); }
  const row = allRows[gidx], card = document.getElementById('act-card-' + gidx);
  if (row && card) card.outerHTML = _actCardHtml(row, _actLast(row, 2));
}

function actToggleBody(head) {
  const entry = head.closest('.act-entry');
  const body = entry && entry.querySelector('.act-entry-body');
  if (!body) return;
  const open = body.style.display === 'none';
  body.style.display = open ? '' : 'none';
  const ch = head.querySelector('.act-chev');
  if (ch) ch.className = 'ti ' + (open ? 'ti-chevron-up' : 'ti-chevron-down') + ' act-chev';
}

// Run a reload function with a spinning icon on the clicked refresh button.
async function refreshWith(btnId, fn) {
  const btn = document.getElementById(btnId);
  const icon = btn && btn.querySelector('i');
  if (btn) btn.disabled = true;
  if (icon) icon.classList.add('act-spin');
  try { await fn(); } catch (e) { console.warn('[refresh]', btnId, e); }
  finally {
    if (icon) icon.classList.remove('act-spin');
    if (btn) btn.disabled = false;
  }
}
function actRefresh() { return refreshWith('act-refresh', load); }

function actPage(delta) { _actPage = Math.max(0, _actPage + delta); renderActivity(); }
function actReset() {
  _actPage = 0;
  try { localStorage.setItem('follow_act_page_size', document.getElementById('act-page-size').value); } catch (e) {}
  renderActivity();
}

function openActivityContact(gidx) {
  const btn = document.getElementById('tab-contacts-btn');
  if (btn) bootstrap.Tab.getOrCreateInstance(btn).show();
  if (typeof _sideOpen !== 'undefined' && !_sideOpen) { _sideOpen = true; _updateSidePanelBtn(); _updateSidePanelVisibility(); }
  requestAnimationFrame(() => openSidePanel(gidx));
}

document.addEventListener('shown.bs.tab', e => { if (e.target.id === 'tab-activity-btn') renderActivity(); });
document.addEventListener('DOMContentLoaded', () => {
  try {
    const saved = localStorage.getItem('follow_act_page_size');
    const sel = document.getElementById('act-page-size');
    if (saved && sel && [...sel.options].some(o => o.value === saved)) sel.value = saved;
  } catch (e) {}
});
