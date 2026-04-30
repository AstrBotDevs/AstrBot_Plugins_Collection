import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "transform_plugin_data" / "run.py"


def load_transform_module():
    if not MODULE_PATH.exists():
        raise AssertionError(f"transform script missing: {MODULE_PATH}")

    spec = importlib.util.spec_from_file_location("transform_plugin_data_run", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load transform module spec")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MetadataParsingTests(unittest.TestCase):
    def test_parse_metadata_text_extracts_new_optional_fields(self):
        module = load_transform_module()

        metadata = module.parse_metadata_text(
            'version: "1.2.3"\n'
            'astrbot_version: ">=4.19.4"\n'
            "support_platforms: aiocqhttp\n"
        )

        self.assertEqual(metadata["version"], "1.2.3")
        self.assertEqual(metadata["astrbot_version"], ">=4.19.4")
        self.assertEqual(metadata["support_platforms"], "aiocqhttp")

    def test_parse_metadata_text_supports_inline_and_block_lists(self):
        module = load_transform_module()

        inline_metadata = module.parse_metadata_text("support_platforms: [aiocqhttp, qq]\n")
        block_metadata = module.parse_metadata_text(
            "support_platforms:\n"
            "  - aiocqhttp\n"
            "  - qq\n"
        )

        self.assertEqual(inline_metadata["support_platforms"], ["aiocqhttp", "qq"])
        self.assertEqual(block_metadata["support_platforms"], ["aiocqhttp", "qq"])


class TransformPluginDataTests(unittest.TestCase):
    def test_transform_plugin_data_includes_metadata_fields_from_repo_info(self):
        module = load_transform_module()
        original_plugins = {
            "demo": {
                "desc": "demo plugin",
                "author": "AstrBot Team",
                "repo": "https://github.com/example/demo",
                "tags": [],
            }
        }
        repo_info = {
            "https://github.com/example/demo": {
                "status": "success",
                "stars": 3,
                "updated_at": "2026-04-30T00:00:00Z",
                "version": "1.2.3",
                "astrbot_version": ">=4.19.4",
                "support_platforms": "aiocqhttp",
                "logo": "",
            }
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_cwd = os.getcwd()
            os.chdir(tmp_dir)
            try:
                result = module.transform_plugin_data(original_plugins, repo_info, {})
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(result["demo"]["astrbot_version"], ">=4.19.4")
        self.assertEqual(result["demo"]["support_platforms"], "aiocqhttp")

    def test_transform_plugin_data_preserves_metadata_fields_from_cache(self):
        module = load_transform_module()
        original_plugins = {
            "demo": {
                "desc": "demo plugin",
                "author": "AstrBot Team",
                "repo": "https://github.com/example/demo",
                "tags": [],
            }
        }
        repo_info = {
            "https://github.com/example/demo": {
                "status": "success",
                "stars": 3,
                "updated_at": "2026-04-30T00:00:00Z",
                "version": "",
                "logo": "",
            }
        }
        existing_cache = {
            "demo": {
                "repo": "https://github.com/example/demo",
                "version": "1.2.2",
                "astrbot_version": ">=4.18.0",
                "support_platforms": "aiocqhttp",
            }
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_cwd = os.getcwd()
            os.chdir(tmp_dir)
            try:
                result = module.transform_plugin_data(original_plugins, repo_info, existing_cache)
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(result["demo"]["astrbot_version"], ">=4.18.0")
        self.assertEqual(result["demo"]["support_platforms"], "aiocqhttp")


if __name__ == "__main__":
    unittest.main()
