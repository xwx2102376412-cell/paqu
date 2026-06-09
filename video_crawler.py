#!/usr/bin/env python3
"""
Find publicly exposed video URLs on a web page, with optional direct-file download.

This tool intentionally does not bypass logins, paywalls, DRM, CAPTCHAs, or other
access controls. Use it only where you have permission to crawl and download.
"""

from __future__ import annotations

import argparse
import hashlib
import html.parser
import json
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.robotparser
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from scrapling.fetchers import Fetcher
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError


DEFAULT_USER_AGENT = "paqu-video-crawler/0.1 (+respectful-public-video-discovery)"
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv", ".m3u8", ".mpd")
VIDEO_MIME_PREFIXES = ("video/", "application/vnd.apple.mpegurl", "application/dash+xml")
URL_PATTERN = re.compile(
    r"""(?P<url>(?:https?://|//|/|\.{1,2}/)[^"' <>()]+?(?:\.mp4|\.webm|\.mov|\.m4v|\.avi|\.mkv|\.m3u8|\.mpd)(?:\?[^"' <>()]*)?)""",
    re.IGNORECASE,
)


class DownloadCancelled(Exception):
    """Raised when a user cancels an in-progress download."""


@dataclass(frozen=True)
class VideoCandidate:
    url: str
    source: str
    mime_type: str | None = None


@dataclass(frozen=True)
class VideoQuality:
    label: str
    url: str
    kind: str
    format_id: str | None = None
    width: int | None = None
    height: int | None = None
    bandwidth: int | None = None
    codecs: str | None = None
    supported: bool = True
    reason: str | None = None


@dataclass(frozen=True)
class VideoResource:
    url: str
    source: str
    kind: str
    mime_type: str | None = None
    qualities: list[VideoQuality] = field(default_factory=list)


class VideoHTMLParser(html.parser.HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.candidates: list[VideoCandidate] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {name.lower(): value for name, value in attrs if value}
        tag = tag.lower()

        if tag in {"video", "source", "track"}:
            self._add_attr_urls(tag, attrs_map, ("src", "data-src", "data-url"))

        for attr_name in ("src", "href", "data-src", "data-url", "data-video", "content"):
            value = attrs_map.get(attr_name)
            if value and looks_like_video_url(value):
                self._add_url(value, f"{tag}[{attr_name}]", attrs_map.get("type"))

        if attrs_map.get("type", "").lower().startswith(VIDEO_MIME_PREFIXES):
            self._add_attr_urls(tag, attrs_map, ("src", "href", "content"))

    def _add_attr_urls(
        self,
        source: str,
        attrs_map: dict[str, str],
        attr_names: Iterable[str],
    ) -> None:
        for attr_name in attr_names:
            value = attrs_map.get(attr_name)
            if value:
                self._add_url(value, f"{source}[{attr_name}]", attrs_map.get("type"))

    def _add_url(self, raw_url: str, source: str, mime_type: str | None = None) -> None:
        absolute = urllib.parse.urljoin(self.base_url, raw_url.strip())
        self.candidates.append(VideoCandidate(absolute, source, mime_type))


def looks_like_video_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    return parsed.path.lower().endswith(VIDEO_EXTENSIONS)


def extract_regex_candidates(html: str, base_url: str) -> list[VideoCandidate]:
    candidates: list[VideoCandidate] = []
    for match in URL_PATTERN.finditer(html):
        raw_url = match.group("url")
        absolute = urllib.parse.urljoin(base_url, raw_url)
        candidates.append(VideoCandidate(absolute, "html-text"))
    return candidates


def unique_candidates(candidates: Iterable[VideoCandidate]) -> list[VideoCandidate]:
    seen: set[str] = set()
    unique: list[VideoCandidate] = []
    for candidate in candidates:
        normalized = candidate.url
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(candidate)
    return unique


def can_fetch(url: str, user_agent: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    robots_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))
    parser = urllib.robotparser.RobotFileParser(robots_url)
    try:
        parser.read()
    except OSError:
        return True
    return parser.can_fetch(user_agent, url)


def scrapling_get(url: str, user_agent: str, timeout: float):
    response = Fetcher.get(url, headers={"User-Agent": user_agent}, timeout=timeout)
    status = getattr(response, "status", None)
    if status and int(status) >= 400:
        raise OSError(f"Scrapling fetched HTTP {status} for {url}")
    return response


def response_charset(headers: dict[str, str]) -> str:
    content_type = headers.get("content-type") or headers.get("Content-Type") or ""
    for part in content_type.split(";"):
        key, _, value = part.strip().partition("=")
        if key.lower() == "charset" and value:
            return value.strip()
    return "utf-8"


def fetch_text(url: str, user_agent: str, timeout: float) -> str:
    response = scrapling_get(url, user_agent, timeout)
    return response.body.decode(response_charset(response.headers), errors="replace")


def fetch_bytes(url: str, user_agent: str, timeout: float) -> bytes:
    response = scrapling_get(url, user_agent, timeout)
    return bytes(response.body)


def ytdlp_base_options(timeout: float) -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": timeout,
        "noplaylist": True,
        "ignore_no_formats_error": False,
    }


def extract_ytdlp_info(page_url: str, timeout: float) -> dict[str, Any]:
    with YoutubeDL(ytdlp_base_options(timeout)) as ydl:
        return ydl.extract_info(page_url, download=False)


def ytdlp_quality_label(height: int | None, ext: str | None, note: str | None = None) -> str:
    parts: list[str] = []
    if height:
        parts.append(f"{height}p")
    elif note:
        parts.append(note)
    else:
        parts.append("最佳")
    if ext:
        parts.append(ext.upper())
    return " ".join(parts)


def ytdlp_format_selector(height: int | None) -> str:
    if height:
        return f"bv*[height<={height}]+ba/b[height<={height}]/best[height<={height}]"
    return "bv*+ba/best"


def ytdlp_resource_from_info(page_url: str, info: dict[str, Any]) -> VideoResource | None:
    entries = info.get("entries")
    if entries:
        first_entry = next((entry for entry in entries if entry), None)
        if not first_entry:
            return None
        info = first_entry

    formats = info.get("formats") or []
    heights = sorted(
        {
            int(fmt["height"])
            for fmt in formats
            if fmt.get("height") and isinstance(fmt.get("height"), int) and fmt.get("vcodec") != "none"
        },
        reverse=True,
    )

    qualities = [
        VideoQuality(
            label="最佳 MP4",
            url=page_url,
            kind="ytdlp",
            format_id=ytdlp_format_selector(None),
            supported=True,
        )
    ]

    for height in heights[:12]:
        qualities.append(
            VideoQuality(
                label=f"{height}p MP4",
                url=page_url,
                kind="ytdlp",
                format_id=ytdlp_format_selector(height),
                height=height,
                supported=True,
            )
        )

    if not formats and info.get("url"):
        qualities = [
            VideoQuality(
                label=ytdlp_quality_label(info.get("height"), info.get("ext")),
                url=page_url,
                kind="ytdlp",
                format_id=info.get("format_id") or "best",
                height=info.get("height"),
                supported=True,
            )
        ]

    if not qualities:
        return None

    title = info.get("title") or info.get("id") or "yt-dlp"
    extractor = info.get("extractor_key") or info.get("extractor") or "yt-dlp"
    return VideoResource(
        url=page_url,
        source=f"yt-dlp: {title}",
        kind="ytdlp",
        mime_type=str(extractor),
        qualities=qualities,
    )


def discover_ytdlp_resources(page_url: str, timeout: float) -> list[VideoResource]:
    try:
        info = extract_ytdlp_info(page_url, timeout)
    except (DownloadError, ExtractorError, OSError):
        return []
    resource = ytdlp_resource_from_info(page_url, info)
    return [resource] if resource else []


def discover_videos(page_url: str, user_agent: str, timeout: float) -> list[VideoCandidate]:
    html = fetch_text(page_url, user_agent, timeout)
    parser = VideoHTMLParser(page_url)
    parser.feed(html)
    return unique_candidates([*parser.candidates, *extract_regex_candidates(html, page_url)])


def parse_hls_attributes(value: str) -> dict[str, str]:
    attributes: dict[str, str] = {}
    index = 0
    while index < len(value):
        while index < len(value) and value[index] in " ,":
            index += 1
        key_start = index
        while index < len(value) and value[index] not in "=":
            index += 1
        if index >= len(value):
            break
        key = value[key_start:index].strip()
        index += 1
        if index < len(value) and value[index] == '"':
            index += 1
            val_start = index
            while index < len(value) and value[index] != '"':
                index += 1
            attr_value = value[val_start:index]
            index += 1
        else:
            val_start = index
            while index < len(value) and value[index] != ",":
                index += 1
            attr_value = value[val_start:index].strip()
        if key:
            attributes[key.upper()] = attr_value
        while index < len(value) and value[index] == ",":
            index += 1
    return attributes


def parse_hls_master_playlist(playlist: str, playlist_url: str) -> list[VideoQuality]:
    qualities: list[VideoQuality] = []
    lines = [line.strip() for line in playlist.splitlines() if line.strip()]
    pending_attrs: dict[str, str] | None = None

    for line in lines:
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending_attrs = parse_hls_attributes(line.split(":", 1)[1])
            continue
        if line.startswith("#"):
            continue
        if pending_attrs is None:
            continue

        width = None
        height = None
        resolution = pending_attrs.get("RESOLUTION")
        if resolution and "x" in resolution.lower():
            width_text, height_text = resolution.lower().split("x", 1)
            if width_text.isdigit() and height_text.isdigit():
                width = int(width_text)
                height = int(height_text)

        bandwidth_text = pending_attrs.get("BANDWIDTH")
        bandwidth = int(bandwidth_text) if bandwidth_text and bandwidth_text.isdigit() else None
        label = f"{height}p" if height else (f"{round(bandwidth / 1000)} kbps" if bandwidth else "variant")
        qualities.append(
            VideoQuality(
                label=label,
                url=urllib.parse.urljoin(playlist_url, line),
                kind="hls",
                width=width,
                height=height,
                bandwidth=bandwidth,
                codecs=pending_attrs.get("CODECS"),
            )
        )
        pending_attrs = None

    return sorted(qualities, key=lambda item: (item.height or 0, item.bandwidth or 0), reverse=True)


def is_hls_url(url: str) -> bool:
    return urllib.parse.urlparse(url).path.lower().endswith(".m3u8")


def is_dash_url(url: str) -> bool:
    return urllib.parse.urlparse(url).path.lower().endswith(".mpd")


def is_manifest_like_direct_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    name = Path(urllib.parse.unquote(parsed.path)).name.lower()
    return name in {"manifest.webm", "manifest.mp4", "manifest.mov", "manifest.mkv"}


def describe_video_candidate(
    candidate: VideoCandidate,
    user_agent: str,
    timeout: float,
) -> VideoResource:
    if is_dash_url(candidate.url):
        return VideoResource(
            url=candidate.url,
            source=candidate.source,
            kind="dash",
            mime_type=candidate.mime_type,
            qualities=[
                VideoQuality(
                    label="unsupported",
                    url=candidate.url,
                    kind="dash",
                    supported=False,
                    reason="暂不支持 DASH/MPD 视频流。",
                )
            ],
        )

    if is_manifest_like_direct_url(candidate.url):
        return VideoResource(
            url=candidate.url,
            source=candidate.source,
            kind="manifest",
            mime_type=candidate.mime_type,
            qualities=[
                VideoQuality(
                    label="unsupported",
                    url=candidate.url,
                    kind="manifest",
                    supported=False,
                    reason="这是页面里的媒体清单线索，不是普通可下载视频文件。",
                )
            ],
        )

    if not is_hls_url(candidate.url):
        return VideoResource(
            url=candidate.url,
            source=candidate.source,
            kind="direct",
            mime_type=candidate.mime_type,
            qualities=[VideoQuality(label="original", url=candidate.url, kind="direct")],
        )

    try:
        playlist = fetch_text(candidate.url, user_agent, timeout)
    except OSError as exc:
        return VideoResource(
            url=candidate.url,
            source=candidate.source,
            kind="hls",
            mime_type=candidate.mime_type,
            qualities=[
                VideoQuality(
                    label="unavailable",
                    url=candidate.url,
                    kind="hls",
                    supported=False,
                    reason=f"无法检查播放列表：{exc}",
                )
            ],
        )

    qualities = parse_hls_master_playlist(playlist, candidate.url)
    if qualities:
        return VideoResource(candidate.url, candidate.source, "hls", candidate.mime_type, qualities)

    _segments, unsupported_reason = parse_hls_media_segments(playlist, candidate.url)
    if unsupported_reason:
        return VideoResource(
            url=candidate.url,
            source=candidate.source,
            kind="hls",
            mime_type=candidate.mime_type,
            qualities=[
                VideoQuality(
                    label="unsupported",
                    url=candidate.url,
                    kind="hls",
                    supported=False,
                    reason=unsupported_reason,
                )
            ],
        )

    return VideoResource(
        url=candidate.url,
        source=candidate.source,
        kind="hls",
        mime_type=candidate.mime_type,
        qualities=[VideoQuality(label="original", url=candidate.url, kind="hls")],
    )


def discover_video_resources(page_url: str, user_agent: str, timeout: float) -> list[VideoResource]:
    ytdlp_resources = discover_ytdlp_resources(page_url, timeout)
    if ytdlp_resources:
        return ytdlp_resources
    return discover_scrapling_resources(page_url, user_agent, timeout)


def discover_scrapling_resources(page_url: str, user_agent: str, timeout: float) -> list[VideoResource]:
    candidates = discover_videos(page_url, user_agent, timeout)
    return [describe_video_candidate(candidate, user_agent, timeout) for candidate in candidates]


def safe_filename(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    name = Path(urllib.parse.unquote(parsed.path)).name
    if not name:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
        name = f"video-{digest}"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def ffmpeg_executable() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise RuntimeError("未找到 ffmpeg。请安装 imageio-ffmpeg 或把 ffmpeg 加入 PATH。") from exc
    return imageio_ffmpeg.get_ffmpeg_exe()


def mp4_target_for(source: Path) -> Path:
    target = source.with_suffix(".mp4")
    if target == source:
        return source
    if target.exists():
        digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:8]
        target = source.with_name(f"{source.stem}-{digest}.mp4")
    return target


def convert_to_mp4(source: Path) -> Path:
    if source.suffix.lower() == ".mp4":
        return source

    target = mp4_target_for(source)
    command = [
        ffmpeg_executable(),
        "-y",
        "-i",
        str(source),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(target),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        command = [
            ffmpeg_executable(),
            "-y",
            "-i",
            str(source),
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(target),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        error = result.stderr.strip() or result.stdout.strip() or "ffmpeg 转换失败。"
        raise RuntimeError(error)
    return target


def ytdlp_download(
    page_url: str,
    output_dir: Path,
    format_selector: str | None,
    timeout: float,
    should_cancel: Callable[[], bool] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)

    def progress_hook(_status: dict[str, Any]) -> None:
        if should_cancel and should_cancel():
            raise DownloadCancelled("下载已取消。")
        if not progress:
            return
        downloaded = int(_status.get("downloaded_bytes") or 0)
        total = int(_status.get("total_bytes") or _status.get("total_bytes_estimate") or 0)
        if downloaded and total:
            progress(min(downloaded, total), total)

    options = {
        **ytdlp_base_options(timeout),
        "format": format_selector or ytdlp_format_selector(None),
        "outtmpl": {"default": str(output_dir / "%(title).200B [%(id)s].%(ext)s")},
        "merge_output_format": "mp4",
        "ffmpeg_location": ffmpeg_executable(),
        "restrictfilenames": False,
        "windowsfilenames": True,
        "progress_hooks": [progress_hook],
    }

    with YoutubeDL(options) as ydl:
        if should_cancel and should_cancel():
            raise DownloadCancelled("下载已取消。")
        info = ydl.extract_info(page_url, download=True)

    requested = info.get("requested_downloads") or []
    for item in requested:
        filepath = item.get("filepath")
        if filepath:
            return Path(filepath)

    filename = info.get("filepath") or info.get("_filename")
    if filename:
        path = Path(filename)
        mp4_path = path.with_suffix(".mp4")
        return mp4_path if mp4_path.exists() else path

    candidates = sorted(output_dir.glob("*.mp4"), key=lambda item: item.stat().st_mtime, reverse=True)
    if candidates:
        return candidates[0]
    raise RuntimeError("yt-dlp 下载完成，但没有找到输出文件。")


def download_file(
    url: str,
    output_dir: Path,
    user_agent: str,
    timeout: float,
    progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / safe_filename(url)
    if target.exists():
        stem = target.stem
        suffix = target.suffix
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
        target = output_dir / f"{stem}-{digest}{suffix}"

    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    downloaded = 0
    with urllib.request.urlopen(request, timeout=timeout) as response, target.open("wb") as file:
        total_text = response.headers.get("Content-Length", "")
        total = int(total_text) if total_text.isdigit() else 0
        while True:
            if should_cancel and should_cancel():
                raise DownloadCancelled("下载已取消。")
            chunk = response.read(1024 * 256)
            if not chunk:
                break
            file.write(chunk)
            downloaded += len(chunk)
            if progress and total:
                progress(min(downloaded, total), total)
    if progress and downloaded:
        progress(downloaded, downloaded)
    return target


def parse_hls_media_segments(playlist: str, playlist_url: str) -> tuple[list[str], str | None]:
    if "#EXT-X-KEY" in playlist:
        return [], "暂不支持加密的 HLS 播放列表。"
    if "#EXT-X-MAP" in playlist:
        return [], "暂不支持 fMP4 类型的 HLS 播放列表。"

    segments: list[str] = []
    for raw_line in playlist.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        segments.append(urllib.parse.urljoin(playlist_url, line))

    if not segments:
        return [], "没有找到可下载的 HLS 媒体分片。"
    return segments, None


def download_hls_playlist(
    playlist_url: str,
    output_dir: Path,
    user_agent: str,
    timeout: float,
    progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Path:
    playlist = fetch_text(playlist_url, user_agent, timeout)
    segments, error = parse_hls_media_segments(playlist, playlist_url)
    if error:
        raise ValueError(error)

    output_dir.mkdir(parents=True, exist_ok=True)
    parsed = urllib.parse.urlparse(playlist_url)
    base_name = Path(urllib.parse.unquote(parsed.path)).stem or "hls-video"
    digest = hashlib.sha256(playlist_url.encode("utf-8")).hexdigest()[:8]
    target = output_dir / f"{safe_filename(base_name)}-{digest}.ts"

    with target.open("wb") as file:
        for index, segment_url in enumerate(segments, start=1):
            if should_cancel and should_cancel():
                raise DownloadCancelled("下载已取消。")
            file.write(fetch_bytes(segment_url, user_agent, timeout))
            if progress:
                progress(index, len(segments))

    return target


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover public video URLs from a web page.")
    parser.add_argument("url", help="Page URL to inspect.")
    parser.add_argument("--download", action="store_true", help="Download direct video URLs.")
    parser.add_argument("--output-dir", default="downloads", help="Directory for downloaded files.")
    parser.add_argument("--json", action="store_true", help="Print results as JSON.")
    parser.add_argument("--timeout", type=float, default=20.0, help="Network timeout in seconds.")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay before downloads, in seconds.")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="HTTP User-Agent header.")
    parser.add_argument("--ignore-robots", action="store_true", help="Skip robots.txt check.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    page_url = args.url

    if not args.ignore_robots and not can_fetch(page_url, args.user_agent):
        print(f"robots.txt does not allow fetching: {page_url}")
        return 2

    candidates = discover_videos(page_url, args.user_agent, args.timeout)

    if args.json:
        print(json.dumps([asdict(candidate) for candidate in candidates], indent=2, ensure_ascii=False))
    else:
        if not candidates:
            print("No public video URLs found in the page HTML.")
        for index, candidate in enumerate(candidates, start=1):
            mime = f" ({candidate.mime_type})" if candidate.mime_type else ""
            print(f"{index}. {candidate.url} [{candidate.source}]{mime}")

    if args.download:
        output_dir = Path(args.output_dir)
        for candidate in candidates:
            if not looks_like_video_url(candidate.url):
                continue
            time.sleep(args.delay)
            downloaded_to = download_file(candidate.url, output_dir, args.user_agent, args.timeout)
            saved_to = convert_to_mp4(downloaded_to)
            print(f"Downloaded: {saved_to}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
