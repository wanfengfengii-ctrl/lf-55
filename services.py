import json
from database import get_db
from datetime import datetime, timedelta
from typing import Optional


_WEEKDAY_WEIGHTS = [0.85, 1.0, 1.0, 1.0, 1.0, 1.15, 1.10]

_SUGGESTION_LABELS = {
    "stagger_open": "分时开门",
    "delay_close": "延后关门",
    "temp_divert": "临时分流",
    "gate_switch": "主副门切换",
}


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


def _time_in_range(t: str, range_start: str, range_end: str) -> bool:
    tm = _time_to_minutes(t)
    rs = _time_to_minutes(range_start)
    re = _time_to_minutes(range_end)
    if rs <= re:
        return rs <= tm <= re
    return tm >= rs or tm <= re


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


def get_active_temp_controls_for_gate(gate_id: int, date_str: str) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        """SELECT tco.* FROM temp_control_orders tco
           JOIN temp_control_gates tcg ON tco.id = tcg.order_id
           WHERE tcg.gate_id = ? AND tco.is_active = 1
           AND tco.start_date <= ? AND tco.end_date >= ?
           ORDER BY tco.priority DESC""",
        (gate_id, date_str, date_str),
    ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        gates = conn.execute(
            "SELECT tcg.gate_id, g.gate_name FROM temp_control_gates tcg JOIN gates g ON tcg.gate_id = g.id WHERE tcg.order_id = ?",
            (d["id"],),
        ).fetchall()
        d["gates"] = [dict(g) for g in gates]
        result.append(d)
    conn.close()
    return result


def get_active_linkage_for_gate(gate_id: int, date_str: str) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        """SELECT gls.* FROM gate_linkage_strategies gls
           JOIN gate_linkage_items gli ON gls.id = gli.strategy_id
           WHERE gli.gate_id = ? AND gls.is_active = 1
           ORDER BY gls.priority DESC""",
        (gate_id,),
    ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        items = conn.execute(
            "SELECT gli.gate_id, gli.effect_open_time, gli.effect_close_time, g.gate_name FROM gate_linkage_items gli JOIN gates g ON gli.gate_id = g.id WHERE gli.strategy_id = ?",
            (d["id"],),
        ).fetchall()
        d["linked_gates"] = [dict(i) for i in items]
        result.append(d)
    conn.close()
    return result


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
    temp_controls = get_active_temp_controls_for_gate(gate_id, date_str)
    linkages = get_active_linkage_for_gate(gate_id, date_str)

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
    rule_chain = [{"source": "时令", "detail": f"{season['season_name']}({base_open}-{base_close})"}]
    override_reason = ""
    linkage_scope = []

    if festival:
        final_close = festival_close
        applied_rules.append(f"节庆延迟({festival['festival_name']})")
        rule_chain.append({"source": "节庆", "detail": f"{festival['festival_name']} 延迟{festival['delay_minutes']}分钟"})
    if alert:
        final_close = min(final_close, alert_close, key=_time_to_minutes)
        final_open = max(final_open, alert_open, key=_time_to_minutes)
        applied_rules.append(f"警戒等级({alert['level_name']})")
        rule_chain.append({"source": "警戒", "detail": f"{alert['level_name']} 提前关{alert['close_advance_minutes']}分/延迟开{alert['open_delay_minutes']}分"})
    if curfew:
        cs_min = _time_to_minutes(curfew["curfew_start"])
        fc_min = _time_to_minutes(final_close)
        if fc_min > cs_min:
            final_close = curfew["curfew_start"]
            applied_rules.append("宵禁提前关闭")
            rule_chain.append({"source": "宵禁", "detail": f"宵禁{curfew['curfew_start']}提前关闭"})

    if temp_controls:
        for tc in temp_controls:
            if tc["action_type"] == "force_close":
                final_open = "00:00"
                final_close = "00:00"
                applied_rules.append(f"管制令(关闭-{tc['order_name']})")
                rule_chain.append({"source": "临时管制令", "detail": f"{tc['order_name']} 强制关闭", "priority": tc["priority"]})
                override_reason = f"临时管制令「{tc['order_name']}」强制关闭，覆盖所有规则"
                conflicts.append(f"临时管制令「{tc['order_name']}」强制关闭城门，覆盖所有现有规则")
            elif tc["action_type"] == "force_open":
                final_open = "00:00"
                final_close = "23:59"
                applied_rules.append(f"管制令(开放-{tc['order_name']})")
                rule_chain.append({"source": "临时管制令", "detail": f"{tc['order_name']} 强制全天开放", "priority": tc["priority"]})
                override_reason = f"临时管制令「{tc['order_name']}」强制开放，覆盖所有规则"
            elif tc["action_type"] == "restrict_hours":
                if tc["forced_open_time"] and tc["forced_close_time"]:
                    final_open = tc["forced_open_time"]
                    final_close = tc["forced_close_time"]
                    applied_rules.append(f"管制令(限时-{tc['order_name']})")
                    rule_chain.append({"source": "临时管制令", "detail": f"{tc['order_name']} 限制时段{tc['forced_open_time']}-{tc['forced_close_time']}", "priority": tc["priority"]})
                    override_reason = f"临时管制令「{tc['order_name']}」限制开放时段为{tc['forced_open_time']}-{tc['forced_close_time']}"
                if curfew:
                    conflicts.extend(check_time_conflict(final_open, final_close, curfew["curfew_start"], curfew["curfew_end"]))

    if linkages:
        for lk in linkages:
            item = next((i for i in lk["linked_gates"] if i["gate_id"] == gate_id), None)
            if item:
                if item["effect_open_time"]:
                    final_open = item["effect_open_time"]
                if item["effect_close_time"]:
                    final_close = item["effect_close_time"]
                applied_rules.append(f"联动({lk['strategy_name']})")
                rule_chain.append({"source": "多城门联动", "detail": f"{lk['strategy_name']}"})
                linked_names = [g["gate_name"] for g in lk["linked_gates"] if g["gate_id"] != gate_id]
                if linked_names:
                    linkage_scope.extend(linked_names)

    linkage_scope = list(dict.fromkeys(linkage_scope))

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
        rc = json.dumps(rule_chain, ensure_ascii=False) if scheme_type == "final" else ""
        or_str = override_reason if scheme_type == "final" else ""
        ls_str = ",".join(linkage_scope) if scheme_type == "final" else ""
        c.execute(
            """INSERT OR REPLACE INTO schedules (gate_id, schedule_date, scheme_type, open_time, close_time, conflict_note, rule_chain, override_reason, linkage_scope)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (gate_id, date_str, scheme_type, o_t, c_t, conflict_str, rc, or_str, ls_str),
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
        "rule_chain": rule_chain,
        "override_reason": override_reason,
        "linkage_scope": linkage_scope,
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


def get_all_temp_controls() -> list[dict]:
    conn = get_db()
    rows = conn.execute("SELECT * FROM temp_control_orders ORDER BY start_date DESC").fetchall()
    result = []
    for row in rows:
        d = dict(row)
        gates = conn.execute(
            "SELECT tcg.gate_id, g.gate_name FROM temp_control_gates tcg JOIN gates g ON tcg.gate_id = g.id WHERE tcg.order_id = ?",
            (d["id"],),
        ).fetchall()
        d["gates"] = [dict(g) for g in gates]
        result.append(d)
    conn.close()
    return result


def create_temp_control(order_name, start_date, end_date, time_start, time_end,
                        action_type, forced_open_time, forced_close_time,
                        priority, override_reason, gate_ids) -> dict:
    conn = get_db()
    c = conn.cursor()
    c.execute(
        """INSERT INTO temp_control_orders (order_name, start_date, end_date, time_start, time_end, action_type, forced_open_time, forced_close_time, priority, override_reason)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (order_name, start_date, end_date, time_start or "00:00", time_end or "23:59",
         action_type, forced_open_time or "", forced_close_time or "",
         priority, override_reason),
    )
    order_id = c.lastrowid
    for gid in gate_ids:
        c.execute("INSERT OR IGNORE INTO temp_control_gates (order_id, gate_id) VALUES (?,?)", (order_id, gid))
    conn.commit()
    conn.close()
    recalculate_schedules_for_date_range(start_date, end_date)
    return {"success": True, "order_id": order_id}


def delete_temp_control(order_id: int) -> dict:
    conn = get_db()
    row = conn.execute("SELECT start_date, end_date FROM temp_control_orders WHERE id = ?", (order_id,)).fetchone()
    if row:
        sd, ed = row["start_date"], row["end_date"]
        conn.execute("DELETE FROM temp_control_gates WHERE order_id = ?", (order_id,))
        conn.execute("DELETE FROM temp_control_orders WHERE id = ?", (order_id,))
        conn.commit()
        conn.close()
        recalculate_schedules_for_date_range(sd, ed)
        return {"success": True}
    conn.close()
    return {"success": False, "error": "管制令不存在"}


def toggle_temp_control(order_id: int) -> dict:
    conn = get_db()
    row = conn.execute("SELECT is_active, start_date, end_date FROM temp_control_orders WHERE id = ?", (order_id,)).fetchone()
    if row:
        new_val = 0 if row["is_active"] else 1
        conn.execute("UPDATE temp_control_orders SET is_active = ? WHERE id = ?", (new_val, order_id))
        conn.commit()
        conn.close()
        recalculate_schedules_for_date_range(row["start_date"], row["end_date"])
        return {"success": True, "is_active": new_val}
    conn.close()
    return {"success": False}


def get_all_linkage_strategies() -> list[dict]:
    conn = get_db()
    rows = conn.execute("SELECT * FROM gate_linkage_strategies ORDER BY priority DESC").fetchall()
    result = []
    for row in rows:
        d = dict(row)
        items = conn.execute(
            "SELECT gli.gate_id, gli.effect_open_time, gli.effect_close_time, g.gate_name FROM gate_linkage_items gli JOIN gates g ON gli.gate_id = g.id WHERE gli.strategy_id = ?",
            (d["id"],),
        ).fetchall()
        d["linked_gates"] = [dict(i) for i in items]
        if d["trigger_gate_id"]:
            tg = conn.execute("SELECT gate_name FROM gates WHERE id = ?", (d["trigger_gate_id"],)).fetchone()
            d["trigger_gate_name"] = tg["gate_name"] if tg else ""
        else:
            d["trigger_gate_name"] = ""
        result.append(d)
    conn.close()
    return result


def create_linkage_strategy(strategy_name, trigger_type, trigger_gate_id,
                            trigger_event, linked_open_time, linked_close_time,
                            priority, description, items) -> dict:
    conn = get_db()
    c = conn.cursor()
    c.execute(
        """INSERT INTO gate_linkage_strategies (strategy_name, trigger_type, trigger_gate_id, trigger_event, linked_open_time, linked_close_time, priority, description)
           VALUES (?,?,?,?,?,?,?,?)""",
        (strategy_name, trigger_type, trigger_gate_id or None, trigger_event,
         linked_open_time or "", linked_close_time or "", priority, description),
    )
    strategy_id = c.lastrowid
    for item in items:
        c.execute(
            "INSERT OR IGNORE INTO gate_linkage_items (strategy_id, gate_id, effect_open_time, effect_close_time) VALUES (?,?,?,?)",
            (strategy_id, item["gate_id"], item.get("effect_open_time", ""), item.get("effect_close_time", "")),
        )
    conn.commit()
    conn.close()
    recalculate_schedules_for_date_range(
        (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"),
        (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"),
    )
    return {"success": True, "strategy_id": strategy_id}


def delete_linkage_strategy(strategy_id: int) -> dict:
    conn = get_db()
    conn.execute("DELETE FROM gate_linkage_items WHERE strategy_id = ?", (strategy_id,))
    conn.execute("DELETE FROM gate_linkage_strategies WHERE id = ?", (strategy_id,))
    conn.commit()
    conn.close()
    recalculate_schedules_for_date_range(
        (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"),
        (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"),
    )
    return {"success": True}


def toggle_linkage_strategy(strategy_id: int) -> dict:
    conn = get_db()
    row = conn.execute("SELECT is_active FROM gate_linkage_strategies WHERE id = ?", (strategy_id,)).fetchone()
    if row:
        new_val = 0 if row["is_active"] else 1
        conn.execute("UPDATE gate_linkage_strategies SET is_active = ? WHERE id = ?", (new_val, strategy_id))
        conn.commit()
        conn.close()
        recalculate_schedules_for_date_range(
            (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"),
            (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"),
        )
        return {"success": True, "is_active": new_val}
    conn.close()
    return {"success": False}


def preview_conflicts(start_date: str, end_date: str) -> dict:
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    conn = get_db()
    gates = conn.execute("SELECT * FROM gates").fetchall()
    conn.close()

    all_conflicts = []
    current = start
    while current <= end:
        date_str = current.strftime("%Y-%m-%d")
        for gate in gates:
            result = generate_schedule_for_gate_date(gate["id"], date_str)
            if result.get("has_conflict"):
                all_conflicts.append({
                    "gate_name": result["gate_name"],
                    "date": date_str,
                    "conflicts": result["conflicts"],
                    "override_reason": result.get("override_reason", ""),
                    "linkage_scope": result.get("linkage_scope", []),
                })
        current += timedelta(days=1)

    return {
        "start_date": start_date,
        "end_date": end_date,
        "total_conflicts": len(all_conflicts),
        "conflicts": all_conflicts,
    }


def simulate_batch_publish(start_date: str, end_date: str) -> dict:
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    conn = get_db()
    gates = conn.execute("SELECT * FROM gates").fetchall()
    conn.close()

    simulation = []
    current = start
    while current <= end:
        date_str = current.strftime("%Y-%m-%d")
        day_result = {"date": date_str, "gates": [], "can_publish": True}
        for gate in gates:
            result = generate_schedule_for_gate_date(gate["id"], date_str)
            can_publish = not result.get("has_conflict", False)
            day_result["gates"].append({
                "gate_name": result["gate_name"],
                "open": result["final"]["open"],
                "close": result["final"]["close"],
                "can_publish": can_publish,
                "conflicts": result.get("conflicts", []),
                "override_reason": result.get("override_reason", ""),
            })
            if not can_publish:
                day_result["can_publish"] = False
        simulation.append(day_result)
        current += timedelta(days=1)

    publishable_days = sum(1 for d in simulation if d["can_publish"])
    total_days = len(simulation)
    total_gates = len(gates)

    return {
        "simulation": simulation,
        "summary": {
            "total_days": total_days,
            "publishable_days": publishable_days,
            "blocked_days": total_days - publishable_days,
            "total_gates": total_gates,
        },
    }


def get_linkage_weekly_comparison(start_date: str, strategy_id: int) -> dict:
    base = datetime.strptime(start_date, "%Y-%m-%d")
    dates = [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]

    conn = get_db()
    strategy = conn.execute("SELECT * FROM gate_linkage_strategies WHERE id = ?", (strategy_id,)).fetchone()
    if not strategy:
        conn.close()
        return {"error": "联动策略不存在"}

    items = conn.execute(
        "SELECT gli.*, g.gate_name FROM gate_linkage_items gli JOIN gates g ON gli.gate_id = g.id WHERE gli.strategy_id = ?",
        (strategy_id,),
    ).fetchall()
    conn.close()

    with_linkage = {"dates": dates, "gates": []}
    without_linkage = {"dates": dates, "gates": []}

    saved_active = strategy["is_active"]

    conn = get_db()
    conn.execute("UPDATE gate_linkage_strategies SET is_active = 0 WHERE id = ?", (strategy_id,))
    conn.commit()
    conn.close()

    for item in items:
        gate_data_without = {"gate_name": item["gate_name"], "open": [], "close": []}
        for d in dates:
            result = generate_schedule_for_gate_date(item["gate_id"], d)
            gate_data_without["open"].append(_time_to_minutes(result["final"]["open"]))
            gate_data_without["close"].append(_time_to_minutes(result["final"]["close"]))
        without_linkage["gates"].append(gate_data_without)

    conn = get_db()
    conn.execute("UPDATE gate_linkage_strategies SET is_active = ? WHERE id = ?", (saved_active, strategy_id))
    conn.commit()
    conn.close()

    for item in items:
        gate_data_with = {"gate_name": item["gate_name"], "open": [], "close": []}
        for d in dates:
            result = generate_schedule_for_gate_date(item["gate_id"], d)
            gate_data_with["open"].append(_time_to_minutes(result["final"]["open"]))
            gate_data_with["close"].append(_time_to_minutes(result["final"]["close"]))
        with_linkage["gates"].append(gate_data_with)

    return {
        "strategy_name": strategy["strategy_name"],
        "with_linkage": with_linkage,
        "without_linkage": without_linkage,
    }


def add_traffic_history(gate_id: int, record_date: str, time_period: str,
                        volume: int, event_factor: float = 1.0, notes: str = "") -> dict:
    conn = get_db()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO traffic_history (gate_id, record_date, time_period, volume, event_factor, notes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (gate_id, record_date, time_period, volume, event_factor, notes),
        )
        conn.commit()
    except Exception as e:
        conn.close()
        return {"success": False, "error": str(e)}
    conn.close()
    return {"success": True}


def batch_add_traffic_history(records: list[dict]) -> dict:
    conn = get_db()
    try:
        for r in records:
            conn.execute(
                """INSERT OR REPLACE INTO traffic_history (gate_id, record_date, time_period, volume, event_factor, notes)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (r["gate_id"], r["record_date"], r["time_period"],
                 r.get("volume", 0), r.get("event_factor", 1.0), r.get("notes", "")),
            )
        conn.commit()
    except Exception as e:
        conn.close()
        return {"success": False, "error": str(e)}
    conn.close()
    return {"success": True, "count": len(records)}


def get_traffic_history(gate_id: int = None, start_date: str = None,
                        end_date: str = None, time_period: str = None) -> list[dict]:
    conn = get_db()
    query = """SELECT th.*, g.gate_name, g.gate_code FROM traffic_history th
               JOIN gates g ON th.gate_id = g.id WHERE 1=1"""
    params = []
    if gate_id is not None:
        query += " AND th.gate_id = ?"
        params.append(gate_id)
    if start_date:
        query += " AND th.record_date >= ?"
        params.append(start_date)
    if end_date:
        query += " AND th.record_date <= ?"
        params.append(end_date)
    if time_period:
        query += " AND th.time_period = ?"
        params.append(time_period)
    query += " ORDER BY th.record_date DESC, th.gate_id"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def predict_traffic_for_week(start_date: str) -> dict:
    base = datetime.strptime(start_date, "%Y-%m-%d")
    dates = [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    conn = get_db()
    gates = conn.execute("SELECT * FROM gates").fetchall()

    history_start = (base - timedelta(days=28)).strftime("%Y-%m-%d")
    history_end = (base - timedelta(days=1)).strftime("%Y-%m-%d")

    predictions = []
    for gate in gates:
        gate_id = gate["id"]
        capacity = gate["capacity"] if gate["capacity"] else 500
        peak_capacity = gate["peak_capacity"] if gate["peak_capacity"] else 200

        for period in ["morning_peak", "evening_peak"]:
            rows = conn.execute(
                """SELECT record_date, volume, event_factor FROM traffic_history
                   WHERE gate_id = ? AND time_period = ? AND record_date >= ? AND record_date <= ?
                   ORDER BY record_date""",
                (gate_id, period, history_start, history_end),
            ).fetchall()

            if not rows:
                base_volume = peak_capacity
                confidence = 0.3
            else:
                volumes = [r["volume"] for r in rows]
                factors = [r["event_factor"] for r in rows]
                adjusted = [v / f if f > 0 else v for v, f in zip(volumes, factors)]
                recent = adjusted[-7:] if len(adjusted) >= 7 else adjusted
                older = adjusted[:-7] if len(adjusted) > 7 else []
                if older:
                    base_volume = sum(recent) * 0.7 / len(recent) + sum(older) * 0.3 / len(older)
                else:
                    base_volume = sum(recent) / len(recent)
                confidence = min(0.95, 0.5 + 0.1 * min(len(rows), 5))

            for i, date_str in enumerate(dates):
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                weekday = dt.weekday()
                weekday_weight = _WEEKDAY_WEIGHTS[weekday]
                predicted_volume = int(base_volume * weekday_weight)
                overload_ratio = predicted_volume / peak_capacity if peak_capacity > 0 else 0
                is_overload = 1 if overload_ratio > 1.0 else 0

                conn.execute(
                    """INSERT OR REPLACE INTO traffic_predictions
                       (gate_id, predict_date, time_period, predicted_volume, confidence,
                        gate_capacity, overload_ratio, is_overload)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (gate_id, date_str, period, predicted_volume, round(confidence, 2),
                     peak_capacity, round(overload_ratio, 2), is_overload),
                )
                predictions.append({
                    "gate_id": gate_id,
                    "gate_name": gate["gate_name"],
                    "gate_code": gate["gate_code"],
                    "predict_date": date_str,
                    "time_period": period,
                    "predicted_volume": predicted_volume,
                    "confidence": round(confidence, 2),
                    "gate_capacity": peak_capacity,
                    "overload_ratio": round(overload_ratio, 2),
                    "is_overload": is_overload,
                })

    conn.commit()
    conn.close()
    _generate_dispatch_suggestions(start_date)
    return {"dates": dates, "predictions": predictions}


def _generate_dispatch_suggestions(start_date: str) -> list[dict]:
    base = datetime.strptime(start_date, "%Y-%m-%d")
    dates = [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    conn = get_db()
    gates = conn.execute("SELECT * FROM gates").fetchall()
    suggestions = []

    for gate in gates:
        gate_id = gate["id"]
        gate_name = gate["gate_name"]
        is_main = gate["is_main"]
        peak_capacity = gate["peak_capacity"] if gate["peak_capacity"] else 200

        for date_str in dates:
            overload_preds = conn.execute(
                """SELECT * FROM traffic_predictions
                   WHERE gate_id = ? AND predict_date = ? AND is_overload = 1""",
                (gate_id, date_str),
            ).fetchall()

            if not overload_preds:
                continue

            for pred in overload_preds:
                period = pred["time_period"]
                pred_vol = pred["predicted_volume"]
                overload_ratio = pred["overload_ratio"]
                period_label = "早高峰" if period == "morning_peak" else "晚高峰"

                existing = conn.execute(
                    """SELECT id FROM dispatch_suggestions
                       WHERE gate_id = ? AND suggest_date = ? AND suggestion_type = ? AND status = 'pending'""",
                    (gate_id, date_str, "stagger_open"),
                ).fetchone()
                if not existing:
                    conn.execute(
                        """INSERT INTO dispatch_suggestions
                           (gate_id, suggest_date, time_period, suggestion_type, description, detail, status, before_volume, after_volume)
                           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                        (gate_id, date_str, period, "stagger_open",
                         f"{gate_name} {date_str} {period_label} 预测流量{pred_vol}人，超出容量{int((overload_ratio - 1) * 100)}%，建议分时开门缓解",
                         f"将开门时间提前30分钟，分批放行，预计降至{int(pred_vol * 0.75)}人",
                         pred_vol, int(pred_vol * 0.75)),
                    )
                    suggestions.append({"gate_name": gate_name, "date": date_str,
                                        "type": "stagger_open", "period": period_label})

                existing = conn.execute(
                    """SELECT id FROM dispatch_suggestions
                       WHERE gate_id = ? AND suggest_date = ? AND suggestion_type = ? AND status = 'pending'""",
                    (gate_id, date_str, "delay_close"),
                ).fetchone()
                if not existing:
                    conn.execute(
                        """INSERT INTO dispatch_suggestions
                           (gate_id, suggest_date, time_period, suggestion_type, description, detail, status, before_volume, after_volume)
                           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                        (gate_id, date_str, period, "delay_close",
                         f"{gate_name} {date_str} {period_label} 预测流量{pred_vol}人，建议延后关门30分钟疏散人群",
                         f"延迟关门至原定时间后30分钟，预计疏散流量{int(pred_vol * 0.2)}人",
                         pred_vol, int(pred_vol * 0.8)),
                    )
                    suggestions.append({"gate_name": gate_name, "date": date_str,
                                        "type": "delay_close", "period": period_label})

                if overload_ratio > 1.3:
                    existing = conn.execute(
                        """SELECT id FROM dispatch_suggestions
                           WHERE gate_id = ? AND suggest_date = ? AND suggestion_type = ? AND status = 'pending'""",
                        (gate_id, date_str, "temp_divert"),
                    ).fetchone()
                    if not existing:
                        conn.execute(
                            """INSERT INTO dispatch_suggestions
                               (gate_id, suggest_date, time_period, suggestion_type, description, detail, status, before_volume, after_volume)
                               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                            (gate_id, date_str, period, "temp_divert",
                             f"{gate_name} {date_str} {period_label} 严重超负荷({int(overload_ratio * 100)}%)，建议临时分流至邻近城门",
                             f"引导{int(pred_vol * 0.3)}人分流至邻近城门，本门降至{int(pred_vol * 0.7)}人",
                             pred_vol, int(pred_vol * 0.7)),
                        )
                        suggestions.append({"gate_name": gate_name, "date": date_str,
                                            "type": "temp_divert", "period": period_label})

                if overload_ratio > 1.5 and is_main:
                    existing = conn.execute(
                        """SELECT id FROM dispatch_suggestions
                           WHERE gate_id = ? AND suggest_date = ? AND suggestion_type = ? AND status = 'pending'""",
                        (gate_id, date_str, "gate_switch"),
                    ).fetchone()
                    if not existing:
                        nearby = conn.execute(
                            """SELECT gate_name FROM gates WHERE direction = (SELECT direction FROM gates WHERE id = ?) AND id != ? AND is_main = 0 LIMIT 1""",
                            (gate_id, gate_id),
                        ).fetchone()
                        nearby_name = nearby["gate_name"] if nearby else "副门"
                        conn.execute(
                            """INSERT INTO dispatch_suggestions
                               (gate_id, suggest_date, time_period, suggestion_type, description, detail, status, before_volume, after_volume)
                               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                            (gate_id, date_str, period, "gate_switch",
                             f"{gate_name} {date_str} {period_label} 极度超负荷({int(overload_ratio * 100)}%)，建议切换至{nearby_name}作为主通行门",
                             f"将主通行功能切换至{nearby_name}，分流{int(pred_vol * 0.5)}人，本门降至{int(pred_vol * 0.5)}人",
                             pred_vol, int(pred_vol * 0.5)),
                        )
                        suggestions.append({"gate_name": gate_name, "date": date_str,
                                            "type": "gate_switch", "period": period_label})

    conn.commit()
    conn.close()
    return suggestions


def get_traffic_predictions(start_date: str) -> dict:
    base = datetime.strptime(start_date, "%Y-%m-%d")
    dates = [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    conn = get_db()
    gates = conn.execute("SELECT * FROM gates ORDER BY id").fetchall()

    result = {"dates": dates, "gates": []}
    for gate in gates:
        gate_data = {
            "gate_name": gate["gate_name"],
            "gate_code": gate["gate_code"],
            "is_main": gate["is_main"],
            "peak_capacity": gate["peak_capacity"] if gate["peak_capacity"] else 200,
            "morning_peak": [],
            "evening_peak": [],
        }
        for d in dates:
            for period in ["morning_peak", "evening_peak"]:
                row = conn.execute(
                    """SELECT * FROM traffic_predictions
                       WHERE gate_id = ? AND predict_date = ? AND time_period = ?""",
                    (gate["id"], d, period),
                ).fetchone()
                if row:
                    gate_data[period].append({
                        "date": d,
                        "predicted_volume": row["predicted_volume"],
                        "confidence": row["confidence"],
                        "overload_ratio": row["overload_ratio"],
                        "is_overload": row["is_overload"],
                    })
                else:
                    gate_data[period].append({
                        "date": d,
                        "predicted_volume": 0,
                        "confidence": 0,
                        "overload_ratio": 0,
                        "is_overload": 0,
                    })
        result["gates"].append(gate_data)

    conn.close()
    return result


def get_dispatch_suggestions(start_date: str = None, status: str = None) -> list[dict]:
    conn = get_db()
    query = """SELECT ds.*, g.gate_name, g.gate_code, g.is_main FROM dispatch_suggestions ds
               JOIN gates g ON ds.gate_id = g.id WHERE 1=1"""
    params = []
    if start_date:
        base = datetime.strptime(start_date, "%Y-%m-%d")
        end_date = (base + timedelta(days=6)).strftime("%Y-%m-%d")
        query += " AND ds.suggest_date >= ? AND ds.suggest_date <= ?"
        params.extend([start_date, end_date])
    if status:
        query += " AND ds.status = ?"
        params.append(status)
    query += " ORDER BY ds.suggest_date, ds.gate_id, ds.suggestion_type"
    rows = conn.execute(query, params).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["suggestion_label"] = _SUGGESTION_LABELS.get(d["suggestion_type"], d["suggestion_type"])
        result.append(d)
    conn.close()
    return result


def execute_dispatch_suggestion(suggestion_id: int) -> dict:
    conn = get_db()
    row = conn.execute("SELECT * FROM dispatch_suggestions WHERE id = ?", (suggestion_id,)).fetchone()
    if not row:
        conn.close()
        return {"success": False, "error": "调度建议不存在"}
    if row["status"] != "pending":
        conn.close()
        return {"success": False, "error": f"该建议状态为{row['status']}，无法执行"}

    conn.execute("UPDATE dispatch_suggestions SET status = 'executed' WHERE id = ?", (suggestion_id,))

    gate_id = row["gate_id"]
    suggest_date = row["suggest_date"]
    suggestion_type = row["suggestion_type"]
    detail = row["detail"]

    order_name = f"流量调度-{suggestion_type}-{suggest_date}-gate{gate_id}"
    existing = conn.execute(
        "SELECT id FROM temp_control_orders WHERE order_name = ?", (order_name,),
    ).fetchone()

    if not existing:
        c = conn.cursor()
        if suggestion_type == "stagger_open":
            schedule_row = conn.execute(
                """SELECT open_time, close_time FROM schedules
                   WHERE gate_id = ? AND schedule_date = ? AND scheme_type = 'final'""",
                (gate_id, suggest_date),
            ).fetchone()
            if schedule_row:
                new_open = _sub_minutes(schedule_row["open_time"], 30)
                c.execute(
                    """INSERT INTO temp_control_orders
                       (order_name, start_date, end_date, time_start, time_end,
                        action_type, forced_open_time, forced_close_time, priority, override_reason, is_active)
                       VALUES (?, ?, ?, '00:00', '23:59', 'restrict_hours', ?, ?, 15, ?, 1)""",
                    (order_name, suggest_date, suggest_date,
                     new_open, schedule_row["close_time"],
                     f"流量预测调度·分时开门：{detail}"),
                )
                order_id = c.lastrowid
                conn.execute(
                    "INSERT OR IGNORE INTO temp_control_gates (order_id, gate_id) VALUES (?, ?)",
                    (order_id, gate_id),
                )

        elif suggestion_type == "delay_close":
            schedule_row = conn.execute(
                """SELECT open_time, close_time FROM schedules
                   WHERE gate_id = ? AND schedule_date = ? AND scheme_type = 'final'""",
                (gate_id, suggest_date),
            ).fetchone()
            if schedule_row:
                new_close = _add_minutes(schedule_row["close_time"], 30)
                c.execute(
                    """INSERT INTO temp_control_orders
                       (order_name, start_date, end_date, time_start, time_end,
                        action_type, forced_open_time, forced_close_time, priority, override_reason, is_active)
                       VALUES (?, ?, ?, '00:00', '23:59', 'restrict_hours', ?, ?, 15, ?, 1)""",
                    (order_name, suggest_date, suggest_date,
                     schedule_row["open_time"], new_close,
                     f"流量预测调度·延后关门：{detail}"),
                )
                order_id = c.lastrowid
                conn.execute(
                    "INSERT OR IGNORE INTO temp_control_gates (order_id, gate_id) VALUES (?, ?)",
                    (order_id, gate_id),
                )

        elif suggestion_type in ("temp_divert", "gate_switch"):
            c.execute(
                """INSERT INTO temp_control_orders
                   (order_name, start_date, end_date, time_start, time_end,
                    action_type, forced_open_time, forced_close_time, priority, override_reason, is_active)
                   VALUES (?, ?, ?, '00:00', '23:59', 'restrict_hours', ?, ?, 15, ?, 1)""",
                (order_name, suggest_date, suggest_date,
                 "05:30", "21:00" if suggestion_type == "temp_divert" else "22:00",
                 f"流量预测调度：{detail}"),
            )
            order_id = c.lastrowid
            conn.execute(
                "INSERT OR IGNORE INTO temp_control_gates (order_id, gate_id) VALUES (?, ?)",
                (order_id, gate_id),
            )
            if suggestion_type == "gate_switch":
                alt_gate = conn.execute(
                    """SELECT id FROM gates WHERE direction = (SELECT direction FROM gates WHERE id = ?)
                       AND id != ? AND is_main = 0 LIMIT 1""",
                    (gate_id, gate_id),
                ).fetchone()
                if alt_gate:
                    conn.execute(
                        "INSERT OR IGNORE INTO temp_control_gates (order_id, gate_id) VALUES (?, ?)",
                        (order_id, alt_gate["id"]),
                    )

    conn.commit()
    conn.close()
    recalculate_schedules_for_date_range(suggest_date)
    return {"success": True}


def dismiss_dispatch_suggestion(suggestion_id: int) -> dict:
    conn = get_db()
    row = conn.execute("SELECT * FROM dispatch_suggestions WHERE id = ?", (suggestion_id,)).fetchone()
    if not row:
        conn.close()
        return {"success": False, "error": "调度建议不存在"}
    conn.execute("UPDATE dispatch_suggestions SET status = 'dismissed' WHERE id = ?", (suggestion_id,))
    conn.commit()
    conn.close()
    return {"success": True}


def get_dispatch_comparison(start_date: str) -> dict:
    base = datetime.strptime(start_date, "%Y-%m-%d")
    dates = [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    conn = get_db()
    gates = conn.execute("SELECT * FROM gates ORDER BY id").fetchall()

    result = {"dates": dates, "gates": []}
    for gate in gates:
        gate_data = {
            "gate_name": gate["gate_name"],
            "gate_code": gate["gate_code"],
            "peak_capacity": gate["peak_capacity"] if gate["peak_capacity"] else 200,
            "before": {"morning_peak": [], "evening_peak": []},
            "after": {"morning_peak": [], "evening_peak": []},
        }
        for d in dates:
            for period in ["morning_peak", "evening_peak"]:
                pred = conn.execute(
                    """SELECT predicted_volume FROM traffic_predictions
                       WHERE gate_id = ? AND predict_date = ? AND time_period = ?""",
                    (gate["id"], d, period),
                ).fetchone()
                before_vol = pred["predicted_volume"] if pred else 0

                suggestions = conn.execute(
                    """SELECT after_volume FROM dispatch_suggestions
                       WHERE gate_id = ? AND suggest_date = ? AND time_period = ? AND status = 'executed'""",
                    (gate["id"], d, period),
                ).fetchall()
                after_vol = before_vol
                if suggestions:
                    min_after = min(s["after_volume"] for s in suggestions)
                    after_vol = min_after

                gate_data["before"][period].append(before_vol)
                gate_data["after"][period].append(after_vol)

        result["gates"].append(gate_data)

    conn.close()
    return result


def get_overload_warnings(start_date: str) -> list[dict]:
    base = datetime.strptime(start_date, "%Y-%m-%d")
    dates = [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    conn = get_db()
    rows = conn.execute(
        """SELECT tp.*, g.gate_name, g.gate_code, g.is_main FROM traffic_predictions tp
           JOIN gates g ON tp.gate_id = g.id
           WHERE tp.is_overload = 1 AND tp.predict_date >= ? AND tp.predict_date <= ?
           ORDER BY tp.overload_ratio DESC""",
        (dates[0], dates[-1]),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["period_label"] = "早高峰" if d["time_period"] == "morning_peak" else "晚高峰"
        d["overload_pct"] = int((d["overload_ratio"] - 1) * 100)
        pending = conn.execute(
            """SELECT suggestion_type, description FROM dispatch_suggestions
               WHERE gate_id = ? AND suggest_date = ? AND status = 'pending'""",
            (d["gate_id"], d["predict_date"]),
        ).fetchall()
        d["pending_suggestions"] = [_SUGGESTION_LABELS.get(p["suggestion_type"], p["suggestion_type"]) for p in pending]
        result.append(d)
    conn.close()
    return result
