#!/usr/bin/env python3
"""Smoke test: 验证 auto_dual_trade 的核心链路 (数据加载→特征构建→训练→预测) 不崩溃"""
import os, sys, json, time
sys.path.insert(0, os.path.dirname(__file__))
# Lazy imports to avoid triggering heavy dependency chains
from utils.feature_builder import assemble_feature_vec, FEATURE_DIM

def test_data_loading():
    """测试K线+OI数据加载"""
    import daily_predictor as dp
    print("[1/5] 加载K线...")
    klines = dp.fetch_klines()
    assert len(klines) >= 100, f"K线币种过少: {len(klines)}"
    print(f"      ✅ {len(klines)} 币种")

    print("[2/5] 加载OI...")
    syms = list(klines.keys())[:200]
    oi_data = dp.fetch_oi(syms)
    assert len(oi_data) > 0, "OI数据为空"
    print(f"      ✅ {len(oi_data)} 币种")

    return klines, oi_data

def test_feature_building(klines, oi_data):
    """测试特征构建"""
    import daily_predictor as dp
    print("[3/5] 构建特征...")
    X, symbols, timestamps = dp.build_features(klines, oi_data)
    assert len(X) > 100, f"样本过少: {len(X)}"
    assert len(X[0]) > 90, f"特征维度不足: {len(X[0])}"
    print(f"      ✅ {len(X)} 样本, {len(X[0])} 维特征")
    return X

def test_model_training(X):
    """测试模型训练"""
    print("[4/5] 训练模型...")
    from xgboost import XGBClassifier
    import numpy as np
    y = np.random.choice([0, 1], size=len(X))
    if sum(y) < 5:
        y[:10] = 1
    model = XGBClassifier(n_estimators=20, max_depth=3, random_state=42, verbosity=0)
    model.fit(np.array(X[:10000]), y[:10000])
    proba = model.predict_proba(np.array(X[:100]))[:, 1]
    assert len(proba) == 100
    print(f"      ✅ 训练完成, 预测均值 {proba.mean():.3f}")

def test_feature_vec_assembly():
    """测试特征向量组装一致性"""
    print("[5/5] 验证特征维度...")
    vec = assemble_feature_vec(
        0.01, 0.02, 0.03, 0.04, 1.0, 0.5, 0.03, 3, 0, 0.01,
        [1.0, 0.0, 0.0], 1.0, 0.0, 1.0, 0.0, 50.0, 55.0, 60.0,
        [0.5, 0.0, 0.0, 0.8], [0.0]*22, [0.0]*56
    )
    assert len(vec) == FEATURE_DIM, f"维度不匹配: {len(vec)} != {FEATURE_DIM}"
    print(f"      ✅ 特征向量 {len(vec)} 维")

if __name__ == '__main__':
    print("=" * 50)
    print("  auto_dual_trade Smoke Test")
    print("=" * 50)
    try:
        klines, oi_data = test_data_loading()
        X = test_feature_building(klines, oi_data)
        test_model_training(X)
        test_feature_vec_assembly()
        print("\n✅ ALL CHECKS PASSED")
    except Exception as e:
        print(f"\n❌ FAILED: {e}")
        raise