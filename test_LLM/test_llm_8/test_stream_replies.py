import unittest
from contextlib import ExitStack
from unittest.mock import patch

import live_stream_llm as llm
from vector_search import CandidateVectorSearch


class StreamReplySelectionTests(unittest.TestCase):
    def test_no_selection_means_manual_mode(self):
        with patch.object(llm, "get_stream_reply_setting", return_value=None):
            self.assertEqual(llm.get_stream_reply_mode("new_stream"), "manual")

    def test_saved_modes_are_manual_or_auto_and_legacy_crowd_migrates(self):
        for stored_mode, expected_mode in (
            ("manual", "manual"),
            ("auto", "auto"),
            ("crowd", "auto"),
        ):
            with self.subTest(mode=stored_mode), patch.object(
                llm, "get_stream_reply_setting", return_value={"reply_mode": stored_mode}
            ):
                self.assertEqual(llm.get_stream_reply_mode("stream_a"), expected_mode)

    def test_auto_selection_uses_existing_reply_pipeline(self):
        expected = {"reply": "自動回覆", "has_reply": True}
        with patch.object(llm, "get_stream_reply_mode", return_value="auto"), patch.object(
            llm, "_choose_for_accounts_unlocked", return_value=expected
        ) as pipeline:
            self.assertIs(
                llm.choose_for_accounts("測試句子", ["account_1"], "stream_a"),
                expected,
            )
            pipeline.assert_called_once()

    def test_reply_mode_endpoint_only_persists_manual_and_auto(self):
        client = llm.app.test_client()
        with patch.object(llm, "get_stream_reply_setting", return_value=None), patch.object(
            llm, "get_stream_enabled_keys", return_value=set()
        ), patch.object(llm, "get_stream_reply_mode", return_value="manual"), patch.object(
            llm.stream_settings_collection, "update_one"
        ) as update:
            for mode in ("auto", "manual"):
                with self.subTest(mode=mode):
                    response = client.patch(
                        "/stream_replies",
                        json={"stream_id": "stream_a", "reply_mode": mode},
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.get_json()["reply_mode"], mode)
                    self.assertEqual(update.call_args.args[1]["$set"]["reply_mode"], mode)
            self.assertEqual(
                client.patch(
                    "/stream_replies",
                    json={"stream_id": "stream_a", "reply_mode": "crowd"},
                ).status_code,
                400,
            )

    def test_manual_vector_search_only_uses_three_fixed_sources(self):
        reference = [
            {"key": "default:1", "source": "default", "text": "好好看"},
            {"key": "default:2", "source": "default", "text": "很方便"},
        ]
        crowd = [{"key": "crowd:2", "source": "crowd", "text": "+1"}]
        learning = {
            "live_database": "live_test", "example_count": 0,
            "minimum_vector_examples": 20, "learning_ready": False,
        }
        search_result = {
            "candidates": [
                {
                    **reference[0], "similarity": 0.95, "score": 0.95,
                    "multi_output": False,
                },
                {
                    **reference[1], "similarity": 0.91, "score": 0.91,
                    "multi_output": False,
                },
            ],
            "best_similarity": 0.95, "threshold": 0.8,
            "candidate_revision": 1, "refreshed": False,
            "cached_reply_count": 2, "elapsed_ms": 1.0,
            "learned_examples_considered": 0, "learned_candidates_selected": 0,
            "query_fragment_count": 1, "static_candidates_compared": 2,
        }
        with ExitStack() as stack:
            stack.enter_context(patch.object(llm, "draw_account_styles", return_value={"account_1": "reaction"}))
            stack.enter_context(patch.object(llm, "observe_cls_memory", return_value=("", {"cls_elapsed_ms": 0, "memory_read_elapsed_ms": 0})))
            stack.enter_context(patch.object(llm.candidate_search, "resolve_stream_product_type", return_value=None))
            stack.enter_context(patch.object(llm.candidate_search, "get_learning_status", return_value=learning))
            stack.enter_context(patch.object(llm, "get_stream_reply_mode", return_value="manual"))
            stack.enter_context(patch.object(llm, "get_stream_reference_candidates", return_value=reference))
            stack.enter_context(patch.object(llm.candidate_search, "list_crowd_slogans", return_value=crowd))
            search = stack.enter_context(patch.object(llm.candidate_search, "search_fixed_candidates", return_value=search_result))
            stack.enter_context(patch.object(llm, "recent_stream_replies", return_value=["好好看"]))
            stack.enter_context(patch.object(llm, "add_stream_history"))
            generator = stack.enter_context(patch.object(llm, "generate_account_replies"))
            result = llm._choose_for_accounts_unlocked("主播說這件好好看", ["account_1"], "manual_test")

        search.assert_called_once()
        self.assertEqual(search.call_args.kwargs["allowed_keys"], {"default:1", "default:2", "crowd:2"})
        generator.assert_not_called()
        self.assertEqual(result["reply_mode"], "manual")
        self.assertEqual(result["account_results"][0]["reply"], "很方便")
        self.assertEqual(result["account_results"][0]["reply_source"], "fixed_vector")
        self.assertEqual(result["recent_candidates_excluded"], ["好好看"])

    def test_recent_duplicate_filter_exempts_crowd_phrases(self):
        candidates = [
            {"key": "default:1", "source": "default", "text": "這個好"},
            {"key": "default:2", "source": "default", "text": "有優惠嗎"},
            {"key": "crowd:3", "source": "crowd", "text": "+1"},
        ]
        with patch.object(llm, "recent_stream_replies", return_value=["這個好", "+1"]):
            kept, excluded = llm.exclude_recent_fixed_candidates(
                "manual_test", candidates
            )
        self.assertEqual([item["key"] for item in kept], ["default:2", "crowd:3"])
        self.assertEqual(excluded, ["這個好"])

    def test_auto_calls_qwen_with_all_phrase_references(self):
        generated = {
            "valid": True,
            "crowd_response": False,
            "results": [{
                "account_id": "account_1", "valid": True, "action": "reply",
                "reply": "自動生成內容", "style": "reaction", "evidence_text": "主播正在介紹商品",
            }],
        }
        learning = {
            "live_database": "live_test", "example_count": 0,
            "minimum_vector_examples": 20, "learning_ready": False,
        }
        reference = [{"key": "default:1", "source": "default", "text": "好好看"}]
        crowd = [{"key": "crowd:2", "source": "crowd", "text": "+1"}]
        with ExitStack() as stack:
            stack.enter_context(patch.object(llm, "ENABLE_QWEN", True))
            stack.enter_context(patch.object(llm, "draw_account_styles", return_value={"account_1": "reaction"}))
            stack.enter_context(patch.object(llm, "observe_cls_memory", return_value=("", {"cls_elapsed_ms": 0, "memory_read_elapsed_ms": 0})))
            stack.enter_context(patch.object(llm.candidate_search, "resolve_stream_product_type", return_value=None))
            stack.enter_context(patch.object(llm.candidate_search, "get_learning_status", return_value=learning))
            stack.enter_context(patch.object(llm.candidate_search, "refresh", return_value={"candidate_revision": 1, "refreshed": False, "cached_reply_count": 6}))
            stack.enter_context(patch.object(llm, "get_stream_reference_candidates", return_value=reference))
            stack.enter_context(patch.object(llm.candidate_search, "list_crowd_slogans", return_value=crowd))
            stack.enter_context(patch.object(llm, "append_model_text", return_value=("主播正在介紹商品" * 10, 100, True)))
            stack.enter_context(patch.object(llm, "consume_model_text", return_value="主播正在介紹商品" * 10))
            stack.enter_context(patch.object(llm, "get_stream_reply_mode", return_value="auto"))
            stack.enter_context(patch.object(llm, "should_call_qwen", return_value=(True, 0)))
            stack.enter_context(patch.object(llm, "accounts_due_for_model_reply", return_value=[]))
            stack.enter_context(patch.object(llm, "recent_stream_replies", return_value=[]))
            stack.enter_context(patch.object(llm, "enforce_model_reply_frequency", return_value={}))
            stack.enter_context(patch.object(llm, "add_stream_history"))
            generator = stack.enter_context(patch.object(llm, "generate_account_replies", return_value=generated))
            result = llm._choose_for_accounts_unlocked("主播正在介紹商品", ["account_1"], "auto_test")

        self.assertEqual(generator.call_args.kwargs["candidates"], reference)
        self.assertEqual(generator.call_args.kwargs["crowd_slogans"], crowd)
        self.assertEqual(result["reference_candidate_count"], 1)
        self.assertEqual(result["crowd_slogan_count"], 1)
        self.assertEqual(result["account_results"][0]["reply_source"], "qwen")

    def test_custom_phrase_lookup_excludes_seeded_defaults(self):
        search = CandidateVectorSearch.__new__(CandidateVectorSearch)
        candidates = [
            {"source": "default", "candidate_type": "custom_phrase", "text": "我來了"},
            {"source": "user", "candidate_type": "custom_phrase", "text": "這件很好看"},
        ]
        with patch.object(search, "list_cached", return_value=candidates):
            self.assertEqual(search.list_custom_phrases(), [candidates[1]])

    def test_third_model_ignore_uses_one_fixed_fallback(self):
        generated = {"account_1": {"account_id": "account_1", "action": "ignore", "reply": "Ignore", "valid": True}}
        with patch.object(llm, "MODEL_IGNORE_STREAKS", {}), patch.object(llm, "STREAM_HISTORY", {}):
            for _ in range(2):
                llm.enforce_model_reply_frequency("auto_test", "主播正在介紹商品", ["account_1"], generated)
            self.assertEqual(generated["account_1"]["reply"], "Ignore")
            self.assertEqual(llm.accounts_due_for_model_reply("auto_test", ["account_1"]), ["account_1"])
            frequency = llm.enforce_model_reply_frequency("auto_test", "主播正在介紹商品", ["account_1"], generated)
            self.assertTrue(generated["account_1"]["frequency_fallback"])
            self.assertIn(generated["account_1"]["reply"], llm.FREQUENCY_FALLBACK_REPLIES)
            self.assertEqual(frequency["forced_accounts"], ["account_1"])
            self.assertEqual(llm.accounts_due_for_model_reply("auto_test", ["account_1"]), [])

    def test_unconfigured_stream_excludes_crowd_slogans_from_phrase_checks(self):
        cached = [
            {"key": "default:one", "source": "default"},
            {"key": "user:two", "source": "user"},
            {"key": "crowd:three", "source": "crowd"},
        ]
        with patch.object(llm, "get_stream_reply_setting", return_value=None), patch.object(
            llm.candidate_search, "list_cached", return_value=cached
        ):
            self.assertEqual(
                llm.get_stream_enabled_keys("new_stream"),
                {"default:one", "user:two"},
            )


if __name__ == "__main__":
    unittest.main()
