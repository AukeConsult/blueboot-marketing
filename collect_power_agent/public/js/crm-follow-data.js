'use strict';

let allRows  = [];
let _sortCol = 'followup_date';
let _sortAsc = true;
let _currentView = 'list';
let _focusQueue = false;
let _visibleRows = [];
const _collapsedGroups = new Set();
let _currentGroupKeys = [];
let _allCampaigns = [];   // loaded from /followup-meta
let _allUsers     = [];   // loaded from /followup-meta (for followup_owner dropdown)
const selected = new Set();   // doc_path strings
let _selectAllActive = false;
let _groupDocPaths = {};              // groupKey -> [doc_path, ...] built by renderGrouped
const _CHANNEL_HREF = {
  linkedin:   v => v.startsWith('http') ? v : 'https://linkedin.com/in/' + v,
  twitter:    v => v.startsWith('http') ? v : 'https://x.com/' + v.replace(/^@/,''),
  facebook:   v => v.startsWith('http') ? v : 'https://facebook.com/' + v,
  instagram:  v => v.startsWith('http') ? v : 'https://instagram.com/' + v.replace(/^@/,''),
  whatsapp:   v => 'https://wa.me/' + v.replace(/[^0-9]/g,''),
  teams:      v => v.includes('@') ? 'https://teams.microsoft.com/l/chat/0/0?users=' + encodeURIComponent(v) : (v.startsWith('http') ? v : '#'),
  telegram:   v => v.startsWith('http') ? v : 'https://t.me/' + v.replace(/^@/,''),
  googlechat: v => v.startsWith('http') ? v : 'https://mail.google.com/chat/u/0/#dm/' + encodeURIComponent(v),
  messenger:  v => v.startsWith('http') ? v : 'https://m.me/' + v,
};
let _sideOpen      = false;           // side panel visible
let _sidePanelGidx = -1;
let _spChannelsOpen = false;          // channels section collapsed by default             // which contact is shown in side panel
let _spCompanyOpen  = false;          // company info section collapsed by default
let _spSectionsOpen = {
  next: true,
  note: true,
  history: true,
  contact: true,
};
let _prefsReady       = false;  // true only after first load() completes with prefs applied
let _loadedPrefs      = null;   // prefs fetched by loadUserPrefs(); applied inside load()
let _followMailEditor = null;
let _followMailGidx   = -1;

// ── Load contacts via API (no direct Firestore reads) ─────────────────────────

async function loadMeta() {
  if (_allCampaigns.length) return;   // already loaded
  try {
    const meta = await fetchJSON(BASE + '/api/crm/followup-meta');
    _allCampaigns = meta.campaigns || [];
    _allUsers     = meta.users     || [];
    const owners = meta.owners || [];
    populateSelect('owner-header', owners, 'All owners');
    // Inject "No owner" option right after "All owners"
    const _ownerSel = document.getElementById('owner-header');
    if (_ownerSel) {
      const _noneOpt = document.createElement('option');
      _noneOpt.value = '__none__'; _noneOpt.textContent = 'No owner';
      _ownerSel.insertBefore(_noneOpt, _ownerSel.options[1]);
    }
  } catch(e) { console.warn('[meta]', e); }
}

function populateCampaignDropdown(ownerVal) {
  const filtered = ownerVal === '__none__'
    ? _allCampaigns.filter(c => !c.owner)
    : ownerVal
      ? _allCampaigns.filter(c => c.owner === ownerVal)
      : _allCampaigns;
  const sel = document.getElementById('campaign-filter');
  const cur = sel.value;
  sel.innerHTML = '<option value="">All campaigns</option>'
    + filtered.map(c => `<option value="${escapeHtml(c.id)}"${c.id === cur ? ' selected' : ''}>${escapeHtml(c.id)}</option>`).join('');
}

function toggleHdrControls(event) {
  if (event) event.stopPropagation();
  const groups = document.getElementById('hdr-action-groups');
  const btn    = document.getElementById('hdr-toggle');
  if (!groups || !btn) return;
  const open   = groups.classList.toggle('hdr-open');
  groups.style.display = open ? 'grid' : '';
  btn.innerHTML = open
    ? '<i class="ti ti-x"></i>'
    : '<i class="ti ti-adjustments-horizontal"></i>';
}
// Close the dropdown when clicking outside of it
document.addEventListener('click', (e) => {
  const groups = document.getElementById('hdr-action-groups');
  const btn    = document.getElementById('hdr-toggle');
  if (!groups || !groups.classList.contains('hdr-open')) return;
  if (!groups.contains(e.target) && !btn.contains(e.target)) {
    groups.classList.remove('hdr-open');
    groups.style.display = '';
    btn.innerHTML = '<i class="ti ti-adjustments-horizontal"></i>';
  }
});

async function onOwnerChange() {
  const own = document.getElementById('owner-header').value;
  populateCampaignDropdown(own);
  // Reset campaign if the current selection doesn't belong to the new owner
  const sel = document.getElementById('campaign-filter');
  if (sel.value && own) {
    const camp = _allCampaigns.find(c => c.id === sel.value);
    if (camp) {
      const mismatch = own === '__none__' ? !!camp.owner : camp.owner !== own;
      if (mismatch) sel.value = '';
    }
  }
  await load();
  saveUserPrefs();
}

async function load() {
  await loadMeta();
  // Ensure campaign dropdown reflects current owner selection
  const ownerVal = document.getElementById('owner-header')?.value || '';
  if (document.getElementById('campaign-filter').options.length <= 1 && _allCampaigns.length) {
    populateCampaignDropdown(ownerVal);
  }

  // Apply saved owner + campaign now that both dropdowns are fully populated
  if (_loadedPrefs && !_prefsReady) {
    const ownerSel = document.getElementById('owner-header');
    if (ownerSel && _loadedPrefs.owner !== undefined) {
      ownerSel.value = _loadedPrefs.owner;
      // Repopulate campaign dropdown for the restored owner, preserving saved campaign
      populateCampaignDropdown(_loadedPrefs.owner);
    }
    const campSel = document.getElementById('campaign-filter');
    if (campSel && _loadedPrefs.campaign !== undefined) {
      if ([...campSel.options].some(o => o.value === _loadedPrefs.campaign))
        campSel.value = _loadedPrefs.campaign;
    }
    const contactStatusSel = document.getElementById('contact-status-filter');
    if (contactStatusSel && _loadedPrefs.filter_contact_status !== undefined) {
      const savedStatus = ['active', 'pending', 'excluded', ''].includes(_loadedPrefs.filter_contact_status)
        ? _loadedPrefs.filter_contact_status
        : 'active';
      contactStatusSel.value = savedStatus;
    }
    const includePendingEl = document.getElementById('include-pending');
    if (includePendingEl && _loadedPrefs.include_pending !== undefined)
      includePendingEl.checked = !!_loadedPrefs.include_pending;
  }

  setTbody('<tr><td colspan="3" class="text-center py-5" style="color:var(--bb-muted)">'
    + '<div class="spinner-border spinner-border-sm me-2"></div>Loading…</td></tr>');
  document.getElementById('follow-empty').style.display = 'none';
  document.getElementById('count-badge').textContent = '';
  setFeedback(null);

  try {
    const _camp  = document.getElementById('campaign-filter')?.value || '';
    const _owner = document.getElementById('owner-header')?.value || '';
    const _cst = document.getElementById('contact-status-filter')?.value || '';
    const _includePending = !!document.getElementById('include-pending')?.checked;
    const params = new URLSearchParams();
    if (_camp)  params.set('campaign_id', _camp);
    if (_owner && !_camp) params.set('owner', _owner);
    if (_includePending) params.set('include_pending', 'true');
    const _url  = BASE + '/api/crm/followup-contacts' + (params.toString() ? '?' + params.toString() : '');
    const data  = await fetchJSON(_url);
    const contacts = data.contacts || [];

    allRows = contacts.map(c => ({
      name:               c.name               || '',
      email:              c.email              || '',
      title:              c.title              || '',
      website:            c.website            || '',
      phone:              c.phone              || '',
      linkedin:           c.linkedin           || '',
      twitter:            c.twitter            || '',
      facebook:           c.facebook           || '',
      instagram:          c.instagram          || '',
      whatsapp:           c.whatsapp           || '',
      teams:              c.teams              || '',
      telegram:           c.telegram           || '',
      googlechat:         c.googlechat         || '',
      messenger:          c.messenger          || '',
      status:             c.status             || 'pending',
      followup_date:      c.followup_date      || '',
      followup_status:    currentFollowupStatus(c.followup_status),
      followup_comment:   c.followup_comment   || '',
      followup_importance: c.followup_importance || '',
      followup_owner:     c.followup_owner     || '',
      comment_history:    c.comment_history    || [],
      campaign_id:        c.campaign_id,
      doc_id:             c.doc_id,
      doc_path:           c.doc_path,
      owner:              c.owner              || '',
      outreach_email:     c.outreach_email     || '',
      outreach_display_name: c.outreach_display_name || '',
      new_mail:           !!c.new_mail,
    }));

    // Apply all remaining prefs (static controls: filters, view, sort) after first load
    if (_loadedPrefs && !_prefsReady) {
      const sv = (id, v) => { if (v !== undefined) { const el = document.getElementById(id); if (el) el.value = v; } };
      if (_loadedPrefs.sync_period) { const sp = document.getElementById('sync-period'); if (sp) sp.value = _loadedPrefs.sync_period; }
      if (_loadedPrefs.view && _loadedPrefs.view !== _currentView) {
        _currentView = _loadedPrefs.view;
        document.getElementById('view-list-btn').className = 'btn btn-sm ' + (_currentView === 'list' ? 'btn-primary' : 'btn-outline-secondary');
        document.getElementById('view-group-btn').className = 'btn btn-sm ' + (_currentView === 'group' ? 'btn-primary' : 'btn-outline-secondary');
        const _gs = document.getElementById('group-selectors');
        if (_gs) _gs.style.display = _currentView === 'group' ? 'flex' : 'none';
      }
      if (_loadedPrefs.focus_queue !== undefined) {
        _focusQueue = !!_loadedPrefs.focus_queue;
        _updateFocusQueueBtn();
      }
      const gp = document.getElementById('group-primary');   if (gp && _loadedPrefs.group_primary) gp.value = _loadedPrefs.group_primary;
      const gs2 = document.getElementById('group-secondary'); if (gs2 && _loadedPrefs.group_secondary !== undefined) gs2.value = _loadedPrefs.group_secondary;
      sv('search',                _loadedPrefs.search);
      sv('followup-filter',       _loadedPrefs.filter_followup_status);
      sv('importance-filter',     _loadedPrefs.filter_importance);
      sv('due-filter',            _loadedPrefs.filter_due);
      if (_loadedPrefs.sort_col !== undefined) _sortCol = _loadedPrefs.sort_col || 'followup_date';
      if (_loadedPrefs.sort_asc !== undefined) _sortAsc  = _loadedPrefs.sort_asc;
      if (_loadedPrefs.side_open) { _sideOpen = true; _updateSidePanelBtn(); _updateSidePanelVisibility(); }
      if (_loadedPrefs.sp_channels_open !== undefined) _spChannelsOpen = !!_loadedPrefs.sp_channels_open;
      if (_loadedPrefs.sp_company_open  !== undefined) _spCompanyOpen  = !!_loadedPrefs.sp_company_open;
      if (_loadedPrefs.sp_sections_open && typeof _loadedPrefs.sp_sections_open === 'object') {
        _spSectionsOpen = { ..._spSectionsOpen, ..._loadedPrefs.sp_sections_open };
        if (_loadedPrefs.sp_sections_open.channels !== undefined) _spChannelsOpen = !!_loadedPrefs.sp_sections_open.channels;
        if (_loadedPrefs.sp_sections_open.company  !== undefined) _spCompanyOpen  = !!_loadedPrefs.sp_sections_open.company;
      }
      _updateSortIndicators();
    }
    _prefsReady = true;
    applyFilter();
  } catch (e) {
    setTbody(`<tr><td colspan="3" class="text-center py-5 text-danger small">
      <i class="ti ti-alert-circle me-1"></i>${escapeHtml(e.message)}</td></tr>`);
    console.error('[crm_follow] load error:', e);
  }
}

function populateSelect(id, values, placeholder) {
  const sel = document.getElementById(id);
  const cur = sel.value;
  sel.innerHTML = `<option value="">${escapeHtml(placeholder)}</option>`
    + values.map(v => `<option value="${escapeHtml(v)}"${v === cur ? ' selected' : ''}>${escapeHtml(v)}</option>`).join('');
}

// Refresh a single contact's history via API after an async job completes.
async function refreshContactHistory(gidx) {
  const row = allRows[gidx];
  if (!row) return;
  try {
    const data = await fetchJSON(
      `${BASE}/api/crm/campaigns/${encodeURIComponent(row.campaign_id)}/contacts/${encodeURIComponent(row.doc_id)}`
    );
    row.comment_history = data.comment_history || [];
    refreshHistoryPanel(gidx);
  } catch(e) {
    console.warn('[refresh-history]', e);
  }
}

// ── API write helper ──────────────────────────────────────────────────────────

async function apiPatchContact(row, fields) {
  const user = (window._authUser && (window._authUser.email || window._authUser.uid)) || 'unknown';
  return fetchJSON(
    `${BASE}/api/crm/campaigns/${encodeURIComponent(row.campaign_id)}/contacts/${encodeURIComponent(row.doc_id)}`,
    {
      method:  'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ ...fields, _user: user }),
    }
  );
}

// ── Sort ──────────────────────────────────────────────────────────────────────

function sortBy(col) {
  if (_focusQueue) {
    _focusQueue = false;
    _updateFocusQueueBtn();
  }
  if (_sortCol === col) { _sortAsc = !_sortAsc; }
  else { _sortCol = col; _sortAsc = true; }
  _updateSortIndicators();
  applyFilter();
  saveUserPrefs();
}

function _updateSortIndicators() {
  document.querySelectorAll('.bb-sortable[data-col]').forEach(th => {
    const ic = th.querySelector('.sort-icon');
    if (!ic) return;
    if (th.dataset.col === _sortCol) { ic.textContent = _sortAsc ? ' ▲' : ' ▼'; th.style.color = 'var(--bb-accent)'; }
    else { ic.textContent = ''; th.style.color = ''; }
  });
}

function applySort(list) {
  if (!_sortCol) return list;
  return [...list].sort((a, b) => {
    if (_sortCol === 'followup_date') return compareFollowupDate(a, b, _sortAsc);
    if (_sortCol === 'followup_importance') return compareImportance(a, b, _sortAsc);
    const va = String(a[_sortCol] || '').toLowerCase();
    const vb = String(b[_sortCol] || '').toLowerCase();
    return _sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
  });
}

function compareFollowupDate(a, b, asc = true) {
  const da = a.followup_date || '';
  const db = b.followup_date || '';
  if (!da && !db) return String(a.name || '').localeCompare(String(b.name || ''));
  if (!da) return 1;
  if (!db) return -1;
  const cmp = da.localeCompare(db);
  if (cmp) return asc ? cmp : -cmp;
  const impRank = { high: 0, medium: 1, low: 2, '': 3 };
  const ia = impRank[a.followup_importance || ''] ?? 3;
  const ib = impRank[b.followup_importance || ''] ?? 3;
  if (ia !== ib) return ia - ib;
  return String(a.name || '').localeCompare(String(b.name || ''));
}

function compareImportance(a, b, asc = true) {
  const rank = { high: 0, medium: 1, low: 2, '': 3 };
  const ia = rank[a.followup_importance || ''] ?? 3;
  const ib = rank[b.followup_importance || ''] ?? 3;
  if (ia !== ib) return asc ? ia - ib : ib - ia;
  return compareFollowupDate(a, b, true);
}


// ── View toggle ───────────────────────────────────────────────────────────────

function toggleView(v) {
  _currentView = v;
  if (v === 'list' && _sideOpen) {
    _sideOpen = false;
    _sidePanelGidx = -1;
    _updateSidePanelBtn();
    _updateSidePanelVisibility();
    document.querySelectorAll('tr.row-sp-active').forEach(tr => tr.classList.remove('row-sp-active'));
  }
  if (v === 'group' && _focusQueue) {
    _focusQueue = false;
    _updateFocusQueueBtn();
  }
  document.getElementById('view-list-btn').className =
    'btn btn-sm ' + (v === 'list' ? 'btn-primary' : 'btn-outline-secondary');
  document.getElementById('view-group-btn').className =
    'btn btn-sm ' + (v === 'group' ? 'btn-primary' : 'btn-outline-secondary');
  const gs = document.getElementById('group-selectors');
  if (gs) gs.style.display = v === 'group' ? 'flex' : 'none';
  _collapsedGroups.clear();
  applyFilter();
  saveUserPrefs();
}

function toggleFocusQueue() {
  _focusQueue = !_focusQueue;
  if (_focusQueue) {
    _currentView = 'list';
    _sortCol = 'followup_date';
    _sortAsc = true;
    _updateSortIndicators();
    document.getElementById('view-list-btn').className = 'btn btn-sm btn-primary';
    document.getElementById('view-group-btn').className = 'btn btn-sm btn-outline-secondary';
    const gs = document.getElementById('group-selectors');
    if (gs) gs.style.display = 'none';
  }
  _updateFocusQueueBtn();
  applyFilter();
  saveUserPrefs();
}

function _updateFocusQueueBtn() {
  const btn = document.getElementById('focus-queue-btn');
  if (btn) btn.className = 'btn btn-sm ' + (_focusQueue ? 'btn-primary' : 'btn-outline-secondary');
  const note = document.getElementById('focus-active-note');
  if (note) note.classList.toggle('is-active', _focusQueue);
}


function toggleGroup(key) {
  if (_collapsedGroups.has(key)) _collapsedGroups.delete(key);
  else _collapsedGroups.add(key);
  applyFilter();
}

function toggleAllGroups() {
  const allCollapsed = _currentGroupKeys.length > 0 && _currentGroupKeys.every(k => _collapsedGroups.has(k));
  if (allCollapsed) {
    _collapsedGroups.clear();
  } else {
    _currentGroupKeys.forEach(k => _collapsedGroups.add(k));
  }
  applyFilter();
}

// ── Filter ────────────────────────────────────────────────────────────────────

function applyFilter() {
  if (_prefsReady) _savePrefsDebounced();
  const q   = document.getElementById('search').value.toLowerCase();

  const fu  = document.getElementById('followup-filter').value;
  const imp = document.getElementById('importance-filter').value;
  const cst = document.getElementById('contact-status-filter').value;
  const due = document.getElementById('due-filter').value;
  const includePending = !!document.getElementById('include-pending')?.checked;

  const today   = _today();
  const weekEnd = _weekEnd();

  let list = allRows.filter(r => {
    if (fu === '__none__') { if (r.followup_status) return false; }
    else if (fu && r.followup_status !== fu) return false;
    if (imp === '__none__') { if (r.followup_importance) return false; }
    else if (imp && r.followup_importance !== imp) return false;
    if (cst && r.status !== cst) return false;
    if (cst && r.status !== cst) return false;
    if (due === 'none'    && r.followup_date) return false;
    if (due === 'overdue' && !(r.followup_date && r.followup_date < today)) return false;
    if (due === 'today'   && r.followup_date !== today) return false;
    if (due === 'week'    && !(r.followup_date && r.followup_date >= today && r.followup_date <= weekEnd)) return false;
    if (q) {
      const hay = [r.name, r.email, r.website, r.title].join(' ').toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  if (_focusQueue) {
    list = _focusQueueSort(list.filter(r => {
      const status = currentFollowupStatus(r.followup_status || '');
      return r.followup_date
        && r.followup_date <= weekEnd
        && !['not_interested'].includes(status);
    }));
  } else {
    list = applySort(list);
  }
  document.getElementById('count-badge').textContent =
    `${list.length} contact${list.length === 1 ? '' : 's'}`;
  _visibleRows = list;
  render(list);
  if (_focusQueue) _openFirstFocusContact(list);
}

function _focusQueueSort(list) {
  return [...list].sort((a, b) => compareFollowupDate(a, b, true));
}

function _openFirstFocusContact(list) {
  if (!_focusQueue || !list.length) return;
  const current = allRows[_sidePanelGidx];
  if (_sideOpen && current && list.includes(current)) return;
  _sideOpen = true;
  _updateSidePanelBtn();
  _updateSidePanelVisibility();
  const gidx = allRows.indexOf(list[0]);
  if (gidx >= 0) requestAnimationFrame(() => openSidePanel(gidx));
}

