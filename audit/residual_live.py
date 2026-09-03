#!/usr/bin/env python3
"""RESIDUAL 残差模型 小资金实盘执行器 (2026-09-01, 2000 CNY≈280U 测试)

资金方案 (2026-09-01 确认, 2026-09-03 迁移72h):
  杠杆 5x 逐仓 | 单笔名义 40U | 单笔保证金 8U | 每日最多 10 笔 (影子臂top10)
  持仓 72h (开仓日+3天08:21到期市价平) | 稳态 3 批共存, 峰值 ~30 笔
  峰值 30 笔×8U=240U (81%资金, 守卫放宽至85%支持满配, 用户确认可承受)
  爆仓距离≈19% | SL-5% | 30笔全灭理论上限≈-62U(-22%), 实际因每日止损很难满配
  2026-09-03 前为 48h/2批; 迁移依据: 9/2 预研 29天580笔 72h 多赚+3101U(+111%)

与影子臂结算 (audit/residual_tracker.py) 的对齐与已知偏差:
  - ⚠️ 2026-09-03 起实盘为 72h 活体实验臂, 影子结算链维持 48h 至 10/23 终审 → 两口径分叉期
    (实盘收益预期应参考 72h 语义; 与影子对照时注意窗口差 24h)
  - SL规则一致: SL-5% / CONTRACT_PRICE 触发 (K线low口径, 非主程序的MARK_PRICE)
  - 已知偏差①: 实盘入场≈08:23-08:26 (pred落地后), 影子名义入场08:21 → 入场价差=执行滞后, 正是本测试要量的
  - 已知偏差②: 实盘SL挂在实盘成交价×0.95, 影子按其名义入场价×0.95 → ±0.0x%级, 可忽略
  - 已知偏差③: top10与在持仓位重叠的币跳过开仓(净持仓无法分批挂SL), 差异记录在 state.days.skipped_overlap

安全边界 (独立于主系统, 不碰 state.json / TRADING_ENABLED):
  - 只做 LONG, 只用 pred 文件 top10_long_residual 字段, 每日≤10笔
  - 开仓时间窗 08:20~10:00 CST 之外拒绝开仓 (--force 才能越过, 防止对历史pred误开)
  - 文件锁防并发; 当日已开过则跳过 (幂等)
  - 余额不足自动减笔数, <1笔则中止
  - 每小时 reconcile: SL触发/到期记录 + SL单丢失重挂

用法:
  python3 residual_live.py trade       # 08:21 cron: 到期平仓 → 等pred → 开新仓
  python3 residual_live.py reconcile   # 每小时 cron: 对账/记录SL触发/重挂丢失SL/兜底平到期
  python3 residual_live.py status      # 人工查看持仓与历史
"""
import os, sys, json, time, math, fcntl, hmac, hashlib
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(os.path.join(BASE, '..'))
sys.path.insert(0, BASE)
DATA_DIR = 'data'
STATE_FILE = os.path.join(DATA_DIR, 'residual_live_state.json')
EXINFO_CACHE = os.path.join(DATA_DIR, 'residual_live_exinfo.json')
LOCK_FILE = '/tmp/residual_live.lock'
PRED_FIELD = 'top10_long_residual'

# ==== 资金参数 (2000 CNY ≈ 280U 测试) ====
NOTIONAL = 40.0      # 单笔名义U (2026-09-01 用户确认上调: 20笔全灭≈-41U/-15%可接受)
LEVERAGE = 5         # 逐仓杠杆
MAX_DAILY = 10       # 每日最多开仓笔数
SL_PCT = 0.05        # 止损 5%
HOLD_DAYS = 3        # 持仓窗口 72h (2026-09-03 由48h迁移, 与标签72h终点语义对齐; 稳态3批共存, 峰值~30笔)
BALANCE_MIN_ABORT = 16.0       # 可用余额低于此值(1笔保证金8U×2)直接中止
BALANCE_BUF_RATIO = 0.85       # 可用余额允许动用 85% 做保证金 (2026-09-03 由60%放宽: 72h三批满配30笔×8U=240U需81%; 用户确认可承受)

CST = timezone(timedelta(hours=8))
DAY_MS = 86400000


def now_cst_min():
    n = datetime.now(CST)
    return n.hour * 60 + n.minute


def log(msg):
    print(f'[{datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")}] {msg}', flush=True)


# ==== API (签名方式与 auto_dual_trade.signed_request 同源) ====
with open('.env') as f:
    for line in f:
        if '=' in line and not line.startswith('#'):
            k, v = line.strip().split('=', 1)
            os.environ[k] = v
API_KEY = os.environ.get('BINANCE_API_KEY', '')
API_SECRET = os.environ.get('BINANCE_API_SECRET', '') or os.environ.get('BINANCE_SECRET_KEY', '')
BASE_URL = 'https://fapi.binance.com'
S = requests.Session()


def signed(method, endpoint, params=None, max_retries=3):
    params = dict(params or {})
    params['timestamp'] = int(time.time() * 1000)
    query = urlencode(params)
    sig = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    params['signature'] = sig
    headers = {'X-MBX-APIKEY': API_KEY}
    url = f'{BASE_URL}{endpoint}'
    for attempt in range(max_retries):
        try:
            r = S.request(method, url, params=params, headers=headers, timeout=15)
            if r.status_code == 429:
                time.sleep(int(r.headers.get('Retry-After', 5)))
                continue
            if r.status_code >= 500:
                time.sleep(min(2 ** attempt, 10))
                continue
            if r.status_code != 200:
                try:
                    return r.json()
                except Exception:
                    return {'error': True, 'http_code': r.status_code, 'msg': r.text[:200]}
            return r.json()
        except Exception as e:
            if attempt >= max_retries - 1:
                return {'error': True, 'msg': str(e)}
            time.sleep(2)
    return {'error': True, 'msg': 'retries exhausted'}


# ==== 状态 ====
def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {'config': {'notional': NOTIONAL, 'leverage': LEVERAGE, 'max_daily': MAX_DAILY},
                'open': {}, 'history': [], 'days': {}}


def save_state(st):
    with open(STATE_FILE + '.tmp', 'w') as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.rename(STATE_FILE + '.tmp', STATE_FILE)


# ==== 交易所过滤器 ====
def load_exinfo():
    try:
        c = json.load(open(EXINFO_CACHE))
        if time.time() - c.get('ts', 0) < 7 * DAY_MS / 1000:
            return c['symbols']
    except Exception:
        pass
    r = S.get(f'{BASE_URL}/fapi/v1/exchangeInfo', timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f'exchangeInfo失败: {r.status_code}')
    syms = r.json()['symbols']
    with open(EXINFO_CACHE + '.tmp', 'w') as f:
        json.dump({'ts': time.time(), 'symbols': syms}, f)
    os.rename(EXINFO_CACHE + '.tmp', EXINFO_CACHE)
    return syms


def get_filters(sym, exinfo):
    si = next((s for s in exinfo if s['symbol'] == sym), None)
    if si is None or si.get('status') != 'TRADING':
        return None
    fl = {f['filterType']: f for f in si['filters']}
    try:
        return {'step': float(fl['LOT_SIZE']['stepSize']),
                'tick': float(fl['PRICE_FILTER']['tickSize']),
                'min_notional': float(fl.get('MIN_NOTIONAL', {}).get('notional', 20)),
                'min_step_qty': float(fl['LOT_SIZE']['minQty']),
                'max_qty': float(fl['LOT_SIZE']['maxQty'])}
    except Exception:
        return None


def floor_step(v, step):
    if step <= 0:
        return v
    return math.floor(round(v / step, 9)) * step


def fmt_qty(q, step):
    s = f'{step:.10f}'.rstrip('0')
    dec = len(s.split('.')[1]) if '.' in s else 0
    return f'{q:.{dec}f}'


def fmt_price(p, tick):
    s = f'{tick:.10f}'.rstrip('0')
    dec = len(s.split('.')[1]) if '.' in s else 0
    return f'{p:.{dec}f}'


def place_sl_algo(sym, stop_price, tick):
    """SL条件单走 Algo Order API (普通order端点-4120被拒, 与主程序同款FIX);
    CONTRACT_PRICE触发=K线low口径, 对齐residual_tracker结算"""
    r = signed('POST', '/fapi/v1/algoOrder', {
        'algoType': 'CONDITIONAL', 'symbol': sym, 'side': 'SELL',
        'type': 'STOP_MARKET', 'triggerPrice': fmt_price(stop_price, tick),
        'closePosition': 'true', 'workingType': 'CONTRACT_PRICE',
        'priceProtect': 'true'})
    return r.get('algoId') or r.get('orderId'), r


def open_algo_ids(sym):
    """该币当前挂在交易所的未触发algo单algoId集合"""
    r = signed('GET', '/fapi/v1/openAlgoOrders', {'symbol': sym})
    if isinstance(r, list):
        return {(o.get('algoId') or o.get('orderId')) for o in r}, r
    return set(), r


def cancel_sl(sym, algo_id):
    """撤SL algo单: 先按algoId单撤, 不行再按symbol撤(该symbol此时仅我们这一张)"""
    if algo_id:
        r = signed('DELETE', '/fapi/v1/algoOrders', {'algoId': algo_id})
        if not (isinstance(r, dict) and r.get('code') not in (None, 200)):
            return True
        log(f'  {sym} 按algoId撤单回执: {str(r)[:100]}')
    r2 = signed('DELETE', '/fapi/v1/algoOpenOrders', {'symbol': sym})
    ok = not (isinstance(r2, dict) and r2.get('code') not in (None, 200))
    if not ok:
        log(f'  {sym} 撤SL失败: {str(r2)[:120]}')
    return ok


def get_price(sym):
    r = S.get(f'{BASE_URL}/fapi/v1/ticker/price', params={'symbol': sym}, timeout=10)
    if r.status_code == 200:
        return float(r.json()['price'])
    return None


# ==== 核心动作 ====
def entry_tag(d=None):
    d = d or datetime.now(CST)
    return d.strftime('%y%m%d')


def open_one(sym, st):
    """开一笔: 5x逐仓 市价30U + 挂SL-5%(CONTRACT_PRICE)"""
    exinfo = load_exinfo()
    fl = get_filters(sym, exinfo)
    if fl is None:
        return None, '不可交易/无过滤器'
    if fl['min_notional'] > NOTIONAL * 1.05:
        return None, f"最小名义{fl['min_notional']}U>单笔{NOTIONAL}U"
    price = get_price(sym)
    if not price or price <= 0:
        return None, '取价失败'
    qty = floor_step(NOTIONAL / price, fl['step'])
    if qty < fl['min_step_qty'] or qty * price < fl['min_notional']:
        return None, f'qty={qty} 低于最小'
    if qty > fl['max_qty']:
        return None, f'qty={qty} 超出单笔上限{fl["max_qty"]}'
    tag = entry_tag()
    # 逐仓 + 杠杆 (幂等, -4046 无需变更 忽略)
    r = signed('POST', '/fapi/v1/marginType', {'symbol': sym, 'marginType': 'ISOLATED'})
    if isinstance(r, dict) and r.get('code') not in (None, -4046):
        log(f'  {sym} marginType: {r}')
    r = signed('POST', '/fapi/v1/leverage', {'symbol': sym, 'leverage': LEVERAGE})
    if isinstance(r, dict) and r.get('code') not in (None,):
        log(f'  {sym} leverage: {r}')
    # 市价开多
    eo = signed('POST', '/fapi/v1/order', {
        'symbol': sym, 'side': 'BUY', 'type': 'MARKET',
        'quantity': fmt_qty(qty, fl['step']),
        'newClientOrderId': f'rl-{sym[:14]}-e-{tag}'[:36]})
    if eo.get('orderId') is None:
        return None, f'下单失败: {str(eo)[:120]}'
    # 等成交
    entry = None
    for _ in range(10):
        time.sleep(0.4)
        o = signed('GET', '/fapi/v1/order', {'symbol': sym, 'orderId': eo['orderId']})
        if o.get('status') == 'FILLED':
            entry = float(o.get('avgPrice') or 0) or None
            qty = float(o.get('executedQty') or qty)
            break
    if entry is None:
        log(f'  {sym} 未确认成交, 撤单兜底')
        signed('DELETE', '/fapi/v1/order', {'symbol': sym, 'orderId': eo['orderId']})
        return None, '未成交'
    # SL-5% (Algo Order API + CONTRACT_PRICE, 与residual_tracker结算口径一致)
    sp = floor_step(entry * (1 - SL_PCT), fl['tick'])
    sl_algo_id, so = place_sl_algo(sym, sp, fl['tick'])
    if sl_algo_id is None:
        log(f'  ⚠️ {sym} SL挂单失败: {str(so)[:120]} (仓位裸奔, reconcile会重挂)')
    else:
        # 确认真的挂在交易所 (主程序同款verify哲学)
        for _ in range(3):
            ids, _r = open_algo_ids(sym)
            if sl_algo_id in ids:
                log(f'  {sym} SL已确认挂交易所: algoId={sl_algo_id} @ {sp}')
                break
            time.sleep(1.0)
    rec = {'symbol': sym, 'direction': 'LONG', 'qty': qty, 'entry': entry,
           'open_time': int(time.time() * 1000), 'date': datetime.now(CST).date().isoformat(),
           'sl_price': sp, 'sl_algo_id': sl_algo_id, 'tag': tag}
    log(f'  开仓 {sym}: qty={qty} entry={entry} SL={sp} 名义≈{qty*entry:.1f}U 保证金≈{qty*entry/LEVERAGE:.1f}U')
    return rec, None


def fetch_income(sym, t0, t1):
    """该窗口内 COMMISSION+FUNDING_FEE+REALIZED_PNL 总和 (U)"""
    total = 0.0
    try:
        rows = signed('GET', '/fapi/v1/income',
                      {'symbol': sym, 'startTime': t0, 'endTime': t1, 'limit': 1000})
        if isinstance(rows, list):
            for e in rows:
                if e.get('incomeType') in ('COMMISSION', 'FUNDING_FEE', 'REALIZED_PNL'):
                    total += float(e['income'])
    except Exception as _e:
        log(f'  {sym} income失败: {_e}')
    return round(total, 4)


def close_one(sym, st, reason, exit_price=None, exit_time=None):
    """平仓并结算入 history: 市价平 + income汇总(真实净U)"""
    pos = st['open'].get(sym)
    if pos is None:
        return
    exit_time = exit_time or int(time.time() * 1000)
    if reason == '到期':
        # 撤SL algo单
        cancel_sl(sym, pos.get('sl_algo_id'))
        fl = get_filters(sym, load_exinfo())
        if fl is None:
            log(f'  ⚠️ {sym} 无法平仓(过滤器缺失), 下轮重试')
            return
        o = signed('POST', '/fapi/v1/order', {
            'symbol': sym, 'side': 'SELL', 'type': 'MARKET',
            'quantity': fmt_qty(pos['qty'], fl['step']),
            'reduceOnly': 'true',
            'newClientOrderId': f'rl-{sym[:12]}-x-{entry_tag()}'[:36]})
        if o.get('orderId') is None:
            log(f'  ⚠️ {sym} 平仓下单失败: {str(o)[:120]}, 下轮重试')
            return
        time.sleep(0.8)
        for _ in range(10):
            oo = signed('GET', '/fapi/v1/order', {'symbol': sym, 'orderId': o['orderId']})
            if oo.get('status') == 'FILLED':
                exit_price = float(oo.get('avgPrice') or exit_price or pos['entry'])
                pos['qty'] = float(oo.get('executedQty') or pos['qty'])
                break
            time.sleep(0.4)
        if exit_price is None:
            exit_price = get_price(sym) or pos['entry']
    gross_pct = (exit_price / pos['entry'] - 1) if exit_price else 0.0
    time.sleep(1.5)  # 等income落账
    # income时间戳为秒级截断且开仓手续费早于open_time → 窗口两侧各加缓冲; 同币上一笔仓位间隔≥2分钟不会串单
    net_u = fetch_income(sym, pos['open_time'] - 2000, int(time.time() * 1000) + 5000)
    rec = dict(pos)
    rec.update({'trigger': reason, 'exit': exit_price, 'exit_time': exit_time,
                'gross_pct': round(gross_pct * 100, 2), 'net_u': net_u})
    st['history'].append(rec)
    del st['open'][sym]
    log(f'  平仓 {sym} [{reason}]: entry={pos["entry"]} exit={exit_price} '
        f'gross={gross_pct*100:+.2f}% 实际净={net_u:+.3f}U')
    save_state(st)


def nominal_expiry_ms(pos):
    """名义到期 = 开仓日+3天的 00:21 UTC (=08:21 CST, 72h持有)。
    2026-09-03 用户决策: 48h→72h迁移 — 与标签语义(72h终点)对齐, 依据 9/2 预研
    (29天580笔: 72h比48h多赚+3101U/+111%, 剔史诗日仍+51U/天)。
    注意: 影子结算链(residual_tracker/晨报3.9)维持48h至10/23终审, 本执行器为72h活体实验臂。
    基于pos['date']计算 → 存量仓位自动延至72h (9/2批→9/5到期, 9/3批→9/6到期)。"""
    d0 = datetime.strptime(pos['date'], '%Y-%m-%d').replace(tzinfo=timezone.utc)
    return int((d0 + timedelta(days=HOLD_DAYS, minutes=21)).timestamp() * 1000)


def reconcile(st, close_expired=True):
    """对账: SL触发落账 / 到期兜底平仓 / SL单丢失重挂"""
    if not st['open']:
        return
    pr = signed('GET', '/fapi/v2/positionRisk')
    if not isinstance(pr, list):
        log(f'[reconcile] positionRisk失败: {str(pr)[:120]}')
        return
    amt_by_sym = {p['symbol']: float(p['positionAmt']) for p in pr}
    now_ms = int(time.time() * 1000)
    for sym in list(st['open'].keys()):
        pos = st['open'][sym]
        amt = amt_by_sym.get(sym, 0.0)
        if amt <= 0:
            # 已离场: 从allOrders找开仓后最后一笔SELL成交 (SL触发/手动), income给真实净额
            aos = signed('GET', '/fapi/v1/allOrders',
                         {'symbol': sym, 'startTime': pos['open_time'] - 2000, 'limit': 50})
            sells = [o for o in (aos if isinstance(aos, list) else [])
                     if o.get('side') == 'SELL' and o.get('status') == 'FILLED'
                     and float(o.get('executedQty') or 0) > 0]
            if sells:
                last = max(sells, key=lambda o: o.get('updateTime', 0))
                exit_price = float(last['avgPrice'])
                exit_time = int(last.get('updateTime') or time.time() * 1000)
                # SL触发成交价≈触发价±滑点; 手动/到期离场价远离触发价
                if pos.get('sl_price') and abs(exit_price / pos['sl_price'] - 1) < 0.01:
                    reason = '止损'
                else:
                    reason = '离场(手动/其他)'
            else:
                exit_price, exit_time = get_price(sym), int(time.time() * 1000)
                reason = '离场(未知)'
            close_one(sym, st, reason, exit_price=exit_price, exit_time=exit_time)
            continue
        # 仍在场: 到期? (72h名义到期=开仓日+3天08:21 CST; 08:21 trade cron主平, hourly兜底)
        if close_expired and now_ms >= nominal_expiry_ms(pos):
            log(f'  {sym} 72h到期, 平仓')
            close_one(sym, st, '到期')
            continue
        # SL algo单还在交易所吗? 不在则重挂 (防裸奔)
        ids, _r = open_algo_ids(sym)
        if pos.get('sl_algo_id') not in ids:
            fl = get_filters(sym, load_exinfo())
            if fl:
                sp = floor_step(pos['entry'] * (1 - SL_PCT), fl['tick'])
                new_id, so = place_sl_algo(sym, sp, fl['tick'])
                if new_id:
                    pos['sl_price'] = sp
                    pos['sl_algo_id'] = new_id
                    log(f'  {sym} SL单缺失已重挂 @ {sp} (algoId={new_id})')
                    save_state(st)
                else:
                    log(f'  ⚠️ {sym} SL重挂失败: {str(so)[:120]}')
        time.sleep(0.15)


def wait_pred(today_str, timeout_s=900):
    """等 pred 文件 + top10_long_residual 字段 (流水线≈08:22-08:25落地)"""
    pf = os.path.join(DATA_DIR, f'pred_{today_str}.json')
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            d = json.load(open(pf))
            cands = d.get(PRED_FIELD)
            if cands:
                return cands
            log(f'pred已落地但无{PRED_FIELD}字段, 继续等...')
        except Exception:
            pass
        time.sleep(20)
    return None


def mode_trade(st, force=False):
    today_str = datetime.now(CST).date().isoformat()
    # 1. 先对账+平到期 (08:21 集中到期)
    log('== 对账/到期平仓 ==')
    reconcile(st, close_expired=True)
    # 2. 时间窗守卫
    m = now_cst_min()
    if not force and not (8 * 60 + 20 <= m <= 10 * 60):
        log(f'当前 {m//60:02d}:{m%60:02d} CST, 不在开仓窗口(08:20-10:00), 跳过开仓')
        return
    if today_str in st['days'] and st['days'][today_str].get('opened'):
        log(f'{today_str} 已开过仓: {st["days"][today_str]["opened"]}, 跳过 (幂等)')
        return
    # 3. 等pred
    log('== 等待 pred 文件 ==')
    cands = wait_pred(today_str)
    if not cands:
        log(f'⚠️ 未取得 {PRED_FIELD} (流水线未产出或超时900s), 今日不开仓')
        st['days'][today_str] = {'opened': [], 'note': '无pred字段'}
        save_state(st)
        return
    cands = cands[:MAX_DAILY]
    # 4. 余额 → 笔数
    acct = signed('GET', '/fapi/v2/account')
    avail = float(acct.get('availableBalance', 0)) if isinstance(acct, dict) else 0.0
    margin_per = NOTIONAL / LEVERAGE
    n_afford = int(avail * BALANCE_BUF_RATIO // margin_per)
    n_target = min(len(cands), MAX_DAILY, max(n_afford, 0))
    log(f'== 开仓: 候选{len(cands)}笔, 可用{avail:.1f}U → 计划{n_target}笔 '
        f'(每笔保证金{margin_per:.0f}U/名义{NOTIONAL:.0f}U/{LEVERAGE}x逐仓) ==')
    if n_target < 1:
        log(f'⚠️ 可用余额不足以开1笔 (需≥{BALANCE_MIN_ABORT:.0f}U缓冲), 中止')
        st['days'][today_str] = {'opened': [], 'note': f'余额不足 avail={avail:.1f}'}
        save_state(st)
        return
    # 5. 逐笔开仓
    opened, skipped = [], []
    for c in cands:
        sym = c['symbol']
        if len(opened) >= n_target:
            skipped.append(f'{sym}(笔数上限)')
            continue
        if sym in st['open']:
            skipped.append(f'{sym}(与前批持仓重叠)')
            continue
        rec, err = open_one(sym, st)
        if rec:
            st['open'][sym] = rec
            opened.append(sym)
            save_state(st)
        else:
            skipped.append(f'{sym}({err})')
        time.sleep(0.5)
    st['days'][today_str] = {'opened': opened, 'skipped': skipped,
                             'avail_u': round(avail, 1), 'n_planned': n_target}
    save_state(st)
    log(f'== 完成: 开仓{len(opened)}笔 {opened}; 跳过{len(skipped)} {skipped if skipped else ""} ==')
    total_u = sum(r['qty'] * r['entry'] for r in st['open'].values())
    log(f'当前在持 {len(st["open"])}笔, 总名义≈{total_u:.1f}U, 总保证金≈{total_u/LEVERAGE:.1f}U')


def mode_status(st):
    print(f'== RESIDUAL 实盘执行器状态 ==')
    print(f'配置: 名义{NOTIONAL}U/笔 {LEVERAGE}x逐仓 SL-{SL_PCT*100:.0f}% 48h')
    acct = signed('GET', '/fapi/v2/account')
    if isinstance(acct, dict):
        print(f'账户: 可用 {acct.get("availableBalance")}U | 总权益 {acct.get("totalMarginBalance")}U')
    if st['open']:
        print(f'\n在持 {len(st["open"])}笔:')
        for sym, p in st['open'].items():
            hold_h = (time.time() * 1000 - p['open_time']) / 3600000
            print(f'  {sym}: entry={p["entry"]} qty={p["qty"]} SL={p["sl_price"]} '
                  f'已持{hold_h:.1f}h ({p["date"]}批)')
    else:
        print('\n无持仓')
    if st['history']:
        print(f'\n历史 {len(st["history"])}笔:')
        tot = 0.0
        for r in st['history']:
            tot += r.get('net_u', 0)
            print(f'  {r["date"]} {r["symbol"]}: {r["trigger"]} {r["gross_pct"]:+.2f}% '
                  f'净{r.get("net_u", 0):+.3f}U')
        print(f'累计实际净盈亏: {tot:+.3f}U')


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if mode not in ('trade', 'reconcile', 'status'):
        print(__doc__)
        sys.exit(1)
    # 文件锁
    lf = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log('另一实例运行中, 退出')
        sys.exit(0)
    st = load_state()
    if mode == 'trade':
        mode_trade(st, force='--force' in sys.argv)
    elif mode == 'reconcile':
        reconcile(st, close_expired=True)
        save_state(st)
    else:
        mode_status(st)


if __name__ == '__main__':
    main()
