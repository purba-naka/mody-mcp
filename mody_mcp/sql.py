"""Tool data lewat SQL warehouse: preview_table, query_table. Hanya SELECT bertemplate dengan parameter marker."""

from __future__ import annotations

import os
from typing import Any

from databricks.sdk.service.sql import (
    ExecuteStatementRequestOnWaitTimeout,
    StatementParameterListItem,
    StatementState,
)
from pydantic import BaseModel

from mody_mcp import core
from mody_mcp.core import clamp, max_rows, mcp, md_table, split_full_name

OPS = {"=", "!=", "<", "<=", ">", ">=", "IN", "IS NULL", "IS NOT NULL"}
MAX_IN_VALUES = 100


class Filter(BaseModel):
    column: str
    op: str
    value: str | int | float | bool | list[str | int | float | bool] | None = None


def build_query(
    full_name: str,
    known_columns: set[str],
    columns: list[str] | None = None,
    filters: list[Filter] | None = None,
    order_by: str | None = None,
    desc: bool = False,
    limit: int = 20,
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
    if where:
        sql += " WHERE " + " AND ".join(where)

    if order_by:
        sql += f" ORDER BY {ident(order_by, 'o')}" + (" DESC" if desc else "")

    sql += f" LIMIT {clamp(limit)}"
    return sql, params


def run_query(sql: str, params: dict[str, str]) -> str:
    warehouse_id = os.environ.get("DATABRICKS_SQL_WAREHOUSE_ID")
    if not warehouse_id:
        raise ValueError("DATABRICKS_SQL_WAREHOUSE_ID belum diset di server")
    # ponytail: semua parameter dikirim sebagai STRING, Databricks SQL meng-cast implisit
    # (period >= '2026-09-01', score > '0.5'). Tambah tipe per parameter kalau ada cast yang gagal.
    resp = core.ws().statement_execution.execute_statement(
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
    return f"{len(rows)} baris\n" + md_table(headers, rows)


def query(full_name: str, **kwargs: Any) -> str:
    split_full_name(full_name)
    table = core.ws().tables.get(full_name=full_name)
    known = {c.name for c in table.columns or []}
    return run_query(*build_query(full_name, known, **kwargs))


@mcp.tool()
def preview_table(full_name: str, limit: int = 20) -> str:
    """Contoh baris dari satu tabel. full_name = catalog.schema.table. limit dibatasi MAX_ROWS server."""
    return query(full_name, limit=limit)


@mcp.tool()
def query_table(
    full_name: str,
    columns: list[str] | None = None,
    filters: list[Filter] | None = None,
    order_by: str | None = None,
    desc: bool = False,
    limit: int = 20,
) -> str:
    """Ambil baris tertentu dari satu tabel, read-only.

    columns: kolom yang diambil, kosong = semua.
    filters: digabung AND. op salah satu =, !=, <, <=, >, >=, IN, IS NULL, IS NOT NULL.
      Contoh: [{"column": "scope", "op": "=", "value": "production"},
               {"column": "pred_class", "op": "IN", "value": ["padi", "jagung"]}]
    order_by + desc: urutan. limit dibatasi MAX_ROWS server.
    Pakai describe_table dulu untuk tahu nama kolom yang sah.
    """
    return query(full_name, columns=columns, filters=filters, order_by=order_by, desc=desc, limit=limit)
