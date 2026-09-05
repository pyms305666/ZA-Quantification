# 第二课：业务层 market/ 逐行全解（docs/11）

> 第一课（docs/10）你已经学会了变量/函数/类/字典/锁。
> 这一课把 **market/ 文件夹的全部代码**一行不落地讲完。这个文件夹是纯 Python、
> 不碰网络，是全项目**最适合逐行学**的部分。
>
> 读法：每段代码块后面跟着逐行拆解。行号对应代码块内的相对顺序。

---

## 2.1 `market/model.py`（93 行）——数据长什么样

### 全文逐段

```python
"""标准行情数据结构：对外接口只暴露本模块定义的数据，客户端无需知道 TqSdk 内部结构。"""
```
文档字符串：声明本文件是"数据契约"——整个项目只认这里定义的数据形状。

```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
```
- 第 1 行：固定写法，让类型标注更灵活；
- `dataclass`：Python 的"自动写类"工具——你只声明有哪些数据，它自动生成构造函数；
- `field(default_factory=list)`：给"可变默认值"用的（直接写 `= []` 会踩 Python 的经典坑）；
- `Optional[float]`：`float` 或 `None` 二选一——"这个字段可能没有值"。

```python
EXCHANGES = ("SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX")
```
国内六大期货交易所。用**元组**（不可变）表示固定清单——防止程序运行中被人改掉。

```python
def split_symbol(symbol: str) -> tuple[str, str]:
    value = (symbol or "").strip()
    if "." in value:
        exchange, _, instrument_id = value.partition(".")
        return exchange.strip().upper(), instrument_id.strip().lower()
    return "", value.lower()
```
逐行：
- `(symbol or "")`：如果 symbol 是 None 就当空字符串——**防空**；
- `.strip()`：去掉首尾空格；
- `partition(".")`：按第一个点切成三段（点前、点本身、点后）；中间的 `_` 是"我不要这段"的命名习惯；
- 返回值统一大写交易所、小写合约——**归一化**（后文多处依赖这个约定）。

```python
@dataclass(frozen=True)
class QuoteLevel:
    price: Optional[float]
    volume: Optional[float]
    def to_dict(self) -> dict:
        return {"price": self.price, "volume": self.volume}
```
- `@dataclass`：自动生成 `__init__`（按字段顺序接收 price、volume）；
- `frozen=True`：造出来之后**不许改**（不可变对象，多线程更安全）；
- `to_dict()`：转成普通字典，方便最后变成 JSON 发给前端。

```python
@dataclass
class MarketQuote:
    symbol: str
    exchange: str
    instrument_id: str
    timestamp: Optional[int]      # epoch 毫秒
    last: Optional[float]
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    pre_close: Optional[float]
    volume: Optional[float]       # 累计成交量（手）
    open_interest: Optional[float]  # 持仓量（手）
    bid: list[QuoteLevel] = field(default_factory=list)
    ask: list[QuoteLevel] = field(default_factory=list)
```
标准行情的完整定义。注意三点：
1. **所有价格字段都是 Optional**——外部数据随时可能缺，模型必须允许"没有"；
2. `bid/ask` 默认是空列表（买一卖一到买五卖五塞在这里）；
3. `to_dict()` 把嵌套结构整体转字典（包括每个 QuoteLevel）。

```python
@dataclass
class Instrument:
    symbol: str
    exchange: str
    instrument_id: str
    name: str = ""
    kind: str = "FUTURE"
    expired: bool = False
    price_tick: Optional[float] = None      # 最小变动价位（如螺纹 1 元）
    volume_multiple: Optional[int] = None   # 合约乘数（如螺纹 10 元/点）
    expire_rest_days: Optional[int] = None  # 距到期剩余自然日
```
合约"身份证"。`price_tick` 和 `volume_multiple` 是决策引擎算止损/手数的关键输入。

### 本文件教你的概念
数据契约（全项目只认我）、dataclass、不可变对象、归一化约定、Optional 表达"可能缺失"。

---

## 2.2 `market/processor.py`（77 行）——脏数据清洗

### 逐段全解

```python
import math
```
数学工具箱（后面用 `math.isfinite` 判断"是不是正常数字"）。

```python
def _number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number
```
**全项目最常用的清洗函数**，逐行：
1. 本来就是空 → 返回空；
2. `try float(value)`：强行转数字。`TypeError`（类型根本不对）/`ValueError`（内容不是数字，比如 "-"）都接住 → 返回 None；
3. `isfinite`：NaN（Not a Number，非数字）和无穷大也算"不正常" → None；
4. 只有干净数字才放行。
**为什么这么严？** 天勤免费行情在非交易时段很多字段是 NaN 或 "-"，一个脏值混进图表整条线就没了。

```python
def _epoch_ms(value: Any) -> Optional[int]:
    if value is None:
        return None
    astype = getattr(value, "astype", None)
    if astype is not None:
        try:
            return int(astype("datetime64[ms]").astype("int64"))
        except (TypeError, ValueError):
            pass
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
```
时间戳统一转成 **epoch 毫秒**（1970-01-01 起算的毫秒数，全世界通用的时间表示）。
- `getattr(value, "astype", None)`：**安全地问**"你有没有 astype 方法？"——没有就返回 None 不报错；
- TqSdk 的时间是 numpy 的纳秒格式，用 `astype` 直接换算成毫秒；
- 不是 numpy 的时间就按普通数字转换。
`getattr(obj, "名字", 默认值)` 是**鸭子类型**的写法："只要你会这个动作，我就当你是鸭子"，不检查出身。

```python
def _levels(quote: Any, side: str) -> list[QuoteLevel]:
    output: list[QuoteLevel] = []
    for index in range(1, 6):
        price = _number(getattr(quote, f"{side}_price{index}", None))
        volume = _number(getattr(quote, f"{side}_volume{index}", None))
        if price is None and volume is None:
            break
        output.append(QuoteLevel(price, volume))
    return output
```
五档盘口提取。`side` 是 "bid" 或 "ask"，循环 1~5 档：
- `f"{side}_price{index}"` 用 **f-string 拼字段名**——bid_price1、bid_price2……这就是"按规律批量访问字段"的技巧；
- 某档价和量都空 → `break` 直接停（后面的档位必然也是空的）——**截断**，不产出无意义的空档。

```python
def to_market_quote(symbol: str, quote: Any) -> Optional[MarketQuote]:
    exchange, instrument_id = split_symbol(symbol)
    last = _number(getattr(quote, "last_price", None))
    if last is None:
        return None
    return MarketQuote(
        symbol=symbol, exchange=exchange, instrument_id=instrument_id,
        timestamp=_epoch_ms(getattr(quote, "datetime", None)),
        last=last,
        open=_number(getattr(quote, "open", None)),
        ...
        bid=_levels(quote, "bid"),
        ask=_levels(quote, "ask"),
    )
```
总装函数：
- `last_price` 是主键——**没有最新价，整条行情就是废的** → 直接返回 None（调用方按"没数据"处理）；
- 其余字段逐个清洗后装进 MarketQuote。
**这一函数教你的**：入口校验（关键字段缺失就整体拒绝）+ 逐字段清洗 + 组装。

---

## 2.3 `market/indicators.py`（131 行）——七个指标逐行

### sma（简单移动平均）

```python
def sma(values: Sequence[Number], period: int) -> Optional[Number]:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period
```
- `values[-period:]`：**切片**语法——取列表最后 period 个（如 `[-3:]` 取最后 3 个）；
- 数据不够或 period 非法 → None（不伪造）。
数学：最近 N 个收盘价的算术平均。

### ema（指数移动平均）

```python
    seed = sum(values[:period]) / period          # 种子值：前 period 个的平均
    multiplier = 2.0 / (period + 1)               # 平滑系数 k = 2/(N+1)
    value = seed
    for close in values[period:]:                 # 从第 period+1 个开始递推
        value = (close - value) * multiplier + value
    return value
```
EMA 是**递推公式**：今天的 EMA = 今天价 × k + 昨天 EMA × (1-k)。
`values[period:]` 是切片：跳过前 period 个（它们已经算进种子了）。
为什么种子的存在重要？没有种子，前几次递推的 EMA 会严重失真（项目注释里专门强调了）。

### macd（12/26/9）

```python
    if len(closes) < slow + signal:
        return None, None, None
```
MACD 需要 EMA26 先成立、DEA 又需要 9 个 DIF，所以最少 35 根。**返回三个值**（DIF、DEA、柱）。

```python
    for index, close in enumerate(closes):
        if index < slow - 1:
            continue
        window = closes[: index + 1]
        fast_ema = sum(window[-fast:]) / fast if fast_ema is None else (close - fast_ema) * fast_k + fast_ema
        slow_ema = sum(window[-slow:]) / slow if slow_ema is None else (close - slow_ema) * slow_k + slow_ema
        dif_list.append(fast_ema - slow_ema)
```
- `enumerate(closes)`：同时拿到"第几个"和"值"；
- `index < slow-1: continue`：前 25 个不够算 EMA26，**跳过**；
- `X if 条件 else Y`：三元表达式——第一次用平均值当种子，之后用递推公式；
- DIF = 快线 EMA12 − 慢线 EMA26（快慢线的距离，衡量短期动能）。

```python
    for dif in dif_list:
        dif_history.append(dif)
        if len(dif_history) == signal:
            dea = sum(dif_history) / signal        # DEA 种子：前 9 个 DIF 平均
        elif dea is not None:
            dea = (dif - dea) * dea_k + dea        # DEA 递推
    dif = dif_list[-1]
    return dif, dea, (dif - dea) * 2               # 柱 = (DIF-DEA)×2
```
DEA 是 DIF 的 EMA9。`dif_list[-1]`：`[-1]` = 列表最后一个。

### rsi（相对强弱，Wilder 平滑）

```python
    for index in range(1, len(closes)):
        change = closes[index] - closes[index - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
```
每天的"涨了多少/跌了多少"分开记：涨取正部，跌取负部取反（都 ≥ 0）。
`range(1, len(closes))` 从第 2 个开始——第 1 个没有"前一天"可比。

```python
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for index in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[index]) / period
        avg_loss = (avg_loss * (period - 1) + losses[index]) / period
```
**Wilder 平滑**：新平均 = (旧平均 × 13 + 今天值) / 14。这是 RSI/ATR 共用的递推（记住它）。

```python
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
```
RSI 公式。特殊处理：跌的总平均为 0（一路涨）时除零会炸 → 直接返回 100。
**边界处理永远在公式后面一步**。

### kdj（随机指标）

```python
    k_value = 50.0
    d_value = 50.0
    for index in range(n - 1, len(closes)):
        window_high = max(highs[index - n + 1: index + 1])
        window_low = min(lows[index - n + 1: index + 1])
        span = window_high - window_low
        rsv = 50.0 if span <= 0 else (closes[index] - window_low) / span * 100.0
        k_value = (k_value * (k_period - 1) + rsv) / k_period
        d_value = (d_value * (d_period - 1) + k_value) / d_period
    j_value = 3 * k_value - 2 * d_value
```
- RSV：今天收盘在最近 9 天高低区间里的位置（0~100，越高越接近天花板）；
- K = RSV 的平滑，D = K 的平滑，J = 3K − 2D（放大波动用）；
- `span <= 0` 防除零——又是边界。

### boll（布林带）

```python
    variance = sum((value - mid) ** 2 for value in window) / period
    stddev = math.sqrt(variance)
    return mid, mid + width * stddev, mid - width * stddev
```
方差→标准差→中轨 ± 2 倍标准差。`** 2` 是平方；`math.sqrt` 开方。
生成器表达式 `sum(x for x in ...)`：不占内存的循环求和。

### atr（真实波幅均值）

```python
        true_range = max(
            highs[index] - lows[index],                      # 当天最高-最低
            abs(highs[index] - closes[index - 1]),           # 跳空高开的部分
            abs(lows[index] - closes[index - 1]),            # 跳空低开的部分
        )
```
TR = 三种波幅取最大（考虑了隔夜跳空）。之后同样是 Wilder 平滑。
**ATR 是决策引擎止损的基石**（1.5×ATR）。

---

## 2.4 `market/evaluator.py`（387 行）——决策引擎逐段

### 常量与门槛

```python
PERIOD_DAILY = 86400        # 一天的秒数（86400 = 24×3600），用秒数当周期 ID
SIGNAL_LONG, SIGNAL_SHORT, SIGNAL_FLAT = "做多", "做空", "观望"
MIN_SCORE = 60              # 信号门槛：总分至少 60
MIN_GAP = 15                # 多空分差至少 15——防止"两边都 50 分"的糊涂账
```
魔法数字**提取成有名字的常量**：改门槛只改一处，读到 `MIN_SCORE` 就懂意思。

### 工具函数

```python
def _closes(bars): return [float(bar["close"]) for bar in bars]
```
列表推导式：把每根 K 线 dict 里的 close 抠出来组成新列表。`_highs/_lows/_volumes` 同理。

```python
def _round_tick(value: float, tick: float, upward: bool = False) -> float:
    if tick <= 0:
        return value
    rounded = math.ceil(value / tick) * tick if upward else math.floor(value / tick) * tick
    return round(rounded, max(0, int(-math.floor(math.log10(tick))) + 1))
```
**按最小变动价位取整**：价格只能停在 1 元、2 元……（螺纹 tick=1）。
- 止损向下取整（floor）、目标向上取整（ceil）——保守方向；
- 最后一行用 `log10(tick)` 算出 tick 有几位小数（0.02 → 2 位），`round` 掉浮点误差。
这行展示了"看似简单的一件事（取整）在金融场景下的真实复杂度"。

```python
def _ma_state(ema_short, ema_long, price) -> int:
    threshold = max(abs(price), 1.0) * 0.0005
    if ema_short - ema_long > threshold:
        return 1
    if ema_long - ema_short > threshold:
        return -1
    return 0
```
均线状态机：+1 多头排列 / -1 空头排列 / 0 粘合。**死区 = 价格的 0.05%**：
两条线差距小于死区就当"粘合"，防止横盘时信号反复跳。

```python
def _macd_state(closes) -> int:
    dif, dea, _ = macd(closes)
    threshold = max(abs(dif), abs(dea), 1.0) * 1e-3
    gap = dif - dea
    ...
```
MACD 同样有死区（量级的 0.1%）。`1e-3` 是科学计数法 = 0.001。

### 趋势因子 `_trend_factor`（满分 40）

```python
    if len(daily_closes) >= 30:
        ema20, ema60 = ema(daily_closes, 20), ema(daily_closes, 60)
        ma_state = _ma_state(ema20, ema60, daily_closes[-1])
        macd_state = _macd_state(daily_closes)
        if ma_state > 0:
            long_score += 6
            notes.append("日线 EMA20>EMA60 多头排列（+6）")
```
结构：**每个判断 = 打分 + 记录理由**。这就是"可解释性"——前端决策面板显示的每一条依据都来自 `notes`。
日线占 24 分（排列 6 + 站上 EMA20 6 + MACD 6 + 共振 6），60 分钟占 16 分（排列 4 + MACD 4 + 共振 8）。

### 动量因子 `_momentum_factor`（满分 25）

```python
    if len(m5) >= 22:
        breakout_high = max(m5_highs[-21:-1])     # 前 20 根的最高（不含当前根）
        if m5_closes[-1] > breakout_high:
            long_score += 8
```
20 周期突破：当前收盘 > 之前 20 根最高点。`[-21:-1]` 切片"倒数第 21 到倒数第 2"——**不含当前根**（突破比较的是"之前"）。
RSI：50~70 多头 +5；30~50 空头 +5；>70 超买反手扣多头 3 分（追高风险）——**同一个指标给两个方向打分的反向逻辑**。

### 量仓因子 `_volume_oi_factor`（满分 20）

```python
    if len(m5) >= 22 and m5[-1].get("volume", 0) > 0:
        average = sum(volumes[-21:-1]) / 20
        if last_volume >= average * 1.5:
```
放量确认：当前量 ≥ 前 20 根均量 × 1.5。持仓量增减与价格方向组合判断（增仓上行/增仓下行/减仓）。
⚠️ 已知问题：这个分支读的 `pre_open_interest` 字段数据层没提供，实际永远不触发（docs/08 §9 问题 4）——**读代码时带着"这个字段真的有值吗"的怀疑，是高级技能**。

### 风险因子 `_risk_factor`（满分 15，多空共用）

```python
    base = 10.0
    atr_pct = atr_value / last * 100
    if atr_pct < 1.0:
        base += 3        # 波动温和，环境加分
    elif atr_pct > 3.0:
        base -= 4        # 波动过大，环境减分
```
风险因子**不区分多空**（风险对两边一样），所以返回值两份相同。

### 主函数 `evaluate`

```python
    klines = {period: [bar for bar in bars if bar.get("close") is not None]
              for period, bars in klines.items()}
```
字典推导式：先整体清洗一遍所有周期的 K 线。

```python
    if last <= 0 or len(m5) < 30 or len(h60) < 30 or len(daily) < 30:
        return {..., "data_ok": False, "rationale": ["历史K线或实时行情不足，暂不评估"]}
```
**数据不足的出口**：诚实地告诉前端"我不评估"，绝不硬算。

```python
    if score_long >= MIN_SCORE and score_long - score_short >= MIN_GAP:
        direction, direction_en = SIGNAL_LONG, "LONG"
```
双门槛（分数够 + 分差够）才给方向。

```python
    if direction != SIGNAL_FLAT:
        distance = max(atr_value * 1.5, tick * 2)
        if direction == SIGNAL_LONG:
            stop = _round_tick(entry - distance, tick, upward=False)
            distance_actual = entry - stop
            target1 = _round_tick(entry + distance_actual * 1.5, tick, upward=True)
            target2 = _round_tick(entry + distance_actual * 3.0, tick, upward=True)
        ...
        raw_count = int(effective_risk // max(distance_actual * multiplier, 1e-9))
        contracts = max(1, min(int(risk.max_contracts), raw_count))
```
风控三件套：
- 止损距离 = max(1.5×ATR, 2×tick)——**至少给两个最小变动的缓冲**；
- 目标 = 1.5R / 3R（R=实际止损距离）；
- 手数 = 风险额 ÷ (止损距离×乘数)，`//` 整除向下取，再 min 上限 10、max 保底 1。

**本文件教你的总纲**：把"老师傅的盘感"翻译成"有门槛、有理由、有风险约束的规则"——这就是量化决策系统的本质。

---

## 本课检查点

- [ ] 能说出 dataclass 和普通 class 的区别
- [ ] 能手算 4 个数的 SMA 和 EMA（k=2/(N+1)）
- [ ] 能解释为什么 `_number` 对 NaN 返回 None 而不是 0
- [ ] 能解释死区阈值防什么
- [ ] 能说出手数公式的每个部分

完成检查点 → 进入 docs/12 第三课（数据层逐行）。
