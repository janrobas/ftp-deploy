# Neoserv (cPanel) reference

Collected from Neoserv's own documentation and standard cPanel behaviour.
Verify against your own control panel when something disagrees.

## Connection

- Protocol: FTP, port 21. Neoserv's docs recommend **plain FTP**
  ("Only use plain FTP (insecure)") for their servers; explicit FTPS on
  port 21 usually works on cPanel too (`"tls": true` in the profile), but
  if the connection fails with an SSL error, fall back to plain FTP —
  it is what Neoserv documents. Passwords go in cleartext over plain FTP,
  so prefer FTPS when it connects.
- SFTP is a separate thing and is **disabled by default**; it must be
  enabled per hosting package in Moj NEOSERV before it works. ftp_sync
  does not use SFTP.
- Main account: the cPanel username and password. It reaches the whole
  package. Additional FTP accounts can be restricted to one folder
  (cPanel → Files → FTP Accounts) — good for limiting a deploy profile
  to a single subdomain's directory.
- Neoserv recommends **limiting simultaneous connections to 4** (and
  switching to active mode) if transfers keep dropping. ftp_sync uses a
  single connection, so it is already well within that limit. If you see
  repeated 421/timeouts mid-transfer, try `--active` and a lower
  `--timeout`, or add `"timeout": 30` to the profile.

## Where the files go (remote_dir)

The document root depends on how the domain is added:

| Domain kind | Typical path (FTP-relative) | Deploy `remote_dir` |
| --- | --- | --- |
| Main domain of the package | `public_html` | `/public_html` |
| Addon domain `example.si` | `example.si` (sibling of `public_html`) | `/example.si` |
| Subdomain `app.example.si` | `app.example.si` | `/app.example.si` |

Check cPanel → Domains to see the real path; for addon/subdomains it may
omit `public_html` entirely. A sub-folder is fine too (e.g.
`/public_html/memory`) as long as the domain's document root is set
accordingly (cPanel → Domains → manage the domain).

FTP accounts created in cPanel are often chrooted, so `remote_dir` must be
written **relative to what that account sees** — with the main account the
paths above work directly; with a restricted account, `/` already *is* the
assigned folder.

## Fresh public_html is not empty

A new package has `index.html`/`index.php` and `cgi-bin/` in
`public_html`. ftp_sync treats them as **foreign** remote files and keeps
them (listed as `kept`). Either remove them once in cPanel File Manager,
or accept that they stay. `--mirror` would delete them — only use it in a
dedicated subfolder you own completely.

## Quirks that matter for ftp_sync

- MLSD is supported (used for listings). If a transfer leaves behind a
  `.<name>.ftpdeploy-tmp` file (connection died mid-upload), ftp_sync
  ignores those files in listings; the next deploy re-uploads and renames
  over them. You can also delete them by hand.
- Uploads are binary (`TYPE I`) and atomic (tmp + rename); cPanel's
  pure-ftpd handles the rename fine.
- Server-reported sizes are what the tool compares against — cPanel does
  not report checksums, which is why ftp_sync tracks hashes locally.
- cPanel's JetBackup keeps daily/weekly/monthly backups of the home
  directory; if a bad deploy needs a rollback, restore the site folder
  from JetBackup (Files → JetBackup 5), don't rely on FTP alone.

## Writable runtime data (MemoryDown-style apps)

FTP cannot set permissions reliably. Directories the PHP app must write
into (`data/users`, `data/auth`, `data/sessions`, `data/logs`) should be
created and made writable either by the app itself on first run (with
`ensure_dirs` creating them via `ftp_sync dirs`) or in cPanel File
Manager (right-click → Permissions, usually 755/775 depending on how the
host runs PHP).
