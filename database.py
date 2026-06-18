import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "citygate.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS gates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gate_code TEXT UNIQUE NOT NULL,
            gate_name TEXT NOT NULL,
            direction TEXT NOT NULL CHECK(direction IN ('东','南','西','北')),
            is_main INTEGER NOT NULL DEFAULT 0 CHECK(is_main IN (0,1)),
            notes TEXT DEFAULT ''
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS seasons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            season_name TEXT UNIQUE NOT NULL,
            start_month INTEGER NOT NULL,
            start_day INTEGER NOT NULL,
            end_month INTEGER NOT NULL,
            end_day INTEGER NOT NULL,
            sunrise_time TEXT NOT NULL,
            sunset_time TEXT NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS curfew_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_name TEXT NOT NULL,
            season_id INTEGER NOT NULL,
            curfew_start TEXT NOT NULL,
            curfew_end TEXT NOT NULL,
            FOREIGN KEY (season_id) REFERENCES seasons(id) ON DELETE CASCADE
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS festivals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            festival_name TEXT NOT NULL,
            festival_date TEXT NOT NULL UNIQUE,
            delay_minutes INTEGER NOT NULL DEFAULT 30,
            notes TEXT DEFAULT ''
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS alert_levels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level_name TEXT UNIQUE NOT NULL,
            level_value INTEGER UNIQUE NOT NULL CHECK(level_value BETWEEN 1 AND 5),
            close_advance_minutes INTEGER NOT NULL DEFAULT 0,
            open_delay_minutes INTEGER NOT NULL DEFAULT 0,
            description TEXT DEFAULT ''
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_date TEXT NOT NULL,
            alert_level_id INTEGER NOT NULL,
            FOREIGN KEY (alert_level_id) REFERENCES alert_levels(id) ON DELETE CASCADE,
            UNIQUE(alert_date)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gate_id INTEGER NOT NULL,
            schedule_date TEXT NOT NULL,
            scheme_type TEXT NOT NULL CHECK(scheme_type IN ('regular','festival','alert','final')),
            open_time TEXT NOT NULL,
            close_time TEXT NOT NULL,
            is_published INTEGER NOT NULL DEFAULT 0 CHECK(is_published IN (0,1)),
            conflict_note TEXT DEFAULT '',
            FOREIGN KEY (gate_id) REFERENCES gates(id) ON DELETE CASCADE,
            UNIQUE(gate_id, schedule_date, scheme_type)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS published_schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gate_id INTEGER NOT NULL,
            schedule_date TEXT NOT NULL,
            open_time TEXT NOT NULL,
            close_time TEXT NOT NULL,
            published_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (gate_id) REFERENCES gates(id) ON DELETE CASCADE,
            UNIQUE(gate_id, schedule_date)
        )
    """)

    conn.commit()
    conn.close()
