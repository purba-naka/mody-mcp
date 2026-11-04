from types import SimpleNamespace as NS

import pytest
from conftest import T0

from mody_mcp import mlflow

EXP = "/Shared/mody/geo_commodity_classification"
MODEL = "mody_dev.geo_commodity_classification.commodity_classifier"


def kv(**items):
    return [NS(key=k, value=v) for k, v in items.items()]


def mlflow_run(run_id, experiment_id="1"):
    return NS(
        info=NS(run_id=run_id, run_name=f"train_{run_id}", status=NS(value="FINISHED"),
                start_time=T0, end_time=T0 + 125_000, experiment_id=experiment_id),
        data=NS(metrics=kv(macro_f1=0.71234, accuracy=0.8),
                params=kv(lr="0.001"),
                tags=kv(git_branch="agent/x", track="pipeline", **{"mlflow.user": "u"})),
    )


@pytest.fixture
def mlflow_fake(fake):
    experiments = {"1": NS(experiment_id="1", name=EXP), "2": NS(experiment_id="2", name="/Shared/other/exp")}
    fake.search_args = {}

    def search_runs(**kw):
        fake.search_args = kw
        return iter([mlflow_run("r1"), mlflow_run("r2")])

    fake.experiments = NS(
        get_by_name=lambda name: NS(experiment=next(e for e in experiments.values() if e.name == name)),
        get_experiment=lambda eid: NS(experiment=experiments[eid]),
        search_runs=search_runs,
        get_run=lambda run_id: NS(run=mlflow_run(run_id, experiment_id="2" if run_id == "foreign" else "1")),
    )
    fake.registered_models = NS(get=lambda name, include_aliases: NS(aliases=[NS(alias_name="champion", version_num=6)]))
    fake.model_versions = NS(list=lambda name: iter([
        NS(version=str(v), run_id=f"r{v}", status=NS(value="READY"), created_at=T0, comment=None) for v in (5, 6, 7)
    ]))
    return fake


def test_search_runs(mlflow_fake):
    out = mlflow.search_runs("/Workspace" + EXP, filter="tags.git_branch = 'agent/x'",
                             order_by="metrics.macro_f1 DESC", metrics=["macro_f1"], limit=1)
    assert mlflow_fake.search_args == {"experiment_ids": ["1"], "filter": "tags.git_branch = 'agent/x'",
                                       "order_by": ["metrics.macro_f1 DESC"], "max_results": 1}
    assert out.startswith(f"1 run di {EXP}")
    assert "| r1 | train_r1 | FINISHED | 2026-10-26 04:14 UTC " in out and "| pipeline | - | agent/x | - | macro_f1=0.7123 |" in out


@pytest.mark.parametrize("path", ["/Shared/other/exp", "/Shared/mody/../other/exp"])
def test_search_runs_rejects_other_experiment(mlflow_fake, path):
    with pytest.raises(ValueError, match="tidak diizinkan"):
        mlflow.search_runs(path)


def test_get_run(mlflow_fake):
    out = mlflow.get_run("r1")
    assert f"- experiment: {EXP}" in out and "durasi: 2m05s" in out
    assert "| lr | 0.001 |" in out and "| macro_f1 | 0.71234 |" in out
    assert "| git_branch | agent/x |" in out and "mlflow.user" not in out


def test_get_run_rejects_run_from_other_experiment(mlflow_fake):
    with pytest.raises(ValueError, match="tidak diizinkan"):
        mlflow.get_run("foreign")


def test_list_model_versions(mlflow_fake):
    lines = mlflow.list_model_versions(MODEL, limit=2).splitlines()
    assert lines[0] == f"2 versi {MODEL}"
    assert lines[3].startswith("| 7 | - | r7 | READY |")
    assert lines[4].startswith("| 6 | champion | r6 | READY |")


def test_list_model_versions_rejects_other_catalog(mlflow_fake):
    with pytest.raises(ValueError, match="tidak diizinkan"):
        mlflow.list_model_versions("main.s.model")
