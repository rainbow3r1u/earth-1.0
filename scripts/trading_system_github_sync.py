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
# ── 状态备份分支 (2026-09-19 立, 用户拍板方案B1) ──────────────────────────────
# 背景(2026-09-17 嵌合记录事故): 运行时账本 data/*_live_state.json 同时被
#   ① git 跟踪 ② 每日被实盘执行器改写 ③ 每日被本脚本推到 main
#   → 08:30 公证的 `git rebase -X theirs` 要对它做**三方合并** → 内容被改
#   → 账本出现嵌合记录 → 对账误判"已离场" → 交易所真仓变孤儿且裸奔 7 小时。
# 处置: 把这类"纯运行时状态"改推到**独立分支 state-backup** ——
#   · main 上不再有它们 → 本地 git add -A 抓不到 → rebase 两棵树里都没有
#     → **没有三方合并、没有 merge driver、没有窗口期**
#   · 备份仍在 GitHub(另一分支), 且本脚本用 Contents API 直推(**本来就不合并**)
#   · 公证完全不受影响(公证对象是 data/pred_*.json, 与账本无关)
# ⚠️ state-backup **必须从 main 创建一次**(含当时的账本), 之后 main 才会摘除它们。
STATE_BACKUP_BRANCH = 'state-backup'
STATE_BACKUP_PREFIXES = (
    'data/hybrid_live_state',       # 刘账户账本(实盘执行器每小时改写)
    'data/residual_live_state',     # 果账户账本(同上)
)


def branch_for(rel, default):
    """纯运行时状态 → 备份分支; 其余(代码/预告/影子账本) → 默认分支 main。"""
    return STATE_BACKUP_BRANCH if rel.startswith(STATE_BACKUP_PREFIXES) else default


def ensure_branch(repo, branch, from_branch):
    """备份分支不存在则从 from_branch 的 HEAD 创建(git Data API)。
    已存在 → 直接返回 True(幂等)。"""
    import urllib.error as _ue
    try:
        api('GET', f'https://api.github.com/repos/{repo}/branches/{urllib.parse.quote(branch)}')
        return True
    except _ue.HTTPError as e:
        if e.code != 404:
            raise
    _, ref = api('GET', f'https://api.github.com/repos/{repo}/git/ref/heads/{urllib.parse.quote(from_branch)}')
    sha = ref['object']['sha']
    api('POST', f'https://api.github.com/repos/{repo}/git/refs',
        {'ref': f'refs/heads/{branch}', 'sha': sha})
    return True
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
    'data/hybrid_tracker',                  # 混合结构主臂影子结算 (+ s5/变体档, 见下)
    # 2×2 对照档 (2026-09-12 用户批准): 显式列出, 避免日后"整理"前缀时被静默漏掉同步
    'data/hybrid_tracker_sl8.json',         #   SL-8%/48h
    'data/hybrid_tracker_h72.json',         #   SL-5%/72h
    'data/hybrid_tracker_sl8h72.json',      #   SL-8%/72h (对齐实盘)
    'data/residual_tracker',                # RESIDUAL影子臂结算
    'data/residual_live_state',             # RESIDUAL实盘执行器持仓/历史
    'data/hybrid_live_state',              # HYBRID 3.8实盘执行器(第二账户2026-09-08)
    'data/forward_ic_history',              # 前向IC/AUC史 (四灯数据源)
    'data/forward_tracker',                 # TOP1前向结算
)
DATA_WHITELIST_EXACT = {
    'data/crypto_sectors.json',             # 板块映射 (特征输入, 版本影响生产)
    'data/exchange_info.json',              # 交易所上市状态 (宇宙准入)
    'data/prod_fingerprint.jsonl',          # 生产指纹账本 (2026-09-13 立): 每日代码/模型/数据哈希, 永久留存
    'data/model_probe.jsonl',               # 模型标尺账本 (2026-09-13 立): 固定输入上的每日模型概率向量 → 测'模型是否被改过'
    'data/volq_shadow.json',                # 金矿格影子(C远×V高)前向证据 (9/13起): 10/23终审用
    'data/br_forecast_summary.json',        # 底率预测摘要(2026-09-16 新增): 晨报读取用
    'data/rv_shadow.json',                  # rv池内加权追踪(前一日1m已实现波动)前向证据 (9/14起): 与金矿格同节点终审
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
    try:
        branch = default_branch(REPO)
    except Exception as e:
        # 2026-09-12: 原无保护 → 启动即网络故障时脚本崩、status 文件不更新,
        # 只能靠次日体检间接发现。现落盘 ERROR 状态并正常返回。
        log(f'default_branch 获取失败: {e}')
        status_payload['message'] = f'ERROR default_branch: {e}'
        write_status(status_payload)
        return
    # 2026-09-19: 确保状态备份分支存在(不存在则从 main 创建, 幂等)
    try:
        ensure_branch(REPO, STATE_BACKUP_BRANCH, branch)
    except Exception as e:
        log(f'  WARN 备份分支 {STATE_BACKUP_BRANCH} 创建/校验失败: {e} (状态文件将暂仍推 main)')
    current = {}
    changed, removed = [], []
    ok_files, fail_files, ok_del, fail_del = set(), set(), set(), set()
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
            _br = branch_for(rel, branch)          # 2026-09-19: 运行时状态 → 备份分支
            status = upload_file(REPO, _br, rel, content, f'sync trading system: {rel}')
            ok_files.add(rel)
            log(f'  uploaded {rel} ({status}) [{i}/{len(changed)}] -> {_br}')
        except Exception as e:
            fail_files.add(rel)
            log(f'  FAIL upload {rel}: {e}')
    for rel in removed:
        sha = file_sha(REPO, branch_for(rel, branch), rel)      # 2026-09-19: 按分支查 sha
        if sha:
            try:
                status = delete_file(REPO, branch_for(rel, branch), rel, sha)   # 2026-09-19
                ok_del.add(rel)
                log(f'  deleted {rel} ({status})')
            except Exception as e:
                fail_del.add(rel)
                log(f'  FAIL delete {rel}: {e}')
    # 2026-09-12 修(静默漏同步): 原实现无条件把**本地**新 md5 写进 manifest, 上传失败的文件
    # 下次比对即"无变化" → 永不重试; 且 status 里 'failed' 恒为 0, 晨报/体检看不到失败。
    # 现: 失败文件在 manifest 中保留旧 md5(新文件则移除条目) → 下次仍判为 changed 并重试;
    # 失败删除同理保留条目; failed 计数如实上报。
    for rel in sorted(fail_files):
        if rel in old:
            current[rel] = old[rel]          # 保留旧值 → 下次仍 changed → 重试
        else:
            current.pop(rel, None)           # 新文件失败 → 不入册 → 下次仍为新增 → 重试
    for rel in sorted(fail_del):
        if rel in old:
            current[rel] = old[rel]          # 删除失败 → 保留 → 下次仍判 removed → 重试
    with open(MANIFEST + '.tmp', 'w', encoding='utf-8') as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
    os.replace(MANIFEST + '.tmp', MANIFEST)
    status_payload.update({
        'status': 'CHANGED' if changed else ('NO_CHANGE' if not removed else 'CHANGED'),
        'changed': len(changed),
        'uploaded': len(ok_files),
        'failed': len(fail_files) + len(fail_del),
        'failed_files': sorted(fail_files | fail_del)[:20],
        'removed': len(removed),
        'files': [rel for rel, _ in changed],
        'repo_mb': repo_mb,
        'message': f'changed={len(changed)} uploaded={len(ok_files)} failed={len(fail_files) + len(fail_del)} '
                   f'removed={len(removed)} repo={repo_mb}MB',
    })
    write_status(status_payload)
    log('=== trading system github sync done ===')

if __name__ == '__main__':
    main()
