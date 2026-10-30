# .ftpignore syntax

One pattern per line. The file lives in the project root next to
`.ftpdeploy.json` (the path is configurable with the `ignore` key).
Last matching pattern wins, like gitignore.

## Syntax

| Construct | Meaning | Example |
| --- | --- | --- |
| `# ...` | comment | `# local only` |
| blank line | ignored | |
| `name` | any file or dir with this name, any depth | `*.log` |
| `dir/` | a directory with this name and **everything under it** | `data/` |
| `/name` | anchored to the project root only | `/vendor/` |
| `*` | any run of chars except `/` | `*.tmp` |
| `**` | any run of chars including `/` | `logs/**/old/` |
| `dir/**` | same as `dir/` (dir and all contents) | `data/**` |
| `!pattern` | re-include what a previous pattern excluded | `!data/keep.md` |

Matching is against posix-style relative paths without a leading slash.

## Always ignored (built in)

These are hard-coded and can never be deployed, even if `.ftpignore`
tries to re-include them: `.git/`, `.ftp-deploy/`, `__pycache__/`, the
files `.ftpdeploy.json` and `.ftpignore` themselves, and any file ending
in `.ftpdeploy-tmp`.

## Examples

```
# VCS and the deploy tool's own state
.git/
.ftp-deploy/
.ftpdeploy.json
.ftpignore

# Secrets
.env

# This project's runtime data must not be synced (kept per-server)
data/

# Dev-only
tests/
.github/
node_modules/
*.log
.DS_Store

# But one fixture must still be deployed
!tests/fixtures/logo.png
```

## Deletion protection is separate

`.ftpignore` decides what is **uploaded**. What must never be **deleted**
remotely is the `protected` list in `.ftpdeploy.json` (same pattern
syntax, e.g. `[".env", "data/**"]`). A file that is ignored locally
and protected remotely is the normal way to say "this lives only on the
server".
