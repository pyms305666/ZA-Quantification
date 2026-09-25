# ZA量化

> 国内期货行情读取、图表展示与技术分析评估工具。当前源码版本见 [`VERSION`](VERSION)；本文描述仓库当前代码，不替代 `docs/验证记录/` 中带日期的历史验收记录。

项目只读取行情、计算指标并生成参考评估，**不提供下单或自动交易能力**。桌面 Web、Electron 和 Android 端均通过本机运行的 Python 行情后端工作；Android 在设备内嵌入 Python，不依赖本项目自建云服务器。

## 当前状态

- 行情通道：C 直连 DIFF 协议（`tqdiff/`），不通过 `TqSdk/TqApi`；`tq/` 是供上层使用的兼容适配层。
- 后端：FastAPI、REST + WebSocket、合约目录/行情/K线缓存与评估逻辑。
- UI：`static/` 中的 HTML/CSS/JavaScript 与 ECharts。
- 桌面：Python Web 启动器、PyInstaller 配置及 Electron 桌面壳。
- Android：`mobile-app/`，使用 Chaquopy 将 Python 服务嵌入 APK，Java 前台服务管理后端生命周期。
- Android 工程内有 Python 核心代码镜像。更改桌面核心后同步镜像，并运行 `python tools/check_core_drift.py --strict` 检查。
- 桌面打包、APK 签名与真机验收属于不同交付环节；某一项通过不代表其他项也已验收。当前发布和设备证据见 `docs/验证记录/`。

## 快速启动：桌面 Web

Windows PowerShell：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item config.json.example config.json # 首次使用；按需编辑
python launcher.py
```

随后在浏览器打开 `http://127.0.0.1:8000`。也可运行 `python main.py` 启动后端而不自动打开浏览器。首次启动需要提供天勤账号；可在 UI 中登录，也可通过配置或环境变量提供凭据。运行环境需能访问天勤认证和行情服务。

## 配置与凭据

`config.json.example` 是可提交的模板；本地 `config.json` 和 `.tqsdk/` 已加入 Git 忽略。配置字段：

| 字段 | 默认值 | 用途 |
|---|---:|---|
| `tqsdk.account` / `tqsdk.password` | 空 | 天勤账号；字段名沿用历史命名，当前传输由 DIFF 客户端处理 |
| `server.host` | `127.0.0.1` | 本机监听地址 |
| `server.port` | `8000` | HTTP 与 WebSocket 端口 |
| `server.log_level` | `info` | Uvicorn 日志级别 |
| `risk.account_equity` | `50000` | 评估参数中的账户权益（元） |
| `risk.max_loss_per_trade` | `900` | 单笔最大亏损（元） |
| `risk.risk_percent` | `1.8` | 展示用风险比例（%） |
| `risk.max_contracts` | `10` | 建议手数上限 |

凭据覆盖顺序为环境变量 `TQ_ACCOUNT` / `TQ_PASSWORD`、本机保存的凭据、`config.json`。桌面端安装 `keyring` 时，优先将密码存入 Windows 凭据管理器；凭据管理器不可用、写入失败或设置 `TQ_GATEWAY_KEYRING=off` 时会退回 `.tqsdk/credentials.json` 明文存储。手机端在 App 私有目录保存配置与凭据。请勿提交任何真实凭据。

可用 `TQ_GATEWAY_CONFIG`、`TQ_GATEWAY_CREDENTIALS` 分别覆盖桌面配置文件和凭据文件路径。Android 运行目录由 App 私有存储决定。

## 功能和 API

- 合约目录查询、交易所筛选与关键词搜索；目录可从内置精简数据和本地完整缓存恢复，首次补全可能因上游服务限速耗时较长。`/api/v1/status` 提供目录就绪/完整/加载状态。
- 最新报价、订阅管理、K 线（1/5/15/30/60 分钟及日线）与技术评估。
- K 线 UI 展示蜡烛图、均线、成交量、持仓量和 MACD；数据是否实时取决于行情连接和市场时段。
- WebSocket 行情推送：`/ws/market`。

主要 HTTP 路由（符号示例：`SHFE.rb2610`）：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET / POST | `/api/v1/auth` | 查看登录配置状态 / 保存账号并连接（不返回密码） |
| GET | `/api/v1/status` | 连接、订阅、目录和行情接收状态 |
| GET | `/api/v1/instruments?exchange=&keyword=&refresh=` | 搜索与筛选合约 |
| GET | `/api/v1/instruments/{exchange}` | 按交易所查询合约 |
| GET | `/api/v1/options/{underlying}` | 查询标的期权代码 |
| GET | `/api/v1/quote/{symbol}` | 查询最新报价；未订阅时可自动订阅并返回 pending |
| GET | `/api/v1/kline/{symbol}?period=300&count=200` | 查询 K 线；period 为秒，count 限制在 30–1000 |
| GET | `/api/v1/decision/{symbol}` | 多周期技术评估 |
| GET / POST / DELETE | `/api/v1/subscriptions`、`/api/v1/subscriptions/{symbol}` | 查看、添加、删除订阅 |
| WS | `/ws/market` | 行情变更推送及订阅交互 |

桌面 Web 和 Android API 有少量差异；编写客户端时以对应服务实现为准。评估输出只供技术分析参考，不构成投资建议。

## 桌面入口

### Python Web / 可执行文件

开发运行见上方快速启动。PyInstaller 规格文件为根目录的 `ZA量化*.spec`，安装器脚本在 `installer/`；构建产物在 `dist/`（若存在）。启动器会检测同路线服务并复用；端口被其他路线占用时提示冲突。打包模式下若没有浏览器连接，启动器可在空闲一段时间后退出。

### Electron

Electron 是 UI 桌面壳，行情后端仍是独立 Python 进程。它会探测本机服务，必要时按项目虚拟环境、`py -3`、系统 Python 的顺序拉起 `launcher.py`。

```powershell
Set-Location electron
npm install
npm start
```

Electron 壳的打包配置不等于已将 Python 后端、依赖和资源集成为一键安装程序。详细说明见 [`electron/README.md`](electron/README.md)。

## Android 构建

需要 JDK 17、Android SDK Platform 34 / Build Tools 34.0.0，并在 `mobile-app/local.properties` 设置本机 `sdk.dir`（该文件不提交）。Gradle Wrapper 为 8.9；Chaquopy 16.1，Android Gradle Plugin 8.2.2。

```powershell
Set-Location mobile-app
.\gradlew.bat --no-daemon assembleDebug
```

Debug APK 在 `app/build/outputs/apk/debug/app-debug.apk`。发布包使用 `assembleRelease`；签名配置从被忽略的 `keystore.properties` 读取，若无发布密钥配置，构建脚本会回退 debug 签名，交付前必须确认 APK 签名与升级要求。版本号和 ABI 配置见 `mobile-app/app/build.gradle`。详见 [`mobile-app/README.md`](mobile-app/README.md)。

## 目录导航

```text
main.py / launcher.py     Python 服务入口与桌面启动器
config.py                 配置、凭据加载与保存
api/                      FastAPI REST、生命周期与 WebSocket
tqdiff/                   DIFF 认证、目录下载/索引、行情协议客户端
tq/                       合约、订阅和客户端兼容适配层
market/                   行情模型、缓存、指标、评估
services.py               运行时服务装配及行情广播
static/                   桌面 Web UI
electron/                 Electron 桌面壳与测试
mobile-app/               Android/Chaquopy 工程及 Python 镜像
tests/                    Python 单元测试
tools/                    漂移检查、行情/延迟/目录诊断工具
docs/                     架构教学、交接、计划及带日期验证证据
installer/                Windows 安装器脚本与说明
dist/                     本机构建/发布产物（若存在）
```

## 开发与验证

按需执行以下检查：

```powershell
python -m unittest discover -s tests -v
python tools/check_core_drift.py --strict
Set-Location electron; npm test
```

Android 构建需使用上节所列本机 SDK 和 Gradle Wrapper。真实行情、断网恢复、锁屏保活及 UI 行为需要有效天勤账号、合适交易时段和/或 Android 真机；单元测试和 APK 构建不能替代这些验证。项目未包含可据以推断生产网页部署方式的容器或 CI/CD 部署定义。

## 文档索引与时效

- 本文件是项目当前状态的主入口。
- [`docs/01-架构总览.md`](docs/01-架构总览.md) 与 `docs/02`–`docs/07` 为架构/学习材料；深入代码前可从架构总览开始。
- `docs/验证记录/` 是按日期保存的测试证据，应按文档日期理解。
- `docs/项目交接-*.md`、`docs/待办-*`、`docs/后续方案-*` 记录特定时间点的状态和决策，不能直接视为当前待办；涉及版本、签名、发布状态时，应对照当前源码、版本文件与最新验证记录。
- `docs/15-手机版方案.md` 是方案材料；当前 Android 实现以 `mobile-app/` 源码为准。

## 常见排查

1. 连接不上：确认天勤账号、网络和行情服务可用，查看 `/api/v1/status` 的 `connected`、`error` 与 `catalog_*` 字段及后端日志。
2. 报价无变化：检查订阅和 `quote_recv_total`；非交易时段没有持续报价属于预期。
3. K 线为空：确认连接已 ready、合约代码正确、周期受支持；不要将快照当作历史 K 线。
4. 端口冲突：检查 `server.port` 并结束占用服务；同一端口不能同时运行多个行情路线。
5. 手机构建失败：确认 JDK、SDK、`local.properties`、ABI 与 Wrapper 下载条件；核心代码修改后先做镜像漂移检查。
