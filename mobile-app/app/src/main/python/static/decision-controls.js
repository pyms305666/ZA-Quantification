/* Shared desktop / Android horizon selection and device-local risk profiles. */
"use strict";
window.DecisionControls = (() => {
  const storageKey = "za-decision-profiles-v1";
  let saved = {}, mode = "short", catalog = null, revision = 0;
  let saveFailed = false;
  try { saved = JSON.parse(localStorage.getItem(storageKey) || "{}"); } catch (_) {}
  if (!saved || typeof saved !== "object" || Array.isArray(saved)) saved = {};
  const profiles = {};
  const inFlight = new Map();
  const host = document.getElementById("decision-controls");
  const metadata = document.getElementById("decision-metadata");
  const fields = { account_equity: "账户权益（元）", max_loss_per_trade: "单笔亏损上限（元）",
    risk_percent: "单笔亏损上限（%）", max_contracts: "最大手数" };
  const escape = text => String(text ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const selected = () => catalog.profiles.find(p => p.id === mode);
  function validate(raw) {
    const result = {};
    for (const key of Object.keys(fields)) {
      const value = Number(raw[key]);
      const [min, max] = catalog.risk_limits[key];
      if (!Number.isFinite(value) || value < min || value > max || (key === "max_contracts" && !Number.isInteger(value)))
        throw new Error(`${fields[key]}需为${min}至${max}之间的${key === "max_contracts" ? "整数" : "有效数值"}`);
      result[key] = value;
    }
    return result;
  }
  function persist() {
    try { localStorage.setItem(storageKey, JSON.stringify({mode, profiles})); saveFailed = false; }
    catch (_) { saveFailed = true; }
  }
  function changed() {
    revision++;
    // New selections invalidate obsolete work and its UI immediately.
    for (const work of inFlight.values()) work.controller.abort();
    inFlight.clear();
    persist();
    render();
    renderMeta(null);
    window.dispatchEvent(new Event("decisionprofilechange"));
  }
  function render() {
    const p = selected(), risk = profiles[mode];
    host.innerHTML = `<div class="dp-modes" role="group" aria-label="评估模式">${catalog.profiles.map(x =>
      `<button type="button" data-mode="${escape(x.id)}" aria-pressed="${x.id === mode}" class="${x.id === mode ? "active" : ""}">${escape(x.label)}</button>`).join("")}</div>
      <div class="dp-description">${escape(p.holding)} · 评估使用 ${escape(p.periods.join(" / "))}</div>
      <details class="dp-settings"><summary>${escape(p.label)}风险设置 · 上限 ¥${Math.min(risk.max_loss_per_trade, risk.account_equity*risk.risk_percent/100).toLocaleString("zh-CN")}</summary>
      <form class="dp-form">${Object.entries(fields).map(([key, label]) => `<label>${label}<input name="${key}" type="number" inputmode="decimal" min="${catalog.risk_limits[key][0]}" max="${catalog.risk_limits[key][1]}" step="${key === "max_contracts" ? "1" : "any"}" required value="${risk[key]}"></label>`).join("")}
      <p class="dp-description">金额与权益比例取更严格值。本设备分别保存四档参数，图表周期独立选择。</p>
      <p class="dp-error" role="alert"></p><button type="submit">保存本档设置</button></form></details>
      ${saveFailed ? '<p class="dp-error">本次设置已生效，但本地保存失败，重启后需重新设置。</p>' : ""}`;
    host.querySelectorAll("[data-mode]").forEach(button => button.addEventListener("click", () => {
      if (mode === button.dataset.mode) return;
      mode = button.dataset.mode;
      changed();
    }));
    host.querySelector("form").addEventListener("submit", event => {
      event.preventDefault();
      const form = event.currentTarget;
      try { profiles[mode] = validate(Object.fromEntries(new FormData(form))); changed(); }
      catch (error) { form.querySelector(".dp-error").textContent = error.message; }
    });
  }
  function renderMeta(d, message) {
    if (!metadata) return;
    if (!d || d.pending) { metadata.textContent = message || (d && d.message) || "正在加载本档评估…"; return; }
    const date = value => value ? new Date(value).toLocaleString("zh-CN", {hour12:false, timeZone:"Asia/Shanghai"}) : "未知";
    const lines = [
      `${d.mode_label || ""} · ${d.holding || ""}`,
      `行情时间：${date(d.quote_timestamp)}（北京时间）`,
      `止损基准：${d.atr_period || "--"} ATR × ${d.stop_atr || 1.5}；目标：${d.target1_r || 1.5}R / ${d.target2_r || 3}R`,
      `门槛：评分 ≥${d.min_score || 60} 且多空分差 ≥${d.min_gap || 15}`,
    ];
    if (d.risk_budget != null) lines.push(`本档预算 ¥${d.risk_budget.toLocaleString("zh-CN")}；一手预计止损 ¥${d.one_lot_risk == null ? "--" : d.one_lot_risk.toLocaleString("zh-CN")}`);
    if (d.contracts === 0) lines.push("建议 0 手：一手超过风险预算，不宜开仓。");
    if (!d.data_ok) lines.push(...(d.rationale || []));
    metadata.innerHTML = lines.map(x => `<div>${escape(x)}</div>`).join("") +
      (d.warnings || []).map(x => `<div class="dp-warning">${escape(x)}</div>`).join("") +
      '<div>预计止损金额按价差计算，不含手续费、滑点与跳空。</div>';
  }
  const ready = (async () => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch("/api/v1/decision-profiles", {signal:controller.signal});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "无法读取评估配置");
      catalog = data;
      mode = data.profiles.some(p => p.id === saved.mode) ? saved.mode : data.default_mode;
      for (const p of data.profiles) {
        try { profiles[p.id] = validate(saved.profiles?.[p.id] || data.risk_defaults); }
        catch (_) { profiles[p.id] = validate(data.risk_defaults); }
      }
      render();
    } catch (error) {
      host.textContent = "评估配置加载失败，请刷新页面重试。";
      throw error;
    } finally { clearTimeout(timeout); }
  })();
  // A failed initialization is also surfaced by request(), without an unhandled rejection.
  ready.catch(() => {});
  async function request(symbol) {
    await ready;
    const query = new URLSearchParams({mode, ...profiles[mode]});
    const key = `${symbol}?${query}`;
    if (inFlight.has(key)) return inFlight.get(key).promise;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 120000);
    const work = {controller};
    work.promise = (async () => {
      try {
        const response = await fetch(`/api/v1/decision/${encodeURIComponent(symbol)}?${query}`, {signal:controller.signal});
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
        return data;
      } finally {
        clearTimeout(timeout);
        if (inFlight.get(key) === work) inFlight.delete(key);
      }
    })();
    inFlight.set(key, work);
    return work.promise;
  }
  return {ready, request, renderMeta, escape, get mode() {return mode;}, get revision() {return revision;}};
})();
