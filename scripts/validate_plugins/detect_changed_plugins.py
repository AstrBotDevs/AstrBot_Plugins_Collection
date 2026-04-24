#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


DEFAULT_ASTRBOT_REF = "master"
ASTRBOT_REMOTE_URL = "https://github.com/AstrBotDevs/AstrBot"


def load_plugins_map(text: str, *, source_name: str) -> dict[str, dict]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"plugins.json is invalid on the {source_name}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("plugins.json must contain a JSON object")
    return data


def detect_changed_plugin_names(*, base: dict[str, dict], head: dict[str, dict]) -> list[str]:
    return [name for name, payload in head.items() if base.get(name) != payload]


def fetch_base_ref(base_ref: str) -> None:
    subprocess.run(["git", "fetch", "origin", base_ref, "--depth", "1"], check=True)


def read_base_plugins_json(base_ref: str) -> str:
    return subprocess.check_output(
        ["git", "show", f"origin/{base_ref}:plugins.json"],
        text=True,
        stderr=subprocess.DEVNULL,
    )


def resolve_astrbot_ref() -> str:
    try:
        default_head = subprocess.check_output(
            ["git", "ls-remote", "--symref", ASTRBOT_REMOTE_URL, "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return DEFAULT_ASTRBOT_REF

    for line in default_head.splitlines():
        if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
            return line.split("refs/heads/", 1)[1].split("\t", 1)[0]
    return DEFAULT_ASTRBOT_REF


def detect_pull_request_selection(*, repo_root: Path, base_ref: str) -> dict[str, object]:
    fetch_base_ref(base_ref)

    try:
        base = load_plugins_map(read_base_plugins_json(base_ref), source_name=f"base ref {base_ref}")
    except (subprocess.CalledProcessError, ValueError):
        base = {}

    head_text = (repo_root / "plugins.json").read_text(encoding="utf-8")
    try:
        head = load_plugins_map(head_text, source_name="PR head")
    except ValueError as exc:
        raise ValueError(f"plugins.json is invalid on the PR head: {exc}") from exc

    changed = detect_changed_plugin_names(base=base, head=head)
    validation_note = ""
    if not changed:
        validation_note = "No plugin entries changed in plugins.json; skipping smoke validation."

    return {
        "changed": changed,
        "should_validate": bool(changed),
        "validation_note": validation_note,
    }


def write_github_env(
    *,
    env_path: Path,
    astrbot_ref: str,
    changed: list[str],
    should_validate: bool,
    validation_note: str,
) -> None:
    with env_path.open("a", encoding="utf-8") as handle:
        handle.write(f"ASTRBOT_REF={astrbot_ref}\n")
        handle.write(f"PLUGIN_NAME_LIST={','.join(changed)}\n")
        handle.write("PLUGIN_LIMIT=\n")
        handle.write(f"SHOULD_VALIDATE={'true' if should_validate else 'false'}\n")
        handle.write(f"VALIDATION_NOTE={validation_note}\n")


def main() -> int:
    base_ref = os.environ["GITHUB_BASE_REF"]
    github_env = Path(os.environ["GITHUB_ENV"])
    repo_root = Path.cwd()

    try:
        result = detect_pull_request_selection(repo_root=repo_root, base_ref=base_ref)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    write_github_env(
        env_path=github_env,
        astrbot_ref=resolve_astrbot_ref(),
        changed=result["changed"],
        should_validate=result["should_validate"],
        validation_note=result["validation_note"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
