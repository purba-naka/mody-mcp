"""Auth HTTP, cek config saat start, audit log, dan registrasi tool."""

import asyncio
import json

import pytest
from starlette.testclient import TestClient

import server

KEY = "k" * 32
TOOLS = {
    "list_catalogs", "list_schemas", "list_tables", "describe_table",  # uc.py
    "preview_table", "query_table",  # sql.py
    "search_runs", "get_run", "list_model_versions",  # mlflow.py
    "list_job_runs", "get_job_run", "get_task_output",  # jobs.py
    "github_list_files", "github_read_file", "github_create_branch", "github_commit_files",  # github.py
    "github_create_pr", "github_update_pr", "github_pr_status", "github_ci_log",
}


def test_bearer_auth_and_all_tools_registered():
    with pytest.raises(SystemExit):
        server.create_app("")
    with TestClient(server.create_app(KEY), base_url="http://127.0.0.1:8000") as client:
        assert client.post("/mcp").status_code == 401
        assert client.post("/mcp", headers={"Authorization": "Bearer wrong"}).status_code == 401
        ok = client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {KEY}", "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert ok.status_code == 200
        assert {t["name"] for t in ok.json()["result"]["tools"]} == TOOLS


@pytest.mark.parametrize("overrides, msg", [
    ({"key": "short"}, "MCP_API_KEY"),
    ({"ALLOWED_CATALOGS": ""}, "ALLOWED_CATALOGS"),
    ({"ALLOWED_CATALOGS": " , "}, "ALLOWED_CATALOGS"),
    ({"ALLOWED_EXPERIMENT_PREFIX": ""}, "ALLOWED_EXPERIMENT_PREFIX"),
    ({"ALLOWED_JOB_PREFIX": " "}, "ALLOWED_JOB_PREFIX"),
    ({"GITHUB_BRANCH_PREFIX": ""}, "GITHUB_BRANCH_PREFIX"),
    ({"GITHUB_BRANCH_PREFIX": "ma"}, "GITHUB_BRANCH_PREFIX"),
])
def test_config_fails_closed(monkeypatch, overrides, msg):
    overrides = dict(overrides)
    key = overrides.pop("key", KEY)
    for k, v in overrides.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(SystemExit, match=msg):
        server.create_app(key)


def test_auth_checks_non_http_scopes():
    sent = []

    async def app(scope, receive, send):
        raise AssertionError("tidak boleh sampai app")

    async def send(msg):
        sent.append(msg)

    asyncio.run(server.BearerAuth(app, KEY)({"type": "websocket", "headers": []}, None, send))
    assert sent[0]["status"] == 401


def test_tool_call_audited_without_values(fake, caplog):
    caplog.set_level("INFO", logger="mody_mcp.audit")
    with pytest.raises(Exception):  # noqa: B017
        asyncio.run(server.mcp.call_tool("list_schemas", {"catalog": "rahasia_pii"}))
    rec = json.loads(caplog.records[-1].getMessage())
    assert rec["tool"] == "list_schemas" and rec["arg_keys"] == ["catalog"] and rec["outcome"] != "ok"
    assert "rahasia_pii" not in caplog.text
