from database import get_db
from datetime import datetime, timedelta
from typing import Optional


def _time_to_minutes(t: str) -> int:
    parts = t.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def _minutes_to_time(m: int) -> str:
    h = m // 60 % 24
    mi = m % 60
    return f"{h:02d}:{mi:02d}"


def _add_minutes(t: str, minutes: int) -> str:
    return _minutes_to_time(_time_to_minutes(t) + minutes)


def _sub_minutes(t: str, minutes: int) -> str:
    return _minutes_to_time(_time_to_minutes(t) - minutes)


def get_season_for_date(date_str: str) -> Optional[dict]:
    conn = get_db()
    row = conn.execute("SELECT * FROM seasons").fetchall()
    conn.close()
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    month = dt.month
    day = dt.day
    date_val = month * 100 + day
    for s in row:
        start_val = s["start_month"] * 100 + s["start_day"]
        end_val = s["end_month"] * 100 + s["end_day"]
        if start_val <= end_val:
            if start_val <= date_val <= end_val:
                return dict(s)
        else:
            if date_val >= start_val or date_val <= end_val:
                return dict(s)
    return None


def get_curfew_for_season(season_id: int) -> Optional[dict]:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM curfew_rules WHERE season_id = ?", (season_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_festival_for_date(date_str: str) -> Optional[dict]:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM festivals WHERE festival_date = ?", (date_str,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_alert_for_date(date_str: str) -> Optional[dict]:
    conn = get_db()
    row = conn.execute(
        "SELECT da.*, al.* FROM daily_alerts da JOIN alert_levels al ON da.alert_level_id = al.id WHERE da.alert_date = ?",
        (date_str,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def check_time_conflict(open_time: str, close_time: str, curfew_start: str, curfew_end: str) -> list[str]:
    conflicts = []
    o = _time_to_minutes(open_time)
    c = _time_to_minutes(close_time)
    cs = _time_to_minutes(curfew_start)
    ce = _time_to_minutes(curfew_end)
    if cs < ce:
        if not (c <= cs or o >= ce):
            conflicts.append(f"开门时段 {open_time}-{close_time} 与宵禁时段 {curfew_start}-{curfew_end} 存在重叠")
    else:
        if not (c <= cs and o >= ce):
            conflicts.append(f"开门时段 {open_time}-{close_time} 与跨日宵禁时段 {curfew_start}-{curfew_end} 存在重叠")
    return conflicts


def generate_schedule_for_gate_date(gate_id: int, date_str: str) -> dict:
    conn = get_db()
    gate = conn.execute("SELECT * FROM gates WHERE id = ?", (gate_id,)).fetchone()
    if not gate:
        conn.close()
        return {"error": "城门不存在"}

    season = get_season_for_date(date_str)
    if not season:
        conn.close()
        return {"error": f"日期 {date_str} 未匹配到时令配置"}

    curfew = get_curfew_for_season(season["id"])
    festival = get_festival_for_date(date_str)
    alert = get_alert_for_date(date_str)

    base_open = season["sunrise_time"]
    base_close = season["sunset_time"]
    conflicts = []

    if curfew:
        conflicts.extend(check_time_conflict(base_open, base_close, curfew["curfew_start"], curfew["curfew_end"]))

    regular_open = base_open
    regular_close = base_close

    festival_open = base_open
    festival_close = base_close
    if festival:
        festival_close = _add_minutes(base_close, festival["delay_minutes"])
        if curfew:
            conflicts.extend(check_time_conflict(festival_open, festival_close, curfew["curfew_start"], curfew["curfew_end"]))

    alert_open = base_open
    alert_close = base_close
    if alert:
        alert_close = _sub_minutes(base_close, alert["close_advance_minutes"])
        alert_open = _add_minutes(base_open, alert["open_delay_minutes"])
        if curfew:
            conflicts.extend(check_time_conflict(alert_open, alert_close, curfew["curfew_start"], curfew["curfew_end"]))

    final_open = base_open
    final_close = base_close
    applied_rules = ["常规"]
    if festival:
        final_close = festival_close
        applied_rules.append(f"节庆延迟({festival['festival_name']})")
    if alert:
        final_close = min(final_close, alert_close, key=_time_to_minutes)
        final_open = max(final_open, alert_open, key=_time_to_minutes)
        applied_rules.append(f"警戒等级({alert['level_name']})")
    if curfew:
        cs_min = _time_to_minutes(curfew["curfew_start"])
        fc_min = _time_to_minutes(final_close)
        if fc_min > cs_min:
            final_close = curfew["curfew_start"]
            applied_rules.append("宵禁提前关闭")

    if _time_to_minutes(final_open) >= _time_to_minutes(final_close):
        conflicts.append(f"最终开门时间 {final_open} 不早于关门时间 {final_close}")

    c = conn.cursor()
    for scheme_type, o_t, c_t in [
        ("regular", regular_open, regular_close),
        ("festival", festival_open, festival_close),
        ("alert", alert_open, alert_close),
        ("final", final_open, final_close),
    ]:
        conflict_str = "; ".join(conflicts) if scheme_type == "final" else ""
        c.execute(
            """INSERT OR REPLACE INTO schedules (gate_id, schedule_date, scheme_type, open_time, close_time, conflict_note)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (gate_id, date_str, scheme_type, o_t, c_t, conflict_str),
        )

    conn.commit()
    conn.close()

    return {
        "gate_id": gate_id,
        "gate_name": gate["gate_name"],
        "date": date_str,
        "season": season["season_name"],
        "sunrise": season["sunrise_time"],
        "sunset": season["sunset_time"],
        "regular": {"open": regular_open, "close": regular_close},
        "festival": {"open": festival_open, "close": festival_close} if festival else None,
        "alert": {"open": alert_open, "close": alert_close} if alert else None,
        "final": {"open": final_open, "close": final_close, "rules": applied_rules},
        "conflicts": conflicts,
        "has_conflict": len(conflicts) > 0,
    }


def generate_weekly_schedules(start_date: str) -> list[dict]:
    results = []
    base = datetime.strptime(start_date, "%Y-%m-%d")
    conn = get_db()
    gates = conn.execute("SELECT * FROM gates").fetchall()
    conn.close()
    for i in range(7):
        d = (base + timedelta(days=i)).strftime("%Y-%m-%d")
        for gate in gates:
            results.append(generate_schedule_for_gate_date(gate["id"], d))
    return results


def check_publish_readiness(date_str: str) -> dict:
    conn = get_db()
    schedules = conn.execute(
        "SELECT s.*, g.gate_name FROM schedules s JOIN gates g ON s.gate_id = g.id WHERE s.schedule_date = ? AND s.scheme_type = 'final'",
        (date_str,),
    ).fetchall()
    conn.close()

    if not schedules:
        return {"can_publish": False, "reasons": ["该日期尚无最终方案"]}

    reasons = []
    for s in schedules:
        if s["conflict_note"]:
            reasons.append(f"{s['gate_name']}: {s['conflict_note']}")
        if _time_to_minutes(s["open_time"]) >= _time_to_minutes(s["close_time"]):
            reasons.append(f"{s['gate_name']}: 开门时间不早于关门时间")

    return {"can_publish": len(reasons) == 0, "reasons": reasons}


def publish_schedule(date_str: str) -> dict:
    readiness = check_publish_readiness(date_str)
    if not readiness["can_publish"]:
        return {"success": False, "errors": readiness["reasons"]}

    conn = get_db()
    schedules = conn.execute(
        "SELECT * FROM schedules WHERE schedule_date = ? AND scheme_type = 'final'",
        (date_str,),
    ).fetchall()

    for s in schedules:
        conn.execute(
            """INSERT OR REPLACE INTO published_schedules (gate_id, schedule_date, open_time, close_time)
               VALUES (?, ?, ?, ?)""",
            (s["gate_id"], date_str, s["open_time"], s["close_time"]),
        )
        conn.execute(
            "UPDATE schedules SET is_published = 1 WHERE id = ?", (s["id"],)
        )

    conn.commit()
    conn.close()
    return {"success": True}


def recalculate_schedules_for_date_range(start_date: str, end_date: str = None) -> dict:
    if end_date is None:
        end_date = start_date
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    conn = get_db()
    gates = conn.execute("SELECT * FROM gates").fetchall()
    conn.close()

    results = []
    current = start
    while current <= end:
        date_str = current.strftime("%Y-%m-%d")
        for gate in gates:
            results.append(generate_schedule_for_gate_date(gate["id"], date_str))
        current += timedelta(days=1)

    conflicts = [r for r in results if r.get("has_conflict", False)]
    return {
        "total": len(results),
        "gates": len(gates),
        "days": (end - start).days + 1,
        "conflicts": len(conflicts),
        "conflict_details": conflicts,
    }


def check_curfew_conflict_with_season(curfew_start: str, curfew_end: str, season_id: int) -> list[str]:
    conflicts = []
    conn = get_db()
    season = conn.execute("SELECT * FROM seasons WHERE id = ?", (season_id,)).fetchone()
    conn.close()

    if not season:
        return ["关联时令不存在"]

    sunrise = season["sunrise_time"]
    sunset = season["sunset_time"]

    cs = _time_to_minutes(curfew_start)
    ce = _time_to_minutes(curfew_end)
    sr = _time_to_minutes(sunrise)
    ss = _time_to_minutes(sunset)

    if cs < ce:
        if not (ss <= cs or sr >= ce):
            conflicts.append(
                f"宵禁时段 {curfew_start}-{curfew_end} 与开门时段 {sunrise}-{sunset} 存在重叠"
            )
    else:
        if not (ss <= cs and sr >= ce):
            conflicts.append(
                f"跨日宵禁时段 {curfew_start}-{curfew_end} 与开门时段 {sunrise}-{sunset} 存在重叠"
            )

    return conflicts


def get_weekly_chart_data(start_date: str) -> dict:
    base = datetime.strptime(start_date, "%Y-%m-%d")
    dates = [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    conn = get_db()
    gates = conn.execute("SELECT * FROM gates").fetchall()

    chart_data = {"dates": dates, "gates": []}
    for gate in gates:
        gate_data = {
            "gate_name": gate["gate_name"],
            "gate_code": gate["gate_code"],
            "regular": {"open": [], "close": []},
            "festival": {"open": [], "close": []},
            "alert": {"open": [], "close": []},
            "final": {"open": [], "close": []},
        }
        for d in dates:
            for scheme in ["regular", "festival", "alert", "final"]:
                row = conn.execute(
                    "SELECT * FROM schedules WHERE gate_id = ? AND schedule_date = ? AND scheme_type = ?",
                    (gate["id"], d, scheme),
                ).fetchone()
                if row:
                    gate_data[scheme]["open"].append(_time_to_minutes(row["open_time"]))
                    gate_data[scheme]["close"].append(_time_to_minutes(row["close_time"]))
                else:
                    gate_data[scheme]["open"].append(None)
                    gate_data[scheme]["close"].append(None)
        chart_data["gates"].append(gate_data)

    conn.close()
    return chart_data


def get_dates_in_season(season_id: int) -> list[str]:
    conn = get_db()
    season = conn.execute("SELECT * FROM seasons WHERE id = ?", (season_id,)).fetchone()
    conn.close()

    if not season:
        return []

    from datetime import datetime, date
    year = date.today().year
    dates = []

    start_month = season["start_month"]
    start_day = season["start_day"]
    end_month = season["end_month"]
    end_day = season["end_day"]

    try:
        start_date = datetime(year, start_month, start_day)
        end_date = datetime(year, end_month, end_day)

        if start_date > end_date:
            end_date = datetime(year + 1, end_month, end_day)

        current = start_date
        while current <= end_date:
            dates.append(current.strftime("%Y-%m-%d"))
            current += timedelta(days=1)
    except ValueError:
        pass

    return dates
