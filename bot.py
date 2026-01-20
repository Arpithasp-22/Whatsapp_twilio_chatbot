import sqlite3
import pandas as pd
import re
import time
from copy import deepcopy
from langchain_google_genai import ChatGoogleGenerativeAI

# ============================================================
# CONFIG
# ============================================================

DB_FILE = "data/sales.db"
TABLE_NAME = "sales"

DATE_COL = "Date"                 # format: dd-Mon-yy
AMOUNT_COL = " Basic Amount "

MODEL = "gemini-1.5-flash"

MAX_ROWS = 5
MAX_TEXT_LEN = 28

llm = None

# ============================================================
# DB HELPERS
# ============================================================

def get_db():
    return sqlite3.connect(DB_FILE, check_same_thread=False)

def read_sql(query, params=None, retries=3):
    for i in range(retries):
        try:
            with get_db() as conn:
                return pd.read_sql(query, conn, params=params)
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                time.sleep(0.2 * (i + 1))
                continue
            raise

# ============================================================
# CONTEXT
# ============================================================

DEFAULT_CONTEXT = {
    "last_dataframe": None,
    "pagination_index": 0,
    "page_size": MAX_ROWS,
}

contexts = {}

def get_context(user_id):
    if user_id not in contexts:
        contexts[user_id] = deepcopy(DEFAULT_CONTEXT)
    return contexts[user_id]

def reset_context(user_id):
    contexts[user_id] = deepcopy(DEFAULT_CONTEXT)

# ============================================================
# GREETING / RESET / MENU
# ============================================================

def is_greeting(msg):
    return msg.lower().strip() in {
        "hi", "hello", "hey", "help", "menu", "start"
    }

def is_bye(msg):
    return msg.lower().strip() in {
        "bye", "reset", "restart", "start over", "clear"
    }

def menu_message():
    return (
        "👋 *Sales Analytics Bot*\n\n"
        "Try questions like:\n"
        "• Total sales in 2023\n"
        "• Sales by month for 2023\n"
        "• Monthly sales\n"
        "• Yearly sales\n\n"
        "Type *reset* to start over."
    )

# ============================================================
# 🧠 SLOT EXTRACTION (CORE INTELLIGENCE)
# ============================================================

def extract_sales_slots(msg: str) -> dict:
    """
    Extract semantic meaning from user query.
    """
    msg_l = msg.lower()

    slots = {
        "metric": "SUM",           # default metric
        "group_by": None,          # YEAR | MONTH | None
        "year": None
    }

    # Metric
    if any(w in msg_l for w in ["total", "sum"]):
        slots["metric"] = "SUM"

    # Grouping
    if "month" in msg_l:
        slots["group_by"] = "MONTH"
    elif "year" in msg_l or "yearly" in msg_l:
        slots["group_by"] = "YEAR"

    # Year filter
    year_match = re.search(r"\b(20\d{2})\b", msg_l)
    if year_match:
        slots["year"] = year_match.group(1)

    return slots

# ============================================================
# 🧮 SQL BUILDER (SAFE & DETERMINISTIC)
# ============================================================

def build_sales_sql(slots: dict) -> tuple[str, list]:
    params = []
    where_clauses = []

    if slots["year"]:
        where_clauses.append("'20' || substr(Date,8,2) = ?")
        params.append(slots["year"])

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    metric_sql = f'{slots["metric"]}("{AMOUNT_COL}") AS TotalSales'

    # GROUP BY YEAR
    if slots["group_by"] == "YEAR":
        sql = f"""
        SELECT
            '20' || substr(Date,8,2) AS Year,
            {metric_sql}
        FROM {TABLE_NAME}
        {where_sql}
        GROUP BY Year
        ORDER BY Year
        """

    # GROUP BY MONTH
    elif slots["group_by"] == "MONTH":
        sql = f"""
        SELECT
            substr(Date,4,3) AS Month,
            {metric_sql}
        FROM {TABLE_NAME}
        {where_sql}
        GROUP BY Month
        ORDER BY
            CASE Month
                WHEN 'Jan' THEN 1 WHEN 'Feb' THEN 2 WHEN 'Mar' THEN 3
                WHEN 'Apr' THEN 4 WHEN 'May' THEN 5 WHEN 'Jun' THEN 6
                WHEN 'Jul' THEN 7 WHEN 'Aug' THEN 8 WHEN 'Sep' THEN 9
                WHEN 'Oct' THEN 10 WHEN 'Nov' THEN 11 WHEN 'Dec' THEN 12
            END
        """

    # TOTAL ONLY
    else:
        sql = f"""
        SELECT
            {metric_sql}
        FROM {TABLE_NAME}
        {where_sql}
        """

    return sql, params

# ============================================================
# TABLE FORMAT
# ============================================================

def format_table(df, start, size):
    chunk = df.iloc[start:start+size]
    header = " | ".join(chunk.columns)
    lines = [header, "-" * len(header)]

    for _, r in chunk.iterrows():
        lines.append(" | ".join(str(v)[:MAX_TEXT_LEN] for v in r))

    out = "```\n" + "\n".join(lines) + "\n```"
    if start + size < len(df):
        out += "\n➡️ Type *show next*"
    return out

# ============================================================
# MAIN ROUTER
# ============================================================

def search_answer(msg: str, user_id: str) -> str:
    ctx = get_context(user_id)
    msg_l = msg.lower().strip()

    # Reset
    if is_bye(msg):
        reset_context(user_id)
        return menu_message()

    # Greeting
    if is_greeting(msg):
        return menu_message()

    # Pagination
    if ctx["last_dataframe"] is not None and "next" in msg_l:
        ctx["pagination_index"] += ctx["page_size"]
        return format_table(
            ctx["last_dataframe"],
            ctx["pagination_index"],
            ctx["page_size"]
        )

    # 🧠 Semantic understanding
    slots = extract_sales_slots(msg)

    # If user gave nothing meaningful, ask clarification
    if not any([slots["group_by"], slots["year"], "sales" in msg_l]):
        return "❓ What sales insight are you looking for? (e.g. total sales in 2023)"

    sql, params = build_sales_sql(slots)
    df = read_sql(sql, params)

    if df.empty:
        return "No data found."

    ctx["last_dataframe"] = df
    ctx["pagination_index"] = 0
    return format_table(df, 0, ctx["page_size"])

# ============================================================
# APP INTEGRATION
# ============================================================

def setup_bot():
    global llm
    llm = ChatGoogleGenerativeAI(
        model=MODEL,
        temperature=0,
        convert_system_message_to_human=True
    )

def handle_message(msg: str, user_id: str):
    return search_answer(msg, user_id), None
