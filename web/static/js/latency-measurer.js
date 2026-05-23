/**
 * latency-measurer.js
 * Echo-based измерение latency аудиосистемы (аудитор п.2.1.3).
 *
 * Алгоритм:
 *   1. Запрашиваем доступ к микрофону
 *   2. Генерируем короткий импульс (10мс chirp) через AudioContext → динамик
 *   3. Одновременно записываем с микрофона
 *   4. Находим пик в записи (кросс-корреляция с оригинальным сигналом)
 *   5. Разница между моментом воспроизведения и пиком = round-trip latency
 *   6. Возвращаем base_latency_ms = round_trip / 2
 *
 * Использование:
 *   const measurer = new LatencyMeasurer();
 *   const { latencyMs, confidence } = await measurer.measure();
 *   // latencyMs — рекомендуемый timing_offset_ms
 */

export class LatencyMeasurer {
  constructor() {
    this._stream  = null;
    this._ctx     = null;
  }

  /** Запросить микрофон. Должен вызываться после user gesture. */
  async requestMic() {
    this._stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation:   false,   // ВАЖНО: отключить, иначе echo не слышно
        noiseSuppression:   false,
        autoGainControl:    false,
        sampleRate:         44100,
      },
    });
    return this._stream;
  }

  /**
   * Провести измерение latency.
   * @param {number} numRounds — сколько раз повторить (усредняем), default 3
   * @returns {{ latencyMs: number, roundTripMs: number, confidence: number, samples: number[] }}
   */
  async measure(numRounds = 3) {
    if (!this._stream) await this.requestMic();

    this._ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 44100 });
    const sampleRate = this._ctx.sampleRate;

    // Генерируем impulse: короткий chirp 500Гц→2кГц за 10мс
    const impulse = this._makeChirp(sampleRate);
    const results = [];

    for (let r = 0; r < numRounds; r++) {
      const ms = await this._measureOnce(impulse, sampleRate);
      if (ms > 0 && ms < 1000) {     // игнорируем нереалистичные значения
        results.push(ms);
      }
      await this._sleep(400);        // пауза между тестами
    }

    await this._ctx.close();

    if (results.length === 0) {
      return { latencyMs: 0, roundTripMs: 0, confidence: 0, samples: [] };
    }

    const roundTripMs = results.reduce((a, b) => a + b, 0) / results.length;
    const latencyMs   = Math.round(roundTripMs / 2);      // одностороннее = half
    const variance    = this._variance(results);
    const confidence  = Math.max(0, 1 - variance / 10000); // 0..1

    return { latencyMs, roundTripMs: Math.round(roundTripMs), confidence, samples: results };
  }

  release() {
    this._stream?.getTracks().forEach(t => t.stop());
    this._stream = null;
  }

  // ── Private ────────────────────────────────────────────────────

  _makeChirp(sampleRate) {
    const durationMs = 12;
    const len   = Math.ceil(sampleRate * durationMs / 1000);
    const buf   = this._ctx.createBuffer(1, len, sampleRate);
    const data  = buf.getChannelData(0);
    const f0 = 400, f1 = 3000;
    for (let i = 0; i < len; i++) {
      const t   = i / sampleRate;
      const T   = durationMs / 1000;
      const phi = 2 * Math.PI * (f0 * t + (f1 - f0) / (2 * T) * t * t);
      // Hanning window
      const win = 0.5 * (1 - Math.cos(2 * Math.PI * i / len));
      data[i]   = Math.sin(phi) * win * 0.7;
    }
    return buf;
  }

  async _measureOnce(impulseBuffer, sampleRate) {
    return new Promise((resolve) => {
      const recordMs = 600;
      const chunks   = [];
      let   playedAt = 0;

      // Запись с микрофона
      const mediaRec = new MediaRecorder(this._stream, {
        mimeType: MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
          ? 'audio/webm;codecs=opus'
          : 'audio/webm',
      });
      mediaRec.ondataavailable = e => { if (e.data.size > 0) chunks.push(e.data); };
      mediaRec.onstop = async () => {
        const blob       = new Blob(chunks, { type: mediaRec.mimeType });
        const arrayBuf   = await blob.arrayBuffer();
        try {
          const decoded  = await this._ctx.decodeAudioData(arrayBuf);
          const pcm      = decoded.getChannelData(0);
          const peakIdx  = this._findPeakCorr(pcm, impulseBuffer.getChannelData(0), sampleRate);
          if (peakIdx < 0) return resolve(-1);
          const echoMs   = (peakIdx / decoded.sampleRate) * 1000;
          resolve(Math.max(0, echoMs - (recordMs * 0.05)));  // вычитаем небольшое смещение
        } catch {
          resolve(-1);
        }
      };

      mediaRec.start();

      // Воспроизводим импульс через 50мс после старта записи
      setTimeout(() => {
        const src = this._ctx.createBufferSource();
        src.buffer = impulseBuffer;

        // Через GainNode → destination (динамик)
        const gain = this._ctx.createGain();
        gain.gain.value = 0.8;
        src.connect(gain);
        gain.connect(this._ctx.destination);

        playedAt = this._ctx.currentTime;
        src.start();
      }, 50);

      // Останавливаем запись
      setTimeout(() => mediaRec.stop(), recordMs);
    });
  }

  /**
   * Найти позицию пика через cross-correlation с template.
   * Возвращает индекс максимальной корреляции в pcm.
   */
  _findPeakCorr(pcm, template, sampleRate) {
    const tLen     = template.length;
    const startIdx = Math.floor(sampleRate * 0.03); // пропускаем 30мс (прямой звук)
    let   maxCorr  = 0, maxIdx = -1;

    for (let i = startIdx; i < pcm.length - tLen; i += 2) {  // шаг 2 для скорости
      let corr = 0;
      for (let j = 0; j < tLen; j++) {
        corr += pcm[i + j] * template[j];
      }
      if (corr > maxCorr) { maxCorr = corr; maxIdx = i; }
    }

    // Требуем минимальный уровень корреляции (иначе — не нашли)
    const maxPcm = Math.max(...Array.from(pcm).slice(startIdx, startIdx + 1000).map(Math.abs));
    if (maxCorr < maxPcm * tLen * 0.05) return -1;

    return maxIdx;
  }

  _variance(arr) {
    const mean = arr.reduce((a, b) => a + b, 0) / arr.length;
    return arr.reduce((s, v) => s + (v - mean) ** 2, 0) / arr.length;
  }

  _sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
}
