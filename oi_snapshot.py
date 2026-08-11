#!/usr/bin/env python3
"""每日 OI 快照备份 (2026-08-10): 把 oi_daily.json 存成带日期的全量快照上传 COS

Why: 币安 openInterestHist API 只保留最近 30 天 OI 历史; 本地 oi_daily.json 是唯一长历史,
     此前仅"覆盖式"上传 COS(永远只有最新版)。若本地文件损坏/误删, 历史将永久丢失。
     本脚本每日存一份全量快照 → COS klines/oi_snapshots/oi_daily_YYYY-MM-DD.json,
     任何历史时点可完整恢复。

运行时机: 每日 06:20 (06:00 数据采集更新 OI 之后)
"""
import os, sys, json, shutil, datetime as dt

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
CST = dt.timezone(dt.timedelta(hours=8))
LOG_FILE = os.path.join(BASE, 'logs', 'oi_snapshot.log')

def log(msg):
    line = f'[{dt.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")}] {msg}'
    print(line, flush=True)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')

OI_CACHE = '/home/myuser/backtester/data_cache/oi_daily.json'

def upload_to_cos(local_path, cos_key, label):
    """上传单个文件到COS (与 daily_data_collection._upload_to_cos 同源)"""
    try:
        from qcloud_cos import CosConfig, CosS3Client
        env_file = os.path.join(BASE, '.env')
        cos_vars = {}
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    if '=' in line and not line.startswith('#'):
                        k, v = line.strip().split('=', 1)
                        cos_vars[k] = v
        config = CosConfig(
            Region=cos_vars.get('COS_REGION', 'ap-seoul'),
            SecretId=cos_vars['COS_SECRET_ID'],
            SecretKey=cos_vars['COS_SECRET_KEY'],
            Endpoint=cos_vars.get('COS_ENDPOINT', 'cos.ap-seoul.myqcloud.com'),
        )
        client = CosS3Client(config)
        with open(local_path, 'rb') as f:
            client.put_object(Bucket=cos_vars['COS_BUCKET'], Key=cos_key, Body=f.read())
        log(f'📤 COS上传: {label} → {cos_key}')
        return True
    except Exception as e:
        log(f'⚠️ COS上传失败 ({label}): {e}')
        return False

def main():
    if not os.path.exists(OI_CACHE):
        log(f'❌ OI 缓存不存在: {OI_CACHE}')
        return
    # 校验 OI 数据可读且非空
    try:
        with open(OI_CACHE) as f:
            data = json.load(f)
        n_sym = len(data)
        n_rec = sum(len(v) if isinstance(v, dict) else 0 for v in data.values())
        if n_sym == 0 or n_rec == 0:
            log(f'❌ OI 数据为空 (币数={n_sym}, 记录数={n_rec}), 放弃备份')
            return
    except Exception as e:
        log(f'❌ OI 读取失败: {e}')
        return

    today = dt.datetime.now(CST).strftime('%Y-%m-%d')
    # 本地快照目录: 留最近 7 天 (COS 是权威, 本地只留短窗口防堆积)
    snap_dir = os.path.join('/home/myuser/backtester/data_cache/oi_snapshots')
    os.makedirs(snap_dir, exist_ok=True)
    local_snap = os.path.join(snap_dir, f'oi_daily_{today}.json')
    shutil.copy2(OI_CACHE, local_snap)

    # COS 快照 (永久保留)
    cos_key = f'klines/oi_snapshots/oi_daily_{today}.json'
    ok = upload_to_cos(local_snap, cos_key, f'OI快照 {today}')

    # 清理本地 7 天前的快照 (COS 已有, 本地不堆积)
    cutoff = dt.datetime.now(CST) - dt.timedelta(days=7)
    cleaned = 0
    for f in os.listdir(snap_dir):
        p = os.path.join(snap_dir, f)
        try:
            mtime = dt.datetime.fromtimestamp(os.path.getmtime(p), tz=CST)
            if mtime < cutoff:
                os.remove(p)
                cleaned += 1
        except Exception:
            pass

    log(f'✅ OI 快照完成: {today} (币数={n_sym}, 记录数={n_rec}, 本地清理{cleaned}个旧快照, COS={"成功" if ok else "失败"})')

if __name__ == '__main__':
    main()
