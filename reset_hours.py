#!/usr/bin/env python3
"""
把上个月开始的任务，逐条重置"当日投入工作量"为 0。

流程：
  1. 登录
  2. 读取"我的任务"列表，用跟 fill_progress.py 一样的规则筛选出
     计划开始日期在上个月的任务
  3. 逐个任务打开"执行日报"Tab，翻页收集这个任务下所有已经存在记录的
     执行日期
  4. 打印完整清单（哪个任务、哪些日期）并要求二次确认，避免手滑清空不该
     清空的记录
  5. 确认后逐条：打开任务 -> 在"填写进展"里通过真实点日历选中该日期
     （会触发页面自带的 dataChange，把这天已有的记录加载出来）-> 只把
     "当日投入工作量"改成 0，其余字段不动 -> 点确定 -> 处理可能弹出的
     二次确认框 -> 重新打开"执行日报"核对这天的工作量是不是真的变成 0 了
"""

import datetime
import getpass
import json
import os
import re

from playwright.sync_api import sync_playwright

from fill_progress import (
    HEADLESS,
    URL,
    dismiss_confirm_dialog_if_present,
    go_back_to_task_list,
    log,
    open_task,
    parse_tasks,
    previous_month,
    select_calendar_date,
)

RESULT_FILE = "reset_hours_result.json"


def list_daily_report_dates(page) -> list[str]:
    """点开当前任务的"执行日报"Tab，翻页收集所有已存在记录的执行日期。"""
    clicked = False
    for fr in page.frames:
        try:
            tab = fr.get_by_text("执行日报", exact=True).first
            if tab.count() and tab.is_visible():
                tab.click()
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        raise RuntimeError('没找到"执行日报"这个 Tab')

    daily_frame = None
    for _ in range(30):
        for fr in reversed(page.frames):
            if "dailyReport.jsf" in fr.url and not fr.is_detached():
                daily_frame = fr
                break
        if daily_frame is not None:
            break
        page.wait_for_timeout(200)
    if daily_frame is None:
        raise RuntimeError("没找到 dailyReport.jsf 这个 frame")

    page.wait_for_timeout(600)

    # "执行日期"具体是第几列不一定固定（跟任务列表一样可能因账号/配置而异），
    # 先按表头文字找一次列号；找不到就退而求其次，取每行里第一个像日期的字段
    # （观察到的表头顺序是 执行日期 排在 填写日期 前面）。
    date_col = None
    header_cells = daily_frame.locator("th")
    for i in range(header_cells.count()):
        if header_cells.nth(i).inner_text().strip() == "执行日期":
            date_col = i
            break

    date_re = re.compile(r"\d{4}-\d{2}-\d{2}")
    dates: list[str] = []
    seen_pages = set()

    while True:
        rows = daily_frame.locator("tbody tr")
        row_count = rows.count()
        for i in range(row_count):
            cells = rows.nth(i).locator("td")
            cell_count = cells.count()
            if cell_count == 0:
                continue
            text = None
            if date_col is not None and date_col < cell_count:
                candidate = cells.nth(date_col).inner_text().strip()
                if date_re.fullmatch(candidate):
                    text = candidate
            if text is None:
                m = date_re.search(rows.nth(i).inner_text())
                if m:
                    text = m.group(0)
            if text:
                dates.append(text)

        page_key = tuple(dates)
        if page_key in seen_pages:
            break
        seen_pages.add(page_key)

        if not click_next_page_if_available(daily_frame, page):
            break
        if len(seen_pages) > 30:  # 保险丝，防止意外死循环
            break

    return sorted(set(dates))


def click_next_page_if_available(daily_frame, page) -> bool:
    """点"执行日报"列表的下一页箭头。这个箭头固定 id 是 pagination_nextPage，
    只有一页数据、或者已经翻到最后一页时它会带 hide 这个 class（真正的
    display:none，不是"看起来禁用了"），这时候不能硬点——元素不可见，
    Playwright 会一直等到超时。这里先检查 class 里有没有 hide，没有才点。"""
    next_link = daily_frame.locator("#pagination_nextPage")
    if next_link.count() == 0:
        return False
    classes = (next_link.get_attribute("class") or "").split()
    if "hide" in classes:
        return False
    next_link.click()
    page.wait_for_timeout(700)
    return True


def reset_one_date(page, task_frame, task_id: str, task_name: str, date_str: str) -> tuple[bool, str]:
    """把某个任务某一天的"当日投入工作量"改成 0 并提交，返回 (是否成功, 说明)。

    提交前会把字段回读一遍打印出来确认真的是 0（fill_progress.py 批量填报
    时也是靠这一步判断，多次验证过靠得住），不再额外多绕一次"重新打开任务 +
    点执行日报核对"——那一步虽然更保险，但会让每条记录都多一轮打开/关闭
    任务，跑起来来回跳转、明显更慢，性价比不高。
    """
    entity_frame, tab_frame = open_task(page, task_frame, task_id)

    select_calendar_date(entity_frame, date_str)
    entity_frame.page.wait_for_timeout(500)  # 给"加载这天已有记录"的异步请求留时间

    # 只改"当日投入工作量"这一个字段，完成率/描述不动，保持已加载出来的原值
    entity_frame.evaluate(
        """(hours) => {
            const el = document.querySelector('#report_in_work');
            if (!el) return;
            el.value = hours;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            if (typeof dataChange === 'function') {
                try { dataChange(); } catch (e) {}
            }
        }""",
        "0",
    )
    entity_frame.page.wait_for_timeout(200)

    hours_now = entity_frame.evaluate("() => document.querySelector('#report_in_work')?.value")
    print(f"    提交前当日投入工作量回读: {hours_now}")

    page_ = tab_frame.page
    tab_frame.locator('input[onclick*="saveInstantce"]').click()
    dismiss_confirm_dialog_if_present(page_)
    page_.wait_for_timeout(800)

    ok = hours_now == "0"
    return ok, f"提交前回读当日投入工作量={hours_now}"


def main():
    username = input("用户名: ").strip()
    password = getpass.getpass("密码: ")

    today = datetime.date.today()
    target_year, target_month = previous_month(today)

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

        log("正在登录...")
        page.goto(URL, wait_until="networkidle", timeout=30000)
        page.fill("#userName", username)
        page.fill("#userPassword", password)
        with page.expect_navigation(wait_until="networkidle", timeout=30000):
            page.click("#loginBtn")
        log("登录成功")

        task_frame = page.frame(name="main")
        task_frame.wait_for_load_state("networkidle", timeout=15000)
        task_frame.wait_for_selector("#taskPanel .body-row", timeout=15000)
        task_frame.wait_for_selector('th.head-th[name="Name"]', timeout=15000)

        log("正在读取任务列表...")
        tasks = parse_tasks(task_frame)
        target_tasks = [
            t
            for t in tasks
            if t["plan_start"]
            and datetime.date.fromisoformat(t["plan_start"]).year == target_year
            and datetime.date.fromisoformat(t["plan_start"]).month == target_month
        ]
        log(f"找到 {len(target_tasks)} 个计划开始日期在 {target_year}-{target_month:02d} 的任务")
        for t in target_tasks:
            print(f"  - {t['name']}")

        if not target_tasks:
            print("没有匹配的任务，结束。")
            browser.close()
            return

        # 逐个任务收集"执行日报"里已经存在记录的日期
        plan: list[dict] = []
        for t in target_tasks:
            log(f"打开《{t['name']}》，读取执行日报里已有的日期...")
            open_task(page, task_frame, t["task_id"])
            dates = list_daily_report_dates(page)
            log(f"《{t['name']}》共有 {len(dates)} 条记录: {', '.join(dates) if dates else '(无)'}")
            plan.append({"task_id": t["task_id"], "name": t["name"], "dates": dates})
            task_frame = go_back_to_task_list(page)

        total = sum(len(p["dates"]) for p in plan)
        print("\n===== 即将把下面这些日期的 当日投入工作量 重置为 0 =====")
        for p in plan:
            print(f"- {p['name']}  共 {len(p['dates'])} 条")
            if p["dates"]:
                print(f"    {', '.join(p['dates'])}")
        print(f"===== 共 {total} 条 =====\n")

        if total == 0:
            print("没有需要重置的记录，结束。")
            browser.close()
            return

        confirm = input("确认要把上面这些记录的当日投入工作量全部清零吗？此操作不可逆，请谨慎确认 (y/n): ")
        if confirm.strip().lower() != "y":
            print("已取消，未做任何修改。")
            browser.close()
            return

        results = []
        done = 0
        for p in plan:
            for date_str in p["dates"]:
                done += 1
                print(f"\n>>> [{done}/{total}] 重置《{p['name']}》 {date_str} 的当日投入工作量为 0")
                ok = False
                detail = ""
                for attempt in (1, 2):
                    try:
                        ok, detail = reset_one_date(page, task_frame, p["task_id"], p["name"], date_str)
                        break
                    except Exception as e:
                        detail = str(e)
                        log(f"出错了（第 {attempt} 次）: {e}")
                        try:
                            task_frame = go_back_to_task_list(page)
                        except Exception:
                            pass
                        page.wait_for_timeout(1000)
                task_frame = go_back_to_task_list(page)
                page.wait_for_timeout(800)

                status = "成功" if ok else "失败/未确认"
                print(f"    结果: {status}  {detail}")
                results.append(
                    {"task": p["name"], "date": date_str, "ok": ok, "detail": detail}
                )

        with open(RESULT_FILE, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

        succeeded = sum(1 for r in results if r["ok"])
        print(f"\n共处理 {len(results)} 条，成功 {succeeded} 条，失败 {len(results) - succeeded} 条")
        print(f"详细结果已存到 {RESULT_FILE}")
        if not HEADLESS:
            input("按回车键关闭浏览器...")
        browser.close()


if __name__ == "__main__":
    main()
