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
import auto_dual_trade as adt  # 生产同源特征构建 (2026-08-01)
from xgboost import XGBClassifier

HOME = os.path.expanduser('~')
KLINE_CACHE = f'{HOME}/backtester/data_cache/notusdt_1d_full.json'
OI_CACHE    = f'{HOME}/backtester/data_cache/oi_daily.json'

MODE = os.environ.get('NOLAG_MODE', 'nolag')  # lag | nolag | aligned
assert MODE in ('lag', 'nolag', 'aligned')
BB_FEATS = os.environ.get('BB_FEATS', '0') == '1'  # 布林特征包: 乖离率+%B+带宽 (946维)
VOLRAW_FEATS = os.environ.get('VOLRAW_FEATS', '0') == '1'  # 原始成交额q(不归一化) (945维)
FUND_FEATS = os.environ.get('FUND_FEATS', '0') == '1'  # 单币资金费率原值 (配合VOLRAW=946维)
RAW_RET_FEATS = os.environ.get('RAW_RET_FEATS', '0') == '1'  # 原始涨幅r1d/r3d/r5d(不归一化, 保留绝对幅度; 946→949维)
EXT_FEATS = os.environ.get('EXT_FEATS', '0') == '1'  # 量能见顶家族+残差家族扩展(946→953维): vol_pct/days_climax/vol_decline/climax_red/vol_res/res_3d/res_5d
DIV_FEATS = os.environ.get('DIV_FEATS', '0') == '1'  # 背离家族(连续测量仪版): pv_corr_20d/pv_slope_div/high_vol_ratio/obv_slope_div
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
NAN_RAW = os.environ.get('NAN_RAW', '0') == '1'  # 保留NaN让XGB原生处理(缺失本身即信息), 默认nan_to_num填零
WINSOR_OFF = os.environ.get('WINSOR_OFF', '0') == '1'  # 全关截尾 (验证winsor压制追涨假设)
WINSOR_Q = float(os.environ.get('WINSOR_Q', '0'))  # >0时改用自定义分位 (如0.001=0.1%/99.9%)
WF_OFFSET = int(os.environ.get('WF_OFFSET', '0'))  # 评估窗口整体前移N天 (稳健性复核: 换时段防单窗口运气)
ENTRY_SLIP = float(os.environ.get('ENTRY_SLIP', '0'))
FEAT_SHIFT = int(os.environ.get('FEAT_SHIFT', '0'))  # 阴性对照: 特征前移N天(故意前视), 管道正确则此臂必须爆炸到~100%胜率  # 入场价不利漂移%: LONG按开盘价*(1+x)成交, SHORT*(1-x) — 检验5分钟延迟的真实成本
# ==== 2026-09-01 LONG改造实验臂 (优化待办⭐) ====
XS_RANK = os.environ.get('XS_RANK', '0') == '1'       # 横截面排名特征+BTC regime特征 追加8维 (治"看不见相对位置")
RESIDUAL_LABEL = os.environ.get('RESIDUAL_LABEL', '0') == '1'  # LONG标签残差化: (币ret - 当日宇宙中位ret) > +5% (治"beta假阳性", SHORT标签不动)
XS_N_FEATS = 8
# ==== 2026-09-01 P0: 训练/推理不对称 — 训练样本同款流动性过滤 (治"死币稀释训练集") ====
VOLUME_FILTER = float(os.environ.get('VOLUME_FILTER', '0'))  # >0=阈值U: 样本日前5日均成交额<U的样本不入训练集 (口径=生产_filter_valid_samples); 敏感性300k/500k/1000k
SEED_OFFSET = int(os.environ.get('SEED_OFFSET', '0'))  # 种子噪声对照: 同数据只换XGB随机种子, 量化指标自身的抖动底
# lag/nolag 共用同一份样本缓存; aligned 标签不同, 独立缓存
CACHE_DIR = f'{HOME}/backtester/data_cache/by_day_cache_v5' + ('_aligned' if MODE == 'aligned' else '') + ('_bb' if BB_FEATS else '') + ('_volraw' if VOLRAW_FEATS else '') + ('_fund' if FUND_FEATS else '') + ('_1d' if LABEL_1D else '') + ('_kr' if KRONOS_ON else '') + ('_rawr' if RAW_RET_FEATS else '') + ('_ext' if EXT_FEATS else '') + ('_div' if DIV_FEATS else '') + ('_xsr' if XS_RANK else '') + ('_resl' if RESIDUAL_LABEL else '') + os.environ.get('CACHE_SUFFIX', '')  # CACHE_SUFFIX: 特殊宇宙(如MIN_KLINES=35)隔离缓存防污染

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
             ('WINQ' + str(WINSOR_Q), WINSOR_Q > 0), ('OFF' + str(WF_OFFSET), WF_OFFSET > 0), ('ESLIP' + str(ENTRY_SLIP), ENTRY_SLIP > 0), ('FSHIFT' + str(FEAT_SHIFT), FEAT_SHIFT > 0),
             ('RAWR', RAW_RET_FEATS), ('NAN', NAN_RAW), ('EXT', EXT_FEATS), ('DIV', DIV_FEATS),
             ('XSR', XS_RANK), ('RESL', RESIDUAL_LABEL),
             ('VF' + str(int(VOLUME_FILTER//1000)) + 'k', VOLUME_FILTER > 0)] if on]
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
        lo = float(col[k1])
        hi = float(col[k99])
        if lo == 0.0 and hi == 0.0:
            # 与生产dp._fast_winsor_bounds一致: 稀疏列分位全零时回退min/max, 防抹掉真实信号
            col_min = float(col.min())
            col_max = float(col.max())
            if col_min < 0.0 or col_max > 0.0:
                lo, hi = col_min, col_max
        bounds.append((lo, hi))
    return bounds
TRAIN_WINDOW = 180; PROB_THRESHOLD = 60.0
STOP_LOSS = float(os.environ.get('SL_PCT', 10.0)); TAKE_PROFIT = float(os.environ.get('TP_PCT', 10.0))  # SL_PCT/TP_PCT环境变量可改止损止盈
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

# ============ 并行样本构建 (生产同源) ============
def _build_coin_samples(args):
    """生产同源特征构建 (2026-08-01 修复):
    回测特征矩阵改用 auto_dual_trade._build_feat_impl, 与生产完全一致。
    此前两套独立实现导致回测自训模型输出饱和(90~100%)而生产只有54~71%,
    回测水位(Sharpe 17~20)不可信。"""
    sym, kls, oi_map, btc_rets, sector_map, sector_heats, fund_rows, btc_vols = args

    # dp 外部特征加载 (每个worker独立加载, 与生产 _build_feat_impl 依赖一致)
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

    if len(kls) < 35:
        return []
    res = adt._build_feat_impl(sym, kls, oi_map, btc_rets, sector_map, sector_heats)
    out = []
    for ts, s, feat, ll_, ls_, ret in res:
        if ll_ == 0 and ls_ == 0 and abs(ret) < 1e-9:
            continue  # 预测样本/标签未实现样本: 与原回测一致, 训练仅用标签可实现样本
        out.append((ts, s, feat, ll_, ls_, ret))
    return out

def build_samples_parallel(klines, oi_data, sector_map, sector_heats, btc_rets, n_workers, fund_data=None):
    # 生产同源模式: 依赖独立特征构建的实验臂不可用 (KRONOS已禁用/其余已证伪关闭)
    _unsupported = [
        (KRONOS_ON, 'KRONOS_ON'), (BB_FEATS, 'BB_FEATS'), (RAW_RET_FEATS, 'RAW_RET_FEATS'),
        (EXT_FEATS, 'EXT_FEATS'), (DIV_FEATS, 'DIV_FEATS'), (LABEL_1D, 'LABEL_1D'),
    ]
    _on = [n for v, n in _unsupported if v]
    if FEAT_SHIFT != 0:
        _on.append('FEAT_SHIFT')
    if _on:
        raise SystemExit(f'生产同源构建模式不支持实验臂: {", ".join(_on)} (已证伪关闭或需独立构建, 2026-08-01)')
    fund_data = fund_data or {}
    btc_vols = [k['q'] for k in klines.get('BTCUSDT', [])]
    tasks = []
    for sym, kls in klines.items():
        if len(kls) < 30: continue
        tasks.append((sym, kls, oi_data.get(sym, {}), btc_rets, sector_map, sector_heats, fund_data.get(sym, []), btc_vols))

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

    # ==== 2026-09-01 LONG改造臂: 落盘前做横截面增强 (XS_RANK / RESIDUAL_LABEL) ====
    if XS_RANK or RESIDUAL_LABEL:
        # BTC regime 量 (按样本日, 全市场共享): 5日已实现波动 / 距30日高回撤 / 宇宙动量因子
        btc_kls_ = klines.get('BTCUSDT', [])
        btc_idx = {k['t']: i for i, k in enumerate(btc_kls_)}
        DAY_MS = 86400000
        xs_stats = {}   # ts -> (btc_vol5, btc_dd30, uni_mom_prev)
        for ts in sdays:
            bt_i = btc_idx.get(ts)
            v5 = 0.0; dd30 = 0.0
            if bt_i is not None and bt_i >= 5:
                rets5 = [btc_kls_[j]['c']/btc_kls_[j]['o'] - 1 for j in range(bt_i-4, bt_i+1)]
                m = sum(rets5)/5
                v5 = (sum((r-m)**2 for r in rets5)/5) ** 0.5 * 100
            if bt_i is not None and bt_i >= 30:
                hi30 = max(k['h'] for k in btc_kls_[bt_i-29:bt_i+1])
                dd30 = (hi30 - btc_kls_[bt_i]['c']) / hi30 * 100
            # 宇宙动量因子: 用前一日已实现的横截面next_ret中位 (当日入场时点可知, 无前视;
            # 用当日会泄漏48h收益信息 — 训练日当日next_ret在08:21入场时未知)
            ts_prev = ts - DAY_MS
            day_rets_prev = [s[4] for s in by_day.get(ts_prev, []) if abs(s[4]) > 1e-9]
            uni_mom = float(np.median(day_rets_prev)) if day_rets_prev else 0.0
            xs_stats[ts] = (v5, dd30, uni_mom)
        log(f'XS增强: BTC regime + 横截面rank 预计算完成 ({len(xs_stats)}天)')

        new_by_day = {}
        for ts in sdays:
            samples = by_day[ts]
            n = len(samples)
            syms = [s[0] for s in samples]
            feats = [list(s[1]) for s in samples]
            # 每样本横截面量: 用当日K线算 (rank必须全宇宙同日可比)
            day_r7 = []; day_r30 = []; day_dd = []; day_turn = []
            for s in samples:
                kl = klines.get(s[0], [])
                idx = {k['t']: i for i, k in enumerate(kl)}.get(ts)
                if idx is None or idx < 30:
                    day_r7.append(0.0); day_r30.append(0.0); day_dd.append(0.0); day_turn.append(0.0)
                    continue
                r7 = (kl[idx]['c']/kl[idx-7]['c'] - 1) * 100 if idx >= 7 else 0.0
                r30 = (kl[idx]['c']/kl[idx-30]['c'] - 1) * 100
                hi30 = max(k['h'] for k in kl[idx-29:idx+1])
                dd = (hi30 - kl[idx]['c']) / hi30 * 100
                q5 = sum(k['q'] for k in kl[idx-4:idx+1]) if idx >= 5 else 0.0
                day_r7.append(r7); day_r30.append(r30); day_dd.append(dd); day_turn.append(q5)
            # rank (当日全宇宙, 0~1, 高=热)
            def _rank(vals):
                order = sorted(range(n), key=lambda i: vals[i])
                r = [0.0]*n
                for pos, i in enumerate(order):
                    r[i] = pos / max(n-1, 1)
                return r
            rk7, rk30, rkdd, rkq = _rank(day_r7), _rank(day_r30), _rank(day_dd), _rank(day_turn)
            v5, dd30, uni_mom = xs_stats[ts]
            # med_feat(第8维特征)必须用前一日横截面中位 — 当日next_ret在入场时未知, 用作特征即前视
            day_rets_med_prev = [s[4] for s in by_day.get(ts - 86400000, []) if abs(s[4]) > 1e-9]
            med_feat = float(np.median(day_rets_med_prev)) if day_rets_med_prev else 0.0
            # med_ret_label(残差标签用): 训练样本的标签本就是历史已实现收益, 残差化对齐标签语义, 允许用当日中位(标签非特征)
            med_ret_label = float(np.median([s[4] for s in samples])) if n else 0.0
            new_samples = []
            for i, (sym, feat, ll_, ls_, ret) in enumerate(samples):
                if XS_RANK:
                    # 8维: 7d收益rank / 30d收益rank / 距30d高回撤rank(高=超跌) / 成交额rank / BTC5日vol / BTC距高回撤 / 宇宙动量(前日) / 宇宙中位(前日)
                    feat = list(feat) + [rk7[i], rk30[i], rkdd[i], rkq[i], v5, dd30, uni_mom, med_feat]
                if RESIDUAL_LABEL:
                    # LONG标签残差化: 币48h ret - 当日宇宙中位ret > +5% (SHORT不动)
                    ll_ = 1 if (ret - med_ret_label) > 5.0 else 0
                new_samples.append((sym, feat, ll_, ls_, ret))
            new_by_day[ts] = new_samples
        by_day = new_by_day
        log(f'横截面增强落盘: XS_RANK={XS_RANK} RESIDUAL_LABEL={RESIDUAL_LABEL}')

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
_vol5_map = {}   # sym -> {ts_sec: 前5日均成交额U} (P0训练过滤用, 懒加载一次性预计算)
_vf_total = 0; _vf_kept = 0   # P0臂过滤统计

def _vol5_at(klines, sym, ts):
    """sym在样本日ts的前5日均成交额U (口径=生产_filter_valid_samples: 样本日自身K线之前的5根);
    None=该日无K线(无法计算, 视为不通过)"""
    m = _vol5_map.get(sym)
    if m is None:
        kd = klines.get(sym, [])
        m = {}
        for j in range(5, len(kd)):
            m[kd[j]['t'] // 1000] = sum(k['q'] for k in kd[j-5:j]) / 5.0
        _vol5_map[sym] = m
    return m.get(ts)

def train_and_predict_batch(train_ts_list, pred_ts, entry_ts, klines, soup_hist=None):
    X_train, yL, yS, _grp, _wts = [], [], [], [], []
    _tmax = max(train_ts_list) if train_ts_list else 0
    for ts in train_ts_list:
        cache_file = f'{CACHE_DIR}/{ts}.npz'
        if not os.path.exists(cache_file): continue
        try:
            data = np.load(cache_file)
            f_ = data['feats']
            lbl = data['labels']
            if VOLUME_FILTER > 0:
                # P0臂: 训练样本同款流动性过滤 (预测端已有同款过滤, 此处只治训练集不对称)
                global _vf_total, _vf_kept
                _v5s = [_vol5_at(klines, str(s), ts) for s in data['syms']]
                _keep = np.array([v is not None and v >= VOLUME_FILTER for v in _v5s])
                _vf_total += len(_keep); _vf_kept += int(_keep.sum())
                if not _keep.any(): continue
                f_ = f_[_keep]; lbl = lbl[_keep]
            X_train.append(f_)
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
    if not NAN_RAW:
        X_train = np.nan_to_num(X_train, nan=0.0, copy=False)
    if not KRONOS_ON:
        X_train[:, 100:932] = 0.0   # Kronos置零 (与生产一致)
    if os.environ.get('LIQ_ON', '0') != '1':
        X_train[:, 72:91] = 0.0     # liq 19维置零 (与生产一致; LIQ_ON=1 实验启用)
    if os.environ.get('ETF_ON', '0') != '1':
        X_train[:, 46:48] = 0.0     # ETF 2维置零 (8/4修复错位, 数据不足; ETF_ON=1 实验启用)
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
        ml = _fit_model(X_train, yL, pL, 42 + SEED_OFFSET)
    ms = _fit_model(X_train, yS, pS, 43 + SEED_OFFSET)

    cache_file = f'{CACHE_DIR}/{pred_ts}.npz'
    if not os.path.exists(cache_file): return None
    try:
        data = np.load(cache_file)
        X_pred = data['feats'].astype(np.float32)
        pred_labels = data['labels']
        pred_syms = data['syms']
    except: return None

    if not NAN_RAW:
        X_pred = np.nan_to_num(X_pred, nan=0.0, copy=False)
    if not KRONOS_ON:
        X_pred[:, 100:932] = 0.0
    if os.environ.get('LIQ_ON', '0') != '1':
        X_pred[:, 72:91] = 0.0
    if os.environ.get('ETF_ON', '0') != '1':
        X_pred[:, 46:48] = 0.0
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
    if ENTRY_SLIP > 0:
        ep = ep * (1 + ENTRY_SLIP/100) if direction == 'long' else ep * (1 - ENTRY_SLIP/100)
    pnl = 0; hit = False; reason = 'hold'
    # FIX 2026-07-31(GPT审计发现): 止盈止损检查必须含入场日当天(off=0) —
    # 原代码从ki+1开始, 入场日当天-5%波动被豁免, 36/180笔(20%)当天已止损却记盈利, 虚增~17pp胜率
    for off in ([1] if LABEL_1D else [0, 1, 2]):
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
    if VOLUME_FILTER > 0:
        print(f'  训练样本过滤: {_vf_kept}/{_vf_total} 保留 {_vf_kept/max(_vf_total,1)*100:.0f}% (阈值{VOLUME_FILTER/1000:.0f}kU)')
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
