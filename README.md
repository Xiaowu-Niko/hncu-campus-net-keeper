# 🏫 湖南城市学院校园网自动认证神器（支持多拨）

基于 Python 的校园网盲刷保活脚本，专为 **OpenWrt 多 WAN 负载均衡** 环境设计。

## 💡 原理

```
┌─────────────────────────────────────────────────┐
│                  校园网网关                        │
│                172.27.99.4                       │
│  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐        │
│  │ 线路1 │  │ 线路2 │  │ 线路3 │  │ ...  │        │
│  └──┬───┘  └──┬───┘  └──┬───┘  └──┬───┘        │
│     │         │         │         │              │
│  ┌──┴─────────┴─────────┴─────────┴──┐           │
│  │       OpenWrt 多拨路由器           │           │
│  │       (mwan3 负载均衡)             │           │
│  └────────────────┬──────────────────┘           │
│                   │                              │
│              💻 你的电脑                         │
│         运行本脚本持续保活                        │
└─────────────────────────────────────────────────┘
```

- 路由器多线多拨，每条线路独立认证
- 某条线掉线 → HTTP 请求被网关劫持到登录页
- 脚本探测到劫持 → 从账号池取一个账号盲刷登录
- `Connection: close` 强制切换 TCP 连接，覆盖所有线路
- 账号池耗尽自动重置，无限循环

## 🚀 快速开始

### 0. 下载项目

```bash
git clone https://github.com/Xiaowu-Niko/hncu-campus-net-keeper.git
cd hncu-campus-net-keeper
```

> 💡 不会用 git？直接点页面上的绿色 **Code** 按钮 → **Download ZIP**，解压就行。

### 1. 安装依赖

```bash
pip install requests
```

### 2. 获取认证信息（F12 抓包）

用浏览器登录校园网，F12 → Network 查看 POST 请求：

| 字段 | 说明 | 示例 |
|------|------|------|
| `LOGIN_URL` | 登录接口地址 | `http://172.27.99.4/api/account/login` |
| `username` | 用户名字段名 | `username` |
| `password` | 密码字段名 | `password` |
| `nasId` | NAS 设备 ID | `1` |
| `isp` | 运营商代码 | `local`(电信) / `mobile`(移动) / `unicom`(联通) |

**⚠️ 务必确认 CSRF Token 接口：** `http://网关IP/api/csrf-token`

### 3. 配置账号

编辑 `campus_network_keeper.py` 顶部的配置区：

```python
INITIAL_ACCOUNTS = [
    {"username": "你的账号1", "password": "密码1"},
    {"username": "你的账号2", "password": "密码2"},
    {"username": "你的账号3", "password": "密码3"},
]
```

> 💡 `isp` 默认 `"local"`，无需填写。只有学校有多运营商出口（移动/联通）时才需要加 `"isp": "mobile"`。

### 4. 运行

**前台运行（调试）：**
```bash
python campus_network_keeper.py
```

**后台静默运行（Windows）：**
```bash
powershell -Command "Start-Process -WindowStyle Hidden -FilePath 'python.exe' -ArgumentList 'campus_network_keeper.py'"
```

**后台静默运行（Linux/OpenWrt）：**
```bash
nohup python campus_network_keeper.py &
```

## ⚙️ 可调参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `PROBE_COUNT_PER_ROUND` | 30 | 每轮探测次数 |
| `SLEEP_AFTER_LOGIN` | 10s | 登录后等待（给网关反应时间） |
| `SLEEP_BETWEEN_ROUNDS_MIN` | 60s | 轮间最小休眠 |
| `SLEEP_BETWEEN_ROUNDS_MAX` | 100s | 轮间最大休眠 |
| `REQUEST_TIMEOUT` | 5s | 请求超时 |
| `IP_COOLDOWN_SECONDS` | 45s | 同 IP 登录冷却 |
| `MAX_LOG_LINES` | 200 | 日志保留行数 |

## 🔧 进阶

### 多运营商混用

不同运营商的 `isp` 字段值可能不同，通过 F12 抓包确认每个账号对应的 `isp` 值，填入账号配置即可。

### TCP 粘连问题

本脚本使用 `Connection: close` 头强制每次探测建立新 TCP 连接，配合 `requests.Session()` 确保 CSRF Token 获取和登录走同一条线路。

### 注意事项

- 账号数量建议 ≤ 路由器 WAN 口数量
- 首次使用建议 `LOG_LEVEL = logging.DEBUG` 查看详细日志
- 日志仅保留最近 200 行，不会撑爆磁盘

## 📄 License

MIT — 仅供学习交流，请遵守学校网络使用规定。

---

⭐ 如果这个项目帮到了你，给个 Star 让更多同学看到！

