"""Persistent cache of lyrics lookup results.

Remembers which music files have already been checked against lrclib so the
app can skip them on later runs (saves network traffic and processing time).
"""

import sqlite3
import time
from pathlib import Path

# Status values stored in the database
STATUS_DOWNLOADED = "downloaded"  # synced lyrics found and processed
STATUS_NO_SYNCED = "no_synced"    # lrclib has the track, but only plain lyrics
STATUS_NO_LYRICS = "no_lyrics"    # lrclib has no entry for the track

NEGATIVE_STATUSES = (STATUS_NO_SYNCED, STATUS_NO_LYRICS)


class LyricsCache:
    def __init__(self, db_path="lyrics_cache.db", retry_after_days=30,
                 force_recheck=False):
        """
        db_path:          location of the SQLite file
        retry_after_days: how long to trust a negative result before retrying
        force_recheck:    if True, should_skip() always returns False so every
                          file is checked again (results are still recorded)
        """
        self.retry_after_days = retry_after_days
        self.force_recheck = force_recheck
        db_path = Path(db_path).expanduser()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self._init_db()

    def _init_db(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS lyrics_checks (
                path       TEXT PRIMARY KEY,
                mtime      REAL NOT NULL,
                size       INTEGER NOT NULL,
                status     TEXT NOT NULL,
                checked_at REAL NOT NULL
            )
        """)
        self.conn.commit()

    def should_skip(self, file_path):
        """Return True if this file doesn't need to be looked up again."""
        if self.force_recheck:
            return False

        p = Path(file_path)
        try:
            st = p.stat()
        except OSError:
            return False

        row = self.conn.execute(
            "SELECT mtime, size, status, checked_at "
            "FROM lyrics_checks WHERE path = ?",
            (str(p),),
        ).fetchone()
        if row is None:
            return False

        mtime, size, status, checked_at = row

        # File changed since the last check -> look it up again
        if mtime != st.st_mtime or size != st.st_size:
            return False

        # Lyrics already downloaded -> nothing more to do
        if status == STATUS_DOWNLOADED:
            return True

        # Known negative result: skip until the retry window expires
        if status in NEGATIVE_STATUSES:
            age_days = (time.time() - checked_at) / 86400
            return age_days < self.retry_after_days

        return False

    def record(self, file_path, status):
        """Store the result of a lookup for this file."""
        p = Path(file_path)
        st = p.stat()
        self.conn.execute(
            """
            INSERT INTO lyrics_checks (path, mtime, size, status, checked_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                mtime=excluded.mtime,
                size=excluded.size,
                status=excluded.status,
                checked_at=excluded.checked_at
            """,
            (str(p), st.st_mtime, st.st_size, status, time.time()),
        )
        self.conn.commit()

    def stats(self):
        """Return {status: count} for a quick summary."""
        rows = self.conn.execute(
            "SELECT status, COUNT(*) FROM lyrics_checks GROUP BY status"
        ).fetchall()
        return dict(rows)

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
