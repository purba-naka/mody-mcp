import pytest
from conftest import TBL

from mody_mcp import uc
from mody_mcp.core import split_full_name


@pytest.mark.parametrize("name", ["inference_log", "a.b", "a..c", "main.s.t"])
def test_full_name_validated(name):
    with pytest.raises(ValueError):
        split_full_name(name)


def test_list_catalogs_filtered(fake):
    out = uc.list_catalogs()
    assert "mody_dev" in out and "main" not in out


def test_list_schemas_rejects_other_catalog(fake):
    with pytest.raises(ValueError, match="tidak diizinkan"):
        uc.list_schemas("main")


def test_describe_table(fake):
    out = uc.describe_table(TBL)
    assert "format: ICEBERG" in out
    assert "| scope | string | yes | production atau shadow |" in out
