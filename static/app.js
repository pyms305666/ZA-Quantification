/* 期货评估器前端：行情 + K线（缩放/拖动/十字光标）+ 决策评估 */
"use strict";

/* ---------------- 常量与状态 ---------------- */
const COLORS = { up: "#ef5350", down: "#26a69a", amber: "#e0a93c", cyan: "#4db6e6", muted: "#8b96a3" };
const PERIODS = { 60: "1分钟", 300: "5分钟", 900: "15分钟", 1800: "30分钟", 3600: "60分钟", 86400: "日线" };

const state = {
  symbol: "SHFE.rb2610",
  period: 300,
  kline: [],
  quote: null,
  decision: null,
  ws: null,
  instruments: [],
  exchange: "",  // 交易所过滤
  watchlist: JSON.parse(localStorage.getItem("watchlist") || '["SHFE.rb2610","SHFE.au2612","DCE.m2609"]'),
  // 请求竞态防护：切换合约/周期后，旧请求的迟到响应直接丢弃
  klineReq: 0,
  decisionReq: 0,
  klineAbort: null,
  decisionAbort: null,
  instPoll: null,   // 合约目录进度轮询定时器
};

const CHART_HINT = "滚轮缩放 · 拖拽平移 · 双击复位 · 十字光标查看";

/* ---------------- 工具 ---------------- */
async function fetchJSON(url, options, timeoutMs = 30000) {
  const controller = new AbortController();
  const external = options && options.signal;
  if (external) {
    if (external.aborted) controller.abort();
    else external.addEventListener("abort", () => controller.abort(), { once: true });
  }
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const resp = await fetch(url, { ...(options || {}), signal: controller.signal });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.detail || `HTTP ${resp.status}`);
    return body;
  } finally {
    clearTimeout(timer);
  }
}

function fmtPrice(v, digits) {
  if (v === null || v === undefined) return "--";
  return Number(v).toLocaleString("zh-CN", { minimumFractionDigits: digits ?? 0, maximumFractionDigits: digits ?? 0 });
}
function fmtVol(v) { return v === null || v === undefined ? "--" : Number(v).toLocaleString("zh-CN"); }

function fmtTime(ms, daily) {
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, "0");
  return daily ? `${p(d.getMonth() + 1)}-${p(d.getDate())}` : `${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* 简单移动平均（前 period-1 个为 null） */
function ma(values, period) {
  const out = new Array(values.length).fill(null);
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    sum += values[i];
    if (i >= period) sum -= values[i - period];
    if (i >= period - 1) out[i] = +(sum / period).toFixed(2);
  }
  return out;
}
/* MACD 12/26/9 */
function macdSeries(closes) {
  const ema = (period) => {
    const out = [closes[0]];
    const k = 2 / (period + 1);
    for (let i = 1; i < closes.length; i++) out.push(closes[i] * k + out[i - 1] * (1 - k));
    return out;
  };
  const f = ema(12), s = ema(26);
  const dif = closes.map((_, i) => +(f[i] - s[i]).toFixed(4));
  const dea = [dif[0]];
  for (let i = 1; i < dif.length; i++) dea.push(+(dif[i] * 0.2 + dea[i - 1] * 0.8).toFixed(4));
  const hist = dif.map((v, i) => +((v - dea[i]) * 2).toFixed(4));
  return { dif, dea, hist };
}

/* ---------------- DOM ---------------- */
const $ = (id) => document.getElementById(id);

/* ---------------- 合约列表 ---------------- */
let searchTimer = null;
$("search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadInstruments, 300);
});

async function loadInstruments() {
  try {
    const keyword = $("search").value.trim();
    const params = new URLSearchParams();
    if (keyword) params.set("keyword", keyword);
    if (state.exchange) params.set("exchange", state.exchange);
    const url = `/api/v1/instruments${params.toString() ? `?${params}` : ""}`;
    const data = await fetchJSON(url);
    state.instruments = data.items || [];
    renderContractList();
    const p = data.progress;
    if (p && p.futures_total && !p.done) {
      // 后台目录协程仍在回写：显示进度并继续轮询
      $("inst-count").textContent = `目录加载中 ${p.info_cached}/${p.futures_total}`;
      scheduleInstrumentRefresh(2000);
    } else {
      $("inst-count").textContent = `共 ${data.total} 个`;
      clearTimeout(state.instPoll);
    }
  } catch (e) {
    $("inst-count").textContent = `加载失败（${e.message}），5秒后重试`;
    scheduleInstrumentRefresh(5000);
  }
}

function scheduleInstrumentRefresh(delay) {
  clearTimeout(state.instPoll);
  state.instPoll = setTimeout(loadInstruments, delay);
}

function renderContractList() {
  const list = $("contract-list");
  list.innerHTML = "";
  // 全量渲染（按交易所过滤后通常几十到几百个；关键词搜索时更少）
  for (const item of state.instruments) {
    const row = document.createElement("div");
    row.className = "contract-item" + (item.symbol === state.symbol ? " active" : "");
    row.innerHTML = `<span class="c-name">${item.name || item.instrument_id}</span>
                     <span class="c-code">${item.instrument_id}</span>`;
    row.title = `${item.exchange} · 乘数 ${item.volume_multiple ?? "-"} · 最小变动 ${item.price_tick ?? "-"}`;
    row.addEventListener("click", () => switchSymbol(item.symbol));
    list.appendChild(row);
  }
}

function renderWatchlist() {
  const box = $("watchlist");
  box.innerHTML = "";
  for (const code of state.watchlist) {
    if (code.toUpperCase().startsWith("KQD.")) continue; // 外盘主连不在国内评估范围
    const btn = document.createElement("button");
    btn.textContent = code.split(".")[1] || code;
    btn.title = code;
    btn.addEventListener("click", () => switchSymbol(code));
    box.appendChild(btn);
  }
}
function toggleWatch() {
  const idx = state.watchlist.indexOf(state.symbol);
  if (idx >= 0) state.watchlist.splice(idx, 1);
  else state.watchlist.push(state.symbol);
  localStorage.setItem("watchlist", JSON.stringify(state.watchlist));
  renderWatchlist();
}

/* ---------------- 图表 ---------------- */
const chart = echarts.init($("chart"));

const gridBase = { left: 76, right: 24 };
const option = {
  animation: false,
  backgroundColor: "transparent",
  tooltip: {
    trigger: "axis",
    axisPointer: { type: "cross", label: { backgroundColor: "#2b353d" } },
    backgroundColor: "#1a2029", borderColor: "#262e3a", textStyle: { color: "#e8edf2", fontSize: 12 },
  },
  axisPointer: { link: [{ xAxisIndex: "all" }], label: { backgroundColor: "#2b353d" } },
  legend: {
    data: ["MA5", "MA10", "MA20", "MA60", "DIF", "DEA", "持仓量"],
    top: 2, left: 76, textStyle: { color: "#8b96a3", fontSize: 11 }, itemWidth: 14, itemHeight: 8,
  },
  grid: [
    { ...gridBase, top: 26, height: "52%" },
    { ...gridBase, top: "66%", height: "13%" },
    { ...gridBase, top: "83%", height: "12%" },
  ],
  xAxis: [0, 1, 2].map((i) => ({
    type: "category", gridIndex: i, data: [],
    axisLine: { lineStyle: { color: "#262e3a" } },
    axisLabel: { color: "#8b96a3", fontSize: 10, hideOverlap: true },
    axisTick: { show: false },
    splitLine: { show: i === 0, lineStyle: { color: "#1c232c" } },
  })),
  yAxis: [
    { scale: true, gridIndex: 0, position: "left", axisLabel: { color: "#8b96a3", fontSize: 10 },
      splitLine: { lineStyle: { color: "#1c232c" } } },
    { gridIndex: 1, position: "left", axisLabel: { color: "#8b96a3", fontSize: 10 },
      splitLine: { show: false } },
    { gridIndex: 2, position: "left", axisLabel: { color: "#8b96a3", fontSize: 10 },
      splitLine: { show: false } },
  ],
  dataZoom: [
    { type: "inside", xAxisIndex: [0, 1, 2], start: 0, end: 100 },
    { type: "slider", xAxisIndex: [0, 1, 2], bottom: 2, height: 16,
      borderColor: "#262e3a", backgroundColor: "#151a21",
      fillerColor: "rgba(39,94,82,0.25)", textStyle: { color: "#8b96a3", fontSize: 10 } },
  ],
  series: [],
};

function buildSeries() {
  const k = state.kline;
  const closes = k.map((b) => b.close);
  const ohlc = k.map((b) => [b.open, b.close, b.low, b.high]);
  const dates = k.map((b) => fmtTime(b.datetime, state.period === 86400));
  const vols = k.map((b, i) => ({
    value: b.volume, itemStyle: { color: b.close >= b.open ? COLORS.up : COLORS.down, opacity: 0.85 },
  }));
  const ois = k.map((b) => b.open_interest);
  const { dif, dea, hist } = macdSeries(closes);
  const histBars = hist.map((v, i) => ({
    value: +v.toFixed(4),
    itemStyle: { color: v >= 0 ? COLORS.up : COLORS.down, opacity: 0.85 },
  }));
  const candle = {
    name: "K线", type: "candlestick", data: ohlc,
    itemStyle: { color: COLORS.up, color0: COLORS.down, borderColor: COLORS.up, borderColor0: COLORS.down },
    markLine: {
      silent: true, symbol: "none",
      lineStyle: { color: COLORS.amber, type: "dashed", width: 1 },
      label: { color: COLORS.amber, fontSize: 10, formatter: "{c}" },
      data: [],
    },
  };
  const series = [candle];
  [5, 10, 20, 60].forEach((p) => series.push({
    name: `MA${p}`, type: "line", data: ma(closes, p), symbol: "none", smooth: true,
    lineStyle: { width: 1 }, itemStyle: { opacity: 0 },
  }));
  series.push({ name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: vols, barWidth: "60%" });
  series.push({ name: "持仓量", type: "line", xAxisIndex: 1, yAxisIndex: 1, data: ois, symbol: "none",
    lineStyle: { width: 1, color: COLORS.cyan }, itemStyle: { color: COLORS.cyan } });
  series.push({ name: "MACD", type: "bar", xAxisIndex: 2, yAxisIndex: 2, data: histBars, barWidth: "60%" });
  series.push({ name: "DIF", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: dif, symbol: "none",
    lineStyle: { width: 1, color: "#e8edf2" } });
  series.push({ name: "DEA", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: dea, symbol: "none",
    lineStyle: { width: 1, color: COLORS.amber } });
  return { series, dates };
}

function renderChart() {
  if (!state.kline.length) return;
  const { series, dates } = buildSeries();
  const n = state.kline.length;
  const lastBars = Math.min(120, n);
  const start = Math.max(0, Math.round((1 - lastBars / n) * 100));
  // 注意：不要传 yAxis（首次 setOption 传 yAxis: [null,...] 会让 ECharts 5.5 初始化
  // yAxis 组件失败，导致 candlestick 初始化时 yAxisModel 为 undefined 而崩溃）。
  chart.setOption({
    xAxis: [{ data: dates }, { data: dates }, { data: dates }],
    dataZoom: [{ start, end: 100 }, { start, end: 100 }],
    series,
  });
}

function updateLastPriceLine() {
  if (!state.quote || !state.kline.length || typeof state.quote.last !== "number") return;
  chart.setOption({
    series: [{ markLine: { data: [{ yAxis: state.quote.last, label: { formatter: state.quote.last } }] } }],
  });
}

chart.on("dblclick", () => {
  const n = state.kline.length;
  const lastBars = Math.min(120, n);
  const start = Math.max(0, Math.round((1 - lastBars / n) * 100));
  chart.dispatchAction({ type: "dataZoom", start, end: 100 });
});

/* ---------------- 行情与盘口 ---------------- */
function renderQuoteBar() {
  const q = state.quote;
  if (!q) return;
  const up = q.last >= q.pre_close;
  const color = q.pre_close && q.last !== q.pre_close ? (up ? COLORS.up : COLORS.down) : COLORS.muted;
  const change = q.last - (q.pre_close || q.last);
  const pct = q.pre_close ? (change / q.pre_close * 100) : 0;
  $("qb-last").textContent = fmtPrice(q.last, 2);
  $("qb-last").style.color = color;
  $("qb-change").textContent = `${change >= 0 ? "+" : ""}${change.toFixed(2)}  ${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%`;
  $("qb-change").style.color = color;
  const set = (id, v) => { $(id).textContent = fmtPrice(v, 2); $(id).style.color = v >= (q.pre_close || 0) ? COLORS.up : COLORS.down; };
  set("m-open", q.open); set("m-high", q.high); set("m-low", q.low); set("m-preclose", q.pre_close);
  $("m-volume").textContent = fmtVol(q.volume);
  $("m-oi").textContent = fmtVol(q.open_interest);
  const oiDelta = q.open_interest - (q.pre_open_interest || q.open_interest);
  $("m-oidelta").textContent = `${oiDelta >= 0 ? "+" : ""}${fmtVol(oiDelta)}`;
  $("m-oidelta").style.color = oiDelta >= 0 ? COLORS.up : COLORS.down;
  updateLastPriceLine();
}

function renderDepth() {
  const q = state.quote;
  if (!q) return;
  const body = $("depth-body");
  body.innerHTML = "";
  const ask = q.ask.slice().reverse(); // 卖五 → 卖一
  ask.forEach((level, index) => {
    const row = document.createElement("div");
    row.className = "depth-row sell";
    row.innerHTML = `<span>卖${5 - index}</span><span style="color:${COLORS.up}">${fmtPrice(level.price, 2)}</span><span class="d-vol">${fmtVol(level.volume)}</span>`;
    body.appendChild(row);
  });
  const split = document.createElement("div");
  split.className = "depth-split";
  const lastColor = q.last >= (q.pre_close || q.last) ? COLORS.up : COLORS.down;
  split.style.color = lastColor;
  split.textContent = fmtPrice(q.last, 2);
  body.appendChild(split);
  q.bid.forEach((level, index) => {
    const row = document.createElement("div");
    row.className = "depth-row buy";
    row.innerHTML = `<span>买${index + 1}</span><span style="color:${COLORS.down}">${fmtPrice(level.price, 2)}</span><span class="d-vol">${fmtVol(level.volume)}</span>`;
    body.appendChild(row);
  });
}

/* ---------------- 决策面板 ---------------- */
function renderDecision() {
  const body = $("decision-body");
  const d = state.decision;
  if (!d || d.pending) {
    body.innerHTML = `<div class="dc-note">${d && d.message ? d.message : "等待行情数据…"}</div>`;
    return;
  }
  const dirColor = d.direction === "做多" ? COLORS.up : d.direction === "做空" ? COLORS.down : COLORS.amber;
  const total = Math.max(1, d.score_long + d.score_short);
  const longPct = (d.score_long / total * 100).toFixed(1);
  const shortPct = (d.score_short / total * 100).toFixed(1);
  const rows = [
    ["入场参考", fmtPrice(d.entry, 2), null],
    ["止损", fmtPrice(d.stop, 2), d.stop != null && d.entry != null ? (d.stop > d.entry ? COLORS.down : COLORS.up) : null],
    ["目标一", fmtPrice(d.target1, 2), null],
    ["目标二", fmtPrice(d.target2, 2), null],
    ["目标点数", d.target_points != null ? `${d.target_points} 点` : "--", null],
    ["建议手数", d.contracts != null ? `${d.contracts} 手` : "--", null],
    ["单笔风险", d.risk_amount != null ? `¥${fmtVol(d.risk_amount)}（${d.risk_percent}%）` : "--", null],
    ["合约乘数", d.multiplier != null ? `${d.multiplier} 元/点` : "--", null],
  ];
  body.innerHTML = `
    <div class="dc-head">
      <span id="dc-direction" style="color:${dirColor}">${d.direction}</span>
      <span id="dc-score">多 ${d.score_long} · 空 ${d.score_short} · 总分 ${d.score}</span>
    </div>
    <div class="dc-scorebar">
      <div class="sb-long" style="width:${longPct}%"></div>
      <div class="sb-short" style="width:${shortPct}%"></div>
    </div>
    <div class="dc-grid">
      ${rows.map(([label, value, color]) =>
        `<div class="dc-item"><label>${label}</label><span${color ? ` style="color:${color}"` : ""}>${value}</span></div>`).join("")}
    </div>
    <ul class="dc-rationale">
      ${d.rationale.map((r) => `<li class="plus">${r}</li>`).join("") || `<li>无触发依据</li>`}
    </ul>
    ${d.data_ok ? `<div class="dc-note">评分 ≥60 且多空差 ≥15 才给方向；仅为评估建议，不构成投资建议</div>` : `<div class="dc-note">${d.rationale[0] || "数据不足"}</div>`}`;
}

/* ---------------- 数据拉取 ---------------- */
function setChartLoading(on) {
  const box = $("chart-loading");
  if (box) box.classList.toggle("hidden", !on);
}

async function loadKline() {
  const reqId = ++state.klineReq;
  if (state.klineAbort) state.klineAbort.abort();
  const controller = new AbortController();
  state.klineAbort = controller;
  setChartLoading(true);
  try {
    const data = await fetchJSON(
      `/api/v1/kline/${encodeURIComponent(state.symbol)}?period=${state.period}&count=400`,
      { signal: controller.signal }, 45000);
    if (reqId !== state.klineReq) return; // 期间已切换合约/周期：丢弃迟到响应
    state.kline = data.bars || [];
    $("chart-hint").textContent = CHART_HINT; // 成功即恢复提示（清除历史错误文案）
    renderChart();
    updateLastPriceLine();
  } catch (e) {
    if (reqId !== state.klineReq) return;
    if (e.name === "AbortError") return;
    $("chart-hint").textContent = `K线加载失败：${e.message}`;
  } finally {
    if (reqId === state.klineReq) setChartLoading(false);
  }
}

async function loadDecision() {
  const reqId = ++state.decisionReq;
  if (state.decisionAbort) state.decisionAbort.abort();
  const controller = new AbortController();
  state.decisionAbort = controller;
  try {
    const data = await fetchJSON(
      `/api/v1/decision/${encodeURIComponent(state.symbol)}`,
      { signal: controller.signal }, 45000);
    if (reqId !== state.decisionReq) return;
    state.decision = data;
    renderDecision();
  } catch (e) {
    if (reqId !== state.decisionReq) return;
    if (e.name === "AbortError") return;
    state.decision = null;
    $("decision-body").innerHTML = `<div class="dc-note">评估不可用：${e.message}</div>`;
  }
}

async function loadStatus() {
  try {
    const st = await fetchJSON("/api/v1/status");
    const ok = st.connected;
    $("conn-status").textContent = ok ? "● 天勤已连接" : `● 天勤未连接${st.error ? `：${st.error}` : ""}`;
    $("conn-status").style.color = ok ? COLORS.down : COLORS.amber;
    $("st-data").textContent = `数据源：天勤 TqSdk${st.route ? ` · ${st.route}` : ""}`;
    $("st-ws").textContent = `WebSocket：${state.ws && state.ws.readyState === 1 ? "已连接" : "未连接"}`;
    $("st-ws").className = state.ws && state.ws.readyState === 1 ? "ok" : "err";
  } catch (e) { /* 网关未启动 */ }
}

/* ---------------- WebSocket ---------------- */
function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/market`);
  state.ws = ws;
  ws.onopen = () => {
    ws.send(JSON.stringify({ action: "subscribe", symbols: [state.symbol] }));
    $("st-ws").textContent = "WebSocket：已连接";
    $("st-ws").className = "ok";
  };
  ws.onmessage = (event) => {
    let msg;
    try { msg = JSON.parse(event.data); } catch { return; }
    if (msg.type === "quote" || msg.type === "quote_snapshot") {
      if (msg.symbol !== state.symbol) return;
      state.quote = msg.data;
      renderQuoteBar();
      renderDepth();
      $("st-updated").textContent = `更新：${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
    } else if (msg.type === "subscribed" && msg.failed && msg.failed.length) {
      // 订阅失败（如连接预热期）：5 秒后自动重试
      setTimeout(() => {
        if (state.ws && state.ws.readyState === 1) {
          state.ws.send(JSON.stringify({ action: "subscribe", symbols: [state.symbol] }));
        }
      }, 5000);
    } else if (msg.type === "hello") {
      $("st-ws").textContent = "WebSocket：已连接";
      $("st-ws").className = "ok";
    }
  };
  ws.onclose = () => {
    $("st-ws").textContent = "WebSocket：断开，重连中…";
    $("st-ws").className = "err";
    setTimeout(() => { if (document.visibilityState !== "hidden") connectWS(); }, 3000);
  };
  ws.onerror = () => ws.close();
}

/* ---------------- 合约切换 ---------------- */
async function switchSymbol(symbol) {
  if (symbol === state.symbol && state.kline.length) return;
  state.symbol = symbol;
  state.quote = null;
  state.decision = null;
  $("qb-title").textContent = symbol;
  $("qb-meta").textContent = "";
  renderContractList();
  renderQuoteBar();
  renderDepth();
  renderDecision();
  if (state.ws && state.ws.readyState === 1) {
    state.ws.send(JSON.stringify({ action: "subscribe", symbols: [symbol] }));
  }
  await loadKline();
  loadDecision();
  const item = state.instruments.find((i) => i.symbol === symbol);
  if (item) $("qb-meta").textContent = `${item.name} · ${item.exchange} · 乘数 ${item.volume_multiple ?? "-"}`;
}

/* ---------------- 初始化 ---------------- */
$("periods").addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  document.querySelectorAll("#periods button").forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  state.period = Number(btn.dataset.period);
  loadKline();
});

$("exchange-tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  document.querySelectorAll("#exchange-tabs button").forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  state.exchange = btn.dataset.ex || "";
  loadInstruments();
});

document.querySelector(".brand").addEventListener("click", toggleWatch);

async function init() {
  renderWatchlist();
  // 必须先应用初始 option（含 grid/xAxis/yAxis 定义）：renderChart 的 setOption 是
  // 增量更新，若首次调用就缺 yAxis，ECharts 不会自动创建 yAxis 组件，
  // candlestick 初始化时 yAxisModel 为 undefined 会崩溃。
  chart.setOption(option);
  // 互不阻塞：状态、合约目录、K线、决策并行加载，避免单点卡死整页。
  loadStatus();
  loadInstruments();
  $("qb-title").textContent = state.symbol;
  loadKline();
  loadDecision();
  connectWS();
  setInterval(loadDecision, 6000);
  setInterval(loadKline, 15000);
  setInterval(loadStatus, 15000);
  window.addEventListener("resize", () => chart.resize());
}
init();
