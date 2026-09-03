"""
End-to-end, in a real browser.

The API tests prove the server behaves. These prove the *product* works: that a
person who has never seen it can sign in, add a source, watch it being read,
and get something back — and that a second person sees none of it.

Everything here drives the actual interface. Nothing calls the API directly,
because the bugs this catches are the ones between the two: a button wired to a
route that no longer exists, a token that is never attached, a job whose
progress never reaches the screen.

    python tools/e2e.py
    python tools/e2e.py --headed --keep     # to watch it, and poke afterwards
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SECRET = "e2e-jwt-secret-at-least-32-characters-long!!!"
ISS = "https://e2e.supabase.co/auth/v1"
ALICE = "11111111-1111-1111-1111-111111111111"
BOB = "22222222-2222-2222-2222-222222222222"
SHOTS = Path(os.environ.get("SUNROOM_E2E_SHOTS", "/tmp/sunroom-e2e"))

DOC = """# Exchange and Obligation

Reciprocity is the mutual give and take between parties of roughly equal standing.
It is the dominant mode in societies without centralized authority.
Redistribution requires a center that collects and then disburses.
Market exchange sets prices through the interaction of supply and demand.

## Forms of reciprocity

Generalized reciprocity involves giving without a specified expectation of return.
It predominates within households and among close kin.
Balanced reciprocity involves an explicit expectation of equivalent return.
Negative reciprocity is the attempt to get something for nothing.
As social distance increases, reciprocity becomes more balanced and then negative.
"""

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    mark = "  ok  " if ok else " FAIL "
    print(f"{mark} {name}" + (f"  — {detail}" if detail and not ok else ""))


def token(sub: str, email: str) -> str:
    import jwt
    return jwt.encode(
        {"sub": sub, "aud": "authenticated", "iss": ISS, "role": "authenticated",
         "email": email, "exp": int(time.time()) + 7200},
        SECRET, algorithm="HS256")


SESSION_SHIM = """
window.supabase = {{
  createClient: () => ({{
    auth: {{
      getSession: async () => ({{data: {{session: {session} }} }}),
      onAuthStateChange: (cb) => {{ window.__authcb = cb;
        return {{data: {{subscription: {{unsubscribe() {{}} }} }} }}; }},
      signInWithOtp: async ({{email}}) => {{ window.__sentTo = email;
        return {{error: null}}; }},
      signOut: async () => {{ window.__signedOut = true; return {{error: null}}; }},
    }}
  }})
}};
"""


async def page_for(browser, sub: str | None = None, email: str = ""):
    """A page carrying a session, or none. supabase-js is stood in for: the
    thing under test is the app's behaviour with a session, not the CDN."""
    pg = await browser.new_page(viewport={"width": 1440, "height": 950})
    session = ("null" if sub is None else json.dumps(
        {"access_token": token(sub, email), "user": {"email": email}}))
    await pg.add_init_script(SESSION_SHIM.format(session=session))
    pg.on("pageerror", lambda e: check(f"no page error ({e})", False))
    return pg


def wait_for_server(base: str, seconds: float = 45.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


async def run(base: str, headed: bool) -> None:
    from playwright.async_api import async_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path="/opt/pw-browsers/chromium", headless=not headed)

        # ── signed out ───────────────────────────────────────────────────
        pg = await page_for(browser)
        await pg.goto(base)
        await pg.wait_for_timeout(1500)
        gate_up = not await pg.evaluate("() => document.getElementById('gate').hidden")
        check("a stranger sees the sign-in screen", gate_up)
        check("and not the library",
              await pg.evaluate("() => !document.querySelector('.doccard')"))
        await pg.screenshot(path=str(SHOTS / "01-signed-out.png"))

        await pg.fill("#gate-email", "alice@e2e.test")
        await pg.click("#gate-go")
        await pg.wait_for_timeout(600)
        check("asking for a link says where it went",
              "alice@e2e.test" in await pg.inner_text("#gate-msg"),
              await pg.inner_text("#gate-msg"))
        await pg.screenshot(path=str(SHOTS / "02-link-sent.png"))
        await pg.close()

        # ── alice ────────────────────────────────────────────────────────
        pg = await page_for(browser, ALICE, "alice@e2e.test")
        await pg.goto(base)
        await pg.wait_for_timeout(2000)
        check("a session goes straight in",
              await pg.evaluate("() => document.getElementById('gate').hidden"))
        check("the account is named",
              "alice@e2e.test" in await pg.inner_text("#account-email"))
        check("an empty library invites a first source",
              "Nothing in here yet" in await pg.inner_text("#view"))
        await pg.screenshot(path=str(SHOTS / "03-empty.png"))

        # add a source, through the interface
        await pg.click("#add-open")
        await pg.wait_for_timeout(400)
        await pg.fill("#src", DOC * 40)
        await pg.fill("#ttl", "Exchange and Obligation")
        await pg.fill("#coll", "ANTH266")
        await pg.wait_for_timeout(900)          # the estimate debounce
        estimate = await pg.inner_text("#addmsg")
        check("a large paste shows what it will cost",
              "tokens" in estimate.lower(), estimate or "(nothing shown)")
        await pg.screenshot(path=str(SHOTS / "04-add.png"))

        await pg.click("#add-go")
        await pg.wait_for_timeout(1200)
        check("the sheet closes once queued",
              not await pg.evaluate(
                  "() => document.getElementById('sheet').classList.contains('on')"))
        check("progress appears in the tray",
              await pg.evaluate("() => !!document.querySelector('.jobcard')"))
        await pg.screenshot(path=str(SHOTS / "05-working.png"))

        for _ in range(90):
            if await pg.evaluate("() => document.querySelectorAll('.doccard').length"):
                break
            await pg.wait_for_timeout(1000)
        await pg.wait_for_timeout(1500)
        check("the document lands in the library",
              await pg.evaluate("() => document.querySelectorAll('.doccard').length") == 1)
        check("the usage meter has moved",
              "0 of" not in await pg.inner_text("#account-usage"),
              await pg.inner_text("#account-usage"))
        await pg.screenshot(path=str(SHOTS / "06-ready.png"))

        # read it, and trace a passage
        await pg.evaluate("() => document.querySelector('.doccard').click()")
        await pg.wait_for_timeout(2200)
        marks = await pg.evaluate("() => document.querySelectorAll('.src mark').length")
        check("the source is highlighted where it can be cited", marks > 5, str(marks))
        check("headings are not run into the prose",
              await pg.evaluate("() => document.querySelectorAll('.src .hd').length") > 0)
        check("and do not show their markdown markers",
              await pg.evaluate(
                  "() => [...document.querySelectorAll('.src .hd')]"
                  ".every(h => !h.textContent.trim().startsWith('#'))"),
              await pg.evaluate(
                  "() => (document.querySelector('.src .hd')||{}).textContent"))

        await pg.evaluate("""() => {
            const m = document.querySelectorAll('.src mark'); if (m.length > 2) m[2].click();
        }""")
        await pg.wait_for_timeout(1200)
        aside = await pg.inner_text("#aside")
        check("clicking a passage shows where it came from",
              "came from" in aside.lower() or "understood" in aside.lower(),
              aside[:80])
        await pg.screenshot(path=str(SHOTS / "07-read.png"))

        # make something
        await pg.evaluate("() => document.querySelector('#nav a[data-view=make]').click()")
        await pg.wait_for_timeout(900)
        await pg.evaluate("() => document.querySelector('.fmt[data-f=brief]').click()")
        await pg.wait_for_timeout(3000)
        parts = await pg.evaluate("() => document.querySelectorAll('#parts .part').length")
        check("a brief comes back with parts", parts > 0, str(parts))
        check("and says it is verified",
              "Verified" in await pg.inner_text("#view"))
        check("and every part cites something",
              await pg.evaluate(
                  "() => [...document.querySelectorAll('#parts .part')]"
                  ".every(p => p.querySelector('.srcchip'))"))
        await pg.evaluate("() => document.querySelector('#scroll').scrollTop = 520")
        await pg.wait_for_timeout(400)
        await pg.screenshot(path=str(SHOTS / "08-brief.png"))

        # a diagram must render, not fall back to source
        await pg.evaluate("() => document.querySelector('.fmt[data-f=explainer]').click()")
        await pg.wait_for_timeout(3200)
        check("diagrams render rather than showing their source",
              await pg.evaluate("() => document.querySelectorAll('#parts pre.raw').length") == 0)
        check("the diagram is drawn",
              await pg.evaluate("() => !!document.querySelector('.mermaid svg')"))
        await pg.screenshot(path=str(SHOTS / "09-explainer.png"))

        # ask, and be declined
        await pg.evaluate("() => document.querySelector('#nav a[data-view=ask]').click()")
        await pg.wait_for_timeout(900)
        await pg.fill("#ask-input", "What is the capital of France?")
        await pg.click("#ask-send")
        await pg.wait_for_timeout(2000)
        body = await pg.inner_text("#view")
        check("a question outside the source is declined",
              "does not cover" in body.lower(), body[-160:])
        await pg.screenshot(path=str(SHOTS / "10-ask.png"))

        # settings
        await pg.click("#account")
        await pg.wait_for_timeout(600)
        check("settings show the month's usage",
              "Used this month" in await pg.inner_text("#set-usage"))
        check("no key means no remove button",
              await pg.evaluate("() => document.getElementById('set-clear').offsetParent === null"))
        await pg.screenshot(path=str(SHOTS / "11-settings.png"))
        await pg.click("#set-close")
        await pg.wait_for_timeout(300)

        # ── bob, who must see none of it ─────────────────────────────────
        pg2 = await page_for(browser, BOB, "bob@e2e.test")
        await pg2.goto(base)
        await pg2.wait_for_timeout(2500)
        check("a second account sees an empty library",
              await pg2.evaluate("() => document.querySelectorAll('.doccard').length") == 0)
        check("and its own name",
              "bob@e2e.test" in await pg2.inner_text("#account-email"))
        await pg2.fill("#search", "reciprocity")
        await pg2.wait_for_timeout(900)
        check("and finds nothing of the first account's",
              "No matches" in await pg2.inner_text("#view"),
              (await pg2.inner_text("#view"))[:80])
        check("and the search header says so",
              "0 results" in await pg2.inner_text("#subtitle"),
              await pg2.inner_text("#subtitle"))
        await pg2.screenshot(path=str(SHOTS / "12-other-account.png"))
        await pg2.close()

        # ── a lapsed session returns to the gate ─────────────────────────
        await pg.evaluate("() => { window.__authcb && window.__authcb('SIGNED_OUT', null); }")
        await pg.wait_for_timeout(800)
        check("signing out returns to the sign-in screen",
              not await pg.evaluate("() => document.getElementById('gate').hidden"))
        await pg.screenshot(path=str(SHOTS / "13-signed-out-again.png"))

        # ── it works on a phone ──────────────────────────────────────────
        phone = await page_for(browser, ALICE, "alice@e2e.test")
        await phone.set_viewport_size({"width": 412, "height": 880})
        await phone.goto(base)
        await phone.wait_for_timeout(2200)
        check("the phone layout has a way back to the menu",
              await phone.evaluate(
                  "() => getComputedStyle(document.getElementById('menu')).display !== 'none'"))
        await phone.click("#menu")
        await phone.wait_for_timeout(500)
        check("the menu opens",
              await phone.evaluate(
                  "() => document.getElementById('rail').classList.contains('open')"))
        check("nothing scrolls sideways",
              await phone.evaluate(
                  "() => document.documentElement.scrollWidth <= window.innerWidth + 1"))
        await phone.screenshot(path=str(SHOTS / "14-phone.png"))
        await phone.close()

        await pg.close()
        await browser.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8410)
    ap.add_argument("--dsn", default="")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    home = Path(tempfile.mkdtemp(prefix="sunroom-e2e-"))
    env = {**os.environ,
           "PRISM_HOME": str(home), "PRISM_PROVIDER": "mock",
           "SUNROOM_ENV": "development",
           "SUNROOM_STORE": "postgres" if args.dsn else "sqlite",
           "SUNROOM_SECRET_KEY": secrets.token_urlsafe(48),
           "SUNROOM_WORKER_SECRET": "e2e-worker",
           "SUNROOM_RATE_LIMIT": "0", "PRISM_CHUNK_CHARS": "1100",
           "SUPABASE_URL": "https://e2e.supabase.co",
           "SUPABASE_ANON_KEY": "e2e-anon", "SUPABASE_JWT_SECRET": SECRET}
    if args.dsn:
        env["DATABASE_URL"] = args.dsn

    base = f"http://127.0.0.1:{args.port}"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "prism.web.api:app", "--host",
         "127.0.0.1", "--port", str(args.port), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT)
    try:
        if not wait_for_server(base):
            print("server never became healthy")
            return 1
        asyncio.run(run(base, args.headed))
    finally:
        if not args.keep:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            shutil.rmtree(home, ignore_errors=True)

    failed = [n for n, ok, _ in results if not ok]
    print()
    print(f"screenshots in {SHOTS}")
    if failed:
        print(f"{len(failed)} of {len(results)} checks failed: "
              + ", ".join(failed[:6]))
        return 1
    print(f"e2e: {len(results)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
