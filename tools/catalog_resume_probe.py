"""真服务器断点续传探针：对真实天勤 CDN 验证 206 + If-Range 续传全链路。

模拟场景（对应真机弱网"下载中断后再打开"）：
1. 先用 Range 取 identity 表示的前 N 字节（N=4096），连同响应 ETag 落盘
   ``symbol_file.json.part`` / ``.part.etag``（等价于上次下载中断的遗留状态）；
2. 调 ``auth.download_symbol_file``——因 .part 已存在，直接进入 identity 续传路径，
   预期：Range: bytes=N- + If-Range → 服务器 206 → 追加尾部 → 解析索引 → 清理状态；
3. 校验：返回目录非空、206 确证（包一层记录 status_code）、状态文件清理、pickle 索引落盘。

用法：``python tools/catalog_resume_probe.py [N]``（N 默认 4096，设为 0 则不预置直接全量）。
退出码 0 = 验证通过。identity 全量约 354MB，受服务器限速影响可能耗时较长。
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tqdiff import auth  # noqa: E402


def main() -> int:
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 4096
    cache_dir = Path(tempfile.mkdtemp(prefix="za-resume-probe-"))
    import os
    os.environ["TQ_GATEWAY_CACHE"] = str(cache_dir)

    part = cache_dir / "symbol_file.json.part"
    etag_file = Path(str(part) + ".etag")

    status_seen: list[int] = []
    real_get = auth.requests.get

    def recording_get(url, **kwargs):
        response = real_get(url, **kwargs)
        status_seen.append(response.status_code)
        return response

    auth.requests.get = recording_get  # type: ignore[assignment]

    if seed > 0:
        import requests
        head = requests.get(auth.SYMBOL_FILE_URL,
                            headers={"Accept-Encoding": "identity", "Range": f"bytes=0-{seed - 1}"},
                            timeout=(15, 60), stream=True)
        assert head.status_code == 206, f"预置分段请求预期 206，实得 {head.status_code}"
        etag = (head.headers.get("etag") or "").strip()
        assert etag, "服务器未返回 ETag，无法验证 If-Range"
        body = head.raw.read(seed, decode_content=False)
        assert len(body) == seed, f"预置分段读取不完整：{len(body)}/{seed}"
        part.write_bytes(body)
        etag_file.write_text(etag, encoding="utf-8")
        head.close()
        print(f"[probe] 预置 .part：{seed} 字节，ETag={etag[:24]}…")
    else:
        print("[probe] 不预置 .part，验证全量 identity 路径")

    started = time.time()
    symbols = auth.download_symbol_file("probe-token", progress=lambda done, total: None)
    elapsed = time.time() - started

    ok_state_cleared = not part.exists() and not etag_file.exists()
    index_exists = (cache_dir / "symbol_index.pkl").exists()
    expected_status = [206] if seed > 0 else [200]
    status_ok = status_seen == expected_status

    print(f"[probe] 请求状态码序列：{status_seen}（预期 {expected_status}）")
    print(f"[probe] 目录条目：{len(symbols)}；耗时 {elapsed:.1f}s")
    print(f"[probe] 状态清理：{'✓' if ok_state_cleared else '✗'}；索引落盘：{'✓' if index_exists else '✗'}")

    passed = len(symbols) > 10000 and ok_state_cleared and index_exists and status_ok
    print(f"[probe] 结论：{'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
