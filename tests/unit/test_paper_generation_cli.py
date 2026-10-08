import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from fmd import main
from fmd.cli import generate
from fmd.generation import recipe
from test_generation_recipe import SOURCE, locked_recipe as locked_recipe


@pytest.mark.parametrize(
    "image,seed", [("I1", 2026091811), ("I2", 2026091812), ("I3", 2026091813)]
)
def test_freeze_is_offline_and_resolves_the_exact_paper_recipe(
    locked_recipe, tmp_path, monkeypatch, image, seed
):
    _, lock, *_ = locked_recipe
    lockpath = tmp_path / "lock.json"
    lockpath.write_text(json.dumps(lock))
    from fmd.generation.pipeline import GenerationPipeline

    monkeypatch.setattr(
        GenerationPipeline, "run", lambda *_: pytest.fail("freeze launched VM")
    )
    destination = tmp_path / image
    assert (
        main(
            [
                "paper",
                "generate",
                "--paper-image",
                image,
                "--freeze-recipe",
                str(destination),
                "--dependency-lock",
                str(lockpath),
                "--output-root",
                str(tmp_path / "out"),
            ]
        )
        == 0
    )
    frozen = recipe.load_recipe(
        destination, source_root=SOURCE, verify_dependencies=False
    )
    assert frozen["recipe"]["config"] == recipe.paper_config(image)
    private = frozen["private"]
    assert private["activity_seed"] == private["hardware_seed"] == seed
    assert len(private["activity_plan"]) == 12
    assert (
        len(
            private["guest_plan"]["scenario_inputs"]["usbstor_setupapi_discrepancy_01"][
                "media"
            ]
        )
        == 3
    )


@pytest.mark.parametrize(
    "extra",
    [
        ["--population-seed", "1"],
        ["--activity-count", "1"],
        ["--randomize-hw"],
        ["--case", "benign"],
        ["--provider", "virtualbox"],
    ],
)
def test_overrides_fail_before_pipeline_dispatch(monkeypatch, extra):
    monkeypatch.setattr(
        generate, "_load_generation_pipeline", lambda: pytest.fail("dispatch")
    )
    with pytest.raises(SystemExit) as error:
        main(
            [
                "paper",
                "generate",
                "--paper-image",
                "I1",
                "--freeze-recipe",
                "recipe",
                "--dependency-lock",
                "lock",
                *extra,
            ]
        )
    assert error.value.code == 2


def test_resume_requires_verified_recipe_before_dispatch(tmp_path, monkeypatch):
    post = tmp_path / "post-export"
    post.mkdir()
    (post / "state.json").write_text(json.dumps({"recipe_directory": "frozen"}))
    calls = []
    monkeypatch.setattr(
        generate, "validate_paper_recipe", lambda path: calls.append(("verify", path))
    )

    class Pipeline:
        @staticmethod
        def resume_post_export(path):
            calls.append(("resume", path))
            return SimpleNamespace(
                run_post_export_resume=lambda: calls.append(("run",))
            )

    monkeypatch.setattr(
        generate,
        "_load_generation_pipeline",
        lambda: SimpleNamespace(GenerationPipeline=Pipeline),
    )
    assert main(["paper", "generate", "--resume-post-export", str(tmp_path)]) == 0
    assert calls == [("verify", Path("frozen")), ("resume", tmp_path), ("run",)]
    calls.clear()

    def reject(_):
        raise ValueError("changed recipe")

    monkeypatch.setattr(generate, "validate_paper_recipe", reject)
    assert main(["paper", "generate", "--resume-post-export", str(tmp_path)]) != 0
    assert calls == []


@pytest.mark.parametrize("wrong_image", ["I2", "I3"])
def test_recipe_refuses_a_different_images_population(wrong_image):
    from fmd.generation import population
    contract = population.load_population_contract(
        SOURCE / f"populations.pilot-{wrong_image.lower()}-20260918.json")
    config = recipe.paper_config("I1")
    public = population.build_public_manifest(experiment="full_scale", seed=config["population_seed"], contract=contract)
    assignment = population.select_private_assignment(public, entropy=b"paper-population-binding")
    plan = population.build_guest_plan(public, assignment, case="positive")
    with pytest.raises(ValueError, match="population differs"):
        recipe.validate_resolved_inputs(config, public, assignment, plan)


@pytest.mark.parametrize("mode", [["--paper-image", "I1", "--freeze-recipe", "recipe", "--dependency-lock", "lock"],
                                  ["--resume-post-export", "output"]])
def test_vm_work_root_rejected_before_freeze_or_resume_dispatch(monkeypatch, mode):
    monkeypatch.setattr(generate, "_load_generation_pipeline", lambda: pytest.fail("dispatch"))
    assert main(["paper", "generate", *mode, "--vm-work-root", "internal"]) != 0


def test_vm_work_root_is_forwarded_only_as_runtime_argument(monkeypatch, tmp_path):
    config = recipe.paper_config("I1")
    seen = []
    monkeypatch.setattr(generate, "validate_paper_recipe", lambda _: {"recipe": {"config": config}})
    class Pipeline:
        def __init__(self, *args, **kwargs):
            seen.append(kwargs)
        def run(self):
            seen.append("run")
    monkeypatch.setattr(generate, "_load_generation_pipeline", lambda: SimpleNamespace(GenerationPipeline=Pipeline))
    root = tmp_path / "internal"
    assert main(["paper", "generate", "--recipe", "recipe", "--vm-work-root", str(root)]) == 0
    assert seen[0]["vm_work_root"] == root and seen[0]["recipe"] == Path("recipe")
    assert seen[0]["population_seed"] == config["population_seed"] and seen[0]["activity_count"] == 12
    assert config == recipe.paper_config("I1") and seen[1] == "run"
