#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Strava 打卡图:时区判定 + 推送决策 + 出图。"""
import json, os, subprocess, sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
WINDOW_OPEN_HOUR = 10     # 当地时间几点之后开始推送当天的新活动
SAFETY_HOURS = 6          # 兜底:活动挂了这么久还没推,无视时区强推
EMPTY_NOTE_HOUR = 21      # 当地时间几点之后,若当天无活动发一句提示
FALLBACK_TZ = "Asia/Hong_Kong"

BADGES = {
    "Run": "🏃 跑步", "TrailRun": "🏃 越野跑", "Ride": "🚴 骑行",
    "VirtualRide": "🚴 骑行", "WeightTraining": "🏋️ 力量训练",
    "Workout": "💪 健身", "Walk": "🚶 步行", "Hike": "🥾 徒步",
    "Swim": "🏊 游泳", "Yoga": "🧘 瑜伽",
}
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def fmt_dur(sec):
    sec = int(round(sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def start_point(act):
    """解出活动起点经纬度;室内活动返回 None。"""
    poly = act.get("reduced_polyline")
    if not poly:
        return None
    try:
        import polyline
        pts = polyline.decode(poly)
        return pts[0] if pts else None
    except Exception as e:
        print(f"[warn] polyline decode failed for {act.get('id')}: {e}", file=sys.stderr)
        return None


def detect_tz(activities):
    """用最近一条带 GPS 的活动推断时区。人在旅行时跑一次户外就会自动更新。
    全是室内活动 / 解析失败时回退到 FALLBACK_TZ(此时靠 SAFETY_HOURS 兜底)。"""
    for act in activities:                      # 已按时间倒序
        pt = start_point(act)
        if not pt:
            continue
        try:
            from timezonefinder import TimezoneFinder
            tz = TimezoneFinder().timezone_at(lat=pt[0], lng=pt[1])
            if tz:
                return tz, act.get("id")
        except Exception as e:
            print(f"[warn] timezonefinder failed: {e}", file=sys.stderr)
            break
    return FALLBACK_TZ, None


def place_of(act):
    """卡片上的位置行 —— 用活动自己的 GPS,不用 Strava 资料里的固定城市。"""
    pt = start_point(act)
    if not pt:
        return "室内 · Indoor"
    try:
        import reverse_geocode
        g = reverse_geocode.get(pt)
        city, county, country = g.get("city", ""), g.get("county", ""), g.get("country", "")
        parts = [city]
        if county.startswith("City of ") and county[8:] != city:
            parts.append(county[8:])          # "The Rocks, Sydney, Australia"
        parts.append(country)
        return ", ".join(p for p in parts if p)
    except Exception as e:
        print(f"[warn] reverse geocode failed: {e}", file=sys.stderr)
        return ""


def render(act, outdir):
    s = act.get("summary", {})
    dt = datetime.fromisoformat(act["start_local"])
    dist, mov = s.get("distance") or 0, s.get("moving_time") or 0
    sport = act.get("sport_type", "")
    stats = []

    if dist > 100:
        hero = (f"{dist/1000:.2f}", "公里", "距离 · DISTANCE")
        stats.append(("运动时间", fmt_dur(mov)))
        if sport in ("Ride", "VirtualRide"):
            if mov:
                stats.append(("平均速度", f"{dist/1000/(mov/3600):.1f}<span class='u'>km/h</span>"))
        elif mov:
            p = mov / (dist / 1000)
            stats.append(("平均配速", f"{int(p//60)}'{int(round(p%60)):02d}\"<span class='u'>/km</span>"))
        stats.append(("爬升", f"{round(s.get('elevation_gain') or 0)}<span class='u'>m</span>"))
        if s.get("total_calories"):
            stats.append(("卡路里", f"{round(s['total_calories'])}<span class='u'>kcal</span>"))
        if s.get("avg_cadence"):
            c = s["avg_cadence"]
            stats.append(("平均踏频", f"{round(c)}<span class='u'>rpm</span>") if sport in ("Ride", "VirtualRide")
                         else ("平均步频", f"{round(c*2)}<span class='u'>spm</span>"))
        if s.get("max_speed"):
            stats.append(("最大速度", f"{s['max_speed']:.1f}<span class='u'>m/s</span>"))
    else:
        hero = (str(int(round(mov / 60))), "分钟", "时长 · DURATION")
        stats.append(("运动时间", fmt_dur(mov)))
        if s.get("total_calories"):
            stats.append(("卡路里", f"{round(s['total_calories'])}<span class='u'>kcal</span>"))
        if s.get("elapsed_time") and s["elapsed_time"] != mov:
            stats.append(("总耗时", fmt_dur(s["elapsed_time"])))

    stats_html = "\n".join(
        f'    <div class="stat"><div class="stat-label">{l}</div>'
        f'<div class="stat-value">{v}</div></div>' for l, v in stats[:6])
    effort = round(s.get("relative_effort") or 0)

    html = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
    for k, v in {
        "{{LOCATION}}": place_of(act), "{{BADGE}}": BADGES.get(sport, "💪 运动"),
        "{{NAME}}": act.get("name", ""),
        "{{DATETIME}}": f"{dt.year}年{dt.month}月{dt.day}日 · {WEEKDAYS[dt.weekday()]} {dt:%H:%M}",
        "{{HERO_VALUE}}": hero[0], "{{HERO_UNIT}}": hero[1], "{{HERO_LABEL}}": hero[2],
        "{{STATS}}": stats_html, "{{EFFORT}}": str(effort),
        "{{EFFORT_PCT}}": str(min(effort, 100)),
    }.items():
        html = html.replace(k, v)

    tmp = os.path.join(outdir, f"_render_{act['id']}.html")
    png = os.path.join(outdir, f"strava-daka-{dt:%Y-%m-%d}-{act['id']}.png")
    open(tmp, "w", encoding="utf-8").write(html)
    # 截图子进程的 stdout 必须吃掉,否则会污染本脚本输出的 JSON
    subprocess.run(["node", os.path.join(HERE, "shot.js"), tmp, png],
                   check=True, stdout=subprocess.DEVNULL)
    return png


def main():
    acts = json.load(open(sys.argv[1], encoding="utf-8"))
    state = json.load(open(sys.argv[2], encoding="utf-8")) if os.path.exists(sys.argv[2]) else {}
    outdir = os.path.abspath(sys.argv[3] if len(sys.argv) > 3 else HERE)
    os.makedirs(outdir, exist_ok=True)
    now_utc = datetime.now(timezone.utc)
    if os.environ.get("FAKE_UTC"):                      # 测试用
        now_utc = datetime.fromisoformat(os.environ["FAKE_UTC"]).replace(tzinfo=timezone.utc)

    tz_name, tz_src = detect_tz(acts)
    try:
        from zoneinfo import ZoneInfo
        local_now = now_utc.astimezone(ZoneInfo(tz_name))
    except Exception:
        tz_name, local_now = "UTC", now_utc

    processed = set(str(i) for i in state.get("processed_ids", []))
    pending = dict(state.get("pending", {}))

    # 记录每条未推送活动的"首次看到时间",供兜底规则用
    fresh = [a for a in acts if str(a["id"]) not in processed]
    for a in fresh:
        pending.setdefault(str(a["id"]), now_utc.isoformat())
    for k in list(pending):                              # 清掉已推送的残留
        if k in processed:
            pending.pop(k)

    window_open = local_now.hour >= WINDOW_OPEN_HOUR
    to_push = []
    for a in fresh:
        seen = datetime.fromisoformat(pending[str(a["id"])])
        stale = (now_utc - seen) >= timedelta(hours=SAFETY_HOURS)
        if window_open or stale:                         # 时区判错也不会漏
            to_push.append(a)

    cards = []
    for a in sorted(to_push, key=lambda x: x["start_local"]):
        try:
            cards.append({"id": str(a["id"]), "name": a.get("name", ""),
                          "sport": a.get("sport_type", ""), "png": render(a, outdir)})
            processed.add(str(a["id"]))
            pending.pop(str(a["id"]), None)
        except Exception as e:
            print(f"[error] render failed for {a.get('id')}: {e}", file=sys.stderr)

    today = local_now.strftime("%Y-%m-%d")
    empty_note = (not cards and not fresh
                  and local_now.hour >= EMPTY_NOTE_HOUR
                  and state.get("last_empty_note") != today)

    new_state = {
        "processed_ids": sorted(processed, key=int)[-30:],
        "pending": pending,
        "last_empty_note": today if empty_note else state.get("last_empty_note", ""),
    }
    print(json.dumps({
        "tz": tz_name, "tz_from_activity": tz_src,
        "local_now": local_now.strftime("%Y-%m-%d %H:%M"),
        "window_open": window_open, "cards": cards,
        "empty_note": empty_note, "state": new_state,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
