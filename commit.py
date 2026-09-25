from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

Signals = dict[str, Any]

UNTRACKED_HEADER = "New untracked files:"

CONFIG_EXTENSIONS = {".json", ".yml", ".yaml", ".env", ".toml"}
FIX_KEYWORDS = ("fix", "bug", "error")
KEYWORDS = ("fix", "bug", "error", "test", "import", "TODO", "FIXME")

# Directory (or file stem) -> scope name
SCOPE_MAP = {
    "auth": "auth",
    "api": "api",
    "ui": "ui",
    "components": "ui",
    "db": "db",
    "models": "db",
    "tests": "tests",
    "test": "tests",
    "docs": "docs",
}

DEFINITION_RE = re.compile(r"\b(?:def|class|function)\s+([A-Za-z_$][\w$]*)")
HUNK_RE = re.compile(r"^@@ [^@]* @@(.*)$")
MAX_DESCRIPTION = 50
# Hash of git's empty tree, used to diff a repository that has no commit yet
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

CONFIG_FILE = ".commitrc"
TYPE_RE = re.compile(r"^[a-z]+$")
SCOPE_RE = re.compile(r"^[a-z0-9-]+$")


@dataclass(frozen=True)
class Rule:
    """A custom type rule from .commitrc, checked before the built-in rules."""

    type: str
    files: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    match: str = "all"


@dataclass(frozen=True)
class Config:
    """Settings that customize the algorithm; defaults reproduce the built-in rules."""

    rules: tuple[Rule, ...] = ()
    scopes: dict[str, str] = field(default_factory=lambda: dict(SCOPE_MAP))
    config_extensions: frozenset[str] = frozenset(CONFIG_EXTENSIONS)
    fix_keywords: tuple[str, ...] = FIX_KEYWORDS
    max_description: int = MAX_DESCRIPTION

    @property
    def keywords(self) -> tuple[str, ...]:
        """Every keyword parse_diff() must count for this config."""
        wanted = list(KEYWORDS) + list(self.fix_keywords)
        for rule in self.rules:
            wanted += rule.keywords
        return tuple(dict.fromkeys(wanted))


DEFAULT_CONFIG = Config()


class ConfigError(ValueError):
    """Raised when a .commitrc file cannot be read or is invalid."""


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command and capture its output; exit if git is not installed."""
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True)
    except FileNotFoundError:
        print("Error: git is not installed or not in PATH.")
        sys.exit(1)


def _fail(step: str, result: subprocess.CompletedProcess[str]) -> None:
    """Print a git failure with its output and exit with an error code."""
    output = (result.stderr or result.stdout).strip()
    print(f"Error: {step} failed (exit code {result.returncode}).")
    if output:
        print(output)
    sys.exit(1)


def get_diff() -> str:
    """Return the diff against HEAD plus a list of untracked files; exit if nothing to do."""
    if _git("rev-parse", "--is-inside-work-tree").returncode != 0:
        print("Error: this directory is not a git repository.")
        sys.exit(1)

    has_head = _git("rev-parse", "--verify", "--quiet", "HEAD").returncode == 0
    result = _git("diff", "HEAD" if has_head else EMPTY_TREE)
    if result.returncode != 0:
        _fail("git diff", result)

    # Porcelain paths are relative to the repository root, like the diff paths
    status = _git("status", "--porcelain", "-z", "--untracked-files=all")
    if status.returncode != 0:
        _fail("git status", status)
    untracked = [
        entry[3:]
        for entry in status.stdout.split("\0")
        if entry.startswith("?? ")
    ]

    diff = result.stdout
    if untracked:
        diff += f"\n{UNTRACKED_HEADER}\n" + "\n".join(untracked) + "\n"

    if not diff.strip():
        print("No changes detected.")
        sys.exit(0)

    return diff


def _string_list(value: Any, key: str) -> tuple[str, ...]:
    """Validate a JSON list of non-empty strings."""
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f'"{key}" must be a list of non-empty strings.')
    return tuple(value)


def _parse_rule(value: Any, index: int) -> Rule:
    """Validate one entry of the "rules" list."""
    where = f"rules[{index}]"
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be an object.")
    unknown = set(value) - {"type", "files", "keywords", "match"}
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}.")
    type_ = value.get("type")
    if not isinstance(type_, str) or not TYPE_RE.match(type_):
        raise ConfigError(f'{where}: "type" must be a lowercase word such as "ci" or "perf".')
    files = _string_list(value.get("files", []), f"{where}.files")
    keywords = _string_list(value.get("keywords", []), f"{where}.keywords")
    if not files and not keywords:
        raise ConfigError(f'{where}: set "files", "keywords" or both.')
    match = value.get("match", "all")
    if match not in ("all", "any"):
        raise ConfigError(f'{where}: "match" must be "all" or "any".')
    return Rule(type_, files, keywords, match)


def parse_config(data: Any) -> Config:
    """Validate decoded .commitrc content and merge it over the defaults."""
    if not isinstance(data, dict):
        raise ConfigError("the file must contain a JSON object.")
    allowed = {"rules", "scopes", "config_extensions", "fix_keywords", "max_description"}
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(
            f"unknown key(s) {', '.join(sorted(unknown))}; "
            f"allowed keys are {', '.join(sorted(allowed))}."
        )

    rules = data.get("rules", [])
    if not isinstance(rules, list):
        raise ConfigError('"rules" must be a list.')

    scopes = dict(SCOPE_MAP)
    custom_scopes = data.get("scopes", {})
    if not isinstance(custom_scopes, dict):
        raise ConfigError('"scopes" must be an object mapping names to scopes.')
    for name, scope in custom_scopes.items():
        if scope is None:
            scopes.pop(name.lower(), None)
        elif isinstance(scope, str) and SCOPE_RE.match(scope):
            scopes[name.lower()] = scope
        else:
            raise ConfigError(
                f'scopes["{name}"] must be a scope made of a-z, 0-9 and "-", or null to remove it.'
            )

    extensions = _string_list(
        data.get("config_extensions", sorted(CONFIG_EXTENSIONS)), "config_extensions"
    )
    for ext in extensions:
        if not ext.startswith("."):
            raise ConfigError(f'config_extensions: "{ext}" must start with a dot.')

    max_description = data.get("max_description", MAX_DESCRIPTION)
    if isinstance(max_description, bool) or not isinstance(max_description, int) or max_description < 10:
        raise ConfigError('"max_description" must be an integer of at least 10.')

    return Config(
        rules=tuple(_parse_rule(rule, i) for i, rule in enumerate(rules)),
        scopes=scopes,
        config_extensions=frozenset(ext.lower() for ext in extensions),
        fix_keywords=_string_list(data.get("fix_keywords", list(FIX_KEYWORDS)), "fix_keywords"),
        max_description=max_description,
    )


def load_config(path: str) -> Config:
    """Read and validate a .commitrc file."""
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
    except OSError as error:
        raise ConfigError(f"cannot read {path}: {error.strerror}.") from error
    except json.JSONDecodeError as error:
        raise ConfigError(
            f"{path} is not valid JSON (line {error.lineno}, column {error.colno}: {error.msg})."
        ) from error
    try:
        return parse_config(data)
    except ConfigError as error:
        raise ConfigError(f"{path}: {error}") from error


def find_config() -> str | None:
    """Return the .commitrc of the repository root, else of the home directory, if any."""
    root = _git("rev-parse", "--show-toplevel").stdout.strip()
    candidates = [os.path.join(root, CONFIG_FILE)] if root else []
    candidates.append(os.path.join(os.path.expanduser("~"), CONFIG_FILE))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def parse_diff(diff: str, config: Config = DEFAULT_CONFIG) -> Signals:
    """Step 1 — extract raw signals from the diff text."""
    files: list[str] = []
    new_files: list[str] = []
    deleted_files: list[str] = []
    untracked: list[str] = []
    added_lines: list[str] = []
    removed_lines: list[str] = []
    changes_per_file: dict[str, int] = {}
    functions: list[str] = []
    current: str | None = None
    in_untracked = False
    # "---"/"+++" are file headers only before the first hunk of each file
    in_header = False

    def add_function(name: str) -> None:
        """Record a touched function name once."""
        if name and name not in functions:
            functions.append(name)

    for line in diff.splitlines():
        if in_untracked:
            if line.strip():
                untracked.append(line.strip())
            continue

        if line == UNTRACKED_HEADER:
            in_untracked = True
        elif line.startswith("diff --git "):
            match = re.match(r"diff --git a/(.+) b/(.+)$", line)
            current = match.group(2) if match else line.split()[-1]
            files.append(current)
            changes_per_file.setdefault(current, 0)
            in_header = True
        elif line.startswith("new file mode") and current:
            new_files.append(current)
        elif line.startswith("deleted file mode") and current:
            deleted_files.append(current)
        elif in_header and (line.startswith("+++") or line.startswith("---")):
            continue
        elif line.startswith("@@"):
            in_header = False
            hunk = HUNK_RE.match(line)
            if hunk:
                definition = DEFINITION_RE.search(hunk.group(1))
                if definition:
                    add_function(definition.group(1))
        elif line.startswith("+"):
            added_lines.append(line[1:])
            if current:
                changes_per_file[current] += 1
        elif line.startswith("-"):
            removed_lines.append(line[1:])
            if current:
                changes_per_file[current] += 1

    # Definitions written or removed directly take precedence over hunk context
    touched: list[str] = []
    for content in added_lines + removed_lines:
        definition = DEFINITION_RE.search(content)
        if definition and definition.group(1) not in touched:
            touched.append(definition.group(1))
    functions = touched + [f for f in functions if f not in touched]

    for path in untracked:
        if path not in files:
            files.append(path)
        changes_per_file.setdefault(path, 0)

    added_text = "\n".join(added_lines)
    keywords = {
        kw: len(re.findall(rf"\b{re.escape(kw)}", added_text, flags=0 if kw.isupper() else re.IGNORECASE))
        for kw in config.keywords
    }

    return {
        "files": files,
        "extensions": sorted({os.path.splitext(f)[1].lower() for f in files if os.path.splitext(f)[1]}),
        "directories": sorted({os.path.dirname(f) for f in files if os.path.dirname(f)}),
        "new_files": new_files + [f for f in untracked if f not in new_files],
        "deleted_files": deleted_files,
        "added": len(added_lines),
        "removed": len(removed_lines),
        "changes_per_file": changes_per_file,
        "functions": functions,
        "keywords": {kw: n for kw, n in keywords.items() if n},
    }


def _is_test_file(path: str) -> bool:
    """Tell whether a path is a test file (tests/ dir, test_ prefix or .test. name)."""
    name = os.path.basename(path)
    parts = path.split("/")[:-1]
    return (
        "tests" in parts
        or "test" in parts
        or name.startswith("test_")
        or ".test." in name
    )


def _is_config_file(path: str, extensions: frozenset[str]) -> bool:
    """Tell whether a path is a configuration file."""
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    is_env = name == ".env" or name.startswith(".env.")
    return ext in extensions or (is_env and ".env" in extensions)


def _matches(path: str, pattern: str) -> bool:
    """Match a glob against the full path, or the file name when it has no "/"."""
    if fnmatch.fnmatch(path, pattern):
        return True
    return "/" not in pattern and fnmatch.fnmatch(os.path.basename(path), pattern)


def _rule_applies(rule: Rule, signals: Signals) -> bool:
    """Tell whether every condition set on a custom rule holds."""
    files = signals["files"]
    if rule.files:
        hits = [any(_matches(f, p) for p in rule.files) for f in files]
        check = all if rule.match == "all" else any
        if not files or not check(hits):
            return False
    if rule.keywords and not any(signals["keywords"].get(kw) for kw in rule.keywords):
        return False
    return True


def classify_type(signals: Signals, config: Config = DEFAULT_CONFIG) -> str:
    """Step 2 — pick the Conventional Commits type with prioritized heuristics."""
    files = signals["files"]
    added, removed = signals["added"], signals["removed"]

    for rule in config.rules:
        if _rule_applies(rule, signals):
            return rule.type

    if files and all(_is_test_file(f) for f in files):
        return "test"
    if files and all(f.lower().endswith(".md") for f in files):
        return "docs"
    if files and all(_is_config_file(f, config.config_extensions) for f in files):
        return "chore"
    if signals["new_files"] and (removed < 10 or removed * 4 <= added):
        return "feat"
    if any(signals["keywords"].get(kw) for kw in config.fix_keywords):
        return "fix"
    if removed > 2 * added:
        return "refactor"
    return "chore"


def _slug(text: str) -> str:
    """Lowercase a name and keep only [a-z0-9-] so it is a valid scope."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _file_scope(path: str, scopes: dict[str, str]) -> str | None:
    """Map a single path to a known scope, or None if it matches none."""
    parts = path.split("/")
    for part in parts[:-1]:
        if part.lower() in scopes:
            return scopes[part.lower()]
    stem = os.path.splitext(parts[-1])[0].lower()
    if stem in scopes:
        return scopes[stem]
    if _is_test_file(path):
        return "tests"
    return None


def extract_scope(signals: Signals, config: Config = DEFAULT_CONFIG) -> str:
    """Step 3 — derive a scope from file paths, or "" when changes are unrelated."""
    files = signals["files"]
    if not files:
        return ""
    if len(files) == 1:
        path = files[0]
        stem = os.path.splitext(os.path.basename(path))[0]
        return _file_scope(path, config.scopes) or _slug(stem)

    scopes = {_file_scope(f, config.scopes) for f in files}
    if len(scopes) == 1 and None not in scopes:
        return scopes.pop()
    return ""


def _fit(description: str, fallback: str | None, limit: int) -> str:
    """Keep a description within `limit` characters, using the fallback or truncating."""
    if len(description) <= limit:
        return description
    if fallback and len(fallback) <= limit:
        return fallback
    return description[:limit].rstrip(" _-")


def build_description(
    signals: Signals, type_: str, scope: str | None = None, config: Config = DEFAULT_CONFIG
) -> str:
    """Step 4 — short imperative description, lowercase, no period, <= max_description chars."""
    limit = config.max_description
    if scope is None:
        scope = extract_scope(signals, config)
    new_files = signals["new_files"]
    functions = signals["functions"]
    added, removed = signals["added"], signals["removed"]
    changes = signals["changes_per_file"]
    most_changed = max(changes, key=changes.get) if changes else "files"
    most_changed = os.path.basename(most_changed)

    if new_files and type_ not in ("fix", "refactor"):
        if len(new_files) == 1:
            description = f"add {os.path.basename(new_files[0])}"
        elif scope:
            description = f"add {scope} module"
        else:
            description = f"add {len(new_files)} files"
    elif removed > 2 * added:
        element = functions[0] if functions else "code"
        target = scope or most_changed
        description = _fit(f"remove {target} {element}", f"remove {element}", limit)
    elif functions:
        redundant = scope.rstrip("s") == type_ or scope == functions[0].lower()
        target = f" in {scope}" if scope and not redundant else ""
        description = _fit(f"update {functions[0]}{target}", f"update {functions[0]}", limit)
    else:
        description = f"update {scope or most_changed}"

    description = description.rstrip(".")
    return _fit(description[:1].lower() + description[1:], None, limit)


def build_message(diff: str, config: Config = DEFAULT_CONFIG) -> str:
    """Step 5 — orchestrate the pipeline into `type(scope): description`."""
    signals = parse_diff(diff, config)
    type_ = classify_type(signals, config)
    scope = extract_scope(signals, config)
    description = build_description(signals, type_, scope, config)
    if scope:
        return f"{type_}({scope}): {description}"
    return f"{type_}: {description}"


def _prefilled_input(prompt: str, text: str) -> str:
    """Ask for input with `text` pre-filled when readline is available."""
    try:
        import readline
    except ImportError:
        return input(prompt)
    readline.set_startup_hook(lambda: readline.insert_text(text))
    try:
        return input(prompt)
    finally:
        readline.set_startup_hook()


def confirm(message: str) -> str:
    """Show the generated message and return the accepted or edited one; exit on cancel."""
    print(f"Generated message: {message}")
    try:
        while True:
            choice = input("[A]ccept / [E]dit / [C]ancel: ").strip().lower()
            if choice in ("a", "accept"):
                return message
            if choice in ("e", "edit"):
                while True:
                    edited = _prefilled_input("Commit message: ", message).strip()
                    if edited:
                        return edited
                    print("The commit message cannot be empty.")
            if choice in ("c", "cancel"):
                break
            print("Please answer A, E or C.")
    except (KeyboardInterrupt, EOFError):
        print()
    print("Cancelled, nothing was committed.")
    sys.exit(0)


def run_commit(message: str) -> None:
    """Stage every change, commit with `message` and push to the upstream branch."""
    result = _git("add", "-A")
    if result.returncode != 0:
        _fail("git add", result)

    result = _git("commit", "-m", message)
    if result.returncode != 0:
        _fail("git commit", result)
    print(result.stdout.strip())

    result = _git("push")
    if result.returncode != 0:
        if "no upstream branch" in result.stderr:
            branch = _git("branch", "--show-current").stdout.strip() or "<branch>"
            print("The commit was created locally but the branch has no upstream.")
            print(f"Push it with: git push -u origin {branch}")
            sys.exit(1)
        print("The commit was created locally but could not be pushed.")
        _fail("git push", result)
    print("Pushed.")


def main() -> None:
    """CLI entry point: diff, generate, confirm, then commit and push."""
    diff = get_diff()
    path = find_config()
    try:
        config = load_config(path) if path else DEFAULT_CONFIG
    except ConfigError as error:
        print(f"Error in {CONFIG_FILE}: {error}")
        sys.exit(1)
    message = confirm(build_message(diff, config))
    run_commit(message)


if __name__ == "__main__":
    main()
