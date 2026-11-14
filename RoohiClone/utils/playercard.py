"""Now-playing neon player card (1280x720) - RoohiClone style.

    path = await make_player_card(videoid)

Layout (matches the reference design):
* dark purple / magenta night background with soft glows, stars, corner brackets
* LEFT : square album art (YouTube cover) inside a neon pink/violet corner frame,
         equalizer badge (top right) and a "STEREO - Pure Sound" chip (bottom left)
* RIGHT: glass player panel - NOW PLAYING / STEREO pills, title, artist, star + more
         buttons, progress bar, bot-name chip, waveform, shuffle / prev / pause /
         next / repeat, volume slider, lyrics + queue pill and an L / R footer

Everything is drawn at 2x and scaled down so edges stay smooth. Cards are saved as
optimised JPEGs (~100-200 KB) and old cards are pruned from cache/ automatically.
"""

import asyncio
import glob
import math
import os
import random
import time
from io import BytesIO

import aiohttp
import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

W, H, S = 1280, 720, 2
CW, CH = W * S, H * S

ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
CACHE_DIR = "cache"
CARD_GLOB = os.path.join(CACHE_DIR, "kavya_*")
KEEP_FILES = 40          # newest cards that are always kept
MAX_AGE = 2 * 3600       # anything older than this (seconds) is deleted
JPEG_QUALITY = 90

PINK = (255, 62, 170)
PINK2 = (240, 80, 200)
VIOLET = (140, 92, 255)
INDIGO = (96, 80, 230)
WHITE = (255, 255, 255, 255)
SOFT = (222, 214, 236, 255)
MUTED = (160, 148, 180, 255)


# ---------------------------------------------------------------- helpers
def P(v):
    """1x design coordinate -> 2x canvas pixel."""
    return int(round(v * S))


def _font(name, size):
    for n in (name, "pc_inter_bold.otf", "pc_bold.ttf", "font2.ttf"):
        try:
            return ImageFont.truetype(os.path.join(ASSETS, n), int(round(size * S)))
        except Exception:
            continue
    return ImageFont.load_default()


def _safe_text(text):
    """Inter has no Devanagari / emoji etc. -> transliterate so no boxes show."""
    text = str(text or "").strip()
    if any(ord(c) > 0x024F for c in text):
        try:
            from unidecode import unidecode
            text = unidecode(text).strip()
        except Exception:
            text = "".join(c for c in text if ord(c) <= 0x024F).strip()
    return " ".join(text.split()) or "Unknown"


def _fit(draw, text, font, max_w):
    if draw.textlength(text, font=font) <= max_w * S:
        return text
    while text and draw.textlength(text + "...", font=font) > max_w * S:
        text = text[:-1]
    return text.rstrip() + "..."


def _secs(duration):
    try:
        return sum(int(x) * 60 ** i for i, x in enumerate(reversed(str(duration).split(":"))))
    except Exception:
        return 0


def _fmt(sec):
    sec = max(0, int(sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _bot_name(client=None):
    try:
        from RoohiClone import app
        client = client or app
        name = (client.me.first_name if client.me else None) or client.name
        return _safe_text(name)
    except Exception:
        return "RoohiClone"


def _track(draw, xy, text, font, fill, spacing=0.0, anchor="l"):
    """Letter-spaced text. xy in 1x coords, baseline anchored. anchor: l / m / r."""
    spacing_px = spacing * S
    widths = [draw.textlength(ch, font=font) for ch in text]
    total = sum(widths) + spacing_px * max(0, len(text) - 1)
    x, y = P(xy[0]), P(xy[1])
    if anchor == "m":
        x -= total / 2
    elif anchor == "r":
        x -= total
    for ch, w in zip(text, widths):
        draw.text((x, y), ch, font=font, fill=fill, anchor="ls")
        x += w + spacing_px
    return total / S


def _lerp(c0, c1, t):
    return tuple(int(round(a + (b - a) * t)) for a, b in zip(c0, c1))


def _grad(w, h, c0, c1, vertical=False, diagonal=False):
    """RGBA gradient image of w x h px."""
    if diagonal:
        t = (np.linspace(0, 1, w)[None, :] + np.linspace(0, 1, h)[:, None]) / 2.0
    elif vertical:
        t = np.linspace(0, 1, h)[:, None] * np.ones((1, w))
    else:
        t = np.linspace(0, 1, w)[None, :] * np.ones((h, 1))
    t = t[..., None]
    c0 = np.array(c0[:3], dtype=np.float32)
    c1 = np.array(c1[:3], dtype=np.float32)
    rgb = (c0 + (c1 - c0) * t).astype(np.uint8)
    a = np.full((h, w, 1), 255, dtype=np.uint8)
    return Image.fromarray(np.concatenate([rgb, a], axis=2), "RGBA")


class Mask:
    """Small L mask covering a 1x bbox; draw shapes with 1x coords, then paint a gradient."""

    def __init__(self, x0, y0, x1, y1, pad=0):
        self.ox, self.oy = P(x0 - pad), P(y0 - pad)
        self.w, self.h = P(x1 - x0 + 2 * pad), P(y1 - y0 + 2 * pad)
        self.img = Image.new("L", (self.w, self.h), 0)
        self.d = ImageDraw.Draw(self.img)

    def _x(self, v):
        return P(v) - self.ox

    def _y(self, v):
        return P(v) - self.oy

    def rrect(self, x0, y0, x1, y1, r, fill=255, outline=None, width=0):
        self.d.rounded_rectangle((self._x(x0), self._y(y0), self._x(x1), self._y(y1)), radius=P(r),
                                 fill=fill, outline=outline, width=P(width) if width else 0)

    def ellipse(self, cx, cy, r, fill=255, outline=None, width=0):
        self.d.ellipse((self._x(cx - r), self._y(cy - r), self._x(cx + r), self._y(cy + r)),
                       fill=fill, outline=outline, width=P(width) if width else 0)

    def line(self, pts, width, fill=255):
        p = [(self._x(x), self._y(y)) for x, y in pts]
        self.d.line(p, fill=fill, width=P(width), joint="curve")
        r = P(width) / 2
        for x, y in (p[0], p[-1]):
            self.d.ellipse((x - r, y - r, x + r, y + r), fill=fill)

    def poly(self, pts, fill=255, round_r=0):
        p = [(self._x(x), self._y(y)) for x, y in pts]
        self.d.polygon(p, fill=fill)
        if round_r:
            self.d.line(p + [p[0]], fill=fill, width=P(round_r * 2), joint="curve")

    def arc(self, cx, cy, r, a0, a1, width, fill=255):
        self.d.arc((self._x(cx - r), self._y(cy - r), self._x(cx + r), self._y(cy + r)),
                   a0, a1, fill=fill, width=P(width))

    def paint(self, canvas, c0, c1=None, vertical=False, diagonal=False, opacity=1.0):
        c1 = c1 or c0
        g = _grad(self.w, self.h, c0, c1, vertical=vertical, diagonal=diagonal)
        m = self.img
        if opacity < 1.0:
            m = m.point(lambda v: int(v * opacity))
        canvas.paste(g, (self.ox, self.oy), m)

    def glow(self, canvas, color, blur=10, strength=1.0):
        """Blurred coloured halo of the mask (drawn under the real shape)."""
        pad = P(blur * 3)
        big = Image.new("L", (self.w + 2 * pad, self.h + 2 * pad), 0)
        big.paste(self.img, (pad, pad))
        big = big.filter(ImageFilter.GaussianBlur(P(blur)))
        if strength != 1.0:
            big = big.point(lambda v: min(255, int(v * strength)))
        layer = Image.new("RGBA", big.size, color[:3] + (255,))
        canvas.paste(layer, (self.ox - pad, self.oy - pad), big)


# ---------------------------------------------------------------- background
def _background(seed):
    """Night-club gradient: dark purple base, magenta glow left, violet glow right."""
    w, h = W, H
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    base = np.zeros((h, w, 3), dtype=np.float32)
    top = np.array([26, 12, 44], dtype=np.float32)
    bot = np.array([14, 10, 34], dtype=np.float32)
    t = (yy / h)[..., None]
    base[:] = top + (bot - top) * t

    def glow(cx, cy, rx, ry, color, amt):
        d = ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2
        a = np.clip(1.0 - d, 0, 1) ** 2 * amt
        return a[..., None] * np.array(color, dtype=np.float32)

    base += glow(160, 360, 520, 520, (190, 40, 150), 0.55)     # magenta, behind the art
    base += glow(1180, 700, 640, 420, (80, 60, 210), 0.60)     # violet, bottom right
    base += glow(700, 40, 420, 220, (110, 40, 170), 0.32)      # soft top glow
    base += glow(640, 380, 260, 380, (30, 10, 60), 0.35)       # dim gap between art and panel
    img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")
    img = img.resize((CW, CH), Image.BICUBIC)       # RGB canvas -> ImageDraw "RGBA" blends properly

    d = ImageDraw.Draw(img, "RGBA")
    rnd = random.Random(seed)
    # faint rings behind the art
    for r, a in ((330, 26), (410, 16)):
        cx, cy = 350, 360
        d.ellipse((P(cx - r), P(cy - r), P(cx + r), P(cy + r)), outline=(255, 90, 190, a), width=P(1.4))
    # tiny dust
    for _ in range(70):
        x, y = rnd.uniform(10, W - 10), rnd.uniform(10, H - 10)
        r = rnd.choice((0.8, 1.0, 1.2, 1.6))
        d.ellipse((P(x - r), P(y - r), P(x + r), P(y + r)), fill=(255, 220, 245, rnd.randint(18, 60)))
    # sparkles
    for x, y, r in ((531, 42, 8), (518, 665, 9), (1236, 96, 6), (46, 330, 5)):
        c = (255, 235, 250, 150)
        d.line([(P(x), P(y - r)), (P(x), P(y + r))], fill=c, width=P(1.4))
        d.line([(P(x - r), P(y)), (P(x + r), P(y))], fill=c, width=P(1.4))
        d.ellipse((P(x - 1.6), P(y - 1.6), P(x + 1.6), P(y + 1.6)), fill=(255, 255, 255, 220))
    # corner brackets
    bc = (200, 192, 214, 150)
    L = 30
    for cx, cy, sx, sy in ((22, 22, 1, 1), (W - 22, 22, -1, 1), (22, H - 22, 1, -1), (W - 22, H - 22, -1, -1)):
        d.line([(P(cx), P(cy + sy * L)), (P(cx), P(cy)), (P(cx + sx * L), P(cy))], fill=bc, width=P(1.6))
    return img


# ---------------------------------------------------------------- left side: album art
def _album(thumb, size=536, radius=36):
    art = ImageOps.fit(thumb, (size * S, size * S), Image.LANCZOS, centering=(0.5, 0.5)).convert("RGBA")
    # darken the bottom so the STEREO chip stays readable
    shade = _grad(size * S, size * S, (0, 0, 0), (0, 0, 0), vertical=True)
    alpha = np.linspace(0, 1, size * S, dtype=np.float32)
    alpha = (np.clip((alpha - 0.62) / 0.38, 0, 1) ** 1.6 * 150).astype(np.uint8)
    shade.putalpha(Image.fromarray(np.tile(alpha[:, None], (1, size * S)), "L"))
    art.alpha_composite(shade)
    mask = Image.new("L", (size * S * 2, size * S * 2), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size * S * 2 - 1, size * S * 2 - 1), radius=radius * S * 2, fill=255)
    art.putalpha(mask.resize((size * S, size * S), Image.LANCZOS))
    return art


def _earbuds(canvas, cx, cy, size, color):
    """Two earbuds (round head + stem) - the 'stereo' icon."""
    m = Mask(cx - size, cy - size, cx + size, cy + size)
    for dx in (-0.30, 0.30):
        hx, hy = cx + dx * size, cy - 0.20 * size
        m.ellipse(hx, hy, 0.22 * size)
        m.rrect(hx + (0.09 if dx > 0 else -0.09) * size - 0.07 * size, hy,
                hx + (0.09 if dx > 0 else -0.09) * size + 0.07 * size, hy + 0.62 * size, 0.07 * size)
    m.paint(canvas, color[:3])


def _draw_left(base, thumb):
    # neon corner frame (pink / violet), outer glow first
    fx0, fy0, fx1, fy1, fr = 66, 76, 634, 644, 52
    ring = Mask(fx0, fy0, fx1, fy1, pad=4)
    ring.rrect(fx0, fy0, fx1, fy1, fr, fill=0, outline=255, width=4.2)
    corners = (
        ((fx0 - 5, fy0 - 5, fx0 + 150, fy0 + 150), PINK, PINK2),      # top-left
        ((fx1 - 150, fy0 - 5, fx1 + 5, fy0 + 150), VIOLET, INDIGO),   # top-right
        ((fx1 - 150, fy1 - 150, fx1 + 5, fy1 + 5), PINK, PINK2),      # bottom-right
        ((fx0 - 5, fy1 - 150, fx0 + 150, fy1 + 5), VIOLET, INDIGO),   # bottom-left
    )
    for (x0, y0, x1, y1), c0, c1 in corners:
        part = Mask(fx0, fy0, fx1, fy1, pad=4)
        part.img = ring.img.copy()
        keep = Image.new("L", part.img.size, 0)
        ImageDraw.Draw(keep).rectangle((P(x0 - (fx0 - 4)), P(y0 - (fy0 - 4)), P(x1 - (fx0 - 4)), P(y1 - (fy0 - 4))), fill=255)
        part.img = Image.composite(part.img, Image.new("L", part.img.size, 0), keep)
        part.glow(base, c0, blur=9, strength=1.5)
        part.paint(base, c0, c1, diagonal=True)

    # album art + hairline border
    ax, ay, asz = 82, 92, 536
    art = _album(thumb, asz, 36)
    base.paste(art.convert("RGB"), (P(ax), P(ay)), art.getchannel("A"))
    d = ImageDraw.Draw(base, "RGBA")
    d.rounded_rectangle((P(ax), P(ay), P(ax + asz), P(ay + asz)), radius=P(36), outline=(255, 255, 255, 70), width=P(1.6))

    # equalizer badge (top right of the art)
    bx, by, br = 566, 144, 31
    badge = Mask(bx - br, by - br, bx + br, by + br)
    badge.ellipse(bx, by, br)
    badge.paint(base, (60, 40, 90), opacity=0.55)
    d.ellipse((P(bx - br), P(by - br), P(bx + br), P(by + br)), outline=(255, 255, 255, 80), width=P(1.4))
    bars = Mask(bx - 20, by - 20, bx + 20, by + 20)
    for i, hh in enumerate((8, 15, 21, 12, 17)):
        x = bx - 16 + i * 8
        bars.rrect(x - 2.4, by - hh / 2, x + 2.4, by + hh / 2, 2.4)
    bars.paint(base, PINK, VIOLET)

    # "STEREO / Pure Sound" chip (bottom left of the art)
    cx0, cy0, cx1, cy1 = 105, 548, 270, 607
    chip = Mask(cx0, cy0, cx1, cy1)
    chip.rrect(cx0, cy0, cx1, cy1, 29.5)
    chip.paint(base, (40, 24, 64), opacity=0.62)
    d.rounded_rectangle((P(cx0), P(cy0), P(cx1), P(cy1)), radius=P(29.5), outline=(255, 255, 255, 60), width=P(1.2))
    dot = Mask(112, 555, 160, 600)
    dot.ellipse(136, 577.5, 22)
    dot.glow(base, PINK, blur=6, strength=0.9)
    dot.paint(base, PINK, VIOLET, diagonal=True)
    _earbuds(base, 136, 577, 21, (255, 255, 255))
    d = ImageDraw.Draw(base, "RGBA")
    _track(d, (170, 570), "STEREO", _font("pc_inter_bold.otf", 15), WHITE, spacing=1.8)
    d.text((P(170), P(592)), "Pure Sound", font=_font("pc_inter_regular.otf", 13), fill=MUTED, anchor="ls")


# ---------------------------------------------------------------- right side: player panel
def _star_pts(cx, cy, ro, ri):
    pts = []
    for i in range(10):
        ang = math.radians(-90 + i * 36)
        r = ro if i % 2 == 0 else ri
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return pts


def _glass_panel(base):
    x0, y0, x1, y1, r = 681, 72, 1199, 652, 38
    # soft shadow + dark tint + faint white sheen
    sh = Mask(x0, y0, x1, y1, pad=2)
    sh.rrect(x0, y0, x1, y1, r)
    sh.glow(base, (10, 4, 30), blur=16, strength=0.9)
    body = Mask(x0, y0, x1, y1)
    body.rrect(x0, y0, x1, y1, r)
    body.paint(base, (60, 34, 96), (34, 20, 74), vertical=True, opacity=0.74)
    sheen = Mask(x0, y0, x1, y1)
    sheen.rrect(x0, y0, x1, y1, r)
    sheen.paint(base, (255, 255, 255), (255, 255, 255), opacity=0.045)
    # gradient border pink -> violet
    edge = Mask(x0, y0, x1, y1, pad=1)
    edge.rrect(x0, y0, x1, y1, r, fill=0, outline=255, width=2.2)
    edge.glow(base, VIOLET, blur=5, strength=0.45)
    edge.paint(base, PINK, VIOLET, diagonal=True, opacity=0.85)

    d = ImageDraw.Draw(base, "RGBA")
    # ghost DJ-headphone outlines behind the controls
    for cx in (985, 1170):
        d.ellipse((P(cx - 78), P(445 - 78), P(cx + 78), P(445 + 78)), outline=(255, 255, 255, 26), width=P(1.6))
        d.ellipse((P(cx - 14), P(445 - 14), P(cx + 14), P(445 + 14)), outline=(255, 255, 255, 34), width=P(1.6))
        d.ellipse((P(cx - 3), P(445 - 3), P(cx + 3), P(445 + 3)), fill=(255, 255, 255, 40))


def _pill_glass(base, x0, y0, x1, y1, border=(255, 255, 255, 70), tint=0.10):
    r = (y1 - y0) / 2
    m = Mask(x0, y0, x1, y1)
    m.rrect(x0, y0, x1, y1, r)
    m.paint(base, (255, 255, 255), opacity=tint)
    d = ImageDraw.Draw(base, "RGBA")
    d.rounded_rectangle((P(x0), P(y0), P(x1), P(y1)), radius=P(r), outline=border, width=P(1.2))


def _speaker(m, x, cy, waves):
    m.rrect(x, cy - 3.8, x + 6, cy + 3.8, 1.2)
    m.poly([(x + 4, cy - 3.8), (x + 13, cy - 10.5), (x + 13, cy + 10.5), (x + 4, cy + 3.8)])
    if waves:
        for r in (6.0, 11.0):
            m.arc(x + 14, cy, r, -52, 52, 2.2)


def _draw_right(base, title, artist, duration, bot_name, played, seed):
    _glass_panel(base)
    d = ImageDraw.Draw(base, "RGBA")
    total = _secs(duration)

    f_title = _font("pc_inter_bold.otf", 29)
    f_artist = _font("pc_inter_regular.otf", 22)
    f_pill = _font("pc_inter_bold.otf", 12)
    f_time = _font("pc_inter_regular.otf", 17)
    f_chip = _font("pc_inter_bold.otf", 12.5)
    f_foot = _font("pc_inter_semibold.otf", 11)

    # NOW PLAYING pill
    _pill_glass(base, 724, 86, 875, 111)
    d = ImageDraw.Draw(base, "RGBA")
    d.ellipse((P(735.5), P(95), P(744.5), P(104)), fill=PINK + (255,))
    _track(d, (753, 103), "NOW PLAYING", f_pill, SOFT, spacing=2.0)

    # STEREO pill (top right)
    _pill_glass(base, 1047, 87, 1160, 111, border=PINK + (120,))
    _earbuds(base, 1066, 99, 11, PINK)
    d = ImageDraw.Draw(base, "RGBA")
    _track(d, (1081, 103), "STEREO", f_pill, SOFT, spacing=2.0)

    # title + artist
    t = _fit(d, _safe_text(title), f_title, 306)
    a = _fit(d, _safe_text(artist), f_artist, 300)
    d.text((P(726), P(145)), t, font=f_title, fill=WHITE, anchor="ls")
    d.text((P(726), P(184)), a, font=f_artist, fill=(176, 144, 186, 255), anchor="ls")

    # star + more buttons
    star_c = Mask(1040, 120, 1092, 172)
    star_c.ellipse(1066, 146, 25)
    star_c.paint(base, (255, 62, 170), opacity=0.16)
    d = ImageDraw.Draw(base, "RGBA")
    d.ellipse((P(1041), P(121), P(1091), P(171)), outline=PINK + (255,), width=P(1.8))
    d.line([(P(x), P(y)) for x, y in _star_pts(1066, 146.5, 11.5, 4.8) + [_star_pts(1066, 146.5, 11.5, 4.8)[0]]],
           fill=PINK + (255,), width=P(2.0), joint="curve")
    more_c = Mask(1112, 120, 1164, 172)
    more_c.ellipse(1138, 146, 25)
    more_c.paint(base, (255, 255, 255), opacity=0.10)
    d = ImageDraw.Draw(base, "RGBA")
    d.ellipse((P(1113), P(121), P(1163), P(171)), outline=(255, 255, 255, 70), width=P(1.6))
    for dy in (-8.5, 0, 8.5):
        d.ellipse((P(1138 - 2.6), P(146 + dy - 2.6), P(1138 + 2.6), P(146 + dy + 2.6)), fill=(230, 224, 240, 255))

    # decorative accent under the artist
    acc = Mask(724, 197, 790, 205)
    acc.rrect(725, 199, 778, 203.5, 2.2)
    acc.paint(base, PINK, VIOLET)
    d = ImageDraw.Draw(base, "RGBA")
    d.ellipse((P(789), P(199), P(793.5), P(203.5)), fill=VIOLET + (200,))
    d.ellipse((P(799), P(199.5), P(802.5), P(203)), fill=(255, 255, 255, 60))

    # progress bar
    x0, x1, yc = 726, 1160, 223.5
    track = Mask(x0 - 2, yc - 5, x1 + 2, yc + 5)
    track.rrect(x0, yc - 3, x1, yc + 3, 3)
    track.paint(base, (255, 255, 255), opacity=0.20)
    frac = min(1.0, played / total) if total else 0.0
    fx = x0 + max(10, (x1 - x0) * frac)
    fill = Mask(x0 - 2, yc - 5, x1 + 2, yc + 5)
    fill.rrect(x0, yc - 3, fx, yc + 3, 3)
    fill.glow(base, PINK, blur=4, strength=0.7)
    fill.paint(base, PINK, VIOLET)
    d = ImageDraw.Draw(base, "RGBA")
    d.ellipse((P(fx - 8.5), P(yc - 8.5), P(fx + 8.5), P(yc + 8.5)), fill=(255, 255, 255, 255), outline=PINK + (255,), width=P(2.2))

    # times + bot chip
    d.text((P(725), P(258)), _fmt(played), font=f_time, fill=SOFT, anchor="ls")
    rem = f"-{_fmt(total - played)}" if total else "LIVE"
    d.text((P(1160), P(258)), rem, font=f_time, fill=(190, 178, 208, 255), anchor="rs")

    name = _fit(d, bot_name.upper(), f_chip, 130)
    meas = sum(d.textlength(ch, font=f_chip) for ch in name) / S + 2.0 * max(0, len(name) - 1)
    cw = meas + 24 + 26
    ccx, ccy = 942, 265.5
    cx0, cx1 = ccx - cw / 2, ccx + cw / 2
    for sx, ex in ((x0 + 74, cx0 - 12), (cx1 + 12, x1 - 4)):
        if ex > sx:
            d.line([(P(sx), P(ccy)), (P(ex), P(ccy))], fill=(255, 255, 255, 40), width=P(1.0))
    chip = Mask(cx0, ccy - 14, cx1, ccy + 14)
    chip.rrect(cx0, ccy - 14, cx1, ccy + 14, 14)
    chip.glow(base, PINK, blur=7, strength=0.7)
    chip.paint(base, PINK, VIOLET)
    _earbuds(base, cx0 + 17, ccy, 9.5, (255, 255, 255))
    d = ImageDraw.Draw(base, "RGBA")
    _track(d, (cx0 + 31, ccy + 4.5), name, f_chip, WHITE, spacing=2.0)

    # waveform
    rnd = random.Random(seed)
    n = 78
    wave = Mask(x0, 280, x1, 328)
    env = [0.35 + 0.65 * abs(math.sin(i / 5.3 + rnd.random() * 0.5)) for i in range(n)]
    for i in range(n):
        cx = x0 + 1.6 + i * (x1 - x0 - 3.2) / (n - 1)
        hh = max(3.2, 15 * env[i] * (0.55 + 0.45 * rnd.random()))
        wave.rrect(cx - 1.6, 303 - hh, cx + 1.6, 303 + hh, 1.6)
    wave.glow(base, VIOLET, blur=4, strength=0.35)
    wave.paint(base, PINK, VIOLET)

    # transport: shuffle, prev, pause, next, repeat
    cy = 365
    sh = Mask(722, 345, 768, 385)
    sh.line([(728, 374), (738, 374), (753, 356), (760, 356)], 2.6)
    sh.line([(728, 356), (738, 356), (753, 374), (760, 374)], 2.6)
    sh.poly([(759, 350.5), (768, 356), (759, 361.5)])
    sh.poly([(759, 368.5), (768, 374), (759, 379.5)])
    sh.paint(base, PINK, PINK2)
    pv = Mask(764, 336, 834, 394)
    pv.rrect(771, 344, 778, 386, 3)
    pv.poly([(826, 346), (826, 384), (782, 365)], round_r=3.5)
    pv.paint(base, (255, 255, 255))
    nx = Mask(1050, 336, 1120, 394)
    nx.rrect(1108, 344, 1115, 386, 3)
    nx.poly([(1058, 346), (1058, 384), (1102, 365)], round_r=3.5)
    nx.paint(base, (255, 255, 255))
    rp = Mask(1128, 345, 1166, 385)
    rp.rrect(1135, 355, 1160, 376, 8, fill=0, outline=255, width=2.4)
    rp.d.rectangle((rp._x(1146), rp._y(351), rp._x(1156), rp._y(359)), fill=0)
    rp.d.rectangle((rp._x(1139), rp._y(371), rp._x(1149), rp._y(380)), fill=0)
    rp.poly([(1145, 349.5), (1153, 354.5), (1145, 359.5)])
    rp.poly([(1150, 371.5), (1142, 376.5), (1150, 381.5)])
    rp.paint(base, (200, 190, 220), opacity=0.95)

    # big pause button with halo
    px, py, pr = 941, 365, 46
    halo = Mask(px - 70, py - 70, px + 70, py + 70)
    halo.ellipse(px, py, 60, fill=0, outline=255, width=1.6)
    halo.paint(base, PINK, VIOLET, diagonal=True, opacity=0.55)
    glow = Mask(px - pr, py - pr, px + pr, py + pr)
    glow.ellipse(px, py, pr)
    glow.glow(base, PINK, blur=14, strength=1.1)
    glow.paint(base, PINK, VIOLET, diagonal=True)
    d = ImageDraw.Draw(base, "RGBA")
    d.ellipse((P(px - pr + 2.5), P(py - pr + 2.5), P(px + pr - 2.5), P(py + pr - 2.5)), outline=(255, 255, 255, 70), width=P(1.4))
    bars = Mask(px - 20, py - 22, px + 20, py + 22)
    bars.rrect(px - 13.5, py - 18, px - 3.5, py + 18, 3.4)
    bars.rrect(px + 3.5, py - 18, px + 13.5, py + 18, 3.4)
    bars.paint(base, (36, 14, 58))

    # volume
    vy = 485
    spk = Mask(776, vy - 14, 826, vy + 14)
    _speaker(spk, 780, vy, True)
    spk.paint(base, (240, 234, 250))
    vt = Mask(826, vy - 8, 1120, vy + 8)
    vt.rrect(830, vy - 3, 1115, vy + 3, 3)
    vt.paint(base, (255, 255, 255), opacity=0.20)
    vf = Mask(826, vy - 8, 1120, vy + 8)
    vf.rrect(830, vy - 3, 1075, vy + 3, 3)
    vf.glow(base, PINK, blur=4, strength=0.6)
    vf.paint(base, PINK, VIOLET)
    d = ImageDraw.Draw(base, "RGBA")
    d.ellipse((P(1075 - 8), P(vy - 8), P(1075 + 8), P(vy + 8)), fill=(255, 255, 255, 255), outline=VIOLET + (255,), width=P(2.4))
    _earbuds(base, 1143, vy, 12, (240, 234, 250))

    # lyrics + queue pill
    _pill_glass(base, 851, 547, 1033, 603, border=(255, 255, 255, 90), tint=0.09)
    d = ImageDraw.Draw(base, "RGBA")
    d.line([(P(941), P(558)), (P(941), P(592))], fill=(255, 255, 255, 50), width=P(1.2))
    bub = Mask(876, 558, 920, 596)
    bub.rrect(881, 561, 915, 585, 7, fill=0, outline=255, width=2.8)
    bub.poly([(888, 584), (888, 593), (897, 584)])
    bub.ellipse(891.5, 573, 2.6)
    bub.ellipse(904.5, 573, 2.6)
    bub.paint(base, (245, 240, 252))
    lst = Mask(966, 558, 1016, 592)
    for i, ly in enumerate((566, 575, 584)):
        lst.ellipse(972, ly, 2.4)
        lst.rrect(979, ly - 2.2, 1006, ly + 2.2, 2.2)
    lst.paint(base, (245, 240, 252))

    # footer: L ▪▪▪▪   FEEL THE SOUND   R ▪▪▪▪
    d = ImageDraw.Draw(base, "RGBA")
    d.line([(P(705), P(616)), (P(1175), P(616))], fill=(255, 255, 255, 26), width=P(1.0))
    _track(d, (731, 632), "L", f_foot, (200, 190, 220, 255), spacing=0)
    for i in range(4):
        d.rounded_rectangle((P(750 + i * 14), P(627.5), P(759 + i * 14), P(631)), radius=P(1.6), fill=PINK + (255,))
    _track(d, (941, 632), "FEEL THE SOUND", f_foot, (176, 166, 198, 255), spacing=3.0, anchor="m")
    _track(d, (1118, 632), "R", f_foot, (200, 190, 220, 255), spacing=0, anchor="r")
    for i in range(4):
        col = PINK + (255,) if i < 3 else PINK + (90,)
        d.rounded_rectangle((P(1122 + i * 14), P(627.5), P(1131 + i * 14), P(631)), radius=P(1.6), fill=col)


# ---------------------------------------------------------------- card
def render_card(thumb, title, artist, duration, bot_name="RoohiClone", played=3, seed="kavya"):
    thumb = thumb.convert("RGB")
    base = _background(seed)
    _draw_left(base, thumb)
    _draw_right(base, title, artist, duration, bot_name or "RoohiClone", played, seed)

    d = ImageDraw.Draw(base, "RGBA")
    # bottom-left brand line + bottom-right page dots
    _track(d, (82, 683), f"{_safe_text(bot_name).upper()}  •  STEREO SOUND", _font("pc_inter_semibold.otf", 11.5),
           (150, 138, 172, 255), spacing=2.6)
    for x, c in ((1136, PINK), (1158, INDIGO), (1180, (120, 114, 140))):
        d.ellipse((P(x - 3.2), P(679 - 3.2), P(x + 3.2), P(679 + 3.2)), fill=c + (255,))

    return base.resize((W, H), Image.LANCZOS).convert("RGB")


# ---------------------------------------------------------------- data + public API
def _strip_bars(img):
    """hqdefault is 4:3 with black bars -> crop to the real 16:9 picture."""
    w, h = img.size
    if abs(w / h - 4 / 3) < 0.05:
        nh = int(w * 9 / 16)
        top = (h - nh) // 2
        img = img.crop((0, top, w, top + nh))
    return img


async def _fetch(videoid):
    from youtubesearchpython.__future__ import VideosSearch

    title, artist, duration = "Unknown Title", "Unknown Artist", "0:00"
    try:
        data = await VideosSearch(f"https://www.youtube.com/watch?v={videoid}", limit=1).next()
        r = data["result"][0]
        title = str(r.get("title") or title)
        artist = str((r.get("channel") or {}).get("name") or artist)
        duration = str(r.get("duration") or duration)
    except Exception as e:
        print(f"[PLAYERCARD] info fetch failed: {e}")

    thumb = None
    for name in ("maxresdefault", "hq720", "hqdefault", "mqdefault"):
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(f"https://i.ytimg.com/vi/{videoid}/{name}.jpg") as resp:
                    if resp.status == 200:
                        thumb = _strip_bars(Image.open(BytesIO(await resp.read())).convert("RGB"))
                        break
        except Exception as e:
            print(f"[PLAYERCARD] thumb fetch failed ({name}): {e}")
    if thumb is None:
        thumb = Image.new("RGB", (640, 360), (60, 40, 90))
    return title, artist, duration, thumb


def prune_cache(keep=KEEP_FILES, max_age=MAX_AGE, protect=None):
    """Delete old player cards so storage never fills up."""
    try:
        now = time.time()
        files = []
        for f in glob.glob(CARD_GLOB):
            try:
                files.append((os.path.getmtime(f), f))
            except OSError:
                pass
        files.sort(reverse=True)
        for i, (mt, f) in enumerate(files):
            if f == protect:
                continue
            if f.endswith(".png") or i >= keep or now - mt > max_age:
                try:
                    os.remove(f)
                except OSError:
                    pass
    except Exception:
        pass


async def make_player_card(videoid, user_id=None, client=None):
    os.makedirs(CACHE_DIR, exist_ok=True)
    bid = getattr(getattr(client, 'me', None), 'id', 0) if client else 0
    out = os.path.join(CACHE_DIR, f"kavya_{videoid}_{bid}.jpg" if bid else f"kavya_{videoid}.jpg")
    if os.path.exists(out):
        os.utime(out, None)  # still in use -> keep it fresh
        return out
    try:
        title, artist, duration, thumb = await _fetch(videoid)
        bot = _bot_name(client)
        loop = asyncio.get_running_loop()

        def _build():
            img = render_card(thumb, title, artist, duration, bot, seed=str(videoid))
            tmp = out + ".tmp"
            img.save(tmp, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True, subsampling=0)
            os.replace(tmp, out)

        await loop.run_in_executor(None, _build)
        await loop.run_in_executor(None, prune_cache, KEEP_FILES, MAX_AGE, out)
        return out
    except Exception as e:
        print(f"[PLAYERCARD] render failed: {e}")
        return None
