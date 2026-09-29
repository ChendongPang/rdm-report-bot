#!/usr/bin/env python3
"""RDM 自动填报 Web 控制台。

用法：python webui.py，浏览器自动打开 http://127.0.0.1:8765 。
在网页表单里填 RDM 账号密码、月份、请假日期、安全阀、大模型接口和工作日志，
点"开始"运行；跑的是 fill_progress.main() 原有逻辑：登录 → 生成排期 →
网页上确认（对应命令行的 y/n）→ 逐条提交，实时日志滚动显示。

- 仅监听 127.0.0.1，局域网访问不到；密码只在内存里中转一次，不落盘。
- WEBUI_PORT 环境变量可改端口；WEBUI_NO_BROWSER=1 时不自动开浏览器。
"""

import datetime
import io
import json
import os
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import openpyxl

import llm_helper
from fill_progress import RunParams, main as run_fill

# PyInstaller 打包后的 exe 里，资源会被解压到 sys._MEIPASS，但用户配置文件
# （work_log.xlsx、task_plan.json 等）应该放在 exe 同级目录，所以要切到那里。
if getattr(sys, "frozen", False):
    os.chdir(os.path.dirname(sys.executable))

HOST = "127.0.0.1"
PORT = int(os.getenv("WEBUI_PORT", "8765"))
CONFIRM_TIMEOUT_SEC = 600  # 排期确认页最多等 10 分钟，超时自动取消并关浏览器


def _playwright_browser_path() -> str | None:
    """检查 Playwright 的 Chromium 二进制是否已经下载到本地缓存目录，存在就返回
    路径，否则返回 None。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
        bp = os.path.join(base, "ms-playwright")
        subdir, exe_name = "chrome-win", "chrome.exe"
    elif sys.platform == "darwin":
        bp = os.path.expanduser("~/Library/Caches/ms-playwright")
        subdir, exe_name = "chrome-mac", "Chromium"
    else:
        bp = os.path.expanduser("~/.cache/ms-playwright")
        subdir, exe_name = "chrome-linux", "chrome"
    if not os.path.isdir(bp):
        return None
    for d in os.listdir(bp):
        if d.startswith("chromromium-"):
            exe = os.path.join(bp, d, subdir, exe_name)
            if os.path.isfile(exe):
                return exe
    return None


def ensure_playwright_chromium(log) -> bool:
    """确保 Playwright Chromium 已经下载到本地：已存在则秒过，不存在则调用
    `playwright install chromium` 自动下载（exe 模式下首次运行会触发，约 150MB）。"""
    if _playwright_browser_path():
        return True
    log("首次运行：正在下载 Chromium 浏览器（约 150MB），请稍候 ...")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        log("Chromium 下载完成")
        return True
    except Exception as exc:
        log(f"Chromium 下载失败：{exc}")
        return False


# ---------- 日志捕获：终端和网页同时可见 ----------

class _LineBuffer:
    """把一次 print 拆成的多次 write() 攒成整行再交给 sink。"""

    def __init__(self, sink):
        self._sink = sink
        self._buf = ""
        self._lock = threading.Lock()

    def write(self, s):
        with self._lock:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    self._sink(line.rstrip())

    def flush(self):
        with self._lock:
            if self._buf.strip():
                self._sink(self._buf.rstrip())
            self._buf = ""


class _Tee(io.TextIOBase):
    """stdout 双写：原终端 + 行缓冲 sink。worker 线程运行期间替换 sys.stdout。"""

    def __init__(self, original, line_sink):
        self._original = original
        self._lines = _LineBuffer(line_sink)

    def write(self, s):
        try:
            self._original.write(s)
        except Exception:
            pass
        self._lines.write(s)
        return len(s)

    def flush(self):
        try:
            self._original.flush()
        except Exception:
            pass
        self._lines.flush()

    def isatty(self):
        return False


# ---------- 运行会话（同一时间只允许一个任务在跑） ----------

class Session:
    """状态机：idle → running → (awaiting_confirm → running)* → done / error。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = "idle"
        self.logs: list[str] = []
        self.error_msg = ""
        self.confirm = None  # awaiting_confirm 时：{plan, total, limit_text}
        self._answered = threading.Event()
        self._confirmed = False
        # "实时画面"：fill_progress 每步推来的 JPEG 截图，前端按 shot_seq 增量拉取
        self._shot_lock = threading.Lock()
        self._shot_bytes: bytes | None = None
        self.shot_seq = 0

    # ---- 日志 ----

    def add_log(self, line: str):
        with self.lock:
            self.logs.append(line)
            if len(self.logs) > 5000:
                self.logs = self.logs[-4000:]

    def snapshot(self, since: int) -> dict:
        with self.lock:
            snap = {
                "state": self.state,
                "error": self.error_msg,
                "logs": self.logs[since:],
                "next": len(self.logs),
                "confirm": self.confirm,
            }
        with self._shot_lock:
            snap["shot_seq"] = self.shot_seq
        return snap

    def set_shot(self, data: bytes):
        with self._shot_lock:
            self._shot_bytes = data
            self.shot_seq += 1

    def get_shot(self) -> bytes | None:
        with self._shot_lock:
            return self._shot_bytes

    # ---- 排期确认（fill_progress 在 worker 线程里回调） ----

    def confirm_callback(self, plan: list[dict], total_entries: int, limit_text: str) -> bool:
        with self.lock:
            self.state = "awaiting_confirm"
            self.confirm = {
                "plan": plan,
                "total": total_entries,
                "limit_text": limit_text,
            }
        self._answered.clear()
        self._confirmed = False
        ok = self._answered.wait(CONFIRM_TIMEOUT_SEC)
        with self.lock:
            self.state = "running"
            self.confirm = None
        if not ok:
            self.add_log(
                f"[webui] 确认超时（{CONFIRM_TIMEOUT_SEC // 60} 分钟无响应），自动取消本次运行"
            )
            return False
        return self._confirmed

    def answer_confirm(self, ok: bool) -> bool:
        with self.lock:
            if self.state != "awaiting_confirm":
                return False
        self._confirmed = ok
        self._answered.set()
        self.add_log("[webui] 用户" + ("确认开始提交" if ok else "已取消本次运行"))
        return True

    # ---- 启动 / worker ----

    def busy(self) -> bool:
        return self.state in ("running", "awaiting_confirm")

    def start(self, params: RunParams):
        with self.lock:
            if self.busy():
                return False, "已有任务在运行，请等它结束再开始新的"
            self.state = "running"
            self.logs = []
            self.error_msg = ""
            self.confirm = None
        with self._shot_lock:
            self._shot_bytes = None
            self.shot_seq += 1
        t = threading.Thread(target=self._worker, args=(params,), daemon=True, name="rdm-worker")
        t.start()
        return True, ""

    def _worker(self, params: RunParams):
        old_stdout = sys.stdout
        sys.stdout = _Tee(old_stdout, self.add_log)
        try:
            self.add_log(
                f"[webui] 开始运行：填写 {params.month} 月（今天 {datetime.date.today():%Y-%m-%d}）"
            )
            # 首次运行 exe 时，Chromium 不存在则拉一次装（普通 python 环境已装好）
            if not ensure_playwright_chromium(self.add_log):
                with self.lock:
                    self.state = "error"
                    self.error_msg = "Chromium 浏览器下载失败，请检查网络后重试"
                return
            run_fill(params=params, confirm_fn=self.confirm_callback, shot_hook=self.set_shot)
            with self.lock:
                self.state = "done"
            self.add_log("[webui] 运行结束")
        except Exception as e:
            with self.lock:
                self.state = "error"
                self.error_msg = str(e)
            self.add_log(f"[webui] 运行出错：{e}")
        finally:
            sys.stdout = old_stdout


session = Session()


# ---------- 工作日志读写（与命令行共用 work_log.xlsx / work_log.md） ----------

def worklog_path() -> str:
    """日志文件路径：优先用已存在的候选（WORK_LOG 环境变量 > work_log.xlsx >
    work_log.md）；都不存在时用第一个候选（WORK_LOG 指定了就新建它，而不是
    错误地回落到 work_log.xlsx 把别的日志写花）。"""
    candidates = llm_helper._work_log_candidates()
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[0]


def read_worklog() -> dict:
    path = worklog_path()
    if not os.path.exists(path):
        return {"path": path, "rows": []}
    if path.lower().endswith(".xlsx"):
        return {"path": path, "rows": llm_helper._read_xlsx_rows(path)}
    # md：把实质行还原成两列（表格行拆列，自由文本行整行当内容）
    rows = []
    for line in llm_helper._work_log_substance(path):
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            a = cells[0] if cells else ""
            b = cells[1] if len(cells) > 1 else ""
            rows.append([a, b])
        else:
            rows.append(["", line])
    return {"path": path, "rows": rows}


def save_worklog(rows: list) -> dict:
    cleaned = []
    for r in rows or []:
        a = (r[0] if len(r) > 0 else "") or ""
        b = (r[1] if len(r) > 1 else "") or ""
        a, b = str(a).strip(), str(b).strip()
        if a or b:
            cleaned.append([a, b])
    path = worklog_path()
    try:
        if path.lower().endswith(".xlsx"):
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "工作日志"
            ws.append(["日期段", "工作内容"])
            for a, b in cleaned:
                ws.append([a, b])
            ws.column_dimensions["A"].width = 18
            ws.column_dimensions["B"].width = 60
            wb.save(path)
        else:
            lines = ["| 日期段 | 工作内容 |", "|---|---|"]
            lines += [f"| {a or ' '} | {b or ' '} |" for a, b in cleaned]
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
    except PermissionError:
        raise RuntimeError(f"{path} 被占用——多半正开着 Excel/编辑器，关掉再保存")
    return {"path": path, "rows": len(cleaned)}


def llm_config() -> dict:
    key = llm_helper.API_KEY
    if key:
        masked = key if len(key) <= 8 else key[:3] + "***" + key[-4:]
    else:
        masked = ""
    return {
        "url": llm_helper.URL,
        "model": llm_helper.MODEL,
        "key_set": bool(key),
        "key_masked": masked,
    }


# ---------- HTTP 服务 ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "RdmWebUI/1.0"

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def log_message(self, fmt, *args):  # 关掉默认的访问日志噪音
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            self._send(200, PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif u.path == "/api/poll":
            q = parse_qs(u.query)
            try:
                since = int(q.get("since", ["0"])[0])
            except ValueError:
                since = 0
            self._json(session.snapshot(max(0, since)))
        elif u.path == "/api/llm-config":
            self._json(llm_config())
        elif u.path == "/api/worklog":
            self._json(read_worklog())
        elif u.path == "/api/screenshot":
            data = session.get_shot()
            if not data:
                self._send(404, b"no screenshot yet", "text/plain; charset=utf-8")
            else:
                self._send(200, data, "image/jpeg")
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._json({"error": "请求体不是合法 JSON"}, 400)

        if u.path == "/api/start":
            self._start(body)
        elif u.path == "/api/confirm":
            if session.answer_confirm(bool(body.get("ok"))):
                self._json({"ok": True})
            else:
                self._json({"ok": False, "error": "当前不在等待确认状态"}, 409)
        elif u.path == "/api/worklog":
            try:
                self._json({"ok": True, **save_worklog(body.get("rows", []))})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        else:
            self._json({"error": "not found"}, 404)

    def _start(self, body: dict):
        if session.busy():
            return self._json({"ok": False, "error": "已有任务在运行"}, 409)

        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        if not username or not password:
            return self._json({"ok": False, "error": "用户名和密码不能为空"}, 400)

        try:
            month = int(body.get("month", 0))
            if not 1 <= month <= 12:
                raise ValueError
        except (TypeError, ValueError):
            return self._json({"ok": False, "error": "月份必须是 1-12"}, 400)

        try:
            max_entries = int(body.get("max_entries", 1))
            if max_entries < 0:
                raise ValueError
        except (TypeError, ValueError):
            return self._json({"ok": False, "error": "安全阀必须是 >=0 的整数（0=不限制）"}, 400)

        leave_raw = str(body.get("leave_dates", "")).strip()
        try:  # 预校验请假日期，别等浏览器登录完才发现格式错
            from fill_progress import parse_leave_dates, resolve_target_month
            y, m = resolve_target_month(datetime.date.today(), month)
            parse_leave_dates(y, m, leave_raw)
        except ValueError as e:
            return self._json({"ok": False, "error": f"请假日期格式有误：{e}"}, 400)

        # LLM 三项：留空 = 沿用当前配置（环境变量或上次运行设的）
        llm_url = str(body.get("llm_url", "")).strip() or None
        llm_key = str(body.get("llm_api_key", "")).strip() or None
        llm_model = str(body.get("llm_model", "")).strip() or None
        if llm_url is None and llm_key is None and llm_model is None:
            llm_url = llm_key = llm_model = None  # 全空：不调 configure，沿用现状

        params = RunParams(
            username=username,
            password=password,
            month=month,
            leave_dates_raw=leave_raw,
            max_entries=max_entries,
            headless=bool(body.get("headless", True)),
            llm_url=llm_url,
            llm_api_key=llm_key,
            llm_model=llm_model,
        )
        ok, err = session.start(params)
        self._json({"ok": ok, "error": err}, 200 if ok else 409)


# ---------- 前端单页（原生 HTML/CSS/JS，无外部依赖，内网也能用） ----------

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RDM 自动填报控制台</title>
<style>
:root{
  --bg:#f3f5f9; --card:#fff; --line:#e3e8ef; --text:#1c2433; --muted:#6b7688;
  --primary:#2f6fed; --primary-h:#245bd4; --ok:#1f9d55; --warn:#c2790b; --err:#d24343;
  --term-bg:#10141c; --term-text:#c7d3e3;
}
*{box-sizing:border-box}
body{margin:0;font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;background:var(--bg);color:var(--text)}
.wrap{width:100%;max-width:1760px;margin:0 auto;padding:24px clamp(16px,2.5vw,44px) 64px}
header{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px;gap:12px}
h1{font-size:20px;margin:0}
.badge{font-size:12px;padding:4px 12px;border-radius:999px;border:1px solid var(--line);background:#fff;color:var(--muted);white-space:nowrap}
.badge.running{color:#fff;background:var(--primary);border-color:var(--primary)}
.badge.awaiting_confirm{color:#fff;background:var(--warn);border-color:var(--warn)}
.badge.done{color:#fff;background:var(--ok);border-color:var(--ok)}
.badge.error{color:#fff;background:var(--err);border-color:var(--err)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 18px 14px;margin-bottom:16px;box-shadow:0 1px 3px rgba(16,24,40,.05)}
.card h2{font-size:14px;margin:0 0 14px;color:var(--muted);font-weight:600;letter-spacing:.05em}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
@media (max-width:640px){.grid{grid-template-columns:1fr}}
.grid .wide{grid-column:1/-1}
label{display:block;font-size:13px;color:var(--muted);margin-bottom:4px}
input[type=text],input[type=password],input[type=number],select{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;font-size:14px;background:#fbfcfe}
.inputWrap{position:relative;width:100%}
.inputWrap input{padding-right:38px}
.eyeBtn{position:absolute;right:4px;top:50%;transform:translateY(-50%);background:transparent;border:0;padding:4px 6px;cursor:pointer;color:#6b7a90;line-height:0;display:flex;align-items:center;justify-content:center;border-radius:6px}
.eyeBtn:hover{color:#1a2540;background:#f0f3fa}
.eyeBtn svg{width:18px;height:18px;display:block}
input:focus,select:focus{outline:2px solid #bcd2ff;border-color:var(--primary)}
.check{display:flex;align-items:center;gap:8px;font-size:14px}
.hint{font-size:12px;color:var(--muted);margin-top:6px}
.btn{display:inline-block;border:0;border-radius:8px;padding:9px 18px;font-size:14px;cursor:pointer;background:var(--primary);color:#fff}
.btn:hover{background:var(--primary-h)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn.ghost{background:#fff;color:var(--text);border:1px solid var(--line)}
.btn.ok{background:var(--ok)} .btn.ok:hover{background:#188a49}
.btn.danger{background:var(--err)} .btn.danger:hover{background:#b93535}
.btn-lg{width:100%;padding:13px;font-size:16px;font-weight:600;letter-spacing:.2em}
.wl-table{width:100%;border-collapse:collapse;table-layout:fixed}
.wl-table td{padding:4px 6px 4px 0}
.wl-table input{width:100%}
.wl-del{color:var(--err);cursor:pointer;border:0;background:none;font-size:18px;padding:2px 8px;line-height:1}
#confirmBox{border:1px solid #f0c36d;background:#fffaf0}
.planTable{width:100%;border-collapse:collapse;font-size:13px;margin:10px 0}
.planTable th,.planTable td{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
.planTable th{background:#f7f9fc;color:var(--muted)}
.dates{font-family:Consolas,monospace;font-size:12px;line-height:1.7}
.term{background:var(--term-bg);color:var(--term-text);border-radius:10px;padding:12px 14px;font-family:Consolas,"Courier New",monospace;font-size:12.5px;line-height:1.55;height:340px;overflow-y:auto;white-space:pre-wrap;word-break:break-all}
.term .err{color:#ff8f8f}.term .ok{color:#7ce38b}.term .sys{color:#8fb8ff}
.rowBtns{display:flex;gap:10px;justify-content:flex-end;margin-top:12px;flex-wrap:wrap}
footer{text-align:center}
.layout{display:grid;grid-template-columns:minmax(0,1fr) clamp(440px,42vw,880px);gap:16px;align-items:start}
.colL{min-width:0}
.colR{position:sticky;top:16px}
@media (max-width:1240px){.layout{grid-template-columns:1fr}.colR{position:static}}
.shotBox{background:#0d1117;border-radius:10px;min-height:400px;display:flex;align-items:center;justify-content:center;overflow:hidden}
.shotBox img{width:100%;height:auto;display:block}
.shotEmpty{color:#5b6676;font-size:13px;padding:24px;text-align:center;line-height:1.9}
.liveTag{display:inline-block;font-size:11px;color:var(--muted);margin-left:8px;font-weight:400}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>RDM 自动填报控制台</h1>
    <span class="badge" id="badge">未运行</span>
  </header>

  <div class="layout">
  <div class="colL">

  <div class="card">
    <h2>运行参数</h2>
    <div class="grid">
      <div><label>RDM 用户名</label><input id="username" type="text" placeholder="青铜器 RDM 账号" autocomplete="off"></div>
      <div><label>RDM 密码</label><div class="inputWrap"><input id="password" type="password" placeholder="不保存，仅本次运行使用" autocomplete="off"><button type="button" class="eyeBtn" data-target="password" title="显示明文" aria-label="切换密码可见"></button></div></div>
      <div><label>填写月份</label><select id="month"></select></div>
      <div><label>请假日期（可选）</label><input id="leave" type="text" placeholder="如 3, 8, 21 或 2026-09-03，留空=无"></div>
      <div><label>安全阀 MAX_ENTRIES</label><input id="maxEntries" type="number" value="0" min="0">
        <div class="hint" id="maxEntriesHint">最多提交几条，0=不限制。0=不限制；其余值表示本次最多提交该条数（首次跑建议 1，确认没问题再放开）。</div></div>
      <div><label>浏览器模式</label>
        <div class="check" style="margin-top:8px"><input id="headless" type="checkbox" checked><span>无头模式（不弹浏览器窗口）</span></div></div>
    </div>
  </div>

  <div class="card">
    <h2>大模型接口（描述生成，可选）</h2>
    <div class="grid">
      <div class="wide"><label>接口地址</label><input id="llmUrl" type="text" placeholder="https://api.deepseek.com/chat/completions"></div>
      <div><label>API Key</label><div class="inputWrap"><input id="llmKey" type="password" placeholder="留空沿用已保存配置" autocomplete="off"><button type="button" class="eyeBtn" data-target="llmKey" title="显示明文" aria-label="切换 Key 可见"></button></div></div>
      <div><label>模型名（可选）</label><input id="llmModel" type="text" placeholder="留空自动（deepseek-chat）"></div>
    </div>
    <div class="hint">不配置也能跑：自动退回模板描述。Key 只在本机内存中转，不落盘。</div>
  </div>

  <div class="card">
    <h2>工作日志（大模型素材，可选）</h2>
    <div id="wlRows"></div>
    <div class="rowBtns">
      <button class="btn ghost" onclick="wlAdd('','')">+ 加一行</button>
      <button class="btn" onclick="wlSave()">保存日志</button>
    </div>
    <div class="hint">第一列日期段如 9.1-9.12，第二列这段时间做了什么。保存后立即生效，大模型按它生成贴合实际的每日描述。</div>
  </div>

  <button class="btn btn-lg" id="startBtn" onclick="startRun()">开 始 运 行</button>

  <div class="card" id="confirmBox" style="display:none;margin-top:16px">
    <h2>排期已生成，请确认</h2>
    <div id="confirmInfo"></div>
    <table class="planTable" id="planTable"></table>
    <div class="rowBtns">
      <button class="btn danger" onclick="confirmRun(false)">取 消</button>
      <button class="btn ok" onclick="confirmRun(true)">确认开始提交</button>
    </div>
  </div>

  <div class="card" style="margin-top:16px">
    <h2>运行日志</h2>
    <div class="term" id="term"></div>
  </div>

  </div><!-- /colL -->

  <div class="colR">
    <div class="card">
      <h2>实时画面<span class="liveTag" id="shotTime"></span></h2>
      <div class="shotBox">
        <img id="shotImg" style="display:none" alt="实时画面">
        <div id="shotEmpty" class="shotEmpty">未在运行<br>开始运行后，每执行一步自动刷新画面</div>
      </div>
      <div class="hint">机器人浏览器页面的实时截图（无头模式也能看到），每完成一步自动更新，能看出当前点到了哪里。</div>
    </div>
  </div><!-- /colR -->

  </div><!-- /layout -->

  <footer class="hint">仅监听 127.0.0.1 · 密码不落盘 · 与命令行 fill_progress.py 同一套逻辑</footer>
</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
let since = 0;
let shotSeq = -1;
let prevUrl = null;

// 月份下拉，默认当前月
(function(){
  const cur = new Date().getMonth() + 1;
  for (let m = 1; m <= 12; m++){
    const o = document.createElement('option');
    o.value = m; o.textContent = m + ' 月';
    if (m === cur) o.selected = true;
    $('month').appendChild(o);
  }
})();

// LLM 配置预填（key 只回掩码，不回明文）
fetch('/api/llm-config').then(r=>r.json()).then(c=>{
  if (c.url) $('llmUrl').value = c.url;
  if (c.model) $('llmModel').value = c.model;
  if (c.key_set) $('llmKey').placeholder = '已配置 ' + c.key_masked + '，留空沿用';
}).catch(()=>{});

// 密码 / Key 输入框右侧的眼睛按钮（切换明文显示）
const EYE_ON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>';
const EYE_OFF = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.94 10.94 0 0 1 12 19c-7 0-10-7-10-7a18.45 18.45 0 0 1 4.06-5.94"/><path d="M9.9 4.24A10.94 10.94 0 0 1 12 4c7 0 10 7 10 7a18.5 18.5 0 0 1-2.16 3.19"/><path d="M14.12 14.12A3 3 0 1 1 9.88 9.88"/><line x1="2" y1="2" x2="22" y2="22"/></svg>';
document.querySelectorAll('.eyeBtn').forEach(b=>{
  b.innerHTML = EYE_ON;
  b.addEventListener('click', ()=>{
    const i = document.getElementById(b.dataset.target);
    const showing = i.type === 'text';
    i.type = showing ? 'password' : 'text';
    b.innerHTML = showing ? EYE_ON : EYE_OFF;
    b.title = showing ? '显示明文' : '隐藏明文';
  });
});

// ---- 工作日志 ----
const wlTable = document.createElement('table');
wlTable.className = 'wl-table';
$('wlRows').appendChild(wlTable);

function wlAdd(a, b){
  const tr = document.createElement('tr');
  const td1 = document.createElement('td'); td1.style.width = '150px';
  const td2 = document.createElement('td');
  const td3 = document.createElement('td'); td3.style.width = '36px';
  const i1 = document.createElement('input'); i1.type='text'; i1.value=a; i1.placeholder='9.1-9.12';
  const i2 = document.createElement('input'); i2.type='text'; i2.value=b; i2.placeholder='这段时间做了什么';
  const del = document.createElement('button'); del.className='wl-del'; del.textContent='×'; del.title='删除本行';
  del.onclick = () => tr.remove();
  td1.appendChild(i1); td2.appendChild(i2); td3.appendChild(del);
  tr.appendChild(td1); tr.appendChild(td2); tr.appendChild(td3);
  wlTable.appendChild(tr);
}

fetch('/api/worklog').then(r=>r.json()).then(d=>{
  const rows = (d.rows && d.rows.length ? d.rows : [['','']]);
  rows.forEach(r => wlAdd(r[0]||'', r[1]||''));
  const cnt = rows.filter(r => (r[0]&&r[0].trim()) || (r[1]&&r[1].trim())).length;
  const inp = $('maxEntries');
  inp.value = cnt;
  inp.dataset.auto = '1';
  const h = $('maxEntriesHint');
  if (cnt > 0) h.textContent = `当前日志 ${cnt} 条 → 默认安全阀 ${cnt}（填完所有日期）。首次跑可手动改成 1 试单条，确认后再放开。0=不限制。`;
  else h.textContent = '当前没有工作日志，留空将全部填满。先在上面加几行日志行后再跑。0=不限制。';
}).catch(()=>wlAdd('',''));

document.addEventListener('input', e=>{
  if (e.target && e.target.id === 'maxEntries') e.target.dataset.auto = '0';
});

async function wlSave(){
  const rows = Array.from(wlTable.querySelectorAll('tr')).map(tr=>{
    const i = tr.querySelectorAll('input');
    return [i[0] ? i[0].value.trim() : '', i[1] ? i[1].value.trim() : ''];
  }).filter(r => r[0] || r[1]);
  const d = await (await fetch('/api/worklog', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({rows})})).json();
  toast(d.ok ? '工作日志已保存（' + d.rows + ' 行）' : '保存失败：' + (d.error||''));
}

// ---- 启动 ----
async function startRun(){
  if (!$('username').value.trim() || !$('password').value){
    toast('请先填 RDM 用户名和密码'); return;
  }
  const body = {
    username: $('username').value.trim(),
    password: $('password').value,
    month: parseInt($('month').value, 10),
    leave_dates: $('leave').value.trim(),
    max_entries: parseInt($('maxEntries').value || '1', 10),
    headless: $('headless').checked,
    llm_url: $('llmUrl').value.trim(),
    llm_api_key: $('llmKey').value,
    llm_model: $('llmModel').value.trim(),
  };
  const d = await (await fetch('/api/start', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)})).json();
  if (!d.ok){ toast('启动失败：' + (d.error||'')); return; }
  since = 0; $('term').innerHTML = '';
  toast('已开始，正在登录并生成排期...');
}

// ---- 确认 ----
function showConfirm(c){
  $('confirmBox').style.display = '';
  $('confirmInfo').innerHTML = '共 <b>' + c.total + '</b> 条待填写记录，本次最多执行 <b>' + esc(c.limit_text) +
    '</b>。点"确认开始提交"后逐条自动填写。';
  let html = '<tr><th>任务</th><th>工作量</th><th>分配日期</th><th>完成率</th></tr>';
  c.plan.forEach(p => {
    html += '<tr><td>' + esc(p.name) + '</td><td>' + p.plan_effort_hours + 'h / ' + p.days_needed + ' 天</td>' +
      '<td class="dates">' + (p.assigned_dates.length ? p.assigned_dates.join('<br>') : '—') + '</td>' +
      '<td>' + (p.rates.length ? p.rates[0] + '% → ' + p.rates[p.rates.length-1] + '%' : '—') + '</td></tr>';
    if (p.pending_days > 0){
      html += '<tr><td colspan="4" style="color:var(--warn)">⚠ 《' + esc(p.name) + '》还有 ' + p.pending_days + ' 天没排上（本月工作日不够），本次不填</td></tr>';
    }
  });
  $('planTable').innerHTML = html;
  $('confirmBox').scrollIntoView({behavior:'smooth', block:'start'});
}

async function confirmRun(ok){
  const d = await (await fetch('/api/confirm', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ok})})).json();
  $('confirmBox').style.display = 'none';
  if (!d.ok) toast(d.error || '操作失败');
}

// ---- 轮询：日志 + 状态 + 确认信息 ----
const BADGE = {
  idle: ['未运行',''], running: ['运行中','running'],
  awaiting_confirm: ['等待确认','awaiting_confirm'],
  done: ['已结束','done'], error: ['出错','error'],
};

function appendLog(line){
  const t = $('term');
  const div = document.createElement('div');
  if (/出错|失败|错误|Traceback|超时/.test(line)) div.className = 'err';
  else if (/提交完成|运行结束|登录成功|描述生成完成/.test(line)) div.className = 'ok';
  else if (/^\[webui\]/.test(line)) div.className = 'sys';
  div.textContent = line;
  t.appendChild(div);
  t.scrollTop = t.scrollHeight;
}

function setBadge(state, error){
  const b = $('badge');
  if (error){ b.textContent = '出错'; b.className = 'badge error'; }
  else { const [text, cls] = BADGE[state] || [state,'']; b.textContent = text; b.className = 'badge ' + cls; }
  const busy = state === 'running' || state === 'awaiting_confirm';
  $('startBtn').disabled = busy;
}

setInterval(async () => {
  try{
    const d = await (await fetch('/api/poll?since=' + since)).json();
    since = d.next;
    d.logs.forEach(appendLog);
    setBadge(d.state, d.error);
    if (typeof d.shot_seq === 'number' && d.shot_seq !== shotSeq){ shotSeq = d.shot_seq; loadShot(); }
    if (d.state === 'awaiting_confirm' && d.confirm) showConfirm(d.confirm);
    else $('confirmBox').style.display = 'none';
  }catch(e){ /* 服务重启等瞬时错误，下个周期再试 */ }
}, 1000);

async function loadShot(){
  try{
    const r = await fetch('/api/screenshot');
    if (!r.ok){
      $('shotImg').style.display = 'none';
      $('shotEmpty').style.display = '';
      $('shotTime').textContent = '';
      return;
    }
    const blob = await r.blob();
    if (prevUrl) URL.revokeObjectURL(prevUrl);
    prevUrl = URL.createObjectURL(blob);
    const img = $('shotImg');
    img.src = prevUrl;
    img.style.display = '';
    $('shotEmpty').style.display = 'none';
    $('shotTime').textContent = '· 更新于 ' + new Date().toLocaleTimeString('zh-CN', {hour12:false});
  }catch(e){}
}

function toast(msg){
  const t = document.createElement('div');
  t.textContent = msg;
  t.style.cssText = 'position:fixed;top:18px;left:50%;transform:translateX(-50%);background:#1c2433;color:#fff;padding:9px 18px;border-radius:8px;font-size:13px;z-index:99;box-shadow:0 4px 14px rgba(0,0,0,.25)';
  document.body.appendChild(t);
  setTimeout(()=>t.remove(), 2600);
}
</script>
</body>
</html>
"""


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"RDM 自动填报控制台已启动: {url}")
    print("浏览器没自动打开的话，手动访问上面的地址；Ctrl+C 退出。")
    if os.getenv("WEBUI_NO_BROWSER", "") != "1":
        threading.Timer(0.5, lambda: _open_browser(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")


def _open_browser(url: str):
    try:
        webbrowser.open(url)
    except Exception:
        pass


if __name__ == "__main__":
    main()
