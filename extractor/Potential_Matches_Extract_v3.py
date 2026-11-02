#!/usr/bin/env python3
"""
Reltio Potential-Match Extract - v3

Usage:
    python Potential_Matches_Extract_v3.py [path/to/file.properties]

What's new compared with v2:
  * Settings are read from the .properties file (default: dev-ov_02.properties
    next to this script).
  * TARGET_RULE can hold a comma-separated list of rules (full URIs or just
    the rule names, e.g. BeneRule01,BeneRule02). The rules run one at a time,
    in that order. If TARGET_RULE is blank or "AllRules", every match group
    of ENTITY_TYPE runs.
  * There is no mapping file. The script reads each rule's definition from the
    tenant configuration and adds a column for every attribute the rule uses.
  * Crosswalks for both records (src_Crosswalks / match_Crosswalks).
    ReltioCleanser crosswalks are left out of both columns.
  * A->B and B->A count as one pair (per rule).
  * ReltioRelevance / ReltioMatchAction columns (last two) hold the score and
    action label _matches returns for relevance_based groups; blank otherwise.
    Read by FPNP_Analyzer to calibrate its relevance model.

Properties used:
    AUTH                  OAuth token URL (default https://auth.reltio.com/oauth/token)
    CLIENT_CREDENTIALS    Base64(client_id:client_secret) or ENC(...)
    ENVIRONMENT_URL, TENANT_ID, ENTITY_TYPE
    TARGET_RULE           comma-separated rules, blank / AllRules = all rules
    TRANSITIVE_MATCH      Yes / No
    EXTRACT_ALL_VALUES    Yes = join every OV value with " | ", No = first OV only
    OUTPUT_FILE           CSV path
    SPLIT_OUTPUT_BY_RULE  Yes = one CSV per rule (<OUTPUT_FILE stem>_<Rule>.csv)
    HEADER_REQUIRED, THREAD_COUNT, PAGE_SIZE, MATCHES_PAGE_SIZE
"""

import base64
import csv
import json
import logging
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Set
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_PROPERTIES = Path(__file__).with_name("config.properties")
DEFAULT_AUTH_URL = "https://auth.reltio.com/oauth/token"
VALUE_SEP = " | "

# Crosswalk sources left out of src_Crosswalks / match_Crosswalks (compared case-insensitively)
EXCLUDED_CROSSWALK_SOURCES = {"reltiocleanser"}


# ---------- optional Jasypt decrypt ----------
try:
    from Crypto.Cipher import DES
    from Crypto.Hash import MD5
    from Crypto.Protocol.KDF import PBKDF1
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False


def jasypt_decrypt(enc: str, password: str = "Reltio") -> str:
    if not HAS_CRYPTO:
        raise RuntimeError(
            "Install pycryptodome to decrypt ENC(...) values:\n"
            "    pip install pycryptodome\n"
            "Or replace CLIENT_CREDENTIALS with the plain Base64 string."
        )
    raw = base64.b64decode(enc)
    salt, ciphertext = raw[:8], raw[8:]
    key_iv = PBKDF1(password.encode(), salt, 16, count=1000, hashAlgo=MD5)
    key, iv = key_iv[:8], key_iv[8:]
    cipher = DES.new(key, DES.MODE_CBC, iv)
    padded = cipher.decrypt(ciphertext)
    return padded[:-padded[-1]].decode("utf-8")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("pot-mat")


def decrypt_if_needed(value: str) -> str:
    if isinstance(value, str) and value.startswith("ENC(") and value.endswith(")"):
        return jasypt_decrypt(value[4:-1])
    return value


# ---------- properties ----------
def load_properties(path: str) -> Dict[str, str]:
    props: Dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith(("#", "!")) or "=" not in line:
                continue
            key, value = line.split("=", 1)
            # Java .properties escapes, e.g. https\://host -> https://host
            value = re.sub(r"\\(.)", r"\1", value.strip())
            props[key.strip()] = value
    return props


def as_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("yes", "y", "true", "1")


# ---------- HTTP / auth ----------
def make_session(pool_size: int) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=pool_size, pool_maxsize=pool_size)
    s.mount("https://", adapter)
    return s


class ReltioClient:
    """Thin wrapper that adds the bearer token and re-authenticates on 401."""

    def __init__(self, auth_url: str, client_credentials_b64: str, api_url: str, pool_size: int):
        self.auth_url = auth_url
        self.creds = client_credentials_b64
        self.api_url = api_url
        self.session = make_session(pool_size)
        self._token = ""
        self._token_lock = threading.Lock()
        self._refresh("")

    def _refresh(self, stale_token: str) -> None:
        with self._token_lock:
            if self._token != stale_token:      # another thread already refreshed
                return
            r = requests.post(
                self.auth_url,
                headers={
                    "Authorization": f"Basic {self.creds}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"grant_type": "client_credentials"},
                timeout=30,
            )
            r.raise_for_status()
            self._token = r.json()["access_token"]

    def request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = path if path.startswith("https://") else f"{self.api_url}/{path.lstrip('/')}"
        for attempt in range(2):
            token = self._token
            headers = dict(kwargs.pop("headers", {}) or {})
            headers["Authorization"] = f"Bearer {token}"
            r = self.session.request(method, url, headers=headers, **kwargs)
            if r.status_code == 401 and attempt == 0:
                log.info("Token expired - re-authenticating")
                self._refresh(token)
                kwargs["headers"] = headers
                continue
            return r
        return r


# ---------- rule metadata ----------
def normalise_rule_uri(rule: str, entity_type: str) -> str:
    rule = rule.strip()
    if "/" not in rule:
        return f"configuration/entityTypes/{entity_type}/matchGroups/{rule}"
    return rule


def fetch_match_groups(client: ReltioClient, entity_type: str) -> List[dict]:
    """All match groups of the entity type (including inherited ones)."""
    r = client.request("GET", "configuration", timeout=120)
    r.raise_for_status()
    type_uri = f"configuration/entityTypes/{entity_type}"
    for et in r.json().get("entityTypes", []):
        if et.get("uri") == type_uri:
            return et.get("matchGroups", []) or []
    raise RuntimeError(f"Entity type {type_uri} not found in tenant configuration")


def rule_attributes(match_group: dict, entity_type: str) -> List[str]:
    """
    Every attribute referenced anywhere in the rule definition (comparators,
    tokenizers, cleansers, ...), as dotted paths such as Address.PostalCode.Zip5,
    in the order they first appear.
    """
    prefix = f"configuration/entityTypes/{entity_type}/attributes/"
    found: List[str] = []

    def add(s: str) -> None:
        if s.startswith(prefix):
            path = ".".join(s[len(prefix):].split("/attributes/"))
            if path not in found:
                found.append(path)

    def walk(node) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                add(k)
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str):
            add(node)

    for key in ("rule", "rules", "scoreStandalone", "scoreIncremental"):
        if key in match_group:
            walk(match_group[key])
    if not found:                                   # unknown layout - scan everything
        walk(match_group)
    return found


# ---------- value extraction ----------
def _pick_values(values: list, extract_all: bool) -> list:
    ov = [v for v in values if isinstance(v, dict) and v.get("ov", True)]
    if not ov:
        ov = [v for v in values if isinstance(v, dict)]
    return ov if extract_all else ov[:1]


def _render(value) -> str:
    """Leaf value; a nested/reference value is rendered as Sub=val; Sub2=val."""
    if not isinstance(value, dict):
        return "" if value is None else str(value)
    parts = []
    for sub, sub_vals in value.items():
        if isinstance(sub_vals, list):
            vals = [str(v.get("value", "")) for v in _pick_values(sub_vals, True)
                    if not isinstance(v.get("value"), dict)]
            if vals:
                parts.append(f"{sub}={','.join(vals)}")
    return "; ".join(parts)


def collect_values(attrs: dict, parts: List[str], extract_all: bool) -> List[str]:
    """Walks nested/reference attributes, across all OV values when extract_all."""
    if not isinstance(attrs, dict):
        return []
    values = attrs.get(parts[0]) or []
    out: List[str] = []
    for v in _pick_values(values, extract_all):
        inner = v.get("value")
        if len(parts) == 1:
            out.append(_render(inner))
        elif isinstance(inner, dict):
            out.extend(collect_values(inner, parts[1:], extract_all))
    return out


def get_attr_value(attrs: dict, path: str, extract_all: bool) -> str:
    vals: List[str] = []
    for v in collect_values(attrs, path.split("."), extract_all):
        if v != "" and v not in vals:
            vals.append(v)
    if not extract_all:
        vals = vals[:1]
    return VALUE_SEP.join(vals)


def format_crosswalks(crosswalks: list) -> str:
    """Source:Value (sourceTable) for every crosswalk except ReltioCleanser ones."""
    items = []
    for cw in crosswalks or []:
        source = (cw.get("type") or "").replace("configuration/sources/", "")
        if source.lower() in EXCLUDED_CROSSWALK_SOURCES:
            continue
        item = f"{source}:{cw.get('value', '')}"
        if cw.get("sourceTable"):
            item += f" ({cw['sourceTable']})"
        if item not in items:
            items.append(item)
    return VALUE_SEP.join(items)


def extract_values(entity: dict, attr_paths: List[str], extract_all: bool) -> Dict[str, str]:
    result = {
        "ReltioURI": entity.get("uri", ""),
        "Crosswalks": format_crosswalks(entity.get("crosswalks")),
    }
    attrs = entity.get("attributes") or {}
    for path in attr_paths:
        result[path] = get_attr_value(attrs, path, extract_all)
    return result


# ---------- core processing ----------
class RuleRun:
    """State shared by the worker threads while one rule is being extracted."""

    def __init__(self, rule_uri: str, label: str, attr_paths: List[str]):
        self.rule_uri = rule_uri
        self.label = label
        self.attr_paths = attr_paths
        self.select = ",".join(["uri", "crosswalks"] + [f"attributes.{a}" for a in attr_paths])
        self.seen_pairs: Set[frozenset] = set()
        self.lock = threading.Lock()
        self.duplicates = 0

    def claim_pair(self, a: str, b: str) -> bool:
        """True the first time a pair is seen (A->B and B->A are the same pair)."""
        pair = frozenset((a, b))
        with self.lock:
            if pair in self.seen_pairs:
                self.duplicates += 1
                return False
            self.seen_pairs.add(pair)
            return True


def fetch_entity(client: ReltioClient, uri: str, select: str) -> dict:
    r = client.request("GET", uri, params={"select": select}, timeout=60)
    r.raise_for_status()
    return r.json()


def process_entity(
    client: ReltioClient,
    run: RuleRun,
    entity_uri: str,
    transitive: bool,
    extract_all: bool,
    matches_page_size: int,
) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    src_map: Optional[Dict[str, str]] = None

    offset = 0
    while True:
        params = {"offset": offset, "max": matches_page_size, "select": run.select}
        if not transitive:
            params["deep"] = 1

        r = client.request("GET", f"{entity_uri}/_matches", params=params, timeout=120)
        if r.status_code == 404 or not r.text.strip():
            break
        r.raise_for_status()
        data = r.json()
        if not data:
            break

        match_list = next(
            (ml for rule_uri, ml in data.items() if rule_uri.lower() == run.rule_uri.lower()),
            [],
        )
        if not match_list:
            break

        for m in match_list:
            obj = m.get("object") or m
            match_uri = obj.get("uri", "")
            if not match_uri or not run.claim_pair(entity_uri, match_uri):
                continue

            if "crosswalks" not in obj:              # select ignored crosswalks
                obj = fetch_entity(client, match_uri, run.select)
            if src_map is None:                      # fetch driving entity lazily
                src_map = extract_values(
                    fetch_entity(client, entity_uri, run.select), run.attr_paths, extract_all
                )
            match_map = extract_values(obj, run.attr_paths, extract_all)

            row = {"Rule": run.rule_uri, "RuleLabel": run.label}
            for k in src_map:
                row[f"src_{k}"] = src_map[k]
                row[f"match_{k}"] = match_map.get(k, "")
            # relevance_based groups: _matches also returns the score and action label (blank otherwise)
            row["ReltioRelevance"] = m.get("relevance", "")
            row["ReltioMatchAction"] = m.get("matchActionLabel") or m.get("matchAction") or ""
            rows.append(row)

        if len(match_list) < matches_page_size:
            break
        offset += matches_page_size

    return rows


def extract_rule(
    client: ReltioClient,
    run: RuleRun,
    entity_type: str,
    transitive: bool,
    extract_all: bool,
    thread_count: int,
    page_size: int,
    matches_page_size: int,
) -> List[Dict[str, str]]:
    filter_expr = (
        f"equals(type,'configuration/entityTypes/{entity_type}') "
        f"and range(matches,1,100000) "
        f"and equals(matchRules,'{run.rule_uri}')"
    )
    log.info("Filter: %s", filter_expr)

    rows: List[Dict[str, str]] = []
    cursor = None
    eof = False
    scanned = 0

    with ThreadPoolExecutor(max_workers=thread_count) as pool:
        while not eof:
            futures = []
            for _ in range(thread_count):
                r = client.request(
                    "POST",
                    f"entities/_scan?filter={quote(filter_expr)}&select=uri&max={page_size}",
                    headers={"Content-Type": "application/json"},
                    json={"cursor": cursor} if cursor else None,
                    timeout=120,
                )
                r.raise_for_status()
                data = r.json()
                objects = data.get("objects") or []
                if not objects:
                    eof = True
                    break

                cursor = data.get("cursor")
                scanned += len(objects)
                for obj in objects:
                    futures.append(pool.submit(
                        process_entity, client, run, obj["uri"],
                        transitive, extract_all, matches_page_size,
                    ))

            for fut in as_completed(futures):
                try:
                    rows.extend(fut.result())
                except Exception as e:
                    log.exception("Entity failed: %s", e)

            log.info("[%s] entities scanned: %d, unique pairs: %d, reverse duplicates skipped: %d",
                     run.label, scanned, len(rows), run.duplicates)

    return rows


# ---------- output ----------
def csv_header_for(attr_paths: List[str]) -> List[str]:
    header = ["Rule", "RuleLabel"]
    for a in ["ReltioURI", "Crosswalks"] + attr_paths:
        header.extend([f"src_{a}", f"match_{a}"])
    return header + ["ReltioRelevance", "ReltioMatchAction"]    # appended last: earlier columns keep position


def write_csv(path: str, header: List[str], rows: List[Dict[str, str]],
              write_header: bool, append: bool) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a" if append else "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore", restval="")
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def per_rule_file(output_file: str, rule_name: str) -> str:
    p = Path(output_file)
    return str(p.with_name(f"{p.stem}_{rule_name}{p.suffix or '.csv'}"))


def run(properties_path: str):
    props = load_properties(properties_path)
    log.info("Properties: %s", properties_path)

    auth_url = props.get("AUTH") or props.get("AUTH_URL") or DEFAULT_AUTH_URL
    creds = decrypt_if_needed(props["CLIENT_CREDENTIALS"])
    env = props["ENVIRONMENT_URL"]
    tenant = props["TENANT_ID"]
    entity_type = props.get("ENTITY_TYPE", "Individual")
    target_rule = props.get("TARGET_RULE", "").strip()
    transitive = as_bool(props.get("TRANSITIVE_MATCH"))
    extract_all = as_bool(props.get("EXTRACT_ALL_VALUES"))
    header_required = as_bool(props.get("HEADER_REQUIRED"), True)
    split_by_rule = as_bool(props.get("SPLIT_OUTPUT_BY_RULE"))
    thread_count = int(props.get("THREAD_COUNT") or 5)
    page_size = int(props.get("PAGE_SIZE") or 100)
    matches_page_size = int(props.get("MATCHES_PAGE_SIZE") or 200)
    output_file = props["OUTPUT_FILE"]

    if props.get("OV_ATTRIBUTE_FILE"):
        log.info("OV_ATTRIBUTE_FILE is ignored in v3 - attributes come from the rule definitions")

    api_url = f"https://{env}/reltio/api/{tenant}"
    log.info("Authenticating against %s...", auth_url)
    client = ReltioClient(auth_url, creds, api_url, pool_size=thread_count + 2)

    # ------------------------------------------------------------------
    # Resolve which rules to run and the attributes each one uses
    # ------------------------------------------------------------------
    groups = {g["uri"].lower(): g for g in fetch_match_groups(client, entity_type) if g.get("uri")}

    if not target_rule or target_rule.lower() == "allrules":
        selected = list(groups.values())
    else:
        selected = []
        for name in (t for t in target_rule.split(",") if t.strip()):
            uri = normalise_rule_uri(name, entity_type)
            g = groups.get(uri.lower())
            if g is None:
                log.error("Rule %s not found in configuration - skipped", uri)
                continue
            selected.append(g)
    if not selected:
        log.error("No match rules to run")
        return

    runs: List[RuleRun] = []
    for g in selected:
        attrs = rule_attributes(g, entity_type)
        label = g.get("label") or g["uri"].rsplit("/", 1)[-1]
        log.info("Rule %s (%s, type=%s) attributes: %s", g["uri"], label, g.get("type", "?"), attrs)
        runs.append(RuleRun(g["uri"], label, attrs))

    # single file = union of every rule's attributes, in rule order
    union_attrs: List[str] = []
    for rr in runs:
        union_attrs.extend(a for a in rr.attr_paths if a not in union_attrs)
    combined_header = csv_header_for(union_attrs)

    # ------------------------------------------------------------------
    # Run the rules one after another
    # ------------------------------------------------------------------
    total = 0
    for i, rr in enumerate(runs, 1):
        log.info("=== Rule %d/%d: %s ===", i, len(runs), rr.rule_uri)
        rows = extract_rule(client, rr, entity_type, transitive, extract_all,
                            thread_count, page_size, matches_page_size)
        total += len(rows)

        if split_by_rule:
            path = per_rule_file(output_file, rr.rule_uri.rsplit("/", 1)[-1])
            write_csv(path, csv_header_for(rr.attr_paths), rows, header_required, append=False)
        else:
            path = output_file
            write_csv(path, combined_header, rows, header_required and i == 1, append=i > 1)
        log.info("Rule %s done: %d unique pairs (%d reverse duplicates removed) -> %s",
                 rr.label, len(rows), rr.duplicates, path)

    log.info("Finished. %d rule(s), %d unique match-pair rows in total.", len(runs), total)


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else str(DEFAULT_PROPERTIES))