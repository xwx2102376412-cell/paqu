import unittest
import time
from pathlib import Path
from unittest.mock import patch

from video_crawler import (
    VideoHTMLParser,
    YtdlpDiscovery,
    VideoResource,
    VideoQuality,
    extract_regex_candidates,
    discover_images_from_html,
    detect_access_gate,
    fetch_text,
    convert_to_mp4,
    describe_video_candidate,
    text_resources_from_page,
    discover_video_resources,
    discover_ytdlp,
    parse_hls_master_playlist,
    parse_hls_media_segments,
    unique_candidates,
    unique_image_candidates,
    VideoCandidate,
    ytdlp_download,
    ytdlp_base_options,
    ytdlp_output_template,
    discover_weixin_finder_resources,
    weixin_finder_id,
)


class VideoCrawlerTests(unittest.TestCase):
    def test_fetch_text_uses_scrapling_fetcher(self):
        class FakeResponse:
            status = 200
            headers = {"content-type": "text/html; charset=utf-8"}
            body = "<html>ok</html>".encode("utf-8")

        with patch("video_crawler.Fetcher.get", return_value=FakeResponse()) as get:
            html = fetch_text("https://example.com", "agent", 3)

        self.assertEqual(html, "<html>ok</html>")
        get.assert_called_once_with("https://example.com", headers={"User-Agent": "agent"}, timeout=3)

    def test_fetch_text_falls_back_when_curl_certificates_fail(self):
        class FakeResponse:
            status = 200
            headers = {"content-type": "text/html; charset=utf-8"}
            body = "<html>fallback</html>".encode("utf-8")

        with patch("video_crawler.Fetcher.get", side_effect=OSError("curl: (77) error setting certificate verify locations")), patch(
            "video_crawler.urllib_get", return_value=FakeResponse()
        ) as fallback:
            html = fetch_text("https://example.com", "agent", 3)

        self.assertEqual(html, "<html>fallback</html>")
        fallback.assert_called_once_with("https://example.com", "agent", 3)

    def test_extracts_video_and_source_tags(self):
        parser = VideoHTMLParser("https://example.com/posts/one")
        parser.feed(
            """
            <video src="/media/a.mp4"></video>
            <video><source src="../b.webm" type="video/webm"></video>
            """
        )

        urls = [candidate.url for candidate in unique_candidates(parser.candidates)]

        self.assertEqual(
            urls,
            [
                "https://example.com/media/a.mp4",
                "https://example.com/b.webm",
            ],
        )

    def test_extracts_images_from_img_srcset_and_meta(self):
        html = """
        <img src="/images/a.jpg" alt="A picture">
        <source srcset="/images/b.webp 1x, https://cdn.example.com/c.png 2x" type="image/webp">
        <meta property="og:image" content="/cover.png">
        """

        candidates = discover_images_from_html("https://example.com/posts/one", html)
        urls = [candidate.url for candidate in unique_image_candidates(candidates)]

        self.assertEqual(
            urls,
            [
                "https://example.com/images/a.jpg",
                "https://example.com/images/b.webp",
                "https://cdn.example.com/c.png",
                "https://example.com/cover.png",
            ],
        )
        self.assertEqual(candidates[0].alt, "A picture")

    def test_extracts_alibaba_style_lazy_and_embedded_images(self):
        html = r"""
        <img data-ks-lazyload="//s.alicdn.com/@sc04/kf/Habc.jpg_720x720q50.jpg" alt="main">
        <div style="background-image:url('//s.alicdn.com/@sc04/kf/Hbg.png_300x300.png')"></div>
        <script>
          window.__data = {"image":"https:\/\/sc04.alicdn.com\/kf\/Hjson.webp_720x720q50.webp"};
        </script>
        """

        candidates = discover_images_from_html("https://www.alibaba.com/product-detail/item.html", html)
        urls = [candidate.url for candidate in candidates]

        self.assertIn("https://s.alicdn.com/@sc04/kf/Habc.jpg_720x720q50.jpg", urls)
        self.assertIn("https://s.alicdn.com/@sc04/kf/Hbg.png_300x300.png", urls)
        self.assertIn("https://sc04.alicdn.com/kf/Hjson.webp_720x720q50.webp", urls)

    def test_detects_alibaba_login_gate(self):
        message = detect_access_gate("https://www.alibaba.com/product-detail/item.html", 'window._config_ = {"action": "login", "url": "https://login.alibaba.com/"}')

        self.assertIn("Alibaba", message)

    def test_detects_douyin_captcha_gate(self):
        message = detect_access_gate("https://www.douyin.com/video/72630138283281", "<html>captcha verify security</html>")

        self.assertIn("抖音", message)
        self.assertIn("不能绕过验证码", message)

    def test_extracts_text_resources_with_scrapling_selector_api(self):
        from scrapling.parser import Selector

        page = Selector(
            """
            <html>
              <head><title>Example Title</title></head>
              <body>
                <h1>Main Heading</h1>
                <p>First paragraph with useful text.</p>
                <p>First paragraph with useful text.</p>
              </body>
            </html>
            """
        )

        resources = text_resources_from_page("https://example.com", page)
        values = [resource.text for resource in resources]

        self.assertIn("Example Title", values)
        self.assertIn("Main Heading", values)
        self.assertIn("First paragraph with useful text.", values)
        self.assertEqual(values.count("First paragraph with useful text."), 1)

    def test_discover_resources_prefers_ytdlp(self):
        with patch(
            "video_crawler.extract_ytdlp_info",
            return_value={
                "title": "Sample",
                "extractor_key": "Generic",
                "formats": [
                    {"height": 720, "vcodec": "avc1", "format_id": "v720"},
                    {"height": 360, "vcodec": "avc1", "format_id": "v360"},
                ],
            },
        ), patch("video_crawler.discover_videos") as discover_videos:
            resources = discover_video_resources("https://example.com/watch", "agent", 3)

        self.assertEqual(resources[0].kind, "ytdlp")
        self.assertEqual(resources[0].qualities[0].label, "最佳 MP4")
        self.assertEqual(resources[0].qualities[1].label, "720p MP4")
        discover_videos.assert_not_called()

    def test_direct_media_url_is_returned_without_page_fetch(self):
        with patch("video_crawler.extract_ytdlp_info") as ytdlp, patch("video_crawler.discover_videos") as discover_videos:
            resources = discover_video_resources("https://finder.video.qq.com/video.mp4?token=abc", "agent", 3)

        self.assertEqual(resources[0].kind, "direct")
        self.assertEqual(resources[0].qualities[0].url, "https://finder.video.qq.com/video.mp4?token=abc")
        ytdlp.assert_not_called()
        discover_videos.assert_not_called()

    def test_extracts_weixin_finder_short_id(self):
        self.assertEqual(weixin_finder_id("https://weixin.qq.com/sph/AkyXBpVPDo"), "AkyXBpVPDo")
        self.assertEqual(
            weixin_finder_id("https://channels.weixin.qq.com/finder-preview/pages/sph?id=AkyXBpVPDo"),
            "AkyXBpVPDo",
        )

    def test_discovers_weixin_finder_video_url(self):
        response = {
            "data": {
                "feedInfo": {
                    "description": "Sample feed",
                    "coverUrl": "https://finder.video.qq.com/cover",
                    "h264VideoInfo": {"videoUrl": "https://finder.video.qq.com/video.mp4?token=abc"},
                },
                "errMsg": {"type": 0},
            }
        }

        with patch("video_crawler.weixin_finder_request", return_value=response):
            resources = discover_weixin_finder_resources("https://weixin.qq.com/sph/AkyXBpVPDo", 3)

        self.assertEqual(resources[0].kind, "direct")
        self.assertEqual(resources[0].qualities[0].url, "https://finder.video.qq.com/video.mp4?token=abc")
        self.assertEqual(resources[0].preview, "https://finder.video.qq.com/cover")

    def test_reports_weixin_finder_unplayable_reason(self):
        response = {
            "data": {
                "feedInfo": {"description": "Sample feed", "coverUrl": "https://finder.video.qq.com/cover"},
                "errMsg": {"type": 2, "title": "此内容暂时无法播放"},
            }
        }

        with patch("video_crawler.weixin_finder_request", return_value=response):
            resources = discover_weixin_finder_resources("https://weixin.qq.com/sph/AkyXBpVPDo", 3)

        self.assertEqual(resources[0].kind, "weixin")
        self.assertFalse(resources[0].qualities[0].supported)
        self.assertIn("暂时无法播放", resources[0].qualities[0].reason)

    def test_extracts_video_urls_from_inline_text(self):
        candidates = extract_regex_candidates(
            "window.videoUrl = 'https://cdn.example.com/clip.m3u8?token=abc';",
            "https://example.com/",
        )

        self.assertEqual(candidates[0].url, "https://cdn.example.com/clip.m3u8?token=abc")

    def test_parses_hls_master_playlist_quality(self):
        qualities = parse_hls_master_playlist(
            """
            #EXTM3U
            #EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720,CODECS="avc1.4d401f,mp4a.40.2"
            720/main.m3u8
            """,
            "https://cdn.example.com/master.m3u8",
        )

        self.assertEqual(qualities[0].label, "720p")
        self.assertEqual(qualities[0].url, "https://cdn.example.com/720/main.m3u8")
        self.assertEqual(qualities[0].height, 720)

    def test_detects_encrypted_hls_playlist_as_unsupported(self):
        segments, error = parse_hls_media_segments(
            """
            #EXTM3U
            #EXT-X-KEY:METHOD=AES-128,URI="key.bin"
            #EXTINF:4.0,
            segment0.ts
            """,
            "https://cdn.example.com/video/index.m3u8",
        )

        self.assertEqual(segments, [])
        self.assertIn("加密", error)

    def test_dash_candidate_is_marked_unsupported(self):
        resource = describe_video_candidate(
            VideoCandidate("https://cdn.example.com/manifest.mpd", "html-text"),
            "agent",
            3,
        )

        self.assertEqual(resource.kind, "dash")
        self.assertFalse(resource.qualities[0].supported)
        self.assertIn("DASH", resource.qualities[0].reason)

    def test_manifest_webm_is_marked_unsupported(self):
        resource = describe_video_candidate(
            VideoCandidate("https://www.youtube.com/manifest.webm", "html-text"),
            "agent",
            3,
        )

        self.assertEqual(resource.kind, "manifest")
        self.assertFalse(resource.qualities[0].supported)
        self.assertIn("媒体清单", resource.qualities[0].reason)

    def test_convert_to_mp4_uses_ffmpeg_for_non_mp4(self):
        source = Path("downloads/unit-convert-source.ts")

        with patch("video_crawler.ffmpeg_executable", return_value="ffmpeg"), patch("video_crawler.subprocess.run") as run:
            run.return_value.returncode = 0
            target = convert_to_mp4(source)

        self.assertEqual(target, Path("downloads/unit-convert-source.mp4"))
        self.assertIn("ffmpeg", run.call_args.args[0][0])

    def test_download_task_status_reaches_done(self):
        import web_app

        with web_app.TASKS_LOCK:
            web_app.TASKS.clear()

        with patch("web_app.download_file", return_value=Path("downloads/test.mp4")):
            task = web_app.create_task(VideoQuality(label="original", url="https://cdn.example.com/a.mp4", kind="direct"))
            for _ in range(20):
                with web_app.TASKS_LOCK:
                    current = web_app.TASKS[task.id]
                    status = current.status
                    saved_to = current.saved_to
                if status == "done":
                    break
                time.sleep(0.05)

        self.assertEqual(status, "done")
        self.assertEqual(Path(saved_to), Path("downloads/test.mp4"))

    def test_ytdlp_download_task_passes_format_id(self):
        import web_app

        with web_app.TASKS_LOCK:
            web_app.TASKS.clear()

        quality = VideoQuality(
            label="720p MP4",
            url="https://example.com/watch",
            kind="ytdlp",
            format_id="best[height<=720]",
        )

        with patch("web_app.ytdlp_download", return_value=Path("downloads/video.mp4")) as download:
            task = web_app.create_task(quality)
            for _ in range(20):
                with web_app.TASKS_LOCK:
                    current = web_app.TASKS[task.id]
                    status = current.status
                    saved_to = current.saved_to
                if status == "done":
                    break
                time.sleep(0.05)

        self.assertEqual(status, "done")
        self.assertEqual(Path(saved_to), Path("downloads/video.mp4"))
        download.assert_called_once()
        self.assertEqual(download.call_args.args[2], "best[height<=720]")

    def test_ytdlp_download_uses_short_windows_safe_filename_template(self):
        captured_options = {}

        class FakeYoutubeDL:
            def __init__(self, options):
                captured_options.update(options)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def extract_info(self, page_url, download):
                return {"filepath": "downloads/video.mp4"}

        with patch("video_crawler.ffmpeg_executable", return_value="ffmpeg"), patch("video_crawler.YoutubeDL", FakeYoutubeDL):
            path = ytdlp_download("https://example.com/watch", Path("downloads"), "best", 3)

        self.assertEqual(path, Path("downloads/video.mp4"))
        self.assertEqual(captured_options["outtmpl"]["default"], ytdlp_output_template(Path("downloads")))
        self.assertIn("%(title).80B", captured_options["outtmpl"]["default"])
        self.assertIn("%(id).32B", captured_options["outtmpl"]["default"])
        self.assertTrue(captured_options["restrictfilenames"])
        self.assertTrue(captured_options["windowsfilenames"])
        self.assertTrue(captured_options["abort_on_unavailable_fragment"])

    def test_ytdlp_template_truncates_long_url_like_ids(self):
        from yt_dlp import YoutubeDL

        long_id = f"stodownload?encfilekey={'a' * 300}&token={'b' * 300}"
        info = {"title": "stodownload", "id": long_id, "ext": "mp4"}

        with YoutubeDL(
            {
                "outtmpl": {"default": ytdlp_output_template(Path("downloads"))},
                "restrictfilenames": True,
                "windowsfilenames": True,
            }
        ) as ydl:
            filename = ydl.prepare_filename(info)

        self.assertLess(len(filename), 120)
        self.assertNotIn("?", filename)
        self.assertNotIn("&", filename)

    def test_ytdlp_base_options_accepts_cookie_file(self):
        options = ytdlp_base_options(3, Path("cookies.txt"))

        self.assertEqual(options["cookiefile"], "cookies.txt")

    def test_discover_ytdlp_reports_incomplete_install(self):
        with patch("video_crawler.extract_ytdlp_info", side_effect=ModuleNotFoundError("No module named 'yt_dlp.extractor.tiktok'")):
            result = discover_ytdlp("https://www.tiktok.com/@aipanic1/video/763273", 3)

        self.assertTrue(result.terminal)
        self.assertIn("yt-dlp 安装不完整", result.error)
        self.assertIn("start_server.bat", result.error)

    def test_ytdlp_base_options_enables_node_when_available(self):
        options = ytdlp_base_options(3)

        if "js_runtimes" in options:
            self.assertIn("node", options["js_runtimes"])
            self.assertIn("path", options["js_runtimes"]["node"])

    def test_web_scan_prefers_ytdlp_before_robots(self):
        import web_app

        resource = VideoResource(
            url="https://example.com/watch",
            source="yt-dlp: Sample",
            kind="ytdlp",
            qualities=[VideoQuality(label="最佳 MP4", url="https://example.com/watch", kind="ytdlp", format_id="best")],
        )

        with patch("web_app.discover_ytdlp", return_value=YtdlpDiscovery([resource])), patch("web_app.can_fetch") as can_fetch:
            status, payload = web_app.scan_page_resources("https://example.com/watch")

        self.assertEqual(status, web_app.HTTPStatus.OK)
        self.assertEqual(payload["resources"][0]["kind"], "ytdlp")
        can_fetch.assert_not_called()

    def test_web_scan_passes_cookie_file_to_ytdlp(self):
        import web_app

        cookies_file = Path("cookies.txt")

        with patch("web_app.discover_ytdlp", return_value=YtdlpDiscovery([])) as ytdlp, patch(
            "web_app.can_fetch", return_value=False
        ):
            web_app.scan_page_resources("https://youtube.com/shorts/abc123", ["video"], cookies_file)

        self.assertEqual(ytdlp.call_args.args[2], cookies_file)

    def test_resolve_cookies_file_auto_detects_youtube_cookie_file(self):
        import web_app

        base = Path("E:/vibe-coding/paqu")

        def fake_exists(path):
            return Path(path).name == "www.youtube.com_cookies.txt"

        def fake_is_file(path):
            return Path(path).name == "www.youtube.com_cookies.txt"

        with patch("web_app.Path.cwd", return_value=base), patch("web_app.Path.exists", fake_exists), patch(
            "web_app.Path.is_file", fake_is_file
        ):
            cookies_file = web_app.resolve_cookies_file("", "https://www.youtube.com/watch?v=abc123")

        self.assertEqual(cookies_file, base / "www.youtube.com_cookies.txt")

    def test_resolve_cookies_file_does_not_auto_detect_for_other_sites(self):
        import web_app

        with patch("web_app.Path.exists", return_value=True), patch("web_app.Path.is_file", return_value=True):
            cookies_file = web_app.resolve_cookies_file("", "https://example.com/watch")

        self.assertIsNone(cookies_file)

    def test_resolve_cookies_file_auto_detects_douyin_cookie_file(self):
        import web_app

        base = Path("E:/vibe-coding/paqu")

        def fake_exists(path):
            return Path(path).name == "www.douyin.com_cookies.txt"

        with patch("web_app.Path.cwd", return_value=base), patch("web_app.Path.exists", fake_exists), patch(
            "web_app.Path.is_file", fake_exists
        ):
            cookies_file = web_app.resolve_cookies_file("", "https://www.douyin.com/video/7410418482975690025")

        self.assertEqual(cookies_file, base / "www.douyin.com_cookies.txt")

    def test_resolve_cookies_file_auto_detects_facebook_cookie_file(self):
        import web_app

        base = Path("E:/vibe-coding/paqu")

        def fake_exists(path):
            return Path(path).name == "www.facebook.com_cookies.txt"

        with patch("web_app.Path.cwd", return_value=base), patch("web_app.Path.exists", fake_exists), patch(
            "web_app.Path.is_file", fake_exists
        ):
            cookies_file = web_app.resolve_cookies_file("", "https://www.facebook.com/reel/87435802865334")

        self.assertEqual(cookies_file, base / "www.facebook.com_cookies.txt")

    def test_resolve_cookies_file_auto_detects_tiktok_cookie_file(self):
        import web_app

        base = Path("E:/vibe-coding/paqu")

        def fake_exists(path):
            return Path(path).name == "www.tiktok.com_cookies.txt"

        with patch("web_app.Path.cwd", return_value=base), patch("web_app.Path.exists", fake_exists), patch(
            "web_app.Path.is_file", fake_exists
        ):
            cookies_file = web_app.resolve_cookies_file("", "https://www.tiktok.com/@creator/video/123")

        self.assertEqual(cookies_file, base / "www.tiktok.com_cookies.txt")

    def test_facebook_ytdlp_error_does_not_fall_back_to_robots(self):
        import web_app

        error = "[facebook] Cannot parse data; please report this issue"
        with patch("web_app.discover_ytdlp", return_value=YtdlpDiscovery([], error, False)), patch(
            "web_app.can_fetch"
        ) as can_fetch:
            status, payload = web_app.scan_page_resources("https://www.facebook.com/reel/87435802865334", ["video"])

        self.assertEqual(status, web_app.HTTPStatus.UNPROCESSABLE_ENTITY)
        self.assertIn("Facebook", payload["error"])
        self.assertIn("cookies", payload["error"])
        can_fetch.assert_not_called()

    def test_resolve_cookies_file_rejects_youtube_cookie_for_douyin(self):
        import web_app

        with patch("web_app.Path.exists", return_value=True), patch("web_app.Path.is_file", return_value=True):
            with self.assertRaises(ValueError) as context:
                web_app.resolve_cookies_file("www.youtube.com_cookies.txt", "https://www.douyin.com/video/72630138283281")

        self.assertIn("不匹配", str(context.exception))

    def test_validate_cookies_content_requires_matching_netscape_file(self):
        import web_app

        valid = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tvalue\n"
        web_app.validate_cookies_content(valid, "youtube")

        with self.assertRaises(ValueError):
            web_app.validate_cookies_content("not a cookie file", "youtube")

        with self.assertRaises(ValueError):
            web_app.validate_cookies_content(valid, "douyin")

    def test_save_site_cookies_writes_site_default_file(self):
        import web_app

        valid = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tvalue\n"
        base = Path("E:/vibe-coding/paqu")

        with patch("web_app.Path.cwd", return_value=base), patch("web_app.Path.write_text") as write_text:
            target = web_app.save_site_cookies(valid, "https://www.youtube.com/watch?v=abc123")

        self.assertEqual(target, base / "www.youtube.com_cookies.txt")
        write_text.assert_called_once_with(valid, encoding="utf-8")

    def test_save_tiktok_cookies_writes_site_default_file(self):
        import web_app

        valid = "# Netscape HTTP Cookie File\n.tiktok.com\tTRUE\t/\tTRUE\t0\tsessionid\tvalue\n"
        base = Path("E:/vibe-coding/paqu")

        with patch("web_app.Path.cwd", return_value=base), patch("web_app.Path.write_text") as write_text:
            target = web_app.save_site_cookies(valid, "https://www.tiktok.com/@creator/video/123")

        self.assertEqual(target, base / "www.tiktok.com_cookies.txt")
        write_text.assert_called_once_with(valid, encoding="utf-8")

    def test_web_scan_localizes_tiktok_cookie_challenge(self):
        import web_app

        error = "[TikTok] 123: This post may not be comfortable for some audiences. Log in for access. Use --cookies"

        with patch("web_app.discover_ytdlp", return_value=YtdlpDiscovery([], error, True)), patch("web_app.can_fetch") as can_fetch:
            status, payload = web_app.scan_page_resources("https://www.tiktok.com/@creator/video/123", ["video"])

        self.assertEqual(status, web_app.HTTPStatus.UNPROCESSABLE_ENTITY)
        self.assertIn("TikTok 要求登录后访问", payload["error"])
        self.assertIn("导入当前网站 cookies", payload["error"])
        can_fetch.assert_not_called()

    def test_web_scan_localizes_curl_certificate_error(self):
        import web_app

        error = "[Instagram] Unable to download webpage: curl: (77) error setting certificate verify locations"

        with patch("web_app.discover_ytdlp", return_value=YtdlpDiscovery([], error, True)), patch("web_app.can_fetch") as can_fetch:
            status, payload = web_app.scan_page_resources("https://www.instagram.com/reels/example/", ["video"])

        self.assertEqual(status, web_app.HTTPStatus.UNPROCESSABLE_ENTITY)
        self.assertIn("TLS 证书配置异常", payload["error"])
        self.assertNotIn("C:\\Users", payload["error"])
        can_fetch.assert_not_called()

    def test_fresh_douyin_cookies_error_is_terminal_and_localized(self):
        import web_app

        error = "[Douyin] 741: Fresh cookies (not necessarily logged in) are needed"

        with patch("web_app.discover_ytdlp", return_value=YtdlpDiscovery([], error, True)), patch("web_app.can_fetch") as can_fetch:
            status, payload = web_app.scan_page_resources(
                "https://www.douyin.com/video/741", ["video"], Path("www.douyin.com_cookies.txt")
            )

        self.assertEqual(status, web_app.HTTPStatus.UNPROCESSABLE_ENTITY)
        self.assertIn("已失效", payload["error"])
        can_fetch.assert_not_called()

    def test_web_scan_localizes_youtube_cookie_challenge(self):
        import web_app

        error = "[youtube] abc123: Sign in to confirm you're not a bot."

        with patch("web_app.discover_ytdlp", return_value=YtdlpDiscovery([], error, True)), patch("web_app.can_fetch") as can_fetch:
            status, payload = web_app.scan_page_resources("https://www.youtube.com/watch?v=abc123", ["video"], Path("cookies.txt"))

        self.assertEqual(status, web_app.HTTPStatus.UNPROCESSABLE_ENTITY)
        self.assertIn("YouTube 要求登录态验证", payload["error"])
        self.assertIn("cookies.txt", payload["error"])
        can_fetch.assert_not_called()

    def test_web_scan_accepts_direct_media_url(self):
        import web_app

        with patch("web_app.can_fetch") as can_fetch:
            status, payload = web_app.scan_page_resources("https://finder.video.qq.com/video.mp4?token=abc", ["video"])

        self.assertEqual(status, web_app.HTTPStatus.OK)
        self.assertEqual(payload["resources"][0]["kind"], "direct")
        self.assertEqual(payload["resources"][0]["qualities"][0]["url"], "https://finder.video.qq.com/video.mp4?token=abc")
        can_fetch.assert_not_called()

    def test_web_scan_accepts_weixin_stodownload_url_as_direct_media(self):
        import web_app

        url = "https://finder.video.qq.com/251/20302/stodownload?encfilekey=abc&token=def"

        with patch("web_app.discover_ytdlp") as ytdlp, patch("web_app.can_fetch") as can_fetch:
            status, payload = web_app.scan_page_resources(url, ["video"])

        self.assertEqual(status, web_app.HTTPStatus.OK)
        self.assertEqual(payload["resources"][0]["kind"], "direct")
        self.assertEqual(payload["resources"][0]["qualities"][0]["url"], url)
        ytdlp.assert_not_called()
        can_fetch.assert_not_called()

    def test_web_scan_converts_weixin_sph_to_stodownload_url(self):
        import web_app

        response = {
            "data": {
                "feedInfo": {
                    "description": "Sample feed",
                    "h264VideoInfo": {
                        "videoUrl": "https://finder.video.qq.com/251/20302/stodownload?encfilekey=abc&token=def"
                    },
                },
                "errMsg": {"type": 0},
            }
        }

        with patch("web_app.discover_ytdlp") as ytdlp, patch("video_crawler.weixin_finder_request", return_value=response), patch(
            "web_app.can_fetch"
        ) as can_fetch:
            status, payload = web_app.scan_page_resources("https://weixin.qq.com/sph/AAzecvr4oP", ["video"])

        self.assertEqual(status, web_app.HTTPStatus.OK)
        self.assertEqual(payload["resources"][0]["qualities"][0]["url"], response["data"]["feedInfo"]["h264VideoInfo"]["videoUrl"])
        ytdlp.assert_not_called()
        can_fetch.assert_not_called()

    def test_web_scan_uses_scrapling_for_selected_image_and_text(self):
        import web_app

        image = VideoResource(
            url="https://example.com/a.jpg",
            source="img[src]",
            kind="image",
            content_type="image",
        )
        text = VideoResource(
            url="https://example.com",
            source="p::text",
            kind="paragraph",
            content_type="text",
            text="Hello world",
            preview="Hello world",
        )

        with patch("web_app.discover_ytdlp") as ytdlp, patch("web_app.can_fetch", return_value=True), patch(
            "web_app.discover_scrapling_content", return_value=[image, text]
        ) as scrapling_content:
            status, payload = web_app.scan_page_resources("https://example.com", ["image", "text"])

        self.assertEqual(status, web_app.HTTPStatus.OK)
        self.assertEqual([item["content_type"] for item in payload["resources"]], ["image", "text"])
        ytdlp.assert_not_called()
        scrapling_content.assert_called_once()

    def test_web_scan_combines_ytdlp_video_with_scrapling_text(self):
        import web_app

        video = VideoResource(
            url="https://example.com/watch",
            source="yt-dlp: Sample",
            kind="ytdlp",
            qualities=[VideoQuality(label="最佳 MP4", url="https://example.com/watch", kind="ytdlp", format_id="best")],
        )
        text = VideoResource(
            url="https://example.com/watch",
            source="title::text",
            kind="title",
            content_type="text",
            text="Sample title",
        )

        with patch("web_app.discover_ytdlp", return_value=YtdlpDiscovery([video])), patch("web_app.can_fetch", return_value=True), patch(
            "web_app.discover_scrapling_content", return_value=[text]
        ) as scrapling_content:
            status, payload = web_app.scan_page_resources("https://example.com/watch", ["video", "text"])

        self.assertEqual(status, web_app.HTTPStatus.OK)
        self.assertEqual([item["content_type"] for item in payload["resources"]], ["video", "text"])
        scrapling_content.assert_called_once()

    def test_web_scan_returns_forbidden_for_access_gate(self):
        import web_app

        with patch("web_app.discover_ytdlp", return_value=YtdlpDiscovery([])), patch("web_app.can_fetch", return_value=True), patch(
            "web_app.discover_scrapling_content", side_effect=PermissionError("需要登录")
        ):
            status, payload = web_app.scan_page_resources("https://www.alibaba.com/product-detail/item.html", ["image"])

        self.assertEqual(status, web_app.HTTPStatus.FORBIDDEN)
        self.assertIn("需要登录", payload["error"])

    def test_web_scan_reports_terminal_ytdlp_error_without_scrapling_fallback(self):
        import web_app

        with patch(
            "web_app.discover_ytdlp",
            return_value=YtdlpDiscovery([], "[youtube] abc123: Video unavailable", True),
        ), patch("web_app.can_fetch") as can_fetch, patch("web_app.discover_scrapling_content") as scrapling_content:
            status, payload = web_app.scan_page_resources("https://youtube.com/shorts/abc123", ["video"])

        self.assertEqual(status, web_app.HTTPStatus.UNPROCESSABLE_ENTITY)
        self.assertIn("Video unavailable", payload["error"])
        can_fetch.assert_not_called()
        scrapling_content.assert_not_called()

    def test_cancel_task_marks_running_task_cancelled(self):
        import web_app

        with web_app.TASKS_LOCK:
            web_app.TASKS.clear()
            task = web_app.DownloadTask(
                id="task-1",
                url="https://example.com/video",
                quality_label="最佳 MP4",
                kind="ytdlp",
                format_id="best",
                output_dir="downloads",
                status="running",
            )
            web_app.TASKS[task.id] = task

        cancelled = web_app.cancel_task("task-1")

        self.assertTrue(cancelled.cancel_requested)
        self.assertEqual(cancelled.status, "running")
        self.assertTrue(web_app.is_task_cancelled("task-1"))

    def test_remove_task_deletes_finished_task_record(self):
        import web_app

        with web_app.TASKS_LOCK:
            web_app.TASKS.clear()
            task = web_app.DownloadTask(
                id="task-finished",
                url="https://example.com/video",
                quality_label="最佳 MP4",
                kind="ytdlp",
                format_id="best",
                output_dir="downloads",
                status="done",
            )
            web_app.TASKS[task.id] = task

        removed, error = web_app.remove_task("task-finished")

        self.assertTrue(removed)
        self.assertIsNone(error)
        with web_app.TASKS_LOCK:
            self.assertNotIn("task-finished", web_app.TASKS)

    def test_remove_task_rejects_active_task(self):
        import web_app

        with web_app.TASKS_LOCK:
            web_app.TASKS.clear()
            task = web_app.DownloadTask(
                id="task-running",
                url="https://example.com/video",
                quality_label="最佳 MP4",
                kind="ytdlp",
                format_id="best",
                output_dir="downloads",
                status="running",
            )
            web_app.TASKS[task.id] = task

        removed, error = web_app.remove_task("task-running")

        self.assertFalse(removed)
        self.assertIn("只能删除", error)
        with web_app.TASKS_LOCK:
            self.assertIn("task-running", web_app.TASKS)

    def test_folder_for_task_prefers_saved_file_parent(self):
        import web_app

        task = web_app.DownloadTask(
            id="task-2",
            url="https://example.com/video",
            quality_label="最佳 MP4",
            kind="ytdlp",
            format_id="best",
            output_dir="downloads",
            saved_to="downloads/video.mp4",
        )

        self.assertEqual(web_app.folder_for_task(task), Path("downloads"))

    def test_folder_for_completed_agent_candidate(self):
        import web_app

        candidate = {"download_status": "done", "saved_to": "downloads/topic/video.mp4"}
        with patch.object(Path, "is_file", return_value=True):
            self.assertEqual(web_app.folder_for_candidate(candidate), Path("downloads/topic"))

    def test_folder_for_agent_candidate_rejects_unfinished_download(self):
        import web_app

        with self.assertRaisesRegex(ValueError, "尚未下载完成"):
            web_app.folder_for_candidate({"download_status": "downloading", "saved_to": "downloads/video.mp4"})

    def test_open_folder_uses_explorer(self):
        import web_app

        with patch.object(Path, "exists", return_value=True), patch.object(Path, "is_dir", return_value=True), patch(
            "web_app.subprocess.Popen"
        ) as popen:
            web_app.open_folder(Path("downloads"))

        popen.assert_called_once_with(["explorer", "downloads"])

    def test_resolves_relative_output_dir(self):
        import web_app

        with patch.object(Path, "mkdir") as mkdir:
            output_dir = web_app.resolve_output_dir("custom-downloads")

        self.assertEqual(output_dir, Path.cwd() / "custom-downloads")
        mkdir.assert_called_once()

    def test_main_opens_agent_page_when_launcher_enables_browser(self):
        import web_app

        class FakeServer:
            def serve_forever(self):
                return None

            def server_close(self):
                return None

        class ImmediateTimer:
            def __init__(self, interval, function, args=()):
                self.function = function
                self.args = args

            def start(self):
                self.function(*self.args)

        with patch.dict(web_app.os.environ, {"PAQU_OPEN_BROWSER": "1"}), patch(
            "web_app.ThreadingHTTPServer", return_value=FakeServer()
        ), patch("web_app.threading.Timer", ImmediateTimer), patch("web_app.webbrowser.open") as browser_open:
            self.assertEqual(web_app.main(), 0)

        browser_open.assert_called_once_with(f"http://{web_app.HOST}:{web_app.PORT}/agent")

    def test_rag_management_is_separate_from_agent_page(self):
        import web_app

        self.assertIn('href="/rag"', web_app.AGENT_HTML)
        self.assertNotIn('id="uploadRag"', web_app.AGENT_HTML)
        self.assertIn('id="upload"', web_app.RAG_HTML)
        self.assertIn('accept="video/*" multiple', web_app.RAG_HTML)
        self.assertIn("for(let i=0;i<files.length;i++)", web_app.RAG_HTML)
        self.assertIn("data-save", web_app.RAG_HTML)
        self.assertIn("data-delete", web_app.RAG_HTML)


if __name__ == "__main__":
    unittest.main()
