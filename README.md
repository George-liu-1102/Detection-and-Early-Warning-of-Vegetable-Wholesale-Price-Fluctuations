# 蔬菜批发价格波动检测与异常告警

基于**北京新发地批发市场公开行情数据**，对 145 个蔬菜品种进行
日度价格波动检测与异常告警，产出异常明细、HTML 报告、业务摘要，并支持
钉钉/企业微信机器人推送与每日定时运行。

- 数据规模：2026-01-01 ~ 2026-08-11，22,362 条日度记录，145 个品种
- 检测方法：30 日滚动 z-score 价格异常 + 单日急涨/急跌 + 产地切换异动 + 持续高位
- 产出物：异常 CSV、业务摘要（Markdown）、HTML 报告与图表
- 工程配套：单元测试、CI、断点续传抓取脚本、Windows 计划任务

## 项目结构

```text
.
├── data/
│   ├── raw/                      # 原始数据（JSONL，蔬菜子集入库）
│   ├── veg_wholesale_xinfadi.csv # 清洗后分析数据（主数据集）   
├── scripts/
│   ├── fetch_xinfadi_data.py     # 抓取新发地公开行情（断点续传）
│   ├── build_xinfadi_dataset.py  # 清洗并生成分析数据集
│   ├── run_veg_monitor.py        # 核心监控：检测/报表/摘要/告警
│   └── schedule_windows.ps1      # 注册 Windows 每日定时任务
├── notebooks/eda.ipynb           # 可复现的分析 notebook（含结论）
├── outputs/                      # 运行产物（CSV/HTML/图表/摘要）
├── docs/
│   ├── DATA_SOURCES.md           # 数据来源与清洗说明
│   └── 业务分析报告.md            # 业务视角的分析结论
├── tests/test_monitor.py         # 单元测试
└── .github/workflows/ci.yml      # CI（测试 + 冒烟运行）
```

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 运行监控（生成 CSV、业务摘要、HTML 报告与图表）
python scripts/run_veg_monitor.py --generate-report

# 3. 打开报告
start outputs/veg_report_index.html     # Windows
open outputs/veg_report_index.html      # macOS

# 4. 运行 notebook（含图表与业务解读）
jupyter notebook notebooks/eda.ipynb

# 5. 运行测试
pip install -r requirements-dev.txt
pytest tests -q
```

### 重新抓取/更新数据

```bash
python scripts/fetch_xinfadi_data.py --start 2026/01/01 --end 2026/08/11
python scripts/build_xinfadi_dataset.py
```

详细说明见 [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md)。

## 检测方法

| 信号 | 规则 | 说明 |
|---|---|---|
| 价格异常 | 30 日滚动 z-score 绝对值 > 3 | 与自身历史中枢比较 |
| 单日急涨 | 日环比 ≥ +15% | 短期快速上涨 |
| 单日急跌 | 日环比 ≤ -12% | 短期快速下跌 |
| 产地切换异动 | 产地变化 且 价格变化 ≥ 5% | 货源结构调整信号 |
| 持续高位 | z-score > 2 连续 ≥ 3 天 | 脱离历史中枢 |
| 风险评分 | 波动率×10 + 价格异常×3 + 急涨 + 急跌 + 产地切换×2 + 持续高位×2 | 品种横向比较 |

阈值均可在命令行调整（`--z-threshold`、`--surge-pct`、`--crash-pct` 等）。

## 告警推送与定时调度

### 告警（钉钉 / 企业微信机器人）

配置 Webhook 环境变量（任选其一），然后带 `--notify` 运行：

```powershell
# 系统环境变量示例（PowerShell）
setx DINGTALK_WEBHOOK "https://oapi.dingtalk.com/robot/send?access_token=xxx"
# 或
setx WECHAT_WORK_WEBHOOK "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"

# 预览告警内容（不实际发送）
python scripts/run_veg_monitor.py --notify-dry-run

# 实际推送
python scripts/run_veg_monitor.py --notify
```

### 每日定时（Windows）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1
```

注册后每天 08:30 自动运行并生成报告；删除任务：
`schtasks /delete /tn "VegPriceMonitor" /f`。

## 主要发现（详见 docs/业务分析报告.md）

- 风险最高的品种集中在**叶菜类**（鸡毛菜、蒿子秆、快菜等），荠菜平均日波动率达 36.5%；
- **季节性明显**：春节前后先涨后跌（3 月急跌 160 次为峰值），5-7 月汛期波动最大；
- **产地切换**占异常事件约一半，是采购复核的第一优先级信号；
- 典型事件：2026-08-01~02 菠菜 -21.2%、小白菜 -36.4%；08-11 油麦菜 +30.1% 创窗口新高。

## 数据局限

- 公开接口无成交量，不做量价联动判别；
- 仅覆盖北京新发地单市场；产地为省级简称、约 20.8% 缺失；
- 约 38 个品种覆盖不足 30 天，滚动统计不可用（仅用单日环比规则）。

## License

MIT
