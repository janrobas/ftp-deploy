---
name: FTP Deploy
description: Deploy the current project to a shared PHP host (Neoserv/cPanel or any FTP server) with an incremental FTP sync that uploads only changed files and deletes only files it previously uploaded. Use when the user asks to deploy, publish, ship, or sync a project to their hosting over FTP.
---

# FTP Deploy

Deterministic incremental deployment over FTP to shared PHP hosting
(Neoserv/cPanel by default, but any FTP server with MLSD support).

The decision engine is a Python script. The agent's job is to configure,
run it, read its plan, and confirm with the user — **never** to decide file
by file what to upload or delete.

## Prerequisites

- Python 3.9+ with only the standard library (`ftplib`). No pip installs.
- An FTP account on the host. On Neoserv the main account is the cPanel
  login (host = your domain or server name, port 21, plain FTP is what
  Neoserv recommends; explicit FTPS usually also works — see
  `references/neoserv.md`).
- The script runs on the machine where the project files are (the machine
  the agent/t3code runs on), not on the server.

## Where the script lives

The skill base directory (`$SKILL_DIR`) is provided to you when this skill
loads. The script is at:

```
$SKILL_DIR/scripts/ftp_sync.py
```

Run it with the same Python as any other project command, e.g.
`python "$SKILL_DIR/scripts/ftp_sync.py" plan`. On Windows, forward
slashes in the path are fine.

## Configuration

Two layers, merged with project values winning:

1. **Per-project** `.ftpdeploy.json` in the project root — created by
   `init`. Contains no secrets; safe to review. This is what makes the
   project deployable from any machine.
2. **Global profiles** `~/.config/opencode/ftp-deploy/hosts.json` (Windows:
   `C:\Users\<you>\.config\opencode\ftp-deploy\hosts.json`) — named entries
   holding host, user, and the password (or `password_env`). The project
   config just references `"profile": "neoserv"`. This file holds the
   secret, so it must never be committed or copied into a project.

Password resolution order: profile `password` → profile/env `password_env`
→ `FTP_PASSWORD` env var → interactive prompt (tty only).

Rules files:
- `.ftpignore` — which local paths are never uploaded (syntax in
  `references/ftpignore.md`). Never deploy `.git`, `.env`,
  `node_modules`, or the deploy tool's own state.
- `protected` in `.ftpdeploy.json` — remote paths that are **never
  deleted**, not even with `--mirror` (e.g. `".env"`, `"data/**"`).
- `ensure_dirs` — remote directories to create even though nothing uploads
  into them (e.g. `data/users` on MemoryDown).

## Workflow

1. **Check state.** If `.ftpdeploy.json` is missing, run `init` (passing
   `--profile neoserv` and `--remote-dir` if you know them) and ask the
   user for the host profile values they want in `hosts.json`. Never
   invent credentials; ask.
2. **Build** when dependencies changed (e.g. `composer.lock` or
   `package-lock.json` differ from the last deploy): run
   `deploy --build`, or run the project's own build command first
   (`composer install --no-dev --optimize-autoloader`). `deploy --dry-run`
   never builds.
3. **Plan.** Run `plan` (add `--json` if you want machine-readable output).
   Always show the user the summary and the lists before anything is
   written to the server. Exit code 4 means "there is something to do".
4. **Deploy.** Run `deploy --yes` for uploads only. Run
   `deploy --delete --yes` only after the user approved removing the
   files the plan lists as deletable. `--delete` only ever removes files
   this tool previously uploaded (tracked in `.ftp-deploy/state-*.json`)
   that are gone locally — foreign remote files are always kept, listed
   as `kept`. `--mirror` additionally removes untracked remote files,
   still bounded by `protected`.
5. **Verify.** If `verify_url` is configured, `deploy` verifies
   automatically at the end; otherwise run `verify` (e.g. against
   `https://site/health`).

## Hard rules

- Never print, log, or echo the FTP password, access tokens, or `.env`
  contents. The script redacts them; do not defeat that.
- Never upload over an existing remote `.env` unless the user explicitly
  says so; use `push .env .env` for that one-off and confirm first.
- Never run `deploy` without `--yes` in a non-interactive session unless
  you are sure the user already approved this exact plan.
- Never delete anything the plan did not list.
- Never commit `.ftpdeploy.json`, `.ftp-deploy/`, or `hosts.json`. If the
  repo's `.gitignore` lacks them, tell the user and suggest the lines
  (`init` also warns).
- On the first deploy to a non-empty remote directory, read
  `references/neoserv.md` about `cgi-bin`/`index.html` leftovers and tell
  the user they will be kept (foreign) unless `--mirror` or manual removal.
- If a deploy fails, don't retry blindly more than once without looking at
  the error. Common causes are in `references/neoserv.md` (passive/active,
  connection limits, FTPS certificate).

## Commands (summary)

```
ftp_sync.py init   [--profile NAME] [--remote-dir DIR]
ftp_sync.py plan   [--json] [--checksum]
ftp_sync.py deploy [--yes] [--delete] [--mirror] [--checksum] [--build] [--dry-run] [--json]
ftp_sync.py push <local-file> <remote-path>
ftp_sync.py verify
ftp_sync.py dirs
ftp_sync.py status                       # offline, local-only check
```

Global flags on every command: `--config PATH` (default `./.ftpdeploy.json`),
`--config-dir DIR` (default the global FTP deploy config dir), `--profile`,
`--host`, `--port`, `--user`, `--password-env`, `--tls/--no-tls`, `--active`,
`--timeout`, `--verbose`, `--json`, `--yes`, `--allow-root`.

Run from the project root; `local_dir` is resolved relative to the config
file's directory, so the exact cwd rarely matters.

## Mobile / t3code note

Everything is non-interactive when config + `--yes` are present: one
command deploys. Keep `hosts.json` on each device you deploy from (it is
just one file to copy), or rely on `password_env` there. The script keeps
its state in the project's `.ftp-deploy/` directory; on a fresh clone with
no state it "adopts" remote files whose size matches local ones instead of
re-uploading everything, so a new device does not re-push `vendor/`.
