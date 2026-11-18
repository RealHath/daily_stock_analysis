# 本仓库二次开发变更记录

> 适用范围：本文件记录 **本 fork 相对上游 `ZhuLinsen/daily_stock_analysis` 的本地改动**。
> 上游没有这些改动；同步上游时请阅读第 7 节「同步上游注意事项」。
>
> 最后更新：2026-11-18

## 1. 背景与目标

本仓库用于**辅助个人交易学习**：每日自动分析少量自选股，把「决策仪表盘」推送到通知渠道，并保留历史以便复盘。

排查上游 v3.35.0 的实际运行记录后，确认三个影响学习价值的问题：

| 问题 | 现象 | 影响 |
| --- | --- | --- |
| A. 运行历史不落地 | GitHub Actions 每次从空库开始 | 回测、命中率、复盘全部失效 |
| B. 没有「回头看」 | 报告只说「今天怎么操作」 | 无法判断建议是否靠谱 |
| C. 成交量单位错 100 倍 | 量比被算成 0.01，判成「缩量上涨」 | 直接压低技术评分、误导结论 |

对应做了 **2 个新功能 + 1 个缺陷修复**，全部默认关闭或零回归，可通过配置启用。

## 2. 变更总览

| 提交 | 类型 | 内容 |
| --- | --- | --- |
| `8166b94e` | 新功能 | 数据库持久化 + 复盘对照 |
| `8e56c629` | chore | Actions 首次回写数据库（机器人提交） |
| `936aa00a` | 修复 | 实时行情成交量单位归一 |

代码规模：16 个文件，+1238 / -6（不含二进制数据库）。

| 模块 | 文件 | 变化 |
| --- | --- | --- |
| 工作流 | `.github/workflows/00-daily-analysis.yml` | +56 |
| 工作流 | `.gitignore` | +6 |
| 复盘对照 | `src/services/review_comparison_service.py` | 新增 338 |
| 复盘对照 | `src/notification.py` | +164 |
| 复盘对照 | `src/core/config_registry.py` | +84 |
| 复盘对照 | `apps/dsa-web/src/locales/settingsHelp.ts` | +44 |
| 复盘对照 | `src/report_language.py` | +36 |
| 复盘对照 | `templates/report_markdown.j2` | +10 |
| 复盘对照 | `src/config.py` | +6 |
| 成交量修复 | `data_provider/realtime_types.py` | +56 |
| 成交量修复 | `data_provider/tushare_fetcher.py` | +19 / -3 |
| 成交量修复 | `data_provider/akshare_fetcher.py` | +13 / -4 |
| 测试 | `tests/test_review_comparison_service.py` | 新增 136 |
| 测试 | `tests/test_tushare_realtime_volume_unit.py` | 新增 258 |
| 文档 | `.env.example` | +7 |
| 文档 | `docs/fork-CHANGELOG.md` | 新增 |
| 文档 | `docs/fork-customizations.md`（本文件） | 新增 |

## 3. 功能一：数据库持久化

### 3.1 问题

上游 Actions 只上传 `reports/` 与 `logs/` 作为 artifact，**从不上传也不恢复** `data/stock_analysis.db`。而 runner 每次都是全新容器，因此：

- 历史分析记录每天归零；
- `BacktestService` 没有历史可回测；
- 策略自适应权重要求样本 ≥ 30 次，永远凑不满，权重恒为 1.0；
- 复盘对照没有数据源。

### 3.2 改动

| 位置 | 改动 |
| --- | --- |
| `.gitignore` | `/data/` → `/data/*`，并放行 `!/data/stock_analysis.db`（其余 `data/` 产物仍忽略） |
| workflow 顶层 | 新增 `permissions: contents: write` |
| checkout 步骤 | 新增 `fetch-depth: 0`（提交后需要 rebase/push） |
| 新增步骤 | `持久化数据库`：合并 WAL → 提交 → push（含 3 次 rebase 重试） |
| artifact 步骤 | 额外包含 `data/stock_analysis.db`，便于失败排查 |

关键点：SQLite 默认开启 WAL，**直接 `git add` 会漏掉最新写入**。步骤内先执行
`PRAGMA wal_checkpoint(TRUNCATE)` 合并到主库再提交。

### 3.3 失败策略

持久化属于附属能力，`push` 三次失败后只输出 `::warning::` 并以 `exit 0` 结束，
**不会把整轮分析判为失败**，分析结果与通知照常产出。

### 3.4 验证（真实环境）

```
提交 8e56c629  chore: persist analysis database [skip ci]
data/stock_analysis.db  1,339,392 字节（已入库）
运行 #34 的 10 个 step 全部 success
```

### 3.5 风险

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| 隐私 | 数据库含自选股、全部分析报告与结论；**仓库为 public 时这些内容公开可见** | 建议把仓库转为 private |
| 体积增长 | 少量自选股约 1–3 MB/年 | 可定期清理旧记录 |
| 分支保护 | 若目标分支受保护，`push` 会被拒 | 会出现 `::warning::`，需要调整保护规则或授予写权限 |

## 4. 功能二：复盘对照

### 4.1 目标

在每日报告末尾回验历史判断的实际表现，给出命中率，用于判断信号是否可靠。

### 4.2 判定口径

| 项 | 规则 |
| --- | --- |
| 样本来源 | `analysis_history` 中最近 N 条记录（排除本轮自身） |
| 基准价 | 分析日当天或之前最近一根日线的收盘价 |
| 验证价 | 分析日之后第 `HORIZON` 个交易日的收盘价 |
| 买入类正确 | 涨跌幅 ≥ +中性带 |
| 卖出类正确 | 涨跌幅 ≤ −中性带 |
| 持有/观望正确 | 涨跌幅绝对值 ≤ 中性带 |
| 命中率 | 正确 ÷（正确 + 错误）；「震荡」不计入分母 |
| 数据不足 | 显示「⏳ 待观察」，不报错、不阻断推送 |

方向判定与结果分类**复用 `BacktestEngine`**，与回测模块保持同一套定义，避免两处口径漂移。

### 4.3 配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `REPORT_REVIEW_N` | `0`（关闭） | 每只股票回验最近 N 条判断 |
| `REPORT_REVIEW_HORIZON_DAYS` | `3` | 验证窗口（交易日，1–20） |
| `REPORT_REVIEW_NEUTRAL_BAND_PCT` | `1.0` | 中性带（%，0.1–50） |

三个配置已注册进 `config_registry`（Web 设置页可见，含中英文帮助文案）。

### 4.4 渲染路径

上游默认 `REPORT_RENDERER_ENABLED=false`，实际走 `notification.generate_dashboard_report`
的手写 Markdown 分支。因此复盘章节**同时接入两条路径**：

- 手写路径：`NotificationService._get_review_comparison_context()` + `_format_review_comparison_lines()`；
- 模板路径：`templates/report_markdown.j2` 增加对应区块（仅在开启模板渲染时生效）。

### 4.5 输出示例

```
## 🧾 复盘对照

📊 命中率 100.0% （1✅ / 0❌ / 0➖ / 0⏳）

### 贵州茅台（600519）

| 时间 | 建议 | 评分 | 涨跌 | 结果 |
|------|------|------|------|------|
| 2026-11-14 18:00 | 买入 | 72 | +2.30% | ✅ 正确 |
| 2026-11-17 18:00 | 观望 | 55 | — | ⏳ 待观察 |

> 以上为历史判断在后续 3 个交易日的实际表现，仅供复盘校验，不构成投资建议。
```

### 4.6 限制

- 需要**先积累历史**：首次运行只记录判断，要等满 `HORIZON` 个交易日才出现对错结论；
- 只读本地数据库，不联网补数据：日线缺失时该条样本降级为「待观察」；
- 依赖功能一（数据库持久化），否则每次运行都是空历史。

## 5. 修复：实时行情成交量单位

### 5.1 契约

`UnifiedRealtimeQuote.volume` 的契约单位是**股**（与历史日线一致），因为它会被合并进日线
DataFrame 并参与量比计算（`src/core/pipeline.py` 的 `_augment_historical_with_realtime`）。

### 5.2 根因

不同数据源的实时接口口径不一致，且部分路径未做归一：

| 数据源 / 接口 | 原始口径 | 修复前处理 |
| --- | --- | --- |
| Tushare Pro `quotation` | 手 | 直接写入（少乘 100） |
| Tushare 旧版 `get_realtime_quotes` | 股 | `// 100`（错误地转成手） |
| AkShare 东财现货（A 股） | 手 | 直接写入 |
| AkShare 东财现货（ETF） | 手 | 直接写入 |
| AkShare 腾讯路径 | 混合 | 已有换手率交叉校验（未改动） |
| AkShare 港股路径 | 不确定 | 保持原样（缺少成交额，无法校验） |

### 5.3 实测证据（002832 / 2026-11-17）

同一笔实时行情内部即可自证单位矛盾：

```
volume = 68,311        amount = 197,799,615.63 元   price = 29.12 元
按「股」: 197,799,615 / 68,311      = 2,895.57 元/股  ← 与股价不符
按「手」: 197,799,615 / 6,831,100   =    28.96 元/股  ← 与股价吻合
```

而数据库日线为 `6,831,199` 股，正好是 `68,311 × 100`。据此判定实时值为「手」。

影响链条：

```
实时量 68,311（手）覆盖日线最后一根 → 5 日均量 5,837,247 股
量比 = 68,311 / 5,837,247 = 0.0117 → 判为「缩量上涨」
量能得分 6（应为 10）→ 技术评分 62（应为 66）
```

### 5.4 修复方式

新增 `data_provider/realtime_types.normalize_realtime_volume_to_shares()`：

1. 用 `成交额 ÷ 价格` 反推股数作为裁判；
2. 在「手（×100）」与「股（×1）」两个候选中取更接近的一方；
3. 无法校验时退回调用方声明的源口径（`lot` / `share`）。

这样即使上游将来改变口径，也会被自动纠正，而不是静默偏差 100 倍。

四个调用点（Tushare ×2、AkShare A 股 / ETF）统一改走该函数；腾讯路径与港股路径保持不变。

### 5.5 修复效果（用真实日线回放）

| | 修复前 | 修复后 |
| --- | --- | --- |
| 传入成交量 | 68,311（手） | 6,831,100（股） |
| 量比 | 0.0117 | 1.1703 |
| 量能判定 | 缩量上涨 | 量能正常 |
| 技术评分 | 62（与生产库一致） | 66（+4） |

「修复前」精确复现了生产数据库中记录的 `signal_score = 62`，证明归因与修复点正确。

### 5.6 上游状态

`ZhuLinsen/daily_stock_analysis` 最新 `main` 仍是同样的写法（`tushare_fetcher.py` 的
`volume=safe_int(row.get('vol'))` 与 `volume=safe_int(row['volume']) // 100`），
**该缺陷上游尚未修复**。这是本 fork 的本地补丁。

### 5.7 已知假设

交叉校验依赖「成交额单位为元」。已核对本仓库实际使用的两个接口（Tushare Pro `quotation`、
AkShare 东财现货）均为元。若将来接入以「千元」为成交额单位的数据源，需要为该路径显式传入口径
或跳过校验。

## 6. 改进：提示词评分锚定与数据缺口披露

### 6.1 背景

实测一次真实运行（002832）：`prompt_tokens=6,926`、`completion_tokens=7,340`，其中约一半输出
token 消耗在填充 88 个 JSON 字段上；同时 `sentiment_score` 完全由模型给出，缺少可复现锚点，
同一份数据在不同模型/温度下分数漂移明显。另外基本面 6 个分块抓取失败时，提示词里对应小节直接
不出现，模型容易把「没有数据」当成「没有风险」。

本轮只做两处低风险改动，不动输出 schema。

### 6.2 评分锚定

| 项 | 内容 |
| --- | --- |
| 基线 | `trend_analysis.signal_score`（确定性技术面打分，0-100） |
| 允许区间 | 基线 ±10 |
| 越界处理 | 截断到区间边界，写 `dashboard.score_anchor` 并输出 WARNING 日志 |
| 留痕字段 | `system_score` / `model_score` / `adjustment` / `band` / `clamped` / `final_score` / `reason_required` |
| 提示词要求 | 调整 ≥8 分必须在 `guardrail_reason` 说明依据；无系统评分时注明「无系统评分为基线」 |

实现位置：`src/analyzer.py` 的 `SCORE_ANCHOR_PROMPT_ZH/EN`、`_apply_score_anchor()`；
四个提示词变体（legacy / 标准 × 中英）均在 `CANONICAL_DECISION_SCALE_PROMPT_*` 之后拼接。

行为变化：`sentiment_score` 被限制在技术面评分 ±10 内。这会让分数更可复现、可回测，代价是
**当基本面/事件面与技术面严重背离时，模型无法给出跨区间的分数**（会在 `score_anchor` 里留下
原始分与被截断记录，便于后续评估是否需要放宽）。

### 6.3 数据缺口披露
新增 `_format_data_gap_section()`，在提示词「基础信息」之后插入 `## ⚠️ 本轮数据缺口（必须披露）`：

覆盖维度：基本面各分块（估值/成长/业绩/机构/资金流/龙虎榜/板块）的 `failed` / `not_supported` /
`partial`、筹码缺失、盘中估算行情（含受影响字段清单）、市场结构 `partial` 与缺失字段、新闻缺失。

强制要求（写进提示词）：逐条写入 `dashboard.phase_decision.data_limitations`；依赖缺失维度的
结论必须写「数据不足，无法判断」；不得把数据缺失表述为「没有风险」；`confidence_level` 不得为「高」。

**零回归**：所有维度齐全时该函数返回空字符串，提示词结构与改动前一致。

### 6.4 报告中的「评分说明」

锚定结果会以**面向用户**的形式出现在报告（邮件 / Web / IM）中，位置在「核心结论」之后、行情快照之前。
只呈现最终结果与原因，不暴露内部字段名。

区间内（真实样例，002832）：

```
### 🧮 评分说明

**最终评分 58 分** ｜ 技术面基线 **62** 分 ｜ 调整 **-4** 分（允许区间 ±10）

**调整依据**：获利比例99.6%叠加缩量上涨与大盘退潮，获利回吐风险偏高
```

被截断时（模型原评分 92）：

```
### 🧮 评分说明

**最终评分 72 分** ｜ 技术面基线 **62** 分 ｜ 调整 **+10** 分（允许区间 ±10）

⚠️ 模型原评分 92 分超出允许区间，已收敛为 72 分（避免与技术面证据脱节）

**调整依据**：MA5>MA10>MA20多头排列且乖离率仅+2.28%，趋势强度75/100
```

口径说明：

| 字段 | 来源 |
| --- | --- |
| 最终评分 | `score_anchor.final_score` |
| 技术面基线 | `score_anchor.system_score` |
| 调整幅度 | **实际生效**的调整量（`final_score - system_score`），不是模型原始偏差 |
| 调整依据 | 优先 `guardrail_reason`；否则取 `signal_attribution` 的最强看空/看多信号；均缺失时用固定文案 |

实现位置：`src/notification.py` 的 `_format_score_anchor_lines()`，在
`generate_dashboard_report()` 中「核心结论」之后调用；文案见 `src/report_language.py`（中英韩）。

### 6.5 验证

| 项 | 结果 |
| --- | --- |
| 新增测试 `tests/test_score_anchor_and_data_gap.py`（14 用例） | ✅ 14 passed |
| 提示词相关既有回归（news_prompt / prompt_language / integrity / report_language 等） | ✅ 98 passed |
| 其他提示词链路（analysis_integration / report_schema / context_pack_prompt 等） | ✅ 59 passed |
| 真实上下文回放（002832 生产快照） | ✅ 缺口段完整列出 8 条，提示词含「系统评分 62」基线 |
| `flake8 --select=E9,F63,F7,F82` | ✅ 0 问题 |

## 7. 启用步骤

1. **授予 Actions 写权限**：`Settings → Actions → General → Workflow permissions`
   选择 `Read and write permissions`；
2. **新增变量**（`Settings → Secrets and variables → Actions → Variables`）：

   | 变量 | 建议值 |
   | --- | --- |
   | `REPORT_REVIEW_N` | `5` |

   （`REPORT_REVIEW_HORIZON_DAYS`、`REPORT_REVIEW_NEUTRAL_BAND_PCT` 留空即用默认值）

3. **手动触发一次**：`Actions → 每日股票分析 → Run workflow`；
4. **确认**：仓库中出现 `chore: persist analysis database [skip ci]` 提交，且推送报告中出现
   `## 🧾 复盘对照` 章节。

## 8. 同步上游注意事项

同步 `ZhuLinsen/daily_stock_analysis` 时注意以下冲突面：

| 文件 | 冲突可能性 | 说明 |
| --- | --- | --- |
| `.github/workflows/00-daily-analysis.yml` | 中 | 该文件上游改动频繁（大量 env 映射） |
| `.gitignore` | 低 | 仅追加白名单规则 |
| `src/config.py`、`src/core/config_registry.py` | 中 | 若上游调整配置项顺序或校验规则 |
| `src/notification.py` | 中 | 复盘章节插入在 `generate_dashboard_report` 内 |
| `data_provider/*` | 低 | 修改的是上游未触碰的行 |
| `docs/CHANGELOG.md` | 无 | **保持与上游完全一致，不做本地修改**；本地变更记录在新增的 `docs/fork-CHANGELOG.md` |

**特意未改动 `docs/INDEX.md` 与 `docs/CHANGELOG.md`**，以减少同步冲突。
本文档的记录入口是新增的 [本仓库变更日志](fork-CHANGELOG.md)，不依赖上游的文档索引。

## 9. 回滚方式

| 改动 | 回滚 |
| --- | --- |
| 复盘对照 | 删除或置空 `REPORT_REVIEW_N`（=0 即关闭），或 revert 对应提交 |
| 数据库持久化 | 移除 `持久化数据库` 步骤与 `permissions`，并恢复 `.gitignore` 的 `/data/` 规则；已提交的数据库可删除后重新忽略 |
| 成交量修复 | revert `936aa00a`（会恢复 100 倍偏差，不建议） |
| 评分锚定 | 移除 `_apply_score_anchor()` 调用即恢复「模型自由给分」；`dashboard.score_anchor` 为附加字段，忽略即可 |
| 数据缺口披露 | 移除 `_format_prompt` 中的 `_format_data_gap_section()` 调用即恢复原提示词 |
| 整体 | `git revert 936aa00a 8166b94e`（`8e56c629` 为机器人数据提交，可单独删除） |

## 10. 验证清单

本次改动在本地执行过的检查：

```bash
# 语法
python -m py_compile src/config.py src/core/config_registry.py src/report_language.py \
    src/notification.py src/services/review_comparison_service.py \
    data_provider/realtime_types.py data_provider/tushare_fetcher.py data_provider/akshare_fetcher.py

# 静态检查（与 scripts/ci_gate.sh 的 flake8 口径一致）
python -m flake8 data_provider/ tests/test_tushare_realtime_volume_unit.py \
    --count --select=E9,F63,F7,F82 --statistics

# 单元测试（180 项通过）
python -m pytest tests/test_review_comparison_service.py \
    tests/test_tushare_realtime_volume_unit.py tests/test_akshare_realtime_logging.py \
    tests/test_tushare_fetcher_followups.py tests/test_tushare_fetcher_get_stock_list.py \
    tests/test_tushare_fetcher_http_client.py tests/test_tushare_permission_circuit_breaker.py \
    tests/test_realtime_types.py tests/test_config_registry.py tests/test_report_language.py -q
```

结果：`180 passed`。

**未在本地验证**（本机缺少部分依赖或需完整 CI 环境）：

- `web-gate`（`apps/dsa-web` 的 `npm run lint` / `npm run build`）——本次仅新增 TS 常量对象；
- `docker-build`；
- `docs/notifications.md` 的 Actions env 表一致性测试——生成器只收录固定通知类 key，
  本次新增的 `REPORT_REVIEW_*` 不在其范围内，理论上不受影响，但需完整依赖才能执行。
