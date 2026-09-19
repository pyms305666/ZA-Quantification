/* =====================================================================
 * ZA量化 桌面端 —— 主进程入口（main.js）v3：自动启动后端 + 可诊断
 * =====================================================================
 * v1：要求用户先手动运行 python launcher.py，Electron 只负责开窗口
 * v2：Electron 自己探测后端 → 没启动就 spawn 自动拉起 → 等就绪 → 开窗口
 * v3（本次，对应 docs/项目检查报告-2026-09-11.md P1）：
 *   ① portBlockedBy 改为模块级 let 声明，每次 ensureServer 开始时重置
 *      （原来依赖非严格模式的隐式全局变量，状态会残留、无法单测）；
 *   ② Python 解释器不再写死 "python"：按 项目 venv → py -3 → PATH python
 *      逐个尝试（候选链在 backend.js，可单测），实际选中哪个写进日志；
 *   ③ 后端 stdout/stderr 不再 stdio:"ignore"：写入 electron/backend.log，
 *      最近 40 行保留在内存尾部缓冲，启动失败时直接显示在错误页上；
 *   ④ 监听子进程 error/exit：python 不存在、依赖缺失、异常退出都有明确原因。
 * ===================================================================== */

const { app, BrowserWindow } = require("electron");
const path = require("path");
const fs = require("fs");
const { spawn } = require("child_process");
const { resolvePythonCandidates, createTailBuffer } = require("./backend");

const SERVER_URL = "http://127.0.0.1:8000";
const MY_ROUTE = "C 直连版";   // 本构建的路线标识
const PROJECT_DIR = path.join(__dirname, "..");          // 项目根目录（electron/ 的上一级）
const LAUNCHER = path.join(PROJECT_DIR, "launcher.py");  // 后端启动脚本
const BACKEND_LOG = path.join(__dirname, "backend.log"); // 后端输出日志（追加写）

// 记录"由我们启动的后端子进程"——"谁创建，谁负责清理"：
// 退出时要把它关掉，否则后台残留 python（占端口、占内存）。
// 用户自己先启动的后端不会记在这里，退出时不碰。
let backendProcess = null;

// 端口被哪个路线占用（null=没被占用）。模块级声明 + 每次探测前重置，
// 避免 v2 里"隐式全局变量跨次启动残留"的问题。
let portBlockedBy = null;

// 最近 40 行后端输出：错误页直接展示，用户不必翻日志文件
const backendTail = createTailBuffer(40);

// ---------------------------------------------------------------------
// 小工具
// ---------------------------------------------------------------------

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// 后端日志：追加写文件 + 内存尾部缓冲（错误页用）
function logBackend(line) {
  const stamped = `[${new Date().toLocaleTimeString("zh-CN", { hour12: false })}] ${line}`;
  backendTail.push(stamped);
  console.log(`[后端] ${stamped}`);
  try {
    fs.appendFileSync(BACKEND_LOG, stamped + "\n");
  } catch { /* 日志写失败不影响主流程 */ }
}

// 探测后端是否活着（v1 就有，原样保留）
async function whatIsOnPort() {
  try {
    const resp = await fetch(SERVER_URL + "/api/v1/status");
    if (!resp.ok) return { running: false, route: null };
    const data = await resp.json();
    return { running: true, route: data.route || "未知版本" };
  } catch {
    return { running: false, route: null };
  }
}

async function isServerUp() {
  const st = await whatIsOnPort();
  return st.running && st.route === MY_ROUTE;
}

/**
 * 用候选链里的某一个解释器启动后端。
 * @returns {Promise<{child: object|null, error: string|null}>}
 *          ENOENT（找不到该命令）返回 error，由调用方换下一个候选；
 *          其它错误（如脚本路径不对）也返回 error，但已记入日志。
 */
function spawnBackend(candidate) {
  return new Promise((resolve) => {
    logBackend(`尝试启动：${candidate.cmd} ${candidate.args.join(" ")}（来源：${candidate.source}）`);
    let child;
    try {
      child = spawn(candidate.cmd, candidate.args, {
        cwd: PROJECT_DIR,
        stdio: ["ignore", "pipe", "pipe"],   // 输出进日志，不再丢弃
      });
    } catch (error) {
      logBackend(`启动异常：${error.message}`);
      return resolve({ child: null, error: error.message });
    }
    // ENOENT 等启动错误通过 error 事件异步到达
    child.on("error", (error) => {
      logBackend(`进程错误：${error.message}`);
      resolve({ child, error: error.message });
    });
    child.stdout.on("data", (d) => String(d).split("\n").filter(Boolean)
      .forEach((l) => logBackend(l)));
    child.stderr.on("data", (d) => String(d).split("\n").filter(Boolean)
      .forEach((l) => logBackend(`[stderr] ${l}`)));
    child.on("exit", (code, signal) => {
      logBackend(`后端退出：code=${code} signal=${signal ?? "-"}`);
    });
    resolve({ child, error: null });
  });
}

// 确保后端在跑：探测 → (被占用则报告) → 按候选链拉起 → 轮询等就绪
async function ensureServer() {
  portBlockedBy = null;   // 每次探测都重置，避免上一次的状态残留
  backendTail.clear();

  const onPort = await whatIsOnPort();
  if (onPort.running) {
    if (onPort.route === MY_ROUTE) {
      console.log("[ZA量化] 检测到本版本后端已在运行，直接使用");
      return true;
    }
    portBlockedBy = onPort.route;
    console.log(`[ZA量化] 8000 端口被另一条路线占用：${onPort.route}（本版本是 ${MY_ROUTE}）`);
    return false;
  }

  console.log("[ZA量化] 后端未运行，正在自动启动 ...");
  const candidates = resolvePythonCandidates(PROJECT_DIR, LAUNCHER);
  let lastError = "没有可用的 Python 解释器";

  for (const candidate of candidates) {
    const { child, error } = await spawnBackend(candidate);
    if (error) {
      lastError = `${candidate.cmd}: ${error}`;
      if (child) child.kill();
      continue;   // 这个解释器不可用，试下一个
    }
    backendProcess = child;
    // 轮询等待：最多 30 秒（60 次 × 0.5 秒）
    for (let i = 0; i < 60; i++) {
      await sleep(500);
      const st = await whatIsOnPort();
      if (st.running && st.route === MY_ROUTE) {
        console.log("[ZA量化] 后端已就绪");
        return true;
      }
      if (child.exitCode !== null) break;   // 进程已经退出，等也没用，换下一个候选
    }
    if (child.exitCode === null) {
      console.log("[ZA量化] 后端启动超时（30 秒），终止子进程");
      child.kill();
    }
    lastError = `${candidate.cmd}: 启动后 30 秒内未就绪（详见 ${BACKEND_LOG}）`;
  }
  console.log(`[ZA量化] 后端启动失败：${lastError}`);
  return false;
}

// ---------------------------------------------------------------------
// 等 Electron 就绪，创建窗口
// ---------------------------------------------------------------------
// 应用就绪：探测 8000 → 需要则自动拉起后端 → 创建主窗口（失败显示可诊断错误页）
app.whenReady().then(async () => {
  const serverReady = await ensureServer();

  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1180,
    minHeight: 760,
    title: "ZA量化",
    icon: path.join(__dirname, "..", "assets", "icon.png"),
    autoHideMenuBar: true,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
    },
  });

  // ---------- 根据后端状态加载内容 ----------
  if (serverReady) {
    await win.loadURL(SERVER_URL);
  } else if (portBlockedBy) {
    await win.loadURL(
      "data:text/html;charset=utf-8," +
        encodeURIComponent(`<!DOCTYPE html>
<html lang="zh-CN">
<body style="font-family:'Microsoft YaHei UI';text-align:center;padding-top:120px;color:#555;">
  <h2 style="color:#333;">ZA量化 · 端口被占用</h2>
  <p>8000 端口已被 <b style="color:#c00;">${portBlockedBy}</b> 占用，</p>
  <p>本次要启动的是 <b>${MY_ROUTE}</b>。</p>
  <p style="font-size:13px;color:#999;">两条路线共用 8000 端口，不能同时运行：<br>
  ① 任务管理器里结束另一个 ZA量化 进程后重试；<br>
  ② 或修改 config.json 的 server.port 给本版本换端口。</p>
</body>
</html>`)
    );
  } else {
    // 失败页带诊断信息：实际尝试的命令 + 后端日志尾部 + 日志文件位置
    const tail = backendTail.text().replace(/</g, "&lt;");
    await win.loadURL(
      "data:text/html;charset=utf-8," +
        encodeURIComponent(`<!DOCTYPE html>
<html lang="zh-CN">
<body style="font-family:'Microsoft YaHei UI';padding:40px 60px;color:#555;">
  <h2 style="color:#333;">ZA量化 · 后端启动失败</h2>
  <p>已依次尝试项目虚拟环境 / <b>py -3</b> / 系统 PATH 中的 python，均未成功。</p>
  <p style="font-size:13px;">完整日志：<b>${BACKEND_LOG}</b></p>
  <pre style="background:#f6f6f6;border:1px solid #ddd;padding:12px;font-size:12px;
              max-height:360px;overflow:auto;white-space:pre-wrap;">${tail || "（无输出）"}</pre>
  <p style="font-size:13px;color:#999;">
    常见原因：① 未安装 Python / 依赖（pip install -r requirements.txt）<br>
    ② config.json 配置错误　③ 行情认证失败（看上方日志中的报错行）</p>
</body>
</html>`)
    );
  }

  // ---------- 锁定标题 ----------
  win.on("page-title-updated", (event) => {
    event.preventDefault();
    win.setTitle("ZA量化");
  });

  // ---------- macOS 特殊处理（Windows 用不到，保留标准写法） ----------
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      app.whenReady().then(() => { /* 简单起见直接跳过 */ });
    }
  });
});

// ---------------------------------------------------------------------
// 窗口全关时退出，并清理我们启动的后端
// ---------------------------------------------------------------------
// 所有窗口关闭：桌面应用惯例直接退出（后端子进程在 before-quit 里清理）
app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});

// before-quit = 应用真正退出前的那一刻：把由我们 spawn 出来的 python 后端关掉
// 退出前清理：只关由我们拉起的后端子进程（谁创建谁负责），用户自己的后端不动
app.on("before-quit", () => {
  if (backendProcess) {
    console.log("[ZA量化] 关闭后端服务");
    backendProcess.kill();   // 给子进程发"终止"信号
    backendProcess = null;
  }
});
