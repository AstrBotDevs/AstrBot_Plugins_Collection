import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "validate_plugins" / "run.py"


def load_validator_module():
    if not MODULE_PATH.exists():
        raise AssertionError(f"validator script missing: {MODULE_PATH}")

    spec = importlib.util.spec_from_file_location("validate_plugins_run", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load validator module spec")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NormalizeRepoUrlTests(unittest.TestCase):
    def test_strips_git_suffix_trailing_slash_and_query(self):
        module = load_validator_module()

        self.assertEqual(
            module.normalize_repo_url(
                "https://github.com/example/demo-plugin.git/?tab=readme-ov-file"
            ),
            "https://github.com/example/demo-plugin",
        )

    def test_rejects_non_github_urls(self):
        module = load_validator_module()

        with self.assertRaises(ValueError):
            module.normalize_repo_url("https://gitlab.com/example/demo-plugin")

    def test_rejects_non_http_schemes(self):
        module = load_validator_module()

        for url in (
            "git://github.com/example/demo-plugin",
            "ssh://github.com/example/demo-plugin",
        ):
            with self.subTest(url=url):
                with self.assertRaisesRegex(ValueError, "repo URL must use http or https"):
                    module.normalize_repo_url(url)

    def test_rejects_missing_owner_or_repository(self):
        module = load_validator_module()

        for url in (
            "https://github.com/",
            "https://github.com/example",
            "https://github.com/example/",
            "https://github.com//demo-plugin",
            "https://github.com/example//",
        ):
            with self.subTest(url=url):
                with self.assertRaisesRegex(ValueError, "repo URL must include owner and repository"):
                    module.normalize_repo_url(url)

    def test_strips_leading_and_trailing_whitespace(self):
        module = load_validator_module()

        self.assertEqual(
            module.normalize_repo_url("  https://github.com/example/demo-plugin  "),
            "https://github.com/example/demo-plugin",
        )


class SelectPluginsTests(unittest.TestCase):
    def test_returns_all_plugins_when_limit_is_none(self):
        module = load_validator_module()
        plugins = {
            "plugin-a": {"repo": "https://github.com/example/plugin-a"},
            "plugin-b": {"repo": "https://github.com/example/plugin-b"},
        }

        selected = module.select_plugins(
            plugins=plugins,
            requested_names=None,
            limit=None,
        )

        self.assertEqual([item[0] for item in selected], ["plugin-a", "plugin-b"])

    def test_returns_all_plugins_when_limit_is_negative_one(self):
        module = load_validator_module()
        plugins = {
            "plugin-a": {"repo": "https://github.com/example/plugin-a"},
            "plugin-b": {"repo": "https://github.com/example/plugin-b"},
        }

        selected = module.select_plugins(
            plugins=plugins,
            requested_names=None,
            limit=-1,
        )

        self.assertEqual([item[0] for item in selected], ["plugin-a", "plugin-b"])

    def test_prefers_explicit_names_in_requested_order(self):
        module = load_validator_module()
        plugins = {
            "plugin-a": {"repo": "https://github.com/example/plugin-a"},
            "plugin-b": {"repo": "https://github.com/example/plugin-b"},
            "plugin-c": {"repo": "https://github.com/example/plugin-c"},
        }

        selected = module.select_plugins(
            plugins=plugins,
            requested_names=["plugin-c", "plugin-a"],
            limit=None,
        )

        self.assertEqual([item[0] for item in selected], ["plugin-c", "plugin-a"])

    def test_respects_positive_limit_when_names_not_requested(self):
        module = load_validator_module()
        plugins = {
            "plugin-a": {"repo": "https://github.com/example/plugin-a"},
            "plugin-b": {"repo": "https://github.com/example/plugin-b"},
            "plugin-c": {"repo": "https://github.com/example/plugin-c"},
        }

        selected = module.select_plugins(
            plugins=plugins,
            requested_names=None,
            limit=1,
        )

        self.assertEqual([item[0] for item in selected], ["plugin-a"])

    def test_raises_key_error_for_unknown_requested_plugin(self):
        module = load_validator_module()
        plugins = {
            "known-plugin": {"repo": "https://github.com/example/known-plugin"},
        }

        with self.assertRaisesRegex(KeyError, "plugin not found: missing-plugin"):
            module.select_plugins(
                plugins=plugins,
                requested_names=["known-plugin", "missing-plugin"],
                limit=None,
            )


class HelperFunctionTests(unittest.TestCase):
    def test_combine_requested_names_merges_trims_and_drops_empty_values(self):
        module = load_validator_module()

        combined = module.combine_requested_names(
            plugin_names=["foo", "  bar  ", "", "   "],
            plugin_name_list="baz,   qux  , ,foo ",
        )

        self.assertEqual(combined, ["foo", "bar", "baz", "qux", "foo"])

    def test_combine_requested_names_handles_none_inputs(self):
        module = load_validator_module()

        self.assertEqual(module.combine_requested_names(None, None), [])

    def test_sanitize_name_replaces_invalid_chars_and_falls_back_when_needed(self):
        module = load_validator_module()

        self.assertEqual(module.sanitize_name("  -invalid name!*?-  "), "invalid-name")
        self.assertEqual(module.sanitize_name("valid-name_123"), "valid-name_123")
        self.assertEqual(module.sanitize_name("   "), "plugin")
        self.assertEqual(module.sanitize_name("!!!"), "plugin")

    def test_build_plugin_clone_dir_is_unique_for_colliding_sanitized_names(self):
        module = load_validator_module()

        first = module.build_plugin_clone_dir(Path("/tmp/work"), "foo bar")
        second = module.build_plugin_clone_dir(Path("/tmp/work"), "foo/bar")

        self.assertNotEqual(first, second)
        self.assertEqual(first.parent, Path("/tmp/work"))
        self.assertEqual(second.parent, Path("/tmp/work"))

    def test_build_process_output_details_keeps_partial_timeout_logs(self):
        module = load_validator_module()

        details = module.build_process_output_details(
            stdout="line one\nline two\n",
            stderr=b"warning\n",
        )

        self.assertEqual(details, {"stdout": "line one\nline two", "stderr": "warning"})

    def test_parse_simple_yaml_handles_comments_quotes_and_whitespace(self):
        module = load_validator_module()

        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as handle:
            handle.write(
                """
                # leading comment

                key1: value1      # trailing comment
                key2: \" spaced value \"
                key3: 'another value'
                key4: value-with-#-hash
                """
            )
            metadata_path = Path(handle.name)

        try:
            parsed = module._parse_simple_yaml(metadata_path)
        finally:
            os.remove(metadata_path)

        self.assertEqual(parsed["key1"], "value1")
        self.assertEqual(parsed["key2"], " spaced value ")
        self.assertEqual(parsed["key3"], "another value")
        self.assertEqual(parsed["key4"], "value-with-#-hash")

    def test_load_metadata_uses_yaml_safe_load_when_available(self):
        module = load_validator_module()

        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as handle:
            handle.write("name: should-be-overridden\n")
            metadata_path = Path(handle.name)

        fake_yaml = mock.Mock()
        fake_yaml.safe_load.return_value = {"name": "from-yaml", "version": "1.0.0"}

        try:
            with mock.patch.object(module, "yaml", fake_yaml):
                metadata = module.load_metadata(metadata_path)
        finally:
            os.remove(metadata_path)

        self.assertEqual(metadata, {"name": "from-yaml", "version": "1.0.0"})
        fake_yaml.safe_load.assert_called_once()

    def test_load_metadata_uses_simple_parser_when_yaml_unavailable(self):
        module = load_validator_module()

        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as handle:
            handle.write(
                """
                name: demo-plugin
                version: \"0.2.3\"
                """
            )
            metadata_path = Path(handle.name)

        yaml_backup = getattr(module, "yaml", None)
        try:
            module.yaml = None
            metadata = module.load_metadata(metadata_path)
        finally:
            module.yaml = yaml_backup
            os.remove(metadata_path)

        self.assertEqual(metadata.get("name"), "demo-plugin")
        self.assertEqual(metadata.get("version"), "0.2.3")

    def test_load_plugins_index_accepts_valid_object(self):
        module = load_validator_module()

        index_obj = {
            "good-plugin": {"name": "Good Plugin", "repo": "https://github.com/example/good"},
            "another-plugin": {"name": "Another Plugin"},
        }

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(index_obj, handle)
            index_path = Path(handle.name)

        try:
            plugins = module.load_plugins_index(index_path)
        finally:
            os.remove(index_path)

        self.assertEqual(plugins, index_obj)

    def test_load_plugins_index_rejects_json_array(self):
        module = load_validator_module()

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump([{"name": "array-entry"}], handle)
            index_path = Path(handle.name)

        try:
            with self.assertRaisesRegex(ValueError, "plugins.json must contain a JSON object"):
                module.load_plugins_index(index_path)
        finally:
            os.remove(index_path)

    def test_load_plugins_index_rejects_non_dict_values(self):
        module = load_validator_module()

        index_obj = {
            "valid-plugin": {"name": "Valid Plugin", "repo": "https://github.com/example/valid"},
            "not-a-dict": "just-a-string",
        }

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(index_obj, handle)
            index_path = Path(handle.name)

        try:
            with self.assertRaisesRegex(ValueError, "plugins.json values must be objects/dicts"):
                module.load_plugins_index(index_path)
        finally:
            os.remove(index_path)


class ValidationProgressTests(unittest.TestCase):
    def test_validate_selected_plugins_emits_progress_and_result_lines(self):
        module = load_validator_module()
        selected = [
            ("plugin-a", {"repo": "https://github.com/example/plugin-a"}),
            ("plugin-b", {"repo": "https://github.com/example/plugin-b"}),
        ]
        fake_results = [
            {"plugin": "plugin-a", "ok": True, "stage": "load", "message": "ok"},
            {"plugin": "plugin-b", "ok": False, "stage": "metadata", "message": "invalid metadata.yaml"},
        ]

        with mock.patch.object(module, "validate_plugin", side_effect=fake_results) as validate_mock:
            with mock.patch("builtins.print") as print_mock:
                results = module.validate_selected_plugins(
                    selected=selected,
                    astrbot_path=Path("/tmp/AstrBot"),
                    script_path=Path("/tmp/run.py"),
                    work_dir=Path("/tmp/work"),
                    clone_timeout=60,
                    load_timeout=300,
                )

        self.assertEqual(results, fake_results)
        self.assertEqual(validate_mock.call_count, 2)
        print_mock.assert_any_call("[1/2] Validating plugin-a", flush=True)
        print_mock.assert_any_call("[1/2] PASS plugin-a [load] ok", flush=True)
        print_mock.assert_any_call("[2/2] FAIL plugin-b [metadata] invalid metadata.yaml", flush=True)


class MetadataValidationTests(unittest.TestCase):
    def test_reports_missing_required_metadata_fields(self):
        module = load_validator_module()

        with tempfile.TemporaryDirectory() as tmp_dir:
            plugin_dir = Path(tmp_dir)
            (plugin_dir / "metadata.yaml").write_text(
                "name: demo_plugin\nauthor: AstrBot Team\n",
                encoding="utf-8",
            )
            (plugin_dir / "main.py").write_text("print('hello')\n", encoding="utf-8")

            result = module.precheck_plugin_directory(plugin_dir)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "metadata")
        self.assertIn("desc", result["message"])
        self.assertIn("version", result["message"])

    def test_reports_invalid_metadata_yaml_without_raising(self):
        module = load_validator_module()

        with tempfile.TemporaryDirectory() as tmp_dir:
            plugin_dir = Path(tmp_dir)
            (plugin_dir / "metadata.yaml").write_text(
                "name: demo_plugin\n<<<<<<< HEAD\ndesc: broken\n=======\ndesc: fixed\n>>>>>>> branch\n",
                encoding="utf-8",
            )
            (plugin_dir / "main.py").write_text("print('hello')\n", encoding="utf-8")

            result = module.precheck_plugin_directory(plugin_dir)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "metadata")
        self.assertIn("invalid metadata.yaml", result["message"])
        self.assertIn("could not find expected ':'", result["details"])


class WorkerCommandTests(unittest.TestCase):
    def test_build_worker_command_contains_required_arguments(self):
        module = load_validator_module()

        command = module.build_worker_command(
            script_path=Path("/tmp/run.py"),
            astrbot_path=Path("/tmp/astrbot"),
            plugin_source_dir=Path("/tmp/plugin-src"),
            plugin_dir_name="demo_plugin",
            normalized_repo_url="https://github.com/example/demo-plugin",
        )

        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1], "/tmp/run.py")
        self.assertIn("--worker", command)
        self.assertIn("--astrbot-path", command)
        self.assertIn("--plugin-source-dir", command)
        self.assertIn("--plugin-dir-name", command)
        self.assertIn("--normalized-repo-url", command)


class WorkerSysPathTests(unittest.TestCase):
    def test_worker_sys_path_includes_astrbot_root_before_codebase(self):
        module = load_validator_module()

        sys_path_entries = module.build_worker_sys_path(
            astrbot_root=Path("/tmp/astrbot-root"),
            astrbot_path=Path("/tmp/AstrBot"),
        )

        self.assertEqual(
            [Path(item) for item in sys_path_entries],
            [Path("/tmp/astrbot-root").resolve(), Path("/tmp/AstrBot").resolve()],
        )


class ReportBuilderTests(unittest.TestCase):
    def test_build_report_counts_passed_and_failed_results(self):
        module = load_validator_module()

        report = module.build_report(
            [
                {"plugin": "plugin-a", "ok": True, "stage": "load", "message": "ok"},
                {"plugin": "plugin-b", "ok": False, "stage": "metadata", "message": "missing desc"},
            ]
        )

        self.assertEqual(report["summary"]["total"], 2)
        self.assertEqual(report["summary"]["passed"], 1)
        self.assertEqual(report["summary"]["failed"], 1)
        self.assertEqual(report["results"][1]["plugin"], "plugin-b")


class WorkerOutputParsingTests(unittest.TestCase):
    def test_parse_worker_output_keeps_market_plugin_key(self):
        module = load_validator_module()
        completed = subprocess.CompletedProcess(
            args=["python3", "run.py"],
            returncode=1,
            stdout='{"plugin": "demo_plugin", "ok": false, "stage": "load", "message": "boom"}',
            stderr="",
        )

        result = module.parse_worker_output(
            plugin="market-plugin-key",
            repo="https://github.com/example/demo-plugin?tab=readme-ov-file",
            normalized_repo_url="https://github.com/example/demo-plugin",
            completed=completed,
            plugin_dir_name="demo_plugin",
        )

        self.assertEqual(result["plugin"], "market-plugin-key")
        self.assertEqual(result["plugin_dir_name"], "demo_plugin")

    def test_parse_worker_output_uses_last_json_line_after_logs(self):
        module = load_validator_module()
        completed = subprocess.CompletedProcess(
            args=["python3", "run.py"],
            returncode=1,
            stdout='log line\n{"plugin": "demo_plugin", "ok": false, "stage": "load", "message": "boom"}',
            stderr="",
        )

        result = module.parse_worker_output(
            plugin="market-plugin-key",
            repo="https://github.com/example/demo-plugin",
            normalized_repo_url="https://github.com/example/demo-plugin",
            completed=completed,
            plugin_dir_name="demo_plugin",
        )

        self.assertEqual(result["plugin"], "market-plugin-key")
        self.assertEqual(result["stage"], "load")
        self.assertEqual(result["message"], "boom")


if __name__ == "__main__":
    unittest.main()
