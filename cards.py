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
