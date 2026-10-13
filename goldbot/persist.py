"""Journal persistence across restarts (for hosts with no disk, e.g. Render free tier).

The SQLite journal (live trades + the historical sample that powers the probability
engine) is lost on every redeploy without a persistent disk. When probability.
block_if_insufficient is false the journal is only informational, but losing the
running sample still resets everything the bot has learned.

Solution: back up journal.sqlite to a PRIVATE GitHub Gist and restore it at startup.
Requires only GITHUB_TOKEN (already available in most deploys) with gist scope.
Fail-open: any error leaves the local journal untouched and never stops the bot.
"""
from __future__ import annotations

import base64
import logging
import os
import sqlite3
import tempfile
import time
from typing import Optional

import requests

log = logging.getLogger("persist")
API = "https://api.github.com"


def _safe_copy(src: str, dst: str) -> bool:
    """Copy a live SQLite DB consistently (WAL checkpoint) to dst."""
    try:
        con = sqlite3.connect(src)
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            bck = sqlite3.connect(dst)
            try:
                con.backup(bck)
            finally:
                bck.close()
        finally:
            con.close()
        return True
    except sqlite3.Error as e:
        log.warning("تعذّر نسخ السجل: %s", e)
        return False


class JournalSync:
    def __init__(self, cfg, token: Optional[str] = None, session: Optional[requests.Session] = None,
                 journal=None):
        p = cfg.section("persistence")
        self.enabled = bool(p.get("enabled", False))
        self.journal_path = cfg.get("paths.journal", "data/journal.sqlite")
        # Optional live Journal handle: restore() must close its connection before swapping the file.
        self.journal = journal
        self.filename = p.get("filename", "goldbot-journal.sqlite")
        self.description = p.get("description", "GOLDNBOY journal backup (private)")
        self.interval = float(p.get("interval_min", 30)) * 60
        self.token = token or os.environ.get("GITHUB_TOKEN") or ""
        self.s = session or requests.Session()
        self.last_error: Optional[str] = None
        self.last_push = 0.0
        self.gist_id: Optional[str] = None
        self._headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json"}

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.token)

    def _local_has_data(self) -> bool:
        """يعتبر السجل المحلي موجودًا إن كان يحتوي أي صف (صفقة أو سجل تاريخي)."""
        if not os.path.exists(self.journal_path) or os.path.getsize(self.journal_path) == 0:
            return False
        try:
            con = sqlite3.connect(self.journal_path)
            try:
                for tbl in ("signals", "history_trades"):
                    try:
                        n = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                    except sqlite3.Error:
                        continue
                    if n:
                        return True
            finally:
                con.close()
        except sqlite3.Error:
            return False
        return False

    # ------------------------------------------------------------------ pull (startup)
    def restore(self) -> bool:
        """يجلب السجل من Gist الخاص إن وُجد ويحلّ محل المحلي الفارغ. لا يفعل شيئًا إن كان المحلي يحتوي بيانات."""
        if not self.ready:
            return False
        if self._local_has_data():
            log.info("السجل المحلي يحتوي بيانات — لا حاجة للاستعادة من Gist")
            return False
        try:
            gid = self.gist_id or self._gist_id_in_listing()
            if not gid:
                log.info("لا يوجد نسخة سابقة على Gist — سيبدأ سجل جديد")
                return False
            r = self.s.get(f"{API}/gists/{gid}", headers=self._headers, timeout=30)
            r.raise_for_status()
            f = r.json()["files"][self.filename]
            content = f.get("content")
            if not content and f.get("raw_url"):
                content = self.s.get(f["raw_url"], timeout=30).text
            data = base64.b64decode(content)
            os.makedirs(os.path.dirname(self.journal_path) or ".", exist_ok=True)
            # The app already holds an open WAL connection. Overwriting the main DB file in place
            # leaves a stale -wal/-shm behind, and SQLite replays it over the restored file — wiping
            # the very data we just restored. Close the connection and drop the sidecars first.
            if self.journal is not None:
                self.journal.close_connection()
            for sidecar in (self.journal_path + "-wal", self.journal_path + "-shm"):
                if os.path.exists(sidecar):
                    os.remove(sidecar)
            with open(self.journal_path, "wb") as fh:
                fh.write(data)
            if self.journal is not None:
                self.journal.reopen()
            self.gist_id = gid
            log.info("تمت استعادة السجل من Gist (%d بايت)", len(data))
            return True
        except Exception as e:                            # noqa: BLE001 - code-host API issues => fail-open
            self.last_error = f"{type(e).__name__}: {e}"
            log.warning("تعذّرت استعادة السجل من Gist (%s) — نتابع بالسجل المحلي", self.last_error)
            return False

    def _find_gist(self) -> Optional[str]:
        if self.gist_id:
            return self.gist_id
        try:
            r = self.s.get(f"{API}/gists", headers=self._headers, timeout=20,
                           params={"per_page": 100})
            r.raise_for_status()
            for g in r.json():
                if self.filename in (g.get("files") or {}):
                    self.gist_id = g["id"]
                    return self.gist_id
        except Exception as e:                            # noqa: BLE001
            self.last_error = f"{type(e).__name__}: {e}"
        return None

    def _gist_id_in_listing(self) -> Optional[str]:
        """معرّف نسخة موجودة عبر GET /gists (بلا تفعيل self.gist_id)."""
        try:
            r = self.s.get(f"{API}/gists", headers=self._headers, timeout=20, params={"per_page": 100})
            r.raise_for_status()
            for g in r.json():
                if self.filename in (g.get("files") or {}):
                    return g["id"]
        except Exception as e:                            # noqa: BLE001
            self.last_error = f"{type(e).__name__}: {e}"
        return None

    # ------------------------------------------------------------------ push (periodic)
    def due(self) -> bool:
        return self.ready and (time.time() - self.last_push) >= self.interval

    def push(self, force: bool = False) -> bool:
        if not self.ready:
            return False
        if not force and not self.due():
            return False
        if not os.path.exists(self.journal_path):
            return False
        tmp = os.path.join(tempfile.mkdtemp(), self.filename)
        if not _safe_copy(self.journal_path, tmp):
            return False
        try:
            with open(tmp, "rb") as fh:
                content = base64.b64encode(fh.read()).decode()
            files = {self.filename: {"content": content}}
            gid = self._find_gist()
            if gid:
                r = self.s.patch(f"{API}/gists/{gid}", headers=self._headers, timeout=30,
                                 json={"files": files})
            else:
                r = self.s.post(f"{API}/gists", headers=self._headers, timeout=30,
                                json={"description": self.description, "public": False, "files": files})
            r.raise_for_status()
            self.gist_id = r.json().get("id", gid)
            self.last_push = time.time()
            self.last_error = None
            log.info("تم رفع نسخة السجل إلى Gist (%s)", self.gist_id)
            return True
        except Exception as e:                            # noqa: BLE001
            self.last_error = f"{type(e).__name__}: {e}"
            log.warning("تعذّر رفع السجل إلى Gist (%s) — يتكرر تلقائيًا", self.last_error)
            return False

    def status_text(self) -> str:
        if not self.enabled:
            return "معطّل"
        if not self.token:
            return "بانتظار GITHUB_TOKEN"
        if self.last_error:
            return f"خطأ: {self.last_error}"
        return f"Gist {self.gist_id}" if self.gist_id else "جاهز (لا نسخة بعد)"
