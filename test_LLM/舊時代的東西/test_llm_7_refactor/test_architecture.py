import unittest
from pathlib import Path

from reply_generator import (
    clean_generated_reply,
    detect_crowd_signal_hints,
    extract_crowd_response_tokens,
    find_matching_candidate,
    normalize_reply_for_compare,
    replies_are_near_duplicates,
    validate_generated_reply,
)
from vector_search import cosine_similarity, serialize_candidate


ROOT = Path(__file__).resolve().parent


class VectorSearchTests(unittest.TestCase):
    def test_cosine_similarity(self):
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [1.0, 0.0]), 1.0)
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [0.0, 1.0]), 0.0)
        self.assertEqual(cosine_similarity([1.0], [1.0, 2.0]), 0.0)

    def test_candidate_serialization_excludes_disabled(self):
        disabled = {
            "_id": "abc",
            "text": "測試",
            "embedding": [1.0],
            "enabled": False,
        }
        self.assertIsNone(serialize_candidate("user", disabled))


class GeneratorTests(unittest.TestCase):
    def test_clean_reply(self):
        self.assertEqual(clean_generated_reply("觀眾：好漂亮"), "好漂亮")

    def test_max_length(self):
        self.assertTrue(validate_generated_reply("好漂亮")[0])
        self.assertFalse(validate_generated_reply("這句留言真的已經超過限制")[0])

    def test_candidate_copy_normalization(self):
        self.assertEqual(normalize_reply_for_compare("我 來了！"), "我來了")
        candidates = [{"text": "我來了"}, {"text": "+1"}]
        self.assertEqual(
            find_matching_candidate("我 來了！", candidates)["text"], "我來了"
        )
        self.assertIsNone(find_matching_candidate("今天也來了", candidates))

    def test_near_duplicate_and_audience_role_validation(self):
        self.assertTrue(
            replies_are_near_duplicates("這條褲子超適合", "這條褲子好適合")
        )
        self.assertFalse(replies_are_near_duplicates("黑色有嗎", "粉色有嗎"))
        self.assertFalse(validate_generated_reply("什麼尺寸的妹妹")[0])
        self.assertEqual(
            validate_generated_reply("什麼尺寸的妹妹")[1],
            "invalid_audience_address",
        )

    def test_crowd_signal_hint_is_soft_but_high_attention(self):
        hints = detect_crowd_signal_hints(
            "外面賣320，我們只要220，快上車220，上車220"
        )
        self.assertEqual(hints["attention"], "high")
        self.assertGreaterEqual(hints["keyword_counts"]["上車"], 2)
        self.assertTrue(hints["explicit_patterns"])
        self.assertFalse(hints["is_hard_decision"])

    def test_incidental_keyword_does_not_become_explicit_decision(self):
        hints = detect_crowd_signal_hints("衣服扣子很好看，加上外套也可以")
        self.assertEqual(hints["attention"], "watch")
        self.assertFalse(hints["explicit_patterns"])
        self.assertFalse(hints["is_hard_decision"])

    def test_crowd_command_is_reduced_to_viewer_token(self):
        self.assertEqual(extract_crowd_response_tokens("幫我刷888留言"), ["888"])
        self.assertEqual(extract_crowd_response_tokens("想了解的扣6"), ["6"])
        self.assertEqual(extract_crowd_response_tokens("要的加1"), ["+1"])
        self.assertEqual(extract_crowd_response_tokens("快上車220"), ["上車220"])


class ArchitectureTests(unittest.TestCase):
    def test_policy_module_is_not_used(self):
        source = (ROOT / "live_stream_llm.py").read_text(encoding="utf-8")
        self.assertNotIn("reply_policy", source)
        self.assertNotIn("evaluate_reply_policy", source)

    def test_vector_logic_is_separated(self):
        live_source = (ROOT / "live_stream_llm.py").read_text(encoding="utf-8")
        vector_source = (ROOT / "vector_search.py").read_text(encoding="utf-8")
        self.assertIn("from vector_search import", live_source)
        self.assertNotIn("def cosine_similarity", live_source)
        self.assertIn("def cosine_similarity", vector_source)

    def test_candidate_revision_is_connected(self):
        user_source = (ROOT / "user_input.py").read_text(encoding="utf-8")
        vector_source = (ROOT / "vector_search.py").read_text(encoding="utf-8")
        self.assertIn("bump_candidate_revision", user_source)
        self.assertIn('"_id": "candidate_revision"', vector_source)

    def test_generator_receives_candidates(self):
        live_source = (ROOT / "live_stream_llm.py").read_text(encoding="utf-8")
        generator_source = (ROOT / "reply_generator.py").read_text(encoding="utf-8")
        self.assertIn("generate_account_replies", live_source)
        self.assertIn("candidates=reference_candidates", live_source)
        self.assertIn("候選禁用原句", generator_source)

    def test_per_account_modes_exist(self):
        source = (ROOT / "live_stream_llm.py").read_text(encoding="utf-8")
        for mode in ("qwen_only", "hybrid", "vector_only"):
            self.assertIn(mode, source)
        self.assertIn("vector_search_performed", source)

    def test_hybrid_threshold_styles_and_buffer_are_connected(self):
        live_source = (ROOT / "live_stream_llm.py").read_text(encoding="utf-8")
        vector_source = (ROOT / "vector_search.py").read_text(encoding="utf-8")
        generator_source = (ROOT / "reply_generator.py").read_text(encoding="utf-8")
        self.assertIn('"SIMILARITY_THRESHOLD", "0.8"', vector_source)
        self.assertIn('"MODEL_MIN_INPUT_CHARS", "100"', live_source)
        self.assertIn('"reaction": int', live_source)
        self.assertNotIn('"request": int', live_source)
        self.assertIn('REPLY_STYLES = {"reaction", "question"}', generator_source)
        self.assertNotIn('"crowd_response": int', live_source)
        self.assertIn("account_styles=", live_source)
        self.assertIn('"crowd_response": crowd_response', live_source)
        self.assertIn('parsed_output["crowd"]', generator_source)
        self.assertIn("detect_crowd_signal_hints", generator_source)
        self.assertIn("哄台訊號預掃描", generator_source)
        self.assertIn('MAX_REPLY_CHARS", "7"', generator_source)

    def test_stream_product_and_learned_qwen_examples_are_connected(self):
        live_source = (ROOT / "live_stream_llm.py").read_text(encoding="utf-8")
        vector_source = (ROOT / "vector_search.py").read_text(encoding="utf-8")
        self.assertIn('"LIVE_DATABASE_PREFIX"', vector_source)
        self.assertIn('"MIN_VECTOR_EXAMPLES"', vector_source)
        self.assertIn("def _live_database_name", vector_source)
        self.assertIn('"static_candidates_compared": 0', vector_source)
        self.assertIn("def store_generated_examples", vector_source)
        self.assertIn('"source": "qwen_example"', vector_source)
        self.assertIn("stream_id=stream_id", live_source)
        self.assertIn("product_type=product_type", live_source)
        self.assertIn("candidate_search.store_generated_examples", live_source)
        self.assertIn('@app.route("/stream_product"', live_source)
        generator_source = (ROOT / "reply_generator.py").read_text(encoding="utf-8")
        self.assertIn('"evidence": {"type": "string"}', generator_source)
        self.assertIn('"evidence_not_in_source"', generator_source)

    def test_cls_memory_is_session_scoped_and_reaches_qwen(self):
        live_source = (ROOT / "live_stream_llm.py").read_text(encoding="utf-8")
        generator_source = (ROOT / "reply_generator.py").read_text(encoding="utf-8")
        cache_source = (ROOT / "cls_memory.py").read_text(encoding="utf-8")
        self.assertIn("observe_cls_memory", live_source)
        self.assertIn("get_stream_process_lock", live_source)
        self.assertIn("stream_context=cls_prompt_context", live_source)
        self.assertIn("CLS_CACHE.clear_session(stream_id)", live_source)
        self.assertIn("turn_id already exists with different text", cache_source)
        self.assertIn("本場歷史參考", generator_source)


if __name__ == "__main__":
    unittest.main()
