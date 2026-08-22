# ZA量化 桌面端（Electron）

把浏览器里的行情界面装进独立窗口。**前端代码零改动**，Electron 只是换了个"壳"。

```
Electron 窗口（渲染进程：static/index.html）
        │  fetch / WebSocket
        ▼
Python 服务（127.0.0.1:8000，天勤数据 + 决策引擎）
```

## 运行

前置：先启动后端服务（另开一个终端）：

```powershell
cd 项目根目录
python launcher.py
```

然后启动桌面端：

```powershell
cd electron
npm install        # 首次需要（国内网络请先：npm config set registry https://registry.npmmirror.com）
npm start
```

> 如果 `npm install` 报错或 electron 二进制没下载，执行：
> ```powershell
> $env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"
> node node_modules/electron/install.js
> ```

## 打包成独立 exe（进阶，可选）

用 electron-builder：

```powershell
npm install --save-dev electron-builder
npx electron-builder --win portable
```

产物在 `dist/`。注意：打包只包含"壳"，Python 服务仍需单独启动——
真正的"一键绿色版"需要把 Python 服务也打包进去（第 2 阶段再讲）。

## 学习导航

代码教学文档在项目 `docs/` 目录，按顺序读：

1. `01-架构总览.md` —— 前后端接口到底是什么
2. `02-Electron入门.md` —— 主进程/渲染进程
3. `03-接口调用教学.md` —— fetch / WebSocket 从零讲
4. `04-前端三件套.md` —— HTML/CSS/JS 怎么配合
5. `05-实战：从零写行情卡片.md` —— 动手写一个完整功能
6. `06-现有代码导读.md` —— 逐段读懂项目现有代码
