import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class CommodityNewsIdentityTests(unittest.TestCase):
    def test_legacy_news_agent_name_is_absent_from_active_project_files(self):
        legacy_name = "company" + "_news"
        offenders = []
        roots_and_suffixes = (
            (ROOT / "src", {".py", ".yaml", ".yml"}),
            (ROOT / "docs", {".md"}),
        )
        for root, suffixes in roots_and_suffixes:
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in suffixes:
                    continue
                text = path.read_text(encoding="utf-8-sig").lower()
                if legacy_name in text:
                    offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual([], sorted(offenders))


if __name__ == "__main__":
    unittest.main()
