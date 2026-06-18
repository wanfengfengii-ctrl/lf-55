from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.status import HTTP_303_SEE_OTHER
from database import get_db, init_db
from services import (
    generate_schedule_for_gate_date,
    generate_weekly_schedules,
    check_publish_readiness,
    publish_schedule,
    get_weekly_chart_data,
    recalculate_schedules_for_date_range,
    check_curfew_conflict_with_season,
    get_dates_in_season,
    _time_to_minutes,
    get_all_temp_controls,
    create_temp_control,
    delete_temp_control,
    toggle_temp_control,
    get_all_linkage_strategies,
    create_linkage_strategy,
    delete_linkage_strategy,
    toggle_linkage_strategy,
    preview_conflicts,
    simulate_batch_publish,
    get_linkage_weekly_comparison,
    add_traffic_history,
    batch_add_traffic_history,
    get_traffic_history,
    predict_traffic_for_week,
    get_traffic_predictions,
    get_dispatch_suggestions,
    execute_dispatch_suggestion,
    dismiss_dispatch_suggestion,
    get_dispatch_comparison,
    get_overload_warnings,
)
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

app = FastAPI(title="古城门启闭排班系统")

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def redirect_with_error(url: str, error_msg: str) -> RedirectResponse:
    params = urlencode({"error": error_msg})
    return RedirectResponse(url=f"{url}?{params}", status_code=HTTP_303_SEE_OTHER)


@app.on_event("startup")
def startup():
    init_db()
    _seed_data()


def _seed_data():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM gates")
    if c.fetchone()[0] == 0:
        gates_data = [
            ("DM-01", "朝阳门", "东", 1, "正东主门"),
            ("NM-01", "安定门", "北", 1, "正北主门"),
            ("WM-01", "阜成门", "西", 0, "西门"),
            ("SM-01", "永定门", "南", 1, "正南主门"),
        ]
        c.executemany(
            "INSERT INTO gates (gate_code, gate_name, direction, is_main, notes) VALUES (?,?,?,?,?)",
            gates_data,
        )

    c.execute("SELECT COUNT(*) FROM seasons")
    if c.fetchone()[0] == 0:
        seasons_data = [
            ("春", 2, 4, 5, 5, "05:45", "18:30"),
            ("夏", 5, 6, 8, 7, "05:00", "19:30"),
            ("秋", 8, 8, 10, 7, "06:00", "18:00"),
            ("冬", 10, 8, 2, 3, "06:30", "17:15"),
        ]
        c.executemany(
            "INSERT INTO seasons (season_name, start_month, start_day, end_month, end_day, sunrise_time, sunset_time) VALUES (?,?,?,?,?,?,?)",
            seasons_data,
        )

    c.execute("SELECT COUNT(*) FROM alert_levels")
    if c.fetchone()[0] == 0:
        alert_data = [
            ("和平", 1, 0, 0, "无威胁"),
            ("戒备", 2, 15, 0, "轻度戒备"),
            ("警戒", 3, 30, 15, "中度警戒"),
            ("战备", 4, 60, 30, "高度战备"),
            ("戒严", 5, 120, 60, "全面戒严"),
        ]
        c.executemany(
            "INSERT INTO alert_levels (level_name, level_value, close_advance_minutes, open_delay_minutes, description) VALUES (?,?,?,?,?)",
            alert_data,
        )

    c.execute("SELECT COUNT(*) FROM curfew_rules")
    if c.fetchone()[0] == 0:
        curfew_data = [
            ("春季宵禁", 1, "21:00", "05:00"),
            ("夏季宵禁", 2, "22:00", "04:30"),
            ("秋季宵禁", 3, "20:30", "05:15"),
            ("冬季宵禁", 4, "20:00", "05:30"),
        ]
        c.executemany(
            "INSERT INTO curfew_rules (rule_name, season_id, curfew_start, curfew_end) VALUES (?,?,?,?)",
            curfew_data,
        )

    c.execute("SELECT COUNT(*) FROM festivals")
    if c.fetchone()[0] == 0:
        import datetime

        year = datetime.datetime.now().year
        fest_data = [
            ("元宵节", f"{year}-02-24", 60, "上元节，花灯延后关城"),
            ("端午节", f"{year}-06-10", 30, "龙舟竞渡"),
            ("中秋节", f"{year}-09-17", 90, "中秋赏月，大幅延迟"),
            ("重阳节", f"{year}-10-11", 45, "登高赏秋"),
        ]
        c.executemany(
            "INSERT INTO festivals (festival_name, festival_date, delay_minutes, notes) VALUES (?,?,?,?)",
            fest_data,
        )

    conn.commit()
    conn.close()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    conn = get_db()
    gates = conn.execute("SELECT * FROM gates").fetchall()
    seasons = conn.execute("SELECT * FROM seasons").fetchall()
    alert_levels = conn.execute("SELECT * FROM alert_levels ORDER BY level_value").fetchall()
    conn.close()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "gates": gates,
        "seasons": seasons,
        "alert_levels": alert_levels,
        "active_page": "home",
    })


@app.get("/gates", response_class=HTMLResponse)
async def gates_page(request: Request, error: str = None):
    conn = get_db()
    gates = conn.execute("SELECT * FROM gates ORDER BY id").fetchall()
    conn.close()
    return templates.TemplateResponse("gates.html", {
        "request": request,
        "gates": gates,
        "active_page": "gates",
        "error_msg": error,
    })


@app.post("/gates/add")
async def add_gate(
    gate_code: str = Form(...),
    gate_name: str = Form(...),
    direction: str = Form(...),
    is_main: int = Form(0),
    notes: str = Form(""),
):
    conn = get_db()
    try:
        existing = conn.execute("SELECT id FROM gates WHERE gate_code = ?", (gate_code,)).fetchone()
        if existing:
            conn.close()
            return redirect_with_error("/gates", f"城门编号 '{gate_code}' 已存在，不能重复")
        conn.execute(
            "INSERT INTO gates (gate_code, gate_name, direction, is_main, notes) VALUES (?,?,?,?,?)",
            (gate_code, gate_name, direction, is_main, notes),
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/gates", status_code=303)


@app.post("/gates/edit/{gate_id}")
async def edit_gate(
    gate_id: int,
    gate_code: str = Form(...),
    gate_name: str = Form(...),
    direction: str = Form(...),
    is_main: int = Form(0),
    notes: str = Form(""),
):
    conn = get_db()
    try:
        existing = conn.execute(
            "SELECT id FROM gates WHERE gate_code = ? AND id != ?",
            (gate_code, gate_id),
        ).fetchone()
        if existing:
            conn.close()
            return redirect_with_error("/gates", f"城门编号 '{gate_code}' 已存在，不能重复")
        conn.execute(
            "UPDATE gates SET gate_code = ?, gate_name = ?, direction = ?, is_main = ?, notes = ? WHERE id = ?",
            (gate_code, gate_name, direction, is_main, notes, gate_id),
        )
        conn.commit()
        recalculate_schedules_for_date_range(
            (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"),
            (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"),
        )
    finally:
        conn.close()
    return RedirectResponse(url="/gates", status_code=303)


@app.post("/gates/delete/{gate_id}")
async def delete_gate(gate_id: int):
    conn = get_db()
    conn.execute("DELETE FROM gates WHERE id = ?", (gate_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/gates", status_code=303)


@app.get("/seasons", response_class=HTMLResponse)
async def seasons_page(request: Request, error: str = None):
    conn = get_db()
    seasons = conn.execute("SELECT * FROM seasons ORDER BY start_month").fetchall()
    conn.close()
    return templates.TemplateResponse("seasons.html", {
        "request": request,
        "seasons": seasons,
        "active_page": "seasons",
        "error_msg": error,
    })


@app.post("/seasons/add")
async def add_season(
    season_name: str = Form(...),
    start_month: int = Form(...),
    start_day: int = Form(...),
    end_month: int = Form(...),
    end_day: int = Form(...),
    sunrise_time: str = Form(...),
    sunset_time: str = Form(...),
):
    if _time_to_minutes(sunrise_time) >= _time_to_minutes(sunset_time):
        return redirect_with_error("/seasons", "日出时间必须早于日落时间")
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO seasons (season_name, start_month, start_day, end_month, end_day, sunrise_time, sunset_time) VALUES (?,?,?,?,?,?,?)",
            (season_name, start_month, start_day, end_month, end_day, sunrise_time, sunset_time),
        )
        conn.commit()
    except Exception as e:
        conn.close()
        return redirect_with_error("/seasons", str(e))
    conn.close()
    return RedirectResponse(url="/seasons", status_code=303)


@app.post("/seasons/edit/{season_id}")
async def edit_season(
    season_id: int,
    season_name: str = Form(...),
    start_month: int = Form(...),
    start_day: int = Form(...),
    end_month: int = Form(...),
    end_day: int = Form(...),
    sunrise_time: str = Form(...),
    sunset_time: str = Form(...),
):
    if _time_to_minutes(sunrise_time) >= _time_to_minutes(sunset_time):
        return redirect_with_error("/seasons", "日出时间必须早于日落时间")
    conn = get_db()
    try:
        conn.execute(
            "UPDATE seasons SET season_name = ?, start_month = ?, start_day = ?, end_month = ?, end_day = ?, sunrise_time = ?, sunset_time = ? WHERE id = ?",
            (season_name, start_month, start_day, end_month, end_day, sunrise_time, sunset_time, season_id),
        )
        conn.commit()
        season_dates = get_dates_in_season(season_id)
        if season_dates:
            recalculate_schedules_for_date_range(season_dates[0], season_dates[-1])
    except Exception as e:
        conn.close()
        return redirect_with_error("/seasons", str(e))
    conn.close()
    return RedirectResponse(url="/seasons", status_code=303)


@app.post("/seasons/delete/{season_id}")
async def delete_season(season_id: int):
    conn = get_db()
    season_dates = get_dates_in_season(season_id)
    conn.execute("DELETE FROM seasons WHERE id = ?", (season_id,))
    conn.commit()
    conn.close()
    if season_dates:
        recalculate_schedules_for_date_range(season_dates[0], season_dates[-1])
    return RedirectResponse(url="/seasons", status_code=303)


@app.get("/curfews", response_class=HTMLResponse)
async def curfews_page(request: Request, error: str = None):
    conn = get_db()
    curfews = conn.execute(
        "SELECT cr.*, s.season_name FROM curfew_rules cr JOIN seasons s ON cr.season_id = s.id ORDER BY s.start_month"
    ).fetchall()
    seasons = conn.execute("SELECT * FROM seasons ORDER BY start_month").fetchall()
    conn.close()
    return templates.TemplateResponse("curfews.html", {
        "request": request,
        "curfews": curfews,
        "seasons": seasons,
        "active_page": "curfews",
        "error_msg": error,
    })


@app.post("/curfews/add")
async def add_curfew(
    rule_name: str = Form(...),
    season_id: int = Form(...),
    curfew_start: str = Form(...),
    curfew_end: str = Form(...),
):
    conflicts = check_curfew_conflict_with_season(curfew_start, curfew_end, season_id)
    if conflicts:
        return redirect_with_error("/curfews", "；".join(conflicts))

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO curfew_rules (rule_name, season_id, curfew_start, curfew_end) VALUES (?,?,?,?)",
            (rule_name, season_id, curfew_start, curfew_end),
        )
        conn.commit()
        season_dates = get_dates_in_season(season_id)
        if season_dates:
            recalculate_schedules_for_date_range(season_dates[0], season_dates[-1])
    except Exception as e:
        conn.close()
        return redirect_with_error("/curfews", str(e))
    conn.close()
    return RedirectResponse(url="/curfews", status_code=303)


@app.post("/curfews/edit/{curfew_id}")
async def edit_curfew(
    curfew_id: int,
    rule_name: str = Form(...),
    season_id: int = Form(...),
    curfew_start: str = Form(...),
    curfew_end: str = Form(...),
):
    conflicts = check_curfew_conflict_with_season(curfew_start, curfew_end, season_id)
    if conflicts:
        return redirect_with_error("/curfews", "；".join(conflicts))

    conn = get_db()
    try:
        conn.execute(
            "UPDATE curfew_rules SET rule_name = ?, season_id = ?, curfew_start = ?, curfew_end = ? WHERE id = ?",
            (rule_name, season_id, curfew_start, curfew_end, curfew_id),
        )
        conn.commit()
        season_dates = get_dates_in_season(season_id)
        if season_dates:
            recalculate_schedules_for_date_range(season_dates[0], season_dates[-1])
    except Exception as e:
        conn.close()
        return redirect_with_error("/curfews", str(e))
    conn.close()
    return RedirectResponse(url="/curfews", status_code=303)


@app.post("/curfews/delete/{curfew_id}")
async def delete_curfew(curfew_id: int):
    conn = get_db()
    row = conn.execute("SELECT season_id FROM curfew_rules WHERE id = ?", (curfew_id,)).fetchone()
    season_id = row["season_id"] if row else None
    conn.execute("DELETE FROM curfew_rules WHERE id = ?", (curfew_id,))
    conn.commit()
    conn.close()
    if season_id:
        season_dates = get_dates_in_season(season_id)
        if season_dates:
            recalculate_schedules_for_date_range(season_dates[0], season_dates[-1])
    return RedirectResponse(url="/curfews", status_code=303)


@app.get("/festivals", response_class=HTMLResponse)
async def festivals_page(request: Request, error: str = None):
    conn = get_db()
    festivals = conn.execute("SELECT * FROM festivals ORDER BY festival_date").fetchall()
    conn.close()
    return templates.TemplateResponse("festivals.html", {
        "request": request,
        "festivals": festivals,
        "active_page": "festivals",
        "error_msg": error,
    })


@app.post("/festivals/add")
async def add_festival(
    festival_name: str = Form(...),
    festival_date: str = Form(...),
    delay_minutes: int = Form(30),
    notes: str = Form(""),
):
    conn = get_db()
    try:
        existing = conn.execute(
            "SELECT id FROM festivals WHERE festival_date = ?",
            (festival_date,),
        ).fetchone()
        if existing:
            conn.close()
            return redirect_with_error("/festivals", f"日期 {festival_date} 已存在节庆活动，同一天只能有一个节庆")
        conn.execute(
            "INSERT INTO festivals (festival_name, festival_date, delay_minutes, notes) VALUES (?,?,?,?)",
            (festival_name, festival_date, delay_minutes, notes),
        )
        conn.commit()
        recalculate_schedules_for_date_range(festival_date)
    except Exception as e:
        conn.close()
        return redirect_with_error("/festivals", str(e))
    conn.close()
    return RedirectResponse(url="/festivals", status_code=303)


@app.post("/festivals/edit/{festival_id}")
async def edit_festival(
    festival_id: int,
    festival_name: str = Form(...),
    festival_date: str = Form(...),
    delay_minutes: int = Form(30),
    notes: str = Form(""),
):
    conn = get_db()
    try:
        existing = conn.execute(
            "SELECT id FROM festivals WHERE festival_date = ? AND id != ?",
            (festival_date, festival_id),
        ).fetchone()
        if existing:
            conn.close()
            return redirect_with_error("/festivals", f"日期 {festival_date} 已存在节庆活动，同一天只能有一个节庆")
        conn.execute(
            "UPDATE festivals SET festival_name = ?, festival_date = ?, delay_minutes = ?, notes = ? WHERE id = ?",
            (festival_name, festival_date, delay_minutes, notes, festival_id),
        )
        conn.commit()
        recalculate_schedules_for_date_range(festival_date)
    except Exception as e:
        conn.close()
        return redirect_with_error("/festivals", str(e))
    conn.close()
    return RedirectResponse(url="/festivals", status_code=303)


@app.post("/festivals/delete/{festival_id}")
async def delete_festival(festival_id: int):
    conn = get_db()
    row = conn.execute("SELECT festival_date FROM festivals WHERE id = ?", (festival_id,)).fetchone()
    festival_date = row["festival_date"] if row else None
    conn.execute("DELETE FROM festivals WHERE id = ?", (festival_id,))
    conn.commit()
    conn.close()
    if festival_date:
        recalculate_schedules_for_date_range(festival_date)
    return RedirectResponse(url="/festivals", status_code=303)


@app.get("/alerts", response_class=HTMLResponse)
async def alerts_page(request: Request, error: str = None):
    conn = get_db()
    levels = conn.execute("SELECT * FROM alert_levels ORDER BY level_value").fetchall()
    daily_alerts = conn.execute(
        "SELECT da.*, al.level_name, al.level_value FROM daily_alerts da JOIN alert_levels al ON da.alert_level_id = al.id ORDER BY da.alert_date DESC"
    ).fetchall()
    conn.close()
    return templates.TemplateResponse("alerts.html", {
        "request": request,
        "levels": levels,
        "daily_alerts": daily_alerts,
        "active_page": "alerts",
        "error_msg": error,
    })


@app.post("/alerts/level/add")
async def add_alert_level(
    level_name: str = Form(...),
    level_value: int = Form(...),
    close_advance_minutes: int = Form(0),
    open_delay_minutes: int = Form(0),
    description: str = Form(""),
):
    conn = get_db()
    try:
        existing = conn.execute(
            "SELECT id FROM alert_levels WHERE level_name = ? OR level_value = ?",
            (level_name, level_value),
        ).fetchone()
        if existing:
            conn.close()
            return redirect_with_error("/alerts", "警戒等级名称或数值已存在")
        conn.execute(
            "INSERT INTO alert_levels (level_name, level_value, close_advance_minutes, open_delay_minutes, description) VALUES (?,?,?,?,?)",
            (level_name, level_value, close_advance_minutes, open_delay_minutes, description),
        )
        conn.commit()
        recalculate_schedules_for_date_range(
            (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"),
            (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"),
        )
    except Exception as e:
        conn.close()
        return redirect_with_error("/alerts", f"警戒等级名称或数值重复: {e}")
    conn.close()
    return RedirectResponse(url="/alerts", status_code=303)


@app.post("/alerts/level/edit/{level_id}")
async def edit_alert_level(
    level_id: int,
    level_name: str = Form(...),
    level_value: int = Form(...),
    close_advance_minutes: int = Form(0),
    open_delay_minutes: int = Form(0),
    description: str = Form(""),
):
    conn = get_db()
    try:
        existing = conn.execute(
            "SELECT id FROM alert_levels WHERE (level_name = ? OR level_value = ?) AND id != ?",
            (level_name, level_value, level_id),
        ).fetchone()
        if existing:
            conn.close()
            return redirect_with_error("/alerts", "警戒等级名称或数值已存在")
        conn.execute(
            "UPDATE alert_levels SET level_name = ?, level_value = ?, close_advance_minutes = ?, open_delay_minutes = ?, description = ? WHERE id = ?",
            (level_name, level_value, close_advance_minutes, open_delay_minutes, description, level_id),
        )
        conn.commit()
        recalculate_schedules_for_date_range(
            (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"),
            (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"),
        )
    except Exception as e:
        conn.close()
        return redirect_with_error("/alerts", f"警戒等级名称或数值重复: {e}")
    conn.close()
    return RedirectResponse(url="/alerts", status_code=303)


@app.post("/alerts/level/delete/{level_id}")
async def delete_alert_level(level_id: int):
    conn = get_db()
    conn.execute("DELETE FROM alert_levels WHERE id = ?", (level_id,))
    conn.commit()
    conn.close()
    recalculate_schedules_for_date_range(
        (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"),
        (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"),
    )
    return RedirectResponse(url="/alerts", status_code=303)


@app.post("/alerts/daily/add")
async def add_daily_alert(
    alert_date: str = Form(...),
    alert_level_id: int = Form(...),
):
    conn = get_db()
    try:
        existing = conn.execute("SELECT id FROM daily_alerts WHERE alert_date = ?", (alert_date,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE daily_alerts SET alert_level_id = ? WHERE alert_date = ?",
                (alert_level_id, alert_date),
            )
        else:
            conn.execute(
                "INSERT INTO daily_alerts (alert_date, alert_level_id) VALUES (?,?)",
                (alert_date, alert_level_id),
            )
        conn.commit()
        recalculate_schedules_for_date_range(alert_date)
    except Exception as e:
        conn.close()
        return redirect_with_error("/alerts", str(e))
    conn.close()
    return RedirectResponse(url="/alerts", status_code=303)


@app.post("/alerts/daily/delete/{daily_id}")
async def delete_daily_alert(daily_id: int):
    conn = get_db()
    row = conn.execute("SELECT alert_date FROM daily_alerts WHERE id = ?", (daily_id,)).fetchone()
    if row:
        alert_date = row["alert_date"]
        conn.execute("DELETE FROM daily_alerts WHERE id = ?", (daily_id,))
        conn.commit()
        recalculate_schedules_for_date_range(alert_date)
    else:
        conn.execute("DELETE FROM daily_alerts WHERE id = ?", (daily_id,))
        conn.commit()
    conn.close()
    return RedirectResponse(url="/alerts", status_code=303)


@app.get("/schedules", response_class=HTMLResponse)
async def schedules_page(request: Request, start_date: str = None, error: str = None):
    if not start_date:
        today = datetime.now()
        start_of_week = today - timedelta(days=today.weekday())
        start_date = start_of_week.strftime("%Y-%m-%d")

    conn = get_db()
    gates = conn.execute("SELECT * FROM gates ORDER BY id").fetchall()
    conn.close()

    return templates.TemplateResponse("schedules.html", {
        "request": request,
        "start_date": start_date,
        "gates": gates,
        "active_page": "schedules",
        "error_msg": error,
    })


@app.get("/api/schedules/generate")
async def api_generate_schedules(start_date: str):
    results = generate_weekly_schedules(start_date)
    return JSONResponse(content=results)


@app.get("/api/schedules/chart-data")
async def api_chart_data(start_date: str):
    data = get_weekly_chart_data(start_date)
    return JSONResponse(content=data)


@app.post("/api/schedules/publish")
async def api_publish_schedule(date_str: str):
    result = publish_schedule(date_str)
    if not result["success"]:
        return JSONResponse(content=result, status_code=400)
    return JSONResponse(content=result)


@app.get("/api/schedules/check-publish")
async def api_check_publish(date_str: str):
    result = check_publish_readiness(date_str)
    return JSONResponse(content=result)


@app.get("/api/schedules/date/{date_str}")
async def api_schedules_for_date(date_str: str):
    conn = get_db()
    rows = conn.execute(
        """SELECT s.*, g.gate_name, g.gate_code FROM schedules s
           JOIN gates g ON s.gate_id = g.id
           WHERE s.schedule_date = ?
           ORDER BY g.id, s.scheme_type""",
        (date_str,),
    ).fetchall()
    conn.close()
    return JSONResponse(content=[dict(r) for r in rows])


@app.get("/api/generate-single")
async def api_generate_single(gate_id: int, date_str: str):
    result = generate_schedule_for_gate_date(gate_id, date_str)
    return JSONResponse(content=result)


@app.get("/temp-controls", response_class=HTMLResponse)
async def temp_controls_page(request: Request, error: str = None):
    conn = get_db()
    gates = conn.execute("SELECT * FROM gates ORDER BY id").fetchall()
    conn.close()
    orders = get_all_temp_controls()
    return templates.TemplateResponse("temp_controls.html", {
        "request": request,
        "gates": gates,
        "orders": orders,
        "active_page": "temp_controls",
        "error_msg": error,
    })


@app.post("/temp-controls/add")
async def add_temp_control(
    order_name: str = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(...),
    time_start: str = Form("00:00"),
    time_end: str = Form("23:59"),
    action_type: str = Form(...),
    forced_open_time: str = Form(""),
    forced_close_time: str = Form(""),
    priority: int = Form(10),
    override_reason: str = Form(""),
    gate_ids: str = Form(...),
):
    gid_list = [int(x) for x in gate_ids.split(",") if x.strip().isdigit()]
    if not gid_list:
        return redirect_with_error("/temp-controls", "请至少选择一个城门")
    if action_type == "restrict_hours":
        if not forced_open_time or not forced_close_time:
            return redirect_with_error("/temp-controls", "限制开放时段类型必须填写限制开门时间和限制关门时间")
        if _time_to_minutes(forced_open_time) >= _time_to_minutes(forced_close_time):
            return redirect_with_error("/temp-controls", "限制开门时间必须早于限制关门时间")
    result = create_temp_control(
        order_name, start_date, end_date, time_start, time_end,
        action_type, forced_open_time, forced_close_time,
        priority, override_reason, gid_list,
    )
    if not result.get("success"):
        return redirect_with_error("/temp-controls", "创建管制令失败")
    return RedirectResponse(url="/temp-controls", status_code=303)


@app.post("/temp-controls/delete/{order_id}")
async def delete_temp_control_route(order_id: int):
    delete_temp_control(order_id)
    return RedirectResponse(url="/temp-controls", status_code=303)


@app.post("/temp-controls/toggle/{order_id}")
async def toggle_temp_control_route(order_id: int):
    toggle_temp_control(order_id)
    return RedirectResponse(url="/temp-controls", status_code=303)


@app.get("/linkage-strategies", response_class=HTMLResponse)
async def linkage_strategies_page(request: Request, error: str = None):
    conn = get_db()
    gates = conn.execute("SELECT * FROM gates ORDER BY id").fetchall()
    conn.close()
    strategies = get_all_linkage_strategies()
    return templates.TemplateResponse("linkage_strategies.html", {
        "request": request,
        "gates": gates,
        "strategies": strategies,
        "active_page": "linkage_strategies",
        "error_msg": error,
    })


@app.post("/linkage-strategies/add")
async def add_linkage_strategy(
    strategy_name: str = Form(...),
    trigger_type: str = Form(...),
    trigger_gate_id: str = Form(""),
    trigger_event: str = Form(""),
    linked_open_time: str = Form(""),
    linked_close_time: str = Form(""),
    priority: int = Form(5),
    description: str = Form(""),
    items_json: str = Form(...),
):
    import json
    try:
        items = json.loads(items_json)
    except Exception:
        return redirect_with_error("/linkage-strategies", "联动项数据格式错误")
    if not items:
        return redirect_with_error("/linkage-strategies", "请至少添加一个联动城门")
    gate_ids = [item["gate_id"] for item in items]
    if len(gate_ids) != len(set(gate_ids)):
        return redirect_with_error("/linkage-strategies", "存在重复的联动城门，请检查后重新提交")
    result = create_linkage_strategy(
        strategy_name, trigger_type, trigger_gate_id,
        trigger_event, linked_open_time, linked_close_time,
        priority, description, items,
    )
    if not result.get("success"):
        return redirect_with_error("/linkage-strategies", "创建联动策略失败")
    return RedirectResponse(url="/linkage-strategies", status_code=303)


@app.post("/linkage-strategies/delete/{strategy_id}")
async def delete_linkage_strategy_route(strategy_id: int):
    delete_linkage_strategy(strategy_id)
    return RedirectResponse(url="/linkage-strategies", status_code=303)


@app.post("/linkage-strategies/toggle/{strategy_id}")
async def toggle_linkage_strategy_route(strategy_id: int):
    toggle_linkage_strategy(strategy_id)
    return RedirectResponse(url="/linkage-strategies", status_code=303)


@app.get("/api/schedules/conflict-preview")
async def api_conflict_preview(start_date: str, end_date: str):
    result = preview_conflicts(start_date, end_date)
    return JSONResponse(content=result)


@app.get("/api/schedules/batch-simulate")
async def api_batch_simulate(start_date: str, end_date: str):
    result = simulate_batch_publish(start_date, end_date)
    return JSONResponse(content=result)


@app.get("/api/linkage/weekly-comparison")
async def api_linkage_weekly_comparison(start_date: str, strategy_id: int):
    result = get_linkage_weekly_comparison(start_date, strategy_id)
    if "error" in result:
        return JSONResponse(content=result, status_code=404)
    return JSONResponse(content=result)


@app.get("/api/linkage/strategies-list")
async def api_linkage_strategies_list():
    strategies = get_all_linkage_strategies()
    return JSONResponse(content=[{
        "id": s["id"],
        "strategy_name": s["strategy_name"],
        "is_active": s["is_active"],
    } for s in strategies])


@app.get("/traffic-prediction", response_class=HTMLResponse)
async def traffic_prediction_page(request: Request, error: str = None):
    conn = get_db()
    gates = conn.execute("SELECT * FROM gates ORDER BY id").fetchall()
    conn.close()
    today = datetime.now()
    start_of_week = today - timedelta(days=today.weekday())
    start_date = start_of_week.strftime("%Y-%m-%d")
    return templates.TemplateResponse("traffic_prediction.html", {
        "request": request,
        "gates": gates,
        "start_date": start_date,
        "active_page": "traffic_prediction",
        "error_msg": error,
    })


@app.post("/api/traffic/history")
async def api_add_traffic_history(request: Request):
    body = await request.json()
    records = body.get("records", [])
    if not records:
        return JSONResponse(content={"success": False, "error": "无数据"}, status_code=400)
    result = batch_add_traffic_history(records)
    return JSONResponse(content=result)


@app.get("/api/traffic/history")
async def api_get_traffic_history(gate_id: int = None, start_date: str = None,
                                  end_date: str = None, time_period: str = None):
    result = get_traffic_history(gate_id, start_date, end_date, time_period)
    return JSONResponse(content=result)


@app.post("/api/traffic/predict")
async def api_predict_traffic(request: Request):
    body = await request.json()
    start_date = body.get("start_date")
    if not start_date:
        return JSONResponse(content={"error": "请提供起始日期"}, status_code=400)
    result = predict_traffic_for_week(start_date)
    return JSONResponse(content=result)


@app.get("/api/traffic/predictions")
async def api_get_predictions(start_date: str):
    result = get_traffic_predictions(start_date)
    return JSONResponse(content=result)


@app.get("/api/traffic/dispatch-suggestions")
async def api_get_dispatch_suggestions(start_date: str = None, status: str = None):
    result = get_dispatch_suggestions(start_date, status)
    return JSONResponse(content=result)


@app.post("/api/traffic/dispatch-execute/{suggestion_id}")
async def api_execute_dispatch(suggestion_id: int):
    result = execute_dispatch_suggestion(suggestion_id)
    if not result["success"]:
        return JSONResponse(content=result, status_code=400)
    return JSONResponse(content=result)


@app.post("/api/traffic/dispatch-dismiss/{suggestion_id}")
async def api_dismiss_dispatch(suggestion_id: int):
    result = dismiss_dispatch_suggestion(suggestion_id)
    if not result["success"]:
        return JSONResponse(content=result, status_code=400)
    return JSONResponse(content=result)


@app.get("/api/traffic/comparison")
async def api_get_comparison(start_date: str):
    result = get_dispatch_comparison(start_date)
    return JSONResponse(content=result)


@app.get("/api/traffic/overload-warnings")
async def api_get_overload_warnings(start_date: str):
    result = get_overload_warnings(start_date)
    return JSONResponse(content=result)


@app.delete("/api/traffic/history/{record_id}")
async def api_delete_traffic_history(record_id: int):
    conn = get_db()
    conn.execute("DELETE FROM traffic_history WHERE id = ?", (record_id,))
    conn.commit()
    conn.close()
    return JSONResponse(content={"success": True})
