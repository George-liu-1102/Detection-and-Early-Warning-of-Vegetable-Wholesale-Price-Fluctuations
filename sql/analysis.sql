-- SQLite 3.25+。输入是已聚合的品种日报价；重复时按导入顺序保留最后一条。
-- 此规则只是防重复导入，原始不同规格报价仍需在清洗步骤聚合，不能直接删掉。
CREATE VIEW daily_clean AS
WITH ranked AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY date, product ORDER BY rowid DESC) AS rn
    FROM quotes
)
SELECT * FROM ranked WHERE rn = 1;

-- LAG 与日历天差值：跨日价格变化保留为 NULL。
CREATE VIEW daily_returns AS
WITH previous AS (
    SELECT date, product, avg_price,
           LAG(date) OVER (PARTITION BY product ORDER BY date) AS previous_date,
           LAG(avg_price) OVER (PARTITION BY product ORDER BY date) AS previous_price
    FROM daily_clean
)
SELECT *, CASE WHEN julianday(date) - julianday(previous_date) = 1
               THEN avg_price / previous_price - 1 END AS daily_return
FROM previous;

-- 月度工作量：分母为实际观测记录，信号重叠按品种日去重。
CREATE VIEW monthly_metrics AS
SELECT substr(date, 1, 7) AS month, COUNT(*) AS n_records,
       SUM(anomaly_any) AS n_anomalies,
       1.0 * SUM(anomaly_any) / COUNT(*) AS anomaly_rate,
       SUM(surge) AS n_surge, SUM(crash) AS n_crash,
       SUM(origin_shift_alert) AS n_origin_shift
FROM daily_clean GROUP BY substr(date, 1, 7);

-- 连续异常日：gaps-and-islands，缺失日期自动中断连续区间。
CREATE VIEW anomaly_runs AS
WITH marked AS (
    SELECT product, date,
           julianday(date) - ROW_NUMBER() OVER (PARTITION BY product ORDER BY date) AS island
    FROM daily_clean WHERE anomaly_any = 1
)
SELECT product, MIN(date) AS start_date, MAX(date) AS end_date, COUNT(*) AS consecutive_days
FROM marked GROUP BY product, island HAVING COUNT(*) >= 3;
