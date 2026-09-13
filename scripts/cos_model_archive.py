#!/usr/bin/env python3
"""模型归档到 COS (2026-09-13 立, 用户指示"是不是可以保存pkl").

背景: 生产每日 08:25 会把模型上传 COS, 但用的是**固定 key**(`klines/cache/xgb_daily_long.pkl`)
      → 每天覆盖; 且 bucket **未开版本控制** → 旧模型不可恢复。
      实测代价: 8/03 的模型因此永久丢失, "8/03的模型=现在的模型" 只能靠代码审计推断, 无法实物比对。
本脚本: 以**日期化 key** 追加归档, 不影响任何生产逻辑(独立脚本, 不碰 auto_dual_trade.py)。
      key 规范: `klines/cache/models/xgb_daily_{long,short}_YYYYMMDD.pkl`
      存储成本: 0.86MB×2×365 ≈ 630MB/年 (标准存储 ~0.1元/GB/月 → 约 1 元/年)

额外能力:
  - 标尺回溯: 有了历史模型实物, `model_probe.py` 可对任意历史模型在固定输入上重算 → "模型是否被改过"从哈希比对升级为实物比对
  - 训练数据(--with-npz): train_data_latest.npz 717MB/天 → 日期化并滚动保留 N 天(默认30 ≈ 21GB)

用法:
  python3 scripts/cos_model_archive.py              # 归档今日模型(幂等)
  python3 scripts/cos_model_archive.py --with-npz   # 同时归档训练数据(滚动保留30天)
  python3 scripts/cos_model_archive.py --list       # 列出 COS 上已归档的模型
  python3 scripts/cos_model_archive.py --backfill   # 把本地现存所有日期模型补齐归档
"""
import os, sys, glob, json, argparse, datetime, logging

BASE = '/home/myuser/websocket_new'
MODELS = os.path.expanduser('~/.local/share/auto_trade/models')
NPZ = os.path.expanduser('~/.local/share/auto_trade/train_data_latest.npz')
PREFIX = 'klines/cache/models/'
NPZ_PREFIX = 'klines/cache/train_npz/'
NPZ_KEEP_DAYS = 30

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')
for _n in ('qcloud_cos', 'urllib3', 'requests'):
    logging.getLogger(_n).setLevel(logging.WARNING)   # 压掉 SDK 的 head/put 噪音
log = logging.getLogger('cos_archive')


def client():
    sys.path.insert(0, BASE)
    from dotenv import load_dotenv
    load_dotenv(f'{BASE}/.env')
    from qcloud_cos import CosConfig, CosS3Client
    cfg = CosConfig(Region=os.environ.get('COS_REGION', 'ap-seoul'),
                    SecretId=os.environ['COS_SECRET_ID'], SecretKey=os.environ['COS_SECRET_KEY'],
                    Endpoint=os.environ.get('COS_ENDPOINT', 'cos.ap-seoul.myqcloud.com'), Timeout=60)
    return CosS3Client(cfg), os.environ['COS_BUCKET']


def exists(c, b, key):
    try:
        c.head_object(Bucket=b, Key=key)
        return True
    except Exception:
        return False


def upload(c, b, path, key):
    if exists(c, b, key):
        return 'skip'
    with open(path, 'rb') as f:
        c.put_object(Bucket=b, Key=key, Body=f.read())
    return 'ok'


def archive_models(c, b, tag=None, quiet=False):
    n_ok = n_skip = 0
    for side in ('long', 'short'):
        for p in sorted(glob.glob(f'{MODELS}/xgb_daily_{side}_2*.pkl')):
            d = os.path.basename(p).split('_')[-1].replace('.pkl', '')
            if tag and d != tag:
                continue
            key = f'{PREFIX}{os.path.basename(p)}'
            r = upload(c, b, p, key)
            if r == 'ok':
                n_ok += 1
                if not quiet:
                    log.info(f'归档 {os.path.basename(p)} → {key}')
            else:
                n_skip += 1
    return n_ok, n_skip


def archive_npz(c, b):
    if not os.path.exists(NPZ):
        log.warning('无 train_data_latest.npz'); return
    d = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d')
    key = f'{NPZ_PREFIX}train_data_{d}.npz'
    r = upload(c, b, NPZ, key)
    log.info(f'训练数据 {"归档" if r=="ok" else "已存在"} → {key}')
    # 滚动清理
    try:
        resp = c.list_objects(Bucket=b, Prefix=NPZ_PREFIX, MaxKeys=1000)
        objs = sorted(resp.get('Contents', []), key=lambda o: o['LastModified'], reverse=True)
        for o in objs[NPZ_KEEP_DAYS:]:
            c.delete_object(Bucket=b, Key=o['Key'])
            log.info(f'清理旧训练数据 {o["Key"]}')
    except Exception as e:
        log.warning(f'清理失败: {e}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--with-npz', action='store_true', help='同时归档训练数据(滚动保留30天)')
    ap.add_argument('--backfill', action='store_true', help='补齐本地现存全部日期模型')
    ap.add_argument('--list', action='store_true', help='列出 COS 上已归档模型')
    a = ap.parse_args()
    c, b = client()
    if a.list:
        resp = c.list_objects(Bucket=b, Prefix=PREFIX, MaxKeys=1000)
        objs = sorted(resp.get('Contents', []), key=lambda o: o['Key'])
        print(f'COS 归档模型 ({len(objs)} 个):')
        for o in objs:
            print(f'  {o["Key"]:60s} {int(o["Size"]):>10,d}  {o["LastModified"]}')
        return 0
    tag = None if a.backfill else datetime.date.today().strftime('%Y%m%d')
    n_ok, n_skip = archive_models(c, b, tag=tag)
    log.info(f'模型归档: 新增 {n_ok}, 已存在跳过 {n_skip}')
    if a.with_npz:
        archive_npz(c, b)
    return 0


if __name__ == '__main__':
    sys.exit(main())
