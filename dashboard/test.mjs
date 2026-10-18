// The page's modules under Node, fed the real snapshots in fixtures/ (the exact JSON the page's <script id="snapshot">
// holds in each mode), with the numbers the pages show asserted, so a regression in the derivation is caught before a
// rollout. No browser: a window/document shim is enough, since the modules only read the snapshot element and return
// HTML strings.
//
//   node dashboard/test.mjs            (Node 18+; test_bwkit.py runs it too when node is on the PATH)

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const MODULES = ['data.js', 'rules.js', 'charts.js', 'view-mint.js', 'view-node.js', 'view-fleet.js'];
let checks = 0;
const ok = (cond, what) => { assert.ok(cond, what); checks++; };
const eq = (a, b, what) => { assert.equal(a, b, what); checks++; };
const count = (html, needle) => html.split(needle).length - 1;

// A page with one fixture embedded: the modules loaded in order into a sandbox that stands in for the browser.
function page(fixture) {
  const text = fs.readFileSync(path.join(here, 'fixtures', fixture), 'utf8');
  const el = { textContent: text };
  const sandbox = {
    console, JSON, Math, Date, Object, Array, String, Number, Boolean, RegExp, Error, Infinity, NaN, isNaN, isFinite, parseInt, parseFloat, URLSearchParams,
    document: { getElementById: (id) => (id === 'snapshot' ? el : null) },
    location: { search: '', pathname: '/' },
    localStorage: { getItem: () => null, setItem() {} },
    fetch: () => Promise.reject(new Error('the test has no network')),
    setTimeout() {}, clearTimeout() {}, setInterval() {},
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  for (const m of MODULES) vm.runInContext(fs.readFileSync(path.join(here, m), 'utf8'), sandbox, { filename: m });
  const BW = sandbox.BW;
  ok(BW.loadSnapshot(), fixture + ' loads as a snapshot');
  return BW;
}

function noThrow(label, fn) { let out; try { out = fn(); } catch (e) { throw new Error(label + ' threw: ' + (e && e.stack || e)); } ok(typeof out === 'string' && out.length > 0, label + ' produced HTML'); return out; }

// ---------------------------------------------------------------- the Mint's view
{
  const BW = page('mint.json'), D = BW.D, now = BW.now();
  eq(D.mode, 'snapshot'); eq(D.snapshot, true); eq(now, 1792342452, 'now is the moment the snapshot was taken');
  const X = BW.derive(now);
  eq(X.role, 'mint');
  eq(X.S.length, 50, '50 settlements'); eq(X.S.filter((s) => s.status === 'disputed').length, 7, '7 disputed'); eq(X.C.length, 14, '14 commitments');
  eq(X.pending.length, 1, 'one receipt never settled'); eq(X.pending[0].lost, true, 'and it is a lost window, two windows old');
  const items = BW.attention(X, now);
  eq(items.length, 0, 'nothing needs attention on this snapshot (the lost window is in the books, not an item): ' + JSON.stringify(items));
  // health from bwkit is read first and shown in the node's wording; a copy of the page's own verdict appears once
  D.status.health = { items: [{ level: 'warn', text: 'The public status was last posted 12 min ago.', since: now - 720 },
                              { level: 'bad', text: 'Only 400.00 MB of disk is free for the ledger.', since: null }], checked_at: now };
  D.status.disk_free_bytes = 400e6;
  const withHealth = BW.attention(X, now);
  eq(withHealth.length, 2, 'the node\'s two items, the disk one not doubled by the page\'s own: ' + JSON.stringify(withHealth));
  eq(withHealth[0].level, 'bad', 'bad before warn');
  delete D.status.health; D.status.disk_free_bytes = 53089857536;
  const html = noThrow('the Mint\'s view', () => BW.views.mint(X, now));
  ok(html.includes('<span class="k">windows settled</span><span class="v">50</span><span class="s">7 disputed · 14 commitments</span>'), 'Now shows 50 windows, 7 disputed, 14 commitments');
  eq(count(html, 'data-verify="'), 14, 'a verify button per committed hour');
  ok(html.includes('<h2>Attention</h2><div class="now"><div class="ok"><span class="k">nothing</span><span class="v">all clear</span>'), 'Attention shows all clear');
  eq(Object.keys(D.proofs).length, 2, 'the snapshot holds two proofs as text'); ok(typeof D.proofs['1792335600'] === 'string', 'a proof is the Mint\'s own text, never re-serialised');
  ok(D.proofs['1792335600'].includes('"kind": "proof"'), 'kept as the Mint wrote it (spaces after the colons)');
  // the drill-downs and the as-of control
  const key = BW.keyOf(X.S[0]);
  noThrow('a window\'s detail', () => BW.sec.windowDetail(X, key));
  const pur = X.purchases[0]; ok(pur, 'the Mint holds purchases'); noThrow('a purchase\'s detail', () => BW.sec.purchaseDetail(X, pur.id));
  const earlier = now - 86400, Y = BW.derive(earlier);
  ok(Y.S.length < X.S.length && Y.S.length > 0, 'as of a day earlier the books are smaller: ' + Y.S.length);
  noThrow('the Mint\'s view as of a day earlier', () => BW.views.mint(Y, earlier));
  // the ledger paging keeps each entry once
  eq(BW.addNew(D.ledger, D.ledger.slice(-3)).length, D.ledger.length, 'addNew drops entries already held');
  eq(BW.addNew(D.ledger.slice(0, 5), D.ledger.slice(3, 8)).length, 8, 'and keeps the new ones');
}

// ---------------------------------------------------------------- the node's view: a hop
{
  const BW = page('hop.json'), D = BW.D, now = BW.now();
  const X = BW.derive(now);
  eq(X.role, 'hop'); eq(X.me, D.status.nym);
  eq(D.mint.name, 'bw-mint', 'the Mint\'s name travels in the snapshot');
  eq(X.S.length, 12, 'the hop\'s 12 settled windows'); eq(X.S.filter((s) => s.status === 'disputed').length, 5, '5 of them disputed');
  const html = noThrow('the hop\'s view', () => BW.views.node(X, now));
  ok(html.includes('<span class="k">owed to me</span><span class="v">15.15¢</span>'), 'owed to me: 15.15¢ (carriage less the fee, plus premiums)');
  ok(html.includes('<span class="k">windows</span><span class="v">12</span><span class="s">5 settled disputed</span>'), 'My month: 12 windows, 5 disputed');
  ok(html.includes('<h2>My month <small>2026-10'), 'the month named');
  ok(html.includes('<h2>The Mint <small>who keeps my books</small></h2>'), 'the Mint card');
  ok(html.includes('<span class="k">Mint</span><span class="v">bw-mint</span>'), 'named bw-mint');
  ok(html.includes('data-verify="1792335600"'), 'the latest committed hour can be verified from the hop\'s page');
  const items = BW.attention(X, now);
  eq(items.length, 0, 'nothing needs attention on the hop: ' + JSON.stringify(items));
  noThrow('the hop\'s view as of six hours earlier', () => BW.views.node(BW.derive(now - 6 * 3600), now - 6 * 3600));
}

// ---------------------------------------------------------------- the node's view: a payer
{
  const BW = page('payer.json'), D = BW.D, now = BW.now();
  const X = BW.derive(now);
  eq(X.role, 'payer');
  eq(X.S.length, 50, 'every settled window is the payer\'s'); eq(X.myR.length, 51, 'its own 51 receipts'); eq(X.myA.length, 50, 'and the 50 acks it was handed: one receipt was never answered');
  const items = BW.attention(X, now);
  eq(items.length, 0, 'no attention item on the payer: ' + JSON.stringify(items));
  const html = noThrow('the payer\'s view', () => BW.views.node(X, now));
  ok(html.includes('<span class="k">I owe</span><span class="v">16.13¢</span>'), 'I owe: 16.13¢');
  ok(html.includes('<h2>My purchases'), 'the payer\'s purchases section'); ok(html.includes('<h2>The Mint '), 'the Mint card');
  eq(X.purchases.length, 1, 'one purchase of its own');
}

// ---------------------------------------------------------------- the fleet's view
{
  const BW = page('fleet.json'), D = BW.D, now = BW.now();
  eq(D.mode, 'fleet'); eq(D.snapshot, true); eq(D.nodes.length, 4);
  const html = noThrow('the fleet\'s view', () => BW.views.fleet(now));
  eq(count(html, '<span class="st up">up</span>'), 3, 'three nodes up');
  eq(count(html, '<span class="st none">no answer</span>'), 1, 'one did not answer (bw-forest)');
  eq(count(html, '<span class="st stale">'), 0, 'none judged stale against the real clock: now is the snapshot\'s moment');
  ok(html.includes('<h2>The hour across the fleet'), 'the hour across the fleet');
  eq(count(html, '<span class="st ok">on time</span>'), 1, 'the Mint\'s last hour committed on time');
  ok(html.includes('<h2>Position <small>the last 24 hours'), 'position across the Mints');
  ok(html.includes('bw-forest'), 'the node that did not answer is still on the grid');
}

console.log('dashboard/test.mjs: ' + checks + ' checks passed on ' + fs.readdirSync(path.join(here, 'fixtures')).filter((f) => f.endsWith('.json')).join(', '));
