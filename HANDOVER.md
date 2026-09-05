# ZA量化（期货评估器）项目交接文档

| 项目 | 内容 |
|---|---|
| 项目名称 | ZA量化（行情网关 + 决策评估 + Web UI） |
| 当前版本 | v1.2.0（双路线并行，见 §2 技术路线） |
| 交付日期 | 2026-09-05 |
| 交付形态 | Git 仓库（3 分支）+ Windows 单文件 exe × 2 + 本文档 |
| 文档维护 | 本文件为项目唯一权威交接文档，重大变更须同步更新 |

> **一句话定位**：基于天勤（ShinnyTech）行情数据的国内期货**只读行情网关 + 多因子决策评估系统**，
> 提供专业 K 线 Web 界面与桌面端封装。**只给评估建议，不做自动交易**，不构成投资建议。

---

## 目录

1. [交付物清单](#1-交付物清单)
2. [技术路线（重点）](#2-技术路线重点)
3. [系统架构](#3-系统架构)
4. [目录结构](#4-目录结构)
5. [接口文档](#5-接口文档)
6. [配置说明](#6-配置说明)
7. [构建与部署](#7-构建与部署)
8. [测试与质量报告](#8-测试与质量报告)
9. [已知问题与风险清单](#9-已知问题与风险清单)
10. [运维手册](#10-运维手册)
11. [路线图与建议](#11-路线图与建议)
12. [附录：参考资料](#12-附录参考资料)

---

## 1. 交付物清单

### 1.1 Git 分支（同一基线 `7cb1b9c` 派生）

| 分支 | 版本 | 状态 | 说明 |
|---|---|---|---|
| `master` | v1.1.1（基线） | 保留 | 原始版本 + 问题分析文档 `docs/07` + 测试截图。**未合入任何路线修复**，仅作历史基线 |
| `route-ab-sync-coroutine` | **v1.2.0-ab** | ✅ 已实测 | A+B 路线（保留 TqSdk 的修复），提交 `43e39a3` |
| `route-c-diff-direct` | **v1.2.1-c-diff** | ✅ 已实测 | C 路线（DIFF 协议直连），提交 `273c8b4`（含冷启动 hotfix），**当前推荐主线** |

### 1.2 可执行文件（`dist/`，未入库，可按 §7 重新打包）

| 文件 | 大小 | 对应路线 |
|---|---|---|
| `ZA量化-AB协程版.exe` | 76 MB | A+B（含 Python 运行时 + TqSdk + pandas） |
| `ZA量化-C直连版.exe` | 16 MB | C（含 Python 运行时 + websockets，已剔除 tqsdk；**v1.2.1 重建版**） |
| `ZA量化.exe` | 76 MB | 旧 v1.1.1 遗留，可删除 |

两个新 exe 均已通过冒烟测试（启动 → 天勤连接 → K 线/行情接口验证，见 §8.4）。

### 1.3 文档

| 文件 | 内容 |
|---|---|
| `HANDOVER.md` | 本交接文档 |
| `docs/01~06-*.md` | 面向新手的教学文档（架构 / Electron / 接口 / 前端 / 实战 / 导读） |
| `docs/07-问题分析与修复方案-首屏K线阻塞.md` | v1.1.1 首屏阻塞问题的完整根因分析与 A/B/C 三路线方案论证（含业界参考） |
| `gui-test-screenshots/` | GUI 实测截图证据（ab1~ab3 为 A+B 路线，c1~c2 为 C 路线，旧 4 张为故障现场） |
| `README.md` | 各分支各自维护，首页标注路线与打包命令 |

---

## 2. 技术路线（重点）

### 2.1 两条路线的由来

v1.1.1 存在一个致命缺陷：**前端每次打开页面会触发全量合约目录查询（866 个合约、18 批次串行，
约 2.5~4.5 分钟），独占唯一的 TqSdk 事件循环线程**，导致首屏 K 线/行情 45 秒超时失败；
叠加前端轮询与线程池（40 线程）耗尽，最坏情况整个服务活锁 10 分钟以上。
且该查询超过服务端 120 秒超时，结果总是被丢弃，缓存永远建立不起来。

针对这个病根，形成了两条修复路线（详细论证见 `docs/07`）：

### 2.2 路线对比

| 维度 | **A+B 路线**（route-ab） | **C 路线**（route-c）★推荐 |
|---|---|---|
| 思路 | 保留 TqSdk，"堵点"逐一修复 | 不用 TqSdk，直连天勤 DIFF 协议 |
| 合约目录 | TqApi 协程（`api.create_task`）后台逐批查询 | openmd 静态合约文件一次拉取 + 24h 磁盘缓存（23.9 万合约） |
| 事件循环 | 单线程命令队列 + 协程让出；命令可放弃（超时跳过） | 每条命令都是独立协程并发执行，**无共享队列、无长命令**，结构性免疫堵塞 |
| K 线 | `get_kline_serial` 同步拉取（事件循环内 1~2s） | `set_chart` 订阅式，数据异步到达，重复请求本地缓存直读 |
| 首屏实测 | K 线首包 0.4s，8s 全量就绪 | 连接 2s，页面 9s 全量就绪 |
| 依赖 | tqsdk==3.10.2（连带 pandas/numpy） | websockets + requests（exe 76MB → 16MB） |
| 风险点 | TqSdk 行为黑盒依赖；free-api 预热期等 | 自实现协议层，需自行跟进天勤协议变化；期权列表接口未实现 |
| 适用场景 | 想稳、少改、跟 TqSdk 生态 | 想轻、想彻底掌握协议、后续做交易接入/多语言 |

**选型建议**：两条路线功能对等（评估引擎 `market/` 完全共用）。
若继续演进，**建议以 C 路线为主线**（体积、速度、自主可控均占优），
A+B 分支保留作为 TqSdk 生态的参照实现；合并策略见 §11。

### 2.3 技术栈明细

| 层 | 技术 | 说明 |
|---|---|---|
| 后端框架 | Python 3.13 + FastAPI + uvicorn | sync/async 混合端点（AB 路线已 async 化 + 信号量限流） |
| 行情数据源 | 天勤（ShinnyTech）免费行情 | AB：TqSdk 3.10.2；C：自实现 DIFF 协议（认证/名称服务/WebSocket/静态合约文件） |
| 实时推送 | WebSocket（FastAPI 原生） | 全客户端共享一条行情源，`is_changing`/diff 驱动推送 |
| 前端 | 原生 HTML/CSS/JS + ECharts 5.5 | 无构建工具；三 grid 蜡烛图（主图+量/持仓+MACD），红涨绿跌 |
| 桌面壳 | Electron 33 | 自动拉起/清理 Python 后端，前端零改动 |
| 打包 | PyInstaller onefile | `launcher.py` 为入口；详见 §7 |
| 测试 | unittest（纯离线，FakeClient 替身） | AB 35 个 / C 39 个，全通过 |

### 2.4 C 路线协议链路（自研部分，重点交接）

```
① OAuth 密码模式登录   POST auth.shinnytech.com/.../openid-connect/token
                        (grant_type=password, client_id=shinny_tq, client_secret=<公开SDK常量>)
            ↓ access_token (JWT，含行情权限 grants)
② 名称服务换行情地址   GET api.shinnytech.com/ns?stock=true&backtest=false   (Bearer token)
            ↓ mdurl = wss://free-api.shinnytech.com/t/nfmd/front/mobile
              ★ 实测坑：stock=false 返回的新前置只有快照、没有 K 线历史，必须 stock=true
③ 合约目录（后台并行） GET openmd.shinnytech.com/t/md/symbols/latest.json
            ↓ 全量 JSON（期货+期权+指数+组合，实测约 330MB / 23.9 万合约），流式下载落盘
              .tqsdk/symbol_file.json，24h 缓存；下载期间行情/K线不受影响（~0.7MB/s 网络实测 8 分钟）
④ WebSocket DIFF 会话  客户端发 {"aid":"peek_message"} → 服务器回 {"aid":"rtn_data","data":[...diffs]}
            行情订阅： {"aid":"subscribe_quote","ins_list":"SHFE.rb2610,DCE.m2609"}
            K线订阅： {"aid":"set_chart","chart_id":<id>,"ins_list":..,"duration":<纳秒>,"view_width":N}
            K线行格式（实测）：{"datetime":ns,"open":..,"high":..,"low":..,"close":..,
                                "volume":..,"open_oi":..,"close_oi":..}（字典，不是数组）
```

三个已踩平的协议坑（均已写入代码注释与回归测试）：
1. `ns` 的 `stock` 参数必须为 `true`，否则前置无 K 线历史；
2. K 线行是**字典**而非数组，解析器两种格式兼容；
3. 前置在合约服务就绪（`insserve_ready`）之前收到的 `set_chart` 会被静默丢弃，
   客户端需轮询重发（实测 2~6 秒内就绪）。

### 2.5 AB 路线关键机制（对应修复点）

- **A（接口/前端）**：kline/decision/quote 端点 async 化，命令等待统一走
  `Services.run_command`（`asyncio.to_thread` + `Semaphore(8)`），不再占满 anyio 默认 40 线程；
  `TqClientError` 统一映射 503；前端请求带序号 + `AbortController`，切换后丢弃迟到响应，
  图表加"加载中"遮罩，成功清除错误文案，静态资源带 `?v=` 版本号防缓存。
- **B（TqSdk 协程）**：按官方模式 `api.create_task` 把目录查询挂到事件循环上逐批（50 个/批）
  `await` 执行，批次间让出主循环；`InstrumentManager` 变为进度式只读缓存
  （`needs_futures/on_futures/next_batch/on_result` 四钩子 + `progress()`）；
  命令队列支持"超时放弃"（abandoned 标记，事件循环跳过执行）。

---

## 3. 系统架构

### 3.1 分层架构（两条路线通用）

```
┌──────────────────────────────────────────────────────────────┐
│  前端 static/（原生 JS + ECharts）                             │
│  报价头 · K线三grid · 五档盘口 · 决策面板 · 合约列表/搜索        │
└───────────────▲───────────────────────────▲──────────────────┘
        WebSocket │/ws/market                 │ REST /api/v1/*
┌───────────────┴───────────────────────────┴──────────────────┐
│  api/  FastAPI                                                │
│  http.py(REST) · websocket.py(推送广播) · Services(运行时容器)  │
├──────────────────────────────────────────────────────────────┤
│  market/  行情核心（与数据源解耦，两路线共用）                    │
│  model(标准数据结构) · processor(归一化) · cache(行情缓存)       │
│  indicators(指标库) · evaluator(决策评估引擎)                   │
├──────────────────────────────────────────────────────────────┤
│  数据层（两条路线的分界）                                        │
│  C: tqdiff/（auth 认证与合约文件 + client WebSocket 协程）       │
│     tq/client.py = 兼容壳                                      │
│  AB: tq/client.py（TqApi 单线程事件循环 + 命令队列 + 协程目录）    │
│  共用: tq/instruments.py(合约目录) · tq/subscriber.py(订阅)      │
└───────────────────────────────┬──────────────────────────────┘
                                ▼
                 天勤行情服务（免费账户，只读行情，无交易）
```

### 3.2 关键设计决策

| 决策 | 理由 | 位置 |
|---|---|---|
| 单一行情连接，全客户端共享 | TqSdk/免费账户连接数有限制；广播代替每客户端订阅 | `api/websocket.py` |
| 标准数据结构隔离（`MarketQuote`/`Instrument`） | 上层不接触数据源细节，使 A/B/C 路线可替换 | `market/model.py` |
| 决策引擎纯函数化（输入 K线+快照+风控参数） | 可离线单测，与数据源完全解耦 | `market/evaluator.py` |
| 合约大小写按交易所规范（SHFE/DCE/INE/GFEX 小写、CZCE/CFFEX 大写） | 天勤服务器大小写敏感 | `tq/instruments.py`、`tqdiff/client.py` |
| 桌面壳只清理自己 spawn 的后端进程 | 不误杀用户自启的服务 | `electron/main.js` |

### 3.3 决策评估引擎（业务核心）

多周期（日线 + 60 分 + 5 分）多因子打分，多空对称，满分各 100：

| 因子 | 满分 | 内容 |
|---|---|---|
| 趋势 | 40 | 日线/60 分 EMA20/60 排列（0.05% 死区）、MACD 方向（0.1% 死区）、多周期共振 |
| 动量 | 25 | 5 分/60 分 20 周期突破、RSI 区间（含超买超卖反向减分）、KDJ 金叉死叉 |
| 量仓 | 20 | 放量确认、持仓量增减与价格方向（⚠️ 见 §9 问题 4）、当日量能对比 |
| 风险 | 15 | ATR 波动水平、布林带位置（多空共用同分） |

**信号门槛**：总分 ≥ 60 且多空分差 ≥ 15 才给"做多/做空"，否则"观望"。
**风控参数**（`config.json` 的 `risk` 节）：单笔风险额 = min(900 元, 权益 × 1.8%)；
止损 = 1.5×ATR 按最小变动价位取整；目标 = 1.5R / 3R；
手数 = 风险额 ÷ (止损距离 × 合约乘数)，上限 10 手。**只输出建议，无任何下单逻辑。**

---

## 4. 目录结构

```
├── HANDOVER.md              # 本文档
├── main.py                  # 开发入口（--debug-stack 可选：30s 转储线程栈）
├── launcher.py              # 打包入口（首次运行生成 config、自动开浏览器）
├── config.py / config.json(.example)   # 配置加载与模板（环境变量 TQ_ACCOUNT/TQ_PASSWORD 优先）
├── VERSION                  # 版本号（AB: 1.2.0-ab / C: 1.2.0-c-diff）
├── ZA量化.spec              # PyInstaller spec（exe 名随分支）
├── requirements.txt         # AB: 含 tqsdk==3.10.2；C: websockets+requests+fastapi+uvicorn
├── api/
│   ├── http.py              # REST 路由、Services 容器、lifespan（启动/关闭行情客户端）
│   └── websocket.py         # /ws/market 推送（订阅/快照/增量/ping）
├── market/                  # 与数据源解耦的行情核心（两路线共用）
│   ├── model.py             # MarketQuote(五档)/Instrument/QuoteLevel
│   ├── processor.py         # 数据源 Quote → MarketQuote 归一化（NaN 全清洗）
│   ├── cache.py             # 线程安全最新行情缓存
│   ├── indicators.py        # SMA/EMA/MACD/RSI/KDJ/BOLL/ATR（纯 Python）
│   └── evaluator.py         # 决策评估引擎（纯函数）
├── tq/                      # 数据层接口（两条路线在此分界）
│   ├── client.py            # AB: TqApi 封装；C: tqdiff 兼容壳
│   ├── instruments.py       # 合约目录（AB: 协程钩子进度式；C: 静态文件直读）
│   └── subscriber.py        # 幂等订阅/退订（拒过期、拒临近交割 ≤1 天）
├── tqdiff/                  # ★ C 路线独有：DIFF 协议直连
│   ├── auth.py              # 登录/名称服务/合约文件（流式+缓存）/K线行解析
│   └── client.py            # asyncio 会话线程、WebSocket、命令协程、图表缓冲
├── static/                  # 前端三件套 + vendor/echarts.min.js（资源带 ?v= 版本参数）
├── electron/                # 桌面壳（main.js 自动拉起/清理后端；package.json）
├── tools/make_icon.py       # 图标生成（青/金双正方形）
├── tests/                   # 离线单元测试（test_market/test_decision/test_tq[/test_tqdiff]）
├── docs/                    # 教学 6 章 + 07 问题分析与方案
├── gui-test-screenshots/    # GUI 实测截图证据
├── assets/                  # 图标
├── dist/                    # 打包产物（不入库）
└── .tqsdk/                  # 运行时缓存（C 路线合约文件；不入库）
```

---

## 5. 接口文档

### 5.1 REST（前缀 `http://127.0.0.1:8000/api/v1`）

| 方法 | 路径 | 说明 | 备注 |
|---|---|---|---|
| GET | `/status` | 连接状态、订阅列表、合约数 | 含 `route`（路线标识）与 `catalog`（目录进度，AB 路线） |
| GET | `/instruments?exchange=&keyword=&refresh=` | 合约目录 | AB: 后台进度式，`items` 渐进增长 |
| GET | `/instruments/{exchange}` | 按交易所过滤 | |
| GET | `/options/{underlying}` | 标的期权列表 | **C 路线未实现**（返回 503 明确错误） |
| GET | `/quote/{symbol}` | 最新行情（未订阅自动订阅） | `pending=true` 表示等首笔 |
| GET | `/kline/{symbol}?period=&count=` | K 线 | period: 60/300/900/1800/3600/86400 秒；count 30~1000 |
| GET | `/decision/{symbol}` | 决策评估 | 目录未就绪 503 / 合约不存在 422 / 数据不足 `data_ok=false` |
| GET/POST/DELETE | `/subscriptions` | 订阅管理 | POST body `{"symbols": [...]}` |

错误约定：数据源命令失败统一 **503** + `{"detail": "原因"}`；参数/合约问题 **422**。

### 5.2 WebSocket `/ws/market`

| 方向 | 消息 |
|---|---|
| 客户端→服务端 | `{"action":"subscribe","symbols":[...]}` / `{"action":"unsubscribe",...}` / `{"action":"ping"}` |
| 服务端→客户端 | `{"type":"hello",...}` · `{"type":"subscribed",...}` · `{"type":"quote_snapshot",...}` · `{"type":"quote",...}` · `{"type":"pong"}` |

C 路线注意：订阅幂等跳过（skipped）时也会补发快照（休市时段页面可拿到首笔行情）。

---

## 6. 配置说明

`config.json`（模板 `config.json.example`；路径可用环境变量 `TQ_GATEWAY_CONFIG` 重定向）：

```jsonc
{
  "tqsdk":  { "account": "手机号", "password": "密码" },   // 天勤免费账户：https://www.tqsdk.com 注册
  "server": { "host": "127.0.0.1", "port": 8000, "log_level": "info" },
  "risk":   { "account_equity": 50000,      // 账户权益（元）
              "max_loss_per_trade": 900,    // 单笔最大亏损（元），手数计算基准
              "risk_percent": 1.8,          // 展示用：风险占权益 %
              "max_contracts": 10 }         // 单笔手数上限
}
```

环境变量：`TQ_ACCOUNT` / `TQ_PASSWORD`（覆盖账号，优先级最高）、
`TQ_GATEWAY_CONFIG`（配置路径）、`TQ_MD_URL`（AB 路线行情地址覆盖）、
`TQ_GATEWAY_CACHE`（C 路线缓存目录，默认 `.tqsdk/`）。

⚠️ **安全**：当前仓库历史中存在真实账号密码（`config.json` 曾被提交），交接后应立即：
1) 修改天勤账户密码；2) `git rm --cached config.json`；3) 视需要用 `git filter-repo` 清理历史。详见 §9。

---

## 7. 构建与部署

### 7.1 开发运行

```powershell
# 以 C 路线为例（当前推荐）
git checkout route-c-diff-direct
python -m pip install -r requirements.txt
python main.py                      # 或 python launcher.py（自动开浏览器）
# 浏览器打开 http://127.0.0.1:8000

# 桌面版（Electron 壳，前端零改动）
cd electron && npm install && npm start
# 或双击根目录 启动桌面版.bat
```

### 7.2 打包 exe（产物名自带路线标识，务必与分支对应）

```powershell
pip install pyinstaller pillow
# A+B 路线分支上：
pyinstaller --noconfirm --clean --onefile --name "ZA量化-AB协程版" --icon assets/icon.ico `
  --add-data "static;static" --add-data "config.json.example;." --add-data "VERSION;." launcher.py
# C 路线分支上：
pyinstaller --noconfirm --clean --onefile --name "ZA量化-C直连版" --icon assets/icon.ico `
  --add-data "static;static" --add-data "config.json.example;." --add-data "VERSION;." launcher.py
# 产物在 dist/，与各分支 ZA量化.spec 内容一致
```

### 7.3 exe 交付冒烟清单（每次打包后必做）

1. 双击运行 → 首次自动生成 `config.json` → 填账号重启；
2. 浏览器自动打开 → 右上角"天勤已连接"；
3. `GET /api/v1/status` 返回 `connected:true` 且 `route` 为对应路线；
4. K 线/报价/决策页面有数据；关闭窗口后任务管理器无残留 python 进程。

---

## 8. 测试与质量报告

### 8.1 单元测试（离线，不依赖网络）

```powershell
python -m unittest discover -s tests -v
```

| 分支 | 数量 | 覆盖 |
|---|---|---|
| route-ab | 35 | market 模型/归一化/缓存并发、指标库、评估引擎（涨/跌/震荡/数据不足/手数上限）、合约归一化、订阅幂等、目录钩子与进度 |
| route-c | 39 | 上述全部 + tqdiff 协议解析（合约记录、字典/数组双格式 K 线行、NaN 拒识） |

### 8.2 接口实测（2026-09-02 夜盘真实行情 + 09-05 日间复测）

| 项 | v1.1.1（基线） | route-ab | route-c |
|---|---|---|---|
| 首屏 K 线 | ❌ 45s 超时后 500 | ✅ **0.4s** | ✅ 连接后即用 |
| 打开页面期间 /status | ❌ 可挂起 10min+ | ✅ 即时 | ✅ 即时 |
| 合约目录 | ❌ 永远加载不完 | ✅ 866 后台完成 | ✅ 静态文件一次就绪（581 期货） |
| 行情/五档/决策/WS 推送 | ✅（仅稳定期） | ✅ | ✅ |
| 切合约/切周期 | ❌ 拥塞窗口内不切换 | ✅ 即点即换 | ✅ |

### 8.3 GUI 实测（内置浏览器黑盒测试，截图见 `gui-test-screenshots/`）

- A+B：首屏 8s 截图（ab1）、切合约 au2612 5s 就位（ab2）、切日线即点即换（ab3）；
- C：首屏 8~9s 截图（c1/c2），行情头/K线/盘口/决策/合约列表全量就绪。

### 8.4 exe 冒烟

`ZA量化-AB协程版.exe` 与 `ZA量化-C直连版.exe` 均通过：2s 启动、天勤连接、
`route` 标识正确、K 线接口返回真实数据、静态页面服务正常。

---

## 9. 已知问题与风险清单

| # | 级别 | 问题 | 影响 / 处置建议 |
|---|---|---|---|
| 1 | **高** | `config.json` 真实账号密码已提交进 git 历史（.gitignore 声明了排除但文件先已被跟踪） | 密码随仓库泄露。交接后立即改密 + `git rm --cached` + 清历史 |
| 2 | 中 | v1.1.1/v1.2.0 运行中后端进程曾 2 次无征兆消失（日志无崩溃痕迹） | 已在 C 路线增加文件日志基础；建议上进程看护（NSSM/计划任务/Electron 心跳重启）并持续观察 |
| 3 | 中 | 休市时段（含周末）无实时报价：决策显示等待、快照为最后收盘值 | 天勤免费账户特性，非 bug。C 路线已做订阅快照重放缓解页面空显示 |
| 4 | 中 | 量仓因子"持仓量增减"评分永不触发：`MarketQuote` 未输出 `pre_open_interest`/`direction` 字段，评估器读到恒 None（最多 10 分白丢）；前端"日增仓"恒 +0 | 待修：数据结构补字段（两路线同改），改动小 |
| 5 | 低 | C 路线 `/api/v1/options` 未实现（返回 503 明确错误） | 前端未使用；需要时按 graphql 接口补齐 |
| 6 | 低 | 前端 `fetchJSON` 外部 signal 与自身超时并存（已兼容）；K 线最新价虚线标签右缘轻微裁剪 | 视觉小瑕疵 |
| 7 | 低 | 免费账户合约"剩余天数"字段 AB 路线偶发为 null（C 路线由 expire_datetime 自算） | 不影响订阅拒绝逻辑 |
| 8 | 提示 | 免费行情非交易时段无报价（README 已声明） | 属数据源限制 |
| 9 | 已修复 | **v1.2.0-c 冷启动崩溃**（已在 v1.2.1-c-diff 修复）：目录等待把 asyncio.Event 误传给 shield，冷启动时五档/决策/合约列表全挂（报"An asyncio.Future... required"或"合约不存在"） | 已修复并回归；教训：Event 不是 awaitable，须 wait()；冷启动实测不可省略 |
| 10 | 已更正 | 合约文件实测约 330MB（v1.2.0 时误记 10~20MB），冷启动首次下载在普通宽带约 5~10 分钟 | 已加流式进度日志；缓存 24h 有效，平时秒级 |

---

## 10. 运维手册

- **日志**：C 路线已在 `main.py` 配 `logging.basicConfig(INFO)`，行情连接状态
  （登录/地址/目录下载进度/断线重连）经 `gateway.diff` logger 输出 stdout；
  生产部署建议用 `python main.py 2>gateway.log` 或 logging FileHandler 落盘轮转。
- **端口**：默认 8000；被占用时改 `config.json` 的 `server.port`，或临时
  `TQ_GATEWAY_CONFIG` 指向另一份配置（exe 冒烟即用此法）。
- **缓存**：C 路线合约文件缓存 `.tqsdk/symbol_file.json`（24h TTL）；删掉即强制重新下载。
- **常见故障速查**：
  - `天勤未连接/初始化中`：账号密码错误或网络问题，看日志"天勤登录中"之后的状态行；
  - `合约目录后台下载中`：仅 C 路线冷启动且网络慢时出现，等待自动重试（缓存后秒级）；
  - 决策面板"尚未收到行情"：非交易时段或刚订阅，开盘自动恢复；
  - 页面数据全空但接口正常：浏览器缓存旧前端，强刷（Ctrl+F5；前端已带 `?v=` 参数应不再出现）。

---

## 11. 路线图与建议

1. **分支合并**：建议将 `route-c-diff-direct` 合入 master 作为主线（`--no-ff` 保留路线历史），
   A+B 分支归档保留；若走 C 主线，记得把 AB 分支的前端竞态防护
   （请求序号/AbortController/加载遮罩） cherry-pick 过来，二者不冲突。
2. **短期**：修 §9 问题 1（密码）与问题 4（量仓因子字段）；补 C 路线期权接口。
3. **中期**：前端合约列表虚拟滚动/分页（867 条全量时更顺滑）；决策历史留痕与回放；
   Electron + 后端一体打包（onefile 壳内嵌二进制）。
4. **长期**：若接交易，DIFF 协议层预留了扩展空间（trade front 登录、下单报文），
   评估引擎已按"建议输出"与"执行"解耦设计。

---

## 12. 附录：参考资料

- 天勤官方：[TqSdk 仓库](https://github.com/shinnytech/tqsdk-python) ·
  [协程模式（多实例运行）](https://doc.shinnytech.com/tqsdk/latest/advanced/multi_strategy.html) ·
  [版本变更（3.8+ 协程增强）](https://tqsdk-python.readthedocs.io/en/latest/version.html)
- 同类实现：[tqsdk-go](https://github.com/pseudocodes/tqsdk-go)（Go 版 DIFF 协议） ·
  [tqsdk-rs](https://github.com/pseudocodes/tqsdk-rs) ·
  [vnpy/vnpy_tqsdk](https://github.com/vnpy/vnpy_tqsdk) ·
  [vnpy 事件引擎](https://github.com/vnpy/vnpy/blob/master/vnpy/event/engine.py)
  （及其社区"队列被 hang 住"案例——与本项目 v1.1.1 病根同构）
- 并发模式：[Go singleflight](https://pkg.go.dev/golang.org/x/sync/singleflight) 及 Python 实现
  [coalescer](https://github.com/roman-postnov/coalescer) / [oneflight](https://github.com/eugeneliukindev/oneflight)
- 前端：[Apache ECharts](https://echarts.apache.org/)（sampling/large/progressive） ·
  [KLineChart](https://github.com/klinecharts/KLineChart) ·
  [hyperlist](https://github.com/tbranyen/hyperlist)（虚拟滚动）
- 线程池机制：[AnyIO 线程文档](https://anyio.readthedocs.io/en/stable/threads.html) ·
  [Starlette Thread Pool](https://starlette.dev/threadpool/) ·
  [fastapi#5759](https://github.com/fastapi/fastapi/issues/5759)

---

*交接人确认：三条分支均可从基线 `7cb1b9c` 检出运行；两个 exe 已冒烟验收；
本文档所述实测数据均来自 2026-09-02 夜盘与 2026-09-05 日间的真实行情验证。*
