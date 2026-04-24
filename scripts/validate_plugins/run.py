#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from urllib.parse import urlparse

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.validate_plugins.plugins_map import load_plugins_map_file

try:
    import yaml
except ImportError:  # pragma: no cover - optional in local unit tests
    yaml = None


REQUIRED_METADATA_FIELDS = ("name", "desc", "version", "author")
DEFAULT_CLONE_TIMEOUT = 120
DEFAULT_MAX_WORKERS = 8
CONFLICT_MARKERS = ("<<<<<<<", "=======", ">>>>>>>")


class MetadataLoadError(ValueError):
    pass


def positive_int(raw_value: str) -> int:
    value = int(raw_value)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def build_result(
    *,
    plugin: str,
    repo: str,
    normalized_repo_url: str | None,
    ok: bool,
    stage: str,
    message: str,
    severity: str | None = None,
    plugin_dir_name: str | None = None,
    details: dict | str | None = None,
) -> dict:
    resolved_severity = severity or ("pass" if ok else "fail")
    resolved_ok = True if resolved_severity in {"pass", "warn"} else ok
    result = {
        "plugin": plugin,
        "repo": repo,
        "normalized_repo_url": normalized_repo_url,
        "ok": resolved_ok,
        "stage": stage,
        "message": message,
        "severity": resolved_severity,
    }
    if plugin_dir_name:
        result["plugin_dir_name"] = plugin_dir_name
    if details is not None:
        result["details"] = details
    return result


def normalize_repo_url(repo_url: str) -> str:
    parsed = urlparse(repo_url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("repo URL must use http or https")
    if parsed.netloc.lower() != "github.com":
        raise ValueError("repo URL must point to github.com")

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise ValueError("repo URL must include owner and repository")

    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo:
        raise ValueError("repo URL owner or repository is empty")

    return f"https://github.com/{owner}/{repo}"


def select_plugins(
    *,
    plugins: dict,
    requested_names: list[str] | None,
    limit: int | None,
) -> list[tuple[str, dict]]:
    if requested_names:
        selected = []
        for name in requested_names:
            if name not in plugins:
                raise KeyError(f"plugin not found: {name}")
            selected.append((name, plugins[name]))
        return selected

    items = list(plugins.items())
    if limit is None or limit < 0:
        return items
    return items[:limit]


def _parse_simple_yaml(path: Path) -> dict:
    """Very small YAML subset parser used as a fallback when PyYAML is unavailable.

    Supported format:
    - Flat mapping of `key: value` pairs
    - No indentation (no nested objects or multiline continuations)
    - No lists (`- item` syntax)
    - `#` starts a comment when preceded by whitespace (or at line start)
    """

    def parse_value(raw_value: str) -> str:
        value = raw_value.strip()
        if not value:
            return ""

        if value[0] in {'"', "'"}:
            quote = value[0]
            end_index = value.rfind(quote)
            if end_index > 0:
                return value[1:end_index]

        value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        return value.strip("\"'")

    result: dict[str, str] = {}
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        if raw_line[0].isspace():
            raise ValueError(
                f"Unsupported YAML indentation in {path} at line {lineno}: {raw_line!r}"
            )

        line = stripped
        if line.startswith("-"):
            raise ValueError(
                f"Unsupported YAML list syntax in {path} at line {lineno}: {raw_line!r}"
            )
        if ":" not in line:
            raise ValueError(
                f"Unsupported YAML content (expected 'key: value') in {path} at line {lineno}: {raw_line!r}"
            )

        key, value = line.split(":", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Empty key is not allowed in {path} at line {lineno}: {raw_line!r}")
        if key in result:
            raise ValueError(f"Duplicate key '{key}' in {path} at line {lineno}")

        result[key] = parse_value(value)
    return result


def load_metadata(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if any(marker in text for marker in CONFLICT_MARKERS):
        raise MetadataLoadError(
            "could not find expected ':' (merge conflict markers found in metadata.yaml)"
        )

    if yaml is not None:
        try:
            loaded = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise MetadataLoadError(str(exc)) from exc
        if loaded is None:
            return {}
        if not isinstance(loaded, dict):
            raise MetadataLoadError("metadata.yaml must contain a mapping at the top level")
        return loaded

    try:
        return _parse_simple_yaml(path)
    except ValueError as exc:
        raise MetadataLoadError(str(exc)) from exc


def precheck_plugin_directory(plugin_dir: Path) -> dict:
    metadata_path = plugin_dir / "metadata.yaml"
    if not metadata_path.exists():
        return {
            "ok": False,
            "stage": "metadata",
            "message": "missing metadata.yaml",
        }

    try:
        metadata = load_metadata(metadata_path)
    except MetadataLoadError as exc:
        return {
            "ok": False,
            "stage": "metadata",
            "message": "invalid metadata.yaml",
            "details": str(exc),
        }

    missing = [
        field
        for field in REQUIRED_METADATA_FIELDS
        if not isinstance(metadata.get(field), str) or not metadata[field].strip()
    ]
    if missing:
        return {
            "ok": False,
            "severity": "warn",
            "stage": "metadata",
            "message": f"missing required metadata fields: {', '.join(missing)}",
        }

    try:
        plugin_name = validate_plugin_dir_name(metadata["name"])
    except ValueError as exc:
        return {
            "ok": False,
            "stage": "metadata",
            "message": "invalid plugin directory name",
            "details": str(exc),
        }

    entry_candidates = [plugin_dir / "main.py", plugin_dir / f"{plugin_name}.py"]
    if not any(path.exists() for path in entry_candidates):
        return {
            "ok": False,
            "stage": "entrypoint",
            "message": f"missing main.py or {plugin_name}.py",
        }

    return {
        "ok": True,
        "stage": "precheck",
        "message": "ok",
        "metadata": metadata,
        "plugin_dir_name": plugin_name,
    }


def build_worker_command(
    *,
    script_path: Path,
    astrbot_path: Path,
    plugin_source_dir: Path,
    plugin_dir_name: str,
    normalized_repo_url: str,
) -> list[str]:
    return [
        sys.executable,
        str(script_path),
        "--worker",
        "--astrbot-path",
        str(astrbot_path),
        "--plugin-source-dir",
        str(plugin_source_dir),
        "--plugin-dir-name",
        plugin_dir_name,
        "--normalized-repo-url",
        normalized_repo_url,
    ]


def build_worker_sys_path(*, astrbot_root: Path, astrbot_path: Path) -> list[str]:
    return [str(astrbot_root.resolve()), str(astrbot_path.resolve())]


def build_report(results: list[dict]) -> dict:
    passed = sum(1 for result in results if result.get("severity") == "pass")
    warned = sum(1 for result in results if result.get("severity") == "warn")
    failed = sum(1 for result in results if result.get("severity") == "fail")
    return {
        "summary": {
            "total": len(results),
            "passed": passed,
            "warned": warned,
            "failed": failed,
        },
        "results": results,
    }


def load_plugins_index(path: Path) -> dict[str, dict]:
    return load_plugins_map_file(path, source_name="plugins.json")


def combine_requested_names(
    plugin_names: list[str] | None,
    plugin_name_list: str | None,
) -> list[str]:
    names = [name.strip() for name in (plugin_names or [])]
    if plugin_name_list:
        names.extend(part.strip() for part in plugin_name_list.split(","))
    return [name for name in names if name]


def sanitize_name(name: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return sanitized or "plugin"


def validate_plugin_dir_name(name: str) -> str:
    candidate = name.strip()
    if not candidate or candidate in {".", ".."}:
        raise ValueError("unsafe plugin_dir_name")
    if "/" in candidate or "\\" in candidate:
        raise ValueError("unsafe plugin_dir_name")
    if ".." in candidate:
        raise ValueError("unsafe plugin_dir_name")
    return candidate


def build_plugin_clone_dir(work_dir: Path, plugin: str) -> Path:
    digest = hashlib.sha256(plugin.encode("utf-8")).hexdigest()[:8]
    return work_dir / f"{sanitize_name(plugin)}-{digest}"


def _normalize_process_output(output: str | bytes | None) -> str | None:
    if output is None:
        return None
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    normalized = output.strip()
    return normalized or None


def build_process_output_details(
    *,
    stdout: str | bytes | None,
    stderr: str | bytes | None,
) -> dict | None:
    details = {}
    stdout_text = _normalize_process_output(stdout)
    stderr_text = _normalize_process_output(stderr)
    if stdout_text:
        details["stdout"] = stdout_text
    if stderr_text:
        details["stderr"] = stderr_text
    return details or None


def clone_plugin_repo(
    repo_url: str,
    destination: Path,
    *,
    timeout: int = DEFAULT_CLONE_TIMEOUT,
) -> None:
    subprocess.run(
        ["git", "clone", "--depth", "1", repo_url, str(destination)],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def parse_worker_output(
    *,
    plugin: str,
    repo: str,
    normalized_repo_url: str,
    completed: subprocess.CompletedProcess[str],
    plugin_dir_name: str,
) -> dict:
    stdout = completed.stdout.strip()
    if stdout:
        for line in reversed(stdout.splitlines()):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                payload["plugin"] = plugin
                payload["repo"] = repo
                payload["normalized_repo_url"] = normalized_repo_url
                payload.setdefault("plugin_dir_name", plugin_dir_name)
                return payload

    stderr = completed.stderr.strip()
    message = stderr or stdout or "worker returned no structured output"
    return build_result(
        plugin=plugin,
        repo=repo,
        normalized_repo_url=normalized_repo_url,
        ok=False,
        stage="worker",
        message=message,
        plugin_dir_name=plugin_dir_name,
    )


def validate_plugin(
    *,
    plugin: str,
    plugin_data: dict,
    astrbot_path: Path,
    script_path: Path,
    work_dir: Path,
    clone_timeout: int,
    load_timeout: int,
) -> dict:
    repo_url = plugin_data.get("repo")
    if not isinstance(repo_url, str) or not repo_url.strip():
        return build_result(
            plugin=plugin,
            repo="",
            normalized_repo_url=None,
            ok=False,
            stage="repo_url",
            message="missing repo field",
        )

    try:
        normalized_repo_url = normalize_repo_url(repo_url)
    except ValueError as exc:
        return build_result(
            plugin=plugin,
            repo=repo_url,
            normalized_repo_url=None,
            ok=False,
            stage="repo_url",
            message=str(exc),
        )

    plugin_clone_dir = build_plugin_clone_dir(work_dir, plugin)
    try:
        clone_plugin_repo(
            normalized_repo_url,
            plugin_clone_dir,
            timeout=clone_timeout,
        )
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        return build_result(
            plugin=plugin,
            repo=repo_url,
            normalized_repo_url=normalized_repo_url,
            ok=False,
            stage="clone",
            message=message,
        )
    except subprocess.TimeoutExpired as exc:
        return build_result(
            plugin=plugin,
            repo=repo_url,
            normalized_repo_url=normalized_repo_url,
            ok=False,
            stage="clone_timeout",
            message=f"git clone timed out after {clone_timeout} seconds",
            details=build_process_output_details(stdout=exc.stdout, stderr=exc.stderr),
        )

    precheck = precheck_plugin_directory(plugin_clone_dir)
    if not precheck["ok"]:
        return build_result(
            plugin=plugin,
            repo=repo_url,
            normalized_repo_url=normalized_repo_url,
            ok=False,
            stage=precheck["stage"],
            message=precheck["message"],
            severity=precheck.get("severity"),
            details=precheck.get("details"),
        )

    plugin_dir_name = precheck["plugin_dir_name"]
    command = build_worker_command(
        script_path=script_path,
        astrbot_path=astrbot_path,
        plugin_source_dir=plugin_clone_dir,
        plugin_dir_name=plugin_dir_name,
        normalized_repo_url=normalized_repo_url,
    )

    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=load_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return build_result(
            plugin=plugin,
            repo=repo_url,
            normalized_repo_url=normalized_repo_url,
            ok=False,
            stage="timeout",
            message=f"worker timed out after {load_timeout} seconds",
            plugin_dir_name=plugin_dir_name,
            details=build_process_output_details(stdout=exc.stdout, stderr=exc.stderr),
        )

    return parse_worker_output(
        plugin=plugin,
        repo=repo_url,
        normalized_repo_url=normalized_repo_url,
        completed=completed,
        plugin_dir_name=plugin_dir_name,
    )


def validate_selected_plugins(
    *,
    selected: list[tuple[str, dict]],
    astrbot_path: Path,
    script_path: Path,
    work_dir: Path,
    clone_timeout: int,
    load_timeout: int,
    max_workers: int,
) -> list[dict]:
    total = len(selected)
    results: list[dict | None] = [None] * total

    def task(index: int, plugin: str, plugin_data: dict) -> tuple[int, dict]:
        return (
            index,
            validate_plugin(
                plugin=plugin,
                plugin_data=plugin_data,
                astrbot_path=astrbot_path,
                script_path=script_path,
                work_dir=work_dir,
                clone_timeout=clone_timeout,
                load_timeout=load_timeout,
            ),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_context: dict[concurrent.futures.Future, tuple[int, str]] = {}

        for index, (plugin, plugin_data) in enumerate(selected, start=1):
            print(f"[{index}/{total}] Queued {plugin}", flush=True)
            future = executor.submit(task, index, plugin, plugin_data)
            future_to_context[future] = (index, plugin)

        for future in concurrent.futures.as_completed(future_to_context):
            index, plugin = future_to_context[future]
            try:
                original_index, result = future.result()
            except Exception as exc:
                original_index = index
                result = build_result(
                    plugin=plugin,
                    repo="",
                    normalized_repo_url=None,
                    ok=False,
                    stage="threadpool",
                    message=str(exc),
                    details=traceback.format_exc(),
                )

            results[original_index - 1] = result
            severity = result.get("severity", "pass" if result.get("ok") else "fail")
            status = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}.get(severity, "FAIL")
            stage = result.get("stage", "unknown")
            message = result.get("message", "")
            print(f"[{original_index}/{total}] {status} {plugin} [{stage}] {message}", flush=True)

    finalized = [result for result in results if result is not None]
    if len(finalized) != total:
        raise RuntimeError("parallel validation finished with missing results")

    return finalized


class NullStub:
    def __getattr__(self, name: str) -> "NullStub":
        del name
        return self

    def __call__(self, *args, **kwargs) -> "NullStub":
        del args, kwargs
        return self

    def __await__(self):
        async def _return_self():
            return self

        return _return_self().__await__()

    def __iter__(self):
        return iter(())

    def __bool__(self) -> bool:
        return False


class DummyContext:
    def __init__(self) -> None:
        self._star_manager = None

    def get_all_stars(self):
        try:
            from astrbot.core.star.star import star_registry

            return list(star_registry)
        except Exception:
            return []

    def get_registered_star(self, star_name: str):
        for star in self.get_all_stars():
            if getattr(star, "name", None) == star_name:
                return star
        return None

    def activate_llm_tool(self, name: str) -> bool:
        del name
        return True

    def deactivate_llm_tool(self, name: str) -> bool:
        del name
        return True

    def register_llm_tool(self, name: str, func_args, desc: str, func_obj) -> None:
        del name, func_args, desc, func_obj

    def unregister_llm_tool(self, name: str) -> None:
        del name

    def __getattr__(self, name: str) -> NullStub:
        del name
        return NullStub()


async def run_worker_load_check(plugin_dir_name: str, normalized_repo_url: str) -> dict:
    try:
        from astrbot.core.star.star_manager import PluginManager
    except Exception as exc:
        return build_result(
            plugin=plugin_dir_name,
            repo=normalized_repo_url,
            normalized_repo_url=normalized_repo_url,
            ok=False,
            stage="astrbot_import",
            message=str(exc),
            plugin_dir_name=plugin_dir_name,
            details=traceback.format_exc(),
        )

    context = DummyContext()
    manager = PluginManager(context, {})

    try:
        success, error = await manager.load(specified_dir_name=plugin_dir_name)
    except Exception as exc:
        return build_result(
            plugin=plugin_dir_name,
            repo=normalized_repo_url,
            normalized_repo_url=normalized_repo_url,
            ok=False,
            stage="load",
            message=str(exc),
            plugin_dir_name=plugin_dir_name,
            details=traceback.format_exc(),
        )

    if success:
        return build_result(
            plugin=plugin_dir_name,
            repo=normalized_repo_url,
            normalized_repo_url=normalized_repo_url,
            ok=True,
            stage="load",
            message="plugin loaded successfully",
            plugin_dir_name=plugin_dir_name,
        )

    return build_result(
        plugin=plugin_dir_name,
        repo=normalized_repo_url,
        normalized_repo_url=normalized_repo_url,
        ok=False,
        stage="load",
        message=str(error) if error else "plugin load failed",
        plugin_dir_name=plugin_dir_name,
        details=manager.failed_plugin_dict.get(plugin_dir_name),
    )


def run_worker(args: argparse.Namespace) -> int:
    temp_root = Path(tempfile.mkdtemp(prefix="astrbot-plugin-worker-"))
    try:
        astrbot_root = temp_root / "astrbot-root"
        plugin_store = astrbot_root / "data" / "plugins"
        plugin_config = astrbot_root / "data" / "config"
        plugin_store.mkdir(parents=True, exist_ok=True)
        plugin_config.mkdir(parents=True, exist_ok=True)

        source_dir = Path(args.plugin_source_dir).resolve()
        target_dir = plugin_store / args.plugin_dir_name
        shutil.copytree(source_dir, target_dir, dirs_exist_ok=True)

        os.environ["ASTRBOT_ROOT"] = str(astrbot_root)
        os.environ.setdefault("TESTING", "true")
        sys.path[:0] = build_worker_sys_path(
            astrbot_root=astrbot_root,
            astrbot_path=Path(args.astrbot_path),
        )

        result = asyncio.run(
            run_worker_load_check(args.plugin_dir_name, args.normalized_repo_url)
        )
    except Exception as exc:
        result = build_result(
            plugin=args.plugin_dir_name,
            repo=args.normalized_repo_url,
            normalized_repo_url=args.normalized_repo_url,
            ok=False,
            stage="worker",
            message=str(exc),
            plugin_dir_name=args.plugin_dir_name,
            details=traceback.format_exc(),
        )
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate AstrBot plugins")
    parser.add_argument("--plugins-json", default="plugins.json")
    parser.add_argument("--plugin-name", action="append", dest="plugin_names")
    parser.add_argument("--plugin-name-list")
    parser.add_argument(
        "--limit",
        type=int,
        help="Validate the first N plugins when plugin names are empty. Omit or use -1 for all plugins.",
    )
    parser.add_argument("--astrbot-path")
    parser.add_argument("--report-path", default="validation-report.json")
    parser.add_argument("--work-dir")
    parser.add_argument("--clone-timeout", type=positive_int, default=DEFAULT_CLONE_TIMEOUT)
    parser.add_argument("--load-timeout", type=positive_int, default=300)
    parser.add_argument("--max-workers", type=positive_int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--plugin-source-dir")
    parser.add_argument("--plugin-dir-name")
    parser.add_argument("--normalized-repo-url")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.worker:
        missing = [
            flag
            for flag, value in (
                ("--astrbot-path", args.astrbot_path),
                ("--plugin-source-dir", args.plugin_source_dir),
                ("--plugin-dir-name", args.plugin_dir_name),
                ("--normalized-repo-url", args.normalized_repo_url),
            )
            if not value
        ]
        if missing:
            parser.error(f"worker mode requires: {', '.join(missing)}")
        return run_worker(args)

    if not args.astrbot_path:
        parser.error("--astrbot-path is required")

    requested_names = combine_requested_names(args.plugin_names, args.plugin_name_list)
    plugins = load_plugins_index(Path(args.plugins_json))
    selected = select_plugins(
        plugins=plugins,
        requested_names=requested_names or None,
        limit=args.limit,
    )

    temp_dir = None
    work_dir = Path(args.work_dir) if args.work_dir else None
    if work_dir is None:
        temp_dir = tempfile.TemporaryDirectory(prefix="astrbot-plugin-validate-")
        work_dir = Path(temp_dir.name)
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        results = validate_selected_plugins(
            selected=selected,
            astrbot_path=Path(args.astrbot_path).resolve(),
            script_path=Path(__file__).resolve(),
            work_dir=work_dir,
            clone_timeout=args.clone_timeout,
            load_timeout=args.load_timeout,
            max_workers=args.max_workers,
        )
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()

    report = build_report(results)
    report_path = Path(args.report_path)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "report_path": str(report_path),
                "summary": report["summary"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
