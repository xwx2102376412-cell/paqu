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
import ssl
import subprocess
import sys
import time
import types
import urllib.parse
import urllib.robotparser
import urllib.request
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    from scrapling.fetchers import Fetcher
except ModuleNotFoundError:
    class Fetcher:
        @staticmethod
        def get(url: str, headers: dict[str, str] | None = None, timeout: float | None = None):
            request = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return types.SimpleNamespace(
                    status=response.status,
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )

    class Selector:
        def __init__(self, html_text: str):
            from lxml import html as lxml_html

            self.root = lxml_html.fromstring(html_text)

        def css(self, selector: str):
            text_mode = selector.endswith("::text")
            element_selector = selector.removesuffix("::text")
            values: list[str] = []
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", element_selector):
                elements = self.root.xpath(f".//{element_selector}")
            else:
                elements = self.root.cssselect(element_selector)
            for element in elements:
                if text_mode:
                    values.append(element.text_content())
                else:
                    values.append(html.unescape(str(element)))
            return types.SimpleNamespace(getall=lambda: values)

    scrapling_module = types.ModuleType("scrapling")
    fetchers_module = types.ModuleType("scrapling.fetchers")
    parser_module = types.ModuleType("scrapling.parser")
    fetchers_module.Fetcher = Fetcher
    parser_module.Selector = Selector
    scrapling_module.fetchers = fetchers_module
    scrapling_module.parser = parser_module
    sys.modules.setdefault("scrapling", scrapling_module)
    sys.modules.setdefault("scrapling.fetchers", fetchers_module)
    sys.modules.setdefault("scrapling.parser", parser_module)
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError


try:
    import certifi
except ModuleNotFoundError:
    CA_BUNDLE: str | None = None
else:
    certifi_path = Path(certifi.where())
    CA_BUNDLE = str(certifi_path) if certifi_path.exists() else None

DEFAULT_USER_AGENT = "paqu-video-crawler/0.1 (+respectful-public-video-discovery)"
CODEX_NODE_EXE = Path(
    r"C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
)
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv", ".m3u8", ".mpd")
VIDEO_MIME_PREFIXES = ("video/", "application/vnd.apple.mpegurl", "application/dash+xml")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif", ".svg")
IMAGE_MIME_PREFIXES = ("image/",)
DEFAULT_CONTENT_TYPES = {"video"}
URL_PATTERN = re.compile(
    r"""(?P<url>(?:https?://|//|/|\.{1,2}/)[^"' <>()]+?(?:\.mp4|\.webm|\.mov|\.m4v|\.avi|\.mkv|\.m3u8|\.mpd)(?:\?[^"' <>()]*)?)""",
    re.IGNORECASE,
)


class DownloadCancelled(Exception):
    """Raised when a user cancels an in-progress download."""


@dataclass(frozen=True)
class YtdlpDiscovery:
    resources: list["VideoResource"]
    error: str | None = None
    terminal: bool = False


class QuietYtdlpLogger:
    def debug(self, _message: str) -> None:
        pass

    def warning(self, _message: str) -> None:
        pass

    def error(self, _message: str) -> None:
        pass


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
    content_type: str = "video"
    preview: str | None = None
    text: str | None = None


@dataclass(frozen=True)
class ImageCandidate:
    url: str
    source: str
    alt: str | None = None
    mime_type: str | None = None


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


class ImageHTMLParser(html.parser.HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.candidates: list[ImageCandidate] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {name.lower(): value for name, value in attrs if value}
        tag = tag.lower()

        if tag in {"img", "source"}:
            self._add_attr_urls(
                tag,
                attrs_map,
                (
                    "src",
                    "data-src",
                    "data-url",
                    "data-image",
                    "data-img",
                    "data-original",
                    "data-original-image",
                    "data-lazy",
                    "data-lazy-src",
                    "data-ks-lazyload",
                    "lazy-src",
                    "main-img-url",
                ),
            )
            self._add_srcset_urls(tag, attrs_map)

        if tag == "meta":
            property_name = attrs_map.get("property") or attrs_map.get("name") or ""
            if property_name.lower() in {"og:image", "twitter:image", "twitter:image:src"}:
                self._add_url(attrs_map.get("content", ""), "meta[image]", attrs_map.get("alt"))

        for attr_name in (
            "src",
            "href",
            "data-src",
            "data-url",
            "data-image",
            "data-img",
            "data-lazy-src",
            "data-ks-lazyload",
            "content",
        ):
            value = attrs_map.get(attr_name)
            if value and looks_like_image_url(value):
                self._add_url(value, f"{tag}[{attr_name}]", attrs_map.get("alt"), attrs_map.get("type"))

        for raw_url in extract_css_urls(attrs_map.get("style", "")):
            self._add_url(raw_url, f"{tag}[style]", attrs_map.get("alt"), attrs_map.get("type"))

        if attrs_map.get("type", "").lower().startswith(IMAGE_MIME_PREFIXES):
            self._add_attr_urls(tag, attrs_map, ("src", "href", "content"))

    def _add_attr_urls(
        self,
        source: str,
        attrs_map: dict[str, str],
        attr_names: Iterable[str],
    ) -> None:
        for attr_name in attr_names:
            self._add_url(attrs_map.get(attr_name, ""), f"{source}[{attr_name}]", attrs_map.get("alt"), attrs_map.get("type"))

    def _add_srcset_urls(self, source: str, attrs_map: dict[str, str]) -> None:
        for attr_name in ("srcset", "data-srcset"):
            for raw_url in parse_srcset(attrs_map.get(attr_name, "")):
                self._add_url(raw_url, f"{source}[{attr_name}]", attrs_map.get("alt"), attrs_map.get("type"))

    def _add_url(
        self,
        raw_url: str,
        source: str,
        alt: str | None = None,
        mime_type: str | None = None,
    ) -> None:
        raw_url = raw_url.strip()
        if not raw_url or raw_url.startswith("data:"):
            return
        absolute = urllib.parse.urljoin(self.base_url, raw_url)
        if looks_like_image_url(absolute) or (mime_type and mime_type.lower().startswith(IMAGE_MIME_PREFIXES)):
            self.candidates.append(ImageCandidate(absolute, source, clean_text(alt or "") or None, mime_type))


def looks_like_video_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    path = parsed.path.lower()
    return path.endswith(VIDEO_EXTENSIONS) or (parsed.netloc.lower() == "finder.video.qq.com" and path.endswith("/stodownload"))


def looks_like_image_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    path = parsed.path.lower()
    return path.endswith(IMAGE_EXTENSIONS) or bool(re.search(r"\.(?:jpe?g|png|webp|gif|bmp|avif|svg)(?:[_@][^/?#]+)?$", path))


def parse_srcset(value: str) -> list[str]:
    urls: list[str] = []
    for candidate in value.split(","):
        url = candidate.strip().split(" ", 1)[0]
        if url:
            urls.append(url)
    return urls


def extract_css_urls(value: str) -> list[str]:
    return [match.strip("\"' ") for match in re.findall(r"url\(([^)]+)\)", value or "", flags=re.IGNORECASE)]


def unescape_embedded_url(value: str) -> str:
    value = html.unescape(value)
    value = value.replace("\\/", "/")
    value = value.replace("\\u002F", "/").replace("\\u002f", "/")
    return value


def extract_regex_candidates(html: str, base_url: str) -> list[VideoCandidate]:
    candidates: list[VideoCandidate] = []
    for match in URL_PATTERN.finditer(html):
        raw_url = match.group("url")
        absolute = urllib.parse.urljoin(base_url, raw_url)
        candidates.append(VideoCandidate(absolute, "html-text"))
    return candidates


def extract_regex_image_candidates(html: str, base_url: str) -> list[ImageCandidate]:
    pattern = re.compile(
        r"""(?P<url>(?:https?://|//|/|\.{1,2}/)[^"' <>()\\]+?(?:\.jpg|\.jpeg|\.png|\.webp|\.gif|\.bmp|\.avif|\.svg)(?:[_@][^"' <>()\\?]*)?(?:\?[^"' <>()\\]*)?)""",
        re.IGNORECASE,
    )
    candidates: list[ImageCandidate] = []
    for source_name, source_text in (("html-text", html), ("embedded-json", unescape_embedded_url(html))):
        for match in pattern.finditer(source_text):
            candidates.append(ImageCandidate(urllib.parse.urljoin(base_url, match.group("url")), source_name))
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


def unique_image_candidates(candidates: Iterable[ImageCandidate]) -> list[ImageCandidate]:
    seen: set[str] = set()
    unique: list[ImageCandidate] = []
    for candidate in candidates:
        if candidate.url in seen:
            continue
        seen.add(candidate.url)
        unique.append(candidate)
    return unique


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_content_types(content_types: Iterable[str] | None) -> set[str]:
    allowed = {"video", "image", "text"}
    selected = {item for item in (content_types or DEFAULT_CONTENT_TYPES) if item in allowed}
    return selected or set(DEFAULT_CONTENT_TYPES)


def can_fetch(url: str, user_agent: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    robots_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))
    parser = urllib.robotparser.RobotFileParser(robots_url)
    try:
        parser.read()
    except OSError:
        return True
    return parser.can_fetch(user_agent, url)


class SimpleResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = headers
        self.body = body
        self._selector: Any | None = None

    def css(self, selector: str):
        if self._selector is None:
            from scrapling.parser import Selector

            self._selector = Selector(response_text(self))
        return self._selector.css(selector)


def urllib_get(url: str, user_agent: str, timeout: float):
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    context = ssl.create_default_context(cafile=CA_BUNDLE) if CA_BUNDLE else None
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        return SimpleResponse(response.status, dict(response.headers.items()), response.read())


def should_fallback_from_fetcher(error: BaseException) -> bool:
    message = str(error).lower()
    markers = (
        "curl: (77)",
        "certificate verify",
        "certificate verify locations",
        "cafile:",
        "capath:",
        "no such file or directory",
    )
    return any(marker in message for marker in markers)


def scrapling_get(url: str, user_agent: str, timeout: float):
    try:
        response = Fetcher.get(url, headers={"User-Agent": user_agent}, timeout=timeout)
    except Exception as exc:
        if not should_fallback_from_fetcher(exc):
            raise
        response = urllib_get(url, user_agent, timeout)
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


def response_text(response: Any) -> str:
    body = getattr(response, "body", b"")
    headers = getattr(response, "headers", {}) or {}
    if isinstance(body, str):
        return body
    return bytes(body).decode(response_charset(headers), errors="replace")


def detect_access_gate(page_url: str, html_text: str) -> str | None:
    host = urllib.parse.urlparse(page_url).netloc.lower()
    lowered = html_text.lower()
    if "login.alibaba.com" in lowered or '"action": "login"' in lowered or "_____tmd_____" in lowered:
        return "页面返回了 Alibaba 登录/风控跳转页，不是商品详情页；当前工具不会绕过登录、验证码或风控。"
    if "captcha" in lowered and ("verify" in lowered or "security" in lowered):
        if "douyin.com" in host:
            return "抖音返回了验证码或安全验证页；当前工具不能绕过验证码。请先在浏览器完成验证，并使用抖音对应的 cookies.txt 后再扫描。"
        return "页面返回了验证码或安全验证内容；当前工具不会绕过验证码。"
    return None


def ytdlp_base_options(timeout: float, cookies_file: Path | None = None) -> dict[str, Any]:
    options = {
        "quiet": True,
        "no_warnings": True,
        "logger": QuietYtdlpLogger(),
        "socket_timeout": timeout,
        "noplaylist": True,
        "ignore_no_formats_error": False,
    }
    if CODEX_NODE_EXE.exists():
        options["js_runtimes"] = {"node": {"path": str(CODEX_NODE_EXE)}}
    if cookies_file:
        options["cookiefile"] = str(cookies_file)
    return options


def extract_ytdlp_info(page_url: str, timeout: float, cookies_file: Path | None = None) -> dict[str, Any]:
    with YoutubeDL(ytdlp_base_options(timeout, cookies_file)) as ydl:
        return ydl.extract_info(page_url, download=False)


def clean_ytdlp_error(error: BaseException) -> str:
    message = str(error).strip()
    if message.startswith("ERROR:"):
        message = message.removeprefix("ERROR:").strip()
    return message or "yt-dlp 未能识别该视频。"


def is_terminal_ytdlp_error(message: str) -> bool:
    lowered = message.lower()
    terminal_markers = (
        "video unavailable",
        "private video",
        "sign in",
        "login",
        "age-restricted",
        "copyright",
        "not available in your country",
        "members-only",
        "removed",
        "deleted",
        "fresh cookies",
        "cannot parse data",
        "requested content is not available",
        "login required",
        "this video is only available for registered users",
    )
    return any(marker in lowered for marker in terminal_markers)


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
        content_type="video",
    )


def discover_ytdlp_resources(page_url: str, timeout: float, cookies_file: Path | None = None) -> list[VideoResource]:
    return discover_ytdlp(page_url, timeout, cookies_file).resources


def discover_ytdlp(page_url: str, timeout: float, cookies_file: Path | None = None) -> YtdlpDiscovery:
    try:
        info = extract_ytdlp_info(page_url, timeout, cookies_file)
    except (DownloadError, ExtractorError) as exc:
        message = clean_ytdlp_error(exc)
        return YtdlpDiscovery([], message, is_terminal_ytdlp_error(message))
    except ModuleNotFoundError as exc:
        module_name = getattr(exc, "name", "") or str(exc)
        if "yt_dlp" in module_name:
            return YtdlpDiscovery(
                [],
                f"yt-dlp 安装不完整，缺少模块 {module_name}。请关闭服务窗口，重新运行 start_server.bat 修复依赖后再扫描。",
                True,
            )
        raise
    except OSError as exc:
        return YtdlpDiscovery([], clean_ytdlp_error(exc), False)
    resource = ytdlp_resource_from_info(page_url, info)
    return YtdlpDiscovery([resource] if resource else [])


def weixin_finder_id(page_url: str) -> str | None:
    parsed = urllib.parse.urlparse(page_url)
    host = parsed.netloc.lower()
    path = parsed.path.strip("/")
    query = urllib.parse.parse_qs(parsed.query)

    if host == "weixin.qq.com" and path.startswith("sph/"):
        short_id = path.split("/", 1)[1].strip()
        return short_id or None
    if host == "channels.weixin.qq.com" and parsed.path.endswith("/pages/sph"):
        short_id = (query.get("id") or [""])[0].strip()
        return short_id or None
    return None


def weixin_finder_request(payload: dict[str, Any], page_url: str, timeout: float) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info",
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36",
            "Origin": "https://channels.weixin.qq.com",
            "Referer": page_url,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def first_weixin_video_url(feed_info: dict[str, Any]) -> tuple[str | None, str | None]:
    video_fields = (
        ("H.265", feed_info.get("h265VideoInfo")),
        ("H.264", feed_info.get("h264VideoInfo")),
    )
    for label, value in video_fields:
        if isinstance(value, dict) and value.get("videoUrl"):
            return str(value["videoUrl"]), label
    if feed_info.get("videoUrl"):
        return str(feed_info["videoUrl"]), "原始视频"
    return None, None


def weixin_finder_resource(
    page_url: str,
    feed_info: dict[str, Any] | None,
    err_msg: dict[str, Any] | None,
) -> VideoResource | None:
    feed_info = feed_info or {}
    err_msg = err_msg or {}
    description = clean_text(str(feed_info.get("description") or ""))
    title = description or "微信视频号公开视频"
    video_url, label = first_weixin_video_url(feed_info)

    if video_url:
        return VideoResource(
            url=video_url,
            source=f"微信视频号: {title}",
            kind="direct",
            mime_type="video/mp4",
            qualities=[VideoQuality(label=label or "原始视频", url=video_url, kind="direct")],
            preview=feed_info.get("coverUrl"),
        )

    reason = clean_text(str(err_msg.get("title") or err_msg.get("content") or "公开视频接口没有返回 videoUrl。"))
    return VideoResource(
        url=page_url,
        source=f"微信视频号: {title}",
        kind="weixin",
        mime_type="WeixinFinderPreview",
        qualities=[
            VideoQuality(
                label="不可下载",
                url=page_url,
                kind="weixin",
                supported=False,
                reason=reason,
            )
        ],
        preview=feed_info.get("coverUrl") or description or None,
    )


def discover_weixin_finder_resources(page_url: str, timeout: float) -> list[VideoResource]:
    short_id = weixin_finder_id(page_url)
    if not short_id:
        return []

    sph_url = f"https://channels.weixin.qq.com/finder-preview/pages/sph?id={urllib.parse.quote(short_id)}"
    try:
        payload = {"baseReq": {"generalToken": ""}, "shortUri": short_id}
        response = weixin_finder_request(payload, sph_url, timeout)
    except (OSError, json.JSONDecodeError):
        return []

    data = response.get("data") if isinstance(response, dict) else {}
    if not isinstance(data, dict):
        return []

    resource = weixin_finder_resource(page_url, data.get("feedInfo"), data.get("errMsg"))
    if resource and any(quality.supported for quality in resource.qualities):
        return [resource]

    scene_info = data.get("sceneInfo") if isinstance(data.get("sceneInfo"), dict) else {}
    export_id = scene_info.get("dynamicExportId")
    if export_id:
        try:
            feed_url = f"https://channels.weixin.qq.com/finder-preview/pages/feed?eid={urllib.parse.quote(str(export_id))}"
            export_payload = {"baseReq": {"generalToken": ""}, "exportId": export_id}
            export_response = weixin_finder_request(export_payload, feed_url, timeout)
        except (OSError, json.JSONDecodeError):
            export_response = {}
        export_data = export_response.get("data") if isinstance(export_response, dict) else {}
        if isinstance(export_data, dict):
            export_resource = weixin_finder_resource(page_url, export_data.get("feedInfo"), export_data.get("errMsg"))
            if export_resource and any(quality.supported for quality in export_resource.qualities):
                return [export_resource]
            if export_resource and export_resource.qualities[0].reason:
                if resource and resource.qualities:
                    return [
                        replace(
                            resource,
                            qualities=[
                                replace(
                                    resource.qualities[0],
                                    reason=export_resource.qualities[0].reason,
                                )
                            ],
                        )
                    ]
                return [export_resource]

    return [resource] if resource else []


def discover_videos(page_url: str, user_agent: str, timeout: float) -> list[VideoCandidate]:
    html = fetch_text(page_url, user_agent, timeout)
    parser = VideoHTMLParser(page_url)
    parser.feed(html)
    return unique_candidates([*parser.candidates, *extract_regex_candidates(html, page_url)])


def discover_videos_from_html(page_url: str, html: str) -> list[VideoCandidate]:
    parser = VideoHTMLParser(page_url)
    parser.feed(html)
    return unique_candidates([*parser.candidates, *extract_regex_candidates(html, page_url)])


def discover_images_from_html(page_url: str, html: str) -> list[ImageCandidate]:
    parser = ImageHTMLParser(page_url)
    parser.feed(html)
    return unique_image_candidates([*parser.candidates, *extract_regex_image_candidates(html, page_url)])


def image_resource_from_candidate(candidate: ImageCandidate) -> VideoResource:
    return VideoResource(
        url=candidate.url,
        source=candidate.source,
        kind="image",
        mime_type=candidate.mime_type,
        content_type="image",
        preview=candidate.alt,
    )


def selector_texts(page: Any, selector: str, limit: int) -> list[str]:
    try:
        values = page.css(selector).getall()
    except Exception:
        return []
    texts: list[str] = []
    for value in values:
        text = clean_text(str(value))
        if text:
            texts.append(text)
        if len(texts) >= limit:
            break
    return texts


def text_resources_from_page(page_url: str, page: Any) -> list[VideoResource]:
    fields = [
        ("title", "title::text", 1),
        ("heading", "h1::text", 10),
        ("heading", "h2::text", 12),
        ("paragraph", "p::text", 40),
    ]
    resources: list[VideoResource] = []
    seen: set[str] = set()
    for kind, selector, limit in fields:
        for text in selector_texts(page, selector, limit):
            if len(text) < 2 or text in seen:
                continue
            seen.add(text)
            resources.append(
                VideoResource(
                    url=page_url,
                    source=selector,
                    kind=kind,
                    content_type="text",
                    preview=text[:160],
                    text=text,
                )
            )
    return resources


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
    if looks_like_video_url(page_url):
        return [describe_video_candidate(VideoCandidate(page_url, "direct-input-url"), user_agent, timeout)]

    ytdlp_resources = discover_ytdlp_resources(page_url, timeout)
    if ytdlp_resources:
        return ytdlp_resources
    weixin_resources = discover_weixin_finder_resources(page_url, timeout)
    if weixin_resources:
        return weixin_resources
    return discover_scrapling_resources(page_url, user_agent, timeout)


def discover_scrapling_resources(page_url: str, user_agent: str, timeout: float) -> list[VideoResource]:
    candidates = discover_videos(page_url, user_agent, timeout)
    return [describe_video_candidate(candidate, user_agent, timeout) for candidate in candidates]


def discover_scrapling_content(
    page_url: str,
    user_agent: str,
    timeout: float,
    content_types: Iterable[str] | None = None,
) -> list[VideoResource]:
    selected = normalize_content_types(content_types)
    page = scrapling_get(page_url, user_agent, timeout)
    html = response_text(page)
    access_gate = detect_access_gate(page_url, html)
    if access_gate:
        raise PermissionError(access_gate)
    resources: list[VideoResource] = []

    if "video" in selected:
        video_candidates = discover_videos_from_html(page_url, html)
        resources.extend(describe_video_candidate(candidate, user_agent, timeout) for candidate in video_candidates)
    if "image" in selected:
        image_candidates = discover_images_from_html(page_url, html)
        resources.extend(image_resource_from_candidate(candidate) for candidate in image_candidates)
    if "text" in selected:
        resources.extend(text_resources_from_page(page_url, page))

    return resources


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


def ytdlp_output_template(output_dir: Path) -> str:
    return str(output_dir / "%(title).80B [%(id).32B].%(ext)s")


def ytdlp_download(
    page_url: str,
    output_dir: Path,
    format_selector: str | None,
    timeout: float,
    cookies_file: Path | None = None,
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
        **ytdlp_base_options(timeout, cookies_file),
        "format": format_selector or ytdlp_format_selector(None),
        "outtmpl": {"default": ytdlp_output_template(output_dir)},
        "merge_output_format": "mp4",
        "ffmpeg_location": ffmpeg_executable(),
        "restrictfilenames": True,
        "windowsfilenames": True,
        "abort_on_unavailable_fragment": True,
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

    resources = discover_video_resources(page_url, args.user_agent, args.timeout)

    if args.json:
        print(json.dumps([asdict(resource) for resource in resources], indent=2, ensure_ascii=False))
    else:
        if not resources:
            print("No public video resources found.")
        for index, resource in enumerate(resources, start=1):
            mime = f" ({resource.mime_type})" if resource.mime_type else ""
            print(f"{index}. {resource.url} [{resource.source}] {resource.kind}{mime}")
            for quality in resource.qualities:
                suffix = "" if quality.supported else f" - {quality.reason or 'unsupported'}"
                print(f"   - {quality.label}: {quality.url}{suffix}")

    if args.download:
        output_dir = Path(args.output_dir)
        for resource in resources:
            quality = next((item for item in resource.qualities if item.supported), None)
            if not quality:
                continue
            time.sleep(args.delay)
            if quality.kind == "ytdlp":
                saved_to = ytdlp_download(quality.url, output_dir, quality.format_id, args.timeout)
            elif quality.kind == "hls":
                downloaded_to = download_hls_playlist(quality.url, output_dir, args.user_agent, args.timeout)
                saved_to = convert_to_mp4(downloaded_to)
            else:
                downloaded_to = download_file(quality.url, output_dir, args.user_agent, args.timeout)
                saved_to = convert_to_mp4(downloaded_to)
            print(f"Downloaded: {saved_to}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
