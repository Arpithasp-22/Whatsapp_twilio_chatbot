import pytest

import bot


def ask(msg, user="u1"):
    return bot.handle_message(user, msg)


# ---------- SQL safety ----------
@pytest.mark.parametrize("sql", [
    "SELECT * FROM sales",
    "select Dealer, sum(\"Basic Amount\") from sales group by Dealer;",
    "WITH t AS (SELECT 1) SELECT * FROM t",
])
def test_safe_sql_allowed(sql):
    assert bot.is_safe_sql(sql)


@pytest.mark.parametrize("sql", [
    "DROP TABLE sales",
    "SELECT 1; DROP TABLE sales",
    "SELECT * FROM sales -- comment",
    "UPDATE sales SET Dealer='x'",
    "PRAGMA table_info(sales)",
    "ATTACH DATABASE 'x.db' AS x",
    "",
])
def test_unsafe_sql_rejected(sql):
    assert not bot.is_safe_sql(sql)


def test_connection_is_read_only(db):
    with pytest.raises(Exception):
        conn = bot.get_conn()
        conn.execute("DELETE FROM sales")


# ---------- periods ----------
@pytest.mark.parametrize("msg,expected", [
    ("sales last month", "30d"), ("last quarter", "90d"), ("ytd", "ytd"),
    ("last 45 days", "45d"), ("hello", None),
])
def test_parse_period(msg, expected):
    assert bot.parse_period(msg) == expected


# ---------- intents ----------
@pytest.mark.parametrize("msg,intent", [
    ("1", "SNAPSHOT"), ("2", "COMPARE"), ("3", "TOP_DEALERS"),
    ("top dealers", "TOP_DEALERS"), ("sales by state", "STATE"),
    ("dealers at risk", "RISK_ANALYSIS"), ("show dashboard", "DASHBOARD"),
    ("balaji vs anand", "COMPARE"), ("top 5 dealers for tmt", "PRODUCT"),
    ("snapshot of balaji", "SNAPSHOT"), ("zzz", None),
])
def test_detect_intent(db, msg, intent):
    assert bot.detect_intent(msg) == intent


def test_keyword_needs_word_boundary(db):
    # 'vs' must not match inside 'canvas'
    assert bot.detect_intent("canvas") is None


# ---------- extraction ----------
def test_extract_dealers():
    assert bot.extract_dealers("compare Balaji vs Anand Traders") == ["Balaji", "Anand Traders"]
    assert bot.extract_dealers("snapshot of Anand") == ["Anand"]  # 'and' inside a name is safe
    assert bot.extract_dealers("dealer balaji last month") == ["balaji"]


def test_extract_product():
    assert bot.extract_product("top 5 dealers for TMT") == "TMT"


# ---------- conversations ----------
def test_menu_and_goodbye(db):
    assert "Welcome" in ask("hi")
    assert "Goodbye" in ask("bye")


def test_snapshot_single_match(db):
    out = ask("snapshot of anand")
    assert "ANAND TRADERS" in out and "Health Score" in out


def test_snapshot_disambiguation_and_selection(db):
    out = ask("dealer balaji")
    assert "Did you mean" in out and "1️⃣" in out
    assert "Dealer Snapshot" in ask("1")


def test_selection_out_of_range_does_not_crash(db):
    ask("dealer balaji")
    assert "between 1 and 2" in ask("9")


def test_slot_filling_after_menu_choice(db):
    assert "Which dealer" in ask("1")
    assert "ANAND TRADERS" in ask("anand")


def test_dealer_not_found(db):
    assert "not found" in ask("snapshot of nosuchdealer")


def test_dealer_with_sql_metacharacters_is_safe(db):
    out = ask("snapshot of x' OR '1'='1")
    assert "not found" in out
    assert ask("top dealers")  # DB intact


def test_compare(db):
    out = ask("compare anand vs dormant")
    assert "Highest Sales" in out and "ANAND TRADERS" in out


def test_top_dealers_and_pagination(db, monkeypatch):
    monkeypatch.setattr(bot, "MAX_ROWS", 2)
    out = ask("top dealers last year")
    assert "BALAJI STEEL" in out and "show next" in out
    page2 = ask("show next")
    assert "DORMANT CO" in page2 or "SRI BALAJI" in page2
    assert "nothing more" in ask("show next")


def test_sales_by_state(db):
    assert "Karnataka" in ask("sales by state")


def test_product_filter(db):
    out = ask("top dealers for coils")
    assert "SRI BALAJI AGENCIES" in out and "ANAND" not in out


def test_risk_includes_dealers_with_no_recent_sales(db):
    assert "DORMANT CO" in ask("dealers at risk")


def test_dashboard(db):
    assert "Sales Dashboard" in ask("show dashboard last month")


def test_period_is_remembered_across_messages(db):
    ask("sales by state last month")
    ctx = bot.get_context("u1")
    ask("top dealers")
    assert ctx["last_period"] == "30d"


def test_free_form_goes_to_llm_and_is_validated(db):
    out = ask("which state has the most revenue?")
    assert "Karnataka" in out


def test_unsafe_llm_sql_blocked(db, monkeypatch):
    monkeypatch.setattr(bot, "generate_sql", lambda m: "DROP TABLE sales")
    assert "safe query" in ask("blah blah analysis")
    assert "Karnataka" in ask("sales by state")


def test_errors_are_caught(db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("nope")
    monkeypatch.setattr(bot, "generate_sql", boom)
    assert "Something went wrong" in ask("unmatched question here")


def test_conversations_are_logged(db):
    ask("hi")
    assert "GREETING" in open(bot.LOGS_FILE).read()


def test_health_score_handles_dealer_with_no_orders():
    score, status, reasons = bot.health_score({"LastTxn": None, "Orders": 0, "TotalSales": 0})
    assert status.endswith("At Risk") and reasons
