import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pr_review as r
from ai_meeting import MeetingError


class FakeGitHub:
    def __init__(self, files):
        self.files = files
        self.posted = []

    def call(self, method, path, body=None, ok_statuses=(200, 201)):
        if "/files" in path:
            page = int(path.rsplit("page=", 1)[1])
            return self.files[(page - 1) * 100: page * 100]
        return {"number": 11, "title": "試作", "body": "説明"}

    def comment(self, number, body):
        self.posted.append((number, body))


class FakeModels:
    def __init__(self, reply="### 結論\n大きな問題なし", fail=False):
        self.reply, self.fail, self.calls = reply, fail, []

    def chat(self, model, system, user, effort=None):
        self.calls.append((model, system, user, effort))
        if self.fail:
            raise MeetingError("HTTP 402")
        return self.reply


class PrReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "docs").mkdir()
        (root / "AGENTS.md").write_text("共通ルール", encoding="utf-8")
        (root / "docs/design.md").write_text("企画の正本", encoding="utf-8")
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def test_model_spec(self):
        self.assertEqual(r.model_spec({}), ("gemini-4-argon", None))
        self.assertEqual(r.model_spec({"GEMINI_MODEL": "gemini-4-neon@high"}), ("gemini-4-neon", "high"))

    def test_extra_request(self):
        self.assertEqual(r.extra_request("@gemini-cli /review"), "")
        self.assertEqual(r.extra_request("@gemini-cli /review 安全性を重点的に\n\n---\n_署名_"), "安全性を重点的に")

    def test_format_diff_truncates(self):
        files = [{"filename": "a.py", "status": "modified", "additions": 1, "deletions": 0, "patch": "x" * (r.PATCH_LIMIT + 10)},
                 {"filename": "b.png", "status": "added"}]
        diff = r.format_diff(files)
        self.assertIn("10 文字を省略", diff)
        self.assertIn("b.png", diff)
        self.assertIn("バイナリ", diff)

    def test_run_posts_review(self):
        files = [{"filename": f"f{i}.txt", "status": "added", "patch": "+a"} for i in range(150)]
        github, models = FakeGitHub(files), FakeModels()
        r.run({"PR_NUMBER": "11", "GEMINI_MODEL": "gemini-4-argon@medium"}, github, models, root=self.root, log=lambda *_: None)
        model, system, user, effort = models.calls[0]
        self.assertEqual((model, effort), ("gemini-4-argon", "medium"))
        self.assertIn("共通ルール", system)
        self.assertIn("企画の正本", user)
        self.assertIn("f149.txt", user)            # 2ページ目のファイルも入っている
        self.assertIn("decisions.md はありません", user)
        self.assertEqual(github.posted[0][0], 11)
        self.assertIn("Geminiレビュー", github.posted[0][1])

    def test_run_reports_failure(self):
        github = FakeGitHub([])
        with self.assertRaises(MeetingError):
            r.run({"PR_NUMBER": "11"}, github, FakeModels(fail=True), root=self.root, log=lambda *_: None)
        self.assertIn("失敗しました(HTTP 402)", github.posted[0][1])


if __name__ == "__main__":
    unittest.main()
