#!/usr/bin/env python3
"""ftp_sync.py - deterministic incremental FTP deploy sync.

Uploads only changed files, deletes only files it previously uploaded,
never touches foreign remote files unless explicitly asked to mirror.

Python 3.9+, standard library only (ftplib). Requires MLSD support on the
server (Neoserv/cPanel supports it).

Commands: init | plan | deploy | push | verify | dirs | status

Configuration: see SKILL.md. State lives in <project>/.ftp-deploy/state-*.json.
"""

from __future__ import annotations

import argparse
import contextlib
import ftplib
import getpass
import hashlib
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "0.1.0"

# Files matching this suffix are the tool's own in-flight uploads.
TMP_SUFFIX = ".ftpdeploy-tmp"

# Never uploaded and never walked, regardless of .ftpignore.
ALWAYS_SKIP_DIRS = {".git", ".ftp-deploy", "__pycache__"}
ALWAYS_SKIP_FILES = {".ftpignore", ".ftpdeploy.json"}

DEFAULT_PROTECTED = [".env", "data/**"]
DEFAULT_ENSURE_DIRS = ["data/users", "data/auth", "data/sessions", "data/logs"]

GLOBAL_CONFIG_DIR = Path.home() / ".config" / "opencode" / "ftp-deploy"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class DeployError(Exception):
    def __init__(self, msg, code=1, ftp_code=None):
        super().__init__(msg)
        self.code = code
        self.ftp_code = ftp_code


def fail(msg, code=1):
    print("error: " + msg, file=sys.stderr)
    raise SystemExit(code)


# ---------------------------------------------------------------------------
# Ignore rules (.ftpignore / protected lists)
# ---------------------------------------------------------------------------

class IgnoreRules:
    """Last-match-wins matcher for gitignore-style patterns.

    Supported: comments (#), blank lines, '*' and '?' (no slash), '**'
    (any depth), trailing '/' for directories (matches the dir and
    everything under it), leading '/' to anchor at the root, and '!'
    negation. A pattern ending in '/**' behaves like a trailing '/'.
    """

    def __init__(self, patterns):
        self._rules = []
        self.has_negations = False
        for pattern in patterns:
            if pattern is None:
                continue
            pattern = pattern.strip()
            if not pattern or pattern.startswith("#"):
                continue
            if pattern.startswith("!"):
                self.has_negations = True
            compiled = self._compile(pattern)
            if compiled is not None:
                self._rules.append(compiled)

    @staticmethod
    def _compile(pattern):
        negate = pattern.startswith("!")
        if negate:
            pattern = pattern[1:]
        if not pattern:
            return None
        anchored = pattern.startswith("/")
        if anchored:
            pattern = pattern[1:]
        if pattern.endswith("/**"):
            pattern = pattern[:-3] + "/"
        dir_only = pattern.endswith("/")
        if dir_only:
            pattern = pattern[:-1]
        if not pattern:
            return None

        body = ""
        i = 0
        n = len(pattern)
        while i < n:
            c = pattern[i]
            if c == "*":
                if pattern[i + 1:i + 2] == "*":
                    j = i + 2
                    if pattern[j:j + 1] == "/":
                        body += "(?:.*/)?"
                        i = j + 1
                    else:
                        body += ".*"
                        i = j
                else:
                    body += "[^/]*"
                    i += 1
            elif c == "?":
                body += "[^/]"
                i += 1
            else:
                body += re.escape(c)
                i += 1

        prefix = "" if anchored else "(?:.*/)?"
        if dir_only:
            regex = "^" + prefix + body + "(?:/.*)?$"
        else:
            regex = "^" + prefix + body + "$"
        return (negate, re.compile(regex))

    def is_ignored(self, rel):
        """rel: posix path, no leading slash, no trailing slash."""
        ignored = False
        for negate, rx in self._rules:
            if rx.match(rel):
                ignored = not negate
        return ignored

    @classmethod
    def from_file(cls, path):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            raise DeployError("cannot read %s: %s" % (path, e))
        rules = cls(lines)
        return rules


# ---------------------------------------------------------------------------
# Local walk
# ---------------------------------------------------------------------------

def _join(rel, name):
    return "%s/%s" % (rel, name) if rel else name


def sha256_file(path, chunk=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            data = f.read(chunk)
            if not data:
                break
            h.update(data)
    return h.hexdigest()


def local_walk(root, rules):
    """Return (dirs, files): dicts of posix relpath -> None / (size, sha256)."""
    dirs = {}
    files = {}
    root = str(root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        rel = "" if rel == "." else rel.replace(os.sep, "/")
        pruned = []
        for d in sorted(dirnames):
            child_rel = _join(rel, d)
            if d in ALWAYS_SKIP_DIRS:
                continue
            if rules.is_ignored(child_rel) and not rules.has_negations:
                continue
            pruned.append(d)
        dirnames[:] = pruned
        if rel:
            dirs[rel] = None
        for name in sorted(filenames):
            if name in ALWAYS_SKIP_FILES or name.endswith(TMP_SUFFIX):
                continue
            relpath = _join(rel, name)
            if rules.is_ignored(relpath):
                continue
            p = os.path.join(dirpath, name)
            size = os.path.getsize(p)
            files[relpath] = (size, sha256_file(p))
    return dirs, files


# ---------------------------------------------------------------------------
# Remote listing
# ---------------------------------------------------------------------------

def remote_walk(ftp, remote_dir, warn=print):
    """Return {relpath: {'type': 'file'|'dir', 'size': int}} under remote_dir."""
    out = {}
    root = remote_dir.rstrip("/") or "/"
    stack = [(root, "")]
    while stack:
        full, rel = stack.pop()
        try:
            entries = list(ftp.mlsd(full))
        except Exception as e:
            raise DeployError(
                "cannot list remote directory %r: %s "
                "(the server must support MLSD; Neoserv/cPanel does)" % (full, e),
                code=3,
            )
        for name, facts in entries:
            if name in (".", "..") or name.endswith(TMP_SUFFIX):
                continue
            ftype = (facts.get("type") or "").lower()
            r = _join(rel, name)
            if ftype == "dir":
                out[r] = {"type": "dir"}
                stack.append((full.rstrip("/") + "/" + name, r))
            elif ftype == "file":
                try:
                    size = int(facts.get("size") or 0)
                except (TypeError, ValueError):
                    size = 0
                out[r] = {"type": "file", "size": size}
            else:
                warn("note: skipping special remote entry %r (type=%r)" % (r, ftype or "?"))
    return out


# ---------------------------------------------------------------------------
# FTP transport
# ---------------------------------------------------------------------------

RECONNECTABLE_CODES = {421, 425, 426, 451}

# ftplib.all_errors is itself a tuple; Python 3.14 rejects nested tuples in
# except clauses, so flatten it here.
FTP_ERRORS = ftplib.all_errors + (ssl.SSLError, OSError, EOFError)


class FtpTransport:
    """One reused FTP connection with one reconnect-on-drop retry per operation."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._ftp = None
        self._retried = False
        self._last_ok = time.time()
        self._connect()

    def _connect(self):
        if self._ftp is not None:
            with contextlib.suppress(Exception):
                self._ftp.quit()
        try:
            ftp = ftplib.FTP_TLS() if self.cfg.tls else ftplib.FTP()
            ftp.encoding = "utf-8"
            ftp.connect(self.cfg.host, self.cfg.port, timeout=self.cfg.timeout)
            ftp.login(self.cfg.user, self.cfg.password)
            if self.cfg.tls:
                ftp.prot_p()
            ftp.set_pasv(not self.cfg.active)
            ftp.voidcmd("TYPE I")
        except FTP_ERRORS as e:
            hint = ""
            if isinstance(e, ssl.SSLError):
                hint = " (TLS failed; try --no-tls if the host uses plain FTP)"
            raise DeployError(
                "cannot connect to %s:%s: %s%s" % (self.cfg.host, self.cfg.port, self._sanitize(e), hint),
                code=3,
            )
        self._ftp = ftp
        self._retried = False
        self._last_ok = time.time()

    def _sanitize(self, text):
        text = str(text)
        if self.cfg.password:
            text = text.replace(self.cfg.password, "***")
        return text

    def _reconnectable(self, e):
        code = getattr(e, "code", None)
        if isinstance(code, str):
            with contextlib.suppress(ValueError):
                code = int(code)
        if code in RECONNECTABLE_CODES:
            return True
        return isinstance(e, (EOFError, ConnectionError, socket.timeout, TimeoutError))

    def run(self, fn, retry=True):
        if time.time() - self._last_ok > 90:
            with contextlib.suppress(Exception):
                self._ftp.voidcmd("NOOP")
        try:
            result = fn(self._ftp)
        except FTP_ERRORS as e:
            code = getattr(e, "code", None)
            if retry and not self._retried and self._reconnectable(e):
                self._retried = True
                self._connect()
                result = fn(self._ftp)
            else:
                raise DeployError(self._sanitize(e), code=3, ftp_code=code)
        self._retried = False
        self._last_ok = time.time()
        return result

    # -- operations ----------------------------------------------------------

    def mlsd(self, path):
        return self.run(lambda f: list(f.mlsd(path)))

    def mkdir_safe(self, full):
        """mkd, tolerating an existing directory (checked with CWD)."""
        try:
            self.run(lambda f: f.mkd(full))
        except DeployError as e:
            exists = False
            with contextlib.suppress(Exception):
                self.run(lambda f: f.cwd(full), retry=False)
                exists = True
            if not exists:
                raise
            self.run(lambda f: f.cwd(self.cfg.remote_dir.rstrip("/") or "/"), retry=False)

    def upload_atomic(self, local_path, remote_dir, name):
        full = remote_dir.rstrip("/") + "/" + name
        tmp = remote_dir.rstrip("/") + "/." + name + TMP_SUFFIX
        with open(local_path, "rb") as fh:
            def do(_ftp):
                fh.seek(0)
                return _ftp.storbinary("STOR " + tmp, fh, blocksize=65536)
            self.run(do)
        with contextlib.suppress(Exception):
            self.run(lambda f: f.delete(full), retry=False)
        self.run(lambda f: f.rename(tmp, full))

    def delete(self, full):
        self.run(lambda f: f.delete(full), retry=False)

    def rmd(self, full):
        self.run(lambda f: f.rmd(full), retry=False)

    def download_hash(self, full):
        h = hashlib.sha256()

        def cb(data):
            h.update(data)

        self.run(lambda f: f.retrbinary("RETR " + full, cb, blocksize=65536))
        return h.hexdigest()

    def quit(self):
        if self._ftp is not None:
            with contextlib.suppress(Exception):
                self._ftp.quit()
            self._ftp = None


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------

class StateStore:
    def __init__(self, path, remote_dir):
        self.path = Path(path)
        self.remote_dir = remote_dir
        self.fresh = not self.path.exists()
        self.data = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise DeployError("cannot read state file %s: %s" % (self.path, e))
            if raw.get("remote_dir") != remote_dir:
                print("note: state file was for remote_dir %r; starting fresh for %r"
                      % (raw.get("remote_dir"), remote_dir))
                self.fresh = True
                self.data = {}
            else:
                self.data = raw.get("files") or {}
        self.adopt_allowed = self.fresh or not self.data

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "remote_dir": self.remote_dir,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "files": dict(sorted(self.data.items())),
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)


def state_path(cfg, profile_name):
    base = cfg.config_parent / ".ftp-deploy"
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", profile_name or cfg.host).strip("-") or "default"
    return base / ("state-%s.json" % slug)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class Config:
    pass  # plain attribute bag, built by load_config()


def default_config_dir(args):
    return Path(args.config_dir) if args.config_dir else GLOBAL_CONFIG_DIR


def load_config(args, require_remote=True):
    cfg_path = Path(args.config) if args.config else Path(".ftpdeploy.json")
    if not cfg_path.exists():
        fail("no config file at %s. Create one with: ftp_sync.py init" % cfg_path)
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        fail("%s is not valid JSON: %s" % (cfg_path, e))
    if not isinstance(raw, dict):
        fail("%s must contain a JSON object" % cfg_path)

    config_dir = default_config_dir(args)
    hosts = {}
    hosts_path = config_dir / "hosts.json"
    if hosts_path.exists():
        try:
            hosts = json.loads(hosts_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            fail("%s is not valid JSON: %s" % (hosts_path, e))

    profile_name = args.profile or raw.get("profile") or ""
    prof = {}
    if profile_name:
        prof = hosts.get(profile_name) or {}
        if not prof:
            fail("profile %r not found in %s. Add it there (see templates)."
                 % (profile_name, hosts_path))

    merged = {}
    for src in (prof, raw):
        for k, v in src.items():
            if k == "profile":
                continue
            if v is not None and v != "":
                merged[k] = v

    cfg = Config()
    cfg.host = args.host or merged.get("host") or ""
    cfg.port = args.port if args.port is not None else int(merged.get("port") or 21)
    cfg.user = args.user or merged.get("user") or ""
    cfg.password = ""
    cfg.password_env = args.password_env or merged.get("password_env") or ""
    cfg.tls = merged.get("tls", False)
    if args.tls_flag is not None:
        cfg.tls = args.tls_flag
    cfg.active = bool(args.active)
    cfg.timeout = args.timeout if args.timeout is not None else int(merged.get("timeout") or 60)
    cfg.remote_dir = merged.get("remote_dir") or ""
    cfg.local_dir = merged.get("local_dir") or "."
    cfg.ignore = merged.get("ignore") or ".ftpignore"
    cfg.protected = merged.get("protected") or []
    cfg.ensure_dirs = merged.get("ensure_dirs") or []
    cfg.verify_url = merged.get("verify_url") or ""
    cfg.verify_expect = merged.get("verify_expect") or None
    cfg.build = merged.get("build") or ""
    cfg.profile_name = profile_name
    cfg.config_path = cfg_path
    cfg.config_parent = cfg_path.parent
    cfg.config_dir = config_dir
    cfg.verbose = bool(args.verbose)

    # project config should not carry a secret
    if "password" in raw and raw.get("password"):
        print("warning: a password is stored in %s - never commit that file." % cfg_path)

    if require_remote:
        if not cfg.host:
            fail("no host configured. Set it in %s or hosts.json." % hosts_path)
        if not cfg.user:
            fail("no user configured. Set it in %s or hosts.json." % hosts_path)
        if not cfg.remote_dir:
            fail("no remote_dir configured. Set it in %s." % cfg_path)
        if cfg.remote_dir in ("/", "") and not getattr(args, "allow_root", False):
            fail("remote_dir is the FTP root; refusing. Pass --allow-root if you "
                 "really mean it.")

    # password
    cfg.password = merged.get("password") or ""
    if not cfg.password and cfg.password_env:
        cfg.password = os.environ.get(cfg.password_env) or ""
    if not cfg.password:
        cfg.password = os.environ.get("FTP_PASSWORD") or ""
    if require_remote and not cfg.password:
        if sys.stdin.isatty():
            cfg.password = getpass.getpass("FTP password for %s@%s: " % (cfg.user, cfg.host))
        else:
            fail("no password available. Put it in the profile in %s (or use "
                 "password_env/FTP_PASSWORD)." % hosts_path)

    # local root, anchored to the config file's directory
    p = Path(cfg.local_dir)
    if not p.is_absolute():
        p = cfg.config_parent / p
    cfg.local_root = p.resolve()
    if require_remote and not cfg.local_root.is_dir():
        fail("local_dir %s does not exist (resolved to %s)" % (cfg.local_dir, cfg.local_root))
    return cfg


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

class Plan:
    def __init__(self):
        self.add = []
        self.remote_modified = []
        self.adopt = []
        self.unchanged = []
        self.delete = []
        self.deletable = []
        self.mirror_delete = []
        self.foreign_kept = []
        self.mkdir = []
        self.rmdir = []

    def uploads(self):
        return self.add + self.remote_modified

    def anything(self):
        return bool(self.add or self.remote_modified or self.delete or
                    self.mirror_delete or self.mkdir or self.rmdir)


def compute_plan(local_dirs, local_files, remote, state, protected,
                 adopt_allowed, delete_enabled, mirror):
    plan = Plan()
    remote_files = {r: v for r, v in remote.items() if v["type"] == "file"}
    remote_dirs = {r for r, v in remote.items() if v["type"] == "dir"}
    local_set = set(local_files) | set(local_dirs)

    for rel, (size, sha) in sorted(local_files.items()):
        st = state.get(rel)
        r = remote.get(rel)
        if st and st.get("sha") == sha:
            if r is None:
                plan.add.append(rel)
            elif r["type"] != "file" or r["size"] != st.get("remote_size"):
                plan.remote_modified.append(rel)
            else:
                plan.unchanged.append(rel)
        else:
            if (r is not None and r["type"] == "file" and adopt_allowed
                    and r["size"] == size):
                plan.adopt.append(rel)
            else:
                plan.add.append(rel)

    needed_dirs = set(local_dirs)
    for rel in local_files:
        acc = ""
        for part in rel.split("/")[:-1]:
            acc = part if not acc else acc + "/" + part
            needed_dirs.add(acc)
    for d in sorted(needed_dirs, key=lambda x: (x.count("/"), x)):
        if d not in remote_dirs:
            plan.mkdir.append(d)

    for rel in sorted(remote):
        if rel in local_set:
            continue
        if protected.is_ignored(rel):
            continue
        v = remote[rel]
        if v["type"] == "file":
            st = state.get(rel)
            if st and v["size"] == st.get("remote_size"):
                if delete_enabled or mirror:
                    plan.delete.append(rel)
                else:
                    plan.deletable.append(rel)
            elif mirror:
                plan.mirror_delete.append(rel)
            else:
                plan.foreign_kept.append(rel)

    # empty remote dirs that are neither needed locally nor protected
    for d in sorted(remote_dirs, key=lambda x: (-x.count("/"), x)):
        if d in needed_dirs or protected.is_ignored(d):
            continue
        if any(r.startswith(d + "/") for r in remote):
            continue
        if delete_enabled:
            plan.rmdir.append(d)
    return plan


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def execute(plan, transport, local_root, remote_dir, local_dirs, verbose):
    done = {"uploaded": 0, "deleted": 0, "mkdir": 0, "rmdir": 0}
    root = remote_dir.rstrip("/") or "/"
    for d in plan.mkdir:
        transport.mkdir_safe(root + "/" + d)
        done["mkdir"] += 1
        if verbose:
            print("mkdir " + d)
    for rel in sorted(set(plan.uploads())):
        src = Path(local_root) / Path(*rel.split("/"))
        parent = "/".join(rel.split("/")[:-1])
        remote_parent = root + ("/" + parent if parent else "")
        transport.upload_atomic(str(src), remote_parent, rel.split("/")[-1])
        done["uploaded"] += 1
        if verbose:
            print("upload " + rel)
    for rel in sorted(set(plan.delete) | set(plan.mirror_delete)):
        transport.delete(root + "/" + rel)
        done["deleted"] += 1
        if verbose:
            print("delete " + rel)
    # rmdir candidates: dirs that were already empty, plus parents of files we
    # just deleted that may have become empty - but never a dir the local tree
    # still contains (an empty local dir is "wanted").
    candidates = list(plan.rmdir)
    wanted = set(local_dirs)
    for rel in sorted(set(plan.delete) | set(plan.mirror_delete)):
        acc = ""
        for part in rel.split("/")[:-1]:
            acc = part if not acc else acc + "/" + part
            if acc not in wanted and acc not in candidates:
                candidates.append(acc)
    candidates.sort(key=lambda x: (-x.count("/"), x))
    for d in candidates:
        try:
            transport.rmd(root + "/" + d)
            done["rmdir"] += 1
            if verbose:
                print("rmdir " + d)
        except DeployError:
            pass  # not empty after all; leave it
    return done


def update_state(state, plan, local_files, remote):
    """state: dict of existing records. Mutates and returns it."""
    new = {}
    for rel, (size, sha) in sorted(local_files.items()):
        if rel in plan.uploads():
            new[rel] = {"sha": sha, "size": size, "remote_size": size}
        else:
            r = remote.get(rel) or {}
            new[rel] = {"sha": sha, "size": size, "remote_size": r.get("size", size)}
    for rel, rec in state.items():
        if rel in new:
            continue
        if rel in remote and rel not in plan.delete and rel not in plan.mirror_delete:
            new[rel] = rec  # keep records of still-remote, not-deleted files
    state.clear()
    state.update(new)
    return state


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render(plan, args):
    if args.json:
        payload = {
            "unchanged": len(plan.unchanged),
            "add": plan.add,
            "remote_modified": plan.remote_modified,
            "adopt": plan.adopt,
            "delete": plan.delete,
            "deletable": plan.deletable,
            "mirror_delete": plan.mirror_delete,
            "foreign_kept": plan.foreign_kept,
            "mkdir": plan.mkdir,
            "rmdir": plan.rmdir,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    total = len(plan.uploads()) + len(plan.delete) + len(plan.mirror_delete) \
        + len(plan.mkdir) + len(plan.rmdir)
    print("summary: %d change(s) to apply, %d unchanged, %d adopted, %d kept foreign"
          % (total, len(plan.unchanged), len(plan.adopt), len(plan.foreign_kept)))
    sections = [
        ("add", plan.add),
        ("remote modified (re-upload)", plan.remote_modified),
        ("adopt (recorded, not uploaded)", plan.adopt),
        ("mkdir", plan.mkdir),
        ("delete (we uploaded it; now gone locally)", plan.delete),
        ("deletable (run deploy --delete)", plan.deletable),
        ("delete (mirror: untracked remote file)", plan.mirror_delete),
        ("rmdir (empty dirs)", plan.rmdir),
        ("kept (foreign remote files, never touched)", plan.foreign_kept),
    ]
    for title, items in sections:
        if items:
            print("  %s:" % title)
            for it in items:
                print("    " + it)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init(args):
    cfg_path = Path(args.config) if args.config else Path(".ftpdeploy.json")
    if cfg_path.exists():
        fail("%s already exists; not overwriting." % cfg_path)
    project = cfg_path.parent.resolve().name or "app"
    remote_dir = args.remote_dir or "/public_html/%s" % re.sub(r"[^A-Za-z0-9_-]", "-", project)
    doc = {
        "profile": args.profile or "neoserv",
        "remote_dir": remote_dir,
        "local_dir": ".",
        "ignore": ".ftpignore",
        "protected": DEFAULT_PROTECTED,
        "ensure_dirs": DEFAULT_ENSURE_DIRS,
        "verify_url": "",
        "verify_expect": '"status":"ok"',
        "build": "",
    }
    cfg_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print("wrote %s" % cfg_path)

    ignore_path = cfg_path.parent / ".ftpignore"
    if not ignore_path.exists():
        ignore_path.write_text(IGNORE_TEMPLATE, encoding="utf-8")
        print("wrote %s" % ignore_path)

    config_dir = default_config_dir(args)
    hosts_path = config_dir / "hosts.json"
    if not hosts_path.exists():
        hosts_path.parent.mkdir(parents=True, exist_ok=True)
        hosts_path.write_text(HOSTS_TEMPLATE, encoding="utf-8")
        try:
            os.chmod(hosts_path, 0o600)
        except OSError:
            print("note: could not restrict %s permissions on this OS" % hosts_path)
        print("wrote %s (fill in your host profile)" % hosts_path)

    # git hygiene
    if git_root(cfg_path.parent):
        for rel in (".ftpdeploy.json", ".ftp-deploy/"):
            if not git_ignored(rel, cfg_path.parent):
                print("warning: %s is not ignored by git. Add these lines to .gitignore:"
                      % rel)
                print("  /.ftpdeploy.json")
                print("  /.ftp-deploy/")
                break
    print("next: edit %s (or %s) with your host details, then run 'plan'."
          % (hosts_path, cfg_path))


IGNORE_TEMPLATE = """\
# The deploy tool's own files and VCS internals are never uploaded.
.git/
.ftp-deploy/
.ftpdeploy.json
.ftpignore

# Never upload secrets.
.env

# Runtime data lives only on the server (also list it in "protected").
data/

# Local-only clutter.
node_modules/
*.log
.DS_Store
"""

HOSTS_TEMPLATE = """\
{
  "neoserv": {
    "host": "ftp.example.si",
    "port": 21,
    "user": "cpanel-user",
    "password_env": "NEOSERV_FTP_PASS",
    "tls": false
  }
}
"""


def git_root(start):
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(start), capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def git_ignored(rel, start):
    try:
        out = subprocess.run(
            ["git", "check-ignore", "-q", rel],
            cwd=str(start), capture_output=True)
        return out.returncode == 0
    except OSError:
        return False


def build(cfg):
    if not cfg.build:
        return
    print("build: %s" % cfg.build)
    result = subprocess.run(cfg.build, shell=True, cwd=str(cfg.local_root))
    if result.returncode != 0:
        fail("build command failed (exit %d)" % result.returncode)


def load_world(args, require_remote=True):
    cfg = load_config(args, require_remote=require_remote)
    rules_path = cfg.local_root / cfg.ignore
    rules = IgnoreRules.from_file(rules_path)
    local_dirs, local_files = local_walk(cfg.local_root, rules)
    return cfg, rules, local_dirs, local_files


def cmd_status(args):
    cfg = load_config(args, require_remote=False)
    rules = IgnoreRules.from_file(cfg.local_root / cfg.ignore)
    local_dirs, local_files = local_walk(cfg.local_root, rules)
    st = StateStore(state_path(cfg, cfg.profile_name), cfg.remote_dir)
    changed = []
    new = []
    for rel, (size, sha) in sorted(local_files.items()):
        rec = st.data.get(rel)
        if rec is None:
            new.append(rel)
        elif rec.get("sha") != sha:
            changed.append(rel)
    stale = sorted(set(st.data) - set(local_files))
    if args.json:
        print(json.dumps({"changed": changed, "new": new,
                          "stale_local_deleted": stale}, indent=2, sort_keys=True))
    else:
        print("local vs last deploy: %d new, %d changed, %d deleted locally"
              % (len(new), len(changed), len(stale)))
        for title, items in (("new", new), ("changed", changed),
                             ("deleted locally", stale)):
            for it in items:
                print("  %s %s" % (title, it))


def cmd_plan(args):
    cfg = load_config(args)
    transport = FtpTransport(cfg)
    try:
        rules = IgnoreRules.from_file(cfg.local_root / cfg.ignore)
        local_dirs, local_files = local_walk(cfg.local_root, rules)
        remote = remote_walk(transport, cfg.remote_dir)
        st = StateStore(state_path(cfg, cfg.profile_name), cfg.remote_dir)
        protected = IgnoreRules(cfg.protected)
        plan = compute_plan(local_dirs, local_files, remote, st.data, protected,
                            st.adopt_allowed, False, False)
        if args.checksum:
            for rel in list(plan.unchanged) + list(plan.adopt):
                full = cfg.remote_dir.rstrip("/") + "/" + rel
                remote_sha = transport.download_hash(full)
                local_sha = local_files[rel][1]
                if remote_sha != local_sha:
                    if rel in plan.unchanged:
                        plan.unchanged.remove(rel)
                    else:
                        plan.adopt.remove(rel)
                    plan.remote_modified.append(rel)
            plan.remote_modified.sort()
        render(plan, args)
    finally:
        transport.quit()
    raise SystemExit(4 if plan.anything() else 0)


def cmd_deploy(args):
    cfg = load_config(args)
    if args.build and not args.dry_run:
        build(cfg)
    transport = FtpTransport(cfg)
    try:
        rules = IgnoreRules.from_file(cfg.local_root / cfg.ignore)
        local_dirs, local_files = local_walk(cfg.local_root, rules)
        remote = remote_walk(transport, cfg.remote_dir)
        st = StateStore(state_path(cfg, cfg.profile_name), cfg.remote_dir)
        protected = IgnoreRules(cfg.protected)
        delete_enabled = args.delete or args.mirror
        plan = compute_plan(local_dirs, local_files, remote, st.data, protected,
                            st.adopt_allowed, delete_enabled, args.mirror)
        if args.checksum:
            for rel in list(plan.unchanged) + list(plan.adopt):
                full = cfg.remote_dir.rstrip("/") + "/" + rel
                remote_sha = transport.download_hash(full)
                if remote_sha != local_files[rel][1]:
                    if rel in plan.unchanged:
                        plan.unchanged.remove(rel)
                    else:
                        plan.adopt.remove(rel)
                    plan.remote_modified.append(rel)
            plan.remote_modified.sort()
        render(plan, args)
        if args.dry_run:
            return
        needs_exec = plan.anything()
        if not needs_exec and not plan.adopt:
            return
        if needs_exec and not args.yes:
            if not sys.stdin.isatty():
                fail("deploy would change the server; re-run with --yes after "
                     "confirming the plan with the user.")
            answer = input("apply these changes? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("aborted")
                return
        if needs_exec:
            done = execute(plan, transport, cfg.local_root, cfg.remote_dir,
                           local_dirs, cfg.verbose)
        else:
            done = {"uploaded": 0, "deleted": 0, "mkdir": 0, "rmdir": 0}
        update_state(st.data, plan, local_files, remote)
        st.save()
        summary = {"uploaded": done["uploaded"], "deleted": done["deleted"],
                   "mkdir": done["mkdir"], "rmdir": done["rmdir"],
                   "unchanged": len(plan.unchanged), "adopted": len(plan.adopt)}
        if args.json:
            summary["verify"] = verify_summary(cfg) if cfg.verify_url else None
            print(json.dumps(summary, indent=2, sort_keys=True))
        else:
            print("deployed: %d uploaded, %d deleted, %d dir(s) created, "
                  "%d dir(s) removed, %d unchanged, %d adopted" % (
                      done["uploaded"], done["deleted"], done["mkdir"],
                      done["rmdir"], len(plan.unchanged), len(plan.adopt)))
            if cfg.verify_url:
                verify_summary(cfg, print_result=True)
    finally:
        transport.quit()


def cmd_push(args):
    cfg = load_config(args)
    local = Path(args.local)
    if not local.is_file():
        fail("no such file: %s" % local)
    remote_arg = args.remote.strip()
    if ".." in remote_arg.split("/"):
        fail("remote path must not contain '..'")
    if remote_arg.startswith("/"):
        if not getattr(args, "allow_root", False) and not remote_arg.startswith(
                cfg.remote_dir.rstrip("/") + "/"):
            fail("push target outside remote_dir; pass --allow-root to allow it.")
        full = remote_arg
    else:
        full = cfg.remote_dir.rstrip("/") + "/" + remote_arg
    parent, name = full.rsplit("/", 1)
    transport = FtpTransport(cfg)
    try:
        transport.upload_atomic(str(local), parent, name)
    finally:
        transport.quit()
    print("pushed %s -> %s" % (local, full))


def verify_summary(cfg, print_result=False):
    req = urllib.request.Request(cfg.verify_url, headers={"User-Agent": "ftp-sync/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status = e.code
        body = e.read().decode("utf-8", "replace")
    except Exception as e:
        result = {"url": cfg.verify_url, "ok": False, "error": str(e)}
        if print_result:
            print("verify: FAILED (%s)" % e)
        return result
    ok = 200 <= status < 300 and (
        not cfg.verify_expect
        or re.sub(r"\s+", "", cfg.verify_expect) in re.sub(r"\s+", "", body)
    )
    result = {"url": cfg.verify_url, "status": status, "ok": ok}
    if print_result:
        print("verify: %s %s (status %d)" % ("OK" if ok else "FAILED", cfg.verify_url, status))
    return result


def cmd_verify(args):
    cfg = load_config(args)
    if not cfg.verify_url:
        fail("no verify_url configured in %s" % cfg.config_path)
    result = verify_summary(cfg, print_result=not args.json)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result.get("ok") else 1)


def cmd_dirs(args):
    cfg = load_config(args)
    transport = FtpTransport(cfg)
    try:
        # The remote_dir itself may not exist yet (fresh subdomain folder):
        # create each level from the FTP root down, then the ensure_dirs.
        acc = ""
        for part in cfg.remote_dir.strip("/").split("/"):
            acc = acc.rstrip("/") + "/" + part
            transport.mkdir_safe(acc)
        for d in cfg.ensure_dirs:
            for part in d.strip("/").split("/"):
                acc = acc.rstrip("/") + "/" + part
                transport.mkdir_safe(acc)
        print("mkdir " + acc)
    finally:
        transport.quit()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv:
        print("ftp_sync.py " + VERSION)
        return

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="path to .ftpdeploy.json (default ./.ftpdeploy.json)")
    common.add_argument("--config-dir", help="dir containing hosts.json")
    common.add_argument("--profile", help="profile name in hosts.json")
    common.add_argument("--host")
    common.add_argument("--port", type=int)
    common.add_argument("--user")
    common.add_argument("--password-env", help="env var holding the FTP password")
    common.add_argument("--tls", dest="tls_flag", action="store_true", default=None)
    common.add_argument("--no-tls", dest="tls_flag", action="store_false")
    common.add_argument("--active", action="store_true", help="use active mode")
    common.add_argument("--timeout", type=int, help="socket timeout in seconds")
    common.add_argument("--verbose", action="store_true")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    common.add_argument("--allow-root", action="store_true",
                        help="allow a remote_dir of / and absolute push targets")

    parser = argparse.ArgumentParser(prog="ftp_sync.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", parents=[common], help="create deploy config files")
    p_init.add_argument("--remote-dir")
    p_init.set_defaults(func=cmd_init)

    p_plan = sub.add_parser("plan", parents=[common], help="show what a deploy would do")
    p_plan.add_argument("--checksum", action="store_true",
                        help="verify unchanged files by downloading and hashing")
    p_plan.set_defaults(func=cmd_plan)

    p_deploy = sub.add_parser("deploy", parents=[common], help="apply the plan")
    p_deploy.add_argument("--delete", action="store_true",
                          help="remove files we uploaded that are gone locally")
    p_deploy.add_argument("--mirror", action="store_true",
                          help="also remove untracked remote files (still respects protected)")
    p_deploy.add_argument("--checksum", action="store_true")
    p_deploy.add_argument("--build", action="store_true",
                          help="run the configured build command first")
    p_deploy.add_argument("--dry-run", action="store_true",
                          help="show the plan without writing anything")
    p_deploy.set_defaults(func=cmd_deploy)

    p_push = sub.add_parser("push", parents=[common],
                            help="upload one file (e.g. .env), no state tracking")
    p_push.add_argument("local")
    p_push.add_argument("remote")
    p_push.set_defaults(func=cmd_push)

    p_verify = sub.add_parser("verify", parents=[common], help="check verify_url")
    p_verify.set_defaults(func=cmd_verify)

    p_dirs = sub.add_parser("dirs", parents=[common], help="create ensure_dirs")
    p_dirs.set_defaults(func=cmd_dirs)

    p_status = sub.add_parser("status", parents=[common],
                              help="offline check: local files vs last deploy state")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        args.func(args)
    except DeployError as e:
        print("error: " + str(e), file=sys.stderr)
        raise SystemExit(e.code)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        raise SystemExit(130)


if __name__ == "__main__":
    main()
