"""Pair keys, report reading, JSON loading, checkpoint resume, read-only guard."""

import gzip
import json

import pytest

from fpnp.checkpoint import Checkpoint
from fpnp.exceptions_log import LOG
from fpnp.json_loader import iter_entities
from fpnp.pair_deduplication import aggregate, normalise_uri, pair_key, split_key
from fpnp.report_reader import read_reports, report_pair_index
from fpnp import synthetic


def test_pair_key_is_symmetric_and_normalised():
    assert pair_key("entities/b", "entities/a") == pair_key("a", "https://x/reltio/api/t/entities/b")
    assert split_key(pair_key("b", "a")) == ("entities/a", "entities/b")
    assert normalise_uri("0BB2d7v") == "entities/0BB2d7v"


def test_report_dedup_keeps_rule_level_rows(tmp_path):
    paths = synthetic.build(str(tmp_path))
    rows = list(read_reports([paths["report"]]))
    idx = report_pair_index(rows)
    k = pair_key("e01", "e02")
    assert len([r for r in rows if r.key == k]) == 3          # three rule-level rows (one reversed)
    assert len(idx[k]) == 3 and len(aggregate([{"PairKey": r.key} for r in rows])[k]) == 3
    # original order kept for diagnostics
    assert {(r.src_uri, r.match_uri) for r in rows if r.key == k} == {("entities/e01", "entities/e02"),
                                                                      ("entities/e02", "entities/e01")}
    # dynamic columns mapped by name, including nested paths
    assert "Identifiers.Value" in rows[1].src_values


def test_json_loader_formats(tmp_path):
    ents = [{"uri": f"entities/{i}", "type": "configuration/entityTypes/T"} for i in range(3)]
    (tmp_path / "a.json").write_text(json.dumps(ents, indent=2), encoding="utf-8")
    (tmp_path / "b.jsonl").write_text("\n".join(json.dumps(e) for e in ents) + "\n{bad json\n", encoding="utf-8")
    (tmp_path / "c.json").write_text(json.dumps({"objects": ents}), encoding="utf-8")
    with gzip.open(tmp_path / "d.jsonl.gz", "wt", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(e) for e in ents))
    got = list(iter_entities([str(tmp_path)]))
    assert len(got) == 12
    assert LOG.counts["INVALID_JSON"] == 1


def test_json_array_streaming_across_chunks(tmp_path, monkeypatch):
    import fpnp.json_loader as jl
    monkeypatch.setattr(jl, "CHUNK", 64)
    ents = [{"uri": f"entities/{i}", "attributes": {"N": [{"value": "x" * 50}]}} for i in range(20)]
    (tmp_path / "big.json").write_text(json.dumps(ents), encoding="utf-8")
    assert [e["uri"] for _, e in iter_entities([str(tmp_path / "big.json")])] == [e["uri"] for e in ents]


def test_checkpoint_resume_never_reevaluates(tmp_path):
    db = str(tmp_path / "cp.sqlite")
    cp = Checkpoint(db, "sig1")
    cp.save_batch(["k1", "k2"], [("k1", "g", {"x": 1})])
    cp.close()
    cp = Checkpoint(db, "sig1", resume=True)
    assert cp.evaluated_keys() == {"k1", "k2"} and len(list(cp.results())) == 1
    cp.close()
    cp = Checkpoint(db, "sig2", resume=True)        # changed config/params -> start over
    assert cp.evaluated_keys() == set()
    cp.close()


def test_read_only_client_refuses_writes(monkeypatch):
    import fpnp.reltio_verification as rv

    class Dummy:
        def __init__(self, *a, **k):
            pass

        def request(self, method, path, **kw):
            return ("sent", method, path)

    monkeypatch.setattr(rv, "ReltioClient", Dummy)
    from fpnp.settings import TenantConnection
    c = rv.ReadOnlyClient(TenantConnection("a", "b", "c", "T", "", 1), min_interval=0)
    assert c.request("POST", "entities/_verifyMatches?rules=x")[0] == "sent"
    assert c.request("GET", "entities/1/_matches")[0] == "sent"
    for m, p in (("POST", "entities"), ("POST", "entities/1/_merge"), ("PUT", "entities/1"),
                 ("DELETE", "entities/1"), ("POST", "entities/_unmerge"), ("POST", "configuration")):
        with pytest.raises(rv.ReadOnlyViolation):
            c.request(m, p)


def test_parse_verify_response():
    from fpnp.reltio_verification import parse_verify_response
    g = "configuration/entityTypes/T/matchGroups/G"
    data = {"matchTokensSummary": {"common": ["a:b"]},
            "rules": {g: {"matched": True, "relevance": 0.91, "matchAction": "POTENTIAL_MATCH",
                          "matchTokens": {"intersection": ["a:b"]}}},
            "candidatesDiscovery": {"globalCandidatesFound": True}}
    r = parse_verify_response(data, g)
    assert r["reltio_matched"] is True and r["relevance"] == 0.91 and r["common_tokens"] == 1
