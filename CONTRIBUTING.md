# Contributing

Focused bug reports and small fixes are welcome. AUM is a personal open-source
project, so review and support are best-effort.

## Report a bug

Use the
[bug report form](https://github.com/minglong51/agent-usage-manager/issues/new?template=bug_report.yml).

Before submitting:

1. Reproduce on the latest PyPI release or current `main`.
2. Record your OS, Python version, AUM version, and installation method.
3. Remove tokens, hostnames, usernames, home paths, internal labels, and full
   command lines from logs, config snippets, and screenshots.
4. Report security-sensitive behavior privately through
   [SECURITY.md](SECURITY.md).

A process false positive or false negative is useful evidence. Include a minimal
sanitized matcher and command shape rather than your complete live config.

## Development setup

```bash
git clone https://github.com/minglong51/agent-usage-manager
cd agent-usage-manager
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

CI runs on macOS and Linux with Python 3.9 and 3.12.

## Pull requests

Keep each change focused. Explain the observed behavior, the intended behavior,
and how you verified it.

Changes to a file, function signature, config surface, API shape, or module
named in `docs/design/LLD.md` must update the affected LLD section. Update
`docs/design/HLD.md` only when a component, trust boundary, dependency, port, or
component contract changes.

Before opening a PR:

```bash
pytest -q
python tests/test_design_docs.py
python tests/test_design_docs.py --audit
```

Do not include live configuration, tokens, process dumps, personal paths, or
unsanitized captures in a commit or PR.
