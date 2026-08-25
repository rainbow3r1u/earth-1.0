# GPU 服务器开机执行清单（MAE strict48 复验）

> 服务器: 175.155.64.171:22158 (Earth 2.0, linux 用户)
> 原则: 只跑预注册配置; 不网格搜索; 不 commit/push; 老生产只读。

## 1. 开机后第一步：只读盘点（不要训练）

```bash
ssh -p 22158 linux@175.155.64.171          # 密码向用户索取; 或用解密后的 earth2_key
df -h /home/linux
cd ~/earth-2.0
git status --short                          # 只读查看, 不动
ls data/klines/*.bin | wc -l                # 期望 500
ls -lh data/klines/BTCUSDT.bin
wc -l data/labels/strict48-v1.jsonl         # 期望 ~456,818
ls -lh data/rust_features.bin data/rust_meta.json data/dataset_*_meta.json 2>/dev/null
ls scripts/ src/bin/ | head -50
```

记录结果到 `mae_audit_20260816/remote_inventory.md`。

## 2. 数据完整性审计

```bash
# 若二进制已构建
./target/release/validate                    # 期望 486 OK / 14 上市晚, 0 缺口
# 否则记录 build 状态, 先 cargo build --release 或复用已有 target
sha256sum data/klines/BTCUSDT.bin | head
```

## 3. 标签器交叉对照（本机 Python vs 服务器 Rust）

1. 把 `strict48_labeler.py` 上传到服务器 `~/earth-2.0/scripts/`；
2. 在服务器生成 1000 条随机样本的原始窗口 JSON（Rust labeler 已有抽查工具，或写只读 dump 脚本）；
3. 分别跑两个 labeler，比较 `first_event/net_pnl_pct/mae_pct/mfe_pct/trade_win/data_status`；
4. 允许差异：浮点尾差 ≤1e-9 相对误差；其余差异必须逐条归因，未解释差异必须 = 0。

## 4. 预注册 walk-forward（CPU 12 核，禁用 gpu_hist）

按 `README_ACCEPTANCE.md` 闸门 B/C/D 跑，固定配置：

- 方向模型: XGBoost 200 树 / md6 / lr0.05 / λ10 / α10 / s0.8 / c0.6 / seed 42/43；
- MAE 模型: 150 树 / 同超参 / seed 42；
- 标签: strict48-v1 TP10/SL5, MAE>5% 二分类；
- 选币: prob≥60 池内多空各 Top-10; LONG θ=0.3 / SHORT θ=0.2；
- 训练: 方向每日重训（能力不够先 7 天重训并标注口径），MAE 7 天重训；
- 段: 2025-06~09 / 2025-10~2026-01 / 2026-02~05 / 2026-05~08。

```bash
# 参考 Earth 2.0 已有命令（具体以服务器实际脚本为准, 先 dry-run 查看参数）
./target/release/align --help
python3 scripts/gen_mae_labels.py --help   # 若存在
python3 scripts/mae_sel_rank.py --help
python3 scripts/mae_topn.py --help
```

输出必须同时保存：实验身份（日期/代码 sha/数据 sha/参数）、每段 net pnl/胜率/stop 率、无滤对照。

## 5. 回传本机

```bash
scp -P 22158 linux@175.155.64.171:~/earth-2.0/logs/mae_audit_*.log /tmp/trading_audit_20260816/
scp -P 22158 linux@175.155.64.171:~/earth-2.0/docs/mae_audit_*.md /tmp/trading_audit_20260816/
```

## 6. 关机条件

- 完成闸门 B/C/D 并回传结果后即可关机；
- 若结果不通过，不再续跑新配置，先回本机做归因并请示用户。
