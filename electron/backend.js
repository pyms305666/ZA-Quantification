/* =====================================================================
 * backend.js —— 后端进程管理的"可测试纯逻辑" + 日志工具
 * =====================================================================
 * 从 main.js 拆出来的原因（见 docs/项目检查报告-2026-09-11.md P1/P2）：
 *   1. main.js 直接给 portBlockedBy 赋值（隐式全局变量）→ 状态残留、无法单测；
 *   2. Python 解释器写死 "python"，依赖系统 PATH，出错时无从诊断；
 *   3. stdio:"ignore" 丢弃后端输出，启动失败只剩"30 秒超时"，定位困难。
 *
 * 本模块只放"不依赖 Electron"的部分，可以用 node 直接单测：
 *   - resolvePythonCandidates(): 生成 Python 解释器候选链（依赖注入 existsSync，可测）
 *   - createTailBuffer():       保存最近 N 行日志（错误页直接展示，不用翻文件）
 * 日志文件路径、spawn/exit/error 处理留在 main.js（需要 Electron 运行时）。
 * ===================================================================== */

const path = require("path");
const fs = require("fs");

/**
 * 生成后端启动的 Python 解释器候选链（按优先级）。
 *
 * @param {string} projectDir  项目根目录（launcher.py 所在）
 * @param {string} launcherPath launcher.py 绝对路径
 * @param {(p: string) => boolean} existsSync 依赖注入：文件存在性检查（测试可传假函数）
 * @returns {{cmd: string, args: string[], source: string}[]}
 *          候选数组；spawn 时逐个尝试，ENOENT（找不到命令）就换下一个
 */
function resolvePythonCandidates(projectDir, launcherPath, existsSync = fs.existsSync) {
  const candidates = [];
  // ① 项目虚拟环境（最可靠：依赖一定装在这里，不依赖系统任何配置）
  const venvPython = path.join(projectDir, ".venv", "Scripts", "python.exe");
  if (existsSync(venvPython)) {
    candidates.push({ cmd: venvPython, args: [launcherPath], source: "venv" });
  }
  // ② Windows 官方安装包自带的 py 启动器：py -3 显式选 Python 3
  candidates.push({ cmd: "py", args: ["-3", launcherPath], source: "py-launcher" });
  // ③ 系统 PATH 里的 python（最后的兜底）
  candidates.push({ cmd: "python", args: [launcherPath], source: "path" });
  return candidates;
}

/**
 * 最近 N 行日志缓冲：错误页直接展示尾部内容，用户不必去翻日志文件。
 *
 * @param {number} capacity 最多保留多少行（超出丢最旧的）
 * @returns {{push(line: string), text(): string, clear()}}
 */
function createTailBuffer(capacity = 40) {
  const lines = [];
  return {
    push(line) {
      lines.push(line);
      while (lines.length > capacity) lines.shift();
    },
    text() {
      return lines.join("\n");
    },
    clear() {
      lines.length = 0;
    },
  };
}

module.exports = { resolvePythonCandidates, createTailBuffer };
