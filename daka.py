#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Strava 打卡图:时区判定 + 推送决策 + 出图。

打卡口径(2026-09-20 合并版):**一天 = 一次打卡 = 一张卡**。
同一天的多项运动合并到一张卡上;当天晚些时候又同步上来新活动时,
输出 action="edit" 让调用方原地更新那张卡,而不是再发一张。
"""
import json, os, subprocess, sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
WINDOW_OPEN_HOUR = 10     # 当地时间几点之后开始推送当天的新活动
# 人工手动打卡时置 DAKA_FORCE=1 绕过上面的时间窗 —— 用户明确要卡,
# 就不该因为「当地还没到 10 点」把他挡回去。定时任务不设这个变量。
FORCE = os.environ.get("DAKA_FORCE") == "1"
# 云端投递(SendUserFile)没有「编辑已发消息」这回事,所以必须按天硬去重:
# 某天已经出过卡,当天后来的新活动只标记、不再出第二张。Telegram 那条链路
# 能原地改图,不需要这个开关。
DAY_ONCE = os.environ.get("DAKA_DAY_ONCE") == "1"
SAFETY_HOURS = 6          # 兜底:活动挂了这么久还没推,无视时区强推
EMPTY_NOTE_HOUR = 21      # 当地时间几点之后,若当天无活动发一句提示
FALLBACK_TZ = "Asia/Hong_Kong"
KEEP_DAY_MESSAGES = 14    # day_messages 只留最近这些天,避免无限膨胀
KEEP_PROCESSED = 80       # 必须 > acts.json 的条数,否则旧活动会被当新的重发一遍
MAX_BACKFILL_DAYS = 3     # 比这更早的日子不再补卡,只静默标记 —— 防止 state 一丢就刷屏

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


def fmt_pace(sec_per_km):
    m, s = int(sec_per_km // 60), int(round(sec_per_km % 60))
    if s == 60:
        m, s = m + 1, 0
    return f"{m}'{s:02d}\""


def sm(act, key, default=0):
    return (act.get("summary") or {}).get(key) or default


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


def day_place(acts):
    """合并卡的位置:优先取第一个有 GPS 的活动。
    直接用最后一项会把室内力量训练的 Indoor 盖掉户外跑的真实地点。"""
    for a in acts:
        if a.get("reduced_polyline"):
            return place_of(a)
    return place_of(acts[-1])


def day_key(act):
    return act["start_local"][:10]


def monthly_day_count(all_acts, day):
    """day 所在的月份里,截至 day(含)共有几个「有运动的日子」。

    按天计,不按活动条数 —— 一天练两次仍只算一次打卡。
    精度取决于 acts.json 是否覆盖整月;调用方应传足够长的列表。
    """
    ym = day[:7]
    days = {day_key(a) for a in all_acts if day_key(a)[:7] == ym and day_key(a) <= day}
    return max(len(days), 1)


def fill(tpl_name, mapping):
    html = open(os.path.join(HERE, tpl_name), encoding="utf-8").read()
    for k, v in mapping.items():
        html = html.replace(k, str(v))
    return html


def shoot(html, png, outdir, tag):
    tmp = os.path.join(outdir, f"_render_{tag}.html")
    open(tmp, "w", encoding="utf-8").write(html)
    # 截图子进程的 stdout 必须吃掉,否则会污染本脚本输出的 JSON
    subprocess.run(["node", os.path.join(HERE, "shot.js"), tmp, png],
                   check=True, stdout=subprocess.DEVNULL)
    return png


def render_single(act, outdir, monthly):
    dt = datetime.fromisoformat(act["start_local"])
    dist, mov = sm(act, "distance"), sm(act, "moving_time")
    sport = act.get("sport_type", "")
    stats = []

    if dist > 100:
        hero = (f"{dist/1000:.2f}", "公里", "距离 · DISTANCE")
        stats.append(("运动时间", fmt_dur(mov)))
        if sport in ("Ride", "VirtualRide"):
            if mov:
                stats.append(("平均速度", f"{dist/1000/(mov/3600):.1f}<span class='u'>km/h</span>"))
        elif mov:
            stats.append(("平均配速", f"{fmt_pace(mov/(dist/1000))}<span class='u'>/km</span>"))
        stats.append(("爬升", f"{round(sm(act,'elevation_gain'))}<span class='u'>m</span>"))
        if sm(act, "total_calories"):
            stats.append(("卡路里", f"{round(sm(act,'total_calories'))}<span class='u'>kcal</span>"))
        if sm(act, "avg_cadence"):
            c = sm(act, "avg_cadence")
            stats.append(("平均踏频", f"{round(c)}<span class='u'>rpm</span>") if sport in ("Ride", "VirtualRide")
                         else ("平均步频", f"{round(c*2)}<span class='u'>spm</span>"))
        if sm(act, "max_speed"):
            stats.append(("最大速度", f"{sm(act,'max_speed'):.1f}<span class='u'>m/s</span>"))
    else:
        hero = (str(int(round(mov / 60))), "分钟", "时长 · DURATION")
        stats.append(("运动时间", fmt_dur(mov)))
        if sm(act, "total_calories"):
            stats.append(("卡路里", f"{round(sm(act,'total_calories'))}<span class='u'>kcal</span>"))
        if sm(act, "elapsed_time") and sm(act, "elapsed_time") != mov:
            stats.append(("总耗时", fmt_dur(sm(act, "elapsed_time"))))

    stats_html = "\n".join(
        f'    <div class="stat"><div class="stat-label">{l}</div>'
        f'<div class="stat-value">{v}</div></div>' for l, v in stats[:6])
    effort = round(sm(act, "relative_effort"))

    html = fill("template.html", {
        "{{LOCATION}}": place_of(act), "{{BADGE}}": BADGES.get(sport, "💪 运动"),
        "{{NAME}}": act.get("name", ""),
        "{{DATETIME}}": f"{dt.year}年{dt.month}月{dt.day}日 · {WEEKDAYS[dt.weekday()]} {dt:%H:%M}",
        "{{HERO_VALUE}}": hero[0], "{{HERO_UNIT}}": hero[1], "{{HERO_LABEL}}": hero[2],
        "{{STATS}}": stats_html, "{{EFFORT}}": effort,
        "{{EFFORT_PCT}}": min(effort, 100), "{{MONTHLY}}": f"本月第 {monthly} 次",
    })
    png = os.path.join(outdir, f"strava-daka-{dt:%Y-%m-%d}.png")
    return shoot(html, png, outdir, f"{dt:%Y%m%d}")


def render_multi(acts, outdir, monthly):
    """同一天多项运动 → 一张合并卡。"""
    acts = sorted(acts, key=lambda a: a["start_local"])
    dt = datetime.fromisoformat(acts[0]["start_local"])
    dist = sum(sm(a, "distance") for a in acts)
    mov = sum(sm(a, "moving_time") or sm(a, "elapsed_time") for a in acts)
    cal = sum(sm(a, "total_calories") for a in acts)
    elev = sum(sm(a, "elevation_gain") for a in acts)
    effort = round(sum(sm(a, "relative_effort") for a in acts))

    rows = []
    for a in acts:
        at = datetime.fromisoformat(a["start_local"])
        d, m = sm(a, "distance"), sm(a, "moving_time")
        if d > 100:
            main = f"{d/1000:.2f} km"
            sub = f"{fmt_dur(m)} · {fmt_pace(m/(d/1000))}/km" if m else fmt_dur(m)
            if sm(a, "elevation_gain"):
                sub += f" · 爬升 {round(sm(a,'elevation_gain'))} m"
        else:
            main = fmt_dur(m or sm(a, "elapsed_time"))
            sub = "无距离记录"
        if sm(a, "total_calories"):
            sub += f" · {round(sm(a,'total_calories'))} kcal"
        rows.append(
            f'    <div class="item"><div class="item-top">'
            f'<span class="item-time">{at:%H:%M}</span>'
            f'<span class="item-name">{BADGES.get(a.get("sport_type",""), "💪 运动")} {a.get("name","")}</span>'
            f'<span class="item-main">{main}</span></div>'
            f'<div class="item-sub">{sub}</div></div>')

    stats = []
    if dist > 100:
        # 配速只用有距离的活动的时间 —— 把力量训练的时长算进来会得出荒谬数字
        run_mov = sum(sm(a, "moving_time") for a in acts if sm(a, "distance") > 100)
        if run_mov:
            stats.append(("跑步配速", f"{fmt_pace(run_mov/(dist/1000))}<span class='u'>/km</span>"))
    stats.append(("总时长", fmt_dur(mov)))
    if elev:
        stats.append(("总爬升", f"{round(elev)}<span class='u'>m</span>"))
    if cal:
        stats.append(("总卡路里", f"{round(cal)}<span class='u'>kcal</span>"))
    stats_html = "\n".join(
        f'    <div class="stat"><div class="stat-label">{l}</div>'
        f'<div class="stat-value">{v}</div></div>' for l, v in stats[:6])

    if dist > 100:
        hero = (f"{dist/1000:.2f}", "公里", "当日总距离 · TOTAL")
    else:
        hero = (str(int(round(mov / 60))), "分钟", "当日总时长 · TOTAL")

    html = fill("template_multi.html", {
        "{{LOCATION}}": day_place(acts), "{{BADGE}}": f"💪 今日 {len(acts)} 项",
        "{{NAME}}": f"{dt.month}月{dt.day}日 运动汇总",
        "{{DATETIME}}": f"{dt.year}年{dt.month}月{dt.day}日 · {WEEKDAYS[dt.weekday()]}",
        "{{HERO_VALUE}}": hero[0], "{{HERO_UNIT}}": hero[1], "{{HERO_LABEL}}": hero[2],
        "{{ITEMS}}": "\n".join(rows), "{{STATS}}": stats_html,
        "{{EFFORT}}": effort, "{{EFFORT_PCT}}": min(effort, 100),
        "{{MONTHLY}}": f"本月第 {monthly} 次",
    })
    png = os.path.join(outdir, f"strava-daka-{dt:%Y-%m-%d}.png")
    return shoot(html, png, outdir, f"{dt:%Y%m%d}")


def caption_of(acts, monthly):
    """完整数据写进 caption。

    图片走 Telegram 媒体 CDN,说明文字随消息本体走 API —— 两条路。
    图加载不出来时数据也一个不少。上限 1024 字符。
    """
    acts = sorted(acts, key=lambda a: a["start_local"])
    dt = datetime.fromisoformat(acts[0]["start_local"])
    head = f"{dt.month}月{dt.day}日 · {WEEKDAYS[dt.weekday()]}"
    if len(acts) == 1:
        a = acts[0]
        d, m = sm(a, "distance"), sm(a, "moving_time")
        L = [f'{BADGES.get(a.get("sport_type",""), "💪 运动")} {a.get("name","")}',
             f'{head} {datetime.fromisoformat(a["start_local"]):%H:%M}', ""]
        if d > 100:
            L += [f"距离 {d/1000:.2f} km", f"时间 {fmt_dur(m)}"]
            if m:
                L.append(f"配速 {fmt_pace(m/(d/1000))}/km")
            if sm(a, "elevation_gain"):
                L.append(f"爬升 {round(sm(a,'elevation_gain'))} m")
        else:
            L.append(f"时长 {fmt_dur(m or sm(a,'elapsed_time'))}")
        if sm(a, "total_calories"):
            L.append(f"卡路里 {round(sm(a,'total_calories'))} kcal")
        if sm(a, "relative_effort"):
            L.append(f"相对努力 {round(sm(a,'relative_effort'))}")
    else:
        L = [f"{head} · 今日 {len(acts)} 项", ""]
        for a in acts:
            at = datetime.fromisoformat(a["start_local"])
            d, m = sm(a, "distance"), sm(a, "moving_time")
            L.append(f'{at:%H:%M} {BADGES.get(a.get("sport_type",""), "💪 运动")} {a.get("name","")}')
            if d > 100:
                L.append(f'     {d/1000:.2f} km · {fmt_dur(m)}'
                         + (f' · {fmt_pace(m/(d/1000))}/km' if m else ''))
            else:
                L.append(f'     {fmt_dur(m or sm(a,"elapsed_time"))}')
        L.append("")
        tot = []
        dist = sum(sm(a, "distance") for a in acts)
        if dist > 100:
            tot.append(f"总距离 {dist/1000:.2f} km")
        tot.append(f'总时长 {fmt_dur(sum(sm(a,"moving_time") or sm(a,"elapsed_time") for a in acts))}')
        cal = sum(sm(a, "total_calories") for a in acts)
        if cal:
            tot.append(f"总卡路里 {round(cal)} kcal")
        L.append(" · ".join(tot))
    L += ["", f"📍 {day_place(acts)}", f"本月第 {monthly} 次"]
    return "\n".join(L)[:1020]


def load_acts(path):
    """MCP 的 list_activities 返回 {"activities": [...]},直接 dump 会是对象;
    有些调用方又会先剥成裸数组。两种都收,省得每次运行都在这里翻车。"""
    d = json.load(open(path, encoding="utf-8"))
    if isinstance(d, dict):
        for k in ("activities", "data", "results"):
            if isinstance(d.get(k), list):
                return d[k]
        raise SystemExit(f"{path}: 是对象但找不到 activities 数组,键有 {list(d)}")
    return d


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
    day_messages = dict(state.get("day_messages", {}))

    fresh = [a for a in acts if str(a["id"]) not in processed]
    for a in fresh:
        pending.setdefault(str(a["id"]), now_utc.isoformat())
    for k in list(pending):                              # 清掉已推送的残留
        if k in processed:
            pending.pop(k)

    days_sent = state.get("days_sent")
    if days_sent is None:
        # 老状态没有这个字段。直接当成空会让「升级当天」漏掉去重 ——
        # 用已推送过的活动反推出哪些天出过卡,一次性补上。
        days_sent = sorted({day_key(a) for a in acts if str(a["id"]) in processed})
    days_sent = list(days_sent)
    window_open = FORCE or local_now.hour >= WINDOW_OPEN_HOUR

    # 未推送的活动按「本地日期」分组 —— 一天一张卡
    days = {}
    for a in fresh:
        seen = datetime.fromisoformat(pending[str(a["id"])])
        stale = (now_utc - seen) >= timedelta(hours=SAFETY_HOURS)
        if window_open or stale:                         # 时区判错也不会漏
            days.setdefault(day_key(a), []).append(a)

    cards = []
    cutoff = (local_now.date() - timedelta(days=MAX_BACKFILL_DAYS)).isoformat()
    for day in sorted(days):
        if day < cutoff:
            # state 丢失或 acts.json 变长时,不要把几个月的历史一次性全推出去
            for a in days[day]:
                processed.add(str(a["id"]))
                pending.pop(str(a["id"]), None)
            print(f"[skip] {day} 早于回填窗口({MAX_BACKFILL_DAYS} 天),只标记不推送", file=sys.stderr)
            continue
        if DAY_ONCE and day in days_sent:
            for a in days[day]:
                processed.add(str(a["id"]))
                pending.pop(str(a["id"]), None)
            print(f"[skip] {day} 当天已出过卡(DAY_ONCE),新活动只标记不推送", file=sys.stderr)
            continue
        # 这一天的全部活动(含已推送过的),这样补图时卡片是完整的一天
        same_day = sorted([a for a in acts if day_key(a) == day],
                          key=lambda x: x["start_local"])
        monthly = monthly_day_count(acts, day)
        try:
            png = (render_single(same_day[0], outdir, monthly) if len(same_day) == 1
                   else render_multi(same_day, outdir, monthly))
        except Exception as e:
            print(f"[error] render failed for {day}: {e}", file=sys.stderr)
            continue
        mid = day_messages.get(day)
        cards.append({
            "day": day,
            "png": png,
            "caption": caption_of(same_day, monthly),
            "action": "edit" if mid else "send",
            "message_id": mid,
            "count": len(same_day),
            "activity_ids": [str(a["id"]) for a in same_day],
        })
        days_sent.append(day)
        for a in days[day]:
            processed.add(str(a["id"]))
            pending.pop(str(a["id"]), None)

    today = local_now.strftime("%Y-%m-%d")
    empty_note = (not cards and not fresh
                  and local_now.hour >= EMPTY_NOTE_HOUR
                  and state.get("last_empty_note") != today)

    new_state = {
        "processed_ids": sorted(processed, key=int)[-KEEP_PROCESSED:],
        "pending": pending,
        "day_messages": {k: v for k, v in sorted(day_messages.items())[-KEEP_DAY_MESSAGES:]},
        "days_sent": sorted(set(days_sent))[-KEEP_DAY_MESSAGES:],
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
