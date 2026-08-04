#!/usr/bin/env python3
"""BTC/ETH ETF净流入 — Playwright无头浏览器从farside.co.uk扒取"""
import json, os, re, time
from datetime import datetime, timezone

OUT = os.path.join(os.path.dirname(__file__), 'etf_flow.json')

def parse_farside_table(page, url, label):
    """解析Farside ETF表格 — 格式: Date | Total | 各ticker...

    FIX 2026-07-12:
    - farside.co.uk 页面只显示最近约14天数据(20行表格含表头/统计行)
    - 对未更新日期显示"0"或"0.0"而非"-", 导致57.6%假0值污染数据
    - 修复: 跳过0值(视为数据未更新), 只保留有真实净流入/流出的日期
    - 增加等待时间到8秒确保表格完全渲染
    """
    print(f"扒取 {label} ETF...")
    page.goto(url, wait_until='domcontentloaded', timeout=30000)
    page.wait_for_timeout(8000)  # FIX: 5s→8s 确保表格完全渲染

    tables = page.query_selector_all('table')
    flows = []
    skipped_zero = 0

    for table in tables:
        rows = table.query_selector_all('tr')
        for row in rows:
            cells = row.query_selector_all('td, th')
            if len(cells) < 3:
                continue

            # 第一列是日期(格式: "28 Apr 2026")
            first = cells[0].inner_text().strip()
            # FIX 2026-08-04: Total在第二列(实测官网表格: Total|IBIT|FBTC|...|BTC|Fee)
            # 此前取最后一列是错的(那列是"BTC"统计, 与Total不同), 导致全部ETF数据错位
            if len(cells) < 3:
                continue
            last = cells[1].inner_text().strip()

            # 匹配日期: "28 Apr 2026" 或 "01 May 2026"
            date_match = re.match(r'^(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})$', first)
            if not date_match:
                continue

            # 解析日期
            months = {'Jan':'01','Feb':'02','Mar':'03','Apr':'04','May':'05','Jun':'06',
                      'Jul':'07','Aug':'08','Sep':'09','Oct':'10','Nov':'11','Dec':'12'}
            day = date_match.group(1).zfill(2)
            month = months[date_match.group(2)]
            year = date_match.group(3)
            date_str = f"{year}-{month}-{day}"

            # FIX: 解析Total值（最后一列）— 括号表示负数，'-'表示无数据
            # 如: "(112.2)" → -112.2, "284.4" → 284.4, "-" → 跳过
            if last == '-' or last == '':
                continue
            total_match = re.match(r'^\(?([-\d.,]+)\)?$', last)
            if total_match:
                val_str = total_match.group(1).replace(',', '')
                try:
                    val = float(val_str)
                    if last.startswith('('):
                        val = -val
                    # FIX 2026-07-12: 跳过0值 — farside对未更新日期显示0而非"-"
                    # 连续多日0.0几乎不可能是真实净流入(买卖完全相抵概率≈0)
                    if val == 0.0:
                        skipped_zero += 1
                        continue
                    flows.append({'date': date_str, 'total_flow': round(val, 2)})
                except ValueError:
                    continue

    if skipped_zero > 0:
        print(f"  [质量] 跳过 {skipped_zero} 个0.0值(farside未更新数据)")

    # 去重（有些页有多个表）
    seen = set()
    unique = []
    for f in flows:
        if f['date'] not in seen:
            seen.add(f['date'])
            unique.append(f)
    unique.sort(key=lambda x: x['date'])
    return unique

def scrape():
    from playwright.sync_api import sync_playwright
    import time

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        # FIX: BTC和ETH用独立browser context，避免页面间干扰
        ctx1 = browser.new_context(
            user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            viewport={'width': 1920, 'height': 1080},
        )
        btc = parse_farside_table(ctx1.new_page(), 'https://farside.co.uk/btc/', 'BTC')
        ctx1.close()
        time.sleep(2)

        ctx2 = browser.new_context(
            user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            viewport={'width': 1920, 'height': 1080},
        )
        eth = parse_farside_table(ctx2.new_page(), 'https://farside.co.uk/eth/', 'ETH')
        ctx2.close()

        browser.close()

    return btc, eth

def main():
    btc, eth = scrape()
    print(f"BTC ETF: {len(btc)} 天" + (f" ({btc[0]['date']} → {btc[-1]['date']})" if btc else " [空]"))
    print(f"ETH ETF: {len(eth)} 天" + (f" ({eth[0]['date']} → {eth[-1]['date']})" if eth else " [空]"))

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    # FIX 2026-07-12: 覆盖模式 — 新采集数据覆盖同日期旧数据
    # (之前追加模式不覆盖, 导致假0.0值永久残留)
    existing = {'btc': [], 'eth': []}
    if os.path.exists(OUT):
        try:
            with open(OUT) as f:
                existing = json.load(f)
        except Exception:
            pass
    # FIX 2026-07-12: 合并前先清除旧数据中的假0.0值
    # 原因: farside只返回最近14天, 14天前的0.0假值无法被新数据覆盖
    # 0.0值是farside页面渲染不完整时采集到的假值, 必须删除
    old_btc_count = len(existing.get('btc', []))
    old_eth_count = len(existing.get('eth', []))
    existing['btc'] = [d for d in existing.get('btc', []) if d['total_flow'] != 0.0]
    existing['eth'] = [d for d in existing.get('eth', []) if d['total_flow'] != 0.0]
    cleaned_btc = old_btc_count - len(existing['btc'])
    cleaned_eth = old_eth_count - len(existing['eth'])
    if cleaned_btc > 0 or cleaned_eth > 0:
        print(f"  [清理] 删除旧假0.0值: BTC {cleaned_btc}条, ETH {cleaned_eth}条")

    # 合并BTC: 新数据覆盖旧数据
    btc_map = {d['date']: d for d in existing.get('btc', [])}
    for d in btc:
        btc_map[d['date']] = d  # 新数据覆盖(同日期以最新采集为准)
    existing['btc'] = sorted(btc_map.values(), key=lambda x: x['date'])
    # 合并ETH: 新数据覆盖旧数据
    eth_map = {d['date']: d for d in existing.get('eth', [])}
    for d in eth:
        eth_map[d['date']] = d
    existing['eth'] = sorted(eth_map.values(), key=lambda x: x['date'])
    existing['updated'] = datetime.now(timezone.utc).isoformat()
    with open(OUT, 'w') as f:
        json.dump(existing, f)
    print(f"保存: {OUT} ({os.path.getsize(OUT)/1024:.1f}KB)")

    # 上传COS
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), '../../.env'))
        from qcloud_cos import CosConfig, CosS3Client
        config = CosConfig(
            Region=os.environ.get('COS_REGION', 'ap-seoul'),
            SecretId=os.environ.get('COS_SECRET_ID', ''),
            SecretKey=os.environ.get('COS_SECRET_KEY', ''),
            Endpoint=os.environ.get('COS_ENDPOINT', 'cos.ap-seoul.myqcloud.com'),
        )
        cos = CosS3Client(config)
        bucket = os.environ.get('COS_BUCKET', 'lhsj-1h-1314017643')
        with open(OUT, 'rb') as f:
            cos.put_object(Bucket=bucket, Key='klines/etf_data/etf_flow.json',
                           Body=f.read(), ContentType='application/json')
        print("[ETF] COS上传成功")
    except Exception as e:
        print(f"[ETF] COS上传失败: {e}")

if __name__ == '__main__':
    main()
