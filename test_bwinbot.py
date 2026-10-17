import json
from math import prod

from bwinbot import build_slip, to_selections


def sels():
    with open("sample_odds.json") as f:
        return to_selections(json.load(f), "bwin")


def test_slip_has_three_distinct_events_near_target():
    slip = build_slip(sels())
    assert slip and len(slip) == 3
    assert len({s.event_id for s in slip}) == 3
    assert abs(prod(s.odds for s in slip) - 8.0) <= 0.5


def test_no_slip_when_tolerance_impossible():
    assert build_slip(sels(), target=50.0, tol=0.1) is None


def test_edge_is_none_with_single_bookmaker():
    s = [x for x in sels() if x.event_id == "e4"]
    assert all(x.edge is None for x in s)
