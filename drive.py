#!/usr/bin/env python3
"""
drive.py — a real, honest browser that can click, type, and submit.

A from-scratch browser driver over the Chrome DevTools Protocol (CDP),
using `--headless=new` (a full Chrome engine — real JS, real cookies,
full CDP, not a stripped-down renderer) and the `websockets` library
that ships with system python3, so there's no Playwright/Selenium
install required to use it.

Built to operate honestly:
  * a real Chrome (headless=new), never disguised
  * the user-agent is NEVER touched — no stealth plugins, no webdriver
    masking, no fingerprint spoofing
  * if a site's defences block this, that IS the site's answer — this
    tool does not solve CAPTCHAs or route around anti-bot checks; every
    fix in it is about making an honest, correctly-identified automated
    action actually register with a framework that was listening for
    real input and not receiving it

If a platform's Terms of Service forbid automated/bot signups, or
require a natural person acting personally, don't use this tool to
create an account there.

Usage:
    drive.py <script.json>
    drive.py '[{"goto": "https://example.com"}, {"text": true}]'

The script is a JSON list of steps, executed in order, one JSON result
line printed to stdout per step so the caller can see what happened:
    {"goto": "https://example.com"}
    {"wait": 2.0}                        seconds
    {"wait_for": "#signup-form"}         until selector exists (timeout 20s)
    {"click": "button.submit"}
    {"type": {"sel": "#email", "text": "..."}}   plain HTML forms only, see note below
    {"realtype": {"sel": "#email", "text": "..."}}  React-controlled inputs — real keystrokes
    {"press": "Enter"}
    {"upload": {"sel": "input[type=file]", "files": ["/abs/path.pdf"]}}
                                          real OS-level file attach via CDP
                                          DOM.setFileInputFiles — not a fake
                                          FileList assignment, so the site's
                                          own onChange fires for real
    {"clickxy": {"x": 100, "y": 200}}    real coordinate click, for widgets a
                                          selector-driven click can't reach
    {"eval": "document.title"}           returns the value
    {"text": true}                       dump rendered page text
    {"shot": "/path/out.png"}
    {"url": true}                        current URL

The Chrome profile persists at ./browser-profile next to this script
across runs (cookies, any logged-in session).
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
CHROME = "/usr/bin/google-chrome-stable"
PROFILE = os.path.join(ROOT, "browser-profile")
PORT = 9223


def log(**kw):
    print(json.dumps(kw), flush=True)


def chrome_running() -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=2)
        return True
    except Exception:
        return False


def ensure_chrome() -> None:
    if chrome_running():
        return
    os.makedirs(PROFILE, exist_ok=True)
    subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
         f"--user-data-dir={PROFILE}",
         "--no-sandbox", "--no-first-run", "--no-default-browser-check",
         "--disable-gpu", "--disable-dev-shm-usage", "--window-size=1280,900",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        if chrome_running():
            time.sleep(1)
            return
        time.sleep(0.5)
    raise RuntimeError("Chrome did not expose a debugging port")


class Tab:
    """Minimal CDP client over websockets — already available to system
    python3, no Playwright/browser download needed."""

    def __init__(self):
        import websockets.sync.client as wsc
        pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=5))
        page = next((p for p in pages if p.get("type") == "page"), None)
        if page is None:
            raise RuntimeError("no page target available")
        self.ws = wsc.connect(
            page["webSocketDebuggerUrl"],
            max_size=64 * 1024 * 1024,
            ping_interval=None,  # CDP is req/response; ws-level keepalive only causes
        )                        # false 1011 timeouts on heavy SPAs — see the guide, recipe 1
        self._id = 0
        self.send("Page.enable")
        self.send("Runtime.enable")
        self.send("DOM.enable")

    def send(self, method: str, **params):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv(timeout=45))
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result", {})

    def js(self, expr: str):
        r = self.send("Runtime.evaluate", expression=expr,
                      returnByValue=True, awaitPromise=True)
        res = r.get("result", {})
        if r.get("exceptionDetails"):
            raise RuntimeError(str(r["exceptionDetails"].get("text", "js error")))
        return res.get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def q(sel: str) -> str:
    return json.dumps(sel)


def run(steps, tab: Tab) -> None:
    for i, step in enumerate(steps):
        log(step=i, starting=list(step.keys())[0])  # visible before a hang, not just after
        try:
            if "goto" in step:
                tab.send("Page.navigate", url=step["goto"])
                time.sleep(step.get("settle", 2.5))
                log(step=i, goto=step["goto"], url=tab.js("location.href"))

            elif "wait" in step:
                time.sleep(float(step["wait"]))
                log(step=i, waited=step["wait"])

            elif "wait_for" in step:
                sel, deadline = step["wait_for"], time.time() + step.get("timeout", 20)
                while time.time() < deadline:
                    if tab.js(f"!!document.querySelector({q(sel)})"):
                        log(step=i, wait_for=sel, found=True); break
                    time.sleep(0.4)
                else:
                    log(step=i, wait_for=sel, found=False)

            elif "clickxy" in step:
                # A genuine coordinate click at fixed viewport coordinates,
                # for elements a CSS selector can't reliably drive a
                # synthetic .click() into — e.g. list items inside a
                # virtualized/animated dropdown, where .click() on the <li>
                # closes the dropdown without registering the selection,
                # because the framework's real handler is wired to a native
                # mouse event sequence, not element.click(). Caller is
                # responsible for scrolling the target into view first (e.g.
                # via an "eval" step calling scrollIntoView()) since this
                # only knows x/y, not a selector.
                x, y = step["clickxy"]["x"], step["clickxy"]["y"]
                tab.send("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
                tab.send("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y,
                          button="left", clickCount=1)
                tab.send("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y,
                          button="left", clickCount=1)
                time.sleep(step.get("settle", 0.5))
                log(step=i, clickxy=[x, y])

            elif "click" in step:
                sel = step["click"]
                ok = tab.js(f"(()=>{{const e=document.querySelector({q(sel)});"
                            f"if(!e)return false;e.scrollIntoView({{block:'center'}});"
                            f"e.click();return true;}})()")
                # el.click() alone does not toggle a custom <div
                # role="checkbox">/<div role="radio"> widget (a form-builder's
                # own checkbox UI, not a native <input>) — the synthetic
                # click never moves document.activeElement and aria-checked
                # stays unchanged, even though the identical call works fine
                # on real <input>/<button>/<a> elements. Detect that specific
                # case (role is checkbox/radio and aria-checked is still
                # "false" after the click) and retry with the standard
                # keyboard-accessibility path every ARIA widget must support:
                # focus, then a real Space keydown/keyup. Only fires for
                # checkbox/radio roles, so normal link/button clicks are
                # untouched.
                if ok:
                    role = tab.js(f"(()=>{{const e=document.querySelector({q(sel)});"
                                   f"return e?e.getAttribute('role'):null;}})()")
                    if role in ("checkbox", "radio"):
                        checked = tab.js(f"document.querySelector({q(sel)}).getAttribute('aria-checked')")
                        if checked == "false":
                            tab.js(f"(()=>{{const e=document.querySelector({q(sel)});e.focus();return true;}})()")
                            time.sleep(0.15)
                            tab.send("Input.dispatchKeyEvent", type="keyDown", key=" ",
                                      code="Space", windowsVirtualKeyCode=32)
                            tab.send("Input.dispatchKeyEvent", type="keyUp", key=" ",
                                      code="Space", windowsVirtualKeyCode=32)
                time.sleep(step.get("settle", 1.5))
                log(step=i, click=sel, ok=bool(ok))

            elif "type" in step:
                sel, txt = step["type"]["sel"], step["type"]["text"]
                # Set value then fire input+change so frameworks notice.
                # Works for plain HTML forms (most server-rendered sites).
                # Fails silently on React-controlled inputs — their onChange
                # is wired through React's own value-setter override, which a
                # bare `el.value = ...` assignment bypasses entirely, so the
                # framework's internal state never updates even though the
                # DOM shows the right value. Use "realtype" for those (see
                # below).
                ok = tab.js(
                    f"(()=>{{const e=document.querySelector({q(sel)});if(!e)return false;"
                    f"e.focus();e.value={json.dumps(txt)};"
                    f"e.dispatchEvent(new Event('input',{{bubbles:true}}));"
                    f"e.dispatchEvent(new Event('change',{{bubbles:true}}));return true;}})()")
                log(step=i, typed_into=sel, ok=bool(ok), chars=len(txt))

            elif "realtype" in step:
                # Genuine simulated keystrokes via CDP Input.insertText, one
                # character at a time, after clicking the field to focus it
                # and clearing any existing content with a real select-all +
                # backspace. This is real input to operate a normal form
                # (the same standing it has for click) — it is what makes
                # React-controlled inputs actually update their internal
                # state, which a bare .value assignment does not.
                # click()/focus() alone does not always focus the field
                # either — on some pages e.focus() runs without error but
                # document.activeElement stays <body>, so every inserted
                # character lands nowhere. Call .focus() directly and check
                # it actually took, rather than trusting a return value —
                # Input.insertText only ever reaches whatever element
                # currently has real focus.
                sel, txt = step["realtype"]["sel"], step["realtype"]["text"]
                ok = tab.js(f"(()=>{{const e=document.querySelector({q(sel)});"
                            f"if(!e)return false;e.scrollIntoView({{block:'center'}});"
                            f"e.focus();return document.activeElement===e;}})()")
                if not ok:
                    # .focus() alone doesn't always establish real focus on
                    # every field (seen on React-portal-rendered form
                    # elements: document.activeElement stayed <body> even
                    # though querySelector finds the element and .focus()
                    # runs without error). Fall back to a genuine
                    # coordinate-based mouse click at the field's real
                    # on-screen position — this is real input directed at a
                    # normal form field, not an anti-bot widget.
                    # A selector can also match more than one element (found
                    # on a page with a duplicated desktop/mobile render of
                    # the same form, two elements sharing one id).
                    # querySelector() always returns the FIRST match
                    # regardless of which one is actually visible and on
                    # top, so use elementFromPoint() to find whichever match
                    # is really interactable at its own coordinates before
                    # clicking it.
                    rect = tab.js(
                        f"(()=>{{const els=[...document.querySelectorAll({q(sel)})];"
                        f"for(const e of els){{const r=e.getBoundingClientRect();"
                        f"if(r.width===0||r.height===0)continue;"
                        f"const cx=r.x+r.width/2,cy=r.y+r.height/2;"
                        f"const top=document.elementFromPoint(cx,cy);"
                        f"if(top===e||(top&&e.contains(top)))return {{x:cx,y:cy}};}}"
                        f"return null;}})()")
                    if rect:
                        x, y = rect["x"], rect["y"]
                        tab.send("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
                        tab.send("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y,
                                  button="left", clickCount=1)
                        tab.send("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y,
                                  button="left", clickCount=1)
                        time.sleep(0.2)
                        ok = tab.js(
                            f"(()=>{{const a=document.activeElement;"
                            f"return !!a && [...document.querySelectorAll({q(sel)})].includes(a);}})()")
                if not ok:
                    # Don't let this masquerade as success — verify real
                    # focus took, via document.activeElement, after BOTH the
                    # .focus() attempt and the coordinate-click fallback,
                    # and report false rather than typing into nothing.
                    log(step=i, realtyped_into=sel, ok=False,
                        error="focus not established (checked document.activeElement "
                              "after .focus() and after coordinate click)")
                    continue
                time.sleep(0.2)
                # select-all (ctrl+a, modifiers bitmask 2) then backspace
                tab.send("Input.dispatchKeyEvent", type="keyDown", key="a",
                          code="KeyA", windowsVirtualKeyCode=65, modifiers=2)
                tab.send("Input.dispatchKeyEvent", type="keyUp", key="a",
                          code="KeyA", windowsVirtualKeyCode=65, modifiers=2)
                tab.send("Input.dispatchKeyEvent", type="keyDown", key="Backspace",
                          windowsVirtualKeyCode=8)
                tab.send("Input.dispatchKeyEvent", type="keyUp", key="Backspace",
                          windowsVirtualKeyCode=8)
                for ch in txt:
                    tab.send("Input.insertText", text=ch)
                    time.sleep(0.015)
                log(step=i, realtyped_into=sel, ok=bool(ok), chars=len(txt))

            elif "press" in step:
                key = step["press"]
                code = {"Enter": 13, "Tab": 9, "Escape": 27}.get(key, 0)
                for t in ("keyDown", "char", "keyUp"):
                    tab.send("Input.dispatchKeyEvent", type=t, key=key,
                             windowsVirtualKeyCode=code, nativeVirtualKeyCode=code,
                             text="\r" if key == "Enter" and t == "char" else "")
                time.sleep(step.get("settle", 1.5))
                log(step=i, pressed=key)

            elif "upload" in step:
                # A real OS-level file attach via CDP DOM.setFileInputFiles,
                # not a fake FileList assignment — the browser treats it
                # exactly as if a human picked the file in the native
                # dialog, so React/whatever framework's own onChange fires
                # for real. Needs an objectId (a live JS object reference),
                # not a nodeId, which is why this goes through
                # Runtime.evaluate with returnByValue=False rather than
                # tab.js() (which always returns a plain value).
                sel = step["upload"]["sel"]
                files = step["upload"]["files"]
                r = tab.send("Runtime.evaluate",
                             expression=f"document.querySelector({q(sel)})",
                             returnByValue=False)
                obj = r.get("result", {})
                if not obj.get("objectId"):
                    log(step=i, upload=sel, ok=False, error="selector not found")
                else:
                    tab.send("DOM.setFileInputFiles", files=files,
                              objectId=obj["objectId"])
                    log(step=i, upload=sel, ok=True, files=files)

            elif "eval" in step:
                log(step=i, eval=step["eval"], value=tab.js(step["eval"]))

            elif "text" in step:
                txt = tab.js(
                    "document.body ? document.body.innerText.replace(/\\n{3,}/g,'\\n\\n') : ''")
                log(step=i, text=(txt or "")[: step.get("limit", 4000)])

            elif "url" in step:
                log(step=i, url=tab.js("location.href"))

            elif "shot" in step:
                r = tab.send("Page.captureScreenshot", format="png")
                with open(step["shot"], "wb") as fh:
                    fh.write(base64.b64decode(r["data"]))
                log(step=i, shot=step["shot"], bytes=os.path.getsize(step["shot"]))

            else:
                log(step=i, error=f"unknown step: {list(step)}")

        except Exception as exc:  # noqa: BLE001
            log(step=i, error=f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(64)
    arg = sys.argv[1]
    steps = json.loads(open(arg).read()) if os.path.exists(arg) else json.loads(arg)
    ensure_chrome()
    tab = Tab()
    try:
        run(steps if isinstance(steps, list) else [steps], tab)
    finally:
        tab.close()
