'use strict';
// ── Side panel ────────────────────────────────────────────────────────────────

function onRowClick(event, gidx) {
  if (event.target.closest('button,a,label,select,input,textarea')) return;
  onRowExpand(gidx);            // expand comment/history for this row
  if (window.innerWidth < 768) {
    openSidePanelFromButton(gidx, event);
    return;
  }
  if (!_sideOpen) return;
  if (_sidePanelGidx === gidx) return;
  openSidePanel(gidx);
}

function openSidePanelFromButton(gidx, event) {
  if (event) event.stopPropagation();
  _sideOpen = true;
  _updateSidePanelBtn();
  _updateSidePanelVisibility();
  openSidePanel(gidx);
  saveUserPrefs();
}

function toggleSidePanel() {
  _sideOpen = !_sideOpen;
  _updateSidePanelBtn();
  _updateSidePanelVisibility();
  saveUserPrefs();
}

function closeSidePanel() {
  _sideOpen = false;
  _sidePanelGidx = -1;
  _updateSidePanelBtn();
  _updateSidePanelVisibility();
  document.querySelectorAll('tr.row-sp-active').forEach(tr => tr.classList.remove('row-sp-active'));
  saveUserPrefs();
}

function toggleSpChannels() {
  toggleSpSection('channels');
}

async function logChatOpened(gidx, url) {
  window.open(url, '_blank', 'noopener');
  const row = allRows[gidx];
  if (!row) return;
  const today = new Date().toISOString().slice(0, 10);
  const alreadyToday = (row.comment_history || []).some(
    h => h.type === 'CHAT' && (h.date || '').slice(0, 10) === today
  );
  if (alreadyToday) return;
  const now  = new Date();
  const hhmm = now.toTimeString().slice(0, 5);
  const entry = { text: `Google Chat opened ${hhmm}`, type: 'CHAT', date: now.toISOString() };
  try {
    await apiPatchContact(row, { _history_entry: entry });
    row.comment_history = row.comment_history || [];
    row.comment_history.push({ ...entry, user: (window._authUser && window._authUser.email) || '' });
    if (_sidePanelGidx === gidx) _refreshSidePanelHistory();
  } catch(e) { console.warn('[chat-log]', e.message); }
}

async function ackNewMail(gidx, event) {
  if (event) event.stopPropagation();
  const row = allRows[gidx];
  if (!row || !row.new_mail) return;
  const now = new Date();
  const hhmm = now.toTimeString().slice(0, 5);
  const entry = { text: `New mail acknowledged ${hhmm}`, type: 'NEWMAIL_ACK', date: now.toISOString() };
  try {
    await apiPatchContact(row, { new_mail: false, _history_entry: entry });
    row.new_mail = false;
    row.comment_history = row.comment_history || [];
    row.comment_history.push({ ...entry, user: (window._authUser && window._authUser.email) || '' });
    // Re-render: update badge in row
    const tr = document.querySelector(`tr[data-doc="${CSS.escape(row.doc_path)}"]`);
    if (tr) {
      const btn = tr.querySelector('.newmail-btn');
      if (btn) btn.remove();
    }
    // Refresh side panel if open on this contact
    if (_sidePanelGidx === gidx) _refreshSidePanelHistory();
    if (_sidePanelGidx === gidx) openSidePanel(gidx);
  } catch(e) { console.warn('[ack-newmail]', e.message); }
}

function _updateSidePanelBtn() {
  const btn = document.getElementById('side-panel-btn');
  if (!btn) return;
  btn.className = 'btn btn-sm ' + (_sideOpen ? 'btn-primary' : 'btn-outline-secondary');
}

function _updateSidePanelVisibility() {
  const panel = document.getElementById('contact-side-panel');
  if (!panel) return;
  if (_sideOpen) panel.classList.add('sp-open');
  else panel.classList.remove('sp-open');
  const backdrop = document.getElementById('sp-backdrop');
  if (backdrop) backdrop.style.display = (_sideOpen && window.innerWidth < 768) ? 'block' : 'none';
}

function _isSpSectionOpen(key) {
  if (key === 'channels') return _spChannelsOpen;
  if (key === 'company') return _spCompanyOpen;
  return _spSectionsOpen[key] !== false;
}

function _setSpSectionOpen(key, open) {
  if (key === 'channels') _spChannelsOpen = open;
  else if (key === 'company') _spCompanyOpen = open;
  else _spSectionsOpen[key] = open;
}

function _getSpSectionsOpenPrefs() {
  return {
    next:     _isSpSectionOpen('next'),
    note:     _isSpSectionOpen('note'),
    history:  _isSpSectionOpen('history'),
    channels: _isSpSectionOpen('channels'),
    contact:  _isSpSectionOpen('contact'),
    company:  _isSpSectionOpen('company'),
  };
}

function _spSectionHeader(key, label, iconCls = '') {
  const open = _isSpSectionOpen(key);
  return `<div class="sp-section-label sp-collapsible" onclick="toggleSpSection('${key}')">
    <span class="sp-collapsible-title">${iconCls ? `<i class="ti ${iconCls}" style="font-size:13px"></i>` : ''}${escapeHtml(label)}</span>
    <i data-sp-chevron="${key}" class="ti ${open ? 'ti-chevron-up' : 'ti-chevron-down'}" style="font-size:12px"></i>
  </div>`;
}

function toggleSpSection(key) {
  const open = !_isSpSectionOpen(key);
  _setSpSectionOpen(key, open);
  if (_prefsReady) saveUserPrefs();
  const body = document.getElementById(`sp-${key}-body`);
  if (body) body.style.display = open ? 'block' : 'none';
  const icon = document.querySelector(`[data-sp-chevron="${key}"]`);
  if (icon) {
    icon.classList.toggle('ti-chevron-up', open);
    icon.classList.toggle('ti-chevron-down', !open);
  }
}

function openSidePanel(gidx) {
  _sidePanelGidx = gidx;
  const row = allRows[gidx];
  if (!row) return;
  document.querySelectorAll('tr.row-sp-active').forEach(tr => tr.classList.remove('row-sp-active'));
  const tr = document.querySelector(`tr[data-doc="${CSS.escape(row.doc_path)}"]`);
  if (tr) tr.classList.add('row-sp-active');
  try {
    document.getElementById('sp-body').innerHTML = _renderSidePanel(row);
    _loadSpCompany(row);
  } catch(e) {
    console.error('[side-panel] render error:', e);
    document.getElementById('sp-body').innerHTML =
      `<div class="sp-section small text-danger p-3">⚠ Render error: ${escapeHtml(e.message)}</div>`;
  }
}

function _renderSidePanel(r) {
  const gidx    = allRows.indexOf(r);
  const initials = (r.name || '?').split(' ').map(w => w[0]).slice(0, 2).join('').toUpperCase();
  const siteShort = r.website ? r.website.replace(/^https?:\/\//, '').replace(/\/$/, '').slice(0, 35) : '';
  const dueCls    = dueDateClass(r.followup_date);
  const dateCls   = dueCls === 'overdue' ? 'date-overdue' : (dueCls === 'due-soon' || dueCls === 'due-today') ? 'date-due-soon' : '';
  const impCls    = (IMPORTANCE_LEVELS.find(i => i.value === r.followup_importance) || IMPORTANCE_LEVELS[0]).cls;
  const fuOpts    = FU_STATUSES.map(st =>
    `<option value="${st.value}"${r.followup_status === st.value ? ' selected' : ''}>${escapeHtml(st.label)}</option>`).join('');
  const impOpts   = IMPORTANCE_LEVELS.map(i =>
    `<option value="${i.value}"${r.followup_importance === i.value ? ' selected' : ''}>${escapeHtml(i.label)}</option>`).join('');
  const newMailBanner = r.new_mail
    ? `<div class="sp-section" style="padding:6px 12px">
         <div class="d-flex align-items-center gap-2" style="background:#fef3c7;border-radius:6px;padding:6px 10px">
           <i class="ti ti-mail-opened" style="color:#d97706;font-size:1rem"></i>
           <span class="small" style="color:#92400e;flex:1">New incoming mail</span>
           <button class="btn btn-sm py-0 px-2" style="font-size:.75rem;background:#d97706;color:#fff;border:none"
             onclick="ackNewMail(${gidx},event)">Mark read</button>
         </div>
       </div>`
    : '';
  return `
    ${newMailBanner}
    <div class="sp-section d-flex align-items-start gap-2 py-3">
      <div class="sp-avatar">${escapeHtml(initials)}</div>
      <div class="flex-grow-1">
        <input type="text" class="follow-input small w-100 fw-500" value="${escapeHtml(r.name || '')}"
          placeholder="Name" data-gidx="${gidx}" data-field="name"
          onchange="saveField(this)"
          onkeydown="if(event.key==='Enter'){event.preventDefault();this.blur()}">
        <input type="text" class="follow-input small w-100 contact-title" value="${escapeHtml(r.title || '')}"
          placeholder="Title" data-gidx="${gidx}" data-field="title"
          onchange="saveField(this)"
          onkeydown="if(event.key==='Enter'){event.preventDefault();this.blur()}">
        <div class="mt-1">${statusBadge(r.status)}</div>
      </div>
      <div class="d-flex gap-1">
        ${r.email    ? `<a href="mailto:${escapeHtml(r.email)}" class="collapse-all-btn sp-ch-btn" title="${escapeHtml(r.email)}"><i class="ti ti-mail"></i></a>` : ''}
        ${r.phone    ? `<a href="tel:${escapeHtml(r.phone)}" class="collapse-all-btn sp-ch-btn" title="${escapeHtml(r.phone)}"><i class="ti ti-phone"></i></a>` : ''}
        ${r.linkedin ? `<a href="${escapeHtml(_CHANNEL_HREF.linkedin(r.linkedin))}" target="_blank" class="collapse-all-btn sp-ch-btn" title="${escapeHtml(r.linkedin)}"><i class="ti ti-brand-linkedin"></i></a>` : ''}
        ${r.twitter  ? `<a href="${escapeHtml(_CHANNEL_HREF.twitter(r.twitter))}" target="_blank" class="collapse-all-btn sp-ch-btn" title="${escapeHtml(r.twitter)}"><i class="ti ti-brand-twitter"></i></a>` : ''}
        ${r.facebook ? `<a href="${escapeHtml(_CHANNEL_HREF.facebook(r.facebook))}" target="_blank" class="collapse-all-btn sp-ch-btn" title="${escapeHtml(r.facebook)}"><i class="ti ti-brand-facebook"></i></a>` : ''}
        ${r.instagram? `<a href="${escapeHtml(_CHANNEL_HREF.instagram(r.instagram))}" target="_blank" class="collapse-all-btn sp-ch-btn" title="${escapeHtml(r.instagram)}"><i class="ti ti-brand-instagram"></i></a>` : ''}
        ${r.whatsapp   ? `<a href="${escapeHtml(_CHANNEL_HREF.whatsapp(r.whatsapp))}" target="_blank" class="collapse-all-btn sp-ch-btn" title="${escapeHtml(r.whatsapp)}"><i class="ti ti-brand-whatsapp"></i></a>` : ''}
        ${r.teams      ? `<a href="${escapeHtml(_CHANNEL_HREF.teams(r.teams))}" target="_blank" class="collapse-all-btn sp-ch-btn" title="${escapeHtml(r.teams)}"><i class="ti ti-brand-teams"></i></a>` : ''}
        ${r.telegram   ? `<a href="${escapeHtml(_CHANNEL_HREF.telegram(r.telegram))}" target="_blank" class="collapse-all-btn sp-ch-btn" title="${escapeHtml(r.telegram)}"><i class="ti ti-brand-telegram"></i></a>` : ''}
        ${r.googlechat ? `<button class="collapse-all-btn sp-ch-btn" title="${escapeHtml(r.googlechat)}" onclick="logChatOpened(${gidx},${JSON.stringify(_CHANNEL_HREF.googlechat(r.googlechat))})"><i class="ti ti-brand-google"></i></button>` : ''}
        ${r.messenger  ? `<a href="${escapeHtml(_CHANNEL_HREF.messenger(r.messenger))}" target="_blank" class="collapse-all-btn sp-ch-btn" title="${escapeHtml(r.messenger)}"><i class="ti ti-brand-messenger"></i></a>` : ''}
      </div>
    </div>
    <div class="sp-section sp-primary">
      ${_spSectionHeader('next', 'Next action', 'ti-list-check')}
      <div id="sp-next-body" class="sp-section-body" style="display:${_isSpSectionOpen('next') ? 'block' : 'none'}">
        <div class="sp-quick-actions">
          <button class="btn btn-sm btn-outline-primary" onclick="openFollowMailModal(${gidx}, event)">
            <i class="ti ti-send me-1"></i>Send mail
          </button>
          <button class="btn btn-sm btn-outline-secondary" onclick="quickFollowupAction(${gidx},'tomorrow')">
            <i class="ti ti-calendar-plus me-1"></i>+1 day
          </button>
          <button class="btn btn-sm btn-outline-secondary" onclick="quickFollowupAction(${gidx},'next_week')">
            <i class="ti ti-calendar-plus me-1"></i>+1 week
          </button>
          <button class="btn btn-sm btn-outline-primary" onclick="quickFollowupAction(${gidx},'contacted')">
            <i class="ti ti-check me-1"></i>Contacted
          </button>
          <button class="btn btn-sm btn-outline-secondary" onclick="quickFollowupAction(${gidx},'in_work')">
            <i class="ti ti-clock me-1"></i>In-work
          </button>
          <button class="btn btn-sm btn-outline-danger" onclick="quickFollowupAction(${gidx},'not_interested')">
            <i class="ti ti-circle-x me-1"></i>Not relevant
          </button>
        </div>
        <div class="sp-workbench-grid">
          <div class="sp-row">
            <i class="ti ti-flag" style="margin-top:5px"></i>
            <select class="follow-select small" data-gidx="${gidx}" data-field="followup_status" onchange="saveField(this)">
              ${fuOpts}
            </select>
          </div>
          <div class="sp-row">
            <i class="ti ti-calendar" style="margin-top:5px"></i>
            <input type="date" class="follow-input small ${dateCls}" value="${escapeHtml(r.followup_date || '')}"
              data-gidx="${gidx}" data-field="followup_date" onchange="saveField(this)">
          </div>
          <div class="sp-row">
            <i class="ti ti-star" style="margin-top:5px"></i>
            <select class="imp-select ${impCls} small" data-gidx="${gidx}" data-field="followup_importance"
              onchange="saveField(this);this.className='imp-select small '+(IMPORTANCE_LEVELS.find(i=>i.value===this.value)||IMPORTANCE_LEVELS[0]).cls">
              ${impOpts}
            </select>
          </div>
          <div class="sp-row">
            <i class="ti ti-user-check" style="margin-top:5px"></i>
            <select class="follow-select small" data-gidx="${gidx}" data-field="followup_owner" onchange="saveField(this)">
              <option value="">— unassigned —</option>
              ${_allUsers.map(u=>`<option value="${escapeHtml(u.email)}"${r.followup_owner===u.email?' selected':''}>${escapeHtml(u.displayName ? u.displayName+' ('+u.email+')' : u.email)}</option>`).join('')}
            </select>
          </div>
        </div>
      </div>
    </div>
    <div class="sp-section">
      ${_spSectionHeader('note', 'Note', 'ti-message-circle')}
      <div id="sp-note-body" class="sp-section-body" style="display:${_isSpSectionOpen('note') ? 'block' : 'none'}">
        <textarea class="follow-input small w-100 sp-comment-input sp-note-input"
          placeholder="Add comment…" data-gidx="${gidx}" data-field="followup_comment"
          onchange="saveField(this)"
          onfocus="this.style.height='auto';this.style.height=this.scrollHeight+'px'"
          oninput="this.style.height='auto';this.style.height=this.scrollHeight+'px'"
          rows="3">${escapeHtml(r.followup_comment || '')}</textarea>
      </div>
    </div>
    <div class="sp-section" id="sp-history-section">
      ${_spHistoryHtml(r)}
    </div>
    <div class="sp-section sp-channels-section">
      ${_spSectionHeader('channels', 'Channels', 'ti-plug-connected')}
      <div id="sp-channels-body" class="sp-section-body sp-channels-body" style="display:${_isSpSectionOpen('channels') ? 'block' : 'none'}">
      ${[
        {field:'linkedin',   icon:'ti-brand-linkedin',   ph:'LinkedIn URL or username'},
        {field:'twitter',    icon:'ti-brand-twitter',    ph:'Twitter / X handle or URL'},
        {field:'facebook',   icon:'ti-brand-facebook',   ph:'Facebook profile URL'},
        {field:'instagram',  icon:'ti-brand-instagram',  ph:'Instagram handle or URL'},
        {field:'whatsapp',   icon:'ti-brand-whatsapp',   ph:'WhatsApp number (with country code)'},
        {field:'teams',      icon:'ti-brand-teams',      ph:'Teams email or meeting URL'},
        {field:'telegram',   icon:'ti-brand-telegram',   ph:'Telegram handle or URL'},
        {field:'googlechat', icon:'ti-brand-google',     ph:'Google Chat email or URL', btnIcon:'ti-messages', btnTitle:'Open Chat', logChat:true},
        {field:'messenger',  icon:'ti-brand-messenger',  ph:'Messenger username or URL'},
      ].sort((a, b) => (r[b.field] ? 1 : 0) - (r[a.field] ? 1 : 0)).map(ch => `
        <div class="sp-row align-items-center">
          <i class="ti ${ch.icon}" style="font-size:16px;flex-shrink:0"></i>
          <input type="text" class="follow-input small flex-grow-1"
            value="${escapeHtml(r[ch.field] || '')}" placeholder="${ch.ph}"
            data-gidx="${gidx}" data-field="${ch.field}"
            onchange="saveField(this)"
            onkeydown="if(event.key==='Enter'){event.preventDefault();this.blur()}">
          ${r[ch.field]
            ? (ch.logChat
                ? `<button class="collapse-all-btn sp-ch-btn" style="flex-shrink:0"
                    title="${ch.btnTitle || 'Open Chat'}"
                    onclick="logChatOpened(${gidx},${JSON.stringify(_CHANNEL_HREF.googlechat(r.googlechat))})">
                    <i class="ti ${ch.btnIcon || 'ti-messages'}"></i></button>`
                : `<a href="${escapeHtml(_CHANNEL_HREF[ch.field](r[ch.field]))}" target="_blank"
                    class="collapse-all-btn${ch.btnIcon ? ' sp-ch-btn' : ''}" style="flex-shrink:0"
                    title="${ch.btnTitle || 'Open'}">
                    <i class="ti ${ch.btnIcon || 'ti-external-link'}"></i></a>`)
            : ''}
        </div>`).join('')}
      </div>
    </div>
    <div class="sp-section">
      ${_spSectionHeader('contact', 'Contact methods', 'ti-address-book')}
      <div id="sp-contact-body" class="sp-section-body" style="display:${_isSpSectionOpen('contact') ? 'block' : 'none'}">
        ${r.email ? `<div class="sp-row"><i class="ti ti-mail"></i><a href="mailto:${escapeHtml(r.email)}" class="small">${escapeHtml(r.email)}</a></div>` : ''}
        <div class="sp-row">
          <i class="ti ti-phone" style="margin-top:5px"></i>
          <input type="tel" class="follow-input small flex-grow-1"
            value="${escapeHtml(r.phone || '')}" placeholder="Phone"
            data-gidx="${gidx}" data-field="phone"
            onchange="saveField(this)"
            onkeydown="if(event.key==='Enter'){event.preventDefault();this.blur()}">
        </div>
        ${siteShort ? `<div class="sp-row"><i class="ti ti-world"></i><a href="${escapeHtml(r.website)}" target="_blank" class="small sp-muted">${escapeHtml(siteShort)}</a></div>` : ''}
        ${!r.email && !r.phone && !siteShort ? '<div class="small sp-muted">No contact info.</div>' : ''}
      </div>
    </div>
    <div class="sp-section" id="sp-company-section">
      ${_spSectionHeader('company', 'Company', 'ti-building')}
      <div id="sp-company-body" class="sp-section-body sp-company-body" style="display:${_isSpSectionOpen('company') ? 'block' : 'none'}">
        <div class="small sp-muted py-1">Loading…</div>
      </div>
    </div>
    ${_focusQueueNavHtml(r)}
`;
}

function toggleSpCompany() {
  toggleSpSection('company');
}

async function _loadSpCompany(r) {
  const section = document.getElementById('sp-company-section');
  if (!section) return;
  const body = section.querySelector('.sp-company-body');
  if (!body) return;
  if (!r.website) {
    body.innerHTML = '<div class="small sp-muted py-1">No website on this contact.</div>';
    return;
  }
  const domain = r.website.replace(/^https?:\/\//, '').replace(/\/$/, '').split('/')[0];
  try {
    const resp = await fetch(`${BASE}/api/crm/leads/by-domain/${encodeURIComponent(domain)}`);
    if (!resp.ok) { body.innerHTML = '<div class="small sp-muted py-1">No company data found.</div>'; return; }
    const d = await resp.json();
    const LABEL = {
      location:              'Location',
      location_country:      'Country',
      ai_company_type:       'Type',
      ai_sector:             'Sector',
      ai_platform:           'Platform',
      page_count:            'Pages',
      title:                 'Page title',
      description:           'Description',
      ai_summary:            'AI summary',
      ai_reseller_potential: 'Reseller potential',
      ai_client_base:        'Client base',
      reseller_score:        'Score',
      ai_specialisation:     'Specialisation',
    };
    const ORDER = Object.keys(LABEL);
    let rows = '';
    for (const key of ORDER) {
      const val = d[key];
      if (val === undefined || val === null || val === '' || (Array.isArray(val) && !val.length)) continue;
      let display = '';
      if (Array.isArray(val)) {
        display = val.map(v => `<span class="bb-pill">${escapeHtml(String(v))}</span>`).join('');
      } else if (key === 'ai_summary') {
        display = `<em style="font-size:11px;line-height:1.5;color:#555">${escapeHtml(String(val))}</em>`;
      } else if (key === 'description') {
        display = `<span style="font-size:11px;line-height:1.5;color:#555">${escapeHtml(String(val))}</span>`;
      } else if (key === 'ai_reseller_potential') {
        const color = val === 'high' ? '#15803d' : val === 'medium' ? '#92400e' : '#6b7280';
        display = `<span style="font-weight:500;color:${color}">${escapeHtml(String(val))}</span>`;
      } else if (key === 'page_count') {
        const band = val > 100000 ? 'ultra' : val > 10000 ? 'huge' : val > 3000 ? 'large' : val > 500 ? 'medium' : val > 50 ? 'small' : 'micro';
        display = `${Number(val).toLocaleString()} <span style="font-size:11px;color:var(--bb-muted)">(${band})</span>`;
      } else {
        display = escapeHtml(String(val));
      }
      rows += `<div class="bb-field-label">${LABEL[key]}</div><div class="bb-field-value">${display}</div>`;
    }
    const pipeBadge = d.source_pipeline === 'leads'
      ? '<span class="bb-pill" style="background:#dcfce7;color:#15803d;margin-bottom:6px;display:inline-block">leads pipeline</span>'
      : d.source_pipeline === 'site_leads'
        ? '<span class="bb-pill" style="background:#dbeafe;color:#1d4ed8;margin-bottom:6px;display:inline-block">site pipeline</span>'
        : '';
    if (d.company) {
      const companyEl = section.querySelector('.sp-collapsible-title');
      if (companyEl) companyEl.innerHTML = `<i class="ti ti-building" style="font-size:13px"></i>${escapeHtml(d.company)}`;
    }
    if (!rows) {
      body.innerHTML = pipeBadge + '<div class="small sp-muted py-1">No detailed company data.</div>';
    } else {
      body.innerHTML = (pipeBadge ? `<div>${pipeBadge}</div>` : '') + `<div class="bb-field-grid sp-company-grid">${rows}</div>`;
    }
  } catch(e) {
    body.innerHTML = `<div class="small text-danger py-1">Error: ${escapeHtml(e.message)}</div>`;
  }
}

function _spHistoryHtml(r) {
  if (!r.comment_history || !r.comment_history.length) {
    return `${_spSectionHeader('history', 'Recent history', 'ti-history')}
      <div id="sp-history-body" class="sp-section-body" style="display:${_isSpSectionOpen('history') ? 'block' : 'none'}">
        <div class="small sp-muted">No history yet.</div>
      </div>`;
  }
  const shown = r.comment_history.slice(-3).reverse();
  const suffix = r.comment_history.length > shown.length ? `<div class="small sp-muted mb-1">latest ${shown.length}</div>` : '';
  return `${_spSectionHeader('history', `Recent history (${r.comment_history.length})`, 'ti-history')}
    <div id="sp-history-body" class="sp-section-body" style="display:${_isSpSectionOpen('history') ? 'block' : 'none'}">
      ${suffix}
      ${shown.map(h => `
      <div class="sp-row">
        <i class="ti ti-clock-hour-4"></i>
        <div class="small">
          <span class="sp-muted">${escapeHtml((h.date||'').slice(0,10))} · ${escapeHtml(h.user||'')}</span><br>
          ${escapeHtml(h.text||'')}
        </div>
      </div>`).join('')}
    </div>`;
}

function _refreshSidePanelHistory() {
  const row = allRows[_sidePanelGidx];
  const el  = document.getElementById('sp-history-section');
  if (!row || !el) return;
  el.innerHTML = _spHistoryHtml(row);
}

function _focusQueueNavHtml(r) {
  if (!_focusQueue || !_visibleRows.length) return '';
  const idx = _visibleRows.indexOf(r);
  if (idx < 0) return '';
  return `<div class="sp-queue-nav">
    <button class="btn btn-sm btn-outline-secondary" onclick="focusQueueStep(-1)" ${idx <= 0 ? 'disabled' : ''}>
      <i class="ti ti-chevron-left me-1"></i>Previous
    </button>
    <button class="btn btn-sm btn-primary" onclick="focusQueueStep(1)" ${idx >= _visibleRows.length - 1 ? 'disabled' : ''}>
      Next<i class="ti ti-chevron-right ms-1"></i>
    </button>
  </div>`;
}

function focusQueueStep(delta) {
  if (!_visibleRows.length) return;
  const current = allRows[_sidePanelGidx];
  const idx = Math.max(0, _visibleRows.indexOf(current));
  const nextIdx = Math.min(_visibleRows.length - 1, Math.max(0, idx + delta));
  const row = _visibleRows[nextIdx];
  const gidx = allRows.indexOf(row);
  if (gidx >= 0) openSidePanel(gidx);
}

function _datePlus(days) {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return d.toISOString().slice(0, 10);
}

async function quickFollowupAction(gidx, action) {
  const row = allRows[gidx];
  if (!row) return;

  const actionMap = {
    tomorrow:       { fields: { followup_date: _datePlus(1) }, label: 'Snoozed to tomorrow' },
    next_week:      { fields: { followup_date: _datePlus(7) }, label: 'Snoozed one week' },
    contacted:      { fields: { followup_status: 'contacted', followup_date: _datePlus(7) }, label: 'Marked contacted; next follow-up in one week' },
    in_work:        { fields: { followup_status: 'in_work', followup_date: _datePlus(7) }, label: 'Marked in-work; next follow-up in one week' },
    not_interested: { fields: { followup_status: 'not_interested', followup_date: '' }, label: 'Marked not relevant' },
  };
  const cfg = actionMap[action];
  if (!cfg) return;

  const entry = { date: new Date().toISOString(), type: 'QUICK_ACTION', text: cfg.label };
  try {
    await apiPatchContact(row, { ...cfg.fields, _history_entry: entry });
    Object.assign(row, cfg.fields);
    row.comment_history = row.comment_history || [];
    row.comment_history.push({ ...entry, user: (window._authUser && window._authUser.email) || '' });
    setFeedback(`<i class="ti ti-check me-1"></i>${escapeHtml(cfg.label)} for <strong>${escapeHtml(row.name || row.email || 'contact')}</strong>.`, 'success');
    applyFilter();
    const newIdx = allRows.indexOf(row);
    if (_sideOpen && newIdx >= 0 && (!_focusQueue || document.querySelector(`tr[data-doc="${CSS.escape(row.doc_path)}"]`))) {
      openSidePanel(newIdx);
    }
  } catch(e) {
    setFeedback(`<i class="ti ti-circle-x me-1"></i>Quick action failed: ${escapeHtml(e.message)}`, 'danger');
  }
}

async function saveField(el) {
  const gidx  = parseInt(el.dataset.gidx, 10);
  const field = el.dataset.field;
  const value = el.value;
  const row   = allRows[gidx];
  if (!row) return;

  if ((row[field] || '') === value) return;
  const oldValue = row[field] || '';
  row[field] = value;

  const container = el.closest('td') || el.closest('.sp-row');
  let dot = container ? container.querySelector('.save-dot') : null;
  if (container) {
    if (!dot) { dot = document.createElement('span'); dot.className = 'save-dot'; container.appendChild(dot); }
    dot.classList.remove('err', 'ok');
    dot.title = '';
  }

  try {
    await apiPatchContact(row, { [field]: value });

    const user = (window._authUser && (window._authUser.email || window._authUser.uid)) || 'unknown';
    const entry = buildLocalHistoryEntry(field, value, user);
    if (entry) {
      row.comment_history.push(entry);
      refreshHistoryPanel(gidx);
      if (_sidePanelGidx === gidx) _refreshSidePanelHistory();
    }

    // Sync other DOM inputs for the same field (table ↔ side panel)
    document.querySelectorAll(`[data-gidx="${gidx}"][data-field="${field}"]`).forEach(other => {
      if (other !== el) other.value = value;
    });

    if (dot) { dot.classList.add('ok'); setTimeout(() => dot.remove(), 900); }
  } catch (e) {
    console.error('[crm_follow] save failed:', e.message);
    row[field] = oldValue;
    if (dot) { dot.classList.add('err'); dot.title = 'Save failed: ' + e.message; }
  }
}

function buildLocalHistoryEntry(field, value, user) {
  const typeMap = {
    followup_status:     'STATUS',
    followup_comment:    'COMMENT',
    followup_date:       'FOLLOWUP',
    followup_importance: 'IMPORTANCE',
    followup_owner:      'OWNER',
  };
  const textMap = {
    followup_status:     v => { const l = (FU_STATUSES.find(s => s.value === v) || {}).label || v || '(none)'; return `Status → ${l}`; },
    followup_comment:    v => v || '(comment cleared)',
    followup_date:       v => v ? `Follow-up date set to ${v}` : 'Follow-up date cleared',
    followup_importance: v => { const l = (IMPORTANCE_LEVELS.find(i => i.value === v) || {}).label || v || '(none)'; return `Importance → ${l}`; },
    followup_owner:      v => v ? `Owner → ${v}` : 'Owner cleared',
  };
  if (!typeMap[field]) return null;
  return { date: new Date().toISOString(), user, text: textMap[field](value), type: typeMap[field] };
}

