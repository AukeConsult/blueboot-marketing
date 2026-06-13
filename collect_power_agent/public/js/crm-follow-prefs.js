'use strict';
// ── Due-date helpers ──────────────────────────────────────────────────────────

function _today()   { return new Date().toISOString().slice(0, 10); }
function _weekEnd() { const d = new Date(); d.setDate(d.getDate() + 7); return d.toISOString().slice(0, 10); }

function dueDateClass(date) {
  if (!date) return '';
  const t = _today();
  if (date < t)           return 'overdue';
  if (date === t)         return 'due-today';
  if (date <= _weekEnd()) return 'due-soon';
  return '';
}

// ── User preference persistence (frontend-status/{email}/pages/followup) ─────

let _savePrefsTimer = null;

function _savePrefsDebounced() {
  clearTimeout(_savePrefsTimer);
  _savePrefsTimer = setTimeout(saveUserPrefs, 800);
}

async function saveUserPrefs() {
  if (!_prefsReady) return;
  const prefs = {
    owner:                 document.getElementById('owner-header')?.value       || '',
    campaign:              document.getElementById('campaign-filter')?.value    || '',
    filter_followup_status: document.getElementById('followup-filter')?.value   || '',
    filter_importance:     document.getElementById('importance-filter')?.value  || '',
    filter_contact_status: document.getElementById('contact-status-filter')?.value || '',
    include_pending:       !!document.getElementById('include-pending')?.checked,
    filter_due:            document.getElementById('due-filter')?.value         || '',
    search:                document.getElementById('search')?.value             || '',
    group_primary:         document.getElementById('group-primary')?.value      || '',
    group_secondary:       document.getElementById('group-secondary')?.value    || '',
    view:                  _currentView,
    focus_queue:           _focusQueue,
    sort_col:              _sortCol,
    sort_asc:              _sortAsc,
    side_open:             _sideOpen,
    sync_period:           document.getElementById('sync-period')?.value        || '',
    sp_channels_open:      _spChannelsOpen,
    sp_company_open:       _spCompanyOpen,
    sp_sections_open:      _getSpSectionsOpenPrefs(),
  };
  try {
    await fetchJSON(`${BASE}/api/crm/user-prefs?page=followup`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(prefs),
    });
  } catch(e) { /* silent — prefs are best-effort */ }
}

async function loadUserPrefs() {
  try {
    const prefs = await fetchJSON(`${BASE}/api/crm/user-prefs?page=followup`);
    _loadedPrefs = prefs || {};
  } catch(e) {
    _loadedPrefs = {};
  }
}

// ── Force table relayout when Bootstrap breakpoints show/hide columns ─────────
// table-layout:fixed computes widths once; toggling to auto forces recalc so
// hidden columns release their space and visible ones fill 100%.
function _relayoutTable() {
  const t = document.querySelector('#follow-layout .table-responsive table');
  if (!t) return;
  t.style.tableLayout = 'auto';
  requestAnimationFrame(() => { t.style.tableLayout = 'fixed'; });
}
{
  let _bpTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(_bpTimer);
    _bpTimer = setTimeout(_relayoutTable, 80);
  });
}

// ── Page init ─────────────────────────────────────────────────────────────────

(async () => {
  await requireAuth();
  requireRole(['user', 'campaign-user', 'admin']);
  await loadUserPrefs();
  await load();
})();


