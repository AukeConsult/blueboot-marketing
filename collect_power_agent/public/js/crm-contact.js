'use strict';
// crm-contact.js — full single-contact follow-up / update page.
// Self-contained: depends only on crm-common.js (BASE, fetchJSON, escapeHtml,
// requireAuth, requireRole) plus Quill. All writes go through the CRM API.

// FU_STATUSES, IMPORTANCE_LEVELS, CHANNELS, channelHref are in crm-defs.js (shared).

let CAMPAIGN_ID = '';
let DOC_ID      = '';
let contact     = null;
let users       = [];
let quill       = null;

// Surface any uncaught error / rejection on-screen instead of leaving the page
// stuck on "Loading…". Also logs a marker so a stale cached build is obvious.
console.log('[crm-contact] script loaded build-2');
function _ccShowFatal(msg) {
  const ld = document.getElementById('cc-loading');
  if (ld) ld.style.display = 'none';
  const fb = document.getElementById('cc-feedback');
  if (fb) {
    fb.className = 'alert alert-danger py-2 px-3 small mb-3';
    fb.textContent = 'Error: ' + msg;
    fb.style.display = '';
  }
}
window.addEventListener('error', (ev) => _ccShowFatal(ev.message || 'script error'));
window.addEventListener('unhandledrejection', (ev) =>
  _ccShowFatal((ev.reason && ev.reason.message) || String(ev.reason) || 'unhandled rejection'));

function qp(n) { return new URLSearchParams(location.search).get(n) || ''; }
function esc(s) { return (window.escapeHtml ? escapeHtml(s) : String(s ?? '')); }

function setFeedback(msg, kind) {
  const fb = document.getElementById('cc-feedback');
  if (!kind) { fb.style.display = 'none'; return; }
  fb.className = `alert alert-${kind} py-2 px-3 small mb-3`;
  fb.innerHTML = msg;
  fb.style.display = '';
}
function flashSaved() {
  const el = document.getElementById('cc-saved');
  el.style.display = '';
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.style.display = 'none'; }, 1500);
}

function userLabel(u) {
  return u.displayName ? `${u.displayName} (${u.email})` : u.email;
}
function statusBadgeHtml(status) {
  const map = { active: 'bg-success', pending: 'bg-secondary', excluded: 'bg-danger' };
  const cls = map[status] || 'bg-secondary';
  return `<span class="badge ${cls}">${esc(status || 'pending')}</span>`;
}

// ── Load + render ─────────────────────────────────────────────────────────────

async function load() {
  CAMPAIGN_ID = qp('campaign');
  DOC_ID      = qp('doc');
  if (!CAMPAIGN_ID || !DOC_ID) {
    document.getElementById('cc-loading').style.display = 'none';
    setFeedback('Missing <code>campaign</code> or <code>doc</code> in the URL.', 'danger');
    return;
  }
  const back = document.getElementById('cc-back');
  if (back) back.href = 'crm_follow.html?focus=' +
    encodeURIComponent(`campaigns/${CAMPAIGN_ID}/campaign_contacts/${DOC_ID}`);
  try {
    try {
      const meta = await fetchJSON(`${BASE}/api/crm/followup-meta`);
      users = meta.users || [];
    } catch (e) { users = []; }
    contact = await fetchJSON(
      `${BASE}/api/crm/campaigns/${encodeURIComponent(CAMPAIGN_ID)}/contacts/${encodeURIComponent(DOC_ID)}`
    );
    render();
    document.getElementById('cc-loading').style.display = 'none';
    document.getElementById('cc-body').style.display = '';
  } catch (e) {
    document.getElementById('cc-loading').style.display = 'none';
    setFeedback(`<i class="ti ti-circle-x me-1"></i>Failed to load contact: ${esc(e.message)}`, 'danger');
  }
}

function fillSelect(id, options, current) {
  const sel = document.getElementById(id);
  sel.innerHTML = options.map(o =>
    `<option value="${esc(o.value)}"${o.value === current ? ' selected' : ''}>${esc(o.label)}</option>`
  ).join('');
}

function render() {
  const r = contact;
  document.title = `${r.name || r.email || 'Contact'} · Follow-up`;

  // header
  const initials = (r.name || r.email || '?').split(/\s+/).map(w => w[0]).slice(0, 2).join('').toUpperCase();
  document.getElementById('cc-avatar').textContent = initials;
  document.getElementById('cc-status-badge').innerHTML = statusBadgeHtml(r.status);
  document.getElementById('f-name').value  = r.name  || '';
  document.getElementById('f-title').value = r.title || '';

  // new mail
  const nm = document.getElementById('cc-newmail');
  nm.style.display = r.new_mail ? '' : 'none';

  // follow-up
  fillSelect('f-followup_status', FU_STATUSES, r.followup_status || '');
  fillSelect('f-followup_importance', IMPORTANCE_LEVELS, r.followup_importance || '');
  fillSelect('f-followup_owner',
    [{ value: '', label: '— unassigned —' }].concat(users.map(u => ({ value: u.email, label: userLabel(u) }))),
    r.followup_owner || '');
  document.getElementById('f-followup_date').value    = r.followup_date || '';
  document.getElementById('f-followup_comment').value = r.followup_comment || '';

  // contact methods
  const emailA = document.getElementById('cc-email');
  if (r.email) { emailA.textContent = r.email; emailA.href = `mailto:${r.email}`; }
  else { emailA.textContent = '—'; emailA.removeAttribute('href'); }
  document.getElementById('f-phone').value   = r.phone   || '';
  document.getElementById('f-website').value = r.website || '';

  // channels
  document.getElementById('cc-channels').innerHTML = CHANNELS.map(ch => {
    const val = r[ch.field] || '';
    const link = val
      ? `<a href="${esc(channelHref(ch.field, val))}" target="_blank" class="btn btn-sm btn-outline-secondary"><i class="ti ti-external-link"></i></a>`
      : '';
    return `<div class="d-flex align-items-center gap-2 mb-2">
      <i class="ti ${ch.icon} text-muted" style="width:18px"></i>
      <input type="text" class="form-control form-control-sm" value="${esc(val)}"
             placeholder="${esc(ch.ph)}" data-field="${ch.field}">
      ${link}
    </div>`;
  }).join('');

  // mail composer defaults
  document.getElementById('cc-mail-from').textContent =
    (r.outreach_display_name && r.outreach_email)
      ? `${r.outreach_display_name} <${r.outreach_email}>`
      : (r.outreach_email || '— no campaign mail account —');
  document.getElementById('cc-mail-to').value = r.name ? `${r.name} <${r.email}>` : (r.email || '');
  document.getElementById('cc-mail-subject').value = 'Follow-up';
  initQuill();

  // history
  renderHistory(r.comment_history || []);

  bindSaves();
}

// ── Field saves ───────────────────────────────────────────────────────────────

function bindSaves() {
  document.querySelectorAll('[data-field]').forEach(el => {
    if (el._bound) return;
    el._bound = true;
    el.addEventListener('change', () => patchField(el.dataset.field, el.value));
  });
  const ack = document.getElementById('cc-ack-btn');
  if (ack && !ack._bound) { ack._bound = true; ack.addEventListener('click', () => patchField('new_mail', false, true)); }
  const sendBtn = document.getElementById('cc-mail-send');
  if (sendBtn && !sendBtn._bound) { sendBtn._bound = true; sendBtn.addEventListener('click', sendMail); }
}

async function patchField(field, value, ackMail) {
  const user = (window._authUser && (window._authUser.email || window._authUser.uid)) || 'unknown';
  try {
    await fetchJSON(
      `${BASE}/api/crm/campaigns/${encodeURIComponent(CAMPAIGN_ID)}/contacts/${encodeURIComponent(DOC_ID)}`,
      { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [field]: value, _user: user }) }
    );
    contact[field] = value;
    flashSaved();
    if (ackMail) { document.getElementById('cc-newmail').style.display = 'none'; }
    if (field === 'status' || field === 'followup_status') {
      document.getElementById('cc-status-badge').innerHTML = statusBadgeHtml(contact.status);
    }
  } catch (e) {
    setFeedback(`<i class="ti ti-circle-x me-1"></i>Save failed (${esc(field)}): ${esc(e.message)}`, 'danger');
  }
}

// ── Mail composer ─────────────────────────────────────────────────────────────

function initQuill() {
  if (quill || !window.Quill) return;
  quill = new Quill('#cc-mail-editor', {
    theme: 'snow',
    placeholder: 'Write your message…',
    modules: { toolbar: [['bold', 'italic', 'underline'], ['link'], [{ list: 'ordered' }, { list: 'bullet' }], ['clean']] },
  });
}

async function sendMail() {
  const fb  = document.getElementById('cc-mail-feedback');
  const btn = document.getElementById('cc-mail-send');
  const subject = (document.getElementById('cc-mail-subject').value || 'Follow-up').trim();
  const html = quill ? quill.root.innerHTML : '';
  const text = quill ? quill.getText().trim() : '';
  if (!text) {
    fb.className = 'alert alert-warning py-2 px-3 small mb-2'; fb.textContent = 'Mail body is required.'; fb.style.display = '';
    return;
  }
  if (!contact.email) {
    fb.className = 'alert alert-warning py-2 px-3 small mb-2'; fb.textContent = 'Contact has no email address.'; fb.style.display = '';
    return;
  }
  const _recipient = contact.name ? `${contact.name} <${contact.email}>` : contact.email;
  if (!confirm(`Send this email now?\n\nTo:  ${_recipient}\nSubject:  ${subject}`)) {
    return;   // user cancelled — nothing is sent
  }
  const user = (window._authUser && (window._authUser.email || window._authUser.uid)) || '';
  btn.disabled = true; btn.innerHTML = '<i class="ti ti-loader me-1"></i>Sending…'; fb.style.display = 'none';
  try {
    const r = await fetchJSON(
      `${BASE}/api/crm/campaigns/${encodeURIComponent(CAMPAIGN_ID)}/contacts/${encodeURIComponent(DOC_ID)}/send-mail`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ to: contact.email, subject, body: text, body_plain: text, body_html: html, _user: user }) }
    );
    if (r.status === 'error') throw new Error(r.message || 'Send failed');
    fb.className = 'alert alert-success py-2 px-3 small mb-2';
    fb.innerHTML = `<i class="ti ti-circle-check me-1"></i>${esc(r.message || 'Mail sent.')}`;
    fb.style.display = '';
    if (quill) quill.setText('');
    await reloadContact();   // refresh history + status
  } catch (e) {
    fb.className = 'alert alert-danger py-2 px-3 small mb-2'; fb.textContent = e.message; fb.style.display = '';
  }
  btn.disabled = false; btn.innerHTML = '<i class="ti ti-send me-1"></i>Send mail';
}

async function reloadContact() {
  try {
    contact = await fetchJSON(
      `${BASE}/api/crm/campaigns/${encodeURIComponent(CAMPAIGN_ID)}/contacts/${encodeURIComponent(DOC_ID)}`
    );
    document.getElementById('cc-status-badge').innerHTML = statusBadgeHtml(contact.status);
    renderHistory(contact.comment_history || []);
  } catch (e) { /* keep current view */ }
}

// ── History ───────────────────────────────────────────────────────────────────

function stripHtml(h) {
  const d = document.createElement('div'); d.innerHTML = h || ''; return (d.textContent || '').trim();
}
function renderHistory(history) {
  document.getElementById('cc-hist-count').textContent = history.length;
  const wrap = document.getElementById('cc-history');
  if (!history.length) { wrap.innerHTML = '<div class="small text-muted">No history yet.</div>'; return; }
  wrap.innerHTML = [...history].reverse().map((h, i) => {
    const date = h.date ? new Date(h.date).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' }) : '—';
    const type = h.type || '';
    const badgeCls = type === 'EMAIL_IN' ? 'bg-info text-dark'
      : (type === 'EMAIL_OUT' || type === 'MAIL_SENT') ? 'bg-primary'
      : type === 'BOUNCE' ? 'bg-danger' : 'bg-secondary';
    const body = (h.body_text && h.body_text.trim())
      ? h.body_text
      : (h.body_html ? stripHtml(h.body_html) : '');
    const meta = [
      h.from ? `<div><span class="text-muted">From</span> ${esc(h.from)}</div>` : '',
      h.to   ? `<div><span class="text-muted">To</span> ${esc(h.to)}</div>`   : '',
      h.subject ? `<div><span class="text-muted">Subject</span> ${esc(h.subject)}</div>` : '',
      (!h.from && !h.to && h.user) ? `<div><span class="text-muted">By</span> ${esc(h.user)}</div>` : '',
    ].join('');
    const bodyHtml = body ? `<pre class="cc-hist-body">${esc(body)}</pre>` : '';
    return `<div class="cc-hist-entry border rounded p-2 mb-2">
      <div class="d-flex align-items-center gap-2 mb-1">
        <span class="badge ${badgeCls}">${esc(type || 'NOTE')}</span>
        <span class="small text-muted">${esc(date)}</span>
      </div>
      <div class="small fw-500">${esc(h.text || '')}</div>
      <div class="small mt-1">${meta}</div>
      ${bodyHtml}
    </div>`;
  }).join('');
}

// ── Return-to-list button ─────────────────────────────────────────────────────
// If we arrived from the follow-up list, go back to it via history so the list's
// scroll position, filters and grouping are restored exactly as they were.
// Otherwise (opened from a direct link) fall back to the plain href navigation.
function bindBackButton() {
  const back = document.getElementById('cc-back');
  if (!back) return;
  back.addEventListener('click', (e) => {
    const cameFromList = document.referrer && document.referrer.indexOf('crm_follow.html') !== -1;
    if (cameFromList && history.length > 1) {
      e.preventDefault();
      history.back();
    }
    // else: let the href="crm_follow.html" navigate normally
  });
}

// ── Boot ──────────────────────────────────────────────────────────────────────

requireAuth().then(() => {
  if (typeof requireRole === 'function') requireRole(['admin', 'campaign-user', 'user']);
  bindBackButton();
  load();
});
