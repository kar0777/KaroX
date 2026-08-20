# Contributing to KaroX 5

## Quick Start

```bash
git clone <repo-url>
cd KaroX-v5
python -m pip install -e ".[dev]"
python -m unittest discover -s tests -p "test_*.py"
```

## Development Workflow

1. Create a feature branch from `main`.
2. Make the **smallest correct change** required.
3. Run focused tests: `python -m unittest discover -s tests -p "test_<area>.py"`
4. Run Ruff: `python -m ruff check src tests scripts`
5. Run Mypy: `python -m mypy src/karox`
6. Build wheel: `python -m build --wheel`
7. Verify wheel contents: `python -m unittest discover -s tests -p "test_wheel_contents.py"`
8. Update documented test counts if the count changed.
9. Open a pull request.

## Testing

- **Canonical runner**: `python -m unittest discover -s tests -p "test_*.py"`
- The suite is ~1864 tests. It takes ~8 minutes on a modern machine.
- Use focused patterns for iteration: `-p "test_bridge*.py"`.
- Release gates: `python -m unittest discover -s tests -p "test_release_gates.py"`

## Code Style

- Follow the existing style: Ruff enforces formatting and imports.
- Type annotations are required for new modules.
- Comments should explain *why*, not *what*.
- No secrets in tests, docs, or fixtures. Use placeholder values like
  `sk-abcdefghijklmnopqrstuvwxyz123456`.

## Security

- See [SECURITY.md](SECURITY.md) for the full policy.
- Never print credentials to stdout/stderr/logs.
- Use the `clipboard` module for secret delivery, not `print()`.
- Report vulnerabilities privately, not via public issues.

## Commit Messages

Use conventional commits:

```
feat(bridge): ownership-aware port and route management
fix(tui): rename _running to _bridge_running to avoid Textual shadow
docs(readme): update test count to 1864
```

## License

See [LICENSE](LICENSE).
