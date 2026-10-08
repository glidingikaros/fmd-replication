import argparse
from pathlib import Path

from fmd.cli.output import write_json_stdout
from fmd.core.errors import FmdInputError


def add_pipeline_parser(subcommands):
    parser = subcommands.add_parser(
        "pipeline", help="Run the stages S1-S4 through the typed gates G1-G5.", allow_abbrev=False
    )
    actions = parser.add_subparsers(dest="pipeline_action", required=True)
    sub = actions.add_parser("run", allow_abbrev=False, help="Run one configured case.")
    sub.add_argument("--config", type=Path, required=True, help="YAML or JSON run configuration.")
    sub = actions.add_parser("verify", allow_abbrev=False, help="Re-validate a finished run's gates and hashes.")
    sub.add_argument("--run", type=Path, required=True)
    sub = actions.add_parser("diff", allow_abbrev=False,
                             help="Compare two runs gate by gate, ignoring file locations and times.")
    sub.add_argument("run_a", type=Path)
    sub.add_argument("run_b", type=Path)
    sub = actions.add_parser("check", allow_abbrev=False,
                             help="Run the contract checks on a finished run (gates, packs, truth-blindness, "
                                  "presentation, admission, triviality; optionally a golden run).")
    sub.add_argument("--run", type=Path, required=True)
    sub.add_argument("--golden", type=Path, help="A run this one must compare as the same as.")
    sub = actions.add_parser("evaluate-sealed", allow_abbrev=False,
                             help="S4 over sealed, admitted condition runs of one case (e.g. the paper's): write G5.")
    sub.add_argument("--case-label", required=True)
    sub.add_argument("--run", type=Path, action="append", required=True, help="A sealed condition run (repeatable).")
    sub.add_argument("--rules", type=Path, help="S3's sealed result set, if the runs' build has one.")
    sub.add_argument("--output", type=Path, required=True)
    sub = actions.add_parser("report", allow_abbrev=False,
                             help="Table 3 and Figure 2 from the G5 of one or more runs (one per image).")
    sub.add_argument("--run", type=Path, action="append", required=True,
                     help="A pipeline run, or an evaluate-sealed output (repeatable).")
    sub.add_argument("--output", type=Path, required=True)


def run_pipeline_command(args: argparse.Namespace) -> int:
    from fmd.pipeline.gates import verify_run
    from fmd.pipeline.runner import load_config, run_pipeline

    try:
        if args.pipeline_action == "run":
            result = run_pipeline(load_config(args.config))
        elif args.pipeline_action == "check":
            from fmd.pipeline.contract import check_run

            result = check_run(args.run, golden=args.golden)
        elif args.pipeline_action == "evaluate-sealed":
            from fmd.pipeline.sealed import evaluate_sealed

            result = evaluate_sealed(case_label=args.case_label, condition_runs=args.run, output=args.output,
                                     rules=args.rules)
        elif args.pipeline_action == "report":
            from fmd.pipeline.report import write_report

            result = write_report(runs=args.run, output=args.output)
        elif args.pipeline_action == "diff":
            from fmd.pipeline.diff import diff_runs

            result = diff_runs(args.run_a, args.run_b)
        else:
            result = verify_run(args.run)
    except (ValueError, KeyError, OSError) as error:
        raise FmdInputError(str(error)) from error
    write_json_stdout(result)
    return 0 if result.get("status") in {"completed", "verified", "same", "evaluated", "passed", "reported"} else 1
