// This file is the Bandwidth Swap verifier as the kit carries it (kit: dashboard/bsp.js); the two copies must stay
// identical, and tools/check-vectors.mjs fails when they differ. Edit the kit's copy and copy it here.
/* bsp.js: the commitment half of the Bandwidth Swap Protocol (pilot profile 1), checked in the browser.

   This is the second implementation. The reference is bwkit.py in github.com/bandwidthswap/kit
   (canon, leaf_of, node_of, _split, merkle_root, merkle_path, merkle_check, commitment_canon,
   verify_commitment, check_proof); this file reproduces its bytes without sharing its code, so a
   proof that passes both was checked by two readers of the same rules. No dependencies: SHA-256 and
   Ed25519 come from WebCrypto. Where a browser has no Ed25519 in WebCrypto, a pinned copy of
   noble-ed25519 is loaded from jsDelivr, and only then.

   The rules (whitepaper v0.6, Section 4 and Appendix A; the kit's README):
     canonical JSON   keys sorted by code point, separators "," and ":", every non-ASCII character
                      escaped, no whitespace; numbers as Python prints them (0.0, 1e-06, 1e+16)
     leaf             SHA-256(0x00 || canonical JSON of the settlement without its field "t")
     node             SHA-256(0x01 || left || right)
     tree             RFC 6962: split at the largest power of two below n; one leaf's root is that leaf
     path             [["L"|"R", hex], ...] from the leaf up; "L" when the sibling is on the left
     NYM              "ed25519:" + base64url (no padding) of the 32-byte public key
     commitment       Ed25519 by the Mint over "bandwidthswap/commitment/v1\n" + canonical
                      [mint, window, root, count]
     receipt          Ed25519 by the signer over "bandwidthswap/receipt/v1\n" + canonical
                      [payer, hop, hop_index, merkle_root, bytes, t_first, t_last, prev_sig]

   Python writes a float that happens to be whole as "0.0" and JavaScript parses it to 0, the same
   as the integer. The canonical bytes differ, so parse(text) here keeps a note of which numbers
   were written as floats, and canon() uses it. Pass documents to verifyProof as text when you can;
   an already-parsed object is handled with a list of the fields known to be floats. */

const COMMIT_DOMAIN = 'bandwidthswap/commitment/v1\n';
const RECEIPT_DOMAIN = 'bandwidthswap/receipt/v1\n';
const LEAF_PREFIX = 0x00, NODE_PREFIX = 0x01;
const LEAF_SKIP = new Set(['t']);
export const RECEIPT_FIELDS = ['payer', 'hop', 'hop_index', 'merkle_root', 'bytes', 't_first', 't_last', 'prev_sig'];
const WINDOW_S = 3600;
/* Fields whose whole-number values are still floats in the kit's records (used only for a document that arrives parsed). */
const FLOAT_FIELDS = new Set(['amount_usd', 'price_usd_per_gb', 'fee_usd', 'premium_usd', 'refund_usd', 'from_poster_usd', 'from_steward_usd',
  'steward_held_usd', 'steward_fee', 'price', 'fee', 'margin', 'premium', 'refund']);   // the settlement's doubles, spec Section 3, and the announcement's
export const NOBLE_URL = 'https://cdn.jsdelivr.net/npm/@noble/ed25519@2.3.0/index.js';

const enc = new TextEncoder();
const subtle = () => (globalThis.crypto && globalThis.crypto.subtle) || null;

/* ------------------------------------------------------------------ bytes */

export function hex(bytes) {
  let s = '';
  for (const b of bytes) s += (b < 16 ? '0' : '') + b.toString(16);
  return s;
}

export function unhex(s) {
  if (typeof s !== 'string' || s.length % 2 || /[^0-9a-fA-F]/.test(s)) throw new Error('not hex');
  const out = new Uint8Array(s.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(s.substr(2 * i, 2), 16);
  return out;
}

export function b64u(bytes) {
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function unb64u(s) {
  if (typeof s !== 'string' || /[^A-Za-z0-9_-]/.test(s)) throw new Error('not base64url');
  const pad = '='.repeat((4 - (s.length % 4)) % 4);
  let bin;
  try { bin = atob(s.replace(/-/g, '+').replace(/_/g, '/') + pad); } catch (e) { throw new Error('not base64url'); }
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < out.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function concat(...parts) {
  let n = 0;
  for (const p of parts) n += p.length;
  const out = new Uint8Array(n);
  let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
}

export async function sha256(bytes) {
  return new Uint8Array(await subtle().digest('SHA-256', bytes));
}

/* ------------------------------------------------------------- parse JSON */

/* Which numbers in a container were written as floats: container -> Set of keys or indices. */
const floats = new WeakMap();

/* Strict JSON (RFC 8259), remembering float lexemes. Integers beyond 2^53 become BigInt. */
export function parse(text) {
  if (typeof text !== 'string') throw new Error('parse takes a string');
  let i = 0;
  const n = text.length;
  const fail = (what) => { throw new Error('not JSON: ' + what + ' at character ' + i); };
  const ws = () => { while (i < n && (text[i] === ' ' || text[i] === '\n' || text[i] === '\r' || text[i] === '\t')) i++; };
  function value(container, key) {
    ws();
    if (i >= n) fail('unexpected end');
    const c = text[i];
    if (c === '{') return object();
    if (c === '[') return array();
    if (c === '"') return string();
    if (c === 't' && text.startsWith('true', i)) { i += 4; return true; }
    if (c === 'f' && text.startsWith('false', i)) { i += 5; return false; }
    if (c === 'n' && text.startsWith('null', i)) { i += 4; return null; }
    if (c === '-' || (c >= '0' && c <= '9')) return number(container, key);
    fail('unexpected "' + c + '"');
  }
  function number(container, key) {
    const m = /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/.exec(text.slice(i, i + 400));
    if (!m) fail('bad number');
    const lex = m[0];
    i += lex.length;
    if (/[.eE]/.test(lex)) {
      if (container) (floats.get(container) || floats.set(container, new Set()).get(container)).add(key);
      return Number(lex);
    }
    const v = Number(lex);
    return Number.isSafeInteger(v) ? v : BigInt(lex);
  }
  function string() {
    i++;
    let out = '';
    for (;;) {
      if (i >= n) fail('unterminated string');
      const c = text[i++];
      if (c === '"') return out;
      if (c === '\\') {
        const e = text[i++];
        if (e === '"' || e === '\\' || e === '/') out += e;
        else if (e === 'n') out += '\n';
        else if (e === 't') out += '\t';
        else if (e === 'r') out += '\r';
        else if (e === 'b') out += '\b';
        else if (e === 'f') out += '\f';
        else if (e === 'u') {
          const h = text.substr(i, 4);
          if (!/^[0-9a-fA-F]{4}$/.test(h)) fail('bad \\u escape');
          out += String.fromCharCode(parseInt(h, 16));
          i += 4;
        } else fail('bad escape');
      } else if (c < ' ') fail('control character in string');
      else out += c;
    }
  }
  function array() {
    i++;
    const out = [];
    ws();
    if (text[i] === ']') { i++; return out; }
    for (;;) {
      out.push(value(out, out.length));
      ws();
      if (text[i] === ',') { i++; continue; }
      if (text[i] === ']') { i++; return out; }
      fail('expected "," or "]"');
    }
  }
  function object() {
    i++;
    const out = {};
    ws();
    if (text[i] === '}') { i++; return out; }
    for (;;) {
      ws();
      if (text[i] !== '"') fail('expected a key');
      const k = string();
      ws();
      if (text[i] !== ':') fail('expected ":"');
      i++;
      out[k] = value(out, k);
      ws();
      if (text[i] === ',') { i++; continue; }
      if (text[i] === '}') { i++; return out; }
      fail('expected "," or "}"');
    }
  }
  const v = value(null, null);
  ws();
  if (i < n) fail('trailing characters');
  return v;
}

/* --------------------------------------------------------- canonical JSON */

/* A float as Python's repr writes it: the shortest digits that round-trip, exponent form below 1e-4
   and from 1e16, two-digit signed exponents, and ".0" on a whole number. */
export function pyFloat(x) {
  if (typeof x !== 'number' || !Number.isFinite(x)) throw new Error('not a finite number');
  if (x === 0) return Object.is(x, -0) ? '-0.0' : '0.0';
  const s = String(Math.abs(x));                 // shortest round-trip digits, by the language spec
  const [mant, ex] = s.split('e');
  const e = ex ? parseInt(ex, 10) : 0;
  const dot = mant.indexOf('.');
  const intPart = dot < 0 ? mant : mant.slice(0, dot), frac = dot < 0 ? '' : mant.slice(dot + 1);
  let digits = intPart + frac;
  const lead = digits.match(/^0*/)[0].length;
  digits = digits.slice(lead).replace(/0+$/, '') || '0';
  const decpt = intPart.length - lead + e;       // value = 0.digits × 10^decpt
  let out;
  if (decpt <= -4 || decpt > 16) {
    const ee = decpt - 1;
    out = digits[0] + (digits.length > 1 ? '.' + digits.slice(1) : '') + 'e' + (ee < 0 ? '-' : '+') + String(Math.abs(ee)).padStart(2, '0');
  } else if (decpt <= 0) {
    out = '0.' + '0'.repeat(-decpt) + digits;
  } else if (decpt >= digits.length) {
    out = digits + '0'.repeat(decpt - digits.length) + '.0';
  } else {
    out = digits.slice(0, decpt) + '.' + digits.slice(decpt);
  }
  return (x < 0 ? '-' : '') + out;
}

/* A string as Python's json.dumps(ensure_ascii=True) writes it. */
export function pyString(s) {
  let out = '"';
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c === 0x22) out += '\\"';
    else if (c === 0x5c) out += '\\\\';
    else if (c === 0x0a) out += '\\n';
    else if (c === 0x0d) out += '\\r';
    else if (c === 0x09) out += '\\t';
    else if (c === 0x08) out += '\\b';
    else if (c === 0x0c) out += '\\f';
    else if (c >= 0x20 && c <= 0x7e) out += s[i];
    else out += '\\u' + c.toString(16).padStart(4, '0');
  }
  return out + '"';
}

function byCodePoint(a, b) {
  const A = Array.from(a, (ch) => ch.codePointAt(0)), B = Array.from(b, (ch) => ch.codePointAt(0));
  for (let i = 0; i < Math.min(A.length, B.length); i++) if (A[i] !== B[i]) return A[i] - B[i];
  return A.length - B.length;
}

function isFloatIn(container, key, v) {
  const marks = container && floats.get(container);
  if (marks) return marks.has(key);
  return !Number.isInteger(v) || FLOAT_FIELDS.has(key);
}

/* Canonical JSON of any value: what the kit's json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True) writes. */
export function canon(v, container = null, key = null) {
  if (v === null) return 'null';
  if (v === true) return 'true';
  if (v === false) return 'false';
  if (typeof v === 'bigint') return v.toString();
  if (typeof v === 'number') return isFloatIn(container, key, v) ? pyFloat(v) : String(v);
  if (typeof v === 'string') return pyString(v);
  if (Array.isArray(v)) return '[' + v.map((x, i) => canon(x, v, i)).join(',') + ']';
  if (typeof v === 'object') {
    return '{' + Object.keys(v).sort(byCodePoint).map((k) => pyString(k) + ':' + canon(v[k], v, k)).join(',') + '}';
  }
  throw new Error('cannot serialise a ' + typeof v);
}

/* Mark which keys of an object hold floats, for a value built in code rather than parsed. */
export function markFloats(container, keys) {
  floats.set(container, new Set(keys));
  return container;
}

/* ------------------------------------------------------------ identities */

export function nymOfPub(pub) {
  if (!(pub instanceof Uint8Array) || pub.length !== 32) throw new Error('a public key is 32 bytes');
  return 'ed25519:' + b64u(pub);
}

export function pubOfNym(nym) {
  if (typeof nym !== 'string' || !nym.startsWith('ed25519:')) throw new Error('not an ed25519 NYM');
  const raw = unb64u(nym.slice('ed25519:'.length));
  if (raw.length !== 32) throw new Error('NYM is not 32 bytes');
  return raw;
}

/* ---------------------------------------------------------------- Ed25519 */

let backend = null;   // 'webcrypto' | 'noble', once decided
let noble = null;

async function decideBackend() {
  if (backend) return backend;
  try {
    const key = await subtle().importKey('raw', new Uint8Array(32), { name: 'Ed25519' }, false, ['verify']);
    if (key) backend = 'webcrypto';
  } catch (e) { /* no Ed25519 in this WebCrypto */ }
  if (!backend) {
    noble = await import(NOBLE_URL);
    backend = 'noble';
  }
  return backend;
}

/* Which Ed25519 is in use: "webcrypto", "noble", or null before the first verification. */
export function ed25519Backend() { return backend; }

/* Choose the Ed25519 implementation instead of letting the first verification decide: "webcrypto" or
   "noble" (loaded from jsDelivr, or passed in as `module` where a runtime cannot import a URL).
   For tests, and for a page that wants to show the fallback works. */
export async function useEd25519(name, module = null) {
  if (name === 'noble') { noble = module || noble || await import(NOBLE_URL); backend = 'noble'; }
  else if (name === 'webcrypto') backend = 'webcrypto';
  else throw new Error('Ed25519 is "webcrypto" or "noble"');
  return backend;
}

/* True when sig (64 bytes) is pub's (32 bytes) Ed25519 signature over msg. False, never a throw, for anything malformed. */
export async function verifySignature(pub, msg, sig) {
  try {
    if (!(pub instanceof Uint8Array) || pub.length !== 32 || !(sig instanceof Uint8Array) || sig.length !== 64) return false;
    if ((await decideBackend()) === 'webcrypto') {
      const key = await subtle().importKey('raw', pub, { name: 'Ed25519' }, false, ['verify']);
      return !!(await subtle().verify({ name: 'Ed25519' }, key, sig, msg));
    }
    return !!(await noble.verifyAsync(sig, msg, pub));
  } catch (e) {
    return false;
  }
}

/* ----------------------------------------------------------------- Merkle */

/* The leaf of a settlement: SHA-256 over 0x00 and the canonical JSON of the entry without "t". */
export async function leafOf(settlement) {
  const body = {};
  const marks = floats.get(settlement);
  for (const k of Object.keys(settlement)) if (!LEAF_SKIP.has(k)) body[k] = settlement[k];
  if (marks) floats.set(body, marks);
  return sha256(concat(Uint8Array.of(LEAF_PREFIX), enc.encode(canon(body))));
}

export async function nodeOf(left, right) {
  return sha256(concat(Uint8Array.of(NODE_PREFIX), left, right));
}

/* RFC 6962's split: the largest power of two below n. */
export function split(n) {
  let k = 1;
  while (k * 2 < n) k *= 2;
  return k;
}

/* The root over leaves (Uint8Arrays) in the order given; one leaf's root is that leaf. */
export async function merkleRoot(leaves) {
  if (!leaves.length) throw new Error('no leaves');
  if (leaves.length === 1) return leaves[0];
  const k = split(leaves.length);
  return nodeOf(await merkleRoot(leaves.slice(0, k)), await merkleRoot(leaves.slice(k)));
}

/* The siblings from leaf i up to the root, [[side, hex], ...]: "L" when the sibling is on the left. */
export async function merklePath(leaves, i) {
  if (leaves.length === 1) return [];
  const k = split(leaves.length);
  if (i < k) return [...await merklePath(leaves.slice(0, k), i), ['R', hex(await merkleRoot(leaves.slice(k)))]];
  return [...await merklePath(leaves.slice(k), i - k), ['L', hex(await merkleRoot(leaves.slice(0, k)))]];
}

/* True when leaf (bytes) climbs this path to the root (hex). False for a malformed path. */
export async function merkleCheck(leaf, path, rootHex) {
  let h = leaf;
  try {
    if (!Array.isArray(path)) return false;
    for (const step of path) {
      if (!Array.isArray(step) || step.length !== 2) return false;
      const [side, sib] = step;
      const s = unhex(sib);
      if (s.length !== 32) return false;
      if (side === 'L') h = await nodeOf(s, h);
      else if (side === 'R') h = await nodeOf(h, s);
      else return false;
    }
  } catch (e) {
    return false;
  }
  return hex(h) === String(rootHex).toLowerCase();
}

/* Leaves go by window start, then the rest of the window, so every party builds the same tree. */
export function settlementOrder(a, b) {
  for (const f of ['t_first', 't_last', 'payer', 'hop', 'hop_index', 'receipt_sig']) {
    const x = a[f], y = b[f];
    if (x === y) continue;
    if (typeof x === 'string' && typeof y === 'string') return byCodePoint(x, y);
    return x < y ? -1 : 1;
  }
  return 0;
}

/* An hour's settlements in leaf order, their leaves and their root, as the Mint builds them. */
export async function hourTree(settlements) {
  const ordered = [...settlements].sort(settlementOrder);
  const leaves = [];
  for (const s of ordered) leaves.push(await leafOf(s));
  return { settlements: ordered, leaves, root: hex(await merkleRoot(leaves)) };
}

/* The hour a settlement is committed under: the Unix hour its window started in. */
export function commitHour(settlement) {
  const t = settlement.t_first;
  return t - (t % WINDOW_S);
}

/* ------------------------------------------------------------- commitment */

/* The signed bytes of a commitment: the domain, then the canonical [mint, window, root, count]. */
export function commitmentCanon(mint, window, root, count, marks) {
  const arr = [mint, window, root, count];
  if (marks) floats.set(arr, new Set([marks.has('window') ? 1 : -1, marks.has('count') ? 3 : -1].filter((i) => i >= 0)));
  else floats.set(arr, new Set());
  return enc.encode(COMMIT_DOMAIN + canon(arr));
}

/* Check a commitment {mint, window, root, count, signature}. Returns {ok, reason, mint, window, root, count}.
   opts.mint, when given, is the NYM it must be signed by; a mismatch is reported in `expected` and fails. */
export async function verifyCommitment(c, opts = {}) {
  const out = { ok: false, reason: null, mint: null, window: null, root: null, count: null, expected: null };
  if (!c || typeof c !== 'object' || Array.isArray(c)) { out.reason = 'the commitment is not an object'; return out; }
  out.mint = c.mint; out.window = c.window; out.root = c.root; out.count = c.count;
  let pub;
  try { pub = pubOfNym(c.mint); } catch (e) { out.reason = 'the Mint\'s NYM is malformed: ' + e.message; return out; }
  if (!Number.isInteger(c.window) || c.window < 0) { out.reason = 'the window is not a Unix hour'; return out; }
  if (typeof c.root !== 'string' || !/^[0-9a-f]{64}$/.test(c.root)) { out.reason = 'the root is not 64 hex characters'; return out; }
  if (!Number.isInteger(c.count) || c.count < 0) { out.reason = 'the count is not a whole number'; return out; }
  let sig;
  try { sig = unb64u(c.signature); } catch (e) { out.reason = 'the signature is not base64url'; return out; }
  if (sig.length !== 64) { out.reason = 'the signature is not 64 bytes'; return out; }
  if (opts.mint) {
    out.expected = { nym: opts.mint, matches: opts.mint === c.mint };
  }
  const signed = await verifySignature(pub, commitmentCanon(c.mint, c.window, c.root, c.count, floats.get(c)), sig);
  if (!signed) { out.reason = 'the signature does not verify: it is not ' + c.mint + '\'s signature over this hour, root and count'; return out; }
  if (out.expected && !out.expected.matches) { out.reason = 'the commitment is signed by ' + c.mint + ', not the Mint you expected, ' + opts.mint; return out; }
  out.ok = true;
  return out;
}

/* Check a proof document from `bwkit prove` with nothing but what it carries, plus opts.mint when the
   commitment must be a particular Mint's. doc may be the document's text (preferred: the float
   lexemes are then exact) or a parsed object. Returns
     { ok, reason, commitment: <verifyCommitment's result>, count, shown,
       settlements: [{ index, ok, reason, leaf, statedLeaf, leafMatches, settlement }] }
   where every settlement's ok is its own inclusion in the root, and ok at the top is all of it. */
export async function verifyProof(doc, opts = {}) {
  const out = { ok: false, reason: null, commitment: null, count: null, shown: 0, settlements: [] };
  if (typeof doc === 'string') {
    try { doc = parse(doc); } catch (e) { out.reason = e.message; return out; }
  }
  if (!doc || typeof doc !== 'object' || Array.isArray(doc)) { out.reason = 'the document is not a JSON object'; return out; }
  if (doc.kind !== 'proof') { out.reason = 'not a proof document (kind is ' + JSON.stringify(doc.kind === undefined ? null : doc.kind) + ', not "proof")'; return out; }
  if (!Array.isArray(doc.settlements)) { out.reason = 'settlements must be a list'; return out; }
  const c = { mint: doc.mint, window: doc.window, root: doc.root, count: doc.count, signature: doc.signature };
  if (floats.get(doc)) floats.set(c, floats.get(doc));
  out.commitment = await verifyCommitment(c, opts);
  out.count = doc.count;
  out.shown = doc.settlements.length;
  let all = out.commitment.ok;
  for (let n = 0; n < doc.settlements.length; n++) {
    const item = doc.settlements[n];
    const r = { index: null, ok: false, reason: null, leaf: null, statedLeaf: null, leafMatches: null, settlement: null };
    out.settlements.push(r);
    try {
      if (!item || typeof item !== 'object') throw new Error('entry ' + n + ' is not an object');
      r.index = item.index;
      r.statedLeaf = typeof item.leaf === 'string' ? item.leaf : null;
      const s = item.settlement;
      r.settlement = s;
      if (!s || typeof s !== 'object' || Array.isArray(s)) throw new Error('entry ' + n + ' has no settlement');
      if (s.kind !== 'settlement') throw new Error('not a settlement');
      if (!Number.isInteger(s.t_first)) throw new Error('t_first is not a Unix second');
      const h = commitHour(s);
      if (h !== doc.window) throw new Error('belongs to the hour ' + h + ', not ' + doc.window);
      if (!Number.isInteger(item.index) || item.index < 0 || !(item.index < doc.count)) {
        throw new Error('index ' + JSON.stringify(item.index === undefined ? null : item.index) + ' is outside the commitment\'s ' + doc.count + ' leaves');
      }
      const leaf = await leafOf(s);
      r.leaf = hex(leaf);
      r.leafMatches = r.statedLeaf === null ? null : r.statedLeaf === r.leaf;
      if (!(await merkleCheck(leaf, item.path, doc.root))) throw new Error('not in the commitment: its leaf does not reach the root');
      r.ok = true;
    } catch (e) {
      r.reason = e.message;
      all = false;
    }
  }
  out.ok = all;
  if (!all && !out.reason) {
    out.reason = !out.commitment.ok ? out.commitment.reason
      : out.settlements.filter((r) => !r.ok).length + ' of ' + out.settlements.length + ' settlements fail';
  }
  return out;
}

/* ---------------------------------------------------------------- receipt */

/* The signed bytes of a receipt or an ack: the domain, then the Figure 2 tuple as a JSON array in field order. */
export function receiptCanon(receipt) {
  const arr = RECEIPT_FIELDS.map((f) => receipt[f]);
  const marks = floats.get(receipt);
  floats.set(arr, new Set(marks ? RECEIPT_FIELDS.map((f, i) => (marks.has(f) ? i : -1)).filter((i) => i >= 0) : []));
  return enc.encode(RECEIPT_DOMAIN + canon(arr));
}

/* True when sig is nym's signature over the receipt. */
export async function verifyReceipt(nym, receipt, sig) {
  try {
    return await verifySignature(pubOfNym(nym), receiptCanon(receipt), unb64u(sig));
  } catch (e) {
    return false;
  }
}

export const version = '0.1.0';
