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
                "# leading comment\n\n"
                "key1: value1      # trailing comment\n"
                'key2: " spaced value "\n'
                "key3: 'another value'\n"
                "key4: value-with-#-hash\n"
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

    def test_parse_simple_yaml_rejects_indented_lines(self):
        module = load_validator_module()

        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as handle:
            handle.write("name: demo\n  nested: nope\n")
            metadata_path = Path(handle.name)

        try:
            with self.assertRaisesRegex(ValueError, "Unsupported YAML indentation"):
                module._parse_simple_yaml(metadata_path)
        finally:
            os.remove(metadata_path)

    def test_parse_simple_yaml_rejects_list_syntax(self):
        module = load_validator_module()

        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as handle:
            handle.write("- item\n")
            metadata_path = Path(handle.name)

        try:
            with self.assertRaisesRegex(ValueError, "Unsupported YAML list syntax"):
                module._parse_simple_yaml(metadata_path)
        finally:
            os.remove(metadata_path)

    def test_parse_simple_yaml_rejects_duplicate_keys(self):
        module = load_validator_module()

        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as handle:
            handle.write("name: first\nname: second\n")
            metadata_path = Path(handle.name)

        try:
            with self.assertRaisesRegex(ValueError, "Duplicate key 'name'"):
                module._parse_simple_yaml(metadata_path)
        finally:
            os.remove(metadata_path)

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

    def test_load_metadata_rejects_non_mapping_yaml_root(self):
        module = load_validator_module()

        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as handle:
            handle.write("- item\n")
            metadata_path = Path(handle.name)

        fake_yaml = mock.Mock()
        fake_yaml.safe_load.return_value = ["item"]
        fake_yaml.YAMLError = ValueError

        try:
            with mock.patch.object(module, "yaml", fake_yaml):
                with self.assertRaisesRegex(
                    module.MetadataLoadError,
                    "metadata.yaml must contain a mapping at the top level",
                ):
                    module.load_metadata(metadata_path)
        finally:
            os.remove(metadata_path)

    def test_load_metadata_uses_simple_parser_when_yaml_unavailable(self):
        module = load_validator_module()

        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as handle:
            handle.write('name: demo-plugin\nversion: "0.2.3"\n')
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

    def test_load_metadata_wraps_fallback_parse_errors(self):
        module = load_validator_module()

        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as handle:
            handle.write("name: demo\n  nested: nope\n")
            metadata_path = Path(handle.name)

        yaml_backup = getattr(module, "yaml", None)
        try:
            module.yaml = None
            with self.assertRaisesRegex(module.MetadataLoadError, "Unsupported YAML indentation"):
                module.load_metadata(metadata_path)
        finally:
            module.yaml = yaml_backup
            os.remove(metadata_path)

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
            with self.assertRaisesRegex(ValueError, "plugins.json entry 'not-a-dict'.*must be a JSON object"):
                module.load_plugins_index(index_path)
        finally:
            os.remove(index_path)


class ValidationProgressTests(unittest.TestCase):
    def test_build_parser_defaults_max_workers_to_eight(self):
        module = load_validator_module()

        args = module.build_parser().parse_args(["--astrbot-path", "/tmp/AstrBot"])

        self.assertEqual(args.max_workers, 8)

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
                    max_workers=8,
                )

        self.assertEqual(results, fake_results)
        self.assertEqual(validate_mock.call_count, 2)
        print_mock.assert_any_call("[1/2] Queued plugin-a", flush=True)
        print_mock.assert_any_call("[1/2] PASS plugin-a [load] ok", flush=True)
        print_mock.assert_any_call("[2/2] FAIL plugin-b [metadata] invalid metadata.yaml", flush=True)

    def test_validate_selected_plugins_preserves_result_order_with_out_of_order_completion(self):
        module = load_validator_module()
        selected = [
            ("plugin-a", {"repo": "https://github.com/example/plugin-a"}),
            ("plugin-b", {"repo": "https://github.com/example/plugin-b"}),
            ("plugin-c", {"repo": "https://github.com/example/plugin-c"}),
        ]
        futures = [mock.Mock(name="future-a"), mock.Mock(name="future-b"), mock.Mock(name="future-c")]
        future_to_result = {
            futures[0]: (1, {"plugin": "plugin-a", "ok": True, "stage": "load", "message": "a"}),
            futures[1]: (2, {"plugin": "plugin-b", "ok": False, "stage": "metadata", "message": "b"}),
            futures[2]: (3, {"plugin": "plugin-c", "ok": True, "stage": "load", "message": "c"}),
        }

        executor = mock.MagicMock()
        executor.__enter__.return_value = executor
        executor.__exit__.return_value = False
        executor.submit.side_effect = futures

        def future_result(future):
            return future_to_result[future]

        for future in futures:
            future.result.side_effect = lambda _timeout=None, future=future: future_result(future)

        with mock.patch.object(module.concurrent.futures, "ThreadPoolExecutor", return_value=executor) as pool_mock:
            with mock.patch.object(module.concurrent.futures, "as_completed", return_value=[futures[2], futures[0], futures[1]]):
                with mock.patch("builtins.print") as print_mock:
                    results = module.validate_selected_plugins(
                        selected=selected,
                        astrbot_path=Path("/tmp/AstrBot"),
                        script_path=Path("/tmp/run.py"),
                        work_dir=Path("/tmp/work"),
                        clone_timeout=60,
                        load_timeout=300,
                        max_workers=8,
                    )

        pool_mock.assert_called_once_with(max_workers=8)
        self.assertEqual([item["plugin"] for item in results], ["plugin-a", "plugin-b", "plugin-c"])
        print_mock.assert_any_call("[1/3] Queued plugin-a", flush=True)
        print_mock.assert_any_call("[3/3] PASS plugin-c [load] c", flush=True)


class ValidatePluginTests(unittest.TestCase):
    def setUp(self):
        self.module = load_validator_module()
        self.plugin_key = "demo-plugin"
        self.plugin_data = {"repo": "https://github.com/example/demo-plugin"}
        self.astrbot_path = Path("/tmp/AstrBot")
        self.script_path = Path("/tmp/run.py")
        self.work_dir = Path("/tmp/work")

    def call_validate_plugin(self, plugin_data=None):
        return self.module.validate_plugin(
            plugin=self.plugin_key,
            plugin_data=self.plugin_data if plugin_data is None else plugin_data,
            astrbot_path=self.astrbot_path,
            script_path=self.script_path,
            work_dir=self.work_dir,
            clone_timeout=30,
            load_timeout=60,
        )

    def test_missing_repo_field_sets_repo_url_stage(self):
        result = self.call_validate_plugin(plugin_data={})

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "repo_url")
        self.assertEqual(result["message"], "missing repo field")

    def test_invalid_repo_url_sets_repo_url_stage(self):
        with mock.patch.object(self.module, "normalize_repo_url", side_effect=ValueError("invalid repo URL")):
            result = self.call_validate_plugin()

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "repo_url")
        self.assertEqual(result["message"], "invalid repo URL")

    def test_clone_called_process_error_uses_stderr_or_stdout(self):
        error = subprocess.CalledProcessError(
            returncode=1,
            cmd=["git", "clone"],
            output="clone stdout",
            stderr="clone stderr",
        )

        with mock.patch.object(self.module, "clone_plugin_repo", side_effect=error):
            result = self.call_validate_plugin()

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "clone")
        self.assertIn("clone stderr", result["message"])

    def test_clone_timeout_uses_process_output_details(self):
        timeout = subprocess.TimeoutExpired(cmd=["git", "clone"], timeout=30, output="slow", stderr="warning")

        with mock.patch.object(self.module, "clone_plugin_repo", side_effect=timeout):
            with mock.patch.object(
                self.module,
                "build_process_output_details",
                return_value={"stdout": "slow", "stderr": "warning"},
            ) as details_mock:
                result = self.call_validate_plugin()

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "clone_timeout")
        self.assertEqual(result["details"], {"stdout": "slow", "stderr": "warning"})
        details_mock.assert_called_once_with(stdout="slow", stderr="warning")

    def test_precheck_failure_is_mapped_into_result(self):
        with mock.patch.object(self.module, "clone_plugin_repo"):
            with mock.patch.object(
                self.module,
                "precheck_plugin_directory",
                return_value={"ok": False, "stage": "metadata", "message": "invalid metadata", "details": "line 3"},
            ):
                result = self.call_validate_plugin()

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "metadata")
        self.assertEqual(result["message"], "invalid metadata")
        self.assertEqual(result["details"], "line 3")

    def test_successful_clone_and_precheck_invokes_worker_and_parses_output(self):
        completed = subprocess.CompletedProcess(
            args=["python3", "run.py"],
            returncode=0,
            stdout='{"ok": true}',
            stderr="",
        )
        parsed_output = {"ok": True, "stage": "load", "message": "plugin loaded successfully"}

        with mock.patch.object(
            self.module,
            "precheck_plugin_directory",
            return_value={"ok": True, "plugin_dir_name": "demo_plugin", "message": "ok", "stage": "precheck"},
        ) as precheck_mock:
            with mock.patch.object(self.module, "clone_plugin_repo"):
                with mock.patch.object(subprocess, "run", return_value=completed) as run_mock:
                    with mock.patch.object(self.module, "parse_worker_output", return_value=parsed_output) as parse_mock:
                        result = self.call_validate_plugin()

        self.assertEqual(result, parsed_output)
        precheck_mock.assert_called_once()
        run_mock.assert_called_once()
        parse_mock.assert_called_once_with(
            plugin=self.plugin_key,
            repo=self.plugin_data["repo"],
            normalized_repo_url=self.plugin_data["repo"],
            completed=completed,
            plugin_dir_name="demo_plugin",
        )


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
