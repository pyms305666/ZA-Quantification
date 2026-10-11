/* Shared market transport: one socket, bounded subscription retries and visibility recovery. */
"use strict";
(function (global) {
  const RECONNECT_DELAYS = [3000, 6000, 12000, 24000, 30000];
  const SUBSCRIBE_DELAYS = [1000, 2000, 4000, 5000, 5000, 5000];

  function create({url, getSymbols, onMessage = () => {}, onState = () => {}}) {
    let socket = null, generation = 0, disposed = false;
    let reconnectTimer = null, subscribeTimer = null;
    let reconnectAttempt = 0, subscribeAttempt = 0;
    let symbols = [], intentKey = "", intentRevision = 0;
    const hidden = () => document.visibilityState === "hidden";
    const current = (ws, token) => !disposed && socket === ws && generation === token;
    const notify = status => onState({status, socket});
    function clearReconnect() { clearTimeout(reconnectTimer); reconnectTimer = null; }
    function clearSubscribe() { clearTimeout(subscribeTimer); subscribeTimer = null; }

    function syncIntent() {
      const next = [...new Set(getSymbols())];
      const key = JSON.stringify([...next].sort());
      symbols = next;
      if (key !== intentKey) {
        intentKey = key;
        intentRevision++;
        subscribeAttempt = 0;
        clearSubscribe();
      }
    }

    function scheduleReconnect() {
      if (disposed || socket || reconnectTimer !== null || hidden()) return;
      const token = generation;
      const delay = RECONNECT_DELAYS[Math.min(reconnectAttempt - 1, RECONNECT_DELAYS.length - 1)];
      const timer = setTimeout(() => {
        if (disposed || reconnectTimer !== timer || generation !== token) return;
        reconnectTimer = null;
        if (hidden()) { notify("paused"); return; }
        connect();
      }, delay);
      reconnectTimer = timer;
    }

    function closed(ws, token) {
      if (!current(ws, token)) return;
      socket = null;
      clearSubscribe();
      reconnectAttempt++;
      notify(hidden() ? "paused" : "retrying");
      scheduleReconnect();
    }

    function closeFailed(ws, token) {
      if (!current(ws, token)) return;
      try { ws.close(); } catch { closed(ws, token); }
    }

    function send(message) {
      const ws = socket, token = generation;
      if (!ws || !current(ws, token) || ws.readyState !== 1) return false;
      try { ws.send(JSON.stringify(message)); return true; }
      catch { closeFailed(ws, token); return false; }
    }

    function sendSubscribe() {
      if (symbols.length) send({action: "subscribe", symbols});
    }

    function subscribe({send: shouldSend = true} = {}) {
      if (disposed) return;
      syncIntent();
      if (shouldSend) sendSubscribe();
    }

    function retrySubscribe(ws, token) {
      if (!current(ws, token) || ws.readyState !== 1 || subscribeTimer !== null || subscribeAttempt >= SUBSCRIBE_DELAYS.length) return;
      const revision = intentRevision;
      const timer = setTimeout(() => {
        if (!current(ws, token) || subscribeTimer !== timer || intentRevision !== revision) return;
        subscribeTimer = null;
        subscribeAttempt++;
        sendSubscribe();
      }, SUBSCRIBE_DELAYS[subscribeAttempt]);
      subscribeTimer = timer;
    }

    function subscriptionResult(msg, ws, token) {
      // The protocol has no request id. Only acknowledgements about the current intent apply.
      syncIntent();
      const failed = Array.isArray(msg.failed) ? msg.failed : [];
      if (failed.some(item => item && symbols.includes(item.symbol))) {
        retrySubscribe(ws, token);
      } else {
        const accepted = [...(Array.isArray(msg.subscribed) ? msg.subscribed : []), ...(Array.isArray(msg.skipped) ? msg.skipped : [])];
        if (symbols.every(symbol => accepted.includes(symbol))) clearSubscribe();
      }
      // An ACK never resets the retry budget; a new intent or socket does.
    }

    function connect() {
      if (disposed || socket || reconnectTimer !== null) return;
      if (hidden()) { notify("paused"); return; }
      const token = ++generation;
      let ws;
      try { ws = new WebSocket(url); }
      catch {
        reconnectAttempt++;
        notify("retrying");
        scheduleReconnect();
        return;
      }
      socket = ws;
      ws.onopen = () => {
        if (!current(ws, token) || ws.readyState !== 1) return;
        clearReconnect();
        clearSubscribe();
        reconnectAttempt = 0;
        subscribeAttempt = 0;
        syncIntent();
        notify("open");
        sendSubscribe();
      };
      ws.onmessage = event => {
        if (!current(ws, token) || ws.readyState !== 1) return;
        let msg;
        try { msg = JSON.parse(event.data); } catch { return; }
        if (!msg || typeof msg !== "object") return;
        if (msg.type === "subscribed") subscriptionResult(msg, ws, token);
        onMessage(msg);
      };
      ws.onclose = () => closed(ws, token);
      ws.onerror = () => closeFailed(ws, token);
      notify("connecting");
    }

    function visibilityChanged() {
      if (disposed) return;
      if (hidden()) {
        clearReconnect();
        if (!socket) notify("paused");
      } else if (!socket) {
        clearReconnect();
        connect();
      }
    }

    function dispose() {
      if (disposed) return;
      disposed = true;
      generation++;
      clearReconnect();
      clearSubscribe();
      document.removeEventListener("visibilitychange", visibilityChanged);
      global.removeEventListener("beforeunload", dispose);
      const ws = socket;
      socket = null;
      if (ws) { try { ws.close(); } catch {} }
      notify("disposed");
    }

    document.addEventListener("visibilitychange", visibilityChanged);
    global.addEventListener("beforeunload", dispose);
    return {connect, subscribe, send, dispose, get socket() { return socket; }};
  }
  global.MarketConnection = {create};
})(window);
