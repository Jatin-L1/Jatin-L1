#!/usr/bin/env python3
"""Render the profile README cards into cards/.

    python scripts/render_cards.py            # animated cards (what you commit)
    STATIC=1 python scripts/render_cards.py   # final frame only, plus build/preview.html
    python scripts/render_cards.py --sample   # synthetic data, for trying the layout

Every card is a self-contained SVG meant for an <img> tag, so it uses only:
inline <style> with CSS keyframes, SMIL <animate>, and fonts subset with
pyftsubset and embedded as base64 WOFF2 data URIs. All animation is switched
off under prefers-reduced-motion (CSS animations via `animation:none`; the few
SMIL elements are swapped for a static twin).

Contribution data comes from data/contributions.json (written daily by
scripts/fetch_contributions.py). Optional repo stats come from data/repos.json.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
from collections import OrderedDict
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ROOT = Path(__file__).resolve().parent.parent
CARDS_DIR = Path(os.environ.get("CARDS_DIR", ROOT / "cards"))
BUILD_DIR = ROOT / "build"
STATIC = os.environ.get("STATIC", "").lower() not in ("", "0", "false", "no")

# --------------------------------------------------------------------------- #
# Content & theme: edit these.
# --------------------------------------------------------------------------- #
LOGIN = "Jatin-L1"
NAME = "Jatin Sharma"
EYEBROW = "hi, I’m"
TAGLINE = "Full-stack, AI & blockchain developer. HackIndia 2025 winner."

# Colours are taken from the existing README (0D1117 background, 58A6FF brand,
# FF6B6B / 4ECDC4 / FFA116 / 96CEB4 accents, C9D1D9 text).
THEME = dict(
    bg="#0d1117",
    panel="#161b22",
    border="#30363d",
    text="#c9d1d9",
    bright="#f0f6fc",
    muted="#8b949e",
    brand="#58a6ff",
    brand_deep="#1f6feb",
    hot="#ff6b6b",
)

# Fonts: Fira Code is the README's existing typing-SVG font; Inter pairs with it.
# Swap files/axes here (and in assets/fonts/) if your website uses something else.
FACES = {
    "sans": dict(family="Inter", weight=400, file="Inter[opsz,wght].ttf", axes=dict(wght=400, opsz=14)),
    "bold": dict(family="Inter", weight=700, file="Inter[opsz,wght].ttf", axes=dict(wght=700, opsz=28)),
    "mono": dict(family="Fira Code", weight=500, file="FiraCode[wght].ttf", axes=dict(wght=500)),
}
STACK_SANS = "Inter,'Segoe UI',system-ui,-apple-system,sans-serif"
STACK_MONO = "'Fira Code',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"

TILES = [
    dict(slug="portfolio", label="PORTFOLIO", title="See my work", sub="jatin01-sharma.vercel.app",
         url="https://jatin01-sharma.vercel.app", accent="#96ceb4", icon="window"),
    dict(slug="linkedin", label="LINKEDIN", title="Let’s connect", sub="in/jatin-sharma-391830283",
         url="https://www.linkedin.com/in/jatin-sharma-391830283/", accent="#4ecdc4", icon="in"),
    dict(slug="leetcode", label="LEETCODE", title="Challenge me", sub="Jatin_10_54",
         url="https://leetcode.com/Jatin_10_54", accent="#ffa116", icon="braces"),
]

# blurb/highlight/tags are yours; stars/forks/language come from data/repos.json.
FEATURED = [
    dict(repo="grainlyy",
         blurb="Blockchain-based ration tracking with tamper-proof records and a multi-role dashboard.",
         highlight="HackIndia 2025 · 1st place of 200+ teams",
         tags=["Solidity", "Ethereum", "Web3.js"]),
    dict(repo="hackrx-doc-intelligence",
         blurb="Document intelligence platform: ask questions over large documents and get grounded answers.",
         highlight="HackRx 2025 Top 500 · 90%+ accuracy · 80% less time",
         tags=["Next.js", "FastAPI", "Gemini AI", "Pinecone"]),
    dict(repo="skillsync",
         blurb="Resume intelligence with NLP that turns slow manual screening into a two-minute pass.",
         highlight="30 min → 2 min · 25% accuracy boost",
         tags=["Next.js", "Node.js", "NLP"]),
]

# Seconds from page load. Cards play top to bottom in this order.
TL = dict(hero=0.0, tiles=0.5, tile_step=0.12, chart=1.1, repos=1.9, repo_step=0.35, graph=3.1)

LANG_COLORS = {
    "Solidity": "#aa6746", "TypeScript": "#3178c6", "JavaScript": "#f1e05a", "Python": "#3572a5",
    "Java": "#b07219", "Jupyter Notebook": "#da5b0b", "HTML": "#e34c26", "CSS": "#563d7c",
    "C++": "#f34b7d", "C": "#555555", "Go": "#00add8", "Rust": "#dea584", "Shell": "#89e051",
}

W = 840  # width of full-width cards (the README column is ~880px)
PAD = 36


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def esc(s) -> str:
    return html.escape(str(s), quote=True)


def mix(a: str, b: str, t: float) -> str:
    ca = [int(a.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    cb = [int(b.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(ca, cb))


def fmt_date(d: dt.date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def fmt_count(n: int) -> str:
    return f"{n / 1000:.1f}k".replace(".0k", "k") if n >= 1000 else str(n)


# --------------------------------------------------------------------------- #
# Fonts: instance variable fonts, measure with them, subset per card
# --------------------------------------------------------------------------- #
_instances: dict[str, Path] = {}
_metrics: dict[str, "Metrics"] = {}
_subsets: dict[tuple, str] = {}


def instance_path(key: str) -> Path:
    if key in _instances:
        return _instances[key]
    spec = FACES[key]
    src = ROOT / "assets" / "fonts" / spec["file"]
    if not src.exists():
        sys.exit(f"Missing font file: {src}")
    cache = BUILD_DIR / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / f"{key}.ttf"
    if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
        font = TTFont(src)
        inst = instancer.instantiateVariableFont(font, dict(spec["axes"]), inplace=False)
        inst.save(out)
    _instances[key] = out
    return out


class Metrics:
    """Advance-width text measurement so wrapping/right-alignment is exact."""

    def __init__(self, path: Path):
        font = TTFont(path)
        self.cmap = font.getBestCmap()
        self.hmtx = font["hmtx"]
        self.upem = font["head"].unitsPerEm

    def width(self, s: str, size: float, ls: float = 0.0) -> float:
        total = 0
        for ch in s:
            g = self.cmap.get(ord(ch), ".notdef")
            total += self.hmtx[g][0] if g in self.hmtx.metrics else self.hmtx[".notdef"][0]
        return total / self.upem * size + ls * len(s)


def metrics(key: str) -> Metrics:
    if key not in _metrics:
        _metrics[key] = Metrics(instance_path(key))
    return _metrics[key]


def subset_b64(key: str, chars: set[str]) -> str:
    """Subset one face to exactly these characters with pyftsubset -> WOFF2 base64."""
    text = "".join(sorted(chars | {" "}))
    ck = (key, text)
    if ck in _subsets:
        return _subsets[ck]
    exe = shutil.which("pyftsubset")
    base = [exe] if exe else [sys.executable, "-m", "fontTools.subset"]
    with tempfile.TemporaryDirectory() as td:
        txt, out = Path(td) / "t.txt", Path(td) / "f.woff2"
        txt.write_text(text, encoding="utf-8")
        subprocess.run(
            base + [str(instance_path(key)), f"--text-file={txt}", "--flavor=woff2",
                    f"--output-file={out}", "--layout-features=kern,tnum", "--no-hinting",
                    "--desubroutinize"],
            check=True, capture_output=True)
        b64 = base64.b64encode(out.read_bytes()).decode()
    _subsets[ck] = b64
    return b64


# --------------------------------------------------------------------------- #
# CSS
# --------------------------------------------------------------------------- #
BASE_CSS = (
    ".sans{font-family:%s;font-weight:400}"
    ".bold{font-family:%s;font-weight:700}"
    ".mono{font-family:%s;font-weight:500;font-variant-ligatures:none}"
) % (STACK_SANS, STACK_SANS, STACK_MONO)

ANIM_CSS = """
.a{animation-duration:.7s;animation-timing-function:cubic-bezier(.2,.7,.2,1);animation-fill-mode:both}
.fade{animation-name:fade}
.rise{animation-name:rise}
.pop{animation-name:pop;transform-box:fill-box;transform-origin:50% 50%}
.grow{animation-name:grow;animation-duration:.9s;transform-box:fill-box;transform-origin:50% 100%}
.growx{animation-name:growx;transform-box:fill-box;transform-origin:0 50%}
.growy{animation-name:growy;transform-box:fill-box;transform-origin:50% 0}
.spin{animation:spin 48s linear infinite;transform-box:view-box}
.breathe{animation:breathe 5s ease-in-out infinite}
.still{display:none}
@keyframes fade{from{opacity:0}to{opacity:1}}
@keyframes rise{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
@keyframes pop{from{opacity:0;transform:scale(.6)}to{opacity:1;transform:none}}
@keyframes grow{from{opacity:.3;transform:scaleY(0)}to{opacity:1;transform:none}}
@keyframes growx{from{transform:scaleX(0)}to{transform:none}}
@keyframes growy{from{transform:scaleY(0)}to{transform:none}}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes breathe{0%,100%{opacity:.65}50%{opacity:1}}
@media (prefers-reduced-motion:reduce){
*{animation:none!important}
.smil{display:none}
.still{display:inline}
}
"""


# --------------------------------------------------------------------------- #
# Card: collects markup, tracks glyphs per face, writes the final SVG
# --------------------------------------------------------------------------- #
class Card:
    def __init__(self, name: str, w: int, h: int, title: str, desc: str, t0: float = 0.0):
        self.name, self.w, self.h, self.title, self.desc, self.t0 = name, w, h, title, desc, t0
        self.parts: list[str] = []
        self.defs: list[str] = []
        self.chars: dict[str, set[str]] = {}

    # animation attributes ------------------------------------------------- #
    def a(self, kind: str | None, local: float = 0.0, cls: str = "") -> str:
        if STATIC or not kind:
            return f' class="{cls}"' if cls else ""
        c = f"a {kind}" + (f" {cls}" if cls else "")
        return f' class="{c}" style="animation-delay:{self.t0 + local:.2f}s"'

    def add(self, s: str) -> None:
        self.parts.append(s)

    def t(self, local: float) -> float:
        """Absolute SMIL begin time."""
        return self.t0 + local

    def pair(self, mk) -> str:
        """SMIL element + static twin; the twin shows under prefers-reduced-motion."""
        if STATIC:
            return mk("")
        return mk("smil") + mk("still")

    # primitives ------------------------------------------------------------ #
    def text(self, x, y, s, face="sans", size=14, fill=None, anchor="start",
             anim=None, delay=0.0, opacity=None, extra=""):
        self.chars.setdefault(face, set()).update(s)
        fill = fill or THEME["text"]
        op = f' opacity="{opacity}"' if opacity is not None else ""
        anc = f' text-anchor="{anchor}"' if anchor != "start" else ""
        self.add(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}"{anc}{op}{extra}'
                 f'{self.a(anim, delay, face)}>{esc(s)}</text>')

    def frame(self, radius=16, glow=None):
        th = THEME
        self.defs.append(
            f'<linearGradient id="bgp" x1="0" y1="0" x2="1" y2="1">'
            f'<stop offset="0" stop-color="{th["panel"]}"/><stop offset="1" stop-color="{th["bg"]}"/></linearGradient>')
        self.add(f'<rect width="{self.w}" height="{self.h}" rx="{radius}" fill="url(#bgp)"/>')
        if glow:
            gx, gy, gr = glow
            self.defs.append(
                f'<radialGradient id="glw" cx="{gx}" cy="{gy}" r="{gr}" gradientUnits="userSpaceOnUse">'
                f'<stop offset="0" stop-color="{th["brand"]}" stop-opacity=".22"/>'
                f'<stop offset="1" stop-color="{th["brand"]}" stop-opacity="0"/></radialGradient>')
            cls = "" if STATIC else ' class="breathe"'
            self.add(f'<rect width="{self.w}" height="{self.h}" rx="{radius}" fill="url(#glw)"{cls}/>')
        self.add(f'<rect x=".5" y=".5" width="{self.w - 1}" height="{self.h - 1}" rx="{radius - .5}" '
                 f'fill="none" stroke="{th["border"]}"/>')

    # output ---------------------------------------------------------------- #
    def svg(self) -> str:
        body = "".join(self.parts)
        faces = []
        for key, chars in sorted(self.chars.items()):
            spec = FACES[key]
            faces.append(
                f'@font-face{{font-family:"{spec["family"]}";font-weight:{spec["weight"]};font-style:normal;'
                f'font-display:block;src:url(data:font/woff2;base64,{subset_b64(key, chars)}) format("woff2")}}')
        css = "".join(faces) + BASE_CSS + ("" if STATIC else ANIM_CSS)
        out = (
            f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{self.w}" height="{self.h}" viewBox="0 0 {self.w} {self.h}" role="img" '
            f'aria-labelledby="t d"><title id="t">{esc(self.title)}</title><desc id="d">{esc(self.desc)}</desc>'
            f'<style>{css}</style><defs>{"".join(self.defs)}</defs>{body}</svg>')
        return out

    def write(self) -> Path:
        CARDS_DIR.mkdir(parents=True, exist_ok=True)
        p = CARDS_DIR / f"{self.name}.svg"
        s = self.svg()
        p.write_text(s, encoding="utf-8")
        print(f"  {p.relative_to(ROOT) if p.is_relative_to(ROOT) else p}  {len(s) / 1024:.1f} KB")
        return p


def wrap(m: Metrics, text: str, size: float, max_w: float, max_lines: int) -> list[str]:
    lines, cur = [], ""
    for w in text.split():
        trial = f"{cur} {w}".strip()
        if m.width(trial, size) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and m.width(last + "…", size) > max_w:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
def load_days(sample: bool):
    if sample:
        rnd = random.Random(7)
        end = dt.date.today()
        days = []
        for i in range(365):
            d = end - dt.timedelta(days=364 - i)
            base = 0 if d.weekday() >= 5 and rnd.random() < .6 else rnd.choice([0, 0, 1, 2, 3, 5, 8])
            if rnd.random() < .04:
                base += rnd.randint(10, 22)
            days.append((d, base))
        for i in range(1, 9):  # make sure there's a live streak
            days[-i] = (days[-i][0], max(days[-i][1], 1 + i % 4))
        print("!! using SYNTHETIC sample data (--sample): do not commit these cards")
        return days
    path = ROOT / "data" / "contributions.json"
    if not path.exists():
        sys.exit("data/contributions.json not found. Run scripts/fetch_contributions.py first "
                 "(or pass --sample to preview the layout with synthetic data).")
    raw = json.loads(path.read_text(encoding="utf-8"))
    days = []
    for row in raw["days"]:
        if isinstance(row, dict):
            days.append((dt.date.fromisoformat(row["date"]), int(row["count"])))
        else:
            days.append((dt.date.fromisoformat(row[0]), int(row[1])))
    days.sort()
    if not days:
        sys.exit("data/contributions.json has no days")
    return days


def load_repos() -> dict:
    p = ROOT / "data" / "repos.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def load_portrait() -> str | None:
    for ext in ("png", "jpg", "jpeg", "webp"):
        p = ROOT / "assets" / f"portrait.{ext}"
        if not p.exists():
            continue
        try:
            from PIL import Image, ImageOps
            import io
            im = ImageOps.fit(Image.open(p).convert("RGB"), (320, 320), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=84, optimize=True)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        except ImportError:
            if p.stat().st_size < 150_000:
                mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
                return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()
            print(f"  (skipping {p.name}: install pillow to downscale it)")
    return None


def compute(days):
    active = sum(1 for _, c in days if c > 0)
    longest = run = 0
    for _, c in days:
        run = run + 1 if c > 0 else 0
        longest = max(longest, run)
    i, cur = len(days) - 1, 0
    if i >= 0 and days[i][1] == 0:  # today may simply not have a commit yet
        i -= 1
    while i >= 0 and days[i][1] > 0:
        cur += 1
        i -= 1
    busiest = max(days, key=lambda d: (d[1], -d[0].toordinal()))
    months: OrderedDict = OrderedDict()
    for d, c in days:
        months[(d.year, d.month)] = months.get((d.year, d.month), 0) + c
    last12 = list(months.items())[-12:]
    return dict(active=active, longest=longest, current=cur, busiest=busiest,
                months=last12, total=sum(c for _, c in days))


# --------------------------------------------------------------------------- #
# Cards
# --------------------------------------------------------------------------- #
def card_hero(portrait: str | None) -> Card:
    th = THEME
    c = Card("hero", W, 250, f"{NAME}: {TAGLINE}", f"Profile banner for {NAME}. {TAGLINE}", TL["hero"])
    c.frame(glow=(700, 40, 340))
    c.defs.append(
        '<pattern id="dots" width="24" height="24" patternUnits="userSpaceOnUse">'
        '<circle cx="2" cy="2" r="1" fill="#fff" opacity=".06"/></pattern>'
        f'<linearGradient id="nm" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{th["bright"]}"/>'
        f'<stop offset="1" stop-color="{mix(th["brand"], "#ffffff", .35)}"/></linearGradient>'
        f'<linearGradient id="bar" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{th["brand"]}"/>'
        f'<stop offset="1" stop-color="{th["brand_deep"]}"/></linearGradient>')
    c.add(f'<rect width="{W}" height="250" rx="16" fill="url(#dots)"/>')

    x = 56
    c.text(x, 78, EYEBROW, "mono", 15, th["muted"], anim="fade", delay=0.1)
    ew = metrics("mono").width(EYEBROW, 15)

    def cursor(cls):
        anim = ('<animate attributeName="opacity" values="1;0;1" dur="1.1s" begin="0.9s" '
                'repeatCount="indefinite"/>') if cls == "smil" else ""
        k = f' class="{cls}"' if cls else ""
        return f'<rect x="{x + ew + 6:.1f}" y="64" width="8" height="17" rx="1.5" fill="{th["brand"]}"{k}>{anim}</rect>'
    c.add(c.pair(cursor))

    c.text(x, 142, NAME, "bold", 60, "url(#nm)", anim="rise", delay=0.2)
    c.add(f'<rect x="{x}" y="160" width="72" height="4" rx="2" fill="url(#bar)"{c.a("growx", 0.6)}/>')
    c.text(x, 204, TAGLINE, "sans", 19, th["text"], anim="rise", delay=0.45)

    cx, cy = 704, 125
    if portrait:
        c.defs.append(f'<clipPath id="pc"><circle cx="{cx}" cy="{cy}" r="80"/></clipPath>')
        c.add(f'<circle cx="{cx}" cy="{cy}" r="88" fill="none" stroke="{th["border"]}" stroke-width="2"{c.a("fade", .3)}/>')
        c.add(f'<g{c.a("pop", .35)}><image href="{portrait}" x="{cx - 80}" y="{cy - 80}" width="160" height="160" '
              f'preserveAspectRatio="xMidYMid slice" clip-path="url(#pc)"/></g>')
        spin = "" if STATIC else f' class="spin" style="transform-origin:{cx}px {cy}px"'
        c.add(f'<g{spin}><circle cx="{cx}" cy="{cy}" r="94" fill="none" stroke="url(#bar)" stroke-width="3" '
              f'stroke-linecap="round" stroke-dasharray="70 520"/></g>')
    else:
        for r, op in ((46, .5), (76, .32), (106, .18)):
            c.add(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{th["brand"]}" opacity="{op}"{c.a("fade", .3)}/>')
        spin = "" if STATIC else f' class="spin" style="transform-origin:{cx}px {cy}px"'
        c.add(f'<g{spin}><circle cx="{cx + 76}" cy="{cy}" r="5" fill="{th["brand"]}"/>'
              f'<circle cx="{cx - 106}" cy="{cy}" r="4" fill="{th["hot"]}"/></g>')
        c.text(cx, cy + 14, "</>", "mono", 40, th["brand"], anchor="middle", anim="pop", delay=0.5)
    return c


def tile_icon(c: Card, kind: str, cx: float, cy: float, accent: str, delay: float) -> None:
    c.add(f'<circle cx="{cx}" cy="{cy}" r="19" fill="{accent}" fill-opacity=".14" stroke="{accent}" '
          f'stroke-opacity=".55"{c.a("pop", delay)}/>')
    if kind == "window":
        c.add(f'<g fill="none" stroke="{accent}" stroke-width="1.7" stroke-linecap="round"{c.a("fade", delay + .1)}>'
              f'<rect x="{cx - 10}" y="{cy - 8}" width="20" height="16" rx="3"/>'
              f'<path d="M{cx - 10} {cy - 3}H{cx + 10}"/></g>')
    elif kind == "in":
        c.text(cx, cy + 6.5, "in", "bold", 19, accent, anchor="middle", anim="fade", delay=delay + .1)
    else:
        c.text(cx, cy + 5, "{ }", "mono", 15, accent, anchor="middle", anim="fade", delay=delay + .1)


def card_tile(i: int, tile: dict) -> Card:
    th = THEME
    c = Card(f"tile-{tile['slug']}", 280, 132, f"{tile['label'].title()}: {tile['title']}",
             f"Link to {tile['sub']}", TL["tiles"] + i * TL["tile_step"])
    acc = tile["accent"]
    c.frame(radius=14)
    c.defs.append(
        f'<linearGradient id="tg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{acc}" stop-opacity=".10"/>'
        f'<stop offset="1" stop-color="{acc}" stop-opacity="0"/></linearGradient>')
    c.add(f'<rect width="280" height="132" rx="14" fill="url(#tg)"/>')
    c.add(f'<rect x="22" y="1" width="236" height="2" rx="1" fill="{acc}"{c.a("growx", .05)}/>')
    tile_icon(c, tile["icon"], 40, 40, acc, 0.0)
    c.text(68, 44.5, tile["label"], "mono", 12, acc, anim="fade", delay=.15)
    # arrow (top right)
    c.add(f'<path d="M236 33L250 19M241 19H250V28" fill="none" stroke="{th["muted"]}" stroke-width="1.8" '
          f'stroke-linecap="round" stroke-linejoin="round"{c.a("fade", .25)}/>')
    c.text(22, 94, tile["title"], "bold", 21, th["bright"], anim="rise", delay=.2)
    c.text(22, 116, tile["sub"], "mono", 12, th["muted"], anim="rise", delay=.3)
    return c


def card_chart(st: dict) -> Card:
    th = THEME
    c = Card("chart-contributions", W, 376, "Contributions per month",
             f"Bar chart of contributions per month. Current streak {st['current']} days, "
             f"{st['active']} active days, busiest day {st['busiest'][1]} contributions on {fmt_date(st['busiest'][0])}.",
             TL["chart"])
    c.frame()
    months = st["months"]
    total12 = sum(v for _, v in months)
    c.text(PAD, 46, "CONTRIBUTIONS", "mono", 13, th["brand"], anim="fade", delay=0)
    c.text(W - PAD, 46, f"{total12:,} in the last 12 months", "mono", 12, th["muted"], anchor="end", anim="fade", delay=.05)

    # stat boxes
    bw, gap, by, bh = 245, 16, 66, 92
    cards = [
        ("CURRENT STREAK", str(st["current"]), "days", f"longest {st['longest']} days"),
        ("ACTIVE DAYS", str(st["active"]), "of 365", f"{round(st['active'] / 365 * 100)}% of the last year"),
        ("BUSIEST DAY", str(st["busiest"][1]), "contributions", fmt_date(st["busiest"][0])),
    ]
    big = metrics("bold")
    for i, (lab, val, unit, sub) in enumerate(cards):
        bx = PAD + i * (bw + gap)
        d = .1 + i * .12
        c.add(f'<rect x="{bx}" y="{by}" width="{bw}" height="{bh}" rx="12" fill="{mix(th["panel"], th["brand"], .06)}" '
              f'stroke="{th["border"]}"{c.a("rise", d)}/>')
        c.text(bx + 18, by + 26, lab, "mono", 11, th["muted"], anim="rise", delay=d + .05)
        c.text(bx + 18, by + 62, val, "bold", 32, th["bright"], anim="rise", delay=d + .1)
        vw = big.width(val, 32)
        c.text(bx + 18 + vw + 8, by + 62, unit, "sans", 14, th["muted"], anim="rise", delay=d + .1)
        c.text(bx + 18, by + 82, sub, "mono", 11.5, th["text"], opacity=.8, anim="rise", delay=d + .15)
        if i == 0 and st["current"] > 0:
            def dot(cls, bx=bx):
                k = f' class="{cls}"' if cls else ""
                anim = ('<animate attributeName="r" values="4;7;4" dur="2s" repeatCount="indefinite"/>'
                        '<animate attributeName="opacity" values="1;.35;1" dur="2s" repeatCount="indefinite"/>'
                        ) if cls == "smil" else ""
                return f'<circle cx="{bx + bw - 22}" cy="{by + 22}" r="4" fill="{th["hot"]}"{k}>{anim}</circle>'
            c.add(c.pair(dot))

    # bars
    base_y, max_h = 322, 112
    slot, bar_w, x0 = 58, 34, PAD
    vmax = max([v for _, v in months] + [1])
    avg = total12 / max(len(months), 1)
    best = max(range(len(months)), key=lambda i: months[i][1]) if months else -1
    for gy, dash in ((base_y, None), (base_y - max_h / 2, "3 5"), (base_y - max_h, "3 5")):
        da = f' stroke-dasharray="{dash}"' if dash else ""
        c.add(f'<line x1="{PAD}" x2="{PAD + slot * 12 + 8}" y1="{gy:.1f}" y2="{gy:.1f}" stroke="{th["border"]}" '
              f'stroke-opacity="{1 if not dash else .55}"{da}/>')
    c.defs.append(
        f'<linearGradient id="bg1" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{th["brand"]}"/>'
        f'<stop offset="1" stop-color="{th["brand_deep"]}"/></linearGradient>')
    for i, ((y, mo), v) in enumerate(months):
        bx = x0 + i * slot + (slot - bar_w) / 2
        h = max(v / vmax * max_h, 2 if v else 0)
        d = .45 + i * .06
        op = 1 if i == best else .62
        if v:
            c.add(f'<rect x="{bx:.1f}" y="{base_y - h:.1f}" width="{bar_w}" height="{h:.1f}" rx="5" fill="url(#bg1)" '
                  f'opacity="{op}"{c.a("grow", d)}/>')
        halo = f' stroke="{mix(th["panel"], th["bg"], .5)}" stroke-width="5" stroke-linejoin="round" paint-order="stroke"'
        c.text(bx + bar_w / 2, base_y - h - 9, str(v), "mono", 12, th["brand"] if i == best else th["muted"],
               anchor="middle", anim="fade", delay=d + .5, extra=halo)
        c.text(bx + bar_w / 2, base_y + 24, dt.date(y, mo, 1).strftime("%b"), "mono", 12,
               th["text"] if i == best else th["muted"], anchor="middle", anim="fade", delay=d)

    # average line (SMIL draw-on + static twin)
    ay = base_y - avg / vmax * max_h
    lx2 = PAD + slot * 12 + 8

    def avg_line(cls):
        k = f' class="{cls}"' if cls else ""
        common = (f'y1="{ay:.1f}" y2="{ay:.1f}" stroke="{th["hot"]}" stroke-width="1.5" stroke-dasharray="5 5"')
        if cls == "smil":
            b = f'{c.t(1.35):.2f}s'
            return (f'<line x1="{PAD}" x2="{PAD}" {common} opacity="0"{k}>'
                    f'<animate attributeName="x2" from="{PAD}" to="{lx2}" begin="{b}" dur="1s" fill="freeze"/>'
                    f'<animate attributeName="opacity" from="0" to=".9" begin="{b}" dur=".3s" fill="freeze"/></line>')
        return f'<line x1="{PAD}" x2="{lx2}" {common} opacity=".9"{k}/>'
    c.add(c.pair(avg_line))
    c.text(W - PAD, ay + 4, f"avg {avg:.0f}", "mono", 11.5, th["hot"], anchor="end", anim="fade", delay=1.9)
    return c


def card_repo(i: int, spec: dict, meta: dict | None) -> Card:
    th = THEME
    name = spec["repo"]
    c = Card(f"repo-{name}", W, 188, f"{LOGIN}/{name}", f"{spec['blurb']} {spec['highlight']}",
             TL["repos"] + i * TL["repo_step"])
    c.frame()
    c.add(f'<rect x="0" y="30" width="4" height="128" rx="2" fill="{th["brand"]}"{c.a("growy", .05)}/>')

    # title row
    owner = f"{LOGIN} /"
    c.text(PAD, 52, owner, "mono", 14, th["muted"], anim="rise", delay=.05)
    ow = metrics("mono").width(owner, 14)
    c.text(PAD + ow + 8, 53, name, "bold", 24, th["brand"], anim="rise", delay=.1)

    if meta:
        items = []
        if meta.get("language"):
            items.append(("lang", meta["language"]))
        if meta.get("stargazers_count") is not None:
            items.append(("star", fmt_count(int(meta["stargazers_count"]))))
        if meta.get("forks_count") is not None:
            items.append(("fork", fmt_count(int(meta["forks_count"]))))
        mm = metrics("mono")
        widths = [(14 if k == "lang" else 18) + mm.width(v, 12) for k, v in items]
        x = W - PAD - (sum(widths) + 18 * max(len(items) - 1, 0))
        for (k, v), wd in zip(items, widths):
            y = 50
            if k == "lang":
                c.add(f'<circle cx="{x + 4:.1f}" cy="{y - 4}" r="4.5" fill="{LANG_COLORS.get(v, th["muted"])}"{c.a("pop", .2)}/>')
                tx = x + 14
            elif k == "star":
                pts = []
                for j in range(10):
                    r = 7.5 if j % 2 == 0 else 3.2
                    ang = -math.pi / 2 + j * math.pi / 5
                    pts.append(f"{x + 7 + r * math.cos(ang):.1f},{y - 5 + r * math.sin(ang):.1f}")
                c.add(f'<polygon points="{" ".join(pts)}" fill="none" stroke="{th["muted"]}" stroke-width="1.4" '
                      f'stroke-linejoin="round"{c.a("fade", .25)}/>')
                tx = x + 18
            else:
                c.add(f'<g fill="none" stroke="{th["muted"]}" stroke-width="1.4" stroke-linecap="round"{c.a("fade", .3)}>'
                      f'<circle cx="{x + 3}" cy="{y - 11}" r="2"/><circle cx="{x + 11}" cy="{y - 11}" r="2"/>'
                      f'<circle cx="{x + 7}" cy="{y + 1}" r="2"/>'
                      f'<path d="M{x + 3} {y - 9}V{y - 7}Q{x + 3} {y - 5.5} {x + 4.5} {y - 5.5}H{x + 9.5}'
                      f'Q{x + 11} {y - 5.5} {x + 11} {y - 7}V{y - 9}M{x + 7} {y - 5.5}V{y - 1}"/></g>')
                tx = x + 18
            c.text(tx, y, v, "mono", 12, th["muted"], anim="fade", delay=.25)
            x += wd + 18

    # blurb
    lines = wrap(metrics("sans"), spec["blurb"], 16, W - 2 * PAD, 2)
    for j, ln in enumerate(lines):
        c.text(PAD, 88 + j * 23, ln, "sans", 16, th["text"], anim="rise", delay=.15 + j * .05)
    hy = 88 + len(lines) * 23 + 14
    c.text(PAD, hy, spec["highlight"], "mono", 12.5, th["hot"], anim="rise", delay=.3)

    # chips
    mm = metrics("mono")
    x = PAD
    cy = 188 - 22 - 22
    for j, tag in enumerate(spec["tags"]):
        w = mm.width(tag, 12) + 22
        c.add(f'<rect x="{x:.1f}" y="{cy}" width="{w:.1f}" height="22" rx="11" '
              f'fill="{mix(th["panel"], th["brand"], .16)}" stroke="{mix(th["border"], th["brand"], .45)}"{c.a("pop", .4 + j * .07)}/>')
        c.text(x + w / 2, cy + 15, tag, "mono", 12, mix(th["brand"], "#ffffff", .25), anchor="middle",
               anim="pop", delay=.42 + j * .07)
        x += w + 8
    return c


def card_graph(days, st: dict) -> Card:
    th = THEME
    pitch, cell = 14, 11
    first_off = (days[0][0].weekday() + 1) % 7  # weeks start on Sunday
    ncols = math.ceil((first_off + len(days)) / 7)
    gx0, gy0 = 62, 92
    h = 244
    c = Card("graph-contributions", W, h, "Contribution graph",
             f"Heatmap of {st['total']:,} contributions over the last year.", TL["graph"])
    c.frame()
    c.text(PAD, 46, "CONTRIBUTION GRAPH", "mono", 13, th["brand"], anim="fade", delay=0)
    c.text(W - PAD, 46, f"{st['total']:,} contributions in the last year", "mono", 12, th["muted"],
           anchor="end", anim="fade", delay=.05)

    nz = sorted(v for _, v in days if v > 0)
    q = [nz[int(len(nz) * f)] for f in (.25, .5, .75)] if nz else [1, 2, 3]
    colors = [mix(th["panel"], "#ffffff", .03)] + \
             [mix(th["panel"], th["brand"], t) for t in (.28, .52, .78, 1.0)]

    def level(v):
        return 0 if v == 0 else 1 + (v > q[0]) + (v > q[1]) + (v > q[2])

    cols: list[list[str]] = [[] for _ in range(ncols)]
    month_marks = []
    for idx, (d, v) in enumerate(days):
        pos = idx + first_off
        col, row = divmod(pos, 7)
        cols[col].append(f'<rect x="{gx0 + col * pitch}" y="{gy0 + row * pitch}" width="{cell}" height="{cell}" '
                         f'rx="2.5" fill="{colors[level(v)]}"/>')
        if d.day == 1 or idx == 0:
            month_marks.append((col, d.strftime("%b")))
    if len(month_marks) > 1 and month_marks[1][0] - month_marks[0][0] < 3:
        month_marks.pop(0)  # partial first month would collide with the next label
    for col, parts in enumerate(cols):
        c.add(f'<g{c.a("fade", .15 + col * .014)}>{"".join(parts)}</g>')
    for col, lab in month_marks:
        c.text(gx0 + col * pitch, gy0 - 10, lab, "mono", 10.5, th["muted"], anim="fade", delay=.1 + col * .014)
    for row, lab in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        c.text(PAD, gy0 + row * pitch + 9, lab, "mono", 10, th["muted"], anim="fade", delay=.1)

    # legend
    ly = gy0 + 7 * pitch + 22
    lx = W - PAD
    c.text(lx, ly + 9, "More", "mono", 10.5, th["muted"], anchor="end", anim="fade", delay=1.2)
    mw = metrics("mono").width("More", 10.5)
    sx = lx - mw - 8 - 5 * pitch + 3
    for k in range(5):
        c.add(f'<rect x="{sx + k * pitch}" y="{ly}" width="{cell}" height="{cell}" rx="2.5" fill="{colors[k]}"{c.a("fade", 1.2)}/>')
    c.text(sx - 8, ly + 9, "Less", "mono", 10.5, th["muted"], anchor="end", anim="fade", delay=1.2)
    return c


# --------------------------------------------------------------------------- #
# Preview page (STATIC=1): the README stack on GitHub's dark background
# --------------------------------------------------------------------------- #
def write_preview(names: dict) -> None:
    BUILD_DIR.mkdir(exist_ok=True)
    rel = os.path.relpath(CARDS_DIR, BUILD_DIR).replace(os.sep, "/")
    tiles = "\n".join(f'<img src="{rel}/{n}.svg" width="32.5%">' for n in names["tiles"])
    repos = "\n".join(f'<img src="{rel}/{n}.svg" width="100%">' for n in names["repos"])
    page = f"""<!doctype html><meta charset="utf-8"><title>README preview</title>
<style>body{{margin:0;background:#0d1117}}main{{width:888px;margin:0 auto;padding:24px 0;text-align:center}}
img{{vertical-align:top}}main>*{{margin-bottom:12px}}</style>
<main><img src="{rel}/hero.svg" width="100%">
<div style="display:flex;justify-content:space-between">{tiles}</div>
<img src="{rel}/chart-contributions.svg" width="100%">
{repos}
<img src="{rel}/graph-contributions.svg" width="100%"></main>"""
    (BUILD_DIR / "preview.html").write_text(page, encoding="utf-8")
    print(f"  build/preview.html  (open it and screenshot at 888px wide)")


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true", help="use synthetic contribution data")
    args = ap.parse_args()

    print(f"Rendering cards ({'STATIC final frame' if STATIC else 'animated'}) -> {CARDS_DIR}")
    days = load_days(args.sample)
    st = compute(days)
    repos = load_repos()
    portrait = load_portrait()

    names = dict(tiles=[], repos=[])
    card_hero(portrait).write()
    for i, t in enumerate(TILES):
        names["tiles"].append(card_tile(i, t).write().stem)
    card_chart(st).write()
    for i, spec in enumerate(FEATURED):
        names["repos"].append(card_repo(i, spec, repos.get(spec["repo"])).write().stem)
    card_graph(days, st).write()
    if STATIC:
        write_preview(names)


if __name__ == "__main__":
    main()
