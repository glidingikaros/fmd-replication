from __future__ import annotations
import argparse
from collections.abc import Sequence
from fmd.cli.paper import add_paper_parser, run_paper
from fmd.cli.pipeline import add_pipeline_parser, run_pipeline_command
from fmd.cli.replicate import add_replicate_parser, run_replicate
from fmd.core.errors import run_cli


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fmd",
        allow_abbrev=False,
        description="Reproduce the Windows/NTFS paper workflow.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    add_paper_parser(commands)
    add_pipeline_parser(commands)
    add_replicate_parser(commands)
    args = parser.parse_args(argv)
    if args.command == "pipeline":
        return run_cli("fmd", lambda: run_pipeline_command(args))
    if args.command == "replicate":
        return run_cli("fmd", lambda: run_replicate(args))
    return run_cli("fmd", lambda: run_paper(args))
