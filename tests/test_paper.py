import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

paper = load("trader-paper", "paper")

CHART = os.path.join(FIX, "trader-entry", "TSLA-2026-10-19-5m.json")
DAY = paper.datetime.date(2026, 10, 19)
T0 = paper.ts_at(DAY, "09:30")


def bar(k, o, h, l, c):
    return {"t": T0 + 300 * k, "o": o, "h": h, "l": l, "c": c, "v": 1000}


class Simulate(unittest.TestCase):
    FLAT = paper.ts_at(DAY, "15:55")
    END = paper.ts_at(DAY, "16:10")

    def test_stop_first(self):
        bars = [bar(9, 100, 101, 99.5, 100), bar(10, 100, 100.5, 97.9, 98)]
        self.assertEqual(paper.simulate_exit(bars, T0 + 2700, 98, 103, self.FLAT, self.END), ("stop", 98, T0 + 3000))

    def test_gap_through_the_stop_fills_at_the_open(self):
        bars = [bar(9, 96, 97, 95, 96)]
        self.assertEqual(paper.simulate_exit(bars, T0 + 2700, 98, 103, self.FLAT, self.END)[:2], ("stop", 96))

    def test_target(self):
        bars = [bar(9, 100, 103.2, 99.5, 103)]
        self.assertEqual(paper.simulate_exit(bars, T0 + 2700, 98, 103, self.FLAT, self.END)[:2], ("target", 103))

    def test_both_in_one_bar_counts_as_the_stop(self):
        bars = [bar(9, 100, 104, 97, 101)]
        self.assertEqual(paper.simulate_exit(bars, T0 + 2700, 98, 103, self.FLAT, self.END)[0], "stop")

    def test_bars_before_the_fill_do_not_count(self):
        bars = [bar(8, 100, 104, 97, 101), bar(9, 100, 101, 99, 100.5)]
        self.assertIsNone(paper.simulate_exit(bars, T0 + 2700, 98, 103, self.FLAT, T0 + 3000))

    def test_an_incomplete_bar_does_not_count(self):
        bars = [bar(9, 100, 104, 99, 103.5)]
        self.assertIsNone(paper.simulate_exit(bars, T0 + 2700, 98, 103, self.FLAT, T0 + 2800))

    def test_flat_by_sells_at_the_flat_bar_open(self):
        bars = [bar(k, 100, 101, 99, 100) for k in range(9, 77)] + [bar(77, 100.4, 100.6, 100.1, 100.2)]
        self.assertEqual(paper.simulate_exit(bars, T0 + 2700, 98, 103, self.FLAT, self.END),
                         ("flat", 100.4, self.FLAT))


class Pick(unittest.TestCase):
    AP = dict(paper.AUTOPILOT_DEFAULTS)

    def test_order_recent_first_then_smallest_move(self):
        hits = [{"symbol": "A", "price": 50, "bar_index": 3, "or_high": 1, "or_low": 0, "pct_over": 0.1},
                {"symbol": "B", "price": 50, "bar_index": 8, "or_high": 1, "or_low": 0, "pct_over": 0.9},
                {"symbol": "C", "price": 50, "bar_index": 8, "or_high": 1, "or_low": 0, "pct_over": 0.2},
                {"symbol": "D", "price": 4, "bar_index": 9, "or_high": 1, "or_low": 0, "pct_over": 0.2}]
        self.assertEqual([h["symbol"] for h in paper.order_hits(hits, 10)], ["C", "B", "A"])

    def test_chase(self):
        self.assertIsNone(paper.chase_check(100.5, 100, 98, self.AP)[1])
        self.assertIn("chase", paper.chase_check(101.5, 100, 98, self.AP)[1])
        self.assertIn("range high", paper.chase_check(99.5, 100, 98, self.AP)[1])
        self.assertIn("midpoint", paper.chase_check(98.9, 100, 98, self.AP)[1])


class EndToEnd(unittest.TestCase):
    """The reel's TSLA day, offline: book at 10:22, settle after the close."""

    def setUp(self):
        self.old = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)
        os.makedirs("trader")
        with open(os.path.join(ROOT, "trader-budget", "config.example.json")) as fh:
            cfg = json.load(fh)
        cfg["autopilot"] = dict(cfg.get("autopilot") or {}, timemachine_gate=False)
        self.cfg = cfg
        self._write("trader/config.json", cfg)
        self._write("trader/watchlist.json", {"mode": "orb", "as_of": "2026-10-19 10:22 ET", "hits": [
            {"symbol": "TSLA", "price": 364.18, "bar_index": 8, "or_high": 362.18, "or_low": 354.47, "pct_over": 0.55}]})

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp)

    def _write(self, path, doc):
        with open(path, "w") as fh:
            json.dump(doc, fh)

    def run_paper(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = paper.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_book_then_settle(self):
        code, out, err = self.run_paper("tick", "--skip-scan", "--offline", CHART, "--now", "2026-10-19 10:22")
        self.assertEqual(code, 0, err)
        self.assertIn("BOUGHT (paper, simulated) 7 TSLA at 365.23", out)
        with open("trader/state/broker.json") as fh:
            placed = [r for r in json.load(fh)["orders"] if r["status"] == "placed"]
        self.assertEqual((placed[0]["kind"], placed[0]["max_loss"], placed[0]["stop"]), ("entry", 47.11, 358.32))

        # a second morning run does not buy TSLA twice
        code, out, err = self.run_paper("open", "--skip-scan", "--offline", CHART, "--now", "2026-10-19 10:30")
        self.assertNotIn("BOUGHT", out)

        # the fixture ends at 10:50, so after the close it is sold at the last close, and says so
        code, out, err = self.run_paper("tick", "--skip-scan", "--offline", CHART, "--now", "2026-10-19 16:10")
        self.assertEqual(code, 0, err)
        with open("trader/state/paper.json") as fh:
            st = json.load(fh)
        self.assertEqual(st["positions"], [])
        c = st["closed"][0]
        self.assertTrue(c["exit_reason"].startswith("flat (no bars after"))
        self.assertEqual((c["exit"], c["pnl"]), (366.68, 10.15))
        with open("trader/portfolio.json") as fh:
            self.assertEqual(json.load(fh), {"cash": 5010.15, "positions": []})
        self.assertIn("realized P&L | $+10.15", open("trader/paper/summary.md").read())

    def test_refuses_a_live_config(self):
        self.cfg["paper"] = False
        self._write("trader/config.json", self.cfg)
        code, out, err = self.run_paper("tick", "--skip-scan", "--offline", CHART, "--now", "2026-10-19 10:22")
        self.assertEqual(code, 2)
        self.assertIn("Simulated fills only run on a paper config", err)

    def _live_cfg(self):
        self.cfg["paper"] = False
        self.cfg.update({"budget": 294, "cash_buffer_pct": 0, "max_position_pct": 100, "max_order_usd": 3000,
                         "max_loss_per_trade_usd": 50, "max_trades_per_day": 1})
        self._write("trader/config.json", self.cfg)

    def test_live_writes_one_order_and_books_nothing(self):
        self._live_cfg()
        code, out, err = self.run_paper("open", "--live", "--cash", "3000", "--skip-scan", "--offline", CHART,
                                        "--now", "2026-10-19 10:22", "--portfolio", "trader/live/portfolio.json",
                                        "--broker-state", "trader/live/broker.json", "--state", "trader/live/state.json")
        self.assertEqual(code, 0, err)
        self.assertIn("LIVE ORDER READY (not placed)", out)
        with open("trader/live/order.json") as fh:
            o = json.load(fh)
        self.assertEqual((o["symbol"], o["qty"]), ("TSLA", 7))
        self.assertEqual(o["entry"], {"type": "limit", "limit_price": "365.78", "time_in_force": "gfd"})
        self.assertEqual(o["exit_oco"]["stop_loss_stop_price"], "358.32")
        self.assertFalse(os.path.exists("trader/state/paper.json"))
        self.assertFalse(os.path.exists("trader/paper/journal.md"))

    def test_live_daily_cap_shrinks_then_stops(self):
        self._live_cfg()
        self.cfg.update({"max_loss_per_trade_usd": 6, "max_daily_loss_usd": 12})
        self._write("trader/config.json", self.cfg)
        args = ["open", "--live", "--cash", "3000", "--skip-scan", "--offline", CHART, "--now", "2026-10-19 10:22",
                "--portfolio", "trader/live/portfolio.json", "--broker-state", "trader/live/broker.json",
                "--state", "trader/live/state.json"]
        code, out, err = self.run_paper(*(args + ["--loss-today", "8"]))
        self.assertIn("Max loss this trade: $4.00", out)
        self.assertNotIn("LIVE ORDER READY", out)  # TSLA risks 6.83 a share: no whole share fits $4
        code, out, err = self.run_paper(*(args + ["--loss-today", "11.5"]))
        self.assertEqual(code, 0, err)
        self.assertIn("no new trades today", out)
        self.assertFalse(os.path.exists("trader/live/order.json"))

    def test_live_skips_a_share_it_cannot_afford(self):
        self._live_cfg()
        code, out, err = self.run_paper("open", "--live", "--cash", "294", "--skip-scan", "--offline", CHART,
                                        "--now", "2026-10-19 10:22", "--portfolio", "trader/live/portfolio.json",
                                        "--broker-state", "trader/live/broker.json", "--state", "trader/live/state.json")
        self.assertEqual(code, 0, err)
        self.assertNotIn("LIVE ORDER READY", out)
        self.assertIn("more than the cash", out)
        self.assertFalse(os.path.exists("trader/live/order.json"))

    def test_live_refuses_a_paper_config_and_paper_refuses_live(self):
        code, out, err = self.run_paper("open", "--live", "--cash", "294", "--skip-scan", "--offline", CHART,
                                        "--now", "2026-10-19 10:22")
        self.assertEqual(code, 2)
        self.assertIn("needs a config that says", err)
        code, out, err = self.run_paper("tick", "--live", "--now", "2026-10-19 10:22")
        self.assertEqual(code, 2)

    def test_weekend_does_nothing(self):
        code, out, err = self.run_paper("tick", "--skip-scan", "--offline", CHART, "--now", "2026-10-18 10:22")
        self.assertEqual(code, 0)
        self.assertIn("Market closed", out)


if __name__ == "__main__":
    unittest.main()
