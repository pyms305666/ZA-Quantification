/* =====================================================================
 * backend.test.js —— 后端探测逻辑单元测试（node 直接运行，无需 Electron）
 * 运行：npm test   （等价于 node test/backend.test.js）
 * ===================================================================== */

const assert = require("assert");
const path = require("path");
const { resolvePythonCandidates, createTailBuffer } = require("../backend");

let passed = 0;

// ---------- 1) 项目里有 .venv：venv 候选排第一，py/python 仍保留为兜底 ----------
{
  const exists = (p) => p.includes(".venv");   // 假的 existsSync：只有 venv "存在"
  const candidates = resolvePythonCandidates("P", "L", exists);
  assert.strictEqual(candidates[0].source, "venv");
  assert.strictEqual(candidates[0].cmd, path.join("P", ".venv", "Scripts", "python.exe"));
  assert.deepStrictEqual(candidates[0].args, ["L"]);
  assert.strictEqual(candidates.length, 3);   // venv + py -3 + PATH python 兜底
  assert.strictEqual(candidates[1].cmd, "py");
  assert.strictEqual(candidates[2].cmd, "python");
  passed += 1;
  console.log("ok 1 - venv 存在时排第一，后续兜底保留");
}

// ---------- 2) 没有 venv：py -3 → PATH python，依次兜底 ----------
{
  const candidates = resolvePythonCandidates("P", "L", () => false);
  assert.strictEqual(candidates.length, 2);
  assert.strictEqual(candidates[0].cmd, "py");
  assert.deepStrictEqual(candidates[0].args, ["-3", "L"]);
  assert.strictEqual(candidates[1].cmd, "python");
  assert.deepStrictEqual(candidates[1].args, ["L"]);
  passed += 1;
  console.log("ok 2 - 无 venv 时 py -3 → python 兜底");
}

// ---------- 3) 尾部缓冲：容量裁剪 + 取文本 ----------
{
  const buf = createTailBuffer(3);
  for (const line of ["a", "b", "c", "d"]) buf.push(line);
  assert.strictEqual(buf.text(), "b\nc\nd");   // 容量 3：最旧的 a 被丢弃
  buf.clear();
  assert.strictEqual(buf.text(), "");
  passed += 1;
  console.log("ok 3 - 尾部缓冲容量裁剪与清空");
}

console.log(`\n共 ${passed} 组断言全部通过`);
