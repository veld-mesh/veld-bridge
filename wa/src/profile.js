// Chromium profile housekeeping.
import fs from 'node:fs';
import path from 'node:path';

const LOCKS = ['SingletonLock', 'SingletonSocket', 'SingletonCookie'];

// A recreated container gets a new hostname, so Chromium reads the previous
// run's lock as "profile in use on another computer" and exits with code 21.
// Only this process uses the profile, and the old Chromium is always killed
// before a new client is built, so any lock left here is stale.
export function clearStaleLocks(profileDir) {
  for (const name of LOCKS) {
    fs.rmSync(path.join(profileDir, name), { force: true });
  }
}
