# update_prices.py
import os
import re
import time
import requests
from datetime import date


# ─── 配置 ───────────────────────────────────────────────
NOTION_TOKEN = os.environ["NOTION_TOKEN"].strip('"“”‘’ ')
DATABASE_ID  = os.environ["databaseID"].strip('"“”‘’ ')

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

ASSET_MAP = {
    "五粮液":  "sz000858",
    "标普500": "sh513500",
    "许继电气": "sz000400"
}
# ────────────────────────────────────────────────────────


def get_prices_sina(codes_map: dict) -> dict:
    """
    codes_map: {"五粮液": "sz000858", "标普500": "sh513500"}
    返回: {"五粮液": 105.5, "标普500": 1.234}
    """
    query = ",".join(codes_map.values())
    url = f"https://hq.sinajs.cn/list={query}"
    headers = {"Referer": "https://finance.sina.com.cn"}
    resp = requests.get(url, headers=headers, timeout=10)
    resp.encoding = "gbk"

    result = {}
    for name, code in codes_map.items():
        match = re.search(rf'hq_str_{code}="([^"]+)"', resp.text)
        if not match:
            print(f"[WARN] {name} 未找到行情数据")
            continue
        fields = match.group(1).split(",")
        if len(fields) < 4 or not fields[3]:
            print(f"[WARN] {name} 数据格式异常: {fields}")
            continue
        result[name] = float(fields[3])  # 当前价（index 3）
    return result


def get_all_holdings() -> list[dict]:
    """查询持有总览中所有未清仓的资产页面。"""
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    payload = {
        "filter": {
            "property": "当前状态",
            "status": {
                "does_not_equal": "已清仓"
            }
        }
    }
    resp = requests.post(url, headers=HEADERS, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json().get("results", [])


def update_price(page_id: str, price: float) -> None:
    """将价格写入 Notion 页面的「当前价格」字段。"""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    payload = {
        "properties": {
            "当前价格": {"number": price}
        }
    }
    resp = requests.patch(url, headers=HEADERS, json=payload, timeout=15)
    resp.raise_for_status()


def main():
    print(f"[{date.today()}] 开始更新股价...")
    pages = get_all_holdings()

    # 只处理在 ASSET_MAP 中的资产
    targets = {}
    page_map = {}
    for page in pages:
        asset_name = page["properties"]["资产"]["title"][0]["plain_text"]
        if asset_name not in ASSET_MAP:
            print(f"[SKIP] {asset_name}")
            continue
        targets[asset_name] = ASSET_MAP[asset_name]
        page_map[asset_name] = page["id"]

    if not targets:
        print("无需更新。")
        return

    prices = get_prices_sina(targets)  # 一次请求拿所有价格

    for name, price in prices.items():
        update_price(page_map[name], price)
        print(f"[OK]   {name} → {price}")
        time.sleep(0.3)

    print("更新完成。")

if __name__ == "__main__":
    main()


SNAPSHOT_DATABASE_ID = os.environ["SNAPSHOT_DATABASE_ID"].strip('"“”‘’ ')

def get_all_holdings_full() -> list[dict]:
    """查询持有总览中的全部资产页面（含已清仓），用于汇总总市值/总投入。"""
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    resp = requests.post(url, headers=HEADERS, json={}, timeout=15)
    resp.raise_for_status()
    return resp.json().get("results", [])


def get_realized_profit_sum(sell_database_id: str) -> float:
    """汇总卖出记录数据库中「单笔利润（自动）」公式列的总和。"""
    url = f"https://api.notion.com/v1/databases/{sell_database_id}/query"
    total = 0.0
    payload = {}
    while True:
        resp = requests.post(url, headers=HEADERS, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        for page in data.get("results", []):
            formula = page["properties"]["单笔利润（自动）"]["formula"]
            total += formula.get("number") or 0
        if not data.get("has_more"):
            break
        payload = {"start_cursor": data["next_cursor"]}
    return round(total, 2)


def create_daily_snapshot(sell_database_id: str) -> None:
    """汇总持有总览的市值/投入 + 卖出记录的已实现利润，写入每日快照数据库。"""
    pages = get_all_holdings_full()
    total_value = 0.0
    total_invested = 0.0
    for page in pages:
        props = page["properties"]
        market_value = props["当前市值（自动）"]["formula"].get("number")
        invested = props["总投入（自动）"]["formula"].get("number") or 0
        # 已清仓资产当前市值为空，不计入市值，也不计入投入（成本已通过卖出记录结算）
        status = props["当前状态"]["status"]["name"]
        if status == "已清仓":
            continue
        total_value += market_value or 0
        total_invested += invested

    realized_profit = get_realized_profit_sum(sell_database_id)
    total_profit = round((total_value - total_invested) + realized_profit, 2)
    return_rate = round(total_profit / total_invested * 100, 2) if total_invested else 0

    today_str = str(date.today())
    url = "https://api.notion.com/v1/pages"
    payload = {
        "parent": {"database_id": SNAPSHOT_DATABASE_ID},
        "properties": {
            "快照日期": {"title": [{"text": {"content": today_str}}]},
            "日期": {"date": {"start": today_str}},
            "总市值": {"number": round(total_value, 2)},
            "总投入": {"number": round(total_invested, 2)},
            "已实现利润": {"number": realized_profit},
            "总利润": {"number": total_profit},
            "总收益率": {"number": return_rate},
        },
    }
    resp = requests.post(url, headers=HEADERS, json=payload, timeout=15)
    resp.raise_for_status()
    print(f"[OK]   每日快照已写入 → 总市值{total_value:.2f} 总投入{total_invested:.2f} 总利润{total_profit:.2f}")


# 在 main() 末尾调用：
# create_daily_snapshot(sell_database_id=os.environ["SELL_DATABASE_ID"])
