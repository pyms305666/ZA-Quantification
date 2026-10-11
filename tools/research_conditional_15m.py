"""A fixed conditional net-return experiment; no orders or app integration."""
import argparse
import csv
import gzip
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier,HistGradientBoostingRegressor
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss
from threadpoolctl import threadpool_limits
from research.intraday_profit import contract_examples
from research.conditional_intraday import CONTEXT_NAMES,contextual_features,fixed_outcomes,replay
from tools.research_15m_cost_features import augment,NAMES
from tools.research_15m_profit import save_json,summary

CACHE=ROOT/'.tqsdk/research/conditional15m'
OUT=ROOT/'docs/研究/2026-09-26-条件净收益模型'
SETTINGS=dict(max_iter=150,max_leaf_nodes=15,max_depth=4,min_samples_leaf=150,
    l2_regularization=20,learning_rate=.05,early_stopping=False,random_state=20260926,categorical_features=[21,27])


def timestamp(date):return int(pd.Timestamp(date,tz='Asia/Shanghai').timestamp()*1000)


def build(universe):
    target=CACHE/'data.npz'
    if target.exists():return np.load(target)
    xs=[];ms=[];ps=[];lookup={s:i for i,s in enumerate(universe['universe']['symbols'])}
    for selected in universe['protocol']['selected_products']:
        product=selected['product'];parts=[]
        for path in sorted((ROOT/'.tqsdk/research/history').glob(product+'*.json.gz')):
            payload=json.loads(gzip.decompress(path.read_bytes()));meta=payload['meta']
            if meta['exchange_id']+'.'+meta['product_id']!=product:continue
            result=contract_examples(payload['bars']['900'],meta,lookup[path.name[:-8]])
            if result is None or not len(result[0]):continue
            x,m=result;times,extra=contextual_features(payload['bars']['900'])
            positions=np.searchsorted(times,m[:,0]-900000)
            parts.append(np.column_stack([x,m,extra[positions]]))
        a=np.concatenate(parts);m=a[:,18:34]
        order=np.lexsort((m[:,1],-m[:,2],m[:,0]));a=a[order];m=a[:,18:34]
        a=a[np.r_[True,m[1:,0]!=m[:-1,0]]];m=a[:,18:34]
        good=(m[:,-1]==1)&np.isfinite(a[:,:18]).all(axis=1)&np.isfinite(a[:,34:]).all(axis=1)
        good &= (m[:,0]>=timestamp('2023-07-01'))&(m[:,0]+1800000<timestamp('2026-09-25'))
        a=a[good];p=np.full(len(a),selected['id']);base=augment((a[:,:18],a[:,18:34],p))
        xs.append(np.column_stack([base[0],a[:,34:]]));ms.append(base[1]);ps.append(p)
        print('context product',product,len(a),flush=True)
    np.savez_compressed(target,x=np.concatenate(xs),m=np.concatenate(ms),p=np.concatenate(ps))
    return np.load(target)


def logit(p):
    p=np.clip(p,1e-6,1-1e-6);return np.log(p/(1-p)).reshape(-1,1)


def main(low_cost=False):
    CACHE.mkdir(parents=True,exist_ok=True);OUT.mkdir(parents=True,exist_ok=True)
    universe=json.loads((ROOT/'.tqsdk/research/external15m/metadata.json').read_text(encoding='utf-8'))
    selection='prior top8 training OI'
    if low_cost:
        candidates=[]
        for item in universe['protocol']['eligible_ranking']:
            d=np.load(ROOT/f".tqsdk/research/profit15m/{item['id']}-train.npz")
            sx,sm=d['x'],d['m']
            distance=np.maximum(sm[:,5],np.ceil(1.5*sx[:,7]*sm[:,3]/sm[:,5])*sm[:,5])
            median_risk=float(np.median(distance*sm[:,4]+10+2*sm[:,5]*sm[:,4]))
            if median_risk<=900:candidates.append(dict(item,median_risk=median_risk))
        candidates.sort(key=lambda a:(a['median_cost_R'],a['product']))
        universe['protocol']['selected_products']=candidates[:8]
        selection='training only: prior liquidity/margin gate; median single-lot risk<=900; top8 lowest cost/stop'
    protocol=dict(model='conditional net-return with separate probability calibration',
        train='2023-07-01 to 2025-06-30',calibration='2025-07-01 to 2025-07-31',
        validation='2025-08-01 to 2025-09-25',historical='2025-09-26 to 2026-09-24; previously exposed',
        feature_names=NAMES+CONTEXT_NAMES,parameters=SETTINGS,training_half_life_days=365,
        horizons=[1,2],edge_grid=[0,.1,.2],profit_probability_grid=[.5,.55],candidate_count=12,
        stop_atr=1.5,take_profit=None,max_cost_over_stop=.35,fee=10,slippage=1,
        approval=dict(min_trades=100,min_win_rate=.5,net_positive=True,max_drawdown=.1),
        max_drawdown_is_provisional=True,selected_products=universe['protocol']['selected_products'],
        product_selection=selection,
        source_review='2024 reversal/liquidity and 2020 intraday momentum; hypotheses, not literal replication',
        adaptive_experiment_after_prior_failures=True,not_an_unseen_holdout=True)
    save_json(OUT/'protocol.json',protocol)
    data=build(universe);x,m,p=(data[a] for a in ('x','m','p'))
    boundaries=[timestamp(a) for a in ['2025-07-01','2025-08-01','2025-09-26']]
    masks=[(m[:,0]+1800000<boundaries[0]),
        (m[:,0]>=boundaries[0])&(m[:,0]+1800000<boundaries[1]),
        (m[:,0]>=boundaries[1])&(m[:,0]+1800000<boundaries[2]),m[:,0]>=boundaries[2]]
    parts=[tuple(a[z] for a in (x,m,p)) for z in masks]
    train,cal,val,test=parts
    weights=np.exp(-np.log(2)*(boundaries[0]-train[1][:,0])/86400000/365)
    predictions={};comparisons=[];diagnostics=[]
    for horizon in (1,2):
        truth,_,_,risk=fixed_outcomes(train[0],train[1],horizon)
        cy,_,_,crisk=fixed_outcomes(cal[0],cal[1],horizon)
        vy,_,_,vrisk=fixed_outcomes(val[0],val[1],horizon)
        means=[[],[]];probs=[[],[]];heads=[]
        for side in (0,1):
            reg=HistGradientBoostingRegressor(**SETTINGS)
            clf=HistGradientBoostingClassifier(**SETTINGS)
            # Preserve actual net returns: do not clip away losing tails.
            reg.fit(train[0],truth[:,side]/risk,sample_weight=weights)
            clf.fit(train[0],truth[:,side]>0,sample_weight=weights)
            calibrator=LogisticRegression(C=1,max_iter=1000)
            calibrator.fit(logit(clf.predict_proba(cal[0])[:,1]),cy[:,side]>0)
            bias=float(np.mean(cy[:,side]/crisk-reg.predict(cal[0])))
            for k,d in enumerate((val,test)):
                means[k].append(reg.predict(d[0])+bias)
                probs[k].append(calibrator.predict_proba(logit(clf.predict_proba(d[0])[:,1]))[:,1])
            heads.append(dict(reg=reg,clf=clf,calibrator=calibrator,mean_bias=bias))
            calibrated=probs[0][-1];prior=float((cy[:,side]>0).mean())
            diagnostics.append(dict(horizon=horizon,side=side,calibration_bias=bias,
                validation_brier=float(brier_score_loss(vy[:,side]>0,calibrated)),
                calibration_constant_brier=float(brier_score_loss(vy[:,side]>0,np.full(len(vy),prior))),
                validation_max_probability=float(calibrated.max()),
                validation_max_expected_net_R=float(means[0][-1].max())))
            print('fit',horizon,side,diagnostics[-1],flush=True)
        joblib.dump(dict(heads=heads,protocol=protocol,horizon=horizon),CACHE/f'model-{horizon}.joblib')
        predictions[horizon]=[(np.column_stack(means[i]),np.column_stack(probs[i])) for i in (0,1)]
        for split,scores in zip(('validation','test'),predictions[horizon]):
            np.savez_compressed(CACHE/f'pred-{horizon}-{split}.npz',mean=scores[0],prob=scores[1])
        mean,prob=predictions[horizon][0]
        for edge in protocol['edge_grid']:
            for floor in protocol['profit_probability_grid']:
                stats=replay(*val,mean,prob,horizon,edge,floor)
                c=dict(horizon=horizon,edge=edge,floor=floor,validation=summary(stats))
                comparisons.append(c);print('validation',c,flush=True)
    approved=[c for c in comparisons if c['validation']['trades']>=100 and c['validation']['net']>0
        and c['validation']['win_rate']>=.5 and c['validation']['max_drawdown']<=.1]
    diagnostic=[c for c in comparisons if c['validation']['trades']>=20] or comparisons
    choice=max(approved or diagnostic,key=lambda c:c['validation']['net'])
    save_json(OUT/'selection.json',dict(approved=bool(approved),choice=choice,comparisons=comparisons))
    mean,prob=predictions[choice['horizon']][1]
    stats=replay(*test,mean,prob,choice['horizon'],choice['edge'],choice['floor'])
    stress={f'fee{fee}_slip{slip}':summary(replay(*test,mean,prob,choice['horizon'],choice['edge'],choice['floor'],fee,slip))
        for fee,slip in [(0,0),(10,0),(10,1),(10,2)]}
    with (OUT/'成交明细.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f);writer.writerow(['entry_ms','exit_ms','symbol','product','direction','lots','net','balance','prior_balance'])
        for row in stats['ledger']:
            row=row.copy();row[2]=universe['universe']['symbols'][row[2]];row[3]=universe['universe']['products'][row[3]]
            writer.writerow(row)
    result=dict(protocol=protocol,counts=dict(zip(('train','calibration','validation','historical'),[len(a[0]) for a in parts])),
        approved=bool(approved),choice=choice,comparisons=comparisons,diagnostics=diagnostics,historical=summary(stats),
        cost_stress=stress,data_sha256=hashlib.sha256((CACHE/'data.npz').read_bytes()).hexdigest(),
        code_sha256={a:hashlib.sha256((ROOT/a).read_bytes()).hexdigest() for a in
            ['research/conditional_intraday.py','tools/research_conditional_15m.py']},
        model_sha256={str(h):hashlib.sha256((CACHE/f'model-{h}.joblib').read_bytes()).hexdigest() for h in (1,2)})
    save_json(OUT/'results.json',result);report(result)
    print('complete approved',bool(approved),summary(stats),flush=True)


def report(r):
    def pct(a):return '不可计算' if a is None else f'{100*a:.2f}%'
    v=r['choice']['validation'];s=r['historical']
    lines=['# 条件净收益数学模型：15分钟专项','',
        f"验证是否通过：{'是' if r['approved'] else '否'}。本轮只研究按训练条件选出的8个品种，不是全部80品种。",
        '品种：'+', '.join(a['product'] for a in r['protocol']['selected_products'])+'。筛选规则：'+r['protocol']['product_selection']+'。',
        '模型已经实现并运行，但能计算不等于可盈利。不通过则仅保留研究产物，不接入客户端。后段历史此前已经查看，不称新的独立测试。','',
        '| 区间 | 成交 | 净收益元 | 净盈利胜率 | 回撤 | 信号覆盖率 |','|---|---:|---:|---:|---:|---:|',
        f"| 验证 | {v['trades']} | {v['net']:.2f} | {pct(v['win_rate'])} | {pct(v['max_drawdown'])} | {pct(v['coverage'])} |",
        f"| 后段历史 | {s['trades']} | {s['net']:.2f} | {pct(s['win_rate'])} | {pct(s['max_drawdown'])} | {pct(s['coverage'])} |",'',
        '## 数学定义','',
        '对做多/做空方向 s，令 Y_s 为下一柱开盘入场、1.5ATR止损或持有期末收盘平仓后的每手净收益。手续费每手开平10元、各1跳不利滑点。止损含跳空；本轮不设固定止盈。风险金额 R 为止损距离×合约乘数+费用与双边滑点。',
        'μ_s(x)=E[Y_s/R | x]，p_s(x)=P(Y_s>0 | x)。两者分别由梯度提升回归/分类拟合。只在2025年7月用未参与主模型训练的样本修正回归偏差、拟合logistic概率校准；校准不保证在新时期保持准确。',
        '合格方向需 μ_s≥门槛、p_s≥门槛、默认成本/止损空间≤0.35，再取合格方向中μ最大的方向；否则观望。手数=min(10, floor(min(900,权益×1.8%)/R), floor(权益/保证金))。没有可负担的1手时不交易。',
        f"选定研究规则：最长{r['choice']['horizon']*15}分钟、净R门槛{r['choice']['edge']}、概率门槛{r['choice']['floor']}。止损可提前退出；并不保证持仓至少15分钟。",'',
        '## 新增条件与时间隔离','',
        '既有价格/波动/量仓/成本/品种特征，加上本连续时段开盘以来的ATR归一化涨跌、近似VWAP偏离、短长波动比、4柱路径效率、时段已运行柱数及15分钟时钟类别。连续时段在观测到休市缺口后重置，不等同完整交易日。VWAP用柱均价×成交量近似；没有假造盘口、期权Gamma或真实成交额。',
        '主模型训练2023年7月至2025年6月、概率与均值修正2025年7月、选择规则2025年8月至9月25日、后段评价2025年9月26日至2026年9月24日。主模型全量训练，按365天半衰期增加近期样本权重，不在后段重新训练。标签跨边界剔除30分钟。',
        f"机会条数：{r['counts']}。2个持有期×3个净R门槛×2个概率门槛=12组。参数先存protocol.json；按验证选择写selection.json后才计算后段策略结果。",
        '此研究是在先前失败及已查看历史之后提出，属于适应性新假设。12组选择仍有多重比较风险。通过要求：验证≥100笔、盈利、净盈利胜率≥50%、回撤≤10%；10%为暂定研究限制。','',
        '## 成本压力','',
        '| 情景 | 成交 | 净收益元 | 胜率 | 回撤 |','|---|---:|---:|---:|---:|']
    for label,a in r['cost_stress'].items():
        lines.append(f"| {label} | {a['trades']} | {a['net']:.2f} | {pct(a['win_rate'])} | {pct(a['max_drawdown'])} |")
    lines += ['',
        '固定预测、门槛与默认成本筛选，改变实际执行成本重算资金、手数与退出；情景收益差不是固定交易的成本归因。共享5万元、全球最多一仓、保证金按15%假设。不跨观测休市或自然日。回撤包含柱内保守不利价格。15分钟OHLC不能证明真实成交，也缺历史规则、盘口与逐笔点差。','',
        '## 文献依据及限制','',
        '本模型是以下证据启发的工程假设，并非原论文复现。2020年研究的开盘半小时预测尾盘半小时不能直接推广到全天每15分钟；2024年研究使用期权与流动性解释反转，我们只有OHLCV，无法检验其全部机制。2018年研究指出日内交易的毛收益可能被成本吞噬。',
        '- [中国商品期货日内动量，2020](https://www.sciencedirect.com/science/article/pii/S0275531919311328)',
        '- [中国商品期货与期权日内反转，2024](https://www.sciencedirect.com/science/article/pii/S0927538X24002865)',
        '- [动量、反转与交易成本，2018](https://www.sciencedirect.com/science/article/pii/S1057521918305696)',
        '- [概率校准官方文档](https://scikit-learn.org/stable/modules/calibration.html)','',
        '复现：python tools/research_conditional_15m.py。缓存与模型位于忽略目录.tqsdk/research/conditional15m；results.json保留诊断、完整比较与代码/数据/模型指纹，成交明细.csv可核对。研究工具不执行订单。']
    (OUT/'研究报告.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--low-cost',action='store_true');args=parser.parse_args()
    if args.low_cost:
        CACHE=ROOT/'.tqsdk/research/conditional15m_low_cost'
        OUT=ROOT/'docs/研究/2026-09-26-条件净收益模型-低成本品种'
    with threadpool_limits(limits=4):main(args.low_cost)
