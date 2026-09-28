# Changelog

Release dates for 0.4.0 and 0.5.0 were not recorded in this tree, so they are left
out rather than guessed.

## 0.6.0 (unreleased, 2026-09-27)

### Fixed (sixth review, 2026-09-28)

- A handle opened on an older file re-reads the schema version inside every write
  transaction. If another process ran `awm migrate` while the handle was open, the handle
  leaves compat mode. Before this, it kept overwriting in place in the v3 file, and the
  superseded value never reached history.
- `migrate()` removes the leftovers of a hard-killed earlier migration, under its write
  lock: every `.partial`, and any full backup that is byte-identical to the new one.
  The report lists them as `reaped_backups`.
- `purge_history` also deletes:
  - transitions whose before- or after-state time falls inside a purged interval, since
    their digests commit to the purged value;
  - the applied `entity_pending` rows that carried a purged value.
- An entity merge refuses only when the dropped entity absorbed a merge at the SAME
  scope. A descendant's merge into it is re-pointed at the survivor, and split points it
  back.
- `pending.slug` keeps letters and digits from every script. Before this, every CJK,
  Cyrillic or Arabic name became `people.unknown`. ASCII names slug exactly as before.
- `reconcile_and_remember` refuses an update to a slot that was reconciled under ANOTHER
  subject unless `overwrite=True`.
- `predict_outcome`'s GENERALIZED marginal reads only the rows of the state's prefix. An
  empty-delta row counts only when its state is one this prefix's own rows reach.
- `--as-of` / MCP `as_of` is parsed by one grammar that gives the same result on 3.10 and
  3.12. It refuses NaN, inf, negative and out-of-range values, and it refuses an 8-digit
  compact date because that would be ambiguous.
- `awm doctor` prints the version and path of the code it runs, and names a mismatch with
  the installed distribution.

### Fixed (third review)

- A v1 file with an empty `schema_meta` (0.3.x crashed between creating its tables and
  stamping the version) is opened as v1 in compat mode, not created fresh and stamped v3.
- `migrate()` rolls back when its COMMIT is refused ("database is locked"), raises
  `MigrationError`, removes the now-pointless backup, and leaves the store usable.
- `purge_history` also deletes the transitions at that scope whose delta or prediction
  names the key once the key is no longer live. Before this, `predict_outcome` kept
  serving a purged value.
- `reconcile_and_remember` only supersedes a value it owns: one written by a reconcile,
  of the same `kind`. Any other value needs `overwrite=True`. New `kind=` and `meta=`
  parameters. MCP `awm_remember(subject=...)` applies the same `overwrite` rule as `key=`.
- `predict_outcome`: RECALLED returns the majority outcome (ties go to the most recent).
  GENERALIZED confidence is shrunk by n/(n+1), as adk does.
- `merge_entities` retargets aliases that descendant scopes confirmed for the dropped
  entity, and `split_entity` restores them. Before this, a descendant could block the
  merge, and the refusal disclosed that the descendant existed.
- New `MemoryStore(check_same_thread=False)` and `clear_transitions(scope)` (used by
  `adk wm reset`).

### Changed: migration is opt-in (fixes a lock-out introduced in 0.4.0)

- 0.4.0 and 0.5.0 migrated an older memory file **the moment they opened it**. The
  schema bump locked every installed older reader out of the file: awm 0.3.x refuses
  anything that is not schema v1, so one read from a newer install broke every other
  tool sharing `~/.aither/awm/memory.db`.
- An older file now opens in **compat mode** and nothing is written on open.
  `remember`/`recall`/`forget`/`count` do what the file's own version does (v1: an
  in-place upsert with no history), so 0.3.x can still read it. Features the file
  cannot hold raise `NeedsMigration`, which names `awm migrate`. A v2 file keeps its v2
  features (history, `as_of`, reconcile, entities).
- New: `MemoryStore.migrate(backup=True, dry_run=False)` and
  `awm migrate [--db P] [--no-backup] [--dry-run] [--json]`. Takes the write lock, writes
  a byte backup next to the file and compares its sha256 with the original, migrates in
  one transaction, and compares every table's row count plus a digest of every memory
  before and after. Any difference raises `MigrationError` and rolls back.
- New: `AWM_AUTO_MIGRATE=1` restores migrate-on-open, still with the backup.
- New: `awm doctor [--db P]` reports the file's schema against the code's and whether
  compat mode is active. It reads with `mode=ro` and never creates the file
  (`awm.probe_schema`).
- New files are still created at schema v3.
- Measured on a copy of a real 2612-row v1 store: opening it with 0.6.0 left it
  byte-identical and awm 0.3.1 still read it; `awm migrate` kept 2612 rows, all 9 v1
  columns identical row for row, and the backup was byte-identical to the original.

### Added: entities

- Near-miss spellings are POSSIBLE links, never confirmed ones: edit distance 1 when
  both names have at least 5 characters, 2 at 8 or more. A neighbour swap counts as one
  edit ("Vanhs" -> "vansh"). Names whose digits differ are never typos. Canonical names
  are matched as well as confirmed aliases.
- A mention written as initials ("VS", "V.S.") ranks the entities whose multi-word name
  it is exactly the initials of first.
- `merge_entities(scope, keep, drop)` / `split_entity(scope, merged_id)`: a reversible
  merge at exactly one scope. The split restores both prior alias sets exactly. Refused
  for sibling ids, a `drop` owned or named at another scope, a confirmed-vs-rejected
  contradiction, and splits made out of order. CLI `awm entity merge|split`, MCP
  `awm_merge_entities` / `awm_split_entity`.
- Schema: a new `entity_merges` table. It is created with new files and by `migrate`,
  and added to an existing v3 file on its first merge. The version stays 3 because 0.5.0
  readers ignore the extra table.

### Added: surprise statistics

- `surprise_stats(scope, since, until=None, prefix=None, buckets=5)` returns count, mean,
  p50/p90 (linear interpolation), max, novel and unexplained counts, and a calibration
  table: per stated-confidence bucket, mean confidence against the observed exact-match
  rate, plus `ece`. It is computed from the transitions as they were recorded. CLI
  `awm world stats`, MCP `awm_surprise_stats`.

### Tests

- Five existing tests assumed migrate-on-open. They now open with `auto_migrate=True`,
  the opt-in path, and still check the same row-preservation and rollback properties.
- The exact MCP tool-set assertion gained the three new tools.

## 0.5.0 (schema v3)

- The world model (`awm.world`): `encode_state` (a scope's visible facts as a
  `WorldState` with a stable digest, also `as_of`), `observe_transition`,
  `predict_outcome` (RECALLED, then an injected predictor (PREDICTED), then GENERALIZED,
  then NONE; these are never mixed), `transitions`, `unexplained_changes`, `surprise_log`.
  CLI `awm world state|predict|surprises`, MCP `awm_world_state`, `awm_predict`,
  `awm_observe`.
- A new `transitions` table (schema v3). **A v2 file was migrated to v3 when first
  opened** (reverted to opt-in in 0.6.0).

## 0.4.0 (schema v2)

- History: a changed value moves the old one to `memory_history` with the interval it
  was true for. `forget` records the deletion there. `history()`, `recall(as_of=...)`,
  and `purge_history()` at exactly one scope.
- `reconcile_and_remember` with `SlotReconciler` / `LLMReconciler`. The host injects the
  model; every decision is validated before it is applied.
- Entity resolution: `resolve_entity`, `confirm_alias`, `reject_alias`, `entities`. A
  known name plus a qualifier is confirmed. Initials and abbreviations are only possible
  links, and nothing is merged on a guess.
- New tables `memory_history`, `entities`, `entity_aliases`, and a nullable `since`
  column on `memories` (schema v2). **A v1 file was migrated to v2 when first opened**.
  That locked awm 0.3.x out of the file, and 0.6.0 reverted it to opt-in.
