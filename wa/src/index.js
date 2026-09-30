// Entry point. Config via environment (systemd EnvironmentFile=/etc/veld-bridge/wa.env).
import { execFile } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { promisify } from 'node:util';
import { Adapter } from './adapter.js';
import { CoreLink } from './core-link.js';
import { clearStaleLocks } from './profile.js';

const env = process.env;
const cfg = {
  socket: env.VB_SOCKET ?? '/run/veld-bridge/wa.sock',
  sessionDir: env.VB_SESSION_DIR ?? '/var/lib/veld-bridge/wa-session',
  chromium: env.VB_CHROMIUM ?? '/usr/bin/chromium',
  qrPng: env.VB_QR_PNG ?? '/var/lib/veld-bridge/qr.png',
  readyTimeoutMs: Number(env.VB_READY_TIMEOUT_MIN ?? 5) * 60_000,
  // Pin WA Web: set to a version found in <session>/web-cache after first link.
  webVersion: env.WA_WEB_VERSION || undefined,
};

const log = {
  line(lvl, msg) {
    process.stdout.write(JSON.stringify({ ts: Date.now() / 1000, lvl, log: 'wa', msg }) + '\n');
  },
  debug(m) { if (env.VB_DEBUG) this.line('DEBUG', m); },
  info(m) { this.line('INFO', m); },
  warn(m) { this.line('WARNING', m); },
  error(m) { this.line('ERROR', m); },
};

// Session holds WhatsApp credentials: 0700, outside the repo, never logged.
fs.mkdirSync(cfg.sessionDir, { recursive: true, mode: 0o700 });
fs.chmodSync(cfg.sessionDir, 0o700);

const { default: wweb } = await import('whatsapp-web.js');
const { Client, LocalAuth } = wweb;

function clientFactory() {
  clearStaleLocks(path.join(cfg.sessionDir, 'session')); // LocalAuth's profile dir
  return new Client({
    authStrategy: new LocalAuth({ dataPath: cfg.sessionDir }),
    webVersion: cfg.webVersion,
    webVersionCache: { type: 'local', path: path.join(cfg.sessionDir, 'web-cache') },
    puppeteer: {
      headless: true,
      executablePath: cfg.chromium,
      args: [
        '--no-sandbox',
        '--disable-dev-shm-usage',
        '--disable-gpu',
        '--no-zygote',
        '--no-first-run',
        '--disable-extensions',
        '--js-flags=--max-old-space-size=256',
      ],
    },
  });
}

const run = promisify(execFile);
async function killChildren() {
  // Chromium sometimes outlives destroy(); anything still parented to us goes.
  try {
    await run('pkill', ['-KILL', '-P', String(process.pid)]);
  } catch {
    // pkill exits 1 when nothing matched
  }
}

async function onQr(qr) {
  const { default: qrcodeTerminal } = await import('qrcode-terminal');
  const { default: QRCode } = await import('qrcode');
  qrcodeTerminal.generate(qr, { small: true }, (art) => process.stdout.write(art + '\n'));
  await QRCode.toFile(cfg.qrPng, qr, { width: 400 });
  log.warn(`QR also written to ${cfg.qrPng}`);
}

async function onReady(client) {
  fs.rmSync(cfg.qrPng, { force: true });
  try {
    log.info(`WA Web version ${await client.getWWebVersion()}`);
  } catch {
    // informational only
  }
}

const link = new CoreLink(cfg.socket, { log });
const adapter = new Adapter({
  clientFactory, link, log, readyTimeoutMs: cfg.readyTimeoutMs, killChildren, onQr, onReady,
});

for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, async () => {
    log.info('shutting down');
    await adapter.stop();
    link.close();
    process.exit(0);
  });
}

link.start();
await adapter.start();
