#!/usr/bin/env python3
"""Run the reusable registry build without delegating to a shell."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys


PACKAGE_RE = re.compile(r"[A-Za-z0-9_-]+")
FORBIDDEN_SHELL = ("||", ";", "|", "`", "$(", "<", ">", "\n", "\r", "\x00")
FORBIDDEN_CARGO_OPTIONS = ("--target-dir", "--config")
MANIFEST_OPTION = "--manifest-path"


class BuildCommandError(ValueError):
    """The caller build command is not in the fail-closed cargo grammar."""


def parse_commands(
    raw: str, package: str, manifest: str = "Cargo.toml"
) -> list[list[str]]:
    """Parse one or more `cargo build` argv lists joined only by `&&`."""

    if not PACKAGE_RE.fullmatch(package):
        raise BuildCommandError(f"invalid Cargo package name: {package!r}")
    if not manifest or "\x00" in manifest or "\n" in manifest or "\r" in manifest:
        raise BuildCommandError(f"invalid Cargo manifest path: {manifest!r}")
    if not raw.strip():
        return [
            ["cargo", "build", "-p", package, MANIFEST_OPTION, manifest]
        ]
    for token in FORBIDDEN_SHELL:
        if token in raw:
            raise BuildCommandError(
                f"registry-build-command contains forbidden shell syntax: {token!r}"
            )

    segments = raw.split("&&")
    if any(not segment.strip() for segment in segments):
        raise BuildCommandError("registry-build-command has an empty cargo build segment")

    commands: list[list[str]] = []
    for segment in segments:
        try:
            argv = shlex.split(segment, posix=True)
        except ValueError as exc:
            raise BuildCommandError(f"invalid registry-build-command quoting: {exc}") from exc
        if argv[:2] != ["cargo", "build"]:
            raise BuildCommandError(
                "each registry-build-command segment must begin with exact `cargo build`"
            )
        for index, argument in enumerate(argv):
            if argument in FORBIDDEN_CARGO_OPTIONS or any(
                argument.startswith(f"{option}=") for option in FORBIDDEN_CARGO_OPTIONS
            ):
                raise BuildCommandError(
                    f"registry-build-command option is owned by the workflow: {argument!r}"
                )
            if index > 0 and argv[index - 1] in FORBIDDEN_CARGO_OPTIONS:
                raise BuildCommandError(
                    f"registry-build-command option is owned by the workflow: {argv[index - 1]!r}"
                )
        manifest_values = []
        for index, argument in enumerate(argv):
            if argument == MANIFEST_OPTION:
                if index + 1 >= len(argv):
                    raise BuildCommandError("--manifest-path requires one exact value")
                manifest_values.append(argv[index + 1])
            elif argument.startswith(f"{MANIFEST_OPTION}="):
                manifest_values.append(argument.split("=", 1)[1])
        if len(manifest_values) > 1:
            raise BuildCommandError("registry-build-command repeats --manifest-path")
        if manifest_values and manifest_values[0] != manifest:
            raise BuildCommandError(
                "registry-build-command manifest differs from the prefetched manifest"
            )
        if not manifest_values:
            argv.extend([MANIFEST_OPTION, manifest])
        commands.append(argv)
    return commands


def run_commands(commands: list[list[str]]) -> None:
    for command in commands:
        subprocess.run(command, check=True)


def main() -> int:
    try:
        commands = parse_commands(
            os.environ.get("REGISTRY_BUILD_COMMAND", ""),
            os.environ.get("PACKAGE", ""),
            os.environ.get("MANIFEST", ""),
        )
        run_commands(commands)
    except (BuildCommandError, subprocess.CalledProcessError) as exc:
        print(f"registry build failed closed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
