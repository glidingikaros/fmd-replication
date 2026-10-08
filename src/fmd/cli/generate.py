from __future__ import annotations
import argparse
from pathlib import Path
from fmd.core.errors import FmdInputError
from fmd.core.paper_protocol import paper_protocol
from fmd.core.paths import PROJECT_ROOT, default_current_root
from fmd.core.sealed_records import read_json

PAPER_BASE_GUEST = {"windows_build": "22000", "timezone": "Pacific Standard Time", "locale": "en-US"}


def add_generate_parser(actions):
    parser = actions.add_parser(
        "generate",
        allow_abbrev=False,
        help="Freeze a paper recipe offline, or execute/resume a locally provisioned realization.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--paper-image", choices=["I1", "I2", "I3"])
    mode.add_argument("--recipe", type=Path)
    mode.add_argument("--resume-post-export", type=Path)
    mode.add_argument("--write-dependency-lock", type=Path,
                      help="Inventory this host's evidence inputs into a portable (v2) dependency lock; launches no VM.")
    mode.add_argument("--check-host", action="store_true",
                      help="List what generation needs from this host and what is missing; writes and launches nothing.")
    parser.add_argument("--freeze-recipe", type=Path)
    parser.add_argument("--dependency-lock", type=Path)
    parser.add_argument("--reuse-assignment", type=Path,
                        help="With --paper-image: freeze on the private assignment of this earlier recipe of the "
                             "same image instead of drawing a new one (needs a v2 lock; recorded in the recipe).")
    parser.add_argument("--provider", choices=["vmware_desktop", "qemu"],
                        help="Generation backend for --check-host, --write-dependency-lock and --paper-image "
                             "(default vmware_desktop; qemu: KVM, WHPX or HVF, the guest matches the host).")
    parser.add_argument("--guest-windows-build", default=PAPER_BASE_GUEST["windows_build"])
    parser.add_argument("--guest-timezone", default=PAPER_BASE_GUEST["timezone"])
    parser.add_argument("--guest-locale", default=PAPER_BASE_GUEST["locale"])
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--vm-work-root", type=Path,
                        help="Existing macOS directory on the base filesystem for an independent APFS recipe VM.")


def _load_generation_pipeline():
    from fmd.generation import pipeline

    return pipeline


def validate_paper_recipe(path):
    from fmd.generation import recipe

    return recipe.load_recipe(
        path, source_root=PROJECT_ROOT / "generation", verify_dependencies=False
    )


def _target(provider):
    from fmd.generation import recipe

    config = recipe.paper_config("I1", provider)
    return config["provider"], config["windows_box"], paper_protocol()["generation"]["base_box_version"]


def check_host(provider="vmware_desktop"):
    from fmd.cli.output import write_json_stdout
    from fmd.generation import dependency_lock, recipe

    backend_name, box, version = _target(provider)
    _, imports = recipe.closure_modules(PROJECT_ROOT)
    report = dependency_lock.check_host(
        backend_name=backend_name, box=box, version=version, imports=imports, cwd=PROJECT_ROOT / "generation",
    )
    write_json_stdout(report)
    return 0 if report["ready"] else 1


def write_dependency_lock(path, guest, provider="vmware_desktop"):
    from fmd.cli.output import write_json_stdout
    from fmd.generation import dependency_lock, recipe

    path = Path(path).expanduser().absolute()
    if path.exists():
        raise FmdInputError("dependency lock destination already exists")
    backend_name, box, version = _target(provider)
    _, imports = recipe.closure_modules(PROJECT_ROOT)
    lock = dependency_lock.build_lock(
        backend_name=backend_name, box=box, version=version, guest=guest, imports=imports,
        cwd=PROJECT_ROOT / "generation",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    recipe.write_private(path, lock)
    write_json_stdout({
        "path": str(path), "schema_version": lock["schema_version"],
        "pinned_inputs_sha256": dependency_lock.pinned_digest(lock),
        "hypervisor": lock["backend"]["hypervisor"], "base_location": lock["base"]["location"],
        "base_files": len(lock["base"]["files"]),
        "base_bytes": sum(row["size_bytes"] for row in lock["base"]["files"]),
        "ansible_core": lock["guest_code"]["ansible_core"]["version"],
        "collections": {name: row["version"] for name, row in lock["guest_code"]["collections"].items()},
        "host_libraries": lock["host_libraries"]["distributions"],
    })
    return 0


def reused_assignment(donor_path, population, config):
    from fmd.generation import recipe
    from fmd.generation.population import build_guest_plan

    donor = recipe.inspect_recipe(donor_path)
    if (donor["recipe"]["config"]["population_seed"] != config["population_seed"]
            or donor["private"]["population_manifest"] != population):
        raise FmdInputError("the donor recipe was frozen for a different public population")
    assignment = donor["private"]["assignment"]
    origin = {"kind": "reused", "recipe_id": donor["recipe"]["recipe_id"],
              "private_sha256": donor["recipe"]["private_sha256"]}
    return assignment, build_guest_plan(population, assignment, case=config["case"]), origin


def run_generate(args: argparse.Namespace) -> int:
    from fmd.generation import recipe

    guest = {"windows_build": args.guest_windows_build, "timezone": args.guest_timezone,
             "locale": args.guest_locale}
    if args.check_host:
        if any(value is not None for value in (args.freeze_recipe, args.dependency_lock, args.reuse_assignment,
                                               args.output_root, getattr(args, "vm_work_root", None))):
            raise FmdInputError("--check-host takes no other options")
        return check_host(args.provider or "vmware_desktop")
    if args.write_dependency_lock is not None:
        if any(value is not None for value in (args.freeze_recipe, args.dependency_lock, args.reuse_assignment,
                                               args.output_root, getattr(args, "vm_work_root", None))):
            raise FmdInputError("--write-dependency-lock accepts only the --guest-* options")
        return write_dependency_lock(args.write_dependency_lock, guest, args.provider or "vmware_desktop")
    if guest != PAPER_BASE_GUEST:
        raise FmdInputError("--guest-* options are allowed only with --write-dependency-lock")
    if args.provider is not None and args.paper_image is None:
        raise FmdInputError("--provider is frozen into a recipe; it is not accepted with --recipe")
    if args.reuse_assignment is not None and args.paper_image is None:
        raise FmdInputError("--reuse-assignment is allowed only when freezing with --paper-image")
    vm_work_root = getattr(args, "vm_work_root", None)
    if vm_work_root is not None and args.recipe is None:
        raise FmdInputError("--vm-work-root is allowed only with --recipe")
    if args.resume_post_export is not None:
        if (
            args.output_root is not None
            or args.freeze_recipe is not None
            or args.dependency_lock is not None
        ):
            raise FmdInputError(
                "--resume-post-export accepts no recipe/output overrides"
            )
        state = read_json(args.resume_post_export / "post-export/state.json")
        if not state.get("recipe_directory"):
            raise FmdInputError("resume requires a frozen paper recipe")
        validate_paper_recipe(Path(state["recipe_directory"]))
        pipeline = _load_generation_pipeline().GenerationPipeline.resume_post_export(
            args.resume_post_export
        )
        pipeline.run_post_export_resume()
        return 0
    output = args.output_root or default_current_root() / "generated"
    if args.recipe is not None:
        if args.freeze_recipe is not None or args.dependency_lock is not None:
            raise FmdInputError("--recipe accepts only --output-root and --vm-work-root")
        bundle = validate_paper_recipe(args.recipe)
        config = bundle["recipe"]["config"]
        pipeline = _pipeline(config, output, recipe_path=args.recipe, vm_work_root=vm_work_root)
        pipeline.run()
        return 0
    if args.freeze_recipe is None or args.dependency_lock is None:
        raise FmdInputError(
            "--paper-image requires --freeze-recipe and --dependency-lock"
        )
    protocol = paper_protocol()
    image = protocol["images"][args.paper_image]
    config = recipe.paper_config(args.paper_image, args.provider or "vmware_desktop")
    pipeline = _pipeline(
        config, output, population_contract=PROJECT_ROOT / image["population_contract"]
    )
    try:
        pipeline.prepare_population()
        assignment, guest_plan, origin = (
            pipeline.private_population_assignment, pipeline.population_guest_plan, None)
        if args.reuse_assignment is not None:
            assignment, guest_plan, origin = reused_assignment(
                args.reuse_assignment, pipeline.public_population_manifest, config)
        frozen = recipe.freeze_recipe(
            args.freeze_recipe,
            source_root=pipeline.work_dir,
            config=config,
            population=pipeline.public_population_manifest,
            assignment=assignment,
            guest_plan=guest_plan,
            dependency_lock=recipe.read_json(args.dependency_lock),
            activity_seed=image["activity_seed"],
            hardware_seed=image["hardware_seed"],
            assignment_origin=origin,
        )
        from fmd.cli.output import write_json_stdout

        write_json_stdout(
            {
                "recipe_id": frozen["recipe_id"],
                "path": str(args.freeze_recipe),
                "schema_version": frozen["schema_version"],
                "assignment_origin": frozen.get("assignment_origin"),
                "status": "frozen_runtime_conformance_unverified",
            }
        )
    finally:
        pipeline.cleanup_population_inputs()
    return 0


def _pipeline(config, output, *, recipe_path=None, population_contract=None, vm_work_root=None):
    return _load_generation_pipeline().GenerationPipeline(
        config["provider"],
        "baseline",
        config["export_format"],
        False,
        config["randomize_hw"],
        experiment=config["experiment"],
        case=config["case"],
        population_seed=config["population_seed"],
        output_root=output,
        windows_box=config["windows_box"],
        vmware_bridge=config["vmware_bridge"],
        recipe=recipe_path,
        population_contract=population_contract,
        activity_count=config["activity_count"],
        vm_work_root=vm_work_root,
    )
