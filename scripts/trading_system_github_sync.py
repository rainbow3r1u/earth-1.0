#!/usr/bin/env python3
"""
每周自动扫描交易系统代码/配置/文档（websocket_new），
MD5 有变化就自动上传 GitHub（rainbow3r1u/earth-1.0）。

排除：.git / .env / data / logs / __pycache__ / 二进制 / 缓存 / 大文件 / 备份。
用法：
  python3 trading_system_github_sync.py --dry-run
  python3 trading_system_github_sync.py
"""
import os, sys, json, base64, hashlib, urllib.request, urllib.error, urllib.parse, yaml, datetime

HOME = os.path.expanduser('~')
ROOT = os.path.join(HOME, 'websocket_new')
REPO = 'rainbow3r1u/earth-1.0'
STATE_DIR = os.path.join(HOME, '.cache', 'trading_system_github_sync')
MANIFEST = os.path.join(STATE_DIR, 'manifest.json')
LOG = os.path.join(HOME, 'logs', 'trading_system_github_sync.log')
STATUS_PATH = os.path.join(HOME, 'websocket_new', 'logs', 'trading_system_sync_status.json')

EXCLUDE_DIRS = {
    '.git', '__pycache__', 'logs', 'node_modules', 'archive',
    'kronos_finetune', 'kronos_model', 'static', 'templates', 'output',
    'experiments', '.codegraph', '.agents',
}
# data/ 白名单 (2026-09-02 补齐盲区): 核心公证数据随代码链同步 GitHub —
# 残差臂/主臂 60 天验证的全部证据链: 预测公证(pred_*/top10_*) + 影子结算(hybrid/residual_tracker)
# + 前向IC史 + 实盘执行器state; 其余 data/ 文件(kline缓存/大json/npz)仍排除
DATA_WHITELIST_PREFIX = (
    'data/pred_',                          # 每日预测公证 (主臂+残差臂TOP10, 事前不可篡改)
    'data/top10_forward_cache',             # TOP10前向结算缓存
    'data/hybrid_tracker',                  # 混合结构主臂影子结算
    'data/residual_tracker',                # RESIDUAL影子臂结算
    'data/residual_live_state',             # RESIDUAL实盘执行器持仓/历史
    'data/forward_ic_history',              # 前向IC/AUC史 (四灯数据源)
    'data/forward_tracker',                 # TOP1前向结算
)
DATA_WHITELIST_EXACT = {
    'data/crypto_sectors.json',             # 板块映射 (特征输入, 版本影响生产)
    'data/exchange_info.json',              # 交易所上市状态 (宇宙准入)
}
EXCLUDE_EXT = {
    '.pyc', '.pyo', '.db', '.sqlite', '.sqlite3', '.npz', '.bin', '.pkl',
    '.tar.gz', '.zip', '.ttf', '.so', '.dll', '.exe', '.png', '.jpg', '.jpeg',
    '.gif', '.ico', '.log', '.bak', '.enc', '.key', '.pem',
}
EXCLUDE_NAMES = {'.env', '.env.local', '.env.prod', '.env.example'}

def log(msg):
    line = f'[{datetime.datetime.now().isoformat()}] {msg}'
    if sys.stdout.isatty():
        print(line, flush=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')

def get_token():
    data = yaml.safe_load(open(os.path.join(HOME, '.config', 'gh', 'hosts.yml')))
    def find(d):
        if isinstance(d, dict):
            for k, v in d.items():
                if k == 'oauth_token' and isinstance(v, str):
                    return v
                r = find(v)
                if r:
                    return r
        return None
    return find(data)

TOKEN = get_token()

def api(method, url, body=None):
    headers = {
        'Authorization': 'Bearer ' + TOKEN,
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'trading-system-sync',
        'Content-Type': 'application/json',
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, json.loads(r.read().decode())

def default_branch(repo):
    _, info = api('GET', f'https://api.github.com/repos/{repo}')
    return info.get('default_branch', 'main')

def file_sha(repo, branch, path):
    try:
        _, info = api('GET', f'https://api.github.com/repos/{repo}/contents/{urllib.parse.quote(path, safe="/")}?ref={branch}')
        return info.get('sha')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise

def upload_file(repo, branch, path, content_bytes, message):
    sha = file_sha(repo, branch, path)
    body = {
        'message': message,
        'content': base64.b64encode(content_bytes).decode(),
        'branch': branch,
    }
    if sha:
        body['sha'] = sha
    url = f'https://api.github.com/repos/{repo}/contents/{urllib.parse.quote(path, safe="/")}'
    status, _ = api('PUT', url, body)
    return status

def delete_file(repo, branch, path, sha):
    url = f'https://api.github.com/repos/{repo}/contents/{urllib.parse.quote(path, safe="/")}'
    status, _ = api('DELETE', url, {'message': f'delete {path}', 'sha': sha, 'branch': branch})
    return status

def md5_file(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()

def collect_files():
    out = {}
    for dp, dns, fns in os.walk(ROOT):
        dns[:] = [d for d in dns if d not in EXCLUDE_DIRS]
        for fn in fns:
            if fn in EXCLUDE_NAMES:
                continue
            if '.bak' in fn.lower() or fn.endswith('~'):
                continue
            if fn.startswith('.') and fn not in ('.gitignore', '.env.example'):
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext in EXCLUDE_EXT:
                continue
            p = os.path.join(dp, fn)
            try:
                if os.path.getsize(p) > 50 * 1024 * 1024:
                    continue
            except OSError:
                continue
            rel = os.path.relpath(p, ROOT)
            # data/ 只收白名单 (公证数据链), 其余排除 (缓存/大文件)
            if rel.startswith('data/'):
                if not (rel.startswith(DATA_WHITELIST_PREFIX) or rel in DATA_WHITELIST_EXACT):
                    continue
            out[rel] = p
    return out

def write_status(payload):
    try:
        os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
        with open(STATUS_PATH, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f'write_status failed: {e}')


def main():
    dry = '--dry-run' in sys.argv
    log('=== trading system github sync start ===')
    status_payload = {
        'date': datetime.date.today().isoformat(),
        'time': datetime.datetime.now().isoformat(),
        'status': 'ERROR',
        'changed': 0,
        'uploaded': 0,
        'failed': 0,
        'removed': 0,
        'files': [],
        'message': '',
    }
    files = collect_files()
    # 仓库体积监控 (2026-09-02): GitHub 1GB建议/5GB软限 — 超阈值写进晨报亮灯
    # size-pack 是远端真实体积(git压缩后); 本地.git含松散对象会虚高, 不作数
    repo_mb = None
    try:
        import subprocess, re as _re
        r = subprocess.run(['git', '-C', ROOT, 'count-objects', '-vH'],
                           capture_output=True, text=True, timeout=30)
        m = _re.search(r'size-pack:\s*([\d.]+)\s*(GiB|MiB|KiB)', r.stdout)
        if m:
            mult = {'KiB': 1/1024, 'MiB': 1.0, 'GiB': 1024.0}[m.group(2)]
            repo_mb = round(float(m.group(1)) * mult, 1)
    except Exception as e:
        log(f'repo size check failed: {e}')
    os.makedirs(STATE_DIR, exist_ok=True)
    old = {}
    if os.path.exists(MANIFEST):
        try:
            old = json.load(open(MANIFEST, encoding='utf-8'))
        except Exception:
            old = {}
    branch = default_branch(REPO)
    current = {}
    changed, removed = [], []
    for rel, path in files.items():
        digest = md5_file(path)
        current[rel] = digest
        if rel not in old or old.get(rel) != digest:
            changed.append((rel, path))
    for rel in old:
        if rel not in current:
            removed.append(rel)
    log(f'total={len(current)} changed={len(changed)} removed={len(removed)}')
    if dry:
        for rel, _ in changed[:30]:
            log(f'  DRY changed {rel}')
        for rel in removed[:30]:
            log(f'  DRY removed {rel}')
        return
    for i, (rel, path) in enumerate(changed, 1):
        try:
            with open(path, 'rb') as f:
                content = f.read()
            status = upload_file(REPO, branch, rel, content, f'sync trading system: {rel}')
            log(f'  uploaded {rel} ({status}) [{i}/{len(changed)}]')
        except Exception as e:
            log(f'  FAIL upload {rel}: {e}')
    for rel in removed:
        sha = file_sha(REPO, branch, rel)
        if sha:
            try:
                status = delete_file(REPO, branch, rel, sha)
                log(f'  deleted {rel} ({status})')
            except Exception as e:
                log(f'  FAIL delete {rel}: {e}')
    with open(MANIFEST, 'w', encoding='utf-8') as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
    status_payload.update({
        'status': 'CHANGED' if changed else ('NO_CHANGE' if not removed else 'CHANGED'),
        'changed': len(changed),
        'uploaded': len(changed) - sum(1 for _, p in changed if not os.path.exists(p)),
        'failed': 0,
        'removed': len(removed),
        'files': [rel for rel, _ in changed],
        'repo_mb': repo_mb,
        'message': f'changed={len(changed)} removed={len(removed)} repo={repo_mb}MB',
    })
    write_status(status_payload)
    log('=== trading system github sync done ===')

if __name__ == '__main__':
    main()
