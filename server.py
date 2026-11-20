"""mody-mcp: MCP server untuk agent Mody (Streamable HTTP).

Databricks (Unity Catalog, MLflow, Jobs) read-only. GitHub: baca repo, commit ke branch agent, kelola PR agent.

Spesifikasi: docs/2026-10-30-mody-mcp-generik-design.md
"""

from __future__ import annotations

import base64
import fnmatch
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from functools import cache
from itertools import islice
from typing import Any, Literal

import httpx
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import (
    ExecuteStatementRequestOnWaitTimeout,
    StatementParameterListItem,
    StatementState,
)
from databricks.sdk.errors import DatabricksError, NotFound, Unauthenticated
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field, field_validator

HOST = os.environ.get("HOST", "127.0.0.1")  # remote: set HOST eksplisit, taruh di belakang proxy TLS
PORT = int(os.environ.get("PORT", "8000"))
MAX_CELL = 200
MAX_CELL_FEW = 800  # hasil sedikit baris: sel (mis. JSON confusion_matrix) boleh lebih panjang
FEW_ROWS = 10
OPS = {"=", "!=", "<", "<=", ">", ">=", "IN", "IS NULL", "IS NOT NULL"}
MAX_IN_VALUES = 100
MIN_API_KEY_LEN = 32

audit = logging.getLogger("mody_mcp.audit")
log = logging.getLogger("mody_mcp")

MAX_ERROR_CHARS = 300
REDACTIONS = (
    (re.compile(r"\s*\[ReqId:[^\]]*\]\.?"), ""),
    (re.compile(r"\s*Config:.*", re.S), ""),
    (re.compile(r"https?://\S+"), "<url>"),
    (re.compile(r"[\w.-]+\.(?:databricks|azuredatabricks)\.(?:com|net)"), "<host>"),
    (re.compile(r"\b(?:workspace|account)_id=\S+"), ""),
)


def redact(text: str) -> str:
    """Buang host, id workspace/akun, URL, dan request id dari pesan error sebelum sampai ke LLM."""
    for pattern, repl in REDACTIONS:
        text = pattern.sub(repl, text)
    return text.strip()[:MAX_ERROR_CHARS]


NOT_FOUND_HINTS = {
    **dict.fromkeys(("describe_table", "query_table", "aggregate_table"), "Cek nama tabel dengan list_tables."),
    "list_tables": "Kosongkan schema untuk melihat semua schema.",
    "list_models": "Kosongkan full_name untuk daftar model.",
    "search_runs": "Kosongkan experiment_path untuk mencari di semua experiment; run_id berasal dari hasil search_runs.",
    "job_runs": "Kosongkan job_name dan run_id untuk daftar job.",
}


def public_error(name: str, e: Exception) -> ToolError | None:
    """Pesan error yang aman dikirim ke agent. None = pesan asli sudah aman (error validasi kita sendiri).

    Detail lengkap hanya ke log server, dengan error_id supaya bisa dilacak.
    """
    cause = e.__cause__ or e
    if isinstance(cause, ToolError) or (isinstance(cause, (ValueError, RuntimeError)) and not isinstance(cause, DatabricksError)):
        return None
    error_id = os.urandom(4).hex()
    log.error("tool_error %s id=%s: %r", name, error_id, cause, exc_info=cause)
    if isinstance(cause, Unauthenticated):
        detail = "autentikasi Databricks gagal (token tidak valid atau kedaluwarsa), hubungi admin server"
    elif isinstance(cause, DatabricksError):
        detail = redact(str(cause)) or type(cause).__name__
        if isinstance(cause, NotFound) and name in NOT_FOUND_HINTS:
            detail = f"{detail.rstrip('. ')}. {NOT_FOUND_HINTS[name]}"
    else:
        detail = "kesalahan internal server"
    return ToolError(f"Error executing tool {name}: {detail} (error_id: {error_id})")


def slim_schema(node: Any) -> Any:
    """Buang anotasi 'title' otomatis pydantic dari JSON schema (noise ~15% token). Parameter bernama title tetap aman."""
    if isinstance(node, list):
        return [slim_schema(n) for n in node]
    if not isinstance(node, dict):
        return node
    return {
        k: ({n: slim_schema(sub) for n, sub in v.items()} if k in ("properties", "$defs") else slim_schema(v))
        for k, v in node.items()
        if not (k == "title" and isinstance(v, str))
    }


class AuditedFastMCP(FastMCP):
    """Satu baris JSON per tool call untuk SIEM. Hanya nama argumen, bukan nilainya (bisa berisi PII/kode)."""

    async def list_tools(self) -> list[Any]:
        tools = await super().list_tools()
        for t in tools:
            t.inputSchema = slim_schema(t.inputSchema)
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        start, outcome, size = time.monotonic(), "ok", None
        try:
            result = await super().call_tool(name, arguments)
            size = len(str(result))
            return result
        except Exception as e:
            outcome = type(e.__cause__ or e).__name__
            safe = public_error(name, e)
            if safe is None:
                raise
            raise safe from None
        finally:
            audit.info(json.dumps({"event": "tool_call", "tool": name, "arg_keys": sorted(arguments or {}),
                                   "outcome": outcome, "result_chars": size,
                                   "ms": round((time.monotonic() - start) * 1000)}))


mcp = AuditedFastMCP("mody-mcp", host=HOST, port=PORT, stateless_http=True, json_response=True)


@cache
def ws() -> WorkspaceClient:
    return WorkspaceClient()  # baca DATABRICKS_HOST/TOKEN (atau CLIENT_ID/SECRET) dari env


def allowed_catalogs() -> set[str]:
    return {c.strip() for c in os.environ.get("ALLOWED_CATALOGS", "").split(",") if c.strip()}


def max_rows() -> int:
    return int(os.environ.get("MAX_ROWS", "100"))


def check_catalog(catalog: str) -> None:
    allowed = allowed_catalogs()
    if catalog not in allowed:
        raise ValueError(f"Catalog '{catalog}' tidak diizinkan")


MAX_SCHEMAS = 30


def resolve_catalog(catalog: str = "") -> str:
    """catalog kosong = satu-satunya catalog yang diizinkan. Kalau ada lebih dari satu, wajib diisi."""
    if catalog:
        check_catalog(catalog)
        return catalog
    allowed = sorted(allowed_catalogs())
    if len(allowed) != 1:
        raise ValueError(f"catalog wajib diisi. Pilihan: {allowed}")
    return allowed[0]


def schema_names(catalog: str) -> list[str]:
    names = [s.name for s in ws().schemas.list(catalog_name=catalog) if s.name != "information_schema"]
    return names[:MAX_SCHEMAS]


def split_full_name(full_name: str) -> list[str]:
    parts = full_name.split(".")
    if len(parts) != 3 or not all(parts):
        raise ValueError(f"full_name harus catalog.schema.table, misalnya mody_dev.core.ref_experiment. Didapat: '{full_name}'")
    check_catalog(parts[0])
    return parts


def cell(value: Any, limit: int = MAX_CELL) -> str:
    text = "-" if value is None or value == "" else str(value)
    text = text.replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    limit = MAX_CELL_FEW if len(rows) <= FEW_ROWS else MAX_CELL
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(cell(v, limit) for v in row) + " |" for row in rows]
    return "\n".join(lines)


def columns_table(columns: list[Any] | None, samples: dict[str, str] | None = None) -> str:
    if not columns:
        return "(kolom tidak tersedia dari Unity Catalog)"
    if samples is None:
        return md_table(
            ["column", "type", "nullable", "comment"],
            [[c.name, c.type_text, "yes" if c.nullable else "no", c.comment] for c in columns],
        )
    return md_table(
        ["column", "type", "nullable", "comment", "contoh_nilai"],
        [[c.name, c.type_text, "yes" if c.nullable else "no", c.comment, samples.get(c.name)] for c in columns],
    )


# ---------- metadata ----------


@mcp.tool()
def list_tables(catalog: str = "", schema: str = "", include_columns: bool = False) -> str:
    """Daftar tabel per schema. Kosongkan semua argumen = semua tabel di semua schema (cukup satu panggilan).

    catalog kosong = catalog tunggal yang diizinkan. include_columns=True menambah kolom tiap tabel (output panjang);
    untuk satu tabel pakai describe_table.
    """
    catalog = resolve_catalog(catalog)
    blocks = []
    for sc in [schema] if schema else schema_names(catalog):
        tables = list(ws().tables.list(catalog_name=catalog, schema_name=sc, omit_columns=not include_columns))
        block = f"Tabel di {catalog}.{sc} ({len(tables)}):\n" + md_table(
            ["table", "type", "comment"],
            [[t.name, t.table_type.value if t.table_type else None, t.comment] for t in tables],
        )
        if include_columns:
            block += "".join(f"\n\n### {t.name}\n{columns_table(t.columns)}" for t in tables)
        blocks.append(block)
    return "\n\n".join(blocks)


@mcp.tool()
def list_models(
    catalog: str = "", schema: str = "", full_name: str = "", metrics: list[str] = [], limit: int = 20  # noqa: B006
) -> str:
    """Model terdaftar Unity Catalog. Tanpa full_name: daftar model (schema kosong = semua schema).

    Dengan full_name (catalog.schema.model): versi model beserta alias (mis. champion), terbaru dulu.
    metrics: nama metrik run tiap versi yang ditampilkan langsung (maksimal 10 versi).
    """
    if full_name:
        return list_model_versions(full_name, limit, metrics)
    catalog = resolve_catalog(catalog)
    rows = []
    for sc in [schema] if schema else schema_names(catalog):
        rows += [[m.full_name, m.comment] for m in ws().registered_models.list(catalog_name=catalog, schema_name=sc)]
    return f"Model di {catalog}{'.' + schema if schema else ''} ({len(rows)}):\n" + md_table(["full_name", "comment"], rows)


SAMPLE_DISTINCT = 8


def categorical_samples(full_name: str, columns: list[Any]) -> dict[str, str]:
    """Nilai kolom string berkardinalitas rendah dari satu sampel acak (satu query warehouse)."""
    names = [c.name for c in columns if (c.type_text or "").lower() == "string"][:30]
    if not names:
        return {}
    sql, params = build_query(full_name, {c.name for c in columns}, columns=names, sample=True, limit=max_rows())
    _, rows, _ = execute(sql, params)
    out = {}
    for i, name in enumerate(names):
        values = sorted({str(r[i]) for r in rows if r[i] is not None})
        if 0 < len(values) <= SAMPLE_DISTINCT:
            out[name] = ", ".join(v[:40] for v in values)
    return out


@mcp.tool()
def describe_table(full_name: str, include_samples: bool = False) -> str:
    """Struktur satu tabel: tipe, format, owner, komentar, kolom. full_name = catalog.schema.table.

    include_samples=True menambah nilai kolom teks berkardinalitas rendah (mis. label kelas) dari sampel acak;
    memakai warehouse, pakai bila perlu tahu nilai filter yang sah.
    """
    split_full_name(full_name)
    t = ws().tables.get(full_name=full_name)
    samples = categorical_samples(full_name, t.columns or []) if include_samples else None
    return (
        f"## {t.full_name}\n"
        f"- type: {cell(t.table_type.value if t.table_type else None)}, "
        f"format: {cell(t.data_source_format.value if t.data_source_format else None)}\n"
        f"- owner: {cell(t.owner)}\n"
        f"- comment: {cell(t.comment)}\n\n" + columns_table(t.columns, samples)
    )


# ---------- data ----------


class Filter(BaseModel):
    column: str = Field(description="Nama kolom di tabel")
    op: str = Field(description="Operator", json_schema_extra={"enum": sorted(OPS)})
    value: str | list[str] | None = Field(default=None, description="Nilai sebagai teks. IN: list. IS NULL: kosong")

    @field_validator("value", mode="before")
    @classmethod
    def as_text(cls, v: Any) -> Any:
        if isinstance(v, (list, tuple)):
            return [str(i) for i in v]
        return v if v is None or isinstance(v, str) else str(v)


def where_clause(filters: list[Filter] | None, ident: Any, params: dict[str, str]) -> str:
    """Susun ' WHERE ...' dari filter. Nama dan nilai selalu jadi parameter marker. Kosong kalau tanpa filter."""
    where = []
    for i, f in enumerate(filters or []):
        op = f.op.upper().strip()
        if op not in OPS:
            raise ValueError(f"op '{f.op}' tidak didukung. Pilihan: {sorted(OPS)}")
        col = ident(f.column, f"f{i}")
        if op in ("IS NULL", "IS NOT NULL"):
            where.append(f"{col} {op}")
        elif op == "IN":
            values = f.value if isinstance(f.value, list) else [f.value]
            if not values or len(values) > MAX_IN_VALUES:
                raise ValueError(f"op IN butuh list berisi 1 sampai {MAX_IN_VALUES} value")
            keys = [f"v{i}_{j}" for j in range(len(values))]
            params.update({k: str(v) for k, v in zip(keys, values)})
            where.append(f"{col} IN ({', '.join(':' + k for k in keys)})")
        else:
            if f.value is None or isinstance(f.value, list):
                raise ValueError(f"op {op} butuh satu value")
            params[f"v{i}"] = str(f.value)
            where.append(f"{col} {op} :v{i}")
    return " WHERE " + " AND ".join(where) if where else ""


def build_query(
    full_name: str,
    known_columns: set[str],
    columns: list[str] | None = None,
    filters: list[Filter] | None = None,
    order_by: str | None = None,
    desc: bool = False,
    limit: int = 20,
    sample: bool = False,
) -> tuple[str, dict[str, str]]:
    """Susun SELECT bertemplate. Semua nama dan nilai dari agent jadi parameter marker, tidak pernah ditempel ke SQL."""
    params: dict[str, str] = {"tbl": full_name}

    def ident(name: str, key: str) -> str:
        if name not in known_columns:
            raise ValueError(f"Kolom '{name}' tidak ada. Kolom yang sah: {sorted(known_columns)}")
        params[key] = f"`{name}`"
        return f"IDENTIFIER(:{key})"

    select = ", ".join(ident(c, f"c{i}") for i, c in enumerate(columns)) if columns else "*"
    sql = f"SELECT {select} FROM IDENTIFIER(:tbl)"

    sql += where_clause(filters, ident, params)

    if sample:
        if order_by:
            raise ValueError("sample tidak bisa digabung dengan order_by")
        sql += " ORDER BY RAND()"
    elif order_by:
        sql += f" ORDER BY {ident(order_by, 'o')}" + (" DESC" if desc else "")

    sql += f" LIMIT {clamp(limit)}"
    return sql, params


def execute(sql: str, params: dict[str, str]) -> tuple[list[str], list[list[Any]], bool]:
    warehouse_id = os.environ.get("DATABRICKS_SQL_WAREHOUSE_ID")
    if not warehouse_id:
        raise ValueError("DATABRICKS_SQL_WAREHOUSE_ID belum diset di server")
    # ponytail: semua parameter dikirim sebagai STRING, Databricks SQL meng-cast implisit
    # (period >= '2026-09-01', score > '0.5'). Tambah tipe per parameter kalau ada cast yang gagal.
    resp = ws().statement_execution.execute_statement(
        statement=sql,
        warehouse_id=warehouse_id,
        parameters=[StatementParameterListItem(name=k, value=v) for k, v in params.items()],
        wait_timeout="30s",
        on_wait_timeout=ExecuteStatementRequestOnWaitTimeout.CANCEL,
        row_limit=max_rows(),
    )
    state = resp.status.state if resp.status else None
    if state != StatementState.SUCCEEDED:
        error = resp.status.error.message if resp.status and resp.status.error else "tanpa pesan"
        raise RuntimeError(f"Query {state.value if state else 'UNKNOWN'}: {error}")
    headers = [c.name for c in resp.manifest.schema.columns]
    rows = (resp.result.data_array if resp.result else None) or []
    return headers, rows, bool(getattr(resp.manifest, "truncated", False))


def run_query(sql: str, params: dict[str, str]) -> str:
    headers, rows, truncated = execute(sql, params)
    note = ""
    if not rows:
        note = (" - kosong. Cek nilai filter: kolom kategori biasanya berisi teks (mis. 'padi'), bukan angka. "
                "Lihat nilai asli dengan query_table tanpa filter")
    elif len(rows) >= max_rows() or truncated:
        note = f" - mencapai batas MAX_ROWS={max_rows()}, hasil mungkin terpotong. Pakai aggregate_table atau filter lebih sempit"
    return f"{len(rows)} baris{note}\n" + md_table(headers, rows)


COLUMN_TTL = 300
COLUMN_CACHE: dict[str, tuple[float, set[str]]] = {}


def table_columns(full_name: str, refresh: bool = False) -> set[str]:
    """Nama kolom tabel untuk validasi. Di-cache beberapa menit supaya tiap query tidak memanggil tables.get."""
    hit = COLUMN_CACHE.get(full_name)
    if hit and not refresh and time.monotonic() - hit[0] < COLUMN_TTL:
        return hit[1]
    columns = {c.name for c in ws().tables.get(full_name=full_name).columns or []}
    COLUMN_CACHE[full_name] = (time.monotonic(), columns)
    return columns


def with_columns(full_name: str, build: Any, other: str | None = None) -> str:
    """Jalankan query dengan validasi kolom. Kalau kolom tidak dikenal dan daftar berasal dari cache, ambil ulang sekali.

    build(kolom_utama, kolom_tabel_kedua_atau_None) -> (sql, params).
    """
    split_full_name(full_name)
    cached = full_name in COLUMN_CACHE or (other is not None and other in COLUMN_CACHE)

    def go(refresh: bool) -> str:
        known = table_columns(full_name, refresh)
        return run_query(*build(known, table_columns(other, refresh) if other else None))

    try:
        return go(False)
    except ValueError as e:
        if cached and "tidak ada" in str(e):
            return go(True)
        raise


def query(full_name: str, **kwargs: Any) -> str:
    return with_columns(full_name, lambda known, _: build_query(full_name, known, **kwargs))


@mcp.tool()
def query_table(
    full_name: str,
    columns: list[str] = [],  # noqa: B006
    filters: list[Filter] = [],  # noqa: B006
    order_by: str = "",
    desc: bool = False,
    limit: int = 20,
    sample: bool = False,
) -> str:
    """Ambil baris mentah satu tabel (read-only); tanpa argumen lain = contoh baris untuk melihat nilai asli kolom.
    Untuk hitungan, rata-rata, distribusi pakai aggregate_table.

    columns: kosong = semua. filters: digabung AND, mis. [{"column": "scope", "op": "=", "value": "production"}]; IN pakai list.
    order_by + desc: urutan. sample=True: sampel acak (tanpa order_by). limit dibatasi MAX_ROWS.
    """
    return query(full_name, columns=columns, filters=filters, order_by=order_by, desc=desc, limit=limit, sample=sample)


MAX_GROUP_BY = 5
MAX_METRICS = 10
AGG_FUNCS = {"count": "COUNT", "count_distinct": "COUNT", "avg": "AVG", "min": "MIN", "max": "MAX", "sum": "SUM",
             "stddev": "STDDEV", "median": "MEDIAN"}


class Metric(BaseModel):
    fn: Literal["count", "count_distinct", "avg", "min", "max", "sum", "stddev", "median"]
    column: str = Field(default="", description="Kolom. Kosong hanya untuk count. Dengan join, kolom tabel kedua diawali b.")
    array: Literal["", "mean", "max", "min"] = Field(default="", description="Kolom array: ringkas tiap baris dulu")
    at: int = Field(default=-1, ge=-1, le=255, description="Kolom array: ambil elemen ke-n (mulai 0). -1 = tidak dipakai")


class Join(BaseModel):
    table: str = Field(description="Tabel kedua: nama tabel di schema yang sama, atau catalog.schema.table")
    on: list[str] = Field(description="Kolom penghubung yang ada di kedua tabel, mis. [\"entity_id\", \"period\"]")


MAX_JOIN_KEYS = 3


def resolve_join_table(full_name: str, table: str) -> str:
    """Nama pendek -> schema yang sama dengan tabel utama. Catalog tetap diperiksa allowlist."""
    other = f"{full_name.rsplit('.', 1)[0]}.{table}" if "." not in table else table
    split_full_name(other)
    return other


def array_expr(col: str, m: Metric) -> str:
    if m.at >= 0 and m.array:
        raise ValueError("Pilih salah satu: array atau at")
    if m.at >= 0:
        return f"try_element_at({col}, {int(m.at) + 1})"
    if m.array == "mean":
        return f"(aggregate({col}, CAST(0 AS DOUBLE), (acc, v) -> acc + v) / nullif(size({col}), 0))"
    if m.array in ("max", "min"):
        return f"array_{m.array}({col})"
    return col


def build_aggregate(
    full_name: str,
    known_columns: set[str],
    metrics: list[Metric],
    group_by: list[str] | None = None,
    filters: list[Filter] | None = None,
    order_by: str | None = None,
    desc: bool = True,
    limit: int = 50,
    join: Join | None = None,
    join_name: str | None = None,
    join_columns: set[str] | None = None,
) -> tuple[str, dict[str, str]]:
    """Susun SELECT agregat bertemplate. Sama seperti build_query: nama dan nilai jadi parameter marker."""
    params: dict[str, str] = {"tbl": full_name}
    group_by = group_by or []
    if not metrics or len(metrics) > MAX_METRICS or len(group_by) > MAX_GROUP_BY:
        raise ValueError(f"metrics wajib 1 sampai {MAX_METRICS}, group_by maksimal {MAX_GROUP_BY}")
    joined = join is not None

    def ident(name: str, key: str) -> str:
        side, col = "a", name
        if joined and name[:2] in ("a.", "b."):
            side, col = name[0], name[2:]
        valid = join_columns if side == "b" and joined else known_columns
        if col not in valid:
            hint = " Dengan join, kolom tabel kedua diawali b." if joined else ""
            raise ValueError(f"Kolom '{name}' tidak ada. Kolom yang sah: {sorted(valid or [])}.{hint}")
        params[key] = f"{side}.`{col}`" if joined else f"`{col}`"
        return f"IDENTIFIER(:{key})"

    source = "FROM IDENTIFIER(:tbl)"
    if joined:
        if not join.on or len(join.on) > MAX_JOIN_KEYS:
            raise ValueError(f"join.on wajib 1 sampai {MAX_JOIN_KEYS} kolom")
        for col in join.on:
            if col not in known_columns or col not in (join_columns or set()):
                raise ValueError(f"Kolom join '{col}' harus ada di kedua tabel")
        params["tbl2"] = join_name or ""
        for i, col in enumerate(join.on):
            params[f"ja{i}"], params[f"jb{i}"] = f"a.`{col}`", f"b.`{col}`"
        cond = " AND ".join(f"IDENTIFIER(:ja{i}) = IDENTIFIER(:jb{i})" for i in range(len(join.on)))
        source = f"FROM IDENTIFIER(:tbl) AS a JOIN IDENTIFIER(:tbl2) AS b ON {cond}"

    select = [ident(c, f"g{i}") for i, c in enumerate(group_by)]
    aliases: list[str] = []
    for i, m in enumerate(metrics):
        if not m.column:
            if m.fn != "count":
                raise ValueError(f"fn '{m.fn}' butuh column")
            expr, alias = "COUNT(*)", "n"
        else:
            col = array_expr(ident(m.column, f"m{i}"), m)
            expr = f"COUNT(DISTINCT {col})" if m.fn == "count_distinct" else f"{AGG_FUNCS[m.fn]}({col})"
            suffix = f"_at{m.at}" if m.at >= 0 else (f"_{m.array}" if m.array else "")
            alias = f"{m.fn}_{re.sub(r'[^A-Za-z0-9_]', '_', m.column)}{suffix}"
        if alias in aliases:
            raise ValueError(f"Metrik '{alias}' dobel")
        aliases.append(alias)
        select.append(f"{expr} AS `{alias}`")

    sql = f"SELECT {', '.join(select)} {source}" + where_clause(filters, ident, params)
    if group_by:
        sql += " GROUP BY " + ", ".join(str(i + 1) for i in range(len(group_by)))
        target = order_by or aliases[0]
        if target in aliases:
            sql += f" ORDER BY `{target}`"
        elif target in group_by:
            sql += f" ORDER BY {group_by.index(target) + 1}"
        else:
            raise ValueError(f"order_by '{target}' harus salah satu dari {group_by + aliases}")
        sql += " DESC" if desc else ""
    sql += f" LIMIT {clamp(limit)}"
    return sql, params


@mcp.tool()
def aggregate_table(
    full_name: str,
    metrics: list[Metric],
    group_by: list[str] = [],  # noqa: B006
    filters: list[Filter] = [],  # noqa: B006
    order_by: str = "",
    desc: bool = True,
    limit: int = 50,
    join: Join | None = None,
) -> str:
    """Agregat satu tabel, opsional digabung tabel kedua (read-only). Pakai ini, bukan query_table, untuk statistik,
    distribusi, perbandingan antar kelas, confusion matrix, dan profil fitur per kelas.

    metrics: [{"fn": "count"}, {"fn": "avg", "column": "pred_score"}] -> n, avg_pred_score. fn: count, count_distinct, avg, min, max, sum, stddev, median.
    group_by: mis. ["truth_class", "pred_class"]; group_by entity_id = satu baris per entity. filters: seperti query_table.
    join: {"table": "ft_point_band", "on": ["entity_id", "period"]} menggabung tabel kedua; kolom tabel kedua diawali b.
    (mis. "b.ndvi"). Kolom array: array="mean"|"max"|"min" meringkas tiap baris dulu, at=n mengambil elemen ke-n.
    """
    other = resolve_join_table(full_name, join.table) if join else None
    return with_columns(
        full_name,
        lambda known, other_cols: build_aggregate(
            full_name, known, metrics, group_by, filters, order_by, desc, limit, join, other, other_cols
        ),
        other,
    )


# ---------- helper ----------


def ts(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ms else None


def duration(start_ms: int | None, end_ms: int | None) -> str | None:
    if not start_ms or not end_ms:
        return None
    minutes, seconds = divmod(int((end_ms - start_ms) / 1000), 60)
    return f"{minutes}m{seconds:02d}s"


def clamp(limit: int) -> int:
    return max(1, min(int(limit), max_rows()))


# ---------- mlflow ----------

MAX_METRIC_NAMES = 40
RUN_TAGS = ("track", "model_version_label", "git_branch", "git_commit")


def experiment_prefix() -> str:
    return os.environ.get("ALLOWED_EXPERIMENT_PREFIX", "/Shared/mody/")


def normalize_experiment(path: str) -> str:
    """API MLflow menyimpan nama tanpa awalan /Workspace, jadi keduanya disamakan."""
    return path.removeprefix("/Workspace")


def check_experiment(name: str) -> None:
    prefix = normalize_experiment(experiment_prefix())
    path = normalize_experiment(name)
    if prefix and (not path.startswith(prefix) or ".." in path.split("/")):
        raise ValueError(f"Experiment '{name}' tidak diizinkan. Harus berawalan '{prefix}'. Kosongkan experiment_path untuk mencari di semua experiment")


MAX_SEARCH_EXPERIMENTS = 20


def allowed_experiments(limit: int) -> list[Any]:
    """Experiment dengan awalan yang diizinkan, terbaru diubah dulu. Filter di server Databricks, startswith jadi pengaman."""
    prefix = normalize_experiment(experiment_prefix())
    pattern = prefix.replace("'", "''") + "%"
    found = ws().experiments.search_experiments(
        filter=f"name LIKE '{pattern}'", order_by=["last_update_time DESC"], max_results=min(limit * 2, 3000)
    )
    return list(islice((e for e in found if normalize_experiment(e.name or "").startswith(prefix)), limit))


@mcp.tool()
def search_runs(
    experiment_path: str = "",
    filter: str = "",
    order_by: str = "",
    metrics: list[str] = [],  # noqa: B006
    limit: int = 10,
    run_id: str = "",
) -> str:
    """Run MLflow (bukan Job Databricks), terbaru dulu. Kolom experiment menunjukkan path experiment.

    experiment_path kosong = cari di semua experiment yang diizinkan sekaligus.
    filter: sintaks MLflow, mis. "tags.git_branch = 'agent/x'" atau "metrics.macro_f1 > 0.7".
    order_by: mis. "metrics.macro_f1 DESC". Untuk mencari run terbaik: order_by metrik + limit kecil
    (nama metrik ada di baris "metrik tersedia"). metrics: metrik yang ditampilkan, kosong = semua.
    limit=1 menyertakan detail lengkap (params, metrics, tags) run teratas.
    run_id: detail lengkap satu run (argumen lain diabaikan).
    """
    if run_id:
        return get_run(run_id)
    if experiment_path:
        check_experiment(experiment_path)
        experiments = [ws().experiments.get_by_name(normalize_experiment(experiment_path)).experiment]
    else:
        experiments = allowed_experiments(MAX_SEARCH_EXPERIMENTS)
        if not experiments:
            raise ValueError("Tidak ada experiment yang diizinkan")
    multi = not experiment_path
    names = {e.experiment_id: e.name for e in experiments}
    runs = islice(
        ws().experiments.search_runs(
            experiment_ids=[e.experiment_id for e in experiments],
            filter=filter or None,
            order_by=[order_by] if order_by else None,
            max_results=clamp(limit),
        ),
        clamp(limit),
    )
    rows, available = [], set()
    for r in runs:
        tags = {t.key: t.value for t in r.data.tags or []}
        values = {m.key: m.value for m in r.data.metrics or []}
        available.update(values)
        shown = metrics or sorted(values)
        rows.append([
            *([names.get(r.info.experiment_id)] if multi else []),
            r.info.run_id, r.info.run_name, r.info.status.value if r.info.status else None, ts(r.info.start_time),
            *(tags.get(k) for k in RUN_TAGS),
            ", ".join(f"{k}={values[k]:.4g}" for k in shown if k in values),
        ])
    head = f"{len(rows)} run di " + (f"{len(experiments)} experiment" if multi else experiments[0].name)
    if available:
        head += f"\nmetrik tersedia: {', '.join(sorted(available)[:MAX_METRIC_NAMES])}"
    out = head + "\n" + md_table(
        [*(["experiment"] if multi else []), "run_id", "run_name", "status", "start", *RUN_TAGS, "metrics"], rows
    )
    if clamp(limit) == 1 and rows:
        out += "\n\n" + get_run(rows[0][1 if multi else 0])
    return out


def get_run(run_id: str) -> str:
    """Detail satu run MLflow: params, metrics, tags. Hanya run dari experiment yang diizinkan."""
    run = ws().experiments.get_run(run_id).run
    experiment = ws().experiments.get_experiment(run.info.experiment_id).experiment
    check_experiment(experiment.name)
    params = sorted((p.key, p.value) for p in run.data.params or [])
    metrics = sorted((m.key, m.value) for m in run.data.metrics or [])
    tags = sorted((t.key, t.value) for t in run.data.tags or [] if not t.key.startswith("mlflow."))
    return (
        f"## run {run.info.run_id} ({cell(run.info.run_name)})\n"
        f"- experiment: {experiment.name}\n"
        f"- status: {cell(run.info.status.value if run.info.status else None)}, "
        f"start: {cell(ts(run.info.start_time))}, durasi: {cell(duration(run.info.start_time, run.info.end_time))}\n\n"
        f"### params ({len(params)})\n{md_table(['key', 'value'], params)}\n\n"
        f"### metrics ({len(metrics)})\n{md_table(['key', 'value'], metrics)}\n\n"
        f"### tags ({len(tags)})\n{md_table(['key', 'value'], tags)}"
    )


MAX_VERSIONS_WITH_METRICS = 10


def run_metrics(run_id: str | None, wanted: list[str], allowed: dict[str, bool]) -> str:
    """Metrik satu run, hanya bila experiment-nya diizinkan. allowed = cache hasil cek per experiment."""
    if not run_id:
        return "-"
    try:
        run = ws().experiments.get_run(run_id).run
        eid = run.info.experiment_id
        if eid not in allowed:
            try:
                check_experiment(ws().experiments.get_experiment(eid).experiment.name)
                allowed[eid] = True
            except ValueError:
                allowed[eid] = False
    except DatabricksError:
        return "(run tidak tersedia)"
    if not allowed[eid]:
        return "(di luar experiment yang diizinkan)"
    values = {m.key: m.value for m in run.data.metrics or []}
    shown = ", ".join(f"{k}={values[k]:.4g}" for k in wanted if k in values)
    return shown or f"(tidak ada {'/'.join(wanted)}; tersedia: {', '.join(sorted(values)[:8]) or '-'})"


def list_model_versions(full_name: str, limit: int = 20, metrics: list[str] = []) -> str:  # noqa: B006
    """Versi model Unity Catalog beserta alias, terbaru dulu. Dipakai list_models(full_name=...)."""
    split_full_name(full_name)
    model = ws().registered_models.get(full_name, include_aliases=True)
    aliases: dict[int, list[str]] = {}
    for a in model.aliases or []:
        aliases.setdefault(int(a.version_num), []).append(a.alias_name)
    versions = sorted(ws().model_versions.list(full_name), key=lambda v: int(v.version), reverse=True)
    versions = versions[: min(clamp(limit), MAX_VERSIONS_WITH_METRICS) if metrics else clamp(limit)]
    allowed: dict[str, bool] = {}
    rows = [
        [v.version, ", ".join(aliases.get(int(v.version), [])), v.run_id,
         v.status.value if v.status else None, ts(v.created_at), v.comment,
         *([run_metrics(v.run_id, metrics, allowed)] if metrics else [])]
        for v in versions
    ]
    return f"{len(rows)} versi {full_name}\n" + md_table(
        ["version", "aliases", "run_id", "status", "created", "comment", *(["metrics"] if metrics else [])], rows
    )


# ---------- jobs ----------

TRACE_TAIL = 3000
MAX_JOB_SCAN = 500
MARKUP = re.compile(r"<[^>]+>|\x1b\[[0-9;]*[A-Za-z]")  # tag HTML dan kode warna ANSI dari notebook


def plain(text: str | None) -> str:
    return MARKUP.sub("", text or "").strip()


CONFIG_LEAK = re.compile(r"\s*Config:[^\n]*")


def scrub(text: str) -> str:
    """Seperti redact, tapi per baris dan tanpa batas panjang: untuk error dan trace task."""
    text = CONFIG_LEAK.sub("", text)
    for pattern, repl in REDACTIONS:
        if "Config" not in pattern.pattern:  # versi re.S membuang seluruh trace sesudahnya
            text = pattern.sub(repl, text)
    return text.strip()


def job_prefix() -> str:
    return os.environ.get("ALLOWED_JOB_PREFIX", "mody_")


def check_job_name(name: str) -> None:
    prefix = job_prefix()
    if prefix and not name.startswith(prefix):
        raise ValueError(f"Job '{name}' tidak diizinkan. Harus berawalan '{prefix}'")


def check_job_id(job_id: int | None) -> str:
    """Pastikan run berasal dari job yang diizinkan. Mengembalikan nama job."""
    if job_id is None:
        raise ValueError("Run ini bukan bagian dari job, tidak diizinkan")
    name = ws().jobs.get(job_id).settings.name
    check_job_name(name)
    return name


def run_state(state: Any) -> str | None:
    if not state:
        return None
    result = state.result_state.value if state.result_state else None
    return result or (state.life_cycle_state.value if state.life_cycle_state else None)


def job_params(run: Any) -> dict[str, str]:
    """Nilai efektif parameter job. value bernilai None kalau run memakai default."""
    return {p.name: p.value if p.value is not None else p.default for p in run.job_parameters or []}


FAILED_STATES = {"FAILED", "TIMEDOUT", "INTERNAL_ERROR"}
MAX_FAILED_TASKS = 3
TASK_TRACE_TAIL = 1500


@mcp.tool()
def job_runs(job_name: str = "", run_id: int = 0, limit: int = 10) -> str:
    """Job Databricks (awalan ALLOWED_JOB_PREFIX), bukan run MLflow. Satu panggilan untuk tiap kebutuhan:

    - tanpa argumen: daftar job.
    - job_name (mis. mody_train_dev): run terakhir job itu + detail run terbaru (params, tasks, error dan trace task gagal).
    - run_id (angka): detail satu run beserta error task yang gagal.
    Kolom overrides berisi parameter yang diubah dari default.
    """
    if run_id:
        return get_job_run(run_id)
    if not job_name:
        return list_jobs(limit=50)
    listing, latest = list_job_runs(job_name, limit)
    return listing + (f"\n\n{get_job_run(latest)}" if latest else "")


def list_jobs(limit: int = 50) -> str:
    prefix, scanned, rows = job_prefix(), 0, []
    for j in ws().jobs.list(limit=100):
        scanned += 1
        if j.settings and (j.settings.name or "").startswith(prefix):
            rows.append([j.settings.name, j.job_id])
        if len(rows) >= clamp(limit) or scanned >= MAX_JOB_SCAN:
            break
    note = ""
    if len(rows) < clamp(limit) and scanned >= MAX_JOB_SCAN:
        note = f" - hanya {MAX_JOB_SCAN} job pertama diperiksa, daftar mungkin tidak lengkap. Kalau tahu namanya, langsung isi job_name"
    return f"{len(rows)} job{note}\n" + md_table(["job_name", "job_id"], rows)


def list_job_runs(job_name: str, limit: int = 10) -> tuple[str, int | None]:
    """Tabel run terakhir satu job dan run_id terbaru (None kalau belum pernah jalan)."""
    check_job_name(job_name)
    job = next(iter(ws().jobs.list(name=job_name)), None)
    if job is None:
        raise ValueError(f"Job '{job_name}' tidak ditemukan. Kosongkan job_name untuk daftar job")
    runs = list(islice(ws().jobs.list_runs(job_id=job.job_id, limit=min(clamp(limit), 25)), clamp(limit)))
    rows = [
        [r.run_id, run_state(r.state), ts(r.start_time), duration(r.start_time, r.end_time),
         r.trigger.value if r.trigger else None,
         ", ".join(f"{p.name}={p.value}" for p in r.job_parameters or [] if p.value not in (None, p.default))]
        for r in runs
    ]
    table = f"{len(rows)} run {job_name}\n" + md_table(["run_id", "state", "start", "durasi", "trigger", "overrides"], rows)
    return table, runs[0].run_id if runs else None


def get_job_run(run_id: int) -> str:
    """Status satu run dan tiap task-nya, plus error task yang gagal. Run yang di-repair menampilkan semua attempt."""
    run = ws().jobs.get_run(run_id, include_history=True)
    name = check_job_id(run.job_id)
    tasks = sorted(run.tasks or [], key=lambda t: (t.start_time or 0, t.attempt_number or 0))
    repairs = [h for h in run.repair_history or [] if h.type and h.type.value == "REPAIR"]
    repair_note = (
        f"{len(repairs)}x, attempt sebelumnya: "
        + ", ".join(cell(run_state(h.state)) for h in (run.repair_history or [])[:-1])
        if repairs else "-"
    )
    final = {t.task_key: t for t in tasks}  # attempt terakhir per task; attempt lama yang sudah di-repair diabaikan
    failed = [t for t in final.values() if run_state(t.state) in FAILED_STATES][-MAX_FAILED_TASKS:]
    errors = "".join(f"\n\n{get_task_output(t.run_id, checked=True, tail=TASK_TRACE_TAIL)}" for t in failed)
    return (
        f"## {name} run {run.run_id}\n"
        f"- state: {cell(run_state(run.state))}, start: {cell(ts(run.start_time))}, "
        f"durasi: {cell(duration(run.start_time, run.end_time))}\n"
        f"- repair: {repair_note}\n"
        f"- message: {cell(run.state.state_message if run.state else None)}\n"
        f"- url: {cell(run.run_page_url)}\n\n"
        f"### params\n{md_table(['name', 'value'], sorted(job_params(run).items()))}\n\n"
        f"### tasks ({len(tasks)})\n"
        + md_table(
            ["task_key", "task_run_id", "attempt", "state", "start", "durasi"],
            [[t.task_key, t.run_id, t.attempt_number, run_state(t.state), ts(t.start_time),
              duration(t.start_time, t.end_time)] for t in tasks],
        )
        + errors
    )


def get_task_output(task_run_id: int, checked: bool = False, tail: int = TRACE_TAIL) -> str:
    """Error dan stack trace satu task, dipotong ke bagian akhir. checked=True: job sudah diverifikasi pemanggil."""
    out = ws().jobs.get_run_output(task_run_id)
    if not checked:
        check_job_id(out.metadata.job_id if out.metadata else None)
    error = scrub(plain(out.error)) or "(tidak ada error)"
    trace = scrub(plain(out.error_trace))
    if len(trace) > tail:
        trace = "...\n" + trace[-tail:]
    state = run_state(out.metadata.state if out.metadata else None)
    return (
        f"### task run {task_run_id} ({cell(state)})\n"
        f"error: {error}\n"
        f"```\n{trace or '(trace kosong)'}\n```"
    )


# ---------- github ----------

GITHUB_API = "https://api.github.com"
DEFAULT_ALLOWED_PATHS = "usecases/*/*.yml,src/mody_ml/usecases/*/*.py,tests/unit/test_*.py,notebooks/training/*/*"
DENIED_PATHS = (".github/",)  # selalu ditolak, walau GITHUB_ALLOWED_PATHS diperluas
# File yang dieksekusi otomatis saat import/pytest/build. Selalu ditolak.
DENIED_NAMES = {"conftest.py", "__init__.py", "setup.py", "pyproject.toml"}
MAX_COMMIT_FILES = 20
MAX_COMMIT_CHARS = 500_000
SAFE_NAME = re.compile(r"[A-Za-z0-9._/-]+")
MAX_FILE_CHARS = 60_000
LOG_TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ", re.MULTILINE)


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class FileChange(BaseModel):
    path: str
    content: str | None = None  # None = hapus file


def github_repo() -> str:
    repo = os.environ.get("GITHUB_REPO", "")
    if not SAFE_NAME.fullmatch(repo) or repo.count("/") != 1:
        raise ValueError("GITHUB_REPO belum diset di server (format owner/name)")
    return repo


def base_branch() -> str:
    return os.environ.get("GITHUB_BASE_BRANCH", "main")


def branch_prefix() -> str:
    return os.environ.get("GITHUB_BRANCH_PREFIX", "agent/")


def allowed_paths() -> list[str]:
    raw = os.environ.get("GITHUB_ALLOWED_PATHS", DEFAULT_ALLOWED_PATHS)
    return [p.strip() for p in raw.split(",") if p.strip()]


@cache
def gh() -> httpx.Client:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ValueError("GITHUB_TOKEN belum diset di server")
    # follow_redirects untuk log Actions; httpx membuang header Authorization saat redirect ke host lain.
    return httpx.Client(
        base_url=GITHUB_API,
        timeout=30,
        follow_redirects=True,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def gh_call(method: str, path: str, **kwargs: Any) -> httpx.Response:
    """Panggil REST API GitHub untuk GITHUB_REPO. path relatif terhadap /repos/<owner>/<name>."""
    resp = gh().request(method, f"/repos/{github_repo()}{path}", **kwargs)
    if resp.is_error:
        is_json = "json" in resp.headers.get("content-type", "")
        message = resp.json().get("message", "") if is_json else resp.text[:200]
        raise GitHubError(resp.status_code, f"GitHub {method} {path}: {resp.status_code} {message}")
    return resp


def check_relative(path: str) -> None:
    """Tolak path absolut, '..', dan karakter yang bisa mengubah URL API."""
    if not SAFE_NAME.fullmatch(path) or any(p in ("", ".", "..") for p in path.split("/")):
        raise ValueError(f"Path '{path}' tidak valid. Pakai path relatif dari root repo, misalnya src/mody_ml/usecases")


def is_writable(path: str) -> bool:
    # ponytail: '*' di fnmatch melewati '/', jadi kedalaman path wajib sama dengan pola. Butuh '**': PurePath.full_match (3.13).
    if path.startswith(DENIED_PATHS) or path.rsplit("/", 1)[-1] in DENIED_NAMES:
        return False
    return any(path.count("/") == p.count("/") and fnmatch.fnmatchcase(path, p) for p in allowed_paths())


def check_path(path: str) -> None:
    check_relative(path)
    if not is_writable(path):
        raise ValueError(f"Path '{path}' tidak boleh diubah agent. Yang diizinkan: {allowed_paths()}")


def check_branch(branch: str) -> None:
    prefix = branch_prefix()
    name = branch.removeprefix(prefix)
    if not branch.startswith(prefix) or not name or not SAFE_NAME.fullmatch(name) or ".." in branch:
        raise ValueError(f"Branch '{branch}' tidak diizinkan. Harus berawalan '{prefix}', misalnya {prefix}tune-lr")


def branch_sha(branch: str) -> str | None:
    try:
        return gh_call("GET", f"/git/ref/heads/{branch}").json()["object"]["sha"]
    except GitHubError as e:
        if e.status == 404:
            return None
        raise


def ref_params(ref: str) -> dict[str, str] | None:
    return {"ref": ref} if ref else None


@mcp.tool()
def github_read(path: str = "", ref: str = "") -> str:
    """Baca repo: path folder = daftar isi (path kosong = root), path file = isi file teks (file panjang dipotong).

    ref = branch atau commit, kosong = branch utama. editable = yes kalau boleh diubah lewat github_commit_files.
    """
    if path:
        check_relative(path)
    item = gh_call("GET", f"/contents/{path}", params=ref_params(ref)).json()
    if isinstance(item, list):
        rows = [
            [i["type"], i["path"], i["size"] if i["type"] == "file" else None, "yes" if is_writable(i["path"]) else "no"]
            for i in item
        ]
        return f"{len(rows)} entri di /{path}\n" + md_table(["type", "path", "size", "editable"], rows)
    if item.get("encoding") != "base64":
        raise ValueError(f"'{path}' terlalu besar untuk dibaca lewat API ({item.get('size')} byte)")
    try:
        text = base64.b64decode(item["content"]).decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"'{path}' adalah file biner") from None
    note = f"\n... (dipotong, {len(text)} karakter)" if len(text) > MAX_FILE_CHARS else ""
    return (
        f"## {path} ({ref or base_branch()}, editable: {'yes' if is_writable(path) else 'no'})\n"
        f"```\n{text[:MAX_FILE_CHARS]}\n```{note}"
    )


@mcp.tool()
def github_commit_files(branch: str, message: str, files: list[FileChange], pr_title: str = "", pr_body: str = "") -> str:
    """Satu commit berisi satu atau beberapa file ke branch agent. Branch dibuat dari branch utama kalau belum ada.

    files: [{"path": "usecases/<uc>/usecase.yml", "content": "<isi lengkap file>"}]. content null = hapus file.
    Isi file ditulis utuh (bukan diff). Hanya path dengan editable = yes yang diterima.
    pr_title diisi = sekaligus buka draft PR ke branch utama (review dan merge oleh manusia).
    """
    check_branch(branch)
    paths = [f.path for f in files]
    if not files or len(set(paths)) != len(paths):
        raise ValueError("files harus berisi minimal satu file dan tiap path hanya sekali")
    if len(files) > MAX_COMMIT_FILES or sum(len(f.content or "") for f in files) > MAX_COMMIT_CHARS:
        raise ValueError(f"Maksimal {MAX_COMMIT_FILES} file dan {MAX_COMMIT_CHARS} karakter per commit")
    for path in paths:
        check_path(path)
    if not message.strip():
        raise ValueError("message commit wajib diisi")

    head = branch_sha(branch)
    is_new = head is None
    parent = head or branch_sha(base_branch())
    if parent is None:
        raise ValueError(f"Branch utama '{base_branch()}' tidak ditemukan")

    base_tree = gh_call("GET", f"/git/commits/{parent}").json()["tree"]["sha"]
    entries = [
        {"path": f.path, "mode": "100644", "type": "blob", **({"sha": None} if f.content is None else {"content": f.content})}
        for f in files
    ]
    tree = gh_call("POST", "/git/trees", json={"base_tree": base_tree, "tree": entries}).json()["sha"]
    commit = gh_call("POST", "/git/commits", json={"message": message, "tree": tree, "parents": [parent]}).json()
    if is_new:
        gh_call("POST", "/git/refs", json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
    else:
        gh_call("PATCH", f"/git/refs/heads/{branch}", json={"sha": commit["sha"], "force": False})

    out = (
        f"Commit {commit['sha'][:12]} ke {branch}{' (branch baru)' if is_new else ''}\n"
        f"- url: {commit.get('html_url')}\n\n"
        + md_table(["path", "aksi"], [[f.path, "hapus" if f.content is None else "tulis"] for f in files])
    )
    if pr_title.strip():
        try:
            out += "\n\n" + github_create_pr(branch, pr_title, pr_body)
        except GitHubError as e:
            if e.status != 422:
                raise
            out += "\n\nPR tidak dibuka: kemungkinan sudah ada PR untuk branch ini (pakai github_update_pr)"
    return out


def agent_pr(number: int) -> dict[str, Any]:
    """PR yang boleh diubah agent: dari branch agent di repo yang sama (bukan fork)."""
    pr = gh_call("GET", f"/pulls/{number}").json()
    check_branch(pr["head"]["ref"])
    if (pr["head"].get("repo") or {}).get("full_name") != github_repo():
        raise ValueError(f"PR #{number} berasal dari fork, tidak diizinkan")
    return pr


def github_create_pr(branch: str, title: str, body: str) -> str:
    """Buka draft pull request dari branch agent ke branch utama."""
    check_branch(branch)
    pr = gh_call(
        "POST", "/pulls", json={"head": branch, "base": base_branch(), "title": title, "body": body, "draft": True}
    ).json()
    return f"PR #{pr['number']} dibuka (draft): {pr['html_url']}"


@mcp.tool()
def github_update_pr(number: int, title: str | None = None, body: str | None = None) -> str:
    """Ubah judul atau deskripsi PR milik agent, misalnya menambah metrik dan run_id MLflow hasil training."""
    changes = {k: v for k, v in {"title": title, "body": body}.items() if v is not None}
    if not changes:
        raise ValueError("Isi title atau body")
    agent_pr(number)
    pr = gh_call("PATCH", f"/pulls/{number}", json=changes).json()
    return f"PR #{number} diperbarui: {pr['html_url']}"


FAILED_CONCLUSIONS = {"failure", "timed_out", "startup_failure"}
MAX_FAILED_LOGS = 3
CI_LOG_TAIL = 1500


@mcp.tool()
def github_pr_status(number: int) -> str:
    """Status PR agent dan check CI di commit terakhirnya, termasuk bagian akhir log check yang gagal (tanpa panggilan lain)."""
    pr = agent_pr(number)
    checks = gh_call("GET", f"/commits/{pr['head']['sha']}/check-runs", params={"per_page": 50}).json()["check_runs"]
    failed = [c for c in checks if c.get("conclusion") in FAILED_CONCLUSIONS][:MAX_FAILED_LOGS]
    logs = "".join(f"\n\n{github_ci_log(c['id'], checked=True, tail=CI_LOG_TAIL, name=c['name'])}" for c in failed)
    return (
        f"## PR #{number} {cell(pr['title'])}\n"
        f"- state: {pr['state']}{' (draft)' if pr.get('draft') else ''}, merged: {cell(pr.get('merged'))}, "
        f"mergeable: {cell(pr.get('mergeable'))}\n"
        f"- branch: {pr['head']['ref']} -> {pr['base']['ref']}, head: {pr['head']['sha'][:12]}\n"
        f"- url: {pr['html_url']}\n\n"
        f"### checks ({len(checks)})\n"
        + md_table(["name", "status", "conclusion", "job_id"],
                   [[c["name"], c["status"], c.get("conclusion"), c["id"]] for c in checks])
        + logs
    )


def github_ci_log(job_id: int, checked: bool = False, tail: int = TRACE_TAIL, name: str = "") -> str:
    """Bagian akhir log satu job GitHub Actions, tanpa timestamp dan kode warna. checked=True: job dari PR agent."""
    if not checked:
        check_branch(gh_call("GET", f"/actions/jobs/{job_id}").json().get("head_branch") or "")
    title = f"### log {name} (job {job_id})" if name else f"### log job {job_id}"
    try:
        log = LOG_TIMESTAMP.sub("", plain(gh_call("GET", f"/actions/jobs/{job_id}/logs").text))
    except GitHubError as e:
        if not checked or e.status not in (404, 410):
            raise
        return f"{title}\n(log tidak tersedia, mungkin check bukan GitHub Actions atau sudah kedaluwarsa)"
    marker = log.rfind("##[error]")
    if marker >= 0:  # langkah cleanup sesudah error tidak berguna, potong di baris error terakhir
        end = log.find("\n", marker)
        log = log[: end if end >= 0 else len(log)]
    if len(log) > tail:
        log = "...\n" + log[-tail:]
    return f"{title}\n```\n{log or '(kosong)'}\n```"


# ---------- HTTP ----------


class BearerAuth:
    """Middleware ASGI: setiap request HTTP wajib membawa Authorization: Bearer <MCP_API_KEY>."""

    def __init__(self, app: Any, api_key: str):
        self.app = app
        self.expected = f"Bearer {api_key}".encode()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "lifespan":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self.app(scope, receive, send)


def check_config(api_key: str) -> None:
    """Fail closed: pembatas yang kosong berarti semua diizinkan, jadi server menolak start."""
    if len(api_key) < MIN_API_KEY_LEN:
        raise SystemExit(f"MCP_API_KEY wajib diisi, minimal {MIN_API_KEY_LEN} karakter")
    if not allowed_catalogs():
        raise SystemExit("ALLOWED_CATALOGS wajib diisi")
    for name, value in (("ALLOWED_EXPERIMENT_PREFIX", experiment_prefix()), ("ALLOWED_JOB_PREFIX", job_prefix())):
        if not value.strip():
            raise SystemExit(f"{name} tidak boleh kosong")
    if not branch_prefix().strip() or base_branch().startswith(branch_prefix()):
        raise SystemExit("GITHUB_BRANCH_PREFIX tidak boleh kosong atau mencakup branch utama")


def create_app(api_key: str) -> BearerAuth:
    check_config(api_key)
    return BearerAuth(mcp.streamable_http_app(), api_key)


if __name__ == "__main__":
    import uvicorn

    # Handler sendiri: FastMCP memasang rich handler di root (multi-baris), SIEM butuh satu baris JSON.
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    audit.addHandler(handler)
    audit.setLevel(logging.INFO)
    audit.propagate = False

    uvicorn.run(create_app(os.environ.get("MCP_API_KEY", "")), host=HOST, port=PORT)
