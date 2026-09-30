// Unix-socket client to core/. Reconnects with backoff; core is the long-lived side.
import { EventEmitter } from 'node:events';
import net from 'node:net';
import { encode, LineSplitter } from './protocol.js';

const MAX_BACKLOG = 500;

export class CoreLink extends EventEmitter {
  constructor(path, { log = console, minDelayMs = 1000, maxDelayMs = 30000 } = {}) {
    super();
    this.path = path;
    this.log = log;
    this.minDelay = minDelayMs;
    this.maxDelay = maxDelayMs;
    this.delay = minDelayMs;
    this.sock = null;
    this.connected = false;
    this.closed = false;
    this.backlog = []; // inbound WA messages while core is away; never lose these
    this.lastState = null;
  }

  start() {
    this.#connect();
  }

  close() {
    this.closed = true;
    clearTimeout(this.timer);
    this.sock?.destroy();
  }

  // Messages are buffered across core restarts; state is re-sent on reconnect;
  // results for requests that died with the old connection are dropped.
  send(obj) {
    if (obj.type === 'state') this.lastState = obj;
    if (this.connected) {
      this.sock.write(encode(obj));
    } else if (obj.type === 'message') {
      this.backlog.push(obj);
      if (this.backlog.length > MAX_BACKLOG) this.backlog.shift();
    }
  }

  #connect() {
    if (this.closed) return;
    const sock = net.createConnection(this.path);
    const lines = new LineSplitter();
    sock.setEncoding('utf8');
    sock.on('connect', () => {
      this.sock = sock;
      this.connected = true;
      this.delay = this.minDelay;
      this.log.info?.(`connected to core at ${this.path}`);
      if (this.lastState) sock.write(encode(this.lastState));
      for (const m of this.backlog.splice(0)) sock.write(encode(m));
      this.emit('connect');
    });
    sock.on('data', (chunk) => {
      for (const line of lines.feed(chunk)) {
        let req;
        try {
          req = JSON.parse(line);
        } catch {
          this.log.warn?.('bad line from core');
          continue;
        }
        this.emit('request', req);
      }
    });
    sock.on('error', (e) => {
      if (this.connected) this.log.warn?.(`core socket error: ${e.message}`);
    });
    sock.on('close', () => {
      const was = this.connected;
      this.connected = false;
      this.sock = null;
      if (was) this.log.warn?.('core connection closed');
      if (this.closed) return;
      this.timer = setTimeout(() => this.#connect(), this.delay);
      this.delay = Math.min(this.delay * 2, this.maxDelay);
    });
  }
}
