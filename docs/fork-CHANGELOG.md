# 本仓库变更日志（Fork 本地）

本文件只记录 **本 fork 相对上游 [ZhuLinsen/daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis) 的本地变更**。

> 上游的 `docs/CHANGELOG.md` 保持与上游完全一致、**不做本地修改**，以降低同步上游时的冲突面。
> 上游自身的版本变更请看 [CHANGELOG.md](CHANGELOG.md)。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
变更细节、判定口径、验证数据与回滚方式见 [二次开发变更记录](fork-customizations.md)。

## [Unreleased]

## [2026-11-18]

### 新功能

- 每日报告新增「复盘对照」：按可配置窗口回验最近 N 次历史判断的实际涨跌与方向正确性，并在报告尾部汇总命中率；价格或历史不足时降级为「待观察」，不阻断推送（`REPORT_REVIEW_N` 默认 `0` = 关闭、`REPORT_REVIEW_HORIZON_DAYS` 默认 `3`、`REPORT_REVIEW_NEUTRAL_BAND_PCT` 默认 `1.0`）。提交 `8166b94e`。
- GitHub Actions 每日分析新增数据库持久化：分析结束后合并 SQLite WAL 并把 `data/stock_analysis.db` 提交回仓库，使回测、复盘对照与策略自适应权重可以跨运行累积样本。提交 `8166b94e`。

### 修复

- 实时行情成交量统一归一为「股」，修复 `UnifiedRealtimeQuote.volume` 契约被违反导致的量比 100 倍偏差：Tushare Pro `quotation`（`vol` 为手）与 AkShare 东财现货（`成交量` 为手）此前直接把原值写入按「股」口径使用的字段，覆盖日线后被判成「缩量上涨」；实盘样例（002832）量比由 0.0117 修正为 1.17、技术评分 62 → 66。新增 `realtime_types.normalize_realtime_volume_to_shares()` 用「成交额 / 价格」反推股数做交叉校验，上游口径变化时自动纠正，无法校验时退回源口径声明。提交 `936aa00a`。

### 文档

- 新增 [本仓库二次开发变更记录](fork-customizations.md)：汇总改动范围、判定口径、真实环境验证数据、风险与回滚方式，以及同步上游时的冲突面说明。提交 `a5e9f6e2`。
- 新增本文件，用于隔离本地变更记录，避免修改上游 `docs/CHANGELOG.md`。提交 `--`。
