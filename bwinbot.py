"""Read odds, track movement, build ~8.0 three-leg slips (legs near 2.0) and grade them.

Read-only: this never places bets.

    python bwinbot.py books  --api                         # which bookmakers does the feed cover?
    python bwinbot.py slip   --api --bookmaker bwin --hours 48 --min-edge 0 --save
    python bwinbot.py track  --api --bookmaker bwin        # snapshot + odds movement
    python bwinbot.py settle --api                         # grade saved slips (run within 3 days)
    python bwinbot.py report                               # hit rate vs. odds-implied, flat-stake ROI

Offline: use --sample sample_odds.json and --scores sample_scores.json instead of --api.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import prod

ODDS_API = "https://api.the-odds-api.com/v4/sports"


@dataclass(frozen=True)
class Selection:
    event_id: str
    sport_key: str
    league: str
    start: str
    home: str
    away: str
    market: str  # h2h | totals | spreads
    pick: str
    point: float | None
    odds: float
    bookmaker: str
    edge: float | None = None  # EV vs. the other bookmakers' consensus, None if unknown

    @property
    def match(self) -> str:
        return f"{self.home} vs {self.away}"

    @property
    def label(self) -> str:
        if self.market == "totals":
            return f"{self.pick} {self.point:g}"
        if self.market == "spreads":
            return f"{self.pick} {self.point:+g}"
        return self.pick

    @property
    def key(self) -> str:
        return f"{self.event_id}|{self.market}|{self.point}|{self.pick}"


# --- data sources -----------------------------------------------------------------------

def api_get(path: str, **params):
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        sys.exit("Set ODDS_API_KEY (https://the-odds-api.com) or use --sample/--scores FILE")
    url = f"{ODDS_API}{path}?" + urllib.parse.urlencode({"apiKey": key, **params})
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.load(resp)


def load_events(args) -> list[dict]:
    if args.sample:
        with open(args.sample) as f:
            return json.load(f)
    return api_get(f"/{args.sport}/odds", regions=args.regions, markets=args.markets,
                   oddsFormat="decimal")


def scores_source(args):
    if args.scores:
        with open(args.scores) as f:
            data = json.load(f)
        return lambda sport_key: data
    return lambda sport_key: api_get(f"/{sport_key}/scores", daysFrom=3)


# --- selections -------------------------------------------------------------------------

def to_selections(events: list[dict], bookmaker: str | None = None) -> list[Selection]:
    """Flatten events to selections. Edge uses the *other* bookmakers' margin-free probabilities."""
    out = []
    for ev in events:
        books = ev.get("bookmakers", [])
        fair: dict[tuple, list[tuple[str, float]]] = {}
        for b in books:
            for m in b["markets"]:
                groups: dict[float, list[dict]] = {}
                for o in m["outcomes"]:
                    groups.setdefault(abs(o.get("point") or 0), []).append(o)
                for outs in groups.values():
                    total = sum(1 / o["price"] for o in outs)
                    for o in outs:
                        fair.setdefault((m["key"], o["name"], o.get("point")), []).append(
                            (b["key"], 1 / o["price"] / total))
        for b in books:
            if bookmaker and b["key"] != bookmaker:
                continue
            for m in b["markets"]:
                for o in m["outcomes"]:
                    others = [p for k, p in fair[(m["key"], o["name"], o.get("point"))] if k != b["key"]]
                    edge = sum(others) / len(others) * o["price"] - 1 if others else None
                    out.append(Selection(
                        ev["id"], ev.get("sport_key", ""), ev.get("sport_title", ""),
                        ev["commence_time"], ev["home_team"], ev["away_team"],
                        m["key"], o["name"], o.get("point"), o["price"], b["title"], edge))
    return out


def _start(s: Selection) -> datetime:
    return datetime.fromisoformat(s.start.replace("Z", "+00:00"))


def filter_selections(sels, league=None, hours=None, min_edge=None, now=None):
    now = now or datetime.now(timezone.utc)
    out = []
    for s in sels:
        if league and league.lower() not in s.league.lower():
            continue
        if hours is not None and not (now <= _start(s) <= now + timedelta(hours=hours)):
            continue
        if min_edge is not None and (s.edge is None or s.edge < min_edge):
            continue
        out.append(s)
    return out


def build_slips(sels, legs=3, target=8.0, tol=0.5, lo=1.6, hi=2.6, pool=50, top=1):
    """Pick `legs` selections from different events with product within target +- tol.

    Score (lower is better): deviation of the total and of each leg from target**(1/legs),
    minus twice the summed edge, so positive-edge legs win ties and near-ties.
    """
    leg_target = target ** (1 / legs)
    cands = sorted((s for s in sels if lo <= s.odds <= hi),
                   key=lambda s: (abs(s.odds - leg_target), -(s.edge or 0)))[:pool]
    scored = []
    for combo in itertools.combinations(cands, legs):
        if len({s.event_id for s in combo}) < legs:
            continue
        total = prod(s.odds for s in combo)
        if abs(total - target) > tol:
            continue
        dev = abs(total - target) + sum(abs(s.odds - leg_target) for s in combo)
        scored.append((dev - 2 * sum(s.edge or 0 for s in combo), combo))
    scored.sort(key=lambda x: x[0])
    return [c for _, c in scored[:top]]


def print_slip(slip, n: int | None = None) -> None:
    if n is not None:
        print(f"--- Slip {n} ---")
    for s in slip:
        edge = f"  edge {s.edge:+.1%}" if s.edge is not None else ""
        print(f"{s.odds:5.2f}  {s.label:<18} {s.match}  [{s.league}, {s.start}, {s.bookmaker}]{edge}")
    print(f"Total odds: {prod(s.odds for s in slip):.2f}")


# --- storage: odds tracking, saved slips, grading ---------------------------------------

def connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS snap (ts REAL, sel TEXT, odds REAL);
        CREATE TABLE IF NOT EXISTS slips (id INTEGER PRIMARY KEY, created TEXT, total REAL,
                                          status TEXT DEFAULT 'pending');
        CREATE TABLE IF NOT EXISTS legs (slip_id INTEGER, event_id TEXT, sport_key TEXT, league TEXT,
                                         start TEXT, home TEXT, away TEXT, market TEXT, point REAL,
                                         pick TEXT, odds REAL, bookmaker TEXT, result TEXT);
    """)
    return db


def track(db, sels: list[Selection]) -> None:
    prev = dict(db.execute("SELECT sel, odds FROM snap WHERE ts = (SELECT MAX(ts) FROM snap)"))
    now = time.time()
    for s in sels:
        old = prev.get(s.key)
        if old is not None and old != s.odds:
            print(f"{s.match} / {s.label}: {old:.2f} -> {s.odds:.2f}")
        db.execute("INSERT INTO snap VALUES (?,?,?)", (now, s.key, s.odds))
    db.commit()
    print(f"Stored {len(sels)} selections.")


def save_slip(db, slip) -> int:
    cur = db.execute("INSERT INTO slips (created, total) VALUES (?, ?)",
                     (datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      prod(s.odds for s in slip)))
    for s in slip:
        db.execute("INSERT INTO legs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                   (cur.lastrowid, s.event_id, s.sport_key, s.league, s.start, s.home, s.away,
                    s.market, s.point, s.pick, s.odds, s.bookmaker))
    db.commit()
    return cur.lastrowid


def grade(market, point, pick, home, away, scores: dict[str, float]) -> str:
    """Return won | lost | void for one leg given the final score."""
    h, a = scores[home], scores[away]
    if market == "h2h":
        winner = home if h > a else away if a > h else "Draw"
        return "won" if pick == winner else "lost"
    if market == "totals":
        return "void" if h + a == point else "won" if (h + a > point) == (pick == "Over") else "lost"
    if market == "spreads":
        mine, other = (h, a) if pick == home else (a, h)
        diff = mine + point - other
        return "void" if diff == 0 else "won" if diff > 0 else "lost"
    raise ValueError(f"unknown market {market}")


def settle(db, get_scores, now: datetime | None = None) -> int:
    """Grade pending legs of started events, then update slip statuses. Returns legs graded."""
    now = (now or datetime.now(timezone.utc)).isoformat()
    cache: dict[str, dict] = {}
    graded = 0
    rows = db.execute("SELECT rowid, event_id, sport_key, home, away, market, point, pick FROM legs "
                      "WHERE result IS NULL AND start < ?", (now,)).fetchall()
    for rowid, event_id, sport_key, home, away, market, point, pick in rows:
        if sport_key not in cache:
            cache[sport_key] = {e["id"]: e for e in get_scores(sport_key)}
        ev = cache[sport_key].get(event_id)
        if not ev or not ev.get("completed") or not ev.get("scores"):
            continue
        scores = {x["name"]: float(x["score"]) for x in ev["scores"]}
        db.execute("UPDATE legs SET result = ? WHERE rowid = ?",
                   (grade(market, point, pick, home, away, scores), rowid))
        graded += 1
    for (slip_id,) in db.execute("SELECT id FROM slips WHERE status = 'pending'").fetchall():
        results = [r for (r,) in db.execute("SELECT result FROM legs WHERE slip_id = ?", (slip_id,))]
        if "lost" in results:
            status = "lost"
        elif None in results:
            continue
        else:
            status = "won" if "won" in results else "void"
        db.execute("UPDATE slips SET status = ? WHERE id = ?", (status, slip_id))
    db.commit()
    return graded


def report(db) -> None:
    n_won = n_settled = 0
    implied = profit = 0.0
    for slip_id, created, total, status in db.execute(
            "SELECT id, created, total, status FROM slips ORDER BY id").fetchall():
        legs = db.execute("SELECT odds, result FROM legs WHERE slip_id = ?", (slip_id,)).fetchall()
        live = [o for o, r in legs if r != "void"]  # void legs drop out of the accumulator
        eff = prod(live) if live else 1.0
        print(f"Slip #{slip_id}  {created}  total {total:.2f}  effective {eff:.2f}  {status.upper()}")
        if status in ("won", "lost"):
            n_settled += 1
            implied += prod(1 / o for o in live)
            if status == "won":
                n_won += 1
                profit += eff - 1
            else:
                profit -= 1
    if not n_settled:
        print("No settled slips yet.")
        return
    print(f"\nSettled {n_settled}: won {n_won} ({n_won / n_settled:.0%}), "
          f"odds-implied hit rate {implied / n_settled:.0%}, "
          f"flat 1-unit ROI {profit / n_settled:+.0%}")


# --- CLI --------------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("command", choices=["slip", "track", "books", "settle", "report"])
    p.add_argument("--sample", help="odds JSON file in The Odds API format")
    p.add_argument("--scores", help="scores JSON file in The Odds API format (for settle)")
    p.add_argument("--api", action="store_true", help="use The Odds API (needs ODDS_API_KEY)")
    p.add_argument("--sport", default="upcoming")
    p.add_argument("--regions", default="eu")
    p.add_argument("--markets", default="h2h,totals", help="h2h,totals,spreads (each costs API quota)")
    p.add_argument("--bookmaker", help="only use this bookmaker key, e.g. bwin")
    p.add_argument("--league", help="substring of league name, e.g. 'epl'")
    p.add_argument("--hours", type=float, help="only events starting within this many hours")
    p.add_argument("--min-edge", type=float, help="minimum edge in percent, e.g. 0")
    p.add_argument("--legs", type=int, default=3)
    p.add_argument("--target", type=float, default=8.0)
    p.add_argument("--tol", type=float, default=0.5)
    p.add_argument("--lo", type=float, default=1.6)
    p.add_argument("--hi", type=float, default=2.6)
    p.add_argument("--top", type=int, default=1, help="number of slips to show")
    p.add_argument("--save", action="store_true", help="store shown slips for later grading")
    p.add_argument("--db", default="odds.sqlite")
    args = p.parse_args()

    if args.command == "report":
        return report(connect(args.db))
    if args.command == "settle":
        if not (args.scores or args.api):
            p.error("choose --scores FILE or --api")
        print(f"Graded {settle(connect(args.db), scores_source(args))} legs.")
        return report(connect(args.db))
    if not (args.sample or args.api):
        p.error("choose --sample FILE or --api")

    events = load_events(args)
    if args.command == "books":
        counts = Counter(b["key"] for ev in events for b in ev.get("bookmakers", []))
        print(f"{len(events)} events; bookmakers (key: events covered):")
        for key, n in counts.most_common():
            print(f"  {key}: {n}")
        return

    sels = filter_selections(
        to_selections(events, args.bookmaker), args.league, args.hours,
        None if args.min_edge is None else args.min_edge / 100)
    if args.command == "track":
        return track(connect(args.db), sels)
    slips = build_slips(sels, args.legs, args.target, args.tol, args.lo, args.hi, top=args.top)
    if not slips:
        print("No combination found; widen --tol, --lo/--hi, --hours or lower --min-edge.")
    db = connect(args.db) if args.save else None
    for i, slip in enumerate(slips, 1):
        print_slip(slip, i if len(slips) > 1 else None)
        if db:
            print(f"Saved as slip #{save_slip(db, slip)}")


if __name__ == "__main__":
    main()
