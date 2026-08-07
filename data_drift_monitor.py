#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据漂移监控 (2026-08-05 D指导 + 用户拍板: 检测层, 不碰模型)

两部分:
  A. 输入数据修订监控: 每日对关键外部数据文件做 MD5/行数/最新日期快照,
     跨天对比 — 行数不变但 MD5 变 = 历史行被修订 → 告警 (防止"数据源静默改历史"
     导致重放/归因失真, 8/3~8/4 无法复现问题的根治)
  B. 重放探针校验: 用昨日存档 pred_feats_YYYY-MM-DD.npz (Codex 8/5 新增),
     对探针币(0GUSDT/1000BONK/BTC)用"今日 K 线"手算特征日特征, 对比存档 —
     差异超阈值 = 该币 K 线被修订 → 告警 (漂移当天发现, 不等评审)

用法:
  python3 data_drift_monitor.py --init    # 首次建基线(今日快照)
  python3 data_drift_monitor.py --check   # 对比昨日快照 vs 今日, 输出报告 (每日8:30 cron)

输出:
  logs/data_drift.log         详细日志
  data/drift_report.json      晨报读取的摘要报告
"""

import os, sys, json, hashlib, datetime, glob

BASE = os.path.dirname(os.path.abspath(__file__))
SNAP_DIR = os.path.expanduser('~/.local/share/auto_trade/data_snapshots')
LOG = os.path.join(BASE, 'logs', 'data_drift.log')
REPORT = os.path.join(BASE, 'data', 'drift_report.json')
os.makedirs(SNAP_DIR, exist_ok=True)
os.makedirs(os.path.join(BASE, 'logs'), exist_ok=True)

MACRO_PATH = os.path.join(BASE, 'data', 'macro_assets.json')

# 受监控的外部数据文件 (生产训练依赖, 含采集器写入 + 数据源同步)
MONITORED = [
    '/home/myuser/blockchair_data/btc_chain.csv',
    '/home/myuser/coingecko_data/btc_dominance.json',
    '/home/myuser/coingecko_data/btc_mcap.json',
    '/home/myuser/defillama_data/protocol_map.json',
    '/home/myuser/defillama_data/btc_chain_tvl.json',
    '/home/myuser/hashrate_data/hashrate_history.json',
    '/home/myuser/stablecoin_data/btc_coinbase_premium_gap.json',
    '/home/myuser/stablecoin_data/btc_coinbase_premium_index.json',
    '/home/myuser/stablecoin_data/btc_korea_premium_index.json',
    '/home/myuser/stablecoin_data/stablecoin_exchange_netflow.json',
    os.path.join(BASE, 'data', 'fear_greed_history.json'),
    os.path.join(BASE, 'data', 'macro_assets.json'),
    os.path.join(BASE, 'data', 'etf_data', 'etf_flow.json'),
    os.path.join(BASE, 'data', 'liq_daily.json'),
]

# 重放探针: (币, 特征日偏移) — 样本日 ts 的特征日 = ts-86400
PROBES = ['0GUSDT', '1000BONKUSDT', 'BTCUSDT']
DRIFT_THRESHOLD = 0.05  # |存档值 - 手算值| > 0.05 判定漂移 (ret_1d_norm 尺度)


def log(msg):
    line = f'[{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] {msg}'
    print(line, flush=True)
    with open(LOG, 'a') as f:
        f.write(line + '\n')


def md5_file(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def file_rows(path):
    """行数(csv)或条目数(json)"""
    try:
        if path.endswith('.csv'):
            with open(path) as f:
                return sum(1 for _ in f) - 1  # 去表头
        else:
            d = json.load(open(path))
            # 常见结构: dict 的 list 值 或 顶层 list
            if isinstance(d, list):
                return len(d)
            if isinstance(d, dict):
                for v in d.values():
                    if isinstance(v, list):
                        return len(v)
                return len(d)
    except Exception:
        return -1


def latest_date(path):
    """最新日期(尽力而为, 失败返回 None)"""
    try:
        if path.endswith('.csv'):
            with open(path) as f:
                lines = f.readlines()
            if len(lines) > 1:
                return lines[-1].split(',')[0]
        else:
            d = json.load(open(path))
            if isinstance(d, list) and d and isinstance(d[0], dict) and 'date' in d[0]:
                return d[-1].get('date')
            if isinstance(d, dict):
                for k, v in d.items():
                    if isinstance(v, list) and v and isinstance(v[0], dict) and 'date' in v[0]:
                        return v[-1].get('date')
                # dict-of-date-keyed (如 etf_flow)
                for v in d.values():
                    if isinstance(v, list) and v and isinstance(v[0], dict) and 'date' in v[0]:
                        return v[-1].get('date')
    except Exception:
        pass
    return None



def macro_meta(path):
    """macro_assets.json 结构化指纹: 资产清单 + 全量日期→收盘价映射
    (8/7修正: 旧版滚动窗口MD5每天集合必变→必然误报; 现存储逐日值, 比对时取交集)"""
    try:
        d = json.load(open(path)).get('data', {})
        meta, closes = {}, {}
        for name, dd in d.items():
            ks = sorted(dd.keys())
            meta[name] = {'days': len(ks), 'first': ks[0] if ks else None, 'last': ks[-1] if ks else None}
            closes[name] = {k: dd[k].get('close') for k in ks}
        return {'assets': meta, 'closes': closes}
    except Exception as e:
        return {'error': str(e)[:80]}


def snapshot():
    snap = {'date': datetime.date.today().isoformat(), 'files': {}}
    for p in MONITORED:
        if not os.path.exists(p):
            snap['files'][p] = {'exists': False}
            continue
        try:
            entry = {
                'exists': True,
                'md5': md5_file(p),
                'rows': file_rows(p),
                'latest': latest_date(p),
            }
            if p == MACRO_PATH:
                entry['macro'] = macro_meta(p)
            snap['files'][p] = entry
        except Exception as e:
            snap['files'][p] = {'exists': True, 'error': str(e)[:80]}
    return snap


def load_prev_snapshot():
    snaps = sorted(glob.glob(os.path.join(SNAP_DIR, 'snapshot_*.json')))
    if not snaps:
        return None
    return json.load(open(snaps[-1]))


def save_snapshot(snap):
    path = os.path.join(SNAP_DIR, f"snapshot_{snap['date']}.json")
    with open(path, 'w') as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    # 只保留最近 10 份
    for old in sorted(glob.glob(os.path.join(SNAP_DIR, 'snapshot_*.json')))[:-10]:
        os.remove(old)


# ---------------- B. 重放探针校验 ----------------

def probe_replay_check():
    """用昨日存档 pred_feats_*.npz 对比今日 K 线手算值"""
    feats = sorted(glob.glob(os.path.join(BASE, 'data', 'pred_feats_*.npz')))
    if not feats:
        return {'status': 'SKIP', 'detail': '无历史存档(8/6 起每日首产)'}
    import numpy as np
    prev = np.load(feats[-1])
    ts = int(prev['ts'])
    syms = [str(s) for s in prev['syms']]
    feats_arr = prev['feats']
    feat_day = ts - 86400  # 特征日 = 样本日 - 1天
    issues = []
    checked = 0
    for sym in PROBES:
        if sym not in syms:
            issues.append(f'{sym}: 不在存档({len(syms)}币)')
            continue
        row = feats_arr[syms.index(sym)]
        arch_c0 = float(row[0])  # ret_1d_norm (原始, 未winsor)
        # 今日 K 线手算 (fetch_klines_full 同源)
        try:
            sys.path.insert(0, BASE)
            import auto_dual_trade as adt
            kls = adt.fetch_klines_full([sym])[sym]
            closes = {int(k['t']) // 1000: float(k['c']) for k in kls}
            if feat_day not in closes or (feat_day - 86400) not in closes:
                issues.append(f'{sym}: K线缺特征日/前日')
                continue
            ret = closes[feat_day] / closes[feat_day - 86400] - 1.0
            # 20日std(含特征日, 与构建一致)
            days = sorted(closes.keys())
            idx = days.index(feat_day)
            window = [closes[days[j]] / closes[days[j] - 86400] - 1.0
                      for j in range(max(0, idx - 19), idx + 1)]
            std20 = float(np.std(window))
            calc = ret / std20 if std20 > 1e-12 else 0.0
            diff = abs(arch_c0 - calc)
            checked += 1
            if diff > DRIFT_THRESHOLD:
                issues.append(f'{sym}: 列0漂移 存档={arch_c0:.4f} 手算={calc:.4f} Δ={diff:.4f} (>0.05, K线疑被修订)')
            else:
                log(f'  探针 {sym}: 存档={arch_c0:.4f} 手算={calc:.4f} Δ={diff:.4f} ✅')
        except Exception as e:
            issues.append(f'{sym}: 校验异常 {str(e)[:60]}')
    if issues:
        return {'status': 'ALERT', 'checked': checked, 'issues': issues}
    return {'status': 'OK', 'checked': checked, 'detail': f'{checked}探针一致(样本日 {datetime.datetime.utcfromtimestamp(ts).date()})'}


# ---------------- 主流程 ----------------

def run_check():
    prev = load_prev_snapshot()
    if prev is None:
        log('无历史快照, 仅建立今日基线 (--init 语义)')
        save_snapshot(snapshot())
        return
    cur = snapshot()
    items = []
    alerts = []
    for p in MONITORED:
        name = os.path.basename(p)
        old = prev['files'].get(p, {})
        new = cur['files'].get(p, {})
        if not new.get('exists', False):
            if old.get('exists', False):
                items.append({'file': name, 'status': 'ALERT', 'detail': '文件消失!'})
                alerts.append(f'{name}: 文件消失')
            continue
        if not old.get('exists', False):
            items.append({'file': name, 'status': 'INFO', 'detail': '新文件'})
            continue
        if old.get('md5') == new.get('md5'):
            items.append({'file': name, 'status': 'OK', 'detail': '未变'})
            continue
        # macro_assets.json: 整表重取型文件, 行数启发式无效, 走结构化对比 (8/6 新增)
        if p == MACRO_PATH and old.get('macro') and new.get('macro'):
            om, nm = old['macro'], new['macro']
            if 'error' in om or 'error' in nm or 'closes' not in nm:
                items.append({'file': name, 'status': 'INFO', 'detail': 'macro指纹格式升级中, 本次跳过'})
                continue
            oa, na = om.get('assets', {}), nm.get('assets', {})
            gone = sorted(set(oa) - set(na))
            back = sorted(set(na) - set(oa))
            if gone:
                items.append({'file': name, 'status': 'ALERT', 'detail': f'资产消失: {gone} (采集器拉取失败?)'})
                alerts.append(f'{name}: 资产消失 {gone}! 生产特征将全零, 立即检查采集器')
                continue
            # 交集比对: 新旧文件共有日期逐日比close, 剔除新文件最新2天(美股未定稿)
            oc, nc = om.get('closes', {}), nm.get('closes', {})
            diffs = []
            for a in sorted(set(oc) & set(nc)):
                skip = set(sorted(nc[a])[-2:])
                for dte in sorted(set(oc[a]) & set(nc[a])):
                    if dte in skip:
                        continue
                    if oc[a][dte] != nc[a][dte]:
                        diffs.append(f'{a} {dte}: {oc[a][dte]}→{nc[a][dte]}')
            if diffs:
                items.append({'file': name, 'status': 'ALERT', 'detail': f'历史修订{len(diffs)}处: ' + '; '.join(diffs[:3])})
                alerts.append(f'{name}: 历史修订{len(diffs)}处(上游改动已定稿值): ' + '; '.join(diffs[:2]))
            elif back:
                items.append({'file': name, 'status': 'INFO', 'detail': f'资产恢复: {back}'})
            else:
                items.append({'file': name, 'status': 'OK', 'detail': f'每日滚动正常({len(na)}资产)'})
            continue
        # MD5 变了: 判断追加 vs 修订
        old_rows, new_rows = old.get('rows', -1), new.get('rows', -1)
        if old_rows >= 0 and new_rows >= 0:
            if new_rows > old_rows:
                items.append({'file': name, 'status': 'OK',
                              'detail': f'追加 {new_rows-old_rows} 行 ({old_rows}→{new_rows})'})
            elif new_rows == old_rows:
                items.append({'file': name, 'status': 'ALERT',
                              'detail': '行数不变但MD5变 = 历史行被修订!'})
                alerts.append(f'{name}: 历史修订(行数{old_rows}不变)')
            else:
                items.append({'file': name, 'status': 'ALERT',
                              'detail': f'行数减少 {old_rows}→{new_rows} = 数据被删!'})
                alerts.append(f'{name}: 行数减少')
        else:
            items.append({'file': name, 'status': 'INFO', 'detail': 'MD5变(无法判断行数)'})

    replay = probe_replay_check()
    if replay['status'] == 'ALERT':
        alerts.extend(replay['issues'])
    report = {
        'date': cur['date'],
        'status': 'ALERT' if alerts else 'OK',
        'file_check': items,
        'replay': replay,
        'alerts': alerts,
    }
    with open(REPORT, 'w') as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    log(f'数据漂移检查完成: {"ALERT " + str(len(alerts)) + " 项" if alerts else "OK"}')
    for a in alerts:
        log(f'  ⚠️ {a}')
    save_snapshot(cur)


if __name__ == '__main__':
    if '--init' in sys.argv:
        save_snapshot(snapshot())
        log('基线快照已建立')
    else:
        run_check()
