"""Daily YouTube discovery agent for public, permission-respecting video research."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import threading
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Protocol

from yt_dlp import YoutubeDL

from ai_clients import deepseek_metadata_judge, model_status, qwen_thumbnail_judge
from video_crawler import ytdlp_base_options, ytdlp_download
from visual_rag import best_match, hash_image_url


AGENT_DIR = Path("agent_data")
DEFAULT_DB = AGENT_DIR / "paqu_agent.sqlite3"
DEFAULT_CSV = AGENT_DIR / "candidates.csv"
DEFAULT_LIMIT = 100
DEFAULT_WINDOW_DAYS = 30


@dataclass(frozen=True)
class TopicConfig:
    key: str
    name: str
    queries: tuple[str, ...]
    strong_terms: tuple[str, ...]
    context_terms: tuple[str, ...]
    exclude_terms: tuple[str, ...]
    enabled: bool = True


TOPICS: tuple[TopicConfig, ...] = (
    TopicConfig(
        "mooring_rope_break",
        "船舶系泊绳断裂",
        ("船舶 系泊绳 断裂", "系泊缆绳 事故", "mooring rope break", "mooring line snap", "ship rope failure", "mooring accident"),
        ("系泊绳断裂", "系泊缆绳断裂", "mooring rope break", "mooring line snap", "mooring line failure", "snap back"),
        ("系泊", "缆绳", "船舶", "码头", "mooring", "ship", "vessel", "dock", "rope", "line"),
        ("编织", "打结教程", "装饰", "钓鱼", "knitting", "tutorial knot", "fishing"),
    ),
    TopicConfig(
        "rescue_rope_accident",
        "救援绳事故",
        ("救援绳 事故", "救生绳 断裂", "rescue rope accident", "rescue rope failure", "lifeline accident", "rope rescue incident"),
        ("救援绳事故", "救生绳断裂", "rescue rope accident", "rescue rope failure", "lifeline failure"),
        ("救援", "救生", "消防", "高空", "rescue", "lifeline", "firefighter", "climbing", "rope"),
        ("魔术", "玩具", "电影预告", "music", "toy", "trailer", "rope trick"),
    ),
    TopicConfig(
        "lifting_sling_test",
        "吊装带承重测试",
        ("吊装带 承重测试", "吊装带 破断试验", "lifting sling load test", "webbing sling breaking test", "lifting strap capacity test", "sling failure test"),
        ("吊装带承重测试", "吊装带破断", "lifting sling load test", "webbing sling breaking test", "sling failure test"),
        ("吊装带", "吊具", "承重", "拉力", "lifting sling", "webbing sling", "load test", "proof test", "breaking strength"),
        ("健身", "行李带", "相机带", "workout", "luggage strap", "camera strap"),
    ),
)


@dataclass
class RelevanceResult:
    relevant: bool
    confidence: float
    topic: str
    level: str
    matched_terms: list[str]
    excluded_terms: list[str]
    reason: str
    score: int


class RelevanceJudge(Protocol):
    def judge(self, topic: TopicConfig, metadata: dict[str, Any]) -> RelevanceResult: ...


class KeywordGenerator(Protocol):
    def generate(self, topic: TopicConfig) -> list[str]: ...


class PresetKeywordGenerator:
    def generate(self, topic: TopicConfig) -> list[str]:
        return list(dict.fromkeys(q.strip() for q in topic.queries if q.strip()))


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


class RuleRelevanceJudge:
    """Explainable v1 judge. A future AI judge can implement the same protocol."""

    def judge(self, topic: TopicConfig, metadata: dict[str, Any]) -> RelevanceResult:
        tags = metadata.get("tags") or []
        subtitles = metadata.get("subtitle_text") or ""
        haystack = normalize_text(" ".join([
            str(metadata.get("title") or ""),
            str(metadata.get("description") or ""),
            " ".join(str(tag) for tag in tags),
            str(metadata.get("channel") or metadata.get("uploader") or ""),
            str(subtitles),
        ]))
        strong = [term for term in topic.strong_terms if normalize_text(term) in haystack]
        context = [term for term in topic.context_terms if normalize_text(term) in haystack]
        excluded = [term for term in topic.exclude_terms if normalize_text(term) in haystack]
        score = min(100, len(strong) * 45 + min(len(context), 4) * 12 - len(excluded) * 35)
        score = max(0, score)
        if excluded and not strong:
            level = "不相关"
        elif strong or score >= 55:
            level = "高相关"
        elif len(context) >= 2 and score >= 24:
            level = "待审核"
        else:
            level = "不相关"
        matched = list(dict.fromkeys(strong + context))
        if level == "高相关":
            reason = f"命中强相关表达：{', '.join(strong or matched)}。"
        elif level == "待审核":
            reason = f"命中主题上下文：{', '.join(matched)}，但缺少明确事故或测试表达。"
        else:
            reason = "未找到足够的主题证据。"
        if excluded:
            reason += f" 排除词：{', '.join(excluded)}。"
        return RelevanceResult(level != "不相关", round(score / 100, 2), topic.name, level, matched, excluded, reason, score)


class HybridAIJudge:
    """DeepSeek metadata judge + Qwen thumbnail review, with deterministic rule fallback."""

    def __init__(self) -> None:
        self.fallback = RuleRelevanceJudge()

    def judge(self, topic: TopicConfig, metadata: dict[str, Any]) -> RelevanceResult:
        baseline = self.fallback.judge(topic, metadata)
        try:
            text_result = deepseek_metadata_judge(topic.name, metadata)
        except Exception as exc:
            baseline.reason += f" DeepSeek 不可用，已回退本地规则：{str(exc)[:160]}"
            return baseline
        if not text_result:
            text_result = {
                "level": baseline.level, "confidence": baseline.confidence, "relevant": baseline.relevant,
                "reason": "未配置 DeepSeek，使用本地文字规则。", "matched_terms": baseline.matched_terms,
                "excluded_terms": baseline.excluded_terms,
            }
        level = str(text_result.get("level") or "待审核")
        if level not in {"高相关", "待审核", "不相关"}:
            level = "待审核"
        confidence = max(0.0, min(float(text_result.get("confidence", 0.5)), 1.0))
        relevant = bool(text_result.get("relevant", level != "不相关"))
        reason = "DeepSeek：" + str(text_result.get("reason") or "已完成文字元数据判断。")
        matched = [str(x) for x in text_result.get("matched_terms") or []]
        excluded = [str(x) for x in text_result.get("excluded_terms") or []]
        # Only spend a visual call on candidates that survive text screening.
        if relevant and metadata.get("thumbnail"):
            try:
                visual = qwen_thumbnail_judge(topic.name, str(metadata["thumbnail"]), metadata)
                if visual:
                    visual_level = str(visual.get("level") or "待审核")
                    if visual_level in {"高相关", "待审核", "不相关"}:
                        level = visual_level
                    confidence = min(confidence, max(0.0, min(float(visual.get("confidence", confidence)), 1.0)))
                    relevant = bool(visual.get("relevant", level != "不相关"))
                    evidence = ", ".join(str(x) for x in visual.get("visual_evidence") or [])
                    reason += " Qwen视觉：" + str(visual.get("reason") or "已检查缩略图。")
                    if evidence:
                        reason += f" 视觉证据：{evidence}。"
            except Exception as exc:
                reason += f" Qwen视觉不可用，保留文字判断：{str(exc)[:160]}"
        score = int(round(confidence * 100))
        return RelevanceResult(relevant, confidence, topic.name, level, matched, excluded, reason, score)


def canonical_youtube_url(video_id: str, webpage_url: str | None = None) -> str:
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    parsed = urllib.parse.urlsplit(str(webpage_url or ""))
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ""))


def parse_upload_date(value: Any) -> str | None:
    text = re.sub(r"\D", "", str(value or ""))[:8]
    if len(text) != 8:
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def metadata_publish_date(item: dict[str, Any]) -> str | None:
    parsed = parse_upload_date(item.get("upload_date") or item.get("release_date"))
    if parsed:
        return parsed
    timestamp = item.get("timestamp") or item.get("release_timestamp")
    try:
        return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).date().isoformat() if timestamp else None
    except (OSError, TypeError, ValueError):
        return None


def normalized_title(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", normalize_text(value))


class AgentStore:
    def __init__(self, db_path: Path = DEFAULT_DB):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_date TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    candidate_count INTEGER NOT NULL DEFAULT 0,
                    new_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_runs_date ON runs(run_date, status);
                CREATE TABLE IF NOT EXISTS topics (
                    topic_key TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    queries TEXT NOT NULL,
                    strong_terms TEXT NOT NULL,
                    context_terms TEXT NOT NULL,
                    exclude_terms TEXT NOT NULL,
                    window_days INTEGER NOT NULL DEFAULT 30,
                    candidate_quota INTEGER NOT NULL DEFAULT 33
                );
                CREATE TABLE IF NOT EXISTS candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT NOT NULL UNIQUE,
                    canonical_url TEXT NOT NULL UNIQUE,
                    topic_key TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    title TEXT NOT NULL,
                    normalized_title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    channel TEXT NOT NULL DEFAULT '',
                    published_at TEXT,
                    thumbnail TEXT NOT NULL DEFAULT '',
                    search_query TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    relevance TEXT NOT NULL,
                    matched_terms TEXT NOT NULL DEFAULT '[]',
                    excluded_terms TEXT NOT NULL DEFAULT '[]',
                    reason TEXT NOT NULL,
                    review_status TEXT NOT NULL DEFAULT 'pending',
                    download_status TEXT NOT NULL DEFAULT 'not_requested',
                    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                    total_bytes INTEGER NOT NULL DEFAULT 0,
                    rag_similarity REAL,
                    rag_sample_id INTEGER,
                    saved_to TEXT,
                    file_sha256 TEXT,
                    duplicate_of INTEGER REFERENCES candidates(id),
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    run_id INTEGER REFERENCES runs(id),
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_candidates_filters ON candidates(topic_key, relevance, review_status, first_seen_at);
                CREATE TABLE IF NOT EXISTS search_terms (
                    run_id INTEGER NOT NULL REFERENCES runs(id),
                    topic_key TEXT NOT NULL,
                    query TEXT NOT NULL,
                    result_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    PRIMARY KEY (run_id, topic_key, query)
                );
                CREATE TABLE IF NOT EXISTS rag_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    frame_hashes TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
            candidate_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(candidates)").fetchall()}
            migrations = {
                "downloaded_bytes": "INTEGER NOT NULL DEFAULT 0",
                "total_bytes": "INTEGER NOT NULL DEFAULT 0",
                "rag_similarity": "REAL",
                "rag_sample_id": "INTEGER",
            }
            for column, declaration in migrations.items():
                if column not in candidate_columns:
                    conn.execute(f"ALTER TABLE candidates ADD COLUMN {column} {declaration}")
            conn.executemany(
                """INSERT OR REPLACE INTO topics(topic_key, name, enabled, queries, strong_terms, context_terms, exclude_terms, window_days, candidate_quota)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 30, 33)""",
                [
                    (
                        topic.key, topic.name, int(topic.enabled), json.dumps(topic.queries, ensure_ascii=False),
                        json.dumps(topic.strong_terms, ensure_ascii=False), json.dumps(topic.context_terms, ensure_ascii=False),
                        json.dumps(topic.exclude_terms, ensure_ascii=False),
                    )
                    for topic in TOPICS
                ],
            )

    def has_successful_run(self, run_date: str) -> bool:
        with self.connect() as conn:
            return conn.execute("SELECT 1 FROM runs WHERE run_date=? AND status='done' LIMIT 1", (run_date,)).fetchone() is not None

    def start_run(self, run_date: str) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            cur = conn.execute("INSERT INTO runs(run_date, started_at, status) VALUES (?, ?, 'running')", (run_date, now))
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, candidate_count: int, new_count: int, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE runs SET finished_at=?, status=?, candidate_count=?, new_count=?, error=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), status, candidate_count, new_count, error, run_id),
            )

    def record_search(self, run_id: int, topic_key: str, query: str, count: int, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO search_terms VALUES (?, ?, ?, ?, ?)", (run_id, topic_key, query, count, error))

    def upsert_candidate(self, run_id: int, topic: TopicConfig, query: str, item: dict[str, Any], result: RelevanceResult) -> tuple[int, bool]:
        video_id = str(item.get("id") or "").strip()
        if not video_id:
            raise ValueError("候选缺少 YouTube 视频 ID。")
        url = canonical_youtube_url(video_id, item.get("webpage_url") or item.get("url"))
        now = datetime.now(timezone.utc).isoformat()
        values = (
            video_id, url, topic.key, topic.name, str(item.get("title") or video_id), normalized_title(str(item.get("title") or "")),
            str(item.get("description") or ""), str(item.get("channel") or item.get("uploader") or ""), metadata_publish_date(item),
            str(item.get("thumbnail") or ""), query, result.score, result.level, json.dumps(result.matched_terms, ensure_ascii=False),
            json.dumps(result.excluded_terms, ensure_ascii=False), result.reason, now, now, run_id,
        )
        with self.connect() as conn:
            existing = conn.execute("SELECT id FROM candidates WHERE video_id=? OR canonical_url=?", (video_id, url)).fetchone()
            if existing:
                conn.execute("UPDATE candidates SET last_seen_at=?, run_id=? WHERE id=?", (now, run_id, existing["id"]))
                return int(existing["id"]), False
            cur = conn.execute("""
                INSERT INTO candidates(video_id, canonical_url, topic_key, topic, title, normalized_title, description, channel,
                    published_at, thumbnail, search_query, score, relevance, matched_terms, excluded_terms, reason,
                    first_seen_at, last_seen_at, run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, values)
            candidate_id = int(cur.lastrowid)
            duplicate = self._similar_candidate(conn, candidate_id, values[5])
            if duplicate:
                conn.execute("UPDATE candidates SET duplicate_of=? WHERE id=?", (duplicate, candidate_id))
            return candidate_id, True

    @staticmethod
    def _similar_candidate(conn: sqlite3.Connection, candidate_id: int, title: str) -> int | None:
        if len(title) < 8:
            return None
        rows = conn.execute("SELECT id, normalized_title FROM candidates WHERE id<>? ORDER BY id DESC LIMIT 500", (candidate_id,)).fetchall()
        for row in rows:
            other = str(row["normalized_title"] or "")
            if other and SequenceMatcher(None, title, other).ratio() >= 0.92:
                return int(row["id"])
        return None

    def list_candidates(self, filters: dict[str, str] | None = None, limit: int = 300) -> list[dict[str, Any]]:
        filters = filters or {}
        clauses: list[str] = []
        params: list[Any] = []
        if not filters.get("review"):
            clauses.append("review_status<>'deleted'")
        mapping = {"topic": "topic_key", "relevance": "relevance", "review": "review_status", "download": "download_status"}
        for key, column in mapping.items():
            value = filters.get(key)
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        if filters.get("date"):
            clauses.append("substr(first_seen_at, 1, 10)=?")
            params.append(filters["date"])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(int(limit), 1000)))
        with self.connect() as conn:
            rows = conn.execute(f"SELECT * FROM candidates{where} ORDER BY id DESC LIMIT ?", params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["matched_terms"] = json.loads(item["matched_terms"] or "[]")
            item["excluded_terms"] = json.loads(item["excluded_terms"] or "[]")
            result.append(item)
        return result

    def list_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def candidate_exists(self, video_id: str) -> bool:
        with self.connect() as conn:
            return conn.execute("SELECT 1 FROM candidates WHERE video_id=? LIMIT 1", (str(video_id),)).fetchone() is not None

    def add_rag_sample(self, name: str, file_path: Path, frame_hashes: list[str]) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO rag_samples(name,file_path,frame_hashes,created_at) VALUES (?,?,?,?)",
                (name, str(file_path), json.dumps(frame_hashes), datetime.now(timezone.utc).isoformat()),
            )
            return int(cur.lastrowid)

    def list_rag_samples(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT id,name,file_path,frame_hashes,created_at FROM rag_samples ORDER BY id DESC").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["frame_count"] = len(json.loads(item.pop("frame_hashes") or "[]"))
            result.append(item)
        return result

    def get_rag_sample(self, sample_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id,name,file_path,frame_hashes,created_at FROM rag_samples WHERE id=?",
                (int(sample_id),),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["frame_count"] = len(json.loads(item.pop("frame_hashes") or "[]"))
        return item

    def rename_rag_sample(self, sample_id: int, name: str) -> bool:
        clean_name = str(name).strip()
        if not clean_name:
            raise ValueError("样本名称不能为空。")
        with self.connect() as conn:
            cur = conn.execute("UPDATE rag_samples SET name=? WHERE id=?", (clean_name[:200], int(sample_id)))
            return cur.rowcount > 0

    def delete_rag_sample(self, sample_id: int) -> bool:
        with self.connect() as conn:
            conn.execute("UPDATE candidates SET rag_sample_id=NULL WHERE rag_sample_id=?", (int(sample_id),))
            cur = conn.execute("DELETE FROM rag_samples WHERE id=?", (int(sample_id),))
            return cur.rowcount > 0

    def rag_hashes(self) -> list[tuple[int, list[str]]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT id,frame_hashes FROM rag_samples").fetchall()
        return [(int(row["id"]), json.loads(row["frame_hashes"] or "[]")) for row in rows]

    def set_rag_match(self, candidate_id: int, sample_id: int | None, similarity: float | None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE candidates SET rag_sample_id=?, rag_similarity=? WHERE id=?",
                (sample_id, similarity, int(candidate_id)),
            )

    def get_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM candidates WHERE id=?", (int(candidate_id),)).fetchone()
        return dict(row) if row else None

    def update_review(self, ids: Iterable[int], status: str) -> int:
        if status not in {"pending", "kept", "excluded", "deleted"}:
            raise ValueError("审核状态无效。")
        clean_ids = sorted({int(value) for value in ids})
        if not clean_ids:
            return 0
        placeholders = ",".join("?" for _ in clean_ids)
        with self.connect() as conn:
            cur = conn.execute(f"UPDATE candidates SET review_status=? WHERE id IN ({placeholders})", [status, *clean_ids])
            return cur.rowcount

    def keep_for_download(self, ids: Iterable[int]) -> int:
        clean_ids = sorted({int(value) for value in ids})
        if not clean_ids:
            return 0
        placeholders = ",".join("?" for _ in clean_ids)
        with self.connect() as conn:
            cur = conn.execute(
                f"UPDATE candidates SET review_status='kept' "
                f"WHERE id IN ({placeholders}) AND review_status<>'deleted'",
                clean_ids,
            )
            return cur.rowcount

    def get_candidates_for_download(self, ids: Iterable[int]) -> list[dict[str, Any]]:
        clean_ids = sorted({int(value) for value in ids})
        if not clean_ids:
            return []
        placeholders = ",".join("?" for _ in clean_ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM candidates WHERE id IN ({placeholders}) AND review_status='kept' AND download_status IN ('not_requested','failed')",
                clean_ids,
            ).fetchall()
            conn.execute(
                f"UPDATE candidates SET download_status='queued', downloaded_bytes=0, total_bytes=0, error=NULL "
                f"WHERE id IN ({placeholders}) AND review_status='kept' AND download_status IN ('not_requested','failed')",
                clean_ids,
            )
        return [dict(row) for row in rows]

    def set_download_progress(self, candidate_id: int, downloaded: int, total: int) -> None:
        downloaded = max(0, int(downloaded))
        total = max(0, int(total))
        with self.connect() as conn:
            conn.execute(
                "UPDATE candidates SET download_status='downloading', downloaded_bytes=?, total_bytes=? WHERE id=?",
                (min(downloaded, total) if total else downloaded, total, candidate_id),
            )

    def set_download_result(self, candidate_id: int, status: str, saved_to: str | None = None, digest: str | None = None, error: str | None = None) -> None:
        duplicate_of = None
        with self.connect() as conn:
            if digest:
                duplicate = conn.execute("SELECT id FROM candidates WHERE file_sha256=? AND id<>? LIMIT 1", (digest, candidate_id)).fetchone()
                duplicate_of = int(duplicate["id"]) if duplicate else None
            conn.execute(
                "UPDATE candidates SET download_status=?, downloaded_bytes=CASE WHEN ?='done' AND total_bytes>0 THEN total_bytes ELSE downloaded_bytes END, saved_to=?, file_sha256=?, duplicate_of=COALESCE(?, duplicate_of), error=? WHERE id=?",
                (status, status, saved_to, digest, duplicate_of, error, candidate_id),
            )

    def export_csv(self, csv_path: Path = DEFAULT_CSV) -> Path:
        csv_path = Path(csv_path)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        rows = self.list_candidates(limit=100000)
        fields = ["id", "video_id", "topic", "title", "canonical_url", "published_at", "channel", "search_query", "score", "relevance", "reason", "review_status", "download_status", "saved_to", "duplicate_of", "first_seen_at", "error"]
        with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        return csv_path


def search_youtube(query: str, limit: int, window_days: int, cookies_file: Path | None = None, timeout: float = 20.0) -> list[dict[str, Any]]:
    options = {
        **ytdlp_base_options(timeout, cookies_file),
        # Flat YouTube search entries omit upload dates, so full public metadata is required.
        "extract_flat": False,
        "playlistend": max(1, limit),
        "dateafter": (date.today() - timedelta(days=window_days)).strftime("%Y%m%d"),
    }
    search_url = (
        "https://www.youtube.com/results?search_query="
        + urllib.parse.quote_plus(query)
        + "&sp=EgIIBA%253D%253D"
    )
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(search_url, download=False)
    return [entry for entry in (info or {}).get("entries") or [] if entry and entry.get("id")]


def is_recent(item: dict[str, Any], window_days: int, today: date | None = None) -> bool:
    published = metadata_publish_date(item)
    if not published:
        return True
    cutoff = (today or date.today()) - timedelta(days=window_days)
    return date.fromisoformat(published) >= cutoff


def run_discovery(
    store: AgentStore,
    limit: int = DEFAULT_LIMIT,
    window_days: int = DEFAULT_WINDOW_DAYS,
    force: bool = False,
    cookies_file: Path | None = None,
    searcher: Callable[[str, int, int, Path | None], list[dict[str, Any]]] = search_youtube,
    keyword_generator: KeywordGenerator | None = None,
    judge: RelevanceJudge | None = None,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), DEFAULT_LIMIT))
    run_date = date.today().isoformat()
    if not force and store.has_successful_run(run_date):
        return {"status": "skipped", "reason": "今天已经成功运行过。", "run_date": run_date}
    generator = keyword_generator or PresetKeywordGenerator()
    status = model_status()
    judge = judge or (HybridAIJudge() if status["deepseek"]["configured"] or status["qwen"]["configured"] else RuleRelevanceJudge())
    topics = [topic for topic in TOPICS if topic.enabled]
    run_id = store.start_run(run_date)
    collected: list[tuple[TopicConfig, str, dict[str, Any]]] = []
    seen: set[str] = set()
    errors: list[str] = []
    raw_limit = min(400, max(limit, limit * 4))
    base_quota = max(1, raw_limit // len(topics))
    try:
        pools: dict[str, list[tuple[str, dict[str, Any]]]] = {topic.key: [] for topic in topics}
        for topic in topics:
            queries = generator.generate(topic)
            per_query = max(4, (base_quota * 2 + len(queries) - 1) // len(queries))
            for query in queries:
                try:
                    items = searcher(query, per_query, window_days, cookies_file)
                    items = [item for item in items if is_recent(item, window_days)]
                    store.record_search(run_id, topic.key, query, len(items))
                    pools[topic.key].extend((query, item) for item in items)
                except Exception as exc:
                    message = f"{topic.name}/{query}: {exc}"
                    errors.append(message)
                    store.record_search(run_id, topic.key, query, 0, str(exc))
        if errors and not any(pools.values()):
            raise RuntimeError("所有 YouTube 搜索均失败：" + errors[0])
        # Fair first pass, then fill shortages dynamically from any remaining pool.
        for topic in topics:
            for query, item in pools[topic.key]:
                video_id = str(item.get("id") or "")
                if video_id and video_id not in seen and sum(1 for t, _, _ in collected if t.key == topic.key) < base_quota:
                    collected.append((topic, query, item)); seen.add(video_id)
        for topic in topics:
            for query, item in pools[topic.key]:
                if len(collected) >= raw_limit:
                    break
                video_id = str(item.get("id") or "")
                if video_id and video_id not in seen:
                    collected.append((topic, query, item)); seen.add(video_id)
        collected = collected[:raw_limit]
        new_count = 0
        high_count = 0
        rag_samples = store.rag_hashes()
        for topic, query, item in collected:
            if high_count >= limit:
                break
            video_id = str(item.get("id") or "")
            if not video_id or store.candidate_exists(video_id):
                continue
            result = judge.judge(topic, item)
            if result.level != "高相关":
                continue
            rag_sample_id = None
            rag_similarity = None
            if rag_samples:
                thumbnail = str(item.get("thumbnail") or "")
                if not thumbnail:
                    continue
                try:
                    rag_sample_id, rag_similarity = best_match(hash_image_url(thumbnail), rag_samples)
                except Exception as exc:
                    errors.append(f"视觉RAG/{video_id}: {exc}")
                    continue
                if rag_similarity < 0.70:
                    continue
            candidate_id, created = store.upsert_candidate(run_id, topic, query, item, result)
            store.set_rag_match(candidate_id, rag_sample_id, rag_similarity)
            new_count += int(created)
            high_count += int(created)
        if high_count < limit:
            errors.append(f"公开来源本次仅找到 {high_count} 个全新高相关视频，未达到目标 {limit} 个。")
        store.finish_run(run_id, "done", high_count, new_count, "\n".join(errors) or None)
        csv_path = store.export_csv()
        return {"status": "done", "run_id": run_id, "candidate_count": high_count, "new_count": new_count, "errors": errors, "csv": str(csv_path)}
    except Exception as exc:
        store.finish_run(run_id, "failed", len(collected), 0, str(exc))
        raise


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_candidates(store: AgentStore, ids: Iterable[int], cookies_file: Path | None = None, root: Path = Path("downloads")) -> list[int]:
    rows = store.get_candidates_for_download(ids)
    queued = [int(row["id"]) for row in rows]

    def worker() -> None:
        for row in rows:
            candidate_id = int(row["id"])
            try:
                store.set_download_result(candidate_id, "downloading")
                target_dir = root / str(row["topic"])
                last_update = 0.0

                def report_progress(downloaded: int, total: int) -> None:
                    nonlocal last_update
                    now = time.monotonic()
                    if now - last_update >= 0.5 or (total > 0 and downloaded >= total):
                        store.set_download_progress(candidate_id, downloaded, total)
                        last_update = now

                saved = ytdlp_download(
                    str(row["canonical_url"]), target_dir, None, 30.0, cookies_file,
                    progress=report_progress,
                )
                digest = sha256_file(saved)
                store.set_download_result(candidate_id, "done", str(saved), digest)
            except Exception as exc:
                store.set_download_result(candidate_id, "failed", error=str(exc))
            finally:
                store.export_csv()

    if rows:
        threading.Thread(target=worker, name="paqu-agent-download", daemon=True).start()
    return queued


def topic_payload() -> list[dict[str, Any]]:
    return [asdict(topic) for topic in TOPICS]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the paqu daily YouTube discovery agent.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--force", action="store_true", help="Run even if today already completed successfully.")
    parser.add_argument("--cookies-file", type=Path)
    parser.add_argument("--init-db", action="store_true", help="Initialize SQLite and exit.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    store = AgentStore()
    if args.init_db:
        print(json.dumps({"status": "ready", "database": str(store.db_path)}, ensure_ascii=False))
        return 0
    result = run_discovery(store, args.limit, args.window_days, args.force, args.cookies_file)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
