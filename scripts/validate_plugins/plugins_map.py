from __future__ import annotations

import json
from pathlib import Path


def validate_plugins_map(data: object, *, source_name: str) -> dict[str, dict]:
    if not isinstance(data, dict):
        raise ValueError("plugins.json must contain a JSON object")

    for name, payload in data.items():
        if not isinstance(name, str):
            raise ValueError(
                f"plugins.json on the {source_name} has a non-string key: {name!r}"
            )
        if not isinstance(payload, dict):
            raise ValueError(
                f"plugins.json entry {name!r} on the {source_name} must be a JSON object"
            )

    return data


def load_plugins_map_text(text: str, *, source_name: str) -> dict[str, dict]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"plugins.json is invalid on the {source_name}: {exc}") from exc

    return validate_plugins_map(data, source_name=source_name)


def load_plugins_map_file(path: Path, *, source_name: str) -> dict[str, dict]:
    return load_plugins_map_text(path.read_text(encoding="utf-8"), source_name=source_name)
