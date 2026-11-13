#!/usr/bin/env python3
"""Paper autopilot for trader-paper: the whole pipeline on a schedule, with
SIMULATED fills. Standard library only. Never talks to a broker.

    python3 paper.py tick                     # what the clock calls for: open, settle, or nothing
    python3 paper.py open                     # scan, pick, plan, size, risk-check, simulate the buy
    python3 paper.py settle                   # walk the 5-minute bars: stop, target or flat by the close
    python3 paper.py report                   # equity, P&L, win rate, average R
    python3 paper.py tick --now "2026-10-19 10:22" --offline DIR --skip-scan   # pinned, no network

Why simulated: trader-broker never places an order from a scheduled task,
because nobody is there to say yes. This tool keeps that rule. It runs the
same scripts a person would (scan.py orb, exit.py plan, timemachine.py run,
size.py, check.py), and where a person would send the order, it books a
simulated fill instead:

  entry   the close of the last completed 5-minute bar, plus slippage_bps
  stop    the first bar whose low reaches the stop fills at the stop, or at the
          bar's open if it opened through it (a gap fills worse)
  target  the first bar whose high reaches the target fills at the target
          (or the open, if it opened above it)
  both    a bar that touches the stop and the target is counted as the stop
  flat    still open at flat_by (15:55): sold at the open of that bar

The fills go into the same files trader-risk reads (trader/state/broker.json,
trader/portfolio.json), so the daily loss limit, trades per day and the cash
buffer all hold for the autopilot exactly as they would for you. Positions and
closed trades are in trader/state/paper.json; a readable journal in
trader/paper/journal.md.

Picking among the scan's hits (trader/config.json, "autopilot" block):
  price >= min_price, last close over the range high by no more than
  max_chase_pct (a late entry is a different trade), then the smallest chase
  first; with timemachine_gate on, the Time Machine's average R must be over
  min_tm_avg_r. The first candidates that pass trader-risk are booked, up to
  max_trades_per_day.

Exit code 0 unless something could not run.
"""

import argparse
import datetime
import importlib.util
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

AUTOPILOT_DEFAULTS = {
    "min_price": 10.0,
    "max_chase_pct": 1.0,
    "max_candidates": 12,
    "timemachine_gate": True,
    "min_tm_avg_r": 0.0,
    "slippage_bps": 5,
    "stop": "or-mid",
    "target": "1R",
    "scan_max_confirm": 200,
    "live_min_leg_pct": 0.4,
    "live_limit_bps": 20,
}

BAR = 300  # seconds in a 5-minute bar


def _load(folder, name):
    path = os.path.join(ROOT, folder, name + ".py")
    spec = importlib.util.spec_from_file_location("paper_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


entry_mod = _load("trader-entry", "entry")
ticket_mod = _load("trader-broker", "ticket")
risk_mod = _load("trader-risk", "check")


class PaperError(Exception):
    pass


# ---------------------------------------------------------------- files

def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def load_config(path, live=False):
    cfg = read_json(path)
    if cfg is None:
        raise PaperError("no %s: copy trader-budget/config.example.json there and set your limits" % path)
    if live:
        if cfg.get("paper") is not False:
            raise PaperError("--live needs a config that says \"paper\": false (trader/live-config.json), not %s" % path)
    elif cfg.get("paper") is not True:
        raise PaperError("%s does not say \"paper\": true. Simulated fills only run on a paper config; "
                         "real orders go through open --live." % path)
    ap = dict(AUTOPILOT_DEFAULTS)
    ap.update(cfg.get("autopilot") or {})
    return cfg, ap


def load_state(path):
    st = read_json(path, {}) or {}
    st.setdefault("positions", [])
    st.setdefault("closed", [])
    st.setdefault("skipped", [])
    return st


# ---------------------------------------------------------------- clock

def now_utc(text=None):
    return entry_mod.parse_now(text) if text else entry_mod.utc_now()


def et(utc):
    return entry_mod.to_eastern(utc)


def ts_at(day, hhmm):
    """UTC epoch seconds of HH:MM New York time on day."""
    h, m = (int(x) for x in hhmm.split(":"))
    local = datetime.datetime(day.year, day.month, day.day, h, m)
    return int(entry_mod.eastern_to_utc(local).replace(tzinfo=datetime.timezone.utc).timestamp())


def ampm_of(ts):
    return et(entry_mod.utc_of(ts)).strftime("%I:%M %p").lstrip("0")


def utc_ts(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp())


# ---------------------------------------------------------------- the simulation

def session_bars(bars, day):
    """Complete-grid 5-minute bars of one New York session, in time order."""
    open_ts, close_ts = ts_at(day, "09:30"), ts_at(day, "16:00")
    return sorted((b for b in bars if open_ts <= b["t"] < close_ts and (b["t"] - open_ts) % BAR == 0),
                  key=lambda b: b["t"])


def simulate_exit(bars, after_ts, stop, target, flat_ts, now_ts):
    """First exit after a fill at after_ts (the entry bar's close). Only bars
    complete by now_ts count. Returns (reason, price, bar_ts) or None if still open."""
    last = None
    for b in bars:
        if b["t"] < after_ts:
            continue
        if b["t"] >= flat_ts:
            return ("flat", round(b["o"], 2), b["t"])
        if b["t"] + BAR > now_ts:
            break
        last = b
        if b["l"] <= stop:
            return ("stop", round(min(stop, b["o"]), 2), b["t"])
        if b["h"] >= target:
            return ("target", round(max(target, b["o"]), 2), b["t"])
    if now_ts >= flat_ts + BAR and last is not None:
        # the flat-by bar never printed (halt, early close data gap): out at the last close
        return ("flat (no bars after %s)" % ampm_of(last["t"] + BAR), round(last["c"], 2), last["t"] + BAR)
    return None


def order_hits(hits, min_price):
    """Most recent triggers first (the least time to run away), then the
    smallest move over the range high at the trigger."""
    ok = [h for h in hits if h.get("symbol") and h.get("or_high") is not None and h.get("or_low") is not None
          and (h.get("price") or 0) >= min_price]
    return sorted(ok, key=lambda h: (-(h.get("bar_index") or 0), h.get("pct_over") or 0))


def chase_check(last, or_high, or_low, ap):
    """None if a fill at last is still the breakout trade, else why not."""
    chase = 100.0 * (last / or_high - 1)
    if last < ap["min_price"]:
        return chase, "price %.2f under min_price %.2f" % (last, ap["min_price"])
    if last <= (or_high + or_low) / 2:
        return chase, "back under the range midpoint"
    if chase < 0:
        return chase, "back under the range high"
    if chase > ap["max_chase_pct"]:
        return chase, "chase %.2f%% over the range high (max %.2f%%)" % (chase, ap["max_chase_pct"])
    return chase, None


# ---------------------------------------------------------------- running the other skills

def run(args, offline=None, now=None, check=True):
    cmd = [sys.executable] + args
    if now:
        cmd += ["--now", now]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode not in (0, 1):
        raise PaperError("%s failed (%d): %s" % (" ".join(args[:2]), p.returncode, (p.stderr or p.stdout).strip()[-400:]))
    return p


def skill(folder, script):
    return os.path.join(ROOT, folder, script)


def chart(sym, day, offline):
    ch = entry_mod.get_chart(sym, "5m", day=day, offline=offline, today=day)
    return session_bars(ch["bars"], day)


# ---------------------------------------------------------------- commands

def cmd_open(a, cfg, ap, now):
    nt = et(now)
    day = nt.date()
    is_open, why = risk_mod.trading_day(day)
    if not is_open:
        print("Market closed today (%s, %s). Nothing to do." % (day, why))
        return 0
    if a.live:
        if a.cash is None or a.cash <= 0:
            raise PaperError("--live needs --cash: the account's buying power from Robinhood")
        write_json(a.portfolio, {"cash": round(float(a.cash), 2), "positions": []})
        if os.path.exists(a.live_out):
            os.remove(a.live_out)
        # Each live run starts from a fresh checkout, so check.py cannot see today's earlier
        # trades: the session passes today's realized loss from Robinhood, and this trade may
        # risk only what is left of the daily cap.
        loss = max(0.0, float(a.loss_today or 0))
        daily = float(cfg.get("max_daily_loss_usd") or 0)
        per = float(cfg.get("max_loss_per_trade_usd") or 0)
        room = round(min(per, daily - loss), 2) if daily else per
        if room < 1.0:
            print("Today's realized loss $%.2f leaves $%.2f of the $%.2f daily cap: no new trades today."
                  % (loss, max(room, 0), daily))
            return 0
        run_cfg = dict(cfg, max_loss_per_trade_usd=room, max_daily_loss_usd=room)
        a.config = os.path.join(os.path.dirname(a.live_out) or ".", "config.run.json")
        write_json(a.config, run_cfg)
        if room < per:
            print("Max loss this trade: $%.2f (today's loss $%.2f against the $%.2f daily cap)." % (room, loss, daily))
    state = load_state(a.state)
    broker = ticket_mod.load_state(a.broker_state)
    today = day.isoformat()
    entries = [r for r in risk_mod.placed_today(broker, today) if risk_mod.rec_is_entry(r)]
    slots = int(cfg.get("max_trades_per_day", 3)) - len(entries)
    if slots <= 0:
        print("Already %d entries today: the max_trades_per_day limit. No new trades." % len(entries))
        return 0
    held = {p["symbol"] for p in state["positions"]}
    done_today = {r["symbol"] for r in entries}

    if not a.skip_scan:
        if not os.path.exists("trader/universe.txt"):
            print("Building the symbol universe (Nasdaq Trader)...")
            p = run([skill("trader-scanner", "scan.py"), "universe"], check=False)
            if p.returncode != 0 or not os.path.exists("trader/universe.txt"):
                raise PaperError("scan.py universe failed: %s" % (p.stderr or p.stdout).strip()[-400:])
        print("Scanning for opening-range breakouts...")
        p = run([skill("trader-scanner", "scan.py"), "orb", "--quiet", "--show", "0",
                 "--max-confirm", str(ap["scan_max_confirm"]), "--config", a.config]
                + (["--offline", a.offline] if a.offline else []), now=a.now)
        tail = [l for l in (p.stdout + p.stderr).splitlines() if l.strip()][-3:]
        print("  " + "\n  ".join(tail))
    wl = read_json("trader/watchlist.json", {}) or {}
    if wl.get("mode") != "orb" or str(wl.get("as_of", ""))[:10] not in (today, ""):
        if a.skip_scan:
            print("No ORB watchlist for today. Nothing to do.")
            return 0
        # the scan ran but wrote nothing for today: a failure, not a quiet day
        raise PaperError("the ORB scan wrote no watchlist for %s: %s" % (today, " / ".join(tail) or "no output"))
    hits = [h for h in wl.get("hits", []) if h.get("symbol") not in held | done_today]

    ranked = order_hits(hits, ap["min_price"])[: int(ap["max_candidates"])]
    skipped = []
    print("%d hits; checking up to %d for %d slot(s)." % (len(hits), len(ranked), slots))

    booked = 0
    for c in ranked:
        if booked >= slots:
            break
        sym = c["symbol"]
        try:
            bars = chart(sym, day, a.offline)
        except Exception as e:  # one bad chart never stops the run
            skipped.append({"symbol": sym, "why": "chart: %s" % e})
            continue
        now_ts = utc_ts(now)
        done = [b for b in bars if b["t"] + BAR <= now_ts]
        if not done:
            continue
        lb = done[-1]
        k = (lb["t"] - ts_at(day, "09:30")) // BAR
        fill_ts = lb["t"] + BAR
        ref = round(lb["c"], 2)
        chase, why = chase_check(ref, c["or_high"], c["or_low"], ap)
        if why:
            skipped.append({"symbol": sym, "why": why})
            continue

        p = run([skill("trader-entry", "entry.py"), "check", sym, "--write", "--config", a.config]
                + (["--offline", a.offline] if a.offline else []), now=a.now)
        doc = read_json("trader/entry.json", {}) or {}
        if doc.get("symbol") != sym or doc.get("status") != "triggered" or doc.get("date") != today:
            skipped.append({"symbol": sym, "why": "entry timer says %s" % (doc.get("status") or "no entry")})
            continue
        p = run([skill("trader-exit", "exit.py"), "plan", "--from", "trader/entry.json", "--entry", str(ref),
                 "--entry-bar", str(k), "--time", ampm_of(fill_ts), "--stop", ap["stop"], "--target", ap["target"],
                 "--config", a.config, "--write"], now=a.now)
        if p.returncode != 0:
            skipped.append({"symbol": sym, "why": "exit plan: %s" % (p.stderr or p.stdout).strip()[-200:]})
            continue
        plan = read_json("trader/plan.json")
        if a.live:
            leg = float(ap["live_min_leg_pct"])
            near = min(ref - plan["stop"], plan["target"] - ref) / ref * 100.0
            if near < leg:
                skipped.append({"symbol": sym, "why": "stop or target %.2f%% from the price (Robinhood OCO wants %.2f%%)" % (near, leg)})
                continue
            if ref > a.cash:
                skipped.append({"symbol": sym, "why": "one share %.2f is more than the cash %.2f" % (ref, a.cash)})
                continue

        tm = None
        if ap["timemachine_gate"]:
            p = run([skill("trader-timemachine", "timemachine.py"), "run", "--json", "--write"], now=a.now, check=False)
            try:
                tmdoc = json.loads(p.stdout)
                tm = {"avg_R": tmdoc["mc"]["avg_R"], "p_target": tmdoc["mc"]["p_target"],
                      "p_stop": tmdoc["mc"]["p_stop"], "verdict": tmdoc.get("verdict")}
            except (ValueError, KeyError, TypeError):
                skipped.append({"symbol": sym, "why": "time machine could not run"})
                continue
            if tm["avg_R"] <= ap["min_tm_avg_r"]:
                skipped.append({"symbol": sym, "why": "time machine avg R %.3f <= %.3f" % (tm["avg_R"], ap["min_tm_avg_r"])})
                continue

        ledger = ["--config", a.config, "--portfolio", a.portfolio]
        sizing = ["--budget", "%.2f" % a.cash] if a.live else []
        p = run([skill("trader-budget", "size.py")] + ledger + sizing + ["--write"], now=a.now)
        orders = (read_json("trader/orders.json", {}) or {}).get("orders") or []
        if p.returncode != 0 or not orders:
            q = run([skill("trader-budget", "size.py")] + ledger + sizing + ["--json"], now=a.now, check=False)
            try:
                why = json.loads(q.stdout)["sizing"].get("refusal") or "no order"
            except (ValueError, KeyError, TypeError):
                why = (p.stderr or p.stdout).strip()[-200:] or "no order"
            skipped.append({"symbol": sym, "why": "budget: %s" % why})
            continue
        p = run([skill("trader-risk", "check.py")] + ledger + ["--state", a.broker_state, "--json"], now=a.now)
        verdict = json.loads(p.stdout)["results"][0]
        if verdict["verdict"] != "PASS":
            skipped.append({"symbol": sym, "why": "risk BLOCK: %s" % "; ".join(verdict["reasons"])})
            continue

        order = orders[0]
        qty = int(order["qty"])
        if a.live:
            limit = round(ref * (1 + float(ap["live_limit_bps"]) / 10000.0), 2)
            live = {"created_by": "trader-paper", "created_at": entry_mod.iso(now), "date": today, "symbol": sym,
                    "qty": qty, "entry": {"type": "limit", "limit_price": "%.2f" % limit, "time_in_force": "gfd"},
                    "exit_oco": {"side": "sell", "stop_loss_stop_price": "%.2f" % plan["stop"],
                                 "take_profit_limit_price": "%.2f" % plan["target"], "time_in_force": "gfd"},
                    "ref_price": ref, "risk_per_share": plan["risk_per_share"], "max_loss_at_stop": verdict["max_loss"],
                    "chase_pct": round(chase, 3), "timemachine": tm, "flat_by": plan.get("flat_by"),
                    "why": order.get("why"), "passed_over": skipped[:10]}
            write_json(a.live_out, live)
            print("LIVE ORDER READY (not placed): buy %d %s, limit %.2f; then sell OCO stop %.2f / target %.2f; max loss at the stop $%.2f"
                  % (qty, sym, limit, plan["stop"], plan["target"], verdict["max_loss"]))
            print("Wrote %s. The session places it through Robinhood (trader-paper/LIVE.md)." % a.live_out)
            return 0
        fill = round(ref * (1 + float(ap["slippage_bps"]) / 10000.0), 2)
        nyiso = ticket_mod.ny_iso(now)
        broker = ticket_mod.load_state(a.broker_state)
        broker.setdefault("orders", []).append({
            "at": nyiso, "date": today, "index": 1, "fingerprint": ticket_mod.fingerprint(order), "symbol": sym,
            "side": "buy", "plain": ticket_mod.plain(order), "status": "placed", "qty": order["qty"],
            "kind": "entry", "max_loss": verdict["max_loss"], "stop": verdict["stop"],
            "broker_order_id": "paper-sim", "note": "simulated fill by trader-paper, no broker"})
        ticket_mod.write_json(a.broker_state, broker)
        ticket_mod.apply_fill(a.portfolio, cfg, sym, "buy", qty, fill, today)
        ticket_mod.log_fill(a.broker_state, now, today, sym, "buy", qty, fill)

        pos = {"id": "%s-%s" % (today, sym), "symbol": sym, "date": today, "qty": qty, "fill": fill, "ref": ref,
               "fill_ts": fill_ts, "fill_time": ampm_of(fill_ts), "stop": plan["stop"], "target": plan["target"],
               "risk_per_share": plan["risk_per_share"], "max_loss": verdict["max_loss"],
               "flat_by": plan.get("flat_by") or cfg.get("flat_by", "15:55"),
               "opening_range": plan.get("opening_range"), "chase_pct": round(chase, 3), "timemachine": tm}
        state["positions"].append(pos)
        booked += 1
        print("BOUGHT (paper, simulated) %d %s at %.2f (bar close %.2f + %s bps), stop %.2f, target %.2f, max loss $%.2f"
              % (qty, sym, fill, ref, ap["slippage_bps"], plan["stop"], plan["target"], verdict["max_loss"]))
        journal(a.journal, "%s %s  BUY %d %s @ %.2f  stop %.2f  target %.2f  max loss $%.2f%s"
                % (today, pos["fill_time"], qty, sym, fill, plan["stop"], plan["target"], verdict["max_loss"],
                   ("  (time machine avg R %.3f)" % tm["avg_R"]) if tm else ""))

    state["skipped"] = [dict(s, date=today) for s in skipped[-50:]]
    write_json(a.state, state)
    if skipped:
        print("Passed over:")
        for s in skipped[:8]:
            print("  %-6s %s" % (s["symbol"], s["why"]))
    return 0


def cmd_settle(a, cfg, ap, now):
    state = load_state(a.state)
    if not state["positions"]:
        print("No open paper positions.")
        return 0
    now_ts = utc_ts(now)
    still = []
    for pos in state["positions"]:
        day = datetime.date.fromisoformat(pos["date"])
        flat_ts = ts_at(day, pos["flat_by"])
        try:
            bars = chart(pos["symbol"], day, a.offline)
        except Exception as e:
            print("%s: no chart (%s); still open." % (pos["symbol"], e))
            still.append(pos)
            continue
        out = simulate_exit(bars, pos["fill_ts"], pos["stop"], pos["target"], flat_ts, now_ts)
        if out is None:
            last = [b for b in bars if b["t"] + BAR <= now_ts]
            print("%s open: %d @ %.2f, last %.2f, stop %.2f, target %.2f"
                  % (pos["symbol"], pos["qty"], pos["fill"], last[-1]["c"] if last else pos["fill"], pos["stop"], pos["target"]))
            still.append(pos)
            continue
        reason, price, bts = out
        pnl = round((price - pos["fill"]) * pos["qty"], 2)
        r = round((price - pos["fill"]) / pos["risk_per_share"], 3) if pos["risk_per_share"] else None
        when = entry_mod.utc_of(bts)
        ticket_mod.apply_fill(a.portfolio, cfg, pos["symbol"], "sell", pos["qty"], price, pos["date"])
        ticket_mod.log_fill(a.broker_state, when, pos["date"], pos["symbol"], "sell", pos["qty"], price)
        closed = dict(pos, exit=price, exit_reason=reason, exit_time=ampm_of(bts), pnl=pnl, R=r)
        state["closed"].append(closed)
        print("SOLD (paper, simulated) %d %s at %.2f: %s, P&L $%+.2f (%+.2fR)"
              % (pos["qty"], pos["symbol"], price, reason, pnl, r or 0))
        journal(a.journal, "%s %s  SELL %d %s @ %.2f  %s  P&L $%+.2f  %+.2fR"
                % (pos["date"], closed["exit_time"], pos["qty"], pos["symbol"], price, reason, pnl, r or 0))
    state["positions"] = still
    write_json(a.state, state)
    return 0


def summary(state, portfolio, cfg):
    closed = state["closed"]
    pnl = round(sum(c["pnl"] for c in closed), 2)
    wins = [c for c in closed if c["pnl"] > 0]
    rs = [c["R"] for c in closed if c.get("R") is not None]
    by_day = {}
    for c in closed:
        d = by_day.setdefault(c["date"], {"trades": 0, "pnl": 0.0})
        d["trades"] += 1
        d["pnl"] = round(d["pnl"] + c["pnl"], 2)
    return {
        "start": float(cfg.get("budget") or 0), "cash": (portfolio or {}).get("cash"),
        "open_positions": len(state["positions"]), "trades": len(closed), "wins": len(wins),
        "win_rate": round(len(wins) / len(closed), 3) if closed else None,
        "realized_pnl": pnl, "avg_R": round(sum(rs) / len(rs), 3) if rs else None,
        "by_exit": {k: sum(1 for c in closed if c["exit_reason"].split()[0] == k) for k in ("target", "stop", "flat")},
        "by_day": by_day,
    }


def cmd_report(a, cfg, ap, now):
    state = load_state(a.state)
    s = summary(state, read_json(a.portfolio), cfg)
    if a.json:
        print(json.dumps(s, indent=2))
        return 0
    lines = ["# Paper autopilot", "",
             "Simulated fills from real 5-minute bars. No broker, no real money. Not financial advice.", "",
             "| | |", "| --- | --- |",
             "| starting budget | $%.2f |" % s["start"],
             "| cash now | %s |" % ("$%.2f" % s["cash"] if s["cash"] is not None else "n/a"),
             "| open positions | %d |" % s["open_positions"],
             "| closed trades | %d |" % s["trades"],
             "| win rate | %s |" % ("%.1f %%" % (100 * s["win_rate"]) if s["win_rate"] is not None else "n/a"),
             "| realized P&L | $%+.2f |" % s["realized_pnl"],
             "| average R | %s |" % ("%+.3f" % s["avg_R"] if s["avg_R"] is not None else "n/a"),
             "| exits | %d target, %d stop, %d flat by the close |" % (s["by_exit"]["target"], s["by_exit"]["stop"], s["by_exit"]["flat"]),
             "", "## By day", "", "| day | trades | P&L |", "| --- | --- | --- |"]
    for d in sorted(s["by_day"], reverse=True):
        lines.append("| %s | %d | $%+.2f |" % (d, s["by_day"][d]["trades"], s["by_day"][d]["pnl"]))
    text = "\n".join(lines) + "\n"
    print(text)
    if not a.no_write:
        os.makedirs(os.path.dirname(a.summary), exist_ok=True)
        with open(a.summary, "w", encoding="utf-8") as fh:
            fh.write(text)
    return 0


def cmd_tick(a, cfg, ap, now):
    nt = et(now)
    is_open, why = risk_mod.trading_day(nt.date())
    if not is_open:
        print("Market closed today (%s, %s). Nothing to do." % (nt.date(), why))
        return 0
    hhmm = nt.strftime("%H:%M")
    start = max(cfg.get("no_entries_before", "09:45"), "09:50")
    stop_new = cfg.get("no_entries_after", "15:30")
    flat_by = cfg.get("flat_by", "15:55")
    rc = cmd_settle(a, cfg, ap, now)
    if start <= hhmm <= stop_new:
        rc = cmd_open(a, cfg, ap, now) or rc
    elif hhmm >= flat_by:
        a.json = False
        rc = cmd_report(a, cfg, ap, now) or rc
    else:
        print("%s ET: outside the entry window (%s to %s)." % (hhmm, start, stop_new))
    return rc


def journal(path, line):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as fh:
        if new:
            fh.write("# Paper trading journal (simulated fills, no broker)\n\n")
        fh.write("- " + line + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Paper autopilot: scheduled pipeline, simulated fills.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("tick", "open", "settle", "report"):
        p = sub.add_parser(name)
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--state", default="trader/state/paper.json")
        p.add_argument("--broker-state", default="trader/state/broker.json")
        p.add_argument("--portfolio", default="trader/portfolio.json")
        p.add_argument("--journal", default="trader/paper/journal.md")
        p.add_argument("--summary", default="trader/paper/summary.md")
        p.add_argument("--now", help='pretend it is this New York time, e.g. "2026-10-19 10:22"')
        p.add_argument("--offline", metavar="DIR", help="folder of saved Yahoo charts (<T>.json), no network")
        p.add_argument("--skip-scan", action="store_true", help="use the trader/watchlist.json already there")
        p.add_argument("--json", action="store_true")
        p.add_argument("--no-write", action="store_true")
        p.add_argument("--live", action="store_true",
                       help="open only: write ONE real order to --live-out for the session to place; no simulated fill")
        p.add_argument("--cash", type=float, help="--live: the account's cash right now")
        p.add_argument("--loss-today", type=float, default=0.0,
                       help="--live: today's realized loss in dollars (positive), from Robinhood's filled orders")
        p.add_argument("--live-out", default="trader/live/order.json")
    a = ap.parse_args(argv)
    try:
        if a.live and a.cmd != "open":
            raise PaperError("--live only goes with open")
        cfg, auto = load_config(a.config, live=a.live)
        now = now_utc(a.now)
        return {"tick": cmd_tick, "open": cmd_open, "settle": cmd_settle, "report": cmd_report}[a.cmd](a, cfg, auto, now)
    except (PaperError, entry_mod.EntryError, ticket_mod.TicketError, risk_mod.RiskError) as e:
        print("paper: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
