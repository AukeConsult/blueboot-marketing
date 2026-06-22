(function () {
  const DEFAULT_CSS = `.mail-wrap, .mail-wrap p, .mail-wrap div, .mail-wrap span, .mail-wrap td { margin: 0; padding: 0; }
.mail-wrap p { margin-bottom: 8px; }
.mail-wrap { font-family: Arial, sans-serif; font-size: 14px; line-height: 1.4; color: #333; max-width: 600px; }
.mail-wrap a { color: #0066cc; text-decoration: none; }
.mail-wrap h1, .mail-wrap h2, .mail-wrap h3 { font-weight: bold; margin-bottom: 8px; }
.mail-wrap ul, .mail-wrap ol { margin: 0 0 8px 20px; padding: 0; }
.mail-wrap li { margin-bottom: 4px; }
.mail-wrap .ql-font-serif { font-family: Georgia, Times New Roman, serif; }
.mail-wrap .ql-font-monospace { font-family: Monaco, Consolas, Courier New, monospace; }
.mail-wrap .ql-size-small { font-size: 12px; }
.mail-wrap .ql-size-large { font-size: 18px; }
.mail-wrap .ql-size-huge { font-size: 24px; }
.mail-wrap .ql-align-center { text-align: center; }
.mail-wrap .ql-align-right { text-align: right; }
.mail-wrap .ql-align-justify { text-align: justify; }`;

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
      this.stepIndex = null;
      this.stepNew = false;
      this.saveTimer = null;
      this.quill = null;
      this.currentTab = 'wysiwyg';
      this.uid = 'me_' + Math.random().toString(36).slice(2, 9);
      this.showMainButton = opts.showMainButton !== false;
      this.showSaveButton = opts.showSaveButton !== false;
      this.showTestButton = opts.showTestButton !== false;
      if (!this.root) throw new Error('MailEditorComponent root not found');
      MailEditorComponent.ensureStyles();
      this.renderShell();
    }

    $(name) {
      return this.root.querySelector(`[data-me="${name}"]`);
    }

    static ensureStyles() {
      if (document.getElementById('mail-editor-component-styles')) return;
      const style = document.createElement('style');
      style.id = 'mail-editor-component-styles';
      style.textContent = `
        .mail-editor-component .ql-toolbar {
          position: relative;
          z-index: 2;
          background: #f9fafb;
          border-radius: 8px 8px 0 0;
        }
        .mail-editor-component .ql-container {
          border-radius: 0 0 8px 8px;
        }
        .mail-editor-component .ql-picker-options {
          z-index: 1400;
        }
        .mail-editor-component .ql-editor {
          min-height: 230px;
          font-size: 14px;
        }
        .mail-editor-component .ql-editor img,
        .mail-editor-component [data-me="previewPane"] img {
          max-width: 100%;
          height: auto;
        }
      `;
      document.head.appendChild(style);
    }

    renderShell() {
      this.root.innerHTML = `
        <div class="mail-editor-component">
          <div class="d-flex align-items-center justify-content-between gap-2 mb-2">
            <div>
              <div class="fw-600 small d-flex align-items-center gap-2">
                <i class="ti ti-mail text-primary me-1"></i><span data-me="title">Mail editor</span>
                <span data-me="stepBadge" class="badge fw-normal" style="display:none;background:#dbeafe;color:#1d4ed8;font-size:.72rem;letter-spacing:.01em"></span>
              </div>
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

          <div data-me="stepBar" class="p-2 mb-2 rounded d-flex align-items-center gap-3" style="display:none;background:#eff6ff;border:1px solid #bfdbfe">
            <label class="form-label small fw-500 mb-0 text-nowrap">Send after days</label>
            <input data-me="stepDelay" class="form-control form-control-sm" type="number" min="0" placeholder="0" style="max-width:90px">
          </div>

          <div class="mb-2">
            <label class="form-label small fw-500 mb-1">Subject</label>
            <input data-me="subject" class="form-control form-control-sm" placeholder="Email subject">
          </div>

          <div class="d-flex align-items-center gap-3 mb-2 small" style="color:var(--bb-muted)">
            <strong>Body:</strong>
            <label class="form-check form-check-inline mb-0"><input data-me="typePlain" class="form-check-input" type="radio" name="${this.uid}_type" value="plain" checked> Plain</label>
            <label class="form-check form-check-inline mb-0"><input data-me="typeHtml" class="form-check-input" type="radio" name="${this.uid}_type" value="html"> HTML</label>
          </div>

          <div data-me="plainPane">
            <textarea data-me="bodyPlain" class="form-control" rows="8" style="font-size:13px;border-radius:8px" placeholder="Hei {{name}},"></textarea>
          </div>

          <div data-me="htmlPane" style="display:none">
            <ul class="nav nav-tabs mb-0" style="border-bottom:none">
              <li class="nav-item"><button class="nav-link active" data-me-tab="wysiwyg" type="button"><i class="ti ti-pencil me-1"></i>Editor</button></li>
              <li class="nav-item"><button class="nav-link" data-me-tab="source" type="button"><i class="ti ti-code me-1"></i>HTML</button></li>
              <li class="nav-item"><button class="nav-link" data-me-tab="preview" type="button"><i class="ti ti-eye me-1"></i>Preview</button></li>
            </ul>
            <div data-me="wysiwygPane" class="border border-top-0 rounded-bottom mb-2">
              <div data-me="quill" style="min-height:230px;font-size:14px"></div>
            </div>
            <div data-me="sourcePane" style="display:none" class="mb-2">
              <textarea data-me="bodyHtml" class="form-control" rows="10" style="font-family:monospace;font-size:12px;border-radius:8px"></textarea>
            </div>
            <div data-me="previewPane" class="border border-top-0 rounded-bottom p-3 mb-2" style="display:none;background:#fff;font-family:Arial,sans-serif;font-size:13px;line-height:1.5;min-height:230px"></div>
          </div>
          <textarea data-me="css" style="display:none"></textarea>
        </div>`;

      this.bind();
    }

    bind() {
      ['subject', 'bodyPlain', 'bodyHtml', 'css', 'stepDelay'].forEach(name => {
        const el = this.$(name);
        if (el) el.addEventListener('input', () => this.autoSave());
      });
      this.$('typePlain').addEventListener('change', () => { this.switchMode(); this.autoSave(); });
      this.$('typeHtml').addEventListener('change', () => { this.switchMode(); this.autoSave(); });
      this.$('mainBtn').addEventListener('click', () => this.editCampaignMail());
      this.$('saveBtn').addEventListener('click', () => this.save(false));
      this.$('testBtn').addEventListener('click', () => this.openTest());
      this.root.querySelectorAll('[data-me-tab]').forEach(btn => {
        btn.addEventListener('click', () => this.showTab(btn.dataset.meTab));
      });
    }

    async load({ campaignId, stepIndex = null, stepNew = false, stepName = '', delay = 0 } = {}) {
      this.campaignId = campaignId || '';
      this.stepIndex = stepIndex != null ? parseInt(stepIndex, 10) : null;
      this.stepNew = !!stepNew;
      if (!this.campaignId) {
        this.$('subtitle').textContent = 'No campaign selected';
        return;
      }
      const r = await fetch(`${this.base}/api/crm/campaigns/${encodeURIComponent(this.campaignId)}`);
      const c = await r.json();
      if (!r.ok || c.status === 'error') throw new Error(c.message || 'Could not load campaign');
      this.campaign = c;

      if (this.stepIndex != null) {
        const seq  = c.mail_sequence || [];
        const step = seq.find(s => s.index === this.stepIndex);
        const label = stepName || (this.stepIndex === 0 ? 'Intro' : `Reminder ${this.stepIndex}`);
        this.$('title').textContent = 'Mail editor';
        this.$('subtitle').textContent = this.campaignId;
        const badge = this.$('stepBadge');
        badge.textContent = label;
        badge.style.display = '';
        this.$('stepBar').style.display = '';
        this.$('stepDelay').value = step ? (step.delay_days ?? 0) : (delay || 0);
        if (step) {
          const body = step.body_html || step.body_text || '';
          const type = step.body_html ? 'html' : 'plain';
          this.applyMail({ subject: step.subject || '', body, type, css: step.css || DEFAULT_CSS });
        } else {
          this.applyMail({ subject: stepName || '', body: '', type: 'plain', css: DEFAULT_CSS });
        }
      } else {
        this.stepIndex = null;
        this.$('title').textContent = 'Mail editor';
        this.$('subtitle').textContent = this.campaignId;
        this.$('stepBadge').style.display = 'none';
        this.$('stepBar').style.display = 'none';
        this.applyMail(c.mail || { type: 'plain', css: DEFAULT_CSS });
      }
    }

    editCampaignMail() {
      if (!this.campaignId) return;
      this.load({ campaignId: this.campaignId }).catch(err => this.feedback(err.message, true));
    }

    loadDraft({ title = 'Mail editor', subtitle = '', mail = {} } = {}) {
      this.campaignId = '';
      this.campaign = null;
      this.stepIndex = null;
      this.stepNew = false;
      this.$('title').textContent = title;
      this.$('subtitle').textContent = subtitle;
      this.$('stepBar').style.display = 'none';
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
        modules: {
          toolbar: {
            container: [
              [{ font: [] }, { size: ['small', false, 'large', 'huge'] }],
              [{ header: [1, 2, 3, false] }],
              ['bold', 'italic', 'underline'],
              [{ color: [] }, { background: [] }],
              [{ align: [] }],
              [{ list: 'ordered' }, { list: 'bullet' }],
              ['link', 'image', 'clean']
            ],
            handlers: {
              link: value => this.handleLink(value),
              image: () => this.handleImage()
            }
          }
        }
      });
      this.quill.on('text-change', () => {
        this.$('bodyHtml').value = this.quill.root.innerHTML;
        this.autoSave();
      });
    }

    // ── Shared modal helper ───────────────────────────────────────────────────

    _ensureModals() {
      if (document.getElementById('me-link-modal')) return;

      // Link modal
      document.body.insertAdjacentHTML('beforeend', `
        <div class="modal fade" id="me-link-modal" tabindex="-1">
          <div class="modal-dialog modal-dialog-centered" style="max-width:400px">
            <div class="modal-content">
              <div class="modal-header border-0 pb-0">
                <h6 class="modal-title"><i class="ti ti-link text-primary me-2"></i>Insert link</h6>
                <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
              </div>
              <div class="modal-body">
                <div class="mb-3">
                  <label class="form-label small fw-500">Link text</label>
                  <input id="me-link-text" class="form-control form-control-sm" placeholder="Click here">
                </div>
                <div>
                  <label class="form-label small fw-500">URL</label>
                  <input id="me-link-url" class="form-control form-control-sm" placeholder="https://">
                </div>
              </div>
              <div class="modal-footer border-0 pt-0">
                <button type="button" class="btn btn-sm btn-secondary" data-bs-dismiss="modal">Cancel</button>
                <button type="button" class="btn btn-sm btn-primary" id="me-link-insert">Insert</button>
              </div>
            </div>
          </div>
        </div>`);

      // Image modal
      document.body.insertAdjacentHTML('beforeend', `
        <div class="modal fade" id="me-image-modal" tabindex="-1">
          <div class="modal-dialog modal-dialog-centered" style="max-width:440px">
            <div class="modal-content">
              <div class="modal-header border-0 pb-0">
                <h6 class="modal-title"><i class="ti ti-photo text-primary me-2"></i>Insert image</h6>
                <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
              </div>
              <div class="modal-body">
                <div class="mb-3">
                  <label class="form-label small fw-500">Image file</label>
                  <input id="me-image-file" type="file" accept="image/*" class="form-control form-control-sm">
                </div>
                <div class="mb-1 small text-muted text-center">— or —</div>
                <div class="mb-3">
                  <label class="form-label small fw-500">Image URL</label>
                  <input id="me-image-url" class="form-control form-control-sm" placeholder="https://example.com/image.png">
                </div>
                <div>
                  <label class="form-label small fw-500">Link URL <span class="text-muted fw-normal">(optional — makes image clickable)</span></label>
                  <input id="me-image-link" class="form-control form-control-sm" placeholder="https://">
                </div>
              </div>
              <div class="modal-footer border-0 pt-0">
                <button type="button" class="btn btn-sm btn-secondary" data-bs-dismiss="modal">Cancel</button>
                <button type="button" class="btn btn-sm btn-primary" id="me-image-insert">Insert</button>
              </div>
            </div>
          </div>
        </div>`);
    }

    handleLink(value) {
      if (!this.quill) return;
      if (!value) {
        this.quill.format('link', false);
        return;
      }
      this._ensureModals();
      const range = this.quill.getSelection(true);
      const currentUrl  = range ? (this.quill.getFormat(range).link || '') : '';
      const currentText = (range && range.length > 0)
        ? this.quill.getText(range.index, range.length).trim()
        : '';

      const urlInput  = document.getElementById('me-link-url');
      const textInput = document.getElementById('me-link-text');
      urlInput.value  = currentUrl || 'https://';
      textInput.value = currentText;

      const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById('me-link-modal'));
      modal.show();
      setTimeout(() => (currentUrl ? urlInput : textInput).focus(), 300);

      const insertBtn = document.getElementById('me-link-insert');
      const doInsert = () => {
        let url = urlInput.value.trim();
        const text = textInput.value.trim();
        if (!url || url === 'https://') return;
        if (!/^[a-z][a-z0-9+.-]*:/i.test(url)) url = 'https://' + url;
        modal.hide();

        if (range && range.length > 0) {
          // Replace selection text if user changed it
          if (text && text !== currentText) {
            this.quill.deleteText(range.index, range.length);
            this.quill.insertText(range.index, text, 'link', url);
          } else {
            this.quill.format('link', url);
          }
        } else {
          const label = text || url;
          this.quill.insertText(range ? range.index : this.quill.getLength(), label, 'link', url);
        }
        this.$('bodyHtml').value = this.quill.root.innerHTML;
        this.autoSave();
        insertBtn.removeEventListener('click', doInsert);
      };
      insertBtn.addEventListener('click', doInsert);
      document.getElementById('me-link-modal').addEventListener('hidden.bs.modal', () => {
        insertBtn.removeEventListener('click', doInsert);
      }, { once: true });
    }

    handleImage() {
      if (!this.quill) return;
      this._ensureModals();

      const fileInput  = document.getElementById('me-image-file');
      const urlInput   = document.getElementById('me-image-url');
      const linkInput  = document.getElementById('me-image-link');
      fileInput.value  = '';
      urlInput.value   = '';
      linkInput.value  = '';

      const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById('me-image-modal'));
      modal.show();

      const insertBtn = document.getElementById('me-image-insert');
      const doInsert = () => {
        const linkUrl = linkInput.value.trim();
        const imgUrl  = urlInput.value.trim();
        const file    = fileInput.files && fileInput.files[0];

        const _embed = (src) => {
          modal.hide();
          const range = this.quill.getSelection(true);
          const idx   = range ? range.index : this.quill.getLength();
          if (linkUrl) {
            // Insert as raw HTML <a><img></a> via clipboard delta workaround
            const html = `<a href="${esc(linkUrl)}" target="_blank"><img src="${esc(src)}" style="max-width:100%"></a>`;
            const curHtml = this.quill.root.innerHTML;
            // Insert at cursor by manipulating innerHTML directly then re-syncing
            const tmp = document.createElement('div');
            tmp.innerHTML = curHtml;
            // Append after cursor position using Quill's clipboard
            this.quill.clipboard.dangerouslyPasteHTML(idx, html);
          } else {
            this.quill.insertEmbed(idx, 'image', src);
          }
          this.$('bodyHtml').value = this.quill.root.innerHTML;
          this.autoSave();
          insertBtn.removeEventListener('click', doInsert);
        };

        if (file) {
          const reader = new FileReader();
          reader.onload = () => _embed(reader.result);
          reader.readAsDataURL(file);
        } else if (imgUrl) {
          _embed(imgUrl);
        }
      };
      insertBtn.addEventListener('click', doInsert);
      document.getElementById('me-image-modal').addEventListener('hidden.bs.modal', () => {
        insertBtn.removeEventListener('click', doInsert);
      }, { once: true });
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
        this.showTab(this.currentTab === 'preview' || this.currentTab === 'source' ? this.currentTab : 'wysiwyg');
      }
    }

    showTab(tab) {
      if (this.currentTab === 'wysiwyg' && this.quill) this.$('bodyHtml').value = this.quill.root.innerHTML;
      if (this.currentTab === 'source' && this.quill) this.quill.root.innerHTML = this.$('bodyHtml').value;
      this.currentTab = tab;
      this.$('wysiwygPane').style.display = tab === 'wysiwyg' ? '' : 'none';
      this.$('sourcePane').style.display = tab === 'source' ? '' : 'none';
      this.$('previewPane').style.display = tab === 'preview' ? '' : 'none';
      this.root.querySelectorAll('[data-me-tab]').forEach(b => b.classList.toggle('active', b.dataset.meTab === tab));
      if (tab === 'preview') this.updatePreview();
    }

    getType() {
      return this.$('typeHtml').checked ? 'html' : 'plain';
    }

    getBody() {
      if (this.getType() !== 'html') return this.$('bodyPlain').value;
      if (this.currentTab === 'wysiwyg' && this.quill) return this.quill.root.innerHTML;
      if (this.currentTab === 'preview' && this.quill) return this.quill.root.innerHTML;
      return this.$('bodyHtml').value;
    }

    buildPayload() {
      if (this.stepIndex != null) {
        const body    = this.getBody();
        const isHtml  = this.getType() === 'html';
        const idx     = this.stepIndex;
        return {
          mail_sequence_step: {
            index:      idx,
            mail_type:  idx === 0 ? 'intro' : `followup_${idx}`,
            delay_days: parseInt(this.$('stepDelay').value || '0', 10) || 0,
            subject:    this.$('subject').value.trim(),
            body_html:  isHtml ? body : '',
            body_text:  isHtml ? '' : body,
            css:        this.$('css').value || DEFAULT_CSS,
          }
        };
      }
      const mail = {
        subject: this.$('subject').value.trim(),
        body: this.getBody(),
        type: this.getType(),
        css: this.$('css').value || DEFAULT_CSS
      };
      return { mail };
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

    openTest() {
      const detail = {
        campaignId: this.campaignId,
        account: this.campaign?.outreach_email_account || '',
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
