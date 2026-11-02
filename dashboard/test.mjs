// The page's modules under Node, fed the real snapshots in fixtures/ (the exact JSON the page's <script id="snapshot">
// holds in each mode), with the numbers the pages show asserted, so a regression in the derivation is caught before a
// rollout. No browser: a window/document shim is enough, since the modules only read the snapshot element and return
// HTML strings.
//
//   node dashboard/test.mjs            (Node 18+; test_bwkit.py runs it too when node is on the PATH)
//
// The last section is the verifier, bsp.js: its own hashes against node:crypto, the fixtures' proofs under every
// backend, a run with no WebCrypto at all (a plain-http page off localhost), the honest answer when a check cannot
// run, and the embedded noble-ed25519 against its published digest.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { createHash, randomBytes } from 'node:crypto';
import { execFileSync } from 'node:child_process';

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
  eq(count(html, 'data-verify-day="1"'), 1, 'and one button for the last day');
  // a claimed window (Section 16): no payer count, the hop's count up to the lock, drawn on the lock; the row draws without a number for the payer
  {
    const s0 = D.ledger.filter((e) => e.kind === 'settlement').slice(-1)[0];
    const claimed = Object.assign({}, s0, { t: s0.t + 1, t_first: s0.t_first + 86400 * 365, t_last: s0.t_last + 86400 * 365, payer_bytes: null, hop_bytes: 250000000, settled_bytes: 200000000,
      status: 'claimed', rule: 'the hop\'s count, up to what the payer locked; the payer never closed the window', receipt_sig: null, ack_sig: null, claim_sig: 'c'.repeat(86),
      amount_usd: 0.02, fee_usd: 0.001, lock: 'a'.repeat(64), debit: 20000, covered: true });
    D.ledger.push(claimed);
    const X2 = BW.derive(claimed.t_last + 10), h2 = noThrow('the Mint\'s view with a claimed window', () => BW.views.mint(X2, claimed.t_last + 10));
    ok(h2.includes('<span class="st claimed">claimed</span>'), 'the window is marked claimed');
    ok(h2.includes('<td class="n">–</td><td class="n">250.00 MB</td>'), 'no payer count, the hop\'s count: ' + (h2.match(/<td class="n">[^<]*<\/td><td class="n">250.00 MB<\/td>/) || [''])[0]);
    D.ledger.pop();
  }
  // the Steward as of a moment: the run nearest before it, from the history the Mint serves; between runs the book does not change
  ok(html.includes('<h2>The Steward <small>runs on this host after each hour\'s commitment</small></h2>'), 'now: the Steward\'s book as the feed carries it');
  const S0 = D.steward;
  D.stewardRuns = [{ t: now - 7200, income_usd: 0.01, escrow_usd: 0.5, posted_usd: 0.2, paid_usd: 0.001, refunded_usd: 0, held_by_cap_usd: 0, posted: 1, skipped: ['the cabin: no link yet'] },
                   { t: now - 3600, income_usd: 0.02, escrow_usd: 0.6, posted_usd: 0.3, paid_usd: 0.002, refunded_usd: 0.0005, held_by_cap_usd: 0.0001, posted: 1, skipped: [] }];
  eq(BW.sec.stewardRunAt(D.stewardRuns, now - 5000).t, now - 7200, 'the run in force at a moment is the last at or before it');
  eq(BW.sec.stewardRunAt(D.stewardRuns, now - 7200).t, now - 7200, 'a run at the moment itself counts');
  eq(BW.sec.stewardRunAt(D.stewardRuns, now - 9000), null, 'none before the first');
  D.asOf = now - 5000;
  const asOfHtml = BW.views.mint(X, D.asOf);
  ok(asOfHtml.includes('<h2>The Steward <small>as its run of '), 'as of a moment, the section is headed by the run it shows');
  ok(asOfHtml.includes('<span class="k">income</span><span class="v">1.00¢</span>') || asOfHtml.includes('<span class="k">income</span><span class="v">$0.0100</span>'), 'the income of that run, not the feed\'s: ' + (asOfHtml.match(/<span class="k">income<\/span><span class="v">[^<]*/) || [''])[0]);
  ok(asOfHtml.includes('<span class="k">match reserve</span><span class="v">'), 'the reserve is m of that run\'s income');
  ok(asOfHtml.includes('That run skipped: the cabin: no link yet.'), 'that run\'s skipped list, not the feed\'s');
  ok(asOfHtml.includes('nearest before ') && asOfHtml.includes('from the 2 runs the Mint keeps; between runs the book does not change'), 'the note says which run and why');
  ok(!asOfHtml.includes('the as-of control does not move them'), 'the old disclaimer is gone');
  D.asOf = now - 9000;
  ok(BW.views.mint(X, D.asOf).includes('No Steward run before '), 'before the first run: says so instead of drawing now\'s book');
  D.stewardRuns = [];
  ok(BW.views.mint(X, D.asOf).includes('The Mint served no run history, so the Steward\'s balances are known only as of now.'), 'no history served: says so');
  D.asOf = null; D.stewardRuns = []; D.steward = S0;
  // the day: the committed hours of the last 24 the node holds, oldest first, and the one line that sums the answers
  const day = BW.dayHours(X, now);
  ok(day.length > 0 && day.length <= 24 && day.every((w, i) => i === 0 || day[i - 1] < w), 'the day\'s hours are ascending and at most 24: ' + day.join(' '));
  eq(day.length, new Set(X.C.filter((c) => c.window >= now - 86400 && c.window <= now).map((c) => c.window)).size, 'every committed hour of the last 24, once each, though an hour committed twice has two commitments');
  eq(BW.dayHours({ C: [{ window: now - 90000 }, { window: now - 3600 }, { window: now - 3600 }] }, now).join(' '), String(now - 3600), 'an older commitment is left out and a duplicate counted once');
  const res = (s, w, n) => ({ window: w, state: s, n: n || 0, okN: n || 0, why: s === 'failed' ? 'not in the commitment: its leaf does not reach the root' : s === 'not checked' ? 'this browser has no WebCrypto on a plain-http page' : '' });
  ok(BW.dayWords([res('verified', now - 7200, 3), res('verified', now - 3600, 4)]).startsWith('<span class="st ok">verified</span> 2 hours, 7 settlements in their roots, every root signed by the Mint'), 'all verified: hours and settlements counted');
  ok(BW.dayWords([res('verified', now - 7200, 3), res('failed', now - 3600, 2)]).startsWith('<span class="st declined">failed</span> 1 of 2 hours ('), 'one failure fails the day and names the hour');
  ok(BW.dayWords([res('not checked', now - 7200), res('not checked', now - 3600)]).startsWith('<span class="st none">not checked</span> this browser has no WebCrypto'), 'nothing judged: not checked, with the reason');
  ok(BW.dayWords([res('verified', now - 7200, 3), res('no proof', now - 3600)]).includes('1 hour with no proof held'), 'an hour without a proof is counted apart, not failed');
  ok(html.includes('<h2>Attention</h2><div class="now"><div class="ok"><span class="k">nothing</span><span class="v">all clear</span>'), 'Attention shows all clear');
  eq(Object.keys(D.proofs).length, 2, 'the snapshot holds two proofs as text'); ok(typeof D.proofs['1792335600'] === 'string', 'a proof is the Mint\'s own text, never re-serialised');
  ok(D.proofs['1792335600'].includes('"kind": "proof"'), 'kept as the Mint wrote it (spaces after the colons)');
  // the drill-downs and the as-of control
  const key = BW.keyOf(X.S[0]);
  noThrow('a window\'s detail', () => BW.sec.windowDetail(X, key));
  const pur = X.purchases[0]; ok(pur, 'the Mint holds purchases'); noThrow('a purchase\'s detail', () => BW.sec.purchaseDetail(X, pur.id));
  // the house's purchase: seven windows drew 1.60¢ of the dollar; past its deadline the rest shows as returned
  const house = X.purchases.find((p) => p.program === 'payer'); ok(house, 'the house\'s purchase is in the snapshot');
  const paid = X.paidOn[house.id]; eq(paid.windows, 7, 'seven windows settled against it'); eq(paid.own.toFixed(6), '0.016017', 'all of the premium from the poster\'s pool'); eq(paid.drawn, 0, 'nothing from the Steward');
  ok(html.includes('<td class="n">1.60¢</td><td class="n">98.40¢</td><td><span class="st open">open</span>'), 'premium paid 1.60¢, 98.40¢ unspent, open');
  const after = house.deadline + 1, Z = BW.derive(after), lateHtml = noThrow('the Mint\'s view past the deadline', () => BW.views.mint(Z, after));
  ok(lateHtml.includes('<td class="n">98.40¢ <span class="m">returned</span></td><td><span class="st closed">closed</span>'), 'past the deadline the unspent 98.40¢ is marked returned');
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
  eq(count(html, 'data-verify-day="1"'), 1, 'and the hop\'s page has the last-day button beside it');
  // the Invoice section: nothing read in this snapshot, then the Mint's text for last month and this month, with the hop's own line picked out
  ok(html.includes('<h2>The invoice <small>'), 'the Invoice section is on the hop\'s page');
  eq(count(html, 'was not read through this node'), 2, 'a snapshot without the Mint\'s invoice says so for both months');
  const months = BW.invoiceMonths(now);
  eq(months.join(' '), '2026-09 2026-10', 'the months the section shows: last month and this one, from the snapshot\'s moment');
  D.mintInvoice = {};
  D.mintInvoice[months[0]] = months[0] + '  bw-house pays bw-neighbor: 300 windows (7 disputed), 31.00 GB, $3.1000 + $0.3000 premium on 150 purchased windows ($0.0000 of it the Steward\'s): the relay\'s $3.2450, the Steward\'s $0.1700\n'
    + months[0] + '    returned to bw-house on short windows: $0.0300\n' + months[0] + '  bw-house pays bw-forest: 38 windows (0 disputed), 45.00 MB, $0.0045: the relay\'s $0.0043, the Steward\'s $0.0002\n'
    + months[0] + '  the Network Steward\'s share: $0.1702 (f = 0.05 of every settlement since 0.3.0; none before)\nwritten 2026-10-01 00:10 CDT by bwkit 0.5.1 on bw-mint\n';
  D.mintInvoice[months[1]] = months[1] + '  bw-house pays bw-neighbor: 12 windows (5 disputed), 1.20 GB, $0.1200: the relay\'s $0.1140, the Steward\'s $0.0060\n'
    + months[1] + '  the Network Steward\'s share: $0.0060 (f = 0.05 of every settlement since 0.3.0; none before)\nthe month so far, computed now; the Mint writes ' + months[1] + '.txt on the first of the next month\n';
  const withInv = BW.views.node(X, now);
  ok(withInv.includes('<h3>' + months[0] + ' <small>as the Mint wrote it, 2026-10-01 00:10 CDT by bwkit 0.5.1 on bw-mint</small></h3>'), 'last month is headed by when the Mint wrote it');
  ok(withInv.includes('<span class="k">owed to me</span><span class="v">$3.2450</span>'), 'the hop\'s own line gives what it is owed for last month');
  ok(!withInv.includes('<span class="v">$0.0043</span>'), 'bw-forest\'s line is not picked out as mine');
  ok(withInv.includes('<h3>' + months[1] + ' <small>this month so far, computed by the Mint now</small></h3>'), 'this month is marked as computed now');
  ok(withInv.includes('<span class="k">owed to me</span><span class="v">$0.1140</span>'), 'and gives this month\'s figure');
  ok(withInv.includes('<pre class="invoice">') && !withInv.includes('computed now; the Mint writes'), 'the Mint\'s text is shown without its trailer');
  ok(withInv.includes('<span class="k">paid</span><span class="v">not yet</span><span class="s">the Mint notes it when the house settles up</span>'), 'last month, written and not marked paid, says not yet');
  eq(count(withInv, '<span class="k">paid</span>'), 1, 'this month so far carries no paid cell');
  // the Mint's paid mark, one line after the written line, becomes the cell
  D.mintInvoice[months[0]] += months[0] + '  bw-house paid bw-neighbor: settled in person, Nov 2 (noted 2026-11-02 10:14 CST on bw-mint)\n' + months[0] + '  bw-house paid bw-forest: bench, nothing owed (noted 2026-11-02 10:15 CST on bw-mint)\n';
  const withPaid = BW.views.node(X, now);
  ok(withPaid.includes('<span class="k">paid</span><span class="v">settled in person, Nov 2</span><span class="s">noted 2026-11-02 10:14 CST on bw-mint</span>'), 'the paid mark of my line is the cell');
  ok(!withPaid.includes('bench, nothing owed</span>'), 'bw-forest\'s mark is not mine');
  ok(!withPaid.includes('<span class="v">not yet</span>'), 'and not yet is gone');
  delete D.mintInvoice;
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
  // a short window's refund is the premium the Mint never drew: it does not lower what the payer owes
  const Xr = Object.assign({}, X, { S: X.S.map((s, i) => (i ? s : Object.assign({}, s, { refund_usd: 0.05 }))) });
  ok(BW.views.node(Xr, now).includes('<span class="k">I owe</span><span class="v">16.13¢</span>'), 'I owe is unchanged by a refund on a short window');
  ok(html.includes('<th class="n">unspent</th>'), 'the purchases table says what is left of the escrow');
  ok(html.includes('<td class="n">$1.00</td><td class="n">7 / 168'), 'the dollar of escrow, seven windows settled against it so far');
  ok(html.includes('<td class="n">1.60¢</td><td class="n">98.40¢</td><td><span class="st open">open</span>'), '1.60¢ drawn, 98.40¢ unspent, open and so not called returned');
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
  // mine or theirs: a counterparty's node is watched, drawn, and left out of Position
  const posOf = (h) => (h.match(/<span class="k">net<\/span><span class="v">([^<]*)/) || [])[1];
  const net0 = posOf(html);
  ok(net0, 'Position has a net figure: ' + net0);
  const hop = D.nodes.find((n) => n.status && n.status.role === 'hop');
  hop.mine = false;
  const theirsHtml = BW.views.fleet(now);
  ok(theirsHtml.includes('watched, not mine: ' + hop.status.name), 'Position names the node watched as a counterparty\'s');
  ok(posOf(theirsHtml) !== net0, 'and its earnings leave the net: ' + net0 + ' → ' + posOf(theirsHtml));
  ok(!theirsHtml.includes(hop.status.name + ' <span class="m">mine</span>'), 'its pair is no longer marked mine');
  ok(theirsHtml.includes('bw-forest'), 'it stays on the grid');
  eq(count(theirsHtml, 'data-mine="'), 0, 'a snapshot has no list editor, so no switch to press');
  hop.mine = true;
  eq(posOf(BW.views.fleet(now)), net0, 'back to mine, the net is as it was');
  D.snapshot = false;
  const live = BW.views.fleet(now);
  eq(count(live, 'data-mine="'), D.nodes.length, 'live, one mine/theirs switch per node');
  eq(count(live, '>mine</button>'), D.nodes.length, 'every node mine by default');
  D.snapshot = true;
}

// ---------------------------------------------------------------- the verifier, bsp.js
{
  const text = fs.readFileSync(path.join(here, 'bsp.js'), 'utf8');
  // The kit has no package.json, so Node reads a .js file as CommonJS; the module is imported as a data: URL, and a fresh
  // one for each run below, because a module decides its backends once.
  const load = () => import('data:text/javascript;base64,' + Buffer.from(text).toString('base64'));
  const hex = (b) => Buffer.from(b).toString('hex');
  const mint = JSON.parse(fs.readFileSync(path.join(here, 'fixtures', 'mint.json'), 'utf8'));
  const proofs = Object.values(mint.proofs);
  ok(proofs.length === 2 && proofs.every((p) => typeof p === 'string'), 'the Mint fixture holds two proofs as text');
  const tampered = proofs[0].replace(/"hop_bytes": (\d+)/, (m, n) => '"hop_bytes": ' + (Number(n) + 1));
  ok(tampered !== proofs[0], 'a tampered copy changes one settlement\'s byte count');
  const allProofs = async (bsp, label) => {
    for (const p of proofs) { const r = await bsp.verifyProof(p); ok(r.ok && !r.unavailable, label + ': a real proof verifies: ' + r.reason); }
    const t = await bsp.verifyProof(tampered);
    ok(!t.ok && !t.unavailable && /not in the commitment/.test(t.settlements[0].reason || ''), label + ': the tampered copy fails, and is judged: ' + t.reason + ' / ' + t.settlements[0].reason);
  };
  // 1. the file's own SHA-256 and SHA-512 against node:crypto, across the block boundaries
  {
    const bsp = await load(); bsp.useHashes('js');
    for (const l of [0, 1, 55, 56, 63, 64, 65, 111, 112, 119, 120, 127, 128, 129, 1000]) {
      const m = l ? new Uint8Array(randomBytes(l)) : new Uint8Array(0);
      eq(hex(await bsp.sha256(m)), createHash('sha256').update(m).digest('hex'), 'SHA-256 in JavaScript agrees with node:crypto at ' + l + ' bytes');
      eq(hex(await bsp.sha512(m)), createHash('sha512').update(m).digest('hex'), 'SHA-512 in JavaScript agrees with node:crypto at ' + l + ' bytes');
    }
  }
  // 2. the proofs under every pairing of hashes and Ed25519
  for (const h of ['webcrypto', 'js']) for (const e of ['webcrypto', 'noble']) {
    const bsp = await load(); bsp.useHashes(h); await bsp.useEd25519(e);
    await allProofs(bsp, 'hashes ' + h + ', Ed25519 ' + e);
    eq(JSON.stringify(bsp.backends()), JSON.stringify({ hashes: h, ed25519: e }), 'backends() names what ran');
  }
  // 3. no WebCrypto at all, in a child process: what a plain-http page off localhost gets
  {
    const script = "Object.defineProperty(globalThis, 'crypto', { value: {}, configurable: true });"
      + "const bsp = await import(process.argv[1]); const r = await bsp.verifyProof(process.argv[2]);"
      + "console.log(JSON.stringify({ ok: r.ok, unavailable: r.unavailable, reason: r.reason, backends: bsp.backends() }));";
    const out = JSON.parse(execFileSync(process.execPath, ['--input-type=module', '-e', script, 'data:text/javascript;base64,' + Buffer.from(text).toString('base64'), proofs[0]], { encoding: 'utf8' }).trim());
    ok(out.ok === true && out.unavailable === false, 'with no crypto.subtle the proof still verifies: ' + JSON.stringify(out));
    eq(JSON.stringify(out.backends), JSON.stringify({ hashes: 'js', ed25519: 'noble' }), 'and it ran on the file\'s own hashes and the embedded noble');
  }
  // 4. the honest answer: a check that cannot run says so, and is not a failed proof
  {
    const bsp = await load(); bsp.useHashes('js');
    await bsp.useEd25519('noble', { etc: {}, verifyAsync: async () => { throw new Error('no curve here'); } });
    const r = await bsp.verifyProof(proofs[0]);
    ok(!r.ok && r.unavailable === true && /could not check the signature: verification could not run/.test(r.reason || ''), 'a verifier that cannot run reports "could not check", not a bad signature: ' + r.reason);
    ok(r.commitment.unavailable === true, 'the commitment carries the same mark');
  }
  // 5. the embedded noble-ed25519 is the published file: its last line restored, the digest is the recorded one
  {
    const bsp = await load();
    const head = 'const nobleEd25519 = (() => {\n', start = text.indexOf(head) + head.length;
    const end = text.lastIndexOf('})();\n', text.indexOf("export const version"));
    const ret = 'return { CURVE: ed25519_CURVE, etc, ExtendedPoint: Point, getPublicKey, getPublicKeyAsync, Point, sign, signAsync, utils, verify, verifyAsync };\n';
    const body = text.slice(start, end);
    ok(body.endsWith(ret), 'the embedded noble ends with the return that replaced its export');
    const published = body.slice(0, -ret.length) + 'export { ed25519_CURVE as CURVE, etc, Point as ExtendedPoint, getPublicKey, getPublicKeyAsync, Point, sign, signAsync, utils, verify, verifyAsync, };\n';
    eq(createHash('sha256').update(published).digest('hex'), bsp.NOBLE_SHA256, 'the embedded noble-ed25519 ' + bsp.NOBLE_VERSION + ' is the published index.js, byte for byte but its last line');
  }
}

console.log('dashboard/test.mjs: ' + checks + ' checks passed on ' + fs.readdirSync(path.join(here, 'fixtures')).filter((f) => f.endsWith('.json')).join(', ') + ', and bsp.js under every backend');
