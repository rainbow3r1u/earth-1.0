#!/usr/bin/env python3
"""新机器部署引导 — 从COS拉取交易系统全部数据文件
用法:
  1. git clone 仓库到 /home/myuser/websocket_new
  2. 配置 websocket_new/.env (含 COS_SECRET_ID/COS_SECRET_KEY/COS_REGION/COS_ENDPOINT/COS_BUCKET)
  3. python3 deploy/bootstrap_from_cos.py [--dry-run]

路径清单见 deploy/cos_paths.json (路径非敏感, 密钥只在.env)
"""
import os, sys, json

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.expanduser('~')


def resolve(path):
    """仓库相对路径 → 绝对路径 (约定系统根在 /home/myuser, 即HOME)"""
    if path.startswith('websocket_new/'):
        return os.path.join(HOME, path)
    return os.path.join(HOME, path)


def main():
    dry_run = '--dry-run' in sys.argv
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cos_paths.json')) as f:
        paths = json.load(f)

    from dotenv import load_dotenv
    load_dotenv(os.path.join(REPO_ROOT, '.env'))
    missing = [k for k in ('COS_SECRET_ID', 'COS_SECRET_KEY') if not os.environ.get(k)]
    if missing:
        print(f'❌ .env 缺少: {missing} (参考 .env.example)')
        sys.exit(1)

    from qcloud_cos import CosConfig, CosS3Client
    config = CosConfig(
        Region=os.environ.get('COS_REGION', paths['region']),
        SecretId=os.environ['COS_SECRET_ID'],
        SecretKey=os.environ['COS_SECRET_KEY'],
        Endpoint=os.environ.get('COS_ENDPOINT', ''),
        Timeout=60,
    )
    cos = CosS3Client(config)
    bucket = os.environ.get('COS_BUCKET', paths['bucket'])

    ok = fail = 0

    def download(cos_key, local_path):
        nonlocal ok, fail
        local_path = resolve(local_path)
        if dry_run:
            print(f'  [dry] {cos_key} -> {local_path}')
            return
        try:
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            r = cos.get_object(Bucket=bucket, Key=cos_key)
            with open(local_path, 'wb') as f:
                f.write(r['Body'].get_raw_stream().read())
            ok += 1
            print(f'  ✓ {cos_key} ({os.path.getsize(local_path)//1024}KB)')
        except Exception as e:
            fail += 1
            print(f'  ✗ {cos_key}: {e}')

    print('== 单文件 (每日同步) ==')
    for item in paths['daily_synced_files']:
        download(item['cos'], item['local'])

    print('== 单文件 (种子快照) ==')
    for item in paths['bootstrap_seed_files']:
        download(item['cos'], item['local'])

    print('== 目录 (每日同步) ==')
    for d in paths['daily_synced_dirs']:
        local_dir = resolve(d['local_dir'])
        marker = d['cos_prefix'].rstrip('/').split('/')[-1]
        print(f'  [{marker}] {d["cos_prefix"]} -> {local_dir}')
        token = ''
        count = 0
        while True:
            r = cos.list_objects(Bucket=bucket, Prefix=d['cos_prefix'], Marker=token, MaxKeys=1000)
            for c in r.get('Contents', []):
                key = c['Key']
                if key.endswith('/'):
                    continue
                rel = key[len(d['cos_prefix']):]
                if d.get('match') and d['match'] not in rel:
                    continue
                # 目录结构: YYYYMMDD/HH.json (sentiment) 或 file.json (defillama)
                if '/' in rel:
                    ymd, hh = rel.split('/', 1)
                    local_path = os.path.join(local_dir, f'sentiment_{ymd}_{hh}')
                else:
                    local_path = os.path.join(local_dir, rel)
                if dry_run:
                    count += 1
                    continue
                try:
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)
                    rr = cos.get_object(Bucket=bucket, Key=key)
                    with open(local_path, 'wb') as f:
                        f.write(rr['Body'].get_raw_stream().read())
                    count += 1
                except Exception as e:
                    fail += 1
                    print(f'    ✗ {key}: {e}')
            if r.get('IsTruncated') == 'true':
                token = r.get('NextMarker', '')
            else:
                break
        ok += count
        print(f'    {count} 个文件')

    print(f'\n完成: {ok} 成功, {fail} 失败' + (' (dry-run)' if dry_run else ''))
    if fail:
        sys.exit(2)


if __name__ == '__main__':
    main()
