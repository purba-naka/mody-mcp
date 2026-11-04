"""Tool Databricks Jobs: run, status task, dan error. Dibatasi ALLOWED_JOB_PREFIX."""

from __future__ import annotations

import os
from itertools import islice
from typing import Any

from mody_mcp import core
from mody_mcp.core import TRACE_TAIL, cell, clamp, duration, mcp, md_table, plain, ts


def job_prefix() -> str:
    return os.environ.get("ALLOWED_JOB_PREFIX", "mody_")


def check_job_name(name: str) -> None:
    prefix = job_prefix()
    if prefix and not name.startswith(prefix):
        raise ValueError(f"Job '{name}' tidak diizinkan. Harus berawalan '{prefix}'")


def check_job_id(job_id: int | None) -> str:
    """Pastikan run berasal dari job yang diizinkan. Mengembalikan nama job."""
    if job_id is None:
        raise ValueError("Run ini bukan bagian dari job, tidak diizinkan")
    name = core.ws().jobs.get(job_id).settings.name
    check_job_name(name)
    return name


def run_state(state: Any) -> str | None:
    if not state:
        return None
    result = state.result_state.value if state.result_state else None
    return result or (state.life_cycle_state.value if state.life_cycle_state else None)


def job_params(run: Any) -> dict[str, str]:
    """Nilai efektif parameter job. value bernilai None kalau run memakai default."""
    return {p.name: p.value if p.value is not None else p.default for p in run.job_parameters or []}


@mcp.tool()
def list_job_runs(job_name: str, limit: int = 10) -> str:
    """Run terakhir satu job, misalnya mody_train_dev. Kolom overrides berisi parameter yang diubah dari default."""
    check_job_name(job_name)
    job = next(iter(core.ws().jobs.list(name=job_name)), None)
    if job is None:
        raise ValueError(f"Job '{job_name}' tidak ditemukan")
    runs = islice(core.ws().jobs.list_runs(job_id=job.job_id, limit=min(clamp(limit), 25)), clamp(limit))
    rows = [
        [r.run_id, run_state(r.state), ts(r.start_time), duration(r.start_time, r.end_time),
         r.trigger.value if r.trigger else None,
         ", ".join(f"{p.name}={p.value}" for p in r.job_parameters or [] if p.value not in (None, p.default))]
        for r in runs
    ]
    return f"{len(rows)} run {job_name}\n" + md_table(["run_id", "state", "start", "durasi", "trigger", "overrides"], rows)


@mcp.tool()
def get_job_run(run_id: int) -> str:
    """Status satu run job dan tiap task-nya. Pakai task_run_id dari sini di get_task_output untuk melihat error.

    Run yang di-repair menampilkan semua attempt. State run mengikuti repair terakhir.
    """
    run = core.ws().jobs.get_run(run_id, include_history=True)
    name = check_job_id(run.job_id)
    tasks = sorted(run.tasks or [], key=lambda t: (t.start_time or 0, t.attempt_number or 0))
    repairs = [h for h in run.repair_history or [] if h.type and h.type.value == "REPAIR"]
    repair_note = (
        f"{len(repairs)}x, attempt sebelumnya: "
        + ", ".join(cell(run_state(h.state)) for h in (run.repair_history or [])[:-1])
        if repairs else "-"
    )
    return (
        f"## {name} run {run.run_id}\n"
        f"- state: {cell(run_state(run.state))}, start: {cell(ts(run.start_time))}, "
        f"durasi: {cell(duration(run.start_time, run.end_time))}\n"
        f"- repair: {repair_note}\n"
        f"- message: {cell(run.state.state_message if run.state else None)}\n"
        f"- url: {cell(run.run_page_url)}\n\n"
        f"### params\n{md_table(['name', 'value'], sorted(job_params(run).items()))}\n\n"
        f"### tasks ({len(tasks)})\n"
        + md_table(
            ["task_key", "task_run_id", "attempt", "state", "start", "durasi"],
            [[t.task_key, t.run_id, t.attempt_number, run_state(t.state), ts(t.start_time),
              duration(t.start_time, t.end_time)] for t in tasks],
        )
    )


@mcp.tool()
def get_task_output(task_run_id: int) -> str:
    """Error dan stack trace satu task (task_run_id dari get_job_run). Trace dipotong ke bagian akhir."""
    out = core.ws().jobs.get_run_output(task_run_id)
    check_job_id(out.metadata.job_id if out.metadata else None)
    error = plain(out.error) or "(tidak ada error)"
    trace = plain(out.error_trace)
    if len(trace) > TRACE_TAIL:
        trace = "...\n" + trace[-TRACE_TAIL:]
    state = run_state(out.metadata.state if out.metadata else None)
    return (
        f"## task run {task_run_id}\n- state: {cell(state)}\n\n"
        f"### error\n{error}\n\n"
        f"### trace\n```\n{trace or '(kosong)'}\n```"
    )
