"""
Page layouts for our server-rendered pages: a narrow card (sign-in, Connect,
messages) and a wide app layout (the dashboard). No scripts anywhere -- every
action is a plain form post -- so the security policy can forbid all script.

Every value shown must go through `escape()`.
"""

from __future__ import annotations

from html import escape

from fastapi.responses import HTMLResponse

_STYLE = """
:root { --bg:#f4f6f9; --card:#fff; --fg:#18202d; --muted:#5b6577; --line:#dce1e8;
        --soft:#eef1f6; --accent:#2f4a8a; --accent-fg:#fff;
        --ok:#1f7a3a; --ok-bg:#e3f3e7; --warn:#8a5a00; --warn-bg:#fbf0d9;
        --bad:#b42318; --bad-bg:#fbe7e5; color-scheme: light; }
@media (prefers-color-scheme: dark) { :root { --bg:#10141b; --card:#181f29; --fg:#e4e8ef;
        --muted:#9aa4b5; --line:#2a3240; --soft:#1f2733; --accent:#8fa8e8; --accent-fg:#10141b;
        --ok:#7fd296; --ok-bg:#163020; --warn:#f0c66b; --warn-bg:#2f2614;
        --bad:#f19a91; --bad-bg:#371a17; color-scheme: dark; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif; }
a { color:var(--accent); }
main.narrow { max-width:32rem; margin:4rem auto; padding:0 1rem; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:1.5rem; }
h1 { font-size:1.35rem; margin:0 0 .75rem; text-wrap:balance; }
h2 { font-size:1.05rem; margin:0; }
p { margin:.5rem 0; } .muted { color:var(--muted); font-size:.88rem; }
form { margin:0; } .row { display:flex; gap:.75rem; margin-top:1.25rem; flex-wrap:wrap; }
label { display:block; margin:.75rem 0 .25rem; font-weight:600; font-size:.88rem; }
input[type=email], input[type=text], textarea { width:100%; padding:.55rem .65rem;
       border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--fg);
       font:inherit; }
textarea { min-height:5rem; resize:vertical; }
button { padding:.5rem 1rem; border-radius:6px; border:1px solid var(--line);
         background:var(--card); color:var(--fg); font:inherit; cursor:pointer; }
button.primary { background:var(--accent); border-color:var(--accent); color:var(--accent-fg); }
button.danger { color:var(--bad); border-color:var(--bad); }
button:focus-visible, input:focus-visible, textarea:focus-visible, summary:focus-visible {
       outline:2px solid var(--accent); outline-offset:2px; }
ul { padding-left:1.2rem; } code { font-size:.85em; }

/* --- app layout -------------------------------------------------------------- */
.bar { background:var(--card); border-bottom:1px solid var(--line); }
.bar-in { max-width:72rem; margin:0 auto; padding:.75rem 1rem; display:flex; gap:1rem;
          align-items:center; flex-wrap:wrap; }
.brand { font-weight:700; letter-spacing:.01em; }
.brand span { color:var(--muted); font-weight:500; }
.who { margin-left:auto; display:flex; gap:.75rem; align-items:center; flex-wrap:wrap;
       font-size:.9rem; color:var(--muted); }
main.app { max-width:72rem; margin:1.5rem auto 4rem; padding:0 1rem; display:grid; gap:1.25rem; }
.notice { padding:.65rem .9rem; border-radius:8px; border:1px solid var(--line); }
.notice.ok { background:var(--ok-bg); color:var(--ok); border-color:transparent; }
.notice.bad { background:var(--bad-bg); color:var(--bad); border-color:transparent; }
.tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(11rem,1fr)); gap:1rem; }
.tile { background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:1rem 1.1rem; }
.tile .k { font-size:.78rem; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); }
.tile .v { font-size:1.7rem; font-weight:650; margin-top:.15rem;
           font-variant-numeric:tabular-nums; }
.tile .s { font-size:.82rem; color:var(--muted); }
section.panel { background:var(--card); border:1px solid var(--line); border-radius:10px; }
.panel-h { display:flex; align-items:center; gap:1rem; padding:1rem 1.1rem;
           border-bottom:1px solid var(--line); flex-wrap:wrap; }
.panel-h .muted { margin:0; }
.panel-b { padding:1rem 1.1rem; }
.scroll { overflow-x:auto; }
table { width:100%; border-collapse:collapse; font-size:.9rem; }
th, td { text-align:left; padding:.55rem .6rem; border-bottom:1px solid var(--line);
         vertical-align:top; }
th { font-size:.75rem; text-transform:uppercase; letter-spacing:.06em; color:var(--muted);
     font-weight:600; }
tr:last-child td { border-bottom:0; }
td.num { font-variant-numeric:tabular-nums; text-align:right; white-space:nowrap; }
td.nowrap { white-space:nowrap; }
.sql { font-family:ui-monospace,Consolas,monospace; font-size:.8rem; color:var(--muted);
       max-width:34rem; overflow-wrap:anywhere; }
.pill { display:inline-block; padding:.08rem .5rem; border-radius:999px; font-size:.76rem;
        font-weight:600; white-space:nowrap; }
.pill.ok { background:var(--ok-bg); color:var(--ok); }
.pill.warn { background:var(--warn-bg); color:var(--warn); }
.pill.bad { background:var(--bad-bg); color:var(--bad); }
.pill.plain { background:var(--soft); color:var(--muted); }
.conns { display:grid; gap:.9rem; }
.conn { border:1px solid var(--line); border-radius:8px; padding:.9rem 1rem; }
.conn-h { display:flex; gap:.6rem; align-items:center; flex-wrap:wrap; }
.conn-h strong { font-size:1rem; }
.conn dl { display:grid; grid-template-columns:max-content 1fr; gap:.25rem .9rem;
           margin:.6rem 0 0; font-size:.88rem; }
.conn dt { color:var(--muted); } .conn dd { margin:0; overflow-wrap:anywhere; }
details { margin-top:.6rem; } summary { cursor:pointer; color:var(--accent); font-size:.88rem; }
details form { margin-top:.5rem; }
.url { font-family:ui-monospace,Consolas,monospace; background:var(--soft); padding:.45rem .6rem;
       border-radius:6px; overflow-wrap:anywhere; user-select:all; display:block; }
.steps { margin:.25rem 0 0; padding-left:1.2rem; } .steps li { margin:.3rem 0; }
.empty { color:var(--muted); padding:.5rem 0; }
@media (max-width:40rem) { .conn dl { grid-template-columns:1fr; } }
"""

_HEADERS = {
    # These pages show who is signed in and carry CSRF tokens: never cache
    # them, and never let another site frame them (clickjacking the buttons).
    # No scripts at all. No `form-action` rule on purpose: browsers apply it
    # to where a form submission *redirects*, and Allow redirects to Claude --
    # the rule would silently stop people from ever getting back.
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                               "frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
}


def _document(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)} - IQ Connect</title><style>{_STYLE}</style></head>"
        f"<body>{body}</body></html>"
    )


def page(title: str, body_html: str, status_code: int = 200) -> HTMLResponse:
    """A narrow card. `body_html` must already be escaped."""
    html = _document(title, f"<main class='narrow'><div class='card'>{body_html}</div></main>")
    return HTMLResponse(html, status_code=status_code, headers=_HEADERS)


def app_page(title: str, *, email: str, csrf: str, body_html: str,
             status_code: int = 200) -> HTMLResponse:
    """The wide app layout with a top bar. `body_html` must already be escaped."""
    bar = (
        "<header class='bar'><div class='bar-in'>"
        "<div class='brand'>IQ Connect <span>&middot; your data, read-only, in Claude</span></div>"
        f"<div class='who'><span>{escape(email)}</span>"
        "<form method='post' action='/auth/logout'>"
        f"<input type='hidden' name='csrf' value='{escape(csrf)}'>"
        "<button type='submit'>Sign out</button></form></div>"
        "</div></header>"
    )
    html = _document(title, f"{bar}<main class='app'>{body_html}</main>")
    return HTMLResponse(html, status_code=status_code, headers=_HEADERS)


def message(title: str, text: str, status_code: int = 400) -> HTMLResponse:
    return page(title, f"<h1>{escape(title)}</h1><p>{escape(text)}</p>", status_code)


__all__ = ["app_page", "escape", "message", "page"]
