from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SCRIPT_VERSION = "2.1"
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
MARKET_TZ = ZoneInfo("Asia/Shanghai")

# 资产名称必须与「持有总览」中的“资产”完全一致。
ASSET_MAP = {
    "五粮液": "sz000858",
    "标普500": "sh513500",
    "许继电气": "sz000400",
}

log = logging.getLogger("price-updater")


@dataclass(frozen=True)
class Settings:
    notion_token: str
    holdings_database_id: str
    snapshot_database_id: str
    sell_database_id: str

    @classmethod
    def from_env(cls) -> "Settings":
        # 兼容旧变量 databaseID；建议迁移为 HOLDINGS_DATABASE_ID。
        holdings_id = os.getenv("HOLDINGS_DATABASE_ID") or os.getenv("databaseID")
        values = {
            "NOTION_TOKEN": os.getenv("NOTION_TOKEN"),
            "HOLDINGS_DATABASE_ID（或旧变量 databaseID）": holdings_id,
            "SNAPSHOT_DATABASE_ID": os.getenv("SNAPSHOT_DATABASE_ID"),
            "SELL_DATABASE_ID": os.getenv("SELL_DATABASE_ID"),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise RuntimeError("缺少环境变量：" + "、".join(missing))
        return cls(
            notion_token=_clean_env(values["NOTION_TOKEN"]),
            holdings_database_id=_clean_env(values["HOLDINGS_DATABASE_ID（或旧变量 databaseID）"]),
            snapshot_database_id=_clean_env(values["SNAPSHOT_DATABASE_ID"]),
            sell_database_id=_clean_env(values["SELL_DATABASE_ID"]),
        )


def _clean_env(value: str | None) -> str:
    return (value or "").strip().strip('"“”‘’ ')


@dataclass(frozen=True)
class Quote:
    """单个标的的行情：价格 + 行情所属交易日（用于识别节假日）。"""

    price: float
    day: str  # YYYY-MM-DD


def build_session(settings: Settings) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {settings.notion_token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
            "User-Agent": f"notion-price-updater/{SCRIPT_VERSION}",
        }
    )
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST", "PATCH"}),
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _annotated_http_error(exc: requests.HTTPError, response: requests.Response) -> requests.HTTPError:
    """把 Notion 错误正文和排查提示附到异常信息，从 Actions 日志可直接定位。"""
    try:
        body = response.json()
        detail = body.get("message", "") if isinstance(body, dict) else ""
    except Exception:
        detail = (response.text or "")[:200]
    hints = {
        401: "Token 无效或已吊销：检查 Secret NOTION_TOKEN 是否为新建的 Integration Token",
        403: "没有权限：检查 Integration 是否已连接该数据库",
        404: "资源不存在：检查数据库 ID 是否正确（32 位十六进制）、Integration 是否已连接该数据库",
        429: "请求被限流：稍后手动重跑即可",
    }
    hint = hints.get(response.status_code, "")
    message = f"{exc}；Notion 返回：{detail}" + (f"。排查提示：{hint}" if hint else "")
    return requests.HTTPError(message, response=response)


def request_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    response = session.request(method, url, json=payload, headers=headers, timeout=timeout)
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise _annotated_http_error(exc, response) from exc
    if not response.content:
        return {}
    return response.json()


def query_database(
    session: requests.Session,
    database_id: str,
    *,
    filter_: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """读取数据库全部页面，正确处理分页。"""
    url = f"{NOTION_API}/databases/{database_id}/query"
    results: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        payload: dict[str, Any] = {"page_size": 100}
        if filter_:
            payload["filter"] = filter_
        if cursor:
            payload["start_cursor"] = cursor
        data = request_json(session, "POST", url, payload=payload)
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            return results
        cursor = data.get("next_cursor")
        if not cursor:
            raise RuntimeError("Notion 返回 has_more=true，但没有 next_cursor")


def _property(props: dict[str, Any], name: str) -> dict[str, Any]:
    value = props.get(name)
    return value if isinstance(value, dict) else {}


def title_value(props: dict[str, Any], name: str) -> str:
    items = _property(props, name).get("title") or []
    return "".join(item.get("plain_text", "") for item in items).strip()


def status_value(props: dict[str, Any], name: str) -> str | None:
    status = _property(props, name).get("status")
    return status.get("name") if isinstance(status, dict) else None


def number_value(props: dict[str, Any], name: str) -> float | None:
    """读取 number、formula(number) 或 rollup(number)。"""
    prop = _property(props, name)
    value = prop.get("number")
    if isinstance(value, (int, float)):
        return float(value)
    for key in ("formula", "rollup"):
        inner = prop.get(key)
        if isinstance(inner, dict) and isinstance(inner.get("number"), (int, float)):
            return float(inner["number"])
    return None


def fetch_sina_quotes(
    codes_map: dict[str, str], session: requests.Session
) -> dict[str, Quote]:
    """新浪行情。任何标的缺失或异常时整体抛错，由调用方切换后备源。"""
    if not codes_map:
        return {}
    query = ",".join(codes_map.values())
    url = "https://hq.sinajs.cn/list=" + query
    response = session.get(
        url,
        headers={"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"},
        timeout=15,
    )
    response.raise_for_status()
    response.encoding = "gbk"

    quotes: dict[str, Quote] = {}
    errors: list[str] = []
    for name, code in codes_map.items():
        match = re.search(rf'hq_str_{re.escape(code)}="([^"]*)"', response.text)
        fields = match.group(1).split(",") if match else []
        try:
            price = float(fields[3])
            # 日期字段位置可能漂移，按格式扫描定位。
            day = next((f for f in fields if re.fullmatch(r"\d{4}-\d{2}-\d{2}", f)), None)
            if price <= 0 or day is None:
                raise ValueError
            quotes[name] = Quote(price=price, day=day)
        except (IndexError, TypeError, ValueError):
            errors.append(name)
    if errors:
        raise RuntimeError("新浪行情缺失或异常：" + "、".join(errors))
    return quotes


def fetch_tencent_quotes(
    codes_map: dict[str, str], session: requests.Session
) -> dict[str, Quote]:
    """腾讯行情（后备源，对云服务器 IP 更友好）。"""
    if not codes_map:
        return {}
    query = ",".join(codes_map.values())
    url = "https://qt.gtimg.cn/q=" + query
    response = session.get(
        url,
        headers={"Referer": "https://gu.qq.com", "User-Agent": "Mozilla/5.0"},
        timeout=15,
    )
    response.raise_for_status()
    response.encoding = "gbk"

    quotes: dict[str, Quote] = {}
    errors: list[str] = []
    for name, code in codes_map.items():
        match = re.search(rf'v_{re.escape(code)}="([^"]*)"', response.text)
        fields = match.group(1).split("~") if match else []
        try:
            price = float(fields[3])
            # 不同标的的买卖盘字段数不同，时间戳位置不固定，按格式扫描定位。
            ts = next((f for f in fields if re.fullmatch(r"\d{14}", f)), None)
            if price <= 0 or ts is None:
                raise ValueError
            day = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"
            datetime.strptime(day, "%Y-%m-%d")  # 非法日期抛 ValueError
            quotes[name] = Quote(price=price, day=day)
        except (IndexError, TypeError, ValueError):
            errors.append(name)
    if errors:
        raise RuntimeError("腾讯行情缺失或异常：" + "、".join(errors))
    return quotes


def quote_sources():
    """行情源列表。调用时构建，确保运行时替换（如测试 patch）能生效。"""
    return (("新浪", fetch_sina_quotes), ("腾讯", fetch_tencent_quotes))


def fetch_quotes(
    codes_map: dict[str, str], session: requests.Session
) -> dict[str, Quote]:
    """按顺序尝试行情源；单个源必须覆盖全部标的，全部失败才报错。"""
    errors: list[str] = []
    for source_name, fetcher in quote_sources():
        try:
            quotes = fetcher(codes_map, session)
        except Exception as exc:
            log.warning("行情源%s失败：%s", source_name, exc)
            errors.append(f"{source_name}（{exc}）")
            continue
        log.info("行情源：%s", source_name)
        return quotes
    raise RuntimeError("所有行情源均失败：" + "；".join(errors))


def update_holding_prices(
    session: requests.Session,
    holdings: Iterable[dict[str, Any]],
    prices: dict[str, float],
) -> int:
    page_ids: dict[str, str] = {}
    for page in holdings:
        name = title_value(page.get("properties", {}), "资产")
        if name:
            page_ids[name] = page["id"]

    missing_pages = sorted(set(prices) - set(page_ids))
    if missing_pages:
        raise RuntimeError("持有总览找不到资产页面：" + "、".join(missing_pages))

    for name, price in prices.items():
        request_json(
            session,
            "PATCH",
            f"{NOTION_API}/pages/{page_ids[name]}",
            payload={"properties": {"当前价格": {"number": price}}},
        )
        log.info("价格已更新：%s → %s", name, price)
    return len(prices)


def aggregate_sales(sell_pages: Iterable[dict[str, Any]]) -> tuple[dict[str, dict[str, float]], float]:
    """按资产汇总已售数量、成本，并计算累计已实现利润。"""
    sold: dict[str, dict[str, float]] = {}
    realized_profit = 0.0
    for page in sell_pages:
        props = page.get("properties", {})
        name = title_value(props, "资产")
        quantity = number_value(props, "数量") or 0.0
        buy_price = number_value(props, "买入均价（rollup）") or 0.0
        profit = number_value(props, "单笔利润（自动）") or 0.0
        realized_profit += profit
        if name and quantity > 0:
            item = sold.setdefault(name, {"quantity": 0.0, "cost": 0.0})
            item["quantity"] += quantity
            item["cost"] += quantity * buy_price
    return sold, round(realized_profit, 2)


def calculate_snapshot(
    holdings: Iterable[dict[str, Any]],
    sold: dict[str, dict[str, float]],
    realized_profit: float,
    price_overrides: dict[str, float],
) -> dict[str, float]:
    """直接由价格、持有数量和成本计算，避免等待 Notion 公式刷新。"""
    total_value = 0.0
    total_invested = 0.0
    errors: list[str] = []

    for page in holdings:
        props = page.get("properties", {})
        name = title_value(props, "资产")
        if not name:
            continue
        bought_quantity = number_value(props, "数量合计（自动）") or 0.0
        bought_cost = number_value(props, "总投入（自动）") or 0.0
        sold_item = sold.get(name, {"quantity": 0.0, "cost": 0.0})
        open_quantity = max(0.0, bought_quantity - sold_item["quantity"])
        open_cost = max(0.0, bought_cost - sold_item["cost"])
        if open_quantity <= 1e-9 or status_value(props, "当前状态") == "已清仓":
            continue

        price = price_overrides.get(name)
        if price is None:
            price = number_value(props, "当前价格")
        if price is None or price <= 0:
            errors.append(f"{name}（缺少有效当前价格）")
            continue

        total_value += price * open_quantity
        total_invested += open_cost

    if errors:
        raise RuntimeError("无法生成快照：" + "；".join(errors))

    total_profit = (total_value - total_invested) + realized_profit
    return_rate = total_profit / total_invested * 100 if total_invested else 0.0
    return {
        "总市值": round(total_value, 2),
        "总投入": round(total_invested, 2),
        "已实现利润": round(realized_profit, 2),
        "总利润": round(total_profit, 2),
        "总收益率": round(return_rate, 2),
    }


def upsert_daily_snapshot(
    session: requests.Session,
    snapshot_database_id: str,
    day: str,
    metrics: dict[str, float],
) -> str:
    """同一天重复运行时更新原记录，并归档意外产生的重复记录。"""
    existing = query_database(
        session,
        snapshot_database_id,
        filter_={"property": "日期", "date": {"equals": day}},
    )
    properties: dict[str, Any] = {
        "快照日期": {"title": [{"text": {"content": day}}]},
        "日期": {"date": {"start": day}},
        **{name: {"number": value} for name, value in metrics.items()},
    }

    if existing:
        page_id = existing[0]["id"]
        request_json(
            session,
            "PATCH",
            f"{NOTION_API}/pages/{page_id}",
            payload={"properties": properties},
        )
        for duplicate in existing[1:]:
            request_json(
                session,
                "PATCH",
                f"{NOTION_API}/pages/{duplicate['id']}",
                payload={"archived": True},
            )
            log.warning("已归档重复快照：%s", duplicate["id"])
        return "updated"

    request_json(
        session,
        "POST",
        f"{NOTION_API}/pages",
        payload={
            "parent": {"database_id": snapshot_database_id},
            "properties": properties,
        },
    )
    return "created"


def run(settings: Settings, *, today: date | None = None) -> dict[str, Any]:
    session = build_session(settings)
    # 快照日期按北京时间计算；GitHub Actions 运行时为 UTC。
    day = str(today or datetime.now(MARKET_TZ).date())

    holdings = query_database(session, settings.holdings_database_id)
    active_names = {
        title_value(page.get("properties", {}), "资产")
        for page in holdings
        if status_value(page.get("properties", {}), "当前状态") != "已清仓"
    }
    targets = {name: code for name, code in ASSET_MAP.items() if name in active_names}

    quotes = fetch_quotes(targets, session)
    prices = {name: quote.price for name, quote in quotes.items()}
    updated_count = update_holding_prices(session, holdings, prices)

    result: dict[str, Any] = {
        "date": day,
        "updated_prices": updated_count,
        "quote_days": sorted({quote.day for quote in quotes.values()}),
    }

    # 节假日行情仍停留在上一交易日：只更新当前价格，不写快照，避免产生假日垃圾行。
    if result["quote_days"] != [day]:
        log.warning(
            "行情日期 %s 与今日 %s 不一致（非交易日或行情未刷新），已更新当前价格并跳过快照",
            result["quote_days"] or "（无持仓行情）",
            day,
        )
        result["snapshot_action"] = "skipped"
        return result

    sell_pages = query_database(session, settings.sell_database_id)
    sold, realized_profit = aggregate_sales(sell_pages)
    metrics = calculate_snapshot(holdings, sold, realized_profit, prices)
    result.update(metrics)
    result["snapshot_action"] = upsert_daily_snapshot(
        session, settings.snapshot_database_id, day, metrics
    )
    return result


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    log.info("notion-price-updater v%s", SCRIPT_VERSION)
    try:
        result = run(Settings.from_env())
    except Exception as exc:  # GitHub Actions 需要非零退出码明确标记失败。
        log.exception("更新失败：%s", exc)
        return 1
    if result["snapshot_action"] == "skipped":
        log.info(
            "完成：%s；价格 %s 项；快照跳过（行情日期 %s）",
            result["date"],
            result["updated_prices"],
            "、".join(result["quote_days"]) or "无",
        )
    else:
        log.info(
            "完成：%s；价格 %s 项；快照 %s；总利润 %.2f",
            result["date"],
            result["updated_prices"],
            result["snapshot_action"],
            result["总利润"],
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
