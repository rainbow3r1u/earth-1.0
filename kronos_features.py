"""
Kronos 特征提取模块 — 统一提取 Kronos-base decode_s1() 的 hidden state
提取 context vector (832D for base) → 全部832维L2归一化

修复: 从PCA 20D升级到原始hidden state 128D，保留更多时序信号
"""
import os
import sys
import json
import numpy as np
import pandas as pd
import requests
import torch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kronos_model.kronos import Kronos, KronosTokenizer

_KRONOS_CACHE = {
    'model': None,
    'tokenizer': None,
    'klines': None,
    'last_ts': None,
    'last_features': None,
    'pca_params': None,
}

# 统一使用本地 Kronos-base（与 extract_embeddings.py 一致）
_LOCAL_BASE = os.path.join(os.path.dirname(__file__), 'kronos_finetune/kronos_pretrained/Kronos-base')
_LOCAL_TOKENIZER = os.path.join(os.path.dirname(__file__), 'kronos_finetune/kronos_pretrained/Kronos-Tokenizer-base')

# PCA 参数文件（由 extract_embeddings.py 生成）
KRONOS_EMBEDDING_FILE = os.path.join(os.path.dirname(__file__), 'data/kronos_embeddings.json')


def _load_pca_params():
    """加载预计算的PCA参数（已弃用，保留兼容）"""
    if _KRONOS_CACHE['pca_params'] is not None:
        return _KRONOS_CACHE['pca_params']

    try:
        with open(KRONOS_EMBEDDING_FILE, 'r') as f:
            data = json.load(f)
        params = {
            'pca_components': np.array(data['pca_components']),   # 旧PCA参数
            'pca_mean': np.array(data['pca_mean']),               # (832,)
            'scaler_mean': np.array(data['scaler_mean']),         # (832,)
            'scaler_scale': np.array(data['scaler_scale']),       # (832,)
        }
        # 全局归一化参数（HIGH-GPU-003: 与extract_embeddings.py对齐）
        if 'data_mean' in data and 'data_std' in data:
            params['data_mean'] = np.array(data['data_mean'])
            params['data_std'] = np.array(data['data_std'])
        _KRONOS_CACHE['pca_params'] = params
        return params
    except Exception as e:
        print(f"[Kronos] PCA参数加载失败: {e}")
        return None


def _get_model_and_tokenizer():
    """懒加载 Kronos base 模型和 tokenizer（只加载一次）"""
    if _KRONOS_CACHE['model'] is not None and _KRONOS_CACHE['tokenizer'] is not None:
        return _KRONOS_CACHE['model'], _KRONOS_CACHE['tokenizer']

    print("[Kronos] Loading tokenizer...")
    tokenizer = KronosTokenizer.from_pretrained(_LOCAL_TOKENIZER)
    print("[Kronos] Loading base model...")
    model = Kronos.from_pretrained(_LOCAL_BASE)
    model.eval()
    _KRONOS_CACHE['model'] = model
    _KRONOS_CACHE['tokenizer'] = tokenizer
    print("[Kronos] Model ready.")
    return model, tokenizer


def _load_btc_klines():
    """加载 BTC 日线缓存，返回 DataFrame [open,high,low,close,volume,quote_volume]"""
    if _KRONOS_CACHE['klines'] is not None:
        return _KRONOS_CACHE['klines']
    cache_file = '/home/myuser/backtester/data_cache/notusdt_1d_full.json'
    kls = []
    if os.path.exists(cache_file):
        try:
            with open(cache_file) as f:
                data = json.load(f)['klines']
            kls = data.get('BTCUSDT', [])
        except Exception:
            pass

    if len(kls) < 30:
        try:
            r = requests.get(
                'https://fapi.binance.com/fapi/v1/klines',
                params={'symbol': 'BTCUSDT', 'interval': '1d', 'limit': 500},
                timeout=15
            )
            raw = r.json()
            kls = [
                {'t': int(k[0]), 'o': float(k[1]), 'h': float(k[2]),
                 'l': float(k[3]), 'c': float(k[4]), 'v': float(k[5]), 'q': float(k[7])}
                for k in raw
            ]
        except Exception:
            pass

    if len(kls) < 30:
        return None

    df = pd.DataFrame(kls)
    df['datetime'] = pd.to_datetime(df['t'], unit='ms', utc=True)
    df = df.rename(columns={'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close', 'v': 'volume', 'q': 'quote_volume'})
    df = df.set_index('datetime').sort_index()
    result = df[['open', 'high', 'low', 'close', 'volume', 'quote_volume']]
    _KRONOS_CACHE['klines'] = result
    return result


def _get_btc_df_asof(ts):
    """获取截至 ts-1天 的 BTC 日线 DataFrame（最多 512 天）"""
    df = _load_btc_klines()
    if df is None:
        return None
    # CRITICAL-BT-010: 用 ts-86400 避免包含当日BTC数据泄露
    cutoff = pd.to_datetime(ts - 86400, unit='s', utc=True)
    df = df[df.index <= cutoff]
    if len(df) < 30:
        return None
    return df.tail(512)


def extract_kronos_features(ts, pred_len=2):
    """
    对给定时间戳 ts，提取 Kronos-base decode_s1() 的 hidden state (832D) → 全部832维L2归一化。

    返回: list of 832 floats (L2归一化后的 context vector)
    """
    # 同一 ts 缓存，避免重复推理
    if _KRONOS_CACHE['last_ts'] == ts and _KRONOS_CACHE['last_features'] is not None:
        return _KRONOS_CACHE['last_features']

    df = _get_btc_df_asof(ts)
    if df is None or len(df) < 30:
        return [0.0] * 832

    model, tokenizer = _get_model_and_tokenizer()
    # FIX: PCA参数可选加载，128维不需要PCA
    pca_params = _load_pca_params()

    try:
        # 准备输入数据
        cols = ['open', 'high', 'low', 'close', 'volume', 'quote_volume']
        data = df[cols].values.astype(np.float32)

        # z-score 归一化
        if pca_params is not None and 'data_mean' in pca_params and 'data_std' in pca_params:
            mean = pca_params['data_mean'].reshape(1, -1)
            std = pca_params['data_std'].reshape(1, -1) + 1e-8
        else:
            mean = data.mean(axis=0, keepdims=True)
            std = data.std(axis=0, keepdims=True) + 1e-8
        data_norm = (data - mean) / std

        # 取最近 CONTEXT_DAYS=256 天（与 extract_embeddings.py 对齐）
        CONTEXT_DAYS = 256
        if len(data_norm) >= CONTEXT_DAYS:
            ctx_data = data_norm[-CONTEXT_DAYS:]
            window_dates = df.index[-CONTEXT_DAYS:]
        else:
            # pad with zeros if history < 256
            pad_len = CONTEXT_DAYS - len(data_norm)
            ctx_data = np.concatenate([np.zeros((pad_len, 6), dtype=np.float32), data_norm], axis=0)
            window_dates = pd.DatetimeIndex([df.index[0] - pd.Timedelta(days=pad_len - i) for i in range(pad_len)]).append(df.index)

        x = torch.FloatTensor(ctx_data).unsqueeze(0)

        # 时间戳编码
        x_stamp = torch.zeros(1, len(ctx_data), 5, dtype=torch.long)
        for idx, d in enumerate(window_dates):
            x_stamp[0, idx, 0] = d.minute
            x_stamp[0, idx, 1] = d.hour
            x_stamp[0, idx, 2] = d.weekday()
            x_stamp[0, idx, 3] = d.day
            x_stamp[0, idx, 4] = d.month

        with torch.no_grad():
            tokens = tokenizer.encode(x, half=True)
            s1_ids, s2_ids = tokens[0], tokens[1]
            s1_logits, context = model.decode_s1(s1_ids, s2_ids, x_stamp)
            # context: [batch_size, seq_len, d_model] = [1, seq_len, 832]
            vec = context[:, -1, :].squeeze(0).cpu().numpy()  # (832,)

        # FIX: 输出原始 hidden state 全部 832 维，L2 归一化
        # vec: (832,) — 全部832维L2归一化
        TARGET_DIM = 832
        vec_128 = vec[:TARGET_DIM].copy()

        # L2归一化（避免不同币种/日期量纲差异）
        norm = np.linalg.norm(vec_128)
        if norm > 1e-8:
            vec_128 = vec_128 / norm
        else:
            vec_128 = np.zeros(TARGET_DIM, dtype=np.float32)

        feats = vec_128.tolist()
        _KRONOS_CACHE['last_ts'] = ts
        _KRONOS_CACHE['last_features'] = feats
        return feats

    except Exception as e:
        print(f"[Kronos] feature extraction failed: {e}")
        return [0.0] * 832


# 兼容 daily_predictor.py 的接口风格
_load_kronos_features = extract_kronos_features
