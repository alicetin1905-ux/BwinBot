"""Read odds, track movement and build a ~8.0 three-leg slip (legs near 2.0).

Read-only: this never places bets.

    python bwinbot.py slip  --sample sample_odds.json
    python bwinbot.py slip  --api --bookmaker bwin        # needs ODDS_API_KEY
    python bwinbot.py track --sample sample_odds.json      # stores snapshot, shows movement
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
from dataclasses import dataclass
from math import prod

ODDS_API = "https://api.the-odds-api.com/v4/sports/{sport}/odds"


@dataclass(frozen=True)
class Selection:
    event_id: str
    league: str
    start: str
    match: str
    pick: str
    odds: float
    bookmaker: str
    edge: float | None = None  # expected value vs. market consensus, None if unknown


def load_events(args) -> list[dict]:
    if args.sample:
        with open(args.sample) as f:
            return json.load(f)
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        sys.exit("Set ODDS_API_KEY (https://the-odds-api.com) or use --sample FILE")
    query = urllib.parse.urlencode({
        "apiKey": key, "regions": args.regions, "markets": "h2h", "oddsFormat": "decimal",
    })
    url = ODDS_API.format(sport=args.sport) + "?" + query
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.load(resp)


def _fair_probs(book: dict) -> dict[str, float]:
    outcomes = book["markets"][0]["outcomes"]
    raw = {o["name"]: 1 / o["price"] for o in outcomes}
    total = sum(raw.values())
    return {name: p / total for name, p in raw.items()}  # remove overround


def to_selections(events: list[dict], bookmaker: str | None) -> list[Selection]:
    out = []
    for ev in events:
        books = [b for b in ev.get("bookmakers", []) if b.get("markets")]
        target = [b for b in books if not bookmaker or b["key"] == bookmaker]
        if not target:
            continue
        fair: dict[str, list[float]] = {}
        for b in books:
            for name, p in _fair_probs(b).items():
                fair.setdefault(name, []).append(p)
        for b in target:
            for o in b["markets"][0]["outcomes"]:
                probs = fair.get(o["name"], [])
                edge = (sum(probs) / len(probs)) * o["price"] - 1 if len(books) > 1 else None
                out.append(Selection(
                    ev["id"], ev.get("sport_title", ""), ev["commence_time"],
                    f'{ev["home_team"]} vs {ev["away_team"]}', o["name"],
                    o["price"], b["title"], edge,
                ))
    return out


def build_slip(sels: list[Selection], legs: int = 3, target: float = 8.0,
               tol: float = 0.5, lo: float = 1.6, hi: float = 2.6, pool: int = 40):
    """Pick `legs` selections from different events with product within target +- tol.

    Ranked by closeness of each leg to target**(1/legs), then by edge.
    """
    leg_target = target ** (1 / legs)
    cands = [s for s in sels if lo <= s.odds <= hi]
    cands.sort(key=lambda s: (abs(s.odds - leg_target), -(s.edge or 0)))
    best, best_score = None, None
    for combo in itertools.combinations(cands[:pool], legs):
        if len({s.event_id for s in combo}) < legs:
            continue
        total = prod(s.odds for s in combo)
        if abs(total - target) > tol:
            continue
        score = (abs(total - target) + sum(abs(s.odds - leg_target) for s in combo),
                 -sum(s.edge or 0 for s in combo))
        if best_score is None or score < best_score:
            best, best_score = combo, score
    return best


def print_slip(slip) -> None:
    if not slip:
        print("No combination found within tolerance; widen --tol or --lo/--hi.")
        return
    for s in slip:
        edge = f"  edge {s.edge:+.1%}" if s.edge is not None else ""
        print(f"{s.odds:5.2f}  {s.pick:<20} {s.match}  [{s.league}, {s.start}, {s.bookmaker}]{edge}")
    print(f"Total odds: {prod(s.odds for s in slip):.2f}")


def track(sels: list[Selection], db_path: str) -> None:
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE IF NOT EXISTS snap (ts REAL, event_id TEXT, pick TEXT, odds REAL)")
    prev = dict(db.execute(
        "SELECT event_id || '|' || pick, odds FROM snap WHERE ts = (SELECT MAX(ts) FROM snap)"))
    now = time.time()
    for s in sels:
        old = prev.get(f"{s.event_id}|{s.pick}")
        if old is not None and old != s.odds:
            print(f"{s.match} / {s.pick}: {old:.2f} -> {s.odds:.2f}")
        db.execute("INSERT INTO snap VALUES (?,?,?,?)", (now, s.event_id, s.pick, s.odds))
    db.commit()
    print(f"Stored {len(sels)} selections.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("command", choices=["slip", "track"])
    p.add_argument("--sample", help="JSON file in The Odds API format")
    p.add_argument("--api", action="store_true", help="fetch from The Odds API (ODDS_API_KEY)")
    p.add_argument("--sport", default="upcoming")
    p.add_argument("--regions", default="eu")
    p.add_argument("--bookmaker", help="only use this bookmaker key, e.g. bwin")
    p.add_argument("--legs", type=int, default=3)
    p.add_argument("--target", type=float, default=8.0)
    p.add_argument("--tol", type=float, default=0.5)
    p.add_argument("--lo", type=float, default=1.6)
    p.add_argument("--hi", type=float, default=2.6)
    p.add_argument("--db", default="odds.sqlite")
    args = p.parse_args()
    if not (args.sample or args.api):
        p.error("choose --sample FILE or --api")

    sels = to_selections(load_events(args), args.bookmaker)
    if args.command == "track":
        track(sels, args.db)
    else:
        print_slip(build_slip(sels, args.legs, args.target, args.tol, args.lo, args.hi))


if __name__ == "__main__":
    main()
