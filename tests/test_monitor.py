"""run_veg_monitor 核心逻辑的单元测试。"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_veg_monitor as rvm

ROOT = Path(__file__).resolve().parents[1]


def make_daily(n_products: int = 3, n_days: int = 90, seed: int = 7) -> pd.DataFrame:
    """构造带人为暴涨点的合成日度数据。"""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2026-01-01", periods=n_days, freq="D")
    rows = []
    for p in range(n_products):
        base = 2.0 + p
        for d in dates:
            price = base + rng.normal(0, 0.08)
            rows.append({
                "date": d,
                "product": f"测试蔬菜{p + 1}",
                "avg_price": round(price, 3),
                "origin": "冀",
            })
    df = pd.DataFrame(rows)
    # 第 40 天对第一个品种制造一次 +50% 的价格跳升
    mask = (df["product"] == "测试蔬菜1") & (df["date"] == dates[39])
    df.loc[mask, "avg_price"] = df.loc[mask, "avg_price"] * 1.5
    return df


def test_load_data_real_csv():
    path = ROOT / "data" / "veg_wholesale_xinfadi.csv"
    if not path.exists():
        pytest.skip("真实数据集不存在，请先运行 build_xinfadi_dataset.py")
    df = rvm.load_data(path)
    assert {"date", "product", "avg_price"}.issubset(df.columns)
    assert df["avg_price"].notna().all()
    assert (df["avg_price"] > 0).all()
    assert df["product"].nunique() > 50


def test_compute_features_creates_rolling_stats():
    df = make_daily()
    out = rvm.compute_features(df)
    for col in ["price_pct_ch", "rolling_mean", "rolling_std", "rolling_z", "rolling_vol"]:
        assert col in out.columns
    mature = out[out["date"] >= pd.Timestamp("2026-03-01")]
    assert mature["rolling_mean"].notna().all()
    assert mature["rolling_std"].notna().all()


def test_classify_detects_planted_surge():
    df = make_daily()
    v = rvm.compute_features(df)
    v = rvm.classify_anomalies(v)
    spike = v[(v["product"] == "测试蔬菜1") & (v["date"] == pd.Timestamp("2026-02-09"))]
    assert not spike.empty
    assert bool(spike["surge"].iloc[0])
    assert bool(spike["anomaly_any"].iloc[0])


def test_classify_no_anomaly_in_quiet_series():
    df = make_daily()
    v = rvm.compute_features(df)
    v = rvm.classify_anomalies(v)
    quiet = v[v["product"] == "测试蔬菜3"]
    assert int(quiet["surge"].sum()) == 0
    assert int(quiet["crash"].sum()) == 0


def test_build_summary_sorted_by_risk():
    df = make_daily()
    v = rvm.compute_features(df)
    v = rvm.classify_anomalies(v)
    summary = rvm.build_summary(v)
    assert "risk_score" in summary.columns
    scores = summary["risk_score"].tolist()
    assert scores == sorted(scores, reverse=True)


def test_build_alert_message_contains_product():
    df = make_daily()
    v = rvm.compute_features(df)
    v = rvm.classify_anomalies(v)
    msg = rvm.build_alert_message(v)
    assert isinstance(msg, str)
    assert "测试蔬菜" in msg


def test_send_alert_dry_run():
    assert rvm.send_alert("测试消息", dry_run=True) is True

def test_missing_origins_never_trigger_switch(tmp_path):
    daily = pd.DataFrame({
        'date': pd.date_range('2026-01-01', periods=6), 'product': ['菜'] * 6,
        'avg_price': [1, 2, 4, 8, 16, 32],
        'origin': [None, np.nan, '', '冀', '鲁/冀', '冀/鲁'],
    })
    path = tmp_path / 'daily.csv'
    daily.to_csv(path, index=False)
    loaded = rvm.load_data(path)
    assert loaded.origin.eq('').sum() == 3
    v = rvm.classify_anomalies(rvm.compute_features(loaded))
    assert v.origin_shift_alert.tolist() == [False, False, False, False, True, False]


def test_gap_is_not_daily_return_and_resets_sustained_high():
    daily = pd.DataFrame({
        'date': pd.to_datetime(['2026-01-01', '2026-01-02', '2026-01-05', '2026-01-06', '2026-01-07']),
        'product': ['菜'] * 5, 'avg_price': [1, 1, 3, 3, 3], 'origin': ['冀'] * 5,
    })
    v = rvm.compute_features(daily)
    assert v.loc[2, 'observed_pct_ch'] == 2
    assert pd.isna(v.loc[2, 'price_pct_ch'])
    v['rolling_z'] = 2.5
    result = rvm.classify_anomalies(v)
    assert not result.loc[2, 'surge']
    assert result.sustained_high.tolist() == [False, False, False, False, True]


def test_calendar_history_excludes_today_and_old_observations():
    daily = make_daily(n_products=1, n_days=90)
    daily = daily.iloc[list(range(20)) + [89]].copy()
    v = rvm.compute_features(daily)
    assert v.iloc[-1].history_n == 0
    assert pd.isna(v.iloc[-1].rolling_mean)
    dense = make_daily(n_products=1, n_days=90)
    a = rvm.compute_features(dense)
    dense.loc[40:, 'avg_price'] *= 10
    b = rvm.compute_features(dense)
    pd.testing.assert_frame_equal(a.iloc[:40], b.iloc[:40])
    assert a.loc[40, 'rolling_mean'] == b.loc[40, 'rolling_mean']
    assert a.loc[40, 'rolling_std'] == b.loc[40, 'rolling_std']
    assert a.loc[40, 'rolling_z'] != b.loc[40, 'rolling_z']


def test_rate_score_does_not_reward_longer_duplicate_history():
    v = rvm.classify_anomalies(rvm.compute_features(make_daily(n_products=1)))
    longer = v.copy()
    longer['date'] += pd.Timedelta(days=90)
    score_a = rvm.build_summary(v).risk_score.iloc[0]
    score_b = rvm.build_summary(pd.concat([v, longer], ignore_index=True)).risk_score.iloc[0]
    assert score_a == score_b
    small = rvm.build_summary(v.iloc[:10])
    assert not small.rank_eligible.iloc[0]
    assert pd.isna(small.risk_score.iloc[0])


def test_duplicate_daily_keys_rejected(tmp_path):
    daily = make_daily(n_products=1)
    path = tmp_path / 'duplicate.csv'
    pd.concat([daily, daily.iloc[:1]]).to_csv(path, index=False)
    with pytest.raises(ValueError, match='重复'):
        rvm.load_data(path)


def test_flat_history_spike_is_not_silently_lost():
    daily = make_daily(n_products=1, n_days=40)
    daily['avg_price'] = 1.0
    daily.loc[39, 'avg_price'] = 1.1
    v = rvm.classify_anomalies(rvm.compute_features(daily))
    assert v.loc[38, 'rolling_z'] == 0
    assert v.loc[39, 'price_anom']
    assert not v.loc[39, 'surge']


def test_notification_requires_explicit_opt_in(monkeypatch, tmp_path):
    daily = make_daily(n_products=1)
    path = tmp_path / 'prices.csv'
    daily.to_csv(path, index=False)
    calls = []
    monkeypatch.setattr(rvm, 'send_alert', lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(sys, 'argv', ['monitor', '--data-path', str(path), '--out-dir', str(tmp_path)])
    assert rvm.main() == 0
    assert calls == []
    monkeypatch.setattr(sys, 'argv', ['monitor', '--data-path', str(path), '--out-dir', str(tmp_path), '--notify-dry-run', '--since', '2027-01-01'])
    assert rvm.main() == 0
    assert calls == []


@pytest.mark.parametrize('value, expected', [('鲁冀', '冀/鲁'), ('鲁/冀', '冀/鲁'), ('冀、冀', '冀'), ('山东/河北', '山东/河北'), ('东北', '东北')])
def test_origin_codes_are_sets_without_guessing_unknown_names(value, expected):
    assert rvm.normalize_origin(value) == expected


def test_notification_contains_business_summary_and_actual_path(tmp_path):
    v = rvm.classify_anomalies(rvm.compute_features(make_daily()))
    report = tmp_path / 'custom-output' / 'business_summary.md'
    message = rvm.build_alert_message(v, summary=rvm.build_summary(v), report_path=report)
    assert '业务摘要' in message
    assert '覆盖 3 个品种' in message
    assert '历史复核重点' in message
    assert '建议：' in message
    assert str(report.resolve()) in message
    assert len(message.encode('utf-8')) <= 2048


def test_chinese_notification_respects_utf8_limit(tmp_path):
    v = rvm.classify_anomalies(rvm.compute_features(make_daily()))
    v['product'] = '超长中文名称' * 100
    message = rvm.build_alert_message(v, top_n=100, report_path=tmp_path / 'business_summary.md')
    assert len(message.encode('utf-8')) <= 2048
    assert 'business_summary.md' in message
    assert '已截断' in message


def test_daily_notification_sent_even_when_no_anomalies(monkeypatch, tmp_path):
    daily = make_daily(n_products=1)
    daily['avg_price'] = 2.
    path = tmp_path / 'prices.csv'
    daily.to_csv(path, index=False)
    calls = []
    monkeypatch.setattr(rvm, 'send_alert', lambda message, **kw: calls.append(message) or True)
    monkeypatch.setattr(sys, 'argv', ['monitor', '--data-path', str(path), '--out-dir', str(tmp_path), '--notify'])
    assert rvm.main() == 0
    assert len(calls) == 1
    assert '2026-03-31 ~ 2026-03-31' in calls[0]
    assert '本期未触发异常规则' in calls[0]
    assert '待复核 0 条' in calls[0]


def test_robot_business_errors_are_not_treated_as_success(monkeypatch, caplog):
    import json
    class Response:
        def __init__(self, result): self.result = result
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps(self.result).encode()
    monkeypatch.setattr(rvm.urllib.request, 'urlopen', lambda *a, **kw: Response({'errcode': 93000}))
    assert not rvm.send_alert('日报', webhook_url='https://example.test/webhook?key=private-placeholder')
    assert 'private-placeholder' not in caplog.text
    monkeypatch.setattr(rvm.urllib.request, 'urlopen', lambda *a, **kw: Response({'errcode': 0}))
    assert rvm.send_alert('日报', webhook_url='https://example.test/webhook')


def test_task_returns_failure_when_push_is_rejected(monkeypatch, tmp_path):
    path = tmp_path / 'prices.csv'
    make_daily().to_csv(path, index=False)
    monkeypatch.setattr(rvm, 'send_alert', lambda *a, **kw: False)
    monkeypatch.setattr(sys, 'argv', ['monitor', '--data-path', str(path), '--out-dir', str(tmp_path), '--notify'])
    assert rvm.main() == 1
