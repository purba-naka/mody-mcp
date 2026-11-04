"""Tool MLflow: run, metrik, dan versi model. Dibatasi ALLOWED_EXPERIMENT_PREFIX dan ALLOWED_CATALOGS."""

from __future__ import annotations

import os
from itertools import islice

from mody_mcp import core
from mody_mcp.core import cell, clamp, duration, mcp, md_table, split_full_name, ts

RUN_TAGS = ("track", "model_version_label", "git_branch", "git_commit")


def experiment_prefix() -> str:
    return os.environ.get("ALLOWED_EXPERIMENT_PREFIX", "/Shared/mody/")


def normalize_experiment(path: str) -> str:
    """API MLflow menyimpan nama tanpa awalan /Workspace, jadi keduanya disamakan."""
    return path.removeprefix("/Workspace")


def check_experiment(name: str) -> None:
    prefix = normalize_experiment(experiment_prefix())
    path = normalize_experiment(name)
    if prefix and (not path.startswith(prefix) or ".." in path.split("/")):
        raise ValueError(f"Experiment '{name}' tidak diizinkan. Harus berawalan '{prefix}'")


@mcp.tool()
def search_runs(
    experiment_path: str,
    filter: str = "",
    order_by: str = "",
    metrics: list[str] | None = None,
    limit: int = 10,
) -> str:
    """Daftar run MLflow di satu experiment, terbaru dulu.

    experiment_path: misalnya /Workspace/Shared/mody/geo_commodity_classification.
    filter: sintaks MLflow, misalnya "tags.git_branch = 'agent/x'" atau "metrics.macro_f1 > 0.7".
    order_by: misalnya "metrics.macro_f1 DESC". Kosong = start_time terbaru.
    metrics: metrik yang ditampilkan, kosong = semua (dipotong kalau panjang).
    """
    check_experiment(experiment_path)
    experiment = core.ws().experiments.get_by_name(normalize_experiment(experiment_path)).experiment
    runs = islice(
        core.ws().experiments.search_runs(
            experiment_ids=[experiment.experiment_id],
            filter=filter or None,
            order_by=[order_by] if order_by else None,
            max_results=clamp(limit),
        ),
        clamp(limit),
    )
    rows = []
    for r in runs:
        tags = {t.key: t.value for t in r.data.tags or []}
        values = {m.key: m.value for m in r.data.metrics or []}
        shown = metrics or sorted(values)
        rows.append([
            r.info.run_id, r.info.run_name, r.info.status.value if r.info.status else None, ts(r.info.start_time),
            *(tags.get(k) for k in RUN_TAGS),
            ", ".join(f"{k}={values[k]:.4g}" for k in shown if k in values),
        ])
    return f"{len(rows)} run di {experiment.name}\n" + md_table(
        ["run_id", "run_name", "status", "start", *RUN_TAGS, "metrics"], rows
    )


@mcp.tool()
def get_run(run_id: str) -> str:
    """Detail satu run MLflow: params, metrics, dan tags (tanpa tag sistem mlflow.*)."""
    run = core.ws().experiments.get_run(run_id).run
    experiment = core.ws().experiments.get_experiment(run.info.experiment_id).experiment
    check_experiment(experiment.name)
    params = sorted((p.key, p.value) for p in run.data.params or [])
    metrics = sorted((m.key, m.value) for m in run.data.metrics or [])
    tags = sorted((t.key, t.value) for t in run.data.tags or [] if not t.key.startswith("mlflow."))
    return (
        f"## run {run.info.run_id} ({cell(run.info.run_name)})\n"
        f"- experiment: {experiment.name}\n"
        f"- status: {cell(run.info.status.value if run.info.status else None)}, "
        f"start: {cell(ts(run.info.start_time))}, durasi: {cell(duration(run.info.start_time, run.info.end_time))}\n\n"
        f"### params ({len(params)})\n{md_table(['key', 'value'], params)}\n\n"
        f"### metrics ({len(metrics)})\n{md_table(['key', 'value'], metrics)}\n\n"
        f"### tags ({len(tags)})\n{md_table(['key', 'value'], tags)}"
    )


@mcp.tool()
def list_model_versions(full_name: str, limit: int = 20) -> str:
    """Versi model Unity Catalog beserta alias (misalnya champion), terbaru dulu. full_name = catalog.schema.model.

    run_id tiap versi bisa dipakai di get_run untuk melihat metrik versi tersebut.
    """
    split_full_name(full_name)
    model = core.ws().registered_models.get(full_name, include_aliases=True)
    aliases: dict[int, list[str]] = {}
    for a in model.aliases or []:
        aliases.setdefault(int(a.version_num), []).append(a.alias_name)
    versions = sorted(core.ws().model_versions.list(full_name), key=lambda v: int(v.version), reverse=True)
    rows = [
        [v.version, ", ".join(aliases.get(int(v.version), [])), v.run_id,
         v.status.value if v.status else None, ts(v.created_at), v.comment]
        for v in versions[: clamp(limit)]
    ]
    return f"{len(rows)} versi {full_name}\n" + md_table(
        ["version", "aliases", "run_id", "status", "created", "comment"], rows
    )
