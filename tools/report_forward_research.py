"""Read a consistent journal snapshot; never connect to the market or place orders."""
import hashlib
import json
import sqlite3
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / '.tqsdk/research/forward15m'
OUT = ROOT / 'docs/研究/2026-09-27-新行情前向验证'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    cfg = json.loads((BASE / 'manifest.json').read_text(encoding='utf-8'))
    status = json.loads((BASE / 'status.json').read_text(encoding='utf-8'))
    errors, counts, predictions, commits, entries = [], Counter(), {}, {}, {}
    with sqlite3.connect((BASE / 'events.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        db.execute('BEGIN')
        rows = db.execute('SELECT * FROM events ORDER BY seq').fetchall()
        states = {k: json.loads(v) for k, v in db.execute('SELECT key,payload FROM state')}
    previous = '0' * 64
    for number, (seq, wall, kind, body, prev, digest) in enumerate(rows, 1):
        if seq != number or prev != previous or sha_bytes([seq, wall, kind, body, prev]) != digest:
            errors.append(f'journal chain: {seq}')
        previous = digest
        counts[kind] += 1
        value = json.loads(body)
        if kind == 'prediction':
            predictions[seq] = value
            for candidate in value['candidates']:
                path = BASE / candidate['input_file']
                if not path.is_file() or sha(path) != candidate['input_sha256']:
                    errors.append(f'input artifact: {seq}')
        elif kind == 'prediction_committed':
            ref = value['prediction_seq']
            if ref not in predictions or ref >= seq:
                errors.append(f'commit chronology: {seq}')
            commits[ref] = value['committed_ms']
        elif kind == 'paper_entry':
            ref = value['prediction_seq']
            if ref not in commits or value['entry_ms'] <= commits[ref] or not predictions.get(ref, {}).get('allow_entries'):
                errors.append(f'entry chronology: {seq}')
            entries[(value['arm'], ref)] = value
        elif kind == 'paper_exit':
            key = (value['arm'], value['prediction_seq'])
            if key not in entries or value['exit_ms'] < entries[key]['entry_ms']:
                errors.append(f'exit chronology: {seq}')
            entries.pop(key, None)
    for name, digest in cfg['source_sha256'].items():
        if not (ROOT / name).is_file() or sha(ROOT / name) != digest:
            errors.append(f'frozen source changed: {name}')
    for name, digest in cfg['model_sha256'].items():
        if sha(BASE / f'{name}.joblib') != digest:
            errors.append(f'frozen model changed: {name}')
    stats = states.get('collector_stats', {})
    days = sum(v >= cfg['qualifying_day_minimum_complete_batches'] for v in stats.get('complete_batches_by_day', {}).values())
    accounts = {}
    for arm, account in states.get('paper', {}).items():
        profit, loss = account['gross_profit'], account['gross_loss']
        factor = profit / loss if loss else None
        net = account['cash'] - cfg['account_per_arm']
        valid = account['trades'] - account['invalid_trades']
        checks = dict(days=days >= cfg['minimum_forward_days'], trades=valid >= cfg['minimum_valid_forward_trades'],
                      positive_net=net > 0, profit_factor=(factor >= cfg['minimum_profit_factor'] if factor is not None else profit > 0),
                      drawdown=account['max_drawdown'] <= cfg['max_drawdown'], execution=account['invalid_trades'] == 0,
                      flat=account['position'] is None and account['pending'] is None, integrity=not errors)
        accounts[arm] = dict(net=net, trades=account['trades'], valid_trades=valid, invalid_trades=account['invalid_trades'],
                             profit_factor=factor, zero_loss_positive_profit=loss == 0 and profit > 0,
                             max_drawdown=account['max_drawdown'], checks=checks,
                             numerical_gates_passed=all(checks.values()))
    result = dict(asof=datetime.now().astimezone().isoformat(), status=status['state'], pid=status['pid'],
                  heartbeat_age_seconds=round((time.time()*1000-status['wall_ms'])/1000, 1),
                  process_liveness='not checked by this report', qualifying_days=days, event_counts=dict(counts),
                  integrity_errors=errors, accounts=accounts, software_replacement_allowed=False,
                  note='Numerical gates alone do not authorize deployment; execution review and client validation remain required.')
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / '证据状态.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# 新行情前向验证状态', '', f"更新时间：{result['asof']}", '',
             f"采集状态：`{result['status']}`；心跳距报告生成 {result['heartbeat_age_seconds']} 秒。此报告为快照，不独立证明进程存活。", '',
             f"有效观察日：{days}/60；真实新预测批次（含不允许入场的部分批次）：{counts['prediction']}；纸面平仓：{counts['paper_exit']}。", '',
             '| 研究组 | 平仓/有效笔数 | 净收益（元） | 最大回撤 | 数值门槛 |', '|---|---:|---:|---:|---|']
    for arm, a in accounts.items():
        lines.append(f"| {arm} | {a['trades']}/{a['valid_trades']} | {a['net']:.2f} | {a['max_drawdown']:.2%} | {'通过，仍需审查' if a['numerical_gates_passed'] else '未通过'} |")
    lines += ['', f"账本、预测输入及冻结文件检查：{'通过' if not errors else '失败：' + '; '.join(errors)}。", '',
              '无效交易的亏损仍计入净收益；未平仓盈亏不计入已实现净收益，回撤包含盘口估值。利润因子和逐项门槛见证据状态.json。', '',
              '目前不满足软件替换条件。零成交不代表策略盈利或稳定。运行方法、费用和证据边界见 [运行说明](运行说明.md)。', '']
    (OUT / '前向验证状态.md').write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


def sha_bytes(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


if __name__ == '__main__':
    main()
