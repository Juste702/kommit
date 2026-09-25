import unittest

from commit import (
    MAX_DESCRIPTION,
    UNTRACKED_HEADER,
    build_description,
    build_message,
    classify_type,
    extract_scope,
    parse_diff,
)


def file_diff(
    path: str,
    added: tuple[str, ...] = (),
    removed: tuple[str, ...] = (),
    new: bool = False,
    deleted: bool = False,
    hunk: str = "",
) -> str:
    """Build the diff of one file as `git diff HEAD` prints it."""
    lines = [f"diff --git a/{path} b/{path}"]
    if new:
        lines.append("new file mode 100644")
    if deleted:
        lines.append("deleted file mode 100644")
    lines.append("--- /dev/null" if new else f"--- a/{path}")
    lines.append("+++ /dev/null" if deleted else f"+++ b/{path}")
    lines.append(f"@@ -1,{len(removed)} +1,{len(added)} @@{hunk}")
    lines += [f"-{line}" for line in removed]
    lines += [f"+{line}" for line in added]
    return "\n".join(lines) + "\n"


def untracked_section(*paths: str) -> str:
    """Build the untracked files section that get_diff() appends."""
    return f"\n{UNTRACKED_HEADER}\n" + "\n".join(paths) + "\n"


def signals_for(*paths: str, added: int = 1, removed: int = 1, **overrides) -> dict:
    """Build a minimal signals dict for the given modified paths."""
    signals = {
        "files": list(paths),
        "extensions": [],
        "directories": [],
        "new_files": [],
        "deleted_files": [],
        "added": added,
        "removed": removed,
        "changes_per_file": {path: added + removed for path in paths},
        "functions": [],
        "keywords": {},
    }
    signals.update(overrides)
    return signals


class ParseDiffTest(unittest.TestCase):
    def test_lists_modified_files(self) -> None:
        diff = file_diff("src/a.py", ["x"]) + file_diff("docs/b.md", ["y"])
        self.assertEqual(parse_diff(diff)["files"], ["src/a.py", "docs/b.md"])

    def test_counts_added_and_removed_lines_without_headers(self) -> None:
        signals = parse_diff(file_diff("a.py", ["1", "2", "3"], ["old"]))
        self.assertEqual(signals["added"], 3)
        self.assertEqual(signals["removed"], 1)

    def test_extensions_and_directories(self) -> None:
        diff = file_diff("src/a.PY", ["x"]) + file_diff("README.md", ["y"])
        signals = parse_diff(diff)
        self.assertEqual(signals["extensions"], [".md", ".py"])
        self.assertEqual(signals["directories"], ["src"])

    def test_new_and_deleted_files(self) -> None:
        diff = file_diff("new.py", ["x"], new=True) + file_diff("old.py", removed=("x",), deleted=True)
        signals = parse_diff(diff)
        self.assertEqual(signals["new_files"], ["new.py"])
        self.assertEqual(signals["deleted_files"], ["old.py"])

    def test_untracked_files_are_new_files(self) -> None:
        signals = parse_diff(file_diff("a.py", ["x"]) + untracked_section("b.py", "src/c.py"))
        self.assertEqual(signals["files"], ["a.py", "b.py", "src/c.py"])
        self.assertEqual(signals["new_files"], ["b.py", "src/c.py"])

    def test_untracked_only(self) -> None:
        signals = parse_diff(untracked_section("a.py"))
        self.assertEqual(signals["files"], ["a.py"])
        self.assertEqual(signals["added"], 0)

    def test_functions_from_changed_lines_before_hunk_context(self) -> None:
        diff = file_diff(
            "a.py",
            ["def helper():", "class Parser:", "function render() {"],
            hunk=" def enclosing(x):",
        )
        self.assertEqual(parse_diff(diff)["functions"], ["helper", "Parser", "render", "enclosing"])

    def test_functions_are_deduplicated(self) -> None:
        diff = file_diff("a.py", ["def run():"], ["def run(x):"], hunk=" def run(x):")
        self.assertEqual(parse_diff(diff)["functions"], ["run"])

    def test_keywords_only_in_added_lines(self) -> None:
        diff = file_diff("a.py", ["# Fix the bug", "import os", "# TODO later"], ["# error"])
        keywords = parse_diff(diff)["keywords"]
        self.assertEqual(keywords, {"fix": 1, "bug": 1, "import": 1, "TODO": 1})

    def test_uppercase_keywords_are_case_sensitive(self) -> None:
        keywords = parse_diff(file_diff("a.py", ["# todo and fixme"]))["keywords"]
        self.assertNotIn("TODO", keywords)
        self.assertNotIn("FIXME", keywords)

    def test_content_looking_like_file_headers_is_counted(self) -> None:
        signals = parse_diff(file_diff("a.c", ["++i;"], ["--j;"]))
        self.assertEqual((signals["added"], signals["removed"]), (1, 1))

    def test_empty_diff(self) -> None:
        signals = parse_diff("")
        self.assertEqual(signals["files"], [])
        self.assertEqual(signals["added"], 0)


class ClassifyTypeTest(unittest.TestCase):
    def test_test_files(self) -> None:
        for path in ("tests/test_a.py", "test_a.py", "src/a.test.js", "pkg/test/helpers.py"):
            with self.subTest(path=path):
                self.assertEqual(classify_type(signals_for(path)), "test")

    def test_mixed_test_and_source_is_not_test(self) -> None:
        signals = signals_for("tests/test_a.py", "src/a.py")
        self.assertNotEqual(classify_type(signals), "test")

    def test_markdown_only_is_docs(self) -> None:
        self.assertEqual(classify_type(signals_for("README.md", "docs/guide.MD")), "docs")

    def test_config_only_is_chore(self) -> None:
        signals = signals_for("package.json", "ci.yml", "pyproject.toml", ".env", new_files=["ci.yml"])
        self.assertEqual(classify_type(signals), "chore")

    def test_new_files_with_few_deletions_is_feat(self) -> None:
        signals = signals_for("src/a.py", added=40, removed=9, new_files=["src/a.py"])
        self.assertEqual(classify_type(signals), "feat")

    def test_new_files_with_many_deletions_is_not_feat(self) -> None:
        signals = signals_for("src/a.py", "src/b.py", added=10, removed=30, new_files=["src/a.py"])
        self.assertEqual(classify_type(signals), "refactor")

    def test_fix_keywords(self) -> None:
        for keyword in ("fix", "bug", "error"):
            with self.subTest(keyword=keyword):
                self.assertEqual(classify_type(signals_for("src/a.py", keywords={keyword: 1})), "fix")

    def test_many_deletions_is_refactor(self) -> None:
        self.assertEqual(classify_type(signals_for("src/a.py", added=2, removed=5)), "refactor")

    def test_exact_two_to_one_ratio_is_not_refactor(self) -> None:
        self.assertEqual(classify_type(signals_for("src/a.py", added=2, removed=4)), "chore")

    def test_default_is_chore(self) -> None:
        self.assertEqual(classify_type(signals_for("src/a.py", added=5, removed=5)), "chore")

    def test_priority_test_before_fix(self) -> None:
        signals = signals_for("tests/test_a.py", keywords={"fix": 1})
        self.assertEqual(classify_type(signals), "test")

    def test_priority_feat_before_fix(self) -> None:
        signals = signals_for("src/a.py", added=10, removed=0, new_files=["src/a.py"], keywords={"error": 2})
        self.assertEqual(classify_type(signals), "feat")


class ExtractScopeTest(unittest.TestCase):
    def test_known_directories(self) -> None:
        cases = {
            "src/auth/login.py": "auth",
            "src/api/routes.py": "api",
            "src/ui/button.js": "ui",
            "components/Button.tsx": "ui",
            "src/db/session.py": "db",
            "models/user.py": "db",
            "tests/test_x.py": "tests",
            "docs/guide.md": "docs",
        }
        for path, scope in cases.items():
            with self.subTest(path=path):
                self.assertEqual(extract_scope(signals_for(path)), scope)

    def test_known_file_stems(self) -> None:
        self.assertEqual(extract_scope(signals_for("auth.py")), "auth")
        self.assertEqual(extract_scope(signals_for("src/api.py")), "api")

    def test_single_file_uses_stem(self) -> None:
        self.assertEqual(extract_scope(signals_for("src/utils.py")), "utils")

    def test_single_file_stem_is_slugified(self) -> None:
        self.assertEqual(extract_scope(signals_for("My File_v2.py")), "my-file-v2")

    def test_multiple_files_same_scope(self) -> None:
        self.assertEqual(extract_scope(signals_for("src/auth/a.py", "src/auth/b.py")), "auth")

    def test_multiple_unrelated_scopes_are_omitted(self) -> None:
        self.assertEqual(extract_scope(signals_for("src/auth/a.py", "src/ui/b.py")), "")

    def test_multiple_files_with_unmapped_one_are_omitted(self) -> None:
        self.assertEqual(extract_scope(signals_for("src/auth/a.py", "README.md")), "")

    def test_no_files(self) -> None:
        self.assertEqual(extract_scope(signals_for()), "")


class BuildDescriptionTest(unittest.TestCase):
    def test_single_new_file(self) -> None:
        signals = signals_for("src/auth/login.py", new_files=["src/auth/login.py"])
        self.assertEqual(build_description(signals, "feat", "auth"), "add login.py")

    def test_several_new_files_with_scope(self) -> None:
        signals = signals_for("src/api/a.py", "src/api/b.py", new_files=["src/api/a.py", "src/api/b.py"])
        self.assertEqual(build_description(signals, "feat", "api"), "add api module")

    def test_several_new_files_without_scope(self) -> None:
        signals = signals_for("a.py", "b.py", "c.py", new_files=["a.py", "b.py", "c.py"])
        self.assertEqual(build_description(signals, "feat", ""), "add 3 files")

    def test_deletions_dominant(self) -> None:
        signals = signals_for("src/db/models.py", added=1, removed=10, functions=["Legacy"])
        self.assertEqual(build_description(signals, "refactor", "db"), "remove db Legacy")

    def test_deletions_dominant_without_function(self) -> None:
        signals = signals_for("src/db/models.py", added=0, removed=10)
        self.assertEqual(build_description(signals, "refactor", "db"), "remove db code")

    def test_function_name(self) -> None:
        signals = signals_for("src/utils.py", functions=["getUser"])
        self.assertEqual(build_description(signals, "chore", "api"), "update getUser in api")

    def test_function_name_keeps_its_case(self) -> None:
        signals = signals_for("a.py", functions=["getUser"])
        self.assertEqual(build_description(signals, "chore", ""), "update getUser")

    def test_scope_redundant_with_type_is_dropped(self) -> None:
        signals = signals_for("tests/test_a.py", functions=["test_login"])
        self.assertEqual(build_description(signals, "test", "tests"), "update test_login")

    def test_scope_equal_to_function_is_dropped(self) -> None:
        signals = signals_for("auth.py", functions=["auth"])
        self.assertEqual(build_description(signals, "chore", "auth"), "update auth")

    def test_fallback_to_scope(self) -> None:
        self.assertEqual(build_description(signals_for("src/auth/a.py"), "chore", "auth"), "update auth")

    def test_fallback_to_most_changed_file(self) -> None:
        signals = signals_for("src/a.py", "lib/b.py", changes_per_file={"src/a.py": 2, "lib/b.py": 9})
        self.assertEqual(build_description(signals, "chore", ""), "update b.py")

    def test_scope_is_derived_when_not_given(self) -> None:
        self.assertEqual(build_description(signals_for("src/auth/a.py"), "chore"), "update auth")

    def test_long_description_drops_scope_suffix(self) -> None:
        name = "a_function_name_long_enough_to_overflow"
        signals = signals_for("a.py", functions=[name])
        self.assertEqual(build_description(signals, "chore", "authentication"), f"update {name}")

    def test_max_length_no_capital_no_period(self) -> None:
        signals = signals_for("a.py", functions=["X" * 80])
        description = build_description(signals, "chore", "")
        self.assertLessEqual(len(description), MAX_DESCRIPTION)
        self.assertTrue(description[0].islower())
        self.assertFalse(description.endswith("."))

    def test_long_new_file_name_is_truncated(self) -> None:
        path = "a" * 70 + ".py"
        description = build_description(signals_for(path, new_files=[path]), "feat", "")
        self.assertLessEqual(len(description), MAX_DESCRIPTION)
        self.assertTrue(description.startswith("add "))


class BuildMessageTest(unittest.TestCase):
    def test_messages(self) -> None:
        cases = {
            "test": (
                file_diff("tests/test_auth.py", ["def test_login():", "    assert True"]),
                "test(tests): update test_login",
            ),
            "docs": (file_diff("README.md", ["more docs"]), "docs(readme): update readme"),
            "config": (
                file_diff("package.json", ['"x": 1']) + file_diff("config.yml", ["a: b"]),
                "chore: update package.json",
            ),
            "feat new file": (
                file_diff("src/auth/login.py", ["def login(user):", "    pass"], new=True),
                "feat(auth): add login.py",
            ),
            "feat untracked": (
                untracked_section("src/api/routes.py", "src/api/handlers.py"),
                "feat(api): add api module",
            ),
            "fix": (
                file_diff(
                    "src/api/client.py",
                    ["    # fix error on timeout", "    retry()"],
                    ["    call()"],
                    hunk=" def fetch(url):",
                ),
                "fix(api): update fetch in api",
            ),
            "refactor": (
                file_diff("src/db/models.py", ["pass"], ["class Legacy:", "  a", "  b", "  c", "  d"]),
                "refactor(db): remove db Legacy",
            ),
            "unrelated scopes": (
                file_diff("src/auth/a.py", ["x"], ["y"]) + file_diff("src/ui/b.js", ["x"], ["y"]),
                "chore: update a.py",
            ),
        }
        for name, (diff, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(build_message(diff), expected)

    def test_format_without_scope(self) -> None:
        message = build_message(file_diff("a/x.py", ["1"]) + file_diff("b/y.py", ["2"]))
        self.assertRegex(message, r"^[a-z]+: \S")

    def test_format_with_scope(self) -> None:
        self.assertRegex(build_message(file_diff("src/utils.py", ["1"])), r"^[a-z]+\([a-z0-9-]+\): \S")


if __name__ == "__main__":
    unittest.main()
