"""Tool GitHub untuk satu repo (GITHUB_REPO): baca file, commit ke branch agent, kelola draft PR, baca CI.

Pembatas tulis: branch harus GITHUB_BRANCH_PREFIX, path harus cocok GITHUB_ALLOWED_PATHS dengan kedalaman sama,
.github/ dan file yang dieksekusi otomatis selalu ditolak, update ref tanpa force, PR selalu draft.
"""

from __future__ import annotations

import base64
import fnmatch
import os
import re
from functools import cache
from typing import Any

import httpx
from pydantic import BaseModel

from mody_mcp.core import TRACE_TAIL, cell, mcp, md_table, plain

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


# ---------- config ----------


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


# ---------- client ----------


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


# ---------- guard ----------


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


def agent_pr(number: int) -> dict[str, Any]:
    """PR yang boleh diubah agent: dari branch agent di repo yang sama (bukan fork)."""
    pr = gh_call("GET", f"/pulls/{number}").json()
    check_branch(pr["head"]["ref"])
    if (pr["head"].get("repo") or {}).get("full_name") != github_repo():
        raise ValueError(f"PR #{number} berasal dari fork, tidak diizinkan")
    return pr


# ---------- baca ----------


@mcp.tool()
def github_list_files(path: str = "", ref: str = "") -> str:
    """Isi satu folder di repo. path kosong = root. ref = branch atau commit, kosong = branch utama.

    Kolom editable = yes kalau file boleh diubah lewat github_commit_files.
    """
    if path:
        check_relative(path)
    items = gh_call("GET", f"/contents/{path}", params=ref_params(ref)).json()
    if not isinstance(items, list):
        raise ValueError(f"'{path}' adalah file, pakai github_read_file")  # noqa: TRY004
    rows = [
        [i["type"], i["path"], i["size"] if i["type"] == "file" else None, "yes" if is_writable(i["path"]) else "no"]
        for i in items
    ]
    return f"{len(rows)} entri di /{path}\n" + md_table(["type", "path", "size", "editable"], rows)


@mcp.tool()
def github_read_file(path: str, ref: str = "") -> str:
    """Isi satu file teks di repo. ref = branch atau commit, kosong = branch utama. File panjang dipotong."""
    check_relative(path)
    item = gh_call("GET", f"/contents/{path}", params=ref_params(ref)).json()
    if isinstance(item, list):
        raise ValueError(f"'{path}' adalah folder, pakai github_list_files")  # noqa: TRY004
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


# ---------- tulis ----------


@mcp.tool()
def github_create_branch(branch: str) -> str:
    """Buat branch agent dari ujung branch utama. Gagal kalau branch sudah ada (tidak pernah menimpa)."""
    check_branch(branch)
    if branch_sha(branch) is not None:
        raise ValueError(f"Branch '{branch}' sudah ada")
    sha = branch_sha(base_branch())
    if sha is None:
        raise ValueError(f"Branch utama '{base_branch()}' tidak ditemukan")
    gh_call("POST", "/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha})
    return f"Branch {branch} dibuat dari {base_branch()} ({sha[:12]})"


@mcp.tool()
def github_commit_files(branch: str, message: str, files: list[FileChange]) -> str:
    """Satu commit berisi satu atau beberapa file ke branch agent. Branch dibuat dari branch utama kalau belum ada.

    files: [{"path": "usecases/<uc>/usecase.yml", "content": "<isi lengkap file>"}]. content null = hapus file.
    Isi file ditulis utuh (bukan diff). Hanya path dengan editable = yes yang diterima.
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

    return (
        f"Commit {commit['sha'][:12]} ke {branch}{' (branch baru)' if is_new else ''}\n"
        f"- url: {commit.get('html_url')}\n\n"
        + md_table(["path", "aksi"], [[f.path, "hapus" if f.content is None else "tulis"] for f in files])
    )


@mcp.tool()
def github_create_pr(branch: str, title: str, body: str) -> str:
    """Buka draft pull request dari branch agent ke branch utama. Ready for review, review, dan merge oleh manusia."""
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


# ---------- CI ----------


@mcp.tool()
def github_pr_status(number: int) -> str:
    """Status PR dan check CI di commit terakhirnya. job_id check yang gagal dipakai di github_ci_log."""
    pr = agent_pr(number)
    checks = gh_call("GET", f"/commits/{pr['head']['sha']}/check-runs", params={"per_page": 50}).json()["check_runs"]
    return (
        f"## PR #{number} {cell(pr['title'])}\n"
        f"- state: {pr['state']}{' (draft)' if pr.get('draft') else ''}, merged: {cell(pr.get('merged'))}, "
        f"mergeable: {cell(pr.get('mergeable'))}\n"
        f"- branch: {pr['head']['ref']} -> {pr['base']['ref']}, head: {pr['head']['sha'][:12]}\n"
        f"- url: {pr['html_url']}\n\n"
        f"### checks ({len(checks)})\n"
        + md_table(["name", "status", "conclusion", "job_id"],
                   [[c["name"], c["status"], c.get("conclusion"), c["id"]] for c in checks])
    )


@mcp.tool()
def github_ci_log(job_id: int) -> str:
    """Bagian akhir log satu job GitHub Actions (job_id dari github_pr_status), tanpa timestamp dan kode warna."""
    check_branch(gh_call("GET", f"/actions/jobs/{job_id}").json().get("head_branch") or "")
    log = LOG_TIMESTAMP.sub("", plain(gh_call("GET", f"/actions/jobs/{job_id}/logs").text))
    if len(log) > TRACE_TAIL:
        log = "...\n" + log[-TRACE_TAIL:]
    return f"## log job {job_id}\n```\n{log or '(kosong)'}\n```"
