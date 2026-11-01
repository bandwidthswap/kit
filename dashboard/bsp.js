// This file is the Bandwidth Swap verifier as the kit carries it (kit: dashboard/bsp.js); the two copies must stay
// identical, and tools/check-vectors.mjs fails when they differ. Edit the kit's copy and copy it here.
/* bsp.js: the commitment half of the Bandwidth Swap Protocol (pilot profile 1), checked in the browser.

   This is the second implementation. The reference is bwkit.py in github.com/bandwidthswap/kit
   (canon, leaf_of, node_of, _split, merkle_root, merkle_path, merkle_check, commitment_canon,
   verify_commitment, check_proof); this file reproduces its bytes without sharing its code, so a
   proof that passes both was checked by two readers of the same rules. No dependencies and nothing
   loaded from anywhere: SHA-256 and Ed25519 come from WebCrypto where the page has it; a plain-http
   page at any address but localhost is not a secure context and has no crypto.subtle at all, and a
   browser may have WebCrypto without Ed25519, so this file carries its own SHA-256 and SHA-512 and a
   byte-for-byte copy of noble-ed25519 2.3.0 (MIT, at the end of the file), used only when they are
   needed; backends() says which ran. A node's page may be opened on a tailnet with no internet, and
   a saved snapshot inlines this file, so it depends on nothing outside itself.

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
/* The embedded Ed25519 fallback: @noble/ed25519 2.3.0's index.js as published on npm (and byte-identical on jsDelivr),
   its SHA-256 recorded here so the copy at the end of this file can be checked against the package. */
export const NOBLE_VERSION = '2.3.0';
export const NOBLE_SHA256 = '70cebecaaa6a126ced702198706c49d03f2d6f70317342b75e50e8d2d4951fe5';

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

/* ----------------------------------------------------------------- hashes */

let hashes = null;   // 'webcrypto' | 'js', once decided

function decideHashes() {
  if (!hashes) hashes = subtle() ? 'webcrypto' : 'js';
  return hashes;
}

/* Choose the hashes instead of letting the first digest decide: "webcrypto" or "js" (this file's own SHA-256 and
   SHA-512). For tests, and for a page that wants to show the fallback works. */
export function useHashes(name) {
  if (name === 'js') hashes = 'js';
  else if (name === 'webcrypto') { if (!subtle()) throw new Error('this runtime has no WebCrypto'); hashes = 'webcrypto'; }
  else throw new Error('hashes are "webcrypto" or "js"');
  return hashes;
}

export async function sha256(bytes) {
  if (decideHashes() === 'webcrypto') return new Uint8Array(await subtle().digest('SHA-256', bytes));
  return sha256js(bytes);
}

export async function sha512(bytes) {
  if (decideHashes() === 'webcrypto') return new Uint8Array(await subtle().digest('SHA-512', bytes));
  return sha512js(bytes);
}
const K256 = [
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];
const H256 = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
const K512 = [
  0x428a2f98d728ae22n, 0x7137449123ef65cdn, 0xb5c0fbcfec4d3b2fn, 0xe9b5dba58189dbbcn,
  0x3956c25bf348b538n, 0x59f111f1b605d019n, 0x923f82a4af194f9bn, 0xab1c5ed5da6d8118n,
  0xd807aa98a3030242n, 0x12835b0145706fben, 0x243185be4ee4b28cn, 0x550c7dc3d5ffb4e2n,
  0x72be5d74f27b896fn, 0x80deb1fe3b1696b1n, 0x9bdc06a725c71235n, 0xc19bf174cf692694n,
  0xe49b69c19ef14ad2n, 0xefbe4786384f25e3n, 0x0fc19dc68b8cd5b5n, 0x240ca1cc77ac9c65n,
  0x2de92c6f592b0275n, 0x4a7484aa6ea6e483n, 0x5cb0a9dcbd41fbd4n, 0x76f988da831153b5n,
  0x983e5152ee66dfabn, 0xa831c66d2db43210n, 0xb00327c898fb213fn, 0xbf597fc7beef0ee4n,
  0xc6e00bf33da88fc2n, 0xd5a79147930aa725n, 0x06ca6351e003826fn, 0x142929670a0e6e70n,
  0x27b70a8546d22ffcn, 0x2e1b21385c26c926n, 0x4d2c6dfc5ac42aedn, 0x53380d139d95b3dfn,
  0x650a73548baf63den, 0x766a0abb3c77b2a8n, 0x81c2c92e47edaee6n, 0x92722c851482353bn,
  0xa2bfe8a14cf10364n, 0xa81a664bbc423001n, 0xc24b8b70d0f89791n, 0xc76c51a30654be30n,
  0xd192e819d6ef5218n, 0xd69906245565a910n, 0xf40e35855771202an, 0x106aa07032bbd1b8n,
  0x19a4c116b8d2d0c8n, 0x1e376c085141ab53n, 0x2748774cdf8eeb99n, 0x34b0bcb5e19b48a8n,
  0x391c0cb3c5c95a63n, 0x4ed8aa4ae3418acbn, 0x5b9cca4f7763e373n, 0x682e6ff3d6b2b8a3n,
  0x748f82ee5defb2fcn, 0x78a5636f43172f60n, 0x84c87814a1f0ab72n, 0x8cc702081a6439ecn,
  0x90befffa23631e28n, 0xa4506cebde82bde9n, 0xbef9a3f7b2c67915n, 0xc67178f2e372532bn,
  0xca273eceea26619cn, 0xd186b8c721c0c207n, 0xeada7dd6cde0eb1en, 0xf57d4f7fee6ed178n,
  0x06f067aa72176fban, 0x0a637dc5a2c898a6n, 0x113f9804bef90daen, 0x1b710b35131c471bn,
  0x28db77f523047d84n, 0x32caab7b40c72493n, 0x3c9ebe0a15c9bebcn, 0x431d67c49c100d4cn,
  0x4cc5d4becb3e42b6n, 0x597f299cfc657e2an, 0x5fcb6fab3ad6faecn, 0x6c44198c4a475817n,
];
const H512 = [
  0x6a09e667f3bcc908n, 0xbb67ae8584caa73bn, 0x3c6ef372fe94f82bn, 0xa54ff53a5f1d36f1n,
  0x510e527fade682d1n, 0x9b05688c2b3e6c1fn, 0x1f83d9abfb41bd6bn, 0x5be0cd19137e2179n,
];

/* SHA-256 and SHA-512 (FIPS 180-4) in plain JavaScript, for a page that has no WebCrypto: a plain-http page at any
   address but localhost is not a secure context and gets no crypto.subtle. The tables are the fractional parts of the
   square and cube roots of the first primes, as the standard defines them. Messages are below 2^53 bits. */
function sha256js(msg) {
  const l = msg.length, padLen = ((l + 9 + 63) >> 6) << 6;
  const m = new Uint8Array(padLen);
  m.set(msg); m[l] = 0x80;
  const dv = new DataView(m.buffer);
  dv.setUint32(padLen - 8, Math.floor((l * 8) / 0x100000000)); dv.setUint32(padLen - 4, (l * 8) >>> 0);
  const rotr = (x, n) => (x >>> n) | (x << (32 - n));
  const h = H256.slice();
  const w = new Uint32Array(64);
  for (let off = 0; off < padLen; off += 64) {
    for (let i = 0; i < 16; i++) w[i] = dv.getUint32(off + 4 * i);
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }
    let [a, b, c, d, e, f, g, hh] = h;
    for (let i = 0; i < 64; i++) {
      const t1 = (hh + (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) + ((e & f) ^ (~e & g)) + K256[i] + w[i]) >>> 0;
      const t2 = ((rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) >>> 0;
      hh = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b; b = a; a = (t1 + t2) >>> 0;
    }
    const r = [a, b, c, d, e, f, g, hh];
    for (let i = 0; i < 8; i++) h[i] = (h[i] + r[i]) >>> 0;
  }
  const out = new Uint8Array(32), ov = new DataView(out.buffer);
  for (let i = 0; i < 8; i++) ov.setUint32(4 * i, h[i]);
  return out;
}

function sha512js(msg) {
  const M64 = (1n << 64n) - 1n;
  const l = msg.length, padLen = ((l + 17 + 127) >> 7) << 7;
  const m = new Uint8Array(padLen);
  m.set(msg); m[l] = 0x80;
  const dv = new DataView(m.buffer);
  dv.setBigUint64(padLen - 8, BigInt(l) * 8n);
  const rotr = (x, n) => ((x >> n) | (x << (64n - n))) & M64;
  const h = H512.slice();
  const w = new Array(80);
  for (let off = 0; off < padLen; off += 128) {
    for (let i = 0; i < 16; i++) w[i] = dv.getBigUint64(off + 8 * i);
    for (let i = 16; i < 80; i++) {
      const s0 = rotr(w[i - 15], 1n) ^ rotr(w[i - 15], 8n) ^ (w[i - 15] >> 7n);
      const s1 = rotr(w[i - 2], 19n) ^ rotr(w[i - 2], 61n) ^ (w[i - 2] >> 6n);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) & M64;
    }
    let [a, b, c, d, e, f, g, hh] = h;
    for (let i = 0; i < 80; i++) {
      const t1 = (hh + (rotr(e, 14n) ^ rotr(e, 18n) ^ rotr(e, 41n)) + ((e & f) ^ ((e ^ M64) & g)) + K512[i] + w[i]) & M64;
      const t2 = ((rotr(a, 28n) ^ rotr(a, 34n) ^ rotr(a, 39n)) + ((a & b) ^ (a & c) ^ (b & c))) & M64;
      hh = g; g = f; f = e; e = (d + t1) & M64; d = c; c = b; b = a; a = (t1 + t2) & M64;
    }
    const r = [a, b, c, d, e, f, g, hh];
    for (let i = 0; i < 8; i++) h[i] = (h[i] + r[i]) & M64;
  }
  const out = new Uint8Array(64), ov = new DataView(out.buffer);
  for (let i = 0; i < 8; i++) ov.setBigUint64(8 * i, h[i]);
  return out;
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

/* noble takes its SHA-512 from this file, so it follows the hash choice above and needs no crypto.subtle of its own. */
function useNoble(module) {
  noble = module || nobleEd25519;
  noble.etc.sha512Async = async (...m) => sha512(concat(...m));
  backend = 'noble';
}

async function decideBackend() {
  if (backend) return backend;
  const s = subtle();
  if (s) {
    try {
      const key = await s.importKey('raw', new Uint8Array(32), { name: 'Ed25519' }, false, ['verify']);
      if (key) backend = 'webcrypto';
    } catch (e) { /* no Ed25519 in this WebCrypto */ }
  }
  if (!backend) useNoble(null);
  return backend;
}

/* Which Ed25519 is in use: "webcrypto", "noble", or null before the first verification. */
export function ed25519Backend() { return backend; }

/* Both choices in one object, for a page's "checked with" line: { hashes: "webcrypto" | "js" | null, ed25519: "webcrypto" | "noble" | null }. */
export function backends() { return { hashes, ed25519: backend }; }

/* Choose the Ed25519 implementation instead of letting the first verification decide: "webcrypto" or "noble" (the
   embedded copy, or another passed in as `module`). For tests, and for a page that wants to show the fallback works. */
export async function useEd25519(name, module = null) {
  if (name === 'noble') useNoble(module);
  else if (name === 'webcrypto') { if (!subtle()) throw new Error('this runtime has no WebCrypto'); backend = 'webcrypto'; }
  else throw new Error('Ed25519 is "webcrypto" or "noble"');
  return backend;
}

/* An error that means the check could not run here, as against a signature that does not verify. */
function unavailable(e) {
  const err = new Error('verification could not run in this browser: ' + (e && e.message ? e.message : String(e)));
  err.unavailable = true;
  return err;
}

/* True when sig (64 bytes) is pub's (32 bytes) Ed25519 signature over msg; false for anything malformed or unsigned.
   Throws, with `unavailable` set, only when no implementation can run here, so a caller says "could not check"
   rather than "does not verify". */
export async function verifySignature(pub, msg, sig) {
  if (!(pub instanceof Uint8Array) || pub.length !== 32 || !(sig instanceof Uint8Array) || sig.length !== 64) return false;
  if ((await decideBackend()) === 'webcrypto') {
    const s = subtle();
    let key;
    try { key = await s.importKey('raw', pub, { name: 'Ed25519' }, false, ['verify']); } catch (e) { return false; }   // not a point
    try { return !!(await s.verify({ name: 'Ed25519' }, key, sig, msg)); } catch (e) { throw unavailable(e); }
  }
  try { return !!(await noble.verifyAsync(sig, msg, pub)); } catch (e) { throw unavailable(e); }   // noble returns false for a bad point
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
  const out = { ok: false, reason: null, mint: null, window: null, root: null, count: null, expected: null, unavailable: false };
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
  let signed;
  try { signed = await verifySignature(pub, commitmentCanon(c.mint, c.window, c.root, c.count, floats.get(c)), sig); }
  catch (e) { out.reason = 'could not check the signature: ' + e.message; out.unavailable = true; return out; }
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
   where every settlement's ok is its own inclusion in the root, and ok at the top is all of it. `unavailable` is
   true when a check could not run here at all (the reason says so), as against a proof that fails. */
export async function verifyProof(doc, opts = {}) {
  const out = { ok: false, reason: null, commitment: null, count: null, shown: 0, settlements: [], unavailable: false };
  if (typeof doc === 'string') {
    try { doc = parse(doc); } catch (e) { out.reason = e.message; return out; }
  }
  if (!doc || typeof doc !== 'object' || Array.isArray(doc)) { out.reason = 'the document is not a JSON object'; return out; }
  if (doc.kind !== 'proof') { out.reason = 'not a proof document (kind is ' + JSON.stringify(doc.kind === undefined ? null : doc.kind) + ', not "proof")'; return out; }
  if (!Array.isArray(doc.settlements)) { out.reason = 'settlements must be a list'; return out; }
  const c = { mint: doc.mint, window: doc.window, root: doc.root, count: doc.count, signature: doc.signature };
  if (floats.get(doc)) floats.set(c, floats.get(doc));
  out.commitment = await verifyCommitment(c, opts);
  out.unavailable = out.commitment.unavailable;
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
      if (e.unavailable) out.unavailable = true;
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

/* ------------------------------------------------------- noble-ed25519 2.3.0 */

/* @noble/ed25519 2.3.0, index.js as published (MIT, Paul Miller), unchanged but for its last line: the module's
   `export { ... }` became the `return { ... }` of this function, so the file stays one module with nothing to fetch.
   NOBLE_SHA256 above is the published file's digest; nobleSourceMatches(text) checks a copy of it against this one. */
const nobleEd25519 = (() => {
/*! noble-ed25519 - MIT License (c) 2019 Paul Miller (paulmillr.com) */
/**
 * 4KB JS implementation of ed25519 EdDSA signatures.
 * Compliant with RFC8032, FIPS 186-5 & ZIP215.
 * @module
 * @example
 * ```js
import * as ed from '@noble/ed25519';
(async () => {
  const privKey = ed.utils.randomPrivateKey();
  const message = Uint8Array.from([0xab, 0xbc, 0xcd, 0xde]);
  const pubKey = await ed.getPublicKeyAsync(privKey); // Sync methods are also present
  const signature = await ed.signAsync(message, privKey);
  const isValid = await ed.verifyAsync(signature, message, pubKey);
})();
```
 */
/**
 * Curve params. ed25519 is twisted edwards curve. Equation is −x² + y² = -a + dx²y².
 * * P = `2n**255n - 19n` // field over which calculations are done
 * * N = `2n**252n + 27742317777372353535851937790883648493n` // group order, amount of curve points
 * * h = 8 // cofactor
 * * a = `Fp.create(BigInt(-1))` // equation param
 * * d = -121665/121666 a.k.a. `Fp.neg(121665 * Fp.inv(121666))` // equation param
 * * Gx, Gy are coordinates of Generator / base point
 */
const ed25519_CURVE = {
    p: 0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffedn,
    n: 0x1000000000000000000000000000000014def9dea2f79cd65812631a5cf5d3edn,
    h: 8n,
    a: 0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffecn,
    d: 0x52036cee2b6ffe738cc740797779e89800700a4d4141d8ab75eb4dca135978a3n,
    Gx: 0x216936d3cd6e53fec0a4e231fdd6dc5c692cc7609525a7b2c9562d608f25d51an,
    Gy: 0x6666666666666666666666666666666666666666666666666666666666666658n,
};
const { p: P, n: N, Gx, Gy, a: _a, d: _d } = ed25519_CURVE;
const h = 8n; // cofactor
const L = 32; // field / group byte length
const L2 = 64;
// Helpers and Precomputes sections are reused between libraries
// ## Helpers
// ----------
// error helper, messes-up stack trace
const err = (m = '') => {
    throw new Error(m);
};
const isBig = (n) => typeof n === 'bigint'; // is big integer
const isStr = (s) => typeof s === 'string'; // is string
const isBytes = (a) => a instanceof Uint8Array || (ArrayBuffer.isView(a) && a.constructor.name === 'Uint8Array');
/** assert is Uint8Array (of specific length) */
const abytes = (a, l) => !isBytes(a) || (typeof l === 'number' && l > 0 && a.length !== l)
    ? err('Uint8Array expected')
    : a;
/** create Uint8Array */
const u8n = (len) => new Uint8Array(len);
const u8fr = (buf) => Uint8Array.from(buf);
const padh = (n, pad) => n.toString(16).padStart(pad, '0');
const bytesToHex = (b) => Array.from(abytes(b))
    .map((e) => padh(e, 2))
    .join('');
const C = { _0: 48, _9: 57, A: 65, F: 70, a: 97, f: 102 }; // ASCII characters
const _ch = (ch) => {
    if (ch >= C._0 && ch <= C._9)
        return ch - C._0; // '2' => 50-48
    if (ch >= C.A && ch <= C.F)
        return ch - (C.A - 10); // 'B' => 66-(65-10)
    if (ch >= C.a && ch <= C.f)
        return ch - (C.a - 10); // 'b' => 98-(97-10)
    return;
};
const hexToBytes = (hex) => {
    const e = 'hex invalid';
    if (!isStr(hex))
        return err(e);
    const hl = hex.length;
    const al = hl / 2;
    if (hl % 2)
        return err(e);
    const array = u8n(al);
    for (let ai = 0, hi = 0; ai < al; ai++, hi += 2) {
        // treat each char as ASCII
        const n1 = _ch(hex.charCodeAt(hi)); // parse first char, multiply it by 16
        const n2 = _ch(hex.charCodeAt(hi + 1)); // parse second char
        if (n1 === undefined || n2 === undefined)
            return err(e);
        array[ai] = n1 * 16 + n2; // example: 'A9' => 10*16 + 9
    }
    return array;
};
/** normalize hex or ui8a to ui8a */
const toU8 = (a, len) => abytes(isStr(a) ? hexToBytes(a) : u8fr(abytes(a)), len);
const cr = () => globalThis?.crypto; // WebCrypto is available in all modern environments
const subtle = () => cr()?.subtle ?? err('crypto.subtle must be defined');
// prettier-ignore
const concatBytes = (...arrs) => {
    const r = u8n(arrs.reduce((sum, a) => sum + abytes(a).length, 0)); // create u8a of summed length
    let pad = 0; // walk through each array,
    arrs.forEach(a => { r.set(a, pad); pad += a.length; }); // ensure they have proper type
    return r;
};
/** WebCrypto OS-level CSPRNG (random number generator). Will throw when not available. */
const randomBytes = (len = L) => {
    const c = cr();
    return c.getRandomValues(u8n(len));
};
const big = BigInt;
const arange = (n, min, max, msg = 'bad number: out of range') => isBig(n) && min <= n && n < max ? n : err(msg);
/** modular division */
const M = (a, b = P) => {
    const r = a % b;
    return r >= 0n ? r : b + r;
};
const modN = (a) => M(a, N);
/** Modular inversion using eucledian GCD (non-CT). No negative exponent for now. */
// prettier-ignore
const invert = (num, md) => {
    if (num === 0n || md <= 0n)
        err('no inverse n=' + num + ' mod=' + md);
    let a = M(num, md), b = md, x = 0n, y = 1n, u = 1n, v = 0n;
    while (a !== 0n) {
        const q = b / a, r = b % a;
        const m = x - u * q, n = y - v * q;
        b = a, a = r, x = u, y = v, u = m, v = n;
    }
    return b === 1n ? M(x, md) : err('no inverse'); // b is gcd at this point
};
const callHash = (name) => {
    // @ts-ignore
    const fn = etc[name];
    if (typeof fn !== 'function')
        err('hashes.' + name + ' not set');
    return fn;
};
const apoint = (p) => (p instanceof Point ? p : err('Point expected'));
// ## End of Helpers
// -----------------
const B256 = 2n ** 256n;
/** Point in XYZT extended coordinates. */
class Point {
    static BASE;
    static ZERO;
    ex;
    ey;
    ez;
    et;
    constructor(ex, ey, ez, et) {
        const max = B256;
        this.ex = arange(ex, 0n, max);
        this.ey = arange(ey, 0n, max);
        this.ez = arange(ez, 1n, max);
        this.et = arange(et, 0n, max);
        Object.freeze(this);
    }
    static fromAffine(p) {
        return new Point(p.x, p.y, 1n, M(p.x * p.y));
    }
    /** RFC8032 5.1.3: Uint8Array to Point. */
    static fromBytes(hex, zip215 = false) {
        const d = _d;
        // Copy array to not mess it up.
        const normed = u8fr(abytes(hex, L));
        // adjust first LE byte = last BE byte
        const lastByte = hex[31];
        normed[31] = lastByte & ~0x80;
        const y = bytesToNumLE(normed);
        // zip215=true:           0 <= y < 2^256
        // zip215=false, RFC8032: 0 <= y < 2^255-19
        const max = zip215 ? B256 : P;
        arange(y, 0n, max);
        const y2 = M(y * y); // y²
        const u = M(y2 - 1n); // u=y²-1
        const v = M(d * y2 + 1n); // v=dy²+1
        let { isValid, value: x } = uvRatio(u, v); // (uv³)(uv⁷)^(p-5)/8; square root
        if (!isValid)
            err('bad point: y not sqrt'); // not square root: bad point
        const isXOdd = (x & 1n) === 1n; // adjust sign of x coordinate
        const isLastByteOdd = (lastByte & 0x80) !== 0; // x_0, last bit
        if (!zip215 && x === 0n && isLastByteOdd)
            err('bad point: x==0, isLastByteOdd'); // x=0, x_0=1
        if (isLastByteOdd !== isXOdd)
            x = M(-x);
        return new Point(x, y, 1n, M(x * y)); // Z=1, T=xy
    }
    /** Checks if the point is valid and on-curve. */
    assertValidity() {
        const a = _a;
        const d = _d;
        const p = this;
        if (p.is0())
            throw new Error('bad point: ZERO'); // TODO: optimize, with vars below?
        // Equation in affine coordinates: ax² + y² = 1 + dx²y²
        // Equation in projective coordinates (X/Z, Y/Z, Z):  (aX² + Y²)Z² = Z⁴ + dX²Y²
        const { ex: X, ey: Y, ez: Z, et: T } = p;
        const X2 = M(X * X); // X²
        const Y2 = M(Y * Y); // Y²
        const Z2 = M(Z * Z); // Z²
        const Z4 = M(Z2 * Z2); // Z⁴
        const aX2 = M(X2 * a); // aX²
        const left = M(Z2 * M(aX2 + Y2)); // (aX² + Y²)Z²
        const right = M(Z4 + M(d * M(X2 * Y2))); // Z⁴ + dX²Y²
        if (left !== right)
            throw new Error('bad point: equation left != right (1)');
        // In Extended coordinates we also have T, which is x*y=T/Z: check X*Y == Z*T
        const XY = M(X * Y);
        const ZT = M(Z * T);
        if (XY !== ZT)
            throw new Error('bad point: equation left != right (2)');
        return this;
    }
    /** Equality check: compare points P&Q. */
    equals(other) {
        const { ex: X1, ey: Y1, ez: Z1 } = this;
        const { ex: X2, ey: Y2, ez: Z2 } = apoint(other); // checks class equality
        const X1Z2 = M(X1 * Z2);
        const X2Z1 = M(X2 * Z1);
        const Y1Z2 = M(Y1 * Z2);
        const Y2Z1 = M(Y2 * Z1);
        return X1Z2 === X2Z1 && Y1Z2 === Y2Z1;
    }
    is0() {
        return this.equals(I);
    }
    /** Flip point over y coordinate. */
    negate() {
        return new Point(M(-this.ex), this.ey, this.ez, M(-this.et));
    }
    /** Point doubling. Complete formula. Cost: `4M + 4S + 1*a + 6add + 1*2`. */
    double() {
        const { ex: X1, ey: Y1, ez: Z1 } = this;
        const a = _a;
        // https://hyperelliptic.org/EFD/g1p/auto-twisted-extended.html#doubling-dbl-2008-hwcd
        const A = M(X1 * X1);
        const B = M(Y1 * Y1);
        const C = M(2n * M(Z1 * Z1));
        const D = M(a * A);
        const x1y1 = X1 + Y1;
        const E = M(M(x1y1 * x1y1) - A - B);
        const G = D + B;
        const F = G - C;
        const H = D - B;
        const X3 = M(E * F);
        const Y3 = M(G * H);
        const T3 = M(E * H);
        const Z3 = M(F * G);
        return new Point(X3, Y3, Z3, T3);
    }
    /** Point addition. Complete formula. Cost: `8M + 1*k + 8add + 1*2`. */
    add(other) {
        const { ex: X1, ey: Y1, ez: Z1, et: T1 } = this;
        const { ex: X2, ey: Y2, ez: Z2, et: T2 } = apoint(other); // doesn't check if other on-curve
        const a = _a;
        const d = _d;
        // https://hyperelliptic.org/EFD/g1p/auto-twisted-extended-1.html#addition-add-2008-hwcd-3
        const A = M(X1 * X2);
        const B = M(Y1 * Y2);
        const C = M(T1 * d * T2);
        const D = M(Z1 * Z2);
        const E = M((X1 + Y1) * (X2 + Y2) - A - B);
        const F = M(D - C);
        const G = M(D + C);
        const H = M(B - a * A);
        const X3 = M(E * F);
        const Y3 = M(G * H);
        const T3 = M(E * H);
        const Z3 = M(F * G);
        return new Point(X3, Y3, Z3, T3);
    }
    /**
     * Point-by-scalar multiplication. Scalar must be in range 1 <= n < CURVE.n.
     * Uses {@link wNAF} for base point.
     * Uses fake point to mitigate side-channel leakage.
     * @param n scalar by which point is multiplied
     * @param safe safe mode guards against timing attacks; unsafe mode is faster
     */
    multiply(n, safe = true) {
        if (!safe && (n === 0n || this.is0()))
            return I;
        arange(n, 1n, N);
        if (n === 1n)
            return this;
        if (this.equals(G))
            return wNAF(n).p;
        // init result point & fake point
        let p = I;
        let f = G;
        for (let d = this; n > 0n; d = d.double(), n >>= 1n) {
            // if bit is present, add to point
            // if not present, add to fake, for timing safety
            if (n & 1n)
                p = p.add(d);
            else if (safe)
                f = f.add(d);
        }
        return p;
    }
    /** Convert point to 2d xy affine point. (X, Y, Z) ∋ (x=X/Z, y=Y/Z) */
    toAffine() {
        const { ex: x, ey: y, ez: z } = this;
        // fast-paths for ZERO point OR Z=1
        if (this.equals(I))
            return { x: 0n, y: 1n };
        const iz = invert(z, P);
        // (Z * Z^-1) must be 1, otherwise bad math
        if (M(z * iz) !== 1n)
            err('invalid inverse');
        // x = X*Z^-1; y = Y*Z^-1
        return { x: M(x * iz), y: M(y * iz) };
    }
    toBytes() {
        const { x, y } = this.assertValidity().toAffine();
        const b = numTo32bLE(y);
        // store sign in first LE byte
        b[31] |= x & 1n ? 0x80 : 0;
        return b;
    }
    toHex() {
        return bytesToHex(this.toBytes());
    } // encode to hex string
    clearCofactor() {
        return this.multiply(big(h), false);
    }
    isSmallOrder() {
        return this.clearCofactor().is0();
    }
    isTorsionFree() {
        // multiply by big number CURVE.n
        let p = this.multiply(N / 2n, false).double(); // ensures the point is not "bad".
        if (N % 2n)
            p = p.add(this); // P^(N+1) // P*N == (P*(N/2))*2+P
        return p.is0();
    }
    static fromHex(hex, zip215) {
        return Point.fromBytes(toU8(hex), zip215);
    }
    get x() {
        return this.toAffine().x;
    }
    get y() {
        return this.toAffine().y;
    }
    toRawBytes() {
        return this.toBytes();
    }
}
/** Generator / base point */
const G = new Point(Gx, Gy, 1n, M(Gx * Gy));
/** Identity / zero point */
const I = new Point(0n, 1n, 1n, 0n);
// Static aliases
Point.BASE = G;
Point.ZERO = I;
const numTo32bLE = (num) => hexToBytes(padh(arange(num, 0n, B256), L2)).reverse();
const bytesToNumLE = (b) => big('0x' + bytesToHex(u8fr(abytes(b)).reverse()));
const pow2 = (x, power) => {
    // pow2(x, 4) == x^(2^4)
    let r = x;
    while (power-- > 0n) {
        r *= r;
        r %= P;
    }
    return r;
};
// prettier-ignore
const pow_2_252_3 = (x) => {
    const x2 = (x * x) % P; // x^2,       bits 1
    const b2 = (x2 * x) % P; // x^3,       bits 11
    const b4 = (pow2(b2, 2n) * b2) % P; // x^(2^4-1), bits 1111
    const b5 = (pow2(b4, 1n) * x) % P; // x^(2^5-1), bits 11111
    const b10 = (pow2(b5, 5n) * b5) % P; // x^(2^10)
    const b20 = (pow2(b10, 10n) * b10) % P; // x^(2^20)
    const b40 = (pow2(b20, 20n) * b20) % P; // x^(2^40)
    const b80 = (pow2(b40, 40n) * b40) % P; // x^(2^80)
    const b160 = (pow2(b80, 80n) * b80) % P; // x^(2^160)
    const b240 = (pow2(b160, 80n) * b80) % P; // x^(2^240)
    const b250 = (pow2(b240, 10n) * b10) % P; // x^(2^250)
    const pow_p_5_8 = (pow2(b250, 2n) * x) % P; // < To pow to (p+3)/8, multiply it by x.
    return { pow_p_5_8, b2 };
};
const RM1 = 0x2b8324804fc1df0b2b4d00993dfbd7a72f431806ad2fe478c4ee1b274a0ea0b0n; // √-1
// for sqrt comp
// prettier-ignore
const uvRatio = (u, v) => {
    const v3 = M(v * v * v); // v³
    const v7 = M(v3 * v3 * v); // v⁷
    const pow = pow_2_252_3(u * v7).pow_p_5_8; // (uv⁷)^(p-5)/8
    let x = M(u * v3 * pow); // (uv³)(uv⁷)^(p-5)/8
    const vx2 = M(v * x * x); // vx²
    const root1 = x; // First root candidate
    const root2 = M(x * RM1); // Second root candidate; RM1 is √-1
    const useRoot1 = vx2 === u; // If vx² = u (mod p), x is a square root
    const useRoot2 = vx2 === M(-u); // If vx² = -u, set x <-- x * 2^((p-1)/4)
    const noRoot = vx2 === M(-u * RM1); // There is no valid root, vx² = -u√-1
    if (useRoot1)
        x = root1;
    if (useRoot2 || noRoot)
        x = root2; // We return root2 anyway, for const-time
    if ((M(x) & 1n) === 1n)
        x = M(-x); // edIsNegative
    return { isValid: useRoot1 || useRoot2, value: x };
};
// N == L, just weird naming
const modL_LE = (hash) => modN(bytesToNumLE(hash)); // modulo L; but little-endian
const sha512a = (...m) => etc.sha512Async(...m); // Async SHA512
const sha512s = (...m) => callHash('sha512Sync')(...m);
// RFC8032 5.1.5
const hash2extK = (hashed) => {
    // slice creates a copy, unlike subarray
    const head = hashed.slice(0, L);
    head[0] &= 248; // Clamp bits: 0b1111_1000
    head[31] &= 127; // 0b0111_1111
    head[31] |= 64; // 0b0100_0000
    const prefix = hashed.slice(L, L2); // private key "prefix"
    const scalar = modL_LE(head); // modular division over curve order
    const point = G.multiply(scalar); // public key point
    const pointBytes = point.toBytes(); // point serialized to Uint8Array
    return { head, prefix, scalar, point, pointBytes };
};
// RFC8032 5.1.5; getPublicKey async, sync. Hash priv key and extract point.
const getExtendedPublicKeyAsync = (priv) => sha512a(toU8(priv, L)).then(hash2extK);
const getExtendedPublicKey = (priv) => hash2extK(sha512s(toU8(priv, L)));
/** Creates 32-byte ed25519 public key from 32-byte private key. Async. */
const getPublicKeyAsync = (priv) => getExtendedPublicKeyAsync(priv).then((p) => p.pointBytes);
/** Creates 32-byte ed25519 public key from 32-byte private key. To use, set `etc.sha512Sync` first. */
const getPublicKey = (priv) => getExtendedPublicKey(priv).pointBytes;
const hashFinishA = (res) => sha512a(res.hashable).then(res.finish);
const hashFinishS = (res) => res.finish(sha512s(res.hashable));
const _sign = (e, rBytes, msg) => {
    // sign() shared code
    const { pointBytes: P, scalar: s } = e;
    const r = modL_LE(rBytes); // r was created outside, reduce it modulo L
    const R = G.multiply(r).toBytes(); // R = [r]B
    const hashable = concatBytes(R, P, msg); // dom2(F, C) || R || A || PH(M)
    const finish = (hashed) => {
        // k = SHA512(dom2(F, C) || R || A || PH(M))
        const S = modN(r + modL_LE(hashed) * s); // S = (r + k * s) mod L; 0 <= s < l
        return abytes(concatBytes(R, numTo32bLE(S)), L2); // 64-byte sig: 32b R.x + 32b LE(S)
    };
    return { hashable, finish };
};
/**
 * Signs message (NOT message hash) using private key. Async.
 * Follows RFC8032 5.1.6.
 */
const signAsync = async (msg, privKey) => {
    const m = toU8(msg);
    const e = await getExtendedPublicKeyAsync(privKey);
    const rBytes = await sha512a(e.prefix, m); // r = SHA512(dom2(F, C) || prefix || PH(M))
    return hashFinishA(_sign(e, rBytes, m)); // gen R, k, S, then 64-byte signature
};
/**
 * Signs message (NOT message hash) using private key. To use, set `hashes.sha512` first.
 * Follows RFC8032 5.1.6.
 */
const sign = (msg, privKey) => {
    const m = toU8(msg);
    const e = getExtendedPublicKey(privKey);
    const rBytes = sha512s(e.prefix, m); // r = SHA512(dom2(F, C) || prefix || PH(M))
    return hashFinishS(_sign(e, rBytes, m)); // gen R, k, S, then 64-byte signature
};
const veriOpts = { zip215: true };
const _verify = (sig, msg, pub, opts = veriOpts) => {
    sig = toU8(sig, L2); // Signature hex str/Bytes, must be 64 bytes
    msg = toU8(msg); // Message hex str/Bytes
    pub = toU8(pub, L);
    const { zip215 } = opts; // switch between zip215 and rfc8032 verif
    let A;
    let R;
    let s;
    let SB;
    let hashable = Uint8Array.of();
    try {
        A = Point.fromHex(pub, zip215); // public key A decoded
        R = Point.fromHex(sig.slice(0, L), zip215); // 0 <= R < 2^256: ZIP215 R can be >= P
        s = bytesToNumLE(sig.slice(L, L2)); // Decode second half as an integer S
        SB = G.multiply(s, false); // in the range 0 <= s < L
        hashable = concatBytes(R.toBytes(), A.toBytes(), msg); // dom2(F, C) || R || A || PH(M)
    }
    catch (error) { }
    const finish = (hashed) => {
        // k = SHA512(dom2(F, C) || R || A || PH(M))
        if (SB == null)
            return false; // false if try-catch catched an error
        if (!zip215 && A.isSmallOrder())
            return false; // false for SBS: Strongly Binding Signature
        const k = modL_LE(hashed); // decode in little-endian, modulo L
        const RkA = R.add(A.multiply(k, false)); // [8]R + [8][k]A'
        return RkA.add(SB.negate()).clearCofactor().is0(); // [8][S]B = [8]R + [8][k]A'
    };
    return { hashable, finish };
};
/** Verifies signature on message and public key. Async. Follows RFC8032 5.1.7. */
const verifyAsync = async (s, m, p, opts = veriOpts) => hashFinishA(_verify(s, m, p, opts));
/** Verifies signature on message and public key. To use, set `hashes.sha512` first. Follows RFC8032 5.1.7. */
const verify = (s, m, p, opts = veriOpts) => hashFinishS(_verify(s, m, p, opts));
/** Math, hex, byte helpers. Not in `utils` because utils share API with noble-curves. */
const etc = {
    sha512Async: async (...messages) => {
        const s = subtle();
        const m = concatBytes(...messages);
        return u8n(await s.digest('SHA-512', m.buffer));
    },
    sha512Sync: undefined,
    bytesToHex: bytesToHex,
    hexToBytes: hexToBytes,
    concatBytes: concatBytes,
    mod: M,
    invert: invert,
    randomBytes: randomBytes,
};
/** ed25519-specific key utilities. */
const utils = {
    getExtendedPublicKeyAsync: getExtendedPublicKeyAsync,
    getExtendedPublicKey: getExtendedPublicKey,
    randomPrivateKey: () => randomBytes(L),
    precompute: (w = 8, p = G) => {
        p.multiply(3n);
        w;
        return p;
    }, // no-op
};
// ## Precomputes
// --------------
const W = 8; // W is window size
const scalarBits = 256;
const pwindows = Math.ceil(scalarBits / W) + 1; // 33 for W=8
const pwindowSize = 2 ** (W - 1); // 128 for W=8
const precompute = () => {
    const points = [];
    let p = G;
    let b = p;
    for (let w = 0; w < pwindows; w++) {
        b = p;
        points.push(b);
        for (let i = 1; i < pwindowSize; i++) {
            b = b.add(p);
            points.push(b);
        } // i=1, bc we skip 0
        p = b.double();
    }
    return points;
};
let Gpows = undefined; // precomputes for base point G
// const-time negate
const ctneg = (cnd, p) => {
    const n = p.negate();
    return cnd ? n : p;
};
/**
 * Precomputes give 12x faster getPublicKey(), 10x sign(), 2x verify() by
 * caching multiples of G (base point). Cache is stored in 32MB of RAM.
 * Any time `G.multiply` is done, precomputes are used.
 * Not used for getSharedSecret, which instead multiplies random pubkey `P.multiply`.
 *
 * w-ary non-adjacent form (wNAF) precomputation method is 10% slower than windowed method,
 * but takes 2x less RAM. RAM reduction is possible by utilizing `.subtract`.
 *
 * !! Precomputes can be disabled by commenting-out call of the wNAF() inside Point#multiply().
 */
const wNAF = (n) => {
    const comp = Gpows || (Gpows = precompute());
    let p = I;
    let f = G; // f must be G, or could become I in the end
    const pow_2_w = 2 ** W; // 256 for W=8
    const maxNum = pow_2_w; // 256 for W=8
    const mask = big(pow_2_w - 1); // 255 for W=8 == mask 0b11111111
    const shiftBy = big(W); // 8 for W=8
    for (let w = 0; w < pwindows; w++) {
        let wbits = Number(n & mask); // extract W bits.
        n >>= shiftBy; // shift number by W bits.
        if (wbits > pwindowSize) {
            wbits -= maxNum;
            n += 1n;
        } // split if bits > max: +224 => 256-32
        const off = w * pwindowSize;
        const offF = off; // offsets, evaluate both
        const offP = off + Math.abs(wbits) - 1;
        const isEven = w % 2 !== 0; // conditions, evaluate both
        const isNeg = wbits < 0;
        if (wbits === 0) {
            // off == I: can't add it. Adding random offF instead.
            f = f.add(ctneg(isEven, comp[offF])); // bits are 0: add garbage to fake point
        }
        else {
            p = p.add(ctneg(isNeg, comp[offP])); // bits are 1: add to result point
        }
    }
    return { p, f }; // return both real and fake points for JIT
};
// !! Remove the export to easily use in REPL / browser console
return { CURVE: ed25519_CURVE, etc, ExtendedPoint: Point, getPublicKey, getPublicKeyAsync, Point, sign, signAsync, utils, verify, verifyAsync };
})();


export const version = '0.2.0';
