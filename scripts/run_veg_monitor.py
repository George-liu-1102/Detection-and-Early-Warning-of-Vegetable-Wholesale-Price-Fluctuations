#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""蔬菜批发价格波动检测与异常告警（真实数据版）。

数据源：北京新发地批发市场公开行情（来源与清洗见 docs/DATA_SOURCES.md）
流程：读取日度数据 -> 计算环比/滚动统计 -> 异常判定 -> 输出 CSV / HTML 报告 /
      业务摘要 -> 可选推送告警（钉钉 / 企业微信机器人 Webhook）。

用法示例：
  python scripts/run_veg_monitor.py --generate-report
  python scripts/run_veg_monitor.py --generate-report --notify-dry-run
  python scripts/run_veg_monitor.py --notify --since 2026-07-01 --notify-url "<webhook>"
"""

import argparse
import json
import logging
import re
import sys
import urllib.request
from html import escape
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # 无界面环境
import matplotlib.pyplot as plt
import matplotlib as mpl
import numpy as np
import pandas as pd

mpl.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC"]
mpl.rcParams["axes.unicode_minus"] = False

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data" / "veg_wholesale_xinfadi.csv"
DEFAULT_OUT = ROOT / "outputs"
DISPLAY_TZ = "Asia/Shanghai"

# 检测参数（可调）
WINDOW = 30                # 滚动均值/标准差窗口（天）
MIN_PERIODS = 15           # 滚动统计最少天数
VOL_WINDOW = 14            # 波动率滚动窗口（天）
VOL_MIN_PERIODS = 7
Z_THRESHOLD = 3.0          # 价格异常 z-score 阈值
SUSTAINED_Z = 2.0          # 持续高位 z-score 阈值
SUSTAINED_DAYS = 3         # 持续天数
SURGE_PCT = 0.15           # 单日急涨阈值（+15%）
CRASH_PCT = -0.12          # 单日急跌阈值（-12%）
ORIGIN_PCT = 0.05          # 产地切换伴随价格异动的价格变化阈值


def setup_logging(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    log_file = out_dir / "veg_monitor.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return log_file


def load_data(path: Path) -> pd.DataFrame:
    """读取日度数据集并做基础校验。"""
    df = pd.read_csv(path, parse_dates=["date"])
    required = {"date", "product", "avg_price", "origin"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"数据缺少必要列：{missing}")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    if df.empty or df["date"].isna().any() or df["product"].isna().any():
        raise ValueError("数据为空或日期/品种缺失")
    df["avg_price"] = pd.to_numeric(df["avg_price"], errors="coerce")
    if not np.isfinite(df["avg_price"]).all() or (df["avg_price"] <= 0).any():
        raise ValueError("数据中存在缺失或非正的 avg_price，请先运行清洗脚本")
    if df.duplicated(["date", "product"]).any():
        raise ValueError("存在重复的 (date, product)，请先聚合数据")
    df["origin"] = df["origin"].map(normalize_origin)
    df = df.sort_values(["product", "date"]).reset_index(drop=True)
    logging.info("加载数据：%d 行，%d 个品种，%s ~ %s",
                 len(df), df["product"].nunique(), df["date"].min().date(), df["date"].max().date())
    return df


def normalize_origin(value) -> str:
    """统一缺失值、分隔符及已知省级简称的集合；保留未知地名。"""
    if pd.isna(value):
        return ""
    provinces = set("京津冀晋蒙辽吉黑沪苏浙皖闽赣鲁豫鄂湘粤桂琼渝川贵云藏陕甘青宁新")
    places = set()
    for token in filter(None, re.split(r"[/、,，\s]+", str(value).strip())):
        places.update(token if set(token).issubset(provinces) else [token])
    return "/".join(sorted(places))


def compute_features(daily: pd.DataFrame) -> pd.DataFrame:
    """只用当日以前的日历窗口；不补价、不把跨日涨跌当作日环比。"""
    if daily.duplicated(["product", "date"]).any():
        raise ValueError("存在重复的 (date, product)")
    parts = []
    for _, part in daily.sort_values(["product", "date"]).groupby("product", sort=False):
        part = part.copy().set_index("date")
        part["origin"] = part["origin"].map(normalize_origin)
        part["gap_days"] = part.index.to_series().diff().dt.days
        part["observed_pct_ch"] = part["avg_price"].pct_change(fill_method=None)
        part["price_pct_ch"] = part["observed_pct_ch"].where(part["gap_days"].eq(1))
        history = part["avg_price"].rolling(f"{WINDOW}D", closed="left", min_periods=MIN_PERIODS)
        part["history_n"] = part["avg_price"].rolling(f"{WINDOW}D", closed="left", min_periods=0).count()
        part["rolling_mean"] = history.mean()
        part["rolling_std"] = history.std()
        part["rolling_z"] = (part["avg_price"] - part["rolling_mean"]) / part["rolling_std"].replace(0, np.nan)
        flat = part["rolling_std"].eq(0)
        delta = part["avg_price"] - part["rolling_mean"]
        part.loc[flat & delta.eq(0), "rolling_z"] = 0.0
        part.loc[flat & delta.ne(0), "rolling_z"] = np.sign(delta[flat & delta.ne(0)]) * np.inf
        part["rolling_vol"] = part["price_pct_ch"].rolling(
            f"{VOL_WINDOW}D", min_periods=VOL_MIN_PERIODS).std()
        part["origin_prev"] = part["origin"].shift(1).fillna("")
        part["origin_comparable"] = (
            part["gap_days"].eq(1) & part["origin"].ne("") & part["origin_prev"].ne(""))
        part["origin_changed"] = part["origin_comparable"] & part["origin"].ne(part["origin_prev"])
        parts.append(part.reset_index())
    return pd.concat(parts, ignore_index=True)


def _sustained_flags(cond: pd.Series, min_days: int, gap_days=None) -> pd.Series:
    """把连续 True 达到 min_days 的位置标记为 True。"""
    run = 0
    flags = []
    gaps = [1] * len(cond) if gap_days is None else gap_days
    for x, gap in zip(cond.fillna(False), gaps):
        if pd.isna(gap) or gap != 1:
            run = 0
        run = run + 1 if x else 0
        flags.append(run >= min_days)
    return pd.Series(flags, index=cond.index)


def classify_anomalies(v: pd.DataFrame,
                       z_threshold: float = Z_THRESHOLD,
                       surge_pct: float = SURGE_PCT,
                       crash_pct: float = CRASH_PCT,
                       origin_pct: float = ORIGIN_PCT) -> pd.DataFrame:
    """异常判定：价格异常 / 急涨 / 急跌 / 产地切换异动 / 持续高位。"""
    v = v.copy()
    v["price_anom"] = v["rolling_z"].abs() > z_threshold
    v["surge"] = v["price_pct_ch"] >= surge_pct
    v["crash"] = v["price_pct_ch"] <= crash_pct
    v["origin_shift_alert"] = v["origin_changed"] & (v["price_pct_ch"].abs() >= origin_pct)
    v["sustained_high"] = False
    for _, part in v.groupby("product", sort=False):
        v.loc[part.index, "sustained_high"] = _sustained_flags(
            part["rolling_z"].gt(SUSTAINED_Z), SUSTAINED_DAYS, part["gap_days"])
    v["anomaly_any"] = v[["price_anom", "surge", "crash", "origin_shift_alert", "sustained_high"]].any(axis=1)
    return v


def build_summary(v: pd.DataFrame) -> pd.DataFrame:
    """按品种汇总波动与异常情况。"""
    latest = (v.sort_values("date").groupby("product").tail(1)
                [["product", "avg_price", "rolling_z", "origin"]]
                .rename(columns={"avg_price": "latest_price",
                                 "rolling_z": "latest_z",
                                 "origin": "latest_origin"}))
    s = v.groupby("product").agg(
        n_days=("date", "count"),
        first_date=("date", "min"),
        last_date=("date", "max"),
        n_daily_comparable=("price_pct_ch", "count"),
        n_z_comparable=("rolling_z", "count"),
        n_origin_comparable=("origin_comparable", "sum"),
        mean_price=("avg_price", "mean"),
        std_price=("avg_price", "std"),
        avg_rolling_vol=("rolling_vol", "mean"),
        max_abs_z=("rolling_z", lambda s: s.abs().max()),
        n_price_anom=("price_anom", "sum"),
        n_surge=("surge", "sum"),
        n_crash=("crash", "sum"),
        n_origin_shift=("origin_shift_alert", "sum"),
        n_sustained=("sustained_high", "sum"),
        n_anomalies=("anomaly_any", "sum"),
    ).reset_index()

    s["anomaly_rate"] = (s["n_anomalies"] / s["n_days"]).round(4)
    for numerator, denominator, name in [
        ("n_price_anom", "n_z_comparable", "price_anom_rate"),
        ("n_surge", "n_daily_comparable", "surge_rate"),
        ("n_crash", "n_daily_comparable", "crash_rate"),
        ("n_origin_shift", "n_origin_comparable", "origin_shift_rate"),
        ("n_sustained", "n_z_comparable", "sustained_rate"),
    ]:
        s[name] = s[numerator] / s[denominator].replace(0, np.nan)
    s["coverage_rate"] = s["n_days"] / ((s["last_date"] - s["first_date"]).dt.days + 1)
    s["rank_eligible"] = (s["n_days"].ge(30) & s["n_z_comparable"].ge(15)
                          & s["n_daily_comparable"].ge(15) & s["avg_rolling_vol"].notna())
    # 每个有效比较日的加权信号率；缺少产地比较时该分项不贡献分数。
    s["risk_score"] = (s["avg_rolling_vol"] * 10 + 100 * (
        3 * s["price_anom_rate"].fillna(0) + s["surge_rate"].fillna(0)
        + s["crash_rate"].fillna(0) + 2 * s["origin_shift_rate"].fillna(0)
        + 2 * s["sustained_rate"].fillna(0))).where(s["rank_eligible"]).round(2)
    s = s.merge(latest, on="product", validate="one_to_one")
    return s.sort_values("risk_score", ascending=False).reset_index(drop=True)


def save_outputs(v: pd.DataFrame, summary: pd.DataFrame, out_dir: Path, since=None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    v.to_csv(out_dir / "veg_volatility_full.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(out_dir / "veg_summary.csv", index=False, encoding="utf-8-sig")

    if since is not None:
        v = v[v["date"] >= pd.Timestamp(since)]

    def dump(flag, name, sort_col=None):
        sub = v[v[flag]]
        if sort_col:
            sub = sub.sort_values(sort_col, key=lambda s: s.abs(), ascending=False)
        sub.to_csv(out_dir / name, index=False, encoding="utf-8-sig")
        logging.info("输出 %s：%d 条", name, len(sub))

    dump("price_anom", "veg_anomalies_price.csv")
    dump("surge", "veg_anomalies_surge.csv", "price_pct_ch")
    dump("crash", "veg_anomalies_crash.csv", "price_pct_ch")
    dump("origin_shift_alert", "veg_anomalies_origin_shift.csv", "price_pct_ch")
    dump("sustained_high", "veg_anomalies_sustained.csv", "rolling_z")
    dump("anomaly_any", "veg_anomalies_all.csv", "rolling_z")


def write_business_summary(v: pd.DataFrame, summary: pd.DataFrame, out_dir: Path, since=None) -> Path:
    """自动生成业务摘要（Markdown），便于直接阅读与汇报。"""
    if since is not None:
        v_recent = v[v["date"] >= pd.Timestamp(since)]
    else:
        v_recent = v

    top_vol = summary[summary["rank_eligible"]].head(10)
    monthly = (v_recent.groupby(v_recent["date"].dt.to_period("M"))
                .agg(n_records=("date", "size"), n_anomalies=("anomaly_any", "sum"),
                     n_surge=("surge", "sum"),
                     n_crash=("crash", "sum"))
                .reset_index())
    monthly["month"] = monthly["date"].astype(str)
    monthly["anomaly_rate"] = monthly["n_anomalies"] / monthly["n_records"]

    recent = v_recent[v_recent["anomaly_any"]].sort_values("date", ascending=False).head(20)

    lines = [
        "# 蔬菜批发价格波动监测——业务摘要",
        "",
        f"生成时间：{pd.Timestamp.now(tz=DISPLAY_TZ):%Y-%m-%d %H:%M}（Asia/Shanghai）",
        "",
        "## 一、监测概况",
        f"- 监测范围：北京新发地批发市场，{v['date'].min().date()} ~ {v['date'].max().date()}",
        f"- 覆盖品种：{v['product'].nunique()} 个；日度记录：{v['date'].nunique()} 天",
        "- 信号可重叠，异常总数按品种日去重；缺失日期不填充价格。",
        "- 日环比仅比较相邻日历天；z-score 使用当日之前 30 个日历天（至少 15 条有效报价）。",
        "- 风险评分为加权信号率，不是损失概率；样本不足的品种不参与排名。",
        f"- 异常事件总数：{int(v_recent['anomaly_any'].sum())} 条（"
        f"价格异常 {int(v_recent['price_anom'].sum())}、急涨 {int(v_recent['surge'].sum())}、"
        f"急跌 {int(v_recent['crash'].sum())}、产地切换异动 {int(v_recent['origin_shift_alert'].sum())}、"
        f"持续高位 {int(v_recent['sustained_high'].sum())}）",
        "",
        "## 二、高波动品种 Top 10（风险评分）",
        "",
        "| 品种 | 平均价格(元/斤) | 平均日波动率 | 异常次数 | 风险评分 |",
        "|---|---|---|---|---|",
    ]
    for _, r in top_vol.iterrows():
        lines.append(f"| {r['product']} | {r['mean_price']:.2f} | {r['avg_rolling_vol']:.3f} | {int(r['n_anomalies'])} | {r['risk_score']} |")

    lines += ["", "## 三、月度异常分布", "", "| 月份 | 有效记录 | 异常总数 | 异常率 | 急涨 | 急跌 |", "|---|---|---|---|---|---|"]
    for _, r in monthly.iterrows():
        lines.append(f"| {r['month']} | {int(r['n_records'])} | {int(r['n_anomalies'])} | {r['anomaly_rate']:.1%} | {int(r['n_surge'])} | {int(r['n_crash'])} |")

    lines += ["", "## 四、最近异常事件（按日期倒序）", "", "| 日期 | 品种 | 均价(元/斤) | 日环比 | 异常类型 |", "|---|---|---|---|---|"]
    for _, r in recent.iterrows():
        types = [t for t, flag in [("价格异常", r["price_anom"]), ("急涨", r["surge"]),
                                   ("急跌", r["crash"]), ("产地切换", r["origin_shift_alert"]),
                                   ("持续高位", r["sustained_high"])] if flag]
        lines.append(f"| {r['date'].date()} | {r['product']} | {r['avg_price']:.2f} | "
                     f"{r['price_pct_ch'] * 100:.1f}% | {'、'.join(types)} |")

    lines += [
        "",
        "## 五、业务建议（供采购/销售参考）",
        "",
        "1. **先核实数据**：检查报价日期、规格、单位及缺失产地；不把跨日变化当作单日急涨急跌。",
        "2. **急涨/急跌**：复核供应商报价并结合实际库存、采购需求与损耗决定动作。",
        "3. **产地切换**：仅双方产地已知且日期连续时复核货源结构；不能直接推断运费或供给变化。",
        "4. **持续高位**：连续日历天偏离近期价格中枢，可加入采购复核队列。",
        "5. **解释边界**：天气、春节、供需因素只是待检验假设；当前数据不支持因果或年度季节性结论。",
        "",
        "> 说明：以上建议基于统计规则自动生成，供人工复核参考，不构成交易指令。",
    ]
    path = out_dir / "business_summary.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    logging.info("业务摘要已生成：%s", path)
    return path


def generate_report(v: pd.DataFrame, summary: pd.DataFrame, out_dir: Path, top_k: int = 12) -> Path:
    """生成 HTML 报告与关键图表。"""
    fig_dir = out_dir / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)

    top = summary[summary["rank_eligible"]].head(top_k)
    generated = []
    for _, r in top.iterrows():
        product = r["product"]
        sel = v[v["product"] == product].sort_values("date")
        if len(sel) < MIN_PERIODS:
            continue
        fname = f"{product}.png"
        plt.figure(figsize=(10, 4))
        plt.plot(sel["date"], sel["avg_price"], label="均价", linewidth=1.2)
        plt.plot(sel["date"], sel["rolling_mean"], label=f"此前{WINDOW}日均值", linestyle="--", linewidth=1)
        anom = sel[sel["price_anom"]]
        if not anom.empty:
            plt.scatter(anom["date"], anom["avg_price"], color="red", s=22, label="价格异常", zorder=5)
        surge = sel[sel["surge"]]
        if not surge.empty:
            plt.scatter(surge["date"], surge["avg_price"], color="orange", s=14, marker="^", label="急涨", zorder=4)
        crash = sel[sel["crash"]]
        if not crash.empty:
            plt.scatter(crash["date"], crash["avg_price"], color="green", s=14, marker="v", label="急跌", zorder=4)
        plt.title(f"{product}（风险评分 {r['risk_score']}）")
        plt.xlabel("日期")
        plt.ylabel("均价（元/斤）")
        plt.legend(fontsize=8, ncol=4)
        plt.tight_layout()
        plt.savefig(fig_dir / fname, dpi=150)
        plt.close()
        generated.append((product, fname))

    # 月度异常条形图
    monthly = (v.groupby(v["date"].dt.to_period("M"))
                .agg(n_records=("date", "size"), n_anomalies=("anomaly_any", "sum"),
                     n_surge=("surge", "sum"),
                     n_crash=("crash", "sum"))
                .reset_index())
    monthly["month"] = monthly["date"].astype(str)
    fig, (ax_count, ax_rate) = plt.subplots(1, 2, figsize=(12, 4))
    x = np.arange(len(monthly))
    ax_count.bar(x - 0.2, monthly["n_surge"], width=0.4, label="急涨", color="orange")
    ax_count.bar(x + 0.2, monthly["n_crash"], width=0.4, label="急跌", color="green")
    ax_count.set_xticks(x, monthly["month"], rotation=45)
    ax_count.set_title("月度急涨/急跌事件数")
    ax_count.legend()
    ax_rate.bar(x, 100 * monthly["n_anomalies"] / monthly["n_records"], color="#4C72B0")
    ax_rate.set_xticks(x, monthly["month"], rotation=45)
    ax_rate.set_title("月度异常率（按实际品种日报价）")
    ax_rate.set_ylabel("异常率（%）")
    fig.tight_layout()
    fig.savefig(fig_dir / "monthly_anomalies.png", dpi=150)
    plt.close(fig)

    html_path = out_dir / "veg_report_index.html"
    latest_day = v["date"].max()
    queue = v[v["date"].eq(latest_day) & v["anomaly_any"]].sort_values(
        "price_pct_ch", key=lambda s: s.abs(), ascending=False)
    with html_path.open("w", encoding="utf-8") as f:
        f.write("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                "<title>采购报价复核看板</title><style>"
                "body{font:16px system-ui;max-width:1100px;margin:32px auto;padding:0 20px;color:#243447;background:#f7f9fc}"
                ".table-wrap{overflow-x:auto;max-width:100%}table{border-collapse:collapse;width:100%;min-width:650px;background:white}"
                "td,th{padding:10px;border-bottom:1px solid #ddd;text-align:left}"
                "input{padding:10px;margin:12px 0}img{max-width:100%}small{color:#586a7d}"
                "</style></head><body>\n")
        f.write(f"<h2>蔬菜批发价格波动检测与异常告警</h2>\n")
        f.write(f"<p>生成时间：{pd.Timestamp.now(tz=DISPLAY_TZ):%Y-%m-%d %H:%M}（Asia/Shanghai） ｜ "
                f"数据范围：{v['date'].min().date()} ~ {v['date'].max().date()}</p>\n")
        f.write("<p>历史行情回放：本页不代表实时价格。信号仅供采购复核，未验证成本节省或误报率。</p>"
                f"<p>覆盖 {len(summary)} 个品种，{len(v):,} 条记录；"
                f"{int(summary.rank_eligible.sum())} 个品种可排名。窗口只使用此前 30 个日历天，缺价不填充。</p>"
                "<label>搜索品种 <input id='search' placeholder='如：黄瓜' type='search'></label>"
                f"<h3>数据末日 {latest_day:%Y-%m-%d}：待复核报价 {len(queue)} 条</h3>"
                "<p>复核步骤：核对报价与规格 → 联系供应商确认 → 结合库存和采购需求决定动作 → 记录结果。</p>"
                "<div class='table-wrap'><table><thead><tr><th>品种</th><th>价格（元/斤）</th><th>日环比</th><th>信号</th><th>产地</th></tr></thead><tbody>")
        flags = [("price_anom", "价格偏离"), ("surge", "急涨"), ("crash", "急跌"),
                 ("origin_shift_alert", "产地切换"), ("sustained_high", "持续高位")]
        for _, r in queue.iterrows():
            change = "不可比" if pd.isna(r.price_pct_ch) else f"{r.price_pct_ch:+.1%}"
            f.write(f"<tr data-product='{escape(r['product'], quote=True)}'><td>{escape(r['product'])}</td>"
                    f"<td>{r.avg_price:.2f}</td><td>{change}</td>"
                    f"<td>{'、'.join(label for flag, label in flags if r[flag])}</td>"
                    f"<td>{escape(r.origin) or '缺失，需补核'}</td></tr>")
        f.write(f"</tbody></table></div><h3>历史风险评分 Top {top_k}</h3>"
                "<small>使用有效比较日的信号率；权重为启发式，分数不是概率。缺少产地比较会影响可比性。"
                "样本不足的品种保留在 CSV 中，暂不排名。</small>"
                "<div class='table-wrap'><table><thead><tr><th>品种</th><th>均价(元/斤)</th><th>日波动率</th><th>异常率</th>"
                "<th>观测天数</th><th>产地可比较日</th><th>风险评分</th></tr></thead><tbody>\n")
        for _, r in top.iterrows():
            f.write(f"<tr data-product='{escape(r['product'], quote=True)}'><td>{escape(r['product'])}</td><td>{r['mean_price']:.2f}</td>"
                    f"<td>{r['avg_rolling_vol']:.3f}</td><td>{r['anomaly_rate']:.1%}</td>"
                    f"<td>{int(r['n_days'])}</td><td>{int(r['n_origin_comparable'])}</td>"
                    f"<td>{r['risk_score']}</td></tr>\n")
        f.write("</tbody></table></div>\n")
        f.write("<h3>月度异常分布</h3>\n<img src='figures/monthly_anomalies.png' alt='月度事件数与异常率' style='width:100%;max-width:820px;border:1px solid #ccc;'><br/>\n")
        f.write("<h3>重点品种走势</h3>\n")
        for product, fname in generated:
            f.write(f"<section data-product='{escape(product, quote=True)}'><h4>{escape(product)}</h4>"
                    f"<img src='figures/{escape(fname, quote=True)}' alt='{escape(product, quote=True)}价格走势'></section>\n")
        f.write("<script>document.getElementById('search').addEventListener('input',function(){"
                "document.querySelectorAll('[data-product]').forEach(el=>{el.hidden=!el.dataset.product.includes(this.value.trim());});"
                "});</script></body></html>\n")
    logging.info("HTML 报告已生成：%s", html_path)
    return html_path


def build_alert_message(v: pd.DataFrame, top_n: int = 5, summary=None, report_path=None) -> str:
    """直接发送可阅读的业务摘要；文本不超过企业微信的 2048 字节限制。"""
    recent = v[v["anomaly_any"]].sort_values("date", ascending=False).head(top_n)
    lines = [
        f"【蔬菜报价业务摘要】{pd.Timestamp.now(tz=DISPLAY_TZ):%m-%d %H:%M}（Asia/Shanghai）",
        f"报价日期：{v['date'].min().date()} ~ {v['date'].max().date()}",
        f"覆盖 {v['product'].nunique()} 个品种、{len(v)} 条报价；待复核 {int(v['anomaly_any'].sum())} 条。",
        (f"信号：价格偏离 {int(v['price_anom'].sum())}、急涨 {int(v['surge'].sum())}、"
         f"急跌 {int(v['crash'].sum())}、产地切换 {int(v['origin_shift_alert'].sum())}、"
         f"持续高位 {int(v['sustained_high'].sum())}（类型可重叠）。"),
        f"最近异常（最多 {top_n} 条）：" if not recent.empty else "本期未触发异常规则。",
    ]
    for _, r in recent.iterrows():
        types = [t for t, flag in [("价格异常", r["price_anom"]), ("急涨", r["surge"]),
                                   ("急跌", r["crash"]), ("产地切换", r["origin_shift_alert"]),
                                   ("持续高位", r["sustained_high"])] if flag]
        change = "日环比不可比" if pd.isna(r['price_pct_ch']) else f"日环比 {r['price_pct_ch']:+.1%}"
        lines.append(
            f"- {r['date'].date()} {r['product']}：{r['avg_price']:.2f} 元/斤，"
            f"{change}（{'、'.join(types)}）"
        )
    if summary is not None:
        top = summary[summary['rank_eligible']].head(3)
        if not top.empty:
            lines.append("历史复核重点：" + "、".join(top['product']) + "（全窗口信号率排名）。")
    lines.append("建议：先核对日期、规格/单位与供应商报价，再结合库存和需求决定采购。")
    path = Path(report_path) if report_path is not None else DEFAULT_OUT / 'business_summary.md'
    footer = "\n完整 business_summary.md 在任务运行电脑：\n" + str(path.resolve())
    # 预留字节给文件位置，避免中文按字符截断后超过机器人文本长度上限。
    suffix = "\n（明细较长，已截断；完整内容见本地文件）"
    body = '\n'.join(lines)
    if len((body + footer).encode('utf-8')) > 1900:
        budget = max(0, 1900 - len((suffix + footer).encode('utf-8')))
        body = body.encode('utf-8')[:budget].decode('utf-8', errors='ignore') + suffix
    return body + footer


def send_alert(message: str, webhook_url: str = None, dry_run: bool = False) -> bool:
    """推送告警到 Webhook（钉钉/企业微信文本消息通用格式）。"""
    import os
    url = webhook_url or os.environ.get("DINGTALK_WEBHOOK") or os.environ.get("WECHAT_WORK_WEBHOOK")
    if dry_run:
        logging.info("[告警预览] 未实际发送。\n%s", message)
        return True
    if not url:
        logging.warning("未配置 Webhook（--notify-url 或环境变量 DINGTALK_WEBHOOK / WECHAT_WORK_WEBHOOK），跳过推送")
        return False
    payload = json.dumps({"msgtype": "text", "text": {"content": message}}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
        result = json.loads(body)
        if result.get('errcode') != 0:
            logging.error("机器人拒绝推送，errcode=%s", result.get('errcode', 'missing'))
            return False
        logging.info("业务摘要推送成功")
        return True
    except Exception as exc:  # noqa: BLE001
        # 网络异常可能含带凭据的 Webhook URL，只记录错误类型。
        logging.error("告警推送失败（%s），请检查网络与 Webhook 配置", type(exc).__name__)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="蔬菜批发价格波动检测与异常告警")
    parser.add_argument("--data-path", type=Path, default=DEFAULT_DATA, help="输入数据 CSV")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT, help="输出目录")
    parser.add_argument("--generate-report", action="store_true", help="生成 HTML 报告与图表")
    parser.add_argument("--top-k", type=int, default=12, help="报告重点品种数量")
    parser.add_argument("--z-threshold", type=float, default=Z_THRESHOLD, help="价格异常 z 阈值")
    parser.add_argument("--surge-pct", type=float, default=SURGE_PCT, help="急涨阈值")
    parser.add_argument("--crash-pct", type=float, default=CRASH_PCT, help="急跌阈值")
    parser.add_argument("--since", default=None, help="只输出该日期（YYYY-MM-DD）之后的异常（用于每日增量告警）")
    parser.add_argument("--notify-url", default=None, help="钉钉/企业微信 Webhook URL")
    parser.add_argument("--notify-dry-run", action="store_true", help="仅预览告警消息，不实际推送")
    parser.add_argument("--notify", action="store_true", help="显式启用 Webhook 推送")
    args = parser.parse_args()

    log_file = setup_logging(args.out_dir)
    try:
        logging.info("启动监测脚本")
        df = load_data(args.data_path)
        v = compute_features(df)
        v = classify_anomalies(v, z_threshold=args.z_threshold,
                               surge_pct=args.surge_pct, crash_pct=args.crash_pct)
        summary = build_summary(v)
        save_outputs(v, summary, args.out_dir, since=args.since)
        summary_path = write_business_summary(v, summary, args.out_dir, since=args.since)

        if args.generate_report:
            generate_report(v, summary, args.out_dir, top_k=args.top_k)

        alert_data = (v[v['date'].eq(v['date'].max())] if args.since is None
                      else v[v["date"] >= pd.Timestamp(args.since)])
        if not alert_data.empty and (args.notify or args.notify_dry_run):
            message = build_alert_message(alert_data, summary=summary, report_path=summary_path)
            if args.notify_dry_run:
                send_alert(message, dry_run=True)
            else:
                if not send_alert(message, webhook_url=args.notify_url):
                    return 1
        elif not alert_data["anomaly_any"].any():
            logging.info("本期无异常事件")

        logging.info("全部完成，日志：%s", log_file)
        return 0
    except Exception as exc:  # noqa: BLE001
        logging.error("运行失败：%s", exc)
        raise


if __name__ == "__main__":
    sys.exit(main())
