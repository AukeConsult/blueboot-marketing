'use strict';
// ── Render ────────────────────────────────────────────────────────────────────

// FU_STATUSES and IMPORTANCE_LEVELS are defined in crm-defs.js (shared).

function currentFollowupStatus(value) {
  const st = String(value || '').trim().toLowerCase();
  return FU_STATUSES.some(s => s.value === st) ? st : '';
}

const GROUP_FIELDS = {
  followup_status: {
    label:   'Follow-up status',
    order:   FU_STATUSES.map(s => s.value),
    labelFn: v => (FU_STATUSES.find(s => s.value === v) || { label: 'No status' }).label,
  },
  status: {
    label:   'Contact status',
    order:   ['active', 'pending', 'excluded', ''],
    labelFn: v => v || 'No contact status',
  },
  followup_importance: {
    label:   'Importance',
    order:   ['high', 'medium', 'low', ''],
    labelFn: v => (IMPORTANCE_LEVELS.find(i => i.value === v) || { label: '— —' }).label,
  },
  owner: {
    label:   'Owner',
    order:   null,
    labelFn: v => v || '(no owner)',
  },
  followup_owner: {
    label:   'Follow-up owner',
    order:   null,
    labelFn: v => v || '(unassigned)',
  },
  followup_date: {
    label:   'Follow-up date',
    order:   null,
    labelFn: v => {
      if (!v) return 'No date set';
      try { return new Date(v + 'T00:00:00').toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' }); }
      catch(e) { return v; }
    },
  },
};


function statusBadge(s) {
  const status = ['pending', 'active', 'excluded'].includes(s) ? s : 'pending';
  const map = { pending: 'bb-badge-pending', active: 'bb-badge-active', excluded: 'bb-badge-excluded' };
  return `<span class="bb-badge ${map[status]}">${escapeHtml(status)}</span>`;
}


function renderContactListCells(r, gidx, siteShort, fuOpts, impCls, dateCls) {
  const phoneHtml = r.phone
    ? `<span class="crm-phone-meta"><i class="ti ti-phone"></i><input type="tel" class="follow-input crm-inline-input" value="${escapeHtml(r.phone || '')}" placeholder="Phone" data-gidx="${gidx}" data-field="phone" onchange="saveField(this)" onkeydown="if(event.key==='Enter'){event.preventDefault();this.blur()}"></span>`
    : `<span class="crm-phone-meta"><i class="ti ti-phone"></i><input type="tel" class="follow-input crm-inline-input" value="" placeholder="Phone" data-gidx="${gidx}" data-field="phone" onchange="saveField(this)" onkeydown="if(event.key==='Enter'){event.preventDefault();this.blur()}"></span>`;
  const emailHtml = `<span class="crm-email-meta"><i class="ti ti-mail"></i><a href="mailto:${escapeHtml(r.email)}" title="${escapeHtml(r.email || '')}">${escapeHtml(r.email || '—')}</a>${r.new_mail ? `<button class="newmail-btn" title="New mail — click to acknowledge" onclick="ackNewMail(${gidx},event)"><i class="ti ti-mail-opened"></i></button>` : ''}</span>`;
  const websiteHtml = siteShort
    ? `<span class="crm-website-meta"><i class="ti ti-world"></i><a href="${escapeHtml(r.website)}" target="_blank" title="${escapeHtml(r.website)}">${escapeHtml(siteShort)}</a></span>`
    : `<span class="crm-website-meta"><i class="ti ti-world"></i><span>—</span></span>`;
  return `<td class="text-center"><input type="checkbox" class="form-check-input row-chk"
        data-gidx="${gidx}" onchange="onRowCheck(this)"></td>
      <td class="crm-contact-cell">
        <div class="crm-contact-main">
          <div class="crm-contact-name">
            <a class="crm-contact-name-link small fw-500"
              href="crm-contact.html?campaign=${encodeURIComponent(r.campaign_id)}&doc=${encodeURIComponent(r.doc_id)}"
              title="Open full contact page">${escapeHtml(r.name || '—')}</a>
            <div class="contact-title small text-muted">${escapeHtml(r.title || '')}</div>
          </div>
          <div class="crm-contact-actions">
            ${statusBadge(r.status)}
            <a class="btn btn-sm btn-outline-primary crm-open-btn"
              href="crm-contact.html?campaign=${encodeURIComponent(r.campaign_id)}&doc=${encodeURIComponent(r.doc_id)}"
              title="Open full contact page">
              <i class="ti ti-external-link me-1"></i>Open
            </a>
            <button class="detail-btn" onclick="openSidePanelFromButton(${gidx}, event)" title="Quick view (side panel)">
              <i class="ti ti-layout-sidebar-right"></i>
            </button>
          </div>
        </div>
        <div class="crm-contact-meta">
          ${emailHtml}
          ${phoneHtml}
          ${websiteHtml}
        </div>
      </td>
      <td class="crm-followup-cell">
        <div class="crm-followup-grid">
          <div><span class="crm-mobile-label">Follow-up status</span><select class="follow-select"
            data-gidx="${gidx}" data-field="followup_status"
            onchange="saveField(this)">${fuOpts}</select></div>
          <div><span class="crm-mobile-label">Date</span><input type="date" class="follow-input ${dateCls}"
            value="${escapeHtml(r.followup_date || '')}"
            data-gidx="${gidx}" data-field="followup_date"
            onchange="saveField(this)"></div>
          <div><span class="crm-mobile-label">Importance</span><select class="imp-select ${impCls}"
            data-gidx="${gidx}" data-field="followup_importance"
            onchange="saveField(this);this.className='imp-select '+(IMPORTANCE_LEVELS.find(i=>i.value===this.value)||IMPORTANCE_LEVELS[0]).cls">
            ${IMPORTANCE_LEVELS.map(i=>`<option value="${i.value}"${r.followup_importance===i.value?' selected':''}>${i.label}</option>`).join('')}
          </select></div>
        </div>
      </td>`;
}

// Mail sending from the follow-up list / side-panel was removed.
// Send mail to a contact from the full contact page (crm-contact.html).

function render(list) {
  if (_currentView === 'group') { renderGrouped(list); return; }
  const empty = document.getElementById('follow-empty');
  if (!list.length) { setTbody(''); empty.style.display = ''; return; }
  empty.style.display = 'none';

  const restoreChecks = () => {
    document.querySelectorAll('.row-chk').forEach(c => {
      const row = allRows[parseInt(c.dataset.gidx, 10)];
      if (row && selected.has(row.doc_path)) c.checked = true;
    });
    _syncSelectAllCheckbox({ allowChecked: _selectAllActive });
  };

  setTbody(list.map(r => {
    const gidx      = allRows.indexOf(r);
    const siteShort = r.website
      ? r.website.replace(/^https?:\/\//, '').replace(/\/$/, '').slice(0, 30) : '';
    const fuOpts    = FU_STATUSES.map(s =>
      `<option value="${s.value}"${r.followup_status === s.value ? ' selected' : ''}>${escapeHtml(s.label)}</option>`
    ).join('');
    const impCls  = (IMPORTANCE_LEVELS.find(i => i.value === r.followup_importance) || IMPORTANCE_LEVELS[0]).cls;
    const dueCls  = dueDateClass(r.followup_date);
    const rowCls  = dueCls === 'overdue' ? 'row-overdue' : (dueCls === 'due-soon' || dueCls === 'due-today') ? 'row-due-soon' : '';
    const dateCls = dueCls === 'overdue' ? 'date-overdue' : (dueCls === 'due-soon' || dueCls === 'due-today') ? 'date-due-soon' : '';

    return `<tr data-doc="${escapeHtml(r.doc_path)}" class="follow-data-row ${rowCls}" onclick="onRowClick(event,${gidx})" style="cursor:pointer">
      ${renderContactListCells(r, gidx, siteShort, fuOpts, impCls, dateCls)}
    </tr>
    <tr class="follow-comment-row">
      <td colspan="3">
        <div class="d-flex align-items-center gap-2 comment-row-inner">
          <i class="ti ti-message-circle comment-row-icon"></i>
          <textarea class="follow-input comment-row-input flex-grow-1"
            placeholder="Add a comment…"
            data-gidx="${gidx}" data-field="followup_comment"
            onchange="saveField(this)"
            onfocus="onRowExpand(${gidx})"
            oninput="this.style.height='auto';this.style.height=this.scrollHeight+'px'"
            rows="1">${escapeHtml(r.followup_comment || '')}</textarea>
          <button class="hist-toggle" onclick="toggleHistory(this,${gidx})" title="Show comment history">
            <i class="ti ti-chevron-down"></i>
            <span class="hist-count${r.comment_history.length ? ' has-entries' : ''}">${r.comment_history.length}</span>
          </button>
        </div>
      </td>
    </tr>
    <tr class="follow-history-row" id="hist-${gidx}" style="display:none">
      <td colspan="3" class="hist-row-td">${renderHistoryContent(r.comment_history)}</td>
    </tr>`;
  }).join(''));
  restoreChecks();
  // Recompute column widths now that the table is populated at the current viewport
  requestAnimationFrame(_relayoutTable);
}


// ── Grouped view ──────────────────────────────────────────────────────────────

function _orderedGroupKeys(rows, field, def) {
  const vals = [...new Set(rows.map(r => r[field] || ''))];
  if (def.order) {
    const ordered = def.order.filter(v => vals.includes(v));
    const extra   = vals.filter(v => !def.order.includes(v)).sort();
    return [...ordered, ...extra];
  }
  // Natural sort; always put empty string last
  const nonEmpty = vals.filter(v => v !== '').sort();
  return vals.includes('') ? [...nonEmpty, ''] : nonEmpty;
}

function _contactRowsHtml(r) {
  const gidx      = allRows.indexOf(r);
  const siteShort = r.website
    ? r.website.replace(/^https?:\/\//, '').replace(/\/$/, '').slice(0, 30) : '';
  const fuOpts = FU_STATUSES.map(s =>
    `<option value="${s.value}"${r.followup_status === s.value ? ' selected' : ''}>${escapeHtml(s.label)}</option>`
  ).join('');
  const impCls  = (IMPORTANCE_LEVELS.find(i => i.value === r.followup_importance) || IMPORTANCE_LEVELS[0]).cls;
  const dueCls  = dueDateClass(r.followup_date);
  const rowCls  = dueCls === 'overdue' ? 'row-overdue' : (dueCls === 'due-soon' || dueCls === 'due-today') ? 'row-due-soon' : '';
  const dateCls = dueCls === 'overdue' ? 'date-overdue' : (dueCls === 'due-soon' || dueCls === 'due-today') ? 'date-due-soon' : '';
  return `<tr data-doc="${escapeHtml(r.doc_path)}" class="follow-data-row ${rowCls}" onclick="onRowClick(event,${gidx})" style="cursor:pointer">
      ${renderContactListCells(r, gidx, siteShort, fuOpts, impCls, dateCls)}
    </tr>
    <tr class="follow-comment-row">
      <td colspan="3">
        <div class="d-flex align-items-center gap-2 comment-row-inner">
          <i class="ti ti-message-circle comment-row-icon"></i>
          <textarea class="follow-input comment-row-input flex-grow-1"
            placeholder="Add a comment…"
            data-gidx="${gidx}" data-field="followup_comment"
            onchange="saveField(this)"
            onfocus="onRowExpand(${gidx})"
            oninput="this.style.height='auto';this.style.height=this.scrollHeight+'px'"
            rows="1">${escapeHtml(r.followup_comment || '')}</textarea>
          <button class="hist-toggle" onclick="toggleHistory(this,${gidx})" title="Show comment history">
            <i class="ti ti-chevron-down"></i>
            <span class="hist-count${r.comment_history.length ? ' has-entries' : ''}">${r.comment_history.length}</span>
          </button>
        </div>
      </td>
    </tr>
    <tr class="follow-history-row" id="hist-${gidx}" style="display:none">
      <td colspan="3" class="hist-row-td">${renderHistoryContent(r.comment_history)}</td>
    </tr>`;
}

function renderGrouped(list) {
  const empty = document.getElementById('follow-empty');
  if (!list.length) { setTbody(''); empty.style.display = ''; return; }
  empty.style.display = 'none';

  const primaryField   = document.getElementById('group-primary')?.value  || 'followup_status';
  const secondaryField = document.getElementById('group-secondary')?.value || '';
  const pDef = GROUP_FIELDS[primaryField]                   || GROUP_FIELDS.followup_status;
  const sDef = secondaryField ? (GROUP_FIELDS[secondaryField] || null) : null;

  const pKeys = _orderedGroupKeys(list, primaryField, pDef);
  _currentGroupKeys = pKeys.map(pVal => 'p:' + primaryField + ':' + pVal);

  // Update collapse-all button label
  const cab = document.getElementById('collapse-all-btn');
  if (cab) {
    const allCollapsed = _currentGroupKeys.length > 0 && _currentGroupKeys.every(k => _collapsedGroups.has(k));
    cab.innerHTML = allCollapsed
      ? '<i class="ti ti-arrows-maximize"></i>'
      : '<i class="ti ti-arrows-minimize"></i>';
    cab.title = allCollapsed ? 'Expand all groups' : 'Collapse all groups';
  }

  let html = '';

  _groupDocPaths = {};

  pKeys.forEach(pVal => {
    const pRows      = list.filter(r => (r[primaryField] || '') === pVal);
    const pLabel     = pDef.labelFn(pVal);
    const pKey       = 'p:' + primaryField + ':' + pVal;
    const pCollapsed = _collapsedGroups.has(pKey);

    _groupDocPaths[pKey] = pRows.map(r => r.doc_path);

    html += `<tr class="group-header-row" data-group-hdr="${escapeHtml(pKey)}" onclick="toggleGroup('${escapeHtml(pKey)}')">
      <td colspan="3"><input type="checkbox" class="form-check-input group-chk" data-gkey="${escapeHtml(pKey)}" onclick="toggleGroupSelect(event,'${escapeHtml(pKey)}')" tabindex="-1"><i class="ti ti-tag me-1"></i>${escapeHtml(pLabel)}<span class="group-count">${pRows.length} contact${pRows.length === 1 ? '' : 's'}</span><i class="ti ti-chevron-down group-chevron${pCollapsed ? ' collapsed' : ''}"></i></td>
    </tr>`;

    if (pCollapsed) return;

    if (sDef) {
      const sKeys = _orderedGroupKeys(pRows, secondaryField, sDef);
      sKeys.forEach(sVal => {
        const sRows      = pRows.filter(r => (r[secondaryField] || '') === sVal);
        const sLabel     = sDef.labelFn(sVal);
        const sKey       = 's:' + primaryField + ':' + pVal + ':' + secondaryField + ':' + sVal;
        const sCollapsed = _collapsedGroups.has(sKey);

        _groupDocPaths[sKey] = sRows.map(r => r.doc_path);

        html += `<tr class="group-subheader-row" data-group-hdr="${escapeHtml(sKey)}" onclick="toggleGroup('${escapeHtml(sKey)}')">
          <td colspan="3"><input type="checkbox" class="form-check-input group-chk" data-gkey="${escapeHtml(sKey)}" onclick="toggleGroupSelect(event,'${escapeHtml(sKey)}')" tabindex="-1"><i class="ti ti-corner-down-right me-1"></i>${escapeHtml(sLabel)}<span class="group-count">${sRows.length} contact${sRows.length === 1 ? '' : 's'}</span><i class="ti ti-chevron-down group-chevron${sCollapsed ? ' collapsed' : ''}"></i></td>
        </tr>`;

        if (!sCollapsed) html += sRows.map(r => _contactRowsHtml(r)).join('');
      });
    } else {
      html += pRows.map(r => _contactRowsHtml(r)).join('');
    }
  });

  setTbody(html);
  document.querySelectorAll('.row-chk').forEach(c => {
    const row = allRows[parseInt(c.dataset.gidx, 10)];
    if (row && selected.has(row.doc_path)) c.checked = true;
  });
  _syncSelectAllCheckbox({ allowChecked: _selectAllActive });
  _syncGroupCheckboxes();
}
