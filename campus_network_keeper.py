#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
湖南城市学院 校园网 多拨保活脚本  v2.0
=======================================

场景
----
上游是一台刷了 OpenWrt 的路由器，做了多线多拨（mwan3 负载均衡），
手上有 2~4 个校园网账号。校园网单条线路限速 100M，
多条线路叠加后实测可达 300M+（见 docs/speedtest-319mbps.png）。

每条虚拟线路都要单独认证，任何一条掉线都会导致部分流量被网关劫持到
登录页。本脚本持续探测，发现被劫持就立刻用一个空闲账号盲刷登录。

设计要点
--------
1. 配置外置：所有网关地址、账号、参数都在 config.json，代码里没有任何
   个人账号信息（仓库里只放 config.example.json 模板）。
2. 强制直连：requests 会读取 Windows 注册表里的系统代理
   （HKCU\\...\\Internet Settings\\ProxyServer，常见为 127.0.0.1:7897），
   一旦 Clash 退出，请求全部异常。这里 trust_env=False + proxies={} 彻底直连。
3. 外层看门狗：主循环整体 try/except，任何未预期异常只记录不退出。
4. 探测三态化：区分「明确掉线」和「网络不可达」。后者不触发登录，
   避免误判导致疯狂拨号把账号打挂。
5. 账号顺序轮换：一个账号认证成功一次就永久推进到下一个，绝不回头复用。
   原因：同一账号同时只能认证两台设备，多拨线路回头复用会互相顶掉，
   表现为「拨号成功但仍有线路未认证」。

用法
----
    python campus_network_keeper.py            # 前台常驻
    python campus_network_keeper.py --once     # 只跑一轮，适合排查
    python campus_network_keeper.py --check    # 只检查配置和线路状态
    python campus_network_keeper.py --verbose  # DEBUG 日志
    python campus_network_keeper.py --config 其他配置.json
"""

from __future__ import annotations

import argparse
import atexit
import copy
import json
import logging
import os
import random
import sys
import time
import traceback

import requests

# ============================================================================
# 路径与默认配置
# ============================================================================

# 所有路径都相对脚本自身位置解析，和「当前工作目录」无关。
# 这样无论从哪个目录双击/计划任务启动，都能找到 config.json 和日志目录。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
EXAMPLE_CONFIG_FILE = os.path.join(BASE_DIR, "config.example.json")
ENV_CONFIG_PATH = "CAMPUS_KEEPER_CONFIG"

DEFAULT_CONFIG = {
    # --- 网关与接口 ---
    "gateway": "172.27.99.4",
    "login_path": "/api/account/login",
    "csrf_path": "/api/csrf-token",
    "session_path": "/api/r/default",
    # 探测地址：第一个通就算在线；被重定向到网关就算掉线
    "probe_urls": [
        "http://www.baidu.com",
        "http://www.qq.com",
        "http://connect.rom.miui.com/generate_204",
    ],

    # --- 账号（按顺序轮换使用）---
    # 每项：{"username": "...", "password": "...", "isp": "local"}
    "accounts": [],

    # --- 表单字段 ---
    "field_username": "username",
    "field_password": "password",
    "form_fields": {"nasId": "1", "isp": "local", "timeLimit": ""},

    # --- 探测节奏 ---
    "probe_count_per_round": 35,
    "sleep_after_login": 10,
    "sleep_between_rounds": [60, 100],
    "request_timeout": 5,
    "ip_cooldown_seconds": 45,

    # --- 网络行为 ---
    "force_direct_connection": True,

    # --- 日志 ---
    "log": {
        "enabled": True,
        "file": "campus_network_keeper.log",
        "level": "INFO",
        "max_lines": 300,
    },
}

# 配置来源标记：为 True 表示没找到 config.json，临时用了示例模板
USING_EXAMPLE_CONFIG = False
CONFIG_SOURCE = "(内置默认值)"


def _deep_merge(base: dict, override: dict) -> dict:
    """把用户配置合并到默认配置上（只合并一层嵌套字典，足够用）。"""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key].update(value)
        else:
            result[key] = value
    return result


def load_config(path: str | None = None) -> dict:
    """
    读取配置。优先级：
        path 参数 > 环境变量 CAMPUS_KEEPER_CONFIG > config.json > config.example.json

    找不到任何配置文件时退回内置默认值（此时没有账号，无法登录）。
    """
    global USING_EXAMPLE_CONFIG, CONFIG_SOURCE

    candidates = []
    if path:
        candidates.append((os.path.abspath(path), False))
    env_path = os.environ.get(ENV_CONFIG_PATH)
    if env_path:
        candidates.append((os.path.abspath(env_path), False))
    candidates.append((CONFIG_FILE, False))
    candidates.append((EXAMPLE_CONFIG_FILE, True))

    for candidate, is_example in candidates:
        if not os.path.isfile(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
        except Exception as exc:  # 配置写坏了要说清楚，不要静默降级
            raise SystemExit(
                "配置文件解析失败：%s\n  %s: %s\n"
                "提示：JSON 不允许注释，也不能有末尾逗号。" % (candidate, type(exc).__name__, exc)
            )
        if not isinstance(user_cfg, dict):
            raise SystemExit("配置文件顶层必须是 JSON 对象：%s" % candidate)

        USING_EXAMPLE_CONFIG = is_example
        CONFIG_SOURCE = candidate
        return _deep_merge(DEFAULT_CONFIG, user_cfg)

    USING_EXAMPLE_CONFIG = False
    CONFIG_SOURCE = "(内置默认值，无配置文件)"
    return copy.deepcopy(DEFAULT_CONFIG)


def apply_config(cfg: dict) -> None:
    """把配置字典展开成模块级常量（测试和 --config 都依赖这一步）。"""
    global GATEWAY_IP, PROBE_URL, FALLBACK_PROBE_URLS, LOGIN_URL, CSRF_URL, SESSION_URL
    global INITIAL_ACCOUNTS, FIELD_USERNAME, FIELD_PASSWORD, EXTRA_FORM_FIELDS
    global PROBE_COUNT_PER_ROUND, SLEEP_AFTER_LOGIN
    global SLEEP_BETWEEN_ROUNDS_MIN, SLEEP_BETWEEN_ROUNDS_MAX
    global REQUEST_TIMEOUT, IP_COOLDOWN_SECONDS, FORCE_DIRECT_CONNECTION
    global LOG_ENABLED, LOG_FILE, LOG_LEVEL, MAX_LOG_LINES

    GATEWAY_IP = str(cfg.get("gateway", DEFAULT_CONFIG["gateway"])).strip()
    probe_urls = cfg.get("probe_urls") or DEFAULT_CONFIG["probe_urls"]
    PROBE_URL = probe_urls[0]
    FALLBACK_PROBE_URLS = list(probe_urls[1:])

    LOGIN_URL = "http://%s%s" % (GATEWAY_IP, cfg.get("login_path", "/api/account/login"))
    CSRF_URL = "http://%s%s" % (GATEWAY_IP, cfg.get("csrf_path", "/api/csrf-token"))
    SESSION_URL = "http://%s%s" % (GATEWAY_IP, cfg.get("session_path", "/api/r/default"))

    INITIAL_ACCOUNTS = list(cfg.get("accounts") or [])
    FIELD_USERNAME = cfg.get("field_username", "username")
    FIELD_PASSWORD = cfg.get("field_password", "password")
    EXTRA_FORM_FIELDS = dict(cfg.get("form_fields") or {})

    PROBE_COUNT_PER_ROUND = int(cfg.get("probe_count_per_round", 35))
    SLEEP_AFTER_LOGIN = int(cfg.get("sleep_after_login", 10))
    rounds = cfg.get("sleep_between_rounds") or [60, 100]
    SLEEP_BETWEEN_ROUNDS_MIN = int(rounds[0])
    SLEEP_BETWEEN_ROUNDS_MAX = int(rounds[-1])
    REQUEST_TIMEOUT = int(cfg.get("request_timeout", 5))
    IP_COOLDOWN_SECONDS = int(cfg.get("ip_cooldown_seconds", 45))
    FORCE_DIRECT_CONNECTION = bool(cfg.get("force_direct_connection", True))

    log_cfg = cfg.get("log") or {}
    LOG_ENABLED = bool(log_cfg.get("enabled", True))
    LOG_FILE = log_cfg.get("file", "campus_network_keeper.log")
    if not os.path.isabs(LOG_FILE):
        LOG_FILE = os.path.join(BASE_DIR, LOG_FILE)
    level_name = str(log_cfg.get("level", "INFO")).upper()
    LOG_LEVEL = getattr(logging, level_name, logging.INFO)
    MAX_LOG_LINES = int(log_cfg.get("max_lines", 300))


CONFIG = load_config()
apply_config(CONFIG)

# 本进程实际使用的配置来源，启动时打印出来，避免「改了 config 却没生效」
CONFIG_FILE_IN_USE = CONFIG_SOURCE


# ============================================================================
# 代理隔离：这是「进程静默消失」的核心修复
# ============================================================================
def make_session() -> requests.Session:
    """
    创建一个绝对不会走系统代理的 Session。

    Windows 上 requests 会调用 getproxies() 读注册表里的 ProxyServer
    (常见 127.0.0.1:7897)。即使 ProxyEnable=0，只要键还在，请求仍会被
    发往该端口；Clash 一退出就连接异常，旧代码未捕获导致进程静默退出。
    """
    s = requests.Session()
    if FORCE_DIRECT_CONNECTION:
        s.trust_env = False          # 忽略 HTTP_PROXY / 注册表代理
        s.proxies = {}               # 显式清空
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    return s


class TrimmingFileHandler(logging.FileHandler):
    """按行数裁剪日志的 FileHandler，带节流，避免频繁开关文件句柄。"""

    def __init__(self, filename: str, max_lines: int = 200, encoding: str = "utf-8"):
        super().__init__(filename, mode="a", encoding=encoding)
        self.max_lines = max_lines
        self._last_trim = 0.0

    def trim(self) -> None:
        # 节流：至少间隔 60 秒才裁剪一次
        now = time.time()
        if now - self._last_trim < 60:
            return
        self._last_trim = now
        try:
            self.flush()
            with open(self.baseFilename, "r", encoding=self.encoding) as f:
                lines = f.readlines()
            if len(lines) > self.max_lines:
                # 先关闭再覆盖写，写完重新打开，保证句柄有效
                self.close()
                with open(self.baseFilename, "w", encoding=self.encoding) as f:
                    f.writelines(lines[-self.max_lines:])
                self.stream = self._open()
        except Exception:
            # 裁剪失败绝不能影响主逻辑
            try:
                if self.stream is None:
                    self.stream = self._open()
            except Exception:
                pass


_file_handler: TrimmingFileHandler | None = None


def setup_logging() -> None:
    """配置日志：控制台 + 文件，并对控制台编码做兜底。"""
    global _file_handler

    # 控制台 UTF-8。用 try 包裹：打包成无控制台运行时 stdout 可能是 None
    try:
        if sys.stdout is not None:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    logger = logging.getLogger()
    logger.setLevel(LOG_LEVEL)
    logger.handlers.clear()

    # 控制台 handler（存在才加，否则 Windows 下写 None 会抛异常）
    if sys.stdout is not None:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        logger.addHandler(ch)

    if LOG_ENABLED:
        try:
            _file_handler = TrimmingFileHandler(
                LOG_FILE, max_lines=MAX_LOG_LINES, encoding="utf-8"
            )
            _file_handler.setFormatter(fmt)
            logger.addHandler(_file_handler)
        except Exception:
            _file_handler = None


def trim_log() -> None:
    if _file_handler is not None:
        try:
            _file_handler.trim()
        except Exception:
            pass


# ============================================================================
# 账号轮换引擎
# ----------------------------------------------------------------------------
#  语义（按实际使用需求）：
#    - 一个账号认证成功一次，就【永久】推进到下一个账号，绝不回头再用。
#    - 原因：路由器多拨，多条线路共享同一套校园网认证；
#            同一账号同时只能认证两台设备，回头复用会互相顶掉。
#    - 只有【认证失败】时才不推进，下一轮重试同一个账号。
#    - 轮换到末尾后回到第一个，循环使用。
# ============================================================================
_rotation_index = 0          # 当前使用 INITIAL_ACCOUNTS 中的第几个
_auth_success_count = 0      # 累计认证成功次数（仅用于日志）


def _index_after(current: int, used_username: str) -> int:
    """
    计算「从 current 推进一次」之后的索引。

    规则：往后找第一个用户名与 used_username 不同的位置。
    这样即使账号列表里有重复条目，也不会原地打转。
    next_account / peek_next_account / advance_rotation 共用这个函数，
    保证「预测的下一个」和「实际的下一个」永远一致。
    """
    total = len(INITIAL_ACCOUNTS)
    if total == 0:
        return 0
    if total == 1:
        return 0
    for step in range(1, total + 1):
        idx = (current + step) % total
        if INITIAL_ACCOUNTS[idx]["username"] != used_username:
            return idx
    return (current + 1) % total


def next_account() -> dict | None:
    """返回当前轮换位置的账号（深拷贝，防止污染 INITIAL_ACCOUNTS）。"""
    if not INITIAL_ACCOUNTS:
        return None
    acct = INITIAL_ACCOUNTS[_rotation_index % len(INITIAL_ACCOUNTS)]
    return copy.deepcopy(acct)


def peek_next_account() -> str:
    """
    返回「当前账号认证成功后」会被使用的账号名，仅用于日志展示。

    用与 advance_rotation 完全相同的推进规则计算，
    避免出现「日志说下一个是 A，实际却用了 B」的误导。
    """
    if not INITIAL_ACCOUNTS:
        return "(无)"
    cur = INITIAL_ACCOUNTS[_rotation_index % len(INITIAL_ACCOUNTS)]
    nxt_idx = _index_after(_rotation_index, cur["username"])
    return INITIAL_ACCOUNTS[nxt_idx]["username"]


def advance_rotation(used_username: str) -> None:
    """认证成功后推进轮换指针。"""
    global _rotation_index, _auth_success_count
    if not INITIAL_ACCOUNTS:
        return
    _auth_success_count += 1
    _rotation_index = _index_after(_rotation_index, used_username)


def reset_rotation() -> None:
    """复位轮换指针（启动时使用）。"""
    global _rotation_index, _auth_success_count
    _rotation_index = 0
    _auth_success_count = 0


def log_rotation_ready() -> None:
    """启动时打印轮换配置。"""
    total = len(INITIAL_ACCOUNTS)
    if total == 0:
        logging.error("❌ 账号列表为空，无法进行认证！请编辑 config.json 的 accounts")
        return
    logging.info("🔄 账号轮换就绪：共 %d 个账号，当前使用第 %d 个（%s）",
                 total, _rotation_index + 1,
                 INITIAL_ACCOUNTS[_rotation_index % total]["username"])


# ---------------------------------------------------------------------------
# 探测
# ---------------------------------------------------------------------------
def _probe_once(session: requests.Session, url: str) -> tuple[bool | None, str]:
    """
    单次探测。

    Returns:
        (state, info)
        state = True  在线
        state = False 明确掉线（被网关重定向 / 非 2xx-3xx）
        state = None  网络不通/异常，无法判定（不要当成掉线！）
    """
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        final_url = resp.url
        if GATEWAY_IP in final_url:
            return False, final_url
        if resp.status_code in (200, 204):
            return True, final_url
        return False, "%s (HTTP %s)" % (final_url, resp.status_code)
    except requests.exceptions.RequestException as e:
        # 这里只记录，不判定为掉线
        return None, "%s: %s" % (type(e).__name__, e)
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)


def probe_network(session: requests.Session) -> tuple[bool, str]:
    """
    探测线路状态。多地址交叉验证：
      - 任一地址明确返回「在线」  -> 在线
      - 任一地址明确被网关劫持    -> 掉线
      - 全部异常（网络不通）      -> 视为【不确定】，返回 True 避免误拨号

    旧代码把「请求异常」直接当成掉线，是账号/拨号被无限消耗的主因。
    """
    infos = []
    for url in [PROBE_URL] + list(FALLBACK_PROBE_URLS):
        state, info = _probe_once(session, url)
        infos.append("%s -> %s" % (url, info))
        if state is True:
            return True, info      # 有一个通就算在线
        if state is False:
            return False, info     # 明确被劫持，直接判掉线

    # 全部是 None（网络不通/代理异常）：不要误判成掉线去疯狂拨号
    logging.warning("⚠️ 所有探测地址均不可达（网络层问题，非认证问题）：%s",
                    " | ".join(infos))
    return True, "(网络不可达，暂不拨号)"


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------
def _fetch_csrf_token(session: requests.Session) -> str:
    try:
        resp = session.get(CSRF_URL, timeout=REQUEST_TIMEOUT)
        data = resp.json()
        token = data.get("csrf_token", "")
        if token:
            logging.debug("   获取 CSRF token 成功: %s...", token[:16])
        return token
    except Exception as e:
        logging.warning("⚠️ 获取 CSRF token 失败：%s", e)
        return ""


def do_login(account: dict) -> bool:
    """
    使用指定账号进行登录。整个函数体被 try 完全包裹，
    任何异常都返回 False，绝不让异常冒泡杀死主进程。
    """
    session = make_session()
    try:
        # 第一步：建立线路会话上下文
        try:
            session.get(SESSION_URL, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        except Exception as e:
            logging.debug("   访问会话入口失败（忽略）：%s", e)

        csrf_token = _fetch_csrf_token(session)
        if not csrf_token:
            logging.error("❌ 无法获取 CSRF token，跳过本次登录")
            return False

        post_headers = {
            "Connection": "close",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "http://%s/tpl/default/login_account.html" % GATEWAY_IP,
            "X-CSRF-Token": csrf_token,
            "X-Requested-With": "XMLHttpRequest",
        }

        post_data = dict(EXTRA_FORM_FIELDS)
        post_data[FIELD_USERNAME] = account["username"]
        post_data[FIELD_PASSWORD] = account["password"]
        if "isp" in account:
            post_data["isp"] = account["isp"]

        resp = session.post(
            LOGIN_URL,
            data=post_data,
            headers=post_headers,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        logging.info("📤 已提交登录 -> 账号: %s, HTTP %s", account["username"], resp.status_code)

        if resp.status_code == 200:
            try:
                result = resp.json()
            except Exception:
                logging.warning("   ⚠️ 无法解析响应 JSON: %s", resp.text[:200])
                return False

            code = result.get("code", -1)
            msg = result.get("msg", "")
            dial = result.get("dialMsg", "")
            if code == 0:
                logging.info("   ✅ 登录成功！账号: %s, 消息: %s %s",
                             account["username"], msg, "[%s]" % dial if dial else "")
                return True
            logging.warning("   ⚠️ 登录失败！账号: %s, code=%s, 消息: %s",
                            account["username"], code, msg)
            return False

        logging.warning("   ⚠️ HTTP 异常: %s, %s", resp.status_code, resp.text[:200])
        return False

    except Exception as e:
        logging.error("❌ 登录请求失败 -> 账号: %s, 错误: %s: %s",
                      account.get("username"), type(e).__name__, e)
        return False
    finally:
        try:
            session.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
def run_probe_round() -> None:
    """执行一轮探测 + 按需盲刷登录。"""

    session = make_session()
    auth_count = 0
    no_auth_count = 0
    skip_count = 0
    recent_logins: dict[str, float] = {}

    try:
        for i in range(1, PROBE_COUNT_PER_ROUND + 1):
            try:
                is_authed, info = probe_network(session)

                if is_authed:
                    auth_count += 1
                    logging.debug("  [%02d/%02d] ✅ 已在线 (%s)",
                                  i, PROBE_COUNT_PER_ROUND, info)
                    time.sleep(0.2)
                    continue

                # 提取线路 IP
                offline_ip = ""
                if "ip=" in str(info):
                    offline_ip = str(info).split("ip=")[1].split("&")[0]

                # IP 冷却
                now = time.time()
                if offline_ip and offline_ip in recent_logins:
                    if now - recent_logins[offline_ip] < IP_COOLDOWN_SECONDS:
                        skip_count += 1
                        logging.info("  [%02d/%02d] ⏳ 跳过 (IP %s 冷却中)",
                                     i, PROBE_COUNT_PER_ROUND, offline_ip)
                        time.sleep(0.2)
                        continue

                no_auth_count += 1
                logging.info("  [%02d/%02d] ❌ 未认证 -> 触发登录 (%s)",
                             i, PROBE_COUNT_PER_ROUND, info or "(无响应)")

                # ---- 取下一个账号（严格的「一次一换」轮换）----
                account = next_account()
                if account is None:
                    logging.error("❌ 账号列表为空，无法登录，本轮结束")
                    break

                uname = account["username"]
                logging.info("🎯 分配账号: %s（第 %d/%d 个，累计认证成功 %d 次）",
                             uname, _rotation_index + 1, len(INITIAL_ACCOUNTS),
                             _auth_success_count)

                login_ok = do_login(account)

                if login_ok:
                    # 认证成功 -> 永久推进到下一个账号，不回头复用
                    advance_rotation(uname)
                    if offline_ip:
                        recent_logins[offline_ip] = time.time()
                    logging.info("   ➡️ 已切换到下一个账号（下一个: %s）", peek_next_account())
                else:
                    # 认证失败：保留当前账号，下一轮重试同一个
                    logging.info("   ↩️ 账号 %s 认证失败，下一轮将重试同一账号", uname)

                time.sleep(SLEEP_AFTER_LOGIN)

            except Exception as e:
                # 单次探测异常不影响整轮
                logging.error("⚠️ 单次探测处理异常（已跳过）：%s: %s", type(e).__name__, e)
                time.sleep(0.5)

    finally:
        try:
            session.close()
        except Exception:
            pass

    parts = ["在线: %d" % auth_count, "登录: %d" % no_auth_count]
    if skip_count:
        parts.append("跳过: %d" % skip_count)
    parts.append("累计认证成功: %d 次" % _auth_success_count)
    logging.info("📊 本轮结束 -> %s", ", ".join(parts))


def _log_exit() -> None:
    """进程退出时记录原因，方便事后排查。"""
    logging.info("🛑 进程即将退出（atexit 触发）")
    logging.shutdown()


def _install_signal_handlers() -> None:
    """Linux/OpenWrt 下 systemctl stop 发的是 SIGTERM，转成 KeyboardInterrupt 干净退出。"""
    if os.name == "nt":
        return
    try:
        import signal

        def _handle_term(signum, frame):  # noqa: ARG001
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, _handle_term)
    except Exception:
        pass


def print_banner() -> None:
    logging.info("=" * 58)
    logging.info("🚀 校园网盲刷保活脚本已启动 (v2.0)")
    logging.info("   配置文件: %s", CONFIG_FILE_IN_USE)
    logging.info("   网关 IP : %s", GATEWAY_IP)
    logging.info("   登录 URL: %s", LOGIN_URL)
    logging.info("   账号数量: %d（顺序轮换：认证成功即换下一个）", len(INITIAL_ACCOUNTS))
    logging.info("   账号顺序: %s", " -> ".join(a["username"] for a in INITIAL_ACCOUNTS) or "(空)")
    logging.info("   强制直连(忽略系统代理): %s", FORCE_DIRECT_CONNECTION)
    logging.info("   PID: %d", os.getpid())
    logging.info("=" * 58)


def run_check() -> int:
    """
    --check：只体检，不改网络状态。
    返回 0 表示配置和线路都正常。
    """
    print("配置来源: %s" % CONFIG_FILE_IN_USE)
    print("网关地址: %s" % GATEWAY_IP)
    print("账号数量: %d" % len(INITIAL_ACCOUNTS))

    problems = []
    if not INITIAL_ACCOUNTS:
        problems.append("accounts 为空：请在 config.json 里填入至少一个校园网账号")
    for i, acct in enumerate(INITIAL_ACCOUNTS, 1):
        if not acct.get("username") or not acct.get("password"):
            problems.append("第 %d 个账号缺少 username 或 password" % i)

    if USING_EXAMPLE_CONFIG:
        problems.append("当前用的是 config.example.json 示例模板，请复制成 config.json 后填入真实账号")

    if problems:
        print("\n[配置检查] 未通过：")
        for p in problems:
            print("  - %s" % p)
        return 1

    print("\n[配置检查] 通过")
    print("[线路探测] 正在探测（最多 %d 秒）..." % (REQUEST_TIMEOUT * 3))
    session = make_session()
    try:
        ok, info = probe_network(session)
    finally:
        try:
            session.close()
        except Exception:
            pass
    print("[线路状态] %s -> %s" % ("在线 ✅" if ok else "未认证 ❌（脚本会自动登录）", info))
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="campus_network_keeper.py",
        description="湖南城市学院校园网多拨保活脚本 v2.0",
    )
    parser.add_argument("--config", metavar="PATH", help="指定配置文件（默认同目录 config.json）")
    parser.add_argument("--once", action="store_true", help="只跑一轮探测就退出")
    parser.add_argument("--check", action="store_true", help="只检查配置和线路状态，不做登录")
    parser.add_argument("--verbose", action="store_true", help="输出 DEBUG 级日志")
    parser.add_argument("--version", action="version", version="campus-network-keeper 2.0.0")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # 注意：默认不解析 sys.argv，方便被当作库/被测试调用
    args = parse_args(list(argv) if argv is not None else [])

    if args.config:
        apply_config(load_config(args.config))
        globals()["CONFIG_FILE_IN_USE"] = CONFIG_SOURCE

    if args.verbose:
        globals()["LOG_LEVEL"] = logging.DEBUG

    setup_logging()
    atexit.register(_log_exit)
    _install_signal_handlers()

    if args.check:
        return run_check()

    if USING_EXAMPLE_CONFIG:
        logging.error("=" * 58)
        logging.error("❌ 检测到没有 config.json，当前使用 config.example.json 示例模板。")
        logging.error("   请复制 config.example.json 为 config.json，填入你自己的校园网账号后重试。")
        logging.error("   位置: %s", os.path.join(BASE_DIR, "config.json"))
        logging.error("=" * 58)
        return 1

    if not INITIAL_ACCOUNTS:
        logging.error("❌ config.json 里 accounts 为空，无法认证。请至少填入一个账号。")
        return 1

    reset_rotation()
    log_rotation_ready()
    print_banner()

    if args.once:
        logging.info("▶▶▶ 单轮模式（--once）")
        run_probe_round()
        return 0

    round_num = 0
    consecutive_errors = 0

    while True:  # 最外层看门狗：任何异常都不退出
        try:
            round_num += 1
            sleep_sec = random.randint(SLEEP_BETWEEN_ROUNDS_MIN, SLEEP_BETWEEN_ROUNDS_MAX)

            logging.info("")
            logging.info("▶▶▶ 第 %d 轮探测开始（%d 次探测，之后休眠 %d 秒）",
                         round_num, PROBE_COUNT_PER_ROUND, sleep_sec)

            run_probe_round()

            if LOG_ENABLED:
                trim_log()

            logging.info("💤 第 %d 轮结束，休眠 %d 秒...", round_num, sleep_sec)
            consecutive_errors = 0
            time.sleep(sleep_sec)

        except KeyboardInterrupt:
            logging.info("")
            logging.info("🛑 收到 Ctrl+C / SIGTERM，脚本已安全退出。")
            return 0
        except SystemExit:
            raise
        except Exception:
            consecutive_errors += 1
            logging.error("💥 主循环发生未捕获异常（第 %d 次），已自动恢复：\n%s",
                          consecutive_errors, traceback.format_exc())
            # 连续异常时退避，避免疯狂刷日志
            time.sleep(min(30 * consecutive_errors, 300))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
