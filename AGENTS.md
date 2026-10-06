# awm for agents

Read this if you are an agent (or a human) editing this package. Short on
purpose: the commands, the traps that cost a session, and where the rest lives.
Nothing here is read at runtime — it is for you.

## What this is

PyPI distribution **`awm`** (version in `pyproject.toml`), import package
`awm`, Python >= 3.10. A portable, scoped agent memory — entities, history and
integrations over a local store.

This repository is a **synced mirror** of the AitherOS monorepo (lane
`.github/workflows/sync-awm.yml`). Hand edits made here are overwritten on the
next sync — change the source and let the lane publish.

## Build, test, verify

```bash
python -m pytest tests -q        # the suite: 401 passed, 3 skipped at v0.6.1
pip install -e .                 # editable install for developing against it
```

The suite was run from a source checkout with no prior install. The publish
lane (`publish-brick.yml`) additionally builds the wheel, installs it and
imports it — a tree that tests green can still ship a broken wheel.

## Rules that keep this useful

- **The suite is the schema contract.** 401 tests over entities and history
  (`test_entities_v06.py`, `test_history.py`) — a stored-data shape change
  lands together with its tests, because memory that reads back differently
  than it was written is the failure nobody notices until it matters.
- **Memory is scoped; crossing scopes takes consent.** The scoping rules are
  the product, not a feature of it — a shortcut that reads across a boundary
  "just for lookup" is the defect this design exists to prevent.
- **The registry drives the public surface.** This repo's README header,
  `llms.txt` and `aither-manifest.json` are generated from the ecosystem
  registry (one yaml in the AitherOS monorepo) and rewritten on every sync.
  Change the registry; do not hand-edit the generated blocks.
- **The install line is a measured claim.** `check_ecosystem_install_lines`
  asserts the advertised `pip install` channel is real and ours. A rename or
  a move lands with the registry entry in the same change.

## Read next

- `llms.txt` — the install/use card written for an agent to execute
- `README.md` — the human front door
- `docs/` — the generated docs site source
