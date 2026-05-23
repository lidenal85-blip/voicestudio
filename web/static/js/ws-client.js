/**
 * ws-client.js
 * Reconnecting WebSocket client для получения прогресса обработки.
 * Использование:
 *   const ws = new StudioWS('/ws/projects/PROJECT_ID');
 *   ws.on('progress', e => console.log(e.stage, e.percent));
 *   ws.on('done',     e => location.reload());
 *   ws.on('error',    e => console.error(e.message));
 *   ws.connect();
 */

export class StudioWS {
  constructor(path) {
    this._path      = path;
    this._ws        = null;
    this._handlers  = {};
    this._reconnect = true;
    this._delay     = 2000;
    this._maxDelay  = 30000;
    this._pingTimer = null;
  }

  on(event, handler) {
    if (!this._handlers[event]) this._handlers[event] = [];
    this._handlers[event].push(handler);
    return this;
  }

  _emit(event, data) {
    (this._handlers[event] || []).forEach(h => h(data));
    (this._handlers['*']    || []).forEach(h => h({ event, ...data }));
  }

  connect() {
    if (this._ws?.readyState === WebSocket.OPEN) return;

    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url   = `${proto}://${location.host}${this._path}`;
    this._ws    = new WebSocket(url);

    this._ws.onopen = () => {
      this._delay = 2000;
      this._emit('connected', {});
      // Keepalive ping каждые 25с
      this._pingTimer = setInterval(() => {
        if (this._ws?.readyState === WebSocket.OPEN) {
          this._ws.send('ping');
        }
      }, 25000);
    };

    this._ws.onmessage = ({ data }) => {
      try {
        const msg = JSON.parse(data);
        if (msg.event !== 'pong') this._emit(msg.event, msg);
      } catch {}
    };

    this._ws.onclose = () => {
      clearInterval(this._pingTimer);
      this._emit('disconnected', {});
      if (this._reconnect) {
        setTimeout(() => this.connect(), this._delay);
        this._delay = Math.min(this._delay * 1.5, this._maxDelay);
      }
    };

    this._ws.onerror = () => {
      this._ws?.close();
    };
  }

  disconnect() {
    this._reconnect = false;
    clearInterval(this._pingTimer);
    this._ws?.close();
  }
}
