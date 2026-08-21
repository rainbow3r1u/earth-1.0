#!/usr/bin/env python3
"""
每周扫描本地 Obsidian / 系统文档，MD5 有变化就自动同步到 GitHub。

同步目标：
  - Obsidian 库        -> github.com/rainbow3r1u/rainbow-vault
  - 系统维护/主文档     -> github.com/rainbow3r1u/trading-system-docs-backup
  - 同时重新打包 tar.gz 备份上传到 backup 仓库
用法：
  python3 obsidian_github_sync.py --dry-run
  python3 obsidian_github_sync.py
"""
import os, sys, json, base64, hashlib, tarfile, io, urllib.request, urllib.error, urllib.parse, yaml, time, datetime

HOME = os.path.expanduser('~')
OBS_ROOT = os.path.join(HOME, 'Sync', 'rainbow')
DOCS = [
    os.path.join(HOME, '量化交易系统运维交接文档.md'),
    os.path.join(HOME, '3.96SHARPE_repo', '服务器内容查看与维护.md'),
    os.path.join(HOME, '3.96SHARPE_repo', 'README.md'),
    os.path.join(HOME, '3.96SHARPE_repo', 'USAGE.md'),
    os.path.join(HOME, 'websocket_new', 'AGENTS.md'),
    os.path.join(HOME, 'websocket_new', 'DEPLOY.md'),
    os.path.join(HOME, 'websocket_new', 'SYSTEM_OVERVIEW.md'),
    os.path.join(HOME, 'websocket_new', 'EXTERNAL_FILES.md'),
]
OBSIDIAN_REPO = 'rainbow3r1u/rainbow-vault'
BACKUP_REPO = 'rainbow3r1u/trading-system-docs-backup'
STATE_DIR = os.path.join(HOME, '.cache', 'obsidian_github_sync')
OBS_MANIFEST = os.path.join(STATE_DIR, 'obsidian_manifest.json')
DOC_MANIFEST = os.path.join(STATE_DIR, 'docs_manifest.json')
ARCHIVE_NAME = f'交易系统文档备份_{datetime.date.today().strftime("%Y%m%d")}.tar.gz'
ARCHIVE_LOCAL = os.path.join(HOME, ARCHIVE_NAME)
LOG = os.path.join(HOME, 'logs', 'obsidian_github_sync.log')

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

def api(method, url, body=None, ok404=False):
    headers = {
        'Authorization': 'Bearer ' + TOKEN,
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'obsidian-sync',
        'Content-Type': 'application/json',
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if ok404 and e.code == 404:
            return e.code, None
        raise

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
    status, info = api('PUT', url, body)
    return status

def delete_file(repo, branch, path, sha):
    url = f'https://api.github.com/repos/{repo}/contents/{urllib.parse.quote(path, safe="/")}'
    body = {'message': f'delete {path}', 'sha': sha, 'branch': branch}
    status, _ = api('DELETE', url, body)
    return status

def md5_file(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()

def walk_obsidian():
    out = {}
    for dp, dns, fns in os.walk(OBS_ROOT):
        dns[:] = [d for d in dns if d not in ('.git', '.obsidian')]
        for fn in fns:
            p = os.path.join(dp, fn)
            rel = os.path.relpath(p, OBS_ROOT)
            out[rel] = p
    return out

def sync_files(repo, root, files, manifest_path, label, dry_run):
    os.makedirs(STATE_DIR, exist_ok=True)
    old = {}
    if os.path.exists(manifest_path):
        try:
            old = json.load(open(manifest_path, encoding='utf-8'))
        except Exception:
            old = {}
    branch = default_branch(repo)
    current = {}
    changed = []
    removed = []
    for rel, path in files.items():
        digest = md5_file(path)
        current[rel] = digest
        if rel not in old or old.get(rel) != digest:
            changed.append((rel, path))
    for rel in old:
        if rel not in current:
            removed.append(rel)
    log(f'[{label}] total={len(current)} changed={len(changed)} removed={len(removed)}')
    if dry_run:
        for rel, _ in changed[:20]:
            log(f'  DRY changed {rel}')
        for rel in removed[:20]:
            log(f'  DRY removed {rel}')
        return
    for i, (rel, path) in enumerate(changed, 1):
        try:
            with open(path, 'rb') as f:
                content = f.read()
            status = upload_file(repo, branch, rel, content, f'sync {label}: {rel}')
            log(f'  uploaded {rel} ({status}) [{i}/{len(changed)}]')
        except Exception as e:
            log(f'  FAIL upload {rel}: {e}')
    for rel in removed:
        sha = file_sha(repo, branch, rel)
        if sha:
            try:
                status = delete_file(repo, branch, rel, sha)
                log(f'  deleted {rel} ({status})')
            except Exception as e:
                log(f'  FAIL delete {rel}: {e}')
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
    log(f'[{label}] manifest updated')

def make_archive():
    # 仅仅在本地生成/更新 tar.gz，供上传到 backup 仓库。
    # 使用与手动打包相同的核心路径。
    import tarfile
    out_path = ARCHIVE_LOCAL
    with tarfile.open(out_path, 'w:gz') as tar:
        def add(path, arcname):
            if os.path.isdir(path):
                for dp, dns, fns in os.walk(path):
                    dns[:] = [d for d in dns if d not in ('.git', '__pycache__', 'node_modules')]
                    for fn in fns:
                        fp = os.path.join(dp, fn)
                        rel = os.path.relpath(fp, HOME)
                        tar.add(fp, arcname=os.path.join('交易系统文档备份_20260822', rel))
            elif os.path.exists(path):
                rel = os.path.relpath(path, HOME)
                tar.add(path, arcname=os.path.join('交易系统文档备份_20260822', rel))
        add(OBS_ROOT, 'OBS')
        for p in DOCS:
            add(p, 'DOC')
        add(os.path.join(HOME, 'websocket_new', 'docs'), 'DOCS')
        add(os.path.join(HOME, 'websocket_new', 'archive'), 'ARCHIVE')
        add(os.path.join(HOME, 'websocket_new', 'docs', 'SYSTEM_CLEAN_MAP.md'), 'MAP')
    return out_path

def main():
    dry_run = '--dry-run' in sys.argv
    log('=== obsidian github sync start ===')
    obs = walk_obsidian()
    sync_files(OBSIDIAN_REPO, OBS_ROOT, obs, OBS_MANIFEST, 'obsidian', dry_run)

    docs = {os.path.relpath(p, HOME): p for p in DOCS if os.path.exists(p)}
    # 额外包含 repository 关键文档
    extra_docs = {
        'websocket_new/docs/SYSTEM_CLEAN_MAP.md': os.path.join(HOME, 'websocket_new', 'docs', 'SYSTEM_CLEAN_MAP.md'),
    }
    for rel, p in extra_docs.items():
        if os.path.exists(p):
            docs[rel] = p
    sync_files(BACKUP_REPO, HOME, docs, DOC_MANIFEST, 'docs', dry_run)

    # 重新打包 backup 并上传
    if not dry_run:
        archive = make_archive()
        archive_bytes = open(archive, 'rb').read()
        branch = default_branch(BACKUP_REPO)
        status = upload_file(BACKUP_REPO, branch, ARCHIVE_NAME, archive_bytes, f'sync docs backup {ARCHIVE_NAME}')
        log(f'archive uploaded {ARCHIVE_NAME} status={status}')
    log('=== obsidian github sync done ===')

if __name__ == '__main__':
    main()
