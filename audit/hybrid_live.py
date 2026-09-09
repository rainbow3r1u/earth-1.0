#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HYBRID 混合结构实盘执行器 (3.8板块结构, 2026-09-08 部署, 第二账户)
=================================================================
结构 (完全对齐 3.8 影子臂 hybrid_tracker 口径, 唯一差异=真金白银):
  LONG : TOP10 全开 · 无止盈 · SL-8% · 72h到期 · 市价开平
  SHORT: 已关闭(2026-09-08晚用户拍板; 代码保留, 影子S5继续跟踪, 证伪线见MAX_DAILY_SHORT注释)
  资金 : 固定名义125U/笔 · 5x逐仓 · LONG≤10 · 72h · 最坏日全灭≈101U(7%权益) · 85%守卫(容量≈49笔)

与影子臂的已知偏差:
  ① 实盘08:21~08:27市价入场 vs 影子00:21 UTC开盘价入场 (滑点差)
  ② SL/TP 由交易所Algo单执行(CONTRACT_PRICE口径) vs 影子K线h/l扫描
  ③ funding/手续费为真实流水(income API记账) vs 影子公式估算
  ④ 权益守卫会在峰值日节流笔数(影子无资金约束) — 结构性差异, 非bug

安全: 凭证在 .env (HYBRID_BINANCE_API_KEY/SECRET), git忽略+同步排除双保险。
用法: python3 audit/hybrid_live.py trade|reconcile|status [--force]
"""
import fcntl, hmac, hashlib, json, math, os, sys, time
import requests
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, 'data')
STATE_FILE = os.path.join(DATA_DIR, 'hybrid_live_state.json')
LOCK_FILE = '/tmp/hybrid_live.lock'
EXINFO_CACHE = os.path.join(DATA_DIR, '.hybrid_exinfo_cache.json')
PRED_FIELD_LONG = 'top10_long'
PRED_FIELD_SHORT = 'top10_short'

# ==== 资金参数 (2026-09-08 用户部署: 10000CNY≈1400U, 3.8结构降档版) ====
NOTIONAL = 125.0      # 单笔名义U (2026-09-08晚用户拍板提额: 10单LONG止损≈101U=承受线; 5x下保证金25U/笔, 85%守卫容量≈49笔)
LEVERAGE = 5          # 逐仓杠杆 (2026-09-08晚二次拍板: SL8拉长持仓+双臂→并发峰值~35笔, 2x容量19笔不够; 镜像果5x+SL8已验证包络(51笔零强平), 爆仓距离≈-19%)
MAX_DAILY_LONG = 10    # LONG每日上限(TOP10全开)
MAX_DAILY_SHORT = 0    # SHORT侧已关闭(2026-09-08晚用户拍板: 直觉+数据双确认)。依据: S5影子36天与LONG日相关+0.07无对冲价值/后半段E归零(前18天+3.9U→后19天+0.2U, 125U口径)/加SHORT使最差日-66→-88U回撤205→243U。证伪线(重开条件): S5滚动20天日均E>+2U(125U口径)持续, 或出现corr转负的崩盘regime; 10/23终审复核。TP/SL常量保留备重开。
SL_PCT_LONG = 0.08    # LONG止损 8% (2026-09-08用户拍板对齐果账户; 依据9/7 TOP10模拟器1585笔网格: 胜率69→75%/右尾截杀96→56笔)
SL_PCT_SHORT = 0.05   # SHORT止损 5% (保持3.8原版; TP10封顶卖右尾结构, 放宽到8%将使盈亏平衡TP率37%→45%, 高于影子36天实测命中36%)
TP_PCT_SHORT = 0.10   # SHORT止盈 +10% (3.8口径)
HOLD_DAYS = 3         # 72h到期 (2026-09-08用户拍板; 右尾敞口放大器, 预研+111%)
BALANCE_BUF_RATIO = 0.85
BALANCE_MIN_ABORT = 30.0

CST = timezone(timedelta(hours=8))
DAY_MS = 86400000

# ==== API (第二账户, 独立凭证) ====
with open(os.path.join(BASE_DIR, '.env')) as f:
    for line in f:
        if '=' in line and not line.startswith('#'):
            k, v = line.strip().split('=', 1)
            os.environ[k] = v
API_KEY = os.environ.get('HYBRID_BINANCE_API_KEY', '')
API_SECRET = os.environ.get('HYBRID_BINANCE_API_SECRET', '')
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


def log(msg):
    print(f'[{datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")}] {msg}', flush=True)


def now_cst_min():
    n = datetime.now(CST)
    return n.hour * 60 + n.minute


# ==== 状态 ====
def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {'config': {'notional': NOTIONAL, 'leverage': LEVERAGE,
                           'sl_pct_long': SL_PCT_LONG, 'sl_pct_short': SL_PCT_SHORT,
                           'tp_short': TP_PCT_SHORT, 'hold_days': HOLD_DAYS},
                'open': {}, 'history': [], 'days': {}}


def save_state(st):
    with open(STATE_FILE + '.tmp', 'w') as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.rename(STATE_FILE + '.tmp', STATE_FILE)


# ==== 交易所过滤器 (缓存7天) ====
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


def safe_cid(s):
    """币安clientOrderId仅允许 ^[.A-Z:/a-z0-9_-]{1,36}$ (龙虾USDT等中文名币兼容)"""
    import string
    ok = set(string.ascii_letters + string.digits + '.:/_-')
    return ''.join(c if c in ok else 'X' for c in s)[:36]


# ==== Algo条件单 (SL/TP统一走 algoOrder, -4120教训) ====
def place_cond_algo(sym, side, order_type, trigger_price, tick):
    """条件单: STOP_MARKET(止损) / TAKE_PROFIT_MARKET(止盈), closePosition全平"""
    r = signed('POST', '/fapi/v1/algoOrder', {
        'algoType': 'CONDITIONAL', 'symbol': sym, 'side': side,
        'type': order_type, 'triggerPrice': fmt_price(trigger_price, tick),
        'closePosition': 'true', 'workingType': 'CONTRACT_PRICE',
        'priceProtect': 'true'})
    return r.get('algoId') or r.get('orderId'), r


def open_algo_ids(sym):
    r = signed('GET', '/fapi/v1/openAlgoOrders', {'symbol': sym})
    if isinstance(r, list):
        return {(o.get('algoId') or o.get('orderId')) for o in r}, r
    return set(), r


def cancel_algo(sym, algo_id):
    if algo_id:
        r = signed('DELETE', '/fapi/v1/algoOrders', {'algoId': algo_id})
        if not (isinstance(r, dict) and r.get('code') not in (None, 200)):
            return True
    r2 = signed('DELETE', '/fapi/v1/algoOpenOrders', {'symbol': sym})
    return not (isinstance(r2, dict) and r2.get('code') not in (None, 200))


def cancel_all_algos(sym, pos):
    """撤该仓位全部条件单(SL+TP)"""
    cancel_algo(sym, pos.get('sl_algo_id'))
    if pos.get('tp_algo_id'):
        cancel_algo(sym, pos.get('tp_algo_id'))


def algo_triggered(algo_id, sym):
    """查条件单最终状态: True=已触发且有成交 | False=未触发 | None=查询失败"""
    if not algo_id:
        return None
    r = signed('GET', '/fapi/v1/algoOrder', {'algoId': str(algo_id), 'symbol': sym})
    if not (isinstance(r, dict) and r.get('algoStatus')):
        return None
    return r.get('algoStatus') == 'FINISHED' and bool(r.get('actualOrderId'))


def get_price(sym):
    r = S.get(f'{BASE_URL}/fapi/v1/ticker/price', params={'symbol': sym}, timeout=10)
    if r.status_code == 200:
        return float(r.json()['price'])
    return None


# ==== 核心动作 ====
def entry_tag(d=None):
    d = d or datetime.now(CST)
    return d.strftime('%y%m%d')


def open_one(sym, direction):
    """开一笔: 逐仓市价开仓 + 挂条件单(LONG:SL / SHORT:SL+TP)"""
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
        return None, f'qty={qty} 超出单笔上限'
    tag = entry_tag()
    # 逐仓+杠杆 (幂等)
    r = signed('POST', '/fapi/v1/marginType', {'symbol': sym, 'marginType': 'ISOLATED'})
    if isinstance(r, dict) and r.get('code') not in (None, -4046):
        log(f'  {sym} marginType: {r}')
    r = signed('POST', '/fapi/v1/leverage', {'symbol': sym, 'leverage': LEVERAGE})
    if isinstance(r, dict) and r.get('code') not in (None,):
        log(f'  {sym} leverage: {r}')
    # 市价开仓
    side = 'BUY' if direction == 'LONG' else 'SELL'
    eo = signed('POST', '/fapi/v1/order', {
        'symbol': sym, 'side': side, 'type': 'MARKET',
        'quantity': fmt_qty(qty, fl['step']),
        'newClientOrderId': safe_cid(f'hl-{sym[:14]}-{direction[0].lower()}-{tag}')})
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
    # 条件单 (CONTRACT_PRICE口径, 与影子结算一致)
    close_side = 'SELL' if direction == 'LONG' else 'BUY'
    if direction == 'LONG':
        sl_price = floor_step(entry * (1 - SL_PCT_LONG), fl['tick'])
        sl_id, so = place_cond_algo(sym, close_side, 'STOP_MARKET', sl_price, fl['tick'])
        if sl_id is None:
            log(f'  ⚠️ {sym} SL挂单失败: {str(so)[:120]} (reconcile会重挂)')
        tp_price, tp_id = None, None
    else:
        sl_price = floor_step(entry * (1 + SL_PCT_SHORT), fl['tick'])
        tp_price = floor_step(entry * (1 - TP_PCT_SHORT), fl['tick'])
        sl_id, so = place_cond_algo(sym, close_side, 'STOP_MARKET', sl_price, fl['tick'])
        tp_id, to = place_cond_algo(sym, close_side, 'TAKE_PROFIT_MARKET', tp_price, fl['tick'])
        if sl_id is None:
            log(f'  ⚠️ {sym} SL挂单失败: {str(so)[:120]}')
        if tp_id is None:
            log(f'  ⚠️ {sym} TP挂单失败: {str(to)[:120]}')
    rec = {'symbol': sym, 'direction': direction, 'qty': qty, 'entry': entry,
           'open_time': int(time.time() * 1000), 'date': datetime.now(CST).date().isoformat(),
           'sl_price': sl_price, 'sl_algo_id': sl_id,
           'tp_price': tp_price, 'tp_algo_id': tp_id, 'tag': tag}
    log(f'  开仓 {sym} [{direction}]: qty={qty} entry={entry} SL={sl_price}'
        f'{f" TP={tp_price}" if tp_price else ""} 名义≈{qty*entry:.0f}U 保证金≈{qty*entry/LEVERAGE:.0f}U')
    return rec, None


def fetch_income(sym, t0, t1):
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
    """平仓并结算入 history: 市价平 + income汇总(真实净U, 含funding)"""
    pos = st['open'].get(sym)
    if pos is None:
        return
    exit_time = exit_time or int(time.time() * 1000)
    if reason in ('到期',):
        cancel_all_algos(sym, pos)
        fl = get_filters(sym, load_exinfo())
        if fl is None:
            log(f'  ⚠️ {sym} 无法平仓(过滤器缺失), 下轮重试')
            return
        side = 'SELL' if pos['direction'] == 'LONG' else 'BUY'
        o = signed('POST', '/fapi/v1/order', {
            'symbol': sym, 'side': side, 'type': 'MARKET',
            'quantity': fmt_qty(pos['qty'], fl['step']),
            'reduceOnly': 'true',
            'newClientOrderId': safe_cid(f'hl-{sym[:12]}-x-{entry_tag()}')})
        if o.get('orderId') is None:
            log(f'  ⚠️ {sym} 平仓下单失败: {str(o)[:120]}, 下轮重试')
            return
        time.sleep(0.8)
        _final = None
        for _ in range(10):
            oo = signed('GET', '/fapi/v1/order', {'symbol': sym, 'orderId': o['orderId']})
            _final = oo.get('status')
            if _final == 'FILLED':
                exit_price = float(oo.get('avgPrice') or exit_price or pos['entry'])
                pos['qty'] = float(oo.get('executedQty') or pos['qty'])
                break
            time.sleep(0.4)
        if _final != 'FILLED':
            log(f'  ⚠️ {sym} 平仓未确认({_final}), 保留state下轮重试')
            return
        # 平完撤残余条件单(TP/SL另一张孤儿)
        cancel_all_algos(sym, pos)
    if exit_price is None:
        exit_price = get_price(sym) or pos['entry']
    if pos['direction'] == 'LONG':
        gross_pct = (exit_price / pos['entry'] - 1) if exit_price else 0.0
    else:
        gross_pct = (1 - exit_price / pos['entry']) if exit_price else 0.0
    time.sleep(1.5)
    net_u = fetch_income(sym, pos['open_time'] - 2000, int(time.time() * 1000) + 5000)
    rec = dict(pos)
    rec.update({'trigger': reason, 'exit': exit_price, 'exit_time': exit_time,
                'gross_pct': round(gross_pct * 100, 2), 'net_u': net_u})
    st['history'].append(rec)
    del st['open'][sym]
    log(f'  平仓 {sym} [{pos["direction"]}][{reason}]: entry={pos["entry"]} exit={exit_price} '
        f'gross={gross_pct*100:+.2f}% 实际净={net_u:+.3f}U')
    save_state(st)


def nominal_expiry_ms(pos):
    """48h到期 = 开仓日+2天的 00:21 UTC (=08:21 CST)。3.8口径strict48。"""
    d0 = datetime.strptime(pos['date'], '%Y-%m-%d').replace(tzinfo=timezone.utc)
    return int((d0 + timedelta(days=HOLD_DAYS, minutes=21)).timestamp() * 1000)


def reconcile(st, close_expired=True):
    """对账: 条件单触发落账 / 48h到期兜底平仓 / SL/TP单丢失重挂"""
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
        if abs(amt) < 1e-9:
            # 已离场: 查平仓成交
            aos = signed('GET', '/fapi/v1/allOrders',
                         {'symbol': sym, 'startTime': pos['open_time'] - 2000, 'limit': 50})
            close_side = 'SELL' if pos['direction'] == 'LONG' else 'BUY'
            outs = [o for o in (aos if isinstance(aos, list) else [])
                    if o.get('side') == close_side and o.get('status') == 'FILLED'
                    and float(o.get('executedQty') or 0) > 0]
            if outs:
                last = max(outs, key=lambda o: o.get('updateTime', 0))
                exit_price = float(last['avgPrice'])
                exit_time = int(last.get('updateTime') or time.time() * 1000)
                # 判定: SL触发→止损 / TP触发→止盈 / 否则手动
                if algo_triggered(pos.get('sl_algo_id'), sym) is True:
                    reason = '止损'
                elif algo_triggered(pos.get('tp_algo_id'), sym) is True:
                    reason = '止盈'
                else:
                    reason = '离场(手动/其他)'
            else:
                exit_price, exit_time = get_price(sym), int(time.time() * 1000)
                reason = '离场(未知)'
            cancel_all_algos(sym, pos)
            close_one(sym, st, reason, exit_price=exit_price, exit_time=exit_time)
            continue
        # 仍在场: 到期?
        if close_expired and now_ms >= nominal_expiry_ms(pos):
            log(f'  {sym} 48h到期, 平仓')
            close_one(sym, st, '到期')
            continue
        # 条件单在交易所吗? 缺则重挂 (SL必挂; SHORT的TP也必挂)
        ids, _r = open_algo_ids(sym)
        fl = get_filters(sym, load_exinfo())
        if fl:
            if pos.get('sl_algo_id') not in ids:
                sl_price = floor_step(pos['entry'] * (1 - SL_PCT_LONG if pos['direction'] == 'LONG' else 1 + SL_PCT_SHORT), fl['tick'])
                close_side = 'SELL' if pos['direction'] == 'LONG' else 'BUY'
                new_id, so = place_cond_algo(sym, close_side, 'STOP_MARKET', sl_price, fl['tick'])
                if new_id:
                    pos['sl_price'] = sl_price
                    pos['sl_algo_id'] = new_id
                    log(f'  {sym} SL单缺失已重挂 @ {sl_price}')
                    save_state(st)
                else:
                    log(f'  ⚠️ {sym} SL重挂失败: {str(so)[:120]}')
            if pos['direction'] == 'SHORT' and pos.get('tp_algo_id') not in ids:
                tp_price = floor_step(pos['entry'] * (1 - TP_PCT_SHORT), fl['tick'])
                new_id, to = place_cond_algo(sym, 'BUY', 'TAKE_PROFIT_MARKET', tp_price, fl['tick'])
                if new_id:
                    pos['tp_price'] = tp_price
                    pos['tp_algo_id'] = new_id
                    log(f'  {sym} TP单缺失已重挂 @ {tp_price}')
                    save_state(st)
                else:
                    log(f'  ⚠️ {sym} TP重挂失败: {str(to)[:120]}')
        time.sleep(0.15)


def wait_pred(today_str, timeout_s=1800):
    pf = os.path.join(DATA_DIR, f'pred_{today_str}.json')
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            d = json.load(open(pf))
            if d.get(PRED_FIELD_LONG) and d.get(PRED_FIELD_SHORT):
                return d
        except Exception:
            pass
        time.sleep(20)
    return None


def mode_trade(st, force=False):
    today_str = datetime.now(CST).date().isoformat()
    # 1. 先对账+平到期 (08:23 cron, 3.8到期名义时点08:21已过)
    log('== 对账/到期平仓 ==')
    reconcile(st, close_expired=True)
    # 2. 时间窗守卫
    m = now_cst_min()
    if not force and not (8 * 60 + 20 <= m <= 10 * 60):
        log(f'当前 {m//60:02d}:{m%60:02d} CST, 不在开仓窗口, 跳过开仓')
        return
    if today_str in st['days'] and st['days'][today_str].get('opened_long') is not None \
            and st['days'][today_str].get('opened_short') is not None:
        log(f'{today_str} 已开过仓, 跳过 (幂等)')
        return
    # 3. 等pred
    log('== 等待 pred 文件 ==')
    pred = wait_pred(today_str)
    if not pred:
        log('⚠️ 未取得 pred (流水线未产出或超时), 今日不开仓')
        st['days'][today_str] = {'opened_long': None, 'opened_short': None, 'note': '无pred'}
        save_state(st)
        return
    longs = pred.get(PRED_FIELD_LONG, [])[:MAX_DAILY_LONG]
    shorts = pred.get(PRED_FIELD_SHORT, [])[:MAX_DAILY_SHORT]
    # 4. 余额守卫 (2x/150U: 峰值日会节流, 结构性非bug)
    acct = signed('GET', '/fapi/v2/account')
    avail = float(acct.get('availableBalance', 0) or 0) if isinstance(acct, dict) else 0.0
    equity = float(acct.get('totalMarginBalance', 0) or 0) if isinstance(acct, dict) else 0.0
    margin_per = NOTIONAL / LEVERAGE
    used = max(equity - avail, 0.0)
    n_eq = int((equity * BALANCE_BUF_RATIO - used) // margin_per)
    n_cash = int(avail // margin_per)
    n_afford = max(min(n_eq, n_cash), 0)
    n_total = min(len(longs) + len(shorts), n_afford)
    log(f'== 开仓: LONG候选{len(longs)} + SHORT候选{len(shorts)}, 权益{equity:.1f}U 可用{avail:.1f}U '
        f'已占用{used:.1f}U → 计划{n_total}笔 (每笔保证金{margin_per:.0f}U/名义{NOTIONAL:.0f}U/{LEVERAGE}x逐仓) ==')
    if n_total < 1:
        log(f'⚠️ 可用余额不足以开1笔, 中止')
        st['days'][today_str] = {'opened_long': [], 'opened_short': [], 'note': f'余额不足 avail={avail:.1f}'}
        save_state(st)
        return
    # 5. 逐笔开仓 (多空交替, 同symbol双向互斥跳过)
    opened_l, opened_s, skipped = [], [], []
    budget = n_total
    # 多空交替开: L1,S1,L2,S2... 均衡两侧吃守卫配额
    pairs_inter = []
    for i in range(max(len(longs), len(shorts))):
        if i < len(longs): pairs_inter.append(('LONG', longs[i]))
        if i < len(shorts): pairs_inter.append(('SHORT', shorts[i]))
    for direction, c in pairs_inter:
        sym = c['symbol'] if isinstance(c, dict) else c
        if budget <= 0:
            skipped.append(f'{sym}({direction})(资金配额尽)')
            continue
        if sym in st['open']:
            skipped.append(f'{sym}({direction})(与在持重叠)')
            continue
        rec, err = open_one(sym, direction)
        if rec:
            st['open'][sym] = rec
            budget -= 1
            (opened_l if direction == 'LONG' else opened_s).append(sym)
            save_state(st)
        else:
            skipped.append(f'{sym}({direction})({err})')
        time.sleep(0.5)
    st['days'][today_str] = {'opened_long': opened_l, 'opened_short': opened_s, 'skipped': skipped,
                             'avail_u': round(avail, 1), 'n_planned': n_total}
    save_state(st)
    log(f'== 完成: LONG {len(opened_l)}笔 {opened_l}; SHORT {len(opened_s)}笔 {opened_s}; 跳过{len(skipped)} ==')
    total_u = sum(r['qty'] * r['entry'] for r in st['open'].values())
    log(f'当前在持 {len(st["open"])}笔, 总名义≈{total_u:.0f}U, 总保证金≈{total_u/LEVERAGE:.0f}U')


def mode_status(st):
    print(f'== HYBRID 3.8实盘执行器状态 (第二账户) ==')
    print(f'配置: 名义{NOTIONAL}U/笔 {LEVERAGE}x逐仓 SL: LONG-{SL_PCT_LONG*100:.0f}% '
          f'(SHORT侧已关: SL-{SL_PCT_SHORT*100:.0f}%/TP+{TP_PCT_SHORT*100:.0f}%参数保留) {HOLD_DAYS*24}h')
    acct = signed('GET', '/fapi/v2/account')
    if isinstance(acct, dict):
        print(f'账户: 可用 {acct.get("availableBalance")}U | 总权益 {acct.get("totalMarginBalance")}U')
    else:
        print(f'账户查询失败: {str(acct)[:120]}')
    if st['open']:
        print(f'\n在持 {len(st["open"])}笔:')
        for sym, p in st['open'].items():
            hold_h = (time.time() * 1000 - p['open_time']) / 3600000
            tp = f" TP={p.get('tp_price')}" if p.get('tp_price') else ''
            print(f"  {sym}: [{p['direction']}] entry={p['entry']} qty={p['qty']} "
                  f"SL={p['sl_price']}{tp} 已持{hold_h:.1f}h ({p['date']}批)")
    else:
        print('\n无持仓')
    if st['history']:
        print(f'\n历史 {len(st["history"])}笔:')
        tot = 0.0
        for r in st['history']:
            tot += r.get('net_u', 0)
            print(f"  {r['date']} {r['symbol']}: [{r['direction']}] {r['trigger']} "
                  f"{r['gross_pct']:+.2f}% 净{r.get('net_u', 0):+.3f}U")
        print(f'累计实际净盈亏: {tot:+.3f}U')


def link_test(st):
    """链路测试: 最小名义各开平一笔 LONG+SHORT, 验证签名/下单/条件单/income全链路"""
    log('== 链路测试: BTCUSDT LONG 最小仓 ==')
    global NOTIONAL
    _orig = NOTIONAL
    NOTIONAL = 100.0  # BTCUSDT step 0.001≈79U
    rec, err = open_one('BTCUSDT', 'LONG')
    if err:
        log(f'LONG开仓失败: {err}')
        NOTIONAL = _orig
        return False
    st['open']['BTCUSDT'] = rec
    save_state(st)
    time.sleep(2)
    close_one('BTCUSDT', st, '到期')
    log('== 链路测试: BTCUSDT SHORT 最小仓 (验证TP+SL双挂) ==')
    rec, err = open_one('BTCUSDT', 'SHORT')
    if err:
        log(f'SHORT开仓失败: {err}')
        NOTIONAL = _orig
        return False
    st['open']['BTCUSDT'] = rec
    save_state(st)
    time.sleep(2)
    close_one('BTCUSDT', st, '到期')
    NOTIONAL = _orig
    ok = len(st['history']) >= 2
    log(f'== 链路测试{"通过" if ok else "失败"}: history {len(st["history"])}笔 ==')
    return ok


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if mode not in ('trade', 'reconcile', 'status', 'linktest'):
        print(__doc__)
        sys.exit(1)
    if not API_KEY or not API_SECRET:
        print('HYBRID凭证缺失(.env), 退出')
        sys.exit(1)
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
    elif mode == 'linktest':
        link_test(st)
    else:
        mode_status(st)


if __name__ == '__main__':
    main()
