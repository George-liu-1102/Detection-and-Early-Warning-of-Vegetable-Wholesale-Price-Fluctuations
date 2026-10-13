# 蔬菜批发报价异常检测与采购复核

基于北京新发地公开报价，回答“采购每天应优先复核哪些品种”。
包含采集清洗、数据质检、日历时间统计、异常检测、阈值回放、SQL 校验和可搜索 HTML 看板。
这是历史行情分析项目，尚未验证提前预测能力或真实采购收益。

- 数据：2026-01-01 至 2026-08-11，22,362 条品种日报价、145 个品种、216 个有报价日期。
- 默认规则：2,607 个异常品种日，产地切换异动 125 条；各信号允许重叠。
- 评估：九组预设阈值 × 三个时间分段，120 条标签留空的人工复核样本。
- 证据：[业务报告](docs/业务分析报告.md)、[分析 Notebook](notebooks/eda.ipynb)、[阈值评估](outputs/evaluation_summary.md)、[看板](outputs/veg_report_index.html)。

## 快速开始

推荐 Python 3.11 或 3.12，无数据库服务、GPU 或 Webhook 也可完成离线分析。

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell：.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt -r requirements-dev.txt

# 数据已随仓库提供；默认只生成结果，不发送通知
python scripts/run_veg_monitor.py --generate-report
python scripts/evaluate_monitor.py
python scripts/run_sql_analysis.py
python -m pytest tests -q
```

用浏览器打开 `outputs/veg_report_index.html`，可搜索品种、查看数据末日复核队列与历史风险。
输出可用 `--out-dir` 改到其他目录；重新生成会覆盖该目录的对应产物，人工标签请单独保存。

可选 Notebook：

```bash
python -m pip install -r requirements-notebook.txt
jupyter lab notebooks/eda.ipynb
# 完整执行并写回结果
jupyter nbconvert --to notebook --execute notebooks/eda.ipynb --inplace --ExecutePreprocessor.timeout=180
```

Linux 生成中文图表需要中文字体，例如 Noto Sans CJK；无字体不影响数值分析。

## 检测口径

| 指标 | 定义 |
|---|---|
| 日环比 | 仅相邻日历天计算；跨日变动另存 observed_pct_ch，不能触发单日规则 |
| 价格偏离 | 当日价格相对 [t−30日, t) 的均值/样本标准差，至少15条报价，绝对 z-score >3 |
| 零方差基准 | 历史价格不变且当日不变，z=0；当日变化记有符号无穷值；历史不足不可评估 |
| 急涨 / 急跌 | 有效日环比 ≥+15% / ≤−12% |
| 产地切换异动 | 连续两天、双方产地已知、规范化产地集合不同，且价格变化绝对值 ≥5% |
| 持续高位 | z>2 连续至少三个日历天；缺失日期中断 |
| 日波动率 | 最近14个日历天有效日环比的样本标准差，至少7次比较 |

风险评分 = 10×平均日波动率 + 100×(3×价格异常率 + 急涨率 + 急跌率 + 2×产地切换率 + 2×持续高位率)。
价格异常和持续高位分母为有效 z-score 日；急涨/急跌分母为有效日环比日；产地信号分母为产地可比较日。
没有产地比较时该分项贡献为零，并披露分母。至少30条报价、15次日环比、15次有效z-score及可用波动率才参与排名。
评分权重为启发式，尚未按真实业务标签校准；不能解释为损失概率。

## 评估与 SQL

`evaluate_monitor.py` 固定九组阈值，分别统计1–5月开发期、6月验证期与7月–8月11日留出期。
比较异常率、日均复核数与 P95，每条信号只使用当时可用历史；没有挑选最优组合。
后续第3日变化比例只做事后描述，缺失报价不填充，不参与异常生成，也不等同于准确率。

`manual_review_sample.csv` 分层抽取120个不同品种日，真实标签需人工填写。
分层样本不能直接估计总体精确率，正式效果验证仍需独立评估集。

[SQL 查询](sql/analysis.sql)展示重复导入去重、LAG日环比、月度异常率与连续异常区间。
`run_sql_analysis.py` 使用 SQLite 内存数据库运行，并对全部日环比与月度汇总执行 Python/SQL 一致性检查，无额外数据库依赖。

## 数据更新、通知与调度

```bash
# 重新抓取会访问新发地网站，本轮修复未联网验证抓取接口
python scripts/fetch_xinfadi_data.py --start 2026/01/01 --end 2026/08/11
python scripts/build_xinfadi_dataset.py

# 用已有蔬菜原始子集重新清洗，无需联网
python scripts/build_xinfadi_dataset.py --raw data/raw/xinfadi_vegetables_raw.jsonl

# 预览，不发送
python scripts/run_veg_monitor.py --notify-dry-run
# 确认配置 DINGTALK_WEBHOOK 或 WECHAT_WORK_WEBHOOK 后，显式发送
python scripts/run_veg_monitor.py --notify --since 2026-08-11
```

普通运行不会因为已有 Webhook 环境变量就自动发送；通知和异常文件均受 `--since` 过滤。
未指定 `--since` 时，日报只统计数据末日，零异常也有摘要。企业微信消息直接展示业务摘要，不上传本地 Markdown 文件。
Webhook 含凭据，请在本地环境安全设置，不要写入仓库。
Windows 调度脚本 `scripts/schedule_windows.ps1` 仅定时分析现有 CSV，并默认显式启用通知；它不自动更新行情。
实际推送、在线抓取与 Windows 调度未在本次 Linux 验证中执行。
文件位置、任务目录和更新方法见 [Windows 每日任务排查](docs/Windows任务排查.md)。

## 局限与求职展示

单市场、不到一年、无成交量；不同规格/产地的非加权均值可能受样本构成影响。
缺失日期与产地已在检测中处理，但仍限制比较；不把春节、天气或供需解释写成已验证因果。

简历可据实描述：基于22,362条真实报价完成采集清洗、异常复核分析，修复缺失值与日历窗口口径；
使用SQL窗口函数进行指标分析和跨工具校验，完成九组阈值的时间分段回放与交互报告。
没有真实上线数据时，不写预测准确率、降本比例或效率提升。

MIT License。
