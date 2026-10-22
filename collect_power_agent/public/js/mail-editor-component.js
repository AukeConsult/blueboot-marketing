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
      this._queue = Promise.resolve();   // serialises saves
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

          <div data-me="stepBar" class="p-2 mb-2 rounded align-items-center gap-3" style="display:none;background:#eff6ff;border:1px solid #bfdbfe">
            <label class="form-label small fw-500 mb-0 text-nowrap">Send after days</label>
            <input data-me="stepDelay" class="form-control form-control-sm" type="number" min="0" placeholder="0" style="max-width:90px">
          </div>

          <div class="mb-2">
            <label class="form-label small fw-500 mb-1">Subject</label>
            <input data-me="subject" class="form-control form-control-sm" placeholder="Email subject">
          </div>

          <div data-me="tools" class="mb-2 p-2 rounded" style="display:none;background:#f9fafb;border:1px solid #e5e7eb">
            <div class="row g-2 mb-2">
              <div class="col-sm-6">
                <label class="form-label small fw-500 mb-1">Campaign ref</label>
                <input data-me="campaignRef" class="form-control form-control-sm" placeholder="hot-no2" autocomplete="off">
              </div>
              <div class="col-sm-6">
                <label class="form-label small fw-500 mb-1">Language (code)</label>
                <input data-me="campaignLang" class="form-control form-control-sm" list="${this.uid}_langs" placeholder="en" autocomplete="off">
                <datalist id="${this.uid}_langs">
                  <option value="en">English</option><option value="da">Danish</option><option value="no">Norwegian</option>
                  <option value="sv">Swedish</option><option value="fi">Finnish</option><option value="de">German</option>
                  <option value="nl">Dutch</option><option value="fr">French</option><option value="es">Spanish</option>
                  <option value="it">Italian</option><option value="pt">Portuguese</option><option value="pl">Polish</option>
                </datalist>
              </div>
            </div>
            <div class="d-flex flex-wrap gap-2 mb-1">
              <button class="btn btn-sm btn-outline-primary" type="button" data-me="applyBtn" title="Set the language and ref in every blueboot.ai link of this mail">
                <i class="ti ti-link me-1"></i>Apply ref and language to links
              </button>
              <button class="btn btn-sm btn-outline-primary" type="button" data-me="trBtn" title="AI translation of the subject and text into the language above">
                <i class="ti ti-language me-1"></i>Translate
              </button>
              <button class="btn btn-sm btn-outline-secondary" type="button" data-me="trUndo" style="display:none">
                <i class="ti ti-arrow-back-up me-1"></i>Undo translation
              </button>
              <button class="btn btn-sm btn-outline-primary" type="button" data-me="trailerBtn" title="Copy the default mail trailer to the end of this mail">
                <i class="ti ti-file-text me-1"></i>Add default trailer
              </button>
            </div>
            <div class="form-text mt-0 mb-0" data-me="toolsMsg">Ref and language belong to the campaign (all steps). <strong>Apply</strong> changes <code>/lang/xx</code> and <code>?ref=</code> in blueboot.ai links, e.g. <code>https://blueboot.ai/lang/en/bs/docs/why-use/?ref=hot-no2</code>. <strong>Translate</strong> is a pure AI translation into the language code; placeholders, links and formatting are kept, it replaces the current text and is saved (Undo is available).</div>
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
            <div data-me="textPane" class="mb-2">
              <label class="form-label small fw-semibold mb-1">Plain-text version
                <span class="text-muted fw-normal">- sent as the text-only part of this mail. Empty = made from the HTML when sent.</span></label>
              <textarea data-me="bodyText" class="form-control" rows="6" style="font-size:13px;border-radius:8px" placeholder="Hei {{name}},"></textarea>
              <button type="button" class="btn btn-sm btn-link p-0 mt-1" data-me="textFromHtml">Make it from the HTML now</button>
            </div>
          </div>
          <textarea data-me="css" style="display:none"></textarea>
        </div>`;

      this.bind();
    }

    bind() {
      ['subject', 'bodyPlain', 'bodyHtml', 'bodyText', 'css', 'stepDelay'].forEach(name => {
        const el = this.$(name);
        if (el) el.addEventListener('input', () => this.autoSave());
      });
      // Raw HTML typed or pasted in the HTML tab is saved when the box loses focus,
      // not only after the 0.9 s debounce.
      this.$('bodyHtml').addEventListener('blur',   () => this.flush());
      this.$('bodyHtml').addEventListener('change', () => this.flush());
      this.$('subject').addEventListener('blur',    () => this.flush());
      // HTML pasted into the Plain box -> switch to HTML mode so it is not sent as plain text.
      this.$('bodyPlain').addEventListener('paste', () => setTimeout(() => this.detectPastedHtml(), 0));
      // Do not lose a pending edit when the tab is hidden or closed.
      this._onHide = () => { if (document.visibilityState === 'hidden') this.flush(true); };
      document.addEventListener('visibilitychange', this._onHide);
      window.addEventListener('pagehide', () => this.flush(true));
      this.$('typePlain').addEventListener('change', () => { this.switchMode(); this.autoSave(); });
      this.$('typeHtml').addEventListener('change', () => { this.switchMode(); this.autoSave(); });
      this.$('campaignRef').addEventListener('input', () => this.autoSave());
      this.$('campaignLang').addEventListener('input', () => this.autoSave());
      this.$('applyBtn').addEventListener('click', () => this.applyRefLang());
      this.$('textFromHtml').addEventListener('click', () => {
        this.$('bodyText').value = MailEditorComponent.htmlToText(this.$('bodyHtml').value);
        this.autoSave();
      });
      this.$('trailerBtn').addEventListener('click', () => this.insertTrailer());
      this.$('trBtn').addEventListener('click', () => this.translate());
      this.$('trUndo').addEventListener('click', () => this.undoTranslate());
      this.$('mainBtn').addEventListener('click', () => this.editCampaignMail());
      this.$('saveBtn').addEventListener('click', () => this.save(false));
      this.$('testBtn').addEventListener('click', () => this.openTest());
      this.root.querySelectorAll('[data-me-tab]').forEach(btn => {
        btn.addEventListener('click', () => this.showTab(btn.dataset.meTab));
      });
    }

    async load({ campaignId, stepIndex = null, stepNew = false, stepName = '', delay = 0 } = {}) {
      await this.flush();          // save any pending edit to the step we are leaving
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
      this.$('tools').style.display = '';
      this.$('campaignRef').value = c.campaign_ref || '';
      this.$('campaignLang').value = c.campaign_lang || '';
      this._beforeTranslate = null;
      this.$('trUndo').style.display = 'none';

      if (this.stepIndex != null) {
        const seq  = c.mail_sequence || [];
        const step = seq.find(s => s.index === this.stepIndex);
        const label = stepName || (this.stepIndex === 0 ? 'Intro' : `Reminder ${this.stepIndex}`);
        this.$('title').textContent = 'Mail editor';
        this.$('subtitle').textContent = this.campaignId;
        const badge = this.$('stepBadge');
        badge.textContent = label;
        badge.style.display = '';
        this.$('stepBar').style.display = 'flex';
        this.$('textPane').style.display = '';
        this.$('stepDelay').value = step ? (step.delay_days ?? 0) : (delay || 0);
        if (step) {
          const body = step.body_html || step.body_text || '';
          const type = step.body_html ? 'html' : 'plain';
          this.applyMail({ subject: step.subject || '', body, type, css: step.css || DEFAULT_CSS,
                          text: step.body_html ? (step.body_text || '') : '' });
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
      this.$('tools').style.display = 'none';     // ref, language, translation and trailer belong to a campaign
      this.$('textPane').style.display = 'none';  // a separate plain-text version exists on sequence steps only
      this.applyMail({ type: 'plain', css: DEFAULT_CSS, ...mail });
    }

    applyMail(mail) {
      const type = mail.type || 'plain';
      this.$('subject').value = mail.subject || '';
      this.$('bodyPlain').value = type === 'plain' ? (mail.body || '') : '';    // never the HTML source
      this.$('bodyHtml').value = mail.body || '';
      this.$('bodyText').value = type === 'html' ? (mail.text || '') : '';
      this.$('css').value = mail.css || DEFAULT_CSS;
      this.$(type === 'html' ? 'typeHtml' : 'typePlain').checked = true;
      this.switchMode();
      if (type === 'html') {
        this.initQuill();
        this._setQuillHtml(mail.body || '');
      }
      this.updatePreview();
    }

    // Show HTML in the Editor tab WITHOUT treating it as an edit.  Quill simplifies
    // markup it does not know (tables, inline styles ...); the raw HTML in the HTML tab
    // stays the source of truth until the user actually edits in the Editor.
    _setQuillHtml(html) {
      if (!this.quill) return;
      try {
        this.quill.setContents(this.quill.clipboard.convert({ html: html || '' }), 'silent');
      } catch (e) {
        console.warn('[mail-editor] could not show HTML in the Editor tab:', e.message);
      }
    }

    detectPastedHtml() {
      const v = this.$('bodyPlain').value;
      if (this.getType() !== 'plain') return;
      if (!/^\s*(<!doctype|<html|<body|<table|<div|<p[\s>]|<h[1-6][\s>]|<br|<a\s|<img|<span|<center)/i.test(v)) return;
      if (!/<\/[a-z]+>/i.test(v)) return;
      this.$('bodyHtml').value = v;
      this.$('bodyPlain').value = '';
      this.$('typeHtml').checked = true;
      this.switchMode();
      this.showTab('source');
      this.feedback('HTML detected - switched to HTML mode.', false);
      this.autoSave();
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
      this.quill.on('text-change', (delta, oldDelta, source) => {
        if (source === 'silent') return;      // programmatic display only
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
      if (!html && !this.$('bodyPlain').value.trim() && this.$('bodyText').value.trim()) {
        this.$('bodyPlain').value = this.$('bodyText').value;       // the plain-text version becomes the plain body
      }
      if (html && !this.$('bodyText').value.trim() && this.$('bodyPlain').value.trim()) {
        this.$('bodyText').value = this.$('bodyPlain').value;       // and the other way round
      }
      if (html) {
        this.initQuill();
        if (this.quill && !this.$('bodyHtml').value && this.$('bodyPlain').value) {
          this.$('bodyHtml').value = this.$('bodyPlain').value;
          this._setQuillHtml(this.$('bodyPlain').value);
        }
        this.showTab(this.currentTab === 'preview' || this.currentTab === 'source' ? this.currentTab : 'wysiwyg');
      }
    }

    showTab(tab) {
      // The HTML tab (textarea) always holds the real HTML; the Editor tab only mirrors it
      // and writes back when the user edits there (see text-change handler).
      if (this.currentTab === 'source' && tab !== 'source') this._setQuillHtml(this.$('bodyHtml').value);
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

    // The separate plain-text version of an HTML mail ('' for plain mails).
    getText() {
      return this.getType() === 'html' ? this.$('bodyText').value : '';
    }

    getBody() {
      if (this.getType() !== 'html') return this.$('bodyPlain').value;
      return this.$('bodyHtml').value;
    }

    buildPayload() {
      if (this.stepIndex != null) {
        const body    = this.getBody();
        const isHtml  = this.getType() === 'html';
        const idx     = this.stepIndex;
        return {
          ...this.campaignPayload(),
          mail_sequence_step: {
            index:      idx,
            mail_type:  idx === 0 ? 'intro' : `followup_${idx}`,
            delay_days: parseInt(this.$('stepDelay').value || '0', 10) || 0,
            subject:    this.$('subject').value.trim(),
            body_html:  isHtml ? body : '',
            body_text:  isHtml ? this.getText().trim() : body,
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
      return { ...this.campaignPayload(), mail };
    }

    // Campaign ref and language are saved with the mail. An invalid value is left out (and flagged),
    // so a typing mistake there never blocks saving the mail itself.
    campaignPayload() {
      if (!this.campaignId) return {};
      const ref = this.$('campaignRef').value.trim(), lang = this.$('campaignLang').value.trim().toLowerCase();
      const out = {};
      if (/^[A-Za-z0-9_-]*$/.test(ref)) out.campaign_ref = ref;
      else this.feedback('Campaign ref may only contain letters, digits, - and _ (not saved yet).', true);
      if (/^([a-z]{2,3}(-[a-z]{2})?)?$/.test(lang)) out.campaign_lang = lang;
      else this.feedback('Language must be a code such as da, en or pt-br (not saved yet).', true);
      return out;
    }

    toolsMessage(text, err = false) {
      const m = this.$('toolsMsg');
      m.textContent = text; m.style.color = err ? '#dc2626' : '';
    }

    // Replace the body (plain box, or HTML source + editor) and save.
    setBody(text) {
      if (this.getType() === 'html') {
        this.$('bodyHtml').value = text;
        this._setQuillHtml(text);
      } else {
        this.$('bodyPlain').value = text;
      }
      this.autoSave();
    }

    // Set /lang/<code> and ?ref=<ref> in every blueboot.ai link of the mail, keeping the rest of each link.
    applyRefLang() {
      const lang = this.$('campaignLang').value.trim().toLowerCase(), ref = this.$('campaignRef').value.trim();
      if (!lang && !ref) { this.toolsMessage('Fill in the ref and/or the language code first.', true); return; }
      const before = this.getBody();
      const { text, links } = MailEditorComponent.mergeRefLang(before, lang, ref);
      if (!links) { this.toolsMessage('No blueboot.ai links found in this mail.', true); return; }
      this.setBody(text);
      this.toolsMessage(text === before
        ? `All ${links} blueboot.ai link(s) already have this ref and language.`
        : `Updated ${links} blueboot.ai link(s).`);
    }

    // HTML -> readable plain text (block ends become blank lines, <br> a line break, links keep their address).
    static htmlToText(html) {
      let h = String(html || '')
        .replace(/<(script|style)[\s\S]*?<\/\1>/gi, '')
        .replace(/<a\b[^>]*href=["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi, (m, href, label) => {
          const t = label.replace(/<[^>]+>/g, '').trim();
          return !t || t === href || /^mailto:/i.test(href) ? (t || href) : `${t} (${href})`;
        })
        .replace(/<br\s*\/?>/gi, '\n')
        .replace(/<\/(p|div|h[1-6]|ul|ol|table|tr|blockquote)>/gi, '\n\n')
        .replace(/<li\b[^>]*>/gi, '- ')
        .replace(/<\/li>/gi, '\n');
      const doc = new DOMParser().parseFromString(h, 'text/html');    // inert: nothing loads or runs
      return (doc.body.textContent || '').replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
    }

    // Works on the mail text, so it covers href="..." in HTML and plain URLs in plain mails.
    static mergeRefLang(text, lang, ref) {
      let links = 0;
      const out = text.replace(/https?:\/\/(?:www\.)?blueboot\.ai[^\s"'<>)]*/gi, match => {
        const tail = (match.match(/[.,;:!?]+$/) || [''])[0];            // sentence punctuation is not part of the link
        const hadAmp = match.includes('&amp;');
        let url;
        try { url = new URL(match.slice(0, match.length - tail.length).replace(/&amp;/g, '&')); } catch (e) { return match; }
        links++;
        if (lang) {
          url.pathname = /^\/lang\/[A-Za-z-]+(?=\/|$)/.test(url.pathname)
            ? url.pathname.replace(/^\/lang\/[A-Za-z-]+/, '/lang/' + lang)
            : '/lang/' + lang + (url.pathname === '/' ? '/' : url.pathname);
        }
        if (ref) url.searchParams.set('ref', ref);
        const result = url.toString();
        return (hadAmp ? result.replace(/&/g, '&amp;') : result) + tail;
      });
      return { text: out, links };
    }

    // Copy the default trailer (Outreach Settings) to the end of the mail: the HTML version into
    // an HTML mail, the plain-text version into a plain mail.
    async insertTrailer() {
      try {
        const r = await fetch(`${this.base}/api/crm/mail-trailer`);
        const t = await r.json();
        if (t.status !== 'ok') throw new Error(t.message || 'Could not load the trailer');
        if (t.enabled && !confirm('The trailer is switched on, so it is already added to every mail when it is sent. Copy it into this mail anyway?')) return;
        const body = this.getBody();
        this.setBody(this.getType() === 'html'
          ? body + '<p><br></p>' + (t.html || '')
          : body.replace(/\s+$/, '') + '\n\n---\n' + (t.text || ''));
      } catch (e) {
        this.feedback(e.message, true);
      }
    }

    async translate() {
      const language = this.$('campaignLang').value.trim().toLowerCase();
      const btn = this.$('trBtn');
      if (!language) { this.toolsMessage('Write the language code to translate into, for example da.', true); return; }
      if (!confirm(`Translate the subject and the mail text into "${language}"? The HTML and the plain-text version are translated separately, an empty one is left empty. It replaces the current text (Undo is available).`)) return;
      const before = { subject: this.$('subject').value, body: this.getBody(), text: this.getText() };
      btn.disabled = true; btn.innerHTML = '<i class="ti ti-loader me-1"></i>Translating…';
      this.toolsMessage('Translating…');
      try {
        const r = await fetch(`${this.base}/api/crm/translate-mail`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ...before, language })
        });
        const d = await r.json();
        if (d.status !== 'ok') throw new Error(d.message || 'Translation failed');
        this._beforeTranslate = before;
        this.$('subject').value = d.subject;
        // Each version goes back into its own field; an empty version was not translated and stays empty.
        if (before.body.trim()) this.setBody(d.body);
        if (before.text.trim() && typeof d.text === 'string') this.$('bodyText').value = d.text;
        this.autoSave();
        this.$('trUndo').style.display = '';
        this.toolsMessage(`Translated into "${language}" and saved. Check the text, then use Apply to set the language in the links.`);
      } catch (e) {
        this.toolsMessage(e.message, true);
      }
      btn.disabled = false; btn.innerHTML = '<i class="ti ti-language me-1"></i>Translate';
    }

    undoTranslate() {
      if (!this._beforeTranslate) return;
      this.$('subject').value = this._beforeTranslate.subject;
      this.setBody(this._beforeTranslate.body);
      if (this.getType() === 'html') this.$('bodyText').value = this._beforeTranslate.text || '';
      this.autoSave();
      this._beforeTranslate = null;
      this.$('trUndo').style.display = 'none';
      this.toolsMessage('Translation undone.');
    }

    autoSave() {
      clearTimeout(this.saveTimer);
      this.saveTimer = setTimeout(() => { this.saveTimer = null; this.save(true); }, 900);
      this.updatePreview();
    }

    // Save a pending edit right now (blur, switching step, tab hidden / closed).
    flush(keepalive = false) {
      if (!this.saveTimer) return this._queue;
      clearTimeout(this.saveTimer);
      this.saveTimer = null;
      return this.save(true, keepalive);
    }

    // Saves run one after another so an older save can never overwrite a newer one.
    save(silent = false, keepalive = false) {
      // Build the request NOW, so a later step/campaign switch cannot change what is saved.
      if (!this.campaignId) return this._queue;
      clearTimeout(this.saveTimer); this.saveTimer = null;   // this save covers any pending edit
      const campaignId = this.campaignId;
      const payload    = JSON.stringify(this.buildPayload());
      this._queue = this._queue.then(() => this._send(campaignId, payload, silent, keepalive));
      return this._queue;
    }

    async _send(campaignId, payload, silent, keepalive) {
      if (!silent) this.feedback('Saving...');
      try {
        const r = await fetch(`${this.base}/api/crm/campaigns/${encodeURIComponent(campaignId)}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: payload,
          keepalive: !!keepalive && payload.length < 60000
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
        text: this.getText(),
        type: this.getType(),
        css: this.$('css').value || DEFAULT_CSS
      };
      this.root.dispatchEvent(new CustomEvent('mail-editor:test', { bubbles: true, detail }));
    }
  }

  window.MailEditorComponent = MailEditorComponent;
})();
