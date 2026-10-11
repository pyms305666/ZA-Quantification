"""Predeclared 15-minute cost-aware research; never sends orders."""
import csv
import gzip
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier,HistGradientBoostingRegressor
from threadpoolctl import threadpool_limits
from research.direction_model import FEATURES,START,VAL_START,TEST_START,END
from research.intraday_profit import RECIPES,contract_examples,outcomes,replay

CACHE=ROOT/'.tqsdk/research/profit15m'
OUT=ROOT/'docs/研究/2026-09-26-15分钟盈利模型'
SEED=20260926
HALF=1753977600000  # 2025-08-01 China midnight; filter fit before this date.


def save_json(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')


def build():
    CACHE.mkdir(parents=True,exist_ok=True)
    paths=sorted((ROOT/'.tqsdk/research/history').glob('*.json.gz'))
    symbols=[p.name[:-8] for p in paths]
    groups={};digest=hashlib.sha256()
    for i,p in enumerate(paths):
        # The product is read from metadata, never inferred from contract suffix.
        d=json.loads(gzip.decompress(p.read_bytes()))
        key=d['meta']['exchange_id']+'.'+d['meta']['product_id']
        groups.setdefault(key,[]).append((i,p))
        digest.update(hashlib.sha256(p.read_bytes()).digest())
    products=sorted(groups); counts={}; rng=np.random.default_rng(SEED)
    for pi,product in enumerate(products):
        parts=[]
        for si,path in groups[product]:
            d=json.loads(gzip.decompress(path.read_bytes()))
            result=contract_examples(d['bars']['900'],d['meta'],si)
            if result is not None and len(result[0]):
                x,m=result;parts.append(np.column_stack([x,m]))
        if not parts:continue
        a=np.concatenate(parts);x=a[:,:18];m=a[:,18:]
        # Highest CURRENT completed-bar OI. Do this before testing future data.
        order=np.lexsort((m[:,1],-m[:,2],m[:,0]));x=x[order];m=m[order]
        first=np.r_[True,m[1:,0]!=m[:-1,0]];x=x[first];m=m[first]
        good=(m[:,-1]==1)&(m[:,0]>=START)&(m[:,0]+1800000<END)
        x=x[good];m=m[good]
        masks={'train':(m[:,0]<VAL_START)&(m[:,0]+1800000<VAL_START),
               'validation':(m[:,0]>=VAL_START)&(m[:,0]<TEST_START)&(m[:,0]+1800000<TEST_START),
               'test':m[:,0]>=TEST_START}
        counts[product]={}
        for split,mask in masks.items():
            ix=np.flatnonzero(mask);counts[product][split]=len(ix)
            if split=='train' and len(ix)>3000:ix=rng.choice(ix,3000,replace=False)
            np.savez_compressed(CACHE/f'{pi}-{split}.npz',x=x[ix].astype('float32'),m=m[ix])
        print('product',pi+1,'/',len(products),product,counts[product],flush=True)
    meta=dict(products=products,symbols=symbols,counts=counts,data_sha256=digest.hexdigest())
    save_json(CACHE/'metadata.json',meta)
    return meta


def load(split,meta):
    xs=[];ms=[];ps=[]
    for i,p in enumerate(meta['products']):
        path=CACHE/f'{i}-{split}.npz'
        if not path.exists():continue
        d=np.load(path);xs.append(d['x']);ms.append(d['m']);ps.append(np.full(len(d['m']),i))
    return np.concatenate(xs),np.concatenate(ms),np.concatenate(ps)


def fit_predict(train,val,test,recipe):
    x,m,_=train;pnl,_,_,risk=outcomes(x,m,recipe)
    heads=[];val_means=[];test_means=[];val_win=[];test_win=[]
    for side in (0,1):
        reg=HistGradientBoostingRegressor(max_iter=100,max_depth=3,max_leaf_nodes=8,
            min_samples_leaf=200,l2_regularization=10,learning_rate=.05,early_stopping=False,random_state=SEED)
        clf=HistGradientBoostingClassifier(max_iter=100,max_depth=3,max_leaf_nodes=8,
            min_samples_leaf=200,l2_regularization=10,learning_rate=.05,early_stopping=False,random_state=SEED)
        reg.fit(x,np.clip(pnl[:,side]/risk,-4,4));clf.fit(x,pnl[:,side]>0)
        val_means.append(reg.predict(val[0]));test_means.append(reg.predict(test[0]))
        val_win.append(clf.predict_proba(val[0])[:,1]);test_win.append(clf.predict_proba(test[0])[:,1])
        heads.append((reg,clf))
    joblib.dump(dict(heads=heads,features=FEATURES,recipe=recipe),CACHE/f'model-{recipe}.joblib')
    return tuple(np.column_stack(a) for a in (val_means,val_win,test_means,test_win))


def filters(x,m,p,means,wins,recipe):
    pnl,_,_,risk=outcomes(x,m,recipe)
    side=np.argmax(means,axis=1);idx=np.arange(len(m))
    active=(means[idx,side]>0)&(wins[idx,side]>=.5)
    gain=pnl[idx,side]/risk
    allowed=[];slots=[]
    for group in np.unique(p):
        mask=active&(p==group)
        if mask.sum()>=100 and gain[mask].mean()>0 and (gain[mask]>0).mean()>=.5:allowed.append(int(group))
    hours=(m[:,0]//3600000+8)%24
    for hour in np.unique(hours):
        mask=active&(hours==hour)
        if mask.sum()>=200 and gain[mask].mean()>0 and (gain[mask]>0).mean()>=.5:slots.append(int(hour))
    return allowed,slots


def summary(stats):return {k:v for k,v in stats.items() if k!='ledger'}


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    protocol={'recipes':RECIPES,'stop_atr':1.5,'edge_grid':[0,.05,.1],
        'win_score_grid':[.5,.55],'risk_cash':900,'risk_percent':1.8,'initial_cash':50000,
        'max_drawdown_gate':.10,'validation_min_trades':100,'validation_min_win_rate':.50,
        'position_limit':1,'fee_round_trip':10,'slippage_per_side_ticks':1,
        'margin_assumption':.15,'same_bar_stop_first':True,'train_quota_per_product':3000,
        'filter_min_product_samples':100,'filter_min_hour_samples':200,
        'filter_cutoff':HALF,'selection_period':'2025-08-01 to 2025-09-25',
        'historical_evaluation':'2025-09-26 to 2026-09-24; previously exposed, not pristine holdout'}
    save_json(OUT/'protocol.json',protocol)
    print('protocol saved',flush=True)
    meta=build()
    train=load('train',meta);val=load('validation',meta);test=load('test',meta)
    print('partitions',len(train[0]),len(val[0]),len(test[0]),flush=True)
    filter_mask=val[1][:,0]+1800000<HALF
    select_mask=val[1][:,0]>=HALF
    filter_data=tuple(a[filter_mask] for a in val);select_data=tuple(a[select_mask] for a in val)
    comparisons=[];predictions={};learned={}
    for recipe in range(len(RECIPES)):
        vm,vw,tm,tw=fit_predict(train,val,test,recipe)
        allowed,slots=filters(*filter_data,vm[filter_mask],vw[filter_mask],recipe)
        learned[recipe]=(allowed,slots);predictions[recipe]=(tm,tw)
        for edge in protocol['edge_grid']:
            for floor in protocol['win_score_grid']:
                stats=replay(*select_data[:2],vm[select_mask],vw[select_mask],recipe,edge,floor,
                             select_data[2],allowed,slots)
                record=dict(recipe=recipe,edge=edge,win_floor=floor,
                            allowed_products=[meta['products'][i] for i in allowed],slots=slots,validation=summary(stats))
                comparisons.append(record)
                print('validation',recipe,edge,floor,summary(stats),flush=True)
    eligible=[c for c in comparisons if c['validation']['trades']>=100
              and c['validation']['net']>0 and c['validation']['max_drawdown']<=.1
              and c['validation']['win_rate']>=.5]
    # Freeze choice before calculating chronological historical evaluation.
    winner=max(eligible,key=lambda c:c['validation']['net']) if eligible else None
    exploratory=max(comparisons,key=lambda c:(c['validation']['net'],c['validation']['trades']))
    choice=winner or exploratory
    save_json(OUT/'selection.json',dict(approved=bool(winner),selected=choice,comparisons=comparisons))
    recipe=choice['recipe'];tm,tw=predictions[recipe];allowed,slots=learned[recipe]
    stats=replay(test[0],test[1],tm,tw,recipe,choice['edge'],choice['win_floor'],test[2],allowed,slots)
    stress={}
    for slip in (0,1,2):
        s=replay(test[0],test[1],tm,tw,recipe,choice['edge'],choice['win_floor'],test[2],allowed,slots,slippage=slip)
        stress[str(slip)]=summary(s)
    old=joblib.load(ROOT/'.tqsdk/research/models/15m.joblib')['model']
    prob=old.predict_proba(test[0])[:,1]
    old_mean=np.column_stack([prob,1-prob]);old_win=np.ones_like(old_mean)
    baseline=replay(test[0],test[1],old_mean,old_win,recipe,0,0,test[2],range(len(meta['products'])),range(24))
    result=dict(protocol=protocol,metadata=meta,training_samples=len(train[0]),validation_samples=len(val[0]),
        historical_samples=len(test[0]),approved_by_validation=bool(winner),selection=choice,
        historical=summary(stats),slippage_stress=stress,old_direction_baseline=summary(baseline),
        code_sha256={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in
                     ['research/intraday_profit.py','tools/research_15m_profit.py']},
        model_sha256=hashlib.sha256((CACHE/f'model-{recipe}.joblib').read_bytes()).hexdigest())
    save_json(OUT/'results.json',result)
    with (OUT/'成交明细.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow(['entry_ms','exit_ms','symbol','product','direction','lots','net','balance','prior_balance'])
        for t in stats['ledger']:
            row=t.copy();row[2]=meta['symbols'][row[2]];row[3]=meta['products'][row[3]];w.writerow(row)
    np.savez_compressed(CACHE/'chosen-evaluation.npz',x=test[0],m=test[1],products=test[2],mean=tm,win=tw)
    write_report(result)
    print('finished',summary(stats),'validation_approved',bool(winner),flush=True)


def write_report(r):
    c=r['selection'];s=r['historical'];b=r['old_direction_baseline'];n,target=RECIPES[c['recipe']]
    def percent(v):return '无成交' if v is None else f'{v*100:.2f}%'
    lines=['# 15分钟净收益模型研究','',
        '## 结论','',
        f"验证筛选是否通过：{'是' if r['approved_by_validation'] else '否；所列结果仅为候选诊断，不建议交易'}。历史评估净收益 {s['net']:.2f} 元，净盈利胜率 {percent(s['win_rate'])}，最大回撤 {percent(s['max_drawdown'])}。",
        '这是一轮历史研究；2025-09至2026-09期间已在此前研究中查看，不能再称完全独立测试。正收益不等于未来盈利；负结果同样保留。没有接入客户端或执行订单。','',
        '## 模型与交易规则','',
        '分别用梯度提升回归估计做多/做空的净收益R，用分类器估计净盈利分值。R以1.5ATR止损金额加成本归一化；回归训练标签截断到[-4,4]，因此输出是截断净收益的估计，不是无偏收益保证。盈利分值未进行概率校准。',
        f"选定规则：最长持有 {n*15} 分钟，止损1.5ATR，止盈{target}R。止损/止盈可能提前退出。预测净R至少 {c['edge']}，盈利分值至少 {c['win_floor']}；允许观望。",
        '15分钟柱收盘形成信号，下一根开盘成交。当前已完成柱持仓量最高的合约为该品种活跃合约，同值按合约编号排序；选择后再检查未来报价，不按未来数据挑合约。当前成交量/持仓量至少100。预测及合约选择只用当时已知数据。',
        '仅保留下一段连续30分钟报价，避开休市、跨自然日和到期；因此覆盖统计限于具备可评价未来行情的候选样本，真实部署还需实时交易日历/时段禁止新开仓。元数据并非历史规则档案。',
        '三组固定候选：[15分钟、1.5R止盈]、[30分钟、1.5R止盈]、[30分钟、2R止盈]。止损和止盈同柱触及按止损先发生，跳空止损按开盘更差价格；开平各5元、各1跳不利滑点。止盈跳空仍只按目标价，避免美化。',
        '5万元共享账户，全球最多1个持仓，冲突按预测净R排序；允许平仓同一时刻再入场。单笔风险预算min(900元,当前权益1.8%)，手数最多10，保证金统一按15%假设。止损可能因跳空和滑点超预算。回撤包含持仓柱内保守不利价格估计，不是逐笔真实权益曲线。', '',
        '## 时间隔离与筛选','',
        f"训练样本 {r['training_samples']:,}；验证样本 {r['validation_samples']:,}；历史评估候选 {r['historical_samples']:,}。逐品种读取全部缓存，训练每品种均匀抽样最多3000，验证和历史评估按完整候选时序回放。",
        '训练2021-09-26至2024-09-25；标签跨界剔除。验证前段截至2025-07-31用于品种/小时过滤（至少100/200条，平均净R>0且胜率≥50%）；2025-08-01至09-25选择阈值/退出规则。品种和时段可来自验证期，无历史评估结果用于挑选。',
        '仅18个预声明候选，最终验证必须至少100笔成交、盈利、回撤≤10%、胜率≥50%。未通过则保留观望建议，仍展示诊断；不会为了漂亮结果修改阈值。短验证区间与多重选择仍可能过拟合，需要更长滚动验证和新的前向样本。',
        f"选用品种：{', '.join(c['allowed_products']) or '无'}；上海时间信号小时：{c['slots']}。", '',
        '## 历史评估与成本压力','',
        '| 模型/成本 | 成交笔数 | 净收益元 | 净盈利胜率 | 回撤 |','|---|---:|---:|---:|---:|',
        f"| 新候选（各1跳） | {s['trades']} | {s['net']:.2f} | {percent(s['win_rate'])} | {percent(s['max_drawdown'])} |",
        f"| 原方向模型，同退出和资金规则 | {b['trades']} | {b['net']:.2f} | {percent(b['win_rate'])} | {percent(b['max_drawdown'])} |"]
    for slip,v in r['slippage_stress'].items():
        lines.append(f"| 新候选各{slip}跳 | {v['trades']} | {v['net']:.2f} | {percent(v['win_rate'])} | {percent(v['max_drawdown'])} |")
    lines += ['',f"信号覆盖率 {percent(s['coverage'])}；实际成交/候选比例 {percent(s['executed_coverage'])}。单账户冲突、保证金与风险限制均会降低实际出手率。",
        '压力回放固定原模型与筛选，仅改变执行成本并重新计算手数；没有针对压力情景重新训练。基准使用原15分钟方向模型，同一候选事件和相同退出规则，但不作新品种/小时过滤；不是完全匹配交易子集的因果增益证明。', '',
        '## 限制与文件','',
        '15分钟OHLC不能证明开盘/触价均可成交，不包含盘口、真实点差、排队、涨跌停及实际逐品种手续费/保证金。止损先发生是保守假设，缺报价样本排除也可能带来选择偏差。仍需要分钟或逐笔行情复核以及新的前向数据验证，才可判断是否实用。',
        'results.json保存完整统计与代码/数据/模型指纹；selection.json保存18组验证结果；protocol.json保存事先规则；成交明细.csv可逐笔核对。缓存和模型在.tqsdk/research/profit15m中，不含账号密码。', '',
        '复现：`python tools/research_15m_profit.py`。']
    (OUT/'研究报告.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


if __name__=='__main__':
    with threadpool_limits(limits=4):main()
