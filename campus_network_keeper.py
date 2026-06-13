#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
湖南城市学院 校园网 多拨负载均衡 盲刷保活脚本
==============================================
适用场景：
  - 上游 OpenWrt 路由器多线多拨（负载均衡）
  - 电脑端请求被随机分配到不同虚拟网卡
  - 手头有多个校园网账号，需自动检测掉线并盲刷认证
  - 支持电信/移动/联通多运营商账号混合使用

策略：高频并发探测 + 全局账号池（pop 消费 + 自动重置）

GitHub: https://github.com/yourusername/hncu-campus-net-keeper
"""

import copy
import logging
import random
import sys
import time
import requests

# ============================================================================
# !!!!!!!! 配置区域 —— 请根据实际抓包结果填写以下所有项 !!!!!!!!!
# ============================================================================

# --- 网关与认证地址 ---
GATEWAY_IP = "172.27.99.4"
# 探测地址：访问百度首页。在线 → HTTP 200 正常返回；掉线 → 被网关劫持/重定向
PROBE_URL = "http://www.baidu.com"
# POST 登录地址（已根据 F12 抓包确认：POST 发往 /api/account/login）
LOGIN_URL = f"http://{GATEWAY_IP}/api/account/login"

# --- 账号池（！！！请替换为你的真实账号！！！）---
# 每个账号格式：{"username": "学号/手机号", "password": "密码", "isp": "运营商"}
# isp 可选值：电信="local"，移动="mobile"，联通="unicom"（请根据 F12 抓包确认）
# 多拨：账号数量对应路由器 WAN 口数，池内耗尽自动重置无限循环
INITIAL_ACCOUNTS = [
    {"username": "你的账号1", "password": "你的密码1", "isp": "local"},
    # {"username": "你的账号2", "password": "你的密码2", "isp": "local"},
    # {"username": "你的账号3", "password": "你的密码3", "isp": "local"},
]

# --- POST 表单字段名（已根据抓包 HTML 确认：name="username" 和 name="password"）---
FIELD_USERNAME = "username"
FIELD_PASSWORD = "password"
# 额外 POST 表单字段（已根据 F12 抓包 Payload 确认）
EXTRA_FORM_FIELDS = {
    "nasId": "1",
    "isp": "local",
    "timeLimit": "",  # 留空表示不限时
}

# --- 探测参数 ---
PROBE_COUNT_PER_ROUND = 30         # 每轮连续探测次数
SLEEP_AFTER_LOGIN = 10              # 登录后等待秒数（给网关充足反应时间）
SLEEP_BETWEEN_ROUNDS_MIN = 60      # 每轮之间最小休眠秒数
SLEEP_BETWEEN_ROUNDS_MAX = 100      # 每轮之间最大休眠秒数（随机）
REQUEST_TIMEOUT = 5                # 单次请求超时秒数
IP_COOLDOWN_SECONDS = 45           # 同一 IP 登录冷却时间（避免重复登录）

# --- 日志 ---
LOG_FILE = "campus_network_keeper.log"
LOG_LEVEL = logging.INFO  # DEBUG 可看详细日志
MAX_LOG_LINES = 200       # 日志文件最多保留行数（超过后自动裁剪旧记录）

# ============================================================================
# !!!!!!!! 配置区域结束 —— 以下为逻辑代码，无需修改 !!!!!!!!!
# ============================================================================

# 全局账号池（运行时可消耗）
available_accounts: list[dict] = []


class TrimmingFileHandler(logging.FileHandler):
    """自定义 FileHandler：支持按行数裁剪日志文件，仅保留最近 max_lines 行。"""

    def __init__(self, filename: str, max_lines: int = 200, encoding: str = "utf-8"):
        super().__init__(filename, mode="a", encoding=encoding)
        self.max_lines = max_lines

    def trim(self) -> None:
        """裁剪日志文件，只保留最近 max_lines 行。"""
        self.flush()
        self.close()
        try:
            with open(self.baseFilename, "r", encoding=self.encoding) as f:
                lines = f.readlines()
            if len(lines) > self.max_lines:
                with open(self.baseFilename, "w", encoding=self.encoding) as f:
                    f.writelines(lines[-self.max_lines :])
        finally:
            self.stream = open(self.baseFilename, "a", encoding=self.encoding)


# 全局文件 handler 引用（供裁剪日志时使用）
_file_handler: TrimmingFileHandler | None = None


def setup_logging() -> None:
    """配置日志：同时输出到控制台和文件。"""
    global _file_handler

    # 强制控制台使用 UTF-8，解决 Windows GBK 编码乱码问题
    sys.stdout.reconfigure(encoding="utf-8")

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    logger = logging.getLogger()
    logger.setLevel(LOG_LEVEL)

    # 控制台 handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # 文件 handler（支持按行数裁剪）
    _file_handler = TrimmingFileHandler(LOG_FILE, max_lines=MAX_LOG_LINES, encoding="utf-8")
    _file_handler.setFormatter(fmt)
    logger.addHandler(_file_handler)


def trim_log() -> None:
    """裁剪日志文件，仅保留最近 MAX_LOG_LINES 行。"""
    if _file_handler is not None:
        try:
            _file_handler.trim()
        except Exception:
            pass  # 裁剪失败不影响主逻辑


def reset_account_pool() -> None:
    """
    重置账号池：深拷贝初始账号列表，重新填满池子。
    触发条件：池子已空但仍有线路处于未认证状态。
    """
    global available_accounts
    available_accounts = copy.deepcopy(INITIAL_ACCOUNTS)
    logging.info("🔄 账号池已重置，重新载入 %d 个账号", len(available_accounts))


def probe_network() -> tuple[bool, str]:
    """
    发送 GET 请求到外部网站（百度），根据是否被网关劫持判定线路状态。

    判定逻辑：
      - 在线：HTTP 200，URL 未被重定向到网关 → 正常上网
      - 掉线：HTTP 请求被网关劫持，最终跳转到 172.27.99.4 的登录页

    Returns:
        (is_authed, info)
        - is_authed: True 表示已认证在线，False 表示未认证
        - info: 最终 URL 或错误信息
    """
    headers = {
        "Connection": "close",  # 关键！强制关闭连接，迫使路由器切换线路
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    try:
        resp = requests.get(
            PROBE_URL,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        final_url = resp.url
        # 如果被重定向到网关 → 掉线
        if GATEWAY_IP in final_url:
            return False, final_url
        else:
            return True, final_url
    except requests.exceptions.RequestException as e:
        logging.warning("⚠️ 探测请求异常：%s", e)
        return False, ""  # 网络不通也视为需要重新认证


def _fetch_csrf_token(session: requests.Session) -> str:
    """
    从网关获取 CSRF token（网关要求 X-CSRF-Token 头）。

    使用 Session 保持连接，确保 CSRF token 和后续 POST 走同一条线路。
    注意：此处不发送 Connection: close，以维持 TCP 会话粘性。

    Returns:
        CSRF token 字符串；获取失败返回空字符串
    """
    try:
        resp = session.get(
            f"http://{GATEWAY_IP}/api/csrf-token",
            timeout=REQUEST_TIMEOUT,
        )
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
    使用指定账号向网关发起 POST 登录请求。
    用独立 Session 先访问登录页建立上下文，再获取 CSRF token 并提交登录。
    Session 内不使用 Connection: close，确保所有请求走同一 TCP 线路。

    Args:
        account: {"username": "xxx", "password": "xxx"}

    Returns:
        True 表示登录请求已发出（不保证认证成功，因为线路是盲的）
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cache-Control": "no-cache",
    })

    try:
        # 第一步：访问 /api/r/default 建立线路会话上下文（模拟浏览器首次访问）
        session.get(
            f"http://{GATEWAY_IP}/api/r/default",
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )

        # 第二步：获取 CSRF token
        csrf_token = _fetch_csrf_token(session)
        if not csrf_token:
            logging.error("❌ 无法获取 CSRF token，跳过本次登录")
            return False

        # 第三步：POST 登录
        post_headers = {
            "Connection": "close",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": f"http://{GATEWAY_IP}/tpl/default/login_account.html",
            "X-CSRF-Token": csrf_token,
            "X-Requested-With": "XMLHttpRequest",
        }

        post_data = dict(EXTRA_FORM_FIELDS)  # 全局默认表单字段
        post_data[FIELD_USERNAME] = account["username"]
        post_data[FIELD_PASSWORD] = account["password"]
        # 账号级别的字段覆盖全局默认（如 isp）
        for key in ("isp",):
            if key in account:
                post_data[key] = account[key]

        resp = session.post(
            LOGIN_URL,
            data=post_data,
            headers=post_headers,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        logging.info(
            "📤 已提交登录 -> 账号: %s, HTTP %s",
            account["username"],
            resp.status_code,
        )
        if resp.status_code == 200:
            try:
                result = resp.json()
                code = result.get("code", -1)
                msg = result.get("msg", "")
                if code == 0:
                    logging.info("   ✅ 登录成功！账号: %s, 消息: %s", account["username"], msg)
                    return True
                else:
                    logging.warning("   ⚠️ 登录失败！账号: %s, code=%s, 消息: %s", account["username"], code, msg)
                    return False
            except Exception:
                logging.warning("   ⚠️ 无法解析响应 JSON: %s", resp.text[:200])
                return False
        else:
            logging.warning("   ⚠️ HTTP 异常: %s, %s", resp.status_code, resp.text[:200])
            return False

    except requests.exceptions.RequestException as e:
        logging.error("❌ 登录请求失败 -> 账号: %s, 错误: %s", account["username"], e)
        return False
    finally:
        session.close()


def run_probe_round() -> None:
    """
    执行一轮高频盲刷探测（PROBE_COUNT_PER_ROUND 次）。
    每次探测到未认证，就从账号池 pop 一个账号去登录。
    同一 IP 在冷却时间内不会重复登录，避免账号浪费。
    """
    global available_accounts

    auth_count = 0
    no_auth_count = 0
    skip_count = 0
    recent_logins: dict[str, float] = {}  # IP → 登录时间戳

    for i in range(1, PROBE_COUNT_PER_ROUND + 1):
        is_authed, info = probe_network()
        if is_authed:
            auth_count += 1
            logging.debug("  [%02d/%02d] ✅ 已在线 (%s)", i, PROBE_COUNT_PER_ROUND, info)
        else:
            # 从重定向 URL 中提取 IP（如 ip=10.38.126.67）
            offline_ip = ""
            if "ip=" in str(info):
                offline_ip = str(info).split("ip=")[1].split("&")[0]

            # IP 冷却检查：同一 IP 在冷却时间内不重复登录
            now = time.time()
            if offline_ip and offline_ip in recent_logins:
                elapsed = now - recent_logins[offline_ip]
                if elapsed < IP_COOLDOWN_SECONDS:
                    skip_count += 1
                    logging.info(
                        "  [%02d/%02d] ⏳ 跳过 (IP %s %d秒前已登录，冷却中)",
                        i, PROBE_COUNT_PER_ROUND, offline_ip, int(elapsed),
                    )
                    time.sleep(0.2)
                    continue

            no_auth_count += 1
            logging.info("  [%02d/%02d] ❌ 未认证 -> 触发登录 (%s)", i, PROBE_COUNT_PER_ROUND, info or "(无响应)")

            # --- 核心逻辑：消耗型账号池 ---
            if not available_accounts:
                # 池子空了 → 说明之前分配出去的线路又有掉线的 → 重置池子
                logging.warning("⚠️ 账号池已耗尽！触发自动重置...")
                reset_account_pool()

            # 从池子中 pop 一个账号去登录
            account = available_accounts.pop(0)
            logging.info(
                "🎯 分配账号: %s (池内剩余: %d)",
                account["username"],
                len(available_accounts),
            )
            login_ok = do_login(account)

            if login_ok:
                # 记录此 IP 的登录时间
                if offline_ip:
                    recent_logins[offline_ip] = time.time()
            else:
                # 登录失败，账号放回池子（可能是临时后端故障）
                available_accounts.append(account)
                logging.info("   ↩️ 账号 %s 放回池子", account["username"])

            # 登录后短暂休眠，给路由器反应时间
            time.sleep(SLEEP_AFTER_LOGIN)

        # 每次探测之间极短暂间隔（避免请求过于密集），非必须
        time.sleep(0.2)

    # 本轮汇总
    summary_parts = [f"在线: {auth_count}", f"登录: {no_auth_count}"]
    if skip_count > 0:
        summary_parts.append(f"跳过: {skip_count}")
    summary_parts.append(f"池内剩余: {len(available_accounts)}")
    logging.info("📊 本轮结束 -> %s", ", ".join(summary_parts))


def main() -> None:
    """主函数：无尽循环，保活守护。"""
    setup_logging()

    # 初始化账号池
    reset_account_pool()

    logging.info("=" * 50)
    logging.info("🚀 校园网盲刷保活脚本已启动")
    logging.info("   网关 IP: %s", GATEWAY_IP)
    logging.info("   登录 URL: %s", LOGIN_URL)
    logging.info("   账号数量: %d", len(INITIAL_ACCOUNTS))
    logging.info("   每轮探测: %d 次", PROBE_COUNT_PER_ROUND)
    logging.info("   探测方式: 访问外网 (未被网关劫持=在线)")
    logging.info("   日志文件: %s", LOG_FILE)
    logging.info("=" * 50)

    round_num = 0

    try:
        while True:
            round_num += 1
            sleep_sec = random.randint(SLEEP_BETWEEN_ROUNDS_MIN, SLEEP_BETWEEN_ROUNDS_MAX)

            logging.info("")
            logging.info("▶▶▶ 第 %d 轮探测开始（本次 %d 次探测，探测后休眠 %d 秒）", round_num, PROBE_COUNT_PER_ROUND, sleep_sec)

            run_probe_round()

            # 每轮结束后裁剪日志，仅保留最近 MAX_LOG_LINES 行
            trim_log()

            logging.info("💤 第 %d 轮结束，休眠 %d 秒...", round_num, sleep_sec)
            time.sleep(sleep_sec)

    except KeyboardInterrupt:
        logging.info("")
        logging.info("🛑 收到中断信号，脚本已安全退出。")
        sys.exit(0)


if __name__ == "__main__":
    main()
