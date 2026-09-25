import json
import os
import tempfile
import unittest

from commit import (
    DEFAULT_CONFIG,
    ConfigError,
    build_description,
    build_message,
    classify_type,
    extract_scope,
    load_config,
    parse_config,
    parse_diff,
)
from tests.test_commit import file_diff, signals_for

EXAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".commitrc.example")


class ParseConfigTest(unittest.TestCase):
    def test_empty_object_gives_defaults(self) -> None:
        self.assertEqual(parse_config({}), DEFAULT_CONFIG)

    def test_example_file_is_valid(self) -> None:
        config = load_config(EXAMPLE)
        self.assertEqual([rule.type for rule in config.rules], ["ci", "build", "perf"])

    def test_scopes_are_merged_over_defaults(self) -> None:
        config = parse_config({"scopes": {"Services": "api"}})
        self.assertEqual(config.scopes["services"], "api")
        self.assertEqual(config.scopes["auth"], "auth")

    def test_null_scope_removes_default(self) -> None:
        self.assertNotIn("models", parse_config({"scopes": {"models": None}}).scopes)

    def test_lists_replace_defaults(self) -> None:
        config = parse_config({"fix_keywords": ["crash"], "config_extensions": [".INI"]})
        self.assertEqual(config.fix_keywords, ("crash",))
        self.assertEqual(config.config_extensions, frozenset({".ini"}))

    def test_invalid_configs(self) -> None:
        cases = {
            "not an object": [],
            "unknown key": {"typo": 1},
            "rules not a list": {"rules": {}},
            "rule not an object": {"rules": ["ci"]},
            "rule unknown key": {"rules": [{"type": "ci", "files": ["x"], "scope": "ci"}]},
            "rule bad type": {"rules": [{"type": "CI!", "files": ["x"]}]},
            "rule without condition": {"rules": [{"type": "ci"}]},
            "rule bad match": {"rules": [{"type": "ci", "files": ["x"], "match": "some"}]},
            "rule files not strings": {"rules": [{"type": "ci", "files": [1]}]},
            "scopes not an object": {"scopes": ["api"]},
            "bad scope value": {"scopes": {"x": "Not Valid"}},
            "extension without dot": {"config_extensions": ["json"]},
            "keywords not a list": {"fix_keywords": "fix"},
            "empty keyword": {"fix_keywords": [""]},
            "max too small": {"max_description": 5},
            "max not an int": {"max_description": "50"},
            "max is a bool": {"max_description": True},
        }
        for name, data in cases.items():
            with self.subTest(name), self.assertRaises(ConfigError):
                parse_config(data)

    def test_error_message_names_the_problem(self) -> None:
        with self.assertRaisesRegex(ConfigError, r"rules\[1\].*\"match\""):
            parse_config({"rules": [{"type": "ci", "files": ["x"]}, {"type": "ci", "files": ["x"], "match": "x"}]})


class LoadConfigTest(unittest.TestCase):
    def write(self, content: str) -> str:
        """Write a temporary .commitrc and return its path."""
        handle, path = tempfile.mkstemp(suffix=".commitrc")
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(content)
        self.addCleanup(os.remove, path)
        return path

    def test_loads_valid_file(self) -> None:
        path = self.write(json.dumps({"max_description": 72}))
        self.assertEqual(load_config(path).max_description, 72)

    def test_invalid_json_reports_position(self) -> None:
        path = self.write('{"max_description": 72,}')
        with self.assertRaisesRegex(ConfigError, r"not valid JSON \(line 1, column"):
            load_config(path)

    def test_invalid_content_reports_path(self) -> None:
        path = self.write('{"typo": 1}')
        with self.assertRaisesRegex(ConfigError, "unknown key"):
            load_config(path)

    def test_missing_file(self) -> None:
        with self.assertRaisesRegex(ConfigError, "cannot read"):
            load_config("/nonexistent/.commitrc")


class CustomRulesTest(unittest.TestCase):
    def test_rule_on_all_files(self) -> None:
        config = parse_config({"rules": [{"type": "ci", "files": [".github/*"]}]})
        self.assertEqual(classify_type(signals_for(".github/workflows/test.yml"), config), "ci")
        self.assertNotEqual(
            classify_type(signals_for(".github/workflows/test.yml", "src/a.py"), config), "ci"
        )

    def test_rule_on_any_file(self) -> None:
        config = parse_config({"rules": [{"type": "build", "files": ["Dockerfile"], "match": "any"}]})
        self.assertEqual(classify_type(signals_for("docker/Dockerfile", "src/a.py"), config), "build")

    def test_pattern_without_slash_matches_file_name(self) -> None:
        config = parse_config({"rules": [{"type": "build", "files": ["requirements*.txt"]}]})
        self.assertEqual(classify_type(signals_for("deps/requirements-dev.txt"), config), "build")

    def test_rule_on_keywords(self) -> None:
        config = parse_config({"rules": [{"type": "perf", "keywords": ["optimize"]}]})
        diff = file_diff("src/a.py", ["# optimize the loop"])
        self.assertEqual(build_message(diff, config), "perf(a): update a")

    def test_rule_needs_both_files_and_keywords(self) -> None:
        config = parse_config({"rules": [{"type": "perf", "files": ["src/*"], "keywords": ["cache"]}]})
        self.assertEqual(classify_type(parse_diff(file_diff("src/a.py", ["add cache"]), config), config), "perf")
        self.assertNotEqual(classify_type(parse_diff(file_diff("lib/a.py", ["add cache"]), config), config), "perf")
        self.assertNotEqual(classify_type(parse_diff(file_diff("src/a.py", ["x"]), config), config), "perf")

    def test_new_file_with_custom_type_is_added(self) -> None:
        config = parse_config({"rules": [{"type": "ci", "files": [".github/*"]}]})
        diff = file_diff(".github/workflows/deploy.yml", ["on: push"], new=True)
        self.assertEqual(build_message(diff, config), "ci(deploy): add deploy.yml")

    def test_custom_rules_run_before_built_in_rules(self) -> None:
        config = parse_config({"rules": [{"type": "ci", "files": ["tests/*"]}]})
        self.assertEqual(classify_type(signals_for("tests/test_a.py"), config), "ci")

    def test_first_matching_rule_wins(self) -> None:
        config = parse_config({"rules": [
            {"type": "ci", "files": ["*.yml"]},
            {"type": "build", "files": ["*.yml"]},
        ]})
        self.assertEqual(classify_type(signals_for("a.yml"), config), "ci")

    def test_rule_with_files_never_matches_empty_diff(self) -> None:
        config = parse_config({"rules": [{"type": "ci", "files": ["*"]}]})
        self.assertEqual(classify_type(signals_for(), config), "chore")


class CustomSettingsTest(unittest.TestCase):
    def test_custom_scope(self) -> None:
        config = parse_config({"scopes": {"services": "api"}})
        self.assertEqual(extract_scope(signals_for("src/services/a.py", "src/services/b.py"), config), "api")

    def test_removed_scope_falls_back_to_file_name(self) -> None:
        config = parse_config({"scopes": {"models": None}})
        self.assertEqual(extract_scope(signals_for("models/user.py"), config), "user")

    def test_custom_fix_keywords(self) -> None:
        config = parse_config({"fix_keywords": ["crash"]})
        diff = file_diff("src/a.py", ["# crash on empty input"])
        self.assertEqual(classify_type(parse_diff(diff, config), config), "fix")
        diff = file_diff("src/a.py", ["# fix typo"])
        self.assertEqual(classify_type(parse_diff(diff, config), config), "chore")

    def test_custom_config_extensions(self) -> None:
        config = parse_config({"config_extensions": [".ini"]})
        self.assertEqual(classify_type(signals_for("setup.ini", added=5, removed=5), config), "chore")
        self.assertEqual(classify_type(signals_for(".env", new_files=[".env"]), config), "feat")

    def test_custom_max_description(self) -> None:
        name = "a_function_name_long_enough_to_overflow"
        signals = signals_for("a.py", functions=[name])
        config = parse_config({"max_description": 72})
        self.assertEqual(build_description(signals, "chore", "authentication", config), f"update {name} in authentication")
        short = parse_config({"max_description": 10})
        self.assertLessEqual(len(build_description(signals, "chore", "", short)), 10)


if __name__ == "__main__":
    unittest.main()
