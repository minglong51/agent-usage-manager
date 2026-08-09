"""Design-doc upkeep — the contract file owns the path -> design-doc ownership map.

The map lives in a fenced ```design-doc-map block in AGENTS.md (or CLAUDE.md when a
repo has only that one) so agents read the same SSOT this tooling parses; there is no
second copy to drift.

pytest asserts the map is STRUCTURAL (every doc it names exists, every package is
mapped or explicitly `none`, and the two contract files stay identical when a repo
keeps both). Drift is REPORTED, never asserted:

    python3 <path/to>/test_design_docs.py

prints, per doc, the modules added or removed under its owned paths since that doc
last changed. A merge gate on doc freshness buys rubber-stamp edits, not maintained
docs, so this stays a report. Added/removed modules are the signal; commit counts are
context only.

This file is shared verbatim across repos — it locates its own repo root, so it works
at any depth. Do not fork it per repo.
"""

import re
import subprocess
from pathlib import Path

import pytest

CONTRACT_NAMES = ("AGENTS.md", "CLAUDE.md")
FENCE = re.compile(r"^```design-doc-map\s*$(.*?)^```\s*$", re.M | re.S)
SKIP_DIRS = {"__pycache__", "node_modules", "venv", ".venv"}
IS_TEST = re.compile(
    r"(^|/)(test[_\-.][^/]+|[^/]+[_\-.](test|spec))\.(py|sh|ts|js|mjs|cjs|tsx|jsx)$"
)


def repo_root() -> Path:
    """Nearest ancestor holding docs/design/ and at least one contract file."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs" / "design").is_dir() and any(
            (parent / name).is_file() for name in CONTRACT_NAMES
        ):
            return parent
    raise FileNotFoundError(
        "no ancestor with docs/design/ plus AGENTS.md or CLAUDE.md — cannot locate the repo root"
    )


REPO = repo_root()


def contract_files() -> list[Path]:
    return [REPO / name for name in CONTRACT_NAMES if (REPO / name).is_file()]


def contract_path() -> Path:
    """The contract file carrying the map. Prefers AGENTS.md when both have it."""
    candidates = [p for p in contract_files() if FENCE.search(p.read_text(encoding="utf-8"))]
    if not candidates:
        raise ValueError(
            f"no ```design-doc-map fence in {[p.name for p in contract_files()]} at {REPO}"
        )
    return candidates[0]


def parse_map(text: str) -> dict[str, list[str]]:
    """path prefix -> owning design docs ([] when deliberately unowned)."""
    block = FENCE.search(text)
    if not block:
        raise ValueError("no ```design-doc-map fence in the contract file")
    owners: dict[str, list[str]] = {}
    for line in block.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "->" not in line:
            raise ValueError(f"design-doc-map row missing '->': {line!r}")
        path, docs = (part.strip() for part in line.split("->", 1))
        if not path.endswith("/"):
            raise ValueError(f"design-doc-map path must end in '/': {line!r}")
        owners[path] = [] if docs == "none" else docs.split()
    return owners


def tracked_dirs() -> set[str]:
    """Every directory prefix containing at least one tracked file.

    A package is what the repo actually versions. Local scratch (`db/`, `reports/`),
    generated output (`public/`, `src/assets/`) and build artifacts (`*.egg-info/`)
    all have zero tracked files, and none of them exist in a fresh clone — so a
    checker that walks the filesystem sees a different repo depending on whose
    machine it runs on. Asking git what is tracked removes that variance entirely,
    and subsumes .gitignore (an ignored dir tracks nothing).
    """
    prefixes: set[str] = set()
    for path in git("ls-files").splitlines():
        parts = path.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            prefixes.add("/".join(parts[:i]) + "/")
    return prefixes


def subdirs(rel: str) -> set[str]:
    tracked = tracked_dirs()
    return {
        f"{rel}{p.name}/"
        for p in (REPO / rel).iterdir()
        if p.is_dir()
        and not p.name.startswith(".")
        and p.name not in SKIP_DIRS
        and f"{rel}{p.name}/" in tracked
    }


def packages(rows: set[str]) -> set[str]:
    """Directories that need their own row.

    A dir with an exact row is covered. A dir with no row but rows beneath it is a
    CONTAINER (harnesses/, packages/, src/) — recurse, so a new subsystem dropped
    inside one cannot inherit its parent's coverage and slip past the map.
    """
    found: set[str] = set()
    pending = subdirs("")
    while pending:
        rel = pending.pop()
        if rel in rows:
            found.add(rel)
        elif any(row.startswith(rel) for row in rows):
            pending |= subdirs(rel)
        else:
            found.add(rel)
    return found


def git(*args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(REPO), *args), capture_output=True, text=True, check=True
    ).stdout.strip()


def owned_paths(owners: dict[str, list[str]]) -> dict[str, list[str]]:
    """Invert the map: design doc -> the paths it is the contract for."""
    inverted: dict[str, list[str]] = {}
    for path, docs in owners.items():
        for doc in docs:
            inverted.setdefault(doc, []).append(path)
    return inverted


def drift(doc: str, paths: list[str]) -> dict[str, object]:
    """Modules added/removed under `paths` since `doc` last changed."""
    sha = git("log", "-1", "--format=%H", "--", doc)
    if not sha:
        return {"doc": doc, "untracked": True, "added": [], "removed": [], "commits": 0}

    def modules(filt: str) -> list[str]:
        out = git("diff", f"--diff-filter={filt}", "--name-only", f"{sha}..HEAD", "--", *paths)
        return [p for p in out.splitlines() if p and not IS_TEST.search(p)]

    return {
        "doc": doc,
        "untracked": False,
        "date": git("log", "-1", "--format=%ad", "--date=short", "--", doc),
        "added": modules("A"),
        "removed": modules("D"),
        "commits": int(git("rev-list", "--count", f"{sha}..HEAD", "--", *paths) or 0),
    }


@pytest.fixture(scope="module")
def owners() -> dict[str, list[str]]:
    return parse_map(contract_path().read_text(encoding="utf-8"))


def test_map_is_parseable(owners):
    assert owners, "design-doc-map is empty"


def test_contract_files_agree():
    """A repo keeping both AGENTS.md and CLAUDE.md must keep them identical.

    Measured 2026-08-09: Claude Code prefers CLAUDE.md, Hermes prefers AGENTS.md, and
    Codex and Kimi read AGENTS.md. Two divergent real files therefore feed DIFFERENT
    contracts to different lanes. Normally CLAUDE.md is a symlink to AGENTS.md and this
    is trivially true; the assertion catches the case where something replaced it.
    """
    files = contract_files()
    if len(files) < 2:
        pytest.skip("repo keeps a single contract file")
    first, second = (p.read_text(encoding="utf-8") for p in files)
    assert first == second, (
        f"{files[0].name} and {files[1].name} have diverged — they must stay byte-identical "
        "so Claude and non-Claude lanes read the same contract."
    )


def test_every_named_doc_exists(owners):
    missing = sorted(
        {doc for docs in owners.values() for doc in docs if not (REPO / doc).is_file()}
    )
    assert not missing, f"design-doc-map names docs that do not exist: {missing}"


def test_every_package_is_mapped(owners):
    unmapped = sorted(packages(set(owners)) - set(owners))
    assert not unmapped, (
        f"packages missing from the design-doc-map in {contract_path().name}: {unmapped}. "
        "Add a row pointing at their HLD/LLD, or '-> none' if they carry no contract."
    )


def test_map_has_no_stale_rows(owners):
    stale = sorted(row for row in owners if not (REPO / row).is_dir())
    assert not stale, f"design-doc-map rows for paths that no longer exist: {stale}"


def main() -> None:
    owners = parse_map(contract_path().read_text(encoding="utf-8"))
    reports = [drift(doc, paths) for doc, paths in sorted(owned_paths(owners).items())]
    flagged = [r for r in reports if r["added"] or r["removed"]]

    for r in reports:
        mark = "DRIFT" if (r["added"] or r["removed"]) else "ok   "
        print(
            f"{mark} {r['doc']}  (last changed {r.get('date', '?')}, "
            f"{r['commits']} commits to owned paths since)"
        )
        for path in r["added"]:
            print(f"        + {path}  — added; does the LLD name it?")
        for path in r["removed"]:
            print(f"        - {path}  — removed; is it still in the doc?")

    print()
    if flagged:
        print(
            f"{len(flagged)} design doc(s) may be stale. Edit the affected sections and "
            "move the **Refreshed:** line — do NOT regenerate wholesale."
        )
    else:
        print("No module-level drift. Boundary changes still need a human read.")


if __name__ == "__main__":
    main()
