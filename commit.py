from __future__ import annotations

import os
import re
import subprocess
import sys
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

    status = _git("status", "--short", "--untracked-files=all")
    if status.returncode != 0:
        _fail("git status", status)
    untracked = [
        line[3:].strip()
        for line in status.stdout.splitlines()
        if line.startswith("??")
    ]

    diff = result.stdout
    if untracked:
        diff += f"\n{UNTRACKED_HEADER}\n" + "\n".join(untracked) + "\n"

    if not diff.strip():
        print("No changes detected.")
        sys.exit(0)

    return diff


def parse_diff(diff: str) -> Signals:
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
        kw: len(re.findall(rf"\b{kw}", added_text, flags=0 if kw.isupper() else re.IGNORECASE))
        for kw in KEYWORDS
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


def _is_config_file(path: str) -> bool:
    """Tell whether a path is a configuration file."""
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    return ext in CONFIG_EXTENSIONS or name == ".env" or name.startswith(".env.")


def classify_type(signals: Signals) -> str:
    """Step 2 — pick the Conventional Commits type with prioritized heuristics."""
    files = signals["files"]
    added, removed = signals["added"], signals["removed"]

    if files and all(_is_test_file(f) for f in files):
        return "test"
    if files and all(f.lower().endswith(".md") for f in files):
        return "docs"
    if files and all(_is_config_file(f) for f in files):
        return "chore"
    if signals["new_files"] and (removed < 10 or removed * 4 <= added):
        return "feat"
    if any(signals["keywords"].get(kw) for kw in FIX_KEYWORDS):
        return "fix"
    if removed > 2 * added:
        return "refactor"
    return "chore"


def _slug(text: str) -> str:
    """Lowercase a name and keep only [a-z0-9-] so it is a valid scope."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _file_scope(path: str) -> str | None:
    """Map a single path to a known scope, or None if it matches none."""
    parts = path.split("/")
    for part in parts[:-1]:
        if part.lower() in SCOPE_MAP:
            return SCOPE_MAP[part.lower()]
    stem = os.path.splitext(parts[-1])[0].lower()
    if stem in SCOPE_MAP:
        return SCOPE_MAP[stem]
    if _is_test_file(path):
        return "tests"
    return None


def extract_scope(signals: Signals) -> str:
    """Step 3 — derive a scope from file paths, or "" when changes are unrelated."""
    files = signals["files"]
    if not files:
        return ""
    if len(files) == 1:
        path = files[0]
        stem = os.path.splitext(os.path.basename(path))[0]
        return _file_scope(path) or _slug(stem)

    scopes = {_file_scope(f) for f in files}
    if len(scopes) == 1 and None not in scopes:
        return scopes.pop()
    return ""


def _fit(description: str, fallback: str | None) -> str:
    """Keep a description within MAX_DESCRIPTION, using the fallback or truncating."""
    if len(description) <= MAX_DESCRIPTION:
        return description
    if fallback and len(fallback) <= MAX_DESCRIPTION:
        return fallback
    return description[:MAX_DESCRIPTION].rstrip(" _-")


def build_description(signals: Signals, type_: str, scope: str | None = None) -> str:
    """Step 4 — short imperative description, lowercase, no period, <= 50 chars."""
    if scope is None:
        scope = extract_scope(signals)
    new_files = signals["new_files"]
    functions = signals["functions"]
    added, removed = signals["added"], signals["removed"]
    changes = signals["changes_per_file"]
    most_changed = max(changes, key=changes.get) if changes else "files"
    most_changed = os.path.basename(most_changed)

    if new_files and type_ in ("feat", "test", "docs", "chore"):
        if len(new_files) == 1:
            description = f"add {os.path.basename(new_files[0])}"
        elif scope:
            description = f"add {scope} module"
        else:
            description = f"add {len(new_files)} files"
    elif removed > 2 * added:
        element = functions[0] if functions else "code"
        target = scope or most_changed
        description = _fit(f"remove {target} {element}", f"remove {element}")
    elif functions:
        redundant = scope.rstrip("s") == type_ or scope == functions[0].lower()
        target = f" in {scope}" if scope and not redundant else ""
        description = _fit(f"update {functions[0]}{target}", f"update {functions[0]}")
    else:
        description = f"update {scope or most_changed}"

    description = description.rstrip(".")
    return _fit(description[:1].lower() + description[1:], None)


def build_message(diff: str) -> str:
    """Step 5 — orchestrate the pipeline into `type(scope): description`."""
    signals = parse_diff(diff)
    type_ = classify_type(signals)
    scope = extract_scope(signals)
    description = build_description(signals, type_, scope)
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
    message = confirm(build_message(diff))
    run_commit(message)


if __name__ == "__main__":
    main()
