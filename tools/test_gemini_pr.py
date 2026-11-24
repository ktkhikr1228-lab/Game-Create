import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gemini_pr as g
from ai_meeting import MeetingError

REPLY = """<<<TITLE>>>
鉄鉱石の図鑑の文を足す
<<<SUMMARY>>>
図鑑に鉄鉱石の項目を足した。
<<<FILE: docs/zukan.md>>>
# 図鑑

## 鉄鉱石
赤茶けた重い石。
<<<END FILE>>>
<<<DELETE: docs/old.md>>>
"""


class FakeGitHub:
    def __init__(self):
        self.posted = []

    def issue(self, number):
        return {"number": number, "title": "図鑑を足す", "body": "docs/zukan.md に鉄鉱石を足して"}

    def comments(self, number):
        return [{"user": {"login": "owner"}, "author_association": "OWNER", "body": "@gemini-cli /pr"}]

    def comment(self, number, body):
        self.posted.append((number, body))


class FakeModels:
    def __init__(self, reply=REPLY):
        self.reply, self.calls = reply, []

    def chat(self, model, system, user, effort=None):
        self.calls.append((model, system, user, effort))
        return self.reply


class GeminiPrTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "repo"
        (root / "docs").mkdir(parents=True)
        (root / ".github/workflows").mkdir(parents=True)
        (root / "AGENTS.md").write_text("共通ルール", encoding="utf-8")
        (root / "docs/design.md").write_text("企画の正本", encoding="utf-8")
        (root / "docs/zukan.md").write_text("# 図鑑\n", encoding="utf-8")
        (root / "docs/old.md").write_text("古い\n", encoding="utf-8")
        self.root = root
        self.runner = Path(self.tmp.name) / "runner"
        self.output = Path(self.tmp.name) / "output.txt"
        self.env = {"ISSUE_NUMBER": "7", "COMMENT_BODY": "@gemini-cli /pr 短めに",
                    "GITHUB_OUTPUT": str(self.output), "RUNNER_TEMP": str(self.runner)}

    def tearDown(self):
        self.tmp.cleanup()

    def test_extra_request(self):
        self.assertEqual(g.extra_request("@gemini-cli /pr"), "")
        self.assertEqual(g.extra_request("@gemini-cli /pr 短めに\n\n---\n_署名_"), "短めに")

    def test_parse_reply(self):
        plan = g.parse_reply(REPLY)
        self.assertEqual(plan.title, "鉄鉱石の図鑑の文を足す")
        self.assertEqual(plan.files["docs/zukan.md"], "# 図鑑\n\n## 鉄鉱石\n赤茶けた重い石。\n")
        self.assertEqual(plan.deletes, ["docs/old.md"])

    def test_parse_reply_in_code_block(self):
        plan = g.parse_reply("```\n" + REPLY + "```")
        self.assertIn("docs/zukan.md", plan.files)

    def test_parse_reply_cut_off(self):
        with self.assertRaises(MeetingError):
            g.parse_reply("<<<TITLE>>>\nx\n<<<FILE: a.md>>>\n途中で")
        with self.assertRaises(MeetingError):
            g.parse_reply("形式を無視した返答")

    def test_safe_path_blocks(self):
        for bad in ["../x.md", "/etc/passwd", ".github/workflows/a.yml", ".git/config", "docs/../../x", "a/.git/x", ""]:
            with self.assertRaises(MeetingError, msg=bad):
                g.safe_path(self.root, bad)
        self.assertEqual(g.safe_path(self.root, "docs/new.md"), (self.root / "docs/new.md").resolve())

    def test_apply_checks_all_paths_first(self):
        plan = g.Plan(files={"docs/a.md": "a\n", ".github/workflows/x.yml": "x\n"})
        with self.assertRaises(MeetingError):
            g.apply_plan(plan, self.root)
        self.assertFalse((self.root / "docs/a.md").exists())

    def test_apply_skips_unchanged(self):
        plan = g.Plan(files={"docs/zukan.md": "# 図鑑\n"})
        self.assertEqual(g.apply_plan(plan, self.root), [])

    def test_too_many_files(self):
        plan = g.Plan(files={f"docs/{i}.md": "x\n" for i in range(g.MAX_FILES + 1)})
        with self.assertRaises(MeetingError):
            g.apply_plan(plan, self.root)

    def test_run_writes_changes_and_outputs(self):
        github, models = FakeGitHub(), FakeModels()
        touched = g.run(self.env, github, models, self.root, log=lambda *_: None)
        self.assertEqual(touched, ["docs/zukan.md", "docs/old.md"])
        self.assertIn("鉄鉱石", (self.root / "docs/zukan.md").read_text(encoding="utf-8"))
        self.assertFalse((self.root / "docs/old.md").exists())
        self.assertEqual(self.output.read_text(encoding="utf-8"), "changed=true\n")
        msg = (self.runner / "gemini_pr/commit_msg.txt").read_text(encoding="utf-8")
        self.assertTrue(msg.startswith("鉄鉱石の図鑑の文を足す\n"))
        self.assertIn("Refs #7", msg)
        self.assertIn("図鑑に鉄鉱石の項目を足した。", (self.runner / "gemini_pr/pr_body.md").read_text(encoding="utf-8"))
        model, system, user, effort = models.calls[0]
        self.assertEqual(model, "gemini-4-argon")
        self.assertIn("共通ルール", system)
        self.assertIn("<<<CONTENT: docs/zukan.md>>>", user)
        self.assertIn("短めに", user)
        self.assertEqual(github.posted, [])

    def test_run_without_changes_comments(self):
        github = FakeGitHub()
        models = FakeModels("<<<TITLE>>>\nなし\n<<<SUMMARY>>>\nどの鉱石を足すか決めてください。\n")
        self.assertEqual(g.run(self.env, github, models, self.root, log=lambda *_: None), [])
        self.assertEqual(self.output.read_text(encoding="utf-8"), "changed=false\n")
        self.assertFalse((self.runner / "gemini_pr").exists())
        self.assertIn("どの鉱石を足すか決めてください", github.posted[0][1])

    def test_thread_text_trims_old_comments(self):
        comments = [{"user": {"login": "a"}, "body": "x" * 1000} for _ in range(100)]
        text = g.thread_text({"number": 1, "title": "t", "body": "b"}, comments)
        self.assertLessEqual(len(text), g.THREAD_BUDGET + 200)


if __name__ == "__main__":
    unittest.main()
