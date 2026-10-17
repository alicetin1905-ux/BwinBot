import json
import os
import tempfile
from datetime import datetime, timezone
from math import prod

from bwinbot import (build_slips, connect, connect_cache, export_rows, filter_selections, grade, render_log, report,
                     save_slip, settle, to_selections, track)

NOW = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)


def sels(bookmaker="bwin"):
    with open("sample_odds.json") as f:
        return to_selections(json.load(f), bookmaker)


def test_slip_has_three_distinct_events_near_target():
    [slip] = build_slips(sels())
    assert len(slip) == 3
    assert len({s.event_id for s in slip}) == 3
    assert abs(prod(s.odds for s in slip) - 8.0) <= 0.5


def test_no_slip_when_tolerance_impossible():
    assert build_slips(sels(), target=50.0, tol=0.1) == []


def test_edge_none_without_other_bookmakers():
    assert all(s.edge is None for s in sels() if s.event_id == "e4")


def test_totals_and_h2h_markets_present():
    assert {s.market for s in sels()} == {"h2h", "totals"}


def test_filters():
    all_ = sels()
    assert {s.league for s in filter_selections(all_, league="epl", now=NOW)} == {"EPL"}
    day = filter_selections(all_, hours=36, now=NOW)  # e1, e2 start on the 18th
    assert {s.event_id for s in day} == {"e1", "e2"}
    pos = filter_selections(all_, min_edge=0.0, now=NOW)
    assert pos and all(s.edge >= 0 for s in pos)


def test_grade():
    sc = {"A": 2.0, "B": 1.0}
    assert grade("h2h", None, "A", "A", "B", sc) == "won"
    assert grade("h2h", None, "Draw", "A", "B", sc) == "lost"
    assert grade("totals", 3.0, "Over", "A", "B", sc) == "void"
    assert grade("totals", 2.5, "Under", "A", "B", sc) == "lost"
    assert grade("spreads", -1.0, "A", "A", "B", sc) == "void"
    assert grade("spreads", 1.5, "B", "A", "B", sc) == "won"


def test_save_settle_report(capsys):
    with open("sample_scores.json") as f:
        scores = json.load(f)
    with tempfile.TemporaryDirectory() as d:
        db = connect(os.path.join(d, "t.sqlite"))
        chosen = {(s.event_id, s.pick): s for s in sels() if s.market == "h2h"}
        slip = [chosen[("e1", "Arsenal")], chosen[("e2", "Sevilla")], chosen[("e3", "Roma")]]
        sid = save_slip(db, slip)
        assert settle(db, lambda k: scores, now=datetime(2026, 10, 25, tzinfo=timezone.utc)) == 2
        assert db.execute("SELECT status FROM slips WHERE id=?", (sid,)).fetchone() == ("lost",)
        # Sevilla drew -> leg lost -> slip lost even though e3 is still open.
        results = dict(db.execute("SELECT pick, result FROM legs"))
        assert results == {"Arsenal": "won", "Sevilla": "lost", "Roma": None}
        report(db)
        assert "LOST" in capsys.readouterr().out


def test_track_prints_movement(capsys):
    with tempfile.TemporaryDirectory() as d:
        db = connect(os.path.join(d, "t.sqlite"))
        track(db, sels())
        changed = [s for s in sels()]
        db.execute("UPDATE snap SET odds = odds + 0.1 WHERE sel = ?", (changed[0].key,))
        db.commit()
        capsys.readouterr()
        track(db, sels())
        assert "->" in capsys.readouterr().out


def test_quarter_lines_skipped():
    ev = [{"id": "x", "sport_key": "soccer_epl", "sport_title": "EPL", "commence_time": "2026-10-18T14:00:00Z",
           "home_team": "A", "away_team": "B", "bookmakers": [{"key": "k", "title": "K", "markets": [
               {"key": "totals", "outcomes": [{"name": "Over", "price": 2.0, "point": 2.25},
                                              {"name": "Under", "price": 1.9, "point": 2.25}]},
               {"key": "totals", "outcomes": [{"name": "Over", "price": 1.8, "point": 2.5},
                                              {"name": "Under", "price": 2.1, "point": 2.5}]}]}]}]
    assert {s.point for s in to_selections(ev)} == {2.5}


class _Args:
    cache_minutes = 60
    reserve = 100


def test_cached_reuses_fresh_data_and_guards_reserve():
    import bwinbot
    with tempfile.TemporaryDirectory() as d:
        db = connect_cache(os.path.join(d, "t.sqlite"))
        calls = []
        fetch = lambda: calls.append(1) or [{"id": "x"}]
        args = _Args()
        assert bwinbot.cached(db, "k", fetch, 2, args) == [{"id": "x"}]
        assert bwinbot.cached(db, "k", fetch, 2, args) == [{"id": "x"}] and len(calls) == 1
        args.cache_minutes = 0  # force refresh; 101 left - cost 2 would dip below the reserve of 100
        db.execute("INSERT OR REPLACE INTO meta VALUES ('remaining', '101')")
        bwinbot.QUOTA["remaining"] = None
        assert bwinbot.cached(db, "k", fetch, 2, args) == [{"id": "x"}] and len(calls) == 1  # stale cache
        assert bwinbot.cached(db, "new", fetch, 2, args) == [] and len(calls) == 1  # nothing cached


def test_log_lists_winners_first():
    with tempfile.TemporaryDirectory() as d:
        db = connect(os.path.join(d, "t.sqlite"))
        chosen = {(s.event_id, s.pick): s for s in sels() if s.market == "h2h"}
        win = save_slip(db, [chosen[("e1", "Arsenal")]])
        lose = save_slip(db, [chosen[("e2", "Sevilla")]])
        db.execute("UPDATE slips SET status='won' WHERE id=?", (win,))
        db.execute("UPDATE slips SET status='lost' WHERE id=?", (lose,))
        db.execute("UPDATE legs SET result='won' WHERE slip_id=?", (win,))
        db.execute("UPDATE legs SET result='lost' WHERE slip_id=?", (lose,))
        text = render_log(db)
        assert text.index("## Winners (1)") < text.index(f"Slip #{win}") < text.index("## Pending (0)")
        assert text.index(f"Slip #{lose}") > text.index("## Lost (1)")
        assert "won 1 (50%)" in text


def test_export_rows_profit_and_labels():
    with tempfile.TemporaryDirectory() as d:
        db = connect(os.path.join(d, "t.sqlite"))
        tot = [s for s in sels() if s.market == "totals" and s.event_id == "e1" and s.pick == "Over"][0]
        win = save_slip(db, [tot])
        lose = save_slip(db, [tot])
        save_slip(db, [tot])  # stays pending
        db.execute("UPDATE slips SET status='won' WHERE id=?", (win,))
        db.execute("UPDATE slips SET status='lost' WHERE id=?", (lose,))
        db.execute("UPDATE legs SET result='won' WHERE slip_id=?", (win,))
        slips, legs = export_rows(db)
        assert [s["profit"] for s in slips] == [round(tot.odds - 1, 2), -1.0, None]
        assert legs[0]["pick"] == "Over 2.5" and legs[0]["leg_id"] == "1-1" and legs[2]["result"] == "open"
