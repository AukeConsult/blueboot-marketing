'use strict';
// ── Comment history UI ────────────────────────────────────────────────────────

// ── Row expand / collapse state ──────────────────────────────────────────────
let _expandedGidx = -1;

function onRowExpand(gidx) {
  if (_expandedGidx === gidx) return;
  if (_expandedGidx >= 0) _collapseRow(_expandedGidx);
  _expandedGidx = gidx;
  // Expand the textarea to fit its content
  const ta = document.querySelector(`.comment-row-input[data-gidx="${gidx}"]`);
  if (ta) { ta.style.height = 'auto'; ta.style.height = ta.scrollHeight + 'px'; }
}

function _collapseRow(gidx) {
  // Collapse textarea to 1 line
  const ta = document.querySelector(`.comment-row-input[data-gidx="${gidx}"]`);
  if (ta) ta.style.height = '';
  // Hide history row and reset toggle
  _setHistOpen(gidx, false);
}

// Collapse expanded row when clicking outside the contacts table
document.addEventListener('click', e => {
  if (_expandedGidx < 0) return;
  if (!e.target.closest('#follow-tbody')) {
    _collapseRow(_expandedGidx);
    _expandedGidx = -1;
  }
}, false);

function _setHistOpen(gidx, open) {
  const hist = document.getElementById('hist-' + gidx);
  if (!hist) return;
  hist.style.display = open ? '' : 'none';
  const comment = hist.previousElementSibling;
  const data = comment ? comment.previousElementSibling : null;
  [data, comment, hist].forEach(tr => tr && tr.classList.toggle('hist-open', open));
  const btn = document.querySelector(`[onclick="toggleHistory(this,${gidx})"]`);
  if (btn) {
    btn.classList.toggle('is-open', open);
    const i = btn.querySelector('i');
    if (i) i.className = open ? 'ti ti-chevron-up' : 'ti ti-chevron-down';
    btn.title = open ? 'Hide history' : 'Show history';
  }
}

function toggleHistory(btn, gidx) {
  const row = document.getElementById('hist-' + gidx);
  if (!row) return;
  const opening = row.style.display === 'none';
  if (opening) {
    // Only one history panel open at a time
    document.querySelectorAll('tr.follow-history-row.hist-open').forEach(tr => {
      const g = tr.id.replace('hist-', '');
      if (g !== String(gidx)) _setHistOpen(g, false);
    });
  }
  _setHistOpen(gidx, opening);
}

function toggleHistEntry(el) {
  const entry = el.closest('.hist-entry');
  if (!entry) return;
  const open = entry.classList.toggle('hist-entry-open');
  const chevron = entry.querySelector('.hist-entry-chevron');
  if (chevron) { chevron.classList.toggle('ti-chevron-down', !open); chevron.classList.toggle('ti-chevron-up', open); }
}

function toggleAllHistEntries(btn) {
  const panel = btn.closest('.hist-panel');
  if (!panel) return;
  const entries = [...panel.querySelectorAll('.hist-entry')];
  const open = entries.some(e => !e.classList.contains('hist-entry-open'));
  entries.forEach(e => {
    e.classList.toggle('hist-entry-open', open);
    const c = e.querySelector('.hist-entry-chevron');
    if (c) { c.classList.toggle('ti-chevron-down', !open); c.classList.toggle('ti-chevron-up', open); }
  });
  btn.textContent = open ? 'Collapse all' : 'Expand all';
}

function _histBodyText(h) {
  if (h.body_text) return String(h.body_text).trim();
  if (h.body_html) {
    const el = document.createElement('div');
    el.innerHTML = String(h.body_html).replace(/<(br|\/p|\/div|\/li)\s*\/?>/gi, '\n');
    return (el.textContent || '').replace(/\n{3,}/g, '\n\n').trim();
  }
  return '';
}

const _HIST_TYPES = {
  EMAIL_IN:  { cls: 'in',     label: 'Reply in' },
  EMAIL_OUT: { cls: 'out',    label: 'Mail out' },
  MAIL_SENT: { cls: 'sent',   label: 'Sent' },
  BOUNCE:    { cls: 'bounce', label: 'Bounce' },
};

function renderHistoryContent(history) {
  if (!history.length) {
    return '<div class="hist-panel"><div class="hist-empty"><i class="ti ti-message-off"></i> No history yet — comments, sent mails and replies will show up here.</div></div>';
  }
  const items = [...history].sort((a, b) => new Date(b.date || 0) - new Date(a.date || 0));
  const rows = items.map(h => {
    const d = h.date ? new Date(h.date).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' }) : '—';
    const t = _HIST_TYPES[h.type] || { cls: 'note', label: 'Note' };
    const text = h.text || '';
    const body = _histBodyText(h);
    const oneLine = text.replace(/\s+/g, ' ');
    const preview = oneLine.length > 90 ? oneLine.slice(0, 90) + '…' : oneLine;
    const meta = [
      h.from ? `<div><span class="hist-detail-lbl">From</span> ${escapeHtml(h.from)}</div>` : '',
      h.to   ? `<div><span class="hist-detail-lbl">To</span> ${escapeHtml(h.to)}</div>` : '',
      h.user ? `<div><span class="hist-detail-lbl">By</span> ${escapeHtml(h.user)}</div>` : '',
    ].join('');
    const showBody = body && body !== text.trim();
    return `<div class="hist-entry hist-k-${t.cls}">
      <div class="hist-entry-summary" onclick="toggleHistEntry(this)">
        <span class="hist-type-badge hist-type-${t.cls}">${t.label}</span>
        <span class="hist-entry-date">${escapeHtml(d)}</span>
        <span class="hist-entry-preview">${escapeHtml(preview)}</span>
        <i class="ti ti-chevron-down hist-entry-chevron"></i>
      </div>
      <div class="hist-entry-detail">
        ${meta}
        ${text ? `<div class="hist-entry-text mt-1">${escapeHtml(text)}</div>` : ''}
        ${showBody ? `<div class="hist-entry-body">${escapeHtml(body)}</div>` : ''}
      </div>
    </div>`;
  }).join('');
  return `<div class="hist-panel">
    <div class="hist-head">
      <span class="hist-title"><i class="ti ti-history"></i> History <span class="hist-title-n">${items.length}</span></span>
      <button type="button" class="hist-expand-all" onclick="toggleAllHistEntries(this)">Expand all</button>
    </div>
    <div class="hist-list">${rows}</div>
  </div>`;
}

function refreshHistoryPanel(gidx) {
  const row = document.getElementById('hist-' + gidx);
  if (!row) return;
  const td = row.querySelector('td:last-child');
  if (td) td.innerHTML = renderHistoryContent(allRows[gidx].comment_history);
  const count = allRows[gidx].comment_history.length;
  const btn   = document.querySelector(`[onclick="toggleHistory(this,${gidx})"] .hist-count`);
  if (btn) { btn.textContent = count; btn.classList.toggle('has-entries', count > 0); }
}

function setTbody(html) {
  document.getElementById('follow-tbody').innerHTML = html;
  // Reset expanded row tracking — DOM was rebuilt
  _expandedGidx = -1;
}

