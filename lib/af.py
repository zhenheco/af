#!/usr/bin/env python3
"""Small command dispatcher for af helpers."""

import argparse
import sys

from cleanup import add_cleanup_parser, run_cleanup
from dispatch import add_dispatch_parser, run_dispatch
from doctor import add_doctor_parser, run_doctor
from gate import add_gate_parser, run_gate
from state import add_state_parser, run_state
from selection import add_select_parser, run_select
from worktree import add_worktree_parser, run_worktree

COMMANDS = {
    "worktree": (add_worktree_parser, run_worktree), "state": (add_state_parser, run_state),
    "select": (add_select_parser, run_select), "dispatch": (add_dispatch_parser, run_dispatch),
    "gate": (add_gate_parser, run_gate), "cleanup": (add_cleanup_parser, run_cleanup),
    "doctor": (add_doctor_parser, run_doctor),
}

def build_parser():
    parser = argparse.ArgumentParser(prog="af")
    commands = parser.add_subparsers(dest="command", required=True)
    for add_parser, _ in COMMANDS.values():
        add_parser(commands)
    return parser

def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
        return COMMANDS[args.command][1](args) or 0
    except Exception as exc:
        print(f"af: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
