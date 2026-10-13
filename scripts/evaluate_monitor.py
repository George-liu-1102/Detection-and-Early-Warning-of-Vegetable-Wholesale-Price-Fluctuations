#!/usr/bin/env python3
"""按时间分段评估预设阈值的工作量，并导出未标注的人工复核样本。"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

import run_veg_monitor as monitor

PERIODS = [('development', '2026-01-01', '2026-06-01'),
           ('validation', '2026-06-01', '2026-07-01'),
           ('holdout', '2026-07-01', '2026-08-12')]


def future_returns(daily, horizon=3):
    """独立事后指标：精确 horizon 日后的报价，缺失时不可评估。"""
    result = []
    for _, part in daily.groupby('product', sort=False):
        prices = part.set_index('date').avg_price
        future = prices.reindex(part.date + pd.Timedelta(days=horizon)).to_numpy()
        result.append(pd.Series(future / part.avg_price.to_numpy() - 1, index=part.index))
    return pd.concat(result).reindex(daily.index)


def evaluate(features):
    forward = future_returns(features)
    rows = []
    # 九个预先定义的组合；不根据留出期表现挑选“最佳”组合。
    for z in [2.5, 3.0, 3.5]:
        for surge, crash in [(0.10, -0.08), (0.15, -0.12), (0.20, -0.16)]:
            flags = monitor.classify_anomalies(features, z_threshold=z, surge_pct=surge, crash_pct=crash)
            for period, start, stop in PERIODS:
                part = flags[flags.date.ge(start) & flags.date.lt(stop)]
                if part.empty:
                    continue
                days = pd.date_range(start, pd.Timestamp(stop) - pd.Timedelta(days=1))
                workload = part.groupby('date').anomaly_any.sum().reindex(days, fill_value=0)
                future = forward.reindex(part.index)
                alerts = part.anomaly_any
                observed = future.notna()
                row = dict(period=period, z_threshold=z, surge_pct=surge, crash_pct=crash,
                           n_records=len(part), observed_days=part.date.nunique(),
                           calendar_days=len(days), n_alerts=int(alerts.sum()),
                           alert_rate=alerts.mean(), alerts_per_calendar_day=workload.mean(),
                           p95_daily_alerts=workload.quantile(0.95),
                           n_forward_alerts=int((alerts & observed).sum()),
                           n_forward_nonalerts=int((~alerts & observed).sum()))
                for name, mask in [('alert', alerts & observed), ('nonalert', ~alerts & observed)]:
                    row[f'forward_abs_5pct_{name}_rate'] = (
                        future[mask].abs().ge(0.05).mean() if mask.any() else np.nan)
                rows.append(row)
    return pd.DataFrame(rows)


def review_sample(flags, n=20):
    """按五种信号与无信号分层抽样；重叠事件仅出现一次，标签留空。"""
    samples, used = [], set()
    for stratum in ['price_anom', 'surge', 'crash', 'origin_shift_alert', 'sustained_high', 'no_alert']:
        mask = ~flags.anomaly_any if stratum == 'no_alert' else flags[stratum]
        candidates = flags[mask & ~flags.index.isin(used)]
        sample = candidates.sample(min(n, len(candidates)), random_state=7).copy()
        used.update(sample.index)
        sample['sample_stratum'] = stratum
        samples.append(sample)
    result = pd.concat(samples).sort_values(['date', 'product'])
    result['review_label'] = ''
    result['review_notes'] = ''
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-path', type=Path, default=monitor.DEFAULT_DATA)
    parser.add_argument('--out-dir', type=Path, default=monitor.DEFAULT_OUT)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    features = monitor.compute_features(monitor.load_data(args.data_path))
    table = evaluate(features)
    if table.empty:
        raise ValueError('数据未覆盖预定义的 2026 年评估区间')
    table.to_csv(args.out_dir / 'threshold_evaluation.csv', index=False, encoding='utf-8-sig')
    sample = review_sample(monitor.classify_anomalies(features))
    sample.to_csv(args.out_dir / 'manual_review_sample.csv', index=False, encoding='utf-8-sig')
    baseline = table[table.z_threshold.eq(3) & table.surge_pct.eq(0.15)]
    lines = ['# 阈值评估与人工复核', '',
             '九组预设阈值按开发期（1–5 月）、验证期（6 月）、留出期（7 月至 8 月 11 日）分别统计。',
             '这是一轮事后时间回放：没有据此选择最优阈值，留出期也未用于调参。',
             '每条信号只使用当时可获得的历史；z-score 基准排除当日。完整组合见 threshold_evaluation.csv。', '',
             '## 默认阈值工作量', '',
             '| 时段 | 记录数 | 异常数 | 异常率 | 日均复核数 | 每日复核数 P95 |',
             '|---|---|---|---|---|---|']
    for _, row in baseline.iterrows():
        lines.append(f'| {row.period} | {row.n_records:.0f} | {row.n_alerts:.0f} | {row.alert_rate:.1%} | {row.alerts_per_calendar_day:.1f} | {row.p95_daily_alerts:.1f} |')
    lines += ['', '## 如何解释结果', '',
              '- 日均工作量的分母为完整日历天（包含无记录天）；异常率分母为实际品种日报价数。',
              '- 九组设置会改变告警数量，不能仅凭告警少就认定效果更好。产地阈值固定 5%，持续高位规则固定。',
              '- CSV 中 forward_abs_5pct 指当日后精确第 3 个日历天价格变化绝对值达到 5% 的比例。',
              '- 只统计第 3 天有报价的样本，同时报告可评估分母；比较告警与无告警样本的该比例。',
              '- 该指标仅描述后续价格变化，包含上涨和回落，不是准确率、误报率、提前预警率或收益率。',
              '- 无成交量、天气、库存及真实业务异常标签，不能验证因果或成本节省。', '',
              '## 人工复核计划', '',
              f'- manual_review_sample.csv 已固定随机种子分层抽取 {len(sample)} 个不同品种日，覆盖五类信号及无信号。',
              '- review_label 和 review_notes 留空，需人工核实原始报价、规格与产地后填写。',
              '- 标签建议：confirmed（需业务复核的有效异动）、data_issue（数据问题）、normal（无需复核）、uncertain（证据不足）。',
              '- 分层样本不代表总体比例。须另设独立、按实际告警分布抽样的评估集，避免调参和评估复用样本；未完成前不报告精确率/召回率。']
    (args.out_dir / 'evaluation_summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'完成 {len(table)} 行阈值评估，{len(sample)} 条待人工复核样本')


if __name__ == '__main__':
    main()
