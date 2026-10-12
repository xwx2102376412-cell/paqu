"""One-off, auditable multi-platform similarity trial for industrial accident videos."""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL

from video_crawler import ytdlp_base_options, ytdlp_download
from visual_rag import best_match, extract_video_hashes, hash_image_url
from download_history import DownloadHistory


@dataclass(frozen=True)
class TrialTopic:
    key: str
    name: str
    queries: tuple[str, ...]
    strong_terms: tuple[str, ...]
    context_terms: tuple[str, ...]
    exclude_terms: tuple[str, ...]


TOPICS = (
    TrialTopic(
        "mooring_line_break",
        "系泊缆绳断裂事故",
        ("mooring line snap accident", "mooring rope break accident", "mooring snap back accident", "系泊缆绳 断裂 事故"),
        ("mooring line snap", "mooring rope break", "snap back", "snapback", "系泊缆绳断裂", "系泊绳断裂"),
        ("mooring", "ship", "vessel", "dock", "rope", "line", "系泊", "缆绳", "船舶", "码头", "事故"),
        ("knot", "fishing", "tutorial", "编绳", "打结", "钓鱼", "测试仪"),
    ),
    TrialTopic(
        "lifting_sling_accident",
        "吊装带或吊装事故",
        ("webbing sling failure accident", "lifting sling snaps load drops", "rigging accident crane load", "吊装带 断裂 事故", "吊索 断裂 吊装事故"),
        ("webbing sling failure", "lifting sling snaps", "sling failure", "rigging accident", "吊装带断裂", "吊索断裂", "吊装事故"),
        ("crane", "lifting", "rigging", "sling", "load", "drop", "吊车", "起重", "吊装", "吊带", "吊索", "坠落", "事故"),
        ("workout", "fashion", "camera strap", "product review", "健身", "背带", "相机带", "产品展示"),
    ),
)


SEED_URLS: dict[str, list[str]] = {
    "mooring_line_break": [
        "https://www.bilibili.com/video/BV13u4m1u7or/",
        "https://www.bilibili.com/video/BV16z421X7uS/",
    ],
    "lifting_sling_accident": [
        "https://www.bilibili.com/video/BV1pXrdYgETG/",
        "https://www.bilibili.com/video/av967609275/",
        "https://www.bilibili.com/video/BV1fz4y197oU/",
        "https://www.bilibili.com/video/BV16m4y1S77i/",
        "https://www.douyin.com/video/7290006900194790693",
        "https://vimeo.com/987065460",
        "https://vimeo.com/987063648",
    ],
}


COOKIE_FILES = {
    "youtube": Path("www.youtube.com_cookies.txt"),
    "bilibili": Path("www.bilibili.com_cookies.txt"),
    "douyin": Path("www.douyin.com_cookies.txt"),
    "tiktok": Path("www.tiktok.com_cookies.txt"),
    "vimeo": Path("www.vimeo.com_cookies.txt"),
}


def platform_for_url(url: str) -> str:
    host = urllib.parse.urlsplit(url).netloc.lower()
    for name in COOKIE_FILES:
        if name in host or (name == "youtube" and "youtu.be" in host):
            return name
    return "unknown"


def normalized(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def text_score(topic: TrialTopic, item: dict[str, Any]) -> tuple[float, list[str]]:
    haystack = normalized(" ".join(str(item.get(k) or "") for k in ("title", "description", "uploader", "channel")))
    strong = [x for x in topic.strong_terms if normalized(x) in haystack]
    context = [x for x in topic.context_terms if normalized(x) in haystack]
    excluded = [x for x in topic.exclude_terms if normalized(x) in haystack]
    score = min(1.0, len(strong) * 0.42 + min(5, len(context)) * 0.11) - len(excluded) * 0.35
    return max(0.0, score), list(dict.fromkeys(strong + context))


def search_youtube(topic: TrialTopic, per_query: int, errors: list[dict[str, str]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    opts = {**ytdlp_base_options(25.0, COOKIE_FILES["youtube"]), "extract_flat": False, "playlistend": per_query}
    for query in topic.queries:
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch{per_query}:{query}", download=False)
            for item in (info or {}).get("entries") or []:
                if item:
                    item["source_platform"] = "youtube"
                    found.append(item)
        except Exception as exc:
            errors.append({"platform": "youtube", "topic": topic.key, "query": query, "error": str(exc)[:500]})
    return found


def search_bilibili(topic: TrialTopic, per_query: int, errors: list[dict[str, str]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    opts = {**ytdlp_base_options(25.0, COOKIE_FILES["bilibili"]), "extract_flat": True, "playlistend": per_query}
    for query in topic.queries:
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"bilisearch{per_query}:{query}", download=False)
            for item in (info or {}).get("entries") or []:
                if item:
                    item["source_platform"] = "bilibili"
                    found.append(item)
        except Exception as exc:
            errors.append({"platform": "bilibili", "topic": topic.key, "query": query, "error": str(exc)[:500]})
    return found


def extract_seed(url: str, errors: list[dict[str, str]], topic_key: str) -> dict[str, Any] | None:
    platform = platform_for_url(url)
    cookie = COOKIE_FILES.get(platform)
    try:
        with YoutubeDL(ytdlp_base_options(25.0, cookie)) as ydl:
            item = ydl.extract_info(url, download=False)
        if item:
            item["source_platform"] = platform
        return item
    except Exception as exc:
        errors.append({"platform": platform, "topic": topic_key, "query": url, "error": str(exc)[:500]})
        return None


def reference_hashes(reference_dir: Path) -> dict[str, list[tuple[int, list[str]]]]:
    grouped = {topic.key: [] for topic in TOPICS}
    next_id = 1
    for path in sorted(reference_dir.glob("*.mp4")):
        is_mooring = bool(re.fullmatch(r"(?:[1-9]|1[0-7])\.mp4", path.name, re.I))
        key = "mooring_line_break" if is_mooring else "lifting_sling_accident"
        grouped[key].append((next_id, extract_video_hashes(path, max_frames=8)))
        next_id += 1
    return grouped


def canonical_url(item: dict[str, Any]) -> str:
    return str(item.get("webpage_url") or item.get("original_url") or item.get("url") or "")


def rank_candidates(topic: TrialTopic, items: list[dict[str, Any]], refs: list[tuple[int, list[str]]], errors: list[dict[str, str]]) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        url = canonical_url(item)
        identity = str(item.get("id") or url)
        if not url or identity in seen:
            continue
        seen.add(identity)
        score, terms = text_score(topic, item)
        if score < 0.22:
            continue
        visual = 0.0
        sample_id = None
        thumb = str(item.get("thumbnail") or "")
        if thumb:
            try:
                sample_id, visual = best_match(hash_image_url(thumb), refs)
            except Exception as exc:
                errors.append({"platform": str(item.get("source_platform") or "unknown"), "topic": topic.key, "query": url, "error": f"thumbnail: {str(exc)[:400]}"})
        combined = round(score * 0.72 + visual * 0.28, 4)
        ranked.append({
            "topic": topic.key,
            "topic_name": topic.name,
            "platform": str(item.get("source_platform") or platform_for_url(url)),
            "id": identity,
            "title": str(item.get("title") or ""),
            "url": url,
            "text_score": round(score, 4),
            "visual_similarity": round(visual, 4),
            "combined_score": combined,
            "matched_terms": terms,
            "reference_sample_id": sample_id,
            "duration": item.get("duration"),
            "uploader": str(item.get("uploader") or item.get("channel") or ""),
        })
    return sorted(ranked, key=lambda x: (x["combined_score"], x["visual_similarity"]), reverse=True)


def eligible_candidates(rows: list[dict[str, Any]], minimum_score: float) -> list[dict[str, Any]]:
    return [row for row in rows if float(row.get("combined_score") or 0.0) >= minimum_score]


def download_top(rows: list[dict[str, Any]], output_dir: Path, limit: int, errors: list[dict[str, str]], history: DownloadHistory) -> list[dict[str, Any]]:
    completed: list[dict[str, Any]] = []
    staging_dir = output_dir / "_候选验证"
    for row in rows:
        if len(completed) >= limit:
            break
        platform = row["platform"]
        known = history.known(platform, str(row.get("id") or ""), str(row.get("url") or ""))
        if known:
            history.record_attempt(row, "skipped", f"already known: {known['status']} #{known['id']}")
            continue
        try:
            saved = ytdlp_download(row["url"], staging_dir, None, 45.0, COOKIE_FILES.get(platform))
            digest, hashes = history.fingerprint(saved)
            duplicate, duplicate_score = history.find_content_duplicate(digest, hashes)
            if duplicate:
                history.record_duplicate(row, int(duplicate["id"]), hashes, digest)
                Path(saved).unlink()
                errors.append({"platform": platform, "topic": row["topic"], "query": row["url"], "error": f"duplicate: history #{duplicate['id']} similarity={duplicate_score:.4f}"})
                continue
            target_dir = output_dir / row["topic_name"]
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / Path(saved).name
            if target.exists():
                target = target_dir / f"{Path(saved).stem}-{row['id']}{Path(saved).suffix}"
            saved = Path(shutil.move(str(saved), str(target)))
            history.record_completed(row, saved, digest, hashes)
            row = dict(row)
            row["saved_to"] = str(saved.resolve())
            completed.append(row)
        except Exception as exc:
            history.record_attempt(row, "failed", str(exc)[:1000])
            errors.append({"platform": platform, "topic": row["topic"], "query": row["url"], "error": f"download: {str(exc)[:500]}"})
    return completed


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["topic_name", "platform", "title", "url", "combined_score", "text_score", "visual_similarity", "matched_terms", "duration", "uploader", "saved_to"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            cooked = dict(row)
            cooked["matched_terms"] = ", ".join(row.get("matched_terms") or [])
            writer.writerow(cooked)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference-dir", type=Path, default=Path("参考视频"))
    parser.add_argument("--output-dir", type=Path, default=Path("爬取视频"))
    parser.add_argument("--limit-per-topic", type=int, default=20)
    parser.add_argument("--limit-mooring", type=int)
    parser.add_argument("--limit-lifting", type=int)
    parser.add_argument("--search-per-query", type=int, default=25)
    parser.add_argument("--min-score", type=float, default=0.50)
    args = parser.parse_args()
    errors: list[dict[str, str]] = []
    history = DownloadHistory()
    refs = reference_hashes(args.reference_dir)
    all_downloaded: list[dict[str, Any]] = []
    ranked_by_topic: dict[str, list[dict[str, Any]]] = {}
    for topic in TOPICS:
        items = search_youtube(topic, args.search_per_query, errors)
        items.extend(search_bilibili(topic, min(8, args.search_per_query), errors))
        for url in SEED_URLS.get(topic.key, []):
            seed = extract_seed(url, errors, topic.key)
            if seed:
                items.append(seed)
        ranked = rank_candidates(topic, items, refs[topic.key], errors)
        ranked_by_topic[topic.key] = ranked
        eligible = eligible_candidates(ranked, max(0.0, min(1.0, args.min_score)))
        topic_limit = args.limit_per_topic
        if topic.key == "mooring_line_break" and args.limit_mooring is not None:
            topic_limit = max(0, args.limit_mooring)
        elif topic.key == "lifting_sling_accident" and args.limit_lifting is not None:
            topic_limit = max(0, args.limit_lifting)
        all_downloaded.extend(download_top(eligible, args.output_dir, topic_limit, errors, history))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.output_dir / "试跑结果.csv", all_downloaded)
    report = {
        "status": "done",
        "downloaded_count": len(all_downloaded),
        "downloaded_by_topic": {topic.key: sum(1 for row in all_downloaded if row["topic"] == topic.key) for topic in TOPICS},
        "searched_platforms": list(COOKIE_FILES),
        "limit_per_topic": args.limit_per_topic,
        "minimum_score": args.min_score,
        "platform_note": {
            "youtube": "yt-dlp native search",
            "bilibili": "yt-dlp native search plus indexed seed URLs",
            "douyin": "indexed seed URLs; yt-dlp has no native search extractor",
            "tiktok": "search attempted but no discoverable video URL in this run",
            "vimeo": "indexed seed URLs; search page may reject automation",
        },
        "keywords": {topic.key: {"queries": topic.queries, "strong_terms": topic.strong_terms, "context_terms": topic.context_terms, "exclude_terms": topic.exclude_terms} for topic in TOPICS},
        "downloaded": all_downloaded,
        "ranked_preview": {key: rows[:20] for key, rows in ranked_by_topic.items()},
        "errors": errors,
        "history": history.stats(),
    }
    report_path = args.output_dir / "试跑报告.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "done", "report": str(report_path), "csv": str(args.output_dir / "试跑结果.csv"), "counts": report["downloaded_by_topic"], "errors": len(errors)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
