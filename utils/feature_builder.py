"""
特征构建工具 — 所有模块共享特征组装顺序，避免维度不一致

特征顺序:
  基础 (10): ret_1d_norm, ret_3d_norm, ret_5d_norm, volatility, vol_ratio, price_position, amplitude, streak, div_sign, oi_chg
  vol_col (3): vol_regime, vol_momentum, vol_persist
  信号 (7): beta, alpha, r2, residual, rsi7, rsi14, rsi30
  rsi_div (4): rsi_div_top, rsi_div_bottom, rsi_overbought_persist, rsi_price_corr_20d
  板块 (22): SECTOR_ORDER 22板块
  宏观 (~878): etf(2)+chain(4)+sent(6)+fg(1)+st(1)+cb(1)+cbg(1)+bd(1)+kg(1)+hr(1)+liq(7)+tvl(6)+Kronos(EMBEDDING_DIM)+ma(3)+ab(1)
  → 实际总维度 = 46 + EMBEDDING_DIM (832 → 878维)
"""
# 注意: 实际特征维度取决于 daily_predictor.EMBEDDING_DIM (Kronos嵌入维度)
# 当前 EMBEDDING_DIM=832 → 总维度 = 10+3+7+4+22+2+4+6+1+1+1+1+1+1+1+7+6+832+3+1 = 915
FEATURE_DIM = 102  # 已废弃，保留兼容 — 实际维度由 assemble_feature_vec 调用方确定


def assemble_feature_vec(
    ret_1d_norm, ret_3d_norm, ret_5d_norm,
    volatility, vol_ratio, price_position, amplitude, streak, div_sign, oi_chg,
    vol_col,
    beta, alpha, r2, residual, rsi7, rsi14, rsi30,
    rsi_div,
    sector_feats,
    macro_feats,
):
    """按规范顺序组装特征向量。所有调用方使用此函数确保维度一致。"""
    base = [ret_1d_norm, ret_3d_norm, ret_5d_norm, volatility, vol_ratio,
            price_position, amplitude, streak, div_sign, oi_chg]
    signals = [beta, alpha, r2, residual, rsi7, rsi14, rsi30]
    return base + list(vol_col) + signals + list(rsi_div) + list(sector_feats) + list(macro_feats)
