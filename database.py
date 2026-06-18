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
        CREATE TABLE IF NOT EXISTS temp_control_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_name TEXT NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            time_start TEXT DEFAULT '00:00',
            time_end TEXT DEFAULT '23:59',
            action_type TEXT NOT NULL CHECK(action_type IN ('force_close','force_open','restrict_hours')),
            forced_open_time TEXT DEFAULT '',
            forced_close_time TEXT DEFAULT '',
            priority INTEGER NOT NULL DEFAULT 10,
            override_reason TEXT DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS temp_control_gates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            gate_id INTEGER NOT NULL,
            FOREIGN KEY (order_id) REFERENCES temp_control_orders(id) ON DELETE CASCADE,
            FOREIGN KEY (gate_id) REFERENCES gates(id) ON DELETE CASCADE,
            UNIQUE(order_id, gate_id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS gate_linkage_strategies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_name TEXT NOT NULL,
            trigger_type TEXT NOT NULL CHECK(trigger_type IN ('gate_event','time_based','manual')),
            trigger_gate_id INTEGER DEFAULT NULL,
            trigger_event TEXT DEFAULT '',
            linked_open_time TEXT DEFAULT '',
            linked_close_time TEXT DEFAULT '',
            priority INTEGER NOT NULL DEFAULT 5,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
            description TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (trigger_gate_id) REFERENCES gates(id) ON DELETE SET NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS gate_linkage_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id INTEGER NOT NULL,
            gate_id INTEGER NOT NULL,
            effect_open_time TEXT DEFAULT '',
            effect_close_time TEXT DEFAULT '',
            FOREIGN KEY (strategy_id) REFERENCES gate_linkage_strategies(id) ON DELETE CASCADE,
            FOREIGN KEY (gate_id) REFERENCES gates(id) ON DELETE CASCADE,
            UNIQUE(strategy_id, gate_id)
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

    c.execute("""
        CREATE TABLE IF NOT EXISTS traffic_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gate_id INTEGER NOT NULL,
            record_date TEXT NOT NULL,
            time_period TEXT NOT NULL CHECK(time_period IN ('morning_peak','evening_peak')),
            volume INTEGER NOT NULL DEFAULT 0,
            event_factor REAL NOT NULL DEFAULT 1.0,
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (gate_id) REFERENCES gates(id) ON DELETE CASCADE,
            UNIQUE(gate_id, record_date, time_period)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS traffic_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gate_id INTEGER NOT NULL,
            predict_date TEXT NOT NULL,
            time_period TEXT NOT NULL CHECK(time_period IN ('morning_peak','evening_peak')),
            predicted_volume INTEGER NOT NULL DEFAULT 0,
            confidence REAL NOT NULL DEFAULT 0.0,
            gate_capacity INTEGER NOT NULL DEFAULT 0,
            overload_ratio REAL NOT NULL DEFAULT 0.0,
            is_overload INTEGER NOT NULL DEFAULT 0 CHECK(is_overload IN (0,1)),
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (gate_id) REFERENCES gates(id) ON DELETE CASCADE,
            UNIQUE(gate_id, predict_date, time_period)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS dispatch_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gate_id INTEGER NOT NULL,
            suggest_date TEXT NOT NULL,
            time_period TEXT NOT NULL DEFAULT 'morning_peak' CHECK(time_period IN ('morning_peak','evening_peak')),
            suggestion_type TEXT NOT NULL CHECK(suggestion_type IN ('stagger_open','delay_close','temp_divert','gate_switch')),
            description TEXT NOT NULL DEFAULT '',
            detail TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','executed','dismissed')),
            before_volume INTEGER DEFAULT 0,
            after_volume INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (gate_id) REFERENCES gates(id) ON DELETE CASCADE
        )
    """)

    try:
        c.execute("ALTER TABLE gates ADD COLUMN capacity INTEGER NOT NULL DEFAULT 500")
    except Exception:
        pass

    try:
        c.execute("ALTER TABLE gates ADD COLUMN peak_capacity INTEGER NOT NULL DEFAULT 200")
    except Exception:
        pass

    try:
        c.execute("ALTER TABLE schedules ADD COLUMN rule_chain TEXT DEFAULT ''")
    except Exception:
        pass

    try:
        c.execute("ALTER TABLE schedules ADD COLUMN override_reason TEXT DEFAULT ''")
    except Exception:
        pass

    try:
        c.execute("ALTER TABLE schedules ADD COLUMN linkage_scope TEXT DEFAULT ''")
    except Exception:
        pass

    c.execute("""
        CREATE TABLE IF NOT EXISTS resource_pools (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            resource_type TEXT NOT NULL CHECK(resource_type IN ('guard','patrol','light','repair','reserve')),
            total_quantity INTEGER NOT NULL DEFAULT 0,
            allocated_quantity INTEGER NOT NULL DEFAULT 0,
            unit TEXT NOT NULL DEFAULT '人',
            description TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS resource_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gate_id INTEGER NOT NULL,
            config_date TEXT NOT NULL,
            time_period TEXT NOT NULL CHECK(time_period IN ('morning_peak','daytime','evening_peak','night')),
            guard_count INTEGER NOT NULL DEFAULT 0,
            patrol_shifts INTEGER NOT NULL DEFAULT 0,
            patrol_interval INTEGER NOT NULL DEFAULT 60,
            light_supplies INTEGER NOT NULL DEFAULT 0,
            repair_occupancy INTEGER NOT NULL DEFAULT 0,
            reserve_team INTEGER NOT NULL DEFAULT 0,
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (gate_id) REFERENCES gates(id) ON DELETE CASCADE,
            UNIQUE(gate_id, config_date, time_period)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS garrison_shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gate_id INTEGER NOT NULL,
            shift_date TEXT NOT NULL,
            shift_type TEXT NOT NULL CHECK(shift_type IN ('morning','midday','evening','night')),
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            guard_count INTEGER NOT NULL DEFAULT 0,
            patrol_route TEXT DEFAULT '',
            assigned_personnel TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'scheduled' CHECK(status IN ('scheduled','confirmed','completed','cancelled')),
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (gate_id) REFERENCES gates(id) ON DELETE CASCADE
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS resource_gaps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gate_id INTEGER NOT NULL,
            gap_date TEXT NOT NULL,
            time_period TEXT NOT NULL,
            resource_type TEXT NOT NULL CHECK(resource_type IN ('guard','patrol','light','repair','reserve')),
            required_quantity INTEGER NOT NULL DEFAULT 0,
            available_quantity INTEGER NOT NULL DEFAULT 0,
            gap_quantity INTEGER NOT NULL DEFAULT 0,
            severity TEXT NOT NULL DEFAULT 'warning' CHECK(severity IN ('info','warning','critical')),
            description TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved','ignored')),
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (gate_id) REFERENCES gates(id) ON DELETE CASCADE
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS gate_downgrade_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gate_id INTEGER NOT NULL,
            suggest_date TEXT NOT NULL,
            time_period TEXT NOT NULL,
            current_level TEXT NOT NULL,
            suggested_level TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            expected_guard_saving INTEGER NOT NULL DEFAULT 0,
            expected_patrol_saving INTEGER NOT NULL DEFAULT 0,
            impact_assessment TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected','implemented')),
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (gate_id) REFERENCES gates(id) ON DELETE CASCADE
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS cross_gate_allocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            allocation_date TEXT NOT NULL,
            from_gate_id INTEGER NOT NULL,
            to_gate_id INTEGER NOT NULL,
            resource_type TEXT NOT NULL CHECK(resource_type IN ('guard','patrol','reserve')),
            transfer_quantity INTEGER NOT NULL DEFAULT 0,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            reason TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','approved','in_progress','completed','cancelled')),
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (from_gate_id) REFERENCES gates(id) ON DELETE CASCADE,
            FOREIGN KEY (to_gate_id) REFERENCES gates(id) ON DELETE CASCADE
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS defense_evaluation_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evaluate_date TEXT NOT NULL,
            gate_id INTEGER NOT NULL,
            time_period TEXT NOT NULL,
            overall_score INTEGER NOT NULL DEFAULT 0,
            guard_sufficiency REAL NOT NULL DEFAULT 0,
            patrol_sufficiency REAL NOT NULL DEFAULT 0,
            light_sufficiency REAL NOT NULL DEFAULT 0,
            repair_sufficiency REAL NOT NULL DEFAULT 0,
            reserve_sufficiency REAL NOT NULL DEFAULT 0,
            has_gap INTEGER NOT NULL DEFAULT 0,
            gaps_count INTEGER NOT NULL DEFAULT 0,
            non_executable_rules TEXT DEFAULT '',
            evaluation_data TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (gate_id) REFERENCES gates(id) ON DELETE CASCADE,
            UNIQUE(evaluate_date, gate_id, time_period)
        )
    """)

    try:
        c.execute("ALTER TABLE gates ADD COLUMN defense_level TEXT NOT NULL DEFAULT 'normal' CHECK(defense_level IN ('minimal','reduced','normal','enhanced','maximum'))")
    except Exception:
        pass

    try:
        c.execute("ALTER TABLE gates ADD COLUMN min_guard_required INTEGER NOT NULL DEFAULT 2")
    except Exception:
        pass

    try:
        c.execute("ALTER TABLE gates ADD COLUMN min_patrol_required INTEGER NOT NULL DEFAULT 1")
    except Exception:
        pass

    c.execute("""
        CREATE TABLE IF NOT EXISTS public_opinion_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_code TEXT UNIQUE NOT NULL,
            gate_id INTEGER NOT NULL,
            event_date TEXT NOT NULL,
            time_period TEXT NOT NULL CHECK(time_period IN ('early_morning','morning_peak','daytime','evening_peak','night','late_night')),
            event_type TEXT NOT NULL CHECK(event_type IN ('vendor_gathering','people_petition','patrol_anomaly','road_blockage','fire_rumor','other')),
            event_level TEXT NOT NULL CHECK(event_level IN ('minor','moderate','serious','critical')),
            credibility INTEGER NOT NULL DEFAULT 50 CHECK(credibility BETWEEN 0 AND 100),
            handle_deadline TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            reporter TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'reported' CHECK(status IN ('reported','responding','handling','resolved','closed')),
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (gate_id) REFERENCES gates(id) ON DELETE CASCADE
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS public_opinion_notice_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_name TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK(event_type IN ('vendor_gathering','people_petition','patrol_anomaly','road_blockage','fire_rumor','other')),
            event_level TEXT NOT NULL CHECK(event_level IN ('minor','moderate','serious','critical')),
            template_content TEXT NOT NULL,
            announce_position TEXT DEFAULT 'gate_top',
            is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS public_opinion_responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            response_type TEXT NOT NULL CHECK(response_type IN ('temp_response','gate_notice','resource_support','schedule_adjust')),
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 5,
            status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','approved','executed','rejected')),
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (event_id) REFERENCES public_opinion_events(id) ON DELETE CASCADE
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS public_opinion_progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            progress_type TEXT NOT NULL CHECK(progress_type IN ('report','assign','handle','update','resolve','close')),
            title TEXT NOT NULL,
            content TEXT DEFAULT '',
            operator TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (event_id) REFERENCES public_opinion_events(id) ON DELETE CASCADE
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS public_opinion_gate_notices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            gate_id INTEGER NOT NULL,
            template_id INTEGER,
            notice_title TEXT NOT NULL,
            notice_content TEXT NOT NULL,
            display_position TEXT DEFAULT 'gate_top',
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published','expired','cancelled')),
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (event_id) REFERENCES public_opinion_events(id) ON DELETE CASCADE,
            FOREIGN KEY (gate_id) REFERENCES gates(id) ON DELETE CASCADE,
            FOREIGN KEY (template_id) REFERENCES public_opinion_notice_templates(id) ON DELETE SET NULL
        )
    """)

    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_po_events_date ON public_opinion_events(event_date)
    """)

    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_po_events_gate ON public_opinion_events(gate_id)
    """)

    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_po_events_status ON public_opinion_events(status)
    """)

    conn.commit()
    conn.close()
