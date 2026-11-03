r"""Microsoft Rewards daily-task runner.

Design goals: no LLM at runtime, idempotent (stops when nothing is left),
one JSON line per event, human-paced clicks, never touches silver-only cards.
"""
from __future__ import annotations

import argparse
import atexit
import ctypes
import json
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent
PROFILE = BASE / "profile"
LOG_DIR = BASE / "logs"
STATE = BASE / "state.json"
CONFIG = BASE / "config.json"
EARLY = "https://rewards.bing.com/earn"

DEFAULT_CONFIG = {
    "enabled": True,
    "headless": False,
    "min_delay": 2.0,
    "max_delay": 5.0,
    "do_search_streak": True,
    "do_activities": True,
    "do_punchcards": True,
    "notify": True,
    "do_puzzle": True,
    "max_cards": 24,
    "auto_window": ["07:00", "23:30"],
    "idle_only": True,
    "idle_seconds": 300,
    "recheck_hours": 6,
    "search_keywords": [
        "今天的天气", "人工智能 最新进展", "健康晚餐食谱", "周末旅行攻略",
        "好看的电影推荐", "咖啡 好处", "颈椎 放松", "秋冬穿搭",
    ],
}

COLLECT = r"""
() => {
  const t = (e) => ((e.innerText || '') || '').trim();
  const scoreRe = /\+(\d+)/;
  const cards = [];
  let n = 0;
  for (const el of document.querySelectorAll('a,span,button')) {
    const txt = t(el);
    if (!txt || txt.length > 240) continue;
    if (!scoreRe.test(txt)) continue;
    if ([...el.querySelectorAll('a,span,button')].some(c => scoreRe.test(t(c)))) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 60 || r.height < 30) continue;
    const mark = 'e' + (n++);
    el.setAttribute('data-rw', mark);
    // "locked" is decided by real interactivity, not by the "需要银牌级别" label:
    // once the account levels up the aria-disabled flag disappears and the card
    // becomes a normal candidate automatically.
    let locked = false;
    let p = el;
    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
      if (p.getAttribute && p.getAttribute('aria-disabled') === 'true') { locked = true; break; }
      if (p.disabled === true) { locked = true; break; }
    }
    if (!locked) {
      try { if (getComputedStyle(el).pointerEvents === 'none') locked = true; } catch (e) {}
    }
    cards.push({
      idx: mark,
      tag: el.tagName,
      txt: txt.replace(/\s+/g, ' '),
      href: el.href || '',
      y: Math.round(r.y),
      silver: locked,
    });
  }
  let secY = -1;
  for (const el of document.querySelectorAll('div,span,h1,h2,h3,h4')) {
    if (t(el) === '日常任务') { secY = Math.round(el.getBoundingClientRect().y); break; }
  }
  return { secY, cards };
}
"""

ACTIVITIES = r"""
() => {
  const t = (e) => ((e.innerText || '') || '').trim();
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll('a')) {
    const txt = t(el);
    if (!txt || txt.length > 240) continue;
    const m = txt.match(/\+(\d+)/);
    if (!m || m[1] !== '10') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 40 || r.height < 20) continue;
    const mark = 'act' + (n++);
    el.setAttribute('data-act', mark);
    out.push({ idx: mark, txt: txt.replace(/\s+/g, ' '), href: el.href || '' });
  }
  return out;
}
"""

EXPAND_STREAK = r"""
() => {
  for (const el of document.querySelectorAll('button,a')) {
    if ((el.innerText || '').includes('每日连续打卡活动')) {
      el.setAttribute('data-open-streak', '1');
      return true;
    }
  }
  return false;
}
"""

QUEST_CARDS = r"""
() => {
  const t = (e) => ((e.innerText || '') || '').trim();
  let taskY = -1;
  for (const el of document.querySelectorAll('div,span,h1,h2,h3,h4')) {
    if (t(el) === '任务') { taskY = Math.round(el.getBoundingClientRect().y); break; }
  }
  const out = [];
  for (const el of document.querySelectorAll('a')) {
    if (!(el.href || '').includes('/earn/quest/')) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 200) continue;
    if (taskY > 0 && r.y < taskY - 10) continue;
    out.push({ txt: t(el).replace(/\s+/g, ' ').slice(0, 90), href: el.href });
  }
  return out;
}
"""

QUEST_JS = r"""
() => {
  const t = (e) => ((e.innerText || '') || '').trim();
  const body = document.body.innerText || '';
  const m = body.match(/(\d+)\s*\/\s*(\d+)\s*个任务/);
  const status = m ? m[1] + '/' + m[2] : null;
  // punchcards that require installing the Rewards desktop app can't be finished here
  const needsApp = /下载(该)?应用|在桌面上下载|下载并登录/.test(body);
  const items = [];
  let n = 0;
  const skipWord = /(反馈|接受|拒绝|管理|Cookie|简体中文|首页|兑换|积分赚取|关于|邀请好友|LL$)/;
  for (const el of document.querySelectorAll('a, span[role="link"], button')) {
    const txt = t(el).replace(/\s+/g, ' ');
    if (!txt || txt.length < 2 || txt.length > 40) continue;
    if (skipWord.test(txt.replace(/\s/g, ''))) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 40 || r.height < 20) continue;
    if (r.x < 600 || r.y < 150) continue;         // right-hand activity column only
    const cs = getComputedStyle(el);
    if (el.getAttribute('aria-disabled') === 'true' || cs.cursor === 'default') continue;
    if (/下载|安装/.test(txt)) continue;
    const mark = 'q' + (n++);
    el.setAttribute('data-qi', mark);
    items.push({ idx: mark, txt: txt.slice(0, 50), href: el.href || '' });
  }
  return { status, needsApp, items };
}
"""

PROGRESS = r"""() => {
  const t = (document.body.innerText || '');
  const num = (re) => { const m = t.match(re); return m ? m[1] + '/' + m[2] : null; };
  const one = (re) => { const m = t.match(re); return m ? m[1] : null; };
  return {
    daily: num(/日常任务[\s\S]{0,40}?(\d+)\s*\/\s*(\d+)/),
    today: one(/今日积分[\s\S]{0,60}?(\d+)/),
    search: num(/搜索[:：]\s*(\d+)\s*\/\s*(\d+)/),
    activity: num(/活动[:：]\s*(\d+)\s*\/\s*(\d+)/),
    checkin: num(/签到[:：]\s*(\d+)\s*\/\s*(\d+)/),
  };
}
"""


class Log:
    def __init__(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.path = LOG_DIR / f"{datetime.now():%Y-%m-%d}.jsonl"

    def event(self, kind: str, **kw) -> None:
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "event": kind, **kw}
        line = json.dumps(rec, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        try:
            print(line, flush=True)  # pythonw has no stdout
        except Exception:
            pass


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG.exists():
        try:
            cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
        except Exception:
            pass
    else:
        CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def pace(cfg: dict) -> None:
    time.sleep(random.uniform(cfg["min_delay"], cfg["max_delay"]))


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds() -> float:
    """Seconds since the last keyboard/mouse input (0 if unknown)."""
    try:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        tick = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
        return ((tick - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:
        return 0.0


def read_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def already_done_today(state: dict, cfg: dict | None = None) -> bool:
    """True when today's last run finished cleanly and no re-check is due.

    Microsoft keeps topping up the daily pool during the day (observed: the
    denominator moved 175 -> 180 and a brand-new +5 card appeared around noon),
    so a single clean run is not proof that nothing is left. After
    ``recheck_hours`` we allow one more pass to pick up late additions.
    """
    cfg = cfg or {}
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("date") != today:
        return False
    runs = state.get("runs") or []
    if not runs:
        return False
    last = runs[-1]
    if last.get("failed"):
        return False
    daily = (last.get("after") or {}).get("daily") or "0/0"
    try:
        got = int(str(daily).split("/")[0])
    except Exception:
        got = 0
    if got <= 0:
        return len(runs) >= 3  # nothing collectable yet: retry a few times, then stop
    try:
        hours = float(cfg.get("recheck_hours", 6) or 0)
    except Exception:
        hours = 6.0
    if hours > 0:
        try:
            last_at = datetime.fromisoformat(str(last.get("at")))
            if (datetime.now() - last_at).total_seconds() >= hours * 3600:
                return False
        except Exception:
            pass
    return True


def auto_gate(cfg: dict) -> tuple[bool, str]:
    now = datetime.now()
    start = cfg.get("auto_window", ["07:00", "23:30"])[0].split(":")
    end = cfg.get("auto_window", ["07:00", "23:30"])[1].split(":")
    minutes = now.hour * 60 + now.minute
    if not (int(start[0]) * 60 + int(start[1]) <= minutes < int(end[0]) * 60 + int(end[1])):
        return False, "outside-window"
    if already_done_today(read_state(), cfg):
        return False, "already-done"
    if cfg.get("idle_only", True):
        idle = idle_seconds()
        if idle < float(cfg.get("idle_seconds", 300)):
            return False, f"user-active({int(idle)}s)"
    return True, "ok"


def read_progress(page) -> dict:
    return page.evaluate(PROGRESS)


def day_points(prog: dict) -> int:
    """Numeric part of the '45/175' daily counter, -1 if unreadable."""
    try:
        return int(str(prog.get("daily", "")).split("/")[0])
    except (ValueError, IndexError):
        return -1


def collect(page) -> dict:
    return page.evaluate(COLLECT)


def open_earn(page) -> None:
    page.goto(EARLY, wait_until="domcontentloaded", timeout=90_000)
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass
    time.sleep(random.uniform(2.0, 3.5))


def click_marked(ctx, page, idx: str, attr: str = "data-rw") -> str:
    """Click a marked card; return 'popup' or 'inplace'."""
    target = page.locator(f'[{attr}="{idx}"]')
    target.scroll_into_view_if_needed(timeout=8000)
    before = page.url
    try:
        with ctx.expect_page(timeout=6000) as info:
            target.click(timeout=10_000)
        popup = info.value
        popup.wait_for_load_state("domcontentloaded", timeout=30_000)
        time.sleep(random.uniform(1.8, 3.2))
        popup.close()
        return "popup"
    except Exception:
        time.sleep(random.uniform(1.8, 2.8))
        return "inplace" if page.url != before else "noop"


def do_search_streak(page, cfg: dict, log: Log) -> None:
    """A real search from the Bing box (search-streak only counts those)."""
    keyword = random.choice(cfg["search_keywords"])
    page.goto("https://cn.bing.com/", wait_until="domcontentloaded", timeout=90_000)
    time.sleep(random.uniform(1.5, 3.0))
    box = page.locator("#sb_form_q").first
    box.click(timeout=10_000)
    page.keyboard.type(keyword, delay=random.randint(90, 220))
    time.sleep(random.uniform(0.4, 1.2))
    page.keyboard.press("Enter")
    page.wait_for_load_state("domcontentloaded", timeout=30_000)
    time.sleep(random.uniform(2.5, 4.5))
    log.event("search", keyword=keyword, url=page.url[:120])


def expand_streak(page) -> bool:
    """Open the daily-streak card so the 3 +10 activity cards render."""
    if not page.evaluate(EXPAND_STREAK):
        return False
    try:
        page.locator("[data-open-streak='1']").click(timeout=8000)
    except Exception:
        return False
    time.sleep(random.uniform(2.0, 3.0))
    return True


def do_activities(ctx, page, cfg: dict, log: Log, tried: set[str]) -> list[str]:
    """The three +10 "daily activity" cards; like the others, opening completes them."""
    got: list[str] = []
    for _ in range(5):
        acts = page.evaluate(ACTIVITIES)
        if not acts:
            if not expand_streak(page):
                break
            acts = page.evaluate(ACTIVITIES)
            if not acts:
                break
        card = next((c for c in acts if c["txt"][:40] not in tried), None)
        if not card:
            break
        title = card["txt"][:40]
        tried.add(title)
        before = read_progress(page).get("activity")
        try:
            how = click_marked(ctx, page, card["idx"], "data-act")
        except Exception as exc:
            log.event("activity", title=title, ok=False,
                      error=f"{type(exc).__name__}: {exc}"[:160])
            open_earn(page)
            pace(cfg)
            continue
        pace(cfg)
        open_earn(page)
        after = read_progress(page).get("activity")
        ok = after != before
        if ok:
            got.append(title)
        log.event("activity", title=title, ok=ok, how=how, before=before, after=after)
        if not ok:
            break
    return got


def open_quest_page(page, href: str):
    """Navigate to a punchcard (quest) page. These cards load in the same tab."""
    page.goto(href, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        pass
    time.sleep(random.uniform(1.5, 2.5))


def click_quest(ctx, qpage, idx: str) -> str:
    """Click one sub-task button inside a quest page; return 'popup'/'inplace'."""
    target = qpage.locator(f'[data-qi="{idx}"]')
    target.scroll_into_view_if_needed(timeout=8000)
    try:
        with ctx.expect_page(timeout=9000) as info:
            target.click(timeout=10_000)
        pop = info.value
        pop.wait_for_load_state("domcontentloaded", timeout=40_000)
        time.sleep(random.uniform(2.0, 3.5))
        pop.close()
        return "popup"
    except Exception:
        time.sleep(random.uniform(2.0, 3.0))
        return "inplace"


def do_punchcards(ctx, page, cfg: dict, log: Log) -> list[str]:
    """Walk the long-term 任务 cards; advance the ones that are unblocked today.

    Most of these gate one step per 24h, so at most one sub-task is clicked per
    card per run.
    """
    got: list[str] = []
    try:
        quests = page.evaluate(QUEST_CARDS)
        if not quests:  # SSR can be slow on a cold profile; one retry is enough
            time.sleep(2.5)
            quests = page.evaluate(QUEST_CARDS)
    except Exception:
        return got
    for q in quests[:5]:
        title = q["txt"][:40]
        if "应用" in title:
            log.event("quest", title=title, skipped="needs-app")
            continue
        try:
            open_quest_page(page, q["href"])
            st = page.evaluate(QUEST_JS)
            if st.get("needsApp"):
                log.event("quest", title=title, status=st.get("status"), skipped="needs-app")
                continue
            if not st.get("items"):
                log.event("quest", title=title, status=st.get("status"), skipped="locked-or-done")
                continue
            before = st.get("status")
            item = st["items"][0]
            how = click_quest(ctx, page, item["idx"])
            # Some sub-tasks navigate the quest tab itself; get back before re-reading.
            try:
                if "punchcard" not in page.url:
                    open_quest_page(page, q["href"])
                else:
                    page.reload(wait_until="domcontentloaded", timeout=60_000)
                    time.sleep(2.0)
            except Exception:
                pass
            after = (page.evaluate(QUEST_JS) or {}).get("status")
            ok = bool(before and after and before != after)
            if ok:
                got.append(title)
            log.event("quest", title=title, step=item["txt"][:30], ok=ok,
                      before=before, after=after, how=how)
        except Exception as exc:
            log.event("quest", title=title, ok=False,
                      error=f"{type(exc).__name__}: {exc}"[:140])
        finally:
            try:
                open_earn(page)
            except Exception:
                pass
        pace(cfg)
    return got


def notify(title: str, message: str) -> None:
    """Windows toast; falls back silently if the helper is missing or blocked."""
    script = BASE / "toast.ps1"
    if not script.exists():
        return
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(script), "-Title", title, "-Message", message],
            capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Microsoft Rewards daily runner")
    ap.add_argument("--auto", action="store_true",
                    help="only run when today is unfinished, inside the window, and the PC is idle")
    ap.add_argument("--force", action="store_true", help="ignore enabled flag")
    args = ap.parse_args(argv)

    cfg = load_config()
    log = Log()
    if not cfg.get("enabled", True) and not args.force:
        log.event("skip", reason="disabled")
        return 0
    if args.auto:
        ok, why = auto_gate(cfg)
        if not ok:
            log.event("skip", reason=why)
            return 0

    started = time.time()
    done: list[str] = []
    failed: list[dict] = []
    tried: set[str] = set()

    marker = BASE / ".running"
    marker.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
    atexit.register(lambda: marker.unlink(missing_ok=True))

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            channel="msedge",
            headless=bool(cfg.get("headless")),
            ignore_default_args=["--enable-automation"],
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 900},
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        open_earn(page)
        first = read_progress(page)
        locked = sum(1 for c in collect(page)["cards"] if c["silver"])
        log.event("start", before=first, locked_cards=locked)

        budget = int(cfg.get("max_cards", 24))
        while budget > 0:
            data = collect(page)
            sec_y = data["secY"]
            daily = [c for c in data["cards"] if sec_y > 0 and c["y"] >= sec_y - 10]
            cand = [
                c for c in daily
                if not c["silver"]
                and "microsoft-edge://" not in c["href"]
                and c["txt"][:40] not in tried
                and (cfg.get("do_puzzle", True) or "puzzle" not in c["href"])
            ]
            if not cand:
                break
            card = cand[0]
            title = card["txt"][:40]
            tried.add(title)
            budget -= 1
            t0 = time.time()
            before_pts = day_points(read_progress(page))
            try:
                how = click_marked(ctx, page, card["idx"])
            except Exception as exc:
                failed.append({"title": title, "reason": f"click: {type(exc).__name__}: {exc}"[:160]})
                log.event("card", title=title, ok=False, error=f"{type(exc).__name__}: {exc}"[:160])
                open_earn(page)
                pace(cfg)
                continue
            pace(cfg)
            open_earn(page)
            still = any(c["txt"][:40] == title for c in collect(page)["cards"])
            gained = day_points(read_progress(page)) - before_pts
            # a card counts as done if it disappeared OR the daily counter moved
            # (some cards stay on the page after completion)
            if still and gained <= 0:
                failed.append({"title": title, "reason": f"not-completed ({how})"})
                log.event("card", title=title, ok=False, how=how, ms=int((time.time() - t0) * 1000))
            else:
                done.append(title)
                log.event("card", title=title, ok=True, how=how, ms=int((time.time() - t0) * 1000))
            pace(cfg)

        if cfg.get("do_activities", True) and first.get("activity") != "3/3":
            try:
                done.extend(do_activities(ctx, page, cfg, log, tried))
            except Exception as exc:
                log.event("activity", ok=False, error=f"{type(exc).__name__}: {exc}"[:180])
        elif first.get("activity") == "3/3":
            log.event("activity", skipped="already 3/3")

        quest_done: list[str] = []
        if cfg.get("do_punchcards", True):
            try:
                open_earn(page)
                quest_done = do_punchcards(ctx, page, cfg, log)
            except Exception as exc:
                log.event("quest", ok=False, error=f"{type(exc).__name__}: {exc}"[:180])

        if cfg.get("do_search_streak", True) and first.get("search") != "1/1":
            try:
                do_search_streak(page, cfg, log)
            except Exception as exc:
                log.event("search", ok=False, error=f"{type(exc).__name__}: {exc}"[:180])
        elif first.get("search") == "1/1":
            log.event("search", skipped="already 1/1")

        open_earn(page)
        after = read_progress(page)
        locked_end = sum(1 for c in collect(page)["cards"] if c["silver"])
        log.event("summary", after=after, done=len(done), failed=failed,
                  quests=quest_done, locked_cards=locked_end,
                  seconds=int(time.time() - started))
        page.screenshot(path=str(LOG_DIR / "last_run.png"), full_page=False)
        time.sleep(1.0)
        ctx.close()

    today = datetime.now().strftime("%Y-%m-%d")
    state = {"date": today, "runs": []}
    if STATE.exists():
        try:
            old = json.loads(STATE.read_text(encoding="utf-8"))
            if old.get("date") == today:
                state = old
        except Exception:
            pass
    state["runs"].append({
        "at": datetime.now().isoformat(timespec="seconds"),
        "before": first, "after": after,
        "done": len(done), "failed": failed, "quests": quest_done,
        "seconds": int(time.time() - started),
    })
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    if cfg.get("notify", True) and (done or quest_done or failed):
        head = "Rewards 每日任务已完成" if not failed else "Rewards 有任务没做完"
        extra = f" · 长期任务 +{len(quest_done)} 步" if quest_done else ""
        try:
            notify(head, f"今日 +{after.get('today', '?')} 分｜日常 {after.get('daily', '?')}｜"
                         f"搜索 {after.get('search', '?')}｜活动 {after.get('activity', '?')}{extra}")
        except Exception:
            pass
    return 0 if not failed else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception:
        import traceback
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / "error.txt").write_text(
            datetime.now().isoformat() + "\n" + traceback.format_exc(), encoding="utf-8"
        )
        raise SystemExit(1)
