# ZA量化 桌面端（Electron）

Electron 是 Web UI 的桌面壳。它通过 HTTP/WebSocket 访问 Python 后端，不负责实现行情协议本身。

```text
Electron 窗口
    │ fetch / WebSocket
    ▼
Python FastAPI（127.0.0.1:8000）
    │
    └─ tqdiff DIFF 客户端 → 天勤行情服务
```

## 运行

当前 `main.js` 会自动探测后端：

- 已有同路线后端运行：直接复用；
- 没有后端运行：按候选链自动选择 Python 解释器启动 `launcher.py`
  （① 项目 `.venv/Scripts/python.exe` → ② `py -3` → ③ 系统 PATH `python`），
  等待服务就绪后打开窗口；实际选中的命令会写入日志；
- 8000 端口被另一条路线占用：显示端口冲突页面。

```powershell
cd electron
npm install
npm start
```

**后端日志与诊断**：后端 stdout/stderr 追加写入 `electron/backend.log`；
启动失败时，错误页会直接展示最近 40 行日志尾部、实际尝试的命令与日志文件路径。
若启动失败，也可先在项目根目录单独运行：

```powershell
python -m pip install -r requirements.txt
python launcher.py
```

然后再执行 `npm start`，这样可以直接看到 Python 后端的错误输出。

## 测试

```powershell
cd electron
npm test          # 后端探测纯逻辑单测（解释器候选链、日志尾部缓冲），秒级
npm run test:gui  # Playwright 窗口级 GUI 测试：端口冲突页 / 自动拉起后端加载主界面 / 关窗清理后端
```

GUI 测试用 `playwright-core` 的 `_electron` 驱动**真实 Electron 窗口**（无需下载浏览器内核）：
场景 2 会真实拉起 python 后端（用 config.json 账号连天勤，只读行情）；
若测试前 8000 已有本路线后端在跑，则只验证"复用"并跳过清理断言。

## 打包限制

```powershell
npm install --save-dev electron-builder
npx electron-builder --win portable
```

Electron 打包默认只包含桌面壳，**不会自动把 Python 后端和行情依赖打进安装包**。要做真正的一键绿色版，需要额外整合 PyInstaller 后端、资源路径、配置文件和进程生命周期，并进行安装后测试。

## 当前已知问题

- 后端探测逻辑已有 3 组单元测试，窗口级 GUI 测试已覆盖端口冲突、自动拉起后端和关闭窗口后的后端清理。
- Electron 打包仍只包含桌面壳；把 Python 后端、依赖与资源打成真正的一键安装包仍需单独设计和验收。

完整项目检查结果见 [`../docs/项目检查报告-2026-09-11.md`](../docs/项目检查报告-2026-09-11.md)。
