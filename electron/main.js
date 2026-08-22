/* =====================================================================
 * ZA量化 桌面端 —— 主进程入口（main.js）v2：自动启动后端
 * =====================================================================
 *
 * 相比 v1 新增了什么？
 *   v1：要求用户先手动运行 python launcher.py，Electron 只负责开窗口
 *   v2：Electron 自己探测后端 → 没启动就用 spawn 自动拉起 → 等就绪 → 开窗口
 *
 * 这样用户只需要双击（或 npm start）一个东西，体验就是"一个完整软件"。
 *
 * 本文件同时是教学代码，每一段都有注释。新知识点：
 *   1. spawn()：Node 里启动一个子进程（类似 Python 的 subprocess.Popen）
 *   2. 轮询（polling）：每隔一段时间探测一次，直到条件满足
 *   3. 写一个小的 sleep 工具函数
 * ===================================================================== */

// ---------------------------------------------------------------------
// 第一步：引入模块
// ---------------------------------------------------------------------
const { app, BrowserWindow } = require("electron");
const path = require("path");
const { spawn } = require("child_process");   // ← 新增：用来启动 Python 服务

const SERVER_URL = "http://127.0.0.1:8000";
const PROJECT_DIR = path.join(__dirname, "..");   // 项目根目录（electron/ 的上一级）
const LAUNCHER = path.join(PROJECT_DIR, "launcher.py");  // 后端启动脚本

// ---------------------------------------------------------------------
// 第二步：小工具函数
// ---------------------------------------------------------------------

// sleep：让程序"睡"多少毫秒（1000 毫秒 = 1 秒）
// 返回一个 Promise，await 它就会暂停对应时长
// 实现原理：setTimeout(回调, 毫秒) 是"到时间执行回调"；
//          包一层 Promise 就能用 await 等待它
function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// 探测后端是否活着（v1 就有，原样保留）
async function isServerUp() {
  try {
    const resp = await fetch(SERVER_URL + "/api/v1/status");
    return resp.ok;
  } catch (error) {
    return false;
  }
}

// 确保后端在跑：
//   1) 探测一下，活着就直接返回 true
//   2) 没活着 → 用 spawn 启动 python launcher.py
//   3) 每 500ms 探测一次，最多等 60 次（30 秒），等它就绪
// 这是"守护进程"最常见的写法：检查 → 拉起 → 等待 → 确认
async function ensureServer() {
  if (await isServerUp()) {
    console.log("[ZA量化] 检测到后端已运行");
    return true;
  }

  console.log("[ZA量化] 后端未运行，正在自动启动 python launcher.py ...");

  // spawn(命令, [参数], 选项)
  //   - python          ：要执行的命令（Windows 上也可以是 python.exe）
  //   - [LAUNCHER]      ：传给 python 的参数（脚本路径）
  //   - cwd             ：子进程的工作目录（必须是项目根，launcher.py 要在那里找 config.json）
  //   - stdio: "ignore" ：子进程的输出丢弃（不然会污染本窗口终端）
  const child = spawn("python", [LAUNCHER], {
    cwd: PROJECT_DIR,
    stdio: "ignore",
  });

  // 轮询等待：最多 30 秒（60 次 × 0.5 秒）
  for (let i = 0; i < 60; i++) {
    await sleep(500);
    if (await isServerUp()) {
      console.log("[ZA量化] 后端已就绪");
      return true;
    }
  }

  console.log("[ZA量化] 后端启动超时（30 秒）");
  child.kill();          // 没起来就杀掉子进程，防止残留
  return false;
}

// ---------------------------------------------------------------------
// 第三步：等 Electron 就绪，创建窗口
// ---------------------------------------------------------------------
app.whenReady().then(async () => {
  // ---------- 3.1 先确保后端在跑 ----------
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

  // ---------- 3.2 根据后端状态加载内容 ----------
  if (serverReady) {
    await win.loadURL(SERVER_URL);
  } else {
    // 后端没起来：显示提示页（内容就是一段 HTML 字符串）
    await win.loadURL(
      "data:text/html;charset=utf-8," +
        encodeURIComponent(`<!DOCTYPE html>
<html lang="zh-CN">
<body style="font-family:'Microsoft YaHei UI';text-align:center;padding-top:120px;color:#555;">
  <h2 style="color:#333;">ZA量化</h2>
  <p>后端服务启动失败（30 秒超时）</p>
  <p style="font-size:13px;color:#999;">
    请检查：① 是否安装了 Python 及依赖（pip install -r requirements.txt）<br>
    ② 直接运行 <b>python launcher.py</b> 看报错信息
  </p>
</body>
</html>`)
    );
  }

  // ---------- 3.3 锁定标题 ----------
  win.on("page-title-updated", (event) => {
    event.preventDefault();
    win.setTitle("ZA量化");
  });

  // ---------- 3.4 macOS 特殊处理（Windows 用不到，保留标准写法） ----------
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      app.whenReady().then(() => { /* 简单起见直接跳过 */ });
    }
  });
});

// ---------------------------------------------------------------------
// 第四步：窗口全关时退出
// ---------------------------------------------------------------------
app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});
