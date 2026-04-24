import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "validate_plugins" / "detect_changed_plugins.py"


def load_detection_module():
    if not MODULE_PATH.exists():
        raise AssertionError(f"detection script missing: {MODULE_PATH}")

    spec = importlib.util.spec_from_file_location("detect_changed_plugins", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load detection module spec")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LoadPluginsMapTests(unittest.TestCase):
    def test_load_plugins_map_requires_json_object(self):
        module = load_detection_module()

        with self.assertRaisesRegex(ValueError, "plugins.json must contain a JSON object"):
            module.load_plugins_map('[{"name": "bad"}]', source_name="head")

    def test_load_plugins_map_returns_dict_for_valid_json(self):
        module = load_detection_module()

        plugins = module.load_plugins_map('{"plugin-a": {"repo": "https://github.com/example/a"}}', source_name="head")

        self.assertEqual(plugins, {"plugin-a": {"repo": "https://github.com/example/a"}})

    def test_load_plugins_map_rejects_non_dict_entries(self):
        module = load_detection_module()

        with self.assertRaisesRegex(ValueError, "plugins.json entry 'plugin-a' on the PR head must be a JSON object"):
            module.load_plugins_map('{"plugin-a": "bad"}', source_name="PR head")


class ChangedPluginDetectionTests(unittest.TestCase):
    def test_detect_changed_plugin_names_returns_only_modified_entries(self):
        module = load_detection_module()

        changed = module.detect_changed_plugin_names(
            base={"plugin-a": {"repo": "a"}, "plugin-b": {"repo": "b"}},
            head={"plugin-a": {"repo": "a"}, "plugin-b": {"repo": "changed"}, "plugin-c": {"repo": "c"}},
        )

        self.assertEqual(changed, ["plugin-b", "plugin-c"])


class AstrbotRefTests(unittest.TestCase):
    def test_resolve_astrbot_ref_uses_remote_default_branch(self):
        module = load_detection_module()

        with mock.patch.object(module.subprocess, "check_output", return_value="ref: refs/heads/main\tHEAD\nabc\tHEAD\n"):
            ref = module.resolve_astrbot_ref()

        self.assertEqual(ref, "main")

    def test_resolve_astrbot_ref_falls_back_to_master(self):
        module = load_detection_module()

        with mock.patch.object(module.subprocess, "check_output", side_effect=module.subprocess.CalledProcessError(1, ["git"])):
            ref = module.resolve_astrbot_ref()

        self.assertEqual(ref, "master")


class PullRequestDetectionTests(unittest.TestCase):
    def test_detect_pull_request_selection_handles_missing_base_file(self):
        module = load_detection_module()

        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            plugins_json = repo_root / "plugins.json"
            plugins_json.write_text('{"plugin-a": {"repo": "https://github.com/example/a"}}', encoding="utf-8")

            with mock.patch.object(module, "fetch_base_ref") as fetch_mock:
                with mock.patch.object(module, "read_base_plugins_json", side_effect=module.subprocess.CalledProcessError(1, ["git"])):
                    result = module.detect_pull_request_selection(repo_root=repo_root, base_ref="main")

        fetch_mock.assert_called_once_with("main")
        self.assertEqual(result["changed"], ["plugin-a"])
        self.assertEqual(result["validation_note"], "")

    def test_detect_pull_request_selection_raises_on_invalid_head_json(self):
        module = load_detection_module()

        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            (repo_root / "plugins.json").write_text('{bad json', encoding="utf-8")

            with mock.patch.object(module, "fetch_base_ref"):
                with mock.patch.object(module, "read_base_plugins_json", return_value='{}'):
                    with self.assertRaisesRegex(ValueError, "plugins.json is invalid on the PR head"):
                        module.detect_pull_request_selection(repo_root=repo_root, base_ref="main")

    def test_write_github_env_outputs_expected_values(self):
        module = load_detection_module()

        with tempfile.NamedTemporaryFile("w+", delete=False) as handle:
            env_path = Path(handle.name)

        try:
            module.write_github_env(
                env_path=env_path,
                astrbot_ref="master",
                changed=["plugin-a", "plugin-b"],
                should_validate=True,
                validation_note="",
            )
            content = env_path.read_text(encoding="utf-8")
        finally:
            env_path.unlink(missing_ok=True)

        self.assertIn("ASTRBOT_REF=master\n", content)
        self.assertIn("PLUGIN_NAME_LIST=plugin-a,plugin-b\n", content)
        self.assertIn("SHOULD_VALIDATE=true\n", content)


if __name__ == "__main__":
    unittest.main()
