"""Move a signed-in session between machines (``mai2srt session export/import``).

The sign-in needs a visible browser, which a headless Linux server does not
have. The cookie jar is browser-agnostic, so the supported workflow is: sign in
where there is a desktop, export, copy the file over, import. The exported file
IS a login credential -- it is written 0600 and the CLI says so.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .config import (
    DEFAULT_ACCOUNT, Config, active_account, add_account, cookie_jar_for,
    list_accounts, set_active_account,
)

FORMAT = "mai2srt-session"
VERSION = 1


class SessionIOError(RuntimeError):
    pass


def _write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` readable by the owner only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
    finally:
        try:
            os.chmod(path, 0o600)   # a pre-existing file keeps its old mode
        except OSError:
            pass


def export_session(cfg: Config, out: Path, account: str | None = None) -> str:
    """Write the account's cookie jar to ``out``; returns the account name."""
    name = account or active_account(cfg)
    if not name or name not in list_accounts(cfg):
        raise SessionIOError("no signed-in account to export (run `mai2srt login` first)")
    jar = cookie_jar_for(cfg, name)
    if not jar.exists():
        raise SessionIOError("account %r has no saved session" % name)
    try:
        cookies = json.loads(jar.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SessionIOError("cookie jar unreadable: %s" % e) from e
    doc = {"format": FORMAT, "version": VERSION, "account": name,
           "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "cookies": cookies}
    _write_private(out, json.dumps(doc, ensure_ascii=False, indent=1))
    return name


def import_session(cfg: Config, src: Path, account: str | None = None) -> str:
    """Install an exported session; it becomes the active account.

    ``account`` overrides the name stored in the file. Importing over an
    existing account replaces its cookies (that is the refresh path).
    """
    try:
        doc = json.loads(Path(src).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SessionIOError("cannot read %s: %s" % (src, e)) from e
    if (not isinstance(doc, dict) or doc.get("format") != FORMAT
            or not isinstance(doc.get("cookies"), list)):
        raise SessionIOError("%s is not a mai2srt session export" % src)
    if doc.get("version") != VERSION:
        raise SessionIOError("unsupported session export version: %r"
                             % doc.get("version"))
    name = (account or doc.get("account") or DEFAULT_ACCOUNT).strip()
    if name != DEFAULT_ACCOUNT and name not in list_accounts(cfg):
        try:
            add_account(cfg, name)
        except ValueError as e:
            raise SessionIOError(str(e)) from e
    cfg.ensure_dirs()
    _write_private(cookie_jar_for(cfg, name),
                   json.dumps(doc["cookies"], ensure_ascii=False, indent=1))
    set_active_account(cfg, name)
    return name
