/**
 * lrc-editor.js
 * LRC-редактор с Command pattern (undo/redo) и Delta-трекингом.
 *
 * Delta Update: отслеживает только изменённые строки с момента последнего сохранения.
 * При сохранении отправляет PATCH /api/projects/{id}/transcription/lines
 * только с изменёнными строками (не весь массив).
 *
 * Использование:
 *   const editor = new LRCEditor({ lines, projectId, onSave, onLinePlay });
 *   editor.mount(containerEl);
 */

// ── Команды (Command Pattern) ──────────────────────────────────

class EditTextCmd {
  constructor(editor, index, oldText, newText) {
    this.editor = editor; this.index = index;
    this.oldText = oldText; this.newText = newText;
  }
  execute() { this.editor._applyText(this.index, this.newText); }
  undo()    { this.editor._applyText(this.index, this.oldText); }
}

class EditTimingCmd {
  constructor(editor, index, field, oldVal, newVal) {
    this.editor = editor; this.index = index;
    this.field = field; this.oldVal = oldVal; this.newVal = newVal;
  }
  execute() { this.editor._applyTiming(this.index, this.field, this.newVal); }
  undo()    { this.editor._applyTiming(this.index, this.field, this.oldVal); }
}

// ── UndoRedo stack ─────────────────────────────────────────────

class UndoRedoStack {
  constructor(limit = 100) {
    this._stack = []; this._pos = -1; this._limit = limit;
  }
  push(cmd) {
    this._stack.splice(this._pos + 1);
    this._stack.push(cmd);
    if (this._stack.length > this._limit) this._stack.shift();
    this._pos = this._stack.length - 1;
    cmd.execute();
  }
  undo() {
    if (this._pos < 0) return false;
    this._stack[this._pos--].undo();
    return true;
  }
  redo() {
    if (this._pos >= this._stack.length - 1) return false;
    this._stack[++this._pos].execute();
    return true;
  }
  get canUndo() { return this._pos >= 0; }
  get canRedo() { return this._pos < this._stack.length - 1; }
}

// ── LRCEditor ─────────────────────────────────────────────────

export class LRCEditor {
  /**
   * @param {Object} opts
   * @param {Array}    opts.lines      - [{index, text, start_ms, end_ms, version}, ...]
   * @param {string}   opts.projectId
   * @param {Function} opts.onSave     - ({updatedLines, slicingJobId}) => void
   * @param {Function} opts.onLinePlay - (line) => void  (клик по кнопке ▷)
   * @param {Function} opts.onDirty   - (bool) => void
   */
  constructor(opts = {}) {
    this._opts       = opts;
    this._lines      = (opts.lines || []).map(l => ({ ...l }));
    this._dirty      = new Set();       // индексы изменённых строк
    this._stack      = new UndoRedoStack();
    this._container  = null;
    this._saving     = false;
    this._searchQ    = '';
  }

  // ── Mount ──────────────────────────────────────────────────────

  mount(el) {
    this._container = el;
    this._render();
    this._bindKeyboard();
  }

  // ── Public API ─────────────────────────────────────────────────

  undo() { if (this._stack.undo()) this._refresh(); }
  redo() { if (this._stack.redo()) this._refresh(); }

  get isDirty()      { return this._dirty.size > 0; }
  get dirtyCount()   { return this._dirty.size; }
  get lines()        { return this._lines; }

  /** Применить изменения с сервера (например, после reload). */
  updateLines(newLines) {
    this._lines = newLines.map(l => ({ ...l }));
    this._dirty.clear();
    this._refresh();
  }

  /** Отправить дельту изменений на сервер. */
  async save() {
    if (!this.isDirty || this._saving) return;
    this._saving = true;
    this._updateSaveBtn();

    const changedLines = [...this._dirty].map(idx => {
      const l = this._lines[idx];
      return { index: l.index, text: l.text, start_ms: l.start_ms, end_ms: l.end_ms };
    });

    try {
      const r = await fetch(`/api/projects/${this._opts.projectId}/transcription/lines`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ changed_lines: changedLines }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      this._dirty.clear();
      this._refresh();
      this._opts.onSave?.(data);
      showToast?.(`Сохранено ${changedLines.length} строк`);
    } catch (e) {
      showToast?.(`Ошибка сохранения: ${e.message}`, false);
    } finally {
      this._saving = false;
      this._updateSaveBtn();
    }
  }

  // ── Internal mutations ─────────────────────────────────────────

  _applyText(index, text) {
    const line = this._lines.find(l => l.index === index);
    if (!line) return;
    line.text = text;
    this._dirty.add(index);
    this._opts.onDirty?.(true);
    this._refreshRow(index);
    this._updateSaveBtn();
  }

  _applyTiming(index, field, val) {
    const line = this._lines.find(l => l.index === index);
    if (!line) return;
    line[field] = val;
    this._dirty.add(index);
    this._opts.onDirty?.(true);
    this._refreshRow(index);
    this._updateSaveBtn();
  }

  // ── Rendering ──────────────────────────────────────────────────

  _render() {
    if (!this._container) return;
    this._container.innerHTML = `
      <div class="lrc-editor">
        <div class="lrc-toolbar">
          <input class="lrc-search" placeholder="🔍 Поиск по тексту..." />
          <span class="lrc-dirty-badge" style="display:none">
            <span class="dirty-count">0</span> изменений
          </span>
          <div class="lrc-toolbar-right">
            <button class="btn" id="lrc-undo" disabled title="Undo (Ctrl+Z)">↩ Undo</button>
            <button class="btn" id="lrc-redo" disabled title="Redo (Ctrl+Y)">↪ Redo</button>
            <button class="btn btn-primary" id="lrc-save" disabled>💾 Сохранить</button>
          </div>
        </div>
        <div class="lrc-lines" id="lrc-lines-list"></div>
      </div>`;

    this._bindToolbar();
    this._renderLines();
  }

  _renderLines() {
    const list = this._container.querySelector('#lrc-lines-list');
    if (!list) return;
    const q = this._searchQ.toLowerCase();

    const filtered = q
      ? this._lines.filter(l => l.text.toLowerCase().includes(q))
      : this._lines;

    list.innerHTML = filtered.map(l => this._rowHTML(l)).join('');
    this._bindRows(list);
  }

  _rowHTML(l) {
    const dirty = this._dirty.has(l.index) ? 'lrc-row--dirty' : '';
    const tStart = this._msToInput(l.start_ms);
    const tEnd   = this._msToInput(l.end_ms);
    return `
      <div class="lrc-row ${dirty}" data-index="${l.index}">
        <span class="lrc-idx">${l.index + 1}</span>
        <div class="lrc-timings">
          <input class="lrc-time-input" data-field="start_ms" data-index="${l.index}"
                 value="${tStart}" title="Начало" />
          <span class="lrc-time-sep">→</span>
          <input class="lrc-time-input" data-field="end_ms" data-index="${l.index}"
                 value="${tEnd}" title="Конец" />
        </div>
        <input class="lrc-text-input" data-index="${l.index}" value="${this._esc(l.text)}" />
        <button class="lrc-play-btn" data-index="${l.index}" title="Перейти к строке">▷</button>
      </div>`;
  }

  _bindRows(list) {
    // Текст
    list.querySelectorAll('.lrc-text-input').forEach(inp => {
      inp.addEventListener('change', e => {
        const idx  = parseInt(e.target.dataset.index);
        const line = this._lines.find(l => l.index === idx);
        if (!line || line.text === e.target.value) return;
        this._stack.push(new EditTextCmd(this, idx, line.text, e.target.value));
        this._updateUndoRedo();
      });
    });

    // Тайминги
    list.querySelectorAll('.lrc-time-input').forEach(inp => {
      inp.addEventListener('change', e => {
        const idx   = parseInt(e.target.dataset.index);
        const field = e.target.dataset.field;
        const ms    = this._inputToMs(e.target.value);
        const line  = this._lines.find(l => l.index === idx);
        if (!line || isNaN(ms) || line[field] === ms) return;
        this._stack.push(new EditTimingCmd(this, idx, field, line[field], ms));
        this._updateUndoRedo();
      });
    });

    // Кнопки ▷
    list.querySelectorAll('.lrc-play-btn').forEach(btn => {
      btn.addEventListener('click', e => {
        const idx  = parseInt(e.target.dataset.index);
        const line = this._lines.find(l => l.index === idx);
        if (line) this._opts.onLinePlay?.(line);
      });
    });
  }

  _bindToolbar() {
    this._container.querySelector('.lrc-search').addEventListener('input', e => {
      this._searchQ = e.target.value;
      this._renderLines();
    });
    this._container.querySelector('#lrc-undo').addEventListener('click', () => this.undo());
    this._container.querySelector('#lrc-redo').addEventListener('click', () => this.redo());
    this._container.querySelector('#lrc-save').addEventListener('click', () => this.save());
  }

  _bindKeyboard() {
    document.addEventListener('keydown', e => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'z' && !e.shiftKey) { e.preventDefault(); this.undo(); }
      if ((e.ctrlKey || e.metaKey) && (e.key === 'y' || (e.key === 'z' && e.shiftKey))) { e.preventDefault(); this.redo(); }
      if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); this.save(); }
    });
  }

  _refresh() { this._renderLines(); this._updateSaveBtn(); this._updateUndoRedo(); }

  _refreshRow(index) {
    const line = this._lines.find(l => l.index === index);
    if (!line) return;
    const row = this._container?.querySelector(`[data-index="${index}"].lrc-row`);
    if (!row) return;
    row.outerHTML = this._rowHTML(line);
    // Re-bind the new row
    const list = this._container?.querySelector('#lrc-lines-list');
    if (list) this._bindRows(list);
  }

  _updateSaveBtn() {
    const btn = this._container?.querySelector('#lrc-save');
    const badge = this._container?.querySelector('.lrc-dirty-badge');
    if (!btn) return;
    btn.disabled  = !this.isDirty || this._saving;
    btn.textContent = this._saving ? '⏳ Сохраняю...' : '💾 Сохранить';
    if (badge) {
      badge.style.display = this.isDirty ? 'inline-flex' : 'none';
      const cnt = badge.querySelector('.dirty-count');
      if (cnt) cnt.textContent = this._dirty.size;
    }
  }

  _updateUndoRedo() {
    const u = this._container?.querySelector('#lrc-undo');
    const r = this._container?.querySelector('#lrc-redo');
    if (u) u.disabled = !this._stack.canUndo;
    if (r) r.disabled = !this._stack.canRedo;
  }

  _msToInput(ms) {
    const s = ms / 1000;
    const m = Math.floor(s / 60);
    return `${String(m).padStart(2,'0')}:${(s % 60).toFixed(2).padStart(5,'0')}`;
  }

  _inputToMs(str) {
    const parts = str.split(':');
    if (parts.length !== 2) return NaN;
    return (parseInt(parts[0]) * 60 + parseFloat(parts[1])) * 1000;
  }

  _esc(s) { return (s||'').replace(/"/g,'&quot;').replace(/</g,'&lt;'); }
}
