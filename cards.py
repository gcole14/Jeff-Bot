"""Procedural ASCII-backdrop PNG card renderer for Jeff Bot (Pillow + Data Dragon + CommunityDragon)."""

import asyncio
import io
import logging
import math
import os
import ssl
from pathlib import Path
from typing import Optional

import aiohttp
from PIL import Image, ImageDraw, ImageFilter, ImageFont

try:
    import certifi
    _SSL_CONTEXT: Optional[ssl.SSLContext] = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = None

from config import BASE_DIR, DB_PATH, REGION

logger = logging.getLogger(__name__)

FONTS_DIR = BASE_DIR / "assets" / "fonts"
INTER_FONT = FONTS_DIR / "Inter-Variable.ttf"
MONO_REG = FONTS_DIR / "JetBrainsMono-Regular.ttf"
MONO_BOLD = FONTS_DIR / "JetBrainsMono-Bold.ttf"

ASSET_CACHE_DIR = Path(os.path.dirname(os.path.abspath(DB_PATH))) / "assets"

# Riot Data Dragon champion filename overrides
CHAMP_KEY_OVERRIDES = {
    "FiddleSticks": "Fiddlesticks",
    "Wukong": "MonkeyKing",
}

CHAMP_DISPLAY_OVERRIDES = {
    "LeeSin": "Lee Sin",
    "TwistedFate": "Twisted Fate",
    "MasterYi": "Master Yi",
    "MissFortune": "Miss Fortune",
    "DrMundo": "Dr. Mundo",
    "JarvanIV": "Jarvan IV",
    "XinZhao": "Xin Zhao",
    "TahmKench": "Tahm Kench",
    "AurelionSol": "Aurelion Sol",
    "KogMaw": "Kog'Maw",
    "RekSai": "Rek'Sai",
    "KhaZix": "Kha'Zix",
    "ChoGath": "Cho'Gath",
    "VelKoz": "Vel'Koz",
    "KaiSa": "Kai'Sa",
    "BelVeth": "Bel'Veth",
    "KSante": "K'Sante",
    "MonkeyKing": "Wukong",
    "FiddleSticks": "Fiddlesticks",
    "Renata": "Renata Glasc",
}


def _display_champ_name(raw: str) -> str:
    return CHAMP_DISPLAY_OVERRIDES.get(raw, raw)

# Subtle accent hues per rank tier
TIER_ACCENTS = {
    "IRON": ((140, 148, 162), (104, 114, 130)),
    "BRONZE": ((205, 138, 92), (156, 98, 64)),
    "SILVER": ((176, 198, 224), (124, 150, 186)),
    "GOLD": ((236, 192, 92), (186, 134, 48)),
    "PLATINUM": ((78, 212, 206), (52, 148, 182)),
    "EMERALD": ((64, 218, 150), (36, 156, 128)),
    "DIAMOND": ((104, 186, 255), (146, 118, 255)),
    "MASTER": ((196, 114, 255), (126, 86, 232)),
    "GRANDMASTER": ((248, 104, 116), (186, 62, 118)),
    "CHALLENGER": ((118, 224, 255), (242, 196, 92)),
    "UNRANKED": ((104, 186, 255), (146, 118, 255)),
}


# -------------------------------------------------------------------------
# Font helpers
# -------------------------------------------------------------------------

_font_cache: dict[tuple[str, int, str], ImageFont.FreeTypeFont] = {}


def _font(size: int, weight: str = "Regular", mono: bool = False) -> ImageFont.FreeTypeFont:
    key = ("mono" if mono else "sans", size, weight)
    if key in _font_cache:
        return _font_cache[key]

    if mono:
        path = MONO_BOLD if weight in ("SemiBold", "Bold", "ExtraBold") else MONO_REG
        f = ImageFont.truetype(str(path), size)
    else:
        f = ImageFont.truetype(str(INTER_FONT), size)
        try:
            f.set_variation_by_name(weight)
        except Exception:
            pass
    _font_cache[key] = f
    return f


def _spaced_text(
    draw: ImageDraw.ImageDraw,
    pos: tuple[float, float],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill,
    tracking: float = 2.0,
) -> float:
    x, y = pos
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking
    return x


def _text_width_spaced(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, tracking: float) -> float:
    if not text:
        return 0.0
    return sum(draw.textlength(ch, font=font) for ch in text) + tracking * max(0, len(text) - 1)


def _fmt_k(val: float) -> str:
    if val >= 1_000_000:
        return f"{val / 1_000_000:.2f}M"
    if val >= 10_000:
        return f"{val / 1_000:.1f}k"
    if val >= 1_000:
        return f"{val / 1_000:.2f}k"
    return f"{val:.0f}"


# -------------------------------------------------------------------------
# Remote Asset Cache (Data Dragon + CommunityDragon)
# -------------------------------------------------------------------------

class AssetCache:
    """Downloads and caches League icons/emblems on disk so card rendering stays fast."""

    def __init__(self, riot_api, cache_dir: Path = ASSET_CACHE_DIR):
        self.riot = riot_api
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=_SSL_CONTEXT) if _SSL_CONTEXT else None
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=12), connector=connector
            )
        return self._session

    async def _fetch_image(self, url: str, rel_path: str) -> Optional[Image.Image]:
        local = self.cache_dir / rel_path
        if local.exists():
            try:
                return Image.open(local).convert("RGBA")
            except Exception:
                pass
        try:
            session = await self._get_session()
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                raw = await resp.read()
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(raw)
            return Image.open(io.BytesIO(raw)).convert("RGBA")
        except Exception as e:
            logger.debug(f"Asset fetch failed ({url}): {e}")
            return None

    async def _dd_version(self) -> str:
        try:
            return await self.riot.get_ddragon_latest_version()
        except Exception:
            return "14.23.1"

    async def get_profile_icon(self, icon_id: int) -> Optional[Image.Image]:
        if not icon_id:
            icon_id = 29
        ver = await self._dd_version()
        url = f"https://ddragon.leagueoflegends.com/cdn/{ver}/img/profileicon/{int(icon_id)}.png"
        return await self._fetch_image(url, f"profile/{int(icon_id)}.png")

    async def get_champion_icon(self, champ_name: str) -> Optional[Image.Image]:
        if not champ_name:
            return None
        ver = await self._dd_version()
        key = CHAMP_KEY_OVERRIDES.get(champ_name, champ_name.replace(" ", "").replace("'", ""))
        url = f"https://ddragon.leagueoflegends.com/cdn/{ver}/img/champion/{key}.png"
        img = await self._fetch_image(url, f"champ/{key}.png")
        if img is None and key != champ_name:
            url2 = f"https://ddragon.leagueoflegends.com/cdn/{ver}/img/champion/{champ_name}.png"
            img = await self._fetch_image(url2, f"champ/{champ_name}.png")
        return img

    async def get_item_icon(self, item_id: int) -> Optional[Image.Image]:
        if not item_id:
            return None
        ver = await self._dd_version()
        url = f"https://ddragon.leagueoflegends.com/cdn/{ver}/img/item/{int(item_id)}.png"
        return await self._fetch_image(url, f"item/{int(item_id)}.png")

    async def get_rank_emblem(self, tier: str) -> Optional[Image.Image]:
        t = (tier or "unranked").lower()
        if t not in {
            "iron", "bronze", "silver", "gold", "platinum", "emerald",
            "diamond", "master", "grandmaster", "challenger", "unranked",
        }:
            t = "unranked"
        url = f"https://raw.communitydragon.org/latest/plugins/rcp-fe-lol-shared-components/global/default/{t}.png"
        return await self._fetch_image(url, f"rank/{t}.png")

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


# -------------------------------------------------------------------------
# Procedural Moonlit Citadel ASCII Backdrop (Cached)
# -------------------------------------------------------------------------

_backdrop_cache: dict[tuple[int, int, str], Image.Image] = {}


def _hash2(x: int, y: int) -> float:
    n = (x * 374761393 + y * 668265263) ^ (x * 1274126177)
    n = (n ^ (n >> 13)) * 1274126177
    return ((n ^ (n >> 16)) & 0xFFFF) / 65535.0


def _smooth_noise(x: float, y: float) -> float:
    ix, iy = int(math.floor(x)), int(math.floor(y))
    fx, fy = x - ix, y - iy
    ux = fx * fx * (3 - 2 * fx)
    uy = fy * fy * (3 - 2 * fy)
    a = _hash2(ix, iy)
    b = _hash2(ix + 1, iy)
    c = _hash2(ix, iy + 1)
    d = _hash2(ix + 1, iy + 1)
    return a + (b - a) * ux + (c - a) * uy + (a - b - c + d) * ux * uy


def _fbm(x: float, y: float) -> float:
    return (
        0.55 * _smooth_noise(x, y)
        + 0.30 * _smooth_noise(x * 2.1 + 13.4, y * 2.1 + 7.1)
        + 0.15 * _smooth_noise(x * 4.3 + 29.2, y * 4.3 + 19.7)
    )


def _mountain_far_y(nx: float) -> float:
    """Far misty mountain peaks across the upper right (y in 0.24..0.38)."""
    return (
        0.365
        - 0.095 * math.exp(-((nx - 0.52) / 0.085) ** 2)
        - 0.085 * math.exp(-((nx - 0.64) / 0.075) ** 2)
        - 0.105 * math.exp(-((nx - 0.90) / 0.085) ** 2)
        - 0.055 * math.exp(-((nx - 0.42) / 0.065) ** 2)
        - 0.016 * math.sin(nx * 38.0) * max(0.0, nx - 0.34)
        - 0.009 * math.cos(nx * 79.0) * max(0.0, nx - 0.34)
    )


def _castle_silhouette_y(nx: float) -> float:
    """Gothic citadel spires & foreground ridge right in front of the moon (strictly above stats row)."""
    ridge = (
        0.395
        - 0.060 * math.exp(-((nx - 0.58) / 0.095) ** 2)
        - 0.055 * math.exp(-((nx - 0.72) / 0.11) ** 2)
        - 0.085 * math.exp(-((nx - 0.92) / 0.09) ** 2)
        - 0.014 * math.sin(nx * 54.0) * max(0.0, nx - 0.44)
    )
    # Detailed gothic castle towers & tall central spires cutting cleanly across the full moon
    towers = [
        # (center_x, half_width, wall_y, spire_height)
        (0.692, 0.011, 0.345, 0.075),
        (0.716, 0.008, 0.335, 0.065),
        (0.742, 0.012, 0.315, 0.105),
        (0.768, 0.010, 0.305, 0.115),
        (0.793, 0.016, 0.285, 0.195),  # Grand cathedral spire piercing high into the moon
        (0.818, 0.011, 0.305, 0.125),
        (0.842, 0.012, 0.320, 0.090),
        (0.866, 0.009, 0.340, 0.060),
    ]
    y = ridge
    for cx, hw, wall_y, spire_h in towers:
        dx = abs(nx - cx)
        if dx <= hw:
            # Steep needle spire roof atop a crisp tower shaft
            spire_top = wall_y - spire_h * (1.0 - (dx / hw) ** 0.55)
            y = min(y, spire_top)
        elif dx <= hw * 1.65:
            # Castle rampart & battlements
            notch = 0.007 if int(nx * 320) % 2 == 0 else 0.0
            y = min(y, wall_y + 0.025 + notch)
    return y


def _render_ascii_backdrop(width: int = 1400, height: int = 788, theme: str = "midnight") -> Image.Image:
    """Renders the signature midnight-navy + lush moonlit ASCII citadel backdrop (cached)."""
    cache_key = (width, height, theme)
    if cache_key in _backdrop_cache:
        return _backdrop_cache[cache_key].copy()

    # 1. Base midnight gradient canvas
    base = Image.new("RGBA", (width, height), (5, 10, 20, 255))
    bg_draw = ImageDraw.Draw(base)
    for y in range(height):
        t = y / height
        r = int(5 + 5 * (1 - t))
        g = int(10 + 13 * (1 - t))
        b = int(22 + 24 * (1 - t))
        bg_draw.line([(0, y), (width, y)], fill=(r, g, b, 255))

    # 2. Luminous moon aura + bottom-right violet ambient glow
    moon_cx = int(width * 0.785)
    moon_cy = int(height * 0.185)
    moon_r = int(height * 0.165)

    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    for mult, col in [
        (3.4, (22, 64, 148, 52)),
        (2.3, (46, 118, 224, 72)),
        (1.5, (98, 182, 255, 92)),
        (1.08, (186, 232, 255, 112)),
    ]:
        rr = int(moon_r * mult)
        gdraw.ellipse((moon_cx - rr, moon_cy - rr, moon_cx + rr, moon_cy + rr), fill=col)

    v_cx, v_cy, v_r = int(width * 0.88), int(height * 0.84), int(height * 0.34)
    gdraw.ellipse((v_cx - v_r, v_cy - v_r, v_cx + v_r, v_cy + v_r), fill=(96, 54, 184, 62))
    glow = glow.filter(ImageFilter.GaussianBlur(radius=42))
    base = Image.alpha_composite(base, glow)

    # 3. Lush, fine-grained ASCII character matrix
    ascii_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    adraw = ImageDraw.Draw(ascii_layer)
    ascii_font = _font(8, weight="Regular", mono=True)
    ramp = " .:-=+*#%@08MW"
    ramp_len = len(ramp) - 1

    cell_w, cell_h = 6, 9
    cols = width // cell_w
    rows = height // cell_h

    for row in range(rows):
        py = row * cell_h
        ny = py / height

        for col in range(cols):
            px = col * cell_w
            nx = px / width

            # Keep far-left behind avatar completely clean, fading in smoothly from x=12%..38%
            left_fade = min(1.0, max(0.0, (nx - 0.12) / 0.26))
            if left_fade <= 0.01:
                continue

            dx_m = px - moon_cx
            dy_m = py - moon_cy
            dist_m = math.hypot(dx_m, dy_m)
            in_moon = dist_m <= moon_r

            far_y = _mountain_far_y(nx)
            castle_y = _castle_silhouette_y(nx)

            # Upper-scene vertical fade so ASCII horizon melts seamlessly into the dark stats strip (y ~ 0.38..0.44)
            horizon_fade = 1.0 if ny <= 0.34 else max(0.0, 1.0 - (ny - 0.34) / 0.095)

            intensity = 0.0
            r_col, g_col, b_col = 72, 134, 236

            if ny < 0.435:
                in_castle = ny >= castle_y and nx > 0.42
                in_far_mtn = ny >= far_y and nx > 0.32

                if in_castle:
                    # Crisp dark gothic castle/foreground silhouette cutting sharply across the moon!
                    edge_dist = ny - castle_y
                    if edge_dist < 0.010 and (dist_m < moon_r * 1.45):
                        # Thin silver-cyan rim highlight right along the backlit castle spires
                        intensity = 0.62 * horizon_fade
                        r_col, g_col, b_col = 148, 212, 255
                    else:
                        # Deep dark navy silhouette with very faint stone texture
                        nval = _fbm(nx * 24.0, ny * 24.0)
                        intensity = (0.12 + 0.12 * nval) * horizon_fade
                        r_col, g_col, b_col = 24, 52, 114
                elif in_moon:
                    # Bright, densely shaded full ASCII moon with organic crater shading
                    edge = max(0.0, 1.0 - (dist_m / moon_r) ** 2.6)
                    crater = _fbm(nx * 20.0 + 3.1, ny * 20.0 + 1.7)
                    intensity = min(1.0, (0.62 + 0.32 * edge + 0.24 * (crater - 0.45))) * max(0.35, horizon_fade)
                    r_col = int(175 + 78 * intensity)
                    g_col = int(222 + 33 * intensity)
                    b_col = 255
                elif in_far_mtn:
                    # Layered moonlit indigo-blue alpine slopes behind the castle
                    slope_depth = min(1.0, (ny - far_y) / 0.12)
                    nval = _fbm(nx * 16.0 + 2.0, ny * 16.0)
                    moon_lit = max(0.0, 1.0 - dist_m / (moon_r * 3.0))
                    intensity = (0.34 + 0.34 * nval + 0.22 * moon_lit) * (1.0 - 0.35 * slope_depth) * horizon_fade * left_fade
                    r_col = int(58 + 74 * moon_lit)
                    g_col = int(112 + 82 * moon_lit)
                    b_col = int(218 + 34 * moon_lit)
                else:
                    # Rich horizontal ASCII night-sky strata + clouds + moon halo
                    strata = 0.5 + 0.5 * math.sin(ny * 115.0 + _fbm(nx * 6.0, ny * 6.0) * 5.0)
                    cloud = _fbm(nx * 6.5, ny * 14.0)
                    halo = max(0.0, 1.0 - dist_m / (moon_r * 2.9)) ** 1.55
                    top_weight = 0.55 + 0.45 * max(0.0, 1.0 - ny / 0.36)
                    intensity = (
                        (0.16 + 0.34 * cloud * (0.65 + 0.35 * strata)) * top_weight
                        + 0.56 * halo
                    ) * left_fade * horizon_fade
                    r_col = int(46 + 114 * halo)
                    g_col = int(102 + 116 * halo)
                    b_col = int(206 + 48 * halo)

            elif ny > 0.58 and nx > 0.54:
                # Lush bottom-right violet/indigo ASCII mountain waves & mist
                wave1 = 0.72 - 0.065 * math.sin(nx * 11.0 + 0.8) - 0.035 * math.cos(nx * 23.0)
                wave2 = 0.84 - 0.055 * math.sin(nx * 14.0 + 2.4)
                x_fade = min(1.0, (nx - 0.54) / 0.28)
                if ny >= wave2:
                    nval = _fbm(nx * 16.0 + 9.0, ny * 16.0)
                    intensity = (0.32 + 0.36 * nval) * x_fade * 0.68
                    t_v = min(1.0, max(0.0, (nx - 0.58) / 0.38))
                    r_col = int(104 + 68 * t_v)
                    g_col = int(78 + 38 * t_v)
                    b_col = int(218 + 30 * t_v)
                elif ny >= wave1:
                    nval = _fbm(nx * 14.0 + 4.0, ny * 14.0)
                    intensity = (0.18 + 0.28 * nval) * x_fade * 0.55
                    r_col, g_col, b_col = 92, 82, 206
                else:
                    # Subtle stipple above the bottom wave
                    if _hash2(col + 3, row + 17) > 0.82:
                        intensity = 0.14 * x_fade
                        r_col, g_col, b_col = 108, 86, 212

            if intensity <= 0.05:
                continue

            ch_idx = min(ramp_len, max(1, int(intensity * ramp_len)))
            ch = ramp[ch_idx]
            alpha = int(min(245, max(24, intensity * 235)))
            adraw.text((px, py), ch, font=ascii_font, fill=(r_col, g_col, b_col, alpha))

    base = Image.alpha_composite(base, ascii_layer)

    # 4. Gentle dark readability scrim behind right-side stats numbers so text always pops
    scrim = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(scrim)
    sdraw.rectangle((int(width * 0.52), int(height * 0.62), int(width * 0.96), int(height * 0.90)), fill=(6, 11, 22, 95))
    scrim = scrim.filter(ImageFilter.GaussianBlur(radius=30))
    base = Image.alpha_composite(base, scrim)

    # Hairline outer card border
    bdraw = ImageDraw.Draw(base)
    bdraw.rounded_rectangle((1, 1, width - 2, height - 2), radius=16, outline=(36, 58, 96, 185), width=2)

    _backdrop_cache[cache_key] = base
    return base.copy()


# -------------------------------------------------------------------------
# Circular Avatar & Supersampled Vector Icon Primitives
# -------------------------------------------------------------------------

def _paste_circular_portrait(
    canvas: Image.Image,
    portrait: Optional[Image.Image],
    center: tuple[int, int],
    diameter: int,
    ring_c1: tuple[int, int, int] = (86, 216, 255),
    ring_c2: tuple[int, int, int] = (176, 110, 255),
    ring_width: int = 4,
    fallback_text: str = "?",
) -> None:
    cx, cy = center
    pad = 16
    box = diameter + pad * 2
    scale = 4
    sbox = box * scale
    sc = sbox // 2
    sr = (diameter // 2) * scale

    # Outer soft glow
    glow = Image.new("RGBA", (box, box), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    gdraw.ellipse(
        (pad - 3, pad - 3, pad + diameter + 3, pad + diameter + 3),
        outline=(*ring_c1, 130),
        width=ring_width + 3,
    )
    glow = glow.filter(ImageFilter.GaussianBlur(radius=6))
    canvas.alpha_composite(glow, (cx - box // 2, cy - box // 2))

    # Build supersampled ring + circular portrait
    layer = Image.new("RGBA", (sbox, sbox), (0, 0, 0, 0))
    mask = Image.new("L", (sbox, sbox), 0)
    mdraw = ImageDraw.Draw(mask)
    inner_r = sr - ring_width * scale
    mdraw.ellipse((sc - inner_r, sc - inner_r, sc + inner_r, sc + inner_r), fill=255)

    if portrait is not None:
        inner_d = inner_r * 2
        # Slight zoom crop (6%) to remove Data Dragon black square borders on champion icons
        pw, ph = portrait.size
        crop_m = int(min(pw, ph) * 0.06)
        cropped = portrait.crop((crop_m, crop_m, pw - crop_m, ph - crop_m))
        resized = cropped.resize((inner_d, inner_d), Image.Resampling.LANCZOS)
        p_layer = Image.new("RGBA", (sbox, sbox), (0, 0, 0, 0))
        p_layer.paste(resized, (sc - inner_r, sc - inner_r))
        layer.paste(p_layer, (0, 0), mask)
    else:
        fb = Image.new("RGBA", (sbox, sbox), (20, 32, 56, 255))
        fb_draw = ImageDraw.Draw(fb)
        f = _font(int(diameter * 0.42) * scale, weight="Bold")
        fb_draw.text((sc, sc), fallback_text[:2].upper(), font=f, fill=(180, 210, 255, 255), anchor="mm")
        layer.paste(fb, (0, 0), mask)

    # Gradient neon border ring
    ring_mask = Image.new("L", (sbox, sbox), 0)
    rmdraw = ImageDraw.Draw(ring_mask)
    rmdraw.ellipse((sc - sr, sc - sr, sc + sr, sc + sr), fill=255)
    rmdraw.ellipse((sc - inner_r, sc - inner_r, sc + inner_r, sc + inner_r), fill=0)

    grad = Image.new("RGBA", (sbox, sbox), (0, 0, 0, 0))
    grdraw = ImageDraw.Draw(grad)
    for y in range(sc - sr, sc + sr + 1):
        t = (y - (sc - sr)) / max(1, 2 * sr)
        r = int(ring_c1[0] + (ring_c2[0] - ring_c1[0]) * t)
        g = int(ring_c1[1] + (ring_c2[1] - ring_c1[1]) * t)
        b = int(ring_c1[2] + (ring_c2[2] - ring_c1[2]) * t)
        grdraw.line([(sc - sr, y), (sc + sr, y)], fill=(r, g, b, 255))
    layer.paste(grad, (0, 0), ring_mask)

    final = layer.resize((box, box), Image.Resampling.LANCZOS)
    canvas.alpha_composite(final, (cx - box // 2, cy - box // 2))


def _paste_rank_emblem(canvas: Image.Image, emblem: Optional[Image.Image], box: tuple[int, int, int, int]) -> None:
    x0, y0, w, h = box
    if emblem is None:
        return
    bbox = emblem.getbbox()
    if bbox:
        emblem = emblem.crop(bbox)
    ew, eh = emblem.size
    scale = min(w / max(1, ew), h / max(1, eh))
    nw, nh = max(1, int(ew * scale)), max(1, int(eh * scale))
    resized = emblem.resize((nw, nh), Image.Resampling.LANCZOS)
    canvas.alpha_composite(resized, (x0 + (w - nw) // 2, y0 + (h - nh) // 2))


def _draw_icon(canvas: Image.Image, kind: str, pos: tuple[int, int], size: int, color: tuple[int, int, int]) -> None:
    """Crisp 4x supersampled monochrome vector icons matching the League HUD style."""
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = (*color, 255)
    lw = max(4, s // 11)

    if kind == "trophy":
        d.pieslice((s * 0.22, s * 0.08, s * 0.78, s * 0.68), 0, 180, fill=c)
        d.rectangle((s * 0.22, s * 0.14, s * 0.78, s * 0.39), fill=c)
        d.arc((s * 0.08, s * 0.16, s * 0.32, s * 0.48), 90, 270, fill=c, width=lw)
        d.arc((s * 0.68, s * 0.16, s * 0.92, s * 0.48), 270, 90, fill=c, width=lw)
        d.rectangle((s * 0.44, s * 0.62, s * 0.56, s * 0.80), fill=c)
        d.rounded_rectangle((s * 0.26, s * 0.78, s * 0.74, s * 0.90), radius=lw, fill=c)
    elif kind == "swords":
        d.line([(s * 0.18, s * 0.18), (s * 0.82, s * 0.82)], fill=c, width=lw + 2)
        d.line([(s * 0.82, s * 0.18), (s * 0.18, s * 0.82)], fill=c, width=lw + 2)
        d.line([(s * 0.14, s * 0.66), (s * 0.34, s * 0.86)], fill=c, width=lw)
        d.line([(s * 0.66, s * 0.86), (s * 0.86, s * 0.66)], fill=c, width=lw)
    elif kind == "gamepad":
        d.rounded_rectangle((s * 0.08, s * 0.26, s * 0.92, s * 0.76), radius=s * 0.22, fill=c)
        bg = (8, 16, 30, 255)
        d.rectangle((s * 0.22, s * 0.46, s * 0.40, s * 0.55), fill=bg)
        d.rectangle((s * 0.27, s * 0.41, s * 0.35, s * 0.60), fill=bg)
        d.ellipse((s * 0.62, s * 0.40, s * 0.72, s * 0.50), fill=bg)
        d.ellipse((s * 0.72, s * 0.50, s * 0.82, s * 0.60), fill=bg)
    elif kind == "star":
        pts = []
        for i in range(10):
            ang = math.radians(-90 + i * 36)
            rad = s * 0.44 if i % 2 == 0 else s * 0.19
            pts.append((s / 2 + rad * math.cos(ang), s / 2 + rad * math.sin(ang)))
        d.polygon(pts, fill=c)
    elif kind == "bars":
        d.rounded_rectangle((s * 0.12, s * 0.56, s * 0.32, s * 0.88), radius=lw // 2, fill=c)
        d.rounded_rectangle((s * 0.40, s * 0.34, s * 0.60, s * 0.88), radius=lw // 2, fill=c)
        d.rounded_rectangle((s * 0.68, s * 0.14, s * 0.88, s * 0.88), radius=lw // 2, fill=c)
    elif kind == "coins":
        for yy in (0.64, 0.46, 0.28):
            d.ellipse((s * 0.16, s * yy, s * 0.84, s * (yy + 0.22)), fill=c, outline=(8, 16, 30, 255), width=max(2, lw // 2))
    elif kind == "eye":
        d.ellipse((s * 0.08, s * 0.24, s * 0.92, s * 0.76), outline=c, width=lw + 1)
        d.ellipse((s * 0.34, s * 0.34, s * 0.66, s * 0.66), fill=c)
        d.ellipse((s * 0.44, s * 0.44, s * 0.56, s * 0.56), fill=(8, 16, 30, 255))
    elif kind == "clock":
        d.ellipse((s * 0.10, s * 0.10, s * 0.90, s * 0.90), outline=c, width=lw + 1)
        d.line([(s * 0.50, s * 0.50), (s * 0.50, s * 0.26)], fill=c, width=lw)
        d.line([(s * 0.50, s * 0.50), (s * 0.70, s * 0.56)], fill=c, width=lw)

    small = img.resize((size, size), Image.Resampling.LANCZOS)
    canvas.alpha_composite(small, pos)


def _draw_footer(canvas: Image.Image, tagline: str = "PLAY  •  IMPROVE  •  CLIMB") -> None:
    w, h = canvas.size
    d = ImageDraw.Draw(canvas)
    fy = h - 46

    # Subtle gold L badge on left
    badge_f = _font(26, weight="Bold")
    d.text((54, fy - 16), "J", font=badge_f, fill=(214, 174, 88, 235))
    d.line([(88, fy), (168, fy)], fill=(54, 78, 118, 160), width=1)

    tf = _font(14, weight="Regular", mono=True)
    tw = _text_width_spaced(d, tagline, tf, 3.0)
    _spaced_text(d, (192, fy - 9), tagline, tf, (124, 146, 178, 220), tracking=3.0)
    d.line([(214 + tw, fy), (w - 58, fy)], fill=(54, 78, 118, 130), width=1)


def _to_png_bytes(img: Image.Image) -> io.BytesIO:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


# -------------------------------------------------------------------------
# 1. Flagship Player Stats Card (/stats)
# -------------------------------------------------------------------------

async def render_player_card(
    ps,
    assets: AssetCache,
    days: int = 1,
    queue_label: str = "All Queues",
    region: str = REGION,
) -> io.BytesIO:
    W, H = 1400, 788
    c1, c2 = TIER_ACCENTS.get((ps.solo_tier or "UNRANKED").upper(), TIER_ACCENTS["UNRANKED"])
    top_champs = ps.top_champions_detailed

    # Fetch all remote icons concurrently
    profile_task = assets.get_profile_icon(ps.profile_icon_id)
    emblem_task = assets.get_rank_emblem(ps.solo_tier)
    champ_tasks = [assets.get_champion_icon(c["name"]) for c in top_champs]
    fetched = await asyncio.gather(profile_task, emblem_task, *champ_tasks)
    profile_img = fetched[0]
    emblem_img = fetched[1]
    champ_imgs = fetched[2:]

    canvas = _render_ascii_backdrop(W, H)
    d = ImageDraw.Draw(canvas)

    # --- Header: Circular Avatar + Name + Subtitle ---
    _paste_circular_portrait(
        canvas,
        profile_img,
        center=(134, 152),
        diameter=148,
        ring_c1=(92, 224, 255),
        ring_c2=(186, 116, 255),
        ring_width=4,
        fallback_text=ps.display_name,
    )

    name_font = _font(62, weight="SemiBold")
    d.text((242, 98), ps.display_name, font=name_font, fill=(244, 248, 255, 255))

    period_tag = "LAST 24H" if days == 1 else f"LAST {days} DAYS"
    sub_parts = ["SUMMONER", region.upper().rstrip("12"), period_tag]
    if queue_label and queue_label != "All Queues":
        sub_parts.append(queue_label.upper())
    subtitle = "  •  ".join(sub_parts)
    sub_font = _font(17, weight="Regular", mono=True)
    _spaced_text(d, (246, 178), subtitle, sub_font, (152, 176, 208, 235), tracking=2.6)

    # --- Middle 4-Column Metric Row ---
    row_top = 294
    row_bot = 434

    # Col 1: Rank Crest + Tier/LP
    _paste_rank_emblem(canvas, emblem_img, (44, 276, 176, 170))
    if ps.solo_tier == "UNRANKED":
        rank_title = "Unranked"
        lp_line = "No Ranked Data"
    elif ps.solo_tier in ("MASTER", "GRANDMASTER", "CHALLENGER"):
        rank_title = ps.solo_tier.capitalize()
        lp_line = f"LP: {ps.solo_lp}"
    else:
        rank_title = f"{ps.solo_tier.capitalize()} {ps.solo_rank}"
        lp_line = f"LP: {ps.solo_lp}"

    d.text((232, 314), rank_title, font=_font(33, weight="SemiBold"), fill=(244, 248, 255, 255))
    d.text((232, 358), "Solo/Duo", font=_font(20, weight="Regular"), fill=(154, 176, 206, 235))
    d.text((232, 390), lp_line, font=_font(20, weight="Medium", mono=True), fill=(182, 202, 228, 245))

    # Vertical dividers
    for div_x in (462, 728, 998):
        d.line([(div_x, row_top + 6), (div_x, row_bot - 6)], fill=(52, 74, 110, 175), width=2)

    # Col 2: Win Rate
    c2_x = 504
    _draw_icon(canvas, "trophy", (c2_x, 304), 26, (124, 184, 255))
    d.text((c2_x + 36, 304), "Win Rate", font=_font(21, weight="Regular"), fill=(190, 208, 232, 240))
    wr_text = f"{ps.win_rate:.1f}%" if ps.games_played else "—"
    d.text((c2_x, 342), wr_text, font=_font(48, weight="Bold"), fill=(252, 254, 255, 255))
    sub_wr = f"({ps.wins}W / {ps.losses}L)" if ps.games_played else f"Season: {ps.solo_wins}W / {ps.solo_losses}L"
    d.text((c2_x, 404), sub_wr, font=_font(19, weight="Regular", mono=True), fill=(146, 168, 198, 235))

    # Col 3: KDA
    c3_x = 768
    _draw_icon(canvas, "swords", (c3_x, 304), 26, (124, 184, 255))
    d.text((c3_x + 36, 304), "KDA", font=_font(21, weight="Regular"), fill=(190, 208, 232, 240))
    kda_text = f"{ps.kda:.2f}" if ps.games_played else "—"
    d.text((c3_x, 342), kda_text, font=_font(48, weight="Bold"), fill=(252, 254, 255, 255))
    sub_kda = (
        f"({ps.avg_kills:.1f} / {ps.avg_deaths:.1f} / {ps.avg_assists:.1f})"
        if ps.games_played
        else "No recent games"
    )
    d.text((c3_x, 404), sub_kda, font=_font(19, weight="Regular", mono=True), fill=(146, 168, 198, 235))

    # Col 4: Games Played
    c4_x = 1038
    _draw_icon(canvas, "gamepad", (c4_x, 304), 28, (124, 184, 255))
    d.text((c4_x + 38, 304), "Games Played", font=_font(21, weight="Regular"), fill=(190, 208, 232, 240))
    d.text((c4_x, 342), str(ps.games_played), font=_font(48, weight="Bold"), fill=(252, 254, 255, 255))

    if ps.lp_change > 0:
        d.text((c4_x, 404), f"▲ +{ps.lp_change} LP", font=_font(19, weight="SemiBold", mono=True), fill=(86, 236, 146, 255))
    elif ps.lp_change < 0:
        d.text((c4_x, 404), f"▼ {ps.lp_change} LP", font=_font(19, weight="SemiBold", mono=True), fill=(255, 114, 118, 255))
    elif abs(ps.win_streak) >= 2:
        st_txt = f"▲ {ps.win_streak}W Streak" if ps.win_streak > 0 else f"▼ {abs(ps.win_streak)}L Streak"
        st_col = (86, 236, 146, 255) if ps.win_streak > 0 else (255, 114, 118, 255)
        d.text((c4_x, 404), st_txt, font=_font(19, weight="SemiBold", mono=True), fill=st_col)
    else:
        d.text((c4_x, 404), f"{ps.hours_played:.1f}h played", font=_font(19, weight="Regular", mono=True), fill=(146, 168, 198, 235))

    # --- Bottom Left: Top Champions ---
    sec_y = 482
    _draw_icon(canvas, "star", (54, sec_y + 2), 22, (206, 144, 255))
    d.text((88, sec_y), "Top Champions", font=_font(21, weight="Regular", mono=True), fill=(204, 218, 238, 245))
    d.line([(278, sec_y + 13), (682, sec_y + 13)], fill=(54, 78, 118, 170), width=1)

    if top_champs:
        slot_centers = [148, 354, 560]
        for idx, champ in enumerate(top_champs[:3]):
            cx = slot_centers[idx]
            c_img = champ_imgs[idx] if idx < len(champ_imgs) else None
            _paste_circular_portrait(
                canvas,
                c_img,
                center=(cx, 578),
                diameter=110,
                ring_c1=c1,
                ring_c2=c2,
                ring_width=3,
                fallback_text=champ["name"],
            )
            cname = _display_champ_name(champ["name"])[:14]
            d.text((cx, 646), cname, font=_font(20, weight="SemiBold"), fill=(242, 246, 255, 255), anchor="mt")
            g_lbl = f"{champ['games']} game" if champ["games"] == 1 else f"{champ['games']} games"
            d.text((cx, 672), g_lbl, font=_font(17, weight="Regular", mono=True), fill=(154, 176, 206, 235), anchor="mt")
            wr_col = (94, 214, 255, 255) if champ["wr"] >= 50 else (255, 126, 130, 255)
            d.text((cx, 696), f"{champ['wr']:.1f}% WR", font=_font(17, weight="SemiBold", mono=True), fill=wr_col, anchor="mt")
    else:
        d.text(
            (368, 600),
            f"No games in {period_tag.lower()}",
            font=_font(21, weight="Regular"),
            fill=(136, 158, 188, 220),
            anchor="mm",
        )

    # --- Bottom Right: Other Stats ---
    rx = 740
    _draw_icon(canvas, "bars", (rx, sec_y + 2), 22, (112, 198, 255))
    d.text((rx + 34, sec_y), "Other Stats", font=_font(21, weight="Regular", mono=True), fill=(204, 218, 238, 245))
    d.line([(rx + 206, sec_y + 13), (W - 58, sec_y + 13)], fill=(54, 78, 118, 170), width=1)

    other_rows = [
        ("swords", "Damage / Game", _fmt_k(ps.avg_damage_per_game) if ps.games_played else "—"),
        ("coins", "Gold / Minute", f"{ps.gold_per_min:.0f}" if ps.games_played else "—"),
        ("eye", "Vision / Game", f"{ps.avg_vision_score:.1f}" if ps.games_played else "—"),
        ("clock", "CS / Minute", f"{ps.cs_per_min:.1f}" if ps.games_played else "—"),
    ]
    for i, (icon_kind, label, val) in enumerate(other_rows):
        ry = 538 + i * 44
        _draw_icon(canvas, icon_kind, (rx + 4, ry + 2), 24, (132, 186, 255))
        d.text((rx + 48, ry), label, font=_font(21, weight="Regular"), fill=(186, 206, 232, 240))
        d.text((W - 60, ry - 2), val, font=_font(25, weight="SemiBold", mono=True), fill=(246, 250, 255, 255), anchor="ra")

    _draw_footer(canvas)
    return _to_png_bytes(canvas)

# -------------------------------------------------------------------------
# 2. Head-to-Head Versus Card (/versus)
# -------------------------------------------------------------------------

async def render_versus_card(a, b, assets: AssetCache) -> io.BytesIO:
    W, H = 1400, 788
    p_a, p_b, e_a, e_b = await asyncio.gather(
        assets.get_profile_icon(a.profile_icon_id),
        assets.get_profile_icon(b.profile_icon_id),
        assets.get_rank_emblem(a.solo_tier),
        assets.get_rank_emblem(b.solo_tier),
    )

    canvas = _render_ascii_backdrop(W, H)
    d = ImageDraw.Draw(canvas)

    # Left player header
    _paste_circular_portrait(canvas, p_a, (132, 134), 124, (86, 216, 255), (120, 146, 255), 4, a.display_name)
    d.text((214, 90), a.display_name, font=_font(44, weight="SemiBold"), fill=(244, 248, 255, 255))
    d.text((214, 148), a.formatted_rank(), font=_font(20, weight="Regular", mono=True), fill=(154, 184, 220, 240))

    # Center VS badge
    d.text((W // 2, 128), "VS", font=_font(52, weight="ExtraBold"), fill=(236, 192, 92, 255), anchor="mm")
    d.text((W // 2, 168), "LAST 7 DAYS", font=_font(15, weight="Regular", mono=True), fill=(148, 170, 202, 230), anchor="mm")

    # Right player header
    _paste_circular_portrait(canvas, p_b, (W - 132, 134), 124, (206, 120, 255), (255, 114, 152), 4, b.display_name)
    d.text((W - 214, 90), b.display_name, font=_font(44, weight="SemiBold"), fill=(244, 248, 255, 255), anchor="ra")
    d.text((W - 214, 148), b.formatted_rank(), font=_font(20, weight="Regular", mono=True), fill=(198, 174, 224, 240), anchor="ra")

    _paste_rank_emblem(canvas, e_a, (54, 222, 110, 100))
    _paste_rank_emblem(canvas, e_b, (W - 164, 222, 110, 100))

    # Comparison bars
    rows = [
        ("WIN RATE", f"{a.win_rate:.0f}%", f"{b.win_rate:.0f}%", a.win_rate, b.win_rate),
        ("KDA", f"{a.kda:.2f}", f"{b.kda:.2f}", a.kda, b.kda),
        ("CS / MIN", f"{a.cs_per_min:.1f}", f"{b.cs_per_min:.1f}", a.cs_per_min, b.cs_per_min),
        ("AVG DAMAGE", _fmt_k(a.avg_damage_per_game), _fmt_k(b.avg_damage_per_game), a.avg_damage_per_game, b.avg_damage_per_game),
        ("GOLD / MIN", f"{a.gold_per_min:.0f}", f"{b.gold_per_min:.0f}", a.gold_per_min, b.gold_per_min),
        ("VISION / GAME", f"{a.avg_vision_score:.1f}", f"{b.avg_vision_score:.1f}", a.avg_vision_score, b.avg_vision_score),
        ("GAMES PLAYED", str(a.games_played), str(b.games_played), a.games_played, b.games_played),
    ]

    bar_max_w = 340
    center_x = W // 2
    for idx, (label, va, vb, na, nb) in enumerate(rows):
        ry = 246 + idx * 66
        a_win = na > nb
        b_win = nb > na
        total = max(0.001, na + nb)
        wa = int(bar_max_w * (na / total)) if (na + nb) > 0 else 0
        wb = int(bar_max_w * (nb / total)) if (na + nb) > 0 else 0

        # Center label pill
        d.text((center_x, ry + 14), label, font=_font(16, weight="Bold", mono=True), fill=(176, 198, 226, 240), anchor="mm")

        # Left bar (grows left from center - 94)
        l_end = center_x - 94
        d.rounded_rectangle((l_end - bar_max_w, ry + 6, l_end, ry + 22), radius=6, fill=(22, 34, 58, 220))
        if wa > 4:
            col_a = (86, 220, 255, 255) if a_win else (74, 114, 174, 210)
            d.rounded_rectangle((l_end - wa, ry + 6, l_end, ry + 22), radius=6, fill=col_a)
        d.text(
            (l_end - bar_max_w - 22, ry + 14),
            va,
            font=_font(24, weight="Bold" if a_win else "Regular", mono=True),
            fill=(116, 232, 255, 255) if a_win else (186, 202, 224, 220),
            anchor="rm",
        )

        # Right bar (grows right from center + 94)
        r_start = center_x + 94
        d.rounded_rectangle((r_start, ry + 6, r_start + bar_max_w, ry + 22), radius=6, fill=(22, 34, 58, 220))
        if wb > 4:
            col_b = (206, 134, 255, 255) if b_win else (112, 92, 168, 210)
            d.rounded_rectangle((r_start, ry + 6, r_start + wb, ry + 22), radius=6, fill=col_b)
        d.text(
            (r_start + bar_max_w + 22, ry + 14),
            vb,
            font=_font(24, weight="Bold" if b_win else "Regular", mono=True),
            fill=(224, 164, 255, 255) if b_win else (186, 202, 224, 220),
            anchor="lm",
        )

    _draw_footer(canvas, "HEAD TO HEAD  •  JEFF BOT")
    return _to_png_bytes(canvas)


# -------------------------------------------------------------------------
# 3. Match / VOD Review Card (/lastgame & /review)
# -------------------------------------------------------------------------

async def render_match_card(
    display_name: str,
    champion: str,
    win: bool,
    queue_name: str,
    duration_seconds: int,
    kills: int,
    deaths: int,
    assists: int,
    cs: int,
    cs_per_min: float,
    damage: int,
    damage_share: float,
    kp: float,
    vision: int,
    gold: int,
    assets: AssetCache,
    item_ids: Optional[list[int]] = None,
    lane_opponent: Optional[str] = None,
    gold_diff_10: Optional[int] = None,
    gold_diff_15: Optional[int] = None,
    role: str = "",
) -> io.BytesIO:
    W, H = 1400, 620
    champ_img = await assets.get_champion_icon(champion)
    item_ids = (item_ids or [])[:6]
    item_imgs = await asyncio.gather(*[assets.get_item_icon(iid) for iid in item_ids]) if item_ids else []

    canvas = _render_ascii_backdrop(W, H)
    d = ImageDraw.Draw(canvas)

    ring1, ring2 = ((68, 232, 152), (56, 192, 255)) if win else ((255, 98, 114), (192, 68, 134))
    _paste_circular_portrait(canvas, champ_img, (136, 156), 152, ring1, ring2, 5, champion)

    res_text = "VICTORY" if win else "DEFEAT"
    res_col = (88, 240, 158, 255) if win else (255, 110, 118, 255)
    _spaced_text(d, (246, 86), res_text, _font(22, weight="Bold", mono=True), res_col, tracking=4.0)

    opp_str = f" vs {lane_opponent}" if lane_opponent else ""
    d.text((244, 122), f"{display_name} — {champion}{opp_str}", font=_font(44, weight="SemiBold"), fill=(246, 250, 255, 255))

    mins, secs = divmod(duration_seconds, 60)
    meta = f"{queue_name.upper()}  •  {mins}M {secs:02d}S"
    if role:
        meta += f"  •  {role.upper()}"
    _spaced_text(d, (246, 186), meta, _font(16, weight="Regular", mono=True), (154, 178, 210, 235), tracking=2.2)

    # Big 4-column stat strip
    kda_val = (kills + assists) / deaths if deaths else float(kills + assists)
    cols = [
        ("swords", "KDA", f"{kills}/{deaths}/{assists}", f"{kda_val:.2f} KDA"),
        ("clock", "CS", str(cs), f"{cs_per_min:.1f} CS / min"),
        ("bars", "Damage", _fmt_k(damage), f"{damage_share:.0f}% team dmg"),
        ("eye", "Vision & KP", f"{kp:.0f}% KP", f"{vision} vision • {_fmt_k(gold)} gold"),
    ]
    col_xs = [64, 392, 720, 1048]
    for i, (ico, lbl, big, sub) in enumerate(cols):
        cx = col_xs[i]
        if i > 0:
            d.line([(cx - 28, 290), (cx - 28, 412)], fill=(52, 74, 110, 175), width=2)
        _draw_icon(canvas, ico, (cx, 286), 24, (124, 184, 255))
        d.text((cx + 34, 286), lbl, font=_font(20, weight="Regular"), fill=(186, 206, 232, 240))
        d.text((cx, 322), big, font=_font(42, weight="Bold"), fill=(252, 254, 255, 255))
        d.text((cx, 382), sub, font=_font(18, weight="Regular", mono=True), fill=(148, 170, 202, 235))

    # Bottom bar: Build items on left + Lane Diff pills on right
    d.text((64, 444), "BUILD", font=_font(16, weight="Bold", mono=True), fill=(160, 184, 218, 230))
    for i in range(6):
        ix = 64 + i * 74
        iy = 474
        d.rounded_rectangle((ix, iy, ix + 62, iy + 62), radius=10, fill=(14, 24, 44, 230), outline=(56, 82, 124, 200), width=2)
        if i < len(item_imgs) and item_imgs[i] is not None:
            im = item_imgs[i].resize((56, 56), Image.Resampling.LANCZOS)
            canvas.alpha_composite(im, (ix + 3, iy + 3))

    if gold_diff_10 is not None or gold_diff_15 is not None:
        d.text((720, 444), "LANE GOLD DIFF", font=_font(16, weight="Bold", mono=True), fill=(160, 184, 218, 230))
        px = 720
        for tag, val in [("@10 MIN", gold_diff_10), ("@15 MIN", gold_diff_15)]:
            if val is None:
                continue
            col = (88, 240, 158, 255) if val >= 0 else (255, 110, 118, 255)
            d.rounded_rectangle((px, 474, px + 260, 536), radius=12, fill=(14, 26, 48, 225), outline=(52, 78, 118, 200), width=2)
            d.text((px + 18, 495), tag, font=_font(16, weight="Regular", mono=True), fill=(154, 178, 210, 235))
            d.text((px + 242, 489), f"{val:+,}g", font=_font(26, weight="Bold", mono=True), fill=col, anchor="ra")
            px += 284

    _draw_footer(canvas, "POST-GAME REPORT  •  JEFF BOT")
    return _to_png_bytes(canvas)


# -------------------------------------------------------------------------
# 4. Squad Recap Card (/daily & /weekly)
# -------------------------------------------------------------------------

async def render_squad_card(
    stats: list,
    title: str,
    subtitle: str,
    assets: AssetCache,
) -> io.BytesIO:
    W, H = 1400, 788
    players = stats[:4]

    # Fetch icons + rank emblems concurrently
    icon_tasks = [assets.get_profile_icon(p.profile_icon_id) for p in players]
    emb_tasks = [assets.get_rank_emblem(p.solo_tier) for p in players]
    fetched = await asyncio.gather(*icon_tasks, *emb_tasks)
    icons = fetched[: len(players)]
    emblems = fetched[len(players) :]

    canvas = _render_ascii_backdrop(W, H)
    d = ImageDraw.Draw(canvas)

    d.text((58, 48), title, font=_font(46, weight="SemiBold"), fill=(246, 250, 255, 255))
    _spaced_text(d, (62, 110), subtitle.upper(), _font(16, weight="Regular", mono=True), (154, 180, 214, 235), tracking=2.4)

    # 2x2 glass-panel grid for the 4 friends
    panel_w, panel_h = 626, 252
    positions = [(58, 176), (716, 176), (58, 456), (716, 456)]

    for i, ps in enumerate(players):
        px, py = positions[i]
        # Frosted dark panel
        panel = Image.new("RGBA", (panel_w, panel_h), (0, 0, 0, 0))
        pd = ImageDraw.Draw(panel)
        pd.rounded_rectangle(
            (0, 0, panel_w - 1, panel_h - 1),
            radius=16,
            fill=(10, 18, 34, 208),
            outline=(48, 72, 112, 185),
            width=2,
        )
        canvas.alpha_composite(panel, (px, py))

        c1, c2 = TIER_ACCENTS.get((ps.solo_tier or "UNRANKED").upper(), TIER_ACCENTS["UNRANKED"])
        _paste_circular_portrait(canvas, icons[i], (px + 66, py + 70), 88, c1, c2, 3, ps.display_name)
        _paste_rank_emblem(canvas, emblems[i], (px + panel_w - 108, py + 18, 88, 86))

        d.text((px + 128, py + 32), ps.display_name, font=_font(30, weight="SemiBold"), fill=(246, 250, 255, 255))
        rank_str = ps.formatted_rank()
        if ps.lp_change:
            rank_str += f"  ({ps.lp_change:+d} LP)"
        d.text((px + 128, py + 72), rank_str, font=_font(17, weight="Regular", mono=True), fill=(156, 182, 214, 240))

        if ps.error:
            d.text((px + 28, py + 150), f"Error: {ps.error[:46]}", font=_font(18), fill=(255, 118, 122, 255))
            continue

        if ps.games_played == 0:
            d.text((px + 28, py + 158), "No games played this period", font=_font(20, weight="Regular"), fill=(134, 156, 188, 220))
            continue

        # Win rate bar
        bar_x, bar_y, bar_w = px + 28, py + 132, panel_w - 56
        d.rounded_rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + 12), radius=6, fill=(24, 38, 64, 230))
        fill_w = int(bar_w * (ps.win_rate / 100.0))
        bar_col = (84, 224, 255, 255) if ps.win_rate >= 50 else (255, 118, 128, 255)
        if fill_w > 4:
            d.rounded_rectangle((bar_x, bar_y, bar_x + fill_w, bar_y + 12), radius=6, fill=bar_col)

        # Stats footer inside tile
        d.text(
            (px + 28, py + 162),
            f"{ps.win_rate:.0f}% WR  ({ps.wins}W-{ps.losses}L)",
            font=_font(21, weight="Bold", mono=True),
            fill=bar_col,
        )
        d.text(
            (px + panel_w - 28, py + 162),
            f"{ps.kda:.2f} KDA  •  {ps.cs_per_min:.1f} CS/m",
            font=_font(19, weight="SemiBold", mono=True),
            fill=(226, 236, 248, 245),
            anchor="ra",
        )
        champs = ", ".join(f"{c} ×{n}" for c, n in ps.top_champions) or "N/A"
        d.text(
            (px + 28, py + 202),
            f"{champs}  •  {_fmt_k(ps.avg_damage_per_game)} dmg/g  •  {ps.hours_played:.1f}h",
            font=_font(16, weight="Regular", mono=True),
            fill=(152, 174, 206, 235),
        )

    _draw_footer(canvas)
    return _to_png_bytes(canvas)


# -------------------------------------------------------------------------
# 5. Recommended Build Path Card (/build)
# -------------------------------------------------------------------------

async def render_build_card(
    champion: str,
    build: dict,
    item_ids: dict[str, Optional[int]],
    assets: AssetCache,
    enemies: Optional[list[str]] = None,
    allies: Optional[list[str]] = None,
    role: str = "",
    patch: str = "",
) -> io.BytesIO:
    """Renders a visual Build Path & Item Order card with Data Dragon champion & item icons."""
    W, H = 1400, 740
    enemies = [e.strip() for e in (enemies or []) if e.strip()][:5]

    # Build ordered main path: Starter -> Boots -> Core 1 -> Core 2 -> Core 3
    main_steps: list[tuple[str, str]] = []
    if build.get("starter", {}).get("item"):
        main_steps.append(("STARTER", build["starter"]["item"]))
    if build.get("boots", {}).get("item"):
        main_steps.append(("BOOTS", build["boots"]["item"]))
    for idx, c in enumerate(build.get("core", [])[:3], 1):
        if c.get("item"):
            main_steps.append((f"CORE {idx}", c["item"]))

    situational = [s for s in build.get("situational", [])[:3] if s.get("item")]

    # Fetch all icons concurrently
    champ_task = assets.get_champion_icon(champion)
    enemy_tasks = [assets.get_champion_icon(e) for e in enemies]
    main_item_tasks = [assets.get_item_icon(item_ids.get(name) or 0) for _, name in main_steps]
    sit_item_tasks = [assets.get_item_icon(item_ids.get(s["item"]) or 0) for s in situational]

    all_fetched = await asyncio.gather(champ_task, *enemy_tasks, *main_item_tasks, *sit_item_tasks)
    champ_img = all_fetched[0]
    p1 = 1 + len(enemies)
    p2 = p1 + len(main_steps)
    enemy_imgs = all_fetched[1:p1]
    main_item_imgs = all_fetched[p1:p2]
    sit_item_imgs = all_fetched[p2:]

    canvas = _render_ascii_backdrop(W, H)
    d = ImageDraw.Draw(canvas)

    # --- Header: Champion Portrait + Title ---
    _paste_circular_portrait(
        canvas,
        champ_img,
        center=(126, 134),
        diameter=134,
        ring_c1=(92, 224, 255),
        ring_c2=(236, 192, 92),
        ring_width=4,
        fallback_text=champion,
    )
    disp_champ = _display_champ_name(champion)
    d.text((224, 82), f"{disp_champ} Build", font=_font(52, weight="SemiBold"), fill=(246, 250, 255, 255))

    sub_parts = ["RECOMMENDED PATH"]
    if role:
        sub_parts.append(role.upper())
    if patch:
        sub_parts.append(f"PATCH {patch}")
    _spaced_text(d, (226, 152), "  •  ".join(sub_parts), _font(16, weight="Regular", mono=True), (154, 180, 214, 235), tracking=2.2)

    # --- Top Right: Enemy Comp Portraits ---
    if enemies:
        panel_w = len(enemies) * 86 + 24
        panel_x = W - 52 - panel_w
        epanel = Image.new("RGBA", (panel_w, 134), (0, 0, 0, 0))
        epd = ImageDraw.Draw(epanel)
        epd.rounded_rectangle((0, 0, panel_w - 1, 133), radius=14, fill=(6, 12, 24, 175), outline=(92, 46, 72, 165), width=1)
        canvas.alpha_composite(epanel, (panel_x, 68))

        d.text((W - 64, 76), "VS ENEMY COMP", font=_font(13, weight="Bold", mono=True), fill=(255, 138, 144, 245), anchor="ra")
        start_x = panel_x + 54
        for idx, ename in enumerate(enemies):
            ex = start_x + idx * 86
            _paste_circular_portrait(
                canvas,
                enemy_imgs[idx],
                center=(ex, 134),
                diameter=62,
                ring_c1=(255, 102, 118),
                ring_c2=(178, 66, 112),
                ring_width=3,
                fallback_text=ename,
            )
            d.text((ex, 174), _display_champ_name(ename)[:10], font=_font(13, weight="SemiBold"), fill=(228, 236, 248, 245), anchor="mt")

    # --- Middle: Ordered Build Path Flow ---
    sec_y = 244
    _draw_icon(canvas, "swords", (58, sec_y + 2), 22, (236, 192, 92))
    d.text((92, sec_y), "Build Order", font=_font(21, weight="Regular", mono=True), fill=(214, 224, 240, 245))
    d.line([(248, sec_y + 13), (W - 58, sec_y + 13)], fill=(54, 78, 118, 170), width=1)

    if main_steps:
        n_steps = len(main_steps)
        box_s = 92
        total_span = W - 180
        step_dx = total_span // max(1, n_steps)
        first_cx = 90 + step_dx // 2
        item_cy = 344

        for idx, (tag, iname) in enumerate(main_steps):
            cx = first_cx + idx * step_dx
            # Tag pill above
            tag_col = (236, 192, 92, 245) if tag.startswith("CORE") else (126, 198, 255, 240)
            d.text((cx, item_cy - 68), tag, font=_font(14, weight="Bold", mono=True), fill=tag_col, anchor="mm")

            # Item tile frame
            x0, y0 = cx - box_s // 2, item_cy - box_s // 2
            border_c = (216, 174, 84, 235) if tag.startswith("CORE") else (84, 152, 228, 220)
            d.rounded_rectangle((x0, y0, x0 + box_s, y0 + box_s), radius=14, fill=(12, 22, 42, 235), outline=border_c, width=3)

            im = main_item_imgs[idx]
            if im is not None:
                resized = im.resize((box_s - 8, box_s - 8), Image.Resampling.LANCZOS)
                canvas.alpha_composite(resized, (x0 + 4, y0 + 4))
            else:
                d.text((cx, item_cy), iname[:3].upper(), font=_font(22, weight="Bold"), fill=(180, 206, 240, 240), anchor="mm")

            # Item name below (wrapped neatly to 2 short lines if needed)
            words = iname.split()
            if len(iname) > 15 and len(words) >= 2:
                mid = len(words) // 2
                l1 = " ".join(words[:mid])
                l2 = " ".join(words[mid:])
                d.text((cx, item_cy + 62), l1, font=_font(16, weight="SemiBold"), fill=(242, 246, 255, 255), anchor="mt")
                d.text((cx, item_cy + 82), l2, font=_font(16, weight="SemiBold"), fill=(242, 246, 255, 255), anchor="mt")
            else:
                d.text((cx, item_cy + 66), iname, font=_font(16, weight="SemiBold"), fill=(242, 246, 255, 255), anchor="mt")

            # Connecting arrow to next item
            if idx < n_steps - 1:
                ax = cx + step_dx // 2
                d.text((ax, item_cy), "→", font=_font(34, weight="Bold"), fill=(112, 178, 248, 210), anchor="mm")

    # --- Bottom Left: Situational Flex Items ---
    sit_y = 482
    _draw_icon(canvas, "star", (58, sit_y + 2), 22, (206, 144, 255))
    d.text((92, sit_y), "Situational Flex", font=_font(21, weight="Regular", mono=True), fill=(214, 224, 240, 245))
    d.line([(312, sit_y + 13), (850, sit_y + 13)], fill=(54, 78, 118, 170), width=1)

    for idx, s in enumerate(situational):
        sx = 64 + idx * 268
        sy = 532
        d.rounded_rectangle((sx, sy, sx + 64, sy + 64), radius=10, fill=(14, 24, 44, 235), outline=(146, 108, 228, 210), width=2)
        im = sit_item_imgs[idx] if idx < len(sit_item_imgs) else None
        if im is not None:
            canvas.alpha_composite(im.resize((58, 58), Image.Resampling.LANCZOS), (sx + 3, sy + 3))
        else:
            d.text((sx + 32, sy + 32), s["item"][:2].upper(), font=_font(18, weight="Bold"), fill=(196, 174, 245, 240), anchor="mm")

        d.text((sx + 76, sy + 4), s["item"][:19], font=_font(17, weight="SemiBold"), fill=(242, 246, 255, 255))
        why_txt = (s.get("why") or "")[:44]
        if len(why_txt) > 22:
            split_i = why_txt.rfind(" ", 0, 23)
            if split_i == -1:
                split_i = 22
            d.text((sx + 76, sy + 28), why_txt[:split_i], font=_font(14, weight="Regular"), fill=(154, 178, 210, 235))
            d.text((sx + 76, sy + 46), why_txt[split_i:].strip(), font=_font(14, weight="Regular"), fill=(154, 178, 210, 235))
        else:
            d.text((sx + 76, sy + 30), why_txt, font=_font(14, weight="Regular"), fill=(154, 178, 210, 235))

    # --- Bottom Right: Runes / Team Context ---
    rx = 890
    _draw_icon(canvas, "bars", (rx, sit_y + 2), 22, (112, 198, 255))
    d.text((rx + 34, sit_y), "Runes & Focus", font=_font(21, weight="Regular", mono=True), fill=(214, 224, 240, 245))
    d.line([(rx + 216, sit_y + 13), (W - 58, sit_y + 13)], fill=(54, 78, 118, 170), width=1)

    runes_txt = build.get("runes") or "Adaptive standard page"
    d.text((rx, 534), runes_txt[:46], font=_font(18, weight="SemiBold"), fill=(116, 224, 255, 255))

    if allies:
        ally_str = "Allies: " + ", ".join(_display_champ_name(a) for a in allies[:4])
        d.text((rx, 568), ally_str[:52], font=_font(15, weight="Regular", mono=True), fill=(156, 180, 214, 235))

    note = (build.get("gameplan") or "").strip()
    if note:
        # Clean 3-line wrap with ellipsis if truncated
        wrapped: list[str] = []
        cur = ""
        for w in note.split():
            cand = f"{cur} {w}".strip()
            if len(cand) <= 50:
                cur = cand
            else:
                wrapped.append(cur)
                cur = w
                if len(wrapped) == 3:
                    break
        if cur and len(wrapped) < 3:
            wrapped.append(cur)
        elif len(wrapped) == 3 and len(" ".join(wrapped)) < len(note):
            wrapped[-1] = wrapped[-1][:47].rstrip(" .,;") + "…"
        for li, line_txt in enumerate(wrapped):
            d.text((rx, 598 + li * 22), line_txt, font=_font(15, weight="Regular"), fill=(208, 220, 238, 235))

    _draw_footer(canvas, "ADAPTIVE BUILD ENGINE  •  JEFF BOT")
    return _to_png_bytes(canvas)


# -------------------------------------------------------------------------
# 6. Loading-Screen 5v5 Scout Card (/scout)
# -------------------------------------------------------------------------

def _tier_from_rank_str(rank_str: str) -> str:
    if not rank_str:
        return "UNRANKED"
    return rank_str.split()[0].upper()


async def render_scout_card(scout: dict, assets: AssetCache) -> io.BytesIO:
    W, H = 1400, 820
    allies = scout.get("allies", [])[:5]
    enemies = scout.get("enemies", [])[:5]
    all_players = allies + enemies

    champ_tasks = [assets.get_champion_icon(p["champion"]) for p in all_players]
    emb_tasks = [assets.get_rank_emblem(_tier_from_rank_str(p.get("rank", ""))) for p in all_players]
    fetched = await asyncio.gather(*champ_tasks, *emb_tasks)
    c_imgs = fetched[: len(all_players)]
    e_imgs = fetched[len(all_players) :]

    canvas = _render_ascii_backdrop(W, H)
    d = ImageDraw.Draw(canvas)

    mins = max(0, scout.get("length_seconds", 0)) // 60
    _spaced_text(d, (58, 42), "LIVE MATCH SCOUT", _font(16, weight="Bold", mono=True), (112, 214, 255, 245), tracking=3.2)
    d.text((58, 68), f"{scout['display_name']} — {_display_champ_name(scout.get('player_champ', ''))}", font=_font(44, weight="SemiBold"), fill=(246, 250, 255, 255))
    _spaced_text(d, (60, 128), f"{scout.get('queue_name', 'MATCH').upper()}  •  {mins}M IN", _font(15, weight="Regular", mono=True), (154, 180, 214, 235), tracking=2.2)

    def _draw_side(side_players: list[dict], img_offset: int, x0: int, header: str, h_col: tuple, ring_pair: tuple):
        panel_w, panel_h = 626, 568
        panel = Image.new("RGBA", (panel_w, panel_h), (0, 0, 0, 0))
        pd = ImageDraw.Draw(panel)
        pd.rounded_rectangle((0, 0, panel_w - 1, panel_h - 1), radius=16, fill=(9, 16, 30, 212), outline=(*h_col[:3], 130), width=2)
        canvas.alpha_composite(panel, (x0, 174))

        _spaced_text(d, (x0 + 24, 190), header, _font(16, weight="Bold", mono=True), (*h_col[:3], 245), tracking=2.8)
        d.line([(x0 + 22, 218), (x0 + panel_w - 22, 218)], fill=(52, 74, 110, 165), width=1)

        for idx, p in enumerate(side_players):
            ry = 232 + idx * 100
            if idx > 0:
                d.line([(x0 + 22, ry - 8), (x0 + panel_w - 22, ry - 8)], fill=(34, 52, 82, 130), width=1)

            r1, r2 = ((236, 192, 92), (92, 224, 255)) if p.get("is_target") else ring_pair
            _paste_circular_portrait(canvas, c_imgs[img_offset + idx], (x0 + 58, ry + 42), 68, r1, r2, 3, p["champion"])

            cname = _display_champ_name(p["champion"])[:14]
            d.text((x0 + 108, ry + 14), cname, font=_font(22, weight="SemiBold"), fill=(246, 250, 255, 255))
            star = " ★" if p.get("is_target") else ""
            d.text((x0 + 108, ry + 46), f"{p['name'][:16]}{star}", font=_font(15, weight="Regular", mono=True), fill=(154, 178, 210, 235))

            # Rank emblem + rank text
            _paste_rank_emblem(canvas, e_imgs[img_offset + idx], (x0 + 292, ry + 14, 56, 56))
            rank_short = p.get("rank", "Unranked").replace(" — ", " ")
            d.text((x0 + 356, ry + 18), rank_short[:16], font=_font(16, weight="SemiBold", mono=True), fill=(224, 234, 248, 245))
            pts = p.get("mastery_pts", 0)
            pts_str = f"{pts // 1000}k pts" if pts >= 1000 else f"{pts} pts"
            wr_str = f" • {p['solo_wr']}% WR" if p.get("solo_wr") is not None else ""
            d.text((x0 + 356, ry + 44), f"{pts_str}{wr_str}", font=_font(14, weight="Regular", mono=True), fill=(148, 172, 204, 230))

            # Status pill (Hot / Tilt / New)
            badge_txt, badge_bg, badge_fg = None, None, None
            st = p.get("streak", 0)
            if st <= -3:
                badge_txt, badge_bg, badge_fg = f"TILT {abs(st)}L", (120, 28, 42, 220), (255, 134, 142, 255)
            elif st >= 3:
                badge_txt, badge_bg, badge_fg = f"HOT {st}W", (20, 96, 62, 220), (96, 242, 162, 255)
            elif pts < 5000:
                badge_txt, badge_bg, badge_fg = "NEW", (84, 48, 134, 220), (214, 168, 255, 255)

            if badge_txt:
                bx = x0 + panel_w - 20
                d.rounded_rectangle((bx - 88, ry + 26, bx, ry + 56), radius=8, fill=badge_bg)
                d.text((bx - 44, ry + 41), badge_txt, font=_font(13, weight="Bold", mono=True), fill=badge_fg, anchor="mm")

    _draw_side(allies, 0, 58, "YOUR TEAM", (92, 214, 255), ((86, 204, 255), (104, 136, 242)))
    _draw_side(enemies, len(allies), 716, "ENEMY TEAM", (255, 108, 118), ((255, 98, 114), (186, 62, 118)))

    _draw_footer(canvas, "LOADING-SCREEN SCOUT  •  JEFF BOT")
    return _to_png_bytes(canvas)


# -------------------------------------------------------------------------
# 7. Squad Post-Game Debrief Card (/debrief & Auto Debriefs)
# -------------------------------------------------------------------------

async def render_debrief_card(debrief: dict, assets: AssetCache) -> io.BytesIO:
    W, H = 1400, 720
    members = debrief.get("members", [])[:4]
    champ_imgs = await asyncio.gather(*[assets.get_champion_icon(m["record"].champion) for m in members])

    canvas = _render_ascii_backdrop(W, H)
    d = ImageDraw.Draw(canvas)

    win = bool(debrief.get("win"))
    res_txt = "SQUAD VICTORY" if win else "SQUAD DEFEAT"
    res_col = (88, 240, 158, 255) if win else (255, 110, 118, 255)
    mins, secs = divmod(debrief.get("duration_seconds", 0), 60)

    _spaced_text(d, (58, 44), res_txt, _font(20, weight="Bold", mono=True), res_col, tracking=3.8)
    d.text((58, 76), f"{len(members)}-Stack Post-Game Debrief", font=_font(44, weight="SemiBold"), fill=(246, 250, 255, 255))
    _spaced_text(d, (60, 136), f"{debrief.get('queue_name', 'MATCH').upper()}  •  {mins}M {secs:02d}S", _font(16, weight="Regular", mono=True), (154, 180, 214, 235), tracking=2.2)

    # Top-right MVP & Most Deaths badges
    mvp = debrief.get("mvp", "")
    md_name, md_cnt = debrief.get("most_deaths", ("", 0))
    d.rounded_rectangle((W - 430, 56, W - 58, 102), radius=10, fill=(28, 24, 14, 215), outline=(218, 174, 76, 200), width=1)
    d.text((W - 412, 79), f"MVP: {mvp}", font=_font(18, weight="Bold", mono=True), fill=(244, 204, 102, 255), anchor="lm")
    d.rounded_rectangle((W - 430, 112, W - 58, 154), radius=10, fill=(36, 14, 22, 215), outline=(218, 86, 98, 180), width=1)
    d.text((W - 412, 133), f"MOST DEATHS: {md_name} ({md_cnt}D)", font=_font(16, weight="SemiBold", mono=True), fill=(255, 136, 144, 255), anchor="lm")

    # Evenly spaced member columns
    n = max(1, len(members))
    gap = 22
    total_w = W - 116
    card_w = (total_w - gap * (n - 1)) // n
    card_h = 446
    top_y = 194

    for idx, m in enumerate(members):
        r = m["record"]
        is_mvp = m["display_name"] == mvp
        cx0 = 58 + idx * (card_w + gap)
        cx_mid = cx0 + card_w // 2

        panel = Image.new("RGBA", (card_w, card_h), (0, 0, 0, 0))
        pd = ImageDraw.Draw(panel)
        outline = (232, 190, 88, 225) if is_mvp else (52, 76, 116, 185)
        pd.rounded_rectangle((0, 0, card_w - 1, card_h - 1), radius=16, fill=(10, 18, 34, 212), outline=outline, width=2)
        canvas.alpha_composite(panel, (cx0, top_y))

        if is_mvp:
            d.rounded_rectangle((cx_mid - 46, top_y + 14, cx_mid + 46, top_y + 38), radius=8, fill=(218, 174, 72, 235))
            d.text((cx_mid, top_y + 26), "SQUAD MVP", font=_font(12, weight="Bold", mono=True), fill=(12, 16, 26, 255), anchor="mm")

        r1, r2 = ((242, 198, 92), (92, 224, 255)) if is_mvp else ((86, 210, 255), (168, 116, 255))
        _paste_circular_portrait(canvas, champ_imgs[idx], (cx_mid, top_y + 106), 104, r1, r2, 4, r.champion)

        d.text((cx_mid, top_y + 172), m["display_name"][:14], font=_font(26, weight="SemiBold"), fill=(246, 250, 255, 255), anchor="mt")
        d.text((cx_mid, top_y + 206), _display_champ_name(r.champion), font=_font(17, weight="Regular", mono=True), fill=(148, 184, 228, 240), anchor="mt")

        d.text((cx_mid, top_y + 244), f"{r.kills} / {r.deaths} / {r.assists}", font=_font(32, weight="Bold"), fill=(252, 254, 255, 255), anchor="mt")
        d.text((cx_mid, top_y + 286), f"{r.kda:.2f} KDA  •  {r.kill_participation:.0f}% KP", font=_font(16, weight="Regular", mono=True), fill=(154, 178, 210, 235), anchor="mt")

        # Damage share bar
        bx = cx0 + 24
        bw = card_w - 48
        by = top_y + 328
        d.text((bx, by), "Damage Share", font=_font(14, weight="Regular", mono=True), fill=(154, 178, 210, 235))
        d.text((bx + bw, by), f"{_fmt_k(r.damage_to_champions)} ({r.damage_share:.0f}%)", font=_font(14, weight="Bold", mono=True), fill=(242, 246, 255, 245), anchor="ra")
        d.rounded_rectangle((bx, by + 24, bx + bw, by + 36), radius=6, fill=(24, 38, 64, 230))
        fw = int(bw * min(1.0, r.damage_share / 45.0))
        bar_c = (242, 198, 92, 255) if is_mvp else (92, 214, 255, 255)
        if fw > 4:
            d.rounded_rectangle((bx, by + 24, bx + fw, by + 36), radius=6, fill=bar_c)

        d.text(
            (cx_mid, top_y + 394),
            f"{r.cs_per_min:.1f} CS/m  •  {r.vision_score} vis",
            font=_font(16, weight="Medium", mono=True),
            fill=(198, 214, 236, 240),
            anchor="mt",
        )

    _draw_footer(canvas, "SQUAD DEBRIEF  •  JEFF BOT")
    return _to_png_bytes(canvas)


# -------------------------------------------------------------------------
# 8. Champion Pool Tier Sheet Card (/pool)
# -------------------------------------------------------------------------

async def render_pool_card(ps, pool: dict, assets: AssetCache, days: int = 30) -> io.BytesIO:
    W, H = 1400, 820
    sections: list[tuple[str, tuple[int, int, int], list[dict]]] = []
    if pool["climb"]:
        sections.append(("CLIMB CHAMPS  (55%+ WR)", (86, 236, 146), pool["climb"][:4]))
    if pool["solid"]:
        sections.append(("SOLID PICKS  (45–54% WR)", (104, 198, 255), pool["solid"][:4]))
    if pool["traps"]:
        sections.append(("LP BLEED / TRAP CHAMPS  (<45% WR)", (255, 110, 118), pool["traps"][:4]))
    if not sections and pool["one_offs"]:
        sections.append(("RECENT PICKS  (1 GAME EACH)", (196, 152, 255), pool["one_offs"][:4]))

    champ_names = [c["champion"] for _, _, items in sections for c in items]
    fetched = await asyncio.gather(
        assets.get_profile_icon(ps.profile_icon_id),
        *[assets.get_champion_icon(cn) for cn in champ_names],
    )
    prof_img = fetched[0]
    champ_img_map = dict(zip(champ_names, fetched[1:]))

    canvas = _render_ascii_backdrop(W, H)
    d = ImageDraw.Draw(canvas)

    _paste_circular_portrait(canvas, prof_img, (116, 112), 112, (92, 224, 255), (186, 116, 255), 4, ps.display_name)
    d.text((198, 66), f"{ps.display_name} — Champion Pool", font=_font(44, weight="SemiBold"), fill=(246, 250, 255, 255))
    _spaced_text(
        d,
        (202, 128),
        f"{pool['total_games']} GAMES  •  {pool['unique_champions']} CHAMPIONS  •  LAST {days} DAYS",
        _font(16, weight="Regular", mono=True),
        (154, 180, 214, 235),
        tracking=2.2,
    )

    cur_y = 196
    for title_txt, col, items in sections[:3]:
        d.rounded_rectangle((58, cur_y + 3, 70, cur_y + 21), radius=3, fill=(*col, 255))
        _spaced_text(d, (84, cur_y), title_txt, _font(17, weight="Bold", mono=True), (*col, 245), tracking=2.0)
        d.line([(500, cur_y + 11), (W - 58, cur_y + 11)], fill=(52, 74, 110, 155), width=1)

        tile_w, tile_h = 304, 108
        for idx, c in enumerate(items[:4]):
            tx = 58 + idx * (tile_w + 22)
            ty = cur_y + 34
            panel = Image.new("RGBA", (tile_w, tile_h), (0, 0, 0, 0))
            pd = ImageDraw.Draw(panel)
            pd.rounded_rectangle((0, 0, tile_w - 1, tile_h - 1), radius=14, fill=(10, 18, 34, 215), outline=(*col, 130), width=2)
            canvas.alpha_composite(panel, (tx, ty))

            _paste_circular_portrait(canvas, champ_img_map.get(c["champion"]), (tx + 52, ty + 54), 68, col, (112, 138, 224), 3, c["champion"])
            d.text((tx + 98, ty + 14), _display_champ_name(c["champion"])[:13], font=_font(21, weight="SemiBold"), fill=(246, 250, 255, 255))
            d.text((tx + 98, ty + 44), f"{c['wins']}W-{c['losses']}L  •  {c['wr']:.0f}% WR", font=_font(16, weight="Bold", mono=True), fill=(*col, 255))
            d.text((tx + 98, ty + 70), f"{c['kda']:.2f} KDA  •  {c['cs_per_min']:.1f} CS/m", font=_font(14, weight="Regular", mono=True), fill=(154, 178, 210, 235))

        cur_y += 164

    # Bottom Role Breakdown strip
    if pool.get("roles"):
        ry = 696
        d.text((58, ry), "ROLES:", font=_font(16, weight="Bold", mono=True), fill=(154, 180, 214, 235))
        rx = 148
        for r in pool["roles"][:5]:
            r_col = (86, 236, 146) if r["wr"] >= 55 else ((104, 198, 255) if r["wr"] >= 45 else (255, 118, 124))
            txt = f"{r['role']}: {r['wins']}W-{r['losses']}L ({r['wr']:.0f}%)"
            tw = int(d.textlength(txt, font=_font(16, weight="SemiBold", mono=True))) + 28
            d.rounded_rectangle((rx, ry - 6, rx + tw, ry + 26), radius=8, fill=(14, 26, 48, 220), outline=(*r_col, 170), width=1)
            d.text((rx + 14, ry + 1), txt, font=_font(16, weight="SemiBold", mono=True), fill=(*r_col, 255))
            rx += tw + 14

    _draw_footer(canvas, "CHAMPION POOL AUDIT  •  JEFF BOT")
    return _to_png_bytes(canvas)


# -------------------------------------------------------------------------
# 9. Wins vs. Losses Coaching Split Card (/coach)
# -------------------------------------------------------------------------

async def render_coach_card(ps, split: dict, assets: AssetCache, days: int = 14) -> io.BytesIO:
    W, H = 1400, 760
    prof_img, emb_img = await asyncio.gather(
        assets.get_profile_icon(ps.profile_icon_id),
        assets.get_rank_emblem(ps.solo_tier),
    )

    canvas = _render_ascii_backdrop(W, H)
    d = ImageDraw.Draw(canvas)

    _paste_circular_portrait(canvas, prof_img, (118, 116), 114, (92, 224, 255), (186, 116, 255), 4, ps.display_name)
    d.text((202, 68), f"Coach Jeff — {ps.display_name}", font=_font(46, weight="SemiBold"), fill=(246, 250, 255, 255))
    _spaced_text(
        d,
        (204, 130),
        f"WINS VS LOSSES SPLIT  •  LAST {days} DAYS  •  {ps.wins}W-{ps.losses}L ({ps.win_rate:.0f}% WR)",
        _font(16, weight="Regular", mono=True),
        (154, 180, 214, 235),
        tracking=2.0,
    )
    _paste_rank_emblem(canvas, emb_img, (W - 180, 48, 124, 124))

    w = split.get("wins")
    l = split.get("losses")

    # Side headers
    w_lbl = f"IN WINS ({w['games']}G)" if w else "IN WINS (0G)"
    l_lbl = f"IN LOSSES ({l['games']}G)" if l else "IN LOSSES (0G)"
    _spaced_text(d, (160, 206), w_lbl, _font(19, weight="Bold", mono=True), (88, 240, 158, 255), tracking=2.6)
    d.text((W - 160, 206), l_lbl, font=_font(19, weight="Bold", mono=True), fill=(255, 114, 122, 255), anchor="ra")

    def _val(bucket, key):
        return bucket[key] if bucket else 0.0

    # (label, win_fmt, loss_fmt, win_num, loss_num, lower_is_better)
    rows = [
        ("AVG DEATHS", f"{_val(w, 'avg_deaths'):.1f}", f"{_val(l, 'avg_deaths'):.1f}", _val(w, "avg_deaths"), _val(l, "avg_deaths"), True),
        ("KDA RATIO", f"{_val(w, 'kda'):.2f}", f"{_val(l, 'kda'):.2f}", _val(w, "kda"), _val(l, "kda"), False),
        ("CS / MIN", f"{_val(w, 'cs_per_min'):.1f}", f"{_val(l, 'cs_per_min'):.1f}", _val(w, "cs_per_min"), _val(l, "cs_per_min"), False),
        ("KILL PART.", f"{_val(w, 'avg_kp'):.0f}%", f"{_val(l, 'avg_kp'):.0f}%", _val(w, "avg_kp"), _val(l, "avg_kp"), False),
        ("DMG SHARE", f"{_val(w, 'avg_dmg_share'):.0f}%", f"{_val(l, 'avg_dmg_share'):.0f}%", _val(w, "avg_dmg_share"), _val(l, "avg_dmg_share"), False),
        ("VISION / GAME", f"{_val(w, 'avg_vision'):.1f}", f"{_val(l, 'avg_vision'):.1f}", _val(w, "avg_vision"), _val(l, "avg_vision"), False),
        ("CONTROL WARDS", f"{_val(w, 'avg_control_wards'):.1f}", f"{_val(l, 'avg_control_wards'):.1f}", _val(w, "avg_control_wards"), _val(l, "avg_control_wards"), False),
    ]

    cx = W // 2
    max_bar = 350
    for idx, (lbl, wf, lf, wn, ln, _) in enumerate(rows):
        ry = 258 + idx * 62
        total = max(0.001, wn + ln)
        bw_w = int(max_bar * (wn / total)) if (wn + ln) > 0 else 0
        bw_l = int(max_bar * (ln / total)) if (wn + ln) > 0 else 0

        d.text((cx, ry + 14), lbl, font=_font(16, weight="Bold", mono=True), fill=(184, 204, 232, 245), anchor="mm")

        l_end = cx - 98
        d.rounded_rectangle((l_end - max_bar, ry + 6, l_end, ry + 22), radius=6, fill=(20, 32, 54, 220))
        if bw_w > 4:
            d.rounded_rectangle((l_end - bw_w, ry + 6, l_end, ry + 22), radius=6, fill=(78, 228, 152, 245))
        d.text((l_end - max_bar - 22, ry + 14), wf if w else "—", font=_font(24, weight="Bold", mono=True), fill=(116, 242, 174, 255), anchor="rm")

        r_start = cx + 98
        d.rounded_rectangle((r_start, ry + 6, r_start + max_bar, ry + 22), radius=6, fill=(20, 32, 54, 220))
        if bw_l > 4:
            d.rounded_rectangle((r_start, ry + 6, r_start + bw_l, ry + 22), radius=6, fill=(246, 102, 116, 245))
        d.text((r_start + max_bar + 22, ry + 14), lf if l else "—", font=_font(24, weight="Bold", mono=True), fill=(255, 132, 142, 255), anchor="lm")

    _draw_footer(canvas, "WINS VS LOSSES COACHING  •  JEFF BOT")
    return _to_png_bytes(canvas)


# -------------------------------------------------------------------------
# 10. Squad & Duo Synergy Chemistry Card (/synergy)
# -------------------------------------------------------------------------

async def render_synergy_card(synergy: dict, assets: AssetCache) -> io.BytesIO:
    W, H = 1400, 788
    icon_ids: dict[str, int] = synergy.get("profile_icons") or {}
    names = list(icon_ids.keys())
    fetched_icons = await asyncio.gather(*[assets.get_profile_icon(icon_ids[n]) for n in names]) if names else []
    icon_map = dict(zip(names, fetched_icons))

    canvas = _render_ascii_backdrop(W, H)
    d = ImageDraw.Draw(canvas)

    days = synergy.get("days", 7)
    d.text((58, 44), "Squad Synergy", font=_font(48, weight="SemiBold"), fill=(246, 250, 255, 255))
    _spaced_text(d, (62, 108), f"TOGETHER VS SOLO CHEMISTRY  •  LAST {days} DAYS", _font(16, weight="Regular", mono=True), (154, 180, 214, 235), tracking=2.2)

    # Top-right stack size pills
    stacks = synergy.get("stacks", {})
    sx = W - 58
    for size in sorted(stacks.keys(), reverse=True):
        w, l = stacks[size]
        if w + l == 0:
            continue
        txt = f"{size}-STACK: {w}W-{l}L ({w / (w + l) * 100:.0f}%)"
        tw = int(d.textlength(txt, font=_font(15, weight="Bold", mono=True))) + 24
        d.rounded_rectangle((sx - tw, 62, sx, 96), radius=8, fill=(18, 28, 52, 220), outline=(146, 118, 255, 180), width=1)
        d.text((sx - tw // 2, 79), txt, font=_font(15, weight="Bold", mono=True), fill=(214, 192, 255, 255), anchor="mm")
        sx -= tw + 12

    # Two summary tiles: Together vs Solo
    def _draw_summary(x0: int, label: str, wins: int, losses: int, wr: float, accent: tuple):
        tw, th = 626, 134
        panel = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        pd = ImageDraw.Draw(panel)
        pd.rounded_rectangle((0, 0, tw - 1, th - 1), radius=16, fill=(10, 18, 34, 212), outline=(*accent, 165), width=2)
        canvas.alpha_composite(panel, (x0, 162))

        _spaced_text(d, (x0 + 26, 180), label, _font(16, weight="Bold", mono=True), (*accent, 245), tracking=2.4)
        g = wins + losses
        d.text((x0 + 26, 212), f"{wr:.0f}% WR" if g else "—", font=_font(40, weight="Bold"), fill=(250, 252, 255, 255))
        d.text((x0 + tw - 26, 224), f"{wins}W / {losses}L  ({g} games)", font=_font(20, weight="SemiBold", mono=True), fill=(182, 202, 228, 240), anchor="ra")

        bx, by, bw = x0 + 26, 272, tw - 52
        d.rounded_rectangle((bx, by, bx + bw, by + 10), radius=5, fill=(24, 38, 64, 230))
        if g and wr > 0:
            d.rounded_rectangle((bx, by, bx + max(8, int(bw * wr / 100.0)), by + 10), radius=5, fill=(*accent, 255))

    _draw_summary(58, "PLAYING TOGETHER", synergy.get("together_wins", 0), synergy.get("together_losses", 0), synergy.get("together_wr", 0.0), (88, 226, 255))
    _draw_summary(716, "PLAYING SOLO", synergy.get("solo_wins", 0), synergy.get("solo_losses", 0), synergy.get("solo_wr", 0.0), (176, 132, 255))

    # Duo chemistry grid (up to 6 pairs in 2x3)
    _draw_icon(canvas, "star", (58, 330), 22, (206, 144, 255))
    d.text((92, 328), "Duo Pair Records", font=_font(21, weight="Regular", mono=True), fill=(214, 224, 240, 245))
    d.line([(314, 341), (W - 58, 341)], fill=(54, 78, 118, 170), width=1)

    duos = synergy.get("duos", [])[:6]
    if duos:
        dw, dh = 626, 108
        for idx, duo in enumerate(duos):
            col_i = idx % 2
            row_i = idx // 2
            dx = 58 + col_i * 658
            dy = 374 + row_i * 122

            panel = Image.new("RGBA", (dw, dh), (0, 0, 0, 0))
            pd = ImageDraw.Draw(panel)
            wr = duo["wr"]
            col = (86, 236, 146) if wr >= 55 else ((104, 198, 255) if wr >= 45 else (255, 114, 122))
            pd.rounded_rectangle((0, 0, dw - 1, dh - 1), radius=14, fill=(10, 18, 34, 212), outline=(*col, 135), width=2)
            canvas.alpha_composite(panel, (dx, dy))

            pair_names = [p.strip() for p in duo["pair"].split("+")]
            p1 = pair_names[0] if pair_names else "?"
            p2 = pair_names[1] if len(pair_names) > 1 else "?"
            _paste_circular_portrait(canvas, icon_map.get(p1), (dx + 46, dy + 54), 60, (92, 224, 255), (146, 118, 255), 3, p1)
            _paste_circular_portrait(canvas, icon_map.get(p2), (dx + 94, dy + 54), 60, (186, 116, 255), (255, 114, 152), 3, p2)

            d.text((dx + 144, dy + 20), duo["pair"], font=_font(22, weight="SemiBold"), fill=(246, 250, 255, 255))
            d.text((dx + dw - 24, dy + 22), f"{wr:.0f}%", font=_font(26, weight="Bold", mono=True), fill=(*col, 255), anchor="ra")
            d.text((dx + 144, dy + 54), f"{duo['wins']}W / {duo['losses']}L  ({duo['games']} games)", font=_font(15, weight="Regular", mono=True), fill=(154, 178, 210, 235))

            bx, by, bw = dx + 144, dy + 82, dw - 168
            d.rounded_rectangle((bx, by, bx + bw, by + 8), radius=4, fill=(24, 38, 64, 230))
            if wr > 0:
                d.rounded_rectangle((bx, by, bx + max(6, int(bw * wr / 100.0)), by + 8), radius=4, fill=(*col, 255))
    else:
        d.text((W // 2, 520), "No duo or squad games found in this window.", font=_font(22), fill=(146, 168, 198, 220), anchor="mm")

    _draw_footer(canvas, "SQUAD CHEMISTRY  •  JEFF BOT")
    return _to_png_bytes(canvas)


# -------------------------------------------------------------------------
# 11. Top 10 Champion Mastery Gallery Card (/mastery)
# -------------------------------------------------------------------------

async def render_mastery_card(mastery: dict, assets: AssetCache) -> io.BytesIO:
    W, H = 1400, 820
    items = mastery.get("masteries", [])[:10]
    fetched = await asyncio.gather(
        assets.get_profile_icon(mastery.get("profile_icon_id", 0)),
        *[assets.get_champion_icon(m["name"]) for m in items],
    )
    prof_img = fetched[0]
    champ_imgs = fetched[1:]

    canvas = _render_ascii_backdrop(W, H)
    d = ImageDraw.Draw(canvas)

    _paste_circular_portrait(canvas, prof_img, (116, 112), 112, (236, 192, 92), (92, 224, 255), 4, mastery["display_name"])
    d.text((198, 66), f"{mastery['display_name']}'s Top Champions", font=_font(44, weight="SemiBold"), fill=(246, 250, 255, 255))
    ver = mastery.get("version") or "LIVE"
    _spaced_text(d, (202, 128), f"CHAMPION MASTERY GALLERY  •  PATCH {ver}", _font(16, weight="Regular", mono=True), (154, 180, 214, 235), tracking=2.2)

    if items:
        # #1 Main spotlight banner
        top = items[0]
        sp = Image.new("RGBA", (W - 116, 162), (0, 0, 0, 0))
        spd = ImageDraw.Draw(sp)
        spd.rounded_rectangle((0, 0, W - 117, 161), radius=16, fill=(14, 22, 38, 218), outline=(236, 192, 92, 210), width=2)
        canvas.alpha_composite(sp, (58, 194))

        _paste_circular_portrait(canvas, champ_imgs[0], (148, 275), 116, (244, 204, 96), (92, 224, 255), 4, top["name"])
        _spaced_text(d, (236, 218), "#1 SIGNATURE MAIN", _font(15, weight="Bold", mono=True), (242, 200, 96, 255), tracking=2.6)
        d.text((234, 246), _display_champ_name(top["name"]), font=_font(44, weight="Bold"), fill=(250, 252, 255, 255))
        d.text((236, 304), f"Mastery Level {top['level']}", font=_font(20, weight="Regular", mono=True), fill=(176, 198, 226, 240))
        d.text((W - 92, 256), f"{top['points']:,}", font=_font(48, weight="Bold", mono=True), fill=(244, 204, 102, 255), anchor="ra")
        d.text((W - 92, 314), "MASTERY POINTS", font=_font(14, weight="Regular", mono=True), fill=(154, 178, 210, 235), anchor="ra")

        # Remaining #2..#10 in 3x3 grid
        tw, th = 414, 106
        for idx, m in enumerate(items[1:10], start=2):
            grid_i = idx - 2
            col_i = grid_i % 3
            row_i = grid_i // 3
            tx = 58 + col_i * (tw + 21)
            ty = 384 + row_i * (th + 18)

            tile = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
            td = ImageDraw.Draw(tile)
            td.rounded_rectangle((0, 0, tw - 1, th - 1), radius=14, fill=(10, 18, 34, 212), outline=(52, 76, 116, 180), width=2)
            canvas.alpha_composite(tile, (tx, ty))

            _paste_circular_portrait(canvas, champ_imgs[idx - 1], (tx + 52, ty + 53), 66, (92, 214, 255), (176, 116, 255), 3, m["name"])
            d.text((tx + 98, ty + 18), f"#{idx}  {_display_champ_name(m['name'])[:14]}", font=_font(21, weight="SemiBold"), fill=(246, 250, 255, 255))
            d.text((tx + 98, ty + 50), f"Level {m['level']}  •  {m['points']:,} pts", font=_font(16, weight="Regular", mono=True), fill=(154, 184, 220, 240))
    else:
        d.text((W // 2, 460), "No mastery data available.", font=_font(24), fill=(146, 168, 198, 220), anchor="mm")

    _draw_footer(canvas, "CHAMPION MASTERY  •  JEFF BOT")
    return _to_png_bytes(canvas)


# -------------------------------------------------------------------------
# 12. Weekly Damage Leaderboard Card (/damage)
# -------------------------------------------------------------------------

async def render_damage_card(stats: list, assets: AssetCache) -> io.BytesIO:
    W, H = 1400, 760
    active = sorted([p for p in stats if p.games_played > 0 and not p.error], key=lambda p: p.total_damage_to_champions, reverse=True)
    inactive = [p for p in stats if p not in active]
    ordered = (active + inactive)[:5]

    icons = await asyncio.gather(*[assets.get_profile_icon(p.profile_icon_id) for p in ordered])

    canvas = _render_ascii_backdrop(W, H)
    d = ImageDraw.Draw(canvas)

    group_total = sum(p.total_damage_to_champions for p in active)
    d.text((58, 46), "Weekly Damage Leaderboard", font=_font(46, weight="SemiBold"), fill=(246, 250, 255, 255))
    _spaced_text(d, (62, 108), "TOTAL DAMAGE TO CHAMPIONS  •  LAST 7 DAYS", _font(16, weight="Regular", mono=True), (154, 180, 214, 235), tracking=2.2)

    d.text((W - 58, 52), f"{group_total:,}", font=_font(42, weight="Bold", mono=True), fill=(255, 134, 114, 255), anchor="ra")
    d.text((W - 58, 104), "SQUAD TOTAL DAMAGE", font=_font(14, weight="Regular", mono=True), fill=(154, 178, 210, 235), anchor="ra")

    max_dmg = max((p.total_damage_to_champions for p in active), default=1)
    rank_colors = [
        (244, 202, 92),   # #1 Gold
        (196, 212, 232),  # #2 Silver
        (214, 146, 94),   # #3 Bronze
        (134, 162, 202),  # #4
        (114, 138, 176),
    ]

    for idx, ps in enumerate(ordered):
        ry = 178 + idx * 106
        rc = rank_colors[min(idx, len(rank_colors) - 1)]
        panel = Image.new("RGBA", (W - 116, 92), (0, 0, 0, 0))
        pd = ImageDraw.Draw(panel)
        pd.rounded_rectangle((0, 0, W - 117, 91), radius=14, fill=(10, 18, 34, 212), outline=(*rc, 170 if idx == 0 else 105), width=2)
        canvas.alpha_composite(panel, (58, ry))

        d.text((92, ry + 46), f"#{idx + 1}", font=_font(28, weight="Bold", mono=True), fill=(*rc, 255), anchor="mm")
        _paste_circular_portrait(canvas, icons[idx], (164, ry + 46), 64, rc, (255, 102, 114), 3, ps.display_name)

        d.text((216, ry + 14), ps.display_name, font=_font(24, weight="SemiBold"), fill=(246, 250, 255, 255))
        if ps.games_played > 0 and not ps.error:
            d.text(
                (216, ry + 46),
                f"{ps.games_played} games  •  {_fmt_k(ps.avg_damage_per_game)} / game",
                font=_font(15, weight="Regular", mono=True),
                fill=(154, 178, 210, 235),
            )
            d.text((W - 84, ry + 18), f"{ps.total_damage_to_champions:,}", font=_font(28, weight="Bold", mono=True), fill=(252, 254, 255, 255), anchor="ra")

            # Neon damage bar
            bx, by, bw = 510, ry + 54, W - 600
            d.rounded_rectangle((bx, by, bx + bw, by + 14), radius=7, fill=(24, 38, 64, 230))
            fw = max(12, int(bw * (ps.total_damage_to_champions / max(1, max_dmg))))
            bar_c = (255, 112, 92, 255) if idx == 0 else (228, 96, 128, 235)
            d.rounded_rectangle((bx, by, bx + fw, by + 14), radius=7, fill=bar_c)
        else:
            msg = "No games this week" if not ps.error else "Lookup error"
            d.text((216, ry + 48), msg, font=_font(16, weight="Regular", mono=True), fill=(134, 156, 188, 215))

    _draw_footer(canvas, "DAMAGE LEADERBOARD  •  JEFF BOT")
    return _to_png_bytes(canvas)


# -------------------------------------------------------------------------
# 13. Rank Promotion & Demotion Ceremony Card
# -------------------------------------------------------------------------

async def render_rank_change_card(event: dict, assets: AssetCache) -> io.BytesIO:
    W, H = 1400, 620
    is_promo = event.get("direction", "promotion") == "promotion"
    prof_img, old_emb, new_emb = await asyncio.gather(
        assets.get_profile_icon(event.get("profile_icon_id", 0)),
        assets.get_rank_emblem(event.get("old_tier", "UNRANKED")),
        assets.get_rank_emblem(event.get("new_tier", "UNRANKED")),
    )

    canvas = _render_ascii_backdrop(W, H)
    d = ImageDraw.Draw(canvas)

    # Radiant halo behind new rank emblem
    halo = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    hd = ImageDraw.Draw(halo)
    c1, c2 = TIER_ACCENTS.get((event.get("new_tier") or "UNRANKED").upper(), TIER_ACCENTS["UNRANKED"])
    halo_col = (*c1, 95) if is_promo else (238, 72, 86, 85)
    hd.ellipse((780, 170, 1180, 550), fill=halo_col)
    halo = halo.filter(ImageFilter.GaussianBlur(radius=48))
    canvas = Image.alpha_composite(canvas, halo)
    d = ImageDraw.Draw(canvas)

    _paste_circular_portrait(
        canvas,
        prof_img,
        (124, 118),
        118,
        (244, 204, 96) if is_promo else (255, 98, 114),
        c1,
        4,
        event["display_name"],
    )

    tag = "TIER PROMOTION" if is_promo else "TIER DEMOTION"
    tag_col = (244, 204, 96, 255) if is_promo else (255, 110, 118, 255)
    _spaced_text(d, (212, 66), tag, _font(19, weight="Bold", mono=True), tag_col, tracking=3.8)
    verb = "Promoted!" if is_promo else "Demoted..."
    d.text((210, 96), f"{event['display_name']} {verb}", font=_font(50, weight="Bold"), fill=(248, 250, 255, 255))

    # Old Emblem -> Arrow -> New Emblem
    _paste_rank_emblem(canvas, old_emb, (240, 240, 180, 180))
    old_txt = f"{event['old_tier'].capitalize()} {event.get('old_rank', '')}".strip()
    d.text((330, 444), old_txt, font=_font(26, weight="SemiBold"), fill=(164, 184, 212, 235), anchor="mt")
    d.text((330, 478), f"{event.get('old_lp', 0)} LP", font=_font(18, weight="Regular", mono=True), fill=(132, 154, 186, 220), anchor="mt")

    d.text((640, 336), "→", font=_font(82, weight="Bold"), fill=tag_col, anchor="mm")

    _paste_rank_emblem(canvas, new_emb, (855, 204, 246, 246))
    new_txt = f"{event['new_tier'].capitalize()} {event.get('new_rank', '')}".strip()
    d.text((978, 456), new_txt, font=_font(34, weight="Bold"), fill=(252, 254, 255, 255), anchor="mt")
    d.text((978, 500), f"{event.get('new_lp', 0)} LP", font=_font(20, weight="SemiBold", mono=True), fill=(*c1, 255), anchor="mt")

    _draw_footer(canvas, "RANK TRACKER  •  JEFF BOT")
    return _to_png_bytes(canvas)


# -------------------------------------------------------------------------
# 14. Weekly Coaching Goal HUD Card (/goal)
# -------------------------------------------------------------------------

async def render_goal_card(
    display_name: str,
    profile_icon_id: int,
    eval_result: dict,
    assets: AssetCache,
    is_new: bool = False,
) -> io.BytesIO:
    W, H = 1400, 560
    prof_img = await assets.get_profile_icon(profile_icon_id)

    canvas = _render_ascii_backdrop(W, H)
    d = ImageDraw.Draw(canvas)

    goal = eval_result["goal"]
    met = bool(eval_result["met"])
    games = eval_result["games"]

    pill_txt, pill_bg, pill_fg = (
        ("NEW GOAL SET", (72, 54, 18, 225), (244, 204, 96, 255))
        if is_new
        else (
            ("ON TRACK", (18, 92, 58, 225), (92, 242, 160, 255))
            if met
            else ("KEEP GRINDING", (20, 58, 108, 225), (112, 210, 255, 255))
        )
    )

    _paste_circular_portrait(canvas, prof_img, (118, 114), 112, (92, 224, 255), (236, 192, 92), 4, display_name)
    _spaced_text(d, (202, 64), "WEEKLY COACHING GOAL", _font(16, weight="Bold", mono=True), (154, 180, 214, 235), tracking=2.8)
    d.text((200, 92), display_name, font=_font(48, weight="SemiBold"), fill=(246, 250, 255, 255))

    d.rounded_rectangle((W - 280, 82, W - 58, 128), radius=10, fill=pill_bg, outline=pill_fg, width=2)
    d.text((W - 169, 105), pill_txt, font=_font(17, weight="Bold", mono=True), fill=pill_fg, anchor="mm")

    # Main goal glass panel
    panel = Image.new("RGBA", (W - 116, 266), (0, 0, 0, 0))
    pd = ImageDraw.Draw(panel)
    pd.rounded_rectangle((0, 0, W - 117, 265), radius=16, fill=(10, 18, 34, 214), outline=(56, 82, 124, 190), width=2)
    canvas.alpha_composite(panel, (58, 206))

    d.text((94, 236), goal["description"][:72], font=_font(26, weight="SemiBold"), fill=(246, 250, 255, 255))

    # Progress bar from baseline -> target
    base_v = float(eval_result["baseline"])
    targ_v = float(eval_result["target"])
    curr_v = float(eval_result["current"]) if games else base_v
    if eval_result["op"] == "<=":
        denom = max(0.01, base_v - targ_v)
        pct = max(0.0, min(1.0, (base_v - curr_v) / denom))
    else:
        denom = max(0.01, targ_v - base_v)
        pct = max(0.0, min(1.0, (curr_v - base_v) / denom))

    bx, by, bw = 94, 304, W - 188
    d.rounded_rectangle((bx, by, bx + bw, by + 20), radius=10, fill=(24, 38, 64, 235))
    fill_c = (86, 238, 152, 255) if met else (96, 204, 255, 255)
    if pct > 0.02:
        d.rounded_rectangle((bx, by, bx + max(18, int(bw * pct)), by + 20), radius=10, fill=fill_c)

    unit = eval_result["unit"]
    cols = [
        ("BASELINE", f"{base_v:.1f} {unit}", (172, 194, 224, 240)),
        (f"CURRENT ({games}G)", f"{curr_v:.1f} {unit}" if games else "0 games yet", fill_c),
        ("TARGET", f"{eval_result['op']} {targ_v:.1f} {unit}", (244, 204, 96, 255)),
    ]
    for i, (lbl, val, col) in enumerate(cols):
        cx = 94 + i * 410
        d.text((cx, 356), lbl, font=_font(15, weight="Bold", mono=True), fill=(148, 172, 204, 235))
        d.text((cx, 384), val, font=_font(32, weight="Bold", mono=True), fill=col)

    _draw_footer(canvas, "WEEKLY GOAL TRACKER  •  JEFF BOT")
    return _to_png_bytes(canvas)
