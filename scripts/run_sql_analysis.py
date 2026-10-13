#!/usr/bin/env python3
"""运行 SQLite 分析，并与 Python 日环比、月度汇总进行一致性检查。"""
import argparse
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

import run_veg_monitor as monitor


def analyze(features, connection):
    table = features.copy()
    table['date'] = table.date.dt.strftime('%Y-%m-%d')
    table.to_sql('quotes', connection, index=False, if_exists='replace')
    connection.executescript((monitor.ROOT / 'sql' / 'analysis.sql').read_text(encoding='utf-8'))
    returns = pd.read_sql_query('SELECT date, product, daily_return FROM daily_returns ORDER BY product, date', connection)
    expected = features.sort_values(['product', 'date']).price_pct_ch.to_numpy()
    np.testing.assert_allclose(returns.daily_return.to_numpy(), expected, equal_nan=True)
    monthly = pd.read_sql_query('SELECT * FROM monthly_metrics ORDER BY month', connection)
    expected_monthly = features.groupby(features.date.dt.strftime('%Y-%m')).agg(
        n_records=('date', 'size'), n_anomalies=('anomaly_any', 'sum'),
        anomaly_rate=('anomaly_any', 'mean'), n_surge=('surge', 'sum'),
        n_crash=('crash', 'sum'), n_origin_shift=('origin_shift_alert', 'sum')).reset_index(names='month')
    pd.testing.assert_frame_equal(monthly, expected_monthly, check_dtype=False)
    runs = pd.read_sql_query('SELECT * FROM anomaly_runs ORDER BY consecutive_days DESC, product, start_date', connection)
    return monthly, runs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-path', type=Path, default=monitor.DEFAULT_DATA)
    parser.add_argument('--out-dir', type=Path, default=monitor.DEFAULT_OUT)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    features = monitor.classify_anomalies(monitor.compute_features(monitor.load_data(args.data_path)))
    with sqlite3.connect(':memory:') as connection:
        monthly, runs = analyze(features, connection)
    monthly.to_csv(args.out_dir / 'sql_monthly_metrics.csv', index=False, encoding='utf-8-sig')
    runs.to_csv(args.out_dir / 'sql_anomaly_runs.csv', index=False, encoding='utf-8-sig')
    print(f'SQL 与 Python 一致性通过：{len(features)} 条日环比，{len(monthly)} 个月度汇总；{len(runs)} 段连续异常')


if __name__ == '__main__':
    main()
