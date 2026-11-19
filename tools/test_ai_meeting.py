import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ai_meeting as m

DESIGN = """# 企画
> 状態：`確定` / `案` / `未決`
## 1. コンセプト
- 物語の濃さ(ほぼ説明なしか、手記で読ませるか)　`未決`
## 12. 未決事項
- [ ] 柄は消耗品か、壊れない付け替え式か
- [x] もう決めたこと
- [ ] 武器種の数
"""


class FakeGitHub:
    def __init__(self, issues=None):
        self.issue_list = issues or []
        self.posted = []
        self.created = []
        self.labels = []

    def ensure_label(self, *a):
        self.labels.append(a[0])

    def issues(self, label, state="all"):
        return [i for i in self.issue_list if label in [x["name"] for x in i["labels"]] and (state == "all" or i["state"] == state)]

    def issue(self, number):
        return next(i for i in self.issue_list if i["number"] == number)

    def comments(self, number):
        return [{"body": b, "user": {"type": "Bot"}} for n, b in self.posted if n == number]

    def create_issue(self, title, body, labels):
        issue = {"number": 100 + len(self.created), "title": title, "body": body, "labels": [{"name": l} for l in labels], "state": "open"}
        self.created.append(issue)
        self.issue_list.append(issue)
        return issue

    def add_labels(self, number, labels):
        self.issue(number)["labels"] += [{"name": l} for l in labels]

    def comment(self, number, body):
        self.posted.append((number, body))


class FakeModels:
    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def chat(self, model, system, user):
        self.calls.append((model, system, user))
        if model in self.fail:
            raise m.MeetingError("HTTP 503")
        return f"{model} の意見"


class MeetingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "docs").mkdir()
        (root / "docs" / "design.md").write_text(DESIGN, encoding="utf-8")
        (root / "docs" / "decisions.md").write_text("# 決定ログ", encoding="utf-8")
        (root / "AGENTS.md").write_text("# ルール\n【現】と【ゲ】を分ける", encoding="utf-8")
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def run_meeting(self, gh, models, **env):
        return m.run({"AI_MODELS": "Claude=c,GPT=g,Gemini=ge", **env}, gh, models, self.root, log=lambda *a: None)

    def test_open_items_reads_unchecked_and_marked_lines(self):
        self.assertEqual(m.open_items(DESIGN), ["物語の濃さ(ほぼ説明なしか、手記で読ませるか)", "柄は消耗品か、壊れない付け替え式か", "武器種の数"])

    def test_auto_topic_skips_already_discussed_items(self):
        gh = FakeGitHub([{"number": 1, "title": "[AI会議] 物語の濃さ(ほぼ説明なしか)", "labels": [{"name": "AI会議"}], "state": "closed"}])
        models = FakeModels()
        result = self.run_meeting(gh, models)
        self.assertEqual(gh.created[0]["title"], "[AI会議] 柄は消耗品か、壊れない付け替え式か")
        # 3モデル × 2周 + まとめ1件
        self.assertEqual(result["comments"], 7)
        self.assertEqual([b.split("\n")[0] for _, b in gh.posted][:3],
                         ["### 🤖 Claude(`c`)・1周目", "### 🤖 GPT(`g`)・1周目", "### 🤖 Gemini(`ge`)・1周目"])
        self.assertTrue(gh.posted[-1][1].startswith("## 📝 まとめ(Claude)"))
        self.assertTrue(all("AI会議の自動投稿" in b for _, b in gh.posted))
        # 2周目には、1周目のコメントが文脈として渡っている
        second_round_prompt = models.calls[3][2]
        self.assertIn("c の意見", second_round_prompt)
        self.assertIn("2周目", second_round_prompt)
        self.assertIn("【現】と【ゲ】を分ける", models.calls[0][1])   # AGENTS.md がルールとして渡る

    def test_agenda_label_issue_takes_priority(self):
        gh = FakeGitHub([{"number": 5, "title": "敵の数", "body": "相談", "labels": [{"name": "議題"}], "state": "open"}])
        self.run_meeting(gh, FakeModels(), MEETING_ROUNDS="1")
        self.assertEqual(gh.created, [])
        self.assertEqual({n for n, _ in gh.posted}, {5})
        self.assertIn("AI会議", [x["name"] for x in gh.issue(5)["labels"]])

    def test_explicit_topic_and_issue(self):
        gh = FakeGitHub()
        self.run_meeting(gh, FakeModels(), MEETING_TOPIC="ボスの数", MEETING_ROUNDS="1")
        self.assertEqual(gh.created[0]["title"], "[AI会議] ボスの数")
        gh2 = FakeGitHub([{"number": 9, "title": "続き", "body": "", "labels": [], "state": "open"}])
        self.run_meeting(gh2, FakeModels(), MEETING_ISSUE="#9", MEETING_ROUNDS="1")
        self.assertEqual({n for n, _ in gh2.posted}, {9})

    def test_one_failing_model_does_not_stop_the_meeting(self):
        gh = FakeGitHub()
        self.run_meeting(gh, FakeModels(fail={"g"}), MEETING_TOPIC="x", MEETING_ROUNDS="1")
        self.assertTrue(any("応答できませんでした: HTTP 503" in b for _, b in gh.posted))
        self.assertTrue(gh.posted[-1][1].startswith("## 📝 まとめ"))

    def test_all_models_failing_stops_after_the_first_round(self):
        with self.assertRaises(m.MeetingError):
            self.run_meeting(FakeGitHub(), FakeModels(fail={"c", "g", "ge"}), MEETING_TOPIC="x")

    def test_nothing_left_to_discuss(self):
        titles = ["物語の濃さ(ほぼ説明なしか)", "柄は消耗品か、壊れない付け替え式か", "武器種の数"]
        gh = FakeGitHub([{"number": i, "title": f"[AI会議] {t}", "labels": [{"name": "AI会議"}], "state": "closed"} for i, t in enumerate(titles)])
        self.assertEqual(self.run_meeting(gh, FakeModels())["issue"], None)

    def test_settings_validation(self):
        self.assertEqual(m.parse_rounds("9"), 3)
        self.assertEqual(m.parse_rounds(""), 2)
        with self.assertRaises(m.MeetingError):
            m.parse_models("Claude=c")
        with self.assertRaises(m.MeetingError):
            m.Models("https://example.invalid/v1", "key\nwith newline")
        with self.assertRaises(m.MeetingError):
            m.Models("", "")
        self.assertEqual(m.Models("https://x/v1", "k", "X-Gravitee-Api-Key").headers, {"X-Gravitee-Api-Key": "k"})
        self.assertEqual(m.Models("https://x/v1", "k").headers, {"Authorization": "Bearer k"})


if __name__ == "__main__":
    unittest.main()
