# kommit
Kommit reads your diff. You get a clean commit message. No API key, no internet, no waiting.

It generates [Conventional Commits](https://www.conventionalcommits.org/) messages (`type(scope): description`) with a deterministic algorithm: no LLM, no external service. The same diff always gives the same message.

## Requirements

- Python 3.10+
- Git

No dependencies beyond the Python standard library.

## Usage

Run it from inside a git repository that has changes:

```bash
python3 /path/to/kommit/commit.py
```

```
Generated message: feat(auth): add login.py
[A]ccept / [E]dit / [C]ancel:
```

- **A** — accept the message, then run `git add -A`, `git commit -m "<message>"` and `git push`.
- **E** — edit the message (pre-filled), then commit and push the same way.
- **C** (or Ctrl+C / Ctrl+D) — exit without touching the repository.

Kommit stages **every** change in the repository, including untracked files, so check your `.gitignore` first.

If the branch has no upstream, the commit is still created locally and Kommit prints the `git push -u origin <branch>` command to run. Any other failing git step stops the run with git's error output.

## How the message is built

Kommit reads `git diff HEAD` plus the list of untracked files, then runs four steps.

### 1. Signals

From the diff it extracts the changed files, added and removed line counts, new/deleted/untracked files, function or class names touched (`def`, `class`, `function` in changed lines, then the enclosing function git shows in each `@@` hunk header) and keywords in added lines (`fix`, `bug`, `error`, `test`, `import`, `TODO`, `FIXME`).

### 2. Type

The first matching rule wins:

| # | Rule | Type |
|---|------|------|
| 1 | All files are tests (`tests/` or `test/` directory, `test_` prefix, `.test.` in the name) | `test` |
| 2 | All files are `.md` | `docs` |
| 3 | All files are config (`.json`, `.yml`, `.yaml`, `.toml`, `.env`) | `chore` |
| 4 | New files, with fewer than 10 removed lines or removed ≤ added / 4 | `feat` |
| 5 | `fix`, `bug` or `error` appears in added lines | `fix` |
| 6 | Removed lines > 2 × added lines | `refactor` |
| 7 | Anything else | `chore` |

### 3. Scope

Each file is mapped to a scope by its directories, or by its file name:

| Directory or file name | Scope |
|------------------------|-------|
| `auth` | `auth` |
| `api` | `api` |
| `ui`, `components` | `ui` |
| `db`, `models` | `db` |
| `tests`, `test` | `tests` |
| `docs` | `docs` |

- A single file with no match uses its own name: `src/utils.py` → `utils`.
- Several files keep a scope only if they all map to the same one. Otherwise the scope is omitted.

### 4. Description

The first matching case wins:

| Case | Description |
|------|-------------|
| One new file | `add <file name>` |
| Several new files | `add <scope> module`, or `add <n> files` without a scope |
| Removed lines > 2 × added lines | `remove <scope> <function>` (`code` if no function is found) |
| A function was touched | `update <function> in <scope>` |
| Otherwise | `update <scope>`, or `update <most changed file>` |

The description is at most 50 characters, starts lowercase and has no trailing period.

### Examples

| Change | Message |
|--------|---------|
| New `src/auth/login.py` | `feat(auth): add login.py` |
| Two untracked files in `src/api/` | `feat(api): add api module` |
| `# fix error on timeout` added inside `fetch()` in `src/api/client.py` | `fix(api): update fetch in api` |
| `class Legacy` removed from `src/db/models.py` | `refactor(db): remove db Legacy` |
| Edited `README.md` | `docs(readme): update readme` |
| Edited `package.json` and `config.yml` | `chore: update package.json` |

## Tests

```bash
python3 -m unittest -v
```
