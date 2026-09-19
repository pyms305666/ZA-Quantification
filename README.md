# ZA量化（行情网关 + 决策评估 + Web UI）

> 文档更新时间：**2026-09-16**
> 当前开发分支：**`mobile-app`**（基于 C 直连 DIFF 路线），版本文件见 `VERSION`。

这是一个国内期货行情读取、合约评估和 K 线展示项目。它只读取行情并生成技术分析建议，**不包含下单或自动交易功能**。

## 当前架构（以代码为准）

```text
天勤 DIFF 服务
  ├─ OAuth 登录（tqdiff/auth.py）
  ├─ 名称服务换取行情 WebSocket 地址
  ├─ openmd 静态合约目录（本地缓存）
  └─ WebSocket subscribe_quote / set_chart
          │
          ▼
`tqdiff/` DIFF 客户端（独立 asyncio 线程）
          │ 兼容接口
          ▼
`tq/` 适配层 → `market/` 行情缓存、指标、决策引擎
          │
          ├─ FastAPI REST：查询行情、合约、K 线和评估
          ├─ WebSocket：推送行情变化
          └─ `static/`：ECharts Web UI
```

当前 C 路线**不是通过 `TqSdk/TqApi` 连接**；`tq/client.py` 只是保持上层调用兼容的适配层。项目同时保留了一套 Android/Chaquopy 代码副本，见 `mobile-app/app/src/main/python/`；核心文件由 `python tools/check_core_drift.py --strict` 在发布前校验，当前无预期外漂移。

## 快速开始：Python Web 版

在项目根目录执行：

```powershell
python -m pip install -r requirements.txt
python main.py
# 浏览器打开 http://127.0.0.1:8000
```

也可以使用启动器：

```powershell
python launcher.py
```

`launcher.py` 会检查端口、启动 FastAPI，并尝试打开浏览器。首次运行需要在页面中填写天勤账号和密码。账号密码优先级为：

1. 环境变量 `TQ_ACCOUNT` / `TQ_PASSWORD`；
2. `.tqsdk/credentials.json`；
3. `config.json` 中的 `tqsdk` 节。

### 凭据存储说明

页面的“保存并连接”会把账号和密码以**明文 JSON**写入 `.tqsdk/credentials.json`。该目录已加入 `.gitignore`，因此通常不会提交到 Git，但它仍然是本机明文文件；请按本机安全要求保护或删除该文件。更安全的做法是使用环境变量，并避免把密码写入 `config.json`。

## Windows 可执行文件

现有产物位于 `dist/`，包括：

- `ZA量化-C直连版.exe`：C 路线 DIFF 直连版；
- `ZA量化-AB协程版.exe`：另一条 A+B 路线产物；
- `ZA量化-手机版-v1.1.3.apk`：当前 Android 发布包（release 签名；从旧 debug 包升级需先卸载）；下载与校验值见 GitHub Release 与 `dist/sha256.txt`。

C 路线可执行文件由 PyInstaller 打包，包含 Python 运行时；运行时仍需要可用的网络、账号和行情服务。若要重新打包：

```powershell
python -m pip install pyinstaller pillow
pyinstaller --noconfirm --clean --onefile `
  --name "ZA量化-C直连版" `
  --icon assets/icon.ico `
  --add-data "static;static" `
  --add-data "config.json.example;." `
  --add-data "VERSION;." `
  launcher.py
```

## Electron 桌面壳

Electron 只是把 Web UI 装进独立窗口。当前 `electron/main.js` 会先探测 `127.0.0.1:8000`，未发现同路线服务时尝试执行项目根目录的 `launcher.py`，成功后再加载窗口。

```powershell
cd electron
npm install
npm start
```

注意：

- 自动启动会依次尝试项目 `.venv/Scripts/python.exe`、`py -3` 与系统 PATH 的 `python`；
- Electron 当前是桌面壳，不等于已把 Python 后端打进 Electron 安装包；
- 如果后端启动失败，直接在项目根目录执行 `python launcher.py` 能看到更完整的 Python 错误；
- 两条路线默认共用 8000 端口，不能同时占用同一个端口。

Electron 打包只会生成壳的产物；若要做真正的一键安装包，还需要把后端运行时、依赖和资源一起设计和验证。

## Android / Chaquopy

Android 源码位于 `mobile-app/`，使用 Chaquopy 将 Python 后端嵌入 APK。Gradle Wrapper 已纳入仓库；在具备 Android SDK 34、JDK 17 与网络依赖的环境执行：

```powershell
cd mobile-app
.\gradlew.bat --no-daemon assembleDebug
```

首次构建前需在 `mobile-app/local.properties` 配置本机 `sdk.dir`（该文件不会提交）。产物位于 `app/build/outputs/apk/debug/app-debug.apk`；每次候选包均须另行进行真机行情连接验证。

## 界面与评估功能

- K 线：蜡烛图、MA5/10/20/60、最新价线；
- 副图：成交量、持仓量、MACD；
- 盘口：买卖五档（以服务端实际返回为准）；
- 合约：搜索、自选、1/5/15/30/60 分钟和日线；
- 评估：趋势、动量、量仓、风险多因子评分，输出做多/做空/观望及止损、目标和建议手数。

评估结果只是技术分析输出，不构成投资建议。

## REST / WebSocket API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/status` | 连接状态、订阅数、合约数、路线标识 |
| GET | `/api/v1/instruments?exchange=&keyword=` | 合约目录 |
| GET | `/api/v1/options/{underlying}` | 标的期权代码 |
| GET | `/api/v1/quote/{symbol}` | 最新行情；未订阅时可能自动订阅 |
| GET | `/api/v1/kline/{symbol}?period=300&count=200` | K 线，周期支持 60/300/900/1800/3600/86400 秒 |
| GET | `/api/v1/decision/{symbol}` | 决策评估 |
| GET/POST/DELETE | `/api/v1/subscriptions` | 订阅管理 |
| WS | `/ws/market` | 行情订阅与变更推送 |

## 验证状态（必须区分自动化测试和真实行情）

本次检查执行了：

```powershell
python -m unittest discover -s tests -v
python -m compileall .
```

当前 v1.1.3 源码已通过 **79 个 Python 单元测试**、核心镜像漂移检查和 Electron 后端逻辑测试；桌面端已在 2026-09-14 交易时段验收实时行情、五周期历史 K 线与端到端延迟（P95 0.5ms）。

2026-09-16 已完成手机端 WS 广播、启动订阅、重连订阅、锁屏保活、合约列表、中文搜索与 K 线渲染修复，并在 vivo V2528A 真机交易时段验证。当前发布版本为 V1.1.2；完整状态、部署边界与后续操作见 `docs/项目交接-V1.1.2.md`。

## 故障排查

1. **状态显示未连接**：检查 `TQ_ACCOUNT/TQ_PASSWORD`、网络、账号权限和服务端返回；查看 `python launcher.py` 的控制台日志。
2. **行情为空**：先请求 `/api/v1/status`，确认 `connected`/`ready`，再请求标准合约代码，例如 `SHFE.rb2610`；非交易时段可能没有实时变化。
3. **历史 K 线为空**：记录合约、周期、`count` 和原始响应；不要用实时快照冒充历史数据。
4. **端口被占用**：结束占用 8000 的旧路线，或修改 `config.json` 的 `server.port`。
5. **Electron 打不开**：先确认 `python --version`、依赖安装和 `python launcher.py` 是否能独立启动。
6. **Android 无法构建**：确认 JDK 17、Android SDK 34、`mobile-app/local.properties` 的 `sdk.dir` 和首次 Wrapper 下载网络可用。

## 目录结构

```text
├── main.py / launcher.py / config.py / config.json(.example)
├── tq/       上层兼容适配（合约、订阅）
├── tqdiff/   DIFF 认证、合约目录和 WebSocket 行情客户端
├── market/   行情模型、缓存、指标和决策引擎
├── api/      FastAPI REST 与 WebSocket
├── static/   Web 前端
├── electron/ Electron 桌面壳
├── mobile-app/ Android/Chaquopy 工程（含一份 Python 副本）
├── tests/    Python 单元测试
└── docs/     架构、教学和问题报告
```

## 已知维护问题

详细记录见 [`docs/项目交接-V1.1.2.md`](docs/项目交接-V1.1.2.md)。v1.1.3 已解决：合约目录断点续传（gzip 优先 + identity/Range 续传）、Android release 签名、桌面凭据迁入 Windows 凭据管理器。当前主要维护风险是桌面端与移动端代码双份、手机端明文凭据（私有沙箱内）、线上网页服务器部署流程未确认。
