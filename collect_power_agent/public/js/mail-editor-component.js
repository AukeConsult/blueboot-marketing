(function () {
  const DEFAULT_CSS = `.mail-wrap, .mail-wrap p, .mail-wrap div, .mail-wrap span, .mail-wrap td { margin: 0; padding: 0; }
.mail-wrap p { margin-bottom: 8px; }
.mail-wrap { font-family: Arial, sans-serif; font-size: 14px; line-height: 1.4; color: #333; max-width: 600px; }
.mail-wrap a { color: #0066cc; text-decoration: none; }
.mail-wrap h1, .mail-wrap h2, .mail-wrap h3 { font-weight: bold; margin-bottom: 8px; }
.mail-wrap ul, .mail-wrap ol { margin: 0 0 8px 20px; padding: 0; }
.mail-wrap li { margin-bottom: 4px; }`;

  function esc(v) {
    return (window.escapeHtml || (s => String(s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]))))(v == null ? '' : String(v));
  }

  class MailEditorComponent {
    constructor(root, opts = {}) {
      this.root = typeof root === 'string' ? document.querySelector(root) : root;
      this.base = opts.base || window.BASE || '';
      this.onSaved = opts.onSaved || null;
      this.campaignId = '';
      this.campaign = null;
      this.stepId = '';
      this.stepNew = false;
      this.saveTimer = null;
      this.quill = null;
      this.currentTab = 'wysiwyg';
      this.uid = 'me_' + Math.random().toString(36).slice(2, 9);
      this.showMainButton = opts.showMainButton !== false;
      this.showSaveButton = opts.showSaveButton !== false;
      this.showTestButton = opts.showTestButton !== false;
      this.showAccountField = opts.showAccountField !== false;
      if (!this.root) throw new Error('MailEditorComponent root not found');
      this.renderShell();
    }

    $(name) {
      return this.root.querySelector(`[data-me="${name}"]`);
    }

    renderShell() {
      this.root.innerHTML = `
        <div class="mail-editor-component">
          <div class="d-flex align-items-center justify-content-between gap-2 mb-2">
            <div>
              <div class="fw-600 small"><i class="ti ti-mail text-primary me-1"></i><span data-me="title">Mail editor</span></div>
              <div class="small" style="color:var(--bb-muted)" data-me="subtitle">No campaign selected</div>
            </div>
            <div class="d-flex align-items-center gap-2">
              <span data-me="feedback" class="small" style="display:none;color:var(--bb-muted)"></span>
              <button class="btn btn-sm btn-outline-secondary py-0" data-me="mainBtn" type="button" title="Edit campaign mail" style="${this.showMainButton ? '' : 'display:none'}">
                <i class="ti ti-mail"></i>
              </button>
              <button class="btn btn-sm btn-outline-success py-0" data-me="saveBtn" type="button" title="Save" style="${this.showSaveButton ? '' : 'display:none'}">
                <i class="ti ti-device-floppy"></i>
              </button>
              <button class="btn btn-sm btn-outline-primary py-0" data-me="testBtn" type="button" title="Send test" style="${this.showTestButton ? '' : 'display:none'}">
                <i class="ti ti-send"></i>
              </button>
            </div>
          </div>

          <div data-me="stepBar" class="p-2 mb-2 rounded" style="display:none;background:#eff6ff;border:1px solid #bfdbfe">
            <div class="row g-2 align-items-end">
              <div class="col-md-7">
                <label class="form-label small fw-500 mb-1">Step name</label>
                <input data-me="stepName" class="form-control form-control-sm" placeholder="Initial outreach">
              </div>
              <div class="col-md-5">
                <label class="form-label small fw-500 mb-1">Send after days</label>
                <input data-me="stepDelay" class="form-control form-control-sm" type="number" min="0" placeholder="0">
              </div>
            </div>
          </div>

          <div class="row g-2 mb-2">
            <div class="col-md-5" data-me="accountWrap" style="${this.showAccountField ? '' : 'display:none'}">
              <label class="form-label small fw-500 mb-1">Outreach account</label>
              <input data-me="account" class="form-control form-control-sm" placeholder="sender@example.com">
            </div>
            <div class="${this.showAccountField ? 'col-md-7' : 'col-12'}">
              <label class="form-label small fw-500 mb-1">Subject</label>
              <input data-me="subject" class="form-control form-control-sm" placeholder="Email subject">
            </div>
          </div>

          <div class="d-flex align-items-center gap-3 mb-2 small" style="color:var(--bb-muted)">
            <strong>Body:</strong>
            <label class="form-check form-check-inline mb-0"><input data-me="typePlain" class="form-check-input" type="radio" name="${this.uid}_type" value="plain" checked> Plain</label>
            <label class="form-check form-check-inline mb-0"><input data-me="typeHtml" class="form-check-input" type="radio" name="${this.uid}_type" value="html"> HTML</label>
            <button class="btn btn-sm btn-outline-secondary py-0 px-2 ms-auto" data-me="previewBtn" type="button" style="font-size:12px">
              <i class="ti ti-eye me-1"></i>Preview
            </button>
          </div>

          <div data-me="plainPane">
            <textarea data-me="bodyPlain" class="form-control" rows="8" style="font-size:13px;border-radius:8px" placeholder="Hei {{name}},"></textarea>
          </div>

          <div data-me="htmlPane" style="display:none">
            <ul class="nav nav-tabs mb-0" style="border-bottom:none">
              <li class="nav-item"><button class="nav-link active" data-me-tab="wysiwyg" type="button"><i class="ti ti-pencil me-1"></i>Editor</button></li>
              <li class="nav-item"><button class="nav-link" data-me-tab="source" type="button"><i class="ti ti-code me-1"></i>HTML</button></li>
              <li class="nav-item"><button class="nav-link" data-me-tab="css" type="button"><i class="ti ti-palette me-1"></i>CSS</button></li>
            </ul>
            <div data-me="wysiwygPane" class="border border-top-0 rounded-bottom mb-2">
              <div data-me="quill" style="min-height:230px;font-size:14px"></div>
            </div>
            <div data-me="sourcePane" style="display:none" class="mb-2">
              <textarea data-me="bodyHtml" class="form-control" rows="10" style="font-family:monospace;font-size:12px;border-radius:8px"></textarea>
            </div>
            <div data-me="cssPane" style="display:none" class="mb-2">
              <textarea data-me="css" class="form-control" rows="8" style="font-family:monospace;font-size:12px;border-radius:8px"></textarea>
            </div>
          </div>

          <div data-me="previewPane" class="border rounded p-3 mt-2" style="display:none;background:#fff;font-family:Arial,sans-serif;font-size:13px;line-height:1.5;min-height:70px"></div>
        </div>`;

      this.bind();
    }

    bind() {
      ['subject', 'account', 'bodyPlain', 'bodyHtml', 'css', 'stepName', 'stepDelay'].forEach(name => {
        const el = this.$(name);
        if (el) el.addEventListener('input', () => this.autoSave());
      });
      this.$('typePlain').addEventListener('change', () => { this.switchMode(); this.autoSave(); });
      this.$('typeHtml').addEventListener('change', () => { this.switchMode(); this.autoSave(); });
      this.$('previewBtn').addEventListener('click', () => this.togglePreview());
      this.$('mainBtn').addEventListener('click', () => this.editCampaignMail());
      this.$('saveBtn').addEventListener('click', () => this.save(false));
      this.$('testBtn').addEventListener('click', () => this.openTest());
      this.root.querySelectorAll('[data-me-tab]').forEach(btn => {
        btn.addEventListener('click', () => this.showTab(btn.dataset.meTab));
      });
    }

    async load({ campaignId, stepId = '', stepNew = false, stepName = '', delay = 0 } = {}) {
      this.campaignId = campaignId || '';
      this.stepId = stepId || '';
      this.stepNew = !!stepNew;
      if (!this.campaignId) {
        this.$('subtitle').textContent = 'No campaign selected';
        return;
      }
      const r = await fetch(`${this.base}/api/crm/campaigns/${encodeURIComponent(this.campaignId)}`);
      const c = await r.json();
      if (!r.ok || c.status === 'error') throw new Error(c.message || 'Could not load campaign');
      this.campaign = c;
      this.$('account').value = c.outreach_email_account || '';

      if (this.stepId) {
        const step = (c.mail_schedule || []).find(s => s.step_id === this.stepId);
        this.$('title').textContent = step ? `Mail editor - ${step.name || 'Step'}` : 'Mail editor - new step';
        this.$('subtitle').textContent = `${this.campaignId} / schedule step`;
        this.$('stepBar').style.display = '';
        this.$('accountWrap').style.display = 'none';
        this.$('stepName').value = step ? (step.name || '') : (stepName || '');
        this.$('stepDelay').value = step ? (step.delay_days ?? 0) : (delay || 0);
        this.applyMail((step && step.mail) || { subject: stepName || '', body: '', type: 'plain', css: DEFAULT_CSS });
      } else {
        this.$('title').textContent = 'Mail editor';
        this.$('subtitle').textContent = this.campaignId;
        this.$('stepBar').style.display = 'none';
        this.$('accountWrap').style.display = this.showAccountField ? '' : 'none';
        this.applyMail(c.mail || { type: 'plain', css: DEFAULT_CSS });
      }
    }

    editCampaignMail() {
      if (!this.campaignId) return;
      this.load({ campaignId: this.campaignId }).catch(err => this.feedback(err.message, true));
    }

    loadDraft({ account = '', title = 'Mail editor', subtitle = '', mail = {}, accountReadOnly = false } = {}) {
      this.campaignId = '';
      this.campaign = null;
      this.stepId = '';
      this.stepNew = false;
      this.$('title').textContent = title;
      this.$('subtitle').textContent = subtitle;
      this.$('stepBar').style.display = 'none';
      this.$('accountWrap').style.display = this.showAccountField ? '' : 'none';
      this.$('account').value = account || '';
      this.$('account').readOnly = !!accountReadOnly;
      this.applyMail({ type: 'plain', css: DEFAULT_CSS, ...mail });
    }

    applyMail(mail) {
      const type = mail.type || 'plain';
      this.$('subject').value = mail.subject || '';
      this.$('bodyPlain').value = mail.body || '';
      this.$('bodyHtml').value = mail.body || '';
      this.$('css').value = mail.css || DEFAULT_CSS;
      this.$(type === 'html' ? 'typeHtml' : 'typePlain').checked = true;
      this.switchMode();
      if (type === 'html') {
        this.initQuill();
        if (this.quill) this.quill.root.innerHTML = mail.body || '';
      }
      this.updatePreview();
    }

    initQuill() {
      if (this.quill || !window.Quill) return;
      this.quill = new Quill(this.$('quill'), {
        theme: 'snow',
        modules: { toolbar: [['bold', 'italic', 'underline'], [{ header: [1, 2, 3, false] }], [{ list: 'ordered' }, { list: 'bullet' }], ['link', 'clean']] }
      });
      this.quill.on('text-change', () => this.autoSave());
    }

    switchMode() {
      const html = this.$('typeHtml').checked;
      this.$('plainPane').style.display = html ? 'none' : '';
      this.$('htmlPane').style.display = html ? '' : 'none';
      if (html) {
        this.initQuill();
        if (this.quill && !this.$('bodyHtml').value && this.$('bodyPlain').value) {
          this.$('bodyHtml').value = this.$('bodyPlain').value;
          this.quill.root.innerHTML = this.$('bodyPlain').value;
        }
      }
    }

    showTab(tab) {
      if (this.currentTab === 'wysiwyg' && this.quill) this.$('bodyHtml').value = this.quill.root.innerHTML;
      if (this.currentTab === 'source' && this.quill) this.quill.root.innerHTML = this.$('bodyHtml').value;
      this.currentTab = tab;
      this.$('wysiwygPane').style.display = tab === 'wysiwyg' ? '' : 'none';
      this.$('sourcePane').style.display = tab === 'source' ? '' : 'none';
      this.$('cssPane').style.display = tab === 'css' ? '' : 'none';
      this.root.querySelectorAll('[data-me-tab]').forEach(b => b.classList.toggle('active', b.dataset.meTab === tab));
    }

    getType() {
      return this.$('typeHtml').checked ? 'html' : 'plain';
    }

    getBody() {
      if (this.getType() !== 'html') return this.$('bodyPlain').value;
      if (this.currentTab === 'wysiwyg' && this.quill) return this.quill.root.innerHTML;
      return this.$('bodyHtml').value;
    }

    buildPayload() {
      const mail = {
        subject: this.$('subject').value.trim(),
        body: this.getBody(),
        type: this.getType(),
        css: this.$('css').value || DEFAULT_CSS
      };
      if (this.stepId) {
        return {
          mail_schedule_step: {
            step_id: this.stepId,
            name: this.$('stepName').value.trim() || 'Step',
            delay_days: parseInt(this.$('stepDelay').value || '0', 10) || 0,
            mail
          }
        };
      }
      return { mail, outreach_email_account: this.$('account').value.trim() };
    }

    autoSave() {
      clearTimeout(this.saveTimer);
      this.saveTimer = setTimeout(() => this.save(true), 900);
      this.updatePreview();
    }

    async save(silent = false) {
      if (!this.campaignId) return;
      if (!silent) this.feedback('Saving...');
      try {
        const r = await fetch(`${this.base}/api/crm/campaigns/${encodeURIComponent(this.campaignId)}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.buildPayload())
        });
        const d = await r.json();
        if (!r.ok || d.status === 'error') throw new Error(d.message || 'Save failed');
        this.feedback(silent ? 'Auto-saved.' : 'Saved.', false);
        if (this.onSaved) this.onSaved(d, this);
      } catch (err) {
        this.feedback(err.message, true);
      }
    }

    feedback(msg, err = false) {
      const fb = this.$('feedback');
      fb.textContent = msg;
      fb.style.color = err ? '#dc2626' : '#16a34a';
      fb.style.display = '';
      if (!err) setTimeout(() => { fb.style.display = 'none'; }, 1800);
    }

    updatePreview() {
      const pane = this.$('previewPane');
      if (pane.style.display === 'none') return;
      const rendered = this.getBody()
        .replace(/\{\{name\}\}/g, 'Tone Hansen')
        .replace(/\{\{company\}\}/g, 'Blueboot AS')
        .replace(/\{\{website\}\}/g, 'blueboot.no')
        .replace(/\{\{domain\}\}/g, 'blueboot.no')
        .replace(/\{\{title\}\}/g, 'Markedssjef')
        .replace(/\{\{location\}\}/g, 'Oslo')
        .replace(/\{\{ai_summary\}\}/g, '[AI summary here]');
      pane.innerHTML = this.getType() === 'html'
        ? `<style>${this.$('css').value || ''}</style><div class="mail-wrap">${rendered}</div>`
        : `<pre style="white-space:pre-wrap;margin:0;font-family:Arial,sans-serif">${esc(rendered)}</pre>`;
    }

    togglePreview() {
      const pane = this.$('previewPane');
      pane.style.display = pane.style.display === 'none' ? '' : 'none';
      this.updatePreview();
    }

    openTest() {
      const detail = {
        campaignId: this.campaignId,
        account: this.stepId ? (this.campaign?.outreach_email_account || '') : this.$('account').value.trim(),
        subject: this.$('subject').value.trim(),
        body: this.getBody(),
        type: this.getType(),
        css: this.$('css').value || DEFAULT_CSS
      };
      this.root.dispatchEvent(new CustomEvent('mail-editor:test', { bubbles: true, detail }));
    }
  }

  window.MailEditorComponent = MailEditorComponent;
})();
