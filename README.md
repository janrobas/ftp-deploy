# ftp-deploy

An [OpenCode](https://opencode.ai) **skill** for deterministic, incremental
deployment to shared PHP hosting over FTP — Neoserv/cPanel by default, but any
FTP server with MLSD support works.

The decision engine is a dependency-free Python script: the AI agent configures
it, runs it and reads its plan — it never decides file-by-file what to upload
or delete.

```
python scripts/ftp_sync.py plan          # show what a deploy would do (read-only)
python scripts/ftp_sync.py deploy --yes  # upload only what changed
```

- **Incremental** — only changed files are uploaded. Change detection uses a
  local state file (SHA-256 + sizes), so it works without checksums on the
  server and without downloading anything.
- **Fresh-machine friendly** — on a new checkout with no state file, files
  whose size matches the server are *adopted* instead of re-uploaded. Cloning
  a project on your phone does not re-push `vendor/`.
- **Deletes are opt-in and scoped** — `--delete` removes only files this tool
  previously uploaded, which are now gone locally. Foreign files on the server
  are listed and kept; `--mirror` is the explicit escape hatch, still bounded
  by a `protected` list.
- **Atomic uploads** — files go up as `.<name>.ftpdeploy-tmp` and are renamed
  into place, so a dropped transfer never replaces a live PHP file.
- **Secrets stay out** — the password lives in a per-machine profile file
  outside any repo, is never printed, and never reaches the server.

## Install

Skills live in `~/.config/opencode/skills/<id>/` (Windows:
`C:\Users\<you>\.config\opencode\skills\ftp-deploy\`). Any of these works:

```bash
# 1. clone directly into the global skills directory
git clone https://github.com/<you>/ftp-deploy.git ~/.config/opencode/skills/ftp-deploy

# 2. or clone anywhere and point OpenCode at it (opencode.jsonc, any scope)
#    "skills": ["C:/code/ftp-deploy"]
```

OpenCode discovers `SKILL.md` automatically; the skill appears as
**FTP Deploy** when the model needs it, and can also be loaded explicitly
(`@ftp-deploy`). Nothing else to install — Python 3.9+ standard library only.

## Quick start (first project)

```bash
cd my-project
python <skill-dir>/scripts/ftp_sync.py init --profile neoserv --remote-dir /public_html/myapp
```

`init` writes `.ftpdeploy.json` and `.ftpignore` in the project, and a
`hosts.json` skeleton under `~/.config/opencode/ftp-deploy/`. Fill in host,
user and password there (or set the `password_env` / `FTP_PASSWORD`
environment variable):

```json
{
  "neoserv": {
    "host": "ftp.example.si",
    "port": 21,
    "user": "cpanel-user",
    "password": "your-password",
    "tls": false
  }
}
```

Then:

```bash
python <skill-dir>/scripts/ftp_sync.py plan            # review (exit 4 = something to do)
python <skill-dir>/scripts/ftp_sync.py deploy --yes    # apply
```

Commit `.ftpdeploy.json` to the repo (it holds no secrets — only the profile
name, remote directory and verify URL). Keep `.ftp-deploy/` (state) ignored
and never commit `hosts.json`.

## Configuration

`.ftpdeploy.json` (project, committable):

| Key | Meaning |
| --- | --- |
| `profile` | Name of the entry in `hosts.json` (host, user, password) |
| `remote_dir` | FTP path to deploy into, e.g. `/public_html/myapp` or `/myaddon.com` |
| `local_dir` | Local directory to sync (default `.`, relative to the config file) |
| `ignore` | Ignore file (default `.ftpignore`, gitignore-style syntax) |
| `protected` | Remote paths never deleted, not even with `--mirror` (e.g. `".env"`, `"data/**"`) |
| `ensure_dirs` | Remote directories to create even though nothing uploads into them |
| `verify_url` / `verify_expect` | Optional post-deploy health check |
| `build` | Optional build command run by `deploy --build` |

`hosts.json` (`~/.config/opencode/ftp-deploy/`, per machine, never committed):

| Key | Meaning |
| --- | --- |
| `host` / `port` / `user` | FTP connection details (Neoserv: port 21) |
| `password` | The password (or use `password_env` and keep it out of the file) |
| `tls` | Explicit FTPS when the host supports it; Neoserv documents plain FTP |
| `timeout` | Optional socket timeout |

## Commands

```
init   [--profile NAME] [--remote-dir DIR]     create the project config files
plan   [--json] [--checksum]                   show what a deploy would do
deploy [--yes] [--delete] [--mirror] [--checksum] [--build] [--dry-run] [--json]
push   <local-file> <remote-path>              one-off upload (.env, hotfix)
verify                                          check verify_url
dirs                                            create remote_dir and ensure_dirs
status                                          offline check: local vs last deploy
```

Shared flags: `--config`, `--config-dir`, `--profile`, `--host`, `--port`,
`--user`, `--password-env`, `--tls/--no-tls`, `--active`, `--timeout`,
`--verbose`, `--json`, `--yes`, `--allow-root`, `--version`.

`plan` exits `4` when there is something to do, `0` when the server matches.
`deploy` refuses to run interactively without confirmation; in scripts pass
`--yes` after you have seen and approved the plan.

## Safety model

- **Deletion is never the default.** Without `--delete` the tool cannot
  remove anything. With it, only state-tracked files are removed, and only if
  the remote size still matches what was recorded.
- **`protected` beats everything** — `--mirror` still keeps those paths.
- **`remote_dir` of `/` is refused** unless `--allow-root` is passed.
- Ignore rules are evaluated per project; the tool's own state, `.git`,
  `.env`, `__pycache__` are never uploaded.
- First deploy into a non-empty `public_html`: pre-existing files
  (`cgi-bin/`, `index.html`) are foreign and kept; see
  `references/neoserv.md`.

## Neoserv / cPanel specifics

`references/neoserv.md` covers the folder layout (`public_html`, addon and
subdomain folders), plain FTP vs FTPS, the "limit to 4 connections" advice,
JetBackup, and how to make runtime `data/` directories writable.

## Development

The decision engine is covered by offline tests (no FTP server needed):

```bash
python scripts/test_ftp_sync.py
```

The plan computation, ignore matching, adoption, deletion safety and execution
order run against an in-memory fake transport. The integration path
(real FTP round-trips) was validated against `pyftpdlib`.

## License

Add your `LICENSE` file. (Consider MIT for a small utility like this.)
