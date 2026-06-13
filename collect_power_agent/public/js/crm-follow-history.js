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
  // Hide history row and reset toggle chevron
  const histRow = document.getElementById('hist-' + gidx);
  if (histRow && histRow.style.display !== 'none') {
    histRow.style.display = 'none';
    const btn = document.querySelector(`[onclick="toggleHistory(this,${gidx})"]`);
    if (btn) btn.querySelector('i').className = 'ti ti-chevron-down';
  }
}

// Collapse expanded row when clicking outside the contacts table
document.addEventListener('click', e => {
  if (_expandedGidx < 0) return;
  if (!e.target.closest('#follow-tbody')) {
    _collapseRow(_expandedGidx);
    _expandedGidx = -1;
  }
}, false);

function toggleHistory(btn, gidx) {
  const row = document.getElementById('hist-' + gidx);
  if (!row) return;
  const open = row.style.display !== 'none';
  row.style.display = open ? 'none' : '';
  btn.querySelector('i').className = open ? 'ti ti-chevron-down' : 'ti ti-chevron-up';
}

let _histExpanded = new Set();

function toggleHistEntry(key, el) {
  if (_histExpanded.has(key)) _histExpanded.delete(key);
  else _histExpanded.add(key);
  const entry = el.closest('.hist-entry');
  if (!entry) return;
  entry.classList.toggle('hist-entry-open', _histExpanded.has(key));
  const chevron = entry.querySelector('.hist-entry-chevron');
  if (chevron) { chevron.classList.toggle('ti-chevron-down', !_histExpanded.has(key)); chevron.classList.toggle('ti-chevron-up', _histExpanded.has(key)); }
}

function renderHistoryContent(history) {
  if (!history.length) return '<span class="small" style="color:var(--bb-muted)">No history yet.</span>';
  return '<div class="hist-list">'
    + [...history].reverse().map((h, i) => {
        const d        = h.date ? new Date(h.date).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' }) : '—';
        const isEmailIn  = h.type === 'EMAIL_IN';
        const isEmailOut = h.type === 'EMAIL_OUT';
        const isEmail    = isEmailIn || isEmailOut;
        const extraCls   = isEmailIn ? 'hist-entry-email-in' : isEmailOut ? 'hist-entry-email-out' : '';
        const typeBadge  = isEmailIn
          ? `<span class="hist-type-badge hist-type-in">IN</span>`
          : isEmailOut ? `<span class="hist-type-badge hist-type-out">OUT</span>` : '';
        const text    = h.text || '';
        const key     = `${i}_${(h.date||'').slice(0,10)}`;
        const preview = text.length > 60 ? text.slice(0, 60) + '…' : text;
        const detail  = isEmail
          ? `<div class="hist-entry-detail">
               ${h.from ? `<div><span class="hist-detail-lbl">From</span> ${escapeHtml(h.from)}</div>` : ''}
               ${h.to   ? `<div><span class="hist-detail-lbl">To</span> ${escapeHtml(h.to)}</div>` : ''}
               ${text   ? `<div class="hist-entry-text mt-1">${escapeHtml(text)}</div>` : ''}
             </div>`
          : `<div class="hist-entry-detail">
               <div><span class="hist-detail-lbl">By</span> ${escapeHtml(h.user || '')}</div>
               ${text ? `<div class="hist-entry-text mt-1">${escapeHtml(text)}</div>` : ''}
             </div>`;
        return `<div class="hist-entry ${extraCls}" onclick="toggleHistEntry('${key}',this)" style="cursor:pointer">
          <div class="hist-entry-summary">
            ${typeBadge}
            <span class="hist-entry-date">${escapeHtml(d)}</span>
            <span class="hist-entry-preview">${escapeHtml(preview)}</span>
            <i class="ti ti-chevron-down hist-entry-chevron"></i>
          </div>
          ${detail}
        </div>`;
      }).join('') + '</div>';
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

