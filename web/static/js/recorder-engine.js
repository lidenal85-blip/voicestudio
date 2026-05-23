/**
 * recorder-engine.js
 * Обёртка над MediaRecorder с поддержкой timing offset (аудитор п.2.1.3).
 *
 * startWithOffset(delayMs):
 *   Запускает MediaRecorder с задержкой delayMs, чтобы компенсировать
 *   задержку аудиосистемы. Это значит, что первые delayMs звука игнорируются,
 *   и запись начинается чуть позже — выравнивая голос с инструменталом.
 *
 * Использование:
 *   const rec = new RecorderEngine();
 *   await rec.requestMic();
 *   rec.onLevel = (db) => updateMeter(db);
 *   rec.startWithOffset(120);   // offset 120мс
 *   // ... пользователь поёт ...
 *   const blob = await rec.stop();
 *   // blob — audio/webm или audio/wav в зависимости от браузера
 */

export class RecorderEngine {
  constructor() {
    this._stream      = null;
    this._mediaRec    = null;
    this._chunks      = [];
    this._startedAt   = 0;
    this._offsetMs    = 0;
    this._isRecording = false;

    // Коллбэки
    this.onLevel    = null;   // (dBFS: number) => void
    this.onStart    = null;   // () => void
    this.onStop     = null;   // (blob: Blob) => void
    this.onError    = null;   // (err) => void

    // Level meter через Web Audio
    this._ctx        = null;
    this._analyser   = null;
    this._levelTimer = null;
  }

  /** Запросить доступ к микрофону. */
  async requestMic(constraints = {}) {
    this._stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation:  constraints.echoCancellation  ?? false,
        noiseSuppression:  constraints.noiseSuppression  ?? false,
        autoGainControl:   constraints.autoGainControl   ?? false,
        sampleRate:        44100,
        channelCount:      1,
      },
    });

    // Level meter setup
    this._ctx      = new (window.AudioContext || window.webkitAudioContext)();
    const src      = this._ctx.createMediaStreamSource(this._stream);
    this._analyser = this._ctx.createAnalyser();
    this._analyser.fftSize = 256;
    src.connect(this._analyser);

    return this._stream;
  }

  /** Лучший поддерживаемый mimeType. */
  get mimeType() {
    const types = [
      'audio/webm;codecs=opus',
      'audio/webm',
      'audio/ogg;codecs=opus',
      'audio/wav',
    ];
    return types.find(t => MediaRecorder.isTypeSupported(t)) || '';
  }

  get isRecording() { return this._isRecording; }

  /** Продолжительность записи в мс. */
  get elapsedMs() {
    if (!this._isRecording || !this._startedAt) return 0;
    return Date.now() - this._startedAt;
  }

  /**
   * Начать запись с компенсацией latency.
   * @param {number} offsetMs — timing_offset_ms из LatencyMeasurer
   */
  startWithOffset(offsetMs = 0) {
    if (!this._stream) throw new Error('Нет доступа к микрофону. Вызови requestMic().');
    if (this._isRecording) return;

    this._offsetMs = offsetMs;
    this._chunks   = [];

    if (offsetMs > 0) {
      // Задержка: начинаем запись через offsetMs мс.
      // Это сдвигает mic-аудио "вперёд" — компенсирует то, что голос
      // физически опоздал из-за задержки динамика.
      setTimeout(() => this._startMediaRecorder(), offsetMs);
    } else {
      this._startMediaRecorder();
    }

    this._startLevelMeter();
  }

  /** Стандартный старт без offset. */
  start() { this.startWithOffset(0); }

  /** Остановить запись и вернуть Blob. */
  stop() {
    return new Promise((resolve, reject) => {
      if (!this._mediaRec || !this._isRecording) {
        return reject(new Error('Не в процессе записи'));
      }

      this._stopLevelMeter();
      this._isRecording = false;

      this._mediaRec.onstop = () => {
        const blob = new Blob(this._chunks, { type: this.mimeType });
        this.onStop?.(blob);
        resolve(blob);
      };
      this._mediaRec.stop();
    });
  }

  /** Отпустить микрофон. */
  release() {
    this._stopLevelMeter();
    this._stream?.getTracks().forEach(t => t.stop());
    this._stream   = null;
    this._mediaRec = null;
    this._ctx?.close();
  }

  // ── Private ────────────────────────────────────────────────────

  _startMediaRecorder() {
    try {
      this._mediaRec = new MediaRecorder(this._stream, {
        mimeType:    this.mimeType,
        audioBitsPerSecond: 128_000,
      });

      this._mediaRec.ondataavailable = e => {
        if (e.data.size > 0) this._chunks.push(e.data);
      };
      this._mediaRec.onerror = e => this.onError?.(e.error);

      this._mediaRec.start(100);   // chunk каждые 100мс
      this._startedAt   = Date.now();
      this._isRecording = true;
      this.onStart?.();
    } catch (e) {
      this.onError?.(e);
    }
  }

  _startLevelMeter() {
    if (!this._analyser) return;
    const data = new Uint8Array(this._analyser.frequencyBinCount);
    this._levelTimer = setInterval(() => {
      this._analyser.getByteFrequencyData(data);
      let sum = 0;
      for (let i = 0; i < data.length; i++) sum += data[i] * data[i];
      const rms  = Math.sqrt(sum / data.length);
      const dBFS = rms > 0 ? 20 * Math.log10(rms / 255) : -80;
      this.onLevel?.(Math.round(dBFS));
    }, 50);
  }

  _stopLevelMeter() {
    if (this._levelTimer) { clearInterval(this._levelTimer); this._levelTimer = null; }
  }
}
