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


_DEFENSE_LEVEL_MULTIPLIERS = {
    "minimal": {"guard": 0.5, "patrol": 0.5, "light": 0.3, "repair": 0.3, "reserve": 0.2},
    "reduced": {"guard": 0.75, "patrol": 0.75, "light": 0.6, "repair": 0.5, "reserve": 0.5},
    "normal": {"guard": 1.0, "patrol": 1.0, "light": 1.0, "repair": 1.0, "reserve": 1.0},
    "enhanced": {"guard": 1.5, "patrol": 1.5, "light": 1.3, "repair": 1.2, "reserve": 1.5},
    "maximum": {"guard": 2.0, "patrol": 2.0, "light": 1.8, "repair": 1.5, "reserve": 2.0},
}

_ALERT_LEVEL_MULTIPLIERS = {
    1: {"guard": 1.0, "patrol": 1.0, "light": 1.0, "repair": 1.0, "reserve": 1.0},
    2: {"guard": 1.2, "patrol": 1.2, "light": 1.1, "repair": 1.0, "reserve": 1.3},
    3: {"guard": 1.5, "patrol": 1.5, "light": 1.3, "repair": 1.2, "reserve": 1.6},
    4: {"guard": 1.8, "patrol": 1.8, "light": 1.6, "repair": 1.4, "reserve": 2.0},
    5: {"guard": 2.5, "patrol": 2.5, "light": 2.0, "repair": 1.8, "reserve": 3.0},
}

_TIME_PERIOD_MULTIPLIERS = {
    "morning_peak": {"guard": 1.3, "patrol": 1.2, "light": 0.8, "repair": 0.5, "reserve": 1.2},
    "daytime": {"guard": 1.0, "patrol": 1.0, "light": 0.6, "repair": 1.0, "reserve": 1.0},
    "evening_peak": {"guard": 1.4, "patrol": 1.3, "light": 1.0, "repair": 0.5, "reserve": 1.3},
    "night": {"guard": 0.8, "patrol": 1.5, "light": 1.5, "repair": 1.2, "reserve": 1.5},
}

_SHIFT_TIME_RANGES = {
    "morning": ("06:00", "12:00"),
    "midday": ("12:00", "18:00"),
    "evening": ("18:00", "24:00"),
    "night": ("00:00", "06:00"),
}

_PERIOD_LABELS = {
    "morning_peak": "早高峰",
    "daytime": "日间",
    "evening_peak": "晚高峰",
    "night": "夜间",
}

_RESOURCE_TYPE_LABELS = {
    "guard": "守军",
    "patrol": "巡逻",
    "light": "灯火",
    "repair": "检修",
    "reserve": "预备队",
}


def init_resource_pools() -> dict:
    conn = get_db()
    c = conn.cursor()
    default_pools = [
        ("guard", 50, 0, "人", "城门守军总数"),
        ("patrol", 20, 0, "队", "巡逻队伍总数"),
        ("light", 200, 0, "盏", "灯火物资总数"),
        ("repair", 10, 0, "组", "检修班组总数"),
        ("reserve", 30, 0, "人", "应急预备队总数"),
    ]
    for rtype, total, alloc, unit, desc in default_pools:
        existing = c.execute("SELECT id FROM resource_pools WHERE resource_type = ?", (rtype,)).fetchone()
        if not existing:
            c.execute(
                "INSERT INTO resource_pools (resource_type, total_quantity, allocated_quantity, unit, description) VALUES (?,?,?,?,?)",
                (rtype, total, alloc, unit, desc),
            )
    conn.commit()
    conn.close()
    return {"success": True}


def get_resource_pools() -> list[dict]:
    conn = get_db()
    rows = conn.execute("SELECT * FROM resource_pools ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_resource_pool(resource_type: str, total_quantity: int) -> dict:
    conn = get_db()
    try:
        conn.execute(
            "UPDATE resource_pools SET total_quantity = ? WHERE resource_type = ?",
            (total_quantity, resource_type),
        )
        conn.commit()
    except Exception as e:
        conn.close()
        return {"success": False, "error": str(e)}
    conn.close()
    return {"success": True}


def get_resource_config(gate_id: int, config_date: str, time_period: str) -> dict:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM resource_configs WHERE gate_id = ? AND config_date = ? AND time_period = ?",
        (gate_id, config_date, time_period),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_resource_configs_for_date(config_date: str) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        """SELECT rc.*, g.gate_name, g.gate_code, g.defense_level, g.min_guard_required, g.min_patrol_required
           FROM resource_configs rc JOIN gates g ON rc.gate_id = g.id
           WHERE rc.config_date = ? ORDER BY g.id, rc.time_period""",
        (config_date,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_resource_config(gate_id: int, config_date: str, time_period: str,
                        guard_count: int, patrol_shifts: int, patrol_interval: int,
                        light_supplies: int, repair_occupancy: int, reserve_team: int,
                        notes: str = "") -> dict:
    conn = get_db()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO resource_configs
               (gate_id, config_date, time_period, guard_count, patrol_shifts, patrol_interval,
                light_supplies, repair_occupancy, reserve_team, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (gate_id, config_date, time_period, guard_count, patrol_shifts, patrol_interval,
             light_supplies, repair_occupancy, reserve_team, notes),
        )
        conn.commit()
    except Exception as e:
        conn.close()
        return {"success": False, "error": str(e)}
    conn.close()
    return {"success": True}


def delete_resource_config(config_id: int) -> dict:
    conn = get_db()
    conn.execute("DELETE FROM resource_configs WHERE id = ?", (config_id,))
    conn.commit()
    conn.close()
    return {"success": True}


def _get_base_requirements(gate: dict, alert: dict = None) -> dict:
    base_guard = max(gate["min_guard_required"], 4 if gate["is_main"] else 2)
    base_patrol = max(gate["min_patrol_required"], 2 if gate["is_main"] else 1)
    base_light = 10 if gate["is_main"] else 5
    base_repair = 2 if gate["is_main"] else 1
    base_reserve = 6 if gate["is_main"] else 3

    defense_mult = _DEFENSE_LEVEL_MULTIPLIERS.get(gate["defense_level"], _DEFENSE_LEVEL_MULTIPLIERS["normal"])

    alert_mult = _ALERT_LEVEL_MULTIPLIERS[1]
    if alert:
        alert_mult = _ALERT_LEVEL_MULTIPLIERS.get(alert["level_value"], _ALERT_LEVEL_MULTIPLIERS[1])

    return {
        "guard": int(base_guard * defense_mult["guard"] * alert_mult["guard"]),
        "patrol": int(base_patrol * defense_mult["patrol"] * alert_mult["patrol"]),
        "light": int(base_light * defense_mult["light"] * alert_mult["light"]),
        "repair": int(base_repair * defense_mult["repair"] * alert_mult["repair"]),
        "reserve": int(base_reserve * defense_mult["reserve"] * alert_mult["reserve"]),
    }


def calculate_resource_requirements(gate_id: int, date_str: str, time_period: str) -> dict:
    conn = get_db()
    gate = conn.execute("SELECT * FROM gates WHERE id = ?", (gate_id,)).fetchone()
    if not gate:
        conn.close()
        return {"error": "城门不存在"}

    alert = get_alert_for_date(date_str)
    festival = get_festival_for_date(date_str)
    temp_controls = get_active_temp_controls_for_gate(gate_id, date_str)
    traffic_pred = conn.execute(
        "SELECT * FROM traffic_predictions WHERE gate_id = ? AND predict_date = ? AND time_period = ?",
        (gate_id, date_str, time_period),
    ).fetchone()

    base_reqs = _get_base_requirements(dict(gate), alert)
    period_mult = _TIME_PERIOD_MULTIPLIERS.get(time_period, _TIME_PERIOD_MULTIPLIERS["daytime"])

    for key in base_reqs:
        base_reqs[key] = int(base_reqs[key] * period_mult[key])

    if festival and time_period in ["evening_peak", "night"]:
        base_reqs["guard"] = int(base_reqs["guard"] * 1.3)
        base_reqs["light"] = int(base_reqs["light"] * 1.5)
        base_reqs["patrol"] = int(base_reqs["patrol"] * 1.2)

    if traffic_pred and traffic_pred["is_overload"]:
        overload_factor = min(2.0, traffic_pred["overload_ratio"])
        base_reqs["guard"] = int(base_reqs["guard"] * overload_factor)
        base_reqs["patrol"] = int(base_reqs["patrol"] * overload_factor)
        base_reqs["reserve"] = int(base_reqs["reserve"] * overload_factor)

    for tc in temp_controls:
        if tc["action_type"] == "force_close":
            base_reqs["guard"] = int(base_reqs["guard"] * 0.3)
            base_reqs["patrol"] = int(base_reqs["patrol"] * 0.5)
            base_reqs["light"] = 0
            base_reqs["repair"] = 0
        elif tc["action_type"] == "force_open":
            base_reqs["guard"] = int(base_reqs["guard"] * 1.5)
            base_reqs["patrol"] = int(base_reqs["patrol"] * 1.3)
            base_reqs["reserve"] = int(base_reqs["reserve"] * 1.5)

    conn.close()
    return base_reqs


def _check_non_executable_rules(gate_id: int, date_str: str, config: dict, reqs: dict) -> list[str]:
    rules = []

    schedule = generate_schedule_for_gate_date(gate_id, date_str)
    if schedule.get("has_conflict"):
        rules.extend([f"排班冲突: {c}" for c in schedule["conflicts"]])

    if config["guard_count"] < reqs["guard"] * 0.5:
        rules.append(f"守军配置严重不足，仅为需求的{int(config['guard_count']/reqs['guard']*100)}%")

    if config["patrol_shifts"] < 1:
        rules.append("至少需要配置1个巡逻班次")

    if config["guard_count"] < 2:
        rules.append("守军人数不能少于2人")

    if config["repair_occupancy"] > 0 and config["guard_count"] < 3:
        rules.append("有检修占道时守军人数不能少于3人")

    conn = get_db()
    gate = conn.execute("SELECT * FROM gates WHERE id = ?", (gate_id,)).fetchone()
    if gate and config["guard_count"] < gate["min_guard_required"]:
        rules.append(f"守军人数低于城门最低要求{gate['min_guard_required']}人")
    conn.close()

    return rules


def evaluate_defense_resources(gate_id: int, date_str: str, time_period: str) -> dict:
    conn = get_db()
    gate = conn.execute("SELECT * FROM gates WHERE id = ?", (gate_id,)).fetchone()
    if not gate:
        conn.close()
        return {"error": "城门不存在"}
    gate_dict = dict(gate)
    conn.close()

    config = get_resource_config(gate_id, date_str, time_period)
    if not config:
        default_config = {
            "guard_count": gate_dict["min_guard_required"],
            "patrol_shifts": gate_dict["min_patrol_required"],
            "patrol_interval": 60,
            "light_supplies": 10 if gate_dict["is_main"] else 5,
            "repair_occupancy": 0,
            "reserve_team": 3 if gate_dict["is_main"] else 1,
        }
        config = default_config

    reqs = calculate_resource_requirements(gate_id, date_str, time_period)
    if "error" in reqs:
        return reqs

    non_executable = _check_non_executable_rules(gate_id, date_str, config, reqs)

    sufficiencies = {}
    for rtype in ["guard", "patrol", "light", "repair", "reserve"]:
        available = config.get(f"{rtype}_count" if rtype != "light" and rtype != "repair" and rtype != "reserve"
                               else "light_supplies" if rtype == "light"
                               else "repair_occupancy" if rtype == "repair"
                               else "reserve_team", 0)
        if rtype == "patrol":
            available = config.get("patrol_shifts", 0)
        required = reqs[rtype]
        if required > 0:
            sufficiencies[rtype] = min(1.0, available / required)
        else:
            sufficiencies[rtype] = 1.0

    overall_score = int(sum(sufficiencies.values()) / len(sufficiencies) * 100)
    has_gap = any(s < 1.0 for s in sufficiencies.values())
    gaps_count = sum(1 for s in sufficiencies.values() if s < 1.0)

    eval_data = json.dumps({
        "config": config,
        "requirements": reqs,
        "sufficiencies": sufficiencies,
    }, ensure_ascii=False)

    conn = get_db()
    c = conn.cursor()
    c.execute(
        """INSERT OR REPLACE INTO defense_evaluation_results
           (evaluate_date, gate_id, time_period, overall_score,
            guard_sufficiency, patrol_sufficiency, light_sufficiency,
            repair_sufficiency, reserve_sufficiency, has_gap, gaps_count,
            non_executable_rules, evaluation_data)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (date_str, gate_id, time_period, overall_score,
         sufficiencies["guard"], sufficiencies["patrol"], sufficiencies["light"],
         sufficiencies["repair"], sufficiencies["reserve"],
         1 if has_gap else 0, gaps_count,
         "; ".join(non_executable), eval_data),
    )
    conn.commit()
    conn.close()

    return {
        "gate_id": gate_id,
        "gate_name": gate_dict["gate_name"],
        "date": date_str,
        "time_period": time_period,
        "period_label": _PERIOD_LABELS.get(time_period, time_period),
        "config": config,
        "requirements": reqs,
        "sufficiencies": sufficiencies,
        "overall_score": overall_score,
        "has_gap": has_gap,
        "gaps_count": gaps_count,
        "non_executable_rules": non_executable,
        "can_execute": len(non_executable) == 0,
    }


def detect_resource_gaps(gate_id: int, date_str: str) -> list[dict]:
    conn = get_db()
    gate = conn.execute("SELECT * FROM gates WHERE id = ?", (gate_id,)).fetchone()
    if not gate:
        conn.close()
        return []
    gate_name = gate["gate_name"]
    conn.close()

    gaps = []
    periods = ["morning_peak", "daytime", "evening_peak", "night"]
    gap_data = []

    for period in periods:
        evaluation = evaluate_defense_resources(gate_id, date_str, period)
        if "error" in evaluation:
            continue

        config = evaluation["config"]
        reqs = evaluation["requirements"]
        sufficiencies = evaluation["sufficiencies"]

        for rtype in ["guard", "patrol", "light", "repair", "reserve"]:
            available = config.get(f"{rtype}_count" if rtype != "light" and rtype != "repair" and rtype != "reserve"
                                   else "light_supplies" if rtype == "light"
                                   else "repair_occupancy" if rtype == "repair"
                                   else "reserve_team", 0)
            if rtype == "patrol":
                available = config.get("patrol_shifts", 0)
            required = reqs[rtype]
            gap = max(0, required - available)

            if gap > 0:
                suf = sufficiencies[rtype]
                severity = "critical" if suf < 0.5 else "warning" if suf < 0.8 else "info"
                gap_data.append({
                    "period": period,
                    "rtype": rtype,
                    "required": required,
                    "available": available,
                    "gap": gap,
                    "severity": severity,
                    "sufficiency": suf,
                })

    conn = get_db()
    for data in gap_data:
        period = data["period"]
        rtype = data["rtype"]
        required = data["required"]
        available = data["available"]
        gap = data["gap"]
        severity = data["severity"]

        existing = conn.execute(
            """SELECT id FROM resource_gaps
               WHERE gate_id = ? AND gap_date = ? AND time_period = ? AND resource_type = ? AND status = 'open'""",
            (gate_id, date_str, period, rtype),
        ).fetchone()

        if not existing:
            conn.execute(
                """INSERT INTO resource_gaps
                   (gate_id, gap_date, time_period, resource_type,
                    required_quantity, available_quantity, gap_quantity,
                    severity, description, status)
                   VALUES (?,?,?,?,?,?,?,?,?,'open')""",
                (gate_id, date_str, period, rtype,
                 required, available, gap, severity,
                 f"{_RESOURCE_TYPE_LABELS[rtype]}缺口{gap}个单位"),
            )
            gap_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        else:
            gap_id = existing["id"]
            conn.execute(
                """UPDATE resource_gaps
                   SET required_quantity = ?, available_quantity = ?, gap_quantity = ?, severity = ?
                   WHERE id = ?""",
                (required, available, gap, severity, gap_id),
            )

        gaps.append({
                    "id": gap_id,
                    "gate_id": gate_id,
                    "gate_name": gate_name,
                    "date": date_str,
                    "time_period": period,
                    "period_label": _PERIOD_LABELS.get(period, period),
                    "resource_type": rtype,
                    "resource_label": _RESOURCE_TYPE_LABELS[rtype],
                    "required": required,
                    "available": available,
                    "gap": gap,
                    "severity": severity,
                    "status": "open",
                })

    conn.commit()
    conn.close()
    return gaps


def generate_garrison_shifts(gate_id: int, date_str: str) -> list[dict]:
    conn = get_db()
    gate = conn.execute("SELECT * FROM gates WHERE id = ?", (gate_id,)).fetchone()
    if not gate:
        conn.close()
        return []

    schedule = generate_schedule_for_gate_date(gate_id, date_str)
    if "error" in schedule:
        conn.close()
        return []

    open_time = schedule["final"]["open"]
    close_time = schedule["final"]["close"]
    open_min = _time_to_minutes(open_time)
    close_min = _time_to_minutes(close_time)

    shifts = []
    c = conn.cursor()

    for shift_type, (shift_start, shift_end) in _SHIFT_TIME_RANGES.items():
        ss_min = _time_to_minutes(shift_start)
        se_min = _time_to_minutes(shift_end) if shift_end != "24:00" else 1440

        if close_min <= open_min:
            overlap = True
        else:
            overlap = (ss_min < close_min) and (se_min > open_min)

        if not overlap:
            continue

        config = None
        if shift_type == "morning":
            config = get_resource_config(gate_id, date_str, "morning_peak")
        elif shift_type == "midday":
            config = get_resource_config(gate_id, date_str, "daytime")
        elif shift_type == "evening":
            config = get_resource_config(gate_id, date_str, "evening_peak")
        else:
            config = get_resource_config(gate_id, date_str, "night")

        guard_count = config["guard_count"] if config else gate["min_guard_required"]
        patrol_shifts = config["patrol_shifts"] if config else gate["min_patrol_required"]

        effective_start = _minutes_to_time(max(ss_min, open_min))
        effective_end = _minutes_to_time(min(se_min, close_min) if close_min > open_min else min(se_min, 1440))

        c.execute(
            """INSERT OR REPLACE INTO garrison_shifts
               (gate_id, shift_date, shift_type, start_time, end_time,
                guard_count, patrol_route, status)
               VALUES (?,?,?,?,?,?,?,'scheduled')""",
            (gate_id, date_str, shift_type, effective_start, effective_end,
             guard_count, f"{gate['gate_name']}周边巡逻"),
        )

        shift_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        shifts.append({
            "id": shift_id,
            "gate_id": gate_id,
            "gate_name": gate["gate_name"],
            "date": date_str,
            "shift_type": shift_type,
            "start_time": effective_start,
            "end_time": effective_end,
            "guard_count": guard_count,
            "patrol_shifts": patrol_shifts,
            "status": "scheduled",
        })

    conn.commit()
    conn.close()
    return shifts


def get_garrison_shifts(gate_id: int = None, date_str: str = None) -> list[dict]:
    conn = get_db()
    query = """SELECT gs.*, g.gate_name, g.gate_code FROM garrison_shifts gs
               JOIN gates g ON gs.gate_id = g.id WHERE 1=1"""
    params = []
    if gate_id:
        query += " AND gs.gate_id = ?"
        params.append(gate_id)
    if date_str:
        query += " AND gs.shift_date = ?"
        params.append(date_str)
    query += " ORDER BY gs.shift_date, g.id, gs.start_time"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def suggest_gate_downgrade(gate_id: int, date_str: str) -> list[dict]:
    conn = get_db()
    gate = conn.execute("SELECT * FROM gates WHERE id = ?", (gate_id,)).fetchone()
    if not gate:
        conn.close()
        return []

    suggestions = []
    periods = ["morning_peak", "daytime", "evening_peak", "night"]
    levels_order = ["minimal", "reduced", "normal", "enhanced", "maximum"]
    current_idx = levels_order.index(gate["defense_level"]) if gate["defense_level"] in levels_order else 2

    for period in periods:
        gaps = conn.execute(
            """SELECT * FROM resource_gaps
               WHERE gate_id = ? AND gap_date = ? AND time_period = ? AND status = 'open'
               ORDER BY severity DESC""",
            (gate_id, date_str, period),
        ).fetchall()

        if not gaps:
            continue

        critical_count = sum(1 for g in gaps if g["severity"] == "critical")
        if critical_count == 0:
            continue

        if current_idx > 0:
            suggested_level = levels_order[current_idx - 1]
            current_mult = _DEFENSE_LEVEL_MULTIPLIERS[gate["defense_level"]]
            suggested_mult = _DEFENSE_LEVEL_MULTIPLIERS[suggested_level]

            guard_saving = 0
            patrol_saving = 0
            for g in gaps:
                if g["resource_type"] == "guard":
                    guard_saving += int(g["required_quantity"] * (1 - suggested_mult["guard"] / current_mult["guard"]))
                elif g["resource_type"] == "patrol":
                    patrol_saving += int(g["required_quantity"] * (1 - suggested_mult["patrol"] / current_mult["patrol"]))

            existing = conn.execute(
                """SELECT id FROM gate_downgrade_suggestions
                   WHERE gate_id = ? AND suggest_date = ? AND time_period = ? AND status IN ('pending','approved')""",
                (gate_id, date_str, period),
            ).fetchone()

            if not existing:
                conn.execute(
                    """INSERT INTO gate_downgrade_suggestions
                       (gate_id, suggest_date, time_period, current_level, suggested_level,
                        reason, expected_guard_saving, expected_patrol_saving, impact_assessment, status)
                       VALUES (?,?,?,?,?,?,?,?,?,'pending')""",
                    (gate_id, date_str, period, gate["defense_level"], suggested_level,
                     f"{_PERIOD_LABELS[period]}存在{critical_count}项严重资源缺口，建议降级防御等级",
                     guard_saving, patrol_saving,
                     f"降级后可节省{guard_saving}名守军和{patrol_saving}个巡逻班次用于缺口填补，但通行效率可能下降"),
                )
                sugg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            else:
                sugg_id = existing["id"]

            suggestions.append({
                "id": sugg_id,
                "gate_id": gate_id,
                "gate_name": gate["gate_name"],
                "date": date_str,
                "time_period": period,
                "period_label": _PERIOD_LABELS.get(period, period),
                "current_level": gate["defense_level"],
                "suggested_level": suggested_level,
                "guard_saving": guard_saving,
                "patrol_saving": patrol_saving,
                "critical_gaps": critical_count,
            })

    conn.commit()
    conn.close()
    return suggestions


def get_gate_downgrade_suggestions(gate_id: int = None, date_str: str = None, status: str = None) -> list[dict]:
    conn = get_db()
    query = """SELECT gds.*, g.gate_name, g.gate_code FROM gate_downgrade_suggestions gds
               JOIN gates g ON gds.gate_id = g.id WHERE 1=1"""
    params = []
    if gate_id:
        query += " AND gds.gate_id = ?"
        params.append(gate_id)
    if date_str:
        query += " AND gds.suggest_date = ?"
        params.append(date_str)
    if status:
        query += " AND gds.status = ?"
        params.append(status)
    query += " ORDER BY gds.suggest_date, g.id, gds.time_period"
    rows = conn.execute(query, params).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["period_label"] = _PERIOD_LABELS.get(d["time_period"], d["time_period"])
        result.append(d)
    conn.close()
    return result


def generate_cross_gate_allocation(date_str: str) -> list[dict]:
    conn = get_db()
    gates = conn.execute("SELECT * FROM gates ORDER BY id").fetchall()
    if not gates:
        conn.close()
        return []

    allocations = []
    periods = ["morning_peak", "daytime", "evening_peak", "night"]

    for period in periods:
        surplus_gates = []
        deficit_gates = []

        for gate in gates:
            gate_dict = dict(gate)
            config = get_resource_config(gate["id"], date_str, period)
            reqs = calculate_resource_requirements(gate["id"], date_str, period)
            if "error" in reqs:
                continue

            if not config:
                config = {
                    "guard_count": gate_dict["min_guard_required"],
                    "patrol_shifts": gate_dict["min_patrol_required"],
                    "reserve_team": 3 if gate_dict["is_main"] else 1,
                }

            guard_surplus = config["guard_count"] - reqs["guard"]
            patrol_surplus = config["patrol_shifts"] - reqs["patrol"]
            reserve_surplus = config["reserve_team"] - reqs["reserve"]

            if guard_surplus > 0 or patrol_surplus > 0 or reserve_surplus > 0:
                surplus_gates.append({
                    "gate": gate_dict,
                    "guard": guard_surplus,
                    "patrol": patrol_surplus,
                    "reserve": reserve_surplus,
                })

            if guard_surplus < 0 or patrol_surplus < 0 or reserve_surplus < 0:
                deficit_gates.append({
                    "gate": gate_dict,
                    "guard": -guard_surplus if guard_surplus < 0 else 0,
                    "patrol": -patrol_surplus if patrol_surplus < 0 else 0,
                    "reserve": -reserve_surplus if reserve_surplus < 0 else 0,
                })

        for deficit in deficit_gates:
            for rtype in ["guard", "patrol", "reserve"]:
                needed = deficit[rtype]
                if needed <= 0:
                    continue

                for surplus in surplus_gates:
                    available = surplus[rtype]
                    if available <= 0:
                        continue

                    transfer = min(needed, available, 3 if rtype == "guard" else 1)
                    if transfer <= 0:
                        continue

                    time_start = "06:00" if period == "morning_peak" else \
                                "12:00" if period == "daytime" else \
                                "17:00" if period == "evening_peak" else "22:00"
                    time_end = "12:00" if period == "morning_peak" else \
                              "18:00" if period == "daytime" else \
                              "22:00" if period == "evening_peak" else "06:00"

                    existing = conn.execute(
                        """SELECT id FROM cross_gate_allocations
                           WHERE allocation_date = ? AND from_gate_id = ? AND to_gate_id = ?
                           AND resource_type = ? AND status IN ('proposed','approved','in_progress')""",
                        (date_str, surplus["gate"]["id"], deficit["gate"]["id"], rtype),
                    ).fetchone()

                    if not existing:
                        conn.execute(
                            """INSERT INTO cross_gate_allocations
                               (allocation_date, from_gate_id, to_gate_id, resource_type,
                                transfer_quantity, start_time, end_time, reason, status)
                               VALUES (?,?,?,?,?,?,?,?,'proposed')""",
                            (date_str, surplus["gate"]["id"], deficit["gate"]["id"], rtype,
                             transfer, time_start, time_end,
                             f"{_PERIOD_LABELS[period]}{surplus['gate']['gate_name']}可调配{_RESOURCE_TYPE_LABELS[rtype]}至{deficit['gate']['gate_name']}"),
                        )
                        alloc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    else:
                        alloc_id = existing["id"]

                    allocations.append({
                        "id": alloc_id,
                        "date": date_str,
                        "time_period": period,
                        "period_label": _PERIOD_LABELS.get(period, period),
                        "from_gate_id": surplus["gate"]["id"],
                        "from_gate_name": surplus["gate"]["gate_name"],
                        "to_gate_id": deficit["gate"]["id"],
                        "to_gate_name": deficit["gate"]["gate_name"],
                        "resource_type": rtype,
                        "resource_label": _RESOURCE_TYPE_LABELS[rtype],
                        "transfer_quantity": transfer,
                        "start_time": time_start,
                        "end_time": time_end,
                    })

                    surplus[rtype] -= transfer
                    deficit[rtype] -= transfer
                    needed -= transfer
                    if needed <= 0:
                        break

    conn.commit()
    conn.close()
    return allocations


def get_cross_gate_allocations(date_str: str = None, status: str = None) -> list[dict]:
    conn = get_db()
    query = """SELECT cga.*, fg.gate_name as from_gate_name, tg.gate_name as to_gate_name
               FROM cross_gate_allocations cga
               JOIN gates fg ON cga.from_gate_id = fg.id
               JOIN gates tg ON cga.to_gate_id = tg.id WHERE 1=1"""
    params = []
    if date_str:
        query += " AND cga.allocation_date = ?"
        params.append(date_str)
    if status:
        query += " AND cga.status = ?"
        params.append(status)
    query += " ORDER BY cga.allocation_date, cga.start_time"
    rows = conn.execute(query, params).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["resource_label"] = _RESOURCE_TYPE_LABELS.get(d["resource_type"], d["resource_type"])
        result.append(d)
    conn.close()
    return result


def update_allocation_status(allocation_id: int, status: str) -> dict:
    conn = get_db()
    valid_statuses = ['proposed', 'approved', 'in_progress', 'completed', 'cancelled']
    if status not in valid_statuses:
        conn.close()
        return {"success": False, "error": "无效状态"}
    try:
        conn.execute(
            "UPDATE cross_gate_allocations SET status = ? WHERE id = ?",
            (status, allocation_id),
        )
        conn.commit()
    except Exception as e:
        conn.close()
        return {"success": False, "error": str(e)}
    conn.close()
    return {"success": True}


def update_gap_status(gap_id: int, status: str) -> dict:
    conn = get_db()
    valid_statuses = ['open', 'resolved', 'ignored']
    if status not in valid_statuses:
        conn.close()
        return {"success": False, "error": "无效状态"}
    try:
        conn.execute(
            "UPDATE resource_gaps SET status = ? WHERE id = ?",
            (status, gap_id),
        )
        conn.commit()
    except Exception as e:
        conn.close()
        return {"success": False, "error": str(e)}
    conn.close()
    return {"success": True}


def update_downgrade_status(suggestion_id: int, status: str) -> dict:
    conn = get_db()
    valid_statuses = ['pending', 'approved', 'rejected', 'implemented']
    if status not in valid_statuses:
        conn.close()
        return {"success": False, "error": "无效状态"}
    try:
        conn.execute(
            "UPDATE gate_downgrade_suggestions SET status = ? WHERE id = ?",
            (status, suggestion_id),
        )
        conn.commit()
    except Exception as e:
        conn.close()
        return {"success": False, "error": str(e)}
    conn.close()
    return {"success": True}


def get_defense_weekly_trend(start_date: str) -> dict:
    base = datetime.strptime(start_date, "%Y-%m-%d")
    dates = [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    conn = get_db()
    gates = conn.execute("SELECT * FROM gates ORDER BY id").fetchall()
    conn.close()

    trend_data = {
        "dates": dates,
        "gates": [],
        "avg_scores": [],
        "total_gaps": [],
    }

    for gate in gates:
        gate_data = {
            "gate_id": gate["id"],
            "gate_name": gate["gate_name"],
            "gate_code": gate["gate_code"],
            "overall_scores": [],
            "guard_counts": [],
            "patrol_shifts": [],
            "light_supplies": [],
            "gap_counts": [],
        }
        for date_str in dates:
            day_score = 0
            day_guard = 0
            day_patrol = 0
            day_light = 0
            day_gaps = 0
            count = 0

            conn = get_db()
            for period in ["morning_peak", "daytime", "evening_peak", "night"]:
                eval_row = conn.execute(
                    """SELECT * FROM defense_evaluation_results
                       WHERE evaluate_date = ? AND gate_id = ? AND time_period = ?""",
                    (date_str, gate["id"], period),
                ).fetchone()

                if eval_row:
                    day_score += eval_row["overall_score"]
                    day_gaps += eval_row["gaps_count"]
                    count += 1
            conn.close()

            for period in ["morning_peak", "daytime", "evening_peak", "night"]:
                config = get_resource_config(gate["id"], date_str, period)
                if config:
                    day_guard += config["guard_count"]
                    day_patrol += config["patrol_shifts"]
                    day_light += config["light_supplies"]

            gate_data["overall_scores"].append(int(day_score / count) if count > 0 else 0)
            gate_data["guard_counts"].append(day_guard)
            gate_data["patrol_shifts"].append(day_patrol)
            gate_data["light_supplies"].append(day_light)
            gate_data["gap_counts"].append(day_gaps)

        trend_data["gates"].append(gate_data)

    for i in range(7):
        day_scores = [g["overall_scores"][i] for g in trend_data["gates"]]
        day_gaps = [g["gap_counts"][i] for g in trend_data["gates"]]
        trend_data["avg_scores"].append(int(sum(day_scores) / len(day_scores)) if day_scores else 0)
        trend_data["total_gaps"].append(sum(day_gaps))

    return trend_data


def get_allocation_comparison(start_date: str) -> dict:
    base = datetime.strptime(start_date, "%Y-%m-%d")
    dates = [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    conn = get_db()
    gates = conn.execute("SELECT * FROM gates ORDER BY id").fetchall()
    conn.close()

    comparison = {
        "dates": dates,
        "gates": [],
        "before": {"avg_score": 0, "total_gaps": 0},
        "after": {"avg_score": 0, "total_gaps": 0},
    }

    total_before_gaps = 0
    total_after_gaps = 0
    total_before_score = 0
    total_after_score = 0
    count = 0

    for gate in gates:
        gate_data = {
            "gate_id": gate["id"],
            "gate_name": gate["gate_name"],
            "before": {"guard": [], "patrol": [], "gap_count": []},
            "after": {"guard": [], "patrol": [], "gap_count": []},
        }

        for date_str in dates:
            before_guard = 0
            before_patrol = 0
            after_guard = 0
            after_patrol = 0
            before_gaps = 0
            after_gaps = 0
            before_suf = 0
            after_suf = 0
            period_count = 0

            conn = get_db()
            allocations = conn.execute(
                """SELECT * FROM cross_gate_allocations
                   WHERE allocation_date = ? AND (from_gate_id = ? OR to_gate_id = ?)
                   AND status IN ('approved','in_progress','completed')""",
                (date_str, gate["id"], gate["id"]),
            ).fetchall()
            conn.close()

            for period in ["morning_peak", "daytime", "evening_peak", "night"]:
                config = get_resource_config(gate["id"], date_str, period)
                reqs = calculate_resource_requirements(gate["id"], date_str, period)
                if "error" in reqs:
                    continue
                if not config:
                    config = {
                        "guard_count": gate["min_guard_required"],
                        "patrol_shifts": gate["min_patrol_required"],
                    }

                before_guard += config["guard_count"]
                before_patrol += config["patrol_shifts"]
                before_gaps += max(0, reqs["guard"] - config["guard_count"])
                before_gaps += max(0, reqs["patrol"] - config["patrol_shifts"])

                adjusted_guard = config["guard_count"]
                adjusted_patrol = config["patrol_shifts"]

                for alloc in allocations:
                    if alloc["time_period"] == period:
                        if alloc["to_gate_id"] == gate["id"]:
                            if alloc["resource_type"] == "guard":
                                adjusted_guard += alloc["transfer_quantity"]
                            elif alloc["resource_type"] == "patrol":
                                adjusted_patrol += alloc["transfer_quantity"]
                        elif alloc["from_gate_id"] == gate["id"]:
                            if alloc["resource_type"] == "guard":
                                adjusted_guard -= alloc["transfer_quantity"]
                            elif alloc["resource_type"] == "patrol":
                                adjusted_patrol -= alloc["transfer_quantity"]

                after_guard += adjusted_guard
                after_patrol += adjusted_patrol
                after_gaps += max(0, reqs["guard"] - adjusted_guard)
                after_gaps += max(0, reqs["patrol"] - adjusted_patrol)

                guard_suf_before = min(1.0, config["guard_count"] / reqs["guard"]) if reqs["guard"] > 0 else 1.0
                patrol_suf_before = min(1.0, config["patrol_shifts"] / reqs["patrol"]) if reqs["patrol"] > 0 else 1.0
                before_suf += (guard_suf_before + patrol_suf_before) / 2

                guard_suf_after = min(1.0, adjusted_guard / reqs["guard"]) if reqs["guard"] > 0 else 1.0
                patrol_suf_after = min(1.0, adjusted_patrol / reqs["patrol"]) if reqs["patrol"] > 0 else 1.0
                after_suf += (guard_suf_after + patrol_suf_after) / 2
                period_count += 1

            gate_data["before"]["guard"].append(before_guard)
            gate_data["before"]["patrol"].append(before_patrol)
            gate_data["before"]["gap_count"].append(before_gaps)
            gate_data["after"]["guard"].append(after_guard)
            gate_data["after"]["patrol"].append(after_patrol)
            gate_data["after"]["gap_count"].append(after_gaps)

            total_before_gaps += before_gaps
            total_after_gaps += after_gaps
            if period_count > 0:
                total_before_score += int(before_suf / period_count * 100)
                total_after_score += int(after_suf / period_count * 100)
                count += 1

        comparison["gates"].append(gate_data)

    comparison["before"]["total_gaps"] = total_before_gaps
    comparison["after"]["total_gaps"] = total_after_gaps
    comparison["before"]["avg_score"] = int(total_before_score / count) if count > 0 else 0
    comparison["after"]["avg_score"] = int(total_after_score / count) if count > 0 else 0

    return comparison


def get_resource_gaps(gate_id: int = None, date_str: str = None, status: str = None) -> list[dict]:
    conn = get_db()
    query = """SELECT rg.*, g.gate_name, g.gate_code FROM resource_gaps rg
               JOIN gates g ON rg.gate_id = g.id WHERE 1=1"""
    params = []
    if gate_id:
        query += " AND rg.gate_id = ?"
        params.append(gate_id)
    if date_str:
        query += " AND rg.gap_date = ?"
        params.append(date_str)
    if status:
        query += " AND rg.status = ?"
        params.append(status)
    query += " ORDER BY rg.gap_date, rg.severity DESC, g.id"
    rows = conn.execute(query, params).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["period_label"] = _PERIOD_LABELS.get(d["time_period"], d["time_period"])
        d["resource_label"] = _RESOURCE_TYPE_LABELS.get(d["resource_type"], d["resource_type"])
        d["date"] = d["gap_date"]
        d["required"] = d["required_quantity"]
        d["available"] = d["available_quantity"]
        d["gap"] = d["gap_quantity"]
        result.append(d)
    conn.close()
    return result


def full_defense_evaluation(start_date: str) -> dict:
    base = datetime.strptime(start_date, "%Y-%m-%d")
    dates = [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    conn = get_db()
    gates = conn.execute("SELECT * FROM gates ORDER BY id").fetchall()
    conn.close()

    init_resource_pools()

    all_evaluations = []
    all_gaps = []
    all_shifts = []
    all_downgrades = []
    all_allocations = []

    for date_str in dates:
        for gate in gates:
            for period in ["morning_peak", "daytime", "evening_peak", "night"]:
                eval_result = evaluate_defense_resources(gate["id"], date_str, period)
                if "error" not in eval_result:
                    all_evaluations.append(eval_result)

            gaps = detect_resource_gaps(gate["id"], date_str)
            all_gaps.extend(gaps)

            shifts = generate_garrison_shifts(gate["id"], date_str)
            all_shifts.extend(shifts)

            downgrades = suggest_gate_downgrade(gate["id"], date_str)
            all_downgrades.extend(downgrades)

        allocations = generate_cross_gate_allocation(date_str)
        all_allocations.extend(allocations)

    critical_gaps = [g for g in all_gaps if g["severity"] == "critical"]
    open_gaps = [g for g in all_gaps if g["status"] == "open"]

    return {
        "dates": dates,
        "evaluations": all_evaluations,
        "gaps": all_gaps,
        "shifts": all_shifts,
        "downgrades": all_downgrades,
        "allocations": all_allocations,
        "summary": {
            "total_gates": len(gates),
            "total_days": len(dates),
            "total_periods": len(gates) * len(dates) * 4,
            "total_gaps": len(all_gaps),
            "open_gaps": len(open_gaps),
            "critical_gaps": len(critical_gaps),
            "total_shifts": len(all_shifts),
            "total_downgrades": len(all_downgrades),
            "total_allocations": len(all_allocations),
            "avg_score": int(sum(e["overall_score"] for e in all_evaluations) / len(all_evaluations)) if all_evaluations else 0,
        },
    }


_EVENT_TYPE_LABELS = {
    "vendor_gathering": "商贩聚集",
    "people_petition": "百姓请愿",
    "patrol_anomaly": "夜巡异常",
    "road_blockage": "道路阻塞",
    "fire_rumor": "失火传闻",
    "other": "其他事件",
}

_EVENT_LEVEL_LABELS = {
    "minor": "一般",
    "moderate": "较重",
    "serious": "严重",
    "critical": "重大",
}

_TIME_PERIOD_LABELS = {
    "early_morning": "凌晨",
    "morning_peak": "早高峰",
    "daytime": "日间",
    "evening_peak": "晚高峰",
    "night": "夜间",
    "late_night": "深夜",
}

_STATUS_LABELS = {
    "reported": "已上报",
    "responding": "响应中",
    "handling": "处置中",
    "resolved": "已解决",
    "closed": "已结案",
}


def _generate_event_code() -> str:
    now = datetime.now()
    conn = get_db()
    count = conn.execute(
        "SELECT COUNT(*) FROM public_opinion_events WHERE date(created_at) = date('now')"
    ).fetchone()[0]
    conn.close()
    return f"MQ{now.strftime('%Y%m%d')}{count + 1:04d}"


def get_all_opinion_events(gate_id: int = None, start_date: str = None, end_date: str = None,
                           status: str = None, event_type: str = None) -> list[dict]:
    conn = get_db()
    query = """SELECT poe.*, g.gate_name, g.gate_code 
               FROM public_opinion_events poe 
               JOIN gates g ON poe.gate_id = g.id 
               WHERE 1=1"""
    params = []
    if gate_id:
        query += " AND poe.gate_id = ?"
        params.append(gate_id)
    if start_date:
        query += " AND poe.event_date >= ?"
        params.append(start_date)
    if end_date:
        query += " AND poe.event_date <= ?"
        params.append(end_date)
    if status:
        query += " AND poe.status = ?"
        params.append(status)
    if event_type:
        query += " AND poe.event_type = ?"
        params.append(event_type)
    query += " ORDER BY poe.event_date DESC, poe.created_at DESC"
    rows = conn.execute(query, params).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["event_type_label"] = _EVENT_TYPE_LABELS.get(d["event_type"], d["event_type"])
        d["event_level_label"] = _EVENT_LEVEL_LABELS.get(d["event_level"], d["event_level"])
        d["time_period_label"] = _TIME_PERIOD_LABELS.get(d["time_period"], d["time_period"])
        d["status_label"] = _STATUS_LABELS.get(d["status"], d["status"])
        result.append(d)
    conn.close()
    return result


def get_opinion_event(event_id: int) -> Optional[dict]:
    conn = get_db()
    row = conn.execute(
        """SELECT poe.*, g.gate_name, g.gate_code 
           FROM public_opinion_events poe 
           JOIN gates g ON poe.gate_id = g.id 
           WHERE poe.id = ?""",
        (event_id,),
    ).fetchone()
    if not row:
        conn.close()
        return None
    d = dict(row)
    d["event_type_label"] = _EVENT_TYPE_LABELS.get(d["event_type"], d["event_type"])
    d["event_level_label"] = _EVENT_LEVEL_LABELS.get(d["event_level"], d["event_level"])
    d["time_period_label"] = _TIME_PERIOD_LABELS.get(d["time_period"], d["time_period"])
    d["status_label"] = _STATUS_LABELS.get(d["status"], d["status"])
    conn.close()
    return d


def create_opinion_event(gate_id: int, event_date: str, time_period: str, event_type: str,
                         event_level: str, credibility: int, handle_deadline: str,
                         title: str, description: str = "", reporter: str = "") -> dict:
    conn = get_db()
    try:
        gate = conn.execute("SELECT * FROM gates WHERE id = ?", (gate_id,)).fetchone()
        if not gate:
            return {"success": False, "error": "城门不存在"}
        event_code = _generate_event_code()
        cursor = conn.execute(
            """INSERT INTO public_opinion_events 
               (event_code, gate_id, event_date, time_period, event_type, event_level, 
                credibility, handle_deadline, title, description, reporter, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?, 'reported')""",
            (event_code, gate_id, event_date, time_period, event_type, event_level,
             credibility, handle_deadline, title, description, reporter),
        )
        event_id = cursor.lastrowid
        conn.execute(
            """INSERT INTO public_opinion_progress 
               (event_id, progress_type, title, content, operator)
               VALUES (?, 'report', '事件上报', ?, ?)""",
            (event_id, description or title, reporter or "系统"),
        )
        conn.commit()
        _generate_auto_responses(event_id)
        return {"success": True, "event_id": event_id, "event_code": event_code}
    except Exception as e:
        conn.rollback()
        return {"success": False, "error": str(e)}
    finally:
        conn.close()


def update_opinion_event_status(event_id: int, status: str, operator: str = "", note: str = "") -> dict:
    conn = get_db()
    try:
        event = conn.execute("SELECT * FROM public_opinion_events WHERE id = ?", (event_id,)).fetchone()
        if not event:
            return {"success": False, "error": "事件不存在"}
        conn.execute(
            "UPDATE public_opinion_events SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status, event_id),
        )
        progress_type_map = {
            "responding": "assign",
            "handling": "handle",
            "resolved": "resolve",
            "closed": "close",
        }
        progress_type = progress_type_map.get(status, "update")
        status_label = _STATUS_LABELS.get(status, status)
        conn.execute(
            """INSERT INTO public_opinion_progress 
               (event_id, progress_type, title, content, operator)
               VALUES (?,?,?,?,?)""",
            (event_id, progress_type, f"状态变更为{status_label}", note or f"状态变更为{status_label}", operator or "系统"),
        )
        conn.commit()
        return {"success": True}
    except Exception as e:
        conn.rollback()
        return {"success": False, "error": str(e)}
    finally:
        conn.close()


def add_event_progress(event_id: int, progress_type: str, title: str,
                       content: str = "", operator: str = "") -> dict:
    conn = get_db()
    try:
        event = conn.execute("SELECT * FROM public_opinion_events WHERE id = ?", (event_id,)).fetchone()
        if not event:
            return {"success": False, "error": "事件不存在"}
        conn.execute(
            """INSERT INTO public_opinion_progress 
               (event_id, progress_type, title, content, operator)
               VALUES (?,?,?,?,?)""",
            (event_id, progress_type, title, content, operator),
        )
        conn.execute(
            "UPDATE public_opinion_events SET updated_at = datetime('now') WHERE id = ?",
            (event_id,),
        )
        conn.commit()
        return {"success": True}
    except Exception as e:
        conn.rollback()
        return {"success": False, "error": str(e)}
    finally:
        conn.close()


def get_event_progress(event_id: int) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM public_opinion_progress WHERE event_id = ? ORDER BY created_at ASC",
        (event_id,),
    ).fetchall()
    result = [dict(r) for r in rows]
    conn.close()
    return result


def _generate_auto_responses(event_id: int):
    conn = get_db()
    try:
        event = conn.execute(
            """SELECT poe.*, g.gate_name, g.gate_code 
               FROM public_opinion_events poe 
               JOIN gates g ON poe.gate_id = g.id 
               WHERE poe.id = ?""",
            (event_id,),
        ).fetchone()
        if not event:
            return
        event = dict(event)

        temp_resp = _generate_temp_response(event)
        conn.execute(
            """INSERT INTO public_opinion_responses 
               (event_id, response_type, title, content, priority, status)
               VALUES (?, 'temp_response', ?, ?, ?, 'proposed')""",
            (event_id, temp_resp["title"], temp_resp["content"], temp_resp["priority"]),
        )

        gate_notice = _generate_gate_notice(event)
        conn.execute(
            """INSERT INTO public_opinion_responses 
               (event_id, response_type, title, content, priority, status)
               VALUES (?, 'gate_notice', ?, ?, ?, 'proposed')""",
            (event_id, gate_notice["title"], gate_notice["content"], gate_notice["priority"]),
        )

        resource_support = _generate_resource_support(event)
        conn.execute(
            """INSERT INTO public_opinion_responses 
               (event_id, response_type, title, content, priority, status)
               VALUES (?, 'resource_support', ?, ?, ?, 'proposed')""",
            (event_id, resource_support["title"], resource_support["content"], resource_support["priority"]),
        )

        schedule_adjust = _generate_schedule_adjust(event)
        conn.execute(
            """INSERT INTO public_opinion_responses 
               (event_id, response_type, title, content, priority, status)
               VALUES (?, 'schedule_adjust', ?, ?, ?, 'proposed')""",
            (event_id, schedule_adjust["title"], schedule_adjust["content"], schedule_adjust["priority"]),
        )

        _create_gate_notice_from_event(event, gate_notice)

        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"生成自动响应失败: {e}")
    finally:
        conn.close()


def _generate_temp_response(event: dict) -> dict:
    event_type = event["event_type"]
    event_level = event["event_level"]
    gate_name = event["gate_name"]
    time_period = _TIME_PERIOD_LABELS.get(event["time_period"], event["time_period"])

    level_weight = {"minor": 1, "moderate": 2, "serious": 3, "critical": 4}
    weight = level_weight.get(event_level, 1)

    responses = {
        "vendor_gathering": {
            "title": "商贩聚集临时响应建议",
            "content": f"""
【响应等级】：{_EVENT_LEVEL_LABELS.get(event_level, '一般')}
【涉及城门】：{gate_name}
【事发时段】：{time_period}
【建议措施】：
1. 增派 {weight * 2} 名城防队员前往现场维持秩序
2. 安排 {weight} 名巡检人员疏导商贩至指定区域
3. 开启城门侧通道，避免主通道拥堵
4. 设置临时警示标识，引导行人绕行
5. 每 {max(30 // weight, 15)} 分钟上报一次现场情况
【预计影响】：城门通行效率下降约 {weight * 10}%，建议错峰出行
【处置时限】：{event['handle_deadline']}
            """.strip(),
            "priority": weight * 2,
        },
        "people_petition": {
            "title": "百姓请愿临时响应建议",
            "content": f"""
【响应等级】：{_EVENT_LEVEL_LABELS.get(event_level, '一般')}
【涉及城门】：{gate_name}
【事发时段】：{time_period}
【建议措施】：
1. 立即启动 {weight + 1} 级响应机制，增派 {weight * 3} 名守卫
2. 安排主事官员前往现场接访，安抚民众情绪
3. 临时关闭城门侧门，保留主通道供人员通行
4. 设置隔离带，防止人群冲击城门
5. 通知周边城门做好联动准备
【预计影响】：城门安全等级提升，通行速度放缓
【处置时限】：{event['handle_deadline']}
            """.strip(),
            "priority": weight * 3,
        },
        "patrol_anomaly": {
            "title": "夜巡异常临时响应建议",
            "content": f"""
【响应等级】：{_EVENT_LEVEL_LABELS.get(event_level, '一般')}
【涉及城门】：{gate_name}
【事发时段】：{time_period}
【建议措施】：
1. 增派 {weight * 2} 组夜巡小队前往异常区域
2. 加强城门附近照明，开启全部探照灯
3. 调阅周边监控录像，排查异常原因
4. 通知相邻城门提高警惕，加强戒备
5. 每 {max(20 // weight, 10)} 分钟汇报一次巡查进展
【预计影响】：夜巡密度增加，城门警戒等级临时提升
【处置时限】：{event['handle_deadline']}
            """.strip(),
            "priority": weight * 2,
        },
        "road_blockage": {
            "title": "道路阻塞临时响应建议",
            "content": f"""
【响应等级】：{_EVENT_LEVEL_LABELS.get(event_level, '一般')}
【涉及城门】：{gate_name}
【事发时段】：{time_period}
【建议措施】：
1. 立即派遣 {weight * 2} 名交通疏导员前往现场
2. 临时开放备用通道，分流过往行人车辆
3. 在城门内外设置分流指示牌
4. 协调相关部门清理障碍物
5. 预计 {weight} 小时内恢复正常通行
【预计影响】：城门通行能力下降 {weight * 20}%，请绕行其他城门
【处置时限】：{event['handle_deadline']}
            """.strip(),
            "priority": weight * 2,
        },
        "fire_rumor": {
            "title": "失火传闻临时响应建议",
            "content": f"""
【响应等级】：{_EVENT_LEVEL_LABELS.get(event_level, '一般')}
【涉及城门】：{gate_name}
【事发时段】：{time_period}
【建议措施】：
1. 立即派遣 {weight} 组火政人员前往核实火情
2. 通知城门守卫做好疏散准备
3. 预备灭火器材，检查消防设施
4. 如火情属实，立即开启全部城门通道便于疏散
5. 通过城防广播及时通报情况，避免恐慌
【预计影响】：可能造成短时恐慌，需做好舆情引导
【处置时限】：{event['handle_deadline']}
            """.strip(),
            "priority": weight * 3,
        },
        "other": {
            "title": "其他民情事件临时响应建议",
            "content": f"""
【响应等级】：{_EVENT_LEVEL_LABELS.get(event_level, '一般')}
【涉及城门】：{gate_name}
【事发时段】：{time_period}
【建议措施】：
1. 派遣 {weight} 名城防队员前往现场了解情况
2. 及时上报事件进展，保持通讯畅通
3. 根据现场情况调整响应策略
4. 做好记录，为后续处置提供依据
【处置时限】：{event['handle_deadline']}
            """.strip(),
            "priority": weight,
        },
    }
    return responses.get(event_type, responses["other"])


def _generate_gate_notice(event: dict) -> dict:
    event_type = event["event_type"]
    event_level = event["event_level"]
    gate_name = event["gate_name"]
    time_period = _TIME_PERIOD_LABELS.get(event["time_period"], event["time_period"])

    notices = {
        "vendor_gathering": {
            "title": f"{gate_name}商贩聚集告示",
            "content": f"【{gate_name}告示】因{time_period}时段城门外商贩聚集，往来行人请留意脚下，注意安全。城防人员正在现场疏导秩序，请配合管理，有序通行。",
            "priority": 3,
        },
        "people_petition": {
            "title": f"{gate_name}通行提示",
            "content": f"【{gate_name}告示】城门附近有民众聚集陈情，城防部门正在妥善处理。请过往行人配合指引，绕行侧门通行，勿在城门附近逗留。感谢配合。",
            "priority": 5,
        },
        "patrol_anomaly": {
            "title": f"{gate_name}安全提示",
            "content": f"【{gate_name}告示】近日{time_period}时段巡查发现异常情况，城防部门已加强巡逻。请市民提高警惕，如发现可疑人员或情况，及时向城门守卫报告。",
            "priority": 4,
        },
        "road_blockage": {
            "title": f"{gate_name}道路通行提示",
            "content": f"【{gate_name}告示】因前方道路临时阻塞，{time_period}时段城门通行可能受阻。建议市民错峰出行或绕行其他城门。不便之处，敬请谅解。",
            "priority": 3,
        },
        "fire_rumor": {
            "title": f"{gate_name}安全告示",
            "content": f"【{gate_name}告示】近日有失火传闻，城防部门正在核实。请市民勿信谣传谣，关注官方通报。如遇火情，请保持冷静，听从城防人员指挥疏散。",
            "priority": 5,
        },
        "other": {
            "title": f"{gate_name}临时通知",
            "content": f"【{gate_name}告示】近日城门周边情况特殊，请过往行人注意安全，配合城防人员管理。如有疑问，可向城门守卫咨询。",
            "priority": 2,
        },
    }
    return notices.get(event_type, notices["other"])


def _generate_resource_support(event: dict) -> dict:
    event_type = event["event_type"]
    event_level = event["event_level"]
    gate_name = event["gate_name"]
    gate_id = event["gate_id"]
    event_date = event["event_date"]
    time_period = event["time_period"]

    level_map = {"minor": 1, "moderate": 2, "serious": 3, "critical": 4}
    level = level_map.get(event_level, 1)

    resource_req = calculate_resource_requirements(gate_id, event_date, "daytime") or {}
    current_config = get_resource_config(gate_id, event_date, "daytime") or {}

    extra_guards = level * 2
    extra_patrol = level
    extra_reserve = level if level >= 2 else 0

    pools = get_resource_pools() or []
    pool_info = {}
    for p in pools:
        pool_info[p["resource_type"]] = {
            "total": p["total_quantity"],
            "allocated": p["allocated_quantity"],
            "available": p["total_quantity"] - p["allocated_quantity"],
        }

    guard_avail = pool_info.get("guard", {}).get("available", 0)
    patrol_avail = pool_info.get("patrol", {}).get("available", 0)
    reserve_avail = pool_info.get("reserve", {}).get("available", 0)

    cur_guard = current_config.get('guard_count', 0) if current_config else 0
    cur_patrol = current_config.get('patrol_shifts', 0) if current_config else 0

    content = f"""
【增援城门】：{gate_name}
【事件等级】：{_EVENT_LEVEL_LABELS.get(event_level, '一般')}
【事发时段】：{_TIME_PERIOD_LABELS.get(time_period, time_period)}

【建议增援配置】：
- 守卫人员：+{extra_guards} 人（建议从预备队抽调）
- 巡逻班次：+{extra_patrol} 组（加强周边巡查）
- 预备队：+{extra_reserve} 组（待命支援）

【现有资源】：
- 当前守卫配置：{cur_guard} 人
- 当前巡逻配置：{cur_patrol} 组

【资源池状态】：
- 守卫可用：{guard_avail} 人 {"✅ 充足" if guard_avail >= extra_guards else "⚠️ 不足"}
- 巡逻可用：{patrol_avail} 组 {"✅ 充足" if patrol_avail >= extra_patrol else "⚠️ 不足"}
- 预备队可用：{reserve_avail} 组 {"✅ 充足" if reserve_avail >= extra_reserve else "⚠️ 不足"}

【建议调派来源】：
1. 从资源池预备队优先调派
2. 如资源不足，可从邻近低负荷城门临时调配
3. 重大事件建议启动跨城门支援机制
    """.strip()

    return {
        "title": f"{gate_name}资源增援建议",
        "content": content,
        "priority": level * 2,
    }


def _generate_schedule_adjust(event: dict) -> dict:
    event_type = event["event_type"]
    event_level = event["event_level"]
    gate_name = event["gate_name"]
    gate_id = event["gate_id"]
    event_date = event["event_date"]
    time_period = event["time_period"]

    level_map = {"minor": 1, "moderate": 2, "serious": 3, "critical": 4}
    level = level_map.get(event_level, 1)

    conn = get_db()
    schedule = conn.execute(
        """SELECT * FROM schedules WHERE gate_id = ? AND schedule_date = ? AND scheme_type = 'final'""",
        (gate_id, event_date),
    ).fetchone()
    daily_alert = get_alert_for_date(event_date)
    temp_controls = get_active_temp_controls_for_gate(gate_id, event_date)

    adjustments = []
    impact_level = "低"

    if level >= 1:
        adjustments.append(f"建议在{_TIME_PERIOD_LABELS.get(time_period, time_period)}时段加强城门值守力量")
    if level >= 2:
        adjustments.append("建议临时增开一条检查通道，加快通行速度")
        impact_level = "中"
    if level >= 3:
        adjustments.append("建议启动联动机制，通知相邻城门做好分流准备")
        impact_level = "高"
    if level >= 4:
        adjustments.append("建议提升警戒等级，考虑提前关闭或延迟开启城门")
        impact_level = "严重"

    open_time = schedule["open_time"] if schedule else "未知"
    close_time = schedule["close_time"] if schedule else "未知"

    content = f"""
【涉及城门】：{gate_name}
【事件等级】：{_EVENT_LEVEL_LABELS.get(event_level, '一般')}
【影响日期】：{event_date}
【影响时段】：{_TIME_PERIOD_LABELS.get(time_period, time_period)}
【影响程度】：{impact_level}

【当日排班】：
- 计划开门：{open_time}
- 计划关门：{close_time}
- 当前警戒：{daily_alert['level_name'] if daily_alert else '和平'}级

【排班调整建议】：
{chr(10).join(f'{i+1}. {a}' for i, a in enumerate(adjustments))}

【临时管制影响】：
- 当前生效管制令：{len(temp_controls)} 条
- 管制优先级：{max([tc['priority'] for tc in temp_controls], default='无')}

【注意事项】：
1. 调整排班需综合考虑当日时令、节庆、警戒等因素
2. 如调整关门时间，需确保不与宵禁规则冲突
3. 重大调整建议上报审批后执行
    """.strip()

    conn.close()

    return {
        "title": f"{gate_name}排班调整影响分析",
        "content": content,
        "priority": level * 2,
    }


def _create_gate_notice_from_event(event: dict, notice_info: dict):
    conn = get_db()
    try:
        event_date = event["event_date"]
        start_time = f"{event_date} 00:00:00"
        end_time = f"{event_date} 23:59:59"

        conn.execute(
            """INSERT INTO public_opinion_gate_notices 
               (event_id, gate_id, notice_title, notice_content, display_position, 
                start_time, end_time, status)
               VALUES (?,?,?,?,?, ?, ?, 'draft')""",
            (event["id"], event["gate_id"], notice_info["title"], notice_info["content"],
             "gate_top", start_time, end_time),
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"创建城门通告失败: {e}")
    finally:
        conn.close()


def get_event_responses(event_id: int) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM public_opinion_responses WHERE event_id = ? ORDER BY priority DESC, created_at ASC",
        (event_id,),
    ).fetchall()
    result = []
    type_labels = {
        "temp_response": "临时响应建议",
        "gate_notice": "城门通告方案",
        "resource_support": "资源增援建议",
        "schedule_adjust": "排班调整影响",
    }
    for row in rows:
        d = dict(row)
        d["response_type_label"] = type_labels.get(d["response_type"], d["response_type"])
        result.append(d)
    conn.close()
    return result


def update_response_status(response_id: int, status: str) -> dict:
    conn = get_db()
    try:
        conn.execute(
            "UPDATE public_opinion_responses SET status = ? WHERE id = ?",
            (status, response_id),
        )
        conn.commit()
        return {"success": True}
    except Exception as e:
        conn.rollback()
        return {"success": False, "error": str(e)}
    finally:
        conn.close()


def get_gate_notices(gate_id: int = None, event_id: int = None, status: str = None) -> list[dict]:
    conn = get_db()
    query = """SELECT ogn.*, g.gate_name, g.gate_code, poe.title as event_title 
               FROM public_opinion_gate_notices ogn 
               JOIN gates g ON ogn.gate_id = g.id 
               LEFT JOIN public_opinion_events poe ON ogn.event_id = poe.id 
               WHERE 1=1"""
    params = []
    if gate_id:
        query += " AND ogn.gate_id = ?"
        params.append(gate_id)
    if event_id:
        query += " AND ogn.event_id = ?"
        params.append(event_id)
    if status:
        query += " AND ogn.status = ?"
        params.append(status)
    query += " ORDER BY ogn.created_at DESC"
    rows = conn.execute(query, params).fetchall()
    result = [dict(r) for r in rows]
    conn.close()
    return result


def publish_gate_notice(notice_id: int) -> dict:
    conn = get_db()
    try:
        conn.execute(
            "UPDATE public_opinion_gate_notices SET status = 'published' WHERE id = ?",
            (notice_id,),
        )
        conn.commit()
        return {"success": True}
    except Exception as e:
        conn.rollback()
        return {"success": False, "error": str(e)}
    finally:
        conn.close()


def get_notice_templates(event_type: str = None, event_level: str = None) -> list[dict]:
    conn = get_db()
    query = "SELECT * FROM public_opinion_notice_templates WHERE is_active = 1"
    params = []
    if event_type:
        query += " AND event_type = ?"
        params.append(event_type)
    if event_level:
        query += " AND event_level = ?"
        params.append(event_level)
    query += " ORDER BY event_type, event_level"
    rows = conn.execute(query, params).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["event_type_label"] = _EVENT_TYPE_LABELS.get(d["event_type"], d["event_type"])
        d["event_level_label"] = _EVENT_LEVEL_LABELS.get(d["event_level"], d["event_level"])
        result.append(d)
    conn.close()
    return result


def create_notice_template(template_name: str, event_type: str, event_level: str,
                           template_content: str, announce_position: str = "gate_top") -> dict:
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO public_opinion_notice_templates 
               (template_name, event_type, event_level, template_content, announce_position, is_active)
               VALUES (?,?,?,?,?, 1)""",
            (template_name, event_type, event_level, template_content, announce_position),
        )
        conn.commit()
        return {"success": True}
    except Exception as e:
        conn.rollback()
        return {"success": False, "error": str(e)}
    finally:
        conn.close()


def get_opinion_weekly_trend(start_date: str) -> dict:
    conn = get_db()
    dates = []
    for i in range(7):
        dt = datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=i)
        dates.append(dt.strftime("%Y-%m-%d"))

    daily_stats = []
    type_stats = {k: 0 for k in _EVENT_TYPE_LABELS.keys()}
    level_stats = {k: 0 for k in _EVENT_LEVEL_LABELS.keys()}
    status_stats = {k: 0 for k in _STATUS_LABELS.keys()}
    total_events = 0
    unresolved = 0

    for date_str in dates:
        rows = conn.execute(
            "SELECT * FROM public_opinion_events WHERE event_date = ?",
            (date_str,),
        ).fetchall()
        day_count = len(rows)
        total_events += day_count
        day_resolved = sum(1 for r in rows if r["status"] in ("resolved", "closed"))
        day_unresolved = day_count - day_resolved
        unresolved += day_unresolved

        daily_stats.append({
            "date": date_str,
            "total": day_count,
            "resolved": day_resolved,
            "unresolved": day_unresolved,
        })

        for r in rows:
            type_stats[r["event_type"]] = type_stats.get(r["event_type"], 0) + 1
            level_stats[r["event_level"]] = level_stats.get(r["event_level"], 0) + 1
            status_stats[r["status"]] = status_stats.get(r["status"], 0) + 1

    conn.close()

    return {
        "start_date": start_date,
        "daily_stats": daily_stats,
        "total_events": total_events,
        "unresolved": unresolved,
        "type_breakdown": [
            {"type": k, "label": v, "count": type_stats.get(k, 0)}
            for k, v in _EVENT_TYPE_LABELS.items()
        ],
        "level_breakdown": [
            {"level": k, "label": v, "count": level_stats.get(k, 0)}
            for k, v in _EVENT_LEVEL_LABELS.items()
        ],
        "status_breakdown": [
            {"status": k, "label": v, "count": status_stats.get(k, 0)}
            for k, v in _STATUS_LABELS.items()
        ],
    }


def get_unclosed_warnings() -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        """SELECT poe.*, g.gate_name, g.gate_code 
           FROM public_opinion_events poe 
           JOIN gates g ON poe.gate_id = g.id 
           WHERE poe.status NOT IN ('resolved', 'closed')
           ORDER BY poe.handle_deadline ASC, poe.event_level DESC"""
    ).fetchall()
    result = []
    now = datetime.now()
    for row in rows:
        d = dict(row)
        d["event_type_label"] = _EVENT_TYPE_LABELS.get(d["event_type"], d["event_type"])
        d["event_level_label"] = _EVENT_LEVEL_LABELS.get(d["event_level"], d["event_level"])
        d["status_label"] = _STATUS_LABELS.get(d["status"], d["status"])
        try:
            deadline = datetime.strptime(d["handle_deadline"], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                deadline = datetime.strptime(d["handle_deadline"], "%Y-%m-%d")
            except ValueError:
                deadline = now
        time_left = deadline - now
        d["is_overdue"] = time_left.total_seconds() < 0
        d["hours_left"] = int(time_left.total_seconds() / 3600)
        level_priority = {"critical": 4, "serious": 3, "moderate": 2, "minor": 1}
        d["priority_score"] = level_priority.get(d["event_level"], 0)
        if d["is_overdue"]:
            d["priority_score"] += 5
        result.append(d)

    result.sort(key=lambda x: x["priority_score"], reverse=True)
    conn.close()
    return result


def get_event_timeline(event_id: int = None, limit: int = 50) -> list[dict]:
    conn = get_db()
    query = """SELECT pop.*, poe.title as event_title, poe.event_code, poe.status as event_status,
                      g.gate_name, g.gate_code
               FROM public_opinion_progress pop 
               JOIN public_opinion_events poe ON pop.event_id = poe.id 
               JOIN gates g ON poe.gate_id = g.id"""
    params = []
    if event_id:
        query += " WHERE pop.event_id = ?"
        params.append(event_id)
    query += " ORDER BY pop.created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    result = []
    type_icons = {
        "report": "📝",
        "assign": "📋",
        "handle": "🔧",
        "update": "📢",
        "resolve": "✅",
        "close": "🏁",
    }
    for row in rows:
        d = dict(row)
        d["progress_icon"] = type_icons.get(d["progress_type"], "📌")
        d["event_type_label"] = _EVENT_TYPE_LABELS.get(d.get("event_type", ""), "")
        d["event_status_label"] = _STATUS_LABELS.get(d["event_status"], d["event_status"])
        result.append(d)
    conn.close()
    return result


def delete_opinion_event(event_id: int) -> dict:
    conn = get_db()
    try:
        event = conn.execute("SELECT * FROM public_opinion_events WHERE id = ?", (event_id,)).fetchone()
        if not event:
            return {"success": False, "error": "事件不存在"}
        conn.execute("DELETE FROM public_opinion_events WHERE id = ?", (event_id,))
        conn.commit()
        return {"success": True}
    except Exception as e:
        conn.rollback()
        return {"success": False, "error": str(e)}
    finally:
        conn.close()


def get_opinion_event_type_labels() -> dict:
    return _EVENT_TYPE_LABELS


def get_opinion_event_level_labels() -> dict:
    return _EVENT_LEVEL_LABELS


def get_opinion_time_period_labels() -> dict:
    return _TIME_PERIOD_LABELS


def get_opinion_status_labels() -> dict:
    return _STATUS_LABELS


def init_opinion_templates():
    conn = get_db()
    try:
        count = conn.execute("SELECT COUNT(*) FROM public_opinion_notice_templates").fetchone()[0]
        if count > 0:
            conn.close()
            return

        templates = [
            ("商贩聚集-一般告示", "vendor_gathering", "minor",
             "【城门告示】因周边商贩聚集，往来行人请注意安全，有序通行。城防人员正在现场疏导，请配合管理。", "gate_top"),
            ("商贩聚集-严重告示", "vendor_gathering", "serious",
             "【城门告示】因城门外商贩大量聚集，造成通道拥堵。城防部门正在全力疏导，请市民绕行其他城门或错峰出行。感谢配合！", "gate_top"),
            ("百姓请愿-提示", "people_petition", "moderate",
             "【城门告示】城门附近有民众陈情活动，城防部门正在妥善处理。请过往行人配合指引，绕行侧门通行，勿在附近逗留。", "gate_top"),
            ("百姓请愿-警戒", "people_petition", "critical",
             "【城门紧急告示】因城门附近聚集人员较多，已启动二级响应。请市民减少前往，配合城防人员管理，注意自身安全。", "gate_top"),
            ("夜巡异常-提示", "patrol_anomaly", "moderate",
             "【城门安全提示】近日夜巡发现异常情况，城防部门已加强巡逻。请市民提高警惕，发现可疑情况及时报告。", "gate_top"),
            ("道路阻塞-提示", "road_blockage", "moderate",
             "【道路通行提示】因前方道路临时阻塞，城门通行可能受阻。建议错峰出行或绕行其他城门。不便之处敬请谅解。", "gate_top"),
            ("失火传闻-告示", "fire_rumor", "serious",
             "【安全告示】近日有失火传闻，城防部门正在核实。请市民勿信谣传谣，关注官方通报。如遇火情请保持冷静，听从指挥疏散。", "gate_top"),
        ]

        for t in templates:
            conn.execute(
                """INSERT INTO public_opinion_notice_templates 
                   (template_name, event_type, event_level, template_content, announce_position, is_active)
                   VALUES (?,?,?,?,?, 1)""",
                t,
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"初始化通告模板失败: {e}")
    finally:
        conn.close()
