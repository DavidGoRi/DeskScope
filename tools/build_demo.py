"""Build the static live demo (GitHub Pages) from the real application.

    python tools/build_demo.py _site --base /DeskScope
    python tools/build_demo.py _demo --file-urls      # for local screenshots

For each language (es, en) it creates the fictitious demo company, signs in as
three people (administrator, technician, employee) through the real web app,
crawls the pages each one can see and saves them as static HTML with links
rewritten. Forms are disabled by ``demo.js``; knowledge base suggestions keep
working in the browser from a small JSON file. Every name and number is
fictitious (example.com).
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from deskscope import knowledge, security, users  # noqa: E402
from deskscope.db import connect, init_db  # noqa: E402
from deskscope.seed import DEMO_PASSWORD, seed_demo, seed_integrations  # noqa: E402
from deskscope.sla import load_policy  # noqa: E402
from deskscope.web import create_app  # noqa: E402

LANGS = ("es", "en")
PEOPLE = (
    ("laura.sanchez", {"es": "administradora", "en": "administrator"}),
    ("carlos.ruiz", {"es": "técnico", "en": "technician"}),
    ("ana.garcia", {"es": "empleada", "en": "employee"}),
)
ALLOWED = re.compile(r"^/(tickets(/\d+|/new)?|kb(/\d+)?|reports|account/tokens|admin/users)$")
START = ["/tickets", "/tickets?status=any", "/tickets?sla=problems&sort=sla", "/tickets/new",
         "/kb", "/reports", "/reports?days=7", "/reports?days=90", "/account/tokens",
         "/admin/users"]
STATUS_VALUES = {"active", "any", "new", "open", "pending", "resolved", "closed"}
REPO = "https://github.com/DavidGoRi/DeskScope"

TEXT = {
    "es": {
        "banner": "Demo de solo lectura con datos ficticios. Estás viendo DeskScope como "
                  "<b>{name}</b> ({role}).",
        "switch": "Cambiar de persona",
        "blocked": "Esto es una demo de solo lectura: los formularios están desactivados. "
                   "Instálalo en dos minutos para probarlo todo.",
    },
    "en": {
        "banner": "Read-only demo with fictitious data. You are seeing DeskScope as "
                  "<b>{name}</b> ({role}).",
        "switch": "Switch person",
        "blocked": "This is a read-only demo: forms are disabled. Install it in two minutes "
                   "to try everything.",
    },
}


def normalise(url: str) -> tuple[str, tuple] | None:
    """(path, kept query) for pages the demo contains, or None."""
    parts = urlsplit(html.unescape(url))
    path = parts.path.rstrip("/") or "/"
    if not ALLOWED.match(path):
        return None
    query = dict(parse_qsl(parts.query))
    kept: tuple = ()
    if path == "/reports" and query.get("days") in ("7", "90"):
        kept = (("days", query["days"]),)
    elif path == "/tickets":
        if query.get("sla") == "problems":
            kept = (("sla", "problems"),)
        elif query.get("status") in STATUS_VALUES - {"active"} and query.get("scope", "all") == "all" \
                and not query.get("q"):
            kept = (("status", query["status"]),)
    return path, kept


def slug(path: str, kept: tuple) -> str:
    name = path.strip("/")
    if kept:
        name += "/" + "-".join(f"{k}-{v}" for k, v in kept)
    return name


class Site:
    def __init__(self, out: Path, base: str, file_urls: bool):
        self.out, self.base, self.file_urls = out, base.rstrip("/"), file_urls

    def page_url(self, lang: str, person: str, path: str, kept: tuple = ()) -> str:
        url = f"{self.base}/{lang}/{person}/{slug(path, kept)}/"
        return url + ("index.html" if self.file_urls else "")

    def root(self) -> str:
        return f"{self.base}/" + ("index.html" if self.file_urls else "")

    def rewrite(self, page: str, lang: str, person: str) -> str:
        def fix(match: re.Match) -> str:
            attr, url = match.group(1), html.unescape(match.group(2))
            if url.startswith("/static/"):
                target = self.base + url
            elif url.startswith("/lang/"):
                other = url.split("/")[2].split("?")[0]
                nxt = dict(parse_qsl(urlsplit(url).query)).get("next", "/tickets")
                found = normalise(nxt) or ("/tickets", ())
                target = self.page_url(other, person, *found)
            elif url == "/logout":
                target = self.root()
            elif url.startswith("/api/docs"):
                target = REPO + "#rest-api"
            elif url.startswith("/reports/export.csv"):
                days = dict(parse_qsl(urlsplit(url).query)).get("days", "30")
                target = f"{self.base}/{lang}/{person}/reports/export-{days}.csv"
            else:
                found = normalise(url)
                target = self.page_url(lang, person, *found) if found else "#"
            return f'{attr}="{html.escape(target, quote=True)}"'

        return re.sub(r'\b(href|action|src)="(/[^"]*)"', fix, page)


def build(out: Path, base: str, file_urls: bool) -> int:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copytree(ROOT / "src" / "deskscope" / "static", out / "static")
    (out / "static" / "demo.js").write_text(DEMO_JS, "utf-8")
    site = Site(out, base, file_urls)
    security.ITERATIONS = 1000  # demo accounts only; keeps the build fast
    policy = load_policy(ROOT / "sla.example.json")
    pages = 0
    names: dict[str, str] = {}

    with tempfile.TemporaryDirectory() as tmp:
        for lang in LANGS:
            db = Path(tmp) / f"demo-{lang}.db"
            init_db(db)
            conn = connect(db)
            seed_demo(conn, lang=lang, history=60)
            seed_integrations(conn, lang=lang)
            conn.close()
            app = create_app(db, "demo-build-key", sla_policy=policy)
            for person, roles in PEOPLE:
                with TestClient(app) as client:
                    client.cookies.set("lang", lang)
                    token = re.search(r'name="csrf_token" value="([^"]+)"',
                                      client.get("/login").text).group(1)
                    client.post("/login", data={"username": person, "password": DEMO_PASSWORD,
                                                "csrf_token": token, "next": "/tickets"})
                    conn = connect(db)
                    user = users.get_by_username(conn, person)
                    names[person] = user.full_name
                    suggestions = [
                        {"id": a["id"], "title": a["title"]}
                        for a in knowledge.search(conn, user, "", limit=200)
                        if a["status"] == "published"
                    ]
                    conn.close()
                    folder = out / lang / person
                    folder.mkdir(parents=True, exist_ok=True)
                    (folder / "kb-suggest.json").write_text(
                        json.dumps(suggestions, ensure_ascii=False), "utf-8")
                    pages += crawl(client, site, lang, person, user.full_name, roles[lang])
    (out / "index.html").write_text(landing(site, names), "utf-8")
    (out / "404.html").write_text(landing(site, names), "utf-8")
    (out / ".nojekyll").write_text("", "utf-8")
    return pages


def crawl(client, site: Site, lang: str, person: str, name: str, role: str) -> int:
    queue = [normalise(u) for u in START]
    seen: set = set()
    saved = 0
    while queue:
        item = queue.pop(0)
        if item is None or item in seen:
            continue
        seen.add(item)
        path, kept = item
        url = path + ("?" + "&".join(f"{k}={v}" for k, v in kept) if kept else "")
        response = client.get(url, follow_redirects=False)
        if response.status_code != 200 or "text/html" not in response.headers.get("content-type", ""):
            continue
        page = response.text
        for link in re.findall(r'href="(/[^"]*)"', page):
            found = normalise(link)
            if found and found not in seen:
                queue.append(found)
        page = site.rewrite(page, lang, person)
        banner = (
            f'<div class="demo-banner">{TEXT[lang]["banner"].format(name=html.escape(name), role=role)} '
            f'<a href="{site.root()}">{TEXT[lang]["switch"]}</a></div>'
        )
        page = page.replace('<main class="page">', banner + '\n  <main class="page">', 1)
        config = json.dumps({
            "suggest": site.base + f"/{lang}/{person}/kb-suggest.json",
            "blocked": TEXT[lang]["blocked"],
            "root": site.root(),
            "articleUrl": site.base + f"/{lang}/{person}/kb/{{id}}/" + ("index.html" if site.file_urls else ""),
        }, ensure_ascii=False)
        script = (f'<script>window.DESKSCOPE_DEMO = {config};</script>\n'
                  f'  <script src="{site.base}/static/demo.js"></script>\n')
        page = page.replace('<script src="', script + '  <script src="', 1)
        target = site.out / lang / person / slug(path, kept) / "index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page, "utf-8")
        saved += 1
    for days in ("7", "30", "90"):  # the CSV export, for people who can see metrics
        response = client.get(f"/reports/export.csv?days={days}", follow_redirects=False)
        if response.status_code == 200:
            target = site.out / lang / person / "reports" / f"export-{days}.csv"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(response.content)
    return saved


def landing(site: Site, names: dict[str, str]) -> str:
    cards = []
    for person, roles in PEOPLE:
        links = " · ".join(
            f'<a href="{site.page_url(lang, person, "/tickets")}">{label}</a>'
            for lang, label in (("es", "Español"), ("en", "English"))
        )
        cards.append(
            f'<section class="card"><h2>{html.escape(names[person])}</h2>'
            f'<p class="muted">{roles["es"].capitalize()} · {roles["en"]}</p><p>{links}</p></section>'
        )
    return LANDING.format(base=site.base, cards="\n".join(cards), repo=REPO)


LANDING = """<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DeskScope · Live demo</title>
  <link rel="icon" href="{base}/static/favicon.svg" type="image/svg+xml">
  <link rel="stylesheet" href="{base}/static/style.css">
</head>
<body>
  <main class="page landing">
    <div class="landing-head">
      <img src="{base}/static/favicon.svg" alt="" width="48" height="48">
      <div>
        <h1>DeskScope</h1>
        <p class="muted">Service desk ligero: tickets, SLA ITIL, base de conocimiento, métricas e integración con PCScope, NetScope y ADFlow.<br>
        Lightweight IT help desk: tickets, ITIL SLAs, knowledge base, metrics and PCScope / NetScope / ADFlow integration.</p>
      </div>
    </div>
    <p>Elige con quién entrar · Choose who to sign in as:</p>
    <div class="landing-grid">
{cards}
    </div>
    <p class="muted">Demo de solo lectura con una empresa ficticia (example.com): 60 días de historial, tickets abiertos por las integraciones, artículos y métricas.
    · Read-only demo of a fictitious company (example.com).</p>
    <p><a class="btn" href="{repo}">GitHub · código, instalación y API</a></p>
  </main>
</body>
</html>
"""

DEMO_JS = """// Read-only demo helpers (only in the static GitHub Pages build).
(function () {
  "use strict";
  var cfg = window.DESKSCOPE_DEMO || {};

  function toast(text) {
    var box = document.createElement("div");
    box.className = "demo-toast";
    box.textContent = text;
    document.body.appendChild(box);
    setTimeout(function () { box.remove(); }, 4000);
  }

  document.addEventListener("submit", function (event) {
    event.preventDefault();
    if (event.target.getAttribute("action") === cfg.root) {
      window.location.href = cfg.root;  // "Sign out" goes back to the person picker
      return;
    }
    toast(cfg.blocked || "Read-only demo");
  }, true);

  // Knowledge base suggestions: same behaviour as the server, from a static list.
  var articles = null;
  var realFetch = window.fetch ? window.fetch.bind(window) : null;
  if (!realFetch) return;
  window.fetch = function (url, options) {
    if (typeof url === "string" && url.indexOf("/kb/suggest?q=") === 0) {
      var words = decodeURIComponent(url.split("q=")[1] || "").toLowerCase()
        .normalize("NFD").replace(/[\\u0300-\\u036f]/g, "").split(/\\W+/)
        .filter(function (w) { return w.length > 3; });
      var load = articles ? Promise.resolve(articles) :
        realFetch(cfg.suggest).then(function (r) { return r.json(); })
          .then(function (list) { articles = list; return list; });
      return load.then(function (list) {
        var hits = list.filter(function (a) {
          var title = a.title.toLowerCase().normalize("NFD").replace(/[\\u0300-\\u036f]/g, "");
          return words.some(function (w) { return title.indexOf(w.slice(0, 5)) !== -1; });
        }).slice(0, 5).map(function (a) { return { id: a.id, title: a.title }; });
        return new Response(JSON.stringify(hits), { headers: { "Content-Type": "application/json" } });
      });
    }
    return realFetch(url, options);
  };

  // Suggestion links point to /kb/<id>: send them to the static article page.
  document.addEventListener("click", function (event) {
    var link = event.target.closest && event.target.closest(".suggestions a");
    if (!link) return;
    var id = (link.getAttribute("href") || "").split("/").pop();
    event.preventDefault();
    window.open(cfg.articleUrl.replace("{id}", id), "_blank", "noopener");
  });
})();
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("--base", default="", help="URL prefix, e.g. /DeskScope on GitHub Pages")
    parser.add_argument("--file-urls", action="store_true",
                        help="absolute file:// links ending in index.html (local screenshots)")
    args = parser.parse_args()
    out = args.out.resolve()
    base = args.base
    if args.file_urls:
        base = out.as_uri()
    pages = build(out, base, args.file_urls)
    print(f"Demo written to {out}: {pages} pages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
