# agent-usage-manager

## Design docs (HLD/LLD)

`docs/design/HLD.md` and `docs/design/LLD.md` are the architecture contract for this
repo. **Where a doc disagrees with the code, the code wins and the doc is stale** — that
is a bug to fix, not a tie to settle in the doc's favor.

A change to mapped code updates the owning doc **in the same PR**. Tiered, so routine
work does not churn the docs:

1. **Update the LLD** when a file, function signature, data model, schema, event shape,
   or config/env surface **the LLD names** changes — or when a module appears or
   disappears under an owned path.
2. **Update the HLD** only on a boundary change: a component added or retired, a new
   port/service/external dependency, a changed trust boundary or auth gate, a changed
   contract between components, or a subsystem promoted, demoted, or absorbed.
3. **Update neither** for bugfixes, styling, tests, or refactors that preserve every
   surface the docs name.

Do not regenerate a doc wholesale to satisfy this. The `design-doc` skill **overwrites**
and will destroy hand-written intent (why a gate exists, what the design refuses to do);
use it to create a missing pair, never to refresh a live one. Edit the affected sections
and move the `**Refreshed:**` line.

`CLAUDE.md` is a **symlink to `AGENTS.md`** — edit `AGENTS.md`, never the symlink.
Claude Code prefers `CLAUDE.md` and ignores a real `AGENTS.md` when both exist, so the
symlink keeps one source of truth and stops anything that later writes a `CLAUDE.md`
from silently shadowing this contract. Codex and Kimi read `AGENTS.md` directly. The
antigravity lane reads no project file at all — give it self-contained prompts.

Ownership map — machine-readable, parsed by `tests/test_design_docs.py`. Add a row when
you add a package; `none` means "no design contract, deliberately". A dir with no row but
rows beneath it is a container and is recursed into, so a package dropped inside one
cannot inherit its parent's coverage.

```design-doc-map
agent_usage_manager/   -> docs/design/HLD.md docs/design/LLD.md
docs/   -> none
tests/   -> none
```

`python3 tests/test_design_docs.py` reports drift: per doc, the modules added or removed
under its owned paths since that doc last changed (test files excluded — a new test needs
no LLD entry). Added/removed modules are the signal; raw commit counts are noise and print
as context only. The pytest asserts the map is structurally sound; it deliberately does
**not** fail on drift, because a doc gate that blocks merges buys rubber-stamp edits,
not maintained docs. A file *modified* to change its public API will not flag — boundary
changes still need a human read.


`python3 tests/test_design_docs.py --audit` answers a different question: which tracked modules
are named in **no** design doc at all. The drift report only diffs forward from a doc's
own last change, so staleness that predates an incomplete refresh is invisible to it
permanently — paws described a `scene.js` renderer for three weeks after it became
`wool.js`, and no number of drift runs could have said so. Run `--audit` when you inherit
a doc you did not write. It skips tests, `__init__.py`, vendored code, type shims,
generated migration revisions, and anything under a `-> none` path.