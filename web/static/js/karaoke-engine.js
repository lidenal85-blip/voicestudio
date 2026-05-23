/**
 * karaoke-engine.js
 * Ядро карaoke-плеера.
 *
 * КРИТИЧЕСКАЯ правка аудитора п.1:
 * Использует MediaElementAudioSourceNode + <audio> вместо AudioBufferSourceNode.
 * Это позволяет начать воспроизведение до полной загрузки файла (streaming).
 *
 * Web Audio граф:
 *   <audio> → MediaElementAudioSourceNode
 *           → GainNode (volume)
 *           → AnalyserNode (waveform fftSize=2048)
 *           → AudioContext.destination
 *
 * Примечание по pitch shift:
 *   Реальный pitch shift (независимый от темпа) требует phase vocoder / AudioWorklet.
 *   В MVP: speed меняет playbackRate (tempo + pitch вместе).
 *   Независимый pitch shift — задача этапа 5 (Advanced Features).
 *
 * Использование:
 *   const engine = new KaraokeEngine({ lrcContent, onLineEnter, onTimeUpdate });
 *   await engine.load('https://.../vocal.wav');
 *   engine.play();
 */

import { LyricSync } from './lyric-sync.js';

// Синглтон AudioContext (одно приложение — один контекст)
let _sharedContext = null;
function getAudioContext() {
  if (!_sharedContext || _sharedContext.state === 'closed') {
    _sharedContext = new (window.AudioContext || window.webkitAudioContext)();
  }
  return _sharedContext;
}

export class KaraokeEngine {
  /**
   * @param {Object} opts
   * @param {string}   opts.lrcContent     - LRC-текст
   * @param {Function} opts.onReady        - () => void
   * @param {Function} opts.onPlay         - () => void
   * @param {Function} opts.onPause        - () => void
   * @param {Function} opts.onEnded        - () => void
   * @param {Function} opts.onError        - (err) => void
   * @param {Function} opts.onTimeUpdate   - (currentMs) => void
   * @param {Function} opts.onLineEnter    - (line) => void
   * @param {Function} opts.onLineExit     - (line) => void
   * @param {Function} opts.onBuffering    - (bool) => void
   */
  constructor(opts = {}) {
    this._opts    = opts;
    this._sync    = new LyricSync(opts.lrcContent || '');
    this._audio   = null;    // HTMLAudioElement
    this._source  = null;    // MediaElementAudioSourceNode
    this._gain    = null;    // GainNode
    this._analyser= null;    // AnalyserNode
    this._ctx     = null;    // AudioContext
    this._raf     = null;
    this._loaded  = false;
    this._volume  = 1.0;
    this._speed   = 1.0;
  }

  // ── Публичный API ──────────────────────────────────────────────

  /**
   * Загрузить трек. Не ждёт полной загрузки — можно сразу play().
   * @param {string} url
   */
  async load(url) {
    this._teardown();

    this._ctx      = getAudioContext();
    this._audio    = new Audio();
    this._audio.crossOrigin = 'anonymous';
    this._audio.preload     = 'auto';

    // Граф
    this._source   = this._ctx.createMediaElementSource(this._audio);
    this._gain     = this._ctx.createGain();
    this._analyser = this._ctx.createAnalyser();
    this._analyser.fftSize = 2048;

    this._source.connect(this._gain);
    this._gain.connect(this._analyser);
    this._analyser.connect(this._ctx.destination);

    this._gain.gain.value = this._volume;

    // Обработчики HTMLAudioElement
    this._audio.addEventListener('canplay',  () => {
      if (!this._loaded) {
        this._loaded = true;
        this._emit('onReady');
      }
    });
    this._audio.addEventListener('playing',  () => { this._startRaf(); this._emit('onPlay'); });
    this._audio.addEventListener('pause',    () => { this._stopRaf();  this._emit('onPause'); });
    this._audio.addEventListener('ended',    () => { this._stopRaf();  this._emit('onEnded'); });
    this._audio.addEventListener('error',    (e) => this._emit('onError', e));
    this._audio.addEventListener('waiting',  () => this._emit('onBuffering', true));
    this._audio.addEventListener('playing',  () => this._emit('onBuffering', false));
    this._audio.addEventListener('seeked',   () => { this._sync.reset(); });

    this._audio.src = url;
    this._audio.load();
  }

  /** Сменить LRC без перезагрузки аудио. */
  setLrc(lrcContent) {
    this._sync.load(lrcContent);
    this._sync.reset();
  }

  play() {
    if (!this._audio) return;
    // AudioContext может быть suspended после user gesture
    if (this._ctx?.state === 'suspended') this._ctx.resume();
    this._audio.play().catch(e => this._emit('onError', e));
  }

  pause() { this._audio?.pause(); }

  /** Перемотка в миллисекундах. */
  seekTo(ms) {
    if (!this._audio) return;
    this._audio.currentTime = ms / 1000;
    this._sync.reset();
  }

  /** Скорость воспроизведения 0.5–2.0. */
  setSpeed(rate) {
    this._speed = Math.max(0.25, Math.min(2.0, rate));
    if (this._audio) this._audio.playbackRate = this._speed;
  }

  /** Громкость 0–100. */
  setVolume(percent) {
    this._volume = percent / 100;
    if (this._gain) this._gain.gain.setTargetAtTime(this._volume, this._ctx.currentTime, 0.01);
  }

  /** Перейти к строке по индексу. */
  jumpToLine(index) {
    const line = this._sync.getLine(index);
    if (line) this.seekTo(line.startMs);
  }

  /** AnalyserNode для WaveformRenderer. */
  get analyser() { return this._analyser; }

  /** Текущее время в мс. */
  get currentMs() { return (this._audio?.currentTime ?? 0) * 1000; }

  /** Длительность в мс. */
  get durationMs() { return (this._audio?.duration ?? 0) * 1000; }

  get isPlaying() { return this._audio && !this._audio.paused && !this._audio.ended; }

  get linesTotal() { return this._sync.total; }

  destroy() { this._teardown(); }

  // ── Private ────────────────────────────────────────────────────

  _emit(name, data) {
    const fn = this._opts[name];
    if (typeof fn === 'function') fn(data);
  }

  _startRaf() {
    if (this._raf) return;
    const tick = () => {
      if (!this._audio || this._audio.paused) return;
      const ms = this.currentMs;

      // Time update
      this._emit('onTimeUpdate', ms);

      // Lyric sync
      const { entered, exited } = this._sync.tick(ms);
      if (entered) this._emit('onLineEnter', entered);
      if (exited)  this._emit('onLineExit',  exited);

      this._raf = requestAnimationFrame(tick);
    };
    this._raf = requestAnimationFrame(tick);
  }

  _stopRaf() {
    if (this._raf) { cancelAnimationFrame(this._raf); this._raf = null; }
  }

  _teardown() {
    this._stopRaf();
    if (this._audio) {
      this._audio.pause();
      this._audio.src = '';
      this._audio = null;
    }
    // Отключаем граф
    try { this._source?.disconnect(); }  catch {}
    try { this._gain?.disconnect(); }    catch {}
    try { this._analyser?.disconnect(); } catch {}
    this._source = this._gain = this._analyser = null;
    this._loaded = false;
    this._sync.reset();
  }
}
