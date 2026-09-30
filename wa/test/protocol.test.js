import assert from 'node:assert/strict';
import { test } from 'node:test';
import { LineSplitter, mediaKind, normalize, serializeId, shouldDrop } from '../src/protocol.js';

test('serializeId accepts old and new WA id shapes', () => {
  assert.equal(serializeId({ _serialized: 'a@c.us' }), 'a@c.us');
  assert.equal(serializeId({ $1: 'b@lid' }), 'b@lid');
  assert.equal(serializeId('c@c.us'), 'c@c.us');
  assert.equal(serializeId(undefined), null);
});

test('status, broadcast and channels are dropped', () => {
  assert.ok(shouldDrop('status@broadcast'));
  assert.ok(shouldDrop('123@newsletter'));
  assert.ok(!shouldDrop('27820000001@c.us'));
  assert.ok(!shouldDrop('1203630@g.us')); // core decides on groups
  assert.ok(!shouldDrop('999@lid'));
});

test('media kinds', () => {
  assert.equal(mediaKind('chat'), 'text');
  assert.equal(mediaKind('image'), 'photo');
  assert.equal(mediaKind('ptt'), 'voice');
  assert.equal(mediaKind('video', true), 'gif');
  assert.equal(mediaKind('multi_vcard'), 'contact');
});

test('line splitter handles partial and multiple lines', () => {
  const s = new LineSplitter();
  assert.deepEqual(s.feed('{"a":1}\n{"b"'), ['{"a":1}']);
  assert.deepEqual(s.feed(':2}\n\n{"c":3}\n'), ['{"b":2}', '{"c":3}']);
});

test('normalize a 1:1 text', () => {
  const m = normalize(
    { id: { $1: 'MID' }, from: '278@c.us', type: 'chat', body: 'hi', timestamp: 5 },
    { name: 'Sam Smith', pushname: 'Sammy' },
    { id: { _serialized: '278@c.us' }, isGroup: false },
  );
  assert.deepEqual(m, {
    type: 'message', id: 'MID', chatId: '278@c.us', isGroup: false, chatName: null,
    author: null, senderName: 'Sam Smith', pushname: 'Sammy', kind: 'text', body: 'hi',
    timestamp: 5,
  });
});

test('normalize voice, location, document, group', () => {
  const voice = normalize({ id: 'v', from: 'x@c.us', type: 'ptt', duration: '42' }, null, null);
  assert.equal(voice.kind, 'voice');
  assert.equal(voice.duration, 42);

  const loc = normalize(
    { id: 'l', from: 'x@c.us', type: 'location', location: { latitude: -29.7, longitude: 30.8 } },
    null, null,
  );
  assert.deepEqual([loc.lat, loc.lon], [-29.7, 30.8]);

  const doc = normalize(
    { id: 'd', from: 'x@c.us', type: 'document', body: 'Invoice.pdf',
      _data: { filename: 'Invoice.pdf', caption: 'for May' } },
    null, null,
  );
  assert.equal(doc.filename, 'Invoice.pdf');
  assert.equal(doc.body, 'for May');

  const grp = normalize(
    { id: 'g', from: '1203@g.us', author: '278@c.us', type: 'chat', body: 'x',
      _data: { notifyName: 'Sam' } },
    { pushname: 'Sam' },
    { id: '1203@g.us', isGroup: true, name: 'Farm Crew' },
  );
  assert.equal(grp.isGroup, true);
  assert.equal(grp.chatName, 'Farm Crew');
  assert.equal(grp.author, '278@c.us');
});
