import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from math import isfinite
from pathlib import Path

import altair as alt
import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
TOKEN_FILE = BASE_DIR / "access_token.json"
DATABASE_FILE = BASE_DIR / "mf_history.sqlite3"
HOLDINGS_URL = "https://api.kite.trade/mf/holdings"
STOCK_HOLDINGS_URL = "https://api.kite.trade/portfolio/holdings"
CUSTOM_INVESTMENT_TYPES = ["FD", "RD", "EPF", "PPF", "Other"]
INDIAN_CURRENCY_AXIS = (
    "(datum.value < 0 ? '-₹' : '₹') + "
    "replace(format(abs(datum.value), '.2f'), "
    "/(\\d)(?=(?:\\d{2})*\\d{3}(?:\\.|$))/g, '$1,')"
)


def format_indian_number(value, decimals=2):
    number = float(value)
    sign = "-" if number < 0 else ""
    integer, _, fraction = f"{abs(number):.{decimals}f}".partition(".")

    if len(integer) > 3:
        groups = [integer[-3:]]
        integer = integer[:-3]
        while integer:
            groups.append(integer[-2:])
            integer = integer[:-2]
        integer = ",".join(reversed(groups))

    if decimals == 0:
        return f"{sign}{integer}"
    return f"{sign}{integer}.{fraction}"


def format_inr(value, decimals=2):
    return f"₹{format_indian_number(value, decimals)}"


def format_inr_compact(value):
    amount = float(value)
    absolute_amount = abs(amount)
    if absolute_amount >= 10_000_000:
        scaled_amount = absolute_amount / 10_000_000
        unit = "crore"
    elif absolute_amount >= 100_000:
        scaled_amount = absolute_amount / 100_000
        unit = "lakh"
    else:
        return format_inr(amount)

    sign = "-" if amount < 0 else ""
    return f"{sign}₹{format_indian_number(scaled_amount)} {unit}"


def summarize_portfolios(mf_rows, stock_rows, custom_rows):
    if mf_rows is None or stock_rows is None:
        return None

    summary = []
    for name, rows in (
        ("Mutual funds", mf_rows),
        ("Stocks", stock_rows),
        ("Custom investments", custom_rows),
    ):
        invested = sum(row["Invested amount (approx. ₹)"] for row in rows)
        current = sum(row["Current value (₹)"] for row in rows)
        summary.append(
            {
                "Portfolio": name,
                "Invested amount (approx. ₹)": invested,
                "Current value (₹)": current,
                "Unrealized P&L (approx. ₹)": current - invested,
            }
        )

    total_invested = sum(row["Invested amount (approx. ₹)"] for row in summary)
    total_current = sum(row["Current value (₹)"] for row in summary)
    summary.append(
        {
            "Portfolio": "Combined total",
            "Invested amount (approx. ₹)": total_invested,
            "Current value (₹)": total_current,
            "Unrealized P&L (approx. ₹)": total_current - total_invested,
        }
    )
    return summary


def create_history_chart(history):
    chart_data = history[
        ["snapshot_date", "portfolio_value", "invested_value"]
    ].melt(
        id_vars=["snapshot_date"],
        var_name="series",
        value_name="value",
    )
    chart_data["series"] = chart_data["series"].map(
        {
            "portfolio_value": "Portfolio value",
            "invested_value": "Invested value (approx.)",
        }
    )
    chart_data["value"] = pd.to_numeric(chart_data["value"], errors="coerce")
    chart_data = chart_data.dropna(subset=["value"])
    chart_data["formatted_value"] = chart_data["value"].map(format_inr)

    return (
        alt.Chart(chart_data)
        .mark_line(point=True)
        .encode(
            x=alt.X(
                field="snapshot_date",
                type="temporal",
                title="Date",
            ),
            y=alt.Y(
                field="value",
                type="quantitative",
                axis=alt.Axis(labelExpr=INDIAN_CURRENCY_AXIS),
                title="Value",
            ),
            color=alt.Color(
                field="series",
                type="nominal",
                scale=alt.Scale(
                    domain=["Invested value (approx.)", "Portfolio value"],
                    scheme="blues"
                ),
                title="",
                legend=alt.Legend(
                    orient="top",
                    direction="horizontal",
                ),
            ),
            tooltip=[
                alt.Tooltip(
                    field="snapshot_date",
                    type="temporal",
                    title="Date",
                ),
                alt.Tooltip(field="series", type="nominal", title="Series"),
                alt.Tooltip(
                    field="formatted_value",
                    type="nominal",
                    title="Value",
                ),
            ],
        )
    )


def create_holding_comparison_chart(holdings, name_field):
    chart_data = holdings[
        [name_field, "Invested amount (approx. ₹)", "Current value (₹)"]
    ].melt(
        id_vars=[name_field],
        var_name="series",
        value_name="value",
    )
    chart_data["series"] = chart_data["series"].map(
        {
            "Invested amount (approx. ₹)": "Invested amount",
            "Current value (₹)": "Current value",
        }
    )
    chart_data["formatted_value"] = chart_data["value"].map(format_inr)

    return (
        alt.Chart(chart_data)
        .mark_bar()
        .encode(
            x=alt.X(field=name_field, type="nominal", sort="-y", title=None),
            xOffset=alt.XOffset(
                field="series",
                type="nominal",
                sort=["Invested amount", "Current value"],
            ),
            y=alt.Y(
                field="value",
                type="quantitative",
                axis=alt.Axis(labelExpr=INDIAN_CURRENCY_AXIS),
                title="Value",
            ),
            color=alt.Color(
                field="series",
                type="nominal",
                scale=alt.Scale(
                    domain=["Invested amount", "Current value"],
                    scheme="blues"
                ),
                title="",
                legend=alt.Legend(
                    orient="top",
                    direction="horizontal",
                ),
            ),
            tooltip=[
                alt.Tooltip(field=name_field, type="nominal"),
                alt.Tooltip(field="series", type="nominal", title="Series"),
                alt.Tooltip(
                    field="formatted_value",
                    type="nominal",
                    title="Value",
                ),
            ],
        )
    )


def load_access_token():
    if not TOKEN_FILE.exists():
        return None
    token_data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    access_token = token_data.get("access_token")
    return access_token or None


def fetch_holdings(api_key, access_token):
    response = requests.get(
        HOLDINGS_URL,
        headers={
            "X-Kite-Version": "3",
            "Authorization": f"token {api_key}:{access_token}",
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success":
        raise RuntimeError(payload.get("message", "Kite holdings request failed."))

    holdings = payload.get("data")
    if not isinstance(holdings, list):
        raise ValueError("Kite returned an invalid mutual-fund holdings response.")
    return holdings


def fetch_stock_holdings(api_key, access_token):
    response = requests.get(
        STOCK_HOLDINGS_URL,
        headers={
            "X-Kite-Version": "3",
            "Authorization": f"token {api_key}:{access_token}",
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success":
        raise RuntimeError(
            payload.get("message", "Kite stock holdings request failed.")
        )

    holdings = payload.get("data")
    if not isinstance(holdings, list):
        raise ValueError("Kite returned an invalid stock holdings response.")
    return holdings


def required_number(holding, field):
    value = holding.get(field)
    if not isinstance(value, (int, float)):
        raise ValueError(
            f"Kite returned a missing or invalid {field} for "
            f"{holding.get('fund') or holding.get('tradingsymbol') or 'a holding'}."
        )
    return float(value)


def create_dashboard_rows(holdings):
    rows = []
    for holding in holdings:
        quantity = required_number(holding, "quantity")
        nav = required_number(holding, "last_price")
        average_price = required_number(holding, "average_price")
        rows.append(
            {
                "Fund": holding.get("fund", "Unknown fund"),
                "ISIN": holding.get("tradingsymbol", ""),
                "Invested amount (approx. ₹)": quantity * average_price,
                "Current value (₹)": quantity * nav,
                "Units": quantity,
                "NAV (₹)": nav,
                "NAV date": holding.get("last_price_date") or "Not provided",
                "Unrealized P&L (approx. ₹)": quantity * (nav - average_price),
            }
        )
    return rows


def create_stock_dashboard_rows(holdings):
    rows = []
    for holding in holdings:
        settled_quantity = required_number(holding, "quantity")
        t1_quantity = required_number(holding, "t1_quantity")
        quantity = settled_quantity + t1_quantity
        last_price = required_number(holding, "last_price")
        average_price = required_number(holding, "average_price")
        rows.append(
            {
                "Symbol": holding.get("tradingsymbol", "Unknown"),
                "Exchange": holding.get("exchange", ""),
                "ISIN": holding.get("isin", ""),
                "Invested amount (approx. ₹)": quantity * average_price,
                "Current value (₹)": quantity * last_price,
                "Quantity": quantity,
                "T+1 quantity": t1_quantity,
                "Average price (₹)": average_price,
                "Last price (₹)": last_price,
                "Unrealized P&L (approx. ₹)": quantity
                * (last_price - average_price),
                "Day change (%)": holding.get("day_change_percentage"),
            }
        )
    return rows


def save_daily_snapshot(rows):
    snapshot_date = datetime.now().astimezone().date().isoformat()
    with closing(sqlite3.connect(DATABASE_FILE)) as connection, connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_snapshots (
                snapshot_date TEXT NOT NULL,
                isin TEXT NOT NULL,
                fund TEXT NOT NULL,
                quantity REAL NOT NULL,
                nav REAL NOT NULL,
                nav_date TEXT NOT NULL,
                market_value REAL NOT NULL,
                invested_value REAL,
                PRIMARY KEY (snapshot_date, isin)
            )
            """
        )
        columns = {
            column[1]
            for column in connection.execute(
                "PRAGMA table_info(daily_snapshots)"
            ).fetchall()
        }
        if "invested_value" not in columns:
            connection.execute(
                "ALTER TABLE daily_snapshots ADD COLUMN invested_value REAL"
            )
        connection.execute(
            "DELETE FROM daily_snapshots WHERE snapshot_date = ?",
            (snapshot_date,),
        )
        connection.executemany(
            """
            INSERT INTO daily_snapshots
                (snapshot_date, isin, fund, quantity, nav, nav_date,
                 market_value, invested_value)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    snapshot_date,
                    row["ISIN"],
                    row["Fund"],
                    row["Units"],
                    row["NAV (₹)"],
                    row["NAV date"],
                    row["Current value (₹)"],
                    row["Invested amount (approx. ₹)"],
                )
                for row in rows
            ],
        )


def save_daily_stock_snapshot(rows):
    snapshot_date = datetime.now().astimezone().date().isoformat()
    with closing(sqlite3.connect(DATABASE_FILE)) as connection, connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_stock_snapshots (
                snapshot_date TEXT NOT NULL,
                exchange TEXT NOT NULL,
                symbol TEXT NOT NULL,
                isin TEXT NOT NULL,
                quantity REAL NOT NULL,
                last_price REAL NOT NULL,
                invested_value REAL NOT NULL,
                market_value REAL NOT NULL,
                PRIMARY KEY (snapshot_date, exchange, symbol)
            )
            """
        )
        connection.execute(
            "DELETE FROM daily_stock_snapshots WHERE snapshot_date = ?",
            (snapshot_date,),
        )
        connection.executemany(
            """
            INSERT INTO daily_stock_snapshots
                (snapshot_date, exchange, symbol, isin, quantity, last_price,
                 invested_value, market_value)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    snapshot_date,
                    row["Exchange"],
                    row["Symbol"],
                    row["ISIN"],
                    row["Quantity"],
                    row["Last price (₹)"],
                    row["Invested amount (approx. ₹)"],
                    row["Current value (₹)"],
                )
                for row in rows
            ],
        )


def load_stock_history():
    if not DATABASE_FILE.exists():
        return pd.DataFrame(
            columns=["snapshot_date", "portfolio_value", "invested_value"]
        )
    with closing(sqlite3.connect(DATABASE_FILE)) as connection, connection:
        table_exists = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'daily_stock_snapshots'
            """
        ).fetchone()
        if not table_exists:
            return pd.DataFrame(
                columns=["snapshot_date", "portfolio_value", "invested_value"]
            )
        return pd.read_sql_query(
            """
            SELECT snapshot_date,
                   SUM(market_value) AS portfolio_value,
                   SUM(invested_value) AS invested_value
            FROM daily_stock_snapshots
            GROUP BY snapshot_date
            ORDER BY snapshot_date
            """,
            connection,
        )


def load_combined_history():
    mf_history = load_history()
    stock_history = load_stock_history()
    if mf_history.empty or stock_history.empty:
        return pd.DataFrame(
            columns=["snapshot_date", "portfolio_value", "invested_value"]
        )

    combined = mf_history.merge(
        stock_history,
        on="snapshot_date",
        how="inner",
        suffixes=("_mf", "_stocks"),
    )
    combined["portfolio_value"] = (
        combined["portfolio_value_mf"] + combined["portfolio_value_stocks"]
    )
    combined["invested_value"] = (
        combined["invested_value_mf"] + combined["invested_value_stocks"]
    )

    custom_history = load_custom_history()
    if not custom_history.empty:
        combined["snapshot_date"] = pd.to_datetime(combined["snapshot_date"])
        custom_history["snapshot_date"] = pd.to_datetime(
            custom_history["snapshot_date"]
        )
        combined = pd.merge_asof(
            combined.sort_values("snapshot_date"),
            custom_history.sort_values("snapshot_date"),
            on="snapshot_date",
            direction="backward",
        )
        combined["portfolio_value"] += combined["custom_current_value"].fillna(0)
        combined["invested_value"] += combined["custom_invested_value"].fillna(0)

    return combined[
        ["snapshot_date", "portfolio_value", "invested_value"]
    ].sort_values("snapshot_date")


def load_history():
    if not DATABASE_FILE.exists():
        return pd.DataFrame(
            columns=["snapshot_date", "portfolio_value", "invested_value"]
        )
    with closing(sqlite3.connect(DATABASE_FILE)) as connection, connection:
        table_exists = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'daily_snapshots'
            """
        ).fetchone()
        if not table_exists:
            return pd.DataFrame(
                columns=["snapshot_date", "portfolio_value", "invested_value"]
            )
        return pd.read_sql_query(
            """
            SELECT snapshot_date,
                   SUM(market_value) AS portfolio_value,
                   SUM(invested_value) AS invested_value
            FROM daily_snapshots
            GROUP BY snapshot_date
            ORDER BY snapshot_date
            """,
            connection,
        )


def load_custom_investments():
    with closing(sqlite3.connect(DATABASE_FILE)) as connection, connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS custom_investments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                investment_type TEXT NOT NULL,
                invested_value REAL NOT NULL,
                current_value REAL NOT NULL
            )
            """
        )
        return pd.read_sql_query(
            """
            SELECT name AS "Investment",
                   investment_type AS "Type",
                   invested_value AS "Invested amount (₹)",
                   current_value AS "Current value (₹)"
            FROM custom_investments
            ORDER BY id
            """,
            connection,
        )


def format_custom_investments_for_editor(investments):
    editor_rows = investments.copy()
    for column in ("Invested amount (₹)", "Current value (₹)"):
        editor_rows[column] = editor_rows[column].map(
            lambda value: format_indian_number(value, 0)
        )
    return editor_rows


def upsert_custom_snapshot(connection, snapshot_date, invested_total, current_total):
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS custom_investment_snapshots (
            snapshot_date TEXT PRIMARY KEY,
            invested_value REAL NOT NULL,
            current_value REAL NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO custom_investment_snapshots
            (snapshot_date, invested_value, current_value)
        VALUES (?, ?, ?)
        ON CONFLICT(snapshot_date) DO UPDATE SET
            invested_value = excluded.invested_value,
            current_value = excluded.current_value
        """,
        (snapshot_date, invested_total, current_total),
    )


def save_custom_investments(editor_rows):
    prepared_rows = []
    for row_number, row in enumerate(editor_rows, start=1):
        name = row.get("Investment")
        investment_type = row.get("Type")
        invested_value = row.get("Invested amount (₹)")
        current_value = row.get("Current value (₹)")

        if (
            pd.isna(name)
            and pd.isna(investment_type)
            and pd.isna(invested_value)
            and pd.isna(current_value)
        ):
            continue

        name = str(name).strip() if not pd.isna(name) else ""
        if not name:
            raise ValueError(f"Row {row_number}: enter an investment name.")
        if investment_type not in CUSTOM_INVESTMENT_TYPES:
            raise ValueError(
                f"Row {row_number}: choose FD, RD, EPF, PPF, or Other."
            )

        values = []
        for label, value in (
            ("invested amount", invested_value),
            ("current value", current_value),
        ):
            try:
                amount = float(str(value).replace(",", "").strip())
            except (TypeError, ValueError):
                raise ValueError(
                    f"Row {row_number}: enter a valid {label}."
                ) from None
            if not isfinite(amount) or amount < 0:
                raise ValueError(
                    f"Row {row_number}: {label.title()} must be a "
                    "non-negative number."
                )
            values.append(amount)

        prepared_rows.append(
            (name, investment_type, values[0], values[1])
        )

    snapshot_date = datetime.now().astimezone().date().isoformat()
    invested_total = sum(row[2] for row in prepared_rows)
    current_total = sum(row[3] for row in prepared_rows)

    with closing(sqlite3.connect(DATABASE_FILE)) as connection, connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS custom_investments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                investment_type TEXT NOT NULL,
                invested_value REAL NOT NULL,
                current_value REAL NOT NULL
            )
            """
        )
        connection.execute("DELETE FROM custom_investments")
        connection.executemany(
            """
            INSERT INTO custom_investments
                (name, investment_type, invested_value, current_value)
            VALUES (?, ?, ?, ?)
            """,
            prepared_rows,
        )
        upsert_custom_snapshot(
            connection,
            snapshot_date,
            invested_total,
            current_total,
        )


def load_custom_history():
    if not DATABASE_FILE.exists():
        return pd.DataFrame(
            columns=["snapshot_date", "custom_invested_value", "custom_current_value"]
        )
    with closing(sqlite3.connect(DATABASE_FILE)) as connection:
        table_exists = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'custom_investment_snapshots'
            """
        ).fetchone()
        if not table_exists:
            return pd.DataFrame(
                columns=[
                    "snapshot_date",
                    "custom_invested_value",
                    "custom_current_value",
                ]
            )
        return pd.read_sql_query(
            """
            SELECT snapshot_date,
                   invested_value AS custom_invested_value,
                   current_value AS custom_current_value
            FROM custom_investment_snapshots
            ORDER BY snapshot_date
            """,
            connection,
        )


def main():
    load_dotenv(BASE_DIR / ".env")
    api_key = os.getenv("KITE_API_KEY")

    st.set_page_config(page_title="Zerodha Portfolio Dashboard", layout="wide")
    st.title("Zerodha portfolio dashboard")
    st.caption(
        "Track your Zerodha mutual-fund and equity holdings. Fund NAVs and stock "
        "prices use the latest values returned by Kite."
    )

    if not api_key:
        st.error("KITE_API_KEY is missing. Configure it in the project's .env file.")
        st.stop()

    try:
        access_token = load_access_token()
    except (OSError, json.JSONDecodeError) as error:
        st.error(f"Could not read the local Kite token file: {error}")
        st.stop()

    if not access_token:
        st.warning("No Kite access token found. Run `python auth.py` and log in.")
        st.stop()

    try:
        custom_investments_df = load_custom_investments()
        if not custom_investments_df.empty:
            upsert_date = datetime.now().astimezone().date().isoformat()
            with closing(sqlite3.connect(DATABASE_FILE)) as connection, connection:
                upsert_custom_snapshot(
                    connection,
                    upsert_date,
                    custom_investments_df["Invested amount (₹)"].sum(),
                    custom_investments_df["Current value (₹)"].sum(),
                )
    except sqlite3.Error as error:
        st.error(f"Could not load saved custom investments: {error}")
        st.stop()

    summary_tab, mf_tab, stock_tab, custom_tab = st.tabs(
        ["Summary", "Mutual funds", "Stocks", "Custom investments"]
    )

    with mf_tab:
        summary_area = st.container()
        refresh = st.button("Refresh mutual funds and NAV")
        should_fetch = (
            refresh
            or "holdings_rows" not in st.session_state
            or st.session_state.get("holdings_token") != access_token
        )
        if should_fetch:
            try:
                api_holdings = fetch_holdings(api_key, access_token)
                rows = create_dashboard_rows(api_holdings)
                save_daily_snapshot(rows)
                st.session_state["holdings_rows"] = rows
                st.session_state["holdings_token"] = access_token
            except (
                requests.RequestException,
                ValueError,
                RuntimeError,
                sqlite3.Error,
            ) as error:
                st.error(f"Unable to refresh mutual-fund holdings: {error}")
                st.info(
                    "If your Kite session has expired, run `python auth.py` to log "
                    "in again, then refresh this page."
                )

        if "holdings_rows" in st.session_state:
            rows = st.session_state["holdings_rows"]
            total_value = sum(row["Current value (₹)"] for row in rows)
            invested_value = sum(
                row["Invested amount (approx. ₹)"] for row in rows
            )
            total_pnl = total_value - invested_value

            with summary_area:
                invested_col, value_col, pnl_col = st.columns(3)
                invested_col.metric(
                    "Invested amount (approx.)", format_inr_compact(invested_value)
                )
                value_col.metric(
                    "Current portfolio value", format_inr_compact(total_value)
                )
                pnl_col.metric(
                    "Unrealized P&L (approx.)",
                    format_inr_compact(total_pnl),
                    delta=format_inr_compact(total_pnl),
                )

            if rows:
                holdings_df = pd.DataFrame(rows)
                st.subheader("Holdings")
                st.dataframe(
                    holdings_df.style.format(
                        {
                            "Units": lambda value: format_indian_number(value, 3),
                            "NAV (₹)": lambda value: format_inr(value, 4),
                            "Current value (₹)": format_inr,
                            "Invested amount (approx. ₹)": format_inr,
                            "Unrealized P&L (approx. ₹)": format_inr,
                        }
                    ),
                    width="stretch",
                    hide_index=True,
                )
                fund_chart = pd.DataFrame(
                    {
                        "fund": holdings_df["Fund"],
                        "Invested amount (approx. ₹)": holdings_df[
                            "Invested amount (approx. ₹)"
                        ],
                        "Current value (₹)": holdings_df["Current value (₹)"],
                    }
                )
                st.altair_chart(
                    create_holding_comparison_chart(fund_chart, "fund"),
                    width="stretch",
                )
            else:
                st.info("No mutual-fund holdings were returned for this account.")

            history = load_history()
            st.subheader("Daily mutual-fund value history")
            if history.empty:
                st.info("History will appear after the first successful refresh.")
            else:
                history["snapshot_date"] = pd.to_datetime(history["snapshot_date"])
                st.altair_chart(
                    create_history_chart(history),
                    width="stretch",
                )
        else:
            with summary_area:
                st.info("Refresh mutual-fund holdings to load the portfolio.")

        st.caption(
            "NAV values use the latest price returned by Kite and may lag the "
            "current date. Invested amount and P&L are estimates from average "
            "price and units."
        )

    with stock_tab:
        summary_area = st.container()
        st.caption(
            "Equity holdings from Kite's long-term holdings endpoint. Quantity "
            "and value include unsettled T+1 shares. Open intraday and F&O "
            "positions are not included."
        )
        refresh_stocks = st.button("Refresh stock holdings and prices")
        should_fetch_stocks = (
            refresh_stocks
            or "stock_rows" not in st.session_state
            or st.session_state.get("stock_holdings_token") != access_token
        )
        if should_fetch_stocks:
            try:
                api_holdings = fetch_stock_holdings(api_key, access_token)
                rows = create_stock_dashboard_rows(api_holdings)
                save_daily_stock_snapshot(rows)
                st.session_state["stock_rows"] = rows
                st.session_state["stock_holdings_token"] = access_token
            except (
                requests.RequestException,
                ValueError,
                RuntimeError,
                sqlite3.Error,
            ) as error:
                st.error(f"Unable to refresh stock holdings: {error}")
                st.info(
                    "If your Kite session has expired, run `python auth.py` to "
                    "log in again, then refresh this page."
                )

        if "stock_rows" in st.session_state:
            rows = st.session_state["stock_rows"]
            total_value = sum(row["Current value (₹)"] for row in rows)
            invested_value = sum(
                row["Invested amount (approx. ₹)"] for row in rows
            )
            total_pnl = total_value - invested_value

            with summary_area:
                invested_col, value_col, pnl_col = st.columns(3)
                invested_col.metric(
                    "Invested amount (approx.)", format_inr_compact(invested_value)
                )
                value_col.metric(
                    "Current stock portfolio value",
                    format_inr_compact(total_value),
                )
                pnl_col.metric(
                    "Unrealized P&L (approx.)",
                    format_inr_compact(total_pnl),
                    delta=format_inr_compact(total_pnl),
                )

            if rows:
                holdings_df = pd.DataFrame(rows)
                st.subheader("Stock holdings")
                st.dataframe(
                    holdings_df.style.format(
                        {
                            "Quantity": lambda value: format_indian_number(
                                value, 0
                            ),
                            "T+1 quantity": lambda value: format_indian_number(
                                value, 0
                            ),
                            "Average price (₹)": format_inr,
                            "Last price (₹)": format_inr,
                            "Current value (₹)": format_inr,
                            "Invested amount (approx. ₹)": format_inr,
                            "Unrealized P&L (approx. ₹)": format_inr,
                            "Day change (%)": "{:+.2f}%",
                        },
                        na_rep="—",
                    ),
                    width="stretch",
                    hide_index=True,
                )
                stock_chart = pd.DataFrame(
                    {
                        "symbol": holdings_df["Symbol"],
                        "Invested amount (approx. ₹)": holdings_df[
                            "Invested amount (approx. ₹)"
                        ],
                        "Current value (₹)": holdings_df["Current value (₹)"],
                    }
                )
                st.altair_chart(
                    create_holding_comparison_chart(stock_chart, "symbol"),
                    width="stretch",
                )
            else:
                st.info("No stock holdings were returned for this account.")

            history = load_stock_history()
            st.subheader("Daily stock portfolio value history")
            if history.empty:
                st.info("History will appear after the first successful refresh.")
            else:
                history["snapshot_date"] = pd.to_datetime(history["snapshot_date"])
                st.altair_chart(
                    create_history_chart(history),
                    width="stretch",
                )
        else:
            with summary_area:
                st.info("Refresh stock holdings to load the portfolio.")

        st.caption(
            "Stock holdings values use Kite's last traded price. The invested "
            "amount and P&L are estimates from average price and quantity."
        )

    with summary_tab:
        mf_rows = st.session_state.get("holdings_rows")
        stock_rows = st.session_state.get("stock_rows")
        custom_rows = [
            {
                "Invested amount (approx. ₹)": row["Invested amount (₹)"],
                "Current value (₹)": row["Current value (₹)"],
            }
            for row in custom_investments_df.to_dict("records")
        ]
        summary = summarize_portfolios(mf_rows, stock_rows, custom_rows)

        if summary is None:
            st.info(
                "Refresh both the Mutual funds and Stocks tabs to load figures "
                "for the combined summary."
            )
        else:
            combined = summary[-1]
            invested_value = combined["Invested amount (approx. ₹)"]
            current_value = combined["Current value (₹)"]
            pnl = combined["Unrealized P&L (approx. ₹)"]

            invested_col, current_col, pnl_col = st.columns(3)
            invested_col.metric(
                "Total invested (approx.)", format_inr_compact(invested_value)
            )
            current_col.metric(
                "Combined current value", format_inr_compact(current_value)
            )
            pnl_col.metric(
                "Combined unrealized P&L (approx.)",
                format_inr_compact(pnl),
                delta=format_inr_compact(pnl),
            )

            summary_df = pd.DataFrame(summary)
            st.dataframe(
                summary_df.style.format(
                    {
                        "Invested amount (approx. ₹)": format_inr_compact,
                        "Current value (₹)": format_inr_compact,
                        "Unrealized P&L (approx. ₹)": format_inr_compact,
                    }
                ),
                width="stretch",
                hide_index=True,
            )
            st.subheader("Invested vs current value by portfolio")
            st.altair_chart(
                create_holding_comparison_chart(
                    summary_df[summary_df["Portfolio"] != "Combined total"],
                    "Portfolio",
                ),
                width="stretch",
            )
            st.caption(
                "Headline and summary values are abbreviated in lakh/crore where "
                "applicable. Holding details and chart values remain in rupees."
            )
            st.subheader("Daily combined portfolio value history")
            combined_history = load_combined_history()
            if combined_history.empty:
                st.info(
                    "Refresh both portfolios on the same day to start the "
                    "combined history chart."
                )
            else:
                combined_history["snapshot_date"] = pd.to_datetime(
                    combined_history["snapshot_date"]
                )
                st.altair_chart(
                    create_history_chart(combined_history),
                    width="stretch",
                )
            st.caption(
                "Combined figures include long-term stocks, mutual funds, and "
                "saved custom investments. Kite portfolio invested amounts and "
                "P&L are estimates based on average prices and holdings quantities."
            )

    with custom_tab:
        custom_records = custom_investments_df.to_dict("records")
        if custom_records:
            custom_total_invested = sum(
                row["Invested amount (₹)"] for row in custom_records
            )
            custom_total_current = sum(
                row["Current value (₹)"] for row in custom_records
            )
            invested_col, current_col, pnl_col = st.columns(3)
            invested_col.metric(
                "Total invested", format_inr_compact(custom_total_invested)
            )
            current_col.metric(
                "Current value", format_inr_compact(custom_total_current)
            )
            pnl_col.metric(
                "Unrealized P&L",
                format_inr_compact(custom_total_current - custom_total_invested),
                delta=format_inr_compact(
                    custom_total_current - custom_total_invested
                ),
            )
        else:
            st.info("Add your first custom investment in the table below.")

        st.caption(
            "Add or edit investments that are not available through Kite, such "
            "as fixed deposits, recurring deposits, EPF, or PPF. Click Save to "
            "persist your changes locally."
        )
        st.subheader("Investment details")
        with st.form("custom_investments_form"):
            edited_custom_df = st.data_editor(
                format_custom_investments_for_editor(custom_investments_df),
                width="stretch",
                hide_index=True,
                num_rows="dynamic",
                column_config={
                    "Investment": st.column_config.TextColumn(
                        "Investment name",
                        required=True,
                    ),
                    "Type": st.column_config.SelectboxColumn(
                        "Type",
                        options=CUSTOM_INVESTMENT_TYPES,
                        required=True,
                    ),
                    "Invested amount (₹)": st.column_config.TextColumn(
                        "Invested amount (₹)",
                        help="Whole rupees with Indian comma grouping.",
                        required=True,
                    ),
                    "Current value (₹)": st.column_config.TextColumn(
                        "Current value (₹)",
                        help="Whole rupees with Indian comma grouping.",
                        required=True,
                    ),
                },
            )
            save_custom_button = st.form_submit_button(
                "Save custom investments"
            )

        if save_custom_button:
            try:
                save_custom_investments(edited_custom_df.to_dict("records"))
                st.success("Custom investments saved.")
                st.rerun()
            except (ValueError, sqlite3.Error) as error:
                st.error(f"Could not save custom investments: {error}")

        if custom_records:
            st.subheader("Invested vs current value by investment")
            custom_chart = pd.DataFrame(
                {
                    "investment": [
                        f"{row['Investment']} ({row['Type']})"
                        for row in custom_records
                    ],
                    "Invested amount (approx. ₹)": [
                        row["Invested amount (₹)"] for row in custom_records
                    ],
                    "Current value (₹)": [
                        row["Current value (₹)"] for row in custom_records
                    ],
                }
            )
            st.altair_chart(
                create_holding_comparison_chart(custom_chart, "investment"),
                width="stretch",
            )

        st.subheader("Daily custom investment value history")
        custom_history = load_custom_history()
        if custom_history.empty:
            st.info(
                "Save custom investments to start recording daily history."
            )
        else:
            custom_history = custom_history.rename(
                columns={
                    "custom_current_value": "portfolio_value",
                    "custom_invested_value": "invested_value",
                }
            )
            custom_history["snapshot_date"] = pd.to_datetime(
                custom_history["snapshot_date"]
            )
            st.altair_chart(
                create_history_chart(custom_history),
                width="stretch",
            )


if __name__ == "__main__":
    main()
