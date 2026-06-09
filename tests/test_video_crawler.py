import unittest
import time
from pathlib import Path
from unittest.mock import patch

from video_crawler import (
    VideoHTMLParser,
    VideoResource,
    VideoQuality,
    extract_regex_candidates,
    fetch_text,
    convert_to_mp4,
    describe_video_candidate,
    discover_video_resources,
    parse_hls_master_playlist,
    parse_hls_media_segments,
    unique_candidates,
    VideoCandidate,
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

    def test_web_scan_prefers_ytdlp_before_robots(self):
        import web_app

        resource = VideoResource(
            url="https://example.com/watch",
            source="yt-dlp: Sample",
            kind="ytdlp",
            qualities=[VideoQuality(label="最佳 MP4", url="https://example.com/watch", kind="ytdlp", format_id="best")],
        )

        with patch("web_app.discover_ytdlp_resources", return_value=[resource]), patch("web_app.can_fetch") as can_fetch:
            status, payload = web_app.scan_page_resources("https://example.com/watch")

        self.assertEqual(status, web_app.HTTPStatus.OK)
        self.assertEqual(payload["resources"][0]["kind"], "ytdlp")
        can_fetch.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
