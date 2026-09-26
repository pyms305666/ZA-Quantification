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
  const calls = [];
  const server = http.createServer((req,res) => {
    const url = new URL(req.url,"http://localhost");
    const json = (body,status=200) => { if(res.destroyed) return; res.writeHead(status,{"content-type":"application/json"});res.end(JSON.stringify(body)); };
    if(url.pathname === "/api/v1/decision-profiles") return json(fixtures.catalog);
    if(url.pathname.startsWith("/api/v1/decision/")) {
      const q = Object.fromEntries(url.searchParams); calls.push(q);
      if(fail) return json({detail:"界面测试：模拟断网"},503);
      const d = {...fixtures.results[q.mode]};
      d.risk_budget = Math.min(Number(q.max_loss_per_trade),Number(q.account_equity)*Number(q.risk_percent)/100);
      if(d.risk_budget < 1) {d.contracts=0;d.risk_amount=0;d.risk_percent=0;}
      if(q.mode === "medium") return setTimeout(()=>json(d),400);
      return json(d);
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
  const page = await context.newPage();
  const errors=[]; page.on("pageerror",error=>errors.push(error.message));
  try {
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
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
    fs.mkdirSync(path.join(root,"build/qa"),{recursive:true});
    await page.screenshot({path:path.join(root,`build/qa/decision-${mobile?"mobile":"desktop"}.png`),fullPage:true});
    fail=true;
    await page.evaluate(()=>loadDecision());
    assert.match(await page.locator('#decision-metadata').textContent(),/模拟断网/);
    assert.equal(await page.evaluate(()=>state.decision),null);
    if(mobile) assert.equal(await page.locator('#dd-entry').textContent(),"--");
    assert.deepEqual(errors,[]);
    console.log(`PASS ${mobile?"mobile 390px":"desktop"}: four modes, independent chart, per-mode save/reload, latest response, zero lots, stale clearing`);
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
