'use strict';
// ── Batch date update ─────────────────────────────────────────────────────────

function getSyncCutoff() {
  const days = parseInt(document.getElementById('sync-period')?.value || '7', 10);
  if (!days) return null;
  const d = new Date(); d.setDate(d.getDate() - days); return d;
}

function showBatchDatePanel() {
  // Populate status options from FU_STATUSES (skip the '— none —' blank entry)
  const sel = document.getElementById('batch-date-status-select');
  sel.innerHTML = '<option value="">— keep current —</option>'
    + FU_STATUSES.filter(s => s.value).map(s =>
        `<option value="${s.value}">${escapeHtml(s.label)}</option>`).join('');
  _showActionPanel('batch-date-panel');
  document.getElementById('batch-apply-count').textContent = selected.size;
  document.getElementById('batch-date-status').textContent = '';
  document.getElementById('batch-date-input').focus();
}

function hideBatchDatePanel() {
  _hideActionPanel('batch-date-panel');
}

async function applyBatchDate() {
  const date    = document.getElementById('batch-date-input').value;
  const comment = document.getElementById('batch-date-comment').value.trim();
  const statusEl = document.getElementById('batch-date-status');
  if (!date) { statusEl.style.color = '#dc2626'; statusEl.textContent = 'Please pick a date first.'; return; }

  const rows = allRows.filter(r => selected.has(r.doc_path));
  if (!rows.length) return;

  statusEl.style.color = 'var(--bb-muted)';
  statusEl.textContent = `Updating ${rows.length} contact${rows.length === 1 ? '' : 's'}…`;

  let done = 0, errors = 0;
  await Promise.all(rows.map(async r => {
    try {
      const statusVal = document.getElementById('batch-date-status-select').value;
      const fields = { followup_date: date };
      if (comment)   fields.followup_comment  = comment;
      if (statusVal) fields.followup_status   = statusVal;
      await apiPatchContact(r, fields);
      r.followup_date = date;
      if (comment)   r.followup_comment = comment;
      if (statusVal) r.followup_status  = statusVal;
      const user = (window._authUser && (window._authUser.email || window._authUser.uid)) || 'unknown';
      r.comment_history.push({ date: new Date().toISOString(), user,
        text: comment ? `Follow-up date set to ${date} — ${comment}` : `Follow-up date set to ${date}`,
        type: 'FOLLOWUP' });
      done++;
    } catch (e) { errors++; console.error('[batch-date]', r.email, e.message); }
  }));

  if (errors) { statusEl.style.color = '#dc2626'; statusEl.textContent = `${done} updated, ${errors} failed.`; }
  else        { statusEl.style.color = '#15803d'; statusEl.textContent = `${done} contact${done === 1 ? '' : 's'} updated.`; }

  applyFilter();
  if (!errors) { hideBatchDatePanel(); clearSelection(); }
}

// ── Action bar panel helpers ─────────────────────────────────────────────────

const _ACTION_PANELS = ['batch-date-panel', 'move-panel', 'assign-owner-panel'];

function _showActionPanel(id) {
  _ACTION_PANELS.forEach(p => {
    document.getElementById(p).style.display = p === id ? '' : 'none';
  });
  // Highlight active button
  document.getElementById('ab-date-btn').classList.toggle('btn-primary', id === 'batch-date-panel');
  document.getElementById('ab-date-btn').classList.toggle('btn-outline-primary', id !== 'batch-date-panel');
  document.getElementById('ab-move-btn').classList.toggle('btn-secondary', id === 'move-panel');
  document.getElementById('ab-move-btn').classList.toggle('btn-outline-secondary', id !== 'move-panel');
  document.getElementById('ab-owner-btn').classList.toggle('btn-secondary', id === 'assign-owner-panel');
  document.getElementById('ab-owner-btn').classList.toggle('btn-outline-secondary', id !== 'assign-owner-panel');
}

function _hideActionPanel(id) {
  document.getElementById(id).style.display = 'none';
  if (id === 'batch-date-panel') {
    document.getElementById('ab-date-btn').classList.replace('btn-primary', 'btn-outline-primary');
  }
  if (id === 'move-panel') {
    document.getElementById('ab-move-btn').classList.replace('btn-secondary', 'btn-outline-secondary');
  }
  if (id === 'assign-owner-panel') {
    document.getElementById('ab-owner-btn').classList.replace('btn-secondary', 'btn-outline-secondary');
  }
}

// ── Assign owner (batch) ─────────────────────────────────────────────────────

function showAssignOwnerPanel() {
  if (!selected.size) return;
  _showActionPanel('assign-owner-panel');
  document.getElementById('assign-owner-count').textContent = selected.size;
  document.getElementById('assign-owner-status').textContent = '';
  const sel = document.getElementById('assign-owner-select');
  sel.innerHTML = '<option value="">— clear owner —</option>'
    + _allUsers.map(u => `<option value="${escapeHtml(u.email)}">${escapeHtml(u.displayName ? u.displayName + ' (' + u.email + ')' : u.email)}</option>`).join('');
}

function hideAssignOwnerPanel() {
  _hideActionPanel('assign-owner-panel');
}

async function applyAssignOwner() {
  const ownerEmail = document.getElementById('assign-owner-select').value;
  const statusEl   = document.getElementById('assign-owner-status');
  const btn        = document.getElementById('assign-owner-apply-btn');
  const rows       = allRows.filter(r => selected.has(r.doc_path));
  if (!rows.length) return;

  btn.disabled = true;
  statusEl.style.color = 'var(--bb-muted)';
  statusEl.textContent = `Updating ${rows.length} contact${rows.length === 1 ? '' : 's'}…`;

  let done = 0, errors = 0;
  for (const r of rows) {
    try {
      await apiPatchContact(r, { followup_owner: ownerEmail });
      r.followup_owner = ownerEmail;
      buildLocalHistoryEntry(r, 'followup_owner', ownerEmail);
      done++;
    } catch(e) {
      errors++;
      console.error('[assign-owner]', r.email, e.message);
    }
  }

  if (errors) {
    statusEl.style.color = '#dc2626';
    statusEl.textContent = `Done with ${errors} error${errors === 1 ? '' : 's'}.`;
  } else {
    hideAssignOwnerPanel();
    clearSelection();
  }

  btn.disabled = false;
  applyFilter();
}

// ── Move contacts ────────────────────────────────────────────────────────────

function showMovePanel() {
  if (!selected.size) return;
  _showActionPanel('move-panel');
  document.getElementById('move-count').textContent = selected.size;
  document.getElementById('move-status').textContent = '';
  // Populate existing-campaign dropdown (exclude current campaign if single-campaign view)
  const currentCampId = document.getElementById('campaign-filter').value;
  const sel = document.getElementById('move-campaign-select');
  sel.innerHTML = _allCampaigns
    .filter(c => c.id !== currentCampId)
    .map(c => `<option value="${escapeHtml(c.id)}">${escapeHtml(c.id)}</option>`)
    .join('');
  // Reset to existing option
  document.getElementById('move-opt-existing').checked = true;
  onMoveOptionChange();
  document.getElementById('move-new-name').value = '';
}

function hideMovePanel() {
  _hideActionPanel('move-panel');
}

function onMoveOptionChange() {
  const isNew = document.getElementById('move-opt-new').checked;
  document.getElementById('move-existing-wrap').style.display = isNew ? 'none' : '';
  document.getElementById('move-new-wrap').style.display     = isNew ? '' : 'none';
  if (isNew) document.getElementById('move-new-name').focus();
}

async function applyMove() {
  const isNew   = document.getElementById('move-opt-new').checked;
  const statusEl = document.getElementById('move-status');
  const btn      = document.getElementById('move-apply-btn');

  let body = {};
  if (isNew) {
    const name = document.getElementById('move-new-name').value.trim();
    if (!name) { statusEl.style.color = '#dc2626'; statusEl.textContent = 'Enter a campaign name.'; return; }
    body.new_campaign_name = name;
  } else {
    const campId = document.getElementById('move-campaign-select').value;
    if (!campId) { statusEl.style.color = '#dc2626'; statusEl.textContent = 'Select a target campaign.'; return; }
    body.target_campaign_id = campId;
  }

  // Group selected rows by source campaign_id
  const rowsToMove = allRows.filter(r => selected.has(r.doc_path));
  if (!rowsToMove.length) return;

  // Group by campaign_id (multi-campaign view support)
  const byCampaign = {};
  for (const r of rowsToMove) {
    if (!byCampaign[r.campaign_id]) byCampaign[r.campaign_id] = [];
    byCampaign[r.campaign_id].push(r.doc_id);
  }

  btn.disabled = true;
  statusEl.style.color = 'var(--bb-muted)';
  statusEl.textContent = `Moving ${rowsToMove.length} contact${rowsToMove.length === 1 ? '' : 's'}…`;

  const targetName = body.target_campaign_id || body.new_campaign_name;

  // Fire one job per source campaign (multi-campaign view support)
  const jobIds = [];
  try {
    for (const [srcCampId, docIds] of Object.entries(byCampaign)) {
      const res = await fetchJSON(
        `${BASE}/api/crm/campaigns/${encodeURIComponent(srcCampId)}/contacts/move`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...body, doc_ids: docIds }) }
      );
      if (res.job_id) jobIds.push(res.job_id);
    }
  } catch (e) {
    statusEl.style.color = '#dc2626';
    statusEl.textContent = `Failed: ${e.message}`;
    btn.disabled = false;
    return;
  }

  hideMovePanel();
  const movedPaths = new Set(rowsToMove.map(r => r.doc_path));

  // Poll all jobs; when all done remove rows and show feedback
  setFeedback(`<i class="ti ti-clock me-1"></i>Moving ${rowsToMove.length} contact${rowsToMove.length===1?'':'s'} to <strong>${escapeHtml(targetName)}</strong> — please wait…`, 'info');
  let totalMoved = 0, anyError = false;
  for (const jid of jobIds) {
    await new Promise(resolve => {
      pollJob(jid, {
        onDone: result => {
          totalMoved += (result.moved || 0);
          if (result.errors && result.errors.length) {
            anyError = true;
            console.warn('[move] job errors', jid, result.errors);
          }
          resolve();
        },
        onError: msg => {
          anyError = true;
          console.warn('[move] job failed', jid, msg);
          resolve();
        },
      });
    });
  }

  // Remove moved rows from allRows only if they were actually moved
  if (totalMoved > 0) {
    for (let i = allRows.length - 1; i >= 0; i--) {
      if (movedPaths.has(allRows[i].doc_path)) allRows.splice(i, 1);
    }
  }
  clearSelection();
  applyFilter();
  if (totalMoved === 0 && anyError) {
    setFeedback(`<i class="ti ti-circle-x me-1"></i>Move failed — check Cloud Function logs for details.`, 'danger');
  } else {
    setFeedback(
      `<i class="ti ti-check me-1"></i>${totalMoved} contact${totalMoved===1?'':'s'} moved to <strong>${escapeHtml(targetName)}</strong>${anyError ? ' (some may have failed — check logs)' : ''}.`,
      anyError ? 'warning' : 'success'
    );
  }
  btn.disabled = false;
}

// ── Selection ─────────────────────────────────────────────────────────────────

function onRowCheck(chk) {
  _selectAllActive = false;
  const row = allRows[parseInt(chk.dataset.gidx, 10)];
  if (!row) return;
  if (chk.checked) selected.add(row.doc_path); else selected.delete(row.doc_path);
  _syncGroupCheckboxes();
  _syncSelectAllCheckbox({ allowChecked: false });
  updateSelectionUI();
}

function toggleSelectAll(chk) {
  _selectAllActive = chk.checked;
  chk.indeterminate = false;
  document.querySelectorAll('.row-chk').forEach(c => {
    c.checked = chk.checked;
    const row = allRows[parseInt(c.dataset.gidx, 10)];
    if (row) {
      if (chk.checked) selected.add(row.doc_path);
      else selected.delete(row.doc_path);
    }
  });
  _syncGroupCheckboxes();
  _syncSelectAllCheckbox({ allowChecked: true });
  updateSelectionUI();
}

function toggleGroupSelect(event, groupKey) {
  event.stopPropagation();
  _selectAllActive = false;
  const chk = event.currentTarget;
  const paths = new Set(_groupDocPaths[groupKey] || []);
  paths.forEach(path => {
    if (chk.checked) selected.add(path);
    else selected.delete(path);
  });
  document.querySelectorAll('.row-chk').forEach(c => {
    const row = allRows[parseInt(c.dataset.gidx, 10)];
    if (row && paths.has(row.doc_path)) c.checked = chk.checked;
  });
  _syncGroupCheckboxes();
  _syncSelectAllCheckbox({ allowChecked: false });
  updateSelectionUI();
}

function _syncGroupCheckboxes() {
  document.querySelectorAll('.group-chk').forEach(gc => {
    const paths = _groupDocPaths[gc.dataset.gkey] || [];
    const checkedCount = paths.filter(p => selected.has(p)).length;
    gc.checked = paths.length > 0 && checkedCount === paths.length;
    gc.indeterminate = checkedCount > 0 && checkedCount < paths.length;
  });
}

function _syncSelectAllCheckbox(opts = {}) {
  const allowChecked = opts.allowChecked !== false;
  const sa = document.getElementById('select-all');
  if (!sa) return;
  const rowChecks = [...document.querySelectorAll('.row-chk')];
  const checkedCount = rowChecks.filter(c => c.checked).length;
  if (!rowChecks.length || checkedCount === 0) {
    sa.checked = false;
    sa.indeterminate = false;
    return;
  }
  const allVisibleChecked = checkedCount === rowChecks.length;
  sa.checked = allowChecked && allVisibleChecked;
  sa.indeterminate = !sa.checked && checkedCount > 0;
}

function clearSelection() {
  _selectAllActive = false;
  selected.clear();
  _ACTION_PANELS.forEach(p => { document.getElementById(p).style.display = 'none'; });
  document.querySelectorAll('.row-chk').forEach(c => { c.checked = false; });
  document.querySelectorAll('.group-chk').forEach(c => { c.checked = false; c.indeterminate = false; });
  const sa = document.getElementById('select-all');
  if (sa) { sa.checked = false; sa.indeterminate = false; }
  updateSelectionUI();
}

function updateSelectionUI() {
  const n = selected.size;
  document.getElementById('batch-count').textContent = n;
  document.getElementById('action-bar').style.display = n > 0 ? '' : 'none';
  const ac = document.getElementById('batch-apply-count');
  if (ac) ac.textContent = n;
  if (n === 0) hideBatchDatePanel();
}

// ── Feedback div (matches pattern on crm-sync.html / campaign.html) ───────────

function setFeedback(msg, type) {
  const fb = document.getElementById('sync-feedback');
  if (!type) { fb.style.display = 'none'; return; }
  fb.className = `alert alert-${type} py-2 px-3 small mb-3`;
  fb.innerHTML = msg;
  fb.style.display = '';
}

// ── Email sync (backend job + poll) ──────────────────────────────────────────

async function syncContactEmails(gidx, btn) {
  const row = allRows[gidx];
  if (!row || !row.email) { alert('Contact has no email address.'); return; }
  btn.classList.add('spinning'); btn.disabled = true;
  try {
    const days     = parseInt(document.getElementById('sync-period')?.value || '7', 10);
    const res  = await fetchJSON(`${BASE}/api/crm/inbound-read`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ campaign_ids: [row.campaign_id], contact_doc_id: row.doc_id, days }),
    });
    setFeedback(
      `<i class="ti ti-clock me-1"></i>Email sync queued <code>${res.job_id}</code>`
      + ` for <strong>${escapeHtml(row.email)}</strong> — polling…`,
      'info'
    );
    await pollJob(res.job_id, {
      onDone: async result => {
        const n = result.synced_entries || 0;
        const u = result.updated_contacts ?? result.synced_contacts ?? 0;
        setFeedback(
          `<i class="ti ti-check me-1"></i><strong>${escapeHtml(row.email)}</strong>`
          + ` — ${n} new email${n === 1 ? '' : 's'} synced; `
          + `<strong>${u}</strong> contact${u === 1 ? '' : 's'} updated.`,
          n > 0 ? 'success' : 'info'
        );
        if (n > 0) await refreshContactHistory(gidx);
      },
      onError: msg => setFeedback(
        `<i class="ti ti-circle-x me-1"></i>Sync failed for <strong>${escapeHtml(row.email)}</strong>: ${escapeHtml(msg)}`,
        'danger'
      ),
    });
  } catch (e) {
    setFeedback(`<i class="ti ti-circle-x me-1"></i>Error: ${escapeHtml(e.message)}`, 'danger');
  } finally {
    btn.classList.remove('spinning'); btn.disabled = false;
  }
}

async function syncAllEmails() {
  const btn = document.getElementById('sync-all-btn');
  btn.disabled  = true;
  btn.innerHTML = '<i class="ti ti-loader me-1" style="animation:spin .7s linear infinite;display:inline-block"></i>Queuing…';
  setFeedback(null);
  try {
    const days     = parseInt(document.getElementById('sync-period')?.value || '7', 10);
    const camp     = document.getElementById('campaign-filter')?.value || '';
    const res  = await fetchJSON(`${BASE}/api/crm/inbound-read`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        days,
        ...(camp ? { campaign_ids: [camp] } : {}),
      }),
    });
    setFeedback(
      `<i class="ti ti-clock me-1"></i>Email sync queued <code>${res.job_id}</code>`
      + ` (last ${days} day${days === 1 ? '' : 's'}) — polling…`,
      'info'
    );
    await pollJob(res.job_id, {
      onDone: result => {
        const n = result.synced_entries || 0;
        const c = result.synced_contacts || 0;
        const u = result.updated_contacts ?? c;
        setFeedback(
          `<i class="ti ti-check me-1"></i>Sync complete — `
          + `<strong>${n}</strong> new email${n === 1 ? '' : 's'} across `
          + `<strong>${c}</strong> contact${c === 1 ? '' : 's'}; `
          + `<strong>${u}</strong> updated.`
          + (result.errors && result.errors.length ? ` <span class="text-danger">${result.errors.length} error(s).</span>` : ''),
          n > 0 ? 'success' : 'info'
        );
        if (n > 0) load();
      },
      onError: msg => setFeedback(
        `<i class="ti ti-circle-x me-1"></i>Sync failed: ${escapeHtml(msg)}`, 'danger'
      ),
    });
  } catch (e) {
    setFeedback(`<i class="ti ti-circle-x me-1"></i>Error: ${escapeHtml(e.message)}`, 'danger');
  } finally {
    btn.disabled  = false;
    btn.innerHTML = '<i class="ti ti-mail-bolt me-1"></i>Sync';
  }
}

