"""The flow table: who paid whom per period, the Steward's take, what each party kept; and the CSV of every row."""

import csv
import os
from collections import OrderedDict
from fractions import Fraction

from .money import fmt, fmt_gb, pct
from .receipts import SOURCE

PARTY_KINDS = ("payment", "commitment", "fee", "remit", "convert", "premium", "bounty", "reimburse", "refund",
               "escrow", "match", "slash", "mint_share")


def periods(run, by=None):
    """[(w0, w1, label)] for the report: by phase (between events), day or window."""
    by = by or run.scn.report
    n = run.windows
    if by == "window":
        return [(w, w, "window %d" % w) for w in range(n)]
    if by == "day":
        per_day = max(1, 86400 // run.window_seconds)
        out = []
        for i, w0 in enumerate(range(0, n, per_day)):
            out.append((w0, min(n - 1, w0 + per_day - 1), "day %d" % (i + 1)))
        return out
    bounds = [0] + run.scn.event_windows() + [n]
    out = []
    for a, b in zip(bounds, bounds[1:]):
        if a >= b:
            continue
        notes = [t.split(": ", 1)[0] for (w, t) in run.events_log
                 if w == a and not t.startswith(("Steward", "Mint", "no link", "bridge")) and ": " in t]
        seen = []
        for t in notes:
            if t not in seen:
                seen.append(t)
        label = "; ".join(seen[:3]) if seen else ("windows %d–%d" % (a, b - 1))
        out.append((a, b - 1, label))
    return out


def day_label(run, w0, w1):
    per_day = 86400 // run.window_seconds if run.window_seconds <= 86400 else None
    if w0 == w1:
        return "window %d" % w0
    if per_day and (w1 - w0 + 1) == per_day and w0 % per_day == 0:
        return "day %d" % (w0 // per_day + 1)
    if per_day and (w1 - w0 + 1) % per_day == 0 and w0 % per_day == 0:
        return "days %d–%d" % (w0 // per_day + 1, w1 // per_day + 1)
    return "windows %d–%d" % (w0, w1)


def summarize(run, w0, w1):
    """Aggregate the rows of a period by from, to, unit and what they were for."""
    groups = OrderedDict()
    for p in run.rows(w0, w1):
        key = (p.from_, p.to, p.unit, p.kind, p.link if p.kind in ("payment", "commitment", "fee", "convert") else None,
               p.commitment if p.kind in ("premium", "bounty", "reimburse", "refund", "escrow", "match", "post") else None)
        g = groups.setdefault(key, {"amount": Fraction(0), "bytes": 0, "n": 0, "reason": p.reason})
        g["amount"] += p.amount
        g["bytes"] += p.nbytes
        g["n"] += 1
    rows = []
    for (frm, to, unit, kind, link, cm), g in groups.items():
        rows.append(dict(from_=frm, to=to, unit=unit, kind=kind, link=link, commitment=cm, amount=g["amount"],
                         bytes=g["bytes"], n=g["n"], what=describe(run, kind, link, cm, g)))
    rows.sort(key=lambda r: (r["kind"] not in ("payment", "commitment"), r["kind"], run.name_of(r["from_"]), run.name_of(r["to"])))
    return rows


def describe(run, kind, link, cm, g):
    f = pct(run.params["f"])
    if kind in ("payment", "commitment"):
        l = run.links.get(link)
        price = fmt(l.price, run.unit_of(l.domain), run.symbol(run.unit_of(l.domain))) if l else "?"
        tail = " from the commitment's escrow" if kind == "commitment" else ""
        return "%s on %s at %s/GB%s, less %s fee" % (fmt_gb(g["bytes"]), link, price, tail, f) if g["bytes"] else g["reason"]
    if kind == "fee":
        return "fee of %s on %s" % (f, link)
    if kind == "remit":
        return "fee remitted to the Steward"
    if kind == "convert":
        return "conversion for the %s hop" % link
    if kind in ("premium", "bounty"):
        c = run.commitments.get(cm)
        return "premium on %s, less %s fee" % (c.label if c else cm, f)
    return g["reason"]


def parties(run):
    out = []
    for n in sorted(run.nodes.values(), key=lambda x: x.id):
        out.append(n.id)
    for d in sorted(run.domains):
        out.append("steward:" + d)
        if run.domains[d].mint is None:
            out.append("mint:" + d)
    return out


def kept_lines(run, w0, w1):
    lines = []
    units = sorted({p.unit for p in run.rows(w0, w1)})
    for acct in parties(run):
        parts = []
        for u in units:
            r = run.received(acct, w0, w1, u)
            p = run.paid(acct, w0, w1, u)
            if r == 0 and p == 0:
                continue
            sym = run.symbol(u)
            if p == 0:
                parts.append("+%s" % fmt(r, u, sym))
            elif r == 0:
                parts.append("−%s" % fmt(p, u, sym))
            else:
                net = r - p
                parts.append("%s%s (in %s, out %s)" % ("+" if net >= 0 else "−", fmt(abs(net), u, sym), fmt(r, u, sym), fmt(p, u, sym)))
        if parts:
            lines.append("%-26s %s" % (run.name_of(acct), " · ".join(parts)))
    return lines


def steward_lines(run, w0, w1):
    lines = []
    for d in sorted(run.domains):
        s = run.stewards[d]
        acct = s.account
        u, sym = s.unit, run.symbol(s.unit)
        took = run.received(acct, w0, w1, u, kinds=("remit",))
        matched = run.paid(acct, w0, w1, u, kinds=("match",))
        posted = run.paid(acct, w0, w1, u, kinds=("post",))
        back = run.received(acct, w0, w1, u, kinds=("refund",))
        if took == 0 and matched == 0 and posted == 0 and back == 0 and len(run.domains) == 1 and s.escrow == 0:
            lines.append("%s: nothing this period" % run.name_of(acct))
            continue
        bits = ["took %s" % fmt(took, u, sym)]
        if matched:
            bits.append("matched %s" % fmt(matched, u, sym))
        if posted:
            bits.append("posted %s in bounties" % fmt(posted, u, sym))
        if back:
            bits.append("%s came back" % fmt(back, u, sym))
        lines.append("%s: %s" % (run.name_of(acct), " · ".join(bits)))
    return lines


def per_gb_lines(run, w0, w1):
    lines = []
    units = sorted({p.unit for p in run.rows(w0, w1)})
    for dem in sorted(run.demands.values(), key=lambda d: d.id):
        g = run.gb_delivered(dem.id, w0, w1)
        if g == 0:
            continue
        done = [x for x in run.delivered if x[1] == dem.id and w0 <= x[0] <= w1]
        paths = sorted({tuple(x[3]) for x in done})
        keys = {x[5] for x in done}
        rows = [p for p in run.payments if p.bundle in keys]
        payer = dem.payer
        bits = []
        for u in units:
            paid = sum(p.amount for p in rows if p.from_ == payer and p.unit == u
                       and p.kind in ("payment", "fee", "convert", "mint_share"))
            if paid:
                bits.append("%s/GB" % fmt(paid / g, u, run.symbol(u)))
        lines.append("%s (%s): %s delivered over %s; %s pays %s" % (
            dem.label or dem.id, dem.id, fmt_gb(int(g * 10 ** 9)), " or ".join("→".join(p) for p in paths) or "no path",
            run.name_of(payer), " + ".join(bits) or "nothing"))
        relays = sorted({run.links[l].owner for p in paths for l in p if run.links[l].owner != payer})
        for r in relays:
            parts, conv = [], []
            for u in units:
                inn = sum(p.amount for p in rows if p.to == r and p.unit == u and p.kind != "convert")
                out = sum(p.amount for p in rows if p.from_ == r and p.unit == u and p.kind != "convert")
                if inn or out:
                    parts.append("%s/GB" % fmt((inn - out) / g, u, run.symbol(u)))
                cin = sum(p.amount for p in rows if p.to == r and p.unit == u and p.kind == "convert")
                if cin:
                    conv.append("%s/GB" % fmt(cin / g, u, run.symbol(u)))
            if parts or conv:
                lines.append("    %s keeps %s%s" % (run.name_of(r), " and ".join(parts) or "nothing",
                                                   ("; converts " + " and ".join(conv) + " for the hops beyond it") if conv else ""))
        for d in sorted(run.domains):
            u = run.unit_of(d)
            take = sum(p.amount for p in rows if p.to == "steward:" + d and p.kind == "remit")
            if take:
                lines.append("    %s takes %s/GB" % (run.name_of("steward:" + d), fmt(take / g, u, run.symbol(u))))
    return lines


def window_rows(run, w):
    lines = []
    for p in run.payments:
        if p.window != w:
            continue
        lines.append("    %-30s → %-30s %12s  %s" % (run.name_of(p.from_)[:30], run.name_of(p.to)[:30],
                                                     fmt(p.amount, p.unit, run.symbol(p.unit)), p.reason))
    return lines


def header(run):
    p = run.params
    lines = [run.name, ""]
    if run.scn.description:
        lines += [run.scn.description, ""]
    lines.append("%d windows of %d s · f = %s · m = %s · payout cap %s of a payer's fees · program window %d · reconcile every %d · receipts: %s" % (
        run.windows, run.window_seconds, pct(p["f"]), pct(p["m"]), pct(p["f"] - p["steward_margin"]), p["program_window"],
        p["reconcile_every"], SOURCE))
    return lines


def render(run, by=None, detail=None, log=False):
    out = header(run)
    detail = set(detail if detail is not None else run.scn.detail_windows)
    for (w0, w1, label) in periods(run, by):
        out.append("")
        out.append("== %s · %s" % (day_label(run, w0, w1), label))
        notes = [(w, t) for (w, t) in run.events_log if w0 <= w <= w1]
        shown = [t for (w, t) in notes if log or not t.startswith(("Mints ", "no link"))]
        for t in shown[:12 if not log else len(shown)]:
            out.append("   · " + t)
        if len(shown) > 12 and not log:
            out.append("   · … %d more lines (--log prints them)" % (len(shown) - 12))
        rows = summarize(run, w0, w1)
        if rows:
            out.append("   %-30s %-30s %12s  %s" % ("from", "to", "amount", "for"))
            for r in rows:
                out.append("   %-30s %-30s %12s  %s" % (run.name_of(r["from_"])[:30], run.name_of(r["to"])[:30],
                                                        fmt(r["amount"], r["unit"], run.symbol(r["unit"])), r["what"]))
        else:
            out.append("   no payments")
        unserved = [(w, d, n, why) for (w, d, n, why) in run.unserved if w0 <= w <= w1]
        if unserved:
            tot = sum(n for (_, _, n, _) in unserved)
            out.append("   nothing moved for %s in %d demand-windows (%s)" % (fmt_gb(tot), len(unserved), sorted({why for (_, _, _, why) in unserved})[0]))
        declined = [x for x in run.declined if w0 <= x[0] <= w1]
        if declined:
            out.append("   %d window%s declined for the clock rule, nothing paid" % (len(declined), "" if len(declined) == 1 else "s"))
        for line in steward_lines(run, w0, w1):
            out.append("   " + line)
        kept = kept_lines(run, w0, w1)
        if kept:
            out.append("   kept this period:")
            for line in kept:
                out.append("      " + line)
        if run.scn.per_gb:
            pg = per_gb_lines(run, w0, w1)
            if pg:
                out.append("   per gigabyte delivered:")
                for line in pg:
                    out.append("      " + line)
        for w in sorted(d for d in detail if w0 <= d <= w1):
            out.append("   window %d, row by row:" % w)
            rows_w = window_rows(run, w)
            out.extend(rows_w or ["    no payments"])
    out.append("")
    out.append("== Over the run")
    for line in kept_lines(run, 0, run.windows - 1):
        out.append("   " + line)
    for d in sorted(run.domains):
        s = run.stewards[d]
        out.append("   %s: income %s · matched %s · posted %s · back %s · escrow now %s" % (
            run.name_of(s.account), fmt(s.received, s.unit, run.symbol(s.unit)), fmt(s.matched, s.unit, run.symbol(s.unit)),
            fmt(s.posted, s.unit, run.symbol(s.unit)), fmt(s.refunds, s.unit, run.symbol(s.unit)), fmt(s.escrow, s.unit, run.symbol(s.unit))))
        if s.capped:
            out.append("   %s: the per-payer cap held back %s of payouts (f − margin = %s of each payer's fees in)" % (
                run.name_of(s.account), fmt(s.capped, s.unit, run.symbol(s.unit)), pct(run.params["f"] - run.params["steward_margin"])))
        m = run.mints[d]
        if m.kept:
            out.append("   %s kept %s of the fees it owed its Steward" % (run.name_of(m.account), fmt(m.kept, m.unit, run.symbol(m.unit))))
    if run.commitments:
        out.append("   commitments:")
        for cm in sorted(run.commitments.values(), key=lambda c: (c.posted_at, c.id)):
            out.append("      %-36s %-13s posted w%-5d escrow %10s  paid out %10s  back %10s  pool %10s  %d/%d windows" % (
                cm.label[:36], cm.status, cm.posted_at, fmt(cm.escrow, cm.unit, run.symbol(cm.unit)),
                fmt(cm.paid_out, cm.unit, run.symbol(cm.unit)), fmt(cm.refunded, cm.unit, run.symbol(cm.unit)),
                fmt(cm.pool, cm.unit, run.symbol(cm.unit)), len(cm.served), cm.windows))
    if run.excluded:
        for (by, of), (w, short) in sorted(run.excluded.items()):
            out.append("   Mint %s excluded Mint %s at window %d (shortfall %s)" % (
                run.mints[by].account, run.mints[of].account, w, fmt(short, run.unit_of(of), run.symbol(run.unit_of(of)))))
    if run.in_custody_at_end:
        tot = sum(n for (_, _, n, _) in run.in_custody_at_end)
        out.append("   in custody at the end of the run: %s in %d bundle%s" % (fmt_gb(tot), len(run.in_custody_at_end), "" if len(run.in_custody_at_end) == 1 else "s"))
    if run.max_holding:
        out.append("   longest a bundle waited at a relay: %d window%s" % (run.max_holding, "" if run.max_holding == 1 else "s"))
    out.append("   %d payment rows · %d settlements (%d disputed, %d declined)" % (
        len(run.payments), len(run.settlements), sum(1 for s in run.settlements if s.status == "disputed"),
        sum(1 for s in run.settlements if s.status == "declined")))
    return "\n".join(out)


def write_csv(run, path):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["from", "to", "amount", "unit", "window", "reason"])
        for p in run.payments:
            wr.writerow(p.row())
    return path
