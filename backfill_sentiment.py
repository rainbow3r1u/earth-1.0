#!/usr/bin/env python3
"""情绪数据回填 — 2026-06-22 guardian 停用导致 sentiment_collector 断采约25天
数据源: Binance fapi 历史接口 (fundingRate 8h级 + globalLongShortAccountRatio 1h级)
文件格式与 sentiment_collector.py 完全一致; 只写缺失小时文件, 已有文件不覆盖
注意: trending/btc_dominance 无历史源, 不回填 (daily_predictor 也未使用这两个字段)
"""
import requests, json, os, time, bisect
from datetime import datetime, timezone, timedelta

DATA_DIR = "/home/myuser/sentiment_data"
GAP_START = datetime(2026, 6, 22, 7, tzinfo=timezone.utc)  # 最后一个已有文件 sentiment_20260622_06
FUND_SEED = datetime(2026, 6, 19, tzinfo=timezone.utc)     # 费率种子起点, 确保每币有 <=GAP_START 的最新结算
MAJORS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "DOGEUSDT",
          "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "MATICUSDT"]
FAPI = "https://fapi.binance.com"


def get(url, params, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code in (418, 429):
                time.sleep(20)
                continue
            if r.status_code != 200:
                print(f"  HTTP {r.status_code} {url} {params}")
                return []
            return r.json()
        except Exception as e:
            if i == tries - 1:
                print(f"  请求失败 {url}: {e}")
                return []
            time.sleep(3)
    return []


def fetch_symbols():
    info = get(f"{FAPI}/fapi/v1/exchangeInfo", {})
    return sorted(s['symbol'] for s in info.get('symbols', [])
                  if s.get('contractType') == 'PERPETUAL'
                  and s.get('quoteAsset') == 'USDT'
                  and s.get('status') == 'TRADING')


def fetch_funding(symbols, start_ms, end_ms):
    """每币 8h 级费率历史 -> {symbol: [(fundingTime_ms, rate)] 升序}"""
    out = {}
    for i, sym in enumerate(symbols):
        rows = get(f"{FAPI}/fapi/v1/fundingRate",
                   {"symbol": sym, "startTime": start_ms, "endTime": end_ms, "limit": 1000})
        if rows:
            out[sym] = sorted((int(r['fundingTime']), float(r['fundingRate'])) for r in rows)
        if (i + 1) % 100 == 0:
            print(f"  funding {i + 1}/{len(symbols)}")
        time.sleep(0.05)
    return out


def fetch_ls(symbol, start_ms, end_ms):
    """单币 1h 级多空比历史 -> {hour_start_ms: ratio}"""
    rows, cur = [], start_ms
    while True:
        batch = get(f"{FAPI}/futures/data/globalLongShortAccountRatio",
                    {"symbol": symbol, "period": "1h",
                     "startTime": cur, "endTime": end_ms, "limit": 500})
        if not batch:
            break
        rows.extend(batch)
        last = int(batch[-1]['timestamp'])
        if len(batch) < 500 or last >= end_ms:
            break
        cur = last + 1
        time.sleep(0.2)
    return {int(r['timestamp']): float(r['longShortRatio']) for r in rows}


def main():
    now = datetime.now(timezone.utc)
    end = now.replace(minute=0, second=0, microsecond=0)  # 当前小时由实时采集器负责
    gap_start_ms = int(GAP_START.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    symbols = fetch_symbols()
    print(f"USDT永续: {len(symbols)}")
    print("拉取资金费率历史...")
    funding = fetch_funding(symbols, int(FUND_SEED.timestamp() * 1000), end_ms)
    print(f"有费率数据: {len(funding)}/{len(symbols)}")
    times_map = {s: [t for t, _ in pairs] for s, pairs in funding.items()}

    print("拉取多空比历史 (10主流币)...")
    ls = {sym: fetch_ls(sym, gap_start_ms, end_ms) for sym in MAJORS}
    print(f"LS完成: {sum(len(m) for m in ls.values())} 条")

    written = skipped = 0
    h = GAP_START
    while h < end:
        h_ms = int(h.timestamp() * 1000)
        path = os.path.join(DATA_DIR, f"sentiment_{h.strftime('%Y%m%d_%H')}.json")
        if os.path.exists(path):
            skipped += 1
            h += timedelta(hours=1)
            continue
        rec = {"ts": int(h.timestamp()), "datetime": h.isoformat()}
        # 费率快照: 每币取 <= 小时起点的最近一次结算 (与 premiumIndex.lastFundingRate 语义一致)
        rates = {}
        for sym, pairs in funding.items():
            idx = bisect.bisect_right(times_map[sym], h_ms) - 1
            if idx >= 0:
                rates[sym] = pairs[idx][1]
        if rates:
            vals = sorted(rates.values(), reverse=True)
            top5 = vals[:5]
            rec["funding_rates"] = {
                "top5_avg": round(sum(top5) / len(top5) * 100, 4),
                "btc": round(rates.get("BTCUSDT", 0) * 100, 4),
                "eth": round(rates.get("ETHUSDT", 0) * 100, 4),
                "high_count": sum(1 for v in rates.values() if v > 0.0005),
                "neg_count": sum(1 for v in rates.values() if v < -0.0001),
            }
        ls_vals = {s: m.get(h_ms) for s, m in ls.items()}
        ls_vals = {s: v for s, v in ls_vals.items() if v is not None}
        if ls_vals:
            rec["long_short_ratios"] = {
                "btc": ls_vals.get("BTCUSDT", 0),
                "eth": ls_vals.get("ETHUSDT", 0),
                "avg_top10": round(sum(ls_vals.values()) / len(ls_vals), 2),
                "extreme_high": sum(1 for v in ls_vals.values() if v > 3),
                "extreme_low": sum(1 for v in ls_vals.values() if v < 0.5),
            }
        if "funding_rates" in rec or "long_short_ratios" in rec:
            with open(path, 'w') as f:
                json.dump(rec, f)
            written += 1
        h += timedelta(hours=1)
    print(f"完成: 写入 {written} 个小时文件, 跳过已存在 {skipped}")


if __name__ == "__main__":
    main()
