import os
import sys

import pytest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

from fpnp.comparators.base import EngineContext  # noqa: E402
from fpnp.exceptions_log import LOG  # noqa: E402
from fpnp.rule_config_parser import parse_match_groups  # noqa: E402
from fpnp.rule_evaluator import RuleEngine  # noqa: E402
from fpnp import synthetic  # noqa: E402

REAL_L3 = os.path.join(os.path.dirname(APP), "L3.json")


@pytest.fixture(autouse=True)
def clean_log():
    LOG.clear()
    yield


@pytest.fixture
def ctx():
    return EngineContext()


@pytest.fixture
def syn_groups():
    return {g.name: g for g in parse_match_groups(synthetic.synthetic_config())}


@pytest.fixture
def syn_engine(syn_groups):
    return RuleEngine(list(syn_groups.values()), EngineContext())


@pytest.fixture
def syn_records(syn_groups):
    from fpnp.attribute_extractor import build_record
    attrs = []
    for g in syn_groups.values():
        attrs += [a for a in g.attributes() if a not in attrs]
    recs = {}
    for e in synthetic.synthetic_entities():
        eid = e["uri"].split("/")[-1]
        recs.setdefault(eid, build_record(e, attrs))
    return recs


def P(attr: str) -> str:
    return synthetic.P + attr
