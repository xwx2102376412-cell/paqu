import tempfile
import unittest
import json
from pathlib import Path

from download_history import DownloadHistory, canonicalize_url, frame_set_similarity


class DownloadHistoryTests(unittest.TestCase):
    def test_canonical_url_discards_tracking_parameters(self):
        self.assertEqual(
            canonicalize_url("https://www.youtube.com/watch?v=abc&utm_source=x"),
            "https://youtube.com/watch?v=abc",
        )

    def test_completed_and_duplicate_are_known_but_failure_is_retryable(self):
        with tempfile.TemporaryDirectory() as temp:
            history = DownloadHistory(Path(temp) / "history.sqlite3")
            first = {"platform": "youtube", "id": "a", "url": "https://youtube.com/watch?v=a", "topic": "x", "title": "one"}
            failed = {"platform": "youtube", "id": "b", "url": "https://youtube.com/watch?v=b", "topic": "x", "title": "two"}
            duplicate = {"platform": "bilibili", "id": "c", "url": "https://bilibili.com/video/c", "topic": "x", "title": "copy"}
            first_id = history.record_completed(first, Path(temp) / "one.mp4", "abc", ["0000000000000000"])
            history.record_attempt(failed, "failed", "network")
            history.record_duplicate(duplicate, first_id, ["0000000000000000"], "abc")
            self.assertIsNotNone(history.known("youtube", "a", first["url"]))
            self.assertIsNone(history.known("youtube", "b", failed["url"]))
            self.assertIsNotNone(history.known("bilibili", "c", duplicate["url"]))
            self.assertEqual(history.stats(), {"completed": 1, "duplicates": 1, "failed_attempts": 1})

    def test_frame_similarity_detects_matching_subset(self):
        left = ["0000000000000000", "ffffffffffffffff", "aaaaaaaaaaaaaaaa"]
        right = ["0000000000000000", "ffffffffffffffff", "aaaaaaaaaaaaaaaa", "1234567890abcdef"]
        self.assertGreaterEqual(frame_set_similarity(left, right), 0.99)

    def test_import_keeps_success_history_when_local_file_was_moved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report = root / "report.json"
            report.write_text(json.dumps({"downloaded": [{
                "platform": "youtube", "id": "moved", "url": "https://youtube.com/watch?v=moved",
                "topic": "x", "title": "moved video", "saved_to": str(root / "no-longer-here.mp4"),
            }]}), encoding="utf-8")
            history = DownloadHistory(root / "history.sqlite3")
            self.assertEqual(history.import_report(report), {"imported": 1, "already_known": 0, "metadata_only": 1})
            self.assertIsNotNone(history.known("youtube", "moved", "https://youtube.com/watch?v=moved"))


if __name__ == "__main__":
    unittest.main()
