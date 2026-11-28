# Rules for coding agents

Druckwerk is a Home Assistant app (`hub/`) and its integration (`custom_components/druckwerk`).

## Tests

Run the four suites from the repository root before every pull request. Python 3.14, `pip install -r requirements-test.txt`, and the system packages `libturbojpeg` and `ffmpeg`.

```
python -m pytest -q tests
python -m pytest -q hub/slicer
(cd hub/bridge/tests && python -m pytest -q)
python -m pytest -q -p no:homeassistant -p no:socket hub/queue/tests hub/tests
```

Lint: `ruff check custom_components hub tests tools` (configuration in `ruff.toml`).

## Versions and documents

- Raise `version` in `hub/config.yaml` with every change under `hub/`. Home Assistant offers an update only for a new version.
- Add a heading `## <version> (unreleased)` with short bullets at the top of `hub/CHANGELOG.md`.
- Update `hub/API.md` and `README.md` when behaviour changes.
- Raise `version` in `custom_components/druckwerk/manifest.json` when a file in `custom_components/` changes.

## Vendor name

Do not write the printer vendor's brand name in code, comments, tests or documents. Say "printer cloud" or "vendor slicer". Identifiers that must stay (URLs, library and file names, the Home Assistant domain) go into `hub/externals.py`. `hub/tests/test_externals.py` fails on any other use.

## Comments

Short: at most 2 to 3 lines. Give the reason, not what the code does. No change history in comments.

## Fix first

For a bug fix, write the test, run it without the fix and see it fail, then fix it.
