# -*- coding: utf-8 -*-
"""
campus_network_keeper.py 的单元测试（不需要真实校园网、不会发出真实请求）。

运行：
    python -m unittest test_keeper -v
    python test_keeper.py
"""

import json
import os
import re
import sys
import unittest
from unittest import mock

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# 强制使用示例配置，保证测试结果与本机 config.json 无关
os.environ["CAMPUS_KEEPER_CONFIG"] = os.path.join(BASE_DIR, "config.example.json")

import campus_network_keeper as k  # noqa: E402


class FakeResponse:
    def __init__(self, url="http://x/", status_code=200, json_data=None, text=""):
        self.url = url
        self.status_code = status_code
        self._json = json_data
        self.text = text or (str(json_data) if json_data is not None else "")

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


# ============================================================================
# 配置
# ============================================================================
class TestConfigLoading(unittest.TestCase):

    def setUp(self):
        self._example = k.USING_EXAMPLE_CONFIG
        self._source = k.CONFIG_SOURCE

    def tearDown(self):
        k.USING_EXAMPLE_CONFIG = self._example
        k.CONFIG_SOURCE = self._source

    def test_example_config_is_valid_json(self):
        with open(os.path.join(BASE_DIR, "config.example.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertIn("accounts", cfg)
        self.assertGreaterEqual(len(cfg["accounts"]), 2)

    def test_example_config_covers_all_default_keys(self):
        cfg = k.load_config(os.path.join(BASE_DIR, "config.example.json"))
        for key in k.DEFAULT_CONFIG:
            self.assertIn(key, cfg, "示例配置缺少默认键: %s" % key)

    def test_load_config_marks_example(self):
        k.load_config(os.path.join(BASE_DIR, "config.example.json"))
        self.assertFalse(k.USING_EXAMPLE_CONFIG,
                         "显式指定 --config 时不应被标记为示例配置")

    def test_missing_file_falls_back_to_defaults(self):
        cfg = k.load_config(os.path.join(BASE_DIR, "definitely-not-here.json"))
        self.assertEqual(cfg["gateway"], k.DEFAULT_CONFIG["gateway"])

    def test_broken_json_exits_with_message(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as f:
            f.write("{ not json ,, }")
            bad = f.name
        try:
            with self.assertRaises(SystemExit):
                k.load_config(bad)
        finally:
            os.unlink(bad)

    def test_apply_config_updates_globals(self):
        cfg = k.load_config(os.path.join(BASE_DIR, "config.example.json"))
        cfg["gateway"] = "10.9.9.9"
        cfg["probe_count_per_round"] = 7
        old = (k.GATEWAY_IP, k.PROBE_COUNT_PER_ROUND, k.LOGIN_URL)
        try:
            k.apply_config(cfg)
            self.assertEqual(k.GATEWAY_IP, "10.9.9.9")
            self.assertEqual(k.PROBE_COUNT_PER_ROUND, 7)
            self.assertIn("10.9.9.9", k.LOGIN_URL)
        finally:
            k.apply_config(k.load_config(os.path.join(BASE_DIR, "config.example.json")))
            del old

    def test_log_file_is_absolute_and_inside_project(self):
        self.assertTrue(os.path.isabs(k.LOG_FILE),
                        "日志路径必须相对脚本目录解析，不能依赖当前工作目录")
        self.assertEqual(os.path.dirname(k.LOG_FILE), BASE_DIR)

    def test_deep_merge_keeps_nested_defaults(self):
        merged = k._deep_merge({"a": {"x": 1, "y": 2}, "b": 3}, {"a": {"y": 9}})
        self.assertEqual(merged["a"], {"x": 1, "y": 9})
        self.assertEqual(merged["b"], 3)


class TestNoSecretsInRepo(unittest.TestCase):
    """防止真实账号/密码被提交进仓库。"""

    def test_python_source_has_no_hardcoded_phone_number(self):
        with open(os.path.join(BASE_DIR, "campus_network_keeper.py"), encoding="utf-8") as f:
            src = f.read()
        # 11 位手机号（1 开头、第二位 3-9）
        hits = re.findall(r"(?<!\d)1[3-9]\d{9}(?!\d)", src)
        self.assertEqual(hits, [], "脚本里出现了疑似真实手机号：%s" % hits)

    def test_example_config_uses_placeholder_accounts(self):
        with open(os.path.join(BASE_DIR, "config.example.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        for acct in cfg["accounts"]:
            self.assertTrue(acct["username"].startswith("138000000"),
                            "示例配置里请使用 138000000xx 这类明显假的占位账号")
            self.assertIn("password", acct)

    def test_real_config_is_gitignored(self):
        path = os.path.join(BASE_DIR, ".gitignore")
        if not os.path.isfile(path):
            self.skipTest("没有 .gitignore")
        with open(path, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f]
        self.assertIn("config.json", lines,
                      "config.json 必须被 .gitignore 忽略，否则真实账号会泄露")


# ============================================================================
# 代理隔离
# ============================================================================
class TestProxyIsolation(unittest.TestCase):
    """最重要的一条：绝不能走系统代理。"""

    def test_make_session_disables_trust_env(self):
        s = k.make_session()
        self.assertFalse(s.trust_env, "trust_env 必须为 False，否则会读注册表代理")
        self.assertEqual(s.proxies, {}, "proxies 必须为空")

    def test_make_session_ignores_env_proxy(self):
        """即使环境变量里有代理，也不能被使用。"""
        with mock.patch.dict(os.environ, {
            "HTTP_PROXY": "http://127.0.0.1:7897",
            "HTTPS_PROXY": "http://127.0.0.1:7897",
        }):
            s = k.make_session()
            self.assertEqual(s.proxies, {})
            self.assertFalse(s.trust_env)
            req = s.prepare_request(
                __import__("requests").Request("GET", "http://www.baidu.com")
            )
            merged = s.merge_environment_settings(req.url, {}, None, None, None)
            self.assertEqual(merged.get("proxies"), {},
                             "环境代理被合并进来了，会导致走 Clash")


# ============================================================================
# 探测
# ============================================================================
class TestProbeLogic(unittest.TestCase):
    """探测三态：在线 / 明确掉线 / 不确定。"""

    def test_gateway_redirect_means_offline(self):
        s = k.make_session()
        with mock.patch.object(s, "get",
                               return_value=FakeResponse(url="http://%s/x?ip=10.0.0.1" % k.GATEWAY_IP)):
            state, info = k._probe_once(s, "http://www.baidu.com")
        self.assertIs(state, False)
        self.assertIn(k.GATEWAY_IP, info)

    def test_200_means_online(self):
        s = k.make_session()
        with mock.patch.object(s, "get",
                               return_value=FakeResponse(url="http://www.baidu.com/", status_code=200)):
            state, _ = k._probe_once(s, "http://www.baidu.com")
        self.assertIs(state, True)

    def test_204_means_online(self):
        s = k.make_session()
        with mock.patch.object(s, "get",
                               return_value=FakeResponse(url="http://x/", status_code=204)):
            state, _ = k._probe_once(s, "http://x")
        self.assertIs(state, True)

    def test_network_error_is_indeterminate_not_offline(self):
        """关键回归：网络异常绝不能被当成掉线，否则会疯狂拨号。"""
        import requests
        s = k.make_session()
        with mock.patch.object(s, "get",
                               side_effect=requests.exceptions.ConnectTimeout("boom")):
            state, info = k._probe_once(s, "http://www.baidu.com")
        self.assertIsNone(state, "网络异常必须是 None（不确定），不能是 False（掉线）")

    def test_unexpected_exception_is_indeterminate(self):
        s = k.make_session()
        with mock.patch.object(s, "get", side_effect=OSError("socket blew up")):
            state, _ = k._probe_once(s, "http://www.baidu.com")
        self.assertIsNone(state)

    def test_probe_network_online_when_first_url_ok(self):
        s = k.make_session()
        with mock.patch.object(k, "_probe_once", return_value=(True, "ok")) as m:
            ok, _ = k.probe_network(s)
        self.assertTrue(ok)
        self.assertEqual(m.call_count, 1, "第一个通就该直接返回")

    def test_probe_network_offline_on_redirect(self):
        s = k.make_session()
        with mock.patch.object(k, "_probe_once", return_value=(False, "gw")):
            ok, _ = k.probe_network(s)
        self.assertFalse(ok)

    def test_probe_network_all_unreachable_is_treated_as_online(self):
        """全部不可达时，不能触发拨号。"""
        s = k.make_session()
        with mock.patch.object(k, "_probe_once", return_value=(None, "err")):
            ok, info = k.probe_network(s)
        self.assertTrue(ok, "网络层不可达时不应判定为掉线")
        self.assertIn("不可达", info)


# ============================================================================
# 登录
# ============================================================================
class TestLoginLogic(unittest.TestCase):

    def test_login_success_code_zero(self):
        s = k.make_session()
        resp = FakeResponse(json_data={"code": 0, "msg": "认证成功"})
        with mock.patch.object(k, "make_session", return_value=s), \
             mock.patch.object(k, "_fetch_csrf_token", return_value="tok"), \
             mock.patch.object(s, "get", return_value=FakeResponse()), \
             mock.patch.object(s, "post", return_value=resp), \
             mock.patch.object(s, "close"):
            self.assertTrue(k.do_login({"username": "u", "password": "p"}))

    def test_login_failure_code_nonzero(self):
        s = k.make_session()
        resp = FakeResponse(json_data={"code": 1, "msg": "密码错误"})
        with mock.patch.object(k, "make_session", return_value=s), \
             mock.patch.object(k, "_fetch_csrf_token", return_value="tok"), \
             mock.patch.object(s, "get", return_value=FakeResponse()), \
             mock.patch.object(s, "post", return_value=resp), \
             mock.patch.object(s, "close"):
            self.assertFalse(k.do_login({"username": "u", "password": "p"}))

    def test_login_missing_csrf_returns_false(self):
        s = k.make_session()
        with mock.patch.object(k, "make_session", return_value=s), \
             mock.patch.object(k, "_fetch_csrf_token", return_value=""), \
             mock.patch.object(s, "get", return_value=FakeResponse()), \
             mock.patch.object(s, "close"):
            self.assertFalse(k.do_login({"username": "u", "password": "p"}))

    def test_login_unexpected_exception_is_contained(self):
        """关键回归：任何异常都不能冒出 do_login。"""
        s = k.make_session()
        with mock.patch.object(k, "make_session", return_value=s), \
             mock.patch.object(s, "get", side_effect=RuntimeError("boom")), \
             mock.patch.object(s, "close"):
            self.assertFalse(k.do_login({"username": "u", "password": "p"}))

    def test_login_handles_unparseable_json(self):
        s = k.make_session()
        resp = FakeResponse(json_data=None, text="<html>not json</html>")
        with mock.patch.object(k, "make_session", return_value=s), \
             mock.patch.object(k, "_fetch_csrf_token", return_value="tok"), \
             mock.patch.object(s, "get", return_value=FakeResponse()), \
             mock.patch.object(s, "post", return_value=resp), \
             mock.patch.object(s, "close"):
            self.assertFalse(k.do_login({"username": "u", "password": "p"}))

    def test_login_http_500_returns_false(self):
        s = k.make_session()
        resp = FakeResponse(status_code=500, text="err")
        with mock.patch.object(k, "make_session", return_value=s), \
             mock.patch.object(k, "_fetch_csrf_token", return_value="tok"), \
             mock.patch.object(s, "get", return_value=FakeResponse()), \
             mock.patch.object(s, "post", return_value=resp), \
             mock.patch.object(s, "close"):
            self.assertFalse(k.do_login({"username": "u", "password": "p"}))


# ============================================================================
# 账号轮换
# ============================================================================
class TestAccountRotation(unittest.TestCase):
    """核心：认证成功即永久换下一个账号，绝不回头复用。"""

    def setUp(self):
        k.reset_rotation()

    def test_starts_at_first_account(self):
        self.assertEqual(k.next_account()["username"],
                         k.INITIAL_ACCOUNTS[0]["username"])

    def test_advance_on_success_moves_to_next(self):
        first = k.next_account()["username"]
        k.advance_rotation(first)
        self.assertEqual(k.next_account()["username"],
                         k.INITIAL_ACCOUNTS[1]["username"])

    def test_full_cycle_visits_each_account_once(self):
        """连续成功 N 次，应该依次走完 N 个不同账号。"""
        seen = []
        for _ in range(len(k.INITIAL_ACCOUNTS)):
            acct = k.next_account()
            seen.append(acct["username"])
            k.advance_rotation(acct["username"])
        self.assertEqual(seen, [a["username"] for a in k.INITIAL_ACCOUNTS])
        self.assertEqual(len(set(seen)), len(seen), "同一账号被重复使用了")

    def test_wraps_around_after_last_account(self):
        for _ in range(len(k.INITIAL_ACCOUNTS)):
            acct = k.next_account()
            k.advance_rotation(acct["username"])
        self.assertEqual(k.next_account()["username"],
                         k.INITIAL_ACCOUNTS[0]["username"])

    def test_key_regression_no_backtrack(self):
        """
        关键回归：认证成功后，同一个账号在后续若干次轮换中
        【绝不能】再次出现，直到走完一整圈。
        """
        n = len(k.INITIAL_ACCOUNTS)
        used = k.next_account()["username"]
        k.advance_rotation(used)

        for _ in range(n - 1):
            nxt = k.next_account()["username"]
            self.assertNotEqual(nxt, used,
                                "账号 %s 在走完一圈前被回头复用了" % used)
            k.advance_rotation(nxt)

    def test_next_account_is_deep_copy(self):
        acct = k.next_account()
        acct["password"] = "MUTATED"
        self.assertNotEqual(k.INITIAL_ACCOUNTS[0]["password"], "MUTATED")

    def test_advance_skips_duplicate_entries(self):
        """账号列表里有重复条目时，推进不能原地打转。"""
        original = k.INITIAL_ACCOUNTS
        k.INITIAL_ACCOUNTS = [dict(original[0]), dict(original[0]), dict(original[-1])]
        try:
            k.reset_rotation()
            first = k.next_account()["username"]
            k.advance_rotation(first)
            self.assertNotEqual(k.next_account()["username"], first,
                                "推进后仍是同一个账号（重复条目导致打转）")
        finally:
            k.INITIAL_ACCOUNTS = original

    def test_single_account_does_not_crash(self):
        original = k.INITIAL_ACCOUNTS
        k.INITIAL_ACCOUNTS = [dict(original[0])]
        try:
            k.reset_rotation()
            acct = k.next_account()
            k.advance_rotation(acct["username"])
            self.assertEqual(k.next_account()["username"], original[0]["username"])
        finally:
            k.INITIAL_ACCOUNTS = original
            k.reset_rotation()

    def test_empty_account_list_returns_none(self):
        original = k.INITIAL_ACCOUNTS
        k.INITIAL_ACCOUNTS = []
        try:
            self.assertIsNone(k.next_account())
            k.advance_rotation("nobody")
        finally:
            k.INITIAL_ACCOUNTS = original

    def test_peek_next_shows_following_account(self):
        first = k.next_account()["username"]
        self.assertEqual(k.peek_next_account(), k.INITIAL_ACCOUNTS[1]["username"])
        k.advance_rotation(first)
        self.assertEqual(k.peek_next_account(), k.INITIAL_ACCOUNTS[2]["username"])

    def test_peek_matches_actual_with_duplicate_entries(self):
        """含重复账号时，peek 的预测必须与实际推进结果一致。"""
        original = k.INITIAL_ACCOUNTS
        k.INITIAL_ACCOUNTS = [dict(original[0]), dict(original[0]), dict(original[-1])]
        try:
            k.reset_rotation()
            for _ in range(6):
                cur = k.next_account()["username"]
                predicted = k.peek_next_account()
                k.advance_rotation(cur)
                actual = k.next_account()["username"]
                self.assertEqual(predicted, actual,
                                 "预测 %s 与实际 %s 不一致" % (predicted, actual))
        finally:
            k.INITIAL_ACCOUNTS = original
            k.reset_rotation()

    def test_peek_matches_actual_normal_case(self):
        k.reset_rotation()
        for _ in range(6):
            cur = k.next_account()["username"]
            predicted = k.peek_next_account()
            k.advance_rotation(cur)
            self.assertEqual(predicted, k.next_account()["username"])


# ============================================================================
# 整轮行为
# ============================================================================
class TestRotationInRounds(unittest.TestCase):
    """整轮跑下来，账号必须严格顺序轮换，不回头。"""

    def setUp(self):
        k.reset_rotation()

    def _run_round_with_n_logins(self, n_logins, login_result=True):
        """
        构造一个真实的轮次：前 n_logins 次探测掉线（每次不同 IP），之后恢复在线。
        返回被使用的账号名列表。
        """
        session = k.make_session()
        used = []
        state = {"probe": 0}

        def fake_probe(_s):
            state["probe"] += 1
            if state["probe"] <= n_logins:
                return (False,
                        "http://%s/tpl/login.html?ip=10.0.0.%d&nasId=1"
                        % (k.GATEWAY_IP, state["probe"]))
            return (True, "http://www.baidu.com/")

        def fake_login(account):
            used.append(account["username"])
            return login_result

        with mock.patch.object(k, "make_session", return_value=session), \
             mock.patch.object(session, "close"), \
             mock.patch.object(k, "probe_network", side_effect=fake_probe), \
             mock.patch.object(k, "do_login", side_effect=fake_login), \
             mock.patch.object(k.time, "sleep", return_value=None):
            k.run_probe_round()
        return used

    def test_success_switches_immediately(self):
        """认证成功后【立刻】换下一个账号，而不是等冷却。"""
        used = self._run_round_with_n_logins(2)
        self.assertEqual(len(used), 2)
        self.assertNotEqual(used[0], used[1],
                            "认证成功后没有立刻换账号，仍是 %s" % used[0])

    def test_three_logins_use_three_distinct_accounts(self):
        used = self._run_round_with_n_logins(3)
        self.assertEqual(used, [a["username"] for a in k.INITIAL_ACCOUNTS],
                         "账号未按顺序轮换，实际: %s" % used)

    def test_no_account_reused_before_full_cycle(self):
        used = self._run_round_with_n_logins(3)
        self.assertEqual(len(used), len(set(used)),
                         "走完一圈前就复用了账号: %s" % used)

    def test_rotation_continues_across_rounds(self):
        """跨轮：第 1 轮用完后，第 2 轮接着走，不回头。"""
        r1 = self._run_round_with_n_logins(1)
        r2 = self._run_round_with_n_logins(1)
        r3 = self._run_round_with_n_logins(1)
        self.assertEqual([r1[0], r2[0], r3[0]],
                         [a["username"] for a in k.INITIAL_ACCOUNTS],
                         "跨轮轮换顺序错误: %s / %s / %s" % (r1, r2, r3))

    def test_wraps_to_first_after_using_all(self):
        seq = []
        for _ in range(4):
            seq.append(self._run_round_with_n_logins(1)[0])
        expected = [a["username"] for a in k.INITIAL_ACCOUNTS]
        self.assertEqual(seq, expected + [expected[0]],
                         "绕回行为错误: %s" % seq)

    def test_failure_keeps_same_account_for_retry(self):
        r1 = self._run_round_with_n_logins(1, login_result=False)
        r2 = self._run_round_with_n_logins(1, login_result=False)
        self.assertEqual(r1, r2, "认证失败后不应切换账号: %s vs %s" % (r1, r2))

    def test_failure_then_success_advances(self):
        self._run_round_with_n_logins(1, login_result=False)
        r = self._run_round_with_n_logins(1, login_result=True)
        self.assertEqual(r[0], k.INITIAL_ACCOUNTS[0]["username"])
        r2 = self._run_round_with_n_logins(1, login_result=True)
        self.assertEqual(r2[0], k.INITIAL_ACCOUNTS[1]["username"])


class TestRoundRobustness(unittest.TestCase):

    def setUp(self):
        k.reset_rotation()

    def test_round_survives_probe_exception(self):
        """单次探测抛异常，整轮不能崩。"""
        session = k.make_session()
        calls = {"n": 0}

        def flaky(_s):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("transient boom")
            return (True, "ok")

        with mock.patch.object(k, "make_session", return_value=session), \
             mock.patch.object(session, "close"), \
             mock.patch.object(k, "probe_network", side_effect=flaky), \
             mock.patch.object(k.time, "sleep", return_value=None):
            k.run_probe_round()

        self.assertEqual(calls["n"], k.PROBE_COUNT_PER_ROUND,
                         "异常后应继续把本轮跑完")

    def test_round_handles_error_inside_round(self):
        session = k.make_session()
        with mock.patch.object(k, "make_session", return_value=session), \
             mock.patch.object(session, "close"), \
             mock.patch.object(k, "probe_network", side_effect=RuntimeError("x")), \
             mock.patch.object(k.time, "sleep", return_value=None):
            k.run_probe_round()

    def test_online_round_triggers_no_login(self):
        session = k.make_session()
        with mock.patch.object(k, "make_session", return_value=session), \
             mock.patch.object(session, "close"), \
             mock.patch.object(k, "probe_network", return_value=(True, "ok")), \
             mock.patch.object(k, "do_login") as login_mock, \
             mock.patch.object(k.time, "sleep", return_value=None):
            k.run_probe_round()
        self.assertEqual(login_mock.call_count, 0, "在线时不该登录")


# ============================================================================
# 主循环 / 命令行
# ============================================================================
class TestMainLoopWatchdog(unittest.TestCase):
    """最关键：主循环遇到任何异常都必须继续活着。"""

    def setUp(self):
        k.USING_EXAMPLE_CONFIG = False
        k.reset_rotation()

    def test_main_survives_repeated_exceptions(self):
        counter = {"n": 0}

        def boom():
            counter["n"] += 1
            if counter["n"] >= 3:
                raise KeyboardInterrupt   # 用它干净地跳出死循环
            raise RuntimeError("catastrophic failure")

        with mock.patch.object(k, "setup_logging"), \
             mock.patch.object(k, "log_rotation_ready"), \
             mock.patch.object(k, "print_banner"), \
             mock.patch.object(k, "run_probe_round", side_effect=boom), \
             mock.patch.object(k, "trim_log"), \
             mock.patch.object(k.time, "sleep", return_value=None):
            k.main()   # 不应抛出 RuntimeError

        self.assertGreaterEqual(counter["n"], 3,
                                "主循环在异常后没有继续，看门狗失效")

    def test_main_refuses_when_using_example_config(self):
        k.USING_EXAMPLE_CONFIG = True
        with mock.patch.object(k, "setup_logging"), \
             mock.patch.object(k, "run_probe_round") as rnd:
            code = k.main()
        self.assertEqual(code, 1, "使用示例配置时必须拒绝启动")
        self.assertEqual(rnd.call_count, 0)


class TestCommandLine(unittest.TestCase):

    def test_once_flag(self):
        args = k.parse_args(["--once"])
        self.assertTrue(args.once)
        self.assertFalse(args.check)

    def test_check_flag(self):
        self.assertTrue(k.parse_args(["--check"]).check)

    def test_config_path_flag(self):
        self.assertEqual(k.parse_args(["--config", "x.json"]).config, "x.json")

    def test_no_args_is_safe(self):
        args = k.parse_args([])
        self.assertFalse(args.once or args.check or args.verbose)

    def test_check_mode_rejects_example_config(self):
        k.USING_EXAMPLE_CONFIG = True
        with mock.patch("builtins.print"):
            code = k.run_check()
        self.assertEqual(code, 1, "示例配置下 --check 应报未通过")


if __name__ == "__main__":
    unittest.main(verbosity=2)
