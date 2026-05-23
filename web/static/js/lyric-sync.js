/**
 * lyric-sync.js
 * LRC-парсер и движок синхронизации lyrics с аудио.
 *
 * Использование:
 *   const sync = new LyricSync(lrcContent);
 *   // В requestAnimationFrame:
 *   const line = sync.getLineAt(currentTimeMs);
 *   if (sync.hasChanged()) { ... }
 */

export class LyricLine {
  constructor(index, text, startMs, endMs) {
    this.index   = index;
    this.text    = text;
    this.startMs = startMs;
    this.endMs   = endMs;
  }
}

export class LyricSync {
  static TAG_RE  = /^\[(\d{2}):(\d{2}\.\d{2})\](.+)$/;
  static META_RE = /^\[(?:ti|ar|al|by|offset):/;

  constructor(lrcContent = '') {
    this.lines       = [];
    this._currentIdx = -1;
    if (lrcContent) this.load(lrcContent);
  }

  /** Загружает LRC-текст, парсит в массив LyricLine. */
  load(lrcContent) {
    this.lines = [];
    this._currentIdx = -1;

    const raw = [];
    for (const line of lrcContent.split('\n')) {
      const l = line.trim();
      if (!l || LyricSync.META_RE.test(l)) continue;
      const m = LyricSync.TAG_RE.exec(l);
      if (!m) continue;
      const startMs = parseInt(m[1]) * 60_000 + Math.round(parseFloat(m[2]) * 1000);
      raw.push({ startMs, text: m[3].trim() });
    }

    // Сортируем и проставляем endMs
    raw.sort((a, b) => a.startMs - b.startMs);
    this.lines = raw.map((r, i) => new LyricLine(
      i, r.text, r.startMs,
      raw[i + 1]?.startMs ?? (r.startMs + 5000),
    ));
  }

  /**
   * Возвращает индекс строки для данного времени (мс).
   * Обновляет внутренний _currentIdx.
   */
  getIndexAt(timeMs) {
    if (!this.lines.length) return -1;
    let lo = 0, hi = this.lines.length - 1, result = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (this.lines[mid].startMs <= timeMs) { result = mid; lo = mid + 1; }
      else hi = mid - 1;
    }
    return result;
  }

  /**
   * Обновляет текущую строку и возвращает { entered, exited, current }.
   * entered/exited null если ничего не изменилось.
   */
  tick(timeMs) {
    const newIdx = this.getIndexAt(timeMs);
    if (newIdx === this._currentIdx) return { entered: null, exited: null, current: this.lines[newIdx] ?? null };
    const exited  = this._currentIdx >= 0 ? this.lines[this._currentIdx] : null;
    const entered = newIdx >= 0 ? this.lines[newIdx] : null;
    this._currentIdx = newIdx;
    return { entered, exited, current: entered };
  }

  /** Сбросить состояние (при перемотке). */
  reset() { this._currentIdx = -1; }

  get current() { return this.lines[this._currentIdx] ?? null; }
  get total()   { return this.lines.length; }
  get isEmpty() { return this.lines.length === 0; }

  /**
   * Найти строку по индексу.
   */
  getLine(index) { return this.lines[index] ?? null; }
}
