// DSH 会话日志修复工具（救治 2026-09-14 瘦身事故造成的"历史加载失败"）
//
// 背景：dsh_session_slim.mjs 早期版本重排 seq 时只重映射了 sourceEventSeqs 与
// surfaceOp，漏掉了压缩事件的 data.shadowedRange / data.shadowedSeqs。于是
// 日志里出现"armed claim covers 81715-81715，紧邻 replace 却是 1813-1813"的自相
// 矛盾，DSH 的 token-meter 折叠按设计抛错（fail loud，宁可报错也不让 token 账目
// 漂移），表现为整扇窗 "history unavailable for session ... (internal)"。
//
// 本工具做两件事：
//   scan   —— 只读体检：逐个会话复现 DSH 的折叠契约，列出违规点（不改任何文件）
//   repair —— 用同目录的 .bak-*（瘦身前日志）重建"旧编号 -> 新编号"映射，只修正
//             压缩事件的那几个引用字段；内容事件一字不动，修完回读校验。
//
// 用法：
//   node dsh_session_repair.mjs scan
//   node dsh_session_repair.mjs repair --dry-run            # 只报告将要改什么
//   node dsh_session_repair.mjs repair                      # 修所有受损会话
//   node dsh_session_repair.mjs repair --session b921299e    # 只修指定会话
//   node dsh_session_repair.mjs repair --force              # 目标日志非正常结束/刚被写过时仍修
//
// 安全：① 本工具不会停止 DSH；② 只处理日志已正常结束(session/end-seed)或 10 分钟
// 未被改动的会话，否则要求 --force；③ 拒绝处理调用者自己的会话；④ 改写前另存
// pre-repair 备份，改写后重新解压回读、复验 seq 连续性与全部影子价格契约。

import { readdirSync, readFileSync, writeFileSync, renameSync, statSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { execFileSync } from 'node:child_process';
import { constants, zstdCompressSync } from 'node:zlib';

const SESSIONS_ROOT = '/home/myuser/.dsh/sessions';
const PROJCACHE = '/home/myuser/.dsh/storages/session_projcache.json';
const DROP = new Set(['assistant/chunk', 'text-chunks', 'reasoning-chunks', 'tool-call-chunks']);
const COMPACTIONS = new Set(['compaction/summary', 'compaction/prune']);

function parseArgs(argv) {
  const args = { cmd: argv[0], session: null, dryRun: false, force: false };
  for (let i = 1; i < argv.length; i++) {
    if (argv[i] === '--session') args.session = argv[++i];
    else if (argv[i] === '--dry-run') args.dryRun = true;
    else if (argv[i] === '--force') args.force = true;
  }
  return args;
}

function dshRunning() {
  try {
    execFileSync('bash', ['-c', 'ss -ltn | grep -q ":3080 "']);
    return true;
  } catch {
    return false;
  }
}

function* allSessionFiles() {
  for (const cwdDir of readdirSync(SESSIONS_ROOT)) {
    const cwdPath = join(SESSIONS_ROOT, cwdDir);
    if (!statSync(cwdPath).isDirectory()) continue;
    for (const sessDir of readdirSync(cwdPath)) {
      // 不限 session- 前缀：子代理会话目录没有前缀，体检时也要覆盖
      const dir = join(cwdPath, sessDir);
      if (!statSync(dir).isDirectory()) continue;
      const file = join(dir, 'session.jsonl.zstd');
      if (existsSync(file)) yield { file, cwdDir, sessDir };
    }
  }
}

function decompressLines(file) {
  const plaintext = execFileSync('zstd', ['-dc', file], { maxBuffer: 2048 * 1024 * 1024 });
  const lines = plaintext.toString('utf8').split('\n');
  if (lines.at(-1) === '') lines.pop();
  return lines;
}

const parseEvents = (lines) => lines.slice(1).filter(Boolean).map((l) => JSON.parse(l));

// 复现 dsh-token-meter 的 foldSurfaceProjection：压缩事件挂出影子价格区间，
// 紧邻的 replace 必须精确消费它，否则 DSH 打开会话时抛错。
function contractViolations(events) {
  const bad = [];
  let armed = null;
  for (const ev of events) {
    if (COMPACTIONS.has(ev.type)) {
      const sr = ev.data?.shadowedRange;
      if (!sr || sr.start === undefined || sr.end === undefined) {
        bad.push({ seq: ev.seq, kind: 'missing-shadowedRange' });
        armed = null;
      } else {
        armed = { start: sr.start, end: sr.end };
      }
      continue;
    }
    const op = ev.surfaceOp;
    if (!op || typeof op !== 'object') { armed = null; continue; }
    if (op.op === 'append') { armed = null; continue; }
    if (armed === null) continue;
    if (armed.start !== op.start || armed.end !== op.end) {
      bad.push({ seq: ev.seq, kind: 'claim-mismatch', replaced: `${op.start}-${op.end}`, claimed: `${armed.start}-${armed.end}` });
    }
    armed = null;
  }
  return bad;
}

/** 旧的展开编号 -> 新的密集编号（与 dsh_session_slim.mjs 的剥离规则一致）。 */
function buildSeqMap(oldEvents) {
  const map = new Map();
  let next = 0;
  for (const rec of oldEvents) {
    if (DROP.has(rec.type)) {
      if (rec.type === 'assistant/chunk' && rec.seq !== undefined) map.set(rec.seq, -1);
      continue;
    }
    if (rec.seq !== undefined) map.set(rec.seq, next++);
  }
  return map;
}

/** 备份日志必须能按剥离规则重建出当前日志的前缀，才允许用它的编号做映射。 */
function mappingFidelity(bakEvents, curEvents) {
  const rebuilt = bakEvents.filter((r) => !DROP.has(r.type));
  if (rebuilt.length > curEvents.length) return { ok: false, why: `备份重建出 ${rebuilt.length} 条 > 当前 ${curEvents.length} 条` };
  for (let i = 0; i < rebuilt.length; i++) {
    if (rebuilt[i].type !== curEvents[i].type) {
      return { ok: false, why: `第 ${i} 条类型不符（备份 ${rebuilt[i].type} vs 当前 ${curEvents[i].type}）` };
    }
  }
  return { ok: true, rebuilt: rebuilt.length, tail: curEvents.length - rebuilt.length };
}

function patchCompactions(curEvents, seqMap) {
  const changed = [];
  for (const rec of curEvents) {
    if (!COMPACTIONS.has(rec.type)) continue;
    const d = rec.data ?? {};
    const before = JSON.stringify([d.shadowedRange, d.shadowedSeqs?.length]);
    const sr = d.shadowedRange;
    if (sr && typeof sr === 'object') {
      for (const k of ['start', 'end']) {
        const m = seqMap.get(sr[k]);
        if (m === undefined || m === -1) throw new Error(`compaction shadowedRange.${k} 引用异常 seq ${sr[k]}`);
        sr[k] = m;
      }
    }
    if (Array.isArray(d.shadowedSeqs)) {
      const out = [];
      for (const s of d.shadowedSeqs) {
        const m = seqMap.get(s);
        if (m === -1) continue;
        if (m === undefined) throw new Error(`compaction shadowedSeqs 引用未知 seq ${s}`);
        out.push(m);
      }
      d.shadowedSeqs = out;
    }
    const after = JSON.stringify([d.shadowedRange, d.shadowedSeqs?.length]);
    if (before !== after) changed.push({ seq: rec.seq, before: JSON.parse(before), after: JSON.parse(after) });
  }
  return changed;
}

function writeLog(file, headerLine, events) {
  const opts = { params: { [constants.ZSTD_c_checksumFlag]: 1 } };
  const headerFrame = zstdCompressSync(Buffer.from(headerLine + '\n', 'utf8'), opts);
  const body = events.map((e) => JSON.stringify(e)).join('\n') + '\n';
  const eventFrame = zstdCompressSync(Buffer.from(body, 'utf8'), opts);
  writeFileSync(file, Buffer.concat([headerFrame, eventFrame]));
  execFileSync('chmod', ['600', file]);
}

function verifyFile(file, expectCount) {
  const lines = decompressLines(file);
  const events = parseEvents(lines);
  if (events.length !== expectCount) throw new Error(`事件数不符：${events.length} vs ${expectCount}`);
  let n = 0;
  for (const ev of events) {
    if (ev.seq !== n) throw new Error(`seq 断裂：期望 ${n}，实际 ${ev.seq}`);
    if (Array.isArray(ev.sourceEventSeqs)) {
      for (const s of ev.sourceEventSeqs) if (s >= n) throw new Error(`seq ${n} 引用未更早的 ${s}`);
    }
    if (COMPACTIONS.has(ev.type)) {
      const sr = ev.data?.shadowedRange;
      const ss = ev.data?.shadowedSeqs;
      if (Array.isArray(ss) && ss.length > 0 && (ss[0] !== sr.start || ss.at(-1) !== sr.end)) {
        throw new Error(`seq ${n} 的 shadowedSeqs 首尾与 shadowedRange 不符`);
      }
      for (const s of ss ?? []) if (typeof s !== 'number' || s >= n) throw new Error(`seq ${n} 的 shadowedSeqs 含越界值 ${s}`);
    }
    n++;
  }
  const bad = contractViolations(events);
  if (bad.length > 0) throw new Error(`改写后仍存在 ${bad.length} 处影子价格契约违规`);
  return events.length;
}

function backupsFor(file) {
  const dir = dirname(file);
  const base = 'session.jsonl.zstd.bak-';
  return readdirSync(dir)
    .filter((n) => n.startsWith(base))
    .map((n) => ({ name: n, path: join(dir, n), mtime: statSync(join(dir, n)).mtimeMs }))
    .sort((a, b) => b.mtime - a.mtime);
}

function repairOne({ file, sessDir }, opts) {
  const lines = decompressLines(file);
  const headerLine = lines[0];
  const cur = parseEvents(lines);
  const violations = contractViolations(cur);
  if (violations.length === 0) return { sessDir, status: 'healthy' };

  const st = statSync(file);
  const last = cur.at(-1);
  const freshlyWritten = Date.now() - st.mtimeMs < 10 * 60 * 1000;
  const ended = last?.type === 'session/end-seed';
  if (!opts.force && !ended && freshlyWritten) {
    return { sessDir, status: 'skipped', why: '日志未正常结束且 10 分钟内被写过（可能正在被写入），需 --force' };
  }

  let map = null;
  let used = null;
  const tried = [];
  for (const bak of backupsFor(file)) {
    let bakEvents;
    try {
      bakEvents = parseEvents(decompressLines(bak.path));
    } catch (e) {
      tried.push(`${bak.name}: 解压失败 ${e.message}`);
      continue;
    }
    const fit = mappingFidelity(bakEvents, cur);
    if (!fit.ok) { tried.push(`${bak.name}: 不可用（${fit.why}）`); continue; }
    map = buildSeqMap(bakEvents);
    used = { ...bak, fidelity: fit };
    break;
  }
  if (map === null) return { sessDir, status: 'no-usable-backup', violationCount: violations.length, tried };

  const changed = patchCompactions(cur, map);
  for (const v of contractViolations(cur)) throw new Error(`干跑修补后 seq ${v.seq} 仍有契约违规（${v.kind}）`);

  const report = { sessDir, status: opts.dryRun ? 'dry-run-ok' : 'repaired', violationCount: violations.length, backup: used.name, rebuilt: used.fidelity.rebuilt, tail: used.fidelity.tail, changed };

  if (opts.dryRun) return report;

  const tmp = file + '.repairnew';
  writeLog(tmp, headerLine, cur);
  verifyFile(tmp, cur.length);
  const stamp = new Date().toLocaleString('sv-SE').replace(/[-: ]/g, '').slice(0, 14); // 本地时间 YYYYMMDDHHMMSS
  renameSync(file, `${file}.pre-repair-${stamp}`);
  renameSync(tmp, file);
  verifyFile(file, cur.length);
  // 投影缓存里若有旧条目，删掉让它重建（避免用瘦身前编号算出的账目残留）
  try {
    const cache = JSON.parse(readFileSync(PROJCACHE, 'utf8'));
    if (cache?.tables?.sessions && sessDir in cache.tables.sessions) {
      delete cache.tables.sessions[sessDir];
      writeFileSync(PROJCACHE, JSON.stringify(cache));
      report.projcacheCleared = true;
    }
  } catch { /* 缓存缺失不影响正确性 */ }
  return report;
}

const args = parseArgs(process.argv.slice(2));

if (args.cmd === 'scan') {
  let bad = 0, total = 0;
  for (const s of allSessionFiles()) {
    total++;
    let cur;
    try {
      cur = parseEvents(decompressLines(s.file));
    } catch (e) {
      console.log(`  ${s.sessDir}  [解压/解析失败: ${e.message}]`);
      bad++;
      continue;
    }
    const v = contractViolations(cur);
    if (v.length === 0) continue;
    bad++;
    const first = v[0];
    console.log(`  ✗ ${s.sessDir}  事件 ${cur.length}  违规 ${v.length} 处  首个: seq ${first.seq}`
      + (first.kind === 'claim-mismatch' ? ` replace ${first.replaced} vs claim ${first.claimed}` : ` ${first.kind}`));
  }
  console.log(`共 ${total} 个会话，受损 ${bad} 个。`);
  process.exit(bad > 0 ? 1 : 0);
} else if (args.cmd === 'repair') {
  const own = process.env.DSH_SESSION_JSONL;
  let done = 0;
  for (const s of allSessionFiles()) {
    if (args.session && !s.sessDir.includes(args.session)) continue;
    if (own && s.file === own) { console.log(`  ${s.sessDir}: 跳过（调用者自己的会话）`); continue; }
    const r = repairOne(s, args);
    const icon = { healthy: '·', 'dry-run-ok': '○', repaired: '✓', skipped: '!', 'no-usable-backup': '✗' }[r.status] ?? '?';
    if (r.status === 'healthy') continue;
    console.log(`  ${icon} ${r.sessDir}  [${r.status}]`
      + (r.violationCount ? `  违规 ${r.violationCount} 处` : '')
      + (r.backup ? `  映射源 ${r.backup}（校验通过：重建 ${r.rebuilt} 条 + 尾部 ${r.tail} 条）` : '')
      + (r.why ? `  ${r.why}` : ''));
    if (r.changed) for (const c of r.changed) console.log(`      seq ${c.seq}: ${JSON.stringify(c.before)} -> ${JSON.stringify(c.after)}`);
    if (r.tried) for (const t of r.tried) console.log(`      ${t}`);
    if (r.projcacheCleared) console.log('      已清理该会话的投影缓存条目');
    if (r.status === 'repaired' || r.status === 'dry-run-ok') done++;
  }
  console.log(args.dryRun ? `干跑完成：${done} 个会话可修（未改动任何文件）。` : `修复完成：${done} 个会话。`);
} else {
  console.error('用法: node dsh_session_repair.mjs scan | repair [--session <id>] [--dry-run] [--force]');
  process.exit(1);
}
