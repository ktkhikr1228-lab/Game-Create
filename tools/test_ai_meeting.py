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

    def test_one_failing_model_is_skipped_quietly(self):
        gh = FakeGitHub()
        result = self.run_meeting(gh, FakeModels(fail={"g"}), MEETING_TOPIC="x", MEETING_ROUNDS="2")
        self.assertFalse(any("応答できませんでした" in b for _, b in gh.posted))       # 失敗はコメントにしない
        self.assertEqual(len(gh.posted), 5)                                              # 2モデル×2周+まとめ
        self.assertIn("参加できなかった回: GPT(1周目)、GPT(2周目)", gh.posted[-1][1])     # まとめに1行だけ残す
        self.assertEqual(result["skipped"], ["GPT(1周目)", "GPT(2周目)"])

    def test_too_few_answers_in_the_first_round_creates_no_issue(self):
        for fail in ({"c", "g", "ge"}, {"c", "g"}):
            gh = FakeGitHub()
            with self.assertRaises(m.MeetingError):
                self.run_meeting(gh, FakeModels(fail=fail), MEETING_TOPIC="x")
            self.assertEqual((gh.created, gh.posted), ([], []))

    def test_summary_falls_back_to_the_next_model(self):
        class SummaryFails(FakeModels):
            def chat(self, model, system, user):
                if "まとめ役" in system and model == "c":
                    raise m.MeetingError("HTTP 429")
                return super().chat(model, system, user)
        gh = FakeGitHub()
        self.run_meeting(gh, SummaryFails(), MEETING_TOPIC="x", MEETING_ROUNDS="1")
        self.assertTrue(gh.posted[-1][1].startswith("## 📝 まとめ(GPT)"))

    def test_old_failure_comments_are_left_out_of_the_discussion(self):
        gh = FakeGitHub([{"number": 13, "title": "前回", "body": "", "labels": [], "state": "open"}])
        gh.posted.append((13, "### 🤖 Claude・1周目\n\n(応答できませんでした: HTTP 429)"))
        models = FakeModels()
        self.run_meeting(gh, models, MEETING_ISSUE="13", MEETING_ROUNDS="1")
        self.assertNotIn("応答できませんでした", models.calls[0][2])

    def test_models_retry_with_long_waits_and_respect_retry_after(self):
        import io
        import json as _json
        import urllib.error
        waits, attempts = [], []

        class Ok:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return _json.dumps({"choices": [{"message": {"content": "意見"}}]}).encode()

        def opener(request, timeout):
            attempts.append(1)
            if len(attempts) == 1:
                raise urllib.error.HTTPError(request.full_url, 429, "busy", {"Retry-After": "90"}, io.BytesIO(b""))
            if len(attempts) == 2:
                raise urllib.error.HTTPError(request.full_url, 520, "x", {}, io.BytesIO(b""))
            return Ok()
        models = m.Models("https://x/v1", "k", opener=opener, sleep=waits.append, interval=0)
        self.assertEqual(models.chat("m", "s", "u"), "意見")
        self.assertEqual(waits, [90, 40])        # 1回目は Retry-After(90秒)、2回目は既定の40秒

        def always_busy(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 429, "busy", {}, io.BytesIO(b""))
        waits.clear()
        with self.assertRaises(m.MeetingError):
            m.Models("https://x/v1", "k", opener=always_busy, sleep=waits.append, interval=0).chat("m", "s", "u")
        self.assertEqual(waits, list(m.RETRY_WAITS))

        def unauthorized(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 401, "no", {}, io.BytesIO(b"secret-part"))
        waits.clear()
        with self.assertRaises(m.MeetingError) as caught:
            m.Models("https://x/v1", "k", opener=unauthorized, sleep=waits.append, interval=0).chat("m", "s", "u")
        self.assertEqual((waits, str(caught.exception)), ([], "HTTP 401"))   # やり直さず、本文も残さない

    def test_calls_are_spaced_out(self):
        import json as _json
        now = [0.0]
        waits = []

        class Ok:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return _json.dumps({"choices": [{"message": {"content": "x"}}]}).encode()

        def sleep(s):
            waits.append(round(s, 1))
            now[0] += s
        models = m.Models("https://x/v1", "k", opener=lambda r, timeout: Ok(), sleep=sleep, interval=15, clock=lambda: now[0])
        models.chat("m", "s", "u")
        now[0] += 5
        models.chat("m", "s", "u")
        self.assertEqual(waits, [10.0])           # 前の呼び出しから15秒あくまで待つ

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


class BriefTest(unittest.TestCase):
    DOC = """# 企画
## 1. コンセプト
- タイル式　`確定`
## 3. 合金
- 配合は刻み式　`確定`
- 品質は量で上がる　`案`
### 炉
- 炉にはティアがある　`案`
## 5. 木
- 交配なし　`確定`
- 実在の種類で差をつける　`案`
## 8. 世界・地形
### バイオーム一覧
- 砂漠や湿地など　`案`
## 12. 未決事項
- [ ] 武器種の数
"""

    def test_terms(self):
        self.assertEqual(m.topic_terms("[AI会議] バイオーム仕様の整理（6軸）"), ["バイオーム", "6軸"])

    def test_related_sections_are_full(self):
        text = m.brief_design(self.DOC, "[AI会議] 合金の配合")
        self.assertIn("品質は量で上がる", text)        # 当たった節は本文まるごと
        self.assertIn("炉にはティアがある", text)       # 親が当たれば子の節も
        self.assertIn("交配なし", text)                # 関係ない節も「確定」の行は残す
        self.assertNotIn("実在の種類で差をつける", text)  # 関係ない節の「案」は省く
        self.assertIn("武器種の数", text)              # 未決事項はいつも載せる

    def test_child_pulls_parent(self):
        text = m.brief_design(self.DOC, "バイオームの整理")
        self.assertIn("## 8. 世界・地形", text)
        self.assertIn("砂漠や湿地など", text)
        self.assertIn("## 3. 合金　(省略)", text.replace("## 3. 合金\n- 配合は刻み式", "## 3. 合金　(省略)"))

    def test_decisions(self):
        dec = "# 決定ログ\n## YYYY-MM-DD タイトル\n- 決定：\n## 2026-11-18 配合は刻み式\n- 決定：刻む\n- 理由：合金の配合を絞る\n## 2026-11-18 木\n- 決定：交配なし\n- 理由：手間\n"
        text = m.brief_decisions(dec, "合金の配合")
        self.assertIn("合金の配合を絞る", text)
        self.assertIn("交配なし", text)
        self.assertNotIn("手間", text)
        self.assertNotIn("YYYY", text)


if __name__ == "__main__":
    unittest.main()


class FakeRoute:
    def __init__(self, base, key, header="", interval=15, key_name="AI_API_KEY"):
        self.base, self.key, self.key_name, self.calls, self.checked = base, key, key_name, [], False

    def check(self):
        self.checked = True

    def chat(self, model, system, user):
        self.calls.append(model)
        return f"{self.base}:{model}"


class RouterTest(unittest.TestCase):
    def test_split_model(self):
        self.assertEqual(m.split_model("gemini:gemini-4-neon"), ("gemini", "gemini-4-neon"))
        self.assertEqual(m.split_model("claude-opus-5.5"), ("", "claude-opus-5.5"))
        self.assertEqual(m.split_model("unknown:model"), ("", "unknown:model"))   # 知らない会社名はゲートウェイへ

    def test_routes_to_gateway_and_official_api(self):
        env = {"AI_API_BASE": "https://gw/v1", "AI_API_KEY": "k", "GEMINI_API_KEY": "g"}
        parts = m.parse_models("Claude=claude-opus-5.5,Gemini=gemini:gemini-4-neon")
        router = m.Router(env, parts, make=FakeRoute)
        self.assertEqual(router.chat("claude-opus-5.5", "s", "u"), "https://gw/v1:claude-opus-5.5")
        self.assertEqual(router.chat("gemini:gemini-4-neon", "s", "u"),
                         "https://generativelanguage.googleapis.com/v1beta/openai:gemini-4-neon")
        self.assertEqual(router.routes["gemini"].key_name, "GEMINI_API_KEY")

    def test_gateway_not_needed_when_every_model_is_official(self):
        env = {"GEMINI_API_KEY": "g", "MISTRAL_API_KEY": "x"}
        router = m.Router(env, m.parse_models("Gemini=gemini:gemini-4-neon,Mistral=mistral:mistral-large-latest"), make=FakeRoute)
        self.assertNotIn("", router.routes)

    def test_missing_official_key_stops_before_the_meeting(self):
        with self.assertRaises(m.MeetingError) as ctx:
            m.Router({"AI_API_BASE": "https://gw/v1", "AI_API_KEY": "k"},
                     m.parse_models("Claude=claude-opus-5.5,Gemini=gemini:gemini-4-neon"), make=FakeRoute)
        self.assertIn("GEMINI_API_KEY", str(ctx.exception))

    def test_anthropic_skips_the_models_check(self):
        env = {"ANTHROPIC_API_KEY": "a", "GEMINI_API_KEY": "g"}
        router = m.Router(env, m.parse_models("Claude=anthropic:claude-opus-5-5,Gemini=gemini:gemini-4-neon"), make=FakeRoute)
        router.check()
        self.assertFalse(router.routes["anthropic"].checked)
        self.assertTrue(router.routes["gemini"].checked)


class EffortTest(unittest.TestCase):
    def test_split_effort(self):
        self.assertEqual(m.split_effort("gemini:gemini-4-argon@medium"), ("gemini:gemini-4-argon", "medium"))
        self.assertEqual(m.split_effort("claude-opus-5.5"), ("claude-opus-5.5", None))
        self.assertEqual(m.split_model("gemini:gemini-4-argon@low"), ("gemini", "gemini-4-argon"))
        with self.assertRaises(m.MeetingError):
            m.parse_models("A=x@fast,B=y")

    def test_effort_is_sent_only_when_given(self):
        import json as _json
        bodies = []

        class Ok:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return _json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

        def opener(request, timeout):
            bodies.append(_json.loads(request.data.decode()))
            return Ok()
        models = m.Models("https://x/v1", "k", opener=opener, sleep=lambda s: None, interval=0)
        models.chat("m", "s", "u")
        models.chat("m", "s", "u", effort="medium")
        self.assertNotIn("reasoning_effort", bodies[0])
        self.assertEqual(bodies[1]["reasoning_effort"], "medium")

    def test_router_uses_the_spec_effort_and_the_override(self):
        calls = []

        class Route:
            def __init__(self, *a, **k): pass
            def check(self): pass
            def chat(self, model, system, user, effort=None):
                calls.append((model, effort))
                return "ok"
        router = m.Router({"GEMINI_API_KEY": "g", "AI_API_BASE": "b", "AI_API_KEY": "k"},
                          m.parse_models("Gemini=gemini:gemini-4-argon@medium,Claude=claude-opus-5.5"), make=Route)
        router.chat("gemini:gemini-4-argon@medium", "s", "u")
        router.chat("gemini:gemini-4-argon@medium", "s", "u", effort="high")
        router.chat("claude-opus-5.5", "s", "u")
        self.assertEqual(calls, [("gemini-4-argon", "medium"), ("gemini-4-argon", "high"), ("claude-opus-5.5", None)])

    def test_summary_effort_is_passed_to_the_summarizer(self):
        seen = []

        class Recorder(FakeModels):
            def chat(self, model, system, user, effort=None):
                seen.append((model, "まとめ役" in system, effort))
                return super().chat(model, system, user)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "docs").mkdir()
            (root / "docs" / "design.md").write_text(DESIGN, encoding="utf-8")
            (root / "docs" / "decisions.md").write_text("", encoding="utf-8")
            (root / "AGENTS.md").write_text("ルール", encoding="utf-8")
            m.run({"AI_MODELS": "A=a,B=b", "MEETING_TOPIC": "議題", "MEETING_ROUNDS": "1", "AI_SUMMARY_EFFORT": "high"},
                  FakeGitHub(), Recorder(), docs_root=root, log=lambda *a: None)
        self.assertIn(("a", True, "high"), seen)
        self.assertTrue(all(effort is None for _, summary, effort in seen if not summary))
