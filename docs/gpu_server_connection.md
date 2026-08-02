# GPU 服务器连接文档

> 服务器: 175.155.64.171
> 端口: **22160**(2026-08-02 更新; 曾用 24220/22183/22156 均已失效)
> 用户: **linux**(密码登录; 曾用 root)
> 系统: Ubuntu 22.04, Python 3.10, CUDA 12.4
> 硬件: RTX 3080 20GB(2026-08-02 换机后同款), 磁盘保留(服务商按天租, 关机不释放)

---

## 1. SSH 连接

```bash
ssh -p 22160 linux@175.155.64.171
```

密码登录 (sshpass 可用; root 免密已失效)。

---

## 2. 硬件规格

| 组件 | 型号 | 规格 |
|------|------|------|
| GPU | RTX 3080 | 20GB VRAM, CUDA CC 8.6 |
| CPU | Xeon Platinum 8275CL | 12核 @ 3.0GHz |
| 内存 | 31GB | — |
| CUDA | 12.4 | Driver 550.107 |

```bash
nvidia-smi
lscpu | grep "Model name"
free -h
```

---

## 3. 工作目录

```
/root/reasonix-projects/websocket_new/     # 主工作目录 (代码)
/root/reasonix-projects/backtester/data_cache/  # K线 + OI 缓存
/root/.local/share/auto_trade/             # 模型 + 重要性日志
/root/sentiment_data/                      # 情绪数据
/root/blockchair_data/                     # 链上数据
/root/defillama_data/                      # TVL 数据
/root/stablecoin_data/                     # 稳定币数据
/root/hashrate_data/                       # 算力数据
/root/coingecko_data/                      # 市值/市占率
/root/etf_data/                            # ETF 资金流
```

---

## 4. Python 环境

```bash
# 查看已安装包
python3 -m pip list | grep -E "torch|xgboost|numpy|pandas|transformers"

# 安装新包
python3 -m pip install <package>
```

已安装: `torch 2.7.1+cu118`, `xgboost 3.2`, `numpy`, `pandas`, `transformers 5.9`, `scikit-learn`, `scipy`, `einops`, `cos-python-sdk-v5`, `py-spy`

---

## 5. 路径桥接

代码硬编码 `/home/myuser/`，GPU 上通过 symlink 桥接到 `/root/`:

```bash
ls -la /home/myuser/
# backtester/  → /root/reasonix-projects/backtester/
# websocket_new/ → /root/reasonix-projects/websocket_new/
# sentiment_data/ → /root/sentiment_data/
# ...
```

---

## 6. COS 中转 (本地 ↔ GPU)

COS Bucket: `lhsj-1h-1314017643`, Region: `ap-seoul`

### 本地上传
```python
from qcloud_cos import CosConfig, CosS3Client
cos = CosS3Client(CosConfig(Region='ap-seoul',
    SecretId=os.environ['COS_SECRET_ID'],
    SecretKey=os.environ['COS_SECRET_KEY']))
B = os.environ['COS_BUCKET']
cos.upload_file(Bucket=B, Key='klines/cache/file.json', LocalFilePath='/path/to/local.json')
```

### GPU 下载
```python
cos.download_file(Bucket=B, Key='klines/cache/file.json', DestFilePath='/root/path.json')
```

常用 COS 路径:
- `klines/cache/notusdt_1d_full.json` — K线缓存
- `klines/cache/oi_daily.json` — OI缓存
- `klines/cache/kronos_features_cache.json` — Kronos特征
- `klines/cache/kronos_importance_log.json` — 重要性排名
- `klines/cache/train_data_latest.npz` — 训练数据
- `klines/cache/xgb_daily_model.pkl` — XGBoost模型

---

## 7. GPU MCP Server

配置文件: `~/.reasonix/config.json` → `mcp[0]`

```json
"gpu=ssh -p 24220 root@175.155.64.171 python3 -u /root/reasonix-projects/websocket_new/gpu_mcp.py"
```

提供的工具:
- `gpu_status` — GPU 使用情况
- `gpu_run_sweep` — 运行爬坡实验
- `gpu_run_backtest` — 运行回测
- `gpu_get_results` — 查看结果

---

## 8. 监控命令

```bash
# GPU 状态
ssh -p 24220 root@175.155.64.171 'nvidia-smi'

# 查看日志
ssh -p 24220 root@175.155.64.171 'tail -20 /tmp/final.log'

# 查看进程
ssh -p 24220 root@175.155.64.171 'ps aux | grep python | grep -v grep'

# Python 栈追踪
ssh -p 24220 root@175.155.64.171 'py-spy dump --pid <PID>'

# 杀掉所有 Python 进程
ssh -p 24220 root@175.155.64.171 'killall -9 python3'
```

---

## 9. 文件传输

```bash
# 本地 → GPU
scp -P 24220 /local/file.py root@175.155.64.171:/root/path/

# GPU → 本地
scp -P 24220 root@175.155.64.171:/root/path/file.json /local/path/

# 文件夹
scp -P 24220 -r /local/dir/ root@175.155.64.171:/root/path/
```

---

## 10. 后台运行脚本

```bash
ssh -p 24220 root@175.155.64.171 'cd /root/reasonix-projects/websocket_new && nohup python3 -u script.py > /tmp/output.log 2>&1 & echo PID=$!'
```

查看: `tail -f /tmp/output.log`

---

## 11. 2026-08-02 Ops Log (audit & maintenance)

> 详见 Obsidian: `Sync/rainbow/研究/前向观察与回测生产差异调查-20260802.md` + `Sync/rainbow/回溯日志/001_946D_180d_GPU.md`

### 实际工作路径(2026-08-02 实测, 文档 3/5 节为旧 root 路径)
- 主工作目录: `/home/linux/websocket_new/`(非 /root/reasonix-projects/)
- 数据缓存: `/home/linux/backtester/data_cache/`; `/home/myuser → /home/linux` 软链(7/12 建)使代码内 myuser 路径可用
- 实验日志: `/home/linux/exp_*.log`(70+, 不在 websocket_new 内)

### 数据同步(prod → GPU, 全部 MD5 验证一致)
- klines/OI/funding/sector(+sector_overrides, 原缺失)/sentiment/coingecko/hashrate/stablecoin/defillama/blockchair
- 修复: GPU 外部数据曾停更 7/17-18, 回测前必须重同步

### 回测引擎与审计
- `gpu_backtest_exp.py::_build_coin_samples` → 改用 `auto_dual_trade._build_feat_impl`(生产同源); 备份 `.bak_0801`
- 审计 cron(只读): `4 8` audit_snapshot.py / `25 8` audit_verify.py → `logs/audit.log`, 定位生产 npz 特征偏差(8/3 出判定)

### 180d 真实水位(08-02, 全同步+生产同源构建)
Sharpe 17.29 / +1050% / MaxDD 15.6% / 胜率 76% — 与脏数据版 18.68 几乎一致, 为生产同源特征稳定水位

---

## 12. 2026-08-02 完整工作总结(与 Obsidian 研究文档同步)

> 全链路调查见 `Sync/rainbow/研究/前向观察与回测生产差异调查-20260802.md`

### 背景
前向观察期(7/30~8/6)TOP1 口径 4 连止损(-20%),与回测水位(Sharpe 17~20/76%)严重背离,启动全链路调查。

### 一、前向评估口径修正史
| 版本 | 口径 | 结果 | 教训 |
|---|---|---|---|
| v1(7/31) | 日线open入场+豁免入场日+15m | GIGGLE"浮盈+7.63%" | 漏开仓当天止损 |
| **v2(8/1定)** | **8:21市价入场+1m粒度+成交即盯盘,先查止损后止盈,48h到期** | TOP1 4单全止损-20% | — |
| tracker/邮件 | verify_yesterday 早盘快照(08:21未收盘) | 乐观偏置(EDGE记+2.59%实-9.8%) | 结算须用完整K线 |

**TOP1 前向(修正)**: 7/29 EDGEUSDT -5%｜7/30 PLAYUSDT -5%｜7/31 ONUSDT -5%｜8/1 GIGGLEUSDT -5%(开仓6分钟)
**Top10 修正结算(7/30、7/31, 1m)**: LONG 20笔 累计**+2.6%**(45%胜率); SHORT 10笔 累计**-20.0%**(20%胜率, 邮件+20%是快照假象)

### 二、回测 vs 生产差异(15+交叉实验)
- 已排除: 代码MD5 / 数据全同步全一致(修了GPU外部数据7/17-18停更+缺sector_overrides) / 标签 / 超参 / winsor / cuda-hist / 宇宙 / 训练窗
- 决定性对照: 生产npz训练→63~68%正常; GPU缓存训练→95~96%饱和; 生产原装模型→GPU X_pred=68.6%正常 ⇒ 差异在训练矩阵
- **关键转折**: 0GUSDT列0: 理论/GPU缓存/生产主流程复刻全=-1.9970; **生产npz=-0.2601(对不上任何构建)**; npz含569个vol_raw=0异常行 ⇒ **偏差在生产端每日构建环节, 回测反而是干净参照系**
- 最可能机制(待8/3): 08:05内存加载旧K线, API更新(08:05:17)在加载之后, 构建用内存旧版

### 三、审计cron(8/2挂, 只读)
`4 8` audit_snapshot.py + `25 8` audit_verify.py → logs/audit.log; 判定MATCH_A/NO_MATCH; crontab备份 /tmp/crontab_backup_20260802.txt

### 四、180d真实水位(8/2, 全同步+adt同源)
**Sharpe 17.29 / +1050% / MaxDD 15.6% / 胜率76%**(LONG 118笔84%/SHORT 62笔60%; prob全饱和90~100%)。脏18.68 vs 净17.29几乎一致 ⇒ 17~18是生产同源特征稳定水位, 8/1"两套构建"结论作废; 生产修好偏差后可复现(待前向验证)

### 五、统计alpha理解(用户8/2)
- "市场存在统计学的Alpha" ✅ 实证支撑(walk-forward 17.29/76%, 脏净两次一致)
- "我们生产现在能拿到" ❌ 未兑现(偏差未修, 前向4连止损); 修偏差→同构→前向验证

### 六、待办
- [ ] 8/3 08:25 看 logs/audit.log 判定
- [ ] 修生产构建偏差, 前向重新观察
- [ ] 修 verify_yesterday 结算口径(生产文件, 需180d回测+用户同意)
- [ ] 8/6 前向评审(1m修正口径汇总)
- [ ] 生产npz构建校验(偏差即告警)

---

## 13. 接手指引 (For Next AI, 2026-08-02)

> 本调查一句话: 前向 4 连止损 vs 回测 17~20 Sharpe 背离 → 追查发现**生产端每日保存的训练数据(npz)特征与代码重建不一致**(8/1 起); 回测(正确构建)反而是干净参照系; 180d 真实水位 17.29 是"生产同源特征"的稳定水位。

### 术语速查
| 术语 | 含义 |
|---|---|
| `npz` | `~/.local/share/auto_trade/train_data_latest.npz`, 生产每天 8:17 保存的训练数据(X_train 为 winsor 后特征) |
| `adt` | `auto_dual_trade.py`(生产主程序); `adt._build_feat_impl` = 生产特征构建函数(唯一正确实现) |
| `回测引擎` | `gpu_backtest_exp.py`(GPU 端), 8/2 起样本构建调用 `adt._build_feat_impl`(生产同源) |
| `饱和` | 模型输出概率 90~100%(回测自训模型固有输出分布); 生产模型输出 54~71% |
| `早盘快照` | `verify_yesterday`(daily_predictor.py)在结算日 08:21 用未收盘 K 线结算 → 收益乐观偏置 |
| `特征日/样本日` | 样本日 ts 的特征用 ts-1 蜡烛; 回测缓存文件名 = 样本日 |

### 关键路径
- 生产代码: `/home/myuser/websocket_new/`; 训练数据 `~/.local/share/auto_trade/train_data_latest.npz`
- 审计: `audit/{audit_snapshot,audit_verify}.py` → `logs/audit.log`
- GPU: `ssh -p 22160 linux@175.155.64.171`, 代码 `/home/linux/websocket_new/`, 回测缓存 `~/backtester/data_cache/by_day_cache_v5_aligned_volraw_fund{,_prod}`
- 观察期结算口径 v2: 8:21 市价入场 + 1m 粒度 + 成交即盯盘(先查止损后止盈, 48h 到期)

### 审计判定决策树(8/3 08:25 后看 logs/audit.log)
- `MATCH_A` = npz 特征与运行前 K 线一致 → 内存旧版 K 线实锤; 修复: 检查 `auto_dual_trade.py` 加载顺序, K 线刷新写盘需在内存加载之前
- `MATCH_B` = 与运行后磁盘一致 → 偏差在 npz 保存环节/其他进程覆盖, 继续查
- `NO_MATCH` = 另有来源

### 接手第一步
1. 读 `logs/audit.log` 判定 → 行动
2. 修完偏差: GPU 重跑 180d 验证水位 ~17.29, 生产前向重新观察
3. 更新本档 + Obsidian 研究文档

### 已知问题
1. 生产 npz 特征偏差(待 8/3 判定+修复)
2. `verify_yesterday` 早盘快照偏置(邮件/tracker 收益不可信, 需改完整 K 线结算, 生产文件改动需 180d 回测+用户同意)
3. 前向 TOP1 4 连止损 = 偏差特征模型输出, 不代表修复后
