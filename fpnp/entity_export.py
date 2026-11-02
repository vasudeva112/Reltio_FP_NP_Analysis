"""
Read-only full entity export via entities/_scan (same call pattern as the existing
extractor), written as JSON Lines. Resumable: the scan cursor is saved after every
page in <output>.cursor and an existing export is appended to.
"""

import json
import logging
import os
from urllib.parse import quote

from .reltio_verification import ReadOnlyClient
from .settings import TenantConnection

log = logging.getLogger("fpnp")


def export_entities(conn: TenantConnection, entity_type: str, out_path: str, page_size: int = 100,
                    resume: bool = True) -> int:
    client = ReadOnlyClient(conn, min_interval=0.0)
    filter_expr = f"equals(type,'configuration/entityTypes/{entity_type}')"
    cursor_file = out_path + ".cursor"
    cursor = None
    written = 0
    mode = "w"
    if resume and os.path.exists(cursor_file) and os.path.exists(out_path):
        with open(cursor_file, encoding="utf-8") as fh:
            cursor = json.load(fh).get("cursor")
        mode = "a"
        log.info("Resuming export from saved cursor")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, mode, encoding="utf-8") as out:
        while True:
            r = client.request(
                "POST",
                f"entities/_scan?filter={quote(filter_expr)}&select=uri,type,attributes,crosswalks&max={page_size}",
                headers={"Content-Type": "application/json"},
                json={"cursor": cursor} if cursor else None,
                timeout=300,
            )
            r.raise_for_status()
            data = r.json()
            objects = data.get("objects") or []
            if not objects:
                break
            for obj in objects:
                out.write(json.dumps(obj, separators=(",", ":")) + "\n")
            written += len(objects)
            out.flush()
            cursor = data.get("cursor")
            with open(cursor_file, "w", encoding="utf-8") as fh:
                json.dump({"cursor": cursor, "written": written}, fh)
            if written % (page_size * 50) == 0:
                log.info("Exported %d %s entities", written, entity_type)
    if os.path.exists(cursor_file):
        os.remove(cursor_file)
    log.info("Export finished: %d entities -> %s", written, out_path)
    return written
