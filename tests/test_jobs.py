from types import SimpleNamespace as NS

import pytest
from conftest import T0

from mody_mcp import jobs
from mody_mcp.core import TRACE_TAIL


def job_state(result):
    return NS(result_state=NS(value=result), life_cycle_state=NS(value="TERMINATED"), state_message="msg")


@pytest.fixture
def jobs_fake(fake):
    names = {1: "mody_train_dev", 2: "airbyte_sync"}
    task = NS(task_key="train", run_id=11, attempt_number=0, state=job_state("FAILED"),
              start_time=T0, end_time=T0 + 61_000)
    run = NS(run_id=100, job_id=1, state=job_state("FAILED"), start_time=T0, end_time=T0 + 61_000,
             trigger=NS(value="ONE_TIME"), run_page_url="https://x/run/100", tasks=[task],
             repair_history=[NS(type=NS(value="ORIGINAL"), state=job_state("FAILED")),
                             NS(type=NS(value="REPAIR"), state=job_state("FAILED"))],
             job_parameters=[NS(name="env", value=None, default="dev"),
                             NS(name="model_label", value="15A", default="")])

    fake.jobs = NS(
        get=lambda job_id: NS(settings=NS(name=names[job_id])),
        list=lambda name: iter([NS(job_id=j) for j, n in names.items() if n == name]),
        list_runs=lambda job_id, limit: iter([run]),
        get_run=lambda run_id, include_history: NS(**{**vars(run), "job_id": 2 if run_id == 999 else 1}),
        get_run_output=lambda run_id: NS(
            error="<span class='ansi-red-fg'>ValueError</span>: kolom hilang",
            error_trace="x" * 5000 + "\x1b[0;31mEND\x1b[0m",
            metadata=NS(job_id=2 if run_id == 999 else 1, state=job_state("FAILED")),
        ),
    )
    return fake


def test_list_job_runs(jobs_fake):
    out = jobs.list_job_runs("mody_train_dev")
    assert out.startswith("1 run mody_train_dev")
    assert "| 100 | FAILED | 2026-10-26 04:14 UTC " in out and "| 1m01s | ONE_TIME | model_label=15A |" in out


@pytest.mark.parametrize("name, msg", [("airbyte_sync", "tidak diizinkan"), ("mody_missing", "tidak ditemukan")])
def test_list_job_runs_rejects(jobs_fake, name, msg):
    with pytest.raises(ValueError, match=msg):
        jobs.list_job_runs(name)


def test_get_job_run(jobs_fake):
    out = jobs.get_job_run(100)
    assert out.startswith("## mody_train_dev run 100")
    assert "| env | dev |" in out and "| model_label | 15A |" in out  # default dipakai kalau value kosong
    assert "| train | 11 | 0 | FAILED |" in out
    assert "- repair: 1x, attempt sebelumnya: FAILED" in out


def test_get_task_output(jobs_fake):
    out = jobs.get_task_output(11)
    assert "ValueError: kolom hilang" in out and "<span" not in out
    assert out.count("x") <= TRACE_TAIL and "END" in out and "\x1b" not in out


@pytest.mark.parametrize("call", [lambda: jobs.get_job_run(999), lambda: jobs.get_task_output(999)])
def test_job_run_from_other_job_rejected(jobs_fake, call):
    with pytest.raises(ValueError, match="tidak diizinkan"):
        call()
