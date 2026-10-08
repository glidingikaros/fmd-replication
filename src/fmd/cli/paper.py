import argparse
import re
from pathlib import Path

from fmd.cli.output import write_json_stdout
from fmd.core.errors import FmdInputError


def image_label(value: str) -> str:
    if not re.fullmatch(r"I[1-3](-[0-9]{2})?", value):
        raise argparse.ArgumentTypeError("expected I1, I2 or I3, optionally with a realization suffix such as I3-05")
    return value


def add_paper_parser(subcommands):
    parser = subcommands.add_parser(
        "paper", help="Run the declared I1–I3 paper workflow.", allow_abbrev=False
    )
    actions = parser.add_subparsers(dest="paper_action", required=True)
    from fmd.cli.generate import add_generate_parser

    add_generate_parser(actions)
    for action in ("inspect", "replay", "score"):
        sub = actions.add_parser(action, allow_abbrev=False)
        sub.add_argument(
            "--records",
            type=Path,
            help="Sealed records; defaults to the included I1 example.",
        )
        if action == "score":
            sub.add_argument(
                "--companion",
                type=Path,
                help="Explicit sealed completion; inputs must match.",
            )
    sub = actions.add_parser("collect", allow_abbrev=False)
    sub.add_argument("--evidence", type=Path, required=True)
    sub.add_argument("--output", type=Path, required=True)
    sub.add_argument("--windows-parsers", type=Path,
                     help="Required when the selection uses PECmd.exe or SBECmd.exe.")
    sub.add_argument("--question", dest="questions", action="append",
                     help="Collect evidence for this question (repeatable; default: all nine).")
    sub.add_argument("--host-toolchain-root", type=Path)
    sub.add_argument("--vm-work-root", type=Path,
                     help="Temporary parser VM directory on the base box APFS volume.")
    sub = actions.add_parser("preflight", allow_abbrev=False)
    sub.add_argument("--windows-parsers", type=Path)
    sub.add_argument("--question", dest="questions", action="append")
    sub.add_argument("--host-toolchain-root", type=Path)
    sub = actions.add_parser("report", allow_abbrev=False)
    sub.add_argument("--index", type=Path, required=True)
    sub.add_argument("--archive-root", type=Path, required=True)
    sub.add_argument("--output", type=Path, required=True)
    sub = actions.add_parser("prepare", allow_abbrev=False)
    sub.add_argument("--analysis", type=Path, required=True)
    sub.add_argument("--generation", type=Path, required=True)
    sub.add_argument("--image", type=image_label, required=True,
                     help="I1, I2 or I3; a realization label such as I3-05 names the requests as in the study.")
    sub.add_argument("--output", type=Path, required=True)
    sub.add_argument("--question-scope", choices=["hidden", "shown"], default="hidden",
                     help="Whether models see each question's scope statement (the paper: hidden).")
    sub.add_argument("--question", dest="questions", action="append",
                     help="Prepare evidence, cards and requests for this question (repeatable; default: all nine).")
    sub = actions.add_parser("assess", allow_abbrev=False,
                             help="S3: decide every sealed request of a build and seal the result set.")
    sub.add_argument("--built", type=Path, required=True)
    sub.add_argument("--output", type=Path, required=True)
    sub.add_argument("--engine", default="rules", help="Registered S3 engine (default: rules).")
    sub = actions.add_parser("freeze", allow_abbrev=False)
    sub.add_argument("--built", type=Path, required=True)
    sub.add_argument("--condition", required=True)
    sub.add_argument("--completion", action="store_true")
    sub.add_argument("--output", type=Path, required=True)
    sub = actions.add_parser("admit", allow_abbrev=False)
    sub.add_argument("--prepared", type=Path, required=True)
    sub.add_argument("--built", type=Path, required=True)
    sub.add_argument("--condition-run", type=Path, required=True, action="append")
    sub.add_argument("--rules", type=Path, help="S3's sealed result set from fmd paper assess.")
    sub = actions.add_parser("run", allow_abbrev=False)
    sub.add_argument("--records", type=Path, required=True)
    sub.add_argument("--primary", type=Path, help="Sealed primary execution required by a frozen completion template.")
    sub.add_argument(
        "--execute",
        action="store_true",
        help="Explicitly authorize live provider dispatch.",
    )
    sub.add_argument("--cap-usd", required=True)
    sub.add_argument("--passes", type=int, help="Run only the first N passes of the sealed schedule.")
    sub.add_argument("--question", dest="question_ids", action="append",
                     help="Run only this question's rows of the sealed schedule (repeatable).")
    sub.add_argument("--development-unadmitted", action="store_true",
                     help="Development only: dispatch although the sealed admission failed; the run records it.")
    sub.add_argument("--input-usd-per-million", required=True)
    sub.add_argument("--output-usd-per-million", required=True)


def run_paper(args):
    from fmd.paper.replay import example_root, inspect_records, replay
    from fmd.evaluation.scoring import score_run
    from fmd.preparation.native import collect_native
    from fmd.paper.workflow import prepare, admit_native, freeze_condition, execute_condition

    action = args.paper_action
    try:
        if action == "generate":
            from fmd.cli.generate import run_generate

            return run_generate(args)
        elif action == "preflight":
            from fmd.collection.paper_host import preflight
            from fmd.profiles import resolve_paper_profile

            result = preflight(
                profile=resolve_paper_profile(args.questions) if args.questions else resolve_paper_profile(),
                windows_parsers=args.windows_parsers,
                host_toolchain_root=args.host_toolchain_root,
            )
        elif action == "report":
            from fmd.evaluation.report import write_report

            result = write_report(
                index=args.index, archive_root=args.archive_root, output=args.output
            )
        elif action in {"inspect", "replay", "score"}:
            root = args.records or example_root()
            result = (
                inspect_records(root)
                if action == "inspect"
                else replay(root)
                if action == "replay"
                else score_run(root, companion=args.companion)
            )
        elif action == "collect":
            from fmd.profiles import resolve_paper_profile

            result = collect_native(
                evidence=args.evidence,
                output=args.output,
                windows_parsers=args.windows_parsers,
                host_toolchain_root=args.host_toolchain_root,
                vm_work_root=getattr(args, "vm_work_root", None),
                profile=resolve_paper_profile(args.questions) if args.questions else None,
            )
        elif action == "prepare":
            from fmd.profiles import resolve_profile

            result = prepare(
                analysis=args.analysis, generation=args.generation,
                image=args.image, output=args.output, question_scope=args.question_scope,
                g2=resolve_profile(args.questions),
            )
        elif action == "assess":
            from fmd.paper.workflow import assess_rules
            from fmd.profiles import question_definitions

            result = assess_rules(built=args.built, output=args.output, engine_name=args.engine,
                                  question_definitions=question_definitions())
        elif action == "freeze":
            result = freeze_condition(
                built=args.built,
                condition=args.condition,
                output=args.output,
                completion=args.completion,
            )
        elif action == "admit":
            result = admit_native(
                args.prepared, built=args.built, condition_runs=args.condition_run, rules=args.rules
            )
        else:
            result = execute_condition(
                args.records,
                cap_usd=args.cap_usd,
                execute=args.execute,
                rates={
                    "input": args.input_usd_per_million,
                    "output": args.output_usd_per_million,
                },
                pass_limit=args.passes,
                question_ids=args.question_ids,
                development_unadmitted=args.development_unadmitted,
                primary=args.primary,
            )
    except (ValueError, KeyError, OSError) as error:
        wrapped = FmdInputError(str(error))
        for note in getattr(error, "__notes__", ()):
            wrapped.add_note(note)
        raise wrapped from error
    write_json_stdout(result)
    return 0 if result.get("status") != "failed" else 1
