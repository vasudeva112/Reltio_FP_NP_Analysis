"""
Streaming loader for entity exports.

Supported layouts (detected from the content, not the extension):
  * JSON Lines  - one entity object per line (what `fpnp_analyzer export` writes)
  * JSON array  - [ {...}, {...} ] streamed with an incremental decoder
  * single JSON object - {"objects": [...]} (raw _scan page) or one entity
  * any of the above gzip-compressed (*.gz)
  * a directory - every *.json / *.jsonl / *.ndjson / *.gz file inside, sorted
"""

import gzip
import json
import os
from typing import Iterator, List, Tuple

from .exceptions_log import LOG

_DECODER = json.JSONDecoder()
CHUNK = 1 << 20


def _open(path: str):
    if path.lower().endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, encoding="utf-8")


def expand_paths(paths: List[str]) -> List[str]:
    out: List[str] = []
    for p in paths:
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                if name.lower().endswith((".json", ".jsonl", ".ndjson", ".gz")):
                    out.append(os.path.join(p, name))
        elif os.path.isfile(p):
            out.append(p)
        else:
            LOG.add("INPUT_ERROR", "Entity export path not found", source=p, severity="ERROR")
    return out


def _first_char(path: str) -> str:
    with _open(path) as fh:
        while True:
            c = fh.read(1)
            if not c or not c.isspace():
                return c


def _iter_array(path: str) -> Iterator[dict]:
    with _open(path) as fh:
        buf = ""
        pos = 0
        started = False
        eof = False
        while True:
            if not eof and len(buf) - pos < CHUNK:
                data = fh.read(CHUNK)
                if not data:
                    eof = True
                buf = buf[pos:] + data
                pos = 0
            # skip whitespace / separators
            while pos < len(buf) and (buf[pos].isspace() or buf[pos] == ","):
                pos += 1
            if not started:
                if pos < len(buf) and buf[pos] == "[":
                    started = True
                    pos += 1
                    continue
                if eof:
                    return
                continue
            if pos < len(buf) and buf[pos] == "]":
                return
            if pos >= len(buf):
                if eof:
                    LOG.add("INVALID_JSON", "JSON array not terminated", source=path, severity="ERROR")
                    return
                continue
            try:
                obj, end = _DECODER.raw_decode(buf, pos)
            except json.JSONDecodeError as e:
                if eof:
                    LOG.add("INVALID_JSON", f"Cannot decode array element: {e.msg}", source=path,
                            example=buf[pos:pos + 200], severity="ERROR")
                    return
                # need more data
                data = fh.read(CHUNK)
                if not data:
                    eof = True
                buf = buf[pos:] + data
                pos = 0
                continue
            pos = end
            yield obj


def _iter_lines(path: str) -> Iterator[dict]:
    with _open(path) as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                LOG.add("INVALID_JSON", f"Invalid JSON line: {e.msg}", source=f"{path}:{n}",
                        example=line[:200], severity="ERROR")


def _looks_like_jsonl(path: str) -> bool:
    with _open(path) as fh:
        for line in fh:
            if line.strip():
                try:
                    json.loads(line)
                    return True
                except json.JSONDecodeError:
                    return False
    return False


def iter_entities(paths: List[str]) -> Iterator[Tuple[str, dict]]:
    """Yields (source_file, entity_dict)."""
    for path in expand_paths(paths):
        first = _first_char(path)
        if first == "[":
            it = _iter_array(path)
        elif first == "{" and _looks_like_jsonl(path):
            it = _iter_lines(path)
        elif first == "{":
            LOG.add("INFO", "Single JSON document loaded fully into memory", source=path, severity="INFO")
            with _open(path) as fh:
                try:
                    doc = json.load(fh)
                except json.JSONDecodeError as e:
                    LOG.add("INVALID_JSON", f"Invalid JSON document: {e.msg}", source=path, severity="ERROR")
                    continue
            it = iter(doc.get("objects") or [doc]) if isinstance(doc, dict) else iter([])
        else:
            LOG.add("INVALID_JSON", "File is not JSON / JSON Lines", source=path, severity="ERROR")
            continue
        for obj in it:
            if isinstance(obj, dict) and "uri" not in obj and isinstance(obj.get("objects"), list):
                for inner in obj["objects"]:          # a raw _scan page, possibly one per line
                    if isinstance(inner, dict):
                        yield path, inner
            elif isinstance(obj, dict):
                yield path, obj
            else:
                LOG.add("INVALID_JSON", "Entity element is not an object", source=path,
                        example=repr(obj)[:200])
