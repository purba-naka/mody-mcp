"""mody-mcp: MCP server untuk agent Mody (Streamable HTTP, satu endpoint /mcp).

Tool ada di mody_mcp/: uc.py, sql.py, mlflow.py, jobs.py, github.py. File ini hanya auth HTTP, cek config, dan start.
Spesifikasi: docs/2026-10-30-mody-mcp-generik-design.md
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any

from mody_mcp import github, jobs, mlflow, sql, uc  # noqa: F401  (import = daftarkan tool ke mcp)
from mody_mcp.core import HOST, PORT, allowed_catalogs, audit, mcp

MIN_API_KEY_LEN = 32


class BearerAuth:
    """Middleware ASGI: setiap request wajib membawa Authorization: Bearer <MCP_API_KEY>."""

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
    for name, value in (("ALLOWED_EXPERIMENT_PREFIX", mlflow.experiment_prefix()), ("ALLOWED_JOB_PREFIX", jobs.job_prefix())):
        if not value.strip():
            raise SystemExit(f"{name} tidak boleh kosong")
    if not github.branch_prefix().strip() or github.base_branch().startswith(github.branch_prefix()):
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
