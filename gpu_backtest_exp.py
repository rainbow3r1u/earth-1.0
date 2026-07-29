#!/usr/bin/env python3
"""
GPU 回测 v5 — 特征滞后/标签对齐实验 (2026-07-18)
基座: gpu_backtest_volfeat.py (v3+volfeat, 90天特征回看)
数据: 修复后的生产缓存 notusdt_1d_full.json (532币, 全量历史, 含n/tbq)

三臂 (NOLAG_MODE 环境变量):
  lag     = 当前生产时序: 预测样本ts=T, 特征蜡烛T-1, 入场T+1开盘 (特征滞后1天)
            训练截止 ts < T (标签收盘 ≤ 入场-1, 与生产一致, 无前视)
  nolag   = 消除滞后: 预测样本ts=T, 特征蜡烛T-1, 入场T开盘
            训练截止 ts ≤ T-2 (标签收盘 ≤ 入场-1, 实盘可得, 无前视)
  aligned = nolag + 标签对齐入场: label=(close[T+2]-open[T])/open[T] (与持仓窗口一致)
            + ab特征改prev_date (同日值22:00 UTC才采集, 在no-lag下构成前视)

注意: volfeat原脚本训练截止 ts ≤ T-1, 其标签在入场日才收盘 → 比实盘多1天信息。
      本脚本 nolag/aligned 臂已按实盘可得性修正, 与 lag 臂公平对比。
用法: NOLAG_MODE=nolag python3 gpu_backtest_nolag.py [days=180] [stride=1]
"""
import os, sys, json, time, gc
from datetime import datetime, timezone
from collections import defaultdict
from multiprocessing import Pool, cpu_count
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import daily_predictor as dp
from xgboost import XGBClassifier

HOME = os.path.expanduser('~')
KLINE_CACHE = f'{HOME}/backtester/data_cache/notusdt_1d_full.json'
OI_CACHE    = f'{HOME}/backtester/data_cache/oi_daily.json'

MODE = os.environ.get('NOLAG_MODE', 'nolag')  # lag | nolag | aligned
assert MODE in ('lag', 'nolag', 'aligned')
BB_FEATS = os.environ.get('BB_FEATS', '0') == '1'  # 布林特征包: 乖离率+%B+带宽 (946维)
VOLRAW_FEATS = os.environ.get('VOLRAW_FEATS', '0') == '1'  # 原始成交额q(不归一化) (945维)
FUND_FEATS = os.environ.get('FUND_FEATS', '0') == '1'  # 单币资金费率原值 (配合VOLRAW=946维)
LONG_MOM_FILTER = os.environ.get('LONG_MOM_FILTER', '0') == '1'  # LONG候选强制高动量: 连涨≥2天+20日位置>0.7
LABEL_1D = os.environ.get('LABEL_1D', '0') == '1'  # 1日标签/24h持仓 (替代48h)
KRONOS_ON = os.environ.get('KRONOS_ON', '0') == '1'  # Kronos 832D复测 (解除置零)
TRAIN_MOM_LONG = os.environ.get('TRAIN_MOM_LONG', '0') == '1'  # LONG训练侧动量过滤: 只用动量样本训练(不改变闸门/样本)
# ===== 实验开关 (gpu_backtest_exp) =====
RANK_MODE = os.environ.get('RANK_MODE', '0') == '1'  # 排序目标: XGBRanker rank:pairwise(group=训练日) + 训练窗Logistic校准出概率
TIME_DECAY = float(os.environ.get('TIME_DECAY', '0'))  # 时间衰减半衰期(天), 0=不加权
DART_ON = os.environ.get('DART', '0') == '1'  # DART booster (树dropout抗噪)
SOUP_ON = os.environ.get('SOUP', '0') == '1'  # 时间集成: 最近3个每日模型概率平均
LGBM_ON = os.environ.get('LGBM', '0') == '1'  # LightGBM对照引擎
PRUNE_COLS = os.environ.get('PRUNE_COLS', '')  # 死特征列清零清单(json数组路径)
XGB_DEVICE = os.environ.get('XGB_DEVICE', 'cuda')  # 主训练设备, cuda/cpu
WINSOR_OFF = os.environ.get('WINSOR_OFF', '0') == '1'  # 全关截尾 (验证winsor压制追涨假设)
WINSOR_Q = float(os.environ.get('WINSOR_Q', '0'))  # >0时改用自定义分位 (如0.001=0.1%/99.9%)
WF_OFFSET = int(os.environ.get('WF_OFFSET', '0'))  # 评估窗口整体前移N天 (稳健性复核: 换时段防单窗口运气)
# lag/nolag 共用同一份样本缓存; aligned 标签不同, 独立缓存
CACHE_DIR = f'{HOME}/backtester/data_cache/by_day_cache_v5' + ('_aligned' if MODE == 'aligned' else '') + ('_bb' if BB_FEATS else '') + ('_volraw' if VOLRAW_FEATS else '') + ('_fund' if FUND_FEATS else '') + ('_1d' if LABEL_1D else '') + ('_kr' if KRONOS_ON else '')

DAYS   = int(sys.argv[1]) if len(sys.argv) > 1 else 180
STRIDE = int(sys.argv[2]) if len(sys.argv) > 2 else 1
N_WORKERS = int(sys.argv[3]) if len(sys.argv) > 3 else cpu_count()

XGB_PARAMS = dict(
    n_estimators=200, max_depth=6, learning_rate=0.05,
    min_child_weight=1, reg_lambda=10, reg_alpha=10,
    subsample=0.8, colsample_bytree=0.6,
    device=XGB_DEVICE,
    eval_metric='logloss', verbosity=0
)
if DART_ON:
    XGB_PARAMS['booster'] = 'dart'
if RANK_MODE:
    XGB_PARAMS.pop('eval_metric', None)  # ranker 用默认 ndcg

_prune_idx = []
if PRUNE_COLS and os.path.exists(PRUNE_COLS):
    with open(PRUNE_COLS) as _pf:
        _prune_idx = [int(c) for c in json.load(_pf)]

_exp_tags = [t for t, on in [('RANK', RANK_MODE), ('DECAY' + str(int(TIME_DECAY)), TIME_DECAY > 0),
             ('DART', DART_ON), ('SOUP', SOUP_ON), ('LGBM', LGBM_ON),
             ('PRUNE', bool(_prune_idx)), ('NOWIN', WINSOR_OFF),
             ('WINQ' + str(WINSOR_Q), WINSOR_Q > 0), ('OFF' + str(WF_OFFSET), WF_OFFSET > 0)] if on]
exp_label = '+'.join(_exp_tags) if _exp_tags else 'BASELINE'


def _quantile_bounds(X, q):
    """自定义分位截尾边界 (partition加速, 与dp._fast_winsor_bounds同逻辑, 分位可调)"""
    n, m = X.shape
    k1 = max(0, int(n * q))
    k99 = min(n - 1, int(n * (1 - q)))
    bounds = []
    Xc = X.copy()
    for j in range(m):
        col = Xc[:, j]
        col.partition([k1, k99])
        bounds.append((float(col[k1]), float(col[k99])))
    return bounds
TRAIN_WINDOW = 180; PROB_THRESHOLD = 60.0
STOP_LOSS = float(os.environ.get('SL_PCT', 10.0)); TAKE_PROFIT = 10.0  # SL_PCT环境变量可改止损
MIN_VOLUME = 500000; TRADE_COST = float(os.environ.get('COST_PCT', 0.5))  # COST_PCT环境变量: 成本敏感性测试
SLIP_SL = float(os.environ.get('SLIP_SL', 0))  # 止损额外滑点期望% (实盘: 25%概率滑67% → 期望+1.2%)

_cached_bounds = None; _cached_bounds_key = None

def log(msg): print(f'[{time.strftime("%H:%M:%S")}] {msg}', flush=True)

def load_klines_offline():
    with open(KLINE_CACHE) as f: raw = json.load(f).get('klines', {})
    _mk = int(os.environ.get("MIN_KLINES", 90))
    log(f"MIN_KLINES={_mk}, 币种={sum(1 for k in raw.values() if len(k) >= _mk)}")
    return {s: k for s, k in raw.items() if len(k) >= _mk}

def load_oi_offline(klines):
    oi = {}
    if os.path.exists(OI_CACHE):
        with open(OI_CACHE) as f: raw = json.load(f)
        for sym in klines:
            if sym in raw and raw[sym]:
                oi[sym] = {int(k): float(v) for k, v in raw[sym].items()}
    return oi

# ============ 并行样本构建 ============
def _build_coin_samples(args):
    sym, kls, oi_map, btc_rets, sector_map, sector_heats, fund_rows = args

    if KRONOS_ON:
        # Kronos复测: 加载832维嵌入缓存 (每个worker独立加载)
        if not getattr(dp, '_kr_features', None):
            _kf = f'{HOME}/websocket_new/data/kronos_features_cache.json'
            if os.path.exists(_kf):
                with open(_kf) as f:
                    _cache = json.load(f)
                dp._kr_features = {int(k): v[:dp.EMBEDDING_DIM] for k, v in _cache.items()}
    else:
        dp._kr_features = {}

    dp._etf_features = dp._load_etf_features()
    dp._chain_features = dp._load_chain_features()
    dp._sent_features = dp._load_sent_features()
    dp._fg_features = dp._load_fear_greed()
    dp._st_features = dp._load_stablecoin_netflow()
    dp._cb_features = dp._load_coinbase_premium()
    dp._cbg_features = dp._load_cb_gap_features()
    dp._bd_features = dp._load_btc_mcap()
    dp._kg_features = dp._load_korea_premium()
    dp._hr_features = dp._load_hashrate_features()
    dp._liq_features = dp._load_liquidation_features()
    dp._tvl_features = dp._load_chain_tvl()
    dp._ma_features = dp._load_macro_assets()
    dp._ab_features = dp._load_btc_dominance_proxy()
    dp._sector_map_cache = sector_map
    try:
        with open(f'{HOME}/defillama_data/protocol_map.json') as f:
            dp._proto_map_local = {k: v[0] for k, v in json.load(f).items()}
    except: pass

    c = [k['c'] for k in kls]; o_ = [k['o'] for k in kls]
    hh = [k['h'] for k in kls]; ll = [k['l'] for k in kls]
    v_ = [k['q'] for k in kls]
    n_ = [k['n'] if 'n' in k else 0 for k in kls]
    tbq_ = [k.get('tbq') for k in kls]
    ts_list = [k['t']//1000 for k in kls]
    crets = dp._compute_returns(c)
    n = len(kls)
    # fund_raw: 单币费率原值 (取样本日前最近一次8h结算, 无前视)
    import bisect as _bisect
    fund_times = [r[0] for r in fund_rows] if FUND_FEATS else []
    fund_rates = [r[1] for r in fund_rows] if FUND_FEATS else []

    samples = []
    i_end = (n - 1 if LABEL_1D else n - 2) if MODE == 'aligned' else n - 1  # aligned需c[i_+2]; 1d标签只需c[i_+1]
    for i_ in range(25, i_end):
        j = i_ - 1
        try:
            r1d = (c[j]-c[j-1])/c[j-1] if c[j-1]>0 else 0
            r3d = (c[j]-c[max(0,j-3)])/c[max(0,j-3)] if c[max(0,j-3)]>0 else 0
            r5d = (c[j]-c[max(0,j-5)])/c[max(0,j-5)] if c[max(0,j-5)]>0 else 0
            r20 = [(c[k]-c[k-1])/c[k-1] if c[k-1]>0 else 0 for k in range(j-18,j+1)] if j>=20 else [0]
            v20d = float(np.std(r20)) if j>=20 else 0.02
            clip = max(v20d, 0.002)
            r1n = round(r1d/clip,4); r3n = round(r3d/(clip*1.732),4); r5n = round(r5d/(clip*2.236),4)
            vol = np.std([(c[k]-c[k-1])/c[k-1] if c[k-1]>0 else 0 for k in range(j-3,j+1)]) if j>=5 else 0.02
            vr = v_[j]/np.mean(v_[max(0,j-5):j]) if j>=5 and np.mean(v_[max(0,j-5):j])>0 else 1
            c20 = c[j-19:j+1] if j>=20 else [0,1]
            pp = (c[j]-min(c20))/(max(c20)-min(c20)) if max(c20)!=min(c20) else 0.5
            amp = (hh[j]-ll[j])/o_[j] if o_[j]>0 else 0
            stk = 0
            for k_ in range(j, max(0, j-7) - 1, -1):
                if c[k_] > o_[k_]: stk += 1
                else: break
            ds = 1 if(c[j]>c[j-3] and v_[j]<v_[j-3]*0.7) else 0
            ts = ts_list[i_]
            oin = oi_map.get(ts_list[j],0); oip = oi_map.get(ts_list[j-1],0)
            oic = (oin-oip)/oip if oip>0 else 0
            if sym=='BTCUSDT': b,a_,r2_,res = 1.0,0.0,1.0,0.0
            else: b,a_,r2_,res = dp._regression_features(btc_rets, crets, j)
            sfeat = dp._get_sector_features(sym, ts-86400, sector_map, sector_heats)
            mfeat = dp._get_macro_features(ts); mfeat = dp._apply_chain_tvl(mfeat, sym, ts)
            if MODE == 'aligned':
                # ab改prev_date: 同日值22:00 UTC才采集, 在no-lag入场(T开盘)时不可得
                mfeat[-1] = dp._ab_features.get(int(ts-86400), [0.0])[0]
            # 标签: lag/nolag=2日收益(从j收盘); aligned=入场开盘→T+2收盘; LABEL_1D=入场开盘→T+1收盘(24h)
            if MODE == 'aligned':
                if LABEL_1D:
                    nr = (c[i_+1]-o_[i_])/o_[i_] if o_[i_]>0 and i_+1<n else 0
                else:
                    nr = (c[i_+2]-o_[i_])/o_[i_] if o_[i_]>0 and i_+2<n else 0
            else:
                nr = (c[i_+1]-c[j])/c[j] if c[j]>0 and i_+1<n else 0
            if abs(nr)>5.0: continue
            rsi7=dp._compute_rsi(c,7,j); rsi14=dp._compute_rsi(c,14,j); rsi30=dp._compute_rsi(c,30,j)
            rsi90=dp._compute_rsi(c,90,j) if j>=90 else 50.0
            r90=[(c[k]-c[k-1])/c[k-1] if c[k-1]>0 else 0 for k in range(j-88,j+1)] if j>=90 else [0]
            v90d=float(np.std(r90)) if j>=90 else 0.02
            c90=c[j-89:j+1] if j>=90 else [0,1]
            pp90=(c[j]-min(c90))/(max(c90)-min(c90)) if j>=90 and max(c90)!=min(c90) else 0.5
            r30d=(c[j]-c[max(0,j-30)])/c[max(0,j-30)] if c[max(0,j-30)]>0 else 0
            r60d=(c[j]-c[max(0,j-60)])/c[max(0,j-60)] if c[max(0,j-60)]>0 else 0
            r90d_ret=(c[j]-c[max(0,j-90)])/c[max(0,j-90)] if c[max(0,j-90)]>0 else 0
            rsi14s=dp._compute_rsi_series(c,14)
            rsi_div=dp._compute_rsi_divergence(c,rsi14s,j,window=20)
            tr_ratio = n_[j]/np.mean(n_[max(0,j-5):j]) if j>=5 and np.mean(n_[max(0,j-5):j])>0 else 1
            _tb = tbq_[j]
            tbr = _tb/v_[j] if (_tb is not None and v_[j]>0) else 0.5
            vol_col=dp._compute_vol_clustering(c,j)
            feat = [r1n,r3n,r5n,vol,vr,pp,amp,stk,ds,oic]+vol_col+[b,a_,r2_,res,rsi7,rsi14,rsi30]+rsi_div+sfeat+mfeat+[rsi90,v90d,pp90,r30d,r60d,r90d_ret]+[tr_ratio,tbr]
            if BB_FEATS:
                # 布林特征包 (20日, 2σ): 乖离率(独立信息量) + %B + 带宽
                c20_ = c[j-19:j+1] if j>=19 else c[:j+1]
                ma20 = float(np.mean(c20_)); sd20 = float(np.std(c20_))
                up_, lo_ = ma20+2*sd20, ma20-2*sd20
                bias20 = c[j]/ma20 - 1 if ma20>0 else 0
                pct_b = (c[j]-lo_)/(up_-lo_) if up_>lo_ else 0.5
                bb_width = (up_-lo_)/ma20 if ma20>0 else 0
                feat = feat + [round(bias20,5), round(pct_b,5), round(bb_width,5)]
            if VOLRAW_FEATS:
                feat = feat + [v_[j]]  # 原始成交额q(USDT), 不平均/不归一
            if FUND_FEATS:
                # 单币资金费率原值: 样本日前最近一次8h结算 (无前视, 无数据=0)
                _fi = _bisect.bisect_right(fund_times, ts * 1000) - 1
                feat = feat + [fund_rates[_fi] if _fi >= 0 else 0.0]
            ll_=1 if nr>0.05 else 0; ls_=1 if nr<-0.05 else 0
            samples.append((ts,sym,feat,ll_,ls_,nr*100))
        except: continue
    return samples

def build_samples_parallel(klines, oi_data, sector_map, sector_heats, btc_rets, n_workers, fund_data=None):
    fund_data = fund_data or {}
    tasks = []
    for sym, kls in klines.items():
        if len(kls) < 30: continue
        tasks.append((sym, kls, oi_data.get(sym, {}), btc_rets, sector_map, sector_heats, fund_data.get(sym, [])))

    log(f'并行构建: {len(tasks)} 币种 × {n_workers} workers (MODE={MODE})')
    t0 = time.time()
    with Pool(n_workers) as pool:
        results = pool.map(_build_coin_samples, tasks)

    by_day = defaultdict(list)
    total = 0
    for samples in results:
        for ts, sym, feat, ll_, ls_, ret in samples:
            by_day[ts].append((sym, feat, ll_, ls_, ret))
            total += 1
    sdays = sorted(by_day.keys())
    log(f'样本构建完成: {total}条 × {len(sdays)}天 ({time.time()-t0:.0f}s)')

    os.makedirs(CACHE_DIR, exist_ok=True)
    for ts in sdays:
        samples = by_day[ts]
        feats = np.array([s[1] for s in samples], dtype=np.float32)
        labels = np.array([[s[2], s[3], s[4]] for s in samples], dtype=np.float32)
        syms  = np.array([s[0] for s in samples])
        np.savez_compressed(f'{CACHE_DIR}/{ts}.npz', feats=feats, labels=labels, syms=syms)
    log(f'预序列化: {len(sdays)}天 → {CACHE_DIR}')
    return sdays

# ============ 回测核心 ============
def train_and_predict_batch(train_ts_list, pred_ts, entry_ts, klines, soup_hist=None):
    X_train, yL, yS, _grp, _wts = [], [], [], [], []
    _tmax = max(train_ts_list) if train_ts_list else 0
    for ts in train_ts_list:
        cache_file = f'{CACHE_DIR}/{ts}.npz'
        if not os.path.exists(cache_file): continue
        try:
            data = np.load(cache_file)
            f_ = data['feats']
            X_train.append(f_)
            lbl = data['labels']
            yL.append(lbl[:, 0]); yS.append(lbl[:, 1])
            _grp.append(len(f_))
            if TIME_DECAY > 0:
                _wts.append(np.full(len(f_), 0.5 ** ((_tmax - ts) / (TIME_DECAY * 86400.0))))
        except: continue

    if not X_train: return None
    X_train = np.concatenate(X_train).astype(np.float32)
    yL = np.concatenate(yL).astype(np.int32); yS = np.concatenate(yS).astype(np.int32)
    sw = np.concatenate(_wts) if _wts else None
    if sw is not None:
        sw = sw / sw.mean()  # 归一到均值1, 不改变有效样本量
    X_train = np.nan_to_num(X_train, nan=0.0, copy=False)
    if not KRONOS_ON:
        X_train[:, 100:932] = 0.0   # Kronos置零 (与生产一致)
    X_train[:, 72:91] = 0.0     # liq 19维置零 (与生产一致)
    if _prune_idx:
        X_train[:, _prune_idx] = 0.0  # 死特征清零实验

    pL = int(yL.sum()); pS = int(yS.sum())
    if pL < 5 or pS < 5: return None

    global _cached_bounds, _cached_bounds_key
    if WINSOR_OFF:
        bounds = None  # 全关截尾
    elif WINSOR_Q > 0:
        bounds_key = ('q', WINSOR_Q, len(X_train), pL, pS)
        if _cached_bounds is None or _cached_bounds_key != bounds_key:
            _cached_bounds = _quantile_bounds(X_train, WINSOR_Q)
            _cached_bounds_key = bounds_key
        bounds = _cached_bounds
    else:
        bounds_key = (len(X_train), pL, pS)
        if _cached_bounds is None or _cached_bounds_key != bounds_key:
            _cached_bounds = dp._fast_winsor_bounds(X_train)
            _cached_bounds_key = bounds_key
        bounds = _cached_bounds
    X_train = dp._apply_winsor(X_train, bounds)

    def _fit_model(X, y, pos, rs):
        """按实验开关选择引擎/目标训练一侧模型"""
        spw = (len(y) - pos) / pos if pos > 0 else 1
        if LGBM_ON:
            from lightgbm import LGBMClassifier
            m = LGBMClassifier(n_estimators=200, max_depth=6, learning_rate=0.05,
                               min_child_weight=1, reg_lambda=10, reg_alpha=10,
                               subsample=0.8, colsample_bytree=0.6,
                               scale_pos_weight=spw, random_state=rs, verbosity=-1, n_jobs=4)
            m.fit(X, y, sample_weight=sw)
            return m
        if RANK_MODE:
            from xgboost import XGBRanker
            from sklearn.linear_model import LogisticRegression
            qid = np.repeat(np.arange(len(_grp)), _grp)
            rk = XGBRanker(objective='rank:pairwise', random_state=rs, **XGB_PARAMS)
            rk.fit(X, y, qid=qid, sample_weight=sw)
            # 训练窗内 Logistic 校准: rank分数→概率 (只用训练窗, 无泄露)
            cal = LogisticRegression(max_iter=1000)
            cal.fit(rk.predict(X).reshape(-1, 1), y)

            class _RankProb:
                def predict_proba(self, Xp):
                    p1 = cal.predict_proba(rk.predict(Xp).reshape(-1, 1))[:, 1]
                    return np.column_stack([1 - p1, p1])
            return _RankProb()
        m = XGBClassifier(**XGB_PARAMS, scale_pos_weight=spw, random_state=rs)
        m.fit(X, y, sample_weight=sw)
        return m

    if TRAIN_MOM_LONG:
        # LONG训练侧动量过滤: 只用 连涨≥2天+20日位置>0.7 的样本训练, 让模型天生学追涨 (无闸门)
        _mask = (X_train[:, 7] >= 2) & (X_train[:, 5] > 0.7)
        X_tr_l, y_tr_l = X_train[_mask], yL[_mask]
        _pL2 = int(y_tr_l.sum()); _nL2 = len(y_tr_l) - _pL2
        if _pL2 >= 5 and _nL2 >= 5 and len(y_tr_l) >= 20:
            try:
                _p = dict(XGB_PARAMS); _p['device'] = 'cpu'  # 子集训练走CPU, 避免GPU dense矩阵断言崩溃
                ml = XGBClassifier(**_p, scale_pos_weight=_nL2/_pL2, random_state=42)
                ml.fit(np.ascontiguousarray(X_tr_l), y_tr_l)
            except Exception:
                ml = None
        else:
            ml = None
    else:
        ml = _fit_model(X_train, yL, pL, 42)
    ms = _fit_model(X_train, yS, pS, 43)

    cache_file = f'{CACHE_DIR}/{pred_ts}.npz'
    if not os.path.exists(cache_file): return None
    try:
        data = np.load(cache_file)
        X_pred = data['feats'].astype(np.float32)
        pred_labels = data['labels']
        pred_syms = data['syms']
    except: return None

    X_pred = np.nan_to_num(X_pred, nan=0.0, copy=False)
    if not KRONOS_ON:
        X_pred[:, 100:932] = 0.0
    X_pred[:, 72:91] = 0.0
    if _prune_idx:
        X_pred[:, _prune_idx] = 0.0

    # SOUP: 时间集成, 最近3个每日模型各自用自己的winsor边界, 概率取平均
    if SOUP_ON and soup_hist is not None:
        soup_hist.append((ml, ms, bounds))
        model_sets = soup_hist[-3:]
    else:
        model_sets = [(ml, ms, bounds)]
    pl = np.zeros(len(X_pred)); ps = np.zeros(len(X_pred))
    for _ml, _ms, _b in model_sets:
        _Xp = dp._apply_winsor(X_pred.copy(), _b)
        if _ml is not None:
            pl += _ml.predict_proba(_Xp)[:, 1]
        ps += _ms.predict_proba(_Xp)[:, 1]
    pl /= len(model_sets); ps /= len(model_sets)

    # 成交量过滤用预测样本日 (与生产 _filter_valid_samples 一致)
    def _mom_ok(kd, ki):
        """LONG高动量过滤: 特征蜡烛(ki-1)连涨≥2天 且 20日价格位置>0.7"""
        j_ = ki - 1
        if j_ < 20: return False
        streak = 0
        for k_ in range(j_, max(0, j_-7)-1, -1):
            if kd[k_]['c'] > kd[k_]['o']: streak += 1
            else: break
        c20 = [x['c'] for x in kd[j_-19:j_+1]]
        if max(c20) == min(c20): return False
        pp = (kd[j_]['c'] - min(c20)) / (max(c20) - min(c20))
        return streak >= 2 and pp > 0.7

    bl = None; bs = None
    for idx, (plv, psv) in enumerate(zip(pl, ps)):
        sym = str(pred_syms[idx]); ret = pred_labels[idx, 2]
        kd = klines.get(sym, [])
        if len(kd) < 30: continue
        ki = dp._find_kline_index(kd, pred_ts)
        if ki is None or ki < 5: continue
        vol5 = np.mean([k['q'] for k in kd[ki-5:ki]])
        if vol5 < MIN_VOLUME: continue
        if bl is None or plv > bl[1]:
            if not LONG_MOM_FILTER or _mom_ok(kd, ki):
                bl = (sym, plv, ret)
        if bs is None or psv > bs[1]: bs = (sym, psv, ret)

    lp = bl[1]*100 if bl else 0; sp = bs[1]*100 if bs else 0
    # RANK臂: 校准概率与分类器不同尺度(60阈值不适用), 且Logistic校准单调不改排序, 免阈值纯比排序质量
    if not RANK_MODE and max(lp, sp) < PROB_THRESHOLD: return None
    if RANK_MODE and bl is None and bs is None: return None

    if bl and (not bs or lp >= sp): direction = 'long'; sym, prob, ret = bl
    else: direction = 'short'; sym, prob, ret = bs

    # 入场: entry_ts 开盘 (lag=样本日+1, nolag/aligned=样本日当天)
    kd = klines.get(sym, []); ki = dp._find_kline_index(kd, entry_ts)
    if ki is None or ki >= len(kd)-2: return None
    ep = kd[ki]['o']
    pnl = 0; hit = False; reason = 'hold'
    for off in ([1] if LABEL_1D else [1, 2]):
        i2 = ki + off
        if i2 >= len(kd): continue
        k = kd[i2]; h = k['h']; l = k['l']
        if direction == 'long':
            if l <= ep*(1-STOP_LOSS/100): pnl=-STOP_LOSS; hit=True; reason='stop'; break
            if h >= ep*(1+TAKE_PROFIT/100): pnl=TAKE_PROFIT; hit=True; reason='take'; break
        else:
            if h >= ep*(1+STOP_LOSS/100): pnl=-STOP_LOSS; hit=True; reason='stop'; break
            if l <= ep*(1-TAKE_PROFIT/100): pnl=TAKE_PROFIT; hit=True; reason='take'; break
    if not hit:
        k2 = kd[ki + (1 if LABEL_1D else 2)]; c2 = k2['c']
        if direction == 'long': pnl = (c2/ep-1)*100
        else: pnl = (1-c2/ep)*100
        pnl = max(-STOP_LOSS, min(TAKE_PROFIT, pnl))
    if reason == 'stop':
        pnl -= SLIP_SL  # 止损额外滑点 (实盘观测: 25%概率滑到1.67倍 → 期望额外~1.2%)
    pnl -= TRADE_COST

    day_str = datetime.fromtimestamp(entry_ts, tz=timezone.utc).strftime('%Y-%m-%d')
    return {'day': day_str, 'direction': direction, 'symbol': sym,
            'prob': f'{prob:.1f}', 'pnl': pnl, 'stopped': hit, 'exit_reason': reason}

def run_walk_forward(sdays, klines):
    END = len(sdays) - 1 - WF_OFFSET
    assert END > 60, f'WF_OFFSET={WF_OFFSET} 过大'
    START = max(30, END - DAYS)
    tasks = []
    for d in range(START, END, STRIDE):
        if MODE == 'lag':
            # 生产时序: 预测样本sdays[d], 入场sdays[d+1]; 训练截止 ts<sdays[d] (与生产一致)
            pred_ts = sdays[d]
            entry_ts = sdays[d+1] if d+1 < len(sdays) else sdays[d]
            train_ts = sdays[max(0, d-TRAIN_WINDOW):d]
        elif MODE == 'nolag':
            # 预测+入场 sdays[d]; 训练截止 ts≤sdays[d-2] (标签收盘≤入场-1, 实盘可得)
            pred_ts = entry_ts = sdays[d]
            train_ts = sdays[max(0, d-TRAIN_WINDOW):d-1]
        else:  # aligned: 标签2日, 需c[k+2]≤入场-1 → 截止 ts≤sdays[d-3]
            pred_ts = entry_ts = sdays[d]
            train_ts = sdays[max(0, d-TRAIN_WINDOW):d-2]
        tasks.append((train_ts, pred_ts, entry_ts))

    log(f'Walk-Forward: {len(tasks)} 预测日, stride={STRIDE}, MODE={MODE}')
    trades = []
    soup_hist = []
    for i, (train_ts, pred_ts, entry_ts) in enumerate(tasks):
        result = train_and_predict_batch(train_ts, pred_ts, entry_ts, klines, soup_hist)
        if result: trades.append(result)
        if (i+1) % 10 == 0:
            cum = sum(t['pnl'] for t in trades)
            wins = sum(1 for t in trades if t['pnl'] > 0)
            log(f'  {i+1}/{len(tasks)} cum={cum:+.1f}% win={wins}/{len(trades)}')
        gc.collect()
    return trades

def print_summary(trades, elapsed):
    if not trades: log('无交易'); return None
    cum = sum(t['pnl'] for t in trades)
    wins = sum(1 for t in trades if t['pnl'] > 0)
    stops = sum(1 for t in trades if t.get('exit_reason')=='stop')
    takes = sum(1 for t in trades if t.get('exit_reason')=='take')
    rets = [t['pnl'] for t in trades]
    sharpe = (np.mean(rets)/(np.std(rets)+1e-6))*np.sqrt(365) if len(rets)>1 else 0
    eq = 100; peak = 100; dd = 0
    for t in trades: eq *= (1+t['pnl']/100); peak = max(peak, eq); dd = max(dd, (peak-eq)/peak*100)
    d0 = trades[0]['day']; d1 = trades[-1]['day']
    sep = '='*60
    print(f'\n{sep}')
    print(f'回测完成 ({DAYS}d stride={STRIDE} MODE={MODE} EXP={exp_label})')
    print(f'  Sharpe={sharpe:.2f}  Cum={cum:+.1f}%  MaxDD={dd:.1f}%')
    print(f'  Trades={len(trades)}  Win={wins}/{len(trades)}({wins/len(trades)*100:.0f}%) T/ST={takes}/{stops}')
    print(f'  Period: {d0} → {d1}  耗时: {elapsed/60:.0f}min')
    print(f'{sep}')
    return {'sharpe': sharpe, 'cum': cum, 'max_dd': dd, 'trades': len(trades),
            'win_rate': f'{wins/len(trades)*100:.0f}%', 'takes': takes, 'stops': stops}

if __name__ == '__main__':
    t0 = time.time()
    log(f'=== GPU回测 v5 (滞后/标签对齐实验) | MODE={MODE} | {DAYS}d stride={STRIDE} | {N_WORKERS}workers ===')

    klines = load_klines_offline()
    fund_data = {}
    if FUND_FEATS:
        _fp = f'{HOME}/backtester/data_cache/funding_hist.json'
        if os.path.exists(_fp):
            with open(_fp) as f:
                fund_data = {s: sorted(rows) for s, rows in json.load(f).items()}
            log(f'费率历史: {len(fund_data)}币')
        else:
            log(f'⚠️ 费率文件缺失: {_fp}, fund_raw 全为0')
    oi_data = load_oi_offline(klines)
    sector_map = dp._load_sector_map()
    dp._sector_map_cache = sector_map
    sector_heats = dp._precompute_sector_heats(klines, sector_map) if sector_map else {}
    btc_kls = klines.get('BTCUSDT', [])
    btc_rets = dp._compute_returns([k['c'] for k in btc_kls]) if len(btc_kls) > 1 else []

    sdays = None
    if os.path.isdir(CACHE_DIR) and any(f.endswith('.npz') for f in os.listdir(CACHE_DIR)):
        sdays = sorted(int(f.replace('.npz','')) for f in os.listdir(CACHE_DIR) if f.endswith('.npz'))
        log(f'缓存命中: {len(sdays)}天 ({CACHE_DIR})')
    if not sdays:
        sdays = build_samples_parallel(klines, oi_data, sector_map, sector_heats, btc_rets, N_WORKERS, fund_data)

    trades = run_walk_forward(sdays, klines)
    summary = print_summary(trades, time.time()-t0)

    if trades and summary:
        out = f'{HOME}/websocket_new/data/gpu_backtest_v5_{MODE}_{DAYS}d_{exp_label}.json'
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, 'w') as f:
            json.dump({**summary, 'config': {'mode': MODE, 'days': DAYS, 'stride': STRIDE,
                       'xgb': {k: str(v) for k, v in XGB_PARAMS.items()}}, 'trades': trades}, f, indent=2, default=str)
        log(f'结果: {out}')
