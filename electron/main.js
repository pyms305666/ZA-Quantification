/* =====================================================================
 * ZA量化 桌面端 —— 主进程入口（main.js）
 * =====================================================================
 *
 * 【先理解两个词，后面所有的都围绕它们】
 *
 * 1. "主进程"（Main Process）：
 *    - 就是运行 Node.js 的这个文件（main.js）
 *    - 负责：创建窗口、控制应用生命周期、访问操作系统能力
 *
 * 2. "渲染进程"（Renderer Process）：
 *    - 就是窗口里显示的那个网页（我们的 static/index.html）
 *    - 负责：画界面、和用户交互
 *    - 它在窗口里其实就是一个"迷你浏览器"，所以 HTML/JS/CSS 都能用
 *
 * 关系图：
 *   Electron 应用
 *   ├── 主进程（main.js，Node.js 世界）
 *   │     └── 创建窗口 ↓
 *   └── 渲染进程（index.html + app.js，浏览器世界）
 *          └── 通过 HTTP / WebSocket 访问 ↓
 *              Python 服务（127.0.0.1:8000，天勤数据 + 决策引擎）
 *
 * 也就是说：Electron 只是把"浏览器"换成了一个"可以自己控制的窗口"，
 * 前后端通信（fetch / WebSocket）和之前浏览器里完全一样，一行不用改。
 * ===================================================================== */

// ---------------------------------------------------------------------
// 第一步：引入需要的模块
// ---------------------------------------------------------------------
// require 是 Node.js 的"导入"语法（类似 Python 的 import）
//  - app          ：整个应用对象（控制窗口创建/退出）
//  - BrowserWindow：用来创建窗口的类
const { app, BrowserWindow } = require("electron");
const path = require("path");

// Python 服务跑在本机 8000 端口，这就是我们的"后端地址"
// 前端页面（渲染进程）就是通过这个地址请求数据的
const SERVER_URL = "http://127.0.0.1:8000";

// ---------------------------------------------------------------------
// 第二步：定义一个"探测后端是否活着"的函数
// ---------------------------------------------------------------------
// 目的：如果用户没先启动 Python 服务就打开桌面端，我们要给出友好提示
// 这就是一个最简单的"前端调后端"例子：
//   fetch(地址)  = 向那个地址发一个 HTTP 请求
//   resp.ok      = 响应是否成功（200 之类的）
// 注意：fetch 是异步的，所以函数前面要写 async，里面用 await 等待结果
async function isServerUp() {
  try {
    const resp = await fetch(SERVER_URL + "/api/v1/status");
    return resp.ok;
  } catch (error) {
    // 服务没启动时，fetch 会抛异常（连不上），我们捕获后返回 false
    return false;
  }
}

// ---------------------------------------------------------------------
// 第三步：等 Electron 初始化完成，然后创建窗口
// ---------------------------------------------------------------------
// app.whenReady() 返回一个 Promise（异步），完成后执行 .then 里的回调
// 相当于 Python 里的：await 应用就绪 然后 创建窗口
app.whenReady().then(async () => {
  // ---------- 3.1 创建窗口 ----------
  const win = new BrowserWindow({
    width: 1440,            // 窗口宽（像素）
    height: 900,            // 窗口高
    minWidth: 1180,         // 最小宽（防止用户把窗口拖得太小界面挤坏）
    minHeight: 760,         // 最小高
    title: "ZA量化",        // 窗口标题
    icon: path.join(__dirname, "..", "assets", "icon.png"),  // 窗口图标
    autoHideMenuBar: true,  // 隐藏默认菜单栏（File/Edit 那些），更像正经软件
    webPreferences: {
      // 安全设置（标准 Electron 推荐写法，先不用深究）：
      nodeIntegration: false,  // 渲染进程里不能用 Node 的 require
      contextIsolation: true,  // 隔离渲染进程的全局环境
    },
  });

  // ---------- 3.2 探测服务，再决定加载什么 ----------
  if (await isServerUp()) {
    // 服务在跑：直接加载我们的前端页面（渲染进程的世界）
    // 之后页面上所有的 fetch / WebSocket 都会去访问 SERVER_URL
    await win.loadURL(SERVER_URL);
  } else {
    // 服务没跑：加载一个内置的"提示页"（data: 开头 = 直接在内存里的网页）
    // 这里顺便演示了前端最基本的东西：一段 HTML + 一段 JS
    await win.loadURL(
      "data:text/html;charset=utf-8," +
        encodeURIComponent(`<!DOCTYPE html>
<html lang="zh-CN">
<body style="font-family:'Microsoft YaHei UI';text-align:center;padding-top:120px;color:#555;">
  <h2 style="color:#333;">ZA量化</h2>
  <p>后端服务未启动</p>
  <p style="font-size:13px;color:#999;">
    请先在项目目录运行 <b>python launcher.py</b>，<br>
    然后关闭本窗口重新打开。
  </p>
</body>
</html>`)
    );
  }

  // ---------- 3.3 锁定标题 ----------
  // 页面里的 <title> 会覆盖窗口标题，这里强制保持"ZA量化"
  win.on("page-title-updated", (event) => {
    event.preventDefault(); // 阻止页面改标题
    win.setTitle("ZA量化"); // 我们手动设置
  });

  // ---------- 3.4 Mac 特殊处理（可以跳过，Windows 用不到） ----------
  app.on("activate", () => {
    // macOS 上点击 Dock 图标、且没有窗口时，重新创建一个窗口
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

// ---------------------------------------------------------------------
// 第四步：窗口全关时退出应用
// ---------------------------------------------------------------------
// 规则：Windows/Linux 上窗口全关 = 退出程序；macOS 习惯是保留在 Dock
app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});
