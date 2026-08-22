# 第 2 章：Electron 入门 —— 把"浏览器"装进"窗口"

> 这一章讲清楚：Electron 是什么、我们用它干什么、它的两个"世界"。
> 学完你应该能看懂 `electron/main.js` 里每一行在干嘛。

## 2.1 Electron 是什么？

一句话：**Electron 是一个"用网页技术做桌面软件"的框架**。

- 平时你在浏览器里打开网页（HTML/CSS/JS）——那是"浏览器窗口"
- 用 Electron，你可以把同样的网页装进一个**独立的、没有地址栏的窗口**——那就是"软件"

市面上很多软件都是这么做的：VS Code、飞书、Slack、网易云音乐（一部分）……

```
浏览器打开网页：  [地址栏][标签栏] ── 网页 ──
Electron 软件：   [独立窗口，没有地址栏] ── 网页 ──
```

对我们来说最大的好处：**前端的 HTML/CSS/JS 一行不用改**，换个"壳"就是软件了。

## 2.2 Electron 的两个世界

Electron 应用里有两种代码，它们住在不同的"世界"：

```
┌─────────────────────────────────────────────┐
│  Electron 应用                               │
│                                              │
│  【主进程】main.js（Node.js 世界）              │
│   · 创建窗口                                  │
│   · 控制软件启动/退出                          │
│   · 能调用操作系统（读写文件等）                │
│              │  创建窗口                      │
│              ▼                               │
│  【渲染进程】index.html + app.js（浏览器世界）   │
│   · 画界面                                    │
│   · 处理点击/滚动/输入                         │
│   · 通过 fetch/WS 访问后端                    │
│                                              │
└─────────────────────────────────────────────┘
        渲染进程 ──fetch/WS──► Python 服务(8000)
```

记法：**主进程管"壳"，渲染进程管"内容"**。

## 2.3 逐行看懂 electron/main.js

我们写好的 `electron/main.js` 就干四件事。我挑关键几行讲：

### ① 引入模块（类似 Python 的 import）

```js
const { app, BrowserWindow } = require("electron");
const path = require("path");
```

- `app`：整个应用（什么时候就绪、什么时候退出）
- `BrowserWindow`：创建窗口的"模板"（类）
- `require` = Node.js 的 import（Python 里是 `import`）

### ② 探测后端是否活着

```js
async function isServerUp() {
  try {
    const resp = await fetch(SERVER_URL + "/api/v1/status");
    return resp.ok;
  } catch (error) {
    return false;
  }
}
```

这里出现了三个"前端/Node 基础语法"，必须搞懂：

| 语法 | 是什么 | 类比 Python |
|---|---|---|
| `async function` | 声明一个"异步函数" | 类似 `async def` |
| `await` | 等一个异步操作完成 | 类似 `await`（asyncio） |
| `try/catch` | 捕获错误 | 就是 `try/except` |

`fetch(地址)` 返回一个"承诺"（Promise），`await` 会等它完成。
如果地址连不上（服务没启动），fetch 会抛异常，`catch` 接住，返回 `false`。

### ③ 创建窗口并加载页面

```js
const win = new BrowserWindow({ width: 1440, height: 900, ... });
```

`new` = 用模板造一个实例（Python 里是调用类的构造方法）。

```js
await win.loadURL(SERVER_URL);   // 加载 http://127.0.0.1:8000 的页面
```

`loadURL` = 让窗口里的"迷你浏览器"去访问这个网址。
**这一步之后，渲染进程就活了**——`static/index.html` 开始执行，里面的 JS 开始调后端接口。

## 2.4 怎么跑起来（开发流程）

```
第一步：启动后端（Python 服务）
    python launcher.py

第二步：启动 Electron 壳（另一个终端）
    cd electron
    npm start
```

你会看到一个 1440x900 的独立窗口"ZA量化"——里面的内容就是我们的行情界面。

## 2.5 小知识：为什么选 Electron 而不是 Tauri？

| | Electron | Tauri |
|---|---|---|
| 你要学的语言 | 只有 JS/HTML/CSS | 前端 JS + **后端 Rust** |
| 安装环境 | 只要 Node.js（你已有） | 要装 Rust 工具链（大） |
| 成品体积 | 100MB+ | 10MB |
| 教程/社区 | 极多 | 较少 |
| 对新手 | 友好 | 有门槛 |

对"学了一年软件工程、想先把东西跑起来"的你，Electron 是更平滑的路。

---

**这一章的作业**：
1. 打开 `electron/main.js`，把每一行注释读一遍，然后默写一遍结构（不用背代码，背"四件事"：引入→探测→建窗→退出）
2. 启动后端后，在 `electron` 目录跑 `npm start`，看到独立窗口就算成功
