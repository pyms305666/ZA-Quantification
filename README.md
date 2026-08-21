# ZA量化（行情网关 + 决策评估 + Web UI）v1.1.1

基于 [TqSdk](https://github.com/shinnytech/tqsdk-python) 的国内期货行情网关与决策评估系统。
**提供行情读取、合约评估（多/空/观望 + 止损/目标/风险）与专业 K 线界面；不涉及下单与自动交易。**

## 直接运行（Windows 可执行文件）

- 打包产物：`dist/ZA量化.exe`（单文件，75MB，含 Python 运行时与天勤 SDK）
- 首次运行：自动生成 `config.json` 模板并打开浏览器，填入天勤账号密码后重启即可
- 图标：两个角度差 45° 的正方形（青色正放 + 金色旋转 45°）
- 重新打包：`pip install pyinstaller pillow` 后执行
  `pyinstaller --noconfirm --clean --onefile --name "ZA量化" --icon assets/icon.ico --add-data "static;static" --add-data "config.json.example;." --add-data "VERSION;." launcher.py`

```
                    ┌─────────────────────┐
                    │   国内期货行情源     │
                    └──────────┬──────────┘
                               │
                         TqSdk / TqApi（单连接事件循环线程）
                               │
                    ┌──────────▼──────────┐
                    │  Market Data Core   │
                    │  ① 合约发现(933个)   │
                    │  ② 动态订阅          │
                    │  ③ K线服务          │
                    │  ④ 决策评估引擎      │
                    │  ⑤ 内存缓存          │
                    └──────────┬──────────┘
                               │
                 ┌─────────────┴─────────────┐
                 │                           │
             HTTP REST                  WebSocket
                 │                           │
                 ▼                           ▼
          ┌──────────────┐            ┌──────────────┐
          │ 查询/评估接口  │            │ 行情变更推送   │
          └──────────────┘            └──────────────┘
                 │                           │
                 └─────────────┬─────────────┘
                               ▼
                    Web 前端（ECharts 专业 K 线）
                    浏览器打开 http://127.0.0.1:8000
```

## 快速开始

```powershell
python -m pip install -r requirements.txt
python main.py
# 浏览器打开 http://127.0.0.1:8000
```

天勤账号配置在 `config.json`（本仓库已配置，密码勿提交；也可用环境变量
`TQ_ACCOUNT` / `TQ_PASSWORD` 覆盖）。当前为天勤免费行情账户。

## 界面功能（行业标准风格，红涨绿跌）

- **K 线主图**：蜡烛图 + MA5/10/20/60，最新价虚线
- **副图**：成交量 + 持仓量、MACD（DIF/DEA/柱）
- **交互**：滚轮缩放（以鼠标为中心）、左键拖拽平移、双击复位、十字光标联动三图
- **报价头**：最新价/涨跌/今开/最高/最低/昨结/成交量/持仓量/日增仓
- **五档盘口**：买卖队列实时刷新（WebSocket 变更推送）
- **合约**：933 个真实合约搜索、自选列表（本地保存）、周期切换 1/5/15/30/60 分/日线
- **决策面板**：方向、多空评分、入场/止损/目标一/目标二、目标点数、建议手数、单笔风险、评估依据

## 决策评估系统（只给建议，不自动交易）

多周期（日线 + 60分 + 5分）多因子评分，满分 100：

| 因子 | 满分 | 内容 |
| --- | --- | --- |
| 趋势 | 40 | 日线/60分 EMA20/60 排列、MACD、多周期共振（带死区防震荡误判） |
| 动量 | 25 | 20 周期高低点突破、RSI 区间、KDJ 金叉死叉 |
| 量仓 | 20 | 放量确认、持仓量增减与价格方向、当日量能对比昨日 |
| 风险 | 15 | ATR 波动水平、布林带位置 |

**信号门槛**：总分 ≥ 60 且多空分差 ≥ 15 才给"做多/做空"，否则"观望"并展示多空力量对比。

**风控参数**（`config.json` 的 `risk` 节）：账户权益 5 万、单笔最大亏损 900 元
（1.8%）、最大 10 手。手数 = 单笔风险额 ÷（止损距离 × 合约乘数），止损 = 1.5×ATR
按最小变动价位取整，目标 = 1.5R / 3R（R = 止损距离）。

## REST API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/status` | 天勤连接状态、订阅数、合约数 |
| GET | `/api/v1/instruments?exchange=&keyword=` | 合约目录（动态建立） |
| GET | `/api/v1/options/{underlying}` | 某标的期权代码 |
| GET | `/api/v1/quote/{symbol}` | 最新行情（未订阅自动订阅） |
| GET | `/api/v1/kline/{symbol}?period=300&count=200` | K 线（period：60/300/900/1800/3600/86400 秒） |
| GET | `/api/v1/decision/{symbol}` | 决策评估结果 |
| GET/POST/DELETE | `/api/v1/subscriptions` | 订阅管理 |
| WS | `/ws/market` | 实时行情推送（subscribe / quote / quote_snapshot / pong） |

## 目录结构

```
├── main.py / config.py / config.json(.example)
├── tq/       TqSdk 连接层（client 事件循环 / instruments 合约发现 / subscriber 订阅）
├── market/   行情核心（model / cache / processor）+ 指标库 + 决策引擎
├── api/      FastAPI（http REST + websocket 推送）
├── static/   Web 前端（index.html / app.js / style.css / vendor/echarts.min.js）
└── tests/    单元测试（python -m unittest discover -s tests -v）
```

## 说明

- 行情与历史 K 线来自天勤，合约是否过期以天勤 `expired` 为准。
- 免费行情账户在非交易时段无实时报价（决策显示"等待行情"）；日盘/夜盘开盘后自动恢复。
- 决策系统为技术分析评估建议，不构成投资建议；请自行控制风险。
