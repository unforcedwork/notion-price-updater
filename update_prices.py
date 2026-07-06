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
