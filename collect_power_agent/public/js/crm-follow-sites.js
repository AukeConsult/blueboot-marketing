'use strict';
// ── Sites tab ─────────────────────────────────────────────────────────────────
let _allSiteLeads     = [];
let _sitesTabLoaded   = '';
let _sitesSortCol     = 'company';
let _sitesSortAsc     = true;

function sortSitesBy(col) {
  if (_sitesSortCol === col) { _sitesSortAsc = !_sitesSortAsc; }
  else { _sitesSortCol = col; _sitesSortAsc = true; }
  _updateSitesSortIndicators();
  applySitesTabFilter();
}

function _updateSitesSortIndicators() {
  document.querySelectorAll('#sites-tab-layout .bb-sortable').forEach(th => {
    const ic = th.querySelector('.sites-sort-icon');
    if (!ic) return;
    const m = (th.getAttribute('onclick') || '').match(/sortSitesBy\('(.+?)'\)/);
    const col = m ? m[1] : null;
    if (col === _sitesSortCol) { ic.textContent = _sitesSortAsc ? ' ▲' : ' ▼'; th.style.color = 'var(--bb-accent)'; }
    else { ic.textContent = ''; th.style.color = ''; }
  });
}

function _sortSiteLeads(list) {
  const impRank = { high: 0, medium: 1, low: 2, '': 3 };
  const prioRank = { A: 0, B: 1, C: 2, '': 3 };
  return [...list].sort((a, b) => {
    let va, vb;
    if (_sitesSortCol === 'followup_importance') {
      va = impRank[a.followup_importance || ''] ?? 3;
      vb = impRank[b.followup_importance || ''] ?? 3;
      return _sitesSortAsc ? va - vb : vb - va;
    }
    if (_sitesSortCol === 'priority') {
      va = prioRank[a.priority || ''] ?? 3;
      vb = prioRank[b.priority || ''] ?? 3;
      return _sitesSortAsc ? va - vb : vb - va;
    }
    va = String(a[_sitesSortCol] || '').toLowerCase();
    vb = String(b[_sitesSortCol] || '').toLowerCase();
    return _sitesSortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
  });
}

const _SITE_FU_STATUSES = [
  {value:'',              label:'— no status —'},
  {value:'to_contact',    label:'To contact'},
  {value:'contacted',     label:'Contacted'},
  {value:'in_work',       label:'In work'},
  {value:'not_interested',label:'Not interested'},
  {value:'deal',          label:'Deal'},
];
const _SITE_IMPORTANCE = [
  {value:'',      label:'— none —'},
  {value:'low',   label:'Low'},
  {value:'medium',label:'Medium'},
  {value:'high',  label:'High'},
];

function _sitesTabKey() {
  const c = document.getElementById('campaign-filter')?.value || '';
  const o = document.getElementById('owner-header')?.value    || '';
  const p = document.getElementById('include-pending')?.checked ? '1' : '0';
  return c + '|' + o + '|' + p;
}

async function loadSitesTab(force) {
  const key = _sitesTabKey();
  if (!force && key === _sitesTabLoaded) { applySitesTabFilter(); return; }
  _sitesTabLoaded = key;
  _allSiteLeads   = [];

  const tbody = document.getElementById('sites-tab-body');
  tbody.innerHTML = '<tr><td colspan="6" class="text-center py-5 small" style="color:var(--bb-muted)">'
    + '<div class="spinner-border spinner-border-sm me-2"></div>Loading…</td></tr>';
  document.getElementById('sites-tab-count-label').textContent = '';

  const camp    = document.getElementById('campaign-filter')?.value || '';
  const owner   = document.getElementById('owner-header')?.value    || '';
  const pending = !!document.getElementById('include-pending')?.checked;
  const params  = new URLSearchParams();
  if (camp)    params.set('campaign_id', camp);
  else if (owner) params.set('owner', owner);
  if (pending) params.set('include_pending', 'true');

  try {
    const data = await fetchJSON(BASE + '/api/crm/leads?' + params.toString());
    _allSiteLeads = data.leads || [];
    _updateSitesSortIndicators();
    applySitesTabFilter();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="8" class="text-center py-5 small text-danger">Error: ${escapeHtml(e.message)}</td></tr>`;
  }
}

function applySitesTabFilter() {
  const q   = (document.getElementById('search')?.value          || '').trim().toLowerCase();
  const imp = document.getElementById('importance-filter')?.value || '';

  const filtered = _allSiteLeads.filter(l => {
    if (imp && l.followup_importance !== imp) return false;
    if (q) {
      const hay = [l.company, l.domain, l.website, l.ai_sector, l.campaign_name]
        .filter(Boolean).join(' ').toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  const sorted = _sortSiteLeads(filtered);
  const total = _allSiteLeads.length;
  document.getElementById('sites-tab-count-label').textContent =
    filtered.length === total ? `${total} sites` : `${filtered.length} / ${total}`;
  document.getElementById('sites-tab-count').textContent = filtered.length || '';
  renderSitesTabTable(sorted);
}

function _siteStatusBadge(s) {
  const map = { pending:'secondary', active:'success', excluded:'danger' };
  return `<span class="badge bg-${map[s]||'secondary'} fw-normal" style="font-size:10px">${escapeHtml(s||'pending')}</span>`;
}
function _sitePrioBadge(p) {
  if (!p) return '<span class="text-muted small">—</span>';
  const map = { A:'success', B:'warning', C:'secondary' };
  return `<span class="badge bg-${map[p]||'secondary'} fw-normal" style="font-size:10px">${escapeHtml(p)}</span>`;
}
function _siteImpBadge(i) {
  if (!i) return '<span class="text-muted small">—</span>';
  const map = { high:'danger', medium:'warning', low:'secondary' };
  return `<span class="badge bg-${map[i]||'secondary'} fw-normal" style="font-size:10px">${escapeHtml(i)}</span>`;
}
function _siteFuBadge(fu) {
  if (!fu) return '<span class="text-muted small">—</span>';
  const label = (_SITE_FU_STATUSES.find(x => x.value === fu)||{}).label || fu;
  return `<span style="font-size:11px;color:var(--bb-muted)">${escapeHtml(label)}</span>`;
}
function _siteContactCount(l) {
  const total = l.contact_count ?? null;
  if (total === null) return '—';
  let out = String(total);
  if (l.pending_count)  out += ` <span class="text-success" style="font-size:10px">${l.pending_count}p</span>`;
  if (l.excluded_count) out += ` <span class="text-danger"  style="font-size:10px">${l.excluded_count}x</span>`;
  return out;
}

function renderSitesTabTable(leads) {
  const tbody = document.getElementById('sites-tab-body');
  if (!leads.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="text-center py-5 small" style="color:var(--bb-muted)">No sites match the current filters.</td></tr>';
    return;
  }
  tbody.innerHTML = leads.map(l => {
    const idx    = _allSiteLeads.indexOf(l);
    const fuOpts = _SITE_FU_STATUSES.map(s =>
      `<option value="${s.value}"${l.followup_status === s.value ? ' selected' : ''}>${escapeHtml(s.label)}</option>`
    ).join('');
    const impCls = (IMPORTANCE_LEVELS.find(i => i.value === (l.followup_importance||'')) || IMPORTANCE_LEVELS[0]).cls;
    const impOpts = IMPORTANCE_LEVELS.map(i =>
      `<option value="${i.value}"${(l.followup_importance||'') === i.value ? ' selected' : ''}>${i.label}</option>`
    ).join('');
    return `<tr class="site-tab-row" data-idx="${idx}">
      <td class="small fw-semibold">${escapeHtml(l.company||l.domain||'—')}</td>
      <td class="small">${escapeHtml(l.ai_sector||'—')}</td>
      <td class="text-center">${_sitePrioBadge(l.priority)}</td>
      <td><select class="follow-select" data-idx="${idx}" data-field="followup_status" onchange="saveSiteLeadField(this)">${fuOpts}</select></td>
      <td><select class="imp-select ${impCls}" data-idx="${idx}" data-field="followup_importance"
        onchange="saveSiteLeadField(this);this.className='imp-select '+(IMPORTANCE_LEVELS.find(i=>i.value===this.value)||IMPORTANCE_LEVELS[0]).cls">${impOpts}</select></td>
      <td class="text-center small">${_siteContactCount(l)}</td>
    </tr>`;
  }).join('');
}

async function saveSiteLeadField(el) {
  const idx   = parseInt(el.dataset.idx, 10);
  const field = el.dataset.field;
  const val   = el.value;
  const lead  = _allSiteLeads[idx];
  if (!lead) return;
  el.style.opacity = '.5';
  el.disabled = true;
  try {
    await fetchJSON(
      `${BASE}/api/crm/campaigns/${encodeURIComponent(lead.campaign_id)}/leads/${encodeURIComponent(lead.lead_id)}`,
      { method:'PATCH', headers:{'Content-Type':'application/json'}, body: JSON.stringify({[field]: val}) }
    );
    lead[field] = val;
  } catch(e) {
    alert('Save failed: ' + e.message);
    el.value = lead[field] || '';  // revert on failure
  }
  finally { el.style.opacity = ''; el.disabled = false; }
}

document.addEventListener('shown.bs.tab', e => {
  if (e.target.id === 'tab-sites-btn') loadSitesTab(false);
});
function _maybeReloadSites() {
  const active = document.querySelector('#follow-tabs .nav-link.active');
  if (active && active.id === 'tab-sites-btn') loadSitesTab(true);
  else _sitesTabLoaded = '';
}
function _maybeFilterSites() {
  const active = document.querySelector('#follow-tabs .nav-link.active');
  if (active && active.id === 'tab-sites-btn') applySitesTabFilter();
}
