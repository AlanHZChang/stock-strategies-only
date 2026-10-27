"""各 FinMind dataset 的 point-in-time loader。

通則：每個 loader (a) 呼叫 fetch_finmind_cached；(b) rename 正規化；
(c) to_datetime + to_numeric(coerce)；(d) 依 as_of 切片（傳 end_date）；
(e) 空資料回空 DataFrame（不 raise），讓因子層判中性。
as_of 是避免 look-ahead 的單一機制。
"""
from __future__ import annotations

import pandas as pd

from .cache import fetch_finmind_cached, FinMindRateLimitError


def _require_cols(df: pd.DataFrame, cols: list[str]) -> bool:
    return all(c in df.columns for c in cols)


def get_institutional(stock_id: str, start: str, as_of: str | None = None, *, force_refresh: bool = False) -> pd.DataFrame:
    """三大法人買賣超（日）。回欄位:
       date, foreign_net, trust_net, dealer_net, total_net（單位：股）。
    FinMind name 欄分桶：Foreign* → 外資、Investment_Trust → 投信、
    Dealer*（self+Hedging）→ 自營；net = buy - sell。"""
    try:
        df = fetch_finmind_cached(
            "TaiwanStockInstitutionalInvestorsBuySell", stock_id, start, end_date=as_of,
            fresh_days=-1 if force_refresh else None, **({"max_retries": 0} if force_refresh else {}),
        )
    except FinMindRateLimitError:
        return pd.DataFrame()
    if df.empty or not _require_cols(df, ["date", "name", "buy", "sell"]):
        return pd.DataFrame()
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).drop_duplicates(["date", "name"], keep="last")
    if as_of:
        df = df[df["date"] <= pd.Timestamp(as_of)]
    df["buy"] = pd.to_numeric(df["buy"], errors="coerce")
    df["sell"] = pd.to_numeric(df["sell"], errors="coerce")
    df["net"] = df["buy"] - df["sell"]

    def bucket(name: str) -> str:
        n = str(name)
        if n.startswith("Foreign"):
            return "foreign_net"
        if n.startswith("Investment_Trust"):
            return "trust_net"
        if n.startswith("Dealer"):
            return "dealer_net"
        return "other"

    df["bucket"] = df["name"].map(bucket)
    df = df[df["bucket"] != "other"]
    # 同日舊制 Dealer 合計與新制分項並存時，只取自行買賣／避險，避免重複加總。
    split_dates = df.loc[df["name"].isin(["Dealer_self", "Dealer_Hedging"]), "date"]
    df = df[~((df["name"] == "Dealer") & df["date"].isin(split_dates))]
    wide = df.groupby(["date", "bucket"])["net"].agg(lambda values: values.sum(min_count=len(values))).unstack().reset_index()
    for col in ["foreign_net", "trust_net", "dealer_net"]:
        if col not in wide.columns:
            wide[col] = float("nan")
    wide["total_net"] = wide["foreign_net"] + wide["trust_net"] + wide["dealer_net"]
    return wide[["date", "foreign_net", "trust_net", "dealer_net", "total_net"]].sort_values("date").reset_index(drop=True)


def get_month_revenue(stock_id: str, start: str, as_of: str | None = None) -> pd.DataFrame:
    """月營收（月）。回欄位:
       avail_date(資料可得日≈次月10日), revenue_year, revenue_month, revenue, mom, yoy。
    防 look-ahead：as_of 切片用 avail_date <= as_of（非所屬月）。"""
    try:
        # 月營收全抓（不在快取層用 as_of 切，因 avail_date 在此才算得出）
        df = fetch_finmind_cached("TaiwanStockMonthRevenue", stock_id, start)
    except FinMindRateLimitError:
        return pd.DataFrame()
    if df.empty or not _require_cols(df, ["revenue_year", "revenue_month", "revenue"]):
        return pd.DataFrame()
    df = df.copy()
    for c in ["revenue_year", "revenue_month", "revenue"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["revenue_year", "revenue_month"])
    df["period"] = pd.to_datetime(
        df["revenue_year"].astype(int).astype(str) + "-"
        + df["revenue_month"].astype(int).astype(str).str.zfill(2) + "-01"
    )
    # 公布日保守估：所屬月底 + 10 天（次月 10 日，法規上限）
    df["avail_date"] = df["period"] + pd.offsets.MonthEnd(0) + pd.Timedelta(days=10)
    df = df.sort_values("period").reset_index(drop=True)
    df["mom"] = df["revenue"].pct_change()
    # YoY 以「去年同月」對齊（用 period 映射），避免月份缺口時 pct_change(12) 位置偏移算錯
    rev_by_period = dict(zip(df["period"], df["revenue"]))

    def _yoy(row):
        base = rev_by_period.get(row["period"] - pd.DateOffset(years=1))
        if base and pd.notna(base) and pd.notna(row["revenue"]):
            return row["revenue"] / base - 1
        return float("nan")

    df["yoy"] = df.apply(_yoy, axis=1)
    if as_of:
        df = df[df["avail_date"] <= pd.to_datetime(as_of)]
    return df[["avail_date", "period", "revenue_year", "revenue_month",
               "revenue", "mom", "yoy"]].reset_index(drop=True)


def get_valuation(stock_id: str, start: str, as_of: str | None = None) -> pd.DataFrame:
    """估值（日）。回 date, per, pbr, dividend_yield。per<=0(虧損)→NaN。
    缺關鍵欄位 → 回空（韌性，不 KeyError）。"""
    try:
        df = fetch_finmind_cached("TaiwanStockPER", stock_id, start, end_date=as_of)
    except FinMindRateLimitError:
        return pd.DataFrame()
    if df.empty:
        return pd.DataFrame()
    rename = {"PER": "per", "PBR": "pbr", "dividend_yield": "dividend_yield"}
    df = df.rename(columns=rename)
    if not _require_cols(df, ["date", "per", "pbr"]):
        return pd.DataFrame()
    df = df.copy()
    for c in ["per", "pbr", "dividend_yield"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df.loc[df["per"] <= 0, "per"] = pd.NA
    df["per"] = pd.to_numeric(df["per"], errors="coerce")
    keep = [c for c in ["date", "per", "pbr", "dividend_yield"] if c in df.columns]
    return df[keep].sort_values("date").reset_index(drop=True)


def get_margin(stock_id: str, start: str, as_of: str | None = None, *, force_refresh: bool = False) -> pd.DataFrame:
    """融資融券（日）。回 date, margin_balance, short_balance,
       margin_chg, short_chg, short_margin_ratio(券資比)。"""
    try:
        df = fetch_finmind_cached(
            "TaiwanStockMarginPurchaseShortSale", stock_id, start, end_date=as_of,
            fresh_days=-1 if force_refresh else None, **({"max_retries": 0} if force_refresh else {}),
        )
    except FinMindRateLimitError:
        return pd.DataFrame()
    rename = {
        "MarginPurchaseTodayBalance": "margin_balance",
        "ShortSaleTodayBalance": "short_balance",
        "MarginPurchaseYesterdayBalance": "margin_yesterday",
        "ShortSaleYesterdayBalance": "short_yesterday",
    }
    df = df.rename(columns=rename)
    if df.empty or not _require_cols(df, ["date", "margin_balance", "short_balance"]):
        return pd.DataFrame()
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).drop_duplicates("date", keep="last")
    if as_of:
        df = df[df["date"] <= pd.Timestamp(as_of)]
    for c in ["margin_balance", "short_balance", "margin_yesterday", "short_yesterday"]:
        if c not in df:
            continue
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.sort_values("date").reset_index(drop=True)
    df["margin_chg"] = df["margin_balance"].diff()
    df["short_chg"] = df["short_balance"].diff()
    # 公告的昨日餘額可避免資料缺日、除權等情況被當成前一筆資料比較。
    for balance, previous, change in (("margin_balance", "margin_yesterday", "margin_chg"),
                                      ("short_balance", "short_yesterday", "short_chg")):
        if previous in df:
            df[change] = df[balance] - df[previous]
    df["short_margin_ratio"] = df["short_balance"] / df["margin_balance"].replace(0, pd.NA)
    columns = ["date", "margin_balance", "short_balance", "margin_chg", "short_chg", "short_margin_ratio"]
    columns += [column for column in ("margin_yesterday", "short_yesterday") if column in df]
    return df[columns].reset_index(drop=True)


def get_shareholding(stock_id: str, start: str, as_of: str | None = None) -> pd.DataFrame:
    """外資持股比例（週/不定期）。回 date, foreign_ratio（pct）。
    頻率不規則 → 因子層用 asof_row 取最近一筆。"""
    try:
        df = fetch_finmind_cached("TaiwanStockShareholding", stock_id, start, end_date=as_of)
    except FinMindRateLimitError:
        return pd.DataFrame()
    df = df.rename(columns={"ForeignInvestmentSharesRatio": "foreign_ratio"})
    if df.empty or not _require_cols(df, ["date", "foreign_ratio"]):
        return pd.DataFrame()
    df = df.copy()
    df["foreign_ratio"] = pd.to_numeric(df["foreign_ratio"], errors="coerce")
    if as_of:
        df = df[df["date"] <= pd.to_datetime(as_of)]
    return df[["date", "foreign_ratio"]].sort_values("date").reset_index(drop=True)


def get_stock_info(refresh: bool = False) -> pd.DataFrame:
    """全市場靜態資料（一次抓、長快取）。回 stock_id, stock_name,
       industry_category, market_type。"""
    try:
        df = fetch_finmind_cached(
            "TaiwanStockInfo", "", "1990-01-01", fresh_days=7, force_refresh=refresh
        )
    except FinMindRateLimitError:
        return pd.DataFrame()
    if df.empty or not _require_cols(df, ["stock_id"]):
        return pd.DataFrame()
    df = df.rename(columns={"type": "market_type"})
    keep = [c for c in ["stock_id", "stock_name", "industry_category", "market_type"] if c in df.columns]
    return df[keep].drop_duplicates(subset=["stock_id"]).reset_index(drop=True)


_COMMON_STOCK_TYPES = {
    "CommonStocksAndOrdinaryShares", "OrdinaryShare", "CommonStock", "CommonStocks",
}


def get_capital_and_industry(stock_id: str, as_of: str | None = None) -> dict:
    """回 {industry, shares_outstanding(元), market_cap(元 at as_of)}；缺則 None。
    市值 = 股本/10 × 收盤（面額10元 → 股數=股本/10）。"""
    out = {"industry": None, "shares_outstanding": None, "market_cap": None}
    info = get_stock_info()
    if not info.empty and stock_id in set(info["stock_id"]):
        row = info.set_index("stock_id").loc[stock_id]
        out["industry"] = row.get("industry_category")
    # 股本（普通股）
    try:
        fin = fetch_finmind_cached(
            "TaiwanStockFinancialStatements", stock_id, "2015-01-01", end_date=as_of
        )
    except FinMindRateLimitError:
        fin = pd.DataFrame()
    shares = None
    if not fin.empty and _require_cols(fin, ["type", "value"]):
        cap = fin[fin["type"].isin(_COMMON_STOCK_TYPES)]
        if not cap.empty:
            shares = float(pd.to_numeric(cap.sort_values("date")["value"], errors="coerce").dropna().iloc[-1])
    out["shares_outstanding"] = shares
    # 市值
    if shares:
        try:
            px = fetch_finmind_cached("TaiwanStockPrice", stock_id, "2015-01-01", end_date=as_of)
        except FinMindRateLimitError:
            px = pd.DataFrame()
        if not px.empty and "close" in px.columns:
            close = pd.to_numeric(px.sort_values("date")["close"], errors="coerce").dropna()
            if len(close):
                out["market_cap"] = shares / 10 * float(close.iloc[-1])
    return out


_INDEX_FALLBACK = {"TAIEX": ["TAIEX", "TWII"], "TWII": ["TWII", "TAIEX"]}


def get_index_history(index_id: str = "TAIEX", start: str | None = None,
                      as_of: str | None = None) -> pd.DataFrame:
    """大盤指數（日）。回 date, open, high, low, close。
    依序試 TAIEX/TWII（沿用 market.py 慣例）。"""
    start = start or "2015-01-01"
    for did in _INDEX_FALLBACK.get(index_id, [index_id]):
        try:
            df = fetch_finmind_cached("TaiwanStockPrice", did, start, end_date=as_of)
        except FinMindRateLimitError:
            continue
        if df.empty:
            continue
        df = df.rename(columns={"max": "high", "min": "low"})
        for c in ["open", "high", "low", "close"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        keep = [c for c in ["date", "open", "high", "low", "close"] if c in df.columns]
        return df[keep].sort_values("date").reset_index(drop=True)
    return pd.DataFrame()
