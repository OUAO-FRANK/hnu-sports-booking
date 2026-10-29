"""Interactive, read-only choices; only a confirmed plan reaches auto.main."""
from datetime import datetime, timedelta
from decimal import Decimal

from auto_grab import BASE, load_cookie
from booking_catalog import (allowed_dates, china_now, get_calendar, get_halls,
    get_resource_info, parse_times, release_time, selection_limit, validate_selection)


class Cancelled(Exception):
    pass


def choose_booking(cookie: str, *, input_fn=None, output=print, now=None) -> list[str] | None:
    input_fn = input_fn or input
    now = now or china_now()

    def ask(prompt):
        answer = input_fn(prompt).strip()
        if answer.lower() in ("q", "quit", "退出"):
            raise Cancelled()
        return answer

    def pick(title, labels, *, zero=False):
        output("\n" + title)
        if zero:
            output("  0. 不限场地（默认，回车直接选择；多段必须同一块）")
        for index, label in enumerate(labels, 1):
            output(f"  {index}. {label}")
        while True:
            value = ask("场地编号 [回车=0，不限场地]（q 退出）：" if zero else "请输入编号（q 退出）：")
            if zero and not value:
                value = "0"
            if value.isdigit() and (0 if zero else 1) <= int(value) <= len(labels):
                return int(value) - 1
            output("编号无效，请重新选择")

    try:
        output("HNU 预约选择 · 确认前仅查询，不提交预约；任意步骤输入 q 退出")
        halls = get_halls(cookie)
        hall = next((h for h in halls if h.get("name") == "体育场馆个人预约"), None)
        if not hall:
            raise RuntimeError("当前账号没有可见的体育场馆个人预约入口")
        output("\n体育场馆个人预约")
        venues = hall["resources"]
        venue = venues[pick("选择场馆", [v["name"] for v in venues])]
        info = get_resource_info(cookie, int(venue["id"]))
        output(f"单次最多 {selection_limit(info)} 段；多段须连续且同场地。")
        days = allowed_dates(info, now)
        calendars = {}
        today = now.date().isoformat()
        if today in days:
            calendars[today] = get_calendar(cookie, int(venue["id"]), today)
            if not any(s.get("time") and datetime_start(today, s["time"]) > now
                       for s in calendars[today]):
                days.remove(today)
                output("今天已无未开场时段，已从日期选项中隐藏")
        if not days:
            raise RuntimeError("允许日期内已无可选择的未开场时段")
        tomorrow = (now.date() + timedelta(days=1)).isoformat()
        default_day = tomorrow if tomorrow in days else days[0]
        output("\n可选日期（不代表有空位）：")
        for index, value in enumerate(days, 1):
            output(f"  {index}. {value}" + ("（默认）" if value == default_day else ""))
        output(f"直接回车：选择默认日期 {default_day}")
        output(f"输入示例：1 或 {days[0]}，均选择 {days[0]}")
        while True:
            value = ask("预约日期（编号 / YYYY-MM-DD，回车选默认）：")
            day = default_day if not value else value
            if value.isdigit() and 1 <= int(value) <= len(days):
                day = days[int(value) - 1]
            if day in days:
                break
            output("日期不在允许范围内，请重新选择")
        cells = calendars[day] if day in calendars else get_calendar(cookie, int(venue["id"]), day)
        # Keep full periods so a user can wait for release or a cancellation.
        times = sorted({s["time"] for s in cells if s.get("time") and
            datetime_start(day, s["time"]) > now})
        if not times:
            raise RuntimeError("目标日期没有尚未开场的排期，未使用历史时段替代；请在网页核对")
        output("\n选择时段（输入下方编号，所选时段全部都要）：")
        for i, time in enumerate(times, 1):
            rows = [s for s in cells if s["time"] == time]
            left = sum(int(s.get("margin") or 0) for s in rows if s["available"])
            output(f"  {i}. {time}  当前可约余量 {left}")
        output(f"单段示例：输入 1，选择 {times[0]}")
        if selection_limit(info) == 1:
            output("本场馆最多选 1 段，只需输入一个编号。")
        else:
            pair = next((i for i in range(len(times) - 1)
                         if times[i].split("-")[1] == times[i + 1].split("-")[0]), None)
            if pair is not None:
                output(f"多段示例：输入 {pair+1},{pair+2}，同时预约 {times[pair]} 和 {times[pair+1]}")
            output("多段编号用逗号分隔，须连续且同一块场地，不会自动减少时段。")
        while True:
            try:
                indexes = [int(s.strip()) - 1 for s in ask("时段编号：").replace("，", ",").split(",")]
                if not indexes or len(set(indexes)) != len(indexes) or any(i < 0 or i >= len(times) for i in indexes):
                    raise ValueError("时段编号无效或重复")
                selected = parse_times(";".join(times[i] for i in indexes))
                validate_selection(info, selected, day, now=now)
                break
            except ValueError as exc:
                output(str(exc))
        matching = [s for s in cells if s["time"] in selected]
        court_sets = [{s["court"] for s in matching if s["time"] == time} for time in selected]
        courts = sorted(set.intersection(*court_sets))
        if not courts:
            raise RuntimeError("所选时段没有共同场地，请重新运行选择")
        court = ""
        if courts != [""]:
            chosen = pick("选择场地", courts, zero=True)
            court = courts[chosen] if chosen >= 0 else ""
        candidates = [s for s in matching if not court or s["court"] == court]
        prices = [[Decimal(str(s["price"])) for s in candidates if s["time"] == time
                   and s.get("price") is not None] for time in selected]
        if not all(prices):
            fee = "费用待服务端确认"
        else:
            low, high = sum(min(p) for p in prices), sum(max(p) for p in prices)
            fee = "免费（当前排期 0 元）" if high == 0 else f"预计 {low:.2f}–{high:.2f} 元"
        modes = ["立即开始查询 / 预约"]
        release = release_time(info)
        if release and datetime.fromisoformat(now.date().isoformat() + "T" + release) < datetime_start(day, selected[0]):
            modes.append(f"等待今天 {release} 放号（已过则立即开始）")
        mode = pick("选择启动方式", modes)
        args = ["--venue", str(venue["id"]), "--date", day, "--times", ";".join(selected)]
        args += ["--court", court] if court else []
        args += ["--at", release] if mode == 1 else ["--now"]
        output("\n请确认预约清单：")
        output(f"项目：{hall['name']} / {info['name']}\n日期：{day}\n时段：{'、'.join(selected)}")
        output(f"场地：{court or ('无须选择场地' if courts == [''] else '不限场号，多段同一块场地')}")
        output(f"费用：{fee}；最终以预约回执为准，免费不创建支付订单")
        output(f"方式：{modes[mode]}")
        if ask("确认后将可能创建真实预约。输入 yes 确认，其他输入取消：").lower() != "yes":
            output("已取消，未提交预约")
            return None
        return args
    except Cancelled:
        output("已退出，未提交预约")
        return None


def datetime_start(day, slot):
    return datetime.fromisoformat(day + "T" + parse_times(slot)[0].split("-")[0])


def main() -> int:
    cookie = load_cookie()
    if not cookie:
        print("没有登录凭据，请先运行 python cdp_cookie.py --wait 完成人工登录")
        return 1
    try:
        args = choose_booking(cookie)
    except (RuntimeError, ValueError, KeyError, TypeError) as exc:
        print(f"[×] 未开始预约：{exc}\n请在 {BASE}/v2/reserve/reserveHall 核对")
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\n已退出")
        return 0
    if args is None:
        return 0
    import auto
    try:
        return auto.main(args)
    except (Exception, KeyboardInterrupt) as exc:
        print(f"[×] 预约流程中断：{exc}；请先在网页核对预约和订单，避免重复提交")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
