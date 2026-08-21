#!/usr/bin/env python3
"""山寨成交额监控 (2026-08-10): 除市值TOP50/100 的每日成交总量, 显著上涨时发邮件提醒

口径:
- 成交额 = 币安 U-M 合约日线 quote volume (q), 数据源 backtester/data_cache/notusdt_1d_full.json
- 排除 = CoinGecko 市值排名前 50 / 前 100 (coingecko_data/mcap_latest.json, 6:10 每日更新)
- 上涨判定 = 今日(最近完整日)成交额 > 前5日均值 × 阈值(默认 1.20 = +20%), 或 > 前20日均值 × 1.15
- 运行时机: 每日 09:05 (日 K 已于 UTC 00:00 收盘, 数据完整)
- 8/16 加文件锁: 防 cron 双进程重复发邮件 (8/15 曾双跑双发)
"""
import fcntl, os, sys, json, smtplib, requests
import datetime as dt
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from email.mime.text import MIMEText

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
CST = timezone(timedelta(hours=8))
NOW = datetime.now(CST)
TODAY = NOW.strftime('%Y-%m-%d')

LOG_FILE = os.path.join(BASE, 'logs', 'altcoin_volume.log')
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

def log(msg):
    line = f'[{datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")}] {msg}'
    print(line, flush=True)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')

# 邮件配置 (与 daily_health_check.py 同源)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE, '.env'))
    SMTP_USER = os.environ.get('SMTP_USER', '')
    SMTP_AUTH_CODE = os.environ.get('SMTP_AUTH_CODE', '')
except Exception:
    SMTP_USER = ''
    SMTP_AUTH_CODE = ''
ALERT_TO = '305488483@qq.com'

def send_mail(subject, body):
    if not SMTP_USER or not SMTP_AUTH_CODE:
        log('[邮件] SMTP未配置, 跳过')
        return False
    msg = MIMEText(body, 'html', 'utf-8')  # 2026-08-10: 改为 HTML 表格
    msg['From'] = SMTP_USER
    msg['To'] = ALERT_TO
    msg['Subject'] = subject
    try:
        with smtplib.SMTP_SSL('smtp.qq.com', 465, timeout=15) as s:
            s.login(SMTP_USER, SMTP_AUTH_CODE)
            s.sendmail(SMTP_USER, [ALERT_TO], msg.as_string())
        log(f'[邮件] 已发送: {subject}')
        return True
    except Exception as e:
        log(f'[邮件] 发送失败: {e}')
        return False

# ============ 数据加载 ============
KLINES = '/home/myuser/backtester/data_cache/notusdt_1d_full.json'
MCAP = '/home/myuser/coingecko_data/mcap_latest.json'
OI = '/home/myuser/backtester/data_cache/oi_daily.json'

def load_day_quote():
    """返回 day_q: {ts: {sym: q}} 和 day_kl: {ts: {sym: {o,c,v}}}"""
    d = json.load(open(KLINES))
    day_q = defaultdict(dict)
    day_kl = defaultdict(dict)
    for sym, rows in d['klines'].items():
        for r in rows:
            day_q[r['t']][sym] = r['q']
            day_kl[r['t']][sym] = {'o': r['o'], 'c': r['c'], 'v': r['v'], 'q': r['q'], 'h': r['h'], 'l': r['l']}
    return day_q, day_kl

# ============ 合约乘数 (8/13: 币安 openInterest 单位=合约张数, 乘数币1张=1000/100万币) ============
def contract_multiplier(sym):
    """从 symbol 前缀解析合约乘数: 1000XECUSDT→1000, 1000000BOBUSDT→1000000, 普通币→1"""
    import re as _re
    base = sym[:-4] if sym.endswith('USDT') else sym
    m = _re.match(r'^(1000000|10000|1000|100)([A-Z0-9]{2,})', base)
    if m and not m.group(2).isdigit():
        return int(m.group(1))
    return 1

def calc_cost_and_dump(sym, oi_data, day_kl, valid_days, circ_map):
    """增量成本价 + 出货判定 (2026-08-10 加)

    口径: 仅统计"多头增仓日" (ΔOI>0 且 收盘≥开盘) — 用 VWAP 作为该批增仓成本
    - 加权平均成本 = Σ(ΔOI_多头 × VWAP) / Σ(ΔOI_多头)
    - 浮盈 = (最新价 / 成本 - 1)
    - 出货信号: 最新日 ΔOI < 0 (OI 下降) 且 最新价 > 成本 (盈利状态平仓)
    返回 dict 或 None
    """
    try:
        recs = oi_data.get(sym)
        if not recs or len(recs) < 10:
            return None
        ks = sorted(int(k) for k in recs.keys())
        # OI 按天对齐 (8/13 修复: 快照 ts=X 00:00 的内容 = X-1 日收盘持仓 → 键减一天)
        oi_by_day = {}
        for k in ks:
            d_utc = (dt.datetime.fromtimestamp(k, tz=dt.timezone.utc) - dt.timedelta(days=1)).strftime('%Y-%m-%d')
            oi_by_day[d_utc] = float(recs[str(k)])
        # 只取 valid_days 覆盖范围内的日线 (8/11 回滚: 全程累计增仓成本, 不用20天窗口)
        cost_acc = 0.0
        vol_acc = 0.0
        prev_oi = None
        for ts in valid_days:
            d_str = dt.datetime.fromtimestamp(ts/1000, tz=dt.timezone.utc).strftime('%Y-%m-%d')
            if d_str not in oi_by_day:
                continue
            oi_v = oi_by_day[d_str]
            kl = day_kl.get(ts, {}).get(sym)
            if kl is None or kl['v'] <= 0:
                prev_oi = oi_v
                continue
            vwap = kl['q'] / kl['v']
            if prev_oi is not None:
                d_oi = oi_v - prev_oi
                # 多头增仓日: ΔOI>0 且 收≥开
                if d_oi > 0 and kl['c'] >= kl['o']:
                    cost_acc += d_oi * vwap
                    vol_acc += d_oi
            prev_oi = oi_v
        if vol_acc <= 0:
            return None
        avg_cost = cost_acc / vol_acc
        # 最新价
        last_ts = valid_days[-1]
        last_kl = day_kl.get(last_ts, {}).get(sym)
        if last_kl is None:
            return None
        cur_price = last_kl['c']
        # 8/11: 成本可靠性检测 — OI 起点日价格 vs 当前价格差距过大 (>3倍) 说明 OI 覆盖期外有大行情,
        # 存量持仓成本不可知, 基于OI增仓的成本估算失真 → 标注 unreliable
        # (如 SKYAI: OI起点5/10价0.55, 现价0.098, 5.6倍差距 — 中间崩过96%, 成本不可信)
        reliable = True
        try:
            oi_start_ts = None
            for ts in valid_days:
                d_str = dt.datetime.fromtimestamp(ts/1000, tz=dt.timezone.utc).strftime('%Y-%m-%d')
                if d_str in oi_by_day:
                    oi_start_ts = ts
                    break
            if oi_start_ts is not None:
                kl0 = day_kl.get(oi_start_ts, {}).get(sym)
                if kl0 and kl0['c'] > 0:
                    ratio = cur_price / kl0['c'] if cur_price > kl0['c'] else kl0['c'] / cur_price
                    if ratio > 3.0:
                        reliable = False
        except Exception:
            pass
        profit = (cur_price / avg_cost - 1) * 100
        # 出货判定 (8/12 改: 以 TUT 为模板 — 先堆积后派发)
        # TUT 模板: 8/9 OI 暴增+63%堆积 → 8/10 OI 骤降-31%出货
        # 出货中 = ① 当天 OI 降 ≥30% (TUT -31%) + ② 近10日 OI 曾暴增 ≥50% (证明有货可出)
        #          + ③ 价格未同步暴涨 <20% (排除空头被轧)
        # 早期迹象 = ①' 当天 OI 降 ≥10% + ② 近10日曾暴增 ≥30% (OI 先撤, 有堆积前提)
        # 注: HMSTR/INX 等从无暴增的震荡币不会触发 (8/12 用户验证修正)
        last_d_str = dt.datetime.fromtimestamp(last_ts/1000, tz=dt.timezone.utc).strftime('%Y-%m-%d')
        prev_d_str = dt.datetime.fromtimestamp(valid_days[-2]/1000, tz=dt.timezone.utc).strftime('%Y-%m-%d')
        last_oi = oi_by_day.get(last_d_str)
        prev_oi_v = oi_by_day.get(prev_d_str)
        dumping = False
        early = False
        if last_oi is not None and prev_oi_v is not None and prev_oi_v > 0:
            # ① 当天降幅
            drop = (prev_oi_v - last_oi) / prev_oi_v
            c1_big = drop >= 0.30    # 出货级: 降≥30%
            c1_sml = drop >= 0.10    # 早期级: 降≥10%
            # ② 近10日 OI 峰值 vs 10日前 (堆积证明)
            oi_10d_ago = None
            for ts in valid_days[-10:]:
                d_str = dt.datetime.fromtimestamp(ts/1000, tz=dt.timezone.utc).strftime('%Y-%m-%d')
                if d_str in oi_by_day:
                    oi_10d_ago = oi_by_day[d_str]
                    break
            peak10 = max(oi_by_day.get(dt.datetime.fromtimestamp(ts/1000, tz=dt.timezone.utc).strftime('%Y-%m-%d'), 0)
                         for ts in valid_days[-10:])
            has_stack_big = oi_10d_ago and oi_10d_ago > 0 and peak10 / oi_10d_ago >= 1.50   # 暴增≥50%
            has_stack_sml = oi_10d_ago and oi_10d_ago > 0 and peak10 / oi_10d_ago >= 1.30   # 增≥30%
            # ③ 当日涨幅 (排除空头被轧)
            day_chg = (last_kl['c'] / last_kl['o'] - 1) * 100 if last_kl['o'] > 0 else 0
            c3 = day_chg < 20
            if c1_sml and has_stack_sml:
                early = True  # 早期迹象: 有堆积+OI开始撤
            if c1_big and has_stack_big and c3:
                dumping = True
        circ = circ_map.get(sym, 0)
        # 当前 OI 占流通% (出货基准用, 2026-08-10 加; 8/12: circ<1万=CoinGecko数据错误按缺失)
        oi_circ_pct = (last_oi * contract_multiplier(sym) / circ * 100) if (last_oi is not None and circ >= 10000) else None
        return {
            'vol': vol_acc, 'cost': avg_cost, 'price': cur_price,
            'profit': profit, 'dumping': dumping, 'early': early,
            'oi_drop_pct': ((prev_oi_v - last_oi) / prev_oi_v * 100) if (last_oi is not None and prev_oi_v) else 0,
            'circ': circ, 'oi_circ_pct': oi_circ_pct, 'reliable': reliable,
        }
    except Exception as e:
        log(f'[警告] 成本分析失败 {sym}: {e}')
        return None

# ============ 出货 OI 基准 (2026-08-10 加) ============
# 每次出货标签时记录该币 OI 占流通%; 攒满 10 次后以 P75 分位作为"出货基准线",
# 之后任何币 OI 占流通% ≥ P75 基准 → 提醒 (持仓集中度已达历史出货高位区间)
DUMP_HIST = os.path.join(BASE, 'data', 'dump_oi_history.json')

def load_dump_hist():
    try:
        with open(DUMP_HIST) as f:
            return json.load(f)
    except Exception:
        return {'records': [], 'avg_pct': None}

def save_dump_hist(h):
    try:
        os.makedirs(os.path.dirname(DUMP_HIST), exist_ok=True)
        with open(DUMP_HIST, 'w') as f:
            json.dump(h, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log(f'[警告] 出货历史保存失败: {e}')

def record_dump_and_scan(conc_syms, day_kl, valid_days, oi_data, circ_map, latest_str, top50_set):
    """1) 对增量TOP3中触发出货的币, 记录 OI 占流通% 到历史
       2) 若历史 ≥10 次, 计算 P75 分位, 全市场扫描 OI 占流通% ≥ P75 的币
       返回 (hist, alert_list) — alert_list: [(sym, oi_pct, avg_pct), ...]
    """
    hist = load_dump_hist()
    records = hist.get('records', [])
    # 1) 记录本次出货样本
    new_records = []
    for sym in conc_syms:
        cd = calc_cost_and_dump(sym, oi_data, day_kl, valid_days, circ_map)
        if cd and cd['dumping'] and cd.get('oi_circ_pct') is not None:
            # 防重复记录同一天同币
            key = (latest_str, sym)
            if not any((r.get('date'), r.get('sym')) == key for r in records):
                new_records.append({'date': latest_str, 'sym': sym, 'oi_circ_pct': round(cd['oi_circ_pct'], 2)})
    if new_records:
        records.extend(new_records)
        # 只保留最近 365 条 (用户 8/10 设计: 均值随样本滚动更新, 上限365天)
        records = records[-365:]
        hist['records'] = records
        save_dump_hist(hist)
        log(f'出货基准: 新增记录 {[r["sym"] for r in new_records]} ({len(records)}条累计)')

    # 2) 计算 P75 分位 (≥10 次启用)
    pcts = sorted(r['oi_circ_pct'] for r in records if isinstance(r.get('oi_circ_pct'), (int, float)))
    avg_pct = None
    if len(pcts) >= 10:
        # 预警线 = P75 分位 (用户 8/10 拍板): 历史出货样本 OI 占比的 75% 分位
        # 只有 OI 集中度处于历史出货的高位区间才提醒
        idx = int(len(pcts) * 0.75)
        avg_pct = pcts[min(idx, len(pcts) - 1)]
        hist['avg_pct'] = round(avg_pct, 2)
        save_dump_hist(hist)

    # 3) 全市场扫描 → 三档预警 (2026-08-10 加早期迹象档)
    #    ⚡ 早期迹象: OI降≥10% + 盈利 (OI先撤, 价格未崩 — 提前预警; 47%出货 OI领先价格)
    #    ⚠️ 出货确认: 4条件全满足
    #    ⚠️ 高风险:    OI占流通% ≥ P75 (堆积中, 未撤)
    #    2026-08-10 崩盘过滤: 近20日跌幅 ≥50% 排除 (BANKUSDT 案例: 已崩盘 = OI 空头堆积)
    alerts = []
    early_alerts = []
    if avg_pct is not None:
        last_ts = valid_days[-1]
        last_d_str = dt.datetime.fromtimestamp(last_ts/1000, tz=dt.timezone.utc).strftime('%Y-%m-%d')
        for sym, recs in oi_data.items():
            if sym in top50_set:  # 只看山寨 (与邮件口径一致)
                continue
            if not isinstance(recs, dict) or not recs:
                continue
            # 崩盘过滤: 近20日收盘跌幅 (20日前收盘 vs 最新收盘)
            kl_sym = [day_kl[ts].get(sym) for ts in valid_days[-20:] if sym in day_kl.get(ts, {})]
            if len(kl_sym) >= 10:
                c_old = kl_sym[0]['c']
                c_new = kl_sym[-1]['c']
                if c_old > 0 and (c_new / c_old - 1) < -0.50:
                    continue  # 近20日跌超50% = 已崩盘, 排除
            # 8/12 拉盘过滤: 10日内从最低收盘后的最大涨幅 ≥30% 才保留 (用户 8/12 改: 30天→10天, 聚焦近期拉升)
            # (LYN/BANK 等 OI 高但盘整的币剔除 — 用户验证: 盘整无出货逻辑)
            kl30 = [day_kl[ts].get(sym) for ts in valid_days[-10:] if sym in day_kl.get(ts, {})]
            if len(kl30) >= 8:
                closes = [k['c'] for k in kl30]
                lo_c = min(closes)
                if lo_c > 0:
                    lo_idx = closes.index(lo_c)
                    hi_after = max(closes[lo_idx:])
                    trend = (hi_after / lo_c - 1) * 100
                    if trend < 30:
                        continue  # 10日收盘趋势涨幅<30% = 盘整, 无出货风险, 排除
                    # 8/12 追加: 当前价 ≥ 10日最高收盘×60% 才保留 (BLUAI 拉完已砸的剔除)
                    # BLUAI 案例: 8/8拉+172% → 8/11砸-59% 回原点, 现价/最高=41% = 出货已完成, 非风险
                    cur_price = closes[-1]
                    if hi_after > 0 and cur_price / hi_after < 0.60:
                        continue
            ks = sorted(int(k) for k in recs.keys())
            # 找 ≤ 最新完整日的 OI
            cur_oi = None
            t_cur_s = last_ts // 1000 + 86400  # 8/13: OI快照ts=X的内容=X-1日收盘, +1天对齐
            for k in ks:
                if k <= t_cur_s:
                    cur_oi = float(recs[str(k)])
            circ = circ_map.get(sym, 0)
            if cur_oi is None or circ <= 0:
                continue
            # 8/12: 流通量 < 1万枚 视为 CoinGecko 数据错误 (如 PUMPBTC circ=650 枚 → 2623万%假象), 跳过
            if circ < 10000:
                continue
            pct = cur_oi * contract_multiplier(sym) / circ * 100
            # 8/11 修复: 不再过滤 >100% — OI 可真实超过流通盘(高杠杆堆积, 如 GUA 321%)
            # 真正要防的是 circ 缺失(0)或 OI 异常小, 已在上面 circ<=0 拦截
            # 出货/早期状态 (复用成本分析, 计算量可控: 全市场~500币 × OI历史~93条)
            cd = calc_cost_and_dump(sym, {sym: recs}, day_kl, valid_days, circ_map)
            if cd and cd.get('dumping') and pct >= 1.0:  # OI占比>1% 才算有效出货 (排除数据异常)
                alerts.append(('dump', sym, pct, avg_pct))
            elif pct >= avg_pct:
                alerts.append(('high', sym, pct, avg_pct))
            elif cd and cd.get('early') and pct >= 1.0:
                early_alerts.append(('early', sym, pct, avg_pct))
        # 排序: 出货确认 > 高风险 > 早期迹象, 组内按 OI% 降序
        alerts.sort(key=lambda x: (0 if x[0] == 'dump' else 1, -x[2]))
        early_alerts.sort(key=lambda x: -x[2])
        log(f'出货基准: P75={avg_pct:.1f}% (n={len(pcts)}), 达线 {len(alerts)} 个 (出货{sum(1 for a in alerts if a[0]=="dump")}/高险{sum(1 for a in alerts if a[0]=="high")}), 早期迹象 {len(early_alerts)} 个')
    else:
        log(f'出货基准: 样本 {len(pcts)}/10 (还差 {10-len(pcts)} 次启用P75提醒)')
    return hist, alerts, early_alerts

# ============ SHORT Top10 出货分析 (2026-08-11 加, 并入每日邮件) ============
def short_top10_dump_analysis(day_kl, valid_days, oi_data, circ_map, latest_str):
    """读取当日 pred 文件的 top10_short, 逐币算出货状态, 返回 HTML 行列表
    列: 币 / prob / OI日变% / OI占流通% / 状态 / 浮盈%
    状态: ⚠️出货中(4条件) / ⚡早期(2条件) / 堆积中(OI增) / 观望(OI降但未达出货条件)
    """
    import glob as _g
    pred_dir = os.path.join(BASE, 'data')
    # 取预测文件: 优先今天已生成的预测 (日期 > latest_str), 否则用 latest_str 当天的
    import re as _re
    target = os.path.join(pred_dir, f'pred_{latest_str}.json')
    files = sorted(_g.glob(os.path.join(pred_dir, 'pred_*.json')))
    if not files:
        return [], '(无预测文件)'
    newest = files[-1]
    m = _re.search(r'pred_(\d{4}-\d{2}-\d{2})\.json', newest)
    pred_file = newest
    if m and m.group(1) <= latest_str and os.path.exists(target):
        pred_file = target
    try:
        with open(pred_file) as f:
            pred = json.load(f)
    except Exception as e:
        log(f'[警告] SHORT分析: 预测文件读取失败 {pred_file}: {e}')
        return [], '(预测读取失败)'
    top10 = pred.get('top10_short', [])
    rows = []
    for it in top10[:10]:
        if isinstance(it, dict):
            sym, prob = it.get('symbol'), float(it.get('prob') or 0)
        else:
            sym, prob = it[0], float(it[1])
        recs = oi_data.get(sym)
        # OI 日变化 + 占流通 (8/13 修复: oi3_txt 提前初始化, 防新币 OI 记录不足时 NameError)
        oi_chg_txt = '—'
        oi3_txt = '—'
        oi_pct_txt = '—'
        state_txt = '—'
        profit_txt = '—'
        if isinstance(recs, dict) and recs:
            ks = sorted(int(k) for k in recs.keys())
            t_cur = valid_days[-1] // 1000 + 86400  # 8/13: 快照+1天对齐
            prev_oi = cur_oi = oi_3d = None
            for k in ks:
                if k <= t_cur - 3 * 86400:
                    oi_3d = float(recs[str(k)])
                if k <= t_cur - 86400:
                    prev_oi = float(recs[str(k)])
                if k <= t_cur:
                    cur_oi = float(recs[str(k)])
            if prev_oi and cur_oi:
                chg = (cur_oi / prev_oi - 1) * 100
                oi_chg_txt = f'{chg:+.1f}%'
                # 近3日累计变化 (2026-08-11 加: 捕捉慢性出货, 如 SKYAI 单日-9.5% 但3日-27%)
                oi3_txt = f'{(cur_oi/oi_3d-1)*100:+.1f}%' if oi_3d else '—'
                circ = circ_map.get(sym, 0)
                if circ >= 10000:  # 8/12: circ<1万=数据错误, 不显示占比
                    oi_pct_txt = f'{cur_oi*contract_multiplier(sym)/circ*100:.1f}%'
                cd = calc_cost_and_dump(sym, {sym: recs}, day_kl, valid_days, circ_map)
                if cd:
                    profit_txt = f'{cd["profit"]:+.0f}%'
                    if cd.get('reliable') is False:
                        profit_txt += ' ⚠️'  # 成本不可靠标注 (崩盘历史币, 8/11)
                    if cd.get('dumping'):
                        state_txt = '⚠️出货中'
                    elif cd.get('early'):
                        state_txt = '⚡早期'
                    elif chg > 0:
                        # 8/15: 堆积分位 — 现价 vs 15日均价 (验证: 15日优于30日, 高位堆积7日中位-6.1% vs 低位+0.8%, 高低差-6.85% n=14353)
                        kls = [day_kl[t].get(sym) for t in valid_days if sym in day_kl.get(t, {})]
                        kls = [k for k in kls if k]
                        if len(kls) >= 15:
                            avg15 = sum(k['c'] for k in kls[-15:]) / 15
                            if kls[-1]['c'] > avg15:
                                state_txt = '▲堆积·高位'
                            else:
                                state_txt = '▼堆积·低位'
                        else:
                            state_txt = '堆积中'
                    else:
                        state_txt = '观望'
        rows.append((sym, prob, oi_chg_txt, oi3_txt, oi_pct_txt, state_txt, profit_txt))
    return rows, os.path.basename(pred_file)

def load_mcap_top(n):
    """返回当日市值前 n 的币集合 (无匹配币时返回空集)"""
    try:
        d = json.load(open(MCAP))
        coins = d.get('coins', {})
        ranked = sorted(coins.items(), key=lambda x: -(x[1].get('mcap') or 0))
        return {k for k, _ in ranked[:n]}
    except Exception as e:
        log(f'[警告] 市值加载失败({e}), 该口径跳过')
        return None

# ============ DEX 链上数据 (2026-08-15 加: CMC Dex API, 免费key可用) ============
# 预警币 → 链上24h买卖方向 (真实钱包交易, 比合约主动买卖更接近真实方向)
DEX_ADDR_CACHE = os.path.join(BASE, 'data', 'dex_address_map.json')
CMC_KEY = os.environ.get('CMC_API_KEY', '')

def _load_addr_cache():
    try:
        return json.load(open(DEX_ADDR_CACHE))
    except Exception:
        return {}

def _save_addr_cache(c):
    try:
        os.makedirs(os.path.dirname(DEX_ADDR_CACHE), exist_ok=True)
        json.dump(c, open(DEX_ADDR_CACHE, 'w'))
    except Exception as e:
        log(f'[DEX] 地址缓存保存失败: {e}')

def _dex_search_addr(sym):
    """symbol → (platform, address); CMC dex/search
    先试去 USDT 后缀的 base (如 GUAUSDT→GUA, 与 CoinGecko 一致), 搜不到再用原符号"""
    import requests as _rq
    try:
        base = sym[:-4] if sym.endswith('USDT') else sym
        queries = (base, sym) if base != sym else (base,)
        for q in queries:
            r = _rq.get('https://pro-api.coinmarketcap.com/v1/dex/search',
                        params={'q': q}, headers={'X-CMC_PRO_API_KEY': CMC_KEY}, timeout=12)
            d = r.json().get('data', {})
            tks = d.get('tks', [])
            if not tks:
                continue
            # 优先符号精确匹配 (s 全等 base), 否则 ssc 最高
            best = None
            for t in tks:
                if (t.get('s') or '').upper() == q.upper():
                    best = t
                    break
            if best is None:
                best = tks[0]
            addr = best.get('addr')
            if not addr:
                continue
            plat = (best.get('plt') or '').lower()
            # 平台名规范化 (token 端点要求简名)
            if 'bep20' in plat or plat == 'bsc' or 'bnb' in plat:
                plat = 'bsc'
            elif 'erc20' in plat or plat == 'ethereum' or plat == 'eth':
                plat = 'ethereum'
            elif plat in ('solana', 'sol'):
                plat = 'solana'
            else:
                plat = plat.split()[0]
            return plat, addr
        return None
    except Exception as e:
        log(f'[DEX] search {sym} 失败: {e}')
        return None

def get_dex_chain_stats(syms):
    """预警币 → {sym: (买USD, 卖USD, 买家数, 卖家数)}; 拿不到返回 None
    symbol→地址 结果缓存到 data/dex_address_map.json, 之后每天直查 token 端点"""
    if not CMC_KEY or not syms:
        return {}
    import requests as _rq
    import time as _t
    addr_cache = _load_addr_cache()
    result = {}
    for sym in sorted(syms):
        try:
            key = addr_cache.get(sym)
            if not key or not key.get('address'):
                found = _dex_search_addr(sym)
                if not found:
                    result[sym] = None
                    continue
                plat, addr = found
                addr_cache[sym] = {'platform': plat, 'address': addr}
                _save_addr_cache(addr_cache)
                _t.sleep(0.3)
            else:
                plat, addr = key['platform'], key['address']
            r = _rq.get('https://pro-api.coinmarketcap.com/v1/dex/token',
                        params={'platform': plat, 'address': addr},
                        headers={'X-CMC_PRO_API_KEY': CMC_KEY}, timeout=12)
            data = r.json().get('data')
            if not data:
                result[sym] = None
                continue
            sts = {s.get('tp'): s for s in (data.get('sts') or [])}
            s24 = sts.get('24h')
            if not s24:
                result[sym] = None
                continue
            result[sym] = (float(s24.get('bvu') or 0), float(s24.get('svu') or 0),
                           s24.get('but'), s24.get('sut'))
        except Exception as e:
            log(f'[DEX] {sym} 链上数据失败: {e}')
            result[sym] = None
        _t.sleep(0.25)
    return result

# ============ 1U 波动榜 (2026-08-15 加: 当日振幅TOP10 + 1000U冲击成本=盘口薄度) ============
def vol_impact_rows(day_kl, latest, topn=10):
    """返回 [(sym, 振幅%, 1U多最大%, 1U空最大%, 冲击成本%|None)]
    振幅 = 当日 (high-low)/open (事后最大波动);
    1U多最大 = high/low-1 (最低点买1U最高点卖); 1U空最大 = 1-low/high;
    冲击成本 = 按卖一档往上累计吃 1000U 的名义均价 vs 中间价 (盘口越薄数值越大)
    """
    import requests as _rq
    import time as _t
    rows = []
    for sym, k in day_kl.get(latest, {}).items():
        o, h, l = k.get('o', 0), k.get('h', 0), k.get('l', 0)
        if o <= 0 or l <= 0:
            continue
        amp = (h - l) / o
        rows.append((amp, sym, h, l))
    rows.sort(reverse=True)
    out = []
    for amp, sym, h, l in rows[:topn]:
        impact = None
        try:
            r = _rq.get('https://fapi.binance.com/fapi/v1/depth',
                        params={'symbol': sym, 'limit': 20}, timeout=5)
            if r.status_code == 200:
                d = r.json()
                asks = d.get('asks', [])
                bids = d.get('bids', [])
                if asks and bids:
                    mid = (float(asks[0][0]) + float(bids[0][0])) / 2
                    if mid > 0:
                        remaining_u = 1000.0  # 剩余美元
                        qty_total = 0.0       # 累计吃掉的币数
                        cost = 0.0
                        for p, q in asks:
                            p = float(p); q = float(q)
                            notional = p * q
                            if notional >= remaining_u:
                                qty_total += remaining_u / p
                                cost += remaining_u
                                remaining_u = 0
                                break
                            qty_total += q
                            cost += notional
                            remaining_u -= notional
                        if remaining_u < 1000.0:
                            avg_price = cost / qty_total
                            impact = (avg_price - mid) / mid * 100
        except Exception:
            pass
        _t.sleep(0.1)
        out.append((sym, amp, h/l - 1, 1 - l/h, impact))
    return out

# ============ OI 快照前置补采 (8/14: 快照北京8:00生成, 采集6:00拿不到, 邮件9:05运行前补拉) ============
def fetch_today_oi_snapshot():
    """补拉"今天 UTC 00:00"的 OI 快照(=昨日收盘持仓), 防止邮件 OI 变化=0
    只在缺今天快照时拉, 限速 12 req/s"""
    import requests as _rq
    try:
        cache = json.load(open(OI))
    except Exception:
        return 0
    target = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    need = [s for s, recs in cache.items() if isinstance(recs, dict) and str(target) not in recs]
    if not need:
        return 0
    updated = 0
    for sym in need:
        try:
            r = _rq.get('https://fapi.binance.com/futures/data/openInterestHist',
                        params={'symbol': sym, 'period': '1d', 'limit': 3}, timeout=6)
            if r.status_code == 200:
                for o in r.json():
                    ts = int(o['timestamp']) // 1000
                    if ts == target:
                        cache[sym][str(ts)] = float(o['sumOpenInterest'])
                        updated += 1
                        break
        except Exception:
            pass
        import time as _t
        _t.sleep(0.08)
    if updated:
        try:
            json.dump(cache, open(OI, 'w'))
        except Exception as e:
            log(f'[警告] OI 快照补采保存失败: {e}')
    log(f'OI 快照补采: {updated}/{len(need)} 币 (今天00:00快照)')
    return updated

def fetch_global_mcap():
    """CoinGecko 免费接口: 全市场总市值 / BTC占比 / ETH占比."""
    try:
        r = requests.get('https://api.coingecko.com/api/v3/global', timeout=15)
        if r.status_code != 200:
            return None
        d = r.json().get('data', {})
        return {
            'total_mcap': (d.get('total_market_cap') or {}).get('usd'),
            'btc_dom': (d.get('market_cap_percentage') or {}).get('btc'),
            'eth_dom': (d.get('market_cap_percentage') or {}).get('eth'),
        }
    except Exception as e:
        log(f'[CoinGecko] 全市场市值获取失败: {e}')
        return None


def fetch_altcoin_ranks(limit=10):
    """CoinGecko 免费接口: 市值排名, 排除 BTC 和稳定币, 返回前 limit 个山寨."""
    try:
        r = requests.get('https://api.coingecko.com/api/v3/coins/markets',
                         params={'vs_currency': 'usd', 'order': 'market_cap_desc',
                                 'per_page': '30', 'page': '1', 'sparkline': 'false'},
                         timeout=15)
        if r.status_code != 200:
            return None
        exclude = {'usdt','usdc','dai','tusd','fdusd','busd','usdp','usds',
                   'pyusd','usde','eurt','aeur','usd1','usdd'}
        rows = []
        for c in r.json():
            sym = (c.get('symbol') or '').lower()
            if sym == 'btc' or sym in exclude:
                continue
            rows.append({
                'symbol': c.get('symbol', '').upper(),
                'name': c.get('name', ''),
                'mcap': c.get('market_cap'),
                'chg': c.get('price_change_percentage_24h'),
            })
            if len(rows) >= limit:
                break
        return rows
    except Exception as e:
        log(f'[CoinGecko] 山寨市值排名获取失败: {e}')
        return None


def main():
    # 8/16: 文件锁, 防 cron 双进程 (8/15 双跑双发过)
    lock_f = open('/tmp/altcoin_volume_alert.lock', 'w')
    try:
        fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log('[锁] 已有实例在运行, 本次跳过')
        return
    try:
        _main_locked()
    finally:
        fcntl.flock(lock_f, fcntl.LOCK_UN)
        lock_f.close()

def _main_locked():
    # 8/14: 邮件运行前先补采今天 OI 快照 (解决 OI变化=0 的时间差问题)
    try:
        fetch_today_oi_snapshot()
    except Exception as e:
        log(f'[警告] OI 快照补采失败: {e}')
    conc = []  # 增量TOP名单 (出货基准扫描用; 增量计算失败时为 [] 不扫描)
    day_q, day_kl = load_day_quote()
    days = sorted(day_q)
    if len(days) < 25:
        log(f'K线不足({len(days)}天), 跳过')
        return

    # 最近完整交易日 = 倒数第1根 (8:05 训练时已含当日完整 K; 09:05 运行 = 昨日收盘完整)
    # 注: 数据缓存最后可能含"今天进行中"的K线(8:05 增量拉取), 若其时间戳 < 今天 UTC 00:00 则排除
    today_utc0 = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    valid_days = [t for t in days if t < today_utc0]
    if not valid_days:
        log('无完整交易日, 跳过')
        return
    latest = valid_days[-1]
    latest_str = datetime.fromtimestamp(latest/1000, tz=timezone.utc).strftime('%Y-%m-%d')

    top50 = load_mcap_top(50)
    top100 = load_mcap_top(100)

    def calc(ts, exclude):
        qs = day_q[ts]
        if exclude is None:
            return sum(qs.values())
        return sum(v for k, v in qs.items() if k not in exclude)

    # 历史序列 (最近21个完整交易日)
    hist = valid_days[-21:]
    series = {}
    for n, name in [(50, 'TOP50'), (100, 'TOP100')]:
        ex = top50 if n == 50 else top100
        series[name] = [calc(t, ex) for t in hist]

    alerts = []
    th = '<th style="padding:3px 6px;background:#2c3e50;color:#fff;border:1px solid #ddd;font-size:12px">%s</th>'
    td = '<td style="padding:3px 6px;border:1px solid #ddd;text-align:right">%s</td>'
    td_l = '<td style="padding:3px 6px;border:1px solid #ddd">%s</td>'
    rows = []
    for name in ('TOP50', 'TOP100'):
        s = series[name]
        cur = s[-1]
        avg5 = sum(s[-6:-1]) / 5
        avg20 = sum(s[-21:-1]) / 20
        r5 = cur / avg5 if avg5 else 0
        r20 = cur / avg20 if avg20 else 0
        hot = 'background:#fff3cd;' if (r5 >= 1.20 or r20 >= 1.15) else ''
        rows.append(
            f'<tr style="{hot}">'
            f'{td_l % f"除市值{name}"}'
            f'{td % f"{cur/1e8:.1f}"}'
            f'{td % f"{avg5/1e8:.1f}"}'
            f'{td % f"{r5:.2f}"}'
            f'{td % f"{avg20/1e8:.1f}"}'
            f'{td % f"{r20:.2f}"}</tr>'
        )
        if r5 >= 1.20 or r20 >= 1.15:
            alerts.append(f'{name}: 今日{cur/1e8:.1f}亿 = 5日均的{r5:.2f}倍 / 20日均的{r20:.2f}倍')

    hist_rows = []
    for t, v50, v100 in zip(hist[-7:], series['TOP50'][-7:], series['TOP100'][-7:]):
        ds = datetime.fromtimestamp(t/1000, tz=timezone.utc).strftime('%m-%d')
        hist_rows.append(f'<tr>{td_l % ds}{td % f"{v50/1e8:.1f}"}{td % f"{v100/1e8:.1f}"}</tr>')

    # 增量集中度: 除市值TOP50口径, 今日 vs 昨日 增量 TOP3 (2026-08-10 加)
    # 2026-08-10 增: OI 净变化列 (对倒无法伪造的真实净持仓变化)
    conc_rows = []
    try:
        qs_prev = day_q[valid_days[-2]]
        qs_cur = day_q[latest]
        # OI 数据: {sym: {ts_sec: oi_枚}} → 最近两天
        oi_data = {}
        try:
            oi_raw = json.load(open(OI))
            for sym, recs in oi_raw.items():
                if not isinstance(recs, dict) or not recs:
                    continue
                ks = sorted(int(k) for k in recs.keys())
                oi_data[sym] = recs
        except Exception as e:
            log(f'[警告] OI 加载失败: {e}')
        # 流通量: mcap coins circ
        circ_map = {}
        try:
            mc_d = json.load(open(MCAP))
            circ_map = {k: (v.get('circ') or 0) for k, v in mc_d.get('coins', {}).items()}
        except Exception:
            pass

        conc = []
        for sym, v in qs_cur.items():
            if sym in top50:
                continue
            dv = v - qs_prev.get(sym, 0)
            if dv > 0:
                conc.append((dv, sym))
        conc.sort(reverse=True)
        total_cur_ex50 = sum(v for k, v in qs_cur.items() if k not in top50)
        total_pos = sum(dv for dv, _ in conc)
        total_neg = sum(qs_prev.get(k, 0) - v for k, v in qs_cur.items() if k not in top50 and v < qs_prev.get(k, 0))
        net = total_pos - total_neg

        def oi_change(sym, days_ts):
            """返回 (OI日变化枚, 变化占流通%) 或 None
            days_ts 为毫秒级K线时间戳, OI 键为秒级 — 换算后匹配"""
            recs = oi_data.get(sym)
            if not recs:
                return None
            ks = sorted(int(k) for k in recs.keys())
            if len(ks) < 2:
                return None
            # 目标: 找 ≤ 昨日 和 ≤ 今日 的 OI 快照 (K线毫秒 → OI秒)
            t_prev_s = days_ts[-2] // 1000 + 86400  # 8/13: 快照+1天对齐 (8/11收盘=8/12快照)
            t_cur_s = days_ts[-1] // 1000 + 86400  # 8/13: 快照+1天对齐
            prev_oi = cur_oi = None
            for k in ks:
                if k <= t_prev_s:
                    prev_oi = recs[str(k)]
                if k <= t_cur_s:
                    cur_oi = recs[str(k)]
            if prev_oi is None or cur_oi is None:
                return None
            chg = float(cur_oi) - float(prev_oi)
            circ = circ_map.get(sym, 0)
            pct = chg * contract_multiplier(sym) / circ * 100 if circ else 0
            return chg, pct

        for dv, sym in conc[:3]:
            pct_net = (dv / net * 100) if net > 0 else None   # 占净增量% (净增≤0时无意义, 显示—)
            pct_pos = dv / total_pos * 100 if total_pos > 0 else 0  # 占正增量合计%
            pct_tot = dv / total_cur_ex50 * 100 if total_cur_ex50 else 0  # 占今日总量%
            oi_info = oi_change(sym, valid_days)
            oi_txt = '—'
            if oi_info:
                chg, pct = oi_info
                oi_txt = f'{chg/1e4:+,.0f}万张 ({pct:+.2f}%)'  # 8/13: 单位=合约张数(非币枚数), 万张更准确
            # 成本价 + 出货判定 (2026-08-10 加)
            cd = calc_cost_and_dump(sym, oi_data, day_kl, valid_days, circ_map)
            cost_txt = '—'
            if cd:
                cost_txt = f'{cd["cost"]:.4f} (浮盈{cd["profit"]:+.0f}%)'
                if cd['dumping']:
                    cost_txt += ' ⚠️出货'
            conc_rows.append(
                f'<tr>{td_l % sym}{td % f"{dv/1e8:.2f}"}'
                f'{td % (f"{pct_net:.0f}%" if pct_net is not None else "—")}{td % f"{pct_pos:.0f}%"}{td % f"{pct_tot:.0f}%"}'
                f'{td % oi_txt}{td % cost_txt}</tr>'
            )
        # 汇总行: 正增量合计 / 减量合计 / 净增量
        conc_rows.append(
            f'<tr style="background:#f5f5f5">{td_l % "正增量合计"}'
            f'{td % f"{total_pos/1e8:.2f}"}{td % "—"}{td % "100%"}{td % f"{total_pos/total_cur_ex50*100:.0f}%"}'
            f'{td % "—"}{td % "—"}</tr>'
        )
        conc_rows.append(
            f'<tr style="background:#f5f5f5">{td_l % "减量合计"}'
            f'{td % f"-{total_neg/1e8:.2f}"}{td % "—"}{td % "—"}{td % "—"}'
            f'{td % "—"}{td % "—"}</tr>'
        )
        conc_rows.append(
            f'<tr style="background:#e8f0fe;font-weight:bold">{td_l % "净增量"}'
            f'{td % f"{net/1e8:+.2f}"}{td % "100%"}{td % "—"}{td % "—"}'
            f'{td % "—"}{td % "—"}</tr>'
        )
    except Exception as e:
        log(f'[警告] 增量集中度计算失败: {e}')

    # 1U 波动榜 (2026-08-15 加: 振幅TOP10 + 盘口薄度)
    vol_rows_html = []
    try:
        vol_rows = vol_impact_rows(day_kl, latest)
        for sym, amp, gl, gs, impact in vol_rows:
            if impact is not None:
                imp_txt = f'<span style="color:#c00;font-weight:bold">{impact:.2f}%</span>' if impact >= 2 else f'{impact:.2f}%'
            else:
                imp_txt = '—'
            vol_rows_html.append(
                f'<tr>{td_l % sym}{td % f"{amp*100:.1f}%"}{td % f"{gl*100:+.1f}%"}{td % f"{gs*100:+.1f}%"}{td % imp_txt}</tr>'
            )
    except Exception as e:
        log(f'[警告] 1U波动榜失败: {e}')
    vol_section = f"""<h4 style="margin:4px 0;font-size:12px">🎯 1U波动榜 (昨日振幅TOP10; 1U多/空=当日低买高卖/高卖低平的极端收益; 冲击=1000U吃单推价% → 盘口薄度, ≥2%标红)</h4>
<table style="border-collapse:collapse;margin:4px 0">
<tr>{th % '币'}{th % '振幅'}{th % '1U多最大'}{th % '1U空最大'}{th % '盘口冲击%(薄度)'}</tr>
{''.join(vol_rows_html) if vol_rows_html else td_l % '(无数据)'}
</table>"""

    # 出货 OI 基准: 记录出货样本 + 全市场达线扫描 (2026-08-10 加)
    dump_alerts = []
    early_alerts = []
    dump_hist = load_dump_hist()
    dump_avg = None
    dump_n = 0
    try:
        conc_syms = [sym for _, sym in conc[:3]] if conc else []
        dump_hist, dump_alerts, early_alerts = record_dump_and_scan(
            conc_syms, day_kl, valid_days, oi_data, circ_map, latest_str, top50)
        dump_avg = dump_hist.get('avg_pct')
        dump_n = len(dump_hist.get('records', []))
    except Exception as e:
        log(f'[警告] 出货基准扫描失败: {e}')

    # SHORT Top10 出货分析 (2026-08-11 加, 并入每日邮件)
    short_rows = []
    short_src = ''
    try:
        short_rows, short_src = short_top10_dump_analysis(day_kl, valid_days, oi_data, circ_map, latest_str)
    except Exception as e:
        log(f'[警告] SHORT Top10 分析失败: {e}')
    # SHORT Top10 币集合 (用于全局预警节标记 🅢, 2026-08-11)
    short_syms = {row[0] for row in short_rows}

    # 出货基准提醒节 (HTML) — 早期迹象独立专栏 (2026-08-10)
    dump_rows = []
    early_rows = []
    # 8/14: 溢价指数列 (premiumIndex: 正=多头主导/负=空头主导, 补 OI 不分多空的洞)
    premium_map = {}
    try:
        import requests as _rq
        import time as _t
        all_syms = [s for _, s, _, _ in dump_alerts[:12]] + [s for _, s, _, _ in early_alerts[:5]]
        for s in set(all_syms):
            try:
                r = _rq.get('https://fapi.binance.com/fapi/v1/premiumIndex', params={'symbol': s}, timeout=5)
                if r.status_code == 200:
                    d = r.json()
                    mp = float(d.get('markPrice', 0)); ip = float(d.get('indexPrice', 0))
                    if ip > 0:
                        premium_map[s] = (mp - ip) / ip * 100
            except Exception:
                pass
            _t.sleep(0.08)
    except Exception as e:
        log(f'[警告] 溢价指数拉取失败: {e}')

    # 8/15: DEX 链上24h买卖方向 (真实钱包交易, 比合约主动买卖更接近真实方向)
    dex_map = {}
    try:
        all_syms2 = [s for _, s, _, _ in dump_alerts[:12]] + [s for _, s, _, _ in early_alerts[:5]]
        dex_map = get_dex_chain_stats(set(all_syms2))
        n_ok = sum(1 for v in dex_map.values() if v)
        log(f'DEX 链上数据: {n_ok}/{len(dex_map)} 币拿到 24h 买卖统计')
    except Exception as e:
        log(f'[警告] DEX 链上数据拉取失败: {e}')

    if dump_avg is not None:
        def dex_cell(sym):
            d = dex_map.get(sym)
            if not d:
                return '—'
            buy, sell, nb, ns = d
            if buy == 0 and sell == 0:
                return '—'  # 链上无活跃交易
            net = buy - sell
            color = '#0a0' if net >= 0 else '#c00'
            sign = '+' if net >= 0 else ''
            nb_s = nb if nb is not None else '?'
            ns_s = ns if ns is not None else '?'
            return f'<span style="color:{color}">净{sign}{net/1e4:.1f}万</span> ({nb_s}/{ns_s})'

        # ① 出货确认 (4条件, 深红) + 高风险 (P75达线, 浅红) — 合并表
        # 🅢 = 该币在今日 SHORT Top10 中 (2026-08-11 加, 一眼看出模型看空的币哪些在出货)
        for kind, sym, pct, avg in dump_alerts[:12]:
            if kind == 'dump':
                label = '⚠️出货中'
                style = 'background:#f5c6c6;font-weight:bold'
            else:
                label = '⚠️高风险'
                style = 'background:#fdecea'
            tag = ' 🅢' if sym in short_syms else ''
            prem = premium_map.get(sym)
            prem_txt = f'{prem:+.2f}%' if prem is not None else '—'
            dump_rows.append(
                f'<tr style="{style}">{td_l % f"{label} {sym}{tag}"}'
                f'{td % f"{pct:.1f}%"}{td % f"{avg:.1f}%"}{td % prem_txt}{td % dex_cell(sym)}</tr>'
            )
        # ② 早期迹象 (OI先撤, 黄色) — 独立专栏
        for kind, sym, pct, avg in early_alerts[:10]:
            tag = ' 🅢' if sym in short_syms else ''
            early_rows.append(
                f'<tr style="background:#fff3cd">{td_l % sym}'
                f'{td % f"{pct:.1f}%"}{td % "OI已撤,价格未崩"}{td % dex_cell(sym)}</tr>'
            )
        dump_section = f"""<h4 style="margin:4px 0;font-size:12px">⚠️ OI 出货预警 (P75={dump_avg:.1f}%, n={dump_n}次出货; 溢价+多头/-空头; 链上=真实钱包净买卖)</h4>
<table style="border-collapse:collapse;margin:4px 0">
<tr>{th % '币'}{th % '当前OI占流通%'}{th % '出货基准%'}{th % '溢价%'}{th % '链上24h净(买家/卖家)'}</tr>
{''.join(dump_rows) if dump_rows else td_l % '(今日无出货/高风险币)'}
</table>
<h4 style="margin:4px 0;font-size:12px">⚡ 早期迹象 (OI先撤, 价格未崩 — 出货前兆)</h4>
<table style="border-collapse:collapse;margin:4px 0">
<tr>{th % '币'}{th % '当前OI占流通%'}{th % '状态'}{th % '链上24h净(买家/卖家)'}</tr>
{''.join(early_rows) if early_rows else '<tr><td style="padding:3px 6px;border:1px solid #ddd;color:#888">(今日无早期迹象)</td></tr>'}
</table>"""
    else:
        dump_section = f"<h4 style=\"margin:4px 0;font-size:12px;color:#888\">OI 出货基准: 样本 {dump_n}/10 次 (攒满后自动按 P75 提醒)</h4>"

    # SHORT Top10 出货分析节 (2026-08-11 加; 8/11 加3日OI列)
    short_html_rows = []
    for sym, prob, oi_chg, oi3, oi_pct, state, profit in short_rows:
        style = ''
        if '出货中' in state:
            style = 'background:#f5c6c6;font-weight:bold'
        elif '早期' in state:
            style = 'background:#fff3cd'
        elif '堆积' in state:
            style = 'background:#e8f0fe'
        short_html_rows.append(
            f'<tr style="{style}">{td_l % sym}'
            f'{td % f"{prob:.1f}%"}{td % oi_chg}{td % oi3}{td % oi_pct}'
            f'{td % state}{td % profit}</tr>'
        )
    short_section = f"""<h4 style="margin:4px 0;font-size:12px">🔻 SHORT Top10 出货分析 (蓝=堆积▲高位/▼低位按15日均价分, 高位堆积7日跌6.1%; 黄=早期/红=出货中; 3日OI=近3日累计)</h4>
<table style="border-collapse:collapse;margin:4px 0">
<tr>{th % '币'}{th % 'prob'}{th % 'OI日变'}{th % '3日OI'}{th % 'OI占流通%'}{th % '状态'}{th % '浮盈'}</tr>
{''.join(short_html_rows) if short_html_rows else td_l % '(无SHORT数据)'}
</table>"""

    # 全市场/山寨市值监控 (CoinGecko 免费接口)
    mcap_section = ''
    gm = fetch_global_mcap()
    ar = fetch_altcoin_ranks()
    if gm and gm.get('total_mcap'):
        total = gm['total_mcap']
        btc_dom = gm.get('btc_dom') or 0.0
        eth_dom = gm.get('eth_dom') or 0.0
        alt_mcap = total * (1 - btc_dom / 100) if total else 0.0
        def _f1e12(v):
            return f'{v/1e12:.2f}万亿' if v >= 1e12 else f'{v/1e9:.0f}亿'
        mcap_section += f"""<h4 style="margin:4px 0;font-size:12px">🌐 全市场/山寨市值 (CoinGecko)</h4>
<table style="border-collapse:collapse;margin:4px 0">
<tr>{th % '总市值'}{th % 'BTC占比'}{th % 'ETH占比'}{th % '山寨总市值(近似)'}</tr>
<tr>{td_l % _f1e12(total)}{td % f'{btc_dom:.1f}%'}{td % f'{eth_dom:.1f}%'}{td_l % _f1e12(alt_mcap)}</tr>
</table>"""
    if ar:
        rank_rows = []
        for i, c in enumerate(ar, 1):
            chg = f"{c['chg']:+.1f}%" if c.get('chg') is not None else '—'
            mcap_txt = f"{c['mcap']/1e9:.1f}B" if c.get('mcap') else '—'
            rank_rows.append(f'<tr>{td_l % f"{i}. {c["symbol"]}"}{td_l % c["name"]}{td % mcap_txt}{td % chg}</tr>')
        mcap_section += f"""<h4 style="margin:4px 0;font-size:12px">🏆 山寨市值排名 Top{len(ar)} (不含BTC/稳定币)</h4>
<table style="border-collapse:collapse;margin:4px 0">
<tr>{th % '币种'}{th % '名称'}{th % '市值'}{th % '24h%'}</tr>
{''.join(rank_rows)}
</table>"""

    body = f"""<html><body style="font-family:Consolas,monospace;font-size:12px;color:#333">
<h3 style="margin:4px 0;font-size:14px">山寨成交额监控 {latest_str} (除市值TOP50/100, 亿USDT)</h3>
{mcap_section}
<table style="border-collapse:collapse;margin:4px 0">
<tr>{th % '口径'}{th % '今日'}{th % '5日均'}{th % '今/5日'}{th % '20日均'}{th % '今/20日'}</tr>
{''.join(rows)}
</table>
<h4 style="margin:4px 0;font-size:12px">增量集中度 (除TOP50, 今日vs昨日; 占净增/占正增/占今日; OI=净持仓变化)</h4>
<table style="border-collapse:collapse;margin:4px 0">
<tr>{th % '币'}{th % '增量(亿)'}{th % '占净增%'}{th % '占正增%'}{th % '占今日%'}{th % 'OI变化(万张合约/占流通%)'}{th % '多头成本(浮盈)'}</tr>
{''.join(conc_rows) if conc_rows else td_l % '(无正增量)'}
</table>
{vol_section}
{dump_section}
{short_section}
<h4 style="margin:4px 0;font-size:12px">近7日</h4>
<table style="border-collapse:collapse;margin:4px 0">
<tr>{th % '日期'}{th % '除TOP50'}{th % '除TOP100'}</tr>
{''.join(hist_rows)}
</table>
</body></html>"""

    # 2026-08-11 改: 每天固定发一封 (用户拍板), 放量与否都发, 标题区分
    if alerts:
        subject = f'⚠️ 山寨成交额放量 {latest_str}: ' + '; '.join(alerts)
    else:
        subject = f'📊 山寨资金日报 {latest_str} (TOP50={series["TOP50"][-1]/1e8:.1f}亿, 无放量)'
    send_mail(subject, body)

if __name__ == '__main__':
    main()
