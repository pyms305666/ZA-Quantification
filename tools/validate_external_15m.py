"""Predeclared chronological comparisons of Kronos and deterministic-event filters."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import joblib
import numpy as np
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestClassifier,RandomForestRegressor
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import OneHotEncoder
from threadpoolctl import threadpool_limits
from research.intraday_profit import RECIPES,outcomes,replay
from tools.research_15m_cost_features import augment
from tools.research_15m_profit import save_json,summary

CACHE=ROOT/'.tqsdk/research/external15m'
OUT=ROOT/'docs/研究/2026-09-26-外部模型验证'


def load(split):
    d=np.load(CACHE/f'{split}.npz')
    x,m,p=augment((d['x'],d['m'],d['products']))
    return x,m,p,d['primary']


def event_predictions(reg,clf,x,side):
    inputs=np.column_stack([x,side])
    expected=reg.predict(inputs);probability=clf.predict_proba(inputs)[:,1]
    mean=np.full((len(x),2),-100.);win=np.zeros_like(mean)
    indices=np.flatnonzero(side!=0);direction=(side[indices]<0).astype(int)
    mean[indices,direction]=expected[indices];win[indices,direction]=probability[indices]
    return mean,win


def meta():
    train=load('train');val=load('validation');test=load('test')
    settings=dict(n_estimators=200,max_depth=4,min_samples_leaf=30,random_state=20260926,n_jobs=4)
    for primary in (0,1):
        side=train[3][:,primary];mask=side!=0
        inputs=np.column_stack([train[0][mask],side[mask]])
        for recipe in range(3):
            pnl,_,_,risk=outcomes(train[0],train[1],recipe)
            truth=pnl[np.arange(len(side)),(side<0).astype(int)]
            if mask.sum()<100 or len(np.unique(truth[mask]>0))<2:
                raise RuntimeError('insufficient event training examples')
            def features():
                return ColumnTransformer([('product',OneHotEncoder(handle_unknown='ignore',sparse_output=False),[21]),
                                          ('numeric','passthrough',[i for i in range(23) if i!=21])])
            reg=make_pipeline(features(),RandomForestRegressor(**settings))
            clf=make_pipeline(features(),RandomForestClassifier(**settings))
            reg.fit(inputs,np.clip(truth[mask]/risk[mask],-4,4));clf.fit(inputs,truth[mask]>0)
            artifact=CACHE/f'event-{primary}-{recipe}.joblib'
            joblib.dump(dict(reg=reg,clf=clf,primary=primary,recipe=recipe,train_events=int(mask.sum())),artifact)
            for split,d in [('validation',val),('test',test)]:
                mean,win=event_predictions(reg,clf,d[0],d[3][:,primary])
                np.savez_compressed(CACHE/f'event-{primary}-{recipe}-{split}.npz',mean=mean,win=win)
            print('event fit',primary,recipe,'training_events',mask.sum(),flush=True)


def kronos_predictions(d,forecast,recipe):
    x,m,p,primary=d;n=RECIPES[recipe][0]
    close=forecast[:,n-1,3]
    _,_,_,risk=outcomes(x,m,recipe)
    gross=(close-m[:,3])*m[:,4]
    costs=10+2*m[:,5]*m[:,4]
    mean=np.column_stack([(gross-costs)/risk,(-gross-costs)/risk])
    return mean,np.ones_like(mean)


def execute(d,mean,win,c,fee=10,slip=1):
    return replay(d[0],d[1],mean,win,c['recipe'],c.get('edge',0),c.get('win_floor',0),
                  d[2],np.unique(d[2]),range(24),fee=fee,slippage=slip)


def choose(comparisons):
    approved=[c for c in comparisons if c['validation']['trades']>=100 and c['validation']['net']>0
              and c['validation']['max_drawdown']<=.1 and c['validation']['win_rate']>=.5]
    # A failed policy remains non-deployable; pick a traded diagnostic rather
    # than report no-trade as the highest-profit model.
    pool=approved or [c for c in comparisons if c['validation']['trades']>=20]
    pool=pool or [c for c in comparisons if c['validation']['trades']>0] or comparisons
    return max(pool,key=lambda c:c['validation']['net']),bool(approved)


def ledger(name,stats,meta):
    with (OUT/f'{name}-成交.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow(['entry_ms','exit_ms','symbol','product','direction','lots','net','balance','prior_balance'])
        for t in stats['ledger']:
            row=t.copy();row[2]=meta['universe']['symbols'][row[2]];row[3]=meta['universe']['products'][row[3]];w.writerow(row)


def evaluate():
    val=load('validation');test=load('test');meta=json.loads((CACHE/'metadata.json').read_text(encoding='utf-8'))
    forecasts={split:np.load(CACHE/f'kronos-{split}.npz')['forecast'] for split in ('validation','test')}
    comparisons=[]
    event_diagnostics=[]
    for recipe in range(3):
        mean,win=kronos_predictions(val,forecasts['validation'],recipe)
        for edge in (0,.1,.2):
            c=dict(model='Kronos-small',recipe=recipe,edge=edge,win_floor=0)
            c['validation']=summary(execute(val,mean,win,c));comparisons.append(c)
        for name,folder in [('event_filter',CACHE),('event_filter_full',CACHE/'full_events')]:
            for primary in (0,1):
                d=np.load(folder/f'event-{primary}-{recipe}-validation.npz')
                side=val[3][:,primary];ix=np.flatnonzero(side);direction=(side[ix]<0).astype(int)
                event_diagnostics.append(dict(model=name,primary=primary,recipe=recipe,events=len(ix),
                    max_expected_net_R=float(d['mean'][ix,direction].max()),
                    max_profit_score=float(d['win'][ix,direction].max())))
                for floor in (.5,.55,.6):
                    c=dict(model=name,primary=primary,recipe=recipe,edge=0,win_floor=floor)
                    c['validation']=summary(execute(val,d['mean'],d['win'],c));comparisons.append(c)
    selections={}
    for name in ('Kronos-small','event_filter','event_filter_full'):
        choice,approved=choose([c for c in comparisons if c['model']==name])
        selections[name]=dict(choice=choice,validation_approved=approved)
    # This is written before calculating any historical strategy result.
    save_json(OUT/'selection.json',dict(selections=selections,comparisons=comparisons,
        diagnostic_choice='if none passes, prefer >=20 traded examples; never promote zero trades as profit'))
    results={}
    for name,selected in selections.items():
        c=selected['choice'];recipe=c['recipe']
        if name=='Kronos-small':mean,win=kronos_predictions(test,forecasts['test'],recipe)
        else:
            folder=CACHE/'full_events' if name=='event_filter_full' else CACHE
            d=np.load(folder/f"event-{c['primary']}-{recipe}-test.npz");mean,win=d['mean'],d['win']
        stats=execute(test,mean,win,c);ledger(name,stats,meta)
        stress={}
        for fee,slip in [(0,0),(10,0),(10,1),(10,2)]:
            stress[f'fee{fee}_slip{slip}']=summary(execute(test,mean,win,c,fee,slip))
        directional={}
        for sign,label in ((1,'long'),(-1,'short')):
            trades=[t for t in stats['ledger'] if t[4]==sign]
            directional[label]=dict(trades=len(trades),net=float(sum(t[6] for t in trades)),
                win_rate=sum(t[6]>0 for t in trades)/len(trades) if trades else None)
        result=dict(selection=selected,historical=summary(stats),cost_stress=stress,directional_trades=directional)
        if name.startswith('event_filter'):
            side=test[3][:,c['primary']];raw=np.full((len(side),2),-100.)
            ix=np.flatnonzero(side);raw[ix,(side[ix]<0).astype(int)]=1
            baseline=execute(test,raw,np.ones_like(raw),dict(recipe=recipe,edge=0,win_floor=0))
            result['raw_primary_baseline']=summary(baseline);ledger(name+'-raw_primary',baseline,meta)
        else:
            actual=test[1][:,10 if RECIPES[recipe][0]==1 else 14]-test[1][:,3]
            prediction=forecasts['test'][:,RECIPES[recipe][0]-1,3]-test[1][:,3]
            result['close_direction_accuracy']=float(((prediction>0)&(actual>0)|(prediction<0)&(actual<0)).mean())
            result['flat_actual_samples']=int((actual==0).sum())
            result['close_direction_baselines']=dict(always_up=float((actual>0).mean()),
                always_down=float((actual<0).mean()),
                momentum=float(((test[0][:,0]>0)&(actual>0)|(test[0][:,0]<0)&(actual<0)).mean()))
            old=joblib.load(ROOT/'.tqsdk/research/models/15m.joblib')['model']
            prob=old.predict_proba(test[0][:,:18])[:,1]
            baseline=execute(test,np.column_stack([prob,1-prob]),np.ones((len(prob),2)),dict(recipe=recipe,edge=0,win_floor=0))
            result['old_direction_baseline']=summary(baseline);ledger('old_direction',baseline,meta)
        np.savez_compressed(CACHE/f'{name}-frozen-scores.npz',mean=mean,win=win)
        results[name]=result
        print('historical',name,result,flush=True)
    payload=dict(protocol=meta['protocol'],data_counts=meta['counts'],results=results,
        validation_event_diagnostics=event_diagnostics,
        full_event_training=json.loads((OUT/'full-event-training.json').read_text(encoding='utf-8')),
        source_downloads=json.loads((CACHE/'downloads.json').read_text(encoding='utf-8')),
        inference={split:json.loads((CACHE/f'kronos-{split}-run.json').read_text(encoding='utf-8')) for split in ('validation','test')},
        data_sha256=meta['cache_sha256'],code_sha256={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in
            ['research/intraday_profit.py','tools/prepare_external_15m_data.py','tools/infer_kronos_15m.py','tools/validate_external_15m.py','tools/train_full_event_filter_15m.py']})
    save_json(OUT/'results.json',payload)
    report(payload)


def report(r):
    def percent(v):return '不可计算' if v is None else f'{v*100:.2f}%'
    lines=['# Kronos与交易事件过滤：15分钟验证','',
        '## 范围与结论','',
        '比较的是八个训练期筛出的代表品种，不能理解为已验证全部80品种。桌面/手机端没有接入这些模型，也没有下单。',
        '品种：'+', '.join(a['product'] for a in r['protocol']['selected_products'])+'。',
        '验证2025-08-01至09-25；后段历史评价2025-09-26至2026-09-24。后段区间此前已经研究过，仍不能称新独立测试。', '',
        '| 候选 | 验证通过 | 验证成交 | 验证净收益元 | 历史成交 | 历史净收益元 | 历史净盈利胜率 | 历史回撤 | 信号覆盖率 |',
        '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for name,a in r['results'].items():
        v=a['selection']['choice']['validation'];s=a['historical']
        lines.append(f"| {name} | {'是' if a['selection']['validation_approved'] else '否'} | {v['trades']} | {v['net']:.2f} | {s['trades']} | {s['net']:.2f} | {percent(s['win_rate'])} | {percent(s['max_drawdown'])} | {percent(s['coverage'])} |")
    lines += ['',
        '通过条件：验证至少100笔、净收益为正、净盈利胜率≥50%、回撤≤10%（10%为暂定研究筛选值）。没有通过时选择一组有成交的候选供诊断，不构成交易推荐。零成交不能称盈利改善。验证通过也不等于未来或实际成交盈利。', '',
        '本轮没有候选通过验证。Kronos后段在双边各1跳、每手开平10元时亏损40200元；固定信号并重新计算零成本成交后仍亏损7250元，说明不能仅归因于费用。压力情景改变手数、退出价格和资金路径，情景收益之差不能直接当作固定交易的成本金额。抽样与全量事件模型均未开仓，不能据零亏损宣称胜率或盈利改善。',
        '全量事件模型在六组验证组合中的预计净R最大值全部小于0，净盈利分值最大值约0.322–0.469，也均低于0.5门槛；因此没有交易并非模型报错或仅因训练抽样少。它表明这些初级事件、特征与固定随机森林设定没有找到可信的成本后机会，不能推广为所有方法都无效。', '',
        '## 做多与做空','',
        '| 候选/方向 | 成交 | 净盈利胜率 | 净收益元 |','|---|---:|---:|---:|']
    for name,a in r['results'].items():
        for direction,s in a['directional_trades'].items():
            lines.append(f"| {name}/{direction} | {s['trades']} | {percent(s['win_rate'])} | {s['net']:.2f} |")
    k=r['results']['Kronos-small'];b=k['close_direction_baselines']
    lines += ['',f"Kronos选定持有时长对应的原始收盘方向准确率 {percent(k['close_direction_accuracy'])}；同一机会集合始终涨 {percent(b['always_up'])}、始终跌 {percent(b['always_down'])}、上一柱动量 {percent(b['momentum'])}。价格不变计未命中。这不是净盈利胜率，也不是全80品种的方向评价。",'',
        '## 模型与数据','',
        'Kronos-small使用官方2025-06-30公开的模型及Tokenizer权重修订，文件和源代码指纹全部保留。权重发布早于本次验证窗口，有助于排除该权重包含此后价格；供应商完整预训练清单仍未获独立核实，训练期可能与预训练重叠。没有微调。',
        '使用128根本合约历史柱，未来2根柱；官方归一化只用输入历史，4次生成取均值，温度0.6、top-p=0.9。输入和归一化为float32，GPU推理使用float16 autocast以降低显存占用；运行配置、固定批次和随机种子均保留，混合精度和有限路径采样仍可能影响预测。成交额按官方缺省方式用volume×OHLC均价近似，并非真实成交额。生成价格变化减去成本、除以止损预算，作为净R分值；不是校准的盈利概率。',
        '事件过滤用MA4/MA16交叉与16柱2标准差布林反转两种明确初级策略。初级策略没有拟合参数，因此不使用训练集拟合预测作为二级输入。随机森林回归净R、分类净盈利，200棵树、深度4、叶至少30、品种独热；只有预计净R≥0并达到盈利分值阈值才执行。',
        '三组退出规则为最长15分钟/1.5R止盈、30分钟/1.5R、30分钟/2R；止损均1.5ATR，可提前退出。Kronos比较净R门槛0/0.1/0.2，事件过滤比较盈利分值0.5/0.55/0.6，首轮27组预设比较，仅用验证收益选择。',
        f"首轮抽样训练仅1343个均线事件、2809个布林事件，验证均未开仓。由此追加全训练事件对照：{r['full_event_training']['samples']}条机会、均线事件{r['full_event_training']['MA_events']}个、布林事件{r['full_event_training']['BB_events']}个。算法、参数及验证/后段数据不变，追加18组，共45组。这是观察首轮验证失败后提出的适应性扩展，不能当作全部事先声明；扩展未依据后段收益选择。",
        '品种筛选只用训练抽样：至少2000条、保证金中位数≤2万元、成本/止损≤0.35，再按持仓量中位数取前8。没有按后段收益选品种。两个模型使用相同带128柱上下文的机会集合；训练抽样保留23888条、验证4800条、后段28584条。缺少上下文的训练112条剔除，未填造。',
        '账户实查DataDownloader权限不存在，历史回测权限存在。本轮已有行情足够进行OHLC级比较，没有调用未授权下载器；没有伪造盘口或用公开网站不兼容的数据拼接。', '',
        '## 成交与成本压力','',
        '每手开平合计10元，双边各1跳；下一柱开盘入场，止损/止盈同柱触及按止损先发生。5万元共享账户、最多一个持仓、手数最多10、单笔预算min(900,权益1.8%)，保证金按15%研究假设。不跨休市、自然日或到期。',
        '| 模型/压力 | 成交 | 净收益元 | 胜率 | 回撤 |','|---|---:|---:|---:|---:|']
    for name,a in r['results'].items():
        for label,s in a['cost_stress'].items():
            lines.append(f"| {name}/{label} | {s['trades']} | {s['net']:.2f} | {percent(s['win_rate'])} | {percent(s['max_drawdown'])} |")
    lines += ['',
        '压力回放固定信号与阈值，改变费用/滑点后重新计算成交与手数，没有重新调参。基准与候选均共享资金，但交易数量、时间及资金路径不同，收益差不是完全匹配交易子集的因果证明。15分钟OHLC仍不能证明触价成交，也无法可靠恢复柱内止损/止盈先后；如候选有优势，需要更细行情复核。', '',
        '## 后续研究边界','',
        '本轮结果不支持接入客户端。进一步研究应另立规则：优先测试成本占波动较低的品种/时段、实际成交额和更细行情特征；筛选仅使用训练与验证数据，继续公布覆盖率。若有候选通过，再用尚未研究的未来区间或新增未使用数据检验。当前账户没有DataDownloader权限，补充该渠道数据需要有效权限；现有OHLC样本足以否决本次候选，但不包含盘口深度、实际成交额或历史逐日保证金/手续费信息。', '',
        '## 文件与复现','',
        'protocol.json记录首轮预声明规则；full-event-protocol.json记录追加原因；selection.json记录45组验证比较和冻结选择；results.json记录成本情景、验证分值上限、数据、代码及权重指纹；各模型成交CSV可逐笔核对。verify_external_15m.py核对预测/权重/输入指纹、精确回放和独立逐笔费用/手数计算，输出核验记录.json；这些核验不等于实际触价成交已证实。模型/输入/预测缓存位于忽略目录.tqsdk/research/external15m，研究依赖位于独立虚拟环境。',
        '依次运行prepare_kronos_research.py、prepare_external_15m_data.py、infer_kronos_15m.py、validate_external_15m.py --meta、train_full_event_filter_15m.py、validate_external_15m.py --evaluate。推理需使用.tqsdk/research/kronos_env/Scripts/python.exe，其余可用原研究环境。', '',
        '官方来源：[Kronos](https://github.com/shiyu-coder/Kronos)、[模型权重](https://huggingface.co/NeoQuasar/Kronos-small)、[事件过滤原始研究](https://hudsonthames.org/does-meta-labeling-add-to-signal-efficacy-triple-barrier-method/)。文献中的结果不等于本项目已得到的结果。']
    (OUT/'验证报告.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--meta',action='store_true');parser.add_argument('--evaluate',action='store_true')
    args=parser.parse_args()
    with threadpool_limits(limits=4):
        if args.meta:meta()
        if args.evaluate:evaluate()
