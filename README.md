# Notion Price Updater

自动从新浪财经拉取 A 股 / ETF 实时价格，写入 Notion 数据库，通过 GitHub Actions 每日定时运行。

## 工作原理

```
新浪财经 API ──→ update_prices.py ──→ Notion API
  (行情数据)        (解析价格)        (更新数据库)
```

脚本每次运行时：

1. 调用 Notion API 查询「持有总览」数据库中所有**未清仓**的资产
2. 将资产名称与 `ASSET_MAP` 中的新浪代码一一对应
3. 向新浪财经发送**一次** HTTP 请求，批量获取所有资产的当前价格
4. 逐个将价格写回 Notion 页面的「当前价格」字段

## 文件结构

```
notion-price-updater/
├── update_prices.py                    # 主脚本
├── .gitignore                          # 忽略 .env 和 __pycache__
└── .github/
    └── workflows/
        └── update_prices.yml           # GitHub Actions 定时任务
```

## 代码说明

### 环境变量

脚本通过 `os.environ` 读取以下环境变量（区分大小写）：

| 变量名 | 说明 |
|--------|------|
| `NOTION_TOKEN` | Notion Integration 的访问令牌，以 `ntn_` 开头 |
| `databaseID` | Notion「持有总览」数据库的 ID |

> ⚠️ 代码中会对读取到的值执行 `.strip()`，去除可能混入的引号和特殊空格。

### 配置项 `ASSET_MAP`

脚本中的 `ASSET_MAP` 字典定义了需要追踪的资产：

```python
ASSET_MAP = {
    "五粮液":  "sz000858",   # 深圳交易所，代码 000858
    "标普500": "sh513500",   # 上海交易所 ETF，代码 513500
}
```

- **key**：必须与 Notion 数据库中「资产」列的文本完全一致
- **value**：新浪财经的股票代码，格式为 `交易所前缀 + 6位代码`
  - `sh` = 上海证券交易所
  - `sz` = 深圳证券交易所

如需添加更多资产，在 `ASSET_MAP` 中增加一行即可，例如：

```python
ASSET_MAP = {
    "五粮液":     "sz000858",
    "标普500":    "sh513500",
    "贵州茅台":   "sh600519",
    "沪深300ETF": "sh510300",
}
```

### 核心函数

#### `get_prices_sina(codes_map)`

批量获取实时价格。向 `hq.sinajs.cn` 发送一次请求，用正则解析返回的 GBK 编码文本，提取每个股票代码对应的**当前价**（字段索引 3）。

返回示例：`{"五粮液": 105.5, "标普500": 2.568}`

#### `get_all_holdings()`

查询 Notion 数据库，筛选条件为「当前状态 ≠ 已清仓」，返回所有活跃资产的页面列表。

#### `update_price(page_id, price)`

通过 Notion PATCH API 将价格写入指定页面的「当前价格」number 字段。

#### `main()`

主流程：查询 Notion → 匹配资产 → 批量获取价格 → 逐个更新 → 打印日志。

### Notion 数据库要求

数据库需包含以下属性（字段名区分大小写）：

| 属性名 | 类型 | 说明 |
|--------|------|------|
| 资产 | Title | 资产名称，需与 `ASSET_MAP` 的 key 一致 |
| 当前价格 | Number | 脚本写入的目标字段 |
| 当前状态 | Status | 用于过滤，值为「已清仓」的记录会被跳过 |

## GitHub Actions 自动化

### 定时规则

```yaml
schedule:
  - cron: '30 7 * * 1-5'   # UTC 07:30 = 北京时间 15:30，仅工作日
```

每个工作日北京时间 15:30（A 股收盘后）自动运行。也可以在 Actions 页面手动触发（`workflow_dispatch`）。

### Secrets 配置

在仓库 Settings → Secrets and variables → Actions 中添加：

| Secret 名称 | 值 |
|-------------|----|
| `NOTION_TOKEN` | Notion Integration Token |
| `databaseID` | Notion 数据库 ID |

**获取方式：**

1. **NOTION_TOKEN**：前往 [Notion Integrations](https://www.notion.so/my-integrations)，创建一个 Internal Integration，复制生成的 Token
2. **databaseID**：在 Notion 中打开数据库页面，URL 中 `notion.so/` 后面的那串 32 位十六进制字符即为数据库 ID
   ```
   https://www.notion.so/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx?v=...
                                      ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                          这就是 databaseID
   ```
3. 授权 Integration 访问数据库：在数据库页面右上角 `···` → Connections → 添加你的 Integration

## 本地运行

```bash
# 设置环境变量
export NOTION_TOKEN="ntn_xxx..."
export databaseID="f38d1e53bce6..."

# 安装依赖
pip install requests

# 运行
python update_prices.py
```

预期输出：

```
[2026-06-06] 开始更新股价...
[OK]   标普500 → 2.568
[OK]   五粮液 → 81.08
更新完成。
```

## 依赖

- Python 3.11+
- requests（唯一运行时依赖）

无其他第三方库依赖。

## 常见问题

**Q: 为什么某些资产显示 `[SKIP]`？**
A: 资产名称不在 `ASSET_MAP` 中。检查 Notion 数据库中的名称是否与代码中的 key 完全一致（包括空格、标点）。

**Q: 为什么显示 `[WARN] 未找到行情数据`？**
A: 新浪财经接口可能暂时不可用，或股票代码格式错误。确认代码以 `sh` 或 `sz` 开头，后接 6 位数字。

**Q: 如何查看运行日志？**
A: 在 GitHub 仓库 → Actions → 点击最近一次 workflow run → 点击 `update` job → 展开 `Run python update_prices.py` 步骤。

**Q: 定时任务没跑？**
A: GitHub Actions 对非活跃仓库会自动禁用 scheduled workflows。至少每 60 天手动触发一次即可保持激活。
