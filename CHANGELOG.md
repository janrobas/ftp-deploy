# Changelog

All notable changes to the ftp-deploy skill are documented here.

## [0.1.0]

First public release.

### Added
- Deterministic incremental FTP sync engine (`scripts/ftp_sync.py`,
  Python 3.9+, standard library only): `init`, `plan`, `deploy`, `push`,
  `verify`, `dirs`, `status`, with `--json` output and exit code 4 on a
  non-empty plan.
- State-based change detection (local SHA-256 + recorded remote sizes):
  changed files are re-uploaded, matching files are skipped, and a fresh
  checkout *adopts* same-size remote files instead of re-uploading them.
- Opt-in, scoped deletion: `--delete` removes only files the tool previously
  uploaded; foreign remote files are listed and kept; `--mirror` is bounded
  by the `protected` list.
- Atomic uploads via `.<name>.ftpdeploy-tmp` + rename.
- `.ftpignore` rule syntax (gitignore-style with `**`, negation, anchors),
  `protected` paths, `ensure_dirs`, optional post-deploy verification and
  optional `--build` step.
- Per-machine `hosts.json` profiles; passwords are never printed or uploaded.
- Offline unit test suite (`scripts/test_ftp_sync.py`) for the decision
  engine; validated end-to-end against `pyftpdlib`.
- Reference docs for Neoserv/cPanel hosting and the ignore syntax.
