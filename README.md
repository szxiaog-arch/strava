# strava-daka

Strava 每日打卡图的渲染脚本。由 Claude 的定时任务拉取执行,不在本地跑。

## 文件

| 文件 | 作用 |
|---|---|
| `daka.py` | 主逻辑:时区判定、去重、推送决策、调用渲染 |
| `template.html` | 卡片版式(深色主题,720px 宽,Strava 橙) |
| `shot.js` | Playwright 截图,2 倍图导出 PNG |

三个文件必须在同一目录。

## 调用方式

```
python3 daka.py acts.json state.json out
```

- `acts.json` — Strava `list_activities` 的返回数组(需 `include_polyline: true`)
- `state.json` — 上次运行状态,首次可传 `{}`
- `out` — 输出目录

stdout 打印 JSON:`cards`(PNG 路径列表)、`empty_note`(布尔)、`state`(回写用)、`tz` / `local_now`(调试)。

依赖:`pip install polyline reverse-geocode timezonefinder`
timezonefinder 缺失时自动退回兜底时区,不影响出图。

## 关键常量(在 daka.py 顶部)

| 常量 | 当前值 | 含义 |
|---|---|---|
| `WINDOW_OPEN_HOUR` | 10 | 当地时间几点后才推当天新活动 |
| `SAFETY_HOURS` | 6 | 活动挂了多久还没推就无视时区强推 |
| `EMPTY_NOTE_HOUR` | 21 | 当地时间几点后才发"休息日?"提醒 |
| `FALLBACK_TZ` | Asia/Hong_Kong | 全是室内活动时的兜底时区 |

时区由最近一条带 GPS 的活动反推,人在国外跑一次户外就自动跟上。

## 定时任务

Claude 定时任务每天 14:00 UTC(北京 22:00)触发一次,每次运行都重新 curl 这三个文件。

**改逻辑改这里,不要改任务 prompt。** 提交后下一次触发自动生效。

### 已知限制

cron 按固定 UTC 时刻触发,不跟随时区。人在国外时触发点会落在当地的非晚间时段,可能导致打卡图延迟一天、"休息日"提醒不触发。如需消除,把 `WINDOW_OPEN_HOUR` 和 `EMPTY_NOTE_HOUR` 都设为 0(代价是提醒可能在当地早晨发出)。
