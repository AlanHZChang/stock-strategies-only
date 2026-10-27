"""證交所／櫃買盤後報表與集保股權分散公開資料。

依官方欄位獨立實作，端點參考：
https://github.com/rank355297-Mark/stock_chips/blob/main/fetch_chips.py
共用市場報表快取，集保僅取第 15 級，歷史週別走官方公開查詢表單。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import tempfile
import threading
import time
from datetime import datetime
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path

import pandas as pd
import requests

from .cache import _cache_dir as _finmind_cache_dir

TDCC_CSV = "https://opendata.tdcc.com.tw/getOD.ashx"
TDCC_HISTORY = "https://www.tdcc.com.tw/portal/zh/smWeb/qryStock"
ENDPOINTS = {
    ("TWSE", "institutional"): "https://www.twse.com.tw/rwd/zh/fund/T86",
    ("TPEX", "institutional"): "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade",
    ("TWSE", "margin"): "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN",
    ("TPEX", "margin"): "https://www.tpex.org.tw/www/zh-tw/margin/balance",
}
_lock = threading.RLock()
_memory = {}
_errors = {}
_last_request = 0.0


def _root():
    return Path(os.environ.get("PUBLIC_CHIPS_CACHE_DIR", str(_finmind_cache_dir().parent / "public_chips")))


def _atomic_write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _request(session, method, url, **kwargs):
    global _last_request
    with _lock:
        pause = 1.0 - (time.monotonic() - _last_request)
        if pause > 0:
            time.sleep(pause)
        _last_request = time.monotonic()
        response = session.request(method, url, timeout=(8, 20), **kwargs)
        response.raise_for_status()
        return response.content


def _text(raw):
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp950")


def _cached_get(url, params, *, refresh=False, valid=None):
    key = hashlib.sha256(json.dumps([url, params], sort_keys=True).encode()).hexdigest()
    path = _root() / "responses" / (key + ".dat")
    memory_key = (str(_root()), key)
    with _lock:
        cached = _memory.get(memory_key)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1]
        if not refresh and path.exists() and time.time() - path.stat().st_mtime < 6 * 3600:
            raw = path.read_bytes()
            if valid is None or valid(raw):
                _memory[memory_key] = (time.monotonic(), raw)
                return raw
        failed = _errors.get(memory_key)
        if failed and time.monotonic() - failed[0] < 60:
            raise failed[1]
        try:
            with requests.Session() as session:
                session.headers["User-Agent"] = "Mozilla/5.0"
                raw = _request(session, "GET", url, params=params)
        except requests.RequestException as exc:
            _errors[memory_key] = (time.monotonic(), exc)
            raise
        _errors.pop(memory_key, None)
        if valid is None or valid(raw):
            _atomic_write(path, raw)
            _memory[memory_key] = (time.monotonic(), raw)
        return raw


def _number(value):
    try:
        result = float(str(value).replace(",", "").replace("%", "").strip())
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _day(value):
    parts = re.findall(r"\d+", str(value))
    if len(parts) == 1 and len(parts[0]) in (7, 8):
        text = parts[0]
        width = len(text) - 4
        parts = [text[:width], text[width:width + 2], text[width + 2:]]
    if len(parts) != 3:
        raise ValueError("公開報表缺少有效資料日期")
    year, month, day = map(int, parts)
    return pd.Timestamp(year=year + 1911 if year < 1911 else year, month=month, day=day)


def _tables(payload):
    return [payload] if "data" in payload else payload.get("tables", [])


def _report_has_rows(raw):
    payload = json.loads(_text(raw))
    return str(payload.get("stat", "")).lower() == "ok" and any(table.get("data") for table in _tables(payload))


def _report(market, kind, day, refresh):
    date = pd.Timestamp(day).normalize()
    params = {"date": date.strftime("%Y%m%d" if market == "TWSE" else "%Y/%m/%d"), "response": "json"}
    if market == "TWSE":
        params["selectType"] = "ALLBUT0999" if kind == "institutional" else "ALL"
    elif kind == "institutional":
        params.update(type="Daily", sect="EW")
    # 最新兩日重抓可取得盤後更正；更早日期共用磁碟快取，避免逐檔下載。
    recent = date >= pd.Timestamp.now().normalize() - pd.Timedelta(days=2)
    raw = _cached_get(ENDPOINTS[market, kind], params, refresh=refresh and recent, valid=_report_has_rows)
    payload = json.loads(_text(raw))
    if not _report_has_rows(raw):
        return None
    report_date = payload.get("date") or next((table.get("date") for table in _tables(payload) if table.get("date")), None)
    if _day(report_date) != date:
        raise ValueError("官方報表日期與查詢日不符")
    return payload


@lru_cache(maxsize=256)
def _parse_report(market, kind, raw):
    """以欄位名或經核對的重複欄位結構解析，未知 schema 不套舊位置。"""
    payload = json.loads(raw)
    rows, matched = {}, False
    for table in _tables(payload):
        fields = [str(field).strip() for field in table.get("fields", [])]
        if not fields or fields[0] not in ("證券代號", "代號"):
            continue
        if kind == "institutional" and market == "TWSE":
            required = ("外陸資買賣超股數(不含外資自營商)", "外資自營商買賣超股數", "投信買賣超股數", "自營商買賣超股數", "三大法人買賣超股數")
            if not all(name in fields for name in required):
                raise ValueError("證交所法人欄位已變更")
            positions = [fields.index(name) for name in required]
        elif kind == "institutional":
            if len(fields) < 24 or any(fields[index] != "買賣超股數" for index in (10, 13, 22)) or fields[23] != "三大法人買賣超股數合計":
                raise ValueError("櫃買法人欄位已變更")
            positions = [10, 13, 22, 23]
        elif market == "TWSE":
            if len(fields) < 13 or fields[5] != "前日餘額" or fields[6] != "今日餘額" or fields[11] != "前日餘額" or fields[12] != "今日餘額":
                continue
            positions = [6, 12, 5, 11]
        else:
            required = ("資餘額", "券餘額", "前資餘額(張)", "前券餘額(張)")
            if not all(name in fields for name in required):
                continue
            positions = [fields.index(name) for name in required]
        matched = True
        for row in table.get("data", []):
            if len(row) <= max(positions):
                continue
            values = [_number(row[index]) for index in positions]
            if kind == "institutional":
                if market == "TWSE":
                    foreign = values[0] + values[1] if values[0] is not None and values[1] is not None else None
                    values = [foreign] + values[2:]
                names = ("foreign_net", "trust_net", "dealer_net", "total_net")
            else:
                names = ("margin_balance", "short_balance", "margin_yesterday", "short_yesterday")
            rows[str(row[0]).strip()] = dict(zip(names, values))
    if not matched:
        raise ValueError("官方盤後報表欄位已變更")
    return rows


def _market_rows(market, kind, day, refresh):
    payload = _report(market, kind, day, refresh)
    return _parse_report(market, kind, json.dumps(payload, ensure_ascii=False)) if payload else {}


def _query_dates(start, as_of, trading_dates, count):
    cutoff = pd.Timestamp(as_of or datetime.now().date()).normalize()
    if trading_dates is None:
        dates = pd.bdate_range(pd.Timestamp(start), cutoff)
    else:
        dates = pd.DatetimeIndex(pd.to_datetime(trading_dates, errors="coerce")).dropna().normalize().unique().sort_values()
        dates = dates[(dates >= pd.Timestamp(start)) & (dates <= cutoff)]
    return dates[-count:][::-1]


def _daily(stock_id, start, as_of, refresh, trading_dates, kind):
    dates = _query_dates(start, as_of, trading_dates, 20 if kind == "institutional" else 3)
    rows, market, failures = [], None, False
    disabled = set()
    for index, day in enumerate(dates):
        choices = [market] if market else ["TWSE", "TPEX"]
        for candidate in choices:
            if candidate in disabled:
                continue
            try:
                table = _market_rows(candidate, kind, day, refresh)
            except requests.RequestException:
                failures = True
                disabled.add(candidate)
                continue
            except ValueError:
                failures = True
                continue
            if str(stock_id) in table:
                market = candidate
                rows.append({"date": day, **table[str(stock_id)]})
                break
        if kind == "margin" and rows:
            break
        if (market and market in disabled) or len(disabled) == 2 or (market is None and index >= 2):
            break
    result = pd.DataFrame(rows)
    if len(result):
        result = result.sort_values("date").reset_index(drop=True)
    result.attrs["source"] = market or "TWSE/TPEX"
    result.attrs["unavailable_reason"] = "公開報表下載失敗或欄位已變更" if failures else "證交所／櫃買盤後資料尚未公布或查無此股票"
    if failures and rows:
        result.attrs["warning"] = "部分公開報表更新失敗，僅使用已取得日期；缺日不補零"
    return result


def get_institutional(stock_id, start, as_of=None, *, force_refresh=False, trading_dates=None):
    return _daily(stock_id, start, as_of, force_refresh, trading_dates, "institutional")


def get_margin(stock_id, start, as_of=None, *, force_refresh=False, trading_dates=None):
    return _daily(stock_id, start, as_of, force_refresh, trading_dates, "margin")


@lru_cache(maxsize=4)
def _tdcc_csv(raw):
    reader = csv.DictReader(io.StringIO(_text(raw)))
    required = ("資料日期", "證券代號", "持股分級")
    if not reader.fieldnames or not all(name in reader.fieldnames for name in required):
        raise ValueError("集保 CSV 欄位已變更")
    percent_key = next((key for key in reader.fieldnames if key.startswith("占集保庫存數比例")), None)
    if percent_key is None:
        raise ValueError("集保 CSV 缺少持股比例欄位")
    weeks = {}
    for row in reader:
        if row["持股分級"].strip() != "15":
            continue
        day = _day(row["資料日期"]).strftime("%Y%m%d")
        value = _number(row.get(percent_key))
        if value is not None and 0 <= value <= 100:
            weeks.setdefault(day, {})[row["證券代號"].strip()] = value
    return weeks


class _HistoryPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inputs, self.dates, self.selected, self.rows = {}, [], None, []
        self.select_name, self.row, self.cell = None, [], None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name"):
            self.inputs[attrs["name"]] = attrs.get("value", "")
        elif tag == "select":
            self.select_name = attrs.get("name")
        elif tag == "option" and self.select_name == "scaDate" and re.fullmatch(r"\d{8}", attrs.get("value", "")):
            self.dates.append(attrs["value"])
            if self.select_name == "scaDate" and "selected" in attrs:
                self.selected = attrs["value"]
        elif tag == "tr":
            self.row = []
        elif tag == "td":
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == "select":
            self.select_name = None
        elif tag == "td" and self.cell is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row:
            self.rows.append(self.row)


def _history_page(session):
    page = _HistoryPage()
    page.feed(_text(_request(session, "GET", TDCC_HISTORY)))
    if not page.dates or not page.inputs.get("SYNCHRONIZER_TOKEN"):
        raise ValueError("集保公開查詢表單缺少週別或 token")
    return page


def _history_value(session, page, stock_id, day):
    if day not in page.dates:
        return None
    fields = {"SYNCHRONIZER_TOKEN": page.inputs["SYNCHRONIZER_TOKEN"],
              "SYNCHRONIZER_URI": "/portal/zh/smWeb/qryStock", "method": "submit",
              "firDate": max(page.dates), "scaDate": day, "sqlMethod": "StockNo", "stockNo": str(stock_id), "stockName": ""}
    result = _HistoryPage()
    result.feed(_text(_request(session, "POST", TDCC_HISTORY, data=fields)))
    if result.selected != day:
        raise ValueError("集保歷史回覆週別與查詢週別不符")
    for row in result.rows:
        if len(row) >= 5 and row[0] == "15" and "1,000,001" in row[1]:
            value = _number(row[4])
            if value is not None and 0 <= value <= 100:
                return value
    return None


def _saved_weeks():
    weeks = {}
    for path in _root().glob("tdcc-week-*.json"):
        match = re.fullmatch(r"tdcc-week-(\d{8})\.json", path.name)
        if match:
            try:
                values = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(values, dict):
                    _day(match.group(1))
                    weeks[match.group(1)] = {str(stock): value for stock, raw in values.items()
                                            if (value := _number(raw)) is not None and 0 <= value <= 100}
            except (OSError, ValueError):
                continue
    return weeks


def _save_week(day, values):
    with _lock:
        path = _root() / f"tdcc-week-{day}.json"
        existing = {}
        if path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                existing = saved if isinstance(saved, dict) else {}
            except (OSError, ValueError):
                pass
        combined = {**existing, **values}
        if combined != existing or not path.exists():
            _atomic_write(path, json.dumps(combined, allow_nan=False).encode())


def get_large_holders(stock_id, start, as_of=None, *, force_refresh=False, trading_dates=None):
    cutoff = pd.Timestamp(as_of or datetime.now().date()).normalize()
    stock_id = str(stock_id)
    failure = None
    try:
        raw = _cached_get(TDCC_CSV, {"id": "1-5"}, refresh=force_refresh, valid=lambda value: bool(_tdcc_csv(value)))
        csv_weeks = _tdcc_csv(raw)
        if not csv_weeks:
            raise ValueError("集保 CSV 尚無千張大戶級距")
        for day, values in csv_weeks.items():
            _save_week(day, values)
    except (requests.RequestException, ValueError) as exc:
        failure = exc
    weeks = _saved_weeks()
    available = sorted(day for day, values in weeks.items() if _day(day) <= cutoff and stock_id in values)
    latest = available[-1] if available else None
    previous = _day(latest).to_period("W-SUN") - 1 if latest else None
    has_previous = any(_day(day).to_period("W-SUN") == previous for day in available) if latest else False
    # CSV 下載失敗時仍查官方最新週別，不能因舊快取已有兩週便停止更新。
    if failure is not None or not latest or not has_previous:
        try:
            with requests.Session() as session:
                session.headers["User-Agent"] = "Mozilla/5.0"
                page = _history_page(session)
                dates = sorted({day for day in page.dates if _day(day) <= cutoff}, reverse=True)
                if dates:
                    target = max(latest, dates[0]) if latest else dates[0]
                    previous_week = _day(target).to_period("W-SUN") - 1
                    prior = next((day for day in dates if _day(day).to_period("W-SUN") == previous_week), None)
                    pending = [day for day in (target, prior) if day is not None and stock_id not in weeks.get(day, {})]
                    for index, day in enumerate(pending):
                        if index:
                            page = _history_page(session)
                        value = _history_value(session, page, stock_id, day)
                        if value is not None:
                            _save_week(day, {stock_id: value})
                            weeks.setdefault(day, {})[stock_id] = value
        except (requests.RequestException, ValueError) as exc:
            failure = exc
    rows = [{"date": _day(day), "large_holder_pct": _number(values[stock_id])}
            for day, values in weeks.items() if pd.Timestamp(start) <= _day(day) <= cutoff and stock_id in values]
    result = pd.DataFrame(rows, columns=["date", "large_holder_pct"]).sort_values("date").reset_index(drop=True)
    result.attrs["source"] = "TDCC"
    if failure is not None:
        result.attrs["warning"] = "集保部分更新失敗，使用已有日期的公開資料"
    result.attrs["unavailable_reason"] = "集保公開資料尚未取得，或指定週別不在可查範圍"
    return result
