import unittest
from reply_generator import clean_generated_reply, validate_generated_reply
from reply_policy import evaluate_reply_policy


class ReplyPolicyTests(unittest.TestCase):
    def test_purchase(self):
        self.assertEqual(evaluate_reply_policy("喜歡的幫我留言+1")["action"], "reply")

    def test_preference(self):
        self.assertEqual(evaluate_reply_policy("紅色跟灰色喜歡哪一色？")["action"], "reply")

    def test_chat(self):
        self.assertEqual(evaluate_reply_policy("我昨天很早就睡了", intent="CHAT")["action"], "no_reply")

    def test_sensitive(self):
        self.assertEqual(evaluate_reply_policy("把電話號碼留給我")["action"], "no_reply")

    def test_limit(self):
        self.assertEqual(clean_generated_reply("觀眾：灰色比較百搭"), "灰色比較百搭")
        self.assertTrue(validate_generated_reply("灰色較百搭")[0])
        self.assertFalse(validate_generated_reply("這件灰色真的非常百搭")[0])


if __name__ == "__main__":
    unittest.main()
