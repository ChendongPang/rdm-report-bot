#!/usr/bin/env python3
"""
自动为"计划开始日期是上个月"的任务，按 估计工作量(小时)/8 算出的天数，
从上个月第一个工作日开始连续排期，逐日填写进展（完成率从 RATE_START 逐天
递增到该任务最后一天落在 (RATE_END_MIN, RATE_END_MAX) 之间的随机值，当日
投入工作量固定 8 小时，描述由大模型生成且互不相同），并提交保存。

流程:
  1. 登录
  2. 读取"我的任务"列表，筛选 计划开始日期 在上个月的任务
  3. 计算每个任务需要的工作日天数 = ceil(估计工作量/8)，任务之间日期连续排（不跳回月初），
     执行日期落在上个月内（自然不会超过今天），同时给每个任务生成一条递增的完成率序列
  4. 把整体计划存成 JSON（task_plan.json），打印出来供确认
  5. 确认后逐条填写: 执行日期 / 完成率(递增) / 当日投入工作量(8) / 描述(大模型生成) -> 点击"确定"
"""

import calendar
import datetime
import getpass
import json
import math
import os
import random
import re
import time

from playwright.sync_api import sync_playwright

from llm_helper import generate_daily_descriptions

URL = "https://imm.kmerit.com:64892/index.jsp"
PLAN_FILE = "task_plan.json"

REPORT_HOURS = "8"

# 完成率不再是固定值：每个任务从 RATE_START 开始，随着天数往上涨，涨到
# 这个任务最后一天时落在 (RATE_END_MIN, RATE_END_MAX) 之间的一个随机值
# （每个任务的终值不一样，看起来更像真实进度，而不是每天都写死同一个数）。
RATE_START = 5
RATE_END_MIN = 92
RATE_END_MAX = 96

# 安全阀：先设小一点跑通流程，确认没问题后再调大。MAX_ENTRIES=0 表示不限制，把计划里的都填完。
_max_entries_raw = int(os.getenv("MAX_ENTRIES", "1"))
MAX_ENTRIES_TO_SUBMIT = _max_entries_raw if _max_entries_raw > 0 else float("inf")

# 默认无头运行（不弹浏览器窗口）；本机想弹出浏览器窗口调试时设 HEADLESS=0
HEADLESS = os.getenv("HEADLESS", "1") != "0"


def log(msg: str):
    """带时间戳的进度输出，flush=True 保证不被缓冲住、能实时看到（尤其是输出被
    重定向/放到 Docker 里跑的时候）。"""
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------- 计划阶段 ----------

def previous_month(today: datetime.date) -> tuple[int, int]:
    first_of_this_month = today.replace(day=1)
    last_day_prev_month = first_of_this_month - datetime.timedelta(days=1)
    return last_day_prev_month.year, last_day_prev_month.month


def business_days_in_month(year: int, month: int, cap: datetime.date | None = None) -> list[datetime.date]:
    last_day = calendar.monthrange(year, month)[1]
    month_end = datetime.date(year, month, last_day)
    if cap is not None:
        month_end = min(month_end, cap)

    days = []
    d = datetime.date(year, month, 1)
    while d <= month_end:
        if d.weekday() < 5:  # 周一~周五
            days.append(d)
        d += datetime.timedelta(days=1)
    return days


def build_column_index(task_frame, td_count: int) -> dict[str, int]:
    """"我的任务"表格支持用户自定义显示哪些列、顺序怎么排（表头右键菜单能改），
    不同账号看到的列不一定一样，写死列号（比如"第5列是任务名称"）在别的账号上
    会直接错位取到别的字段。改成按表头 <th name="..."> 里的字段名（跟数据库
    字段名一致，比如 PlanStartDate/PlanEffort，不是显示文字）动态找列号。

    表头的 th.head-th 在页面里会出现两次（一行是每列上面的筛选输入框，一行是
    文字标签，两行列顺序完全一致），之前按"总数减去数据列数"硬猜该取哪一段，
    在列配置不同的账号上对不齐、直接漏掉 Name 这个字段。改成不猜位置：直接找
    "同一个 <tr> 里 th.head-th 数量正好等于数据行列数"的那一整行，按它的顺序
    建映射——这行是筛选行还是标签行不重要，两行顺序一样。
    """
    mapping = task_frame.evaluate(
        """(tdCount) => {
            const ths = Array.from(document.querySelectorAll('th.head-th'));
            const rows = new Map();
            for (const th of ths) {
                const tr = th.closest('tr');
                if (!tr) continue;
                if (!rows.has(tr)) rows.set(tr, []);
                rows.get(tr).push(th.getAttribute('name'));
            }
            for (const names of rows.values()) {
                if (names.length === tdCount) {
                    const idx = {};
                    names.forEach((n, i) => { if (n) idx[n] = i; });
                    return idx;
                }
            }
            return null;
        }""",
        td_count,
    )
    if mapping is None:
        raise RuntimeError(f"表头里没找到跟数据行列数一致（{td_count} 列）的那一行，可能页面结构变了")
    return mapping


def parse_tasks(task_frame) -> list[dict]:
    rows = task_frame.locator("#taskPanel .body-row")
    count = rows.count()
    if count == 0:
        return []

    td_count = rows.first.locator("td").count()
    col = build_column_index(task_frame, td_count)
    required = ("Name", "PlanStartDate", "PlanEffort")
    missing = [f for f in required if f not in col]
    if missing:
        raise RuntimeError(f"任务列表表头里没找到这些字段，可能是列配置变了，需要重新适配: {missing}")

    tasks = []
    for i in range(count):
        row = rows.nth(i)
        task_id = row.get_attribute("id")
        cells = row.locator("td")
        name = cells.nth(col["Name"]).inner_text().strip()
        plan_start_text = cells.nth(col["PlanStartDate"]).inner_text().strip()
        plan_effort_text = cells.nth(col["PlanEffort"]).inner_text().strip()

        plan_start = None
        if plan_start_text:
            try:
                plan_start = datetime.datetime.strptime(plan_start_text, "%Y-%m-%d").date()
            except ValueError:
                plan_start = None

        plan_effort = float(plan_effort_text) if plan_effort_text else 0.0

        tasks.append(
            {
                "task_id": task_id,
                "name": name,
                "plan_start": plan_start.isoformat() if plan_start else None,
                "plan_effort_hours": plan_effort,
            }
        )
    return tasks


def build_rate_sequence(n: int) -> list[int]:
    """给一个任务的 n 天生成递增的完成率序列：从 RATE_START 开始，随天数逐步
    涨到最后一天落在 (RATE_END_MIN, RATE_END_MAX) 之间的一个随机值（每个任务
    的终值不同，更像真实进度而不是每天都写死同一个数）。"""
    if n <= 0:
        return []
    end = random.randint(RATE_END_MIN, RATE_END_MAX)
    if n == 1:
        return [end]
    return [round(RATE_START + (end - RATE_START) * i / (n - 1)) for i in range(n)]


def build_plan(tasks: list[dict], today: datetime.date) -> list[dict]:
    target_year, target_month = previous_month(today)

    target_tasks = [
        t
        for t in tasks
        if t["plan_start"]
        and datetime.date.fromisoformat(t["plan_start"]).year == target_year
        and datetime.date.fromisoformat(t["plan_start"]).month == target_month
    ]

    business_days = business_days_in_month(target_year, target_month, cap=today)

    plan = []
    cursor = 0
    for t in target_tasks:
        days_needed = math.ceil(t["plan_effort_hours"] / 8) if t["plan_effort_hours"] > 0 else 0
        assigned = business_days[cursor : cursor + days_needed]
        cursor += len(assigned)

        plan.append(
            {
                "task_id": t["task_id"],
                "name": t["name"],
                "plan_effort_hours": t["plan_effort_hours"],
                "days_needed": days_needed,
                "assigned_dates": [d.isoformat() for d in assigned],
                "pending_days": days_needed - len(assigned),
                "rates": build_rate_sequence(len(assigned)),
            }
        )
    return plan


def print_plan(plan: list[dict]):
    print("\n===== 上个月任务排期计划 =====")
    if not plan:
        print("（没有找到计划开始日期在上个月的任务）")
        return
    for p in plan:
        print(f"- {p['name']}  估计工作量: {p['plan_effort_hours']}小时  需要 {p['days_needed']} 天")
        if p["assigned_dates"]:
            print(f"    分配日期: {', '.join(p['assigned_dates'])}")
        if p["rates"]:
            print(f"    完成率: {p['rates'][0]}% -> {p['rates'][-1]}%")
        if p["pending_days"] > 0:
            print(f"    ⚠ 上个月工作日不够分了，还有 {p['pending_days']} 天没排上，本次不会填写")
    print("=============================\n")


# ---------- 执行阶段 ----------

def wait_for_frame(page, url_fragment: str, task_id: str, timeout_ms: int = 15000):
    start = time.time()
    while (time.time() - start) * 1000 < timeout_ms:
        for fr in page.frames:
            if url_fragment in fr.url and task_id in fr.url:
                return fr
        page.wait_for_timeout(300)
    raise TimeoutError(f"等待 frame 超时: {url_fragment} / {task_id}")


def open_task(page, task_frame, task_id: str):
    row = task_frame.locator(f'tr.body-row[id="{task_id}"]')
    # 回到列表页后，行是逐个渲染出来的——只等"随便某一行出现"不够，这个具体
    # task_id 对应的行可能还没画出来，这时候数 td 数量会是 0，后面找表头直接
    # 找不到（"跟数据行列数一致（0 列）的那一行"）。这里专门等这一行本身出现。
    row.wait_for(state="attached", timeout=15000)
    cells = row.locator("td")
    # 跟 parse_tasks 一样，"任务名称"是哪一列因人而异（列可自定义配置），
    # 不能写死第 5 列，要动态查表头
    col = build_column_index(task_frame, cells.count())
    cells.nth(col["Name"]).locator("a").click()
    entity_frame = wait_for_frame(page, "entity.jsf", task_id)
    entity_frame.wait_for_selector("#operate_remark", timeout=15000)
    tab_frame = wait_for_frame(page, "entityTab.jsf", task_id)
    return entity_frame, tab_frame


def dismiss_confirm_dialog_if_present(page, timeout_ms: int = 5000) -> bool:
    """点击"确定"保存后，如果当日投入工作量达到上限等情况，会再弹出一个二次确认框
    （标题"提示"，内容含"一旦填写完成，则不能修改，请您仔细核实！当日已投入工作量X"），
    需要再点一次框内的"确定"才算真正提交。没弹出则直接跳过，不报错。

    弹窗里的"确定"必须在弹窗容器内部查找，不能在整个 frame 里找最后一个文本为
    "确定"的元素——如果弹窗不是新插入到 DOM 末尾（而是复用一个位置更靠前的固定
    容器），"取最后一个"可能会点错回原表单的确定按钮，导致弹窗其实没被确认。

    另外弹窗的"确定"/"取消"很可能和主表单一样是 <input type="button" value="确定">，
    而不是普通文字元素——input 的 value 不属于 textContent，get_by_text() 匹配不到，
    所以按钮定位要同时兼容"文字元素"和"input[value=...]"两种情况。
    """
    # 用 CSS 逗号选择器一次性兼容 input 按钮 / <button> / <a> / 纯文字元素几种写法
    ok_css = 'input[value="确定"], button:text-is("确定"), a:text-is("确定"), span:text-is("确定")'

    start = time.time()
    while (time.time() - start) * 1000 < timeout_ms:
        for fr in page.frames:
            try:
                hint = fr.get_by_text("一旦填写完成", exact=False).first
                if hint.count() == 0 or not hint.is_visible():
                    continue
                # 找到同时包含"确定"和"取消"按钮（input 或文字元素两种写法都算）的最近
                # 祖先元素，即弹窗的按钮区域/容器；不在整个 frame 里裸找，避免点回主表单按钮
                dialog = hint.locator(
                    "xpath=ancestor::*["
                    "(.//input[@value='确定'] or .//*[normalize-space(text())='确定'])"
                    " and "
                    "(.//input[@value='取消'] or .//*[normalize-space(text())='取消'])"
                    "][1]"
                )
                if dialog.count() == 0:
                    continue
                dialog.locator(ok_css).first.click()
                return True
            except Exception:
                continue
        page.wait_for_timeout(200)
    return False


def select_calendar_date(entity_frame, date_str: str):
    """真实点击日历控件选中目标日期，而不是绕过它——绕过控件会跳过它 onfocus 里
    `iCalendar.setDay($(this), "dataChange")` 触发的"按日期加载已有记录"逻辑，
    导致提交时没有正确绑定到"编辑这条已有记录"的状态。

    日历格子（#c_calendarDiv 下的 #c_body 表格）每个 <td> 的 title 属性就是完整
    日期，格式 "YYYY-M-D"（月、日都不补零），比如 title="2026-7-1"；class 用
    this-month / other-month / no-workday 区分当前月/相邻月溢出/非工作日。
    翻月份靠读 #c_year / #c_month 的当前显示值，跟目标年月比较后点"上一月"/
    "下一月"（<a title="上一月"|"下一月">）箭头翻到位。
    """
    # 之前跨 frame 搜 "#c_calendarDiv" 抓到的其实是另一个 frame 里同名但一直
    # display:none、没内容的空壳（iCalendar.js 这个共享控件脚本可能在好几个 frame
    # 里都加载了，各自建了一份模板）。截图证实真正弹出来的日历就渲染在
    # entity_frame 里 #report_action_date 旁边，所以直接锁定 entity_frame，不用
    # 再跨 frame 搜。
    target = datetime.date.fromisoformat(date_str)
    page = entity_frame.page

    entity_frame.locator("#report_action_date").click()

    cal = entity_frame.locator("#c_calendarDiv")
    try:
        cal.wait_for(state="visible", timeout=8000)
    except Exception:
        os.makedirs("screenshots", exist_ok=True)
        page.screenshot(path="screenshots/debug_calendar_year_timeout.png", full_page=True)
        raise

    year_el = entity_frame.locator("#c_year")
    month_el = entity_frame.locator("#c_month")
    year_el.filter(has_text=re.compile(r"\d")).first.wait_for(timeout=8000)

    for _ in range(36):  # 最多翻 3 年，防止死循环
        cur_year = int(year_el.inner_text().strip())
        cur_month = int(month_el.inner_text().strip())
        if (cur_year, cur_month) == (target.year, target.month):
            break
        # 日历头部(#c_ctrl_head)和底部(#c_ctrl_foot)各有一份翻月箭头，两边等效，取第一个即可
        if (cur_year, cur_month) < (target.year, target.month):
            entity_frame.locator('a[title="下一月"]').first.click()
        else:
            entity_frame.locator('a[title="上一月"]').first.click()
        page.wait_for_timeout(150)
    else:
        raise RuntimeError(f"翻月份翻不到 {target.year}-{target.month}")

    title_attr = f"{target.year}-{target.month}-{target.day}"
    cell = entity_frame.locator(f'#c_body td[title="{title_attr}"]')
    cell.first.click()
    page.wait_for_timeout(400)


def fill_and_submit(entity_frame, tab_frame, date_str: str, description: str, rate: int):
    # 先真实点日历选中目标日期（会触发控件自己的 dataChange，如果这天已有记录，
    # 通常这时完成率/描述就会被加载成旧值），再等一下给可能的异步加载留时间，
    # 然后把完成率/工时/描述覆盖成我们要的新值。
    select_calendar_date(entity_frame, date_str)
    entity_frame.page.wait_for_timeout(500)

    entity_frame.evaluate(
        """([rate, hours, remark]) => {
            const setVal = (sel, value) => {
                const el = document.querySelector(sel);
                if (!el) return;
                el.value = value;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            };
            setVal('#report_rate', rate);
            setVal('#report_in_work', hours);
            setVal('#operate_remark', remark);
            if (typeof dataChange === 'function') {
                try { dataChange(); } catch (e) {}
            }
        }""",
        [str(rate), REPORT_HOURS, description],
    )
    entity_frame.page.wait_for_timeout(200)

    # 点提交前先回读一遍，打印出来确认这一刻表单里真的是新值，不是被覆盖回旧值
    values = entity_frame.evaluate(
        """() => ({
            date: document.querySelector('#report_action_date')?.value,
            rate: document.querySelector('#report_rate')?.value,
            hours: document.querySelector('#report_in_work')?.value,
            remark: document.querySelector('#operate_remark')?.value,
        })"""
    )
    print(f"    提交前字段回读: {values}")

    page = tab_frame.page
    tab_frame.locator('input[onclick*="saveInstantce"]').click()
    dismiss_confirm_dialog_if_present(page)
    page.wait_for_timeout(800)


def go_back_to_task_list(page):
    # 页面里同一个 id 值出现了两处：一处是折叠在"个人空间"面板里、默认不可见的
    # <span id="...">；另一处是顶部导航栏里始终可见的 <span mid="...">我的任务</span>。
    # 应该点后者（用 mid 属性定位，再点里面真正的 <a> 链接）。
    page.click('span[mid="3b8cc05e-597e-4500-84e5-310a8b54d75b"] a')
    task_frame = page.frame(name="main")
    task_frame.wait_for_selector("#taskPanel .body-row", timeout=15000)
    # 表头（th.head-th，决定"第几列是任务名称"）是异步渲染的，经常比数据行晚
    # 出现——只等数据行会导致刚回到列表就立刻 open_task 时，build_column_index
    # 读到的表头还是空的/不全的，找不到 "Name" 这个字段（KeyError）。这里额外
    # 等一下表头里出现 name="Name" 的那一列，确保回列表页时表头也真的就绪了。
    task_frame.wait_for_selector('th.head-th[name="Name"]', timeout=15000)
    return task_frame


def main():
    username = input("用户名: ").strip()
    password = getpass.getpass("密码: ")

    today = datetime.date.today()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, args=["--ignore-certificate-errors"])
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        if not HEADLESS:
            # 尝试让浏览器窗口自动弹到前台；WSL/Windows 的焦点抢占保护可能仍会
            # 拦下来，不保证 100% 生效，但没有副作用
            try:
                page.bring_to_front()
            except Exception:
                pass

        log("正在打开登录页...")
        page.goto(URL, wait_until="networkidle", timeout=30000)
        page.fill("#userName", username)
        page.fill("#userPassword", password)
        log("正在登录...")
        with page.expect_navigation(wait_until="networkidle", timeout=30000):
            page.click("#loginBtn")
        log("登录成功")

        task_frame = page.frame(name="main")
        task_frame.wait_for_load_state("networkidle", timeout=15000)
        task_frame.wait_for_selector("#taskPanel .body-row", timeout=15000)
        task_frame.wait_for_selector('th.head-th[name="Name"]', timeout=15000)

        log("正在读取任务列表...")
        tasks = parse_tasks(task_frame)
        log(f"读取到 {len(tasks)} 条任务")

        with open("raw_tasks.json", "w", encoding="utf-8") as f:
            json.dump(tasks, f, ensure_ascii=False, indent=2)

        print(f"\n===== 任务列表原始数据（共 {len(tasks)} 条，今天是 {today.isoformat()}） =====")
        for t in tasks:
            print(f"- {t['name']} | 计划开始日期: {t['plan_start']} | 估计工作量: {t['plan_effort_hours']}小时")
        print("=====================================\n")

        plan = build_plan(tasks, today)

        with open(PLAN_FILE, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)
        print(f"计划已保存到 {PLAN_FILE}")

        print_plan(plan)

        total_entries = sum(len(p["assigned_dates"]) for p in plan)
        if total_entries == 0:
            print("没有需要填写的条目，结束。")
            browser.close()
            return

        limit_text = "不限制" if MAX_ENTRIES_TO_SUBMIT == float("inf") else f"{int(MAX_ENTRIES_TO_SUBMIT)} 条"
        confirm = input(f"共 {total_entries} 条待填写记录，本次最多执行 {limit_text}（MAX_ENTRIES 环境变量可调，0=不限制）。确认开始？(y/n): ")
        if confirm.strip().lower() != "y":
            print("已取消。")
            browser.close()
            return

        run_total = min(total_entries, MAX_ENTRIES_TO_SUBMIT)
        submitted = 0
        failed = []  # [(task_name, date_str, error), ...]
        for p in plan:
            if submitted >= MAX_ENTRIES_TO_SUBMIT:
                break
            if not p["assigned_dates"]:
                continue

            log(f"正在为《{p['name']}》生成 {len(p['assigned_dates'])} 条工作描述（调用大模型）...")
            descriptions = generate_daily_descriptions(p["name"], len(p["assigned_dates"]))
            p["descriptions"] = descriptions
            log("描述生成完成")

            for date_str, desc, rate in zip(p["assigned_dates"], descriptions, p["rates"]):
                if submitted >= MAX_ENTRIES_TO_SUBMIT:
                    break

                idx = submitted + 1
                print(f"\n>>> [{idx}/{run_total}] 填写任务《{p['name']}》 日期 {date_str}")
                print(f"    完成率: {rate}%")
                print(f"    描述: {desc}")

                # 批量跑（尤其是 MAX_ENTRIES=0 不限制）的时候，某一条偶发超时/卡顿
                # 不该让前面几十条已经提交成功的全部搭进去——失败重试一次，还不行
                # 就跳过这一条、记下来继续跑后面的，而不是让整个脚本崩掉。
                ok = False
                last_err = None
                for attempt in (1, 2):
                    try:
                        log(f"打开任务详情页...{'' if attempt == 1 else '（重试）'}")
                        entity_frame, tab_frame = open_task(page, task_frame, p["task_id"])

                        log(f"选择执行日期 {date_str}...")
                        log("填写完成率/工时/描述并提交...")
                        fill_and_submit(entity_frame, tab_frame, date_str, desc, rate)
                        log("提交完成")

                        page.wait_for_timeout(1500)
                        page.screenshot(
                            path=f"screenshots/submit_{p['task_id']}_{date_str}.png", full_page=True
                        )
                        ok = True
                        break
                    except Exception as e:
                        last_err = e
                        log(f"这条失败了: {e}")
                        # 出错时页面状态不明（可能卡在详情页/弹窗），先想办法回到列表页
                        # 再重试，不然重试大概率也是错的
                        try:
                            task_frame = go_back_to_task_list(page)
                        except Exception:
                            pass
                        page.wait_for_timeout(1000)

                task_frame = go_back_to_task_list(page)
                page.wait_for_timeout(800)  # 给页面一点稳定时间，避免立刻点下一条又超时

                if ok:
                    submitted += 1
                    log(f"进度: {submitted}/{run_total} 条已提交")
                else:
                    failed.append((p["name"], date_str, str(last_err)))
                    log(f"跳过这一条（{p['name']} {date_str}），继续下一条")

        with open(PLAN_FILE, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)

        print(f"\n本次共提交 {submitted} 条记录，已更新 {PLAN_FILE}")
        if failed:
            print(f"有 {len(failed)} 条重试后仍失败，需要手动检查/重跑：")
            for name, date_str, err in failed:
                print(f"  - 《{name}》 {date_str}：{err}")
        if not HEADLESS:
            input("按回车键关闭浏览器...")
        browser.close()


if __name__ == "__main__":
    main()
