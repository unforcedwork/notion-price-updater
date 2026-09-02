import os
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import update_prices as app


class FakeResponse:
    def __init__(self, data=None, text="", status_code=200):
        self._data = data or {}
        self.text = text
        self.status_code = status_code
        self.content = text.encode() if text else (b"{}" if data is not None else b"")
        self.encoding = None

    def raise_for_status(self):
        if self.status_code >= 400:
            raise app.requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


class QueueSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append((method, url, json))
        return self.responses.pop(0)

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, None))
        return self.responses.pop(0)


def title(name):
    return {"title": [{"plain_text": name}]}


def number(value):
    return {"number": value}


def rollup(value):
    return {"rollup": {"number": value}}


def formula(value):
    return {"formula": {"number": value}}


def holding(name, qty, cost, price=None, status="持有", page_id="p"):
    return {
        "id": page_id,
        "properties": {
            "资产": title(name),
            "数量合计（自动）": rollup(qty),
            "总投入（自动）": rollup(cost),
            "当前价格": number(price),
            "当前状态": {"status": {"name": status}},
        },
    }


def sale(name, qty, buy_price, profit):
    return {
        "properties": {
            "资产": title(name),
            "数量": number(qty),
            "买入均价（rollup）": rollup(buy_price),
            "单笔利润（自动）": formula(profit),
        }
    }


def sina_text(code, name, price, day="2026-09-01"):
    fields = [name, "0", "0", str(price)] + ["0"] * 26 + [day, "15:05:00"]
    return f'var hq_str_{code}="' + ",".join(fields) + '";'


def tencent_text(code, name, price, ts="20260901150500"):
    fields = ["51", name, code, str(price)] + ["0"] * 26 + [ts, "-1.0"]
    return f'v_{code}="' + "~".join(fields) + '";'


class SettingsTests(unittest.TestCase):
    def test_old_database_env_is_supported(self):
        env = {
            "NOTION_TOKEN": " token ",
            "databaseID": "holdings",
            "SNAPSHOT_DATABASE_ID": "snapshots",
            "SELL_DATABASE_ID": "sales",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = app.Settings.from_env()
        self.assertEqual(settings.holdings_database_id, "holdings")
        self.assertEqual(settings.notion_token, "token")


class ApiTests(unittest.TestCase):
    def test_query_database_paginates(self):
        session = QueueSession(
            [
                FakeResponse({"results": [{"id": "1"}], "has_more": True, "next_cursor": "next"}),
                FakeResponse({"results": [{"id": "2"}], "has_more": False}),
            ]
        )
        rows = app.query_database(session, "db")
        self.assertEqual([row["id"] for row in rows], ["1", "2"])
        self.assertEqual(session.calls[1][2]["start_cursor"], "next")

    def test_fetch_sina_quotes(self):
        text = sina_text("sz000858", "五粮液", 101.23) + "\n" + sina_text(
            "sh513500", "标普500", 2.456
        )
        session = QueueSession([FakeResponse(text=text)])
        quotes = app.fetch_sina_quotes(
            {"五粮液": "sz000858", "标普500": "sh513500"}, session
        )
        self.assertEqual(
            quotes,
            {
                "五粮液": app.Quote(101.23, "2026-09-01"),
                "标普500": app.Quote(2.456, "2026-09-01"),
            },
        )
        self.assertTrue(session.calls[0][1].startswith("https://hq.sinajs.cn/list="))

    def test_fetch_sina_quotes_fails_on_partial_result(self):
        session = QueueSession([FakeResponse(text=sina_text("sz000858", "五粮液", 0))])
        with self.assertRaisesRegex(RuntimeError, "五粮液"):
            app.fetch_sina_quotes({"五粮液": "sz000858"}, session)

    def test_fetch_tencent_quotes(self):
        text = tencent_text("sz000858", "五粮液", 70.83) + "\n" + tencent_text(
            "sh513500", "标普500ETF", 2.644
        )
        session = QueueSession([FakeResponse(text=text)])
        quotes = app.fetch_tencent_quotes(
            {"五粮液": "sz000858", "标普500": "sh513500"}, session
        )
        self.assertEqual(
            quotes,
            {
                "五粮液": app.Quote(70.83, "2026-09-01"),
                "标普500": app.Quote(2.644, "2026-09-01"),
            },
        )
        self.assertTrue(session.calls[0][1].startswith("https://qt.gtimg.cn/q="))

    def test_fetch_tencent_quotes_fails_on_bad_timestamp(self):
        session = QueueSession(
            [FakeResponse(text=tencent_text("sz000858", "五粮液", 70.83, ts="0"))]
        )
        with self.assertRaisesRegex(RuntimeError, "五粮液"):
            app.fetch_tencent_quotes({"五粮液": "sz000858"}, session)

    def test_fetch_quotes_falls_back_to_tencent(self):
        session = QueueSession([])
        with patch.object(
            app, "fetch_sina_quotes", side_effect=RuntimeError("被拦截")
        ), patch.object(
            app,
            "fetch_tencent_quotes",
            return_value={"五粮液": app.Quote(70.0, "2026-09-01")},
        ) as mock_tx:
            quotes = app.fetch_quotes({"五粮液": "sz000858"}, session)
        self.assertEqual(quotes, {"五粮液": app.Quote(70.0, "2026-09-01")})
        mock_tx.assert_called_once()

    def test_fetch_quotes_raises_when_all_sources_fail(self):
        session = QueueSession([])
        with patch.object(
            app, "fetch_sina_quotes", side_effect=RuntimeError("a")
        ), patch.object(app, "fetch_tencent_quotes", side_effect=RuntimeError("b")):
            with self.assertRaisesRegex(RuntimeError, "所有行情源均失败"):
                app.fetch_quotes({"五粮液": "sz000858"}, session)


class CalculationTests(unittest.TestCase):
    def test_partial_sale_reduces_quantity_and_cost(self):
        holdings = [holding("测试资产", qty=10, cost=100, price=12)]
        sold, realized = app.aggregate_sales([sale("测试资产", qty=4, buy_price=10, profit=8)])
        metrics = app.calculate_snapshot(holdings, sold, realized, {})
        self.assertEqual(metrics["总市值"], 72.0)
        self.assertEqual(metrics["总投入"], 60.0)
        self.assertEqual(metrics["已实现利润"], 8.0)
        self.assertEqual(metrics["总利润"], 20.0)
        self.assertEqual(metrics["总收益率"], 33.33)

    def test_new_price_overrides_stale_notion_value(self):
        holdings = [holding("测试资产", qty=2, cost=10, price=4)]
        metrics = app.calculate_snapshot(holdings, {}, 0, {"测试资产": 6})
        self.assertEqual(metrics["总市值"], 12.0)
        self.assertEqual(metrics["总利润"], 2.0)

    def test_open_asset_without_price_blocks_snapshot(self):
        holdings = [holding("测试资产", qty=2, cost=10, price=None)]
        with self.assertRaisesRegex(RuntimeError, "缺少有效当前价格"):
            app.calculate_snapshot(holdings, {}, 0, {})


class SnapshotTests(unittest.TestCase):
    def test_upsert_updates_first_and_archives_duplicate(self):
        session = QueueSession(
            [
                FakeResponse({"results": [{"id": "a"}, {"id": "b"}], "has_more": False}),
                FakeResponse({"id": "a"}),
                FakeResponse({"id": "b"}),
            ]
        )
        action = app.upsert_daily_snapshot(
            session,
            "snap-db",
            "2026-08-06",
            {"总市值": 100, "总投入": 90, "已实现利润": 1, "总利润": 11, "总收益率": 12.22},
        )
        self.assertEqual(action, "updated")
        self.assertEqual(session.calls[1][0], "PATCH")
        self.assertEqual(session.calls[2][2], {"archived": True})

    def test_upsert_creates_when_missing(self):
        session = QueueSession(
            [FakeResponse({"results": [], "has_more": False}), FakeResponse({"id": "new"})]
        )
        action = app.upsert_daily_snapshot(
            session,
            "snap-db",
            "2026-08-06",
            {"总市值": 100, "总投入": 90, "已实现利润": 1, "总利润": 11, "总收益率": 12.22},
        )
        self.assertEqual(action, "created")
        self.assertEqual(session.calls[1][0], "POST")


class RunTests(unittest.TestCase):
    def _settings(self):
        return app.Settings("token", "holdings-db", "snapshot-db", "sell-db")

    def test_run_skips_snapshot_on_non_trading_day(self):
        """节假日行情日期仍停留在上一交易日：更新价格但跳过快照。"""
        holdings = [holding("五粮液", qty=100, cost=11400, price=71.0)]
        with patch.object(app, "build_session", return_value=object()), patch.object(
            app, "query_database", return_value=holdings
        ) as mock_query, patch.object(
            app,
            "fetch_quotes",
            return_value={"五粮液": app.Quote(70.0, "2026-08-28")},
        ), patch.object(
            app, "update_holding_prices", return_value=1
        ) as mock_update, patch.object(
            app, "upsert_daily_snapshot"
        ) as mock_upsert:
            result = app.run(self._settings(), today=date(2026, 8, 31))
        self.assertEqual(result["snapshot_action"], "skipped")
        self.assertEqual(result["quote_days"], ["2026-08-28"])
        mock_update.assert_called_once()
        mock_upsert.assert_not_called()
        mock_query.assert_called_once()  # 跳过快照时不再查询卖出记录

    def test_run_uses_beijing_date_by_default(self):
        """UTC 仍在上一天时，快照日期应取北京时间当天。"""
        holdings = [holding("五粮液", qty=100, cost=11400, price=71.0)]
        beijing_now = datetime(2026, 9, 2, 1, 30, tzinfo=app.MARKET_TZ)  # UTC 2026-09-01 17:30
        with patch.object(app, "build_session", return_value=object()), patch.object(
            app, "datetime"
        ) as mock_dt, patch.object(
            app, "query_database", side_effect=[holdings, []]
        ), patch.object(
            app,
            "fetch_quotes",
            return_value={"五粮液": app.Quote(70.0, "2026-09-02")},
        ), patch.object(app, "update_holding_prices", return_value=1), patch.object(
            app, "upsert_daily_snapshot", return_value="created"
        ) as mock_upsert:
            mock_dt.now.return_value = beijing_now
            result = app.run(self._settings())
        self.assertEqual(result["date"], "2026-09-02")
        self.assertEqual(mock_upsert.call_args[0][2], "2026-09-02")
        self.assertEqual(result["snapshot_action"], "created")
        self.assertEqual(result["总市值"], 7000.0)


# 2026-09-02 从 qt.gtimg.cn 抓取的真实报文（三只当前持仓）
REAL_TENCENT_PAYLOAD = (
    'v_sz000858="51~五 粮 液~000858~70.83~71.83~71.61~257811~76794~180996~70.82~4~0.00~138~0.00~0~0.00~0~0.00~0~70.82~4~0.00~0~0.00~0~0.00~0~0.00~0~~20260902145700~-1.00~-1.39~71.76~70.81~70.83/257811/1833340752~257811~183334~0.66~21.01~~71.76~70.81~1.32~2749.28~2749.34~2.32~79.01~64.65~1.00~138~71.11~15.71~30.70~~~0.26~183334.0752~0.0000~0~ A~GP-A~-31.47~-1.49~7.28~11.04~7.26~126.34~67.94~-1.47~-6.19~-7.45~3881513391~3881608005~94.52~-35.49~3881513391~~~-42.60~-0.04~~CNY~0~~70.88~-70~";\n'
    'v_sz000400="51~许继电气~000400~21.06~21.49~21.40~93822~36498~57288~0.00~0~0.00~0.00~0~0.00~0~0.00~0~0.00~0~0.00~0~0.00~0~0.00~0~0.00~0~0.00~0~~20260902145700~-0.43~-2.00~21.45~21.04~21.06/93822/198177816~93822~19818~0.92~22.60~~21.45~21.04~1.91~213.77~214.52~1.74~23.64~19.34~0.88~0~21.12~25.79~18.38~~~1.12~19817.7816~0.0000~0~ A~GP-A~-17.38~-6.19~2.18~7.68~4.09~34.43~19.37~-5.69~-10.31~-4.83~1015065609~1018622249~~-17.76~1015065609~~~-5.31~0.00~~CNY~0~~21.15~-107~";\n'
    'v_sh513500="1~标普500ETF博时~513500~2.644~2.716~2.679~2227767~850519~1376234~2.643~8904~2.642~5277~2.641~5102~2.640~9076~2.639~1209~2.644~6027~2.645~3829~2.646~2976~2.647~5737~2.648~3192~~20260902145656~-0.072~-2.65~2.681~2.642~2.644/2227767/591906236~2227767~59191~2.18~~~2.681~2.642~1.44~270.08~270.08~0.00~2.988~2.444~2.27~7807~2.657~~~~~~59190.6236~0.0000~0~ A~ETF~8.72~-2.36~~~~2.778~2.130~0.08~-0.94~3.61~10214638600~10214638600~15.21~7.87~10214638600~8.04~2.4472~20.90~0.00~2.4652~CNY~0~___D__F__Y~2.650~-8334~";'
)


class RealDataTests(unittest.TestCase):
    def test_tencent_parser_handles_real_payload(self):
        """用生产环境真实报文验证腾讯解析。"""
        session = QueueSession([FakeResponse(text=REAL_TENCENT_PAYLOAD)])
        quotes = app.fetch_tencent_quotes(
            {"五粮液": "sz000858", "许继电气": "sz000400", "标普500": "sh513500"},
            session,
        )
        self.assertEqual(quotes["五粮液"], app.Quote(70.83, "2026-09-02"))
        self.assertEqual(quotes["许继电气"], app.Quote(21.06, "2026-09-02"))
        self.assertEqual(quotes["标普500"], app.Quote(2.644, "2026-09-02"))


class ErrorHintTests(unittest.TestCase):
    def test_404_includes_connection_hint(self):
        session = QueueSession(
            [FakeResponse(data={"message": "Could not find database"}, status_code=404)]
        )
        with self.assertRaisesRegex(app.requests.HTTPError, "Integration 是否已连接该数据库"):
            app.request_json(
                session, "POST", "https://api.notion.com/v1/databases/x/query", payload={}
            )

    def test_401_includes_token_hint(self):
        session = QueueSession(
            [FakeResponse(data={"message": "Unauthorized"}, status_code=401)]
        )
        with self.assertRaisesRegex(app.requests.HTTPError, "NOTION_TOKEN"):
            app.request_json(
                session, "POST", "https://api.notion.com/v1/databases/x/query", payload={}
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
