// DSH 会话日志瘦身工具（预防"历史加载失败"）
//
// 背景：DSH 打开会话时服务端要全量读取日志事件，客户端对 session.history
// 有 30 秒硬超时（AbortSignal.timeout）。流式碎片（assistant/chunk 及其打包行
// text-chunks/reasoning-chunks/tool-call-chunks）通常占事件总量 90% 以上，
// 但完整内容都在 user/message、assistant/message、tool/call、tool/result 里。
// 剥离碎片 + 重排 seq + 重映射序号引用，可在不丢任何历史内容的前提下大幅瘦身。
//
// 用法：
//   node dsh_session_slim.mjs list                      # 体检：列出所有会话的事件规模
//   node dsh_session_slim.mjs check                     # 静默体检：有超阈值会话则退出码1（供 guardian 判断）
//   node dsh_session_slim.mjs slim                      # 瘦身所有超过阈值的会话（默认 10000 事件）
//   node dsh_session_slim.mjs slim --threshold 5000     # 自定义阈值
//   node dsh_session_slim.mjs slim --session <session-id>  # 只处理指定会话
//   node dsh_session_slim.mjs slim --force              # DSH 运行中也强制处理（有风险，慎用）
//
// 安全：每次改写前先备份为 session.jsonl.zstd.bak-<日期>（同日不覆盖，避免毁掉
// 还原依据）；改写后回读校验 seq 连续性、序号引用与压缩事件的影子价格契约。
// 检测到 DSH 正在监听端口时默认拒绝执行。
//
// ⚠️ 2026-09-14 事故与修复（勿删）：本脚本早期版本重排 seq 时只重映射了
// sourceEventSeqs 与 surfaceOp，漏了压缩事件的 data.shadowedRange / shadowedSeqs
// （它们仍指向瘦身前的"展开编号"，最大到 811911）。结果 token-meter 折叠时发现
// armed claim 与紧邻 replace 区间不符，抛 "token surface: replace at seq N ... has
// no adjacent shadow price"，表现为整扇窗 "history unavailable for session ...
// (internal)"（4 个会话中招，均为凌晨被本脚本瘦身的）。修复：在第二遍重编号里一并
// 重映射这两个字段，并在回读校验中加入"压缩事件区间必须等于紧邻 replace 区间"。
// 若再出现同类症状：`node dsh_session_repair.mjs scan` 体检，
// `node dsh_session_repair.mjs repair` 用 .bak 重建映射修回。

import { readdirSync, readFileSync, writeFileSync, renameSync, statSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';
import { constants, zstdCompressSync } from 'node:zlib';

const SESSIONS_ROOT = '/home/myuser/.dsh/sessions';
const DROP = new Set(['assistant/chunk', 'text-chunks', 'reasoning-chunks', 'tool-call-chunks']);

function parseArgs(argv) {
  const args = { cmd: argv[0], threshold: 10000, session: null, force: false };
  for (let i = 1; i < argv.length; i++) {
    if (argv[i] === '--threshold') args.threshold = Number(argv[++i]);
    else if (argv[i] === '--session') args.session = argv[++i];
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
      if (!sessDir.startsWith('session-')) continue;
      const file = join(cwdPath, sessDir, 'session.jsonl.zstd');
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

// 统计一个日志文件的事件构成（list 与 slim 共用）。
// 注意：打包chunk行加载时会展开成多条事件，真实加载成本=展开后事件总数(total)。
function analyze(lines) {
  let rows = 0, chunkRows = 0, expandedChunks = 0;
  for (let i = 1; i < lines.length; i++) {
    const line = lines[i];
    if (!line) continue;
    rows++;
    const rec = JSON.parse(line);
    if (rec.type === 'text-chunks' || rec.type === 'reasoning-chunks' || rec.type === 'tool-call-chunks') {
      chunkRows++;
      expandedChunks += (rec.type === 'tool-call-chunks' ? rec.data.args : rec.data.texts).length;
    } else if (rec.type === 'assistant/chunk') {
      chunkRows++;
      expandedChunks += 1;
    }
  }
  const kept = rows - chunkRows;
  return { rows, kept, dropped: expandedChunks, total: kept + expandedChunks };
}

// 瘦身后 seq 变了，删掉该会话的投影缓存条目（不删也能自愈，但首次加载会走慢路径）
function cleanProjcache(sessDir) {
  const cacheFile = '/home/myuser/.dsh/storages/session_projcache.json';
  try {
    const data = JSON.parse(readFileSync(cacheFile, 'utf8'));
    const sessions = data?.tables?.sessions;
    if (sessions && sessDir in sessions) {
      delete sessions[sessDir];
      writeFileSync(cacheFile, JSON.stringify(data));
      return true;
    }
  } catch {
    // 缓存文件不存在或解析失败都不影响瘦身的正确性
  }
  return false;
}

// 重写：剥离碎片、重排 seq、重映射 sourceEventSeqs 与 surfaceOp 引用
function rewrite(file) {
  const lines = decompressLines(file);
  const headerLine = lines[0];
  const stats = analyze(lines);
  if (stats.dropped === 0) return { ...stats, skipped: true };

  // 第一遍：确定保留集与 旧seq -> 新seq 映射
  const seqMap = new Map();
  const parsed = [];
  let nextNew = 0;
  for (let i = 1; i < lines.length; i++) {
    const line = lines[i];
    if (!line) continue;
    const rec = JSON.parse(line);
    if (rec.type === 'text-chunks' || rec.type === 'reasoning-chunks' || rec.type === 'tool-call-chunks') {
      const len = (rec.type === 'tool-call-chunks' ? rec.data.args : rec.data.texts).length;
      for (let k = 0; k < len; k++) seqMap.set(rec.seq0 + k, -1);
      parsed.push({ rec, drop: true });
      continue;
    }
    if (DROP.has(rec.type)) {
      seqMap.set(rec.seq, -1);
      parsed.push({ rec, drop: true });
      continue;
    }
    seqMap.set(rec.seq, nextNew++);
    parsed.push({ rec, drop: false });
  }

  // 第二遍：重编号 + 重映射引用
  const kept = [];
  for (const { rec, drop } of parsed) {
    if (drop) continue;
    rec.seq = seqMap.get(rec.seq);
    if (Array.isArray(rec.sourceEventSeqs)) {
      const remapped = [];
      for (const s of rec.sourceEventSeqs) {
        const m = seqMap.get(s);
        if (m === -1) continue; // 引用的是被剥离的碎片，丢弃该引用
        if (m === undefined) throw new Error(`sourceEventSeqs 引用未知 seq ${s}（事件类型 ${rec.type}）`);
        remapped.push(m);
      }
      if (remapped.length === 0) delete rec.sourceEventSeqs;
      else rec.sourceEventSeqs = remapped;
    }
    if (rec.surfaceOp && typeof rec.surfaceOp === 'object') {
      for (const k of ['start', 'end']) {
        const m = seqMap.get(rec.surfaceOp[k]);
        if (m === undefined || m === -1) throw new Error(`surfaceOp.${k} 引用异常 seq ${rec.surfaceOp[k]}`);
        rec.surfaceOp[k] = m;
      }
    }
    // 压缩事件的"影子价格"引用（shadowedRange/shadowedSeqs）同样指向旧编号。
    // 漏改的后果：token-meter 折叠时会发现 armed claim 与紧邻 replace 的区间不符，
    // 抛 "replace ... has no adjacent shadow price"，整扇窗的历史全部加载失败
    // （2026-09-14 事故：4 个会话中招）。这里必须与 seq/surfaceOp 一起重映射。
    if (rec.type === 'compaction/summary' || rec.type === 'compaction/prune') {
      const d = rec.data ?? {};
      if (d.shadowedRange && typeof d.shadowedRange === 'object') {
        for (const k of ['start', 'end']) {
          const m = seqMap.get(d.shadowedRange[k]);
          if (m === undefined || m === -1) throw new Error(`compaction shadowedRange.${k} 引用异常 seq ${d.shadowedRange[k]}`);
          d.shadowedRange[k] = m;
        }
      }
      if (Array.isArray(d.shadowedSeqs)) {
        const remappedSeqs = [];
        for (const s of d.shadowedSeqs) {
          const m = seqMap.get(s);
          if (m === -1) continue; // 被剥离的碎片从不是表面节点，正常不会走到这里
          if (m === undefined) throw new Error(`compaction shadowedSeqs 引用未知 seq ${s}`);
          remappedSeqs.push(m);
        }
        d.shadowedSeqs = remappedSeqs;
      }
    }
    kept.push(rec);
  }

  // 编码：帧1=header 行，帧2=事件行，均带 zstd checksum（与官方写入器一致）
  const opts = { params: { [constants.ZSTD_c_checksumFlag]: 1 } };
  const headerFrame = zstdCompressSync(Buffer.from(headerLine + '\n', 'utf8'), opts);
  const body = kept.map((e) => JSON.stringify(e)).join('\n') + '\n';
  const eventFrame = zstdCompressSync(Buffer.from(body, 'utf8'), opts);
  const tmp = file + '.new';
  writeFileSync(tmp, Buffer.concat([headerFrame, eventFrame]));

  // 回读校验：seq 连续 + 引用全部指向更早事件 + 压缩事件的影子价格契约自洽
  const plain2 = execFileSync('zstd', ['-dc', tmp], { maxBuffer: 2048 * 1024 * 1024 }).toString('utf8');
  const lines2 = plain2.split('\n');
  if (lines2.at(-1) === '') lines2.pop();
  if (lines2[0] !== headerLine) throw new Error('header 行不一致');
  let n = 0;
  let armed = null; // 压缩事件挂出的影子价格区间，必须被紧邻的 replace 精确消费
  for (let i = 1; i < lines2.length; i++) {
    if (!lines2[i]) continue;
    const ev = JSON.parse(lines2[i]);
    if (ev.seq !== n) throw new Error(`第 ${i} 行 seq 断裂：期望 ${n}，实际 ${ev.seq}`);
    if (Array.isArray(ev.sourceEventSeqs)) {
      for (const s of ev.sourceEventSeqs) if (s >= n) throw new Error(`seq ${n} 引用未更早的 ${s}`);
    }
    if (ev.type === 'compaction/summary' || ev.type === 'compaction/prune') {
      const sr = ev.data?.shadowedRange;
      const shadowedSeqs = ev.data?.shadowedSeqs;
      if (!sr || sr.start === undefined || sr.end === undefined) throw new Error(`第 ${i} 行压缩事件缺少 shadowedRange`);
      if (Array.isArray(shadowedSeqs) && shadowedSeqs.length > 0
          && (shadowedSeqs[0] !== sr.start || shadowedSeqs.at(-1) !== sr.end)) {
        throw new Error(`第 ${i} 行 compaction shadowedSeqs 首尾与 shadowedRange 不符`);
      }
      armed = { start: sr.start, end: sr.end };
    } else if (ev.surfaceOp && typeof ev.surfaceOp === 'object') {
      if (ev.surfaceOp.op === 'append') armed = null;
      else if (armed !== null) {
        if (armed.start !== ev.surfaceOp.start || armed.end !== ev.surfaceOp.end) {
          throw new Error(`第 ${i} 行 replace(${ev.surfaceOp.start}-${ev.surfaceOp.end}) 与 shadowedRange(${armed.start}-${armed.end}) 不符`
            + ' —— 会在打开会话时报 "has no adjacent shadow price"');
        }
        armed = null;
      }
    } else {
      armed = null;
    }
    n++;
  }
  if (n !== kept.length) throw new Error(`事件数不一致：${n} vs ${kept.length}`);

  const today = new Date().toLocaleDateString('sv-SE').replaceAll('-', '');
  // 备份不覆盖：同日二次瘦身若覆盖同名 .bak，会毁掉"旧编号 -> 新编号"的唯一还原依据
  let backup = `${file}.bak-${today}`;
  for (let k = 2; existsSync(backup); k++) backup = `${file}.bak-${today}-${k}`;
  renameSync(file, backup);
  renameSync(tmp, file);
  execFileSync('chmod', ['600', file]);
  return { ...stats, skipped: false, kept: n, backup };
}

const args = parseArgs(process.argv.slice(2));

if (args.cmd === 'list') {
  console.log('会话体检报告：');
  for (const { file, cwdDir, sessDir } of allSessionFiles()) {
    const size = statSync(file).size;
    let report;
    try {
      report = analyze(decompressLines(file));
    } catch (e) {
      console.log(`  ${sessDir}  [解压失败: ${e.message}]`);
      continue;
    }
    const flag = report.total > args.threshold ? '  ← 超阈值，建议 slim' : '';
    console.log(`  ${sessDir}  展开事件 ${report.total}（完整记录 ${report.kept} + 碎片 ${report.dropped}） 压缩 ${(size / 1024 / 1024).toFixed(1)}MB${flag}`);
  }
} else if (args.cmd === 'slim') {
  if (dshRunning() && !args.force) {
    console.error('错误：DSH web 正在运行（端口 3080 监听中）。请先停止 DSH 再执行，或加 --force（有竞态风险）。');
    process.exit(1);
  }
  let processed = 0;
  for (const { file, sessDir } of allSessionFiles()) {
    if (args.session && !sessDir.includes(args.session)) continue;
    const lines = decompressLines(file);
    const pre = analyze(lines);
    if (!args.session && pre.total <= args.threshold) continue;
    if (pre.dropped === 0) { console.log(`${sessDir}: 无碎片，跳过`); continue; }
    console.log(`${sessDir}: 展开事件 ${pre.total}，碎片 ${pre.dropped}，开始瘦身...`);
    const r = rewrite(file);
    cleanProjcache(sessDir);
    console.log(`${sessDir}: 完成 → 保留 ${r.kept} 个事件，备份已存为 *.bak-*`);
    processed++;
  }
  console.log(processed === 0 ? '没有需要瘦身的会话。' : `共处理 ${processed} 个会话。`);
} else if (args.cmd === 'check') {
  // 供 guardian 使用：退出码 0 = 无超阈值会话，1 = 有需要瘦身的会话
  const over = [];
  for (const { file, sessDir } of allSessionFiles()) {
    const pre = analyze(decompressLines(file));
    if (pre.total > args.threshold) over.push(`${sessDir}(${pre.total}事件)`);
  }
  if (over.length) {
    console.log(over.join(' '));
    process.exit(1);
  }
} else {
  console.error('用法: node dsh_session_slim.mjs list | check | slim [--threshold N] [--session <id>] [--force]');
  process.exit(1);
}
