import unittest
import threading
from pathlib import Path
from unittest.mock import patch

from reply_generator import (
    clean_generated_reply,
    compose_crowd_reply,
    detect_crowd_signal_hints,
    extract_contextual_crowd_attributes,
    extract_crowd_response_tokens,
    find_matching_candidate,
    normalize_reply_for_compare,
    source_contains_exact_slogan,
    replies_are_near_duplicates,
    validate_generated_reply,
)
from vector_search import CandidateVectorSearch, cosine_similarity, serialize_candidate


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

    def test_fixed_search_only_ranks_allowed_database_rows(self):
        search = CandidateVectorSearch.__new__(CandidateVectorSearch)
        search._lock = threading.RLock()
        search._cache = [
            {
                "key": "default:1",
                "source": "default",
                "id": "1",
                "text": "這件好好看",
                "embedding": [1.0, 0.0],
                "weight": 1.0,
                "multi_output": False,
            },
            {
                "key": "crowd:2",
                "source": "crowd",
                "id": "2",
                "text": "+1",
                "embedding": [0.0, 1.0],
                "weight": 1.0,
                "multi_output": True,
            },
        ]
        search.refresh = lambda: {
            "cached_reply_count": 2,
            "candidate_revision": 1,
            "refreshed": False,
        }
        with patch("vector_search.get_embeddings", return_value=[[1.0, 0.0]]):
            result = search.search_fixed_candidates(
                "主播正在介紹衣服",
                allowed_keys={"default:1"},
                threshold=0.5,
            )
        self.assertEqual([item["key"] for item in result["candidates"]], ["default:1"])
        self.assertEqual(result["static_candidates_compared"], 1)

    def test_fixed_search_generates_missing_crowd_embedding_once(self):
        search = CandidateVectorSearch.__new__(CandidateVectorSearch)
        search._lock = threading.RLock()
        search._cache = [{
            "key": "crowd:1",
            "source": "crowd",
            "id": "1",
            "text": "+1",
            "embedding": [],
            "weight": 1.0,
            "multi_output": True,
        }]
        search.refresh = lambda: {
            "cached_reply_count": 1,
            "candidate_revision": 1,
            "refreshed": False,
        }
        with patch(
            "vector_search.get_embeddings",
            side_effect=[[[1.0, 0.0]], [[1.0, 0.0]], [[1.0, 0.0]]],
        ) as embeddings:
            first = search.search_fixed_candidates("+1", threshold=0.5)
            second = search.search_fixed_candidates("+1", threshold=0.5)
        self.assertEqual(first["candidates"][0]["key"], "crowd:1")
        self.assertEqual(second["candidates"][0]["key"], "crowd:1")
        self.assertEqual(embeddings.call_count, 3)
        self.assertEqual(search._cache[0]["embedding"], [1.0, 0.0])


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

    def test_add_some_and_ambiguous_type_this_are_not_plus_one(self):
        for source in ("這邊再加一些", "這個畫面幫我打這一家"):
            hints = detect_crowd_signal_hints(source)
            self.assertEqual(hints["suggested_tokens"], [])
            self.assertFalse(hints["explicit_patterns"])

    def test_crowd_command_is_reduced_to_viewer_token(self):
        self.assertEqual(extract_crowd_response_tokens("幫我刷888留言"), ["888"])
        self.assertEqual(extract_crowd_response_tokens("想了解的扣6"), ["6"])
        self.assertEqual(extract_crowd_response_tokens("要的加1"), ["+1"])
        self.assertEqual(extract_crowd_response_tokens("要的加一"), ["+1"])
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
        self.assertIn("啟用預設／自訂語句", generator_source)
        self.assertIn("啟用衝人氣口號", generator_source)

    def test_manual_and_auto_are_distinct_top_level_modes(self):
        live_source = (ROOT / "live_stream_llm.py").read_text(encoding="utf-8")
        self.assertIn('stream_reply_mode == "manual"', live_source)
        self.assertIn("search_fixed_candidates", live_source)
        self.assertIn('qwen_account_ids = list(account_ids) if stream_reply_mode == "auto" else []', live_source)
        self.assertIn('reply_mode="auto"', live_source)
        self.assertNotIn('reply_mode must be fixed or auto', live_source)

    def test_prompt_requires_friendly_natural_reaction(self):
        generator_source = (ROOT / "reply_generator.py").read_text(encoding="utf-8")
        for phrase in ("自然欣賞", "不得諷刺", "不得諷刺、嗆聲", "質疑主播誠信與能力"):
            self.assertIn(phrase, generator_source)

    def test_per_account_modes_exist(self):
        source = (ROOT / "live_stream_llm.py").read_text(encoding="utf-8")
        for mode in ("qwen_only", "hybrid", "vector_only"):
            self.assertIn(mode, source)
        self.assertIn("vector_search_performed", source)

    def test_three_model_ignores_trigger_per_account_fallback(self):
        source = (ROOT / "live_stream_llm.py").read_text(encoding="utf-8")
        self.assertIn("MODEL_REPLY_GUARANTEE_INTERVAL = 3", source)
        self.assertIn("def enforce_model_reply_frequency", source)
        self.assertIn("MODEL_IGNORE_STREAKS.pop", source)
        self.assertIn("FREQUENCY_FALLBACK_REPLIES", source)
        self.assertIn('"qwen_frequency_fallback"', source)

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

    def test_custom_phrases_and_crowd_database_are_separated(self):
        user_source = (ROOT / "user_input.py").read_text(encoding="utf-8")
        vector_source = (ROOT / "vector_search.py").read_text(encoding="utf-8")
        live_source = (ROOT / "live_stream_llm.py").read_text(encoding="utf-8")
        generator_source = (ROOT / "reply_generator.py").read_text(encoding="utf-8")
        self.assertEqual(compose_crowd_reply("+1", ["M", "黑色"]), "+1 M 黑色")
        self.assertIn('"candidate_type": "custom_phrase"', user_source)
        self.assertIn('"candidate_type": "crowd_slogan"', user_source)
        self.assertIn('"live_stream_crowd_db"', user_source)
        self.assertIn("list_custom_phrases", vector_source)
        self.assertIn("list_crowd_slogans", vector_source)
        self.assertNotIn("set_stream_product_context", vector_source)
        self.assertNotIn('"product_context": product_context', live_source)
        self.assertIn('"base_token"', generator_source)
        self.assertIn('"crowd_replies"', generator_source)
        self.assertIn('"style"', generator_source)

    def test_crowd_slogan_requires_literal_source_match(self):
        self.assertTrue(source_contains_exact_slogan("要的直接+1", "+1"))
        self.assertFalse(source_contains_exact_slogan("要的加一", "+1"))
        self.assertFalse(source_contains_exact_slogan("再加一些", "+1"))
        self.assertTrue(source_contains_exact_slogan("大家刷888", "888"))
        self.assertFalse(source_contains_exact_slogan("價格1888", "888"))

    def test_crowd_attributes_come_from_speech_context(self):
        attributes = extract_contextual_crowd_attributes(
            "黑色、粉色都有，S M L，要的加1",
            "",
            "服飾",
        )
        values = [item["value"] for item in attributes]
        self.assertIn("黑色", values)
        self.assertIn("粉色", values)
        self.assertIn("S", values)
        self.assertIn("M", values)
        self.assertIn("L", values)
        self.assertNotIn("220元", values)

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
