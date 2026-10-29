"""Read-only reservation catalog and rules shared by menu and booking core."""
from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo

from auto_grab import _req, require_success
from shape_adapter import parse_margin


def china_now():
    return datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)


def parse_times(value: str) -> list[str]:
    value = value.translate(str.maketrans({"：": ":", "–": "-", "—": "-", "；": ";"}))
    intervals = []
    for part in value.split(";"):
        match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", part)
        if not match:
            raise ValueError("时段应为 HH:MM-HH:MM，多段用分号分隔")
        sh, sm, eh, em = map(int, match.groups())
        if not (0 <= sh <= 23 and 0 <= eh <= 23 and 0 <= sm <= 59 and 0 <= em <= 59):
            raise ValueError("时间必须在 00:00-23:59 内")
        start, end = sh * 60 + sm, eh * 60 + em
        if end <= start:
            raise ValueError("结束时间必须晚于开始时间")
        intervals.append((start, end))
    intervals.sort()
    if any(previous[1] != current[0] for previous, current in zip(intervals, intervals[1:])):
        raise ValueError("多段预约必须连续且不能重复或重叠")
    return [f"{start//60:02d}:{start%60:02d}-{end//60:02d}:{end%60:02d}" for start, end in intervals]


def get_halls(cookie: str) -> list[dict]:
    response = _req("/site/reservation/hall", cookie=cookie)
    require_success(response)
    halls = response.get("d")
    if not isinstance(halls, list) or any(not isinstance(h.get("resources"), list) for h in halls):
        raise RuntimeError("预约大厅结构不认识，请在网页查看")
    return [hall for hall in halls if hall["resources"]]


def get_resource_info(cookie: str, venue: int) -> dict:
    response = _req("/site/reservation/resource-info", params={"resource_id": venue}, cookie=cookie)
    require_success(response)
    info = response.get("d")
    advanced = (info.get("json_advanced_rule") or {}) if isinstance(info, dict) else {}
    related = advanced.get("rule") or {}
    sports_payment = (str(advanced.get("mode")) == "1"
                      and str(related.get("related_id")) == "616"
                      and str(related.get("lock_mode")) == "2")
    if (not isinstance(info, dict) or str(info.get("id")) != str(venue)
            or str(info.get("calendar_type")) not in ("1", "2")
            or str(info.get("is_group", 0)) != "0"
            or (str(advanced.get("mode", 0)) != "0" and not sports_payment)):
        raise RuntimeError("此资源使用特殊预约流程，请打开预约大厅网页办理")
    if info.get("apply_status") is False:
        raise RuntimeError("当前账号没有此场馆的预约权限，请在网页查看")
    return info


def get_calendar(cookie: str, venue: int, day: str) -> list[dict]:
    response = _req("/site/reservation/resource-info-margin", cookie=cookie,
                    params={"resource_id": venue, "start_time": day, "end_time": day})
    require_success(response)
    slots = parse_margin(response, include_unavailable=True)
    if slots is None:
        raise RuntimeError("目标日期的排期结构不认识")
    return [slot for slot in slots if slot.get("date") == day]


def normal_rule(info: dict) -> dict:
    return (info.get("json_allow_range") or {}).get("normal") or {}


def selection_limit(info: dict) -> int:
    single = normal_rule(info).get("single") or {}
    if str(single.get("is_enable")) == "1":
        return max(1, int(single["number"]))
    return 1


def allowed_dates(info: dict, now=None) -> list[str]:
    now = now or china_now()
    rule = normal_rule(info)
    bounds = rule.get("range")
    if not isinstance(bounds, list) or len(bounds) < 2 or rule.get("type") not in ("d", "w"):
        raise ValueError("场馆的日期范围规则不认识，请在网页查看")
    multiplier = 7 if rule["type"] == "w" else 1
    first, last = int(bounds[0]) * multiplier, int(bounds[1]) * multiplier
    weekdays = rule.get("week", list(range(1, 8)))
    return [(now.date() + timedelta(days=offset)).isoformat()
            for offset in range(max(0, first), last + 1)
            if str((now.date() + timedelta(days=offset)).isoweekday()) in map(str, weekdays)]


def validate_selection(info: dict, times: list[str], day: str, *, now=None) -> None:
    normalized = parse_times(";".join(times))
    if len(normalized) > selection_limit(info):
        raise ValueError(f"该场馆单次最多选择 {selection_limit(info)} 个时段")
    if day not in allowed_dates(info, now):
        raise ValueError("日期不在场馆允许的预约范围内")


def release_time(info: dict) -> str:
    rule = normal_rule(info)
    limit = rule.get("day_limit") or {}
    clock = limit.get("time") if str(limit.get("is_enable")) == "1" else rule.get("rules_time")
    if isinstance(clock, str):
        try:
            return datetime.strptime(clock, "%H:%M:%S").strftime("%H:%M:%S")
        except ValueError:
            pass
    return ""
