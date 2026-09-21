import unittest

from cls_memory import CLSCache, EncoderSpec, MemoryReader, format_live_memory


SPEC = EncoderSpec("test-model", "test-revision", 2)


class CLSMemoryTests(unittest.TestCase):
    def setUp(self):
        self.cache = CLSCache(SPEC, max_turns=3)

    def observe(self, session, turn_id, text, vector):
        return self.cache.observe(session, text, vector, SPEC, turn_id)

    def test_sessions_are_isolated(self):
        self.observe("live-a", "1", "紅色上衣", [1.0, 0.0])
        self.observe("live-b", "1", "黑色長褲", [0.0, 1.0])
        self.assertEqual(self.cache.count("live-a"), 1)
        self.assertEqual(self.cache.count("live-b"), 1)

    def test_stable_turn_id_does_not_store_twice(self):
        first, _, stored = self.observe("live", "turn-1", "紅色上衣", [1.0, 0.0])
        retry, history, stored_again = self.observe(
            "live", "turn-1", "紅色上衣", [1.0, 0.0]
        )
        self.assertEqual(first, retry)
        self.assertEqual(history, ())
        self.assertTrue(stored)
        self.assertFalse(stored_again)
        self.assertEqual(self.cache.count("live"), 1)

    def test_reader_excludes_current_and_combines_recent_semantic(self):
        self.observe("live", "1", "紅色上衣", [1.0, 0.0])
        self.observe("live", "2", "黑色長褲", [0.0, 1.0])
        current, history, _ = self.observe(
            "live", "3", "這件紅色很好看", [0.9, 0.1]
        )
        rows = MemoryReader(top_k=1, recent_k=1, max_chars=100).read(
            current, history
        )
        ids = {row["turn_id"] for row in rows}
        self.assertEqual(ids, {"1", "2"})
        self.assertNotIn("3", ids)
        self.assertIn("紅色上衣", format_live_memory(rows))

    def test_cache_is_bounded(self):
        for number in range(5):
            self.observe(
                "live",
                str(number),
                f"片段{number}",
                [1.0, float(number + 1)],
            )
        self.assertEqual(self.cache.count("live"), 3)


if __name__ == "__main__":
    unittest.main()
