"""Verify layers: deterministic coverage/soundness + tighten-only advisory.

Run:  .venv/bin/python -m pytest tests/test_verify.py -q
(or)  .venv/bin/python tests/test_verify.py   # falls back to a plain runner
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from orchestrator import (  # noqa: E402
    _broke_flows, _deterministic_verify, _strongest_floor, _extract_json,
    _advisory_verify,
)


def _entry(tool, result, is_error=False, **inp):
    return {"tool": tool, "input": inp, "result": json.dumps(result),
            "is_error": is_error}


# -- _broke_flows: the delivered-but-accepted-0 regression --------------------

def test_delivered_to_subnet_is_not_broken():
    # A flow that DELIVERED_TO_SUBNET before and after is fine, even though a
    # traceroute would report accepted==0 for it (the bug this replaces).
    diff = {"changed_flows": [
        {"flow": "a->b", "before": "DELIVERED_TO_SUBNET", "after": "DELIVERED_TO_SUBNET"}]}
    assert _broke_flows(diff) == []


def test_exits_network_success_not_broken():
    diff = {"changed_flows": [
        {"flow": "a->b", "before": "EXITS_NETWORK", "after": "EXITS_NETWORK"}]}
    assert _broke_flows(diff) == []


def test_success_to_failure_is_broken():
    diff = {"changed_flows": [
        {"flow": "a->b", "before": "ACCEPTED", "after": "NO_ROUTE"},
        {"flow": "c->d", "before": "DELIVERED_TO_SUBNET", "after": "DENIED_IN"}]}
    assert len(_broke_flows(diff)) == 2


def test_failure_to_success_not_counted_as_broken():
    # The change FIXED this flow — not a break.
    diff = {"changed_flows": [
        {"flow": "a->b", "before": "NO_ROUTE", "after": "ACCEPTED"}]}
    assert _broke_flows(diff) == []


# -- _deterministic_verify: coverage bar -------------------------------------

def test_missing_all_coverage():
    log = [_entry("apply_failure_set", {"ok": True})]
    r = _deterministic_verify(log)
    assert not r["complete"]
    assert len(r["missing_probes"]) == 3  # probe, before/after, loop


def test_full_coverage_is_complete():
    log = [
        _entry("network_traceroute", {"trace_count": 4, "accepted": 4}),
        _entry("differential_reachability", {"changed_flow_count": 0, "changed_flows": []}),
        _entry("detect_loops", {"loop_count": 0}),
    ]
    r = _deterministic_verify(log)
    assert r["complete"]
    assert r["recommended_floor"] is None


# -- _deterministic_verify: conflict floor from diff content -----------------

def test_conflict_floors_insufficient_data():
    log = [
        _entry("network_traceroute", {"trace_count": 4, "accepted": 0}),
        _entry("differential_reachability", {"changed_flows": [
            {"flow": "a->b", "before": "ACCEPTED", "after": "NO_ROUTE"}]}),
        _entry("detect_loops", {"loop_count": 0}),
        _entry("batfish_failure_impact", {"overall": "NO_IMPACT"}),
    ]
    r = _deterministic_verify(log)
    assert r["recommended_floor"] == "INSUFFICIENT-DATA"
    assert r["concerns"]


def test_no_conflict_when_diff_clean():
    # blast-radius NO_IMPACT AND the diff shows nothing broke -> no conflict.
    log = [
        _entry("network_traceroute", {"trace_count": 4, "accepted": 0}),
        _entry("differential_reachability", {"changed_flows": [
            {"flow": "a->b", "before": "DELIVERED_TO_SUBNET", "after": "DELIVERED_TO_SUBNET"}]}),
        _entry("detect_loops", {"loop_count": 0}),
        _entry("batfish_failure_impact", {"overall": "NO_IMPACT"}),
    ]
    r = _deterministic_verify(log)
    assert r["recommended_floor"] is None  # no false INSUFFICIENT-DATA


# -- _strongest_floor --------------------------------------------------------

def test_strongest_floor_picks_most_restrictive():
    assert _strongest_floor(None, "GO-WITH-CONDITIONS", "INSUFFICIENT-DATA") == "INSUFFICIENT-DATA"
    assert _strongest_floor("NO-GO", "INSUFFICIENT-DATA") == "NO-GO"
    assert _strongest_floor(None, None) is None
    assert _strongest_floor("GO", None) is None  # GO ranks 0 -> no floor


# -- _extract_json -----------------------------------------------------------

def test_extract_json_plain_and_fenced_and_prose():
    assert _extract_json('{"a": 1}') == {"a": 1}
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('Here you go: {"a": 1} thanks') == {"a": 1}
    assert _extract_json("not json") is None


# -- _advisory_verify: tighten-only clamp + fail-loud ------------------------

class _Resp:
    def __init__(self, content):
        self.content = content


class _Provider:
    def __init__(self, content):
        self._content = content
    def chat(self, messages, role=None, format_hint=None):
        return _Resp(self._content)


class _Ledger:
    def to_public_dict(self):
        return {"base": "b", "current": "c"}


def test_advisory_clamps_go_to_none():
    # A verifier that tries to APPROVE (GO) must be clamped to no floor.
    prov = _Provider('{"concerns": [], "recommended_floor": "GO"}')
    r = _advisory_verify(prov, "q", _Ledger(), [], {}, lambda _m: None)
    assert r["available"] is True
    assert r["recommended_floor"] is None


def test_advisory_clamps_nogo_to_none():
    # A verifier asserting a break (NO-GO) is not allowed — engine-only fact.
    prov = _Provider('{"concerns": ["x"], "recommended_floor": "NO-GO"}')
    r = _advisory_verify(prov, "q", _Ledger(), [], {}, lambda _m: None)
    assert r["recommended_floor"] is None
    assert r["concerns"] == ["x"]


def test_advisory_allows_insufficient_data():
    prov = _Provider('{"concerns": ["thin evidence"], "recommended_floor": "INSUFFICIENT-DATA"}')
    r = _advisory_verify(prov, "q", _Ledger(), [], {}, lambda _m: None)
    assert r["recommended_floor"] == "INSUFFICIENT-DATA"


def test_advisory_fails_loud_on_error():
    class _Boom:
        def chat(self, *a, **k):
            raise RuntimeError("provider down")
    r = _advisory_verify(_Boom(), "q", _Ledger(), [], {}, lambda _m: None)
    assert r["available"] is False
    assert r["recommended_floor"] is None
    assert r["error"] == "RuntimeError"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
