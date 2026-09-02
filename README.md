# Notion 每日价格更新器

每个交易日收盘后（北京时间 15:40，周一至周五）由 GitHub Actions 自动运行：

1. 抓取最新行情，更新「持有总览」的当前价格；
2. 在「每日资产快照」写入或更新当日快照（总市值、总投入、已实现利润、总利润、总收益率）。

## v2.1 变更

- 新增腾讯行情后备源：新浪接口对云服务器 IP 拦截严格，任一源失败自动切换，两个源都失败才判失败。
- 快照日期按北京时间计算（原先使用运行机器的 UTC 日期，跨时区手动触发会写错日期）。
- 节假日守卫：行情日期与当天不一致（非交易日）时只更新当前价格、跳过快照，不再产生假日垃圾行。
- 行情源列表改为运行时解析，修复后备源切换可能失效的缺陷。
- 单元测试 15 项全过（双行情源解析、后备切换、节假日跳过、北京时间日期、分页、部分卖出、快照去重等）。

## GitHub 设置（一次性，约 5 分钟）

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

## 行为说明

- 资产映射 `ASSET_MAP` 在 `update_prices.py` 顶部；新增开放持仓需同步添加同名条目，资产名称与「持有总览」完全一致。
- 部分卖出：从买入数量和成本中扣除已售数量及对应成本；「卖出记录」与「持有总览」的资产名称必须完全一致。
- 重复运行安全：同日快照更新而非重复创建；意外产生的重复快照自动归档。
- 任一开放持仓缺有效价格时整体失败并返回非零退出码，不写入半套数据。

## 本地运行

```bash
pip install -r requirements.txt
python -m unittest -v
python update_prices.py
```

兼容旧环境变量 `databaseID`，建议迁移到 `HOLDINGS_DATABASE_ID`。
