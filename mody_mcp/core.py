"""Bagian bersama: instance MCP + audit log, client Databricks, guard catalog, dan format output."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from functools import cache
from typing import Any

from databricks.sdk import WorkspaceClient
from mcp.server.fastmcp import FastMCP

HOST = os.environ.get("HOST", "127.0.0.1")  # remote: set HOST eksplisit, taruh di belakang proxy TLS
PORT = int(os.environ.get("PORT", "8000"))
MAX_CELL = 200
TRACE_TAIL = 3000
MARKUP = re.compile(r"<[^>]+>|\x1b\[[0-9;]*[A-Za-z]")  # tag HTML dan kode warna ANSI dari notebook/log

audit = logging.getLogger("mody_mcp.audit")


class AuditedFastMCP(FastMCP):
    """Satu baris JSON per tool call untuk SIEM. Hanya nama argumen, bukan nilainya (bisa berisi PII/kode)."""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        start, outcome = time.monotonic(), "ok"
        try:
            return await super().call_tool(name, arguments)
        except Exception as e:
            outcome = type(e).__name__
            raise
        finally:
            audit.info(json.dumps({"event": "tool_call", "tool": name, "arg_keys": sorted(arguments or {}),
                                   "outcome": outcome, "ms": round((time.monotonic() - start) * 1000)}))


mcp = AuditedFastMCP("mody-mcp", host=HOST, port=PORT, stateless_http=True, json_response=True)


# ---------- Databricks ----------


@cache
def ws() -> WorkspaceClient:
    return WorkspaceClient()  # baca DATABRICKS_HOST/TOKEN (atau CLIENT_ID/SECRET) dari env


def allowed_catalogs() -> set[str]:
    return {c.strip() for c in os.environ.get("ALLOWED_CATALOGS", "").split(",") if c.strip()}


def check_catalog(catalog: str) -> None:
    if catalog not in allowed_catalogs():
        raise ValueError(f"Catalog '{catalog}' tidak diizinkan")


def split_full_name(full_name: str) -> list[str]:
    parts = full_name.split(".")
    if len(parts) != 3 or not all(parts):
        raise ValueError(f"full_name harus catalog.schema.table, misalnya mody_dev.core.ref_experiment. Didapat: '{full_name}'")
    check_catalog(parts[0])
    return parts


def max_rows() -> int:
    return int(os.environ.get("MAX_ROWS", "100"))


def clamp(limit: int) -> int:
    return max(1, min(int(limit), max_rows()))


# ---------- format ----------


def cell(value: Any) -> str:
    text = "-" if value is None or value == "" else str(value)
    text = text.replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= MAX_CELL else text[:MAX_CELL] + "..."


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)


def columns_table(columns: list[Any] | None) -> str:
    if not columns:
        return "(kolom tidak tersedia dari Unity Catalog)"
    return md_table(
        ["column", "type", "nullable", "comment"],
        [[c.name, c.type_text, "yes" if c.nullable else "no", c.comment] for c in columns],
    )


def plain(text: str | None) -> str:
    return MARKUP.sub("", text or "").strip()


def ts(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ms else None


def duration(start_ms: int | None, end_ms: int | None) -> str | None:
    if not start_ms or not end_ms:
        return None
    minutes, seconds = divmod(int((end_ms - start_ms) / 1000), 60)
    return f"{minutes}m{seconds:02d}s"
