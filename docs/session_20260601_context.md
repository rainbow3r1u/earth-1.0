# Session Context — Kronos 维度筛选实验

> 会话时间: 2026-06-01 ~ 2026-06-02
> GPU 服务器: 175.155.64.171:24220 (RTX 3080 20GB, 12核 Xeon, 31GB)

---

## 1. GPU 服务器初始化

### 连接方式
```bash
ssh -p 24220 root@175.155.64.171
```

### 数据同步 (本地 → COS → GPU)
- COS Bucket: `lhsj-1h-1314017643`, Region: `ap-seoul`
- K线缓存: 529币日线 (32MB)
- OI缓存: 532币日级 (415KB)
- Kronos 模型: 406MB (`kronos_finetune/kronos_pretrained/`)
- Kronos embeddings: 2048点 × 832维 (35MB, 从 `kronos_features_cache.json` 转换)
- 情绪/链上/ETF/稳定币等数据目录已通过 symlink 桥接

### 路径修复
GPU 上代码硬编码 `/home/myuser/`，通过 25+ 条 symlink 桥接到 `/root/` 路径。

### Python 环境
- `torch 2.7.1+cu118`, `xgboost 3.2`, `numpy`, `pandas`, `transformers 5.9`
- `einops`, `cos-python-sdk-v5`

---

## 2. Kronos 832 维爬坡实验

### 实验目标
从 Kronos 832 维 embedding 中找出真正对回测有贡献的维度。

### 重要性排名
来源: `~/.local/share/auto_trade/kronos_importance_log.json` (XGBoost feature importance 聚合)

Top8 维度: `kronos_emb_207, 806, 222, 774, 168, 49, 66, 193`

### Phase 1: 粗扫 (30天, step=10, 0→830)
**脚本**: `gpu_final_sweep.py` — 12核并行样本 + GPU XGBoost
**结果**: 94轮, Top8 最佳 (PnL=-194 vs Top0=-260)

结论: **832维中只有前8维在30天回测中有正贡献**，其余824维全是噪声。

### Phase 2: 365天验证
**脚本**: `final_run.py` — 12核并行样本 (78s) + GPU XGBoost 365轮 walk-forward
**对比**:
| 配置 | Kronos维度 |
|------|-----------|
| BASELINE_83 | 纯基线83维 (0 Kronos) |
| BASELINE_plus_Top8 | 基线83维 + Top8 Kronos |

**状态**: 运行中 (80% 完成)

---

## 3. 关键发现

1. **Kronos 不是独立盈利的特征** — 单独用 Kronos (无基线) 全负收益
2. **Kronos 的价值在风控** — 之前 FULL 模型 (915维) Sharpe 10.87, 回撤降 27%
3. **832维中真正有用的维度极少** — 30天回测只有8维起正作用
4. **重要性排名 ≠ 回测贡献** — 排名靠前的维度回测也不一定好

---

## 4. Superpowers 技能安装

14 个技能已安装到 `~/.reasonix/skills/`:
- `writing-plans`, `subagent-driven-development`, `test-driven-development`
- `systematic-debugging`, `verification-before-completion`, `brainstorming`
- `dispatching-parallel-agents`, `executing-plans`, `requesting-code-review`
- `receiving-code-review`, `using-git-worktrees`, `finishing-a-development-branch`
- `using-superpowers`, `writing-skills`

调用: `/skill <name>` 或 `run_skill({ name: "..." })`

---

## 5. CodeGraph MCP

已激活 (v0.9.8)，索引 99 文件。Daemon 持久运行 (PID 1916082)。
配置: `~/.reasonix/config.json` → `mcpServers.codegraph`

---

## 6. 重要文件路径

### 本地服务器
| 文件 | 路径 |
|------|------|
| 系统文档 | `~/websocket_new/SYSTEM_OVERVIEW.md` |
| 重要性日志 | `~/.local/share/auto_trade/kronos_importance_log.json` |
| 爬坡脚本 | `~/websocket_new/kronos_sweep.py` |
| GPU MCP | `~/websocket_new/gpu_mcp.py` |

### GPU 服务器
| 文件 | 路径 |
|------|------|
| 粗扫脚本 | `/root/reasonix-projects/websocket_new/gpu_final_sweep.py` |
| 365验证 | `/root/reasonix-projects/websocket_new/final_run.py` |
| 粗扫日志 | `/tmp/final_sweep.log` (94轮完整结果) |
| 365验证日志 | `/tmp/final.log` |
| 验证结果 | `/root/reasonix-projects/websocket_new/data/verify*_365d.json` |
| 最终结果 | `/root/reasonix-projects/websocket_new/data/final_*_365d.json` |
| 爬坡结果 | `/root/reasonix-projects/websocket_new/data/kronos_sweep_top*_30d.json` |

---

## 7. 常用命令

```bash
# GPU 状态
ssh -p 24220 root@175.155.64.171 'nvidia-smi'

# 查看回测进度
ssh -p 24220 root@175.155.64.171 'tail -10 /tmp/final.log'

# 查看进程
ssh -p 24220 root@175.155.64.171 'ps aux | grep python3 | grep -v grep'

# Python 栈追踪
ssh -p 24220 root@175.155.64.171 'py-spy dump --pid <PID>'

# 同步数据: 本地 → COS → GPU
# 1) python3 cos_upload.py  (本地)
# 2) python3 cos_download.py (GPU)
```

---

## 8. 踩坑记录

### 坑1: GPU 服务器外网不通
- **现象**: `fetch_oi()` 调用 Binance API 全部 SYN-SENT，TCP 443 被墙
- **解决**: 所有数据走 COS 中转，代码中 monkey-patch `requests.get` 拦截 Binance URL，`fetch_oi` 强制读本地缓存

### 坑2: OI 缓存过期触发 API
- **现象**: OI 缓存 >2 天旧，`fetch_oi` 自动调 API，全卡死
- **解决**: Override `fetch_oi` 跳过新鲜度检查，强制用缓存

### 坑3: `kronos_embeddings.json` 不存在
- **现象**: 代码优先读预计算 embedding，缺文件回退到 Kronos 模型实时推理 (需 `torch` + `transformers`)
- **解决**: 将 `kronos_features_cache.json` (2048点×832维) 转换成 embeddings 格式

### 坑4: 单线程样本构建 - 12核围观
- **现象**: 原版 `dual_backtest` 单线程循环 529 币×2000 天，CPU 100% 但只用 1 核，GPU 闲置
- **解决**: 用 `multiprocessing.Pool(12)` 并行 `_build_coin_samples`，78 秒完成 (单核需 10+ 分钟)

### 坑5: 自写回测缺少止损
- **现象**: 自定义回测 PnL -6502、DD 6607%，远超生产结果
- **根因**: 没实现 -10% 止损，单笔巨亏拖垮整体
- **解决**: 在 walk-forward 循环中加入日级止损检查 (`dr=='L' and low<=sl_price`)

### 坑6: 止损公式符号反了
- **现象**: STOP_LOSS=-10，`entry*(1-STOP_LOSS/100)=1.1` (做多止损价高于入场价)
- **根因**: 做多应是 `entry*(1+STOP_LOSS/100)=0.9`，做空 `entry*(1-STOP_LOSS/100)=1.1`
- **解决**: 修正为 `entry*(1+STOP_LOSS/100) if LONG else entry*(1-STOP_LOSS/100)`

### 坑7: Python f-string 不支持反斜杠
- **现象**: `f"...{r[\"key\"]}..."` → SyntaxError
- **解决**: 先提取变量 `v=r['key']` 再 `f"...{v}..."`，或用 `%` 格式化

### 坑8: `def p(ts,zi=zi): f=_om(ts); ks=...` 缩进错误
- **现象**: Python 不允许 `def` 和函数体在同一行 (3.10)
- **解决**: 分两行写

### 坑9: SSH 多行命令引号地狱
- **现象**: `ssh ... 'python3 -c "..."'` 嵌套引号频繁报错
- **解决**: 写脚本文件 → `scp` 上传 → `ssh ... python3 script.py`

### 坑10: 365 天 `numpy.percentile` 指数级变慢
- **现象**: walk-forward 越往后训练集越大，`percentile` 从 2 秒膨胀到 20+ 秒/迭代
- **根因**: `percentile` 需要对整个训练集排序，O(n log n)，n 随迭代线性增长
- **解决**: 未解决——365 天跑不完。建议改用 `np.nanpercentile` 或采样估计

### 坑11: 自定义回测无声崩溃
- **现象**: 进程 exit 0 但无输出无结果文件，无 traceback
- **根因**: walk-forward 循环中异常被 `except: continue` 吞掉
- **解决**: 加 `traceback.print_exc()` 或 `py-spy dump` 定位

### 坑12: 数据路径硬编码 `/home/myuser/`
- **现象**: GPU 服务器用户是 `root`，所有路径不存在
- **解决**: 25+ 条 `ln -sf` 桥接

---

## 9. TODO / 待续

- [ ] final_run.py 完成 → 对比 BASELINE vs Top8 365天结果
- [ ] 决定是否精简模型 (83基线 + N Kronos)
- [ ] 修复 GPU MCP server 握手问题
