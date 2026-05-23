/**
 * waveform.js
 * Real-time waveform renderer на Canvas через Web Audio AnalyserNode.
 *
 * Использование:
 *   const wf = new WaveformRenderer(canvasEl, analyserNode);
 *   wf.start();   // запустить анимацию
 *   wf.stop();    // остановить
 *   wf.setColor('#7afdd6');
 *   wf.drawStatic(audioBuffer); // статичная форма волны
 */

export class WaveformRenderer {
  constructor(canvas, analyser = null) {
    this._canvas   = canvas;
    this._ctx      = canvas.getContext('2d');
    this._analyser = analyser;
    this._raf      = null;
    this._color    = '#7afdd6';
    this._bgColor  = 'transparent';
    this._running  = false;

    // Retina support
    this._resize();
    window.addEventListener('resize', () => this._resize());
  }

  setAnalyser(analyser) {
    this._analyser = analyser;
    return this;
  }

  setColor(color) { this._color = color; return this; }

  _resize() {
    const rect = this._canvas.getBoundingClientRect();
    const dpr  = window.devicePixelRatio || 1;
    this._canvas.width  = rect.width  * dpr;
    this._canvas.height = rect.height * dpr;
    this._ctx.scale(dpr, dpr);
    this._W = rect.width;
    this._H = rect.height;
  }

  /** Запуск real-time анимации. */
  start() {
    if (this._running) return;
    this._running = true;
    this._tick();
  }

  stop() {
    this._running = false;
    if (this._raf) { cancelAnimationFrame(this._raf); this._raf = null; }
    this._clear();
  }

  _tick() {
    if (!this._running) return;
    this._draw();
    this._raf = requestAnimationFrame(() => this._tick());
  }

  _clear() {
    this._ctx.clearRect(0, 0, this._W, this._H);
  }

  _draw() {
    if (!this._analyser) { this._clear(); return; }

    const bufLen = this._analyser.frequencyBinCount;
    const data   = new Uint8Array(bufLen);
    this._analyser.getByteTimeDomainData(data);

    const ctx = this._ctx;
    const W = this._W, H = this._H;

    ctx.clearRect(0, 0, W, H);

    // Центральная линия (тихо)
    ctx.strokeStyle = 'rgba(122,253,214,0.08)';
    ctx.lineWidth   = 1;
    ctx.beginPath();
    ctx.moveTo(0, H / 2);
    ctx.lineTo(W, H / 2);
    ctx.stroke();

    // Waveform
    ctx.beginPath();
    ctx.strokeStyle = this._color;
    ctx.lineWidth   = 1.5;
    ctx.shadowColor = this._color;
    ctx.shadowBlur  = 4;
    ctx.lineJoin    = 'round';

    const sliceW = W / bufLen;
    let x = 0;
    for (let i = 0; i < bufLen; i++) {
      const v = data[i] / 128;
      const y = (v * H) / 2;
      if (i === 0) ctx.moveTo(x, y);
      else         ctx.lineTo(x, y);
      x += sliceW;
    }
    ctx.stroke();
    ctx.shadowBlur = 0;
  }

  /**
   * Отрисовать статичную форму волны (для превью).
   * @param {Float32Array} pcmData - данные из AudioBuffer.getChannelData(0)
   * @param {number} progressRatio - 0..1, закрашивает часть другим цветом
   */
  drawStatic(pcmData, progressRatio = 0) {
    if (this._running) return;   // не перебиваем live

    const ctx = this._ctx;
    const W = this._W, H = this._H;
    ctx.clearRect(0, 0, W, H);

    const step      = Math.ceil(pcmData.length / W);
    const halfH     = H / 2;
    const progX     = W * progressRatio;

    // Центральная линия
    ctx.strokeStyle = 'rgba(122,253,214,0.08)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, halfH); ctx.lineTo(W, halfH);
    ctx.stroke();

    // Bars
    for (let i = 0; i < W; i++) {
      let min = 1, max = -1;
      for (let j = 0; j < step; j++) {
        const val = pcmData[(i * step + j)] ?? 0;
        if (val < min) min = val;
        if (val > max) max = val;
      }
      const barH  = Math.max(2, (max - min) * halfH);
      const yTop  = halfH - barH / 2;

      ctx.fillStyle = i < progX
        ? this._color
        : 'rgba(122,253,214,0.25)';
      ctx.fillRect(i, yTop, 1, barH);
    }
  }

  /** Обновить позицию прогресса на статичной волне. */
  updateProgress(ratio, pcmData) {
    this.drawStatic(pcmData, ratio);
  }
}
