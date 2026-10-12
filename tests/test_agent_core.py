import tempfile
import unittest
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import agent_core


class AgentCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = agent_core.AgentStore(self.root / "agent.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_topics_contain_bilingual_queries(self):
        self.assertEqual(len(agent_core.TOPICS), 3)
        for topic in agent_core.TOPICS:
            queries = agent_core.PresetKeywordGenerator().generate(topic)
            self.assertTrue(any(any("\u4e00" <= char <= "\u9fff" for char in query) for query in queries))
            self.assertTrue(any(query.isascii() for query in queries))
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0], 3)

    def test_rule_judge_returns_structured_explanation(self):
        topic = agent_core.TOPICS[0]
        result = agent_core.RuleRelevanceJudge().judge(topic, {
            "title": "Mooring line snap back accident on vessel",
            "description": "A ship mooring rope failure at the dock",
            "tags": ["safety"],
        })
        self.assertTrue(result.relevant)
        self.assertEqual(result.level, "高相关")
        self.assertIn("mooring line snap", result.matched_terms)
        self.assertTrue(result.reason)

    def test_hybrid_ai_judge_combines_text_and_visual_results(self):
        topic = agent_core.TOPICS[0]
        metadata = {"title": "Mooring incident", "thumbnail": "https://example.com/t.jpg"}
        text = {"relevant": True, "confidence": 0.9, "level": "高相关", "matched_terms": ["mooring"], "excluded_terms": [], "reason": "文字匹配"}
        visual = {"relevant": True, "confidence": 0.7, "level": "待审核", "visual_evidence": ["船舶缆绳"], "reason": "单张图无法确认断裂"}
        with patch("agent_core.deepseek_metadata_judge", return_value=text), patch("agent_core.qwen_thumbnail_judge", return_value=visual):
            result = agent_core.HybridAIJudge().judge(topic, metadata)
        self.assertEqual(result.level, "待审核")
        self.assertEqual(result.score, 70)
        self.assertIn("DeepSeek", result.reason)
        self.assertIn("Qwen视觉", result.reason)

    def test_hybrid_ai_judge_falls_back_when_api_fails(self):
        topic = agent_core.TOPICS[0]
        with patch("agent_core.deepseek_metadata_judge", side_effect=RuntimeError("timeout")):
            result = agent_core.HybridAIJudge().judge(topic, {"title": "Mooring line snap"})
        self.assertTrue(result.relevant)
        self.assertIn("回退本地规则", result.reason)

    def test_exclusion_prevents_false_positive(self):
        topic = agent_core.TOPICS[0]
        result = agent_core.RuleRelevanceJudge().judge(topic, {"title": "Fishing rope tutorial knot"})
        self.assertFalse(result.relevant)
        self.assertEqual(result.level, "不相关")
        self.assertIn("fishing", result.excluded_terms)

    def test_recent_filter_uses_upload_date_and_timestamp(self):
        today = date(2026, 8, 13)
        self.assertTrue(agent_core.is_recent({"upload_date": "20260801"}, 30, today))
        self.assertFalse(agent_core.is_recent({"upload_date": "20260101"}, 30, today))
        stamp = datetime(2026, 8, 10, tzinfo=timezone.utc).timestamp()
        self.assertTrue(agent_core.is_recent({"timestamp": stamp}, 30, today))

    def test_search_uses_supported_ytdlp_search_prefix(self):
        captured = {}

        class FakeYDL:
            def __init__(self, options):
                captured["options"] = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def extract_info(self, url, download):
                captured["url"] = url
                return {"entries": []}

        with patch("agent_core.YoutubeDL", FakeYDL):
            agent_core.search_youtube("mooring line snap", 3, 30)
        self.assertIn("youtube.com/results?search_query=mooring+line+snap", captured["url"])
        self.assertFalse(captured["options"]["extract_flat"])

    def test_dynamic_quota_fills_short_topic_and_caps_at_limit(self):
        counter = 0

        def searcher(query, limit, window_days, cookies_file):
            nonlocal counter
            counter += 1
            # Rescue searches are intentionally empty; other topics fill the shortage.
            if "rescue" in query.lower() or "救援" in query or "救生" in query:
                return []
            return [
                {
                    "id": f"v{counter}-{index}",
                    "title": query,
                    "upload_date": date.today().strftime("%Y%m%d"),
                    "description": query,
                }
                for index in range(limit)
            ]

        result = agent_core.run_discovery(self.store, limit=10, force=True, searcher=searcher)
        self.assertEqual(result["candidate_count"], 10)
        self.assertEqual(result["new_count"], 10)

    def test_all_search_failures_mark_run_failed(self):
        def failing_searcher(query, limit, window_days, cookies_file):
            raise OSError("network unavailable")

        with self.assertRaisesRegex(RuntimeError, "所有 YouTube 搜索均失败"):
            agent_core.run_discovery(self.store, limit=3, force=True, searcher=failing_searcher)
        self.assertEqual(self.store.list_runs()[0]["status"], "failed")

    def test_video_id_and_url_are_idempotent(self):
        topic = agent_core.TOPICS[0]
        run_id = self.store.start_run("2026-08-13")
        item = {"id": "abc123", "title": "Mooring line snap", "upload_date": "20260810"}
        judged = agent_core.RuleRelevanceJudge().judge(topic, item)
        first_id, created = self.store.upsert_candidate(run_id, topic, "query", item, judged)
        second_id, created_again = self.store.upsert_candidate(run_id, topic, "query", item, judged)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first_id, second_id)

    def test_similar_title_marks_possible_duplicate(self):
        topic = agent_core.TOPICS[2]
        run_id = self.store.start_run("2026-08-13")
        judge = agent_core.RuleRelevanceJudge()
        first = {"id": "one", "title": "Lifting sling breaking load test 50 tons"}
        second = {"id": "two", "title": "Lifting sling breaking load test - 50 tons"}
        first_id, _ = self.store.upsert_candidate(run_id, topic, "q", first, judge.judge(topic, first))
        second_id, _ = self.store.upsert_candidate(run_id, topic, "q", second, judge.judge(topic, second))
        row = next(item for item in self.store.list_candidates() if item["id"] == second_id)
        self.assertEqual(row["duplicate_of"], first_id)

    def test_download_requires_kept_review_status(self):
        topic = agent_core.TOPICS[0]
        run_id = self.store.start_run("2026-08-13")
        item = {"id": "abc123", "title": "Mooring line snap"}
        candidate_id, _ = self.store.upsert_candidate(run_id, topic, "q", item, agent_core.RuleRelevanceJudge().judge(topic, item))
        self.assertEqual(self.store.get_candidates_for_download([candidate_id]), [])
        self.store.update_review([candidate_id], "kept")
        self.assertEqual(len(self.store.get_candidates_for_download([candidate_id])), 1)

    def test_selected_candidate_can_be_kept_and_downloaded_in_one_action(self):
        topic = agent_core.TOPICS[0]
        run_id = self.store.start_run("2026-08-15")
        item = {"id": "direct-download", "title": "Mooring line snap"}
        candidate_id, _ = self.store.upsert_candidate(run_id, topic, "q", item, agent_core.RuleRelevanceJudge().judge(topic, item))
        self.store.keep_for_download([candidate_id])
        rows = self.store.get_candidates_for_download([candidate_id])
        self.assertEqual([row["id"] for row in rows], [candidate_id])

    def test_soft_deleted_candidate_cannot_be_revived_by_download_action(self):
        topic = agent_core.TOPICS[0]
        run_id = self.store.start_run("2026-08-15")
        item = {"id": "deleted-direct-download", "title": "Mooring line snap"}
        candidate_id, _ = self.store.upsert_candidate(run_id, topic, "q", item, agent_core.RuleRelevanceJudge().judge(topic, item))
        self.store.update_review([candidate_id], "deleted")
        self.store.keep_for_download([candidate_id])
        self.assertEqual(self.store.get_candidates_for_download([candidate_id]), [])

    def test_completed_download_is_not_changed_back_to_queued(self):
        topic = agent_core.TOPICS[2]
        run_id = self.store.start_run("2026-08-14")
        item = {"id": "already-downloaded", "title": "Lifting sling breaking load test"}
        candidate_id, _ = self.store.upsert_candidate(run_id, topic, "q", item, agent_core.RuleRelevanceJudge().judge(topic, item))
        self.store.update_review([candidate_id], "kept")
        self.store.set_download_result(candidate_id, "done", "saved.mp4", "digest")
        self.assertEqual(self.store.get_candidates_for_download([candidate_id]), [])
        row = self.store.list_candidates({"review": "kept"})[0]
        self.assertEqual(row["download_status"], "done")
        self.assertEqual(row["saved_to"], "saved.mp4")

    def test_download_progress_is_persisted_and_completed_at_100_percent(self):
        topic = agent_core.TOPICS[2]
        run_id = self.store.start_run("2026-08-15")
        item = {"id": "progress-video", "title": "Lifting sling breaking load test"}
        candidate_id, _ = self.store.upsert_candidate(run_id, topic, "q", item, agent_core.RuleRelevanceJudge().judge(topic, item))
        self.store.update_review([candidate_id], "kept")
        self.store.get_candidates_for_download([candidate_id])
        self.store.set_download_progress(candidate_id, 25, 100)
        row = self.store.list_candidates({"review": "kept"})[0]
        self.assertEqual(row["download_status"], "downloading")
        self.assertEqual(row["downloaded_bytes"], 25)
        self.assertEqual(row["total_bytes"], 100)
        self.store.set_download_result(candidate_id, "done", "saved.mp4", "digest")
        row = self.store.list_candidates({"review": "kept"})[0]
        self.assertEqual(row["downloaded_bytes"], 100)
        self.assertEqual(row["total_bytes"], 100)

    def test_existing_database_is_migrated_with_progress_columns(self):
        legacy_path = self.root / "legacy.sqlite3"
        conn = sqlite3.connect(legacy_path)
        try:
            conn.execute(
                "CREATE TABLE candidates (id INTEGER PRIMARY KEY, topic_key TEXT, relevance TEXT, "
                "review_status TEXT, first_seen_at TEXT)"
            )
            conn.commit()
        finally:
            conn.close()
        agent_core.AgentStore(legacy_path)
        conn = sqlite3.connect(legacy_path)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(candidates)")}
        finally:
            conn.close()
        self.assertIn("downloaded_bytes", columns)
        self.assertIn("total_bytes", columns)
        self.assertIn("rag_similarity", columns)
        self.assertIn("rag_sample_id", columns)

    def test_rag_sample_round_trip(self):
        sample_id = self.store.add_rag_sample("example.mp4", self.root / "example.mp4", ["0" * 16, "f" * 16])
        samples = self.store.list_rag_samples()
        self.assertEqual(samples[0]["id"], sample_id)
        self.assertEqual(samples[0]["frame_count"], 2)
        self.assertEqual(self.store.rag_hashes(), [(sample_id, ["0" * 16, "f" * 16])])

    def test_rag_sample_can_be_renamed_queried_and_deleted(self):
        sample_id = self.store.add_rag_sample("old.mp4", self.root / "old.mp4", ["0" * 16])
        self.assertTrue(self.store.rename_rag_sample(sample_id, "目标事故样本"))
        sample = self.store.get_rag_sample(sample_id)
        self.assertEqual(sample["name"], "目标事故样本")
        self.assertEqual(sample["frame_count"], 1)
        self.assertTrue(self.store.delete_rag_sample(sample_id))
        self.assertIsNone(self.store.get_rag_sample(sample_id))

    def test_soft_deleted_candidate_is_hidden_and_cannot_download(self):
        topic = agent_core.TOPICS[0]
        run_id = self.store.start_run("2026-08-13")
        item = {"id": "deleted-video", "title": "Mooring line snap"}
        candidate_id, _ = self.store.upsert_candidate(run_id, topic, "q", item, agent_core.RuleRelevanceJudge().judge(topic, item))
        self.store.update_review([candidate_id], "kept")
        self.store.update_review([candidate_id], "deleted")
        self.assertNotIn(candidate_id, [row["id"] for row in self.store.list_candidates()])
        deleted = self.store.list_candidates({"review": "deleted"})
        self.assertEqual([row["id"] for row in deleted], [candidate_id])
        self.assertEqual(self.store.get_candidates_for_download([candidate_id]), [])

    def test_sha256_duplicate_and_csv_bom(self):
        topic = agent_core.TOPICS[0]
        run_id = self.store.start_run("2026-08-13")
        judge = agent_core.RuleRelevanceJudge()
        ids = []
        for video_id in ("a", "b"):
            item = {"id": video_id, "title": f"Mooring line snap {video_id}"}
            candidate_id, _ = self.store.upsert_candidate(run_id, topic, "q", item, judge.judge(topic, item))
            ids.append(candidate_id)
        self.store.set_download_result(ids[0], "done", "a.mp4", "samehash")
        self.store.set_download_result(ids[1], "done", "b.mp4", "samehash")
        second = next(item for item in self.store.list_candidates() if item["id"] == ids[1])
        self.assertEqual(second["duplicate_of"], ids[0])
        csv_path = self.store.export_csv(self.root / "out.csv")
        self.assertTrue(csv_path.read_bytes().startswith(b"\xef\xbb\xbf"))


if __name__ == "__main__":
    unittest.main()
