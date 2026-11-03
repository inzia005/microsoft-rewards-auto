r"""One-time: open a dedicated Edge profile, wait for manual sign-in, then dump
the Rewards page structure + captured /api/ JSON into logs/probe_*.json.

Usage:  python login_probe.py
"""
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent
PROFILE = BASE / "profile"
LOGS = BASE / "logs"
LOGS.mkdir(parents=True, exist_ok=True)

TS = time.strftime("%Y%m%d_%H%M%S")
RAW = LOGS / f"probe_{TS}.json"
STATUS = BASE / "login_state.json"

EARLY = "https://rewards.bing.com/earn"

IGNORE_ARGS = ["--enable-automation", "--disable-popup-blocking"]


def capture(ctx, store):
    def on_response(resp):
        url = resp.url
        if "/api/" not in url and "rewards" not in url:
            return
        try:
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                return
            body = resp.text()
            if not body or len(body) < 20:
                return
            store.append({"url": url, "status": resp.status, "body": json.loads(body)})
        except Exception:
            pass

    ctx.on("response", on_response)


def summarize(store, limit=600):
    """Compact stdout: what endpoints exist and the shape of each payload."""
    out = []
    seen = set()
    for rec in store:
        u = rec["url"].split("?")[0]
        if u in seen:
            continue
        seen.add(u)
        j = rec["body"]
        line = {"url": u, "status": rec["status"], "type": type(j).__name__}
        if isinstance(j, dict):
            line["keys"] = list(j.keys())[:12]
        out.append(json.dumps(line, ensure_ascii=False)[:limit])
    return out


def sleep_until_logged_in(ctx, page, timeout=2700):
    """Poll until the rewards session is authenticated.

    Signals, cheapest first: left the login host, rewards shell rendered,
    or a personalised /api/getuserinfo payload.
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        time.sleep(4)
        cur = None
        for pg in ctx.pages:
            if "rewards.bing.com" in pg.url or "bing.com/rewards" in pg.url:
                cur = pg
                break
        if cur is None:
            urls = [pg.url.split("?")[0] for pg in ctx.pages]
            sig = "urls:" + "|".join(urls)[:140]
            if sig != last:
                print("poll:", sig, flush=True)
                last = sig
            continue
        try:
            probe = cur.evaluate(
                """async () => {
                    const body = (document.body && document.body.innerText) || '';
                    let res = {status: 0, json: false, len: 0, bodyLen: body.length,
                               hasPoints: /积分|points|Rewards/i.test(body)};
                    try {
                        const r = await fetch('https://rewards.bing.com/api/getuserinfo?type=1',
                                              {credentials:'include', cache:'no-store'});
                        const t = await r.text();
                        let j = null;
                        try { j = JSON.parse(t); } catch(e) {}
                        res.status = r.status;
                        res.len = t.length;
                        res.json = !!j;
                        res.keys = j ? Object.keys(j).slice(0,8) : null;
                    } catch(e) { res.err = String(e).slice(0,100); }
                    return res;
                }"""
            )
        except Exception as exc:
            probe = {"err": str(exc)[:120]}
        sig = json.dumps(probe, sort_keys=True)[:200]
        if sig != last:
            print("poll:", sig, flush=True)
            last = sig
        authed = (
            probe.get("status") == 200 and probe.get("len", 0) > 200 and probe.get("json")
        ) or (probe.get("bodyLen", 0) > 500 and probe.get("hasPoints"))
        if authed:
            return cur, True
    return page, False


def main():
    store = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            channel="msedge",
            headless=False,
            ignore_default_args=IGNORE_ARGS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--window-size=1440,900",
            ],
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 900},
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        capture(ctx, store)

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(EARLY, wait_until="domcontentloaded", timeout=90000)
        print("start_url:", page.url, flush=True)

        page, ok = sleep_until_logged_in(ctx, page)
        print("LOGIN_OK" if ok else "LOGIN_TIMEOUT", flush=True)

        if ok:
            page.goto(EARLY, wait_until="domcontentloaded", timeout=90000)
            try:
                page.wait_for_load_state("networkidle", timeout=25000)
            except Exception:
                pass
            time.sleep(3)

            dom = {}
            try:
                dom = page.evaluate(
                    """() => {
                        const txt = (el) => (el.innerText || '').trim().slice(0, 120);
                        const cards = [...document.querySelectorAll(
                            'a,div[role=button],button,[class*=card],[class*=Card]')]
                            .map(e => ({
                                tag: e.tagName,
                                cls: (e.className || '').toString().slice(0, 100),
                                txt: txt(e),
                                href: e.href || '',
                            }))
                            .filter(o => o.txt.length > 2)
                            .slice(0, 120);
                        return {
                            title: document.title,
                            url: location.href,
                            bodyTxt: (document.body.innerText || '').slice(0, 2500),
                            cards,
                        };
                    }"""
                )
            except Exception as exc:
                dom = {"err": str(exc)[:200]}

            payload = {"ts": TS, "url": page.url, "api": store, "dom": dom}
            RAW.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            STATUS.write_text(
                json.dumps({"logged_in": True, "at": TS, "probe": str(RAW)}, ensure_ascii=False),
                encoding="utf-8",
            )

            print("--- API endpoints ---", flush=True)
            for line in summarize(store):
                print(line, flush=True)
            print("--- DOM ---", flush=True)
            print("title:", dom.get("title"), flush=True)
            print("url:", dom.get("url"), flush=True)
            print("cards:", len(dom.get("cards", [])), flush=True)
            print("--- BODY TEXT (first 1500) ---", flush=True)
            print((dom.get("bodyTxt") or "")[:1500], flush=True)
            print("RAW:", RAW, flush=True)
        else:
            STATUS.write_text(
                json.dumps({"logged_in": False, "at": TS}, ensure_ascii=False), encoding="utf-8"
            )

        time.sleep(2)
        ctx.close()


if __name__ == "__main__":
    main()
