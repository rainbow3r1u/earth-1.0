# GPU 服务器连接文档

> 服务器: 175.155.64.171
> 端口: 24220
> 系统: Ubuntu 22.04, Python 3.10, CUDA 12.4

---

## 1. SSH 连接

```bash
ssh -p 24220 root@175.155.64.171
```

免密登录 (已配置 key)。

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
