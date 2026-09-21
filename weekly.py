#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Strava 跑步周报:按本地时区切周 + 去重 + 出图。

和 daka.py 同一套调用约定:
    python3 weekly.py acts.json state.json out
stdout 打印 JSON。只统计跑步(Run/TrailRun/VirtualRun)。

去重:state["weekly_sent"] 记已发过的周(周一日期)。手动触发过之后自动跑到,
不会重复发 —— 这是「如果手动触发过了就不重复发」的实现点。
"""
import json, os, subprocess, sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
FALLBACK_TZ = "Asia/Hong_Kong"
RUN_TYPES = ("Run", "TrailRun", "VirtualRun")
KEEP_WEEKS = 12
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def sm(a, k, d=0):
    return (a.get("summary") or {}).get(k) or d


def load_acts(path):
    d = json.load(open(path, encoding="utf-8"))
    if isinstance(d, dict):
        for k in ("activities", "data", "results"):
            if isinstance(d.get(k), list):
                return d[k]
        raise SystemExit(f"{path}: 找不到 activities 数组,键有 {list(d)}")
    return d


def start_point(act):
    poly = act.get("reduced_polyline")
    if not poly:
        return None
    try:
        import polyline
        pts = polyline.decode(poly)
        return pts[0] if pts else None
    except Exception:
        return None


def detect_tz(acts):
    for a in acts:
        pt = start_point(a)
        if not pt:
            continue
        try:
            from timezonefinder import TimezoneFinder
            tz = TimezoneFinder().timezone_at(lat=pt[0], lng=pt[1])
            if tz:
                return tz
        except Exception:
            break
    return FALLBACK_TZ


def fmt_dur(sec):
    sec = int(round(sec))
    h, m = sec // 3600, (sec % 3600) // 60
    return f"{h}:{m:02d}:{sec % 60:02d}" if h else f"{m}:{sec % 60:02d}"


def fmt_pace(sp):
    if not sp:
        return "—"
    m, s = int(sp // 60), int(round(sp % 60))
    if s == 60:
        m, s = m + 1, 0
    return f"{m}'{s:02d}\""


def collect(acts, start, end):
    out = []
    for a in acts:
        if (a.get("sport_type") or "") not in RUN_TYPES:
            continue
        sl = a.get("start_local") or ""
        if not sl:
            continue
        dt = datetime.fromisoformat(sl)
        if not (start <= dt <= end):
            continue
        km = sm(a, "distance") / 1000
        if km <= 0:
            continue
        mv = sm(a, "moving_time")
        out.append({"dt": dt, "km": km, "sec": mv,
                    "pace": mv / km if km else 0,
                    "elev": sm(a, "elevation_gain"),
                    "cal": sm(a, "total_calories")})
    out.sort(key=lambda x: x["dt"])
    return out


def render(runs, start, end, prev_km, outdir):
    total = sum(r["km"] for r in runs)
    tsec = sum(r["sec"] for r in runs)
    longest = max(runs, key=lambda r: r["km"])
    fastest = min(runs, key=lambda r: r["pace"])
    maxkm = longest["km"]

    rows = []
    for r in runs:
        pct = max(6, r["km"] / maxkm * 100)
        star = ' <span class="star">★ 最远</span>' if r is longest else ""
        rows.append(
            f'    <div class="item"><div class="item-top">'
            f'<span class="item-day">{WEEKDAYS[r["dt"].weekday()]} {r["dt"].month}/{r["dt"].day}</span>'
            f'<span class="item-km">{r["km"]:.2f} km</span>'
            f'<span class="item-t">{fmt_dur(r["sec"])}</span>'
            f'<span class="item-p">{fmt_pace(r["pace"])}/km{star}</span></div>'
            f'<div class="bar"><i style="width:{pct:.1f}%"></i></div></div>')

    stats = [("跑步次数", f'{len(runs)} <span class="u">次</span>'),
             ("总时长", fmt_dur(tsec)),
             ("平均配速", f'{fmt_pace(tsec / total)}<span class="u">/km</span>'),
             ("单次最远", f'{maxkm:.2f}<span class="u">km</span>'),
             ("最快配速", f'{fmt_pace(fastest["pace"])}<span class="u">/km</span>'),
             ("总爬升", f'{sum(r["elev"] for r in runs):.0f}<span class="u">m</span>')]
    stats_html = "\n".join(
        f'    <div class="stat"><div class="stat-label">{l}</div>'
        f'<div class="stat-value">{v}</div></div>' for l, v in stats)

    delta = ""
    if prev_km:
        d = total - prev_km
        col = "#f0854f" if d >= 0 else "#898781"
        delta = (f'较上周 <b style="color:{col}">{"▲" if d >= 0 else "▼"} {abs(d):.2f} km</b>'
                 f'<br><span style="opacity:.6">上周 {prev_km:.2f} km</span>')

    html = open(os.path.join(HERE, "template_week.html"), encoding="utf-8").read()
    for k, v in {
        "{{RANGE}}": f"{start.month}月{start.day}日 – {end.month}月{end.day}日",
        "{{WEEKNO}}": f"{start.year} 年第 {start.isocalendar()[1]} 周",
        "{{HERO_VALUE}}": f"{total:.2f}", "{{DELTA}}": delta,
        "{{ITEMS}}": "\n".join(rows), "{{STATS}}": stats_html,
        "{{WEEKTAG}}": f"第 {start.isocalendar()[1]} 周",
    }.items():
        html = html.replace(k, str(v))

    tmp = os.path.join(outdir, f"_week_{start:%Y%m%d}.html")
    png = os.path.join(outdir, f"strava-weekly-{start:%Y-%m-%d}.png")
    open(tmp, "w", encoding="utf-8").write(html)
    subprocess.run(["node", os.path.join(HERE, "shot.js"), tmp, png],
                   check=True, stdout=subprocess.DEVNULL)
    return png


def caption(runs, start, end, prev_km):
    total = sum(r["km"] for r in runs)
    tsec = sum(r["sec"] for r in runs)
    longest = max(runs, key=lambda r: r["km"])
    L = [f"🏃 跑步周报 · {start.month}月{start.day}日–{end.month}月{end.day}日", "",
         f"总距离 {total:.2f} km · {len(runs)} 次",
         f"总时长 {fmt_dur(tsec)}",
         f"平均配速 {fmt_pace(tsec / total)}/km",
         f"单次最远 {longest['km']:.2f} km（{WEEKDAYS[longest['dt'].weekday()]}）"]
    if prev_km:
        d = total - prev_km
        L.append(f"较上周 {'▲' if d >= 0 else '▼'} {abs(d):.2f} km")
    L += ["", "本周明细"]
    for r in runs:
        L.append(f"· {WEEKDAYS[r['dt'].weekday()]} {r['dt'].month}/{r['dt'].day}  "
                 f"{r['km']:.2f} km  {fmt_dur(r['sec'])}  {fmt_pace(r['pace'])}/km")
    return "\n".join(L)[:1020]


def merge_polylines(acts, path):
    """把第二份(只取最近几条、带 GPS 轨迹)的 polyline 并进主列表。

    为什么要拆两次调用:list_activities 的 include_polyline 是全有或全无,
    40 条全带轨迹约 41KB,一次灌进上下文会撞到每分钟输入 token 的限流
    (2026-09-21 实测两个 run 各卡了 10 分半)。可轨迹其实只有两处要用 ——
    判时区的「最近一条带 GPS 的活动」和当天要出卡那条 —— 其余只是用来数
    「本月第几次」。所以主列表不带轨迹,再用一份少量的补上。
    """
    if not path or not os.path.exists(path):
        return acts
    try:
        extra = load_acts(path)
    except Exception as e:
        print(f"[warn] polyline 补充文件读取失败,退回无 GPS: {e}", file=sys.stderr)
        return acts
    by_id = {str(a["id"]): a for a in acts}
    hit = 0
    for a in extra:
        poly = a.get("reduced_polyline")
        target = by_id.get(str(a.get("id")))
        if poly and target is not None and not target.get("reduced_polyline"):
            target["reduced_polyline"] = poly
            hit += 1
    print(f"[info] 从 {os.path.basename(path)} 补了 {hit} 条 GPS 轨迹", file=sys.stderr)
    return acts


def main():
    acts = load_acts(sys.argv[1])
    state = json.load(open(sys.argv[2], encoding="utf-8")) if os.path.exists(sys.argv[2]) else {}
    outdir = os.path.abspath(sys.argv[3] if len(sys.argv) > 3 else HERE)
    if len(sys.argv) > 4:
        acts = merge_polylines(acts, sys.argv[4])
    os.makedirs(outdir, exist_ok=True)

    now = datetime.now(timezone.utc)
    if os.environ.get("FAKE_UTC"):
        now = datetime.fromisoformat(os.environ["FAKE_UTC"]).replace(tzinfo=timezone.utc)
    tz_name = detect_tz(acts)
    try:
        from zoneinfo import ZoneInfo
        local_now = now.astimezone(ZoneInfo(tz_name))
    except Exception:
        tz_name, local_now = "UTC", now

    # 本地时区下「本周」= 周一 00:00 ~ 周日 23:59
    monday = (local_now - timedelta(days=local_now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    sunday = monday.replace(hour=23, minute=59, second=59) + timedelta(days=6)
    wk = monday.strftime("%Y-%m-%d")

    sent = list(state.get("weekly_sent", []))
    already = wk in sent
    runs = collect(acts, monday, sunday)
    prev = sum(r["km"] for r in collect(acts, monday - timedelta(days=7),
                                        sunday - timedelta(days=7))) or None

    card = None
    if not already and runs:
        try:
            card = {"png": render(runs, monday, sunday, prev, outdir),
                    "caption": caption(runs, monday, sunday, prev),
                    "count": len(runs),
                    "total_km": round(sum(r["km"] for r in runs), 2)}
            sent = (sent + [wk])[-KEEP_WEEKS:]
        except Exception as e:
            print(f"[error] 周报渲染失败: {e}", file=sys.stderr)

    print(json.dumps({
        "tz": tz_name, "local_now": local_now.strftime("%Y-%m-%d %H:%M"),
        "week": f"{wk} ~ {sunday:%Y-%m-%d}", "run_count": len(runs),
        "already_sent": already, "card": card,
        "state": {**state, "weekly_sent": sent},
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
