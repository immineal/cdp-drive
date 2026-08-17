# cdp-drive

A CDP browser driver in one file, no Playwright/Selenium install required.
It uses `--headless=new` real Chrome and the `websockets` package that
already ships with most system Python installs.

```
python3 drive.py '[{"goto": "https://example.com"}, {"text": true}]'
```

I wrote this while running an unattended agent that had to operate real
signup forms, file uploads, and logged-in sessions against production
sites for weeks. Playwright and Selenium both work fine; the reason to
reach for this instead is that it's ~400 lines you can read end to end
when something breaks, with no bundled browser binary and no test-mode
fingerprint to explain away.

## Gotchas fixed here

`Tab.__init__` connects with `ping_interval=None`. Leave that out and
`websockets`' own client-side keepalive will eventually go unanswered on
a JS-heavy single-page app, and you get `ConnectionClosedError: sent
1011 (internal error) keepalive ping timeout`. That looks like a site
problem. It's a library default — CDP itself is plain request/response
and doesn't need one.

Sites that render separate desktop and mobile markup sometimes reuse the
same `id` on both, hidden by a CSS media query instead of removed from
the DOM. `querySelector()` always returns the first match, not the
visible one. The `realtype` step's focus-fallback handles this: it walks
every match from `querySelectorAll()`, drops zero-size ones with
`getBoundingClientRect()`, then confirms with `elementFromPoint()` that
what's left is actually the element on top at its own coordinates before
clicking it (see the `rect = tab.js(...)` block).

## What it does not do

No CAPTCHA solving, no user-agent spoofing, no `navigator.webdriver`
masking. If a site's anti-bot check stops it, that's what the check is
for — this driver is for getting real input into forms that ignore a
bare `element.click()` or `.value = x`, not for hiding that a script is
driving the browser.

## License

MIT, see `LICENSE`.

## The rest of the bugs

This file already works around the ones above — including React-controlled
inputs that ignore a plain `.value` assignment, via the `realtype` step.
Two more that aren't fixed here — two state-changing clicks racing a
framework's own re-render, and telling a dead one-time token apart from
a mail scanner that already burned it — are written up with the full
diagnosis in [Agent Ops Cookbook](https://laterrr.gumroad.com/l/agent-ops-cookbook)
(PDF + this same driver, $9).
