"""ZA量化 · 开盘实盘验证套件（P0 真实行情 / K线实时性 / 200ms 延迟 / 手机端）。

周一（或任意交易时段）开盘后，一条命令拿齐全部验收数据：

  python tools/live_verify.py preflight                     # 0. 开跑前体检（后端/连接/目录/交易时段）
  python tools/live_verify.py all --symbol SHFE.rb2610      # 1-5 全流程 + 生成证据报告（约 12 分钟）
  python tools/live_verify.py kline-live --minutes 3        # 单跑 K 线实时性（快速抽查）
  python tools/live_verify.py phone                         # 手机端（USB 连接真机后）：后端/K线/logcat/截图

证据落盘：docs/验证记录/<日期>/（原始 JSON + Markdown 报告），可直接回填 docs/待办与验收计划。
依赖：requests、websockets（均在 requirements.txt）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_ROOT = ROOT / "docs" / "验证记录"
DEFAULT_ADB_CANDIDATES = (
    "adb",
    r"E:\android-sdk\platform-tools\adb.exe",
)

PERIODS = {"60": "1分钟", "300": "5分钟", "900": "15分钟", "3600": "60分钟", "86400": "日线"}


# ---------------------------------------------------------------- 基础工具

def out_dir(tag: str = "") -> Path:
    d = EVIDENCE_ROOT / dt.datetime.now().strftime("%Y-%m-%d")
    if tag:
        d = d / tag
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_json(name: str, data) -> Path:
    p = out_dir() / name
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  [证据] {p.relative_to(ROOT)}")
    return p


def get_json(base: str, path: str, timeout: float = 30.0):
    r = requests.get(base + path, timeout=timeout)
    r.raise_for_status()
    return r.json()


def is_trading_time(now: dt.datetime | None = None) -> tuple[bool, str]:
    """粗判交易时段（国内期货：日盘 9:00-10:15/10:30-11:30/13:30-15:00，夜盘 21:00-23:00）。

    只做提示不做拦截——夜盘品种/日盘细节有差异，最终以真实行情是否流动为准。
    """
    now = now or dt.datetime.now()
    wd = now.weekday()   # 0=周一
    t = now.hour * 60 + now.minute
    if wd >= 5:
        return False, "周末休市"
    day = (540 <= t <= 615) or (630 <= t <= 690) or (810 <= t <= 900)
    night = (wd <= 4 and 1260 <= t <= 1385) or (wd >= 1 and wd <= 5 and t <= 60)  # 夜盘 21:00-23:00（跨零点粗判）
    if day or night:
        return True, "交易时段内"
    return False, "非交易时段（日盘 9:00-11:30/13:30-15:00，夜盘 21:00-23:00）"


# ---------------------------------------------------------------- 子命令

def cmd_preflight(args) -> bool:
    """开跑前体检：后端可达、天勤已连接、目录就绪、交易时段提示。"""
    print("== Preflight ==")
    ok = True
    try:
        st = get_json(args.url, "/api/v1/status", timeout=5)
    except Exception as e:
        print(f"  ✗ 后端不可达：{e}")
        print("    → 先启动后端：python launcher.py（或 dist 下的 exe / cd electron && npm start）")
        return False
    print(f"  路由: {st.get('route')} | 账号: {st.get('account') or '--'}")
    print(f"  天勤连接: {'✓' if st.get('connected') else '✗ 连接中/失败（error=' + str(st.get('error')) + '）'}")
    ok &= bool(st.get("connected"))
    print(f"  合约目录: ready={st.get('catalog_ready')} loading={st.get('catalog_loading')} "
          f"进度={st.get('catalog_progress')} 已收录={st.get('futures_count')}")
    trading, hint = is_trading_time()
    print(f"  交易时段: {hint}")
    if st.get("catalog_loading"):
        print("  ⚠ 目录仍在后台下载——K线/行情不受影响，搜索暂不全；可先跑行情类验证")
    print("== Preflight " + ("通过 ✓" if ok else "未通过 ✗") + " ==")
    return ok


def cmd_quote(args) -> dict:
    """P0：真实报价五档字段验收。"""
    print(f"== 报价验收 {args.symbol} ==")
    d = get_json(args.url, f"/api/v1/quote/{args.symbol}")
    q = d.get("data") or {}
    fields = ["last", "volume", "open_interest", "pre_close"]
    missing = [f for f in fields if q.get(f) is None]
    bid = (q.get("bid") or [{}])[0]
    ask = (q.get("ask") or [{}])[0]
    ok = not missing and bid.get("price") is not None and ask.get("price") is not None
    print(f"  最新价={q.get('last')} 量={q.get('volume')} 持仓={q.get('open_interest')} "
          f"买一={bid.get('price')}×{bid.get('volume')} 卖一={ask.get('price')}×{ask.get('volume')}")
    print(f"  判定: {'✓ 五档/关键字段齐全' if ok else '✗ 缺字段: ' + ','.join(missing)}")
    save_json("quote.json", {"symbol": args.symbol, "ok": ok, "quote": q})
    return d


def cmd_kline_history(args) -> dict:
    """P0：五个周期历史 K 线验收（非空/时间升序/OHLC 有效/字段齐全）。"""
    print("== 历史 K 线验收（5 周期）==")
    result = {}
    all_ok = True
    for period, name in PERIODS.items():
        try:
            d = get_json(args.url, f"/api/v1/kline/{args.symbol}?period={period}&count=200")
        except Exception as e:
            print(f"  {name:<5} ✗ 请求失败: {e}")
            result[name] = {"ok": False, "error": str(e)}
            all_ok = False
            continue
        bars = d.get("bars") or []
        dts = [b.get("datetime") for b in bars]
        ordered = dts == sorted(dts)
        valid = all(
            b.get("open") is not None and b.get("high") is not None and
            b.get("low") is not None and b.get("close") is not None and b.get("volume") is not None
            for b in bars)
        ok = len(bars) > 0 and ordered and valid
        all_ok &= ok
        last = bars[-1] if bars else {}
        print(f"  {name:<5} 根数={len(bars):<4} 升序={'✓' if ordered else '✗'} "
              f"字段有效={'✓' if valid else '✗'} 最后一根 close={last.get('close')}")
        result[name] = {"ok": ok, "count": len(bars), "bars": bars}
    print(f"  判定: {'✓ 五周期全部非空有效' if all_ok else '✗ 存在异常（看上面/HISTORY 诊断日志）'}")
    save_json("kline_history.json", {"symbol": args.symbol, "all_ok": all_ok,
                                     "periods": {k: {kk: vv for kk, vv in v.items() if kk != "bars"}
                                                 for k, v in result.items()}})
    return result


def cmd_kline_live(args) -> dict:
    """P0：K 线实时性验收——连续采样，验证"形成中"K 线会变化、新 K 线会出现。

    支持多周期同跑（--periods 60 300 86400），**逐周期独立判定**——
    周期无关性是 K 线实时性修复（增量合并）的核心保证，周一验收按周期留证。

    判定逻辑（区分"无新行情"与"系统停滞"，对应验收标准）：
      - 行情活跃（行情时间戳/成交量在变）且该周期 K 线末根有变化 → PASS
      - 行情活跃但该周期 K 线始终不变 → FAIL（推送/合并/解析停滞，正是要抓的 bug）
      - 无新行情 → INCONCLUSIVE（休市/无成交，换个时段再测；周期性快照刷新不算行情）
    """
    periods = [str(p) for p in (getattr(args, "periods", None) or [getattr(args, "period", 60)])]
    names = [PERIODS.get(p, p) for p in periods]
    print(f"== K 线实时性 {args.symbol} 周期={names} 采样 {args.minutes} 分钟 ==")
    timeline = []
    t0 = time.time()
    while time.time() - t0 < args.minutes * 60:
        try:
            q = get_json(args.url, f"/api/v1/quote/{args.symbol}", timeout=15)
            quote = q.get("data") or {}
            rec = {"t": round(time.time() - t0, 1),
                   "wall": dt.datetime.now().strftime("%H:%M:%S"),
                   "quote_dt": quote.get("datetime"),
                   "quote_volume": quote.get("volume")}
            line = f"  [{rec['wall']}]"
            for p in periods:
                d = get_json(args.url, f"/api/v1/kline/{args.symbol}?period={p}&count=200", timeout=30)
                bars = d.get("bars") or []
                last = bars[-1] if bars else {}
                rec[f"P{p}"] = {"bars": len(bars),
                                "last_dt": last.get("datetime"),
                                "last_close": last.get("close"),
                                "last_volume": last.get("volume")}
                line += (f" | {PERIODS.get(p, p)}: close={rec[f'P{p}']['last_close']}"
                         f" vol={rec[f'P{p}']['last_volume']}")
            timeline.append(rec)
            print(line)
        except Exception as e:
            print(f"  [{dt.datetime.now():%H:%M:%S}] 采样异常: {e}")
        time.sleep(args.interval)

    # 行情是否真正活跃：行情时间戳或成交量在变（周期性快照刷新不算"新行情"）
    market_active = any(
        a["quote_dt"] != b["quote_dt"] or a["quote_volume"] != b["quote_volume"]
        for a, b in zip(timeline, timeline[1:]))

    # 逐周期判定
    per_period = {}
    for p in periods:
        key = f"P{p}"
        changes = sum(
            1 for a, b in zip(timeline, timeline[1:])
            if a[key]["last_close"] != b[key]["last_close"]
            or a[key]["last_dt"] != b[key]["last_dt"]
            or a[key]["last_volume"] != b[key]["last_volume"]
            or a[key]["bars"] != b[key]["bars"])
        if not timeline or len(timeline) < 3:
            v, r = "INCONCLUSIVE", "样本不足"
        elif market_active and changes > 0:
            v, r = "PASS", "行情活跃且该周期 K 线持续更新（实时性正常）"
        elif market_active:
            v, r = "FAIL", "行情活跃但该周期 K 线始终不变 → 推送/合并/解析停滞（配合 TQ_GATEWAY_DEBUG=1 定位）"
        else:
            v, r = "INCONCLUSIVE", "采样期间无新行情（休市/无成交）——换交易时段再测"
        per_period[PERIODS.get(p, p)] = {"period_sec": int(p), "verdict": v,
                                         "reason": r, "kline_changes": changes}
        print(f"  判定[{PERIODS.get(p, p)}]: {v} — {r}（K线变化 {changes} 次）")
    if any(v["verdict"] == "FAIL" for v in per_period.values()):
        overall = "FAIL"
    elif all(v["verdict"] == "PASS" for v in per_period.values()) and per_period:
        overall = "PASS"
    else:
        overall = "INCONCLUSIVE"
    print(f"  总判定: {overall}（行情活跃={market_active}）")
    data = {"symbol": args.symbol, "periods": periods, "minutes": args.minutes,
            "verdict": overall, "market_active": market_active,
            "per_period": per_period, "timeline": timeline}
    save_json("kline_live.json", data)
    return data


def cmd_latency(args) -> dict:
    """P0：200ms 端到端延迟采样（WS ts → 本工具收到；验收要求 ≥10 分钟）。"""
    print(f"== 延迟采样 {args.seconds}s ==")
    out = out_dir() / "latency.json"
    cmd = [sys.executable, str(ROOT / "tools" / "latency_probe.py"),
           "--url", args.url.replace("http", "ws") + "/ws/market",
           "--symbols", *args.symbols, "--seconds", str(args.seconds),
           "--out", str(out)]
    r = subprocess.run(cmd, cwd=str(ROOT))
    stats = {}
    if out.exists():
        stats = json.loads(out.read_text(encoding="utf-8")).get("stats", {})
        p95 = stats.get("p95_ms")
        if stats.get("count", 0) < 30:
            verdict = "INCONCLUSIVE"   # 样本不足（休市/无成交），不具统计意义
        elif p95 is not None and p95 <= 200:
            verdict = "PASS"
        else:
            verdict = "FAIL"
        print(f"  判定: {verdict} — p95={p95}ms 样本={stats.get('count')}（目标 ≤200ms 且样本 ≥30）")
        stats["verdict"] = verdict
        out.write_text(json.dumps({"stats": stats}, ensure_ascii=False, indent=2), encoding="utf-8")
    return stats


def _find_adb(args) -> str:
    override = getattr(args, "adb", "")
    if override:
        return override
    for c in DEFAULT_ADB_CANDIDATES:
        try:
            subprocess.run([c, "version"], capture_output=True, check=True)
            return c
        except Exception:
            continue
    raise SystemExit("找不到 adb：用 --adb 指定路径（如 E:\\android-sdk\\platform-tools\\adb.exe）")


def cmd_phone(args) -> dict:
    """P0：手机端验证（真机 USB 连接）——后端状态/K线实时/logcat/截图。

    通过 adb forward 把手机内 127.0.0.1:8000 映射到本机 18000 端口访问。
    `all` 流程调用时 args 可能没有 phone 专属参数，统一 getattr 兜底。
    """
    adb = _find_adb(args)
    print("== 手机端验证 ==")
    devs = subprocess.run([adb, "devices"], capture_output=True, text=True).stdout
    serials = [ln.split("\t")[0] for ln in devs.splitlines()
               if "\tdevice" in ln and not ln.startswith("*")]
    if not serials:
        print("  ✗ 无已连接设备（adb devices 为空）——手机开 USB 调试后重试")
        return {"ok": False}
    serial = getattr(args, "serial", "") or serials[0]
    print(f"  设备: {serial}")

    subprocess.run([adb, "-s", serial, "forward", "tcp:18000", "tcp:8000"], check=True)
    base = "http://127.0.0.1:18000"
    result = {"serial": serial, "base": base}

    # 1) 手机后端状态
    try:
        st = get_json(base, "/api/v1/status", timeout=8)
        result["status"] = st
        print(f"  手机后端: connected={st.get('connected')} catalog_ready={st.get('catalog_ready')} "
              f"quotes={st.get('quote_count')}")
    except Exception as e:
        print(f"  ✗ 手机后端不可达（App 是否已打开？）: {e}")
        return {**result, "ok": False}

    # 2) 手机 K 线实时性（两次采样间隔内末根变化 = 手机侧数据在动）
    sym = getattr(args, "symbol", "SHFE.rb2610")
    gap = getattr(args, "gap", 20.0)
    try:
        k1 = get_json(base, f"/api/v1/kline/{sym}?period=60&count=50", timeout=30)["bars"]
        print(f"  等待 {gap}s 后二次采样 …")
        time.sleep(gap)
        k2 = get_json(base, f"/api/v1/kline/{sym}?period=60&count=50", timeout=30)["bars"]
        changed = (len(k1) != len(k2)
                   or (k1 and k2 and (k1[-1].get("close") != k2[-1].get("close")
                                      or k1[-1].get("datetime") != k2[-1].get("datetime"))))
        result["kline_changed"] = bool(changed)
        print(f"  K线实时性: {'✓ 末根有变化（手机侧数据在更新）' if changed else '△ 两次采样无变化（休市或无成交时属正常）'}")
    except Exception as e:
        print(f"  ✗ K线采样失败: {e}")

    # 3) logcat + 截图证据
    log = subprocess.run([adb, "-s", serial, "logcat", "-d"], capture_output=True)
    lp = out_dir() / "phone_logcat.txt"
    lp.write_bytes(log.stdout)
    print(f"  [证据] {lp.relative_to(ROOT)}")
    shot = subprocess.run([adb, "-s", serial, "exec-out", "screencap", "-p"], capture_output=True)
    sp = out_dir() / "phone_screen.png"
    sp.write_bytes(shot.stdout)
    print(f"  [证据] {sp.relative_to(ROOT)}")
    subprocess.run([adb, "-s", serial, "forward", "--remove", "tcp:18000"], capture_output=True)
    save_json("phone.json", result)
    return result


def cmd_report(args) -> Path:
    """汇总当天证据目录为一份 Markdown 报告（可直接回填待办文件）。"""
    d = out_dir()
    files = sorted(d.glob("*.json"))
    lines = [f"# 实盘验证记录 · {d.name}", "",
             f"- 时间：{dt.datetime.now():%Y-%m-%d %H:%M:%S}",
             f"- 后端：{args.url}", ""]
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        lines.append(f"## {f.name}")
        lines.append("```json")
        lines.append(json.dumps(data if len(str(data)) < 4000
                                else {k: v for k, v in list(data.items()) if k != "timeline"},
                                ensure_ascii=False, indent=2)[:4000])
        lines.append("```")
        lines.append("")
    if (d / "latency.json").exists():
        lines.insert(3, f"- 延迟证据：latency.json（p95 目标 ≤200ms）")
    md = d / "报告.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    print(f"[报告] {md.relative_to(ROOT)}")
    return md


def cmd_all(args) -> None:
    print(f"=== ZA量化 实盘验证套件 · {dt.datetime.now():%Y-%m-%d %H:%M:%S} ===")
    if not cmd_preflight(args):
        print("\npreflight 未通过，终止（先启动后端并确认天勤已连接）。")
        sys.exit(2)
    cmd_quote(args)
    cmd_kline_history(args)
    cmd_kline_live(args)
    cmd_latency(args)
    try:
        cmd_phone(args)
    except SystemExit:
        print("  （无 adb/设备，跳过手机端）")
    except Exception as e:
        print(f"  （手机端跳过: {e}）")
    cmd_report(args)
    print("\n=== 完成：证据见 docs/验证记录/ ===")


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", default="http://127.0.0.1:8000", help="后端地址（手机端用 phone 子命令自带转发）")
    common.add_argument("--symbol", default="SHFE.rb2610")

    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("preflight", "quote", "kline-history"):
        sub.add_parser(name, parents=[common])
    al = sub.add_parser("all", parents=[common],
                        help="全流程：preflight→报价→五周期K线→K线实时性→延迟→手机端→报告")
    al.add_argument("--periods", nargs="+", default=["60", "300", "86400"],
                    help="K线实时性逐周期验收的周期秒数（默认 1分/5分/日线 三档覆盖）")
    al.add_argument("--minutes", type=float, default=3.0, help="K线实时性采样分钟")
    al.add_argument("--interval", type=float, default=10.0, help="K线采样间隔秒")
    al.add_argument("--seconds", type=float, default=600.0, help="延迟采样秒数（验收要求 ≥600）")
    al.add_argument("--symbols", nargs="+", default=["SHFE.rb2610", "SHFE.au2612", "DCE.m2609"],
                    help="延迟采样订阅的合约")
    kl = sub.add_parser("kline-live", parents=[common])
    kl.add_argument("--periods", nargs="+", default=["60"],
                    help="K线周期秒数列表（默认 60；可多周期同跑逐周期判定，如 --periods 60 300 86400）")
    kl.add_argument("--period", type=int, default=None, help="单周期简写（等价 --periods <p>）")
    kl.add_argument("--minutes", type=float, default=3.0)
    kl.add_argument("--interval", type=float, default=10.0)
    lat = sub.add_parser("latency", parents=[common])
    lat.add_argument("--seconds", type=float, default=600.0)
    lat.add_argument("--symbols", nargs="+", default=["SHFE.rb2610", "SHFE.au2612", "DCE.m2609"])
    ph = sub.add_parser("phone", parents=[common])
    ph.add_argument("--serial", default="", help="adb 设备序列号（默认取第一台）")
    ph.add_argument("--adb", default="", help="adb 路径（默认自动探测）")
    ph.add_argument("--gap", type=float, default=20.0, help="K线两次采样的间隔秒")
    rep = sub.add_parser("report", parents=[common])

    args = p.parse_args()
    if args.cmd == "preflight":
        cmd_preflight(args)
    elif args.cmd == "quote":
        cmd_quote(args)
    elif args.cmd == "kline-history":
        cmd_kline_history(args)
    elif args.cmd == "kline-live":
        cmd_kline_live(args)
    elif args.cmd == "latency":
        cmd_latency(args)
    elif args.cmd == "phone":
        cmd_phone(args)
    elif args.cmd == "report":
        cmd_report(args)
    elif args.cmd == "all":
        cmd_all(args)


if __name__ == "__main__":
    main()
