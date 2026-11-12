import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot  # noqa: E402


def _d(days_ago):
    return (date.today() - timedelta(days=days_ago)).isoformat()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Small synthetic sales DB; patches bot to use it and a temp log file."""
    path = tmp_path / "sales.db"
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE sales (Dealer TEXT, State TEXT, Product TEXT, "Basic Amount" REAL, "Invoice Date" TEXT)')
    rows = [("BALAJI STEEL CORPORATION", "Karnataka", "TMT Bars", 200_000, _d(d)) for d in range(2, 60, 2)]
    rows += [("SRI BALAJI AGENCIES", "Karnataka", "Coils", 30_000, _d(5)), ("SRI BALAJI AGENCIES", "Karnataka", "Coils", 20_000, _d(80))]
    rows += [("ANAND TRADERS", "Kerala", "TMT Bars", 90_000, _d(10))] * 6
    rows += [("DORMANT CO", "Kerala", "Coils", 500_000, _d(300))]
    conn.executemany("INSERT INTO sales VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    monkeypatch.setattr(bot, "DB_FILE", str(path))
    monkeypatch.setattr(bot, "LOGS_FILE", str(tmp_path / "logs.jsonl"))
    monkeypatch.setattr(bot, "generate_sql", lambda msg: 'SELECT State, SUM("Basic Amount") AS Total FROM sales GROUP BY State')
    bot.load_intents()
    bot._conversation_contexts.clear()
    return path
