// NDJSON glue with core/ and message normalisation. Pure: no WhatsApp, no sockets.

export function encode(obj) {
  return JSON.stringify(obj) + '\n';
}

export class LineSplitter {
  #buf = '';

  feed(chunk) {
    this.#buf += chunk;
    const lines = this.#buf.split('\n');
    this.#buf = lines.pop();
    return lines.filter((l) => l.trim() !== '');
  }
}

// WA ids moved from `_serialized` to `$1` in mid-2026; accept both.
export function serializeId(id) {
  if (id == null) return null;
  if (typeof id === 'string') return id;
  return id._serialized ?? id.$1 ?? null;
}

// Never relayed, whatever core's config says.
export function shouldDrop(chatId) {
  return !chatId || chatId === 'status@broadcast' || chatId.endsWith('@broadcast')
    || chatId.endsWith('@newsletter');
}

const KINDS = {
  chat: 'text',
  image: 'photo',
  video: 'video',
  ptt: 'voice',
  audio: 'voice',
  document: 'document',
  location: 'location',
  vcard: 'contact',
  multi_vcard: 'contact',
  sticker: 'sticker',
};

export function mediaKind(type, isGif = false) {
  if (type === 'video' && isGif) return 'gif';
  return KINDS[type] ?? type ?? 'text';
}

// whatsapp-web.js Message (+ its Contact and Chat) -> the object core expects.
// Groups are passed through with isGroup; core decides (allowlist).
export function normalize(msg, contact, chat) {
  const chatId = serializeId(chat?.id) ?? msg.from;
  const kind = mediaKind(msg.type, msg.isGif);
  const out = {
    type: 'message',
    id: serializeId(msg.id),
    chatId,
    isGroup: Boolean(chat?.isGroup) || chatId.endsWith('@g.us'),
    chatName: chat?.isGroup ? (chat.name ?? null) : null,
    author: msg.author ?? null,
    senderName: contact?.name ?? null,              // as saved in the owner's phone
    pushname: contact?.pushname ?? msg._data?.notifyName ?? null,
    kind,
    body: kind === 'document' || kind === 'contact' ? '' : (msg.body ?? ''),
    timestamp: msg.timestamp ?? null,
  };
  if (kind === 'document') {
    out.filename = msg._data?.filename ?? null;
    // Captions on documents arrive in `caption`, body is often the file name.
    out.body = msg._data?.caption ?? '';
  }
  if (kind === 'voice' && msg.duration != null) out.duration = Number(msg.duration);
  if (kind === 'location' && msg.location) {
    out.lat = Number(msg.location.latitude);
    out.lon = Number(msg.location.longitude);
  }
  return out;
}

export function contactSummary(c) {
  return {
    id: serializeId(c.id),
    name: c.name ?? null,
    pushname: c.pushname ?? null,
    isGroup: Boolean(c.isGroup),
  };
}
