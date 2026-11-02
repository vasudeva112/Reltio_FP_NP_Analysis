"""
Optional tenant verification - READ-ONLY.

Uses the existing ReltioClient (OAuth client credentials, token refresh, HTTP retry on
429/5xx) behind ReadOnlyClient, which refuses every call that is not on the read-only
whitelist below. Nothing is ever merged, unmerged, updated or reconfigured.

  POST {tenant}/entities/_verifyMatches?rules=<matchGroupUri>
       body {"first": {"uri": e1}, "second": {"uri": e2}}
  GET  {tenant}/{entity}/_matches?deep=1        (read current potential matches)

The request/response shape follows Reltio's "Verifying matches" documentation
(response: rules.<uri>.matched / relevance / matchTokens.intersection,
matchTokensSummary.common, candidatesDiscovery). Confirm against your tenant's API
version before relying on it; unknown shapes are stored raw and flagged.
"""

import json
import logging
import random
import re
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from .exceptions_log import LOG
from .legacy import ReltioClient
from .settings import TenantConnection

log = logging.getLogger("fpnp")

_READ_ONLY_POST = (re.compile(r"(^|/)entities/_verifyMatches(\?|$)"), re.compile(r"(^|/)entities/_scan(\?|$)"))


class ReadOnlyViolation(RuntimeError):
    pass


class ReadOnlyClient:
    def __init__(self, conn: TenantConnection, min_interval: float = 0.5, pool: int = 4):
        self._client = ReltioClient(conn.auth_url, conn.client_credentials_b64, conn.api_url, pool)
        self.min_interval = min_interval
        self._last = 0.0
        self.calls = 0

    def _throttle(self) -> None:
        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def request(self, method: str, path: str, **kw):
        m = method.upper()
        if m == "GET":
            pass
        elif m == "POST" and any(p.search(path) for p in _READ_ONLY_POST):
            pass
        else:
            raise ReadOnlyViolation(f"Refused non read-only call: {m} {path}")
        self._throttle()
        self.calls += 1
        return self._client.request(m, path, **kw)


def verify_pair(client: ReadOnlyClient, e1: str, e2: str, group_uri: Optional[str]) -> dict:
    path = "entities/_verifyMatches" + (f"?rules={quote(group_uri, safe='/')}" if group_uri else "")
    body = {"first": {"uri": e1}, "second": {"uri": e2}}
    try:
        r = client.request("POST", path, json=body, headers={"Content-Type": "application/json"}, timeout=120)
    except ReadOnlyViolation:
        raise
    except Exception as e:  # network errors after retries
        return {"error": str(e)}
    if r.status_code >= 400:
        return {"error": f"HTTP {r.status_code}: {r.text[:300]}"}
    try:
        data = r.json()
    except ValueError:
        return {"error": "non-JSON response", "raw": r.text[:500]}
    return parse_verify_response(data, group_uri)


def parse_verify_response(data: dict, group_uri: Optional[str]) -> dict:
    out = {"raw": json.dumps(data)[:4000]}
    common = (data.get("matchTokensSummary") or {}).get("common")
    out["common_tokens"] = len(common) if isinstance(common, list) else None
    rules = data.get("rules") or {}
    if group_uri and isinstance(rules, dict):
        rr = next((v for k, v in rules.items() if k.lower() == group_uri.lower()), None)
        if rr is None:
            out["reltio_matched"] = None
            out["note"] = "group not in response rules"
            LOG.add("VERIFY_RESPONSE_SHAPE", "Verify response has no entry for the requested group",
                    group=group_uri)
        else:
            out["reltio_matched"] = rr.get("matched")
            out["relevance"] = rr.get("relevance")
            out["match_action"] = rr.get("matchAction")
            out["bypassed"] = rr.get("bypassed")
            inter = ((rr.get("matchTokens") or {}).get("intersection"))
            out["rule_token_intersection"] = len(inter) if isinstance(inter, list) else None
            if out["rule_token_intersection"] is not None:
                out["common_tokens"] = out["rule_token_intersection"]
    else:
        out["reltio_matched"] = any(isinstance(v, dict) and v.get("matched") for v in rules.values())
    disc = data.get("candidatesDiscovery") or {}
    out["global_candidates_found"] = disc.get("globalCandidatesFound")
    return out


def read_matches(client: ReadOnlyClient, entity_uri: str) -> dict:
    r = client.request("GET", f"{entity_uri}/_matches", params={"deep": 1, "max": 200}, timeout=120)
    if r.status_code >= 400 or not r.text.strip():
        return {}
    return r.json()


def choose_sample(fp_rows: List[dict], fn_rows: List[dict], n: int, seed: int) -> List[Tuple[str, str, str, str, str]]:
    """(kind, pair_key, group_uri, e1, e2) - stratified over FN classes and FP disagreements/risk."""
    rnd = random.Random(seed)
    buckets: Dict[str, list] = {}
    for r in fn_rows:
        if r.get("Classification") != "Reltio Match":
            buckets.setdefault(f"FN:{r['Classification']}", []).append(
                ("FN", r["PairKey"], r["MatchGroupURI"], r["EntityURI1"], r["EntityURI2"]))
    for r in fp_rows:
        if "MatchGroupURI" not in r or not r.get("SourceURI"):
            continue
        k = "FP:python_disagrees" if r.get("PythonMatched") is False else f"FP:{r.get('Classification')}"
        buckets.setdefault(k, []).append(("FP", r["PairKey"], r["MatchGroupURI"], r["SourceURI"], r["MatchedURI"]))
    for b in buckets.values():
        rnd.shuffle(b)
    sample, seen = [], set()
    while len(sample) < n and any(buckets.values()):
        for k in sorted(buckets):
            if buckets[k] and len(sample) < n:
                item = buckets[k].pop()
                if (item[1], item[2]) not in seen:
                    seen.add((item[1], item[2]))
                    sample.append(item)
    return sample


def run_verification(conn: TenantConnection, sample, min_interval: float) -> Dict[str, dict]:
    client = ReadOnlyClient(conn, min_interval)
    results: Dict[str, dict] = {}
    for n, (kind, key, group, e1, e2) in enumerate(sample, 1):
        res = verify_pair(client, e1, e2, group)
        res.update(kind=kind, pair_key=key, group_uri=group)
        results[f"{key}##{group}"] = res
        if "error" in res:
            LOG.add("VERIFY_FAILED", "Verify Matches call failed", group=group, example=f"{key}: {res['error']}")
        log.info("verify %d/%d %s %s -> %s", n, len(sample), kind, key, res.get("reltio_matched", res.get("error")))
    return results
