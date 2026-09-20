# strava-daka

Strava 每日打卡图的渲染脚本。由 Claude 的定时任务拉取执行,不在本地跑。

**打卡口径:一天 = 一次打卡 = 一张卡。** 同一天的多项运动合并到一张卡上;
当天晚些时候又同步上来新活动时,更新原来那张卡,而不是再发一张。

## 文件

| 文件 | 作用 |
|---|---|
| `daka.py` | 主逻辑:时区判定、按天聚合、推送决策、调用渲染、生成 caption |
| `template.html` | 单项运动的卡片版式(深色主题,720px 宽,Strava 橙) |
| `template_multi.html` | 同日多项运动的合并版式,多一段「今日明细」列表 |
| `shot.js` | Playwright 截图,2 倍图导出 PNG |

四个文件必须在同一目录。

## 调用方式

```
python3 daka.py acts.json state.json out
```

- `acts.json` — Strava `list_activities` 的返回数组(需 `include_polyline: true`)。
  **要覆盖到当月月初**,否则「本月第 N 次」会偏小。建议至少取 40 条。
- `state.json` — 上次运行状态,首次可传 `{}`
- `out` — 输出目录

stdout 打印 JSON。

## 输出契约

```jsonc
{
  "tz": "Asia/Shanghai",
  "local_now": "2026-09-19 22:00",
  "window_open": true,
  "cards": [
    {
      "day": "2026-09-19",        // 本地日期,一天一张
      "png": "/abs/path.png",
      "caption": "…",             // 完整数据,直接当 caption 发
      "action": "send",           // "send" 新发 | "edit" 原地更新
      "message_id": null,         // action=edit 时是要更新的那条消息
      "count": 2,                 // 当天合并了几项运动
      "activity_ids": ["…", "…"]
    }
  ],
  "empty_note": false,
  "state": { … }                  // 回写用
}
```

### 调用方必须做的两件事

1. **按 `action` 分支**
   - `send` → `sendPhoto`,**把返回的 `message_id` 写进 `state.day_messages[day]`**
   - `edit` → `editMessageMedia`(带上 `caption`),`state` 不用改
2. **`edit` 遇到 `Bad Request: message is not modified` 要当成功**
   这表示卡片已是最新,是幂等结果。**绝不能退回新发一张**,否则同一天会冒出第二张卡,
   违背「一天一次打卡」。只有消息被删、或超过 48 小时不可编辑时,才退回 `sendPhoto`。

`daka.py` 自己不发消息,也拿不到新消息的 id,所以 `day_messages` 的回写只能由调用方完成。

## 为什么 caption 要带完整数据

图片走 Telegram 媒体 CDN,说明文字随消息本体走 API —— 两条不同的路。
实测有过图片一直转圈下不来、但文字正常到达的情况。把数据写进 caption,
图加载不出来时信息也不丢。上限 1024 字符,当前用量远低于。

## 关键常量(在 daka.py 顶部)

| 常量 | 当前值 | 含义 |
|---|---|---|
| `WINDOW_OPEN_HOUR` | 10 | 当地时间几点后才推当天新活动 |
| `SAFETY_HOURS` | 6 | 活动挂了多久还没推就无视时区强推 |
| `EMPTY_NOTE_HOUR` | 21 | 当地时间几点后才发「休息日?」提醒 |
| `FALLBACK_TZ` | Asia/Hong_Kong | 全是室内活动时的兜底时区 |
| `KEEP_DAY_MESSAGES` | 14 | `day_messages` 只留最近多少天 |

时区由最近一条带 GPS 的活动反推,人在国外跑一次户外就自动跟上。

## 几个容易改错的地方

- **合并卡的配速只能用「有距离的活动」的时间**。把力量训练的时长算进总时长再除以跑步距离,
  会得出荒谬的数字(实测 6'27" 被算成 10'30")。
- **合并卡的地点取第一个有 GPS 的活动**。直接用最后一项,会被室内力量训练的
  「室内 · Indoor」盖掉户外跑的真实地点。
- **「本月第 N 次」按天计,不按活动条数**。一天练两次仍然只算一次。

依赖:`pip install polyline reverse-geocode timezonefinder`、`npm i playwright`
timezonefinder 缺失时自动退回兜底时区,不影响出图。
