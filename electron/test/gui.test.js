/* =====================================================================
 * gui.test.js —— Electron 窗口级 GUI 测试（Playwright，无需浏览器下载）
 * =====================================================================
 * 运行：npm run test:gui    （等价于 node test/gui.test.js）
 *
 * 覆盖场景（对应 docs/待办与验收计划 P1-Electron）：
 *   1. 8000 被另一条路线占用 → 显示端口冲突页（含占用者路线名）；
 *   2. 后端未启动 → 自动拉起 python launcher.py，窗口加载主界面（#st-ws 出现）；
 *   3. 关闭窗口 → 由 Electron 启动的后端进程被清理（8000 不再响应）。
 *
 * 说明：场景 2 会用 config.json 里的真实账号连天勤（只读行情，与手动启动一致）；
 *       若测试前 8000 已有本路线后端在跑，则只验证"复用"，跳过清理断言。
 * ===================================================================== */

const assert = require("assert");
const http = require("http");
const path = require("path");
const { _electron } = require("playwright-core");

const ELECTRON_BIN = require("electron");   // node 环境下 require("electron") 返回 electron.exe 路径
const APP_DIR = path.join(__dirname, "..");
const SERVER = "http://127.0.0.1:8000";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function portUp() {
  try {
    const r = await fetch(SERVER + "/api/v1/status");
    return r.ok;
  } catch {
    return false;
  }
}

async function waitPort(expected, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if ((await portUp()) === expected) return true;
    await sleep(500);
  }
  return false;
}

/* 场景 1：端口被另一条路线占用 → 冲突页 */
async function testPortConflictPage() {
  const fake = http.createServer((req, res) => {
    res.setHeader("content-type", "application/json");
    res.end(JSON.stringify({ route: "AB 协程版" }));
  }).listen(8000);
  let app = null;
  try {
    app = await _electron.launch({ args: ["."], executablePath: ELECTRON_BIN, cwd: APP_DIR });
    const win = await app.firstWindow();
    await win.waitForLoadState("domcontentloaded");
    const html = await win.content();
    assert.ok(html.includes("端口被占用"), "应显示端口占用页");
    assert.ok(html.includes("AB 协程版"), "应显示占用者的路线名");
    console.log("ok 1 - 端口被另一条路线占用 → 冲突页（含路线名）");
  } finally {
    if (app) await app.close().catch(() => {});
    await new Promise((r) => fake.close(r));
  }
}

/* 场景 2 + 3：正常启动（自动拉起后端）+ 关窗清理 */
async function testNormalStartupAndCleanup() {
  const wasUp = await portUp();
  let app = null;
  try {
    app = await _electron.launch({ args: ["."], executablePath: ELECTRON_BIN, cwd: APP_DIR });
    const win = await app.firstWindow();
    await win.waitForURL(/127\.0\.0\.1:8000/, { timeout: 90_000 });
    await win.waitForSelector("#st-ws", { timeout: 20_000 });
    const html = await win.content();
    assert.ok(!html.includes("后端启动失败"), "不应出现启动失败页");
    console.log("ok 2 - 自动拉起后端并加载主界面（#st-ws 就绪）");

    await app.close();
    app = null;

    if (wasUp) {
      console.log("skip 3 - 测试前 8000 已有后端（非本测试启动），跳过清理断言");
    } else {
      const cleaned = await waitPort(false, 10_000);
      assert.ok(cleaned, "窗口关闭后，由 Electron 启动的后端应被清理（8000 不再响应）");
      console.log("ok 3 - 关闭窗口后自动清理后端进程");
    }
  } finally {
    if (app) await app.close().catch(() => {});
  }
}

(async () => {
  await testPortConflictPage();
  await testNormalStartupAndCleanup();
  console.log("\nGUI 测试全部通过");
})().catch((error) => {
  console.error("GUI 测试失败:", error && error.message ? error.message : error);
  process.exit(1);
});
