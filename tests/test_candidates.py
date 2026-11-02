"""Match tokens, candidate generation, over-collision, recall vs exhaustive."""

from types import SimpleNamespace

from conftest import P
from fpnp.candidate_generation import CandidateGenerator, EntityStore
from fpnp.match_tokens import GroupTokenizer
from fpnp.pipeline import measure_recall
from fpnp.settings import Settings


def settings(**kw):
    s = Settings()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def store_from(records):
    st = EntityStore()
    for r in records:
        st.add(r)
    return st


def test_token_phrases_concatenate_and_skip_null_operators(syn_engine, syn_groups, syn_records, ctx):
    g = syn_groups["SynExactNameDob"]
    tok = GroupTokenizer(g, ctx)
    ph = tok.phrases(syn_engine.prepare(g, syn_records["e01"]))
    # MiddleName is exactOrNull -> no tokens; FirstName/LastName/DoB concatenated
    assert ph == {"FirstName=john|LastName=smith|DoB=1980-01-01"}
    assert tok.phrases(syn_engine.prepare(g, syn_records["e02"])) == ph


def test_ignore_in_token_and_constraint_gating(syn_engine, syn_groups, syn_records, ctx):
    g = syn_groups["SynIdFuzzyName"]
    tok = GroupTokenizer(g, ctx)
    ph = tok.phrases(syn_engine.prepare(g, syn_records["e11"]))
    assert all("Identifiers.Type" not in p for p in ph) and any("Identifiers.Value=777" in p for p in ph)
    # e03 has SSN -> eligible; an entity without SSN/ITN gets no tokens
    no_id = syn_records["e09"]
    assert tok.phrases(syn_engine.prepare(g, no_id)) == set()


def test_tokenization_miss_is_found_only_by_blocking(syn_engine, syn_groups, syn_records):
    recs = [syn_records[k] for k in sorted(syn_records)]
    st = store_from(recs)
    i, j = sorted((st.index["entities/e11"], st.index["entities/e12"]))
    gen = CandidateGenerator(st, list(syn_groups.values()), syn_engine, settings())
    pairs = gen.run()
    src = pairs[(i, j)]
    assert not src.token_groups and src.blocking
    assert src.label(syn_groups["SynIdFuzzyName"].uri) == "BLOCKING_ONLY"
    counts = gen.source_counts()
    assert counts["token_logic"] > 0 and counts["extra_blocking_only"] > 0


def test_overcollided_phrases_are_separated(syn_engine, syn_groups, syn_records):
    recs = [syn_records[k] for k in ("e15", "e16", "e17")]       # same LN/address/zip
    st = store_from(recs)
    g = syn_groups["SynHousehold"]
    gen = CandidateGenerator(st, [g], syn_engine, settings(max_token_collision=2, blocking_enabled=False))
    pairs = gen.run()
    assert len(pairs) == 3
    assert all(p.overcollided_groups == {g.uri} and not p.token_groups for p in pairs.values())
    assert gen.stats.group(g.uri)["overcollided_phrases"] == 1


def test_exhaustive_refused_above_limit(syn_engine, syn_groups, syn_records):
    st = store_from(list(syn_records.values()))
    gen = CandidateGenerator(st, list(syn_groups.values()), syn_engine, settings(exhaustive_max_entities=5))
    assert gen.exhaustive_pass(st.by_type["Person"]) is False


def test_recall_against_exhaustive_on_synthetic(syn_engine, syn_groups, syn_records):
    st = store_from([syn_records[k] for k in sorted(syn_records)])
    rows = {r["MatchGroupURI"]: r for r in measure_recall(st, list(syn_groups.values()), syn_engine, settings())}
    total = rows["(all groups)"]
    assert total["MatchesExhaustive"] > 0
    assert total["RecallOptimised"] == 1.0                  # synthetic only - not a production claim
    assert total["RecallTokenOnly"] < 1.0                   # tokens alone miss the designed tokenisation case
