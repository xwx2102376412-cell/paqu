"""Persistent successful-download and cross-platform duplicate history."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from visual_rag import extract_video_hashes, hash_similarity


DEFAULT_HISTORY_DB = Path("agent_data") / "multiplatform_history.sqlite3"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonicalize_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/") or "/"
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(k, v) for k, v in query if k.lower() not in {"source", "from", "share_source", "utm_source", "utm_medium", "utm_campaign"}]
    return urllib.parse.urlunsplit((parsed.scheme.lower() or "https", host, path, urllib.parse.urlencode(sorted(query)), ""))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def frame_set_similarity(left: list[str], right: list[str], sample_count: int = 3) -> float:
    if not left or not right:
        return 0.0
    scores = sorted((max(hash_similarity(a, b) for b in right) for a in left), reverse=True)
    count = min(max(1, sample_count), len(scores))
    return sum(scores[:count]) / count


class DownloadHistory:
    def __init__(self, db_path: Path = DEFAULT_HISTORY_DB) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS known_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    video_id TEXT NOT NULL,
                    canonical_url TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('completed','duplicate')),
                    local_path TEXT,
                    sha256 TEXT,
                    frame_hashes TEXT NOT NULL DEFAULT '[]',
                    duplicate_of INTEGER,
                    recorded_at TEXT NOT NULL,
                    UNIQUE(platform, video_id),
                    UNIQUE(canonical_url),
                    FOREIGN KEY(duplicate_of) REFERENCES known_items(id)
                );
                CREATE INDEX IF NOT EXISTS idx_known_sha256 ON known_items(sha256);
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    video_id TEXT NOT NULL,
                    canonical_url TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('failed','completed','duplicate','skipped')),
                    error TEXT,
                    attempted_at TEXT NOT NULL
                );
                """
            )

    def known(self, platform: str, video_id: str, url: str) -> sqlite3.Row | None:
        canonical = canonicalize_url(url)
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM known_items WHERE (platform=? AND video_id=?) OR canonical_url=? LIMIT 1",
                (platform, video_id, canonical),
            ).fetchone()

    def completed_rows(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM known_items WHERE status='completed' ORDER BY id").fetchall()

    def find_content_duplicate(self, sha256: str, frame_hashes: list[str], threshold: float = 0.92) -> tuple[sqlite3.Row | None, float]:
        best = None
        best_score = 0.0
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM known_items WHERE status='completed'").fetchall()
        for row in rows:
            if sha256 and row["sha256"] == sha256:
                return row, 1.0
            existing = json.loads(row["frame_hashes"] or "[]")
            score = max(frame_set_similarity(frame_hashes, existing), frame_set_similarity(existing, frame_hashes))
            if score > best_score:
                best, best_score = row, score
        return (best, best_score) if best is not None and best_score >= threshold else (None, best_score)

    def record_completed(self, row: dict[str, Any], path: Path, sha256: str, frame_hashes: list[str]) -> int:
        return self._record_known(row, "completed", path, sha256, frame_hashes, None)

    def record_duplicate(self, row: dict[str, Any], duplicate_of: int, frame_hashes: list[str], sha256: str = "") -> int:
        return self._record_known(row, "duplicate", None, sha256, frame_hashes, duplicate_of)

    def _record_known(self, row: dict[str, Any], status: str, path: Path | None, sha256: str, frame_hashes: list[str], duplicate_of: int | None) -> int:
        values = (
            str(row.get("platform") or "unknown"), str(row.get("id") or ""), canonicalize_url(str(row.get("url") or "")),
            str(row.get("topic") or ""), str(row.get("title") or ""), status,
            str(Path(path).resolve()) if path else None, sha256 or None, json.dumps(frame_hashes), duplicate_of, utc_now(),
        )
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO known_items(platform,video_id,canonical_url,topic,title,status,local_path,sha256,frame_hashes,duplicate_of,recorded_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(platform,video_id) DO UPDATE SET canonical_url=excluded.canonical_url, topic=excluded.topic,
                   title=excluded.title, status=excluded.status, local_path=excluded.local_path, sha256=excluded.sha256,
                   frame_hashes=excluded.frame_hashes, duplicate_of=excluded.duplicate_of""",
                values,
            )
            item_id = conn.execute("SELECT id FROM known_items WHERE platform=? AND video_id=?", values[:2]).fetchone()[0]
        self.record_attempt(row, status, None)
        return int(item_id)

    def record_attempt(self, row: dict[str, Any], status: str, error: str | None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO attempts(platform,video_id,canonical_url,topic,status,error,attempted_at) VALUES(?,?,?,?,?,?,?)",
                (str(row.get("platform") or "unknown"), str(row.get("id") or ""), canonicalize_url(str(row.get("url") or "")), str(row.get("topic") or ""), status, error, utc_now()),
            )

    def fingerprint(self, path: Path) -> tuple[str, list[str]]:
        return sha256_file(path), extract_video_hashes(path, max_frames=8)

    def import_report(self, report_path: Path) -> dict[str, int]:
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
        imported = skipped = metadata_only = 0
        for row in report.get("downloaded") or []:
            if self.known(str(row.get("platform") or ""), str(row.get("id") or ""), str(row.get("url") or "")):
                skipped += 1
                continue
            path = Path(str(row.get("saved_to") or ""))
            if not path.is_file():
                self.record_completed(row, path, "", [])
                imported += 1
                metadata_only += 1
                continue
            digest, hashes = self.fingerprint(path)
            self.record_completed(row, path, digest, hashes)
            imported += 1
        return {"imported": imported, "already_known": skipped, "metadata_only": metadata_only}

    def stats(self) -> dict[str, int]:
        with self.connect() as conn:
            completed = conn.execute("SELECT COUNT(*) FROM known_items WHERE status='completed'").fetchone()[0]
            duplicates = conn.execute("SELECT COUNT(*) FROM known_items WHERE status='duplicate'").fetchone()[0]
            failed = conn.execute("SELECT COUNT(*) FROM attempts WHERE status='failed'").fetchone()[0]
        return {"completed": int(completed), "duplicates": int(duplicates), "failed_attempts": int(failed)}
