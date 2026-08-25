#!/usr/bin/env python3
"""strict48 labeler 单元测试: 覆盖 Earth-2.0 strict48-v1 规则卡的核心不变量."""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from strict48_labeler import LabelerConfig, label_trade

E = 1_784_000_000  # 固定 entry_ts (秒)
MIN = 60
EXP = E + 172800


def bar(t, o=100.0, h=101.0, l=99.0, c=100.0):
    return {'t': t, 'o': o, 'h': h, 'l': l, 'c': c}


def full_bars(entry=E, n=2880, first=None, expiry_bar=None):
    """生成从 entry 开始连续 n 根窗口内 1m bar; 可选替换首根/追加到期 bar."""
    bars = []
    for i in range(n):
        t = entry + i * MIN
        if i == 0 and first is not None:
            bars.append(first)
        else:
            bars.append(bar(t))
    if expiry_bar is not None:
        bars.append(expiry_bar)
    return bars


class LongTests(unittest.TestCase):
    def test_sl_first(self):
        rec = label_trade([bar(E), bar(E+MIN, h=100.5, l=94.0, c=95.0)], 'XUSDT', E, 'LONG')
        self.assertEqual(rec['first_event'], 0)
        self.assertEqual(rec['trigger'], 'SL_FIRST')
        self.assertFalse(rec['ambiguous_same_bar'])
        self.assertAlmostEqual(rec['gross_pnl_pct'], -5.0)
        self.assertAlmostEqual(rec['net_pnl_pct'], -5.17)
        self.assertFalse(rec['trade_win'])
        self.assertEqual(rec['data_status'], 'VALID')
        self.assertAlmostEqual(rec['mae_pct'], 6.0)
        self.assertAlmostEqual(rec['mfe_pct'], 1.0)

    def test_tp_first(self):
        rec = label_trade([bar(E), bar(E+MIN, h=110.5, l=99.5)], 'XUSDT', E, 'LONG')
        self.assertEqual(rec['first_event'], 1)
        self.assertEqual(rec['trigger'], 'TP_FIRST')
        self.assertAlmostEqual(rec['gross_pnl_pct'], 10.0)
        self.assertAlmostEqual(rec['net_pnl_pct'], 9.86)
        self.assertTrue(rec['trade_win'])

    def test_same_bar_both_sl_first_conservative(self):
        rec = label_trade([bar(E), bar(E+MIN, h=110.5, l=94.5)], 'XUSDT', E, 'LONG')
        self.assertEqual(rec['first_event'], 0)
        self.assertTrue(rec['ambiguous_same_bar'])

    def test_exact_thresholds_trigger(self):
        rec_sl = label_trade([bar(E), bar(E+MIN, h=101.0, l=95.0)], 'XUSDT', E, 'LONG')
        rec_tp = label_trade([bar(E), bar(E+MIN, h=110.0, l=99.0)], 'XUSDT', E, 'LONG')
        self.assertEqual(rec_sl['trigger'], 'SL_FIRST')
        self.assertEqual(rec_tp['trigger'], 'TP_FIRST')

    def test_timeout_uses_expiry_open(self):
        expiry = bar(EXP, o=102.0, h=103.0, l=101.0, c=102.5)
        rec = label_trade(full_bars(expiry_bar=expiry), 'XUSDT', E, 'LONG')
        self.assertEqual(rec['first_event'], 2)
        self.assertEqual(rec['trigger'], 'TIMEOUT')
        self.assertAlmostEqual(rec['gross_pnl_pct'], 2.0)
        self.assertAlmostEqual(rec['net_pnl_pct'], 1.86)
        self.assertTrue(rec['trade_win'])
        self.assertIsNone(rec['minutes_to_event'])

    def test_expiry_bar_is_half_open_excluded(self):
        # 到期 bar 同时满足 SL/TP 也不得触发
        expiry = bar(EXP, o=100.0, h=111.0, l=94.0, c=100.0)
        rec = label_trade(full_bars(expiry_bar=expiry), 'XUSDT', E, 'LONG')
        self.assertEqual(rec['trigger'], 'TIMEOUT')
        self.assertFalse(rec['ambiguous_same_bar'])

    def test_mae_mfe_only_within_window(self):
        bars = full_bars(first=bar(E, h=108.0, l=92.0), expiry_bar=bar(EXP, o=100.0))
        rec = label_trade(bars, 'XUSDT', E, 'LONG')
        self.assertAlmostEqual(rec['mae_pct'], 8.0)
        self.assertAlmostEqual(rec['mfe_pct'], 8.0)


class ShortTests(unittest.TestCase):
    def test_sl_tp(self):
        sl = label_trade([bar(E), bar(E+MIN, h=105.5, l=99.5)], 'XUSDT', E, 'SHORT')
        tp = label_trade([bar(E), bar(E+MIN, h=99.5, l=89.5)], 'XUSDT', E, 'SHORT')
        self.assertEqual(sl['trigger'], 'SL_FIRST')
        self.assertAlmostEqual(sl['net_pnl_pct'], -5.17)
        self.assertEqual(tp['trigger'], 'TP_FIRST')
        self.assertAlmostEqual(tp['net_pnl_pct'], 9.86)

    def test_same_bar_both(self):
        rec = label_trade([bar(E), bar(E+MIN, h=105.5, l=89.5)], 'XUSDT', E, 'SHORT')
        self.assertEqual(rec['trigger'], 'SL_FIRST')
        self.assertTrue(rec['ambiguous_same_bar'])

    def test_mae_mfe(self):
        rec = label_trade([bar(E), bar(E+MIN, h=108.0, l=92.0)], 'XUSDT', E, 'SHORT')
        # SHORT: MAE=上涨, MFE=下跌
        self.assertEqual(rec['trigger'], 'SL_FIRST')
        self.assertAlmostEqual(rec['mae_pct'], 8.0)
        self.assertAlmostEqual(rec['mfe_pct'], 8.0)


class DataStatusTests(unittest.TestCase):
    def test_missing_entry(self):
        rec = label_trade([bar(E+180)], 'XUSDT', E, 'LONG')
        self.assertEqual(rec['data_status'], 'MISSING_ENTRY')

    def test_no_bars(self):
        rec = label_trade([], 'XUSDT', E, 'LONG')
        self.assertEqual(rec['data_status'], 'NO_BARS')

    def test_incomplete_window(self):
        rec = label_trade(full_bars(n=60), 'XUSDT', E, 'LONG')
        self.assertEqual(rec['data_status'], 'INCOMPLETE_WINDOW')
        self.assertEqual(rec['trigger'], 'TIMEOUT')

    def test_event_before_gap_is_valid(self):
        bars = [bar(E), bar(E+MIN, h=100.0, l=94.0)]  # SL 已触发, 后续无数据
        rec = label_trade(bars, 'XUSDT', E, 'LONG')
        self.assertEqual(rec['trigger'], 'SL_FIRST')
        self.assertEqual(rec['data_status'], 'VALID')


class CostTests(unittest.TestCase):
    def test_funding_long_pays_short_receives(self):
        cfg = LabelerConfig(funding=((E + MIN, 0.0001),))
        long_rec = label_trade([bar(E), bar(E+MIN, h=110.5, l=99.5)], 'XUSDT', E, 'LONG', cfg)
        short_rec = label_trade([bar(E), bar(E+MIN, h=99.5, l=89.5)], 'XUSDT', E, 'SHORT', cfg)
        self.assertAlmostEqual(long_rec['net_pnl_pct'], 9.85)
        self.assertAlmostEqual(short_rec['net_pnl_pct'], 9.87)

    def test_tiny_positive_net_wins_tiny_negative_loses(self):
        # TIMEOUT 且 gross 略高于/略低于成本 0.14%
        win = label_trade(full_bars(expiry_bar=bar(EXP, o=100.20)), 'XUSDT', E, 'LONG')
        lose = label_trade(full_bars(expiry_bar=bar(EXP, o=99.90)), 'XUSDT', E, 'LONG')
        self.assertTrue(win['trade_win'])
        self.assertFalse(lose['trade_win'])

    def test_net_pnl_zero_rule_implementation(self):
        # net == 0 时必须判输 (代码不变量: trade_win = net_pnl > 0)
        rec = label_trade([bar(E), bar(E+MIN, h=100.0, l=94.0)], 'XUSDT', E, 'LONG')
        self.assertEqual(rec['trade_win'], rec['net_pnl_pct'] > 0)
        self.assertFalse(rec['trade_win'])


class InvariantTests(unittest.TestCase):
    def test_deterministic(self):
        bars = full_bars(first=bar(E, h=110.0, l=94.0), expiry_bar=bar(EXP, o=100.0))
        a = label_trade(bars, 'XUSDT', E, 'LONG')
        b = label_trade(bars, 'XUSDT', E, 'LONG')
        self.assertEqual(a, b)

    def test_expiry_minus_entry_is_172800(self):
        rec = label_trade([bar(E)], 'XUSDT', E, 'LONG')
        self.assertEqual(rec['expiry_ts'] - rec['entry_ts'], 172800)

    def test_ambiguous_status_aligns_with_rust_single_truth(self):
        rec = label_trade([bar(E), bar(E+MIN, h=110.5, l=94.5)], 'XUSDT', E, 'LONG')
        self.assertEqual(rec['data_status'], 'AMBIGUOUS')
        self.assertEqual(rec['first_event'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
