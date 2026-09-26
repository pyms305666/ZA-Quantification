/* ZA量化 手机版前端 · 连接本机后端（127.0.0.1:8000，同源） */
"use strict";

/* ---------- 状态 ---------- */
const KLINE_PERIODS = [
  { p: 60, label: "1 分钟" }, { p: 300, label: "5 分钟" }, { p: 900, label: "15 分钟" },
  { p: 1800, label: "30 分钟" }, { p: 3600, label: "60 分钟" }, { p: 86400, label: "日线" },
];
const state = {
  symbol: "SHFE.rb2610",
  period: KLINE_PERIODS.some(x => x.p === Number(localStorage.getItem("klinePeriod")))
    ? Number(localStorage.getItem("klinePeriod")) : 300,
  kline: [],
  quote: null,
  decision: null,
  decisionReq: 0,
  instruments: [],
  watchlist: JSON.parse(localStorage.getItem("watchlist") || '["SHFE.rb2610","SHFE.au2612","DCE.m2609"]'),
  screen: "quotes",
  klineReq: 0,
  ws: null,
  chart: null,
  macdChart: null,
  catalogReady: false,       // 后端合约目录是否就绪
  instrumentsLoading: false, // 防止重复拉取全量合约目录
  quotes: {},                // symbol -> quote 缓存：切屏/重建列表时回填价格，避免 -- 闪失
};
const UP = "#ef5350", DOWN = "#26a69a", AMBER = "#e0a93c", MUTED = "#8b96a3";
const $ = (id) => document.getElementById(id);
const fmt = (v, d = 2) => v == null ? "--" : Number(v).toLocaleString("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d });
const now = () => new Date().toLocaleTimeString("zh-CN", { hour12: false });

/* ---------- 接口 ---------- */
/**
 * 后端 REST 请求封装：JSON 解析 + 超时中止 + 错误信息归一化。
 * @param {string} path 接口路径（如 "/api/v1/status"）
 * @param {number} timeoutMs 超时毫秒数（默认 45s，K线/决策类接口较慢）
 * @returns {Promise<Object>} 后端 JSON；非 2xx 抛 Error(detail)，供各 load* 统一捕获
 */
async function api(path, timeoutMs = 45000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const r = await fetch(path, { signal: controller.signal });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
    return body;
  } finally { clearTimeout(timer); }
}

/* ---------- 屏幕切换 ---------- */
/**
 * 切换主界面屏幕：全部 .screen 加 hidden，仅显示目标屏；底部 tab 高亮同步。
 * 切到 K线页时延迟 60ms 触发图表 resize（容器从 display:none 恢复后尺寸才生效）；
 * 切回行情页时重建自选列表。
 * @param {string} name 屏幕名："quotes" | "kline" | "decision" | "me"
 */
function showScreen(name) {
  for (const s of document.querySelectorAll(".screen")) s.classList.add("hidden");
  $("screen-" + name).classList.remove("hidden");
  for (const t of document.querySelectorAll("#tabbar .tab"))
    t.classList.toggle("active", t.dataset.s === name || (name === "depth" && t.dataset.s === "kline"));
  state.screen = name;
  if (name === "kline") { setTimeout(() => { state.chart && state.chart.resize(); state.macdChart && state.macdChart.resize(); }, 60); }
  if (name === "quotes") renderWatchlist();
}

/* ---------- 行情主页 ---------- */
/* 从自选里移除一个合约（长按/点✕）；刷新自选列表并更新订阅 */
function removeWatch(symbol) {
  const i = state.watchlist.indexOf(symbol);
  if (i < 0) return;
  state.watchlist.splice(i, 1);
  localStorage.setItem("watchlist", JSON.stringify(state.watchlist));
  if (symbol === state.symbol) {
    // 删的是当前查看的合约：切回第一个自选，否则留在空页
    state.symbol = state.watchlist[0] || state.symbol;
    $("k-name").textContent = state.symbol.split(".")[1];
    $("k-code").textContent = state.symbol;
    $("d-code").textContent = state.symbol;
    $("d-name").textContent = state.symbol;
    if (state.ws && state.ws.readyState === 1) {
      state.ws.send(JSON.stringify({ action: "subscribe", symbols: state.watchlist.length ? state.watchlist : [state.symbol] }));
    }
  } else if (state.ws && state.ws.readyState === 1) {
    // 删除非当前合约：取消订阅该合约，避免残留
    state.ws.send(JSON.stringify({ action: "unsubscribe", symbols: [symbol] }));
  }
  renderWatchlist();
}
/**
 * 重建搜索结果列表：清空容器、重置分页游标后渲染第一批（50 条）。
 * 关键字搜索（缺陷 E）与全量浏览（缺陷 D 滚动分批）共用此入口。
 */
function renderSearch() {
  const box = $("resultlist");
  box.innerHTML = "";
  state.searchPage = 0;          // 缺陷 D：滚动加载分页游标
  state.searchPageSize = 50;
  appendSearchPage();
}

/* 缺陷 D：分批 append（每次 50 条），滚到底部自动加载下一批。
   此前 slice(0,50) 把 578 条期货截到 50，用户永远看不到后面的合约。 */
/**
 * 追加渲染下一批搜索结果（每批 searchPageSize=50 条），滚到底部自动续加载。
 * 全部渲染完后移除滚动监听，避免触底空转。
 */
function appendSearchPage() {
  const box = $("resultlist");
  if (state.searchPage == null) state.searchPage = 0;
  const start = state.searchPage * state.searchPageSize;
  if (start >= state.instruments.length) {
    // 全部渲染完：去掉滚动监听，避免空加载
    if (state._searchScroll) $("resultlist").removeEventListener("scroll", state._searchScroll);
    return;
  }
  const slice = state.instruments.slice(start, start + state.searchPageSize);
  for (const it of slice) {
    const row = document.createElement("div");
    row.className = "watch-row";
    row.innerHTML = `<div class="l"><div class="nm">${it.name || it.instrument_id}</div>` +
      `<div class="cd num">${it.symbol}</div></div><div class="p"><div class="last num muted">›</div></div>`;
    row.addEventListener("click", () => switchSymbol(it.symbol));
    box.appendChild(row);
  }
  state.searchPage += 1;
  // 首次绑定滚动监听
  if (!state._searchScroll) {
    state._searchScroll = () => {
      if (state.screen !== "quotes") return;
      const el = $("resultlist");
      if (el.scrollTop + el.clientHeight >= el.scrollHeight - 60) appendSearchPage();
    };
    $("resultlist").addEventListener("scroll", state._searchScroll);
  }
}

/* ---------- K 线 ---------- */
/**
 * 初始化 K 线主图与 MACD 副图（ECharts 实例 + 基础配置，只执行一次）。
 * 缺陷 G：progressive 分块渲染是手机端防 tile 内存超限的关键配置。
 * 初始化失败仅记录日志（echarts 加载失败时页面其余功能仍可用）。
 */
function initCharts() {
  try {
  state.chart = echarts.init($("k-chart"));
  state.macdChart = echarts.init($("k-macd"));
  state.chart.setOption({
    animation: false, backgroundColor: "transparent",
    // 缺陷 G：progressive 降采样——超过阈值后 ECharts 分块渲染，避免一次性
    // 为全部 K 线分配 tile 内存（手机 WebView 长会话后主图只剩坐标轴的根因）。
    progressive: 200, progressiveThreshold: 500,
    grid: { left: 8, right: 56, top: 10, bottom: 10 },
    xAxis: { type: "category", data: [], axisLine: { lineStyle: { color: "#262e3a" } }, axisLabel: { show: false } },
    yAxis: { scale: true, position: "right", splitLine: { lineStyle: { color: "#161c24" } },
             axisLabel: { color: "#8b96a3", fontSize: 9 } },
    dataZoom: [{ type: "inside", xAxisIndex: 0 }],
    series: [
      { type: "candlestick", data: [], itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN },
        progressive: 200, progressiveThreshold: 500 },
      { type: "line", data: [], symbol: "none", lineStyle: { width: 1, color: AMBER } },
    ],
  });
  state.macdChart.setOption({
    animation: false, backgroundColor: "transparent",
    grid: { left: 8, right: 56, top: 6, bottom: 6 },
    xAxis: { type: "category", data: [], axisLabel: { show: false } },
    yAxis: { position: "right", splitLine: { show: false }, axisLabel: { color: "#8b96a3", fontSize: 9 } },
    series: [{ type: "bar", data: [] }],
  });
  } catch (e) { console.log('initCharts fail:', e); }
}

/**
 * 渲染 K 线主图（蜡烛 + MA20）与 MACD 副图。
 * 缺陷 G：增量 setOption（lazyReplace）——只更新 series/xAxis 数据，
 * 不重建 grid 等静态配置，避免长会话后手机 WebView tile 内存超限。
 */
function renderKline() {
  const k = state.kline;
  if (!k.length) return;
  const dates = k.map(b => fmtTime(b.datetime));
  const ohlc = k.map(b => [b.open, b.close, b.low, b.high]);
  const closes = k.map(b => b.close);
  const ma = (n) => closes.map((_, i) => i < n - 1 ? null :
    +(closes.slice(i - n + 1, i + 1).reduce((a, b) => a + b, 0) / n).toFixed(2));
  // 缺陷 G：增量 setOption——只更新 series.data 与 xAxis.data，
  // 不重建 grid/yAxis/legend 等静态配置，避免 ECharts 内部 tile 频繁分配/释放。
  // 手机 WebView tile 内存上限远低于桌面，全量 setOption 每次都重新初始化
  // 所有组件，长会话后主图只剩坐标轴（logcat: tile memory limits exceeded）。
  state.chart.setOption({
    xAxis: { data: dates },
    series: [{ data: ohlc }, { data: ma(20) }],
  }, { lazyReplace: true });
  // MACD 副图
  const dif = [], dea = [], hist = [];
  let f = closes[0], sl = closes[0];
  const fk = 2 / 13, sk = 2 / 27;
  for (const c of closes) {
    f = (c - f) * fk + f; sl = (c - sl) * sk + sl;
    dif.push(f - sl); dea.push(dif[dif.length - 1] * 0.8 + dea.slice(-1)[0] * 0.2 || dif[0]);
  }
  for (let i = 0; i < closes.length; i++) hist.push((dif[i] - dea[i]) * 2);
  state.macdChart.setOption({
    xAxis: { data: dates },
    series: [{ data: hist.map(v => ({ value: v, itemStyle: { color: v >= 0 ? UP : DOWN } })) }],
  }, { lazyReplace: true });
}
/**
 * K 线横轴时间标签：日线显示 "月-日"，其余周期显示 "时:分"。
 * @param {number} ms epoch 毫秒
 */
function fmtTime(ms) {
  const d = new Date(ms);
  return state.period >= 86400
    ? `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, "0")}`
    : `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/* ---------- 行情渲染 ---------- */
function colorBy(v, ref) { return ref == null ? "" : (v >= ref ? "up" : "down"); }
/**
 * 渲染当前合约的完整报价：报价头（最新/涨跌/开高低收/量/仓）、盘口买卖一、
 * 自选列表对应行、三张指数卡。数据来自 WS 推送（renderQuote 由 connectWS 触发）。
 */
function renderQuote() {
  const q = state.quote;
  if (!q) return;
  if (q.symbol) state.quotes[q.symbol] = q;   // 缓存，供自选列表切屏后回填
  $("k-last").textContent = fmt(q.last);
  $("k-last").className = "big num " + colorBy(q.last, q.pre_close);
  const chg = q.pre_close ? q.last - q.pre_close : null;
  const pct = q.pre_close ? (chg / q.pre_close * 100) : null;
  $("k-chg").textContent = chg == null ? "--" : `${chg >= 0 ? "+" : ""}${chg.toFixed(2)}  ${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%`;
  $("k-chg").className = "chg num " + colorBy(q.last, q.pre_close);
  $("k-open").textContent = fmt(q.open); $("k-high").textContent = fmt(q.high);
  $("k-low").textContent = fmt(q.low); $("k-preclose").textContent = fmt(q.pre_close);
  $("k-vol").textContent = fmt(q.volume, 0); $("k-oi").textContent = fmt(q.open_interest, 0);
  const b1 = q.bid && q.bid[0], a1 = q.ask && q.ask[0];
  $("ds-bid").textContent = b1 ? `${fmt(b1.price)} · ${fmt(b1.volume, 0)}` : "--";
  $("ds-ask").textContent = a1 ? `${fmt(a1.price)} · ${fmt(a1.volume, 0)}` : "--";
  $("ds-last").textContent = fmt(q.last);
  // 自选行
  const wl = $("w-" + q.symbol), wc = $("wc-" + q.symbol);
  if (wl) wl.textContent = fmt(q.last);
  if (wc) wc.textContent = q.pre_close ? `${q.last - q.pre_close >= 0 ? "+" : ""}${(q.last - q.pre_close).toFixed(2)}` : "--";
  // 指数卡（螺纹/黄金/豆粕）
  for (const [sym, vid, cid] of [["SHFE.rb2610", "idx-rb", "idx-rb-c"],
       ["SHFE.au2612", "idx-au", "idx-au-c"], ["DCE.m2609", "idx-m", "idx-m-c"]]) {
    if (q.symbol !== sym) continue;
    $(vid).textContent = fmt(q.last);
    $(cid).textContent = q.pre_close ? `${q.last - q.pre_close >= 0 ? "+" : ""}${(q.last - q.pre_close).toFixed(2)} (${pctOf(q.last, q.pre_close)}%)` : "--";
    $(cid).className = "c " + colorBy(q.last, q.pre_close);
  }
}
function pctOf(v, ref) { return ref ? ((v - ref) / ref * 100).toFixed(2) : "--"; }

/* ---------- 决策 ---------- */
/**
 * 渲染决策页：方向/多空分/评分条/入场止损目标/手数/风险额/评估依据列表。
 * @param {Object|null} d 评估结果；null 或 pending=true 时显示占位 "--"
 */
function renderDecision(d) {
  if (!d || d.pending) {
    for (const id of ["dd-dir", "dd-score", "dd-entry", "dd-stop", "dd-t1", "dd-t2", "dd-lots", "dd-risk", "dd-whycount"])
      $(id).textContent = "--";
    $("dd-why").replaceChildren();
    $("dd-bar-l").style.width = "50%";
    $("dc-quick").classList.add("hidden");
    return;
  }
  const cls = d.direction === "做多" ? "up" : d.direction === "做空" ? "down" : "amber";
  $("dd-dir").textContent = d.direction;
  $("dd-dir").className = "d " + cls;
  $("dd-score").textContent = `多 ${d.score_long} · 空 ${d.score_short} · 总分 ${d.score}`;
  const total = Math.max(1, d.score_long + d.score_short);
  $("dd-bar-l").style.width = (d.score_long / total * 100) + "%";
  for (const [id, key] of [["dd-entry", "entry"], ["dd-stop", "stop"], ["dd-t1", "target1"],
       ["dd-t2", "target2"], ["dd-lots", "contracts"]])
    $(id).textContent = d[key] == null ? "待信号" : fmt(d[key]);
  $("dd-risk").textContent = d.risk_amount == null ? "待信号" : `¥${fmt(d.risk_amount, 0)} (${d.risk_percent}%)`;
  $("dd-whycount").textContent = `评估依据 · ${(d.rationale || []).length} 条`;
  $("dd-why").innerHTML = (d.rationale || []).map(r => `<li>${DecisionControls.escape(r)}</li>`).join("") || "<li>无</li>";
}
/**
 * 渲染 K线页底部的"决策速览"抽屉（方向/评分/关键价位精简版）。
 * @param {Object} d 评估结果（非 pending）
 */
function renderQuickPanel(d) {
  let note = $("dc-profile-note");
  if (!note) {
    note = document.createElement("div");
    note.id = "dc-profile-note";
    note.className = "dp-warning";
    $("dc-quick").append(note);
  }
  note.textContent = `${d.holding || ""}。${(d.warnings || []).join(" ")}${!d.data_ok ? " 历史数据不足，暂不评估。" : ""}`;
  const cls = d.direction === "做多" ? "up" : d.direction === "做空" ? "down" : "amber";
  $("dc-dir").textContent = d.direction;
  $("dc-dir").className = "d " + cls;
  $("dc-score").textContent = `${d.mode_label || ""} · 多 ${d.score_long} · 空 ${d.score_short}${d.quote_fresh === false ? " · 历史行情" : ""}`;
  const total = Math.max(1, d.score_long + d.score_short);
  document.querySelector(".dcq-bar .l").style.width = (d.score_long / total * 100) + "%";
  for (const [id, key] of [["dc-entry", "entry"], ["dc-stop", "stop"], ["dc-t1", "target1"], ["dc-t2", "target2"]])
    $(id).textContent = d[key] == null ? "待信号" : fmt(d[key]);
  $("dc-quick").classList.remove("hidden");
}

/* ---------- 加载 ---------- */
/**
 * 拉取并渲染当前合约的 K 线（20 秒定时轮询 + 切合约/切周期时手动触发）。
 * klineReq 序号防乱序：慢请求返回时若已有更新的请求，直接丢弃本次结果。
 */
async function loadKline() {
  const reqId = ++state.klineReq;
  try {
    const d = await api(`/api/v1/kline/${encodeURIComponent(state.symbol)}?period=${state.period}&count=150`, 45000);
    if (reqId !== state.klineReq) return;
    state.kline = d.bars || [];
    renderKline();
  } catch (e) { console.log("kline:", e.message); }
}
/**
 * 拉取并渲染决策评估（6 秒定时轮询；后端数据不足时返回 pending 占位）。
 * 同样带 reqId 序号防乱序。
 */
async function loadDecision() {
  const reqId = ++state.decisionReq, symbol = state.symbol;
  try {
    const d = await DecisionControls.request(symbol);
    if (reqId !== state.decisionReq || symbol !== state.symbol) return;
    state.decision = d;
    DecisionControls.renderMeta(d);
    renderDecision(d.pending ? null : d);
    if (!d.pending) renderQuickPanel(d);
  } catch (e) {
    if (reqId !== state.decisionReq || symbol !== state.symbol) return;
    state.decision = null;
    renderDecision(null);
    DecisionControls.renderMeta(null, `评估不可用：${e.message}`);
  }
}
window.addEventListener("decisionprofilechange", () => {
  state.decision = null;
  renderDecision(null);
  loadDecision();
});
/**
 * 拉取合约目录列表（搜索/全量浏览）。
 * 目录未就绪（catalogReady=false）时不请求、不报错——只展示"下载中"提示，
 * loadStatus 轮询发现就绪后会自动补拉（配合后端内置表 + 后台下载链路）。
 * @param {string} keyword 搜索关键字（空 = 全量分批浏览）
 */
async function loadInstruments(keyword = "") {
  // 目录未就绪：不拉取、不报错，只显示下载中提示，等 loadStatus 发现就绪后再补拉。
  if (!state.catalogReady) {
    if (state.screen === "quotes")
      $("resultlist").innerHTML = `<div class="watch-row"><div class="l"><div class="cd">合约目录下载中，下载完成后自动显示搜索结果…</div></div></div>`;
    return;
  }
  if (state.instrumentsLoading) return;  // 防止重复/并发拉取导致报错叠加
  state.instrumentsLoading = true;
  try {
    const d = await api(`/api/v1/instruments${keyword ? "?keyword=" + encodeURIComponent(keyword) : ""}`);
    state.instruments = d.items || [];
    renderSearch();
  } catch (e) {
    // 只显示一次，不重复堆积；不自动反复重试（避免 503 + abort 两个报错叠一起）
    if (state.screen === "quotes")
      $("resultlist").innerHTML = `<div class="watch-row"><div class="l"><div class="cd">加载合约列表失败（${e.message}），可切换页面后重试</div></div></div>`;
    console.log("instruments:", e.message);
  } finally {
    state.instrumentsLoading = false;
  }
}
/**
 * 轮询后端状态（15 秒一次）：更新连接标识/账号/路线/目录下载进度提示，
 * 目录就绪瞬间自动补拉一次搜索列表。失败静默（下一轮再试）。
 */
async function loadStatus() {
  try {
    const st = await api("/api/v1/status");
    $("sb-state").textContent = st.connected ? "● 已连接" : "● 连接中";
    $("sb-state").style.color = st.connected ? DOWN : AMBER;
    $("me-account").textContent = "账户 " + (st.account || "--");
    $("me-route").textContent = st.route || "--";
    $("q-route").textContent = st.route || "";
    state.catalogReady = !!st.catalog_ready;
    // 合约目录加载中提示（用户能感知"导入未完成"）
    const tip = $("catalog-tip");
    if (tip) {
      if (st.catalog_loading) {
        const prog = st.catalog_progress ? ` ${st.catalog_progress}` : "";
        tip.classList.remove("hidden");
        tip.textContent = `⏳ 正在下载合约目录${prog}，行情/自选已可用，完成后即可搜索全部合约…`;
      } else if (st.catalog_ready) {
        tip.classList.add("hidden");
      }
    }
    // 目录一就绪，立刻补拉一次搜索列表
    if (state.catalogReady && !state.instrumentsLoading) loadInstruments($("search").value.trim());
  } catch (e) { /* 忽略 */ }
}

/* ---------- WebSocket ---------- */
/**
 * 建立行情 WebSocket：订阅全部自选；收到 quote/quote_snapshot 时更新主报价区
 * 与自选行，并做端到端延迟统计（每 100 笔打一条 console 日志）；
 * 订阅失败（后端未就绪窗口）触发退避重试；断线 3 秒后自动重连
 * （页面不可见时不重连，切回前台由 visibilitychange 统一处理）。
 */
function connectWS() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/market`);
  state.ws = ws;
  ws.onopen = () => {
    // 订阅全部自选合约（不只是当前查看的），这样自选列表每行都能实时更新价格
    sendSubscribe();
  };
  ws.onmessage = (event) => {
    let msg; try { msg = JSON.parse(event.data); } catch { return; }
    if (msg.type === "quote" || msg.type === "quote_snapshot") {
      // 端到端延迟埋点（P0-200ms）：服务端发送时刻 msg.ts → WebView 收到
      if (msg.ts) {
        const latMs = Math.max(0, Date.now() / 1000 - msg.ts) * 1000;
        const st = (state.latency = state.latency || { count: 0, sum: 0, max: 0 });
        st.count += 1; st.sum += latMs; st.max = Math.max(st.max, latMs);
        if (st.count % 100 === 0) {
          console.debug(`[延迟统计] n=${st.count} avg=${(st.sum / st.count).toFixed(0)}ms max=${st.max.toFixed(0)}ms`);
        }
      }
      // 当前查看的合约更新主报价区；自选列表里所有合约都更新对应行
      if (msg.symbol === state.symbol) { state.quote = msg.data; renderQuote(); }
      updateWatchPrice(msg.symbol, msg.data);
    } else if (msg.type === "subscribed" && msg.failed && msg.failed.length) {
      // 缺陷 A 前端：订阅失败（后端尚未连接就绪）——退避重试，不丢一次即放弃。
      // 后端也会在 on_connected 时重放 pending，这里兜底防"hello 之后还没就绪"的窗口。
      retrySubscribe();
    }
  };
  ws.onclose = () => setTimeout(() => { if (document.visibilityState !== "hidden") connectWS(); }, 3000);
}

/* 发送一次订阅全部自选（onopen / 重试 / 切换合约共用） */
function sendSubscribe() {
  if (!state.ws || state.ws.readyState !== 1) return;
  const all = [...new Set([state.symbol, ...state.watchlist])];
  if (all.length) state.ws.send(JSON.stringify({ action: "subscribe", symbols: all }));
}

/* 退避重试订阅：首次 1s，之后 2s/4s，封顶 5s，最多 6 次（~20s 窗口） */
function retrySubscribe() {
  clearTimeout(state._retryTimer);
  state._retryCount = (state._retryCount || 0) + 1;
  if (state._retryCount > 6) { state._retryCount = 0; return; }
  const delay = Math.min(5000, 1000 * Math.pow(2, state._retryCount - 1));
  state._retryTimer = setTimeout(() => {
    state._retryCount = 0;
    sendSubscribe();
  }, delay);
}

/* 更新自选列表里某一行的价格（即使不是当前查看的合约） */
function updateWatchPrice(symbol, quote) {
  if (quote && quote.last != null) state.quotes[symbol] = quote;   // 缓存
  const lastEl = $("w-" + symbol), chgEl = $("wc-" + symbol);
  if (lastEl) lastEl.textContent = fmt(quote.last);
  if (chgEl) {
    if (quote.pre_close) {
      const c = quote.last - quote.pre_close;
      chgEl.textContent = (c >= 0 ? "+" : "") + c.toFixed(2);
      chgEl.className = "chg num " + colorBy(quote.last, quote.pre_close);
    } else chgEl.textContent = "--";
  }
}

/* 重建自选列表后，从内存缓存立刻回填每行价格（避免切屏后短暂显示 --） */
function renderWatchlist() {
  const box = $("watchlist");
  box.innerHTML = "";
  $("q-count").textContent = state.watchlist.length;
  for (const code of state.watchlist) {
    const row = document.createElement("div");
    row.className = "watch-row";
    row.innerHTML = `<div class="l"><div class="nm">${code.split(".")[1]}</div>` +
      `<div class="cd num">${code}</div></div><div class="p"><div class="last num" id="w-${code}">--</div>` +
      `<div class="chg num" id="wc-${code}">--</div></div>` +
      `<div class="del" data-sym="${code}" title="删除">✕</div>`;
    row.addEventListener("click", () => switchSymbol(code));
    box.appendChild(row);
    // 从缓存回填价格（如有）
    const cached = state.quotes[code];
    if (cached) updateWatchPrice(code, cached);
  }
  // 删除按钮（绑定到整行的删除图标）
  box.querySelectorAll(".del").forEach(el =>
    el.addEventListener("click", (e) => { e.stopPropagation(); removeWatch(el.dataset.sym); }));
}

/* ---------- 合约切换 ---------- */
/**
 * 切换当前查看的合约（点自选行/搜索结果行触发）。
 * 首次查看会自动加入自选并持久化；用缓存价格先回填报价头避免闪 "--"；
 * 切屏到 K线并重新拉取 K 线与决策；重新订阅（当前 + 全部自选）。
 */
function switchSymbol(symbol) {
  if (symbol === state.symbol) { showScreen("kline"); return; }
  state.symbol = symbol;
  state.decision = null; state.kline = [];
  DecisionControls.renderMeta(null);
  if (!state.watchlist.includes(symbol)) {
    state.watchlist.push(symbol);
    localStorage.setItem("watchlist", JSON.stringify(state.watchlist));
  }
  // 有缓存价格就先回填，避免切换瞬间报价头闪 --（后续实时推送会覆盖）
  state.quote = state.quotes[symbol] || null;
  if (state.quote) renderQuote();
  $("k-name").textContent = symbol.split(".")[1];
  $("k-code").textContent = symbol;
  $("d-code").textContent = symbol;
  $("d-name").textContent = symbol;
  showScreen("kline");
  // 切换合约时重新订阅当前+全部自选，保证价格持续更新
  sendSubscribe();
  loadKline(); loadDecision();
}

/** 切换当前合约的自选收藏状态（K线页 ★/☆ 按钮），同步持久化与列表。 */
function toggleWatch() {
  const i = state.watchlist.indexOf(state.symbol);
  if (i >= 0) state.watchlist.splice(i, 1); else state.watchlist.push(state.symbol);
  localStorage.setItem("watchlist", JSON.stringify(state.watchlist));
  $("k-star").textContent = state.watchlist.includes(state.symbol) ? "★" : "☆";
  renderWatchlist();
}

/* ---------- 登录 ---------- */
/**
 * 启动时检查登录态：未配置凭据或后端未就绪都弹出登录层（盖在主界面上）。
 * @returns {Promise<boolean>} 已配置凭据且后端可达返回 true
 */
async function checkAuth() {
  try {
    const auth = await api("/api/v1/auth", 8000);
    if (!auth.configured) {
      $("screen-login").classList.remove("hidden");
      $("login-error").textContent = "";
      return false;
    }
    $("me-account").textContent = "账户 " + auth.account;
    $("screen-login").classList.add("hidden");
    return true;
  } catch (e) {
    // 后端未就绪时也显示登录层，避免用户"卡在没反应"的主界面
    $("screen-login").classList.remove("hidden");
    $("login-error").textContent = "后端连接中，" + e.message;
    return false;
  }
}
/**
 * 登录提交：POST /api/v1/auth 保存凭据并触发后端用新凭据重连；
 * 成功后隐藏登录层、重载全部数据流。失败在登录层内联展示原因。
 */
async function saveLogin() {
  const account = $("login-account").value.trim(), password = $("login-password").value;
  if (!account || !password) { $("login-error").textContent = "账号与密码不能为空"; return; }
  $("login-save").disabled = true;
  $("login-error").textContent = "正在登录…";
  try {
    const r = await fetch("/api/v1/auth", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account, password }) });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
    $("login-error").textContent = "";
    $("screen-login").classList.add("hidden");
    $("me-account").textContent = "账户 " + (body.account || "--");
    $("login-account").value = ""; $("login-password").value = "";
    // 登录成功：重新加载行情，让连接用新凭据建立
    loadStatus(); loadKline(); loadDecision(); loadInstruments(); connectWS();
    setTimeout(loadStatus, 2000);   // 留出重连建立连接的时间再刷新一次状态
  } catch (e) {
    $("login-error").textContent = e.message;
  } finally {
    $("login-save").disabled = false;
  }
}
/** 退出登录：DELETE /api/v1/auth 清除凭据，弹回登录层（后端同时断开连接）。 */
async function logout() {
  try {
    await fetch("/api/v1/auth", { method: "DELETE" });
  } catch (e) { /* 忽略 */ }
  $("screen-login").classList.remove("hidden");
  $("login-error").textContent = "已退出登录";
  loadStatus();
}

/* ---------- K线默认周期（"我的"页设置，localStorage 持久化） ---------- */
/** 周期秒数 → 中文标签（"5 分钟"/"日线"…）；未知名回退 "5 分钟"。 */
function periodLabel(p) {
  return (KLINE_PERIODS.find(x => x.p === p) || KLINE_PERIODS[1]).label;
}
/** 同步两处周期 UI：设置行显示当前值 + K线页周期按钮高亮归位。 */
function syncPeriodUi() {
  $("me-period").textContent = periodLabel(state.period) + " ›";
  document.querySelectorAll("#ptabs button").forEach(x =>
    x.classList.toggle("active", Number(x.dataset.p) === state.period));
}
/**
 * 设置 K 线默认周期（唯一的周期变更入口，K线页按钮与设置行共用）：
 * 校验合法 → 写 localStorage 持久化 → 同步 UI → K线页可见时重载图表。
 * @param {number} p 周期秒数（必须在 KLINE_PERIODS 白名单内）
 */
function setPeriod(p) {
  if (!KLINE_PERIODS.some(x => x.p === p)) return;
  state.period = p;
  localStorage.setItem("klinePeriod", String(p));
  syncPeriodUi();
  if (state.screen === "kline") loadKline();
}
/** 关闭"使用说明与免责声明"浮层（我知道了按钮 / 点遮罩共用）。 */
function closeHelpSheet() {
  $("sheet-mask").classList.add("hidden");
  $("sheet-help").classList.add("hidden");
}

/* ---------- 事件绑定 ---------- */
document.querySelectorAll("#tabbar .tab").forEach(t =>
  t.addEventListener("click", () => showScreen(t.dataset.s)));
$("ptabs").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  setPeriod(Number(b.dataset.p));
});
$("btn-dc").addEventListener("click", () => {
  $("dc-quick").classList.toggle("hidden");
  $("dc-caret").textContent = $("dc-quick").classList.contains("hidden") ? "›" : "⌄";
});
$("search").addEventListener("input", () => {
  const kw = $("search").value.trim();
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(() => loadInstruments(kw), 300);
});
$("login-save").addEventListener("click", saveLogin);
$("login-password").addEventListener("keydown", (e) => { if (e.key === "Enter") saveLogin(); });
$("logout").addEventListener("click", logout);
/* "我的"页：K线默认周期（点按循环切换并持久化）与使用说明浮层 */
$("me-period-row").addEventListener("click", () => {
  const idx = KLINE_PERIODS.findIndex(x => x.p === state.period);
  setPeriod(KLINE_PERIODS[(idx + 1) % KLINE_PERIODS.length].p);
});
$("me-help").addEventListener("click", () => {
  $("sheet-mask").classList.remove("hidden");
  $("sheet-help").classList.remove("hidden");
});
$("sheet-close").addEventListener("click", closeHelpSheet);
$("sheet-mask").addEventListener("click", closeHelpSheet);
document.querySelector("#screen-login .hint .cyan").addEventListener("click",
  () => window.open("https://www.tqsdk.com", "_blank"));
window.addEventListener("resize", () => { state.chart && state.chart.resize(); state.macdChart && state.macdChart.resize(); });

/* ---------- 启动 ---------- */
(/**
 * 启动序列：初始化图表与自选列表 → 同步周期 UI → 显示主界面骨架 →
 * 检查登录态（未配置则弹登录层覆盖）→ 拉起全部数据流（状态/K线/决策/目录/WS）
 * → 挂载三组定时轮询与前台切回监听。
 */
async function init() {
  console.log('APP_INIT start, bodyBg=' + getComputedStyle(document.body).backgroundColor +
    ' appMainVisible=' + (document.getElementById('app-main') ? 'yes' : 'no'));
  initCharts();
  renderWatchlist();
  syncPeriodUi();
  showScreen("quotes");
  // 未配置凭据时 checkAuth 会弹出登录层并盖在 app-main 之上；
  // 无论是否配置都显示主界面骨架，让状态条/路由信息可见（登录层保留在未配置时覆盖）。
  await checkAuth();
  document.getElementById("app-main").classList.remove("hidden");
  loadStatus(); loadInstruments(); loadKline(); loadDecision(); connectWS();
  setInterval(loadStatus, 15000);
  setInterval(loadKline, 20000);
  setInterval(loadDecision, 6000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") { connectWS(); loadStatus(); loadKline(); }
  });
})();
