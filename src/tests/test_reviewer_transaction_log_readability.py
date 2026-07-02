import unittest
from pathlib import Path


class ReviewerTransactionLogReadabilityTest(unittest.TestCase):
    def test_transaction_log_template_has_readable_chinese(self):
        source_path = (
            Path(__file__).resolve().parents[1]
            / "tools"
            / "agent_tools"
            / "research"
            / "reviewer_phase4_review.py"
        )
        source = source_path.read_text(encoding="utf-8")

        required_phrases = [
            "完整交易日志",
            "一、账户总览",
            "二、当日交易执行",
            "四、未交易品种原因详述",
            "信号模板分布",
        ]
        for phrase in required_phrases:
            self.assertIn(phrase, source)

        mojibake_fragments = ["瀹", "涓", "鎵", "淇", "鍥", "浜", "鏃"]
        for fragment in mojibake_fragments:
            self.assertNotIn(fragment, source)


if __name__ == "__main__":
    unittest.main()
