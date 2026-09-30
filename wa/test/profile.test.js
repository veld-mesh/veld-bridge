import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { clearStaleLocks } from '../src/profile.js';

test('clearStaleLocks removes Chromium locks and keeps the session', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'kk-profile-'));
  for (const f of ['SingletonLock', 'SingletonSocket', 'SingletonCookie', 'Cookies']) {
    fs.writeFileSync(path.join(dir, f), 'x');
  }
  clearStaleLocks(dir);
  assert.deepEqual(fs.readdirSync(dir), ['Cookies']);
  clearStaleLocks(dir); // nothing left to remove is fine
  clearStaleLocks(path.join(dir, 'missing'));
  fs.rmSync(dir, { recursive: true });
});
