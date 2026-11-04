"""Fixture bersama: env default, Databricks palsu, GitHub palsu. Tidak ada koneksi ke layanan sungguhan."""

import json
from types import SimpleNamespace as NS

import httpx
import pytest

from mody_mcp import core
from mody_mcp import github as gh_mod
from mody_mcp.sql import StatementState

TBL = "mody_dev.geo_commodity_classification.inference_log"
REPO = "AGATA-PIHC/ml-ops-databricks"
T0 = 1_792_988_074_000


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ALLOWED_CATALOGS", "mody_dev")
    monkeypatch.setenv("MAX_ROWS", "100")


class FakeWs:
    def __init__(self):
        col = NS(name="scope", type_text="string", nullable=True, comment="production atau shadow")
        table = NS(full_name=TBL, name="inference_log", table_type=NS(value="MANAGED"),
                   data_source_format=NS(value="ICEBERG"), owner="sp", comment=None, columns=[col])
        self.catalogs = NS(list=lambda: [NS(name="mody_dev", comment=None), NS(name="main", comment="x")])
        self.tables = NS(get=lambda full_name: table)
        self.sent = {}

        def execute_statement(**kw):
            self.sent = kw
            return NS(status=NS(state=StatementState.SUCCEEDED, error=None),
                      manifest=NS(schema=NS(columns=[NS(name="scope")])),
                      result=NS(data_array=[["production"], ["a|b\nc" + "x" * 300]]))

        self.statement_execution = NS(execute_statement=execute_statement)


@pytest.fixture
def fake(monkeypatch):
    w = FakeWs()
    monkeypatch.setattr(core, "ws", lambda: w)
    monkeypatch.setenv("DATABRICKS_SQL_WAREHOUSE_ID", "wh")
    return w


class FakeGitHub:
    """REST API GitHub palsu. Mencatat setiap request yang mengubah data."""

    YML = "usecases/geo_commodity_classification/usecase.yml"

    def __init__(self):
        self.branches = {"main": "sha-main"}
        self.writes = []
        self.job_branch = "agent/tune"
        self.pr = {"number": 7, "title": "tune", "state": "open", "draft": True, "merged": False, "mergeable": True,
                   "html_url": "https://github.com/pr/7",
                   "head": {"ref": "agent/tune", "sha": "abc123def4567890", "repo": {"full_name": REPO}},
                   "base": {"ref": "main"}}

    def handle(self, request: httpx.Request) -> httpx.Response:
        import base64

        prefix = f"/repos/{REPO}"
        assert request.url.path.startswith(prefix) and request.headers["authorization"] == "Bearer tok"
        path, method = request.url.path.removeprefix(prefix), request.method
        body = json.loads(request.content) if request.content else None
        if method != "GET":
            self.writes.append((method, path, body))

        if path.startswith("/git/ref/heads/"):
            sha = self.branches.get(path.removeprefix("/git/ref/heads/"))
            return httpx.Response(200, json={"object": {"sha": sha}}) if sha else httpx.Response(404, json={"message": "Not Found"})
        if path.startswith("/git/commits/") and method == "GET":
            return httpx.Response(200, json={"tree": {"sha": "tree-base"}})
        routes = {
            ("POST", "/git/trees"): {"sha": "tree-new"},
            ("POST", "/git/commits"): {"sha": "commit-new-0123456789", "html_url": "https://github.com/c/1"},
            ("POST", "/git/refs"): {},
            ("PATCH", "/git/refs/heads/agent/tune"): {},
            ("POST", "/pulls"): {"number": 8, "html_url": "https://github.com/pr/8"},
            ("GET", "/pulls/7"): self.pr,
            ("PATCH", "/pulls/7"): self.pr,
            ("GET", "/contents/usecases"): [
                {"type": "dir", "path": "usecases/geo_commodity_classification", "size": 0}],
            ("GET", "/contents/.github/workflows"): [
                {"type": "file", "path": ".github/workflows/ci.yml", "size": 10}],
            ("GET", f"/contents/{self.YML}"): {"encoding": "base64", "content": base64.b64encode(b"epochs: 50\n").decode()},
            ("GET", "/commits/abc123def4567890/check-runs"): {"check_runs": [
                {"name": "test", "status": "completed", "conclusion": "failure", "id": 99}]},
        }
        if (method, path) == ("GET", "/actions/jobs/99"):
            return httpx.Response(200, json={"id": 99, "head_branch": self.job_branch})
        if (method, path) == ("GET", "/actions/jobs/99/logs"):
            return httpx.Response(200, text="2026-11-03T01:02:03.4567890Z \x1b[31mFAILED\x1b[0m test_x\n")
        if (method, path) in routes:
            return httpx.Response(200, json=routes[(method, path)])
        return httpx.Response(404, json={"message": "Not Found"})


@pytest.fixture
def github(monkeypatch):
    fake = FakeGitHub()
    client = httpx.Client(base_url=gh_mod.GITHUB_API, transport=httpx.MockTransport(fake.handle),
                          headers={"Authorization": "Bearer tok"})
    monkeypatch.setattr(gh_mod, "gh", lambda: client)
    monkeypatch.setenv("GITHUB_REPO", REPO)
    monkeypatch.delenv("GITHUB_ALLOWED_PATHS", raising=False)
    return fake
