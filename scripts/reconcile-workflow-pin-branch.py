#!/usr/bin/env python3
"""Apply release-branch invariants to manifest-allowlisted workflow pin files.

This is intentionally separate from the general release-train CLI. Only the
dedicated workflow-pin App may invoke it, because its generated paths live
under ``.github/workflows`` and require GitHub App Workflows permission.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path


def _load(name: str, filename: str):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rrb = _load("pin_reconcile_release_branch", "reconcile-release-branch.py")
pins = _load("pin_update_manifest", "update-kin-actions-pins.py")


def allowed_paths(manifest: Path, repository: str) -> list[str]:
    paths = pins.load_consumer_paths(manifest, repository)
    for path in paths:
        if not path.startswith(".github/workflows/"):
            raise rrb.InvariantError(
                f"pin manifest path is outside workflow authority: {path}"
            )
    rrb.validate_generated_paths(paths, allow_workflows=True)
    return paths


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("validate-train", "neutralize", "validate-merge")
    )
    parser.add_argument("--repo", default=".")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--trusted-main", required=True)
    parser.add_argument("--train-head")
    parser.add_argument("--old-train-head")
    parser.add_argument("--merge-commit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        generated = allowed_paths(args.manifest.resolve(), args.repository)
        if args.command == "validate-train":
            if not args.train_head:
                raise rrb.InvariantError("--train-head is required")
            result = rrb.validate_train(
                args.repo,
                trusted_main=args.trusted_main,
                train_head=args.train_head,
                generated_paths=generated,
                allow_workflow_generated=True,
            )
        elif args.command == "neutralize":
            if not args.old_train_head:
                raise rrb.InvariantError("--old-train-head is required")
            result = rrb.neutralize(
                args.repo,
                trusted_main=args.trusted_main,
                old_train_head=args.old_train_head,
                generated_paths=generated,
                allow_workflow_generated=True,
            )
        else:
            if not args.old_train_head or not args.merge_commit:
                raise rrb.InvariantError(
                    "--old-train-head and --merge-commit are required"
                )
            result = rrb.validate_merge(
                args.repo,
                merge_commit=args.merge_commit,
                trusted_main=args.trusted_main,
                old_train_head=args.old_train_head,
                generated_paths=generated,
                allow_workflow_generated=True,
            )
    except (rrb.InvariantError, pins.PinUpdateError) as exc:
        print(f"workflow-pin invariant failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
