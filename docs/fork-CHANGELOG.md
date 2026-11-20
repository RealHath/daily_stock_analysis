# 本仓库变更日志（Fork 本地）

本文件只记录 **本 fork 相对上游 [ZhuLinsen/daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis) 的本地变更**。

> 上游的 `docs/CHANGELOG.md` 保持与上游完全一致、**不做本地修改**，以降低同步上游时的冲突面。
> 上游自身的版本变更请看 [CHANGELOG.md](CHANGELOG.md)。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
变更细节、判定口径、验证数据与回滚方式见 [二次开发变更记录](fork-customizations.md)。

## [Unreleased]

### 新功能

- 每日新增独立邮件「今日 AI 推荐 Top N」：复用已有选股引擎（`ScreeningService`）从全 A 股筛选，AI 重排给出入选理由/市场观点/组合风险，渲染风格与现有报告一致（emoji + 分级标题 + 加粗标签）。以独立邮件发送（标题「🤖 AI 智能推荐 - 日期」），不与个股/大盘主报告混合，复用现有 SMTP 配置与收件人。邮件同时展示两份列表：① 引擎原始榜（策略默认价，不限用户价格上限）② 价格筛选榜（≤ `RECOMMEND_PRICE_MAX`，如 50 元）；未设价格上限时退回单列表。完全 fail-open——选股失败或两份列表均无候选则跳过该邮件，原主报告照常发送。新增 `src/daily_recommendation.py`，`main.py` 推送后独立调用。开关与参数：`SCREENING_ENABLED=true` 开启、`SCREENING_STRATEGY`（默认 `balanced_alpha`）、`RECOMMEND_PRICE_MAX`（价格上限，如 `50`）、`RECOMMEND_TOP_N`（默认 5），workflow `env:` 与 `.env.example` 均已透传/说明。两份列表共用一次选股结果（多取 Top N×4 候选），价格过滤在结果层做，不改动筛选引擎代码。
- 新增可切换的 A 股基本面 provider：`FUNDAMENTAL_PROVIDER=tushare` 时改用 Tushare Pro 财务接口（`fina_indicator` / `income` / `cashflow` / `dividend` / `forecast` / `express` / `top10_holders` / `stk_holdernumber` / `moneyflow` / `top_list`）聚合成长性/业绩分红/机构/资金流/龙虎榜，绕开在海外 IP 被限流的东财接口；默认仍为 `akshare`，原链路零改动。新 provider 不设独立超时，与 AkShare 共用 `FUNDAMENTAL_STAGE_TIMEOUT_SECONDS` / `FUNDAMENTAL_FETCH_TIMEOUT_SECONDS` 预算，且保持 fail-open 与相同返回结构；workflow `env:` 已透传该变量，Settings 配置即可生效。
- 邮件报告标题追加「（基本面）（技术面）」标注：改 `report_language.py` 中文 `dashboard_title`，一处覆盖 Python fallback 与 Jinja 模板、Markdown 与企业微信渠道，方便非专业用户一眼识别报告包含的分析维度。

### 修复

- GitHub Actions workflow（`00-daily-analysis.yml`）透传基本面超时与东财补丁变量：此前 `FUNDAMENTAL_STAGE_TIMEOUT_SECONDS`、`FUNDAMENTAL_FETCH_TIMEOUT_SECONDS`、`ENABLE_EASTMONEY_PATCH` 虽已在 Settings 配置，但 workflow 的 `env:` 未引用，导致运行时始终使用默认值（8s / 8s / false），基本面聚合因预算耗尽而全部降级为「数据缺失」。现按 `vars. || secrets. || 默认值` 模式补上引用，Settings 中的配置方可生效。

## [2026-11-18]

### 新功能

- 提示词新增「评分锚定」：`sentiment_score` 以确定性技术面评分（`trend_analysis.signal_score`）为基线，允许区间 ±10；越界会被截断到边界，并在 `dashboard.score_anchor` 记录「系统分 / 模型原始分 / 调整幅度 / 是否截断」，同时写 WARNING 日志。四个提示词变体（legacy / 标准 × 中英）均已接入。
- 提示词新增「本轮数据缺口」段落：把基本面各分块（估值/成长/业绩/机构/资金流/龙虎榜/板块）、筹码、盘中估算行情、市场结构缺失字段、新闻缺失等显式列进提示词，并要求写入 `dashboard.phase_decision.data_limitations`、缺失维度的结论必须写明「数据不足，无法判断」、`confidence_level` 不得为「高」。数据齐全时该段落不输出（零回归）。
- 报告新增「🧮 评分说明」小节：在「核心结论」之后展示**最终评分 / 技术面基线 / 实际调整幅度 / 调整依据**，被截断时额外说明「模型原评分 X 分超出允许区间，已收敛为 Y 分」。只呈现结果与原因，不暴露内部字段名。

- 每日报告新增「复盘对照」：按可配置窗口回验最近 N 次历史判断的实际涨跌与方向正确性，并在报告尾部汇总命中率；价格或历史不足时降级为「待观察」，不阻断推送（`REPORT_REVIEW_N` 默认 `0` = 关闭、`REPORT_REVIEW_HORIZON_DAYS` 默认 `3`、`REPORT_REVIEW_NEUTRAL_BAND_PCT` 默认 `1.0`）。提交 `8166b94e`。
- GitHub Actions 每日分析新增数据库持久化：分析结束后合并 SQLite WAL 并把 `data/stock_analysis.db` 提交回仓库，使回测、复盘对照与策略自适应权重可以跨运行累积样本。提交 `8166b94e`。

### 修复

- 实时行情成交量统一归一为「股」，修复 `UnifiedRealtimeQuote.volume` 契约被违反导致的量比 100 倍偏差：Tushare Pro `quotation`（`vol` 为手）与 AkShare 东财现货（`成交量` 为手）此前直接把原值写入按「股」口径使用的字段，覆盖日线后被判成「缩量上涨」；实盘样例（002832）量比由 0.0117 修正为 1.17、技术评分 62 → 66。新增 `realtime_types.normalize_realtime_volume_to_shares()` 用「成交额 / 价格」反推股数做交叉校验，上游口径变化时自动纠正，无法校验时退回源口径声明。提交 `936aa00a`。

### 文档

- 新增 [本仓库二次开发变更记录](fork-customizations.md)：汇总改动范围、判定口径、真实环境验证数据、风险与回滚方式，以及同步上游时的冲突面说明。提交 `a5e9f6e2`。
- 新增本文件，用于隔离本地变更记录，避免修改上游 `docs/CHANGELOG.md`。提交 `--`。
