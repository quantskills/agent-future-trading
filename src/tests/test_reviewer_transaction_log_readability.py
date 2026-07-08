import unittest
from pathlib import Path


class ReviewerTransactionLogReadabilityTest(unittest.TestCase):
    def test_transaction_log_template_has_readable_chinese_and_utf8_writer(self):
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
            "5. Signal Summary",
            "信号模板分布",
            "System Decision Flow",
            "TradingPhase.PHASE4",
        ]
        for phrase in required_phrases:
            self.assertIn(phrase, source)

        self.assertIn('output_path.write_text(report_text, encoding="utf-8")', source)
        self.assertNotIn('encoding="gbk"', source)
        self.assertNotIn('encoding="cp936"', source)

        mojibake_fragments = [
            "瀹",
            "涓",
            "鎵",
            "淇",
            "鍥",
            "浜",
            "鐎",
            "娑",
            "閹",
            "娣",
            "閸",
            "娴",
            "閺",
        ]
        for fragment in mojibake_fragments:
            self.assertNotIn(fragment, source)


if __name__ == "__main__":
    unittest.main()
