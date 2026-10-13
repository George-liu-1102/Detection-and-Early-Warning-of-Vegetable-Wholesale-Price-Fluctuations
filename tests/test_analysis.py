"""时间回放与 SQL 的业务口径回归测试。"""
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import evaluate_monitor as evaluation
import run_sql_analysis as sql_analysis
import run_veg_monitor as monitor


def test_future_returns_use_exact_calendar_date():
    daily = pd.DataFrame({'product': ['菜'] * 4,
        'date': pd.to_datetime(['2026-01-01', '2026-01-02', '2026-01-04', '2026-01-06']),
        'avg_price': [10., 20., 15., 30.]})
    result = evaluation.future_returns(daily)
    assert result.iloc[0] == 0.5
    assert np.isnan(result.iloc[1])
    assert np.isnan(result.iloc[-1])


def test_evaluation_uses_disjoint_periods_and_preserves_features():
    daily = pd.DataFrame({'date': pd.date_range('2026-01-01', '2026-08-11'),
                          'product': '菜', 'avg_price': 2., 'origin': '冀'})
    features = monitor.compute_features(daily)
    before = features.copy(deep=True)
    result = evaluation.evaluate(features)
    pd.testing.assert_frame_equal(features, before)
    assert len(result) == 27
    baseline = result[result.z_threshold.eq(3) & result.surge_pct.eq(.15)]
    assert baseline.n_records.tolist() == [151, 30, 42]
    assert baseline.n_alerts.sum() == 0
    assert baseline.n_forward_nonalerts.tolist() == [151, 30, 39]


def test_sql_dedup_daily_returns_and_gap_islands():
    daily = pd.DataFrame({'date': pd.to_datetime(['2026-01-01', '2026-01-02', '2026-01-03',
                         '2026-01-05', '2026-01-06', '2026-01-07']),
                         'product': '菜', 'avg_price': [1., 2., 3., 4., 5., 6.], 'origin': '冀'})
    features = monitor.classify_anomalies(monitor.compute_features(daily))
    features['anomaly_any'] = True
    with sqlite3.connect(':memory:') as connection:
        monthly, runs = sql_analysis.analyze(features, connection)
        assert monthly.n_anomalies.iloc[0] == 6
        assert runs.consecutive_days.tolist() == [3, 3]
        # 重复导入最后一条，SQL 视图仍只保留六个唯一品种日。
        connection.execute('INSERT INTO quotes SELECT * FROM quotes WHERE date = ?', ('2026-01-07',))
        assert connection.execute('SELECT count(*) FROM daily_clean').fetchone()[0] == 6


def test_review_samples_are_unlabelled_unique_and_reproducible():
    daily = pd.DataFrame({'date': pd.date_range('2026-01-01', periods=70), 'product': '菜',
                          'avg_price': [1., 2.] * 35, 'origin': '冀'})
    flags = monitor.classify_anomalies(monitor.compute_features(daily))
    a, b = evaluation.review_sample(flags, n=5), evaluation.review_sample(flags, n=5)
    pd.testing.assert_frame_equal(a, b)
    assert not a.duplicated(['date', 'product']).any()
    assert a.review_label.eq('').all()
