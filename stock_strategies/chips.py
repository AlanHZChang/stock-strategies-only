"""法人、大戶及融資券摘要。數值保持原始單位，通知層再換算張數。"""
from __future__ import annotations

import math
import pandas as pd

from .public_chips import get_institutional, get_large_holders, get_margin

NET_COLUMNS = ("foreign_net", "trust_net", "dealer_net", "total_net")


def _number(value):
    try:
        value = float(value)
        return round(value, 6) if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _date(value):
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def _frame(data, as_of):
    if data is None or data.empty or "date" not in data:
        return pd.DataFrame()
    data = data.copy()
    data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.normalize()
    return data.dropna(subset=["date"]).loc[lambda df: df["date"] <= as_of].sort_values("date").drop_duplicates("date", keep="last")


def _unavailable(reason="資料不足"):
    return {"status": "unavailable", "reason": reason}


def _institutional(data, calendar):
    if data.empty or not all(column in data for column in NET_COLUMNS):
        return _unavailable()
    indexed = data.set_index("date")
    latest = data.iloc[-1]
    end = latest["date"]
    daily = {column: _number(latest[column]) for column in NET_COLUMNS}
    expected = calendar[calendar <= end]
    calendar_verified = len(expected) > 0 and expected[-1] == end
    five = {"days": 0, "start_date": None, "end_date": _date(end),
            **{column: None for column in NET_COLUMNS}}
    if calendar_verified:
        window = expected[-5:]
        rows = indexed.reindex(window)[list(NET_COLUMNS)].apply(pd.to_numeric, errors="coerce")
        valid = rows.map(lambda value: _number(value) is not None).all(axis=1)
        five["days"] = int(valid.sum())
        if len(window) == 5 and valid.all():
            five.update({column: _number(rows[column].sum()) for column in NET_COLUMNS})
            five["start_date"] = _date(window[0])

    net = daily["foreign_net"]
    direction = "unknown" if net is None else "buy" if net > 0 else "sell" if net < 0 else "flat"
    streak = {"direction": direction, "days": 0, "at_least": False}
    if direction in ("buy", "sell"):
        dates = expected[::-1] if calendar_verified else [end]
        for day in dates:
            value = _number(indexed.loc[day, "foreign_net"]) if day in indexed.index else None
            if value is None:
                streak["at_least"] = True
                break
            if (value > 0 if direction == "buy" else value < 0):
                streak["days"] += 1
            else:
                break
        else:
            streak["at_least"] = True
    return {"status": "available" if all(value is not None for value in daily.values()) else "partial",
            "date": _date(end), "unit": "shares", **daily,
            "foreign_streak": streak, "five_day": five}


def _holders(data):
    if data.empty or "large_holder_pct" not in data:
        return _unavailable("集保千張大戶公開資料不足")
    latest = data.iloc[-1]
    value = _number(latest["large_holder_pct"])
    if value is None or not 0 <= value <= 100:
        return _unavailable("最新一期持股比例缺漏")
    previous_week = latest["date"].to_period("W-SUN") - 1
    previous = data[data["date"].dt.to_period("W-SUN") == previous_week]
    previous_value = _number(previous.iloc[-1]["large_holder_pct"]) if len(previous) else None
    if previous_value is not None and not 0 <= previous_value <= 100:
        previous_value = None
    change = value - previous_value if previous_value is not None else None
    return {"status": "available", "date": _date(latest["date"]), "percent": value,
            "previous_date": _date(previous.iloc[-1]["date"]) if len(previous) else None,
            "previous_percent": previous_value, "change_pp": _number(change),
            "change_pct": _number(change / previous_value * 100) if previous_value is not None and previous_value > 0 else None}


def _margin(data, calendar):
    if data.empty or not all(column in data for column in ("margin_balance", "short_balance")):
        return _unavailable()
    latest = data.iloc[-1]
    earlier = calendar[calendar < latest["date"]]
    previous_date = earlier[-1] if len(earlier) else None
    previous_rows = data[data["date"] == previous_date] if previous_date is not None else pd.DataFrame()
    result = {"status": "available", "date": _date(latest["date"]), "unit": "lots",
              "previous_date": _date(previous_date) if previous_date is not None else None}
    for prefix in ("margin", "short"):
        balance = _number(latest[f"{prefix}_balance"])
        balance = balance if balance is not None and balance >= 0 else None
        if f"{prefix}_yesterday" in data:
            previous = _number(latest[f"{prefix}_yesterday"])
        else:
            previous = _number(previous_rows.iloc[-1][f"{prefix}_balance"]) if len(previous_rows) else None
        previous = previous if previous is not None and previous >= 0 else None
        result[f"{prefix}_balance"] = balance
        result[f"{prefix}_change"] = _number(balance - previous) if balance is not None and previous is not None else None
        if balance is None:
            result["status"] = "partial"
    return result


def analyze_chips(institutional, holders, margin, prices, as_of) -> dict:
    """只使用截止日以前資料；連續天數與五日累計須對齊個股交易日。"""
    cutoff = pd.Timestamp(as_of).normalize()
    price_frame = _frame(prices, cutoff)
    calendar = pd.DatetimeIndex(price_frame["date"]) if len(price_frame) else pd.DatetimeIndex([])
    return {"as_of": _date(cutoff),
            "institutional": _institutional(_frame(institutional, cutoff), calendar),
            "large_holders": _holders(_frame(holders, cutoff)),
            "margin": _margin(_frame(margin, cutoff), calendar)}


def load_chip_summary(stock_id: str, as_of, prices: pd.DataFrame | None = None, *, force_refresh=False) -> dict:
    cutoff = pd.Timestamp(as_of).normalize()
    start = _date(cutoff - pd.Timedelta(days=120))
    trading_dates = prices["date"] if prices is not None and "date" in prices else None
    data, errors = {}, {}
    for name, fetch in (("institutional", get_institutional), ("large_holders", get_large_holders), ("margin", get_margin)):
        try:
            data[name] = fetch(stock_id, start, as_of=_date(cutoff), force_refresh=force_refresh,
                               trading_dates=trading_dates)
            if data[name].empty and data[name].attrs.get("unavailable_reason"):
                errors[name] = data[name].attrs["unavailable_reason"]
        except Exception as exc:
            data[name] = pd.DataFrame()
            code = getattr(getattr(exc, "response", None), "status_code", None)
            errors[name] = "官方公開資料暫時拒絕存取" if code in (401, 403) else "公開資料取得失敗或來源限流"
    result = analyze_chips(data["institutional"], data["large_holders"], data["margin"], prices, cutoff)
    for name, reason in errors.items():
        result[name] = _unavailable(reason)
    for name, frame in data.items():
        for key in ("source", "warning"):
            if frame.attrs.get(key):
                result[name][key] = frame.attrs[key]
    return result


def merge_chip_snapshot(fresh: dict, saved: dict | None) -> dict:
    """更新失敗時沿用有日期的舊摘要，明確標記回退而不偽裝成最新值。"""
    merged = dict(fresh)
    for name in ("institutional", "large_holders", "margin"):
        old = (saved or {}).get(name, {})
        new = fresh.get(name, {})
        if new.get("status") == "unavailable" and old.get("status") in ("available", "partial"):
            merged[name] = {**old, "fallback": True, "refresh_error": new.get("reason", "更新失敗")}
    return merged
