import unittest

from PIL import Image

import visual_rag


class VisualRagTests(unittest.TestCase):
    def test_difference_hash_is_stable_for_same_image(self):
        image = Image.new("RGB", (32, 32), "black")
        self.assertEqual(visual_rag.difference_hash(image), visual_rag.difference_hash(image.copy()))

    def test_best_match_returns_nearest_sample(self):
        sample_id, score = visual_rag.best_match("0000000000000000", [(1, ["ffffffffffffffff"]), (2, ["0000000000000000"])])
        self.assertEqual(sample_id, 2)
        self.assertEqual(score, 1.0)


if __name__ == "__main__":
    unittest.main()
