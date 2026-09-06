/* ZA量化 手机版前端 · 连接本机后端（127.0.0.1:8000，同源） */
"use strict";

/* ---------- 状态 ---------- */
const state = {
  symbol: "SHFE.rb2610",
  period: 300,
  kline: [],
  quote: null,
  decision: null,
  instruments: [],
  watchlist: JSON.parse(localStorage.getItem("watchlist") || '["SHFE.rb2610","SHFE.au2612","DCE.m2609"]'),
  screen: "quotes",
  klineReq: 0,
  ws: null,
  chart: null,
  macdChart: null,
};
const UP = "#ef5350", DOWN = "#26a69a", AMBER = "#e0a93c", MUTED = "#8b96a3";
const $ = (id) => document.getElementById(id);
const fmt = (v, d = 2) => v == null ? "--" : Number(v).toLocaleString("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d });
const now = () => new Date().toLocaleTimeString("zh-CN", { hour12: false });

/* ---------- 接口 ---------- */
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
function renderWatchlist() {
  const box = $("watchlist");
  box.innerHTML = "";
  $("q-count").textContent = state.watchlist.length;
  for (const code of state.watchlist) {
    const row = document.createElement("div");
    row.className = "watch-row";
    row.innerHTML = `<div class="l"><div class="nm">${code.split(".")[1]}</div>` +
      `<div class="cd num">${code}</div></div><div class="p"><div class="last num" id="w-${code}">--</div>` +
      `<div class="chg num" id="wc-${code}">--</div></div>`;
    row.addEventListener("click", () => switchSymbol(code));
    box.appendChild(row);
  }
}
function renderSearch() {
  const box = $("resultlist");
  box.innerHTML = "";
  for (const it of state.instruments.slice(0, 50)) {
    const row = document.createElement("div");
    row.className = "watch-row";
    row.innerHTML = `<div class="l"><div class="nm">${it.name || it.instrument_id}</div>` +
      `<div class="cd num">${it.symbol}</div></div><div class="p"><div class="last num muted">›</div></div>`;
    row.addEventListener("click", () => switchSymbol(it.symbol));
    box.appendChild(row);
  }
}

/* ---------- K 线 ---------- */
function initCharts() {
  try {
  state.chart = echarts.init($("k-chart"));
  state.macdChart = echarts.init($("k-macd"));
  state.chart.setOption({
    animation: false, backgroundColor: "transparent",
    grid: { left: 8, right: 56, top: 10, bottom: 10 },
    xAxis: { type: "category", data: [], axisLine: { lineStyle: { color: "#262e3a" } }, axisLabel: { show: false } },
    yAxis: { scale: true, position: "right", splitLine: { lineStyle: { color: "#161c24" } },
             axisLabel: { color: "#8b96a3", fontSize: 9 } },
    dataZoom: [{ type: "inside", xAxisIndex: 0 }],
    series: [
      { type: "candlestick", data: [], itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN } },
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

function renderKline() {
  const k = state.kline;
  if (!k.length) return;
  const dates = k.map(b => fmtTime(b.datetime));
  const ohlc = k.map(b => [b.open, b.close, b.low, b.high]);
  const closes = k.map(b => b.close);
  const ma = (n) => closes.map((_, i) => i < n - 1 ? null :
    +(closes.slice(i - n + 1, i + 1).reduce((a, b) => a + b, 0) / n).toFixed(2));
  state.chart.setOption({
    xAxis: { data: dates },
    series: [{ data: ohlc }, { data: ma(20) }],
  });
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
  });
}
function fmtTime(ms) {
  const d = new Date(ms);
  return state.period >= 86400
    ? `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, "0")}`
    : `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/* ---------- 行情渲染 ---------- */
function colorBy(v, ref) { return ref == null ? "" : (v >= ref ? "up" : "down"); }
function renderQuote() {
  const q = state.quote;
  if (!q) return;
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
function renderDecision(d) {
  if (!d || d.pending) { $("dd-dir").textContent = "--"; return; }
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
  $("dd-why").innerHTML = (d.rationale || []).map(r => `<li>${r}</li>`).join("") || "<li>无</li>";
}
function renderQuickPanel(d) {
  const cls = d.direction === "做多" ? "up" : d.direction === "做空" ? "down" : "amber";
  $("dc-dir").textContent = d.direction;
  $("dc-dir").className = "d " + cls;
  $("dc-score").textContent = `多 ${d.score_long} · 空 ${d.score_short} · 总分 ${d.score}`;
  const total = Math.max(1, d.score_long + d.score_short);
  document.querySelector(".dcq-bar .l").style.width = (d.score_long / total * 100) + "%";
  for (const [id, key] of [["dc-entry", "entry"], ["dc-stop", "stop"], ["dc-t1", "target1"], ["dc-t2", "target2"]])
    $(id).textContent = d[key] == null ? "待信号" : fmt(d[key]);
  $("dc-quick").classList.remove("hidden");
}

/* ---------- 加载 ---------- */
async function loadKline() {
  const reqId = ++state.klineReq;
  try {
    const d = await api(`/api/v1/kline/${encodeURIComponent(state.symbol)}?period=${state.period}&count=150`, 45000);
    if (reqId !== state.klineReq) return;
    state.kline = d.bars || [];
    renderKline();
  } catch (e) { console.log("kline:", e.message); }
}
async function loadDecision() {
  const reqId = ++state.decisionReq || (state.decisionReq = 1);
  try {
    const d = await api(`/api/v1/decision/${encodeURIComponent(state.symbol)}`, 45000);
    if (reqId !== state.decisionReq) return;
    state.decision = d;
    renderDecision(d.pending ? null : d);
    if (!d.pending) renderQuickPanel(d);
  } catch (e) {
    if (reqId !== state.decisionReq) return;
    $("dd-dir").textContent = "--";
    console.log("decision:", e.message);
  }
}
async function loadInstruments(keyword = "") {
  try {
    const d = await api(`/api/v1/instruments${keyword ? "?keyword=" + encodeURIComponent(keyword) : ""}`);
    state.instruments = d.items || [];
    renderSearch();
  } catch (e) {
    $("resultlist").innerHTML = `<div class="watch-row"><div class="l"><div class="cd">加载中（${e.message}），5秒后重试</div></div></div>`;
    setTimeout(() => { if (state.screen === "quotes") loadInstruments(keyword); }, 5000);
  }
}
async function loadStatus() {
  try {
    const st = await api("/api/v1/status");
    $("sb-state").textContent = st.connected ? "● 已连接" : "● 连接中";
    $("sb-state").style.color = st.connected ? DOWN : AMBER;
    $("me-account").textContent = "账户 " + (st.account || "--");
    $("me-route").textContent = st.route || "--";
    $("q-route").textContent = st.route || "";
  } catch (e) { /* 忽略 */ }
}

/* ---------- WebSocket ---------- */
function connectWS() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/market`);
  state.ws = ws;
  ws.onopen = () => ws.send(JSON.stringify({ action: "subscribe", symbols: [state.symbol] }));
  ws.onmessage = (event) => {
    let msg; try { msg = JSON.parse(event.data); } catch { return; }
    if ((msg.type === "quote" || msg.type === "quote_snapshot") && msg.symbol === state.symbol) {
      state.quote = msg.data; renderQuote();
    }
  };
  ws.onclose = () => setTimeout(() => { if (document.visibilityState !== "hidden") connectWS(); }, 3000);
}

/* ---------- 合约切换 ---------- */
function switchSymbol(symbol) {
  if (symbol === state.symbol) { showScreen("kline"); return; }
  state.symbol = symbol;
  state.quote = null; state.decision = null; state.kline = [];
  if (!state.watchlist.includes(symbol)) {
    state.watchlist.push(symbol);
    localStorage.setItem("watchlist", JSON.stringify(state.watchlist));
  }
  $("k-name").textContent = symbol.split(".")[1];
  $("k-code").textContent = symbol;
  $("d-code").textContent = symbol;
  $("d-name").textContent = symbol;
  showScreen("kline");
  if (state.ws && state.ws.readyState === 1)
    state.ws.send(JSON.stringify({ action: "subscribe", symbols: [symbol] }));
  loadKline(); loadDecision();
}

function toggleWatch() {
  const i = state.watchlist.indexOf(state.symbol);
  if (i >= 0) state.watchlist.splice(i, 1); else state.watchlist.push(state.symbol);
  localStorage.setItem("watchlist", JSON.stringify(state.watchlist));
  $("k-star").textContent = state.watchlist.includes(state.symbol) ? "★" : "☆";
  renderWatchlist();
}

/* ---------- 登录 ---------- */
async function checkAuth() {
  try {
    const auth = await api("/api/v1/auth", 8000);
    if (!auth.configured) { $("screen-login").classList.remove("hidden"); return false; }
    $("me-account").textContent = "账户 " + auth.account;
    return true;
  } catch (e) { return false; }
}
async function saveLogin() {
  const account = $("login-account").value.trim(), password = $("login-password").value;
  if (!account || !password) { $("login-error").textContent = "账号与密码不能为空"; return; }
  try {
    const r = await fetch("/api/v1/auth", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account, password }) });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
    $("screen-login").classList.add("hidden");
    loadStatus(); loadKline(); loadDecision();
  } catch (e) { $("login-error").textContent = e.message; }
}

/* ---------- 事件绑定 ---------- */
document.querySelectorAll("#tabbar .tab").forEach(t =>
  t.addEventListener("click", () => showScreen(t.dataset.s)));
$("ptabs").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  document.querySelectorAll("#ptabs button").forEach(x => x.classList.remove("active"));
  b.classList.add("active");
  state.period = Number(b.dataset.p);
  loadKline();
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
window.addEventListener("resize", () => { state.chart && state.chart.resize(); state.macdChart && state.macdChart.resize(); });

/* ---------- 启动 ---------- */
(async function init() {
  console.log('APP_INIT start, bodyBg=' + getComputedStyle(document.body).backgroundColor +
    ' appMainVisible=' + (document.getElementById('app-main') ? 'yes' : 'no'));
  initCharts();
  renderWatchlist();
  showScreen("quotes");
  const configured = await checkAuth();
  // 始终显示主界面（手机端天勤凭据本地保存，无需强制登录弹窗）
  document.getElementById("app-main").classList.remove("hidden");
  loadStatus(); loadInstruments(); loadKline(); loadDecision(); connectWS();
  setInterval(loadStatus, 15000);
  setInterval(loadKline, 20000);
  setInterval(loadDecision, 6000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") { connectWS(); loadStatus(); loadKline(); }
  });
})();
