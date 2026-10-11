/* Actual frontend scripts with in-memory DOM, fetch and timers. No backend or account. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "../..");
const A = "SHFE.rb2610", B = "SHFE.au2612";
const risk = {account_equity: 50000, max_loss_per_trade: 900, risk_percent: 1.8, max_contracts: 10};
const catalog = {
  default_mode: "short", risk_defaults: risk,
  risk_limits: {account_equity: [.01, 1e12], max_loss_per_trade: [.01, 1e12], risk_percent: [.0001, 100], max_contracts: [1, 1000000]},
  profiles: ["ultra", "short", "medium", "long"].map(id => ({id, label: id, holding: id, periods: []})),
};
const decision = (symbol, entry) => ({symbol, mode: "short", direction: "观望", entry,
  score_long: 10, score_short: 5, score: 10, stop: entry - 1, target1: entry + 1, target2: entry + 2,
  contracts: 1, risk_amount: 1, risk_percent: 1.8, rationale: [], warnings: [], data_ok: true});
const bars = marker => Array.from({length: 150}, (_, i) => ({datetime: marker + i * 300000,
  open: 1, close: 2, low: 1, high: 2, volume: 1, open_interest: 1}));
const flush = () => new Promise(resolve => setImmediate(resolve));

function harness(mobile, deferCatalog = false) {
  const staticRoot = path.join(root, mobile ? "mobile-app/app/src/main/python/static" : "static");
  const elements = new Map(), requests = [], intervals = [], timeouts = new Map(), charts = [], sockets = [], logs = [];
  const windowEvents = new Map(), documentEvents = new Map();
  const listen = (events, name, fn) => {if (!events.has(name)) events.set(name, new Set()); events.get(name).add(fn);};
  const unlisten = (events, name, fn) => events.get(name)?.delete(fn);
  const dispatch = (events, event) => {for (const fn of events.get(event.type) || []) fn(event);};
  let nextTimer = 0;
  function element(id) {
    if (elements.has(id)) return elements.get(id);
    const classes = new Set(), listeners = new Map();
    const e = {id, textContent: "", style: {}, className: "", value: "", dataset: {}, children: [], _html: "",
      get innerHTML() { return this._html; },
      set innerHTML(value) { this._html = value; this.children = []; },
      addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: name => listeners.delete(name),
      appendChild(child) { this.children.push(child); }, append(child) { this.children.push(child); },
      replaceChildren() { this.children = []; this._html = ""; },
      querySelector: selector => element(`${id}:${selector}`), querySelectorAll: () => [],
      classList: {add: (...names) => names.forEach(n => classes.add(n)), remove: (...names) => names.forEach(n => classes.delete(n)),
        contains: n => classes.has(n), toggle: (n, force) => (force ?? !classes.has(n)) ? classes.add(n) : classes.delete(n)},
      listeners,
    };
    elements.set(id, e);
    return e;
  }
  function chart() {
    const c = {option: {}, patches: [], events: new Map(),
      setOption(patch) {
        this.patches.push(patch);
        for (const [key, value] of Object.entries(patch)) {
          if (Array.isArray(value)) this.option[key] = value.map((item, i) => ({...(this.option[key]?.[i] || {}), ...item}));
          else this.option[key] = value;
        }
      },
      getOption() { return this.option; }, on(name, fn) { this.events.set(name, fn); }, resize() {},
      dispatchAction(action) {
        this.option.dataZoom = (this.option.dataZoom || [{}]).map(z => ({...z, start: action.start, end: action.end}));
      },
    };
    charts.push(c);
    return c;
  }
  const response = (body, status = 200) => ({ok: status < 400, status, json: async () => body});
  function fetchFake(url, options = {}) {
    if (url === "/api/v1/decision-profiles" && !deferCatalog) return Promise.resolve(response(catalog));
    if (url.startsWith("/api/v1/kline/") || url.startsWith("/api/v1/decision/") || url === "/api/v1/decision-profiles") {
      return new Promise((resolve, reject) => requests.push({url, signal: options.signal,
        resolve: (body, status) => resolve(response(body, status)), reject}));
    }
    return Promise.resolve(response({configured: true, connected: true, account: "fixture", catalog_ready: true, items: [], total: 0}));
  }
  const context = vm.createContext({AbortController, URLSearchParams, Event, Date,
    console: {log: (...args) => logs.push(args.join(" ")), debug() {}},
    localStorage: {getItem: key => key === "watchlist" ? JSON.stringify([A, B]) : null, setItem() {}},
    document: {visibilityState: "visible", body: element("body"), getElementById: element,
      querySelector: element, querySelectorAll: () => [], createElement: () => element(`created-${elements.size}`),
      addEventListener: (name, fn) => listen(documentEvents, name, fn),
      removeEventListener: (name, fn) => unlisten(documentEvents, name, fn)},
    getComputedStyle: () => ({backgroundColor: "fixture"}),
    location: {protocol: "http:", host: "fixture.invalid"}, echarts: {init: chart}, fetch: fetchFake,
    setTimeout: (fn, delay) => {const id = ++nextTimer; timeouts.set(id, {fn, delay}); return id;},
    clearTimeout: id => timeouts.delete(id), setInterval: (fn, delay) => {intervals.push({fn, delay}); return intervals.length;},
    addEventListener: (name, fn) => listen(windowEvents, name, fn), removeEventListener: (name, fn) => unlisten(windowEvents, name, fn),
    dispatchEvent: e => dispatch(windowEvents, e),
    WebSocket: class {
      constructor(url) {this.url = url; this.readyState = 0; this.sent = []; sockets.push(this); queueMicrotask(() => this.open());}
      open() {if (this.readyState !== 0) return; this.readyState = 1; this.onopen?.();}
      send(data) {assert.equal(this.readyState, 1); this.sent.push(JSON.parse(data));}
      close() {if (this.readyState >= 2) return; this.readyState = 2; queueMicrotask(() => this.disconnect());}
      disconnect() {this.readyState = 3; this.onclose?.();}
      emit(message) {this.onmessage?.({data: JSON.stringify(message)});}
    },
  });
  context.window = context;
  vm.runInContext(fs.readFileSync(path.join(staticRoot, "decision-controls.js"), "utf8"), context);
  vm.runInContext(fs.readFileSync(path.join(staticRoot, "market-connection.js"), "utf8"), context);
  vm.runInContext(fs.readFileSync(path.join(staticRoot, "app.js"), "utf8"), context);
  return {context, requests, intervals, timeouts, charts, sockets, logs, element,
    run: code => vm.runInContext(code, context),
    latest: kind => requests.filter(r => r.url.startsWith(`/api/v1/${kind}/`)).at(-1),
    count: kind => requests.filter(r => r.url.startsWith(`/api/v1/${kind}/`)).length,
    visibility(value) {context.document.visibilityState = value; dispatch(documentEvents, new Event("visibilitychange"));},
    period(period) {
      if (mobile) this.run(`setPeriod(${period})`);
      else element("periods").listeners.get("click")({target: {closest: () => ({dataset: {period}, classList: {add() {}}})}});
    },
  };
}

function assertCleared(h, mobile) {
  assert.equal(h.run("state.decision"), null);
  assert.equal(h.run("state.kline.length"), 0);
  assert.equal(h.element(mobile ? "k-last" : "qb-last").textContent, "--");
  if (mobile) {
    assert.equal(h.element("dd-entry").textContent, "--");
    assert.equal(h.element("dc-entry").textContent, "--");
    assert.ok(h.element("dc-quick").classList.contains("hidden"));
  } else {
    assert.equal(h.element("depth-body").children.length, 0);
    assert.ok(!h.element("decision-body").innerHTML.includes("入场参考"));
    assert.ok(!h.element("chart-hint").textContent.includes("失败"));
  }
  for (const c of h.charts) for (const series of c.getOption().series || []) assert.equal(series.data.length, 0);
}

async function check(mobile) {
  const name = mobile ? "mobile" : "desktop";
  // Both apps retain one healthy socket and reject all callbacks from a replaced connection.
  const connection = harness(mobile); await flush();
  const originalSocket = connection.sockets[0];
  connection.run("connectWS(); connectWS()"); connection.visibility("hidden"); connection.visibility("visible"); await flush();
  assert.equal(connection.sockets.length, 1); assert.equal(originalSocket.readyState, 1);
  connection.visibility("hidden"); originalSocket.disconnect();
  assert.ok(![...connection.timeouts.values()].some(timer => timer.delay === 3000));
  const oldCallbacks = {open: originalSocket.onopen, close: originalSocket.onclose, error: originalSocket.onerror, message: originalSocket.onmessage};
  connection.visibility("visible"); connection.visibility("visible"); await flush();
  assert.equal(connection.sockets.length, 2); const activeSocket = connection.sockets[1];
  assert.equal(connection.run("state.ws"), activeSocket);
  const message = last => ({type: "quote", symbol: A, data: {symbol: A, last, pre_close: 110,
    open: 111, high: 112, low: 109, volume: 1, open_interest: 1, ask: [], bid: []}});
  activeSocket.emit(message(222)); const sentCount = activeSocket.sent.length;
  oldCallbacks.open(); oldCallbacks.close(); oldCallbacks.error(); oldCallbacks.message({data: JSON.stringify(message(999))});
  assert.equal(connection.run("state.quote.last"), 222); assert.equal(connection.run("state.ws"), activeSocket);
  assert.equal(activeSocket.readyState, 1); assert.equal(activeSocket.sent.length, sentCount);
  assert.equal(connection.sockets.length, 2); assert.ok(![...connection.timeouts.values()].some(timer => timer.delay === 3000));
  // Polls share slow work rather than creating a new request or aborting it.
  const poll = harness(mobile); await flush();
  const firstKline = poll.latest("kline"), firstDecision = poll.latest("decision");
  for (let i = 0; i < 7; i++) {poll.run("loadKline(); loadDecision()"); await flush();}
  assert.equal(poll.count("kline"), 1); assert.equal(poll.count("decision"), 1);
  assert.equal(firstKline.signal.aborted, false); assert.equal(firstDecision.signal.aborted, false);
  assert.deepEqual(poll.intervals.map(x => x.delay).sort((a, b) => a - b), mobile ? [6000, 15000, 20000] : [6000, 15000, 15000]);
  assert.equal(new URL(firstKline.url, "http://fixture.invalid").searchParams.get("count"), mobile ? "150" : "400");
  const decisionQuery = Object.fromEntries(new URL(firstDecision.url, "http://fixture.invalid").searchParams);
  assert.deepEqual(decisionQuery, {mode: "short", ...Object.fromEntries(Object.entries(risk).map(([key, value]) => [key, String(value)]))});

  // Populate real rendering functions, then keep old refreshes pending during a switch.
  firstKline.resolve({bars: bars(1000)}); firstDecision.resolve(decision(A, 111)); await flush();
  poll.context.quoteFixture = {symbol: A, last: 111, pre_close: 110, open: 111, high: 112, low: 109, volume: 1,
    open_interest: 1, ask: [{price: 112, volume: 1}], bid: [{price: 110, volume: 1}]};
  poll.run(mobile ? "state.quote=quoteFixture; renderQuote()" : "state.quote=quoteFixture; renderQuoteBar(); renderDepth()");
  poll.charts[0].dispatchAction({start: 20, end: 50});
  const refresh = poll.run("loadKline()");
  poll.latest("kline").resolve({bars: bars(2000)}); await refresh;
  assert.deepEqual(Array.from(poll.charts[0].getOption().dataZoom, z => [z.start, z.end]), mobile ? [[20, 50]] : [[20, 50], [20, 50]]);
  poll.run("loadKline(); loadDecision()"); await flush();
  const oldKline = poll.latest("kline"), oldDecision = poll.latest("decision");
  if (!mobile) poll.element("chart-hint").textContent = "K线加载失败：旧合约错误";
  poll.run(`switchSymbol(${JSON.stringify(B)})`); await flush();
  assert.equal(oldKline.signal.aborted, true); assert.equal(oldDecision.signal.aborted, true);
  assertCleared(poll, mobile);
  const nextKline = poll.latest("kline"), nextDecision = poll.latest("decision");
  const klineWork = poll.run("state.klineWork"), decisionWork = poll.run("state.decisionWork");
  oldKline.resolve({bars: bars(3000)}); oldDecision.resolve(decision(A, 333)); await flush();
  assert.equal(poll.run("state.klineWork"), klineWork); assert.equal(poll.run("state.decisionWork"), decisionWork);
  assertCleared(poll, mobile);
  const counts = [poll.count("kline"), poll.count("decision")];
  poll.run("loadKline(); loadDecision()"); await flush();
  assert.deepEqual([poll.count("kline"), poll.count("decision")], counts);
  nextKline.resolve({bars: bars(4000)}); nextDecision.resolve(decision(B, 222)); await flush();
  assert.equal(poll.run("state.kline[0].datetime"), 4000); assert.equal(poll.run("state.decision.entry"), 222);
  assert.equal(poll.run("state.klineWork"), null); assert.equal(poll.run("state.decisionWork"), null);

  // Aborted work can still reject later: neither its error nor finally owns the replacement.
  const errors = harness(mobile); await flush();
  const staleK = errors.latest("kline"), staleD = errors.latest("decision");
  errors.run(`switchSymbol(${JSON.stringify(B)})`); await flush();
  const currentK = errors.run("state.klineWork"), currentD = errors.run("state.decisionWork");
  staleK.reject(new Error("stale Kline failure")); staleD.reject(new Error("stale decision failure")); await flush();
  assert.equal(errors.run("state.klineWork"), currentK); assert.equal(errors.run("state.decisionWork"), currentD);
  assert.ok(!errors.element("decision-metadata").textContent.includes("stale"));
  assert.ok(!errors.element("chart-hint").textContent.includes("stale"));
  assert.equal(errors.latest("kline").signal.aborted, false); assert.equal(errors.latest("decision").signal.aborted, false);
  errors.latest("kline").reject(new Error("current Kline failure"));
  errors.latest("decision").reject(new Error("current decision failure")); await flush();
  assert.equal(errors.run("state.klineWork"), null); assert.equal(errors.run("state.decisionWork"), null);
  assert.match(errors.element("decision-metadata").textContent, /current decision failure/);
  const failures = errors.count("kline"); errors.run("loadKline()"); await flush(); assert.equal(errors.count("kline"), failures + 1);

  // A -> B -> A must not let the old A finally delete the new A request from shared controls.
  const back = harness(mobile); await flush(); const oldA = back.latest("decision"), oldAKline = back.latest("kline");
  back.run(`switchSymbol(${JSON.stringify(B)})`); await flush();
  back.run(`switchSymbol(${JSON.stringify(A)})`); await flush(); const newA = back.latest("decision");
  assert.notEqual(newA, oldA); assert.equal(oldA.signal.aborted, true);
  const currentA = back.run("state.decisionWork"), currentAKline = back.run("state.klineWork");
  oldA.resolve(decision(A, 111)); oldAKline.resolve({bars: bars(1000)}); await flush();
  assert.equal(back.run("state.decisionWork"), currentA); assert.equal(back.run("state.decision"), null);
  assert.equal(back.run("state.klineWork"), currentAKline); assert.equal(back.run("state.kline.length"), 0);
  const countA = back.count("decision"); back.run(`DecisionControls.request(${JSON.stringify(A)})`); await flush();
  assert.equal(back.count("decision"), countA);
  const countKlineA = back.count("kline"); back.run("loadKline()"); await flush(); assert.equal(back.count("kline"), countKlineA);
  newA.resolve(decision(A, 444)); back.latest("kline").resolve({bars: bars(4444)}); await flush();
  assert.equal(back.run("state.decision.entry"), 444); assert.equal(back.run("state.kline[0].datetime"), 4444);

  // A period change invalidates work even when the mobile Kline screen is currently hidden.
  const period = harness(mobile); await flush(); const oldPeriod = period.latest("kline");
  period.period(86400); await flush(); assert.equal(oldPeriod.signal.aborted, true);
  oldPeriod.resolve({bars: bars(1000)}); await flush(); assert.equal(period.run("state.kline.length"), 0);
  period.run("loadKline()"); await flush(); const daily = period.latest("kline");
  assert.equal(new URL(daily.url, "http://fixture.invalid").searchParams.get("period"), "86400");
  daily.resolve({bars: bars(5000)}); await flush(); assert.equal(period.run("state.kline[0].datetime"), 5000);

  // Cancellation also fences requests waiting for the profile catalogue to finish loading.
  const pending = harness(mobile, true); await flush(); pending.run(`switchSymbol(${JSON.stringify(B)})`);
  pending.requests.find(r => r.url === "/api/v1/decision-profiles").resolve(catalog); await flush();
  assert.equal(pending.count("decision"), 1); assert.ok(pending.latest("decision").url.includes(encodeURIComponent(B)));

  if (mobile) {
    const remove = harness(true); await flush();
    remove.latest("kline").resolve({bars: bars(1000)}); remove.latest("decision").resolve(decision(A, 111)); await flush();
    remove.run(`removeWatch(${JSON.stringify(A)})`); await flush();
    assert.equal(remove.run("state.symbol"), B); assert.equal(remove.run("state.screen"), "quotes"); assertCleared(remove, true);
    assert.equal(remove.element("d-code").textContent, B);
    assert.ok(remove.sockets[0].sent.some(m => m.action === "subscribe" && m.symbols.includes(B)));
    assert.ok(!remove.sockets[0].sent.some(m => m.action === "unsubscribe" && m.symbols.includes(A)), "keep existing global current-removal subscription semantics");
    remove.run(`removeWatch(${JSON.stringify(B)})`); await flush(); assert.equal(remove.run("state.symbol"), B); assert.equal(remove.run("state.watchlist.length"), 0);
    const other = harness(true); await flush(); other.run(`removeWatch(${JSON.stringify(B)})`);
    assert.equal(other.run("state.symbol"), A); assert.ok(other.sockets[0].sent.some(m => m.action === "unsubscribe" && m.symbols.includes(B)));
    const removing = harness(true); await flush();
    removing.sockets[0].emit({type: "subscribed", failed: [{symbol: B}]});
    const retry = [...removing.timeouts.entries()].find(([, timer]) => timer.delay === 1000);
    assert.ok(retry); removing.run(`removeWatch(${JSON.stringify(B)})`);
    assert.equal(removing.timeouts.has(retry[0]), false); const sentBefore = removing.sockets[0].sent.length;
    retry[1].fn(); assert.equal(removing.sockets[0].sent.length, sentBefore);
    assert.equal(removing.sockets[0].sent.at(-1).action, "unsubscribe");
    removing.sockets[0].emit({type: "subscribed", failed: [{symbol: B}]});
    assert.ok(![...removing.timeouts.values()].some(timer => timer.delay === 1000));
  }
  console.log(`PASS ${name}: cancellation, catalogue wait, success/error/finally races, slow polling, DOM clearing, period, zoom and socket identity/visibility`);
}

(async () => {await check(false); await check(true);})().catch(error => {console.error(error); process.exitCode = 1;});
