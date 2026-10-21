"""Exercise firmware state properties without a Home Assistant installation."""

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / 'custom_components' / 'keenetic_api' if (ROOT / 'custom_components').is_dir() else ROOT / 'keenetic_api'
source = ast.parse((COMPONENT / "update.py").read_text(encoding="utf-8"))
entity = next(node for node in source.body if isinstance(node, ast.ClassDef))
properties = [
    node for node in entity.body
    if isinstance(node, ast.FunctionDef)
    and any(isinstance(item, ast.Name) and item.id == "property" for item in node.decorator_list)
]
isolated_class = ast.ClassDef(
    name="FirmwareState", bases=[], keywords=[], body=properties, decorator_list=[]
)
module = ast.fix_missing_locations(ast.Module(body=[isolated_class], type_ignores=[]))
namespace = {}
exec(compile(module, str(ROOT / "keenetic_api" / "update.py"), "exec"), namespace)


class FirmwareStateTest(unittest.TestCase):
    def make_state(self, data, old_version=None):
        state = namespace["FirmwareState"]()
        state.coordinator = SimpleNamespace(data=data)
        state._in_progress_old_version = old_version
        return state

    def test_missing_firmware_does_not_crash_or_report_installing(self):
        for data in (None, {}, {"current": None, "new": None}):
            with self.subTest(data=data):
                state = self.make_state(data)
                self.assertIsNone(state.installed_version)
                self.assertIsNone(state.latest_version)
                self.assertIsNone(state.title)
                self.assertIsNone(state.release_url)
                self.assertFalse(state.in_progress)

    def test_unexpected_version_types(self):
        for value in ([], "unavailable", False, 42):
            with self.subTest(value=value):
                state = self.make_state({"current": value, "new": value})
                self.assertIsNone(state.installed_version)
                self.assertIsNone(state.latest_version)

    def test_valid_versions_and_install_completion(self):
        state = self.make_state({
            "current": {"title": "4.3"}, "new": {"title": "4.4"},
            "channel": "stable", "release_notes": "https://example.test/notes",
        }, old_version="4.3")
        self.assertEqual(state.installed_version, "4.3")
        self.assertEqual(state.latest_version, "4.4")
        self.assertEqual(state.title, "stable")
        self.assertEqual(state.release_url, "https://example.test/notes")
        self.assertTrue(state.in_progress)
        state.coordinator.data["current"]["title"] = "4.4"
        self.assertFalse(state.in_progress)

    def test_integration_python_syntax(self):
        for path in COMPONENT.glob("*.py"):
            with self.subTest(file=path.name):
                compile(path.read_text(encoding="utf-8"), str(path), "exec")


if __name__ == "__main__":
    unittest.main()
