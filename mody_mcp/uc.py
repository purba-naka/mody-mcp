"""Tool metadata Unity Catalog: catalog, schema, tabel, kolom. Tidak memakai SQL warehouse."""

from __future__ import annotations

from mody_mcp import core
from mody_mcp.core import (
    allowed_catalogs,
    cell,
    check_catalog,
    columns_table,
    mcp,
    md_table,
    split_full_name,
)


@mcp.tool()
def list_catalogs() -> str:
    """Daftar catalog Unity Catalog yang bisa diakses. Mulai dari sini kalau belum tahu nama catalog."""
    allowed = allowed_catalogs()
    rows = [[c.name, c.comment] for c in core.ws().catalogs.list() if c.name in allowed]
    return md_table(["catalog", "comment"], rows)


@mcp.tool()
def list_schemas(catalog: str) -> str:
    """Daftar schema di satu catalog, dengan komentarnya."""
    check_catalog(catalog)
    rows = [[s.name, s.comment] for s in core.ws().schemas.list(catalog_name=catalog)]
    return f"Schema di {catalog} ({len(rows)}):\n" + md_table(["schema", "comment"], rows)


@mcp.tool()
def list_tables(catalog: str, schema: str, include_columns: bool = False) -> str:
    """Daftar tabel di satu schema. include_columns=True menambahkan kolom tiap tabel (output lebih panjang)."""
    check_catalog(catalog)
    tables = list(core.ws().tables.list(catalog_name=catalog, schema_name=schema, omit_columns=not include_columns))
    out = f"Tabel di {catalog}.{schema} ({len(tables)}):\n" + md_table(
        ["table", "type", "comment"],
        [[t.name, t.table_type.value if t.table_type else None, t.comment] for t in tables],
    )
    if include_columns:
        out += "".join(f"\n\n### {t.name}\n{columns_table(t.columns)}" for t in tables)
    return out


@mcp.tool()
def describe_table(full_name: str) -> str:
    """Struktur lengkap satu tabel: tipe, format, owner, komentar, dan semua kolom. full_name = catalog.schema.table."""
    split_full_name(full_name)
    t = core.ws().tables.get(full_name=full_name)
    return (
        f"## {t.full_name}\n"
        f"- type: {cell(t.table_type.value if t.table_type else None)}, "
        f"format: {cell(t.data_source_format.value if t.data_source_format else None)}\n"
        f"- owner: {cell(t.owner)}\n"
        f"- comment: {cell(t.comment)}\n\n" + columns_table(t.columns)
    )
