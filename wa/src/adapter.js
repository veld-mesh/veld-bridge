// Owns the whatsapp-web.js Client lifecycle and answers core's requests.
//
// Lifecycle rules (ADR-001):
// - on `disconnected` the library destroys itself -> build a NEW Client;
// - "not ready within N min" watchdog (known upstream hang) -> rebuild;
// - kill leftover Chromium children before re-init;
// - send with { sendSeen: false }; mark seen only when core asks.
import { contactSummary, normalize, serializeId, shouldDrop } from './protocol.js';

export class Adapter {
  constructor({
    clientFactory,
    link,
    log = console,
    readyTimeoutMs = 5 * 60_000,
    rebuildDelayMs = 10_000,
    killChildren = async () => {},
    onQr = async () => {},
    onReady = async () => {},
  }) {
    this.clientFactory = clientFactory;
    this.link = link;
    this.log = log;
    this.readyTimeoutMs = readyTimeoutMs;
    this.rebuildDelayMs = rebuildDelayMs;
    this.killChildren = killChildren;
    this.onQr = onQr;
    this.onReady = onReady;
    this.client = null;
    this.ready = false;
    this.generation = 0;
    this.stopped = false;
    this.watchdog = null;
    this.rebuildTimer = null;
    this.onRequest = (req) => this.#handle(req);
    this.ownSendMs = 30_000;
    this.sentAt = new Map(); // chatId -> when we last sent there
  }

  #sentRecently(chatId) {
    const t = this.sentAt.get(chatId);
    return t !== undefined && Date.now() - t < this.ownSendMs;
  }

  async start() {
    this.link.on('request', this.onRequest);
    await this.#build();
  }

  async stop() {
    this.stopped = true;
    clearTimeout(this.watchdog);
    clearTimeout(this.rebuildTimer);
    this.link.off('request', this.onRequest);
    await this.#teardown();
  }

  #state(state) {
    this.link.send({ type: 'state', state });
  }

  async #build() {
    if (this.stopped) return;
    const gen = ++this.generation;
    const client = this.clientFactory();
    this.client = client;
    this.ready = false;
    const current = () => gen === this.generation && !this.stopped;

    client.on('qr', async (qr) => {
      if (!current()) return;
      this.log.warn?.('WhatsApp needs linking: scan the QR (WhatsApp > Linked devices)');
      this.#state('QR');
      this.link.send({ type: 'qr' });
      await this.onQr(qr);
    });
    client.on('ready', async () => {
      if (!current()) return;
      clearTimeout(this.watchdog);
      this.ready = true;
      this.log.info?.('WhatsApp ready');
      this.#state('CONNECTED');
      await this.onReady(client);
    });
    client.on('auth_failure', (m) => {
      if (!current()) return;
      this.log.error?.(`auth failure: ${m}`);
      this.#state('AUTH_FAILURE');
    });
    client.on('change_state', (s) => {
      if (!current()) return;
      this.log.info?.(`WA state ${s}`);
      if (s !== 'CONNECTED' && this.ready) {
        this.ready = false;
        this.#state(String(s));
      } else if (s === 'CONNECTED' && !this.ready) {
        this.ready = true;
        this.#state('CONNECTED');
      }
    });
    client.on('disconnected', (reason) => {
      if (!current()) return;
      this.log.warn?.(`WhatsApp disconnected: ${reason}`);
      this.#rebuildSoon();
    });
    client.on('message', (msg) => {
      if (current()) this.#inbound(msg);
    });
    // The owner sent a message from their phone (or WA Web): core pauses the mesh relay.
    // message_create also fires for our own sends; skip chats we just sent to.
    client.on('message_create', (msg) => {
      if (!current() || !msg?.fromMe) return;
      const chatId = msg.to ?? serializeId(msg.id?.remote);
      if (!chatId || shouldDrop(chatId) || this.#sentRecently(chatId)) return;
      this.link.send({ type: 'own', chatId, timestamp: msg.timestamp ?? null });
    });
    // The owner read the chat on another device (phone, WA Web): core drops what's
    // still queued for the mesh from that chat.
    client.on('unread_count', (chat) => {
      const chatId = serializeId(chat?.id);
      if (current() && chatId && chat.unreadCount === 0 && !shouldDrop(chatId)) {
        this.link.send({ type: 'chatRead', chatId });
      }
    });

    // Watchdog: initialize() can hang forever on a WA Web change. Waiting on
    // a QR scan is not a hang, so the timer pauses on `qr` and re-arms once
    // the scan authenticates.
    const arm = () => {
      clearTimeout(this.watchdog);
      this.watchdog = setTimeout(() => {
        if (current() && !this.ready) {
          this.log.warn?.(`not ready after ${this.readyTimeoutMs} ms, rebuilding client`);
          this.#rebuildSoon(0);
        }
      }, this.readyTimeoutMs);
    };
    client.on('qr', () => clearTimeout(this.watchdog));
    client.on('authenticated', () => current() && !this.ready && arm());
    arm();

    try {
      await client.initialize();
    } catch (e) {
      if (current()) {
        this.log.error?.(`initialize failed: ${e.message}`);
        this.#rebuildSoon();
      }
    }
  }

  #rebuildSoon(delay = this.rebuildDelayMs) {
    this.ready = false;
    this.#state('DISCONNECTED');
    clearTimeout(this.watchdog);
    clearTimeout(this.rebuildTimer);
    this.generation++; // ignore any late events from the old client
    this.rebuildTimer = setTimeout(async () => {
      await this.#teardown();
      await this.#build();
    }, delay);
  }

  async #teardown() {
    const old = this.client;
    this.client = null;
    if (old) {
      try {
        await old.destroy();
      } catch (e) {
        this.log.debug?.(`destroy: ${e.message}`);
      }
    }
    try {
      await this.killChildren();
    } catch (e) {
      this.log.warn?.(`reaping chromium: ${e.message}`);
    }
  }

  async #inbound(msg) {
    try {
      if (msg.fromMe) return;
      const chatId = msg.from;
      if (shouldDrop(chatId)) return;
      const [contact, chat] = await Promise.all([
        msg.getContact().catch(() => null),
        msg.getChat().catch(() => null),
      ]);
      const out = normalize(msg, contact, chat);
      if (!out.isGroup) out.aliases = await this.#aliases(chatId);
      this.link.send(out);
    } catch (e) {
      this.log.warn?.(`dropping inbound message: ${e.message}`);
    }
  }

  // One person can write from their phone id (@c.us) or their @lid. Core
  // matches favourites and mutes against both, so send both along.
  async #aliases(chatId) {
    try {
      const [ids] = await this.client.getContactLidAndPhone([chatId]);
      return [ids?.lid, ids?.pn].filter((id) => id && id !== chatId);
    } catch {
      return [];
    }
  }

  async #handle(req) {
    const reply = (ok, error = null, data = undefined) =>
      this.link.send({ type: 'result', req: req.req, ok, error, ...(data ? { data } : {}) });
    const client = this.client;
    if (!client || !this.ready) return reply(false, 'WA not ready');
    try {
      switch (req.type) {
        case 'send': {
          this.sentAt.set(req.chatId, Date.now()); // before: message_create can beat the promise
          const sent = await client.sendMessage(req.chatId, req.text, { sendSeen: false });
          return reply(true, null, { id: serializeId(sent?.id) });
        }
        case 'markSeen':
          await client.sendSeen(req.chatId);
          return reply(true);
        case 'getContacts': {
          const contacts = await client.getContacts();
          return reply(true, null, contacts.filter((c) => c.name || c.pushname).map(contactSummary));
        }
        default:
          return reply(false, `unknown request ${req.type}`);
      }
    } catch (e) {
      return reply(false, String(e.message || e).slice(0, 80));
    }
  }
}
