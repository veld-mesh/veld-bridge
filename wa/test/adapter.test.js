// Adapter + CoreLink against a fake core (Unix socket) and a fake WA Client.
import assert from 'node:assert/strict';
import { EventEmitter, once } from 'node:events';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, test } from 'node:test';
import { Adapter } from '../src/adapter.js';
import { CoreLink } from '../src/core-link.js';
import { encode, LineSplitter } from '../src/protocol.js';

const quiet = { info() {}, warn() {}, error() {}, debug() {} };
const tick = (ms = 20) => new Promise((r) => setTimeout(r, ms));

class FakeClient extends EventEmitter {
  constructor() {
    super();
    this.sent = [];
    this.seen = [];
    this.destroyed = false;
    this.hang = false;
  }
  async initialize() {
    if (!this.hang) setImmediate(() => this.emit('ready'));
  }
  async sendMessage(chatId, text, opts) {
    this.sent.push({ chatId, text, opts });
    return { id: { $1: 'OUT1' } };
  }
  async sendSeen(chatId) { this.seen.push(chatId); }
  async getContactLidAndPhone(ids) {
    return ids.map((id) => (id === '9@lid' ? { lid: '9@lid', pn: '1@c.us' } : { lid: null, pn: id }));
  }
  async getContacts() {
    return [{ id: { _serialized: '1@c.us' }, name: 'Sam', pushname: null }, { id: '2@c.us' }];
  }
  async destroy() { this.destroyed = true; }
}

class FakeCore {
  constructor(p) {
    this.path = p;
    this.got = [];
    this.sock = null;
    this.server = net.createServer((s) => {
      this.sock = s;
      const lines = new LineSplitter();
      s.setEncoding('utf8');
      s.on('data', (c) => lines.feed(c).forEach((l) => this.got.push(JSON.parse(l))));
    });
  }
  listen() { this.server.listen(this.path); return once(this.server, 'listening'); }
  send(obj) { this.sock.write(encode(obj)); }
  of(type) { return this.got.filter((m) => m.type === type); }
  async close() {
    this.sock?.destroy();
    await new Promise((r) => this.server.close(r));
  }
}

let dir, core, link, clients, adapter, killed;

function makeAdapter(opts = {}) {
  clients = [];
  killed = 0;
  adapter = new Adapter({
    clientFactory: () => {
      const c = new FakeClient();
      if (opts.hangFirst && clients.length === 0) c.hang = true;
      clients.push(c);
      return c;
    },
    link, log: quiet, rebuildDelayMs: 10, readyTimeoutMs: opts.readyTimeoutMs ?? 10_000,
    killChildren: async () => { killed++; },
    voiceDir: opts.voiceDir ?? null,
  });
  return adapter;
}

beforeEach(async () => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vbwa-'));
  core = new FakeCore(path.join(dir, 'wa.sock'));
  await core.listen();
  link = new CoreLink(core.path, { log: quiet, minDelayMs: 10, maxDelayMs: 20 });
  link.start();
  await once(link, 'connect');
});

afterEach(async () => {
  await adapter?.stop();
  link.close();
  await core.close();
});

test('ready -> CONNECTED state reaches core', async () => {
  await makeAdapter().start();
  await tick();
  assert.deepEqual(core.of('state').at(-1), { type: 'state', state: 'CONNECTED' });
});

test('inbound message is normalised; status/fromMe dropped', async () => {
  await makeAdapter().start();
  await tick();
  const base = { getContact: async () => ({ name: 'Sam' }), getChat: async () => null };
  clients[0].emit('message', { ...base, id: { $1: 'A' }, from: '1@c.us', type: 'chat', body: 'hi' });
  clients[0].emit('message', { ...base, id: 'B', from: 'status@broadcast', type: 'chat' });
  clients[0].emit('message', { ...base, id: 'C', from: '1@c.us', fromMe: true, type: 'chat' });
  await tick();
  const msgs = core.of('message');
  assert.equal(msgs.length, 1);
  assert.equal(msgs[0].id, 'A');
  assert.equal(msgs[0].senderName, 'Sam');
});

test('voice note is saved for core to transcribe; a failed download still relays it', async () => {
  const voiceDir = fs.mkdtempSync(path.join(os.tmpdir(), 'vbvoice-'));
  await makeAdapter({ voiceDir }).start();
  await tick();
  const base = { getContact: async () => ({ name: 'Sam' }), getChat: async () => null,
    from: '1@c.us', type: 'ptt', duration: '7' };
  clients[0].emit('message', { ...base, id: { $1: 'false_1@c.us_V1' },
    downloadMedia: async () => ({ mimetype: 'audio/ogg', data: Buffer.from('OggS-audio').toString('base64') }) });
  clients[0].emit('message', { ...base, id: 'V2', downloadMedia: async () => { throw new Error('gone'); } });
  await tick(50);
  // Saving the file takes longer than the failed download, so don't rely on order.
  const byId = Object.fromEntries(core.of('message').map((m) => [m.id, m]));
  const ok = byId['false_1@c.us_V1'];
  const failed = byId.V2;
  assert.equal(ok.kind, 'voice');
  assert.equal(ok.duration, 7);
  assert.equal(path.dirname(ok.audioPath), voiceDir);
  assert.match(path.basename(ok.audioPath), /^false_1_c_us_V1\.ogg$/);
  assert.equal(fs.readFileSync(ok.audioPath, 'utf8'), 'OggS-audio');
  assert.equal(failed.id, 'V2');
  assert.equal(failed.audioPath, undefined);
});

test('chat read on another device -> chatRead to core, only at zero unread', async () => {
  await makeAdapter().start();
  await tick();
  clients[0].emit('unread_count', { id: { _serialized: '1@c.us' }, unreadCount: 2 });
  clients[0].emit('unread_count', { id: { _serialized: '1@c.us' }, unreadCount: 0 });
  clients[0].emit('unread_count', { id: 'status@broadcast', unreadCount: 0 });
  await tick();
  assert.deepEqual(core.of('chatRead'), [{ type: 'chatRead', chatId: '1@c.us' }]);
});

test('message sent from the phone -> own to core; our own sends and others are not', async () => {
  await makeAdapter().start();
  await tick();
  clients[0].emit('message_create', { fromMe: true, to: '1@c.us', timestamp: 1790000000 });
  clients[0].emit('message_create', { fromMe: false, from: '2@c.us', to: 'me@c.us' });
  clients[0].emit('message_create', { fromMe: true, to: 'status@broadcast' });
  core.send({ type: 'send', req: 3, chatId: '3@c.us', text: 'from the mesh' });
  await tick();
  clients[0].emit('message_create', { fromMe: true, to: '3@c.us' }); // echo of that send
  await tick();
  assert.deepEqual(core.of('own'), [{ type: 'own', chatId: '1@c.us', timestamp: 1790000000 }]);
});

test('send uses sendSeen:false; markSeen only on request; results carry req', async () => {
  await makeAdapter().start();
  await tick();
  core.send({ type: 'send', req: 7, chatId: '1@c.us', text: 'yo' });
  await tick();
  assert.deepEqual(clients[0].sent, [{ chatId: '1@c.us', text: 'yo', opts: { sendSeen: false } }]);
  assert.deepEqual(clients[0].seen, []);
  assert.deepEqual(core.of('result').at(-1), {
    type: 'result', req: 7, ok: true, error: null, data: { id: 'OUT1' },
  });
  core.send({ type: 'markSeen', req: 8, chatId: '1@c.us' });
  core.send({ type: 'getContacts', req: 9 });
  await tick();
  assert.deepEqual(clients[0].seen, ['1@c.us']);
  const contacts = core.of('result').find((r) => r.req === 9).data;
  assert.deepEqual(contacts, [{ id: '1@c.us', name: 'Sam', pushname: null, isGroup: false }]);
});

test('requests before ready fail fast', async () => {
  await makeAdapter({ hangFirst: true }).start();
  core.send({ type: 'send', req: 1, chatId: '1@c.us', text: 'x' });
  await tick();
  assert.deepEqual(core.of('result')[0], { type: 'result', req: 1, ok: false, error: 'WA not ready' });
});

test('disconnected -> new Client, old one destroyed, chromium reaped', async () => {
  await makeAdapter().start();
  await tick();
  clients[0].emit('disconnected', 'NAVIGATION');
  await tick(60);
  assert.equal(clients.length, 2);
  assert.ok(clients[0].destroyed);
  assert.ok(killed >= 1);
  const states = core.of('state').map((s) => s.state);
  assert.deepEqual(states.slice(-2), ['DISCONNECTED', 'CONNECTED']);
  // late events from the dead client are ignored
  clients[0].emit('ready');
  clients[0].emit('message', { from: '1@c.us' });
  await tick();
  assert.equal(core.of('message').length, 0);
});

test('watchdog rebuilds a client that never becomes ready', async () => {
  await makeAdapter({ hangFirst: true, readyTimeoutMs: 30 }).start();
  await tick(120);
  assert.equal(clients.length, 2);
  assert.equal(core.of('state').at(-1).state, 'CONNECTED');
});

test('messages survive a core restart', async () => {
  await makeAdapter().start();
  await tick();
  await core.close();
  await tick(30);
  const base = { getContact: async () => null, getChat: async () => null };
  clients[0].emit('message', { ...base, id: 'Q', from: '1@c.us', type: 'chat', body: 'while away' });
  await tick();
  core = new FakeCore(core.path);
  fs.rmSync(core.path, { force: true });
  await core.listen();
  await tick(100);
  assert.equal(core.of('message')[0]?.id, 'Q');
  assert.equal(core.of('state').at(-1).state, 'CONNECTED'); // state replayed on reconnect
});

test('1:1 messages carry the other WA id (lid <-> phone) as aliases', async () => {
  await makeAdapter().start();
  await tick();
  const base = { getContact: async () => ({ name: 'Thabo' }), getChat: async () => null };
  clients[0].emit('message', { ...base, id: 'L1', from: '9@lid', type: 'chat', body: 'yo' });
  clients[0].emit('message', { ...base, id: 'P1', from: '1@c.us', type: 'chat', body: 'yo' });
  await tick();
  const [lid, pn] = core.of('message');
  assert.deepEqual(lid.aliases, ['1@c.us']);
  assert.deepEqual(pn.aliases, []);
});
