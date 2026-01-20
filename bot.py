import sqlite3
import pandas as pd
import re
from copy import deepcopy
from datetime import datetime
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate

# ============================================================
# CONFIG
# ============================================================

DB_FILE = "data/sales.db"
TABLE_NAME = "sales"

DATE_COL = "Date"
AMOUNT_COL = "Basic Amount"

MODEL = "gemini-1.5-flash"

MAX_ROWS = 5
MAX_LIMIT = 50
DEFAULT_PERIOD = "30d"

llm = None
COLUMNS = None

# ============================================================
# DB
# ============================================================

def get_db():
    return sqlite3.connect(DB_FILE, check_same_thread=False)

def read_sql(query, params=None):
    with get_db() as conn:
        df = pd.read_sql(query, conn, params=params)

    if AMOUNT_COL in df.columns:
        df[AMOUNT_COL] = (
            df[AMOUNT_COL]
            .astype(str)
            .str.replace(r'[^0-9.]', '', regex=True)
            .replace('', '0')
            .astype(float)
        )

    return df

# ============================================================
# NUMERIC SANITIZATION (🔥 FINAL FIX)
# ============================================================

def amount_expr():
    return (
        "CAST("
        "NULLIF("
        "REPLACE("
        "REPLACE("
        "REPLACE("
        "REPLACE("
        "REPLACE("
        f'"{AMOUNT_COL}"'
        ", '₹', ''"
        "), 'Rs.', ''"
        "), ',', ''"
        "), ' ', ''"
        "), CHAR(160), ''"
        ")"
        ", ''"
        ") AS REAL)"
    )

# ============================================================
# CONTEXT
# ============================================================

DEFAULT_CONTEXT = {
    "last_dataframe": None,
    "pagination_index": 0,
    "page_size": MAX_ROWS,
    "last_period": DEFAULT_PERIOD,
    "last_dealer": None,
}

contexts = {}

def get_context(user_id):
    if user_id not in contexts:
        contexts[user_id] = deepcopy(DEFAULT_CONTEXT)
    return contexts[user_id]

def reset_context(user_id):
    contexts[user_id] = deepcopy(DEFAULT_CONTEXT)

# ============================================================
# GREETING / RESET
# ============================================================

def is_greeting(msg):
    return msg.lower().strip() in {"hi", "hello", "help", "menu", "start"}

def is_reset(msg):
    return msg.lower().strip() in {"reset", "restart", "start over", "clear", "bye"}

def menu_message():
    return (
        "👋 *Sales Analytics Bot*\n\n"
        "You can ask:\n"
        "• Total sales in 2023\n"
        "• Average sales by city in 2023\n"
        "• Top 5 cities by sales\n"
        "• Sales by product\n"
        "• Dealer health for ABC Traders\n"
        "• Compare ABC Traders vs XYZ Steel\n\n"
        "Type *reset* to start over."
    )

# ============================================================
# PERIOD
# ============================================================

def parse_period(msg):
    m = msg.lower()
    if "2023" in m:
        return "2023"
    if "2022" in m:
        return "2022"
    if "last quarter" in m:
        return "90d"
    if "last month" in m:
        return "30d"
    return DEFAULT_PERIOD

def period_clause(period):
    if period.isdigit():
        return f"strftime('%Y', \"{DATE_COL}\") = '{period}'"
    days = int(period.replace("d", ""))
    return f"\"{DATE_COL}\" >= date('now','-{days} day')"

# ============================================================
# DIMENSION & SHAPE DETECTION
# ============================================================

DIMENSIONS = {
    "city": "City",
    "product": "Product",
    "dealer": "Dealer",
    "state": "State",
    "month": "strftime('%Y-%m', \"Date\")",
}

GROUP_TRIGGERS = [
    " by ",
    "wise",
    "top",
    "monthly",
    "per ",
]

def expects_grouped_output(msg):
    msg = msg.lower()
    return any(t in msg for t in GROUP_TRIGGERS)

# 🔧 FIX 1: plural-safe dimension detection (cities, dealers, products)
def detect_dimension(msg):
    msg = msg.lower()
    for key, col in DIMENSIONS.items():
        if key in msg or f"{key}s" in msg:
            return col
    return None

def detect_limit(msg):
    m = re.search(r"\btop\s+(\d+)", msg.lower())
    return min(int(m.group(1)), MAX_LIMIT) if m else None

def detect_aggregate(msg):
    msg = msg.lower()
    if "average" in msg or "avg" in msg:
        return "AVG"
    return "SUM"

# ============================================================
# GROUPED SQL
# ============================================================

def enforce_grouped_sql(msg, period):
    dim = detect_dimension(msg)
    if not dim:
        raise ValueError("Grouping requested but no dimension found")

    agg = detect_aggregate(msg)
    limit = detect_limit(msg)
    amt = amount_expr()

    if agg == "AVG":
        value_expr = f"AVG(NULLIF({amt}, 0))"
    else:
        value_expr = f"SUM({amt})"

    sql = f"""
    SELECT
        {dim},
        {value_expr} AS Value
    FROM {TABLE_NAME}
    WHERE {period_clause(period)}
    GROUP BY {dim}
    ORDER BY Value DESC
    """

    if limit:
        sql += f"\nLIMIT {limit}"

    return sql.strip()

# ============================================================
# FUZZY DEALERS
# ============================================================

def fuzzy_dealers(name):
    df = read_sql(
        f"SELECT DISTINCT Dealer FROM {TABLE_NAME} WHERE Dealer LIKE ? LIMIT 5",
        params=[f"%{name}%"]
    )
    return df["Dealer"].tolist()

# ============================================================
# DEALER SNAPSHOT + HEALTH
# ============================================================

def dealer_snapshot(dealer, period):
    amt = amount_expr()
    query = f"""
    SELECT
        Dealer,
        SUM({amt}) AS TotalSales,
        COUNT(*) AS Orders,
        AVG(NULLIF({amt}, 0)) AS AvgOrderValue,
        MAX("{DATE_COL}") AS LastTxn
    FROM {TABLE_NAME}
    WHERE Dealer = ? AND {period_clause(period)}
    """
    df = read_sql(query, params=[dealer])
    if df.empty:
        return None
    return df.iloc[0].to_dict()

def dealer_health(snapshot):
    today = pd.Timestamp.today()
    days_since = (today - pd.to_datetime(snapshot["LastTxn"])).days

    recency = 100 if days_since <= 30 else 60 if days_since <= 60 else 20
    frequency = 100 if snapshot["Orders"] >= 10 else 60 if snapshot["Orders"] >= 5 else 20
    volume = 100 if snapshot["TotalSales"] >= 500000 else 60 if snapshot["TotalSales"] >= 200000 else 20

    score = round(recency * 0.3 + frequency * 0.3 + volume * 0.4)

    status = (
        "🟢 Healthy" if score >= 80 else
        "🟡 Watch" if score >= 60 else
        "🔴 At Risk"
    )

    return score, status

def dealer_health_card(snapshot):
    score, status = dealer_health(snapshot)
    return f"""
📍 *Dealer Health Snapshot*

🏪 Dealer: {snapshot['Dealer']}
💰 Total Sales: ₹{int(snapshot['TotalSales']):,}
📦 Orders: {int(snapshot['Orders'])}
🕒 Last Order: {snapshot['LastTxn']}

{status}
⭐ Health Score: {score}/100
""".strip()

# ============================================================
# DEALER COMPARISON
# ============================================================

def compare_dealers(dealers, period):
    rows = []
    for d in dealers:
        snap = dealer_snapshot(d, period)
        if snap:
            score, _ = dealer_health(snap)
            rows.append({
                "Dealer": d,
                "Sales": int(snap["TotalSales"]),
                "Orders": int(snap["Orders"]),
                "HealthScore": score
            })
    return pd.DataFrame(rows) if len(rows) >= 2 else None

# ============================================================
# LLM SQL (NON-GROUPED ONLY)
# ============================================================

def run_llm_sql(msg, period):
    prompt = ChatPromptTemplate.from_messages([
        ("system",
         f"Generate ONLY a SQL SELECT query on table {TABLE_NAME}. "
         f"Columns: {COLUMNS}. "
         f"Always filter using: {period_clause(period)}."),
        ("human", msg)
    ])

    sql = llm.invoke(prompt.format_messages()).content.strip()
    sql = sql.replace("`", "\"")

    if not sql.lower().startswith("select"):
        raise ValueError("Invalid SQL")

    return read_sql(sql)

# ============================================================
# FORMAT
# ============================================================

def format_table(df, start, size):
    chunk = df.iloc[start:start + size]
    header = " | ".join(chunk.columns)
    lines = [header, "-" * len(header)]
    for _, r in chunk.iterrows():
        lines.append(" | ".join(str(v)[:28] for v in r))
    out = "```\n" + "\n".join(lines) + "\n```"
    if start + size < len(df):
        out += "\n➡️ show next"
    return out

# ============================================================
# MAIN ENTRY
# ============================================================

def search_answer(msg, user_id):
    ctx = get_context(user_id)

    if is_reset(msg):
        reset_context(user_id)
        return menu_message()

    if is_greeting(msg):
        return menu_message()

    if ctx["last_dataframe"] is not None and "next" in msg.lower():
        ctx["pagination_index"] += ctx["page_size"]
        return format_table(ctx["last_dataframe"], ctx["pagination_index"], ctx["page_size"])

    ctx["last_period"] = parse_period(msg)

    try:
        # 🔧 FIX 2: deterministic handling for simple totals
        if "total" in msg.lower() and not expects_grouped_output(msg):
            amt = amount_expr()
            sql = f"""
            SELECT SUM({amt}) AS TotalSales
            FROM {TABLE_NAME}
            WHERE {period_clause(ctx["last_period"])}
            """
            df = read_sql(sql)
            ctx["last_dataframe"] = df
            ctx["pagination_index"] = 0
            return format_table(df, 0, ctx["page_size"])

        if "health" in msg.lower():
            matches = fuzzy_dealers(msg)
            if len(matches) == 1:
                snap = dealer_snapshot(matches[0], ctx["last_period"])
                return dealer_health_card(snap)
            elif len(matches) > 1:
                return "Did you mean:\n" + "\n".join(matches)

        if "vs" in msg.lower() or "compare" in msg.lower():
            parts = re.split(r"vs|compare", msg, flags=re.IGNORECASE)
            dealers = []
            for p in parts:
                m = fuzzy_dealers(p.strip())
                if m:
                    dealers.append(m[0])
            df = compare_dealers(dealers, ctx["last_period"])
            ctx["last_dataframe"] = df
            ctx["pagination_index"] = 0
            return format_table(df, 0, ctx["page_size"])

        if expects_grouped_output(msg):
            sql = enforce_grouped_sql(msg, ctx["last_period"])
            df = read_sql(sql)
        else:
            df = run_llm_sql(msg, ctx["last_period"])

        ctx["last_dataframe"] = df
        ctx["pagination_index"] = 0
        return format_table(df, 0, ctx["page_size"])

    except Exception:
        return (
            "❓ I couldn’t answer that safely.\n\n"
            "Try:\n"
            "• Average sales by city in 2023\n"
            "• Top 5 cities by sales\n"
            "• Dealer health for ABC Traders"
        )

# ============================================================
# SETUP
# ============================================================

def setup_bot():
    global llm, COLUMNS
    llm = ChatGoogleGenerativeAI(model=MODEL, temperature=0)
    COLUMNS = read_sql(f"SELECT * FROM {TABLE_NAME} LIMIT 1").columns.tolist()

# ============================================================
# FLASK / TWILIO WRAPPER
# ============================================================

def handle_message(msg: str, user_id: str):
    response_text = search_answer(msg, user_id)
    return response_text, None
