# Notion 每日价格更新器 / Notion Daily Price Updater

[中文](#中文) · [English](#english)

---

## 中文

每个交易日收盘后（北京时间 15:40，周一至周五）由 GitHub Actions 自动运行：

1. 抓取最新行情，更新「持有总览」的当前价格；
2. 在「每日资产快照」写入或更新当日快照（总市值、总投入、已实现利润、总利润、总收益率）。

### v2.1 变更

- 新增腾讯行情后备源：新浪接口对云服务器 IP 拦截严格，任一源失败自动切换，两个源都失败才判失败。
- 快照日期按北京时间计算（原先使用运行机器的 UTC 日期，跨时区手动触发会写错日期）。
- 节假日守卫：行情日期与当天不一致（非交易日）时只更新当前价格、跳过快照，不再产生假日垃圾行。
- 行情日期字段按格式扫描定位，兼容不同标的的字段漂移。
- Notion API 报错附带中文排查提示；工作流新增 Secrets 预检步骤。
- 单元测试 18 项全过，另有端到端模拟测试（双行情源解析、后备切换、节假日跳过、北京时间日期、分页、部分卖出、快照去重等）。

### GitHub 设置（一次性，约 5 分钟）

1. 打开 https://www.notion.so/profile/integrations 新建 Integration（旧 Token 已泄露，应先吊销），复制 `ntn_` 开头的新 Token。
2. 把 Integration 连接到三个数据库（打开数据库 → 右上角「…」→ 连接 → 选择刚建的 Integration）：
   - 持有总览
   - 卖出记录
   - 每日资产快照
3. 仓库 Settings → Secrets and variables → Actions → New repository secret，添加 4 个 Secret：
   - `NOTION_TOKEN`：第 1 步的 Token
   - `HOLDINGS_DATABASE_ID`：「持有总览」数据库 ID（打开数据库 → 复制链接，URL 中 32 位十六进制部分）
   - `SNAPSHOT_DATABASE_ID`：「每日资产快照」数据库 ID
   - `SELL_DATABASE_ID`：「卖出记录」数据库 ID
4. 在 Actions 页签启用工作流，可用 Run workflow 手动触发验证。

### 行为说明

- 资产映射 `ASSET_MAP` 在 `update_prices.py` 顶部；新增开放持仓需同步添加同名条目，资产名称与「持有总览」完全一致。
- 部分卖出：从买入数量和成本中扣除已售数量及对应成本；「卖出记录」与「持有总览」的资产名称必须完全一致。
- 重复运行安全：同日快照更新而非重复创建；意外产生的重复快照自动归档。
- 任一开放持仓缺有效价格时整体失败并返回非零退出码，不写入半套数据。

### 本地运行

```bash
pip install -r requirements.txt
python -m unittest -v
python update_prices.py
```

兼容旧环境变量 `databaseID`，建议迁移到 `HOLDINGS_DATABASE_ID`。

---

## English

A GitHub Actions workflow that runs after each trading day close (15:40 Beijing time, Monday–Friday):

1. Fetches the latest quotes and updates the current price of each asset in the "Holdings" (持有总览) database;
2. Creates or updates the day's record in the "Daily Asset Snapshot" (每日资产快照) database — total market value, total cost, realized profit, total profit, and return rate.

### What changed in v2.1

- Added Tencent Quotes as a fallback source: Sina aggressively blocks cloud IPs, so the script fails over automatically and only errors when both sources fail.
- Snapshot dates are computed in Beijing time (previously the runner's UTC date, which wrote the wrong date for cross-timezone manual triggers).
- Holiday guard: when the quote date differs from today (non-trading day), only prices are updated and the snapshot is skipped — no holiday junk rows.
- Quote-date fields are located by format scanning, tolerating field drift across different tickers.
- Notion API errors carry actionable troubleshooting hints; the workflow now pre-checks Secrets and reports problems directly in the Annotations panel.
- 18 unit tests passing (dual-source parsing, failover, holiday skip, Beijing date, pagination, partial sells, snapshot dedup, and more), plus an end-to-end mock test.

### GitHub setup (one-time, ~5 minutes)

1. Go to https://www.notion.so/profile/integrations and create a new Integration (the old token was leaked — revoke it first), then copy the new `ntn_` token.
2. Connect the integration to the three databases (open the database → "…" menu → Connections → select your integration):
   - Holdings (持有总览)
   - Sell Records (卖出记录)
   - Daily Asset Snapshot (每日资产快照)
3. In the repo: Settings → Secrets and variables → Actions → New repository secret, add 4 secrets:
   - `NOTION_TOKEN`: the token from step 1
   - `HOLDINGS_DATABASE_ID`: Holdings database ID (open the database → Copy link; use the 32-hex part of the URL)
   - `SNAPSHOT_DATABASE_ID`: Daily Asset Snapshot database ID
   - `SELL_DATABASE_ID`: Sell Records database ID
4. Enable the workflow in the Actions tab; use "Run workflow" to verify manually.

### Behavior notes

- The asset mapping `ASSET_MAP` lives at the top of `update_prices.py`; new open positions need a matching entry whose name exactly matches the one in Holdings.
- Partial sells: sold quantity and its cost basis are deducted; asset names in Sell Records must exactly match Holdings.
- Safe to re-run: same-day snapshots are updated in place, and accidental duplicates are auto-archived.
- If any open position lacks a valid price, the run fails with a non-zero exit code instead of writing partial data.

### Run locally

```bash
pip install -r requirements.txt
python -m unittest -v
python update_prices.py
```

The legacy `databaseID` environment variable is still supported; migrating to `HOLDINGS_DATABASE_ID` is recommended.
