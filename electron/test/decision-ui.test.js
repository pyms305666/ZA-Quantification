/* Own-app browser tests with synthetic API fixtures; never connects to a trading account. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const {execFileSync} = require("node:child_process");
const {chromium} = require("playwright-core");
const root = path.resolve(__dirname,"../..");
const fixtures = JSON.parse(execFileSync(process.env.QA_PYTHON || "python", ["-c", `
import json
from config import RiskConfig
from market.decision_profiles import profile_catalog, PROFILES
from market.evaluator import evaluate
from tests.test_decision_profiles import INSTRUMENT,NOW,history,fresh_quote
print(json.dumps(dict(catalog=profile_catalog(RiskConfig()),results={m:evaluate(INSTRUMENT,fresh_quote(),history(),RiskConfig(),m,NOW) for m in PROFILES})))
`], {cwd:root, encoding:"utf8"}));

async function check(browser, mobile) {
  const staticRoot = path.join(root,mobile?"mobile-app/app/src/main/python/static":"static");
  let fail = false;
  let hold = false;
  const calls = [];
  const pending = [];
  const server = http.createServer((req,res) => {
    const url = new URL(req.url,"http://localhost");
    const json = (body,status=200) => { if(res.destroyed) return; res.writeHead(status,{"content-type":"application/json"});res.end(JSON.stringify(body)); };
    const complete = (body, symbol, kind) => {
      if (!hold) return json(body);
      pending.push({symbol, kind, completed:false, release() {this.completed=true; json(body);}});
    };
    if(url.pathname === "/api/v1/decision-profiles") return json(fixtures.catalog);
    if(url.pathname.startsWith("/api/v1/decision/")) {
      const q = Object.fromEntries(url.searchParams); calls.push(q);
      const symbol = decodeURIComponent(url.pathname.split("/").at(-1));
      if(fail) return json({detail:"界面测试：模拟断网"},503);
      const d = {...fixtures.results[q.mode]};
      d.symbol = symbol;
      d.risk_budget = Math.min(Number(q.max_loss_per_trade),Number(q.account_equity)*Number(q.risk_percent)/100);
      if(d.risk_budget < 1) {d.contracts=0;d.risk_amount=0;d.risk_percent=0;}
      if (hold) return complete(d, symbol, "decision");
      if(q.mode === "medium") return setTimeout(()=>json(d),400);
      return json(d);
    }
    if(url.pathname.startsWith("/api/v1/kline/")) {
      const symbol = decodeURIComponent(url.pathname.split("/").at(-1));
      const period = Number(url.searchParams.get("period")), count = Number(url.searchParams.get("count"));
      const bars = Array.from({length:count}, (_,i) => ({datetime:Date.UTC(2026,0,1)+i*period*1000,
        open:100,close:101,low:99,high:102,volume:10,open_interest:20}));
      return complete({bars}, symbol, "kline");
    }
    if(url.pathname.startsWith("/api/")) return json({configured:true,account:"界面测试",connected:true,
      catalog_ready:true,catalog_complete:true,route:"C 直连版",bars:[],data:null,pending:true,items:[],instruments:[],symbols:[],total:0});
    const relative = url.pathname === "/" ? "index.html" : url.pathname.replace(/^\/static\//,"");
    const file = path.resolve(staticRoot,relative);
    if(!file.startsWith(staticRoot+path.sep) || !fs.existsSync(file)) {res.writeHead(404);return res.end();}
    const mime = {".js":"application/javascript",".css":"text/css",".html":"text/html"}[path.extname(file)] || "application/octet-stream";
    res.writeHead(200,{"content-type":mime+"; charset=utf-8"});res.end(fs.readFileSync(file));
  });
  await new Promise(resolve=>server.listen(0,"127.0.0.1",resolve));
  const context = await browser.newContext({viewport:mobile?{width:390,height:844}:{width:1440,height:1000}});
  const origin = `http://127.0.0.1:${server.address().port}`;
  await context.route("**/*", route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort("blockedbyclient"));
  await context.addInitScript(() => {
    window.__fixtureSockets = [];
    window.WebSocket = class {
      constructor() {this.readyState=1;this.sent=[];window.__fixtureSockets.push(this);queueMicrotask(()=>this.onopen?.());}
      send(data) {this.sent.push(JSON.parse(data));}
      close() {this.readyState=3;}
      emit(message) {this.onmessage?.({data:JSON.stringify(message)});}
    };
  });
  const page = await context.newPage();
  const errors=[]; page.on("pageerror",error=>errors.push(error.message));
  const waitPending = async predicate => {
    const deadline = Date.now()+5000;
    while(!predicate()) {
      assert.ok(Date.now()<deadline,"timed out waiting for held fixture requests");
      await new Promise(resolve=>setTimeout(resolve,10));
    }
  };
  try {
    await page.goto(origin);
    if(mobile) await page.locator('.tab[data-s="decision"]').click();
    await page.waitForFunction(()=>document.querySelector('#decision-metadata').textContent.includes('短线 ·'));
    const period = await page.evaluate(()=>state.period);
    for(const mode of ["ultra","short","medium","long"]) {
      await page.locator(`[data-mode="${mode}"]`).click();
      await page.waitForFunction(m=>state.decision?.mode === m,mode);
      assert.equal(await page.evaluate(()=>state.period),period,"mode must not change chart period");
    }
    await page.locator('[data-mode="ultra"]').click();
    await page.locator('.dp-settings summary').click();
    await page.locator('[name="max_loss_per_trade"]').fill("0.01");
    await page.locator('.dp-form button').click();
    await page.waitForFunction(()=>state.decision?.contracts===0);
    assert.equal(calls.at(-1).max_loss_per_trade,"0.01");
    assert.match(await page.locator('#decision-metadata').textContent(),/建议 0 手/);
    await page.locator('[data-mode="short"]').click();
    await page.locator('.dp-settings summary').click();
    assert.equal(await page.locator('[name="max_loss_per_trade"]').inputValue(),"900");
    await page.locator('[data-mode="ultra"]').click();
    await page.reload();
    if(mobile) await page.locator('.tab[data-s="decision"]').click();
    await page.waitForFunction(()=>state.decision?.mode === "ultra");
    await page.locator('.dp-settings summary').click();
    assert.equal(await page.locator('[name="max_loss_per_trade"]').inputValue(),"0.01");
    await page.locator('[data-mode="medium"]').click();
    await page.locator('[data-mode="long"]').click();
    await page.waitForFunction(()=>state.decision?.mode === "long");
    await page.waitForTimeout(500); // deliberately let the old synthetic response arrive
    assert.equal(await page.evaluate(()=>state.decision.mode),"long");
    if(mobile) {
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth <= window.innerWidth+1),"mobile must not overflow horizontally");
    }

    // Real ECharts preserves a user zoom on an ordinary refresh of the same selection.
    await page.evaluate(async mobile => {
      const c = mobile ? state.chart : chart;
      c.dispatchAction({type:"dataZoom",start:20,end:50});
      await loadKline();
    }, mobile);
    const zoom = await page.evaluate(mobile => (mobile?state.chart:chart).getOption().dataZoom.map(z=>[z.start,z.end]), mobile);
    for (const [start,end] of zoom) {
      assert.ok(Math.abs(start-20)<.00001, "refresh keeps zoom start");
      assert.ok(Math.abs(end-50)<.00001, "refresh keeps zoom end");
    }
    const original = await page.evaluate(()=>state.symbol);
    await page.evaluate(symbol => window.__fixtureSockets.at(-1).emit({type:"quote",symbol,data:{symbol,last:111,pre_close:110,
      open:111,high:112,low:109,volume:1,open_interest:1,ask:[{price:112,volume:1}],bid:[{price:110,volume:1}]}}), original);
    hold=true;
    await page.evaluate(()=>{loadKline();loadDecision();});
    await page.waitForFunction(()=>state.klineWork && state.decisionWork);
    await waitPending(()=>pending.length>=2);
    const old = pending.slice();
    const selected = "SHFE.au2612";
    await page.evaluate(symbol=>{switchSymbol(symbol);}, selected);
    assert.equal(await page.evaluate(()=>state.decision),null);
    assert.equal(await page.locator(mobile?"#k-last":"#qb-last").textContent(),"--");
    if(mobile) {
      assert.equal(await page.locator("#dd-entry").textContent(),"--");
      assert.equal(await page.locator("#dc-entry").textContent(),"--");
      assert.equal(await page.locator("#dc-quick").evaluate(e=>e.classList.contains("hidden")),true);
    } else assert.equal(await page.locator("#depth-body").textContent(),"");
    assert.equal(await page.evaluate(mobile=>(mobile?[state.chart,state.macdChart]:[chart]).every(c=>c.getOption().series.every(s=>s.data.length===0)),mobile),true);
    await waitPending(()=>pending.some(p=>p.symbol===selected && p.kind==="decision") && pending.some(p=>p.symbol===selected && p.kind==="kline"));
    old.forEach(p=>p.release());
    assert.equal(await page.evaluate(()=>state.decision),null,"cancelled old work cannot repopulate the selected panel");
    pending.filter(p=>p.symbol===selected && !p.completed).forEach(p=>p.release());
    await page.waitForFunction(symbol=>state.decision?.symbol===symbol && state.kline.length>0,selected);
    if(mobile) {
      await page.evaluate(()=>{showScreen("quotes");removeWatch(state.symbol);});
      assert.equal(await page.evaluate(()=>state.screen),"quotes");
      assert.equal(await page.evaluate(()=>state.symbol),original);
      assert.equal(await page.locator("#dd-entry").textContent(),"--");
      assert.equal(await page.locator("#dc-entry").textContent(),"--");
      await waitPending(()=>pending.some(p=>p.symbol===original && !p.completed && p.kind==="decision") && pending.some(p=>p.symbol===original && !p.completed && p.kind==="kline"));
      pending.filter(p=>!p.completed).forEach(p=>p.release());
      await page.waitForFunction(symbol=>state.decision?.symbol===symbol,original);
    }
    hold=false;
    fs.mkdirSync(path.join(root,"build/qa"),{recursive:true});
    await page.screenshot({path:path.join(root,`build/qa/decision-${mobile?"mobile":"desktop"}.png`),fullPage:true});
    fail=true;
    await page.evaluate(()=>loadDecision());
    assert.match(await page.locator('#decision-metadata').textContent(),/模拟断网/);
    assert.equal(await page.evaluate(()=>state.decision),null);
    if(mobile) assert.equal(await page.locator('#dd-entry').textContent(),"--");
    assert.deepEqual(errors,[]);
    console.log(`PASS ${mobile?"mobile 390px":"desktop"}: four modes, independent chart, per-mode save/reload, latest response, zero lots, selection clearing, zoom, offline Fake WS`);
  } finally {
    await context.close();
    server.closeAllConnections();
    await new Promise(resolve=>server.close(resolve));
  }
}
(async()=>{
  const browser=await chromium.launch({channel:process.env.QA_BROWSER || "msedge",headless:true});
  try { await check(browser,false);await check(browser,true); }
  finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
