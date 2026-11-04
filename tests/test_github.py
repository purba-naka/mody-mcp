import pytest
from conftest import REPO, FakeGitHub

from mody_mcp import github as gh
from mody_mcp.github import FileChange

YML = FakeGitHub.YML


@pytest.mark.parametrize("path, ok", [
    (YML, True),
    ("usecases/geo_commodity_classification/rules.yml", True),
    ("src/mody_ml/usecases/geo_commodity/model.py", True),
    ("tests/unit/test_model.py", True),
    ("notebooks/training/geo_commodity_classification/train_template.py", True),
    ("tests/unit/conftest.py", False),
    ("tests/unit/helper.py", False),
    ("tests/unit/sub/test_x.py", False),
    ("src/mody_ml/usecases/geo_commodity/__init__.py", False),
    ("src/mody_ml/usecases/a/b/c.py", False),
    ("usecases/a/b/usecase.yml", False),
    ("usecases/geo_commodity_classification/model_card.md", False),
    ("src/mody_ml/lifecycle/model/registry.py", False),
    ("resources/jobs/train.yml", False),
    ("conf/env/dev.yml", False),
    ("databricks.yml", False),
    (".github/workflows/ci.yml", False),
])
def test_writable_paths(monkeypatch, path, ok):
    monkeypatch.delenv("GITHUB_ALLOWED_PATHS", raising=False)
    assert gh.is_writable(path) is ok


def test_github_dir_denied_even_if_allowed(monkeypatch):
    monkeypatch.setenv("GITHUB_ALLOWED_PATHS", "*")
    assert gh.is_writable("x.py") and not gh.is_writable(".github/workflows/ci.yml")
    monkeypatch.setenv("GITHUB_ALLOWED_PATHS", "*,*/*,*/*/*")
    assert not gh.is_writable("conftest.py") and not gh.is_writable("src/pkg/__init__.py")


@pytest.mark.parametrize("path", ["/etc/passwd", "src/../.github/x", "src//x", "src/x?ref=y", "src/x#y", "./src"])
def test_unsafe_paths_rejected(path):
    with pytest.raises(ValueError, match="tidak valid"):
        gh.check_relative(path)


@pytest.mark.parametrize("branch", ["main", "agent/", "feature/x", "agent/../main", "agent/a b"])
def test_branch_rejected(branch):
    with pytest.raises(ValueError, match="tidak diizinkan"):
        gh.check_branch(branch)


# ---------- baca ----------


def test_list_files_marks_editable(github):
    out = gh.github_list_files("usecases")
    assert "| dir | usecases/geo_commodity_classification | - | no |" in out
    assert "| file | .github/workflows/ci.yml | 10 | no |" in gh.github_list_files(".github/workflows")


def test_read_file(github):
    out = gh.github_read_file(YML)
    assert f"## {YML} (main, editable: yes)" in out and "epochs: 50" in out


def test_github_error_reported(github):
    with pytest.raises(gh.GitHubError, match="404 Not Found"):
        gh.github_read_file("src/missing.py")


# ---------- tulis ----------


def test_create_branch(github):
    assert gh.github_create_branch("agent/new") == "Branch agent/new dibuat dari main (sha-main)"
    assert github.writes == [("POST", "/git/refs", {"ref": "refs/heads/agent/new", "sha": "sha-main"})]


@pytest.mark.parametrize("branch, msg", [("agent/tune", "sudah ada"), ("main", "tidak diizinkan")])
def test_create_branch_rejected(github, branch, msg):
    github.branches["agent/tune"] = "sha-tune"
    with pytest.raises(ValueError, match=msg):
        gh.github_create_branch(branch)
    assert github.writes == []


def test_commit_creates_branch_from_main(github):
    out = gh.github_commit_files("agent/new", "tune lr", [FileChange(path=YML, content="epochs: 80\n")])
    assert "(branch baru)" in out
    (_, _, tree), (_, _, commit), (_, ref_path, ref) = github.writes
    assert tree == {"base_tree": "tree-base",
                    "tree": [{"path": YML, "mode": "100644", "type": "blob", "content": "epochs: 80\n"}]}
    assert commit["parents"] == ["sha-main"]
    assert ref_path == "/git/refs" and ref == {"ref": "refs/heads/agent/new", "sha": "commit-new-0123456789"}


def test_commit_updates_existing_branch_without_force(github):
    github.branches["agent/tune"] = "sha-tune"
    gh.github_commit_files("agent/tune", "hapus", [FileChange(path="tests/unit/test_old.py")])
    (_, _, tree), (_, _, commit), (method, path, ref) = github.writes
    assert tree["tree"][0]["sha"] is None  # hapus file
    assert commit["parents"] == ["sha-tune"]
    assert (method, path, ref["force"]) == ("PATCH", "/git/refs/heads/agent/tune", False)


@pytest.mark.parametrize("branch, files, msg", [
    ("main", [FileChange(path=YML, content="x")], "tidak diizinkan"),
    ("agent/x", [FileChange(path="src/mody_ml/lifecycle/a.py", content="x")], "tidak boleh diubah"),
    ("agent/x", [FileChange(path=".github/workflows/ci.yml", content="x")], "tidak boleh diubah"),
    ("agent/x", [FileChange(path=YML, content="a"), FileChange(path=YML, content="b")], "hanya sekali"),
    ("agent/x", [], "minimal satu"),
    ("agent/x", [FileChange(path=f"usecases/u{i}/a.yml", content="x") for i in range(21)], "Maksimal"),
    ("agent/x", [FileChange(path=YML, content="x" * 500_001)], "Maksimal"),
])
def test_commit_rejected_before_any_write(github, branch, files, msg):
    with pytest.raises(ValueError, match=msg):
        gh.github_commit_files(branch, "m", files)
    assert github.writes == []


def test_create_pr_is_draft_to_main(github):
    assert gh.github_create_pr("agent/tune", "Tune", "hipotesis") == "PR #8 dibuka (draft): https://github.com/pr/8"
    assert github.writes == [("POST", "/pulls", {"head": "agent/tune", "base": "main", "title": "Tune",
                                                  "body": "hipotesis", "draft": True})]


def test_update_pr(github):
    gh.github_update_pr(7, body="macro_f1 0.70")
    assert github.writes == [("PATCH", "/pulls/7", {"body": "macro_f1 0.70"})]


@pytest.mark.parametrize("head", [{"ref": "feature/x", "repo": {"full_name": REPO}},
                                  {"ref": "agent/x", "repo": {"full_name": "other/fork"}}])
def test_update_pr_rejects_non_agent_pr(github, head):
    github.pr["head"] = {**github.pr["head"], **head}
    with pytest.raises(ValueError):
        gh.github_update_pr(7, body="x")
    assert github.writes == []


# ---------- CI ----------


def test_pr_status_and_ci_log(github):
    out = gh.github_pr_status(7)
    assert "- state: open (draft)" in out and "| test | completed | failure | 99 |" in out
    log = gh.github_ci_log(99)
    assert "FAILED test_x" in log and "2026-11-03T" not in log and "\x1b" not in log


def test_pr_status_rejects_non_agent_pr(github):
    github.pr["head"] = {**github.pr["head"], "ref": "feature/x"}
    with pytest.raises(ValueError, match="tidak diizinkan"):
        gh.github_pr_status(7)


@pytest.mark.parametrize("branch", ["main", "feature/x", None])
def test_ci_log_rejects_non_agent_job(github, branch):
    github.job_branch = branch
    with pytest.raises(ValueError, match="tidak diizinkan"):
        gh.github_ci_log(99)
