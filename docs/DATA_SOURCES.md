# 数据来源、粒度与清洗口径

## 来源与文件

来源为北京新发地批发市场[价格行情公开页](https://www.xinfadi.com.cn/priceDetail.html)。
现有抓取脚本通过 `/getPriceData.html` 分页 POST 表单字段 limit、current、pubDateStartTime、pubDateEndTime。
脚本当前配置的接口地址为 `http://www.xinfadi.com.cn/getPriceData.html`；本轮只验证仓库已有数据的离线分析，未重新联网确认接口或传输方式。
历史采集说明记载全品类102,717条、蔬菜27,415条；全量文件未入库，本轮不重新验证全品类总数。

- `data/raw/xinfadi_raw.jsonl`：抓取输出的全品类文件，忽略入库；本仓库未附带。
- `data/raw/xinfadi_vegetables_raw.jsonl`：已入库蔬菜原始子集，可追溯报价字段。
- `data/veg_wholesale_xinfadi.csv`：22,362条已聚合品种日报价，145个品种。
- 日期窗口223个日历天，216个不同的有报价日期；不能将“有报价天数”等同于连续日期覆盖。

## 离线复现

```bash
python scripts/build_xinfadi_dataset.py --raw data/raw/xinfadi_vegetables_raw.jsonl
python scripts/run_veg_monitor.py --generate-report
```

清洗脚本会重写蔬菜子集与清洗后CSV；用 `--veg-raw` 和 `--out` 可导出到其他路径进行比对。
线上更新先运行抓取脚本，再清洗；抓取按原始记录id去重，分析输入按(date, product)聚合。

## 清洗与粒度

1. 仅保留 prodCat=蔬菜、unitInfo=斤；价格缺失或非正的记录剔除。
2. 日度聚合：同一(date, product)内均价取报价均值、最低/最高价取极值，记录聚合前条数n_records。
3. 该均值不是成交量加权价格。规格和产地构成变化可能影响均值，不能直接推断市场供需。
4. 清洗文件产地合并去重；加载时规范化空值、分隔符、省级简称与顺序。例如“鲁冀”和“冀/鲁”归为同一集合。
5. 未知地名保留原文，不猜测映射；缺失产地不参与切换判定。
6. 分析加载层拒绝空数据、缺失日期/品种、重复品种日、缺失/非有限/非正价格。

## 关键质量指标

- 产地缺失 4,654 条，占 20.8%。
- 品种内相邻记录日期间隔超过一天的比较有 1,516 次，最长 18 天。
- 覆盖不足30条报价的品种有 38 个；是否有滚动统计取决于此前30个日历天是否至少有15条报价，不能仅由总覆盖天数判定。

## 分析字段与有效比较

原始分析字段：date、product、avg_price、low_price、high_price、origin、n_records。
新增特征：gap_days、observed_pct_ch（相邻观测涨跌）、price_pct_ch（仅连续日历天）、history_n、rolling_mean、rolling_std、rolling_z、rolling_vol、origin_comparable与origin_changed。
缺价不插值、不前向填充。z-score基准为[t−30日,t)，排除当日；零历史方差且价格不变记z=0，变化记有符号无穷值。
输出中的缺失统计表示不能计算，不能默认为正常或零风险。

## 外推限制

无成交量/成交额、单市场、不到一年；产地和规格信息有限，品种覆盖不一致。
天气、春节与供需影响需补充多年、多市场和外部证据验证，现阶段只作研究假设。
检测规则用于报价复核，真实业务异常标签与采购效果尚未验证。
