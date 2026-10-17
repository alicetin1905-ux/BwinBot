"""Read odds, track movement, build ~8.0 three-leg slips (legs near 2.0) and grade them.

Read-only: this never places bets.

    python bwinbot.py quota                                # free: remaining API credits
    python bwinbot.py books  --api                         # which bookmakers does the feed cover?
    python bwinbot.py slip   --api --bookmaker bwin --hours 48 --min-edge 0 --save
    python bwinbot.py track  --api --bookmaker bwin        # snapshot + odds movement
    python bwinbot.py settle --api                         # grade saved slips (run within 3 days)
    python bwinbot.py log --out slips_log.md               # markdown log, winners first
    python bwinbot.py export                               # docs/data/slips.json + legs.json
    python bwinbot.py report                               # hit rate vs. odds-implied, flat-stake ROI

Credits: odds are cached (--cache-minutes), leagues with no matches in --hours are skipped via the
free events endpoint, and fetching stops before remaining credits drop below --reserve.

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
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import prod

ODDS_API = "https://api.the-odds-api.com/v4/sports"
PRESETS = {  # --sport europe
    "europe": ["soccer_epl", "soccer_efl_champ", "soccer_turkey_super_league",
               "soccer_germany_bundesliga", "soccer_germany_bundesliga2", "soccer_belgium_first_div",
               "soccer_netherlands_eredivisie", "soccer_italy_serie_a", "soccer_spain_la_liga"],
}


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
    edge: float | None = None  # EV vs. other bookmaker groups' consensus, None if unknown

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

QUOTA = {"remaining": None, "spent": 0}  # filled from the API's x-requests-* headers


def api_get(path: str, **params):
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        sys.exit("Set ODDS_API_KEY (https://the-odds-api.com) or use --sample/--scores FILE")
    url = f"{ODDS_API}{path}?" + urllib.parse.urlencode({"apiKey": key, **params})
    with urllib.request.urlopen(url, timeout=30) as resp:
        h = resp.headers
        if h.get("x-requests-remaining") is not None:
            QUOTA["remaining"] = float(h["x-requests-remaining"])
            QUOTA["spent"] += float(h.get("x-requests-last") or 0)
        return json.load(resp)


def cached(db, key: str, fetch, cost: float, args):
    """Return fetch() with a cache and a credit guard, so the free plan is never used up.

    Fresh cache (< --cache-minutes) is reused for free. Otherwise the call only happens if the
    last known remaining credits stay above --reserve; if not, stale cache (or nothing) is used.
    """
    row = db.execute("SELECT ts, body FROM cache WHERE key = ?", (key,)).fetchone()
    if row and time.time() - row[0] < args.cache_minutes * 60:
        return json.loads(row[1])
    left = db.execute("SELECT v FROM meta WHERE k = 'remaining'").fetchone()
    left = QUOTA["remaining"] if QUOTA["remaining"] is not None else float(left[0]) if left else None
    if left is not None and left - cost < args.reserve:
        print(f"quota guard: {left:.0f} credits left, reserve {args.reserve:.0f}; "
              f"{'using stale cache' if row else 'skipping'} for {key}", file=sys.stderr)
        return json.loads(row[1]) if row else []
    try:
        data = fetch()
    except urllib.error.HTTPError as e:  # e.g. league out of season
        print(f"skipped {key}: HTTP {e.code}", file=sys.stderr)
        return []
    db.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)", (key, time.time(), json.dumps(data)))
    if QUOTA["remaining"] is not None:
        db.execute("INSERT OR REPLACE INTO meta VALUES ('remaining', ?)", (str(QUOTA["remaining"]),))
    db.commit()
    return data


def soccer_leagues(league: str | None, limit: int) -> list[str]:
    """Active football league keys from the (free) /sports listing, outrights excluded."""
    sports = api_get("")
    keys = [x["key"] for x in sports
            if x["group"] == "Soccer" and x["active"] and not x.get("has_outrights")
            and (not league or league.lower() in x["title"].lower() or league.lower() in x["key"])]
    return keys[:limit]


def load_events(args, db) -> list[dict]:
    if args.sample:
        with open(args.sample) as f:
            return json.load(f)
    if args.sport == "soccer":
        keys = soccer_leagues(args.league, args.max_leagues)
    else:
        keys = PRESETS.get(args.sport) or args.sport.split(",")
    end = (datetime.now(timezone.utc) + timedelta(hours=args.hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cost = len(args.markets.split(",")) * len(args.regions.split(","))
    events = []
    for key in keys:
        # The /events listing is free; skip leagues with nothing in the window (saves credits).
        try:
            if not api_get(f"/{key}/events", commenceTimeTo=end):
                continue
        except urllib.error.HTTPError as e:
            print(f"skipped {key}: HTTP {e.code}", file=sys.stderr)
            continue
        events += cached(db, f"odds|{key}|{args.regions}|{args.markets}",
                         lambda: api_get(f"/{key}/odds", regions=args.regions, markets=args.markets,
                                         oddsFormat="decimal"),
                         cost, args)
    return events


def scores_source(args, db):
    if args.scores:
        with open(args.scores) as f:
            data = json.load(f)
        return lambda sport_key: data
    return lambda sport_key: cached(db, f"scores|{sport_key}",
                                    lambda: api_get(f"/{sport_key}/scores", daysFrom=3), 2, args)


# --- selections -------------------------------------------------------------------------

def to_selections(events: list[dict], bookmaker: str | None = None) -> list[Selection]:
    """Flatten events to selections. Edge uses the margin-free probabilities of other bookmakers
    (regional variants like unibet_se/unibet_uk count as one group, so they never vouch for themselves).

    Asian quarter lines (2.25, 1.75, ...) are skipped: they split the stake over two lines, which
    `grade` does not model.
    """
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
                    if o.get("point") is not None and (o["point"] * 2) % 1:
                        continue
                    group = b["key"].split("_")[0]
                    others = [p for k, p in fair[(m["key"], o["name"], o.get("point"))]
                              if k.split("_")[0] != group]
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


def connect_cache(path: str) -> sqlite3.Connection:
    """Throwaway API cache + last-known credits; kept out of git and apart from slip history."""
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, ts REAL, body TEXT);
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
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
    now = ((now or datetime.now(timezone.utc)) - timedelta(hours=2.5)).isoformat()  # match over?
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


def _slip_summary(db) -> str | None:
    n_won = n_settled = 0
    implied = profit = 0.0
    for slip_id, status in db.execute("SELECT id, status FROM slips").fetchall():
        if status not in ("won", "lost"):
            continue
        live = [o for (o, r) in db.execute("SELECT odds, result FROM legs WHERE slip_id = ?", (slip_id,))
                if r != "void"]  # void legs drop out of the accumulator
        n_settled += 1
        implied += prod(1 / o for o in live)
        if status == "won":
            n_won += 1
            profit += prod(live) - 1
        else:
            profit -= 1
    if not n_settled:
        return None
    return (f"Settled {n_settled}: won {n_won} ({n_won / n_settled:.0%}), "
            f"odds-implied hit rate {implied / n_settled:.0%}, "
            f"flat 1-unit ROI {profit / n_settled:+.0%}")


def report(db) -> None:
    for slip_id, created, total, status in db.execute(
            "SELECT id, created, total, status FROM slips ORDER BY id").fetchall():
        live = [o for (o, r) in db.execute("SELECT odds, result FROM legs WHERE slip_id = ?", (slip_id,))
                if r != "void"]
        print(f"Slip #{slip_id}  {created}  total {total:.2f}  "
              f"effective {prod(live) if live else 1.0:.2f}  {status.upper()}")
    print(("\n" + summary) if (summary := _slip_summary(db)) else "No settled slips yet.")


def leg_label(market: str, pick: str, point: float | None) -> str:
    if market == "totals":
        return f"{pick} {point:g}"
    return f"{pick} {point:+g}" if market == "spreads" else pick


def export_rows(db) -> tuple[list[dict], list[dict]]:
    """Slip and leg rows (plain JSON types) for the dashboard."""
    slips, legs = [], []
    for slip_id, created, total, status in db.execute(
            "SELECT id, created, total, status FROM slips ORDER BY id").fetchall():
        rows = db.execute("SELECT odds, result, pick, point, market, home, away, league, start FROM legs "
                          "WHERE slip_id = ? ORDER BY rowid", (slip_id,)).fetchall()
        live = [r[0] for r in rows if r[1] != "void"]
        eff = prod(live) if live else 1.0
        profit = round(eff - 1, 2) if status == "won" else -1.0 if status == "lost" else None
        slips.append({"slip_id": slip_id, "date": created[:10], "status": status, "total": round(total, 2),
                      "effective_odds": round(eff, 2), "profit": profit, "legs": len(rows),
                      "implied_prob": round(prod(1 / o for o in live), 4) if live else None})
        for i, (odds, result, pick, point, market, home, away, league, start) in enumerate(rows, 1):
            legs.append({"leg_id": f"{slip_id}-{i}", "slip_id": slip_id, "date": created[:10],
                         "result": result or "open", "odds": odds, "pick": leg_label(market, pick, point),
                         "match": f"{home} vs {away}", "league": league, "kickoff": start})
    return slips, legs


def render_log(db) -> str:
    """Markdown log of every saved slip, winners first."""
    out = ["# Slip log", "", _slip_summary(db) or "No settled slips yet.", ""]
    sections = [("Winners", "won"), ("Pending", "pending"), ("Lost", "lost"), ("Void", "void")]
    for title, status in sections:
        slips = db.execute("SELECT id, created, total FROM slips WHERE status = ? ORDER BY id DESC",
                           (status,)).fetchall()
        out += [f"## {title} ({len(slips)})", ""]
        for slip_id, created, total in slips or []:
            out += [f"### Slip #{slip_id} - {created[:10]} - total {total:.2f}", "",
                    "| Result | Odds | Pick | Match | League | Kick-off |", "|---|---|---|---|---|---|"]
            for odds, result, pick, point, market, home, away, league, start in db.execute(
                    "SELECT odds, result, pick, point, market, home, away, league, start FROM legs "
                    "WHERE slip_id = ?", (slip_id,)):
                out.append(f"| {result or 'open'} | {odds:.2f} | {leg_label(market, pick, point)} | {home} vs {away} | {league} | {start} |")
            out.append("")
        if not slips:
            out += ["_none yet_", ""]
    return "\n".join(out)


# --- CLI --------------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("command", choices=["slip", "track", "books", "settle", "report", "log", "export", "quota"])
    p.add_argument("--sample", help="odds JSON file in The Odds API format")
    p.add_argument("--scores", help="scores JSON file in The Odds API format (for settle)")
    p.add_argument("--api", action="store_true", help="use The Odds API (needs ODDS_API_KEY)")
    p.add_argument("--sport", default="europe",
                   help="europe (EPL, Championship, Turkey, Bundesliga 1+2, Belgium, Eredivisie, Serie A,\n"
                        "La Liga), soccer (every active football league, see --max-leagues) or comma-separated keys")
    p.add_argument("--max-leagues", type=int, default=10, help="cap on leagues fetched; each costs API credits")
    p.add_argument("--regions", default="eu")
    p.add_argument("--markets", default="h2h,totals", help="h2h,totals,spreads (each costs API quota)")
    p.add_argument("--bookmaker", help="only use this bookmaker key, e.g. bwin")
    p.add_argument("--league", help="substring of league name, e.g. 'epl'")
    p.add_argument("--hours", type=float, default=72, help="only events starting within this many hours")
    p.add_argument("--cache-minutes", type=float, default=360,
                   help="reuse fetched odds this long before spending credits again (0 = always refresh)")
    p.add_argument("--reserve", type=float, default=100,
                   help="never let a fetch take remaining API credits below this")
    p.add_argument("--min-edge", type=float, help="minimum edge in percent, e.g. 0")
    p.add_argument("--legs", type=int, default=3)
    p.add_argument("--target", type=float, default=8.0)
    p.add_argument("--tol", type=float, default=0.5)
    p.add_argument("--lo", type=float, default=1.6)
    p.add_argument("--hi", type=float, default=2.6)
    p.add_argument("--top", type=int, default=1, help="number of slips to show")
    p.add_argument("--save", action="store_true", help="store shown slips for later grading")
    p.add_argument("--db", default="slips.sqlite", help="slips, legs and odds snapshots (tracked in git)")
    p.add_argument("--cache-db", default=".cache.sqlite", help="API cache (not tracked)")
    p.add_argument("--out", help="log: also write the markdown log to this file")
    p.add_argument("--out-dir", default="docs/data", help="export: where slips.json and legs.json go (served by GitHub Pages)")
    args = p.parse_args()

    db = connect(args.db)
    cdb = connect_cache(args.cache_db)
    if args.command == "report":
        return report(db)
    if args.command == "log":
        text = render_log(db)
        if args.out:
            with open(args.out, "w") as f:
                f.write(text)
        return print(text)
    if args.command == "export":
        os.makedirs(args.out_dir, exist_ok=True)
        for name, rows in zip(("slips", "legs"), export_rows(db)):
            with open(os.path.join(args.out_dir, f"{name}.json"), "w") as f:
                json.dump(rows, f, indent=1, ensure_ascii=False)
            print(f"{name}.json: {len(rows)} rows")
        return
    if args.command == "quota":
        api_get("")  # free call; only here to read the quota headers
        print(f"API credits remaining: {QUOTA['remaining']:.0f}")
        return
    if args.command == "settle":
        if not (args.scores or args.api):
            p.error("choose --scores FILE or --api")
        print(f"Graded {settle(db, scores_source(args, cdb))} legs.")
        report(db)
        return quota_summary()
    if not (args.sample or args.api):
        p.error("choose --sample FILE or --api")

    events = load_events(args, cdb)
    if args.command == "books":
        counts = Counter(b["key"] for ev in events for b in ev.get("bookmakers", []))
        print(f"{len(events)} events; bookmakers (key: events covered):")
        for key, n in counts.most_common():
            print(f"  {key}: {n}")
        return quota_summary()

    sels = filter_selections(
        to_selections(events, args.bookmaker), args.league, None if args.sample else args.hours,
        None if args.min_edge is None else args.min_edge / 100)
    if args.command == "track":
        track(db, sels)
        return quota_summary()
    slips = build_slips(sels, args.legs, args.target, args.tol, args.lo, args.hi, top=args.top)
    if not slips:
        print("No combination found; widen --tol, --lo/--hi, --hours or lower --min-edge.")
    for i, slip in enumerate(slips, 1):
        print_slip(slip, i if len(slips) > 1 else None)
        if args.save:
            print(f"Saved as slip #{save_slip(db, slip)}")
    quota_summary()


def quota_summary() -> None:
    if QUOTA["remaining"] is not None:
        print(f"[API credits: spent {QUOTA['spent']:.0f} this run, {QUOTA['remaining']:.0f} remaining]")


if __name__ == "__main__":
    main()
