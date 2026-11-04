from types import SimpleNamespace as NS

import pytest
from conftest import TBL

from mody_mcp import sql
from mody_mcp.sql import Filter, build_query

COLS = {"scope", "period", "pred_class", "truth_class"}


def test_query_uses_only_parameter_markers():
    q, params = build_query(
        TBL, COLS,
        columns=["period", "pred_class"],
        filters=[Filter(column="scope", op="=", value="x'; DROP TABLE t; --"),
                 Filter(column="pred_class", op="in", value=["padi", "jagung"]),
                 Filter(column="truth_class", op="IS NOT NULL")],
        order_by="period", desc=True, limit=50,
    )
    assert q == (
        "SELECT IDENTIFIER(:c0), IDENTIFIER(:c1) FROM IDENTIFIER(:tbl)"
        " WHERE IDENTIFIER(:f0) = :v0 AND IDENTIFIER(:f1) IN (:v1_0, :v1_1) AND IDENTIFIER(:f2) IS NOT NULL"
        " ORDER BY IDENTIFIER(:o) DESC LIMIT 50"
    )
    assert "DROP" not in q
    assert params["v0"] == "x'; DROP TABLE t; --"
    assert params["tbl"] == TBL and params["c0"] == "`period`"
    assert params["v1_0"] == "padi" and params["v1_1"] == "jagung"


@pytest.mark.parametrize("kwargs, msg", [
    ({"columns": ["nope"]}, "tidak ada"),
    ({"order_by": "x; DROP"}, "tidak ada"),
    ({"filters": [Filter(column="scope", op="LIKE", value="a")]}, "tidak didukung"),
    ({"filters": [Filter(column="scope", op="=")]}, "satu value"),
    ({"filters": [Filter(column="scope", op="IN", value=[])]}, "1 sampai 100"),
    ({"filters": [Filter(column="scope", op="IN", value=["x"] * 101)]}, "1 sampai 100"),
])
def test_query_rejects_bad_input(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        build_query(TBL, COLS, **kwargs)


def test_limit_clamped(monkeypatch):
    monkeypatch.setenv("MAX_ROWS", "10")
    assert build_query(TBL, COLS, limit=999)[0].endswith("LIMIT 10")
    assert build_query(TBL, COLS, limit=0)[0].endswith("LIMIT 1")


def test_query_table_runs_and_formats(fake):
    out = sql.query_table(TBL, filters=[Filter(column="scope", op="=", value="production")], limit=5)
    assert fake.sent["warehouse_id"] == "wh"
    assert "IDENTIFIER(:f0) = :v0" in fake.sent["statement"]
    assert out.startswith("2 baris")
    assert "a\\|b c" in out and "..." in out  # pipe di-escape, newline dibuang, sel panjang dipotong


def test_query_table_rejects_unknown_column(fake):
    with pytest.raises(ValueError, match="tidak ada"):
        sql.query_table(TBL, columns=["pred_class"])


def test_query_failure_reported(fake):
    fake.statement_execution.execute_statement = lambda **kw: NS(
        status=NS(state=sql.StatementState.FAILED, error=NS(message="TABLE_OR_VIEW_NOT_FOUND")))
    with pytest.raises(RuntimeError, match="FAILED: TABLE_OR_VIEW_NOT_FOUND"):
        sql.preview_table(TBL)
