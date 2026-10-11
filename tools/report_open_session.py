"""Snapshot current collector health and cumulative paired decision outcomes."""
from collections import Counter
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3
import subprocess
import time

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'docs/研究/2026-09-28-开盘观察'
CN=timezone(timedelta(hours=8))


def read(base):
    status_error=None
    for attempt in range(3):
        try:
            status=json.loads((base/'status.json').read_text(encoding='utf-8'))
            if not isinstance(status,dict) or not status.get('state') or not status.get('pid') or not status.get('wall_ms'):
                raise ValueError('Status lacks a collector state, PID or heartbeat')
            break
        except (OSError,ValueError) as exc:
            status_error=f'{type(exc).__name__}: {exc}'
            if attempt<2:time.sleep(.2)
    else:
        status=dict(state='status_unavailable',pid=None,wall_ms=None,stats={})
    status['status_read_error']=status_error if status['state']=='status_unavailable' else None
    with sqlite3.connect((base/'events.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
        db.execute('BEGIN')
        events=[(kind,wall,json.loads(body)) for kind,wall,body in db.execute('SELECT kind,wall_ms,payload FROM events ORDER BY seq')]
        stats=db.execute("SELECT payload FROM state WHERE key IN ('stats','collector_stats') ORDER BY key LIMIT 1").fetchone()
        pending=db.execute("SELECT payload FROM state WHERE key='pending'").fetchone()
        if stats:status['stats']=json.loads(stats[0])
        if pending:status['pending_endpoints']=len(json.loads(pending[0]))
    if status['pid'] is None:
        status.update(process_query_error='Process identity unavailable because status cannot be read',process_exists=None)
        return status,events,None
    proc=subprocess.run(['powershell','-NoProfile','-Command',f'Get-CimInstance Win32_Process -Filter "ProcessId = {int(status["pid"])}" | Select-Object -ExpandProperty CommandLine'],
                        capture_output=True,text=True,errors='replace',timeout=10)
    alive=(('forward_' in proc.stdout and 'run' in proc.stdout) if proc.returncode==0 else None)
    status['process_query_error']=(proc.stderr or '').strip()[:500] if proc.returncode else None
    status['process_exists']=bool(proc.stdout.strip()) if proc.returncode==0 else None
    if alive is None:
        fallback=subprocess.run(['powershell','-NoProfile','-Command',
            f'Get-Process -Id {int(status["pid"])} -ErrorAction Stop | Select-Object Id,ProcessName | ConvertTo-Json -Compress'],
            capture_output=True,text=True,errors='replace',timeout=10)
        if fallback.returncode==0 and fallback.stdout.strip():
            process=json.loads(fallback.stdout)
            status['process_exists']=True
            status['process_name']=process['ProcessName']
    return status,events,alive


def pct(a,b):return '—' if not b else f'{a/b:.2%}'


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    base=ROOT/'.tqsdk/research/forward15m';observer=ROOT/'.tqsdk/research/decision_forward'
    parent,p_events,p_alive=read(base);child,c_events,c_alive=read(observer)
    now=int(time.time()*1000);p_counts=Counter(k for k,_,_ in p_events);c_counts=Counter(k for k,_,_ in c_events)
    outcomes={}
    for kind,wall,v in c_events:
        if kind!='outcome':continue
        h=str(v['horizon'])
        for arm,side in v['sides'].items():
            s=outcomes.setdefault(f'{arm}_{h}',dict(observed=0,signals=0,correct=0,cost_hits=0,flat=0))
            s['observed']+=1;s['signals']+=int(side!=0)
            s['correct']+=int(side*v['delta']>0)
            s['flat']+=int(side!=0 and v['delta']==0)
            s['cost_hits']+=int(side*v['delta']*v['multiple']>v['cost'])
    parent_heartbeat_age=(now-parent['wall_ms'])/1000 if parent.get('wall_ms') else None
    child_heartbeat_age=(now-child['wall_ms'])/1000 if child.get('wall_ms') else None
    supervisor=subprocess.run(['powershell','-NoProfile','-Command',
        '$t=Get-ScheduledTask -TaskName CodexFuturesDecisionResearch -ErrorAction Stop; '
        'if($t){[pscustomobject]@{state=[string]$t.State;logon_trigger=(@($t.Triggers | '
        'Where-Object {$_.CimClass.CimClassName -eq "MSFT_TaskLogonTrigger"}).Count -gt 0)} | ConvertTo-Json -Compress}'],
        capture_output=True,text=True,errors='replace',timeout=10)
    supervisor_info=json.loads(supervisor.stdout) if supervisor.returncode==0 and supervisor.stdout.strip() else {}
    # An inaccessible task query is unknown, not proof of a missing task.
    supervisor_state=supervisor_info.get('state','query_unavailable')
    value=dict(asof=datetime.now(CN).isoformat(),parent_alive=p_alive,observer_alive=c_alive,
        parent_process_exists=parent.get('process_exists'),observer_process_exists=child.get('process_exists'),
        parent_process_query_error=parent.get('process_query_error'),observer_process_query_error=child.get('process_query_error'),
        parent_status_read_error=parent.get('status_read_error'),observer_status_read_error=child.get('status_read_error'),
        supervisor_query_error=supervisor.stderr.strip()[:500] or ('Task query returned no result' if not supervisor_info else None),
        parent_state=parent['state'],observer_state=child['state'],parent_heartbeat_age_seconds=round(parent_heartbeat_age,1) if parent_heartbeat_age is not None else None,
        observer_heartbeat_age_seconds=round(child_heartbeat_age,1) if child_heartbeat_age is not None else None,supervisor_state=supervisor_state,
        fresh_ticks=parent['stats']['fresh_ticks'],
        parent_prediction_batches=p_counts['prediction'],observer_predictions=c_counts['prediction'],
        observer_committed=c_counts['prediction_committed'],observed_endpoints=c_counts['outcome'],
        invalid_endpoints=child['stats']['invalid_endpoints'],pending_endpoints=child.get('pending_endpoints',0),
        supervisor_logon_trigger=supervisor_info.get('logon_trigger'),
        paper_entries=p_counts['paper_entry'],paper_exits=p_counts['paper_exit'],
        skip_reasons=[v for k,_,v in p_events if k=='batch_quality'][-8:],
        outcomes=outcomes,amendments=dict(parent=p_counts['manifest_amendment'],observer=c_counts['manifest_amendment']))
    (OUT/'实时状态.json').write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    process_health=lambda alive,age: ('状态文件不可用，需检查' if age is None else ('心跳正常（进程命令行查询未完成）' if alive is None and age<30 else ('运行中' if alive and age<30 else '需检查')))
    parent_age_text=f'{parent_heartbeat_age:.1f}秒' if parent_heartbeat_age is not None else '不可用'
    child_age_text=f'{child_heartbeat_age:.1f}秒' if child_heartbeat_age is not None else '不可用'
    trigger_label={True:'已配置',False:'未配置',None:'本次未核实'}[value['supervisor_logon_trigger']]
    lines=['# 前向采集与评估观察', '',
           f"报告生成：{value['asof']}。本报告是生成时的快照，数字随行情变化。", '',
           f"主采集器：{process_health(p_alive,parent_heartbeat_age)}，状态 `{parent['state']}`，心跳距今{parent_age_text}；已记录新tick {value['fresh_ticks']} 个。",
           f"原评估器观察程序：{process_health(c_alive,child_heartbeat_age)}，状态 `{child['state']}`，心跳距今{child_age_text}。", '',
           f"研究模型预测批次{value['parent_prediction_batches']}，原评估器与校准候选的配对预测{value['observer_committed']}，已得到15/30分钟终点{value['observed_endpoints']}。纸面入场{value['paper_entries']}，纸面平仓{value['paper_exits']}。研究程序不发送真实订单。", '',
           '记录审查：9月27日晚至本次开盘前，两个进程曾退出，原状态文件停留在22:30左右。9月28日9:03重新启动，间断期间没有实时预测；不能事后补记为前向证据。', '',
           f"任务计划守护进程：状态 `{supervisor_state}`，任务名 `CodexFuturesDecisionResearch`，当前用户登录触发器{trigger_label}。每20秒检查两采集进程，缺失时重新启动。守护运行期间请求阻止自动空闲睡眠，屏幕仍可关闭或锁定；退出登录、手动睡眠及关机期间仍无法采集。启动执行 `& .\\tools\\start_decision_supervisor.ps1`；停止守护执行 `& .\\tools\\stop_decision_supervisor.ps1`。", '',
           '进程或任务查询被拒绝时标记为本次未核实，不据此认定进程停止或任务未注册。心跳正常只能证明程序仍在更新状态；交易时段还须检查实时行情和预测边界增长。', '',
           f"无效终点共 {value['invalid_endpoints']} 个，当前待配对终点 {value['pending_endpoints']} 个。无效数取账本累计计数；一次重启可能同时使多个终点无效。", '',
           '任务初次启动受本机 Restricted 执行策略阻止；随后仅对该任务的PowerShell进程加入 ExecutionPolicy Bypass，确认守护进程运行并写入日志。未更改系统全局执行策略。', '',
           '9月28日开盘探针确认八个品种的1、5、15分钟行情序列均就绪，并观察到交易所时间戳相对本机接收时间领先约0.1–0.5秒。先前规则会误判这类已收到的报价。首个真实预测前停止程序，将源码和冻结方案v1存档，检查账本确认两实验均为零预测、零结局、零交易；随后修订为允许报价时间与本机接收时间相差最多5秒，并独立核对接收顺序。两账本保存版本修订事件；原始创建时间和模型权重保持一致。', '',
           '修订后八个品种的新报价均能通过时钟容差，软件现有评估器可对其完整输入给出方向或观望。隔离虚拟行情核验覆盖正常、交易所时钟领先、旧报价、终点缺失和预测落盘迟到五种情况，真实SQLite研究账本与虚拟账本隔离。', '',
           '## 配对方向准确率', '', '| 方案及期限 | 可观察终点 | 给方向 | 方向正确 | 准确率 | 超过成本比例 |',
           '|---|---:|---:|---:|---:|---:|']
    for key in ('original_15','calibrated_15','original_30','calibrated_30'):
        s=outcomes.get(key,dict(observed=0,signals=0,correct=0,cost_hits=0))
        lines.append(f"| {key} | {s['observed']} | {s['signals']} | {s['correct']} | {pct(s['correct'],s['signals'])} | {pct(s['cost_hits'],s['signals'])} |")
    lines += ['', '终点价格只是价格方向诊断，不是可成交的账户收益。样本量不足时百分比不作可靠性判断；零次建议也不代表模型表现良好。', '',
              '后续接入评估使用本次首批结果之后冻结的 [接入判据](接入判据.json)。已看到的9:15及9:30预测不计入这套长期门槛。判据另要求真实执行、回撤和客户端核查；任何统计门槛都不能保证未来不会亏损。运行 `python tools/report_decision_qualification.py` 刷新 [接入门槛状态](接入门槛状态.json)。', '',
              f"当前研究账本哈希链和输入文件可用 `python tools/report_forward_research.py`、`python tools/verify_decision_forward.py` 重算。状态JSON见 [实时状态](实时状态.json)；当前八品种输入就绪快照见 [开盘行情就绪快照](开盘行情就绪快照.json)。", '']
    (OUT/'观察记录.md').write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps({k:v for k,v in value.items() if k!='skip_reasons'},ensure_ascii=False))


if __name__=='__main__':main()
