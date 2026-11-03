r"""Local dashboard for the Rewards runner: history, status, on/off switch.

Standard library only. Serves http://127.0.0.1:8765
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOG_DIR = BASE / "logs"
CONFIG = BASE / "config.json"
PYTHON = Path(sys.executable)
RUNNER = BASE / "run.py"
PORT = 8765

_lock = threading.Lock()

# pythonw has no console of its own; any console child (tasklist, powershell)
# would otherwise allocate a fresh console window that flashes on screen.
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
_alive_cache: list = [0.0, False]


def read_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {"enabled": True}


def write_config(cfg: dict) -> None:
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def history(limit: int = 14) -> list[dict]:
    rows: dict[str, dict] = {}
    for path in sorted(LOG_DIR.glob("*.jsonl"))[-limit:]:
        day = path.stem
        summaries: list[dict] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                if rec.get("event") == "summary":
                    summaries.append(rec)
        except Exception:
            pass
        last = summaries[-1] if summaries else None
        fails = (last or {}).get("failed") or []
        rows[day] = {
            "date": day,
            "runs": len(summaries),
            "done": sum(int(s.get("done") or 0) for s in summaries),
            "fails": fails[-6:],
            "after": (last or {}).get("after"),
            "fail_count": len(fails),
            "locked": int((last or {}).get("locked_cards") or 0),
            "quests": sum(len(s.get("quests") or []) for s in summaries),
            "seconds": (last or {}).get("seconds"),
            "at": (last or {}).get("ts"),
        }
    return list(rows.values())[-limit:][::-1]


def snapshot() -> dict:
    cfg = read_config()
    rows = history()
    today = datetime.now().strftime("%Y-%m-%d")
    live = next((r for r in rows if r["date"] == today), None)
    running = RUNNER.is_file() and _runner_alive()
    return {"config": cfg, "history": rows, "today": live, "running": running, "now": datetime.now().isoformat(timespec="seconds")}


def _runner_alive() -> bool:
    now = time.monotonic()
    if now - _alive_cache[0] < 2.0:
        return bool(_alive_cache[1])
    alive = False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq msedge.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=8, creationflags=NOWIN,
        ).stdout
        alive = "msedge.exe" in out.lower() and _marker_alive()
    except Exception:
        alive = False
    _alive_cache[0] = now
    _alive_cache[1] = alive
    return alive


def _marker_alive() -> bool:
    marker = BASE / ".running"
    if not marker.exists():
        return False
    try:
        ts = datetime.fromisoformat(marker.read_text(encoding="utf-8").strip())
        return (datetime.now() - ts).total_seconds() < 600
    except Exception:
        return False


def start_run() -> str:
    with _lock:
        if _marker_alive():
            return "already running"
        marker = BASE / ".running"
        marker.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            [str(PYTHON), "-X", "utf8", str(RUNNER)],
            cwd=str(BASE), creationflags=flags, close_fds=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return "started"


PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>Rewards 每日任务面板</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:#0f1216;color:#e6e9ee;font:15px/1.6 "Segoe UI",system-ui,sans-serif}
h1{font-size:20px;margin:0 0 4px}
.sub{color:#8b95a5;font-size:13px;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:20px}
.card{background:#171c23;border:1px solid #232a33;border-radius:12px;padding:14px 16px}
.card .k{color:#8b95a5;font-size:12px;letter-spacing:.04em}
.card .v{font-size:24px;font-weight:600;margin-top:2px}
.card .v small{font-size:13px;color:#8b95a5;font-weight:400}
.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:20px}
button{background:#2563eb;color:#fff;border:0;border-radius:9px;padding:9px 16px;font-size:14px;cursor:pointer}
button.ghost{background:#1e242c;border:1px solid #2c3540}
button:disabled{opacity:.45;cursor:not-allowed}
.switch{display:flex;align-items:center;gap:10px;background:#171c23;border:1px solid #232a33;border-radius:9px;padding:8px 14px}
.switch b{font-weight:600}
table{width:100%;border-collapse:collapse;background:#171c23;border:1px solid #232a33;border-radius:12px;overflow:hidden}
th,td{padding:9px 12px;text-align:left;border-bottom:1px solid #232a33;font-size:13.5px;vertical-align:top}
th{color:#8b95a5;font-weight:500;background:#141920;white-space:nowrap}
tr:last-child td{border-bottom:0}
.ok{color:#3fb950}.warn{color:#d29922}.bad{color:#f85149}
.err{color:#f85149;font-size:12.5px;margin:2px 0 0}
.note{color:#8b949e;font-size:12.5px;margin:4px 0 0}
code{background:#11161c;padding:1px 5px;border-radius:5px;font-size:12px}
</style></head><body>
<h1>Microsoft Rewards 每日任务</h1>
<div class="sub" id="sub">加载中…</div>
<div class="grid" id="cards"></div>
<div class="row">
  <div class="switch">自动运行 <b id="sw">—</b></div>
  <button id="toggle">切换</button>
  <button class="ghost" id="run">立即运行一次</button>
  <button class="ghost" id="reload">刷新</button>
</div>
<table><thead><tr><th>日期</th><th>运行</th><th>完成卡片</th><th>今日积分</th><th>日常任务</th><th>打卡</th><th>状态 / 问题</th></tr></thead>
<tbody id="rows"></tbody></table>
<script>
const $=s=>document.querySelector(s);
let data=null;
async function load(){
  const r=await fetch('/api/data',{cache:'no-store'}); data=await r.json(); render();
}
function esc(s){return String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function render(){
  const c=data.config||{}, t=data.today||{}, h=data.history||[];
  $('#sub').textContent='最近刷新 '+data.now.replace('T',' ')+(data.running?' · 正在运行':'')+(c.enabled?'':' · 已关闭自动运行');
  $('#sw').textContent=c.enabled?'已开启':'已关闭';
  $('#sw').className=c.enabled?'ok':'bad';
  const after=(t.after||{});
  const box=(k,v)=>`<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  $('#cards').innerHTML=
    box('今日积分',after.today??'—')+
    box('日常任务',(after.daily||'—')+`<small> 分</small>`)+
    box('连续打卡',`搜索 ${esc(after.search||'?')} · 活动 ${esc(after.activity||'?')}`)+
    box('今日运行',(t.runs??0)+' 次')+
    box('完成卡片',(t.done??0)+' 张')+
    box('长期任务',(t.quests??0)+' 项')+
    box('耗时',(t.seconds??'—')+' 秒');
  $('#rows').innerHTML=h.length?h.map(r=>{
    const a=r.after||{};
    const st=r.runs===0?'<span class="warn">未运行</span>':(r.fail_count?'<span class="warn">有 '+r.fail_count+' 项未完成</span>':'<span class="ok">成功</span>');
    const errs=(r.fails||[]).map(f=>`<div class="err">✗ ${esc(f.title)} — ${esc(f.reason)}</div>`).join('');
    const lk=r.locked?'<div class="note">另有 '+r.locked+' 张银牌卡未解锁，升级后自动纳入</div>':'';
    return `<tr><td>${esc(r.date)}</td><td>${r.runs}</td><td>${r.done}</td>
      <td>${esc(a.today??'—')}</td><td>${esc(a.daily??'—')}</td>
      <td>搜索 ${esc(a.search||'?')} · 活动 ${esc(a.activity||'?')}${r.quests?` · 长期 ${r.quests}`:''}</td>
      <td>${st}${lk}${errs}</td></tr>`;
  }).join(''):'<tr><td colspan="7" class="warn">还没有运行记录</td></tr>';
}
$('#toggle').onclick=async()=>{await fetch('/api/toggle',{method:'POST'});load();};
$('#run').onclick=async()=>{const r=await fetch('/api/run',{method:'POST'});const j=await r.json();alert(j.result);load();};
$('#reload').onclick=load;
load(); setInterval(load,30000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.startswith("/api/data"):
            self._send(200, json.dumps(snapshot(), ensure_ascii=False).encode(), "application/json; charset=utf-8")
        elif self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        if self.path.startswith("/api/toggle"):
            cfg = read_config()
            cfg["enabled"] = not bool(cfg.get("enabled", True))
            write_config(cfg)
            self._send(200, json.dumps({"enabled": cfg["enabled"]}, ensure_ascii=False).encode(), "application/json")
        elif self.path.startswith("/api/run"):
            self._send(200, json.dumps({"result": start_run()}, ensure_ascii=False).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")


def main() -> None:
    port = int(os.environ.get("REWARDS_PORT", PORT))
    if "PYTHONIOENCODING" not in os.environ:
        try:  # pythonw.exe has no console; keep CJK from raising on print
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"dashboard: http://127.0.0.1:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / "serve_error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        raise
