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
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

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
    "LeBlanc": "Leblanc",
    "Vel'Koz": "Velkoz",
    "VelKoz": "Velkoz",
    "Cho'Gath": "Chogath",
    "ChoGath": "Chogath",
    "Kha'Zix": "Khazix",
    "KhaZix": "Khazix",
    "Kai'Sa": "Kaisa",
    "KaiSa": "Kaisa",
    "Bel'Veth": "Belveth",
    "BelVeth": "Belveth",
    "K'Sante": "KSante",
    "Kog'Maw": "KogMaw",
    "Rek'Sai": "RekSai",
    "Dr. Mundo": "DrMundo",
    "Renata Glasc": "Renata",
    "Nunu & Willump": "Nunu",
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
        key = CHAMP_KEY_OVERRIDES.get(
            champ_name,
            champ_name.replace(" ", "").replace("'", "").replace(".", ""),
        )
        url = f"https://ddragon.leagueoflegends.com/cdn/{ver}/img/champion/{key}.png"
        img = await self._fetch_image(url, f"champ/{key}.png")
        if img is None and key != champ_name:
            url2 = f"https://ddragon.leagueoflegends.com/cdn/{ver}/img/champion/{champ_name}.png"
            img = await self._fetch_image(url2, f"champ/{champ_name}.png")
        return img

    async def get_champion_splash(self, champ_name: str) -> Optional[Image.Image]:
        """Fetches & caches Riot's official HD champion splash art for Splash-to-ASCII backdrops."""
        if not champ_name:
            return None
        key = CHAMP_KEY_OVERRIDES.get(
            champ_name,
            champ_name.replace(" ", "").replace("'", "").replace(".", ""),
        )
        url = f"https://ddragon.leagueoflegends.com/cdn/img/champion/splash/{key}_0.jpg"
        img = await self._fetch_image(url, f"splash/{key}_0.jpg")
        if img is None and key != champ_name:
            url2 = f"https://ddragon.leagueoflegends.com/cdn/img/champion/splash/{champ_name}_0.jpg"
            img = await self._fetch_image(url2, f"splash/{champ_name}_0.jpg")
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
# 6 Runeterra Procedural ASCII Backdrops + Live Champion Splash-to-ASCII
# -------------------------------------------------------------------------

_backdrop_cache: dict[tuple[int, int, str], Image.Image] = {}
_splash_backdrop_cache: dict[tuple[int, int, str], Image.Image] = {}
_MAX_SPLASH_CACHE = 16


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
    ridge = (
        0.395
        - 0.060 * math.exp(-((nx - 0.58) / 0.095) ** 2)
        - 0.055 * math.exp(-((nx - 0.72) / 0.11) ** 2)
        - 0.085 * math.exp(-((nx - 0.92) / 0.09) ** 2)
        - 0.014 * math.sin(nx * 54.0) * max(0.0, nx - 0.44)
    )
    towers = [
        (0.692, 0.011, 0.345, 0.075),
        (0.716, 0.008, 0.335, 0.065),
        (0.742, 0.012, 0.315, 0.105),
        (0.768, 0.010, 0.305, 0.115),
        (0.793, 0.016, 0.285, 0.195),
        (0.818, 0.011, 0.305, 0.125),
        (0.842, 0.012, 0.320, 0.090),
        (0.866, 0.009, 0.340, 0.060),
    ]
    y = ridge
    for cx, hw, wall_y, spire_h in towers:
        dx = abs(nx - cx)
        if dx <= hw:
            spire_top = wall_y - spire_h * (1.0 - (dx / hw) ** 0.55)
            y = min(y, spire_top)
        elif dx <= hw * 1.65:
            notch = 0.007 if int(nx * 320) % 2 == 0 else 0.0
            y = min(y, wall_y + 0.025 + notch)
    return y


def _shurima_silhouette_y(nx: float) -> float:
    """Twin pyramids + central Shuriman obelisk spire rising toward the Sun Disc."""
    dune = 0.395 - 0.032 * math.sin(nx * 14.0 + 0.4) - 0.016 * math.cos(nx * 29.0)
    # Left pyramid, right pyramid, central obelisk
    p1 = 0.24 + abs(nx - 0.61) * 1.45 if abs(nx - 0.61) < 0.11 else 1.0
    p2 = 0.25 + abs(nx - 0.90) * 1.35 if abs(nx - 0.90) < 0.11 else 1.0
    obelisk = 0.19 + abs(nx - 0.785) * 4.5 if abs(nx - 0.785) < 0.032 else 1.0
    return min(dune, p1, p2, obelisk)


def _ionia_silhouette_y(nx: float) -> float:
    """Steep misty Ionian karst pillars + pagoda shrine roof."""
    pillars = [
        (0.49, 0.036, 0.26),
        (0.60, 0.044, 0.21),
        (0.72, 0.038, 0.24),
        (0.85, 0.052, 0.19),
        (0.95, 0.042, 0.25),
    ]
    y = 0.41 - 0.02 * math.sin(nx * 22.0)
    for cx, hw, top_y in pillars:
        dx = abs(nx - cx)
        if dx < hw:
            # Steep flat-topped karst pillar with organic rock slope
            t = dx / hw
            y = min(y, top_y + 0.17 * (t ** 2.6))
    # Pagoda eaves atop pillar at 0.60
    if abs(nx - 0.60) < 0.035:
        eave = 0.178 + abs(nx - 0.60) * 0.85
        y = min(y, eave)
    return y


def _zaun_skyline_y(nx: float) -> float:
    """Geometric Piltover/Zaun Art-Deco towers & clocktower spires."""
    towers = [
        (0.47, 0.018, 0.30),
        (0.53, 0.022, 0.25),
        (0.59, 0.016, 0.28),
        (0.66, 0.026, 0.22),
        (0.73, 0.019, 0.26),
        (0.79, 0.028, 0.16),  # Grand Piltover clocktower
        (0.86, 0.024, 0.23),
        (0.93, 0.028, 0.27),
    ]
    y = 0.39
    for cx, hw, top_y in towers:
        dx = abs(nx - cx)
        if dx <= hw:
            # Stepped Art-Deco silhouette
            step = 0.03 if dx > hw * 0.58 else 0.0
            needle = -0.045 * (1.0 - dx / (hw * 0.18)) if dx < hw * 0.18 else 0.0
            y = min(y, top_y + step + needle)
    return y


def _noxus_peaks_y(nx: float) -> float:
    """Jagged obsidian Noxian fortress & volcanic bastion peaks."""
    ridge = (
        0.385
        - 0.11 * math.exp(-((nx - 0.54) / 0.075) ** 2)
        - 0.13 * math.exp(-((nx - 0.68) / 0.08) ** 2)
        - 0.14 * math.exp(-((nx - 0.89) / 0.085) ** 2)
        - 0.024 * abs(math.sin(nx * 44.0)) * max(0.0, nx - 0.35)
    )
    # Immortal Bastion central jagged horn spires
    if abs(nx - 0.785) < 0.045:
        horn = 0.26 + 0.09 * math.exp(-((nx - 0.785) / 0.014) ** 2) - 0.07 * (1.0 - abs(nx - 0.785) / 0.045)
        ridge = min(ridge, horn)
    return ridge


THEME_BASES = {
    "citadel": ((5, 10, 22), (10, 23, 46), (36, 58, 96)),
    "midnight": ((5, 10, 22), (10, 23, 46), (36, 58, 96)),
    "shurima": ((12, 9, 5), (26, 20, 11), (96, 76, 36)),
    "ionia": ((5, 13, 18), (10, 28, 36), (36, 86, 94)),
    "zaun": ((5, 12, 16), (9, 28, 34), (34, 92, 96)),
    "crimson": ((14, 5, 10), (30, 10, 18), (104, 38, 52)),
    "cosmic": ((7, 6, 20), (16, 12, 42), (68, 50, 118)),
}


def _render_ascii_backdrop(width: int = 1400, height: int = 788, theme: str = "citadel") -> Image.Image:
    """Renders one of 6 Runeterra procedural ASCII backdrops (cached in memory)."""
    if theme == "midnight":
        theme = "citadel"
    cache_key = (width, height, theme)
    if cache_key in _backdrop_cache:
        return _backdrop_cache[cache_key].copy()

    bot_rgb, top_rgb, border_rgb = THEME_BASES.get(theme, THEME_BASES["citadel"])

    # 1. Base gradient canvas
    base = Image.new("RGBA", (width, height), (*bot_rgb, 255))
    bg_draw = ImageDraw.Draw(base)
    for y in range(height):
        t = 1.0 - y / height
        r = int(bot_rgb[0] + (top_rgb[0] - bot_rgb[0]) * t)
        g = int(bot_rgb[1] + (top_rgb[1] - bot_rgb[1]) * t)
        b = int(bot_rgb[2] + (top_rgb[2] - bot_rgb[2]) * t)
        bg_draw.line([(0, y), (width, y)], fill=(r, g, b, 255))

    # 2. Theme-specific celestial / ambient aura
    cx = int(width * 0.785)
    cy = int(height * 0.185)
    rad = int(height * 0.165)

    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)

    glow_palettes = {
        "citadel": [((22, 64, 148, 52), 3.4), ((46, 118, 224, 72), 2.3), ((98, 182, 255, 92), 1.5), ((186, 232, 255, 112), 1.08)],
        "shurima": [((148, 92, 18, 55), 3.5), ((214, 146, 36, 75), 2.3), ((248, 198, 78, 95), 1.45), ((255, 238, 168, 115), 1.02)],
        "ionia": [((16, 108, 106, 52), 3.4), ((42, 164, 152, 68), 2.2), ((112, 222, 206, 86), 1.4), ((238, 148, 198, 58), 1.9)],
        "zaun": [((12, 98, 112, 54), 3.4), ((28, 168, 156, 74), 2.2), ((72, 234, 194, 92), 1.4), ((166, 255, 232, 100), 1.0)],
        "crimson": [((144, 18, 36, 58), 3.5), ((212, 38, 54, 80), 2.2), ((255, 98, 72, 98), 1.38), ((255, 188, 118, 110), 1.05)],
        "cosmic": [((72, 28, 156, 58), 3.6), ((128, 56, 222, 76), 2.3), ((96, 186, 255, 88), 1.5), ((224, 142, 255, 96), 1.08)],
    }
    for col, mult in glow_palettes.get(theme, glow_palettes["citadel"]):
        rr = int(rad * mult)
        gdraw.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=col)

    bottom_glow = {
        "citadel": (96, 54, 184, 62),
        "shurima": (184, 118, 32, 58),
        "ionia": (196, 88, 154, 52),
        "zaun": (28, 168, 128, 58),
        "crimson": (176, 32, 48, 62),
        "cosmic": (132, 58, 212, 64),
    }.get(theme, (96, 54, 184, 62))
    v_cx, v_cy, v_r = int(width * 0.88), int(height * 0.84), int(height * 0.34)
    gdraw.ellipse((v_cx - v_r, v_cy - v_r, v_cx + v_r, v_cy + v_r), fill=bottom_glow)
    glow = glow.filter(ImageFilter.GaussianBlur(radius=42))
    base = Image.alpha_composite(base, glow)

    # 3. Fine-grained ASCII character matrix
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

            left_fade = min(1.0, max(0.0, (nx - 0.12) / 0.26))
            if left_fade <= 0.01:
                continue

            dx_c = px - cx
            dy_c = py - cy
            dist_c = math.hypot(dx_c, dy_c)
            horizon_fade = 1.0 if ny <= 0.34 else max(0.0, 1.0 - (ny - 0.34) / 0.095)

            intensity = 0.0
            r_col, g_col, b_col = 72, 134, 236

            if theme == "citadel":
                if ny < 0.435:
                    far_y = _mountain_far_y(nx)
                    castle_y = _castle_silhouette_y(nx)
                    if ny >= castle_y and nx > 0.42:
                        if (ny - castle_y) < 0.010 and dist_c < rad * 1.45:
                            intensity, r_col, g_col, b_col = 0.62 * horizon_fade, 148, 212, 255
                        else:
                            intensity = (0.12 + 0.12 * _fbm(nx * 24.0, ny * 24.0)) * horizon_fade
                            r_col, g_col, b_col = 24, 52, 114
                    elif dist_c <= rad:
                        edge = max(0.0, 1.0 - (dist_c / rad) ** 2.6)
                        crater = _fbm(nx * 20.0 + 3.1, ny * 20.0 + 1.7)
                        intensity = min(1.0, (0.62 + 0.32 * edge + 0.24 * (crater - 0.45))) * max(0.35, horizon_fade)
                        r_col, g_col, b_col = int(175 + 78 * intensity), int(222 + 33 * intensity), 255
                    elif ny >= far_y and nx > 0.32:
                        moon_lit = max(0.0, 1.0 - dist_c / (rad * 3.0))
                        intensity = (0.34 + 0.34 * _fbm(nx * 16.0 + 2.0, ny * 16.0) + 0.22 * moon_lit) * horizon_fade * left_fade
                        r_col, g_col, b_col = int(58 + 74 * moon_lit), int(112 + 82 * moon_lit), int(218 + 34 * moon_lit)
                    else:
                        strata = 0.5 + 0.5 * math.sin(ny * 115.0 + _fbm(nx * 6.0, ny * 6.0) * 5.0)
                        cloud = _fbm(nx * 6.5, ny * 14.0)
                        halo = max(0.0, 1.0 - dist_c / (rad * 2.9)) ** 1.55
                        intensity = ((0.16 + 0.34 * cloud * (0.65 + 0.35 * strata)) + 0.56 * halo) * left_fade * horizon_fade
                        r_col, g_col, b_col = int(46 + 114 * halo), int(102 + 116 * halo), int(206 + 48 * halo)

            elif theme == "shurima":
                # Golden Sun Disc with radiant sunbeams + pyramids & obelisk
                if ny < 0.44:
                    sil_y = _shurima_silhouette_y(nx)
                    if ny >= sil_y and nx > 0.40:
                        if (ny - sil_y) < 0.012:
                            intensity, r_col, g_col, b_col = 0.72 * horizon_fade, 255, 224, 134
                        else:
                            intensity = (0.14 + 0.14 * _fbm(nx * 20.0, ny * 20.0)) * horizon_fade
                            r_col, g_col, b_col = 92, 62, 24
                    elif dist_c <= rad * 0.92:
                        # Concentric ornate Shurima Sun Disc rings
                        ring_gap = abs(dist_c - rad * 0.62) < 6.5
                        intensity = (0.28 if ring_gap else 0.88) * max(0.4, horizon_fade)
                        r_col, g_col, b_col = 255, int(212 + 38 * intensity), int(112 + 86 * intensity)
                    else:
                        angle = math.atan2(dy_c, dx_c)
                        rays = max(0.0, math.sin(angle * 12.0)) ** 3
                        halo = max(0.0, 1.0 - dist_c / (rad * 3.1)) ** 1.4
                        haze = _fbm(nx * 7.0, ny * 12.0)
                        intensity = (0.14 * haze + 0.48 * halo + 0.32 * rays * halo) * left_fade * horizon_fade
                        r_col, g_col, b_col = int(176 + 78 * halo), int(118 + 94 * halo), int(42 + 64 * halo)

            elif theme == "ionia":
                # Crescent moon + misty karst peaks + floating sakura petals
                if ny < 0.44:
                    sil_y = _ionia_silhouette_y(nx)
                    # Crescent = inside main circle, outside offset shadow circle
                    dist_cut = math.hypot(px - (cx - rad * 0.42), py - (cy - rad * 0.14))
                    in_crescent = dist_c <= rad * 0.92 and dist_cut > rad * 0.78
                    if ny >= sil_y and nx > 0.38:
                        mist = _fbm(nx * 14.0, ny * 18.0)
                        if (ny - sil_y) < 0.011:
                            intensity, r_col, g_col, b_col = 0.68 * horizon_fade, 148, 244, 224
                        else:
                            intensity = (0.16 + 0.24 * mist) * horizon_fade
                            r_col, g_col, b_col = 24, 92, 102
                    elif in_crescent:
                        intensity = 0.92 * max(0.4, horizon_fade)
                        r_col, g_col, b_col = 228, 255, 248
                    else:
                        mist = _fbm(nx * 6.0, ny * 15.0)
                        halo = max(0.0, 1.0 - dist_c / (rad * 2.7)) ** 1.6
                        petal = _hash2(col + 19, row + 7) > 0.988
                        if petal:
                            intensity, r_col, g_col, b_col = 0.78 * left_fade * horizon_fade, 255, 152, 206
                        else:
                            intensity = (0.18 + 0.32 * mist + 0.44 * halo) * left_fade * horizon_fade
                            r_col, g_col, b_col = int(42 + 88 * halo), int(152 + 86 * halo), int(158 + 72 * halo)

            elif theme == "zaun":
                # Art-Deco Piltover/Zaun skyline + hextech cyan beam + emerald chem-fog
                if ny < 0.44:
                    sky_y = _zaun_skyline_y(nx)
                    if ny >= sky_y and nx > 0.40:
                        # Lit tower windows inside dark geometric buildings
                        win = (col % 3 == 0) and (row % 3 == 0) and _hash2(col, row) > 0.55
                        if win:
                            intensity, r_col, g_col, b_col = 0.78 * horizon_fade, 114, 248, 224
                        elif (ny - sky_y) < 0.010:
                            intensity, r_col, g_col, b_col = 0.68 * horizon_fade, 98, 236, 212
                        else:
                            intensity = 0.14 * horizon_fade
                            r_col, g_col, b_col = 18, 66, 76
                    else:
                        # Hextech grid + rising vertical spotlight beams
                        grid = 0.12 if (col % 8 == 0 or row % 5 == 0) else 0.0
                        beam = max(0.0, 1.0 - abs(nx - 0.79) / 0.16) * max(0.0, 1.0 - ny / 0.40)
                        fog = _fbm(nx * 8.0, ny * 14.0)
                        intensity = (grid + 0.24 * fog + 0.52 * beam) * left_fade * horizon_fade
                        r_col, g_col, b_col = int(38 + 68 * beam), int(162 + 84 * beam), int(168 + 62 * beam)

            elif theme == "crimson":
                # Blood Moon black solar eclipse with blazing crimson corona + Noxus bastion peaks
                if ny < 0.44:
                    sil_y = _noxus_peaks_y(nx)
                    corona = math.exp(-((dist_c - rad * 0.94) / 14.0) ** 2)
                    in_eclipse_core = dist_c < rad * 0.86
                    if ny >= sil_y and nx > 0.36:
                        if (ny - sil_y) < 0.012:
                            intensity, r_col, g_col, b_col = 0.76 * horizon_fade, 255, 118, 102
                        else:
                            intensity = (0.14 + 0.12 * _fbm(nx * 20.0, ny * 20.0)) * horizon_fade
                            r_col, g_col, b_col = 74, 18, 30
                    elif in_eclipse_core:
                        intensity = 0.08 * horizon_fade
                        r_col, g_col, b_col = 48, 12, 22
                    else:
                        halo = max(0.0, 1.0 - dist_c / (rad * 2.8)) ** 1.6
                        smoke = _fbm(nx * 7.5, ny * 14.0)
                        ember = _hash2(col + 31, row + 13) > 0.985
                        if ember:
                            intensity, r_col, g_col, b_col = 0.88 * left_fade * horizon_fade, 255, 198, 112
                        else:
                            intensity = min(1.0, (0.16 * smoke + 0.38 * halo + 0.92 * corona)) * left_fade * horizon_fade
                            r_col = int(176 + 79 * min(1.0, corona + halo))
                            g_col = int(36 + 114 * corona + 42 * halo)
                            b_col = int(56 + 44 * corona)

            elif theme == "cosmic":
                # Mount Targon spiral galaxy nebula + shooting stars
                if ny < 0.44:
                    targon_y = 0.39 - 0.15 * math.exp(-((nx - 0.80) / 0.075) ** 2) - 0.09 * math.exp(-((nx - 0.62) / 0.09) ** 2)
                    if ny >= targon_y and nx > 0.42:
                        if (ny - targon_y) < 0.011:
                            intensity, r_col, g_col, b_col = 0.78 * horizon_fade, 196, 178, 255
                        else:
                            intensity = (0.14 + 0.16 * _fbm(nx * 18.0, ny * 18.0)) * horizon_fade
                            r_col, g_col, b_col = 42, 32, 92
                    else:
                        ang = math.atan2(dy_c * 1.45, dx_c)
                        spiral = 0.5 + 0.5 * math.sin(ang * 2.0 + dist_c * 0.048)
                        arm_falloff = max(0.0, 1.0 - dist_c / (rad * 3.0)) ** 1.25
                        star = 0.85 if _hash2(col + 5, row + 23) > 0.982 else 0.0
                        neb = _fbm(nx * 9.0, ny * 12.0)
                        intensity = min(1.0, (0.14 * neb + 0.68 * spiral * arm_falloff + star)) * left_fade * horizon_fade
                        t_mix = spiral
                        r_col = int(86 + 148 * t_mix)
                        g_col = int(162 - 36 * t_mix + 64 * arm_falloff)
                        b_col = 255

            # Bottom-right themed ASCII foothills (all 6 themes)
            if ny > 0.58 and nx > 0.54 and intensity == 0.0:
                wave1 = 0.72 - 0.065 * math.sin(nx * 11.0 + 0.8) - 0.035 * math.cos(nx * 23.0)
                wave2 = 0.84 - 0.055 * math.sin(nx * 14.0 + 2.4)
                x_fade = min(1.0, (nx - 0.54) / 0.28)
                b_pal = {
                    "citadel": (132, 96, 232),
                    "shurima": (218, 158, 62),
                    "ionia": (82, 198, 186),
                    "zaun": (62, 206, 168),
                    "crimson": (218, 64, 82),
                    "cosmic": (162, 98, 246),
                }.get(theme, (132, 96, 232))
                if ny >= wave2:
                    intensity = (0.30 + 0.34 * _fbm(nx * 16.0 + 9.0, ny * 16.0)) * x_fade * 0.64
                    r_col, g_col, b_col = b_pal
                elif ny >= wave1:
                    intensity = (0.16 + 0.26 * _fbm(nx * 14.0 + 4.0, ny * 14.0)) * x_fade * 0.50
                    r_col, g_col, b_col = (int(b_pal[0] * 0.78), int(b_pal[1] * 0.78), int(b_pal[2] * 0.88))

            if intensity <= 0.05:
                continue

            ch_idx = min(ramp_len, max(1, int(intensity * ramp_len)))
            ch = ramp[ch_idx]
            alpha = int(min(245, max(24, intensity * 235)))
            adraw.text((px, py), ch, font=ascii_font, fill=(r_col, g_col, b_col, alpha))

    base = Image.alpha_composite(base, ascii_layer)

    scrim = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(scrim)
    sdraw.rectangle((int(width * 0.52), int(height * 0.62), int(width * 0.96), int(height * 0.90)), fill=(*bot_rgb, 95))
    scrim = scrim.filter(ImageFilter.GaussianBlur(radius=30))
    base = Image.alpha_composite(base, scrim)

    bdraw = ImageDraw.Draw(base)
    bdraw.rounded_rectangle((1, 1, width - 2, height - 2), radius=16, outline=(*border_rgb, 185), width=2)

    _backdrop_cache[cache_key] = base
    return base.copy()


def _render_splash_ascii_backdrop(
    width: int,
    height: int,
    splash_img: Optional[Image.Image],
    cache_tag: str,
    fallback_theme: str = "citadel",
    tint_rgb: Optional[tuple[int, int, int]] = None,
) -> Image.Image:
    """Converts an official Riot champion splash art into a glowing colored ASCII backdrop."""
    if splash_img is None:
        return _render_ascii_backdrop(width, height, theme=fallback_theme)

    cache_key = (width, height, f"splash:{cache_tag}:{tint_rgb}")
    if cache_key in _splash_backdrop_cache:
        return _splash_backdrop_cache[cache_key].copy()

    # 1. Base midnight canvas
    base = Image.new("RGBA", (width, height), (6, 10, 20, 255))
    bg_draw = ImageDraw.Draw(base)
    for y in range(height):
        t = 1.0 - y / height
        bg_draw.line([(0, y), (width, y)], fill=(int(5 + 6 * t), int(10 + 11 * t), int(20 + 20 * t), 255))

    # 2. Frame splash art across the right 82% of the card, enhanced for crisp ASCII readability
    cell_w, cell_h = 6, 9
    cols = width // cell_w
    rows = height // cell_h

    src = splash_img.convert("RGB")
    src = ImageEnhance.Contrast(src).enhance(1.38)
    src = ImageEnhance.Color(src).enhance(1.25)

    # Fit height to card height, anchor toward right side where Riot splash art focuses the champ
    sw, sh = src.size
    target_h = rows
    target_w = max(cols, int(round(sw * (target_h / max(1, sh)) * (cell_h / cell_w))))
    resized = src.resize((target_w, target_h), Image.Resampling.BILINEAR)

    # Align right edge of splash with right edge of card
    x_offset = max(0, target_w - cols)
    cropped = resized.crop((x_offset, 0, x_offset + cols, target_h))
    pixels = list(cropped.getdata())

    # Soft ambient aura from splash average right-side color (or optional Victory/Defeat tint)
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    sample_r = sample_g = sample_b = 0
    sample_n = 0
    for r_i in range(0, rows // 2, 3):
        for c_i in range(cols // 2, cols, 3):
            pr, pg, pb = pixels[r_i * cols + c_i]
            sample_r += pr
            sample_g += pg
            sample_b += pb
            sample_n += 1
    avg_rgb = (
        (sample_r // max(1, sample_n), sample_g // max(1, sample_n), sample_b // max(1, sample_n))
        if sample_n
        else (86, 168, 255)
    )
    if tint_rgb:
        avg_rgb = (
            int(avg_rgb[0] * 0.55 + tint_rgb[0] * 0.45),
            int(avg_rgb[1] * 0.55 + tint_rgb[1] * 0.45),
            int(avg_rgb[2] * 0.55 + tint_rgb[2] * 0.45),
        )
    gdraw.ellipse((int(width * 0.42), -int(height * 0.15), int(width * 1.05), int(height * 0.72)), fill=(*avg_rgb, 62))
    glow = glow.filter(ImageFilter.GaussianBlur(radius=54))
    base = Image.alpha_composite(base, glow)

    # 3. Render ASCII matrix sampled directly from splash pixels
    ascii_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    adraw = ImageDraw.Draw(ascii_layer)
    ascii_font = _font(8, weight="Regular", mono=True)
    ramp = " .:-=+*#%@08MW"
    ramp_len = len(ramp) - 1

    for row in range(rows):
        py = row * cell_h
        ny = py / height
        row_off = row * cols

        for col in range(cols):
            px = col * cell_w
            nx = px / width

            # Keep left header (avatar + name + subtitle) 100% crisp; bloom splash across the right 65%
            left_fade = min(1.0, max(0.0, (nx - 0.34) / 0.24))
            if left_fade <= 0.01:
                continue

            pr, pg, pb = pixels[row_off + col]
            lum = (0.299 * pr + 0.587 * pg + 0.114 * pb) / 255.0

            # Showcase champion brightly in upper-right, softly attenuated behind lower stats
            zone_weight = 1.08 if ny <= 0.42 else (0.42 + 0.30 * max(0.0, (nx - 0.52) / 0.48))
            intensity = min(1.0, lum * left_fade * zone_weight)

            if intensity <= 0.065:
                continue

            # Blend splash RGB slightly toward luminous cyan/tint so dark hues still glow cleanly
            r_col = min(255, int(pr * 0.85 + 32))
            g_col = min(255, int(pg * 0.85 + 38))
            b_col = min(255, int(pb * 0.88 + 52))
            if tint_rgb:
                r_col = int(r_col * 0.78 + tint_rgb[0] * 0.22)
                g_col = int(g_col * 0.78 + tint_rgb[1] * 0.22)
                b_col = int(b_col * 0.78 + tint_rgb[2] * 0.22)

            ch_idx = min(ramp_len, max(1, int(min(1.0, lum * 1.15) * ramp_len)))
            alpha = int(min(238, max(28, intensity * 240)))
            adraw.text((px, py), ramp[ch_idx], font=ascii_font, fill=(r_col, g_col, b_col, alpha))

    base = Image.alpha_composite(base, ascii_layer)

    # 4. Smooth dark scrim over stat-heavy areas so foreground text stays effortless to read
    scrim = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(scrim)
    sdraw.rectangle((0, int(height * 0.36), width, height), fill=(6, 10, 20, 118))
    scrim = scrim.filter(ImageFilter.GaussianBlur(radius=34))
    base = Image.alpha_composite(base, scrim)

    border_col = (
        min(255, max(42, avg_rgb[0])),
        min(255, max(58, avg_rgb[1])),
        min(255, max(92, avg_rgb[2])),
    )
    bdraw = ImageDraw.Draw(base)
    bdraw.rounded_rectangle((1, 1, width - 2, height - 2), radius=16, outline=(*border_col, 170), width=2)

    if len(_splash_backdrop_cache) >= _MAX_SPLASH_CACHE:
        _splash_backdrop_cache.pop(next(iter(_splash_backdrop_cache)))
    _splash_backdrop_cache[cache_key] = base
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

    # Fetch all remote icons + #1 champion splash concurrently
    top_champ_name = top_champs[0]["name"] if top_champs else None
    profile_task = assets.get_profile_icon(ps.profile_icon_id)
    emblem_task = assets.get_rank_emblem(ps.solo_tier)
    splash_task = assets.get_champion_splash(top_champ_name) if top_champ_name else asyncio.sleep(0, result=None)
    champ_tasks = [assets.get_champion_icon(c["name"]) for c in top_champs]
    fetched = await asyncio.gather(profile_task, emblem_task, splash_task, *champ_tasks)
    profile_img = fetched[0]
    emblem_img = fetched[1]
    splash_img = fetched[2]
    champ_imgs = fetched[3:]

    canvas = (
        _render_splash_ascii_backdrop(W, H, splash_img, top_champ_name or "default", fallback_theme="citadel")
        if splash_img is not None
        else _render_ascii_backdrop(W, H, theme="citadel")
    )
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

    canvas = _render_ascii_backdrop(W, H, theme="crimson")
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
    champ_img, splash_img = await asyncio.gather(
        assets.get_champion_icon(champion),
        assets.get_champion_splash(champion),
    )
    item_ids = (item_ids or [])[:6]
    item_imgs = await asyncio.gather(*[assets.get_item_icon(iid) for iid in item_ids]) if item_ids else []

    tint = (76, 232, 162) if win else (246, 94, 114)
    canvas = _render_splash_ascii_backdrop(
        W, H, splash_img, champion, fallback_theme="cosmic" if win else "crimson", tint_rgb=tint
    )
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
    subtitle_extra: str = "",
) -> io.BytesIO:
    """Renders a self-contained visual Build Path & Item Order card with Data Dragon icons."""
    W, H = 1400, 800
    enemies = [e.strip() for e in (enemies or []) if e.strip()][:5]

    # Build ordered main path: (tag, item_name, short_why)
    main_steps: list[tuple[str, str, str]] = []
    if build.get("starter", {}).get("item"):
        main_steps.append(("STARTER", build["starter"]["item"], build["starter"].get("why", "")))
    if build.get("boots", {}).get("item"):
        main_steps.append(("BOOTS", build["boots"]["item"], build["boots"].get("why", "")))
    for idx, c in enumerate(build.get("core", [])[:3], 1):
        if c.get("item"):
            main_steps.append((f"CORE {idx}", c["item"], c.get("why", "")))

    situational = [s for s in build.get("situational", [])[:3] if s.get("item")]

    # Fetch all icons + champion splash concurrently
    champ_task = assets.get_champion_icon(champion)
    splash_task = assets.get_champion_splash(champion)
    enemy_tasks = [assets.get_champion_icon(e) for e in enemies]
    main_item_tasks = [assets.get_item_icon(item_ids.get(name) or 0) for _, name, _ in main_steps]
    sit_item_tasks = [assets.get_item_icon(item_ids.get(s["item"]) or 0) for s in situational]

    all_fetched = await asyncio.gather(champ_task, splash_task, *enemy_tasks, *main_item_tasks, *sit_item_tasks)
    champ_img = all_fetched[0]
    splash_img = all_fetched[1]
    p1 = 2 + len(enemies)
    p2 = p1 + len(main_steps)
    enemy_imgs = all_fetched[2:p1]
    main_item_imgs = all_fetched[p1:p2]
    sit_item_imgs = all_fetched[p2:]

    canvas = _render_splash_ascii_backdrop(W, H, splash_img, champion, fallback_theme="zaun")
    d = ImageDraw.Draw(canvas)

    def _wrap_text(text: str, max_chars: int, max_lines: int) -> list[str]:
        lines: list[str] = []
        cur = ""
        for word in (text or "").split():
            cand = f"{cur} {word}".strip()
            if len(cand) <= max_chars:
                cur = cand
            else:
                if cur:
                    lines.append(cur)
                cur = word
                if len(lines) == max_lines:
                    break
        if cur and len(lines) < max_lines:
            lines.append(cur)
        elif len(lines) == max_lines and len(" ".join(lines)) < len((text or "").strip()):
            lines[-1] = lines[-1][: max_chars - 1].rstrip(" .,;") + "…"
        return lines

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
    d.text((224, 78), f"{disp_champ} Build", font=_font(54, weight="SemiBold"), fill=(246, 250, 255, 255))

    sub_parts = []
    if subtitle_extra:
        sub_parts.append(subtitle_extra.upper())
    else:
        sub_parts.append("RECOMMENDED PATH")
    if role:
        sub_parts.append(role.upper())
    if patch:
        sub_parts.append(f"PATCH {patch}")
    _spaced_text(d, (226, 150), "  •  ".join(sub_parts), _font(16, weight="Regular", mono=True), (154, 180, 214, 235), tracking=2.0)

    # --- Top Right: Enemy Comp Portraits ---
    if enemies:
        panel_w = len(enemies) * 86 + 24
        panel_x = W - 52 - panel_w
        epanel = Image.new("RGBA", (panel_w, 134), (0, 0, 0, 0))
        epd = ImageDraw.Draw(epanel)
        epd.rounded_rectangle((0, 0, panel_w - 1, 133), radius=14, fill=(6, 12, 24, 185), outline=(92, 46, 72, 165), width=1)
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
            d.text((ex, 174), _display_champ_name(ename)[:11], font=_font(13, weight="SemiBold"), fill=(228, 236, 248, 245), anchor="mt")

    # --- Middle: Ordered Build Path Flow with Per-Item Reasons ---
    sec_y = 236
    _draw_icon(canvas, "swords", (58, sec_y + 2), 22, (236, 192, 92))
    d.text((92, sec_y), "Build Order", font=_font(21, weight="Regular", mono=True), fill=(214, 224, 240, 245))
    d.line([(248, sec_y + 13), (W - 58, sec_y + 13)], fill=(54, 78, 118, 170), width=1)

    if main_steps:
        n_steps = len(main_steps)
        box_s = 92
        total_span = W - 120
        step_dx = total_span // max(1, n_steps)
        first_cx = 60 + step_dx // 2
        item_cy = 342

        for idx, (tag, iname, why) in enumerate(main_steps):
            cx = first_cx + idx * step_dx
            tag_col = (236, 192, 92, 245) if tag.startswith("CORE") else (126, 198, 255, 240)
            d.text((cx, item_cy - 68), tag, font=_font(14, weight="Bold", mono=True), fill=tag_col, anchor="mm")

            x0, y0 = cx - box_s // 2, item_cy - box_s // 2
            border_c = (216, 174, 84, 235) if tag.startswith("CORE") else (84, 152, 228, 220)
            d.rounded_rectangle((x0, y0, x0 + box_s, y0 + box_s), radius=14, fill=(12, 22, 42, 235), outline=border_c, width=3)

            im = main_item_imgs[idx]
            if im is not None:
                resized = im.resize((box_s - 8, box_s - 8), Image.Resampling.LANCZOS)
                canvas.alpha_composite(resized, (x0 + 4, y0 + 4))
            else:
                d.text((cx, item_cy), iname[:3].upper(), font=_font(22, weight="Bold"), fill=(180, 206, 240, 240), anchor="mm")

            # Item name (up to 2 lines, never truncated)
            name_lines = _wrap_text(iname, max_chars=18, max_lines=2)
            ny = item_cy + 58
            for nl in name_lines:
                d.text((cx, ny), nl, font=_font(17, weight="SemiBold"), fill=(244, 248, 255, 255), anchor="mt")
                ny += 21

            # Concise why rationale underneath
            if why:
                for wl in _wrap_text(why, max_chars=24, max_lines=2):
                    d.text((cx, ny + 4), wl, font=_font(13, weight="Regular"), fill=(154, 180, 214, 235), anchor="mt")
                    ny += 17

            if idx < n_steps - 1:
                ax = cx + step_dx // 2
                d.text((ax, item_cy), "→", font=_font(34, weight="Bold"), fill=(112, 178, 248, 210), anchor="mm")

    # --- Bottom Left: Situational Flex Items ---
    sit_y = 516
    _draw_icon(canvas, "star", (58, sit_y + 2), 22, (206, 144, 255))
    d.text((92, sit_y), "Situational Swaps", font=_font(21, weight="Regular", mono=True), fill=(214, 224, 240, 245))
    d.line([(334, sit_y + 13), (848, sit_y + 13)], fill=(54, 78, 118, 170), width=1)

    for idx, s in enumerate(situational):
        sx = 58 + idx * 270
        sy = 562
        d.rounded_rectangle((sx, sy, sx + 64, sy + 64), radius=10, fill=(14, 24, 44, 235), outline=(146, 108, 228, 210), width=2)
        im = sit_item_imgs[idx] if idx < len(sit_item_imgs) else None
        if im is not None:
            canvas.alpha_composite(im.resize((58, 58), Image.Resampling.LANCZOS), (sx + 3, sy + 3))
        else:
            d.text((sx + 32, sy + 32), s["item"][:2].upper(), font=_font(18, weight="Bold"), fill=(196, 174, 245, 240), anchor="mm")

        iname_lines = _wrap_text(s["item"], max_chars=18, max_lines=2)
        iy = sy
        for nl in iname_lines:
            d.text((sx + 76, iy), nl, font=_font(16, weight="SemiBold"), fill=(244, 248, 255, 255))
            iy += 20
        for wl in _wrap_text(s.get("why", ""), max_chars=23, max_lines=2):
            d.text((sx + 76, iy + 2), wl, font=_font(13, weight="Regular"), fill=(154, 178, 210, 235))
            iy += 17

    # --- Bottom Right: Runes & Gameplan ---
    rx = 886
    _draw_icon(canvas, "bars", (rx, sit_y + 2), 22, (112, 198, 255))
    d.text((rx + 34, sit_y), "Runes & Gameplan", font=_font(21, weight="Regular", mono=True), fill=(214, 224, 240, 245))
    d.line([(rx + 254, sit_y + 13), (W - 58, sit_y + 13)], fill=(54, 78, 118, 170), width=1)

    runes_txt = build.get("runes") or "Adaptive standard page"
    d.text((rx, 562), runes_txt[:46], font=_font(20, weight="SemiBold"), fill=(116, 224, 255, 255))

    if allies:
        ally_str = "Allies: " + ", ".join(_display_champ_name(a) for a in allies[:4])
        d.text((rx, 594), ally_str[:52], font=_font(15, weight="Regular", mono=True), fill=(156, 180, 214, 235))

    note = (build.get("gameplan") or "").strip()
    if note:
        for li, line_txt in enumerate(_wrap_text(note, max_chars=50, max_lines=4)):
            d.text((rx, 624 + li * 22), line_txt, font=_font(15, weight="Regular"), fill=(218, 228, 244, 240))

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

    canvas = _render_ascii_backdrop(W, H, theme="zaun")
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

    win = bool(debrief.get("win"))
    canvas = _render_ascii_backdrop(W, H, theme="cosmic" if win else "crimson")
    d = ImageDraw.Draw(canvas)

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

    canvas = _render_ascii_backdrop(W, H, theme="ionia")
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

    canvas = _render_ascii_backdrop(W, H, theme="ionia")
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

    canvas = _render_ascii_backdrop(W, H, theme="ionia")
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
    top_main = items[0]["name"] if items else None
    fetched = await asyncio.gather(
        assets.get_profile_icon(mastery.get("profile_icon_id", 0)),
        assets.get_champion_splash(top_main) if top_main else asyncio.sleep(0, result=None),
        *[assets.get_champion_icon(m["name"]) for m in items],
    )
    prof_img = fetched[0]
    splash_img = fetched[1]
    champ_imgs = fetched[2:]

    canvas = _render_splash_ascii_backdrop(W, H, splash_img, top_main or "mastery", fallback_theme="shurima")
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

    canvas = _render_ascii_backdrop(W, H, theme="crimson")
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

    canvas = _render_ascii_backdrop(W, H, theme="shurima" if is_promo else "crimson")
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

    canvas = _render_ascii_backdrop(W, H, theme="shurima")
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


# -------------------------------------------------------------------------
# 15. League Patch Notes Summary Card (/patch & Auto Patch Alerts)
# -------------------------------------------------------------------------

async def render_patch_card(
    summary: dict,
    assets: AssetCache,
    publish_date: str = "",
) -> io.BytesIO:
    """Renders a sleek ASCII-backdrop Patch Notes card with Buffs, Nerfs, Items/Systems & Squad Impact."""
    W, H = 1400, 900
    patch_ver = str(summary.get("patch") or "NEW")
    headline = (summary.get("headline") or f"Patch {patch_ver} Gameplay Highlights").strip()
    buffs = [b for b in (summary.get("buffs") or []) if b.get("champion")][:6]
    nerfs = [n for n in (summary.get("nerfs") or []) if n.get("champion")][:6]
    adjusted = [a for a in (summary.get("adjusted") or []) if a.get("champion")][:3]
    sys_items = [s for s in (summary.get("items_and_systems") or []) if s.get("name")][:4]
    squad_alerts = [str(x).strip() for x in (summary.get("squad_alerts") or []) if str(x).strip()][:3]

    champ_names = [b["champion"] for b in buffs] + [n["champion"] for n in nerfs] + [a["champion"] for a in adjusted]
    unique_champs = list(dict.fromkeys(champ_names))
    fetched = await asyncio.gather(*[assets.get_champion_icon(c) for c in unique_champs]) if unique_champs else []
    c_img_map = dict(zip(unique_champs, fetched))

    canvas = _render_ascii_backdrop(W, H, theme="zaun")
    d = ImageDraw.Draw(canvas)

    def _wrap(text: str, max_chars: int, max_lines: int) -> list[str]:
        lines: list[str] = []
        cur = ""
        for w in (text or "").split():
            cand = f"{cur} {w}".strip()
            if len(cand) <= max_chars:
                cur = cand
            else:
                if cur:
                    lines.append(cur)
                cur = w
                if len(lines) == max_lines:
                    break
        if cur and len(lines) < max_lines:
            lines.append(cur)
        elif len(lines) == max_lines and len(" ".join(lines)) < len((text or "").strip()):
            lines[-1] = lines[-1][: max_chars - 1].rstrip(" .,;") + "…"
        return lines

    # --- Header ---
    _spaced_text(d, (58, 38), "LEAGUE OF LEGENDS  •  PATCH UPDATE", _font(15, weight="Bold", mono=True), (242, 200, 96, 245), tracking=3.2)
    d.text((58, 64), f"Patch {patch_ver} Notes", font=_font(52, weight="Bold"), fill=(248, 250, 255, 255))
    sub_line = headline
    if publish_date:
        sub_line = f"{publish_date}  •  {headline}"
    d.text((60, 128), sub_line[:88], font=_font(19, weight="Regular"), fill=(176, 198, 228, 240))

    # --- Top Columns: BUFFS (left) & NERFS (right) ---
    col_w, col_h = 626, 446
    top_y = 176

    def _draw_change_column(x0: int, header_txt: str, entries: list[dict], accent: tuple, ring2: tuple):
        panel = Image.new("RGBA", (col_w, col_h), (0, 0, 0, 0))
        pd = ImageDraw.Draw(panel)
        pd.rounded_rectangle((0, 0, col_w - 1, col_h - 1), radius=16, fill=(10, 18, 34, 214), outline=(*accent, 155), width=2)
        canvas.alpha_composite(panel, (x0, top_y))

        d.rounded_rectangle((x0 + 22, top_y + 18, x0 + 32, top_y + 38), radius=3, fill=(*accent, 255))
        _spaced_text(d, (x0 + 44, top_y + 18), header_txt, _font(17, weight="Bold", mono=True), (*accent, 255), tracking=2.6)
        d.line([(x0 + 22, top_y + 48), (x0 + col_w - 22, top_y + 48)], fill=(52, 74, 110, 160), width=1)

        if not entries:
            d.text((x0 + col_w // 2, top_y + col_h // 2), "None highlighted this patch", font=_font(18), fill=(138, 160, 192, 220), anchor="mm")
            return

        row_h = 62
        for idx, item in enumerate(entries[:6]):
            ry = top_y + 60 + idx * row_h
            if idx > 0:
                d.line([(x0 + 22, ry - 4), (x0 + col_w - 22, ry - 4)], fill=(32, 48, 78, 120), width=1)

            cname = item["champion"]
            _paste_circular_portrait(canvas, c_img_map.get(cname), (x0 + 50, ry + 26), 46, accent, ring2, 2, cname)
            d.text((x0 + 86, ry + 4), _display_champ_name(cname), font=_font(20, weight="SemiBold"), fill=(246, 250, 255, 255))
            d.text((x0 + 86, ry + 30), (item.get("summary") or "")[:58], font=_font(15, weight="Regular"), fill=(176, 198, 226, 238))

    _draw_change_column(58, "CHAMPION BUFFS", buffs, (84, 236, 148), (56, 188, 224))
    _draw_change_column(716, "CHAMPION NERFS", nerfs, (255, 108, 118), (192, 66, 126))

    # --- Bottom Left: Items, Systems & Adjusted ---
    bot_y = 644
    bot_h = 196
    l_panel = Image.new("RGBA", (col_w, bot_h), (0, 0, 0, 0))
    lpd = ImageDraw.Draw(l_panel)
    lpd.rounded_rectangle((0, 0, col_w - 1, bot_h - 1), radius=16, fill=(10, 18, 34, 214), outline=(236, 192, 92, 145), width=2)
    canvas.alpha_composite(l_panel, (58, bot_y))

    _spaced_text(d, (82, bot_y + 16), "ITEMS, SYSTEMS & ADJUSTED", _font(15, weight="Bold", mono=True), (242, 200, 96, 250), tracking=2.4)
    d.line([(80, bot_y + 42), (58 + col_w - 22, bot_y + 42)], fill=(52, 74, 110, 155), width=1)

    combined_sys = []
    for s in sys_items:
        combined_sys.append((s["name"], (s.get("tag") or "SYS").upper()[:4], s.get("summary", "")))
    for a in adjusted:
        combined_sys.append((a["champion"], "ADJ", a.get("summary", "")))

    tag_colors = {
        "BUFF": ((20, 92, 58, 225), (92, 242, 160, 255)),
        "NERF": ((118, 28, 42, 225), (255, 134, 142, 255)),
        "ADJ": ((74, 58, 20, 225), (244, 204, 96, 255)),
        "NEW": ((24, 68, 118, 225), (112, 218, 255, 255)),
    }
    for idx, (name, tag, desc) in enumerate(combined_sys[:4]):
        ry = bot_y + 54 + idx * 34
        bg, fg = tag_colors.get(tag, ((46, 58, 86, 225), (196, 212, 238, 255)))
        d.rounded_rectangle((80, ry, 134, ry + 24), radius=6, fill=bg)
        d.text((107, ry + 12), tag, font=_font(12, weight="Bold", mono=True), fill=fg, anchor="mm")
        d.text((146, ry + 2), f"{name}: ", font=_font(16, weight="SemiBold"), fill=(242, 246, 255, 255))
        nw = int(d.textlength(f"{name}: ", font=_font(16, weight="SemiBold")))
        max_desc = max(14, int((col_w - 110 - nw) / 8.2))
        d.text((146 + nw, ry + 3), desc[:max_desc], font=_font(15, weight="Regular"), fill=(176, 198, 226, 235))

    # --- Bottom Right: Squad Impact ---
    r_panel = Image.new("RGBA", (col_w, bot_h), (0, 0, 0, 0))
    rpd = ImageDraw.Draw(r_panel)
    rpd.rounded_rectangle((0, 0, col_w - 1, bot_h - 1), radius=16, fill=(10, 18, 34, 214), outline=(186, 126, 255, 155), width=2)
    canvas.alpha_composite(r_panel, (716, bot_y))

    _spaced_text(d, (740, bot_y + 16), "SQUAD IMPACT & TAKEAWAYS", _font(15, weight="Bold", mono=True), (210, 156, 255, 250), tracking=2.4)
    d.line([(738, bot_y + 42), (716 + col_w - 22, bot_y + 42)], fill=(52, 74, 110, 155), width=1)

    if squad_alerts:
        ay = bot_y + 54
        for alert in squad_alerts[:3]:
            for li, line_txt in enumerate(_wrap(alert, max_chars=58, max_lines=2)):
                prefix = "• " if li == 0 else "  "
                d.text((740, ay), f"{prefix}{line_txt}", font=_font(16, weight="Medium" if li == 0 else "Regular"), fill=(224, 232, 248, 245))
                ay += 21
            ay += 6
    else:
        d.text((740, bot_y + 72), "• Check the buffs & nerfs above for your mains!", font=_font(16), fill=(176, 198, 226, 235))

    _draw_footer(canvas, f"PATCH {patch_ver} BRIEFING  •  JEFF BOT")
    return _to_png_bytes(canvas)
