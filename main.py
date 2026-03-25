"""
🕵️ Bot del Impostor para Telegram
Juego donde todos reciben la misma palabra excepto el impostor.
Soporte de idiomas: Español / English
"""

import asyncio
import logging
import random
import sqlite3
import anthropic
import io
import os
import urllib.request
from PIL import Image, ImageDraw, ImageFont
from datetime import datetime

# ── Fuentes con cobertura unicode completa ──────────────────────

def _get_font_dir():
    """Retorna directorio escribible para fuentes: /data/fonts o /tmp/fonts."""
    for d in ["/data/fonts", "/tmp/fonts"]:
        try:
            os.makedirs(d, exist_ok=True)
            # Verificar que es escribible
            test = os.path.join(d, ".write_test")
            open(test, "w").close()
            os.remove(test)
            return d
        except Exception:
            continue
    return "/tmp"

_FONT_DIR     = _get_font_dir()
_FONT_REGULAR = f"{_FONT_DIR}/NotoSans-Regular.ttf"
_FONT_BOLD    = f"{_FONT_DIR}/NotoSans-Bold.ttf"
_FONT_UNIFONT    = f"{_FONT_DIR}/unifont.otf"
_FONT_FREESERIF_DL = f"{_FONT_DIR}/FreeSerif.ttf"
_FONT_FREESANS_DL  = f"{_FONT_DIR}/FreeSans.ttf"
_FONT_CJK_DL       = f"{_FONT_DIR}/NotoSansCJK-Regular.otf"

# Fuentes del sistema (Ubuntu/Debian — disponibles en Render si se instalan)
_FONT_UNIFONT_SYS  = "/usr/share/fonts/opentype/unifont/unifont.otf"
_FONT_FREESERIF    = "/usr/share/fonts/truetype/freefont/FreeSerif.ttf"
_FONT_FREESANS     = "/usr/share/fonts/truetype/freefont/FreeSans.ttf"
_FONT_FREESANSBOLD = "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"
_FONT_DEJAVUSANS   = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_FONT_DEJAVUBOLD   = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_FONT_CJK_REGULAR  = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
_FONT_CJK_BOLD     = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"

# Prioridad de fuentes para nombres de usuario
# Unifont cubre BMP (runas, syllabics, coreano, etc.)
# FreeSerif cubre SMP matemático (U+1D400-U+1D7FF)
# Ambas son necesarias para cobertura completa
# IMPORTANTE: fuentes vectoriales (DejaVu, NotoSans) van PRIMERO.
# Unifont es bitmap y solo funciona bien a 16px; va al final como
# ultimo recurso para caracteres exoticos no cubiertos por vectoriales.
_RENDER_FONT_PRIORITY = [
    _FONT_DEJAVUSANS,      # vectorial: Latin/Griego/Cirilico/Arabe basico
    _FONT_REGULAR,         # NotoSans vectorial
    _FONT_FREESANS,        # sistema (si disponible)
    _FONT_FREESANS_DL,     # descargado
    _FONT_CJK_REGULAR,     # CJK/coreano
    _FONT_FREESERIF,       # SMP math (sistema)
    _FONT_FREESERIF_DL,    # SMP math (descargado)
    _FONT_CJK_DL,          # NotoSansCJK descargado - Korean/JP/ZH
    _FONT_UNIFONT_SYS,     # Unifont sistema - exoticos BMP
    _FONT_UNIFONT,         # Unifont descargado - ultimo recurso
]
_RENDER_FONT_PRIORITY_BOLD = [
    _FONT_DEJAVUBOLD,
    _FONT_BOLD,
    _FONT_FREESANSBOLD,
    _FONT_CJK_BOLD,
    _FONT_UNIFONT_SYS,
    _FONT_UNIFONT,
]

# Cache: (path, size) → ImageFont
_font_cache: dict = {}
# Cache: codepoint → path con el glifo
_codepoint_to_path: dict = {}
# Cache de cmaps por path: path → set de codepoints
_font_cmaps: dict = {}

def _get_font_cmap(path: str) -> set:
    """Retorna el conjunto de codepoints soportados por la fuente (via fonttools)."""
    if path in _font_cmaps:
        return _font_cmaps[path]
    cmap = set()
    try:
        from fontTools.ttLib import TTFont as _TTFont
        tt = _TTFont(path, fontNumber=0)
        best = tt.getBestCmap()
        if best:
            cmap = set(best.keys())
        tt.close()
    except Exception:
        pass
    _font_cmaps[path] = cmap
    return cmap

def _path_has_glyph(path: str, char: str) -> bool:
    """Verifica via cmap si la fuente tiene el glifo para el carácter."""
    cmap = _get_font_cmap(path)
    if cmap:
        return ord(char) in cmap
    # Fallback si fonttools no está disponible: comparación pixel
    f = _load(path, 22)
    if not f:
        return False
    try:
        _NOTDEF_REF = "\uE000"
        img_c = Image.new("L", (30, 30), 0)
        img_r = Image.new("L", (30, 30), 0)
        ImageDraw.Draw(img_c).text((2, 2), char, font=f, fill=255)
        ImageDraw.Draw(img_r).text((2, 2), _NOTDEF_REF, font=f, fill=255)
        return img_c.tobytes() != img_r.tobytes()
    except Exception:
        return False

def _load(path: str, size: int):
    """Carga fuente con cache. Retorna None si falla."""
    key = (path, size)
    if key in _font_cache:
        return _font_cache[key]
    result = None
    if path and os.path.exists(path) and os.path.getsize(path) > 10_000:
        try:
            result = ImageFont.truetype(path, size)
        except Exception:
            pass
    _font_cache[key] = result
    return result

def _best_font_for_char(char: str, size: int):
    """Encuentra la mejor fuente disponible para un carácter específico."""
    cp = ord(char)
    # Cache por codepoint (el path es independiente del size)
    if cp not in _codepoint_to_path:
        best_path = None
        for path in _RENDER_FONT_PRIORITY:
            if not (path and os.path.exists(path) and os.path.getsize(path) > 10_000):
                continue
            if _path_has_glyph(path, char):
                best_path = path
                break
        _codepoint_to_path[cp] = best_path

    path = _codepoint_to_path[cp]
    if path:
        f = _load(path, size)
        if f:
            return f
    # Fallback: primera fuente cargable
    for p in _RENDER_FONT_PRIORITY:
        f = _load(p, size)
        if f:
            return f
    return ImageFont.load_default()

def _get_font(size: int, bold: bool = False):
    """Fuente principal para headers/labels."""
    priority = _RENDER_FONT_PRIORITY_BOLD if bold else _RENDER_FONT_PRIORITY
    for path in priority:
        f = _load(path, size)
        if f:
            return f
    return ImageFont.load_default()

# Fuentes Unifont (bitmap, solo 16px nativo)
_UNIFONT_PATHS: list = []      # se llena en _ensure_cmaps
_BAD_FONTS: set = set()        # fuentes que no renderizan (CFF en FreeType viejo)

def _font_renders_ok(path: str, size: int = 22) -> bool:
    """Verifica que la fuente produce píxeles reales (detecta CFF problemáticos)."""
    try:
        cmap = _font_cmaps.get(path, set())
        if not cmap:
            return False
        # Tomar un codepoint de muestra: preferir Latin (A=65), sino el primero
        cp = 65 if 65 in cmap else next(iter(sorted(cmap - {32})), None)
        if cp is None:
            return False
        f = ImageFont.truetype(path, size)
        from PIL import Image as _Im, ImageDraw as _IDr
        img = _Im.new("L", (size * 4, size * 3), 0)
        _IDr.Draw(img).text((4, 4), chr(cp), font=f, fill=255)
        return any(p > 0 for p in img.tobytes())
    except Exception:
        return False

def _ensure_cmaps():
    """Pre-carga y valida los cmaps de todas las fuentes disponibles."""
    global _UNIFONT_PATHS, _BAD_FONTS
    _log = logging.getLogger(__name__)
    for p in _RENDER_FONT_PRIORITY:
        if p and os.path.exists(p) and os.path.getsize(p) > 10_000:
            _get_font_cmap(p)  # pobla _font_cmaps
            # Validar que la fuente realmente renderiza (detectar CFF vacío)
            if not _font_renders_ok(p):
                _BAD_FONTS.add(p)
                _log.warning(f"[FONTS] ⚠️ {os.path.basename(p)} no renderiza (CFF/FreeType incompatible) — excluida")
    # Paths de Unifont válidos para fallback a 16px
    _UNIFONT_PATHS = [p for p in [_FONT_UNIFONT, _FONT_UNIFONT_SYS]
                      if p and os.path.exists(p) and os.path.getsize(p) > 10_000
                      and p not in _BAD_FONTS]

_cmaps_ready = False    # flag para ensure solo una vez por arranque

def draw_text_smart(draw, pos, text: str, size: int, fill):
    """Dibuja texto char a char eligiendo la mejor fuente disponible.

    Estrategia:
    1. Recorre _RENDER_FONT_PRIORITY buscando la primera fuente que tenga el glifo.
    2. Unifont (bitmap) siempre se renderiza a 16px nativo y se centra verticalmente.
    3. Si ninguna fuente tiene el glifo, se avanza el cursor sin dibujar.
    """
    global _cmaps_ready
    if not _cmaps_ready:
        _ensure_cmaps()
        _cmaps_ready = True

    UNIFONT_NATIVE = 16  # Unifont es bitmap OTF, solo legible a su tamaño nativo
    x, y = pos

    # Separar paths de Unifont del resto para tratarlos diferente
    unifont_set = {_FONT_UNIFONT, _FONT_UNIFONT_SYS}
    vectorial_paths = [p for p in _RENDER_FONT_PRIORITY
                       if p and p not in unifont_set and p not in _BAD_FONTS
                       and os.path.exists(p) and os.path.getsize(p) > 10_000]

    for char in text:
        if char == " ":
            x += size // 3
            continue
        cp = ord(char)
        drawn = False

        # 1. Intentar fuentes vectoriales primero (calidad completa al tamaño pedido)
        for path in vectorial_paths:
            cmap = _font_cmaps.get(path, set())
            if cp in cmap:
                f = _load(path, size)
                if f:
                    draw.text((x, y), char, font=f, fill=fill)
                    bb = draw.textbbox((0, 0), char, font=f)
                    x += max(bb[2] - bb[0], 4)
                    drawn = True
                    break


        if drawn:
            continue

        # 2. Fallback: Unifont a 16px nativo
        for uni_path in _UNIFONT_PATHS:
            cmap = _font_cmaps.get(uni_path, set())
            if cp in cmap:
                f = _load(uni_path, UNIFONT_NATIVE)
                if f:
                    y_adj = y + (size - UNIFONT_NATIVE) // 2
                    draw.text((x, y_adj), char, font=f, fill=fill)
                    bb = draw.textbbox((0, 0), char, font=f)
                    char_w = max(bb[2] - bb[0], 4)
                    x += int(char_w * size / UNIFONT_NATIVE * 0.75)
                    drawn = True
                    break

        if not drawn:
            # Glifo no disponible en ninguna fuente → avanzar cursor
            x += size // 2

    return x

def _init_fonts():
    """Descarga fuentes necesarias y loguea el estado del sistema de fuentes."""
    _log = logging.getLogger(__name__)
    _log.info(f"[FONTS] Directorio de fuentes: {_FONT_DIR}")

    # Descargar fuentes si no existen en sistema ni en cache
    # Unifont  → BMP completo (runas, syllabics, coreano, etc.)
    # FreeSerif → SMP math alphanumeric (𝓩 𝙄 etc.)
    # Fuentes descargadas al /data/fonts para cobertura unicode completa.
    # CJK (Korean/JP/ZH) necesita NotoSansCJK — vectorial, ~12MB, se persiste en /data/fonts.
    DOWNLOAD_LIST = [
        (_FONT_UNIFONT, [
            "https://unifoundry.com/pub/unifont/unifont-15.1.05/font-builds/unifont-15.1.05.otf",
            "https://github.com/nicowillis/fonts/raw/master/Unifont.ttf",
        ]),
        (_FONT_REGULAR, [
            "https://cdn.jsdelivr.net/gh/googlefonts/noto-fonts@main/hinted/ttf/NotoSans/NotoSans-Regular.ttf",
            "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSans/NotoSans-Regular.ttf",
        ]),
        (_FONT_BOLD, [
            "https://cdn.jsdelivr.net/gh/googlefonts/noto-fonts@main/hinted/ttf/NotoSans/NotoSans-Bold.ttf",
            "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSans/NotoSans-Bold.ttf",
        ]),
        # NotoSansCJK: cubre Korean, Japanese, Chinese con calidad vectorial
        # Intentamos primero Korean-only (~6MB), luego el subset SC (~12MB) como fallback
        (_FONT_CJK_DL, [
            "https://cdn.jsdelivr.net/gh/googlefonts/noto-cjk@main/Sans/SubsetOTF/KR/NotoSansKR-Regular.otf",
            "https://cdn.jsdelivr.net/gh/googlefonts/noto-cjk@main/Sans/SubsetOTF/SC/NotoSansSC-Regular.otf",
            "https://github.com/googlefonts/noto-cjk/raw/main/Sans/SubsetOTF/KR/NotoSansKR-Regular.otf",
        ]),
    ]
    for dest, urls in DOWNLOAD_LIST:
        if os.path.exists(dest) and os.path.getsize(dest) > 50_000:
            _log.info(f"[FONTS] OK (cache): {dest} ({os.path.getsize(dest)//1024}KB)")
            continue
        for url in urls:
            try:
                _log.info(f"[FONTS] Descargando: {url}")
                urllib.request.urlretrieve(url, dest)
                size = os.path.getsize(dest)
                if size > 50_000:
                    _log.info(f"[FONTS] ✅ Descargado: {dest} ({size//1024}KB)")
                    break
                os.remove(dest)
            except Exception as e:
                _log.warning(f"[FONTS] Falló {url}: {e}")
        else:
            _log.warning(f"[FONTS] No se pudo descargar: {os.path.basename(dest)}")

    # Loguear estado final de todas las fuentes
    all_paths = _RENDER_FONT_PRIORITY + _RENDER_FONT_PRIORITY_BOLD
    for p in dict.fromkeys(all_paths):  # deduplicar
        exists = p and os.path.exists(p)
        size_kb = os.path.getsize(p)//1024 if exists else 0
        _log.info(f"[FONTS] {'✅' if exists else '❌'} {os.path.basename(p) if p else '?'} ({size_kb}KB)")

    # Pre-calentar cmap de las fuentes disponibles
    for p in _RENDER_FONT_PRIORITY:
        if p and os.path.exists(p) and os.path.getsize(p) > 10_000:
            cmap = _get_font_cmap(p)
            _log.info(f"[FONTS] cmap {os.path.basename(p)}: {len(cmap)} codepoints")

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Conflict
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

TOKEN = os.environ.get("BOT_TOKEN")
MAX_JUGADORES = 8

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# TEXTOS EN AMBOS IDIOMAS
# ══════════════════════════════════════════════════════════════

TEXTOS = {
    "es": {
        # cmd_start
        "start": (
            "🕵️ *¡Bienvenido al Bot del Impostor\\!*\n\n"
            "El juego es simple:\n"
            "• Todos reciben la *misma palabra secreta*\n"
            "• Excepto el/los *impostores*, que no la saben\n"
            "• Den pistas sin decirla directamente 🎭\n"
            "• El grupo vota para eliminar jugadores por rondas\n\n"
            "*Comandos:*\n"
            "`/playimpostor` — Crear una partida\n"
            "`/join` — Unirse a la partida\n"
            "`/vote` — Abrir votación \\(solo el creador\\)\n"
            "`/howtoplay` — Cómo se juega\n"
            "`/score` — Ver marcador\n"
            "`/resetimpostor` — Resetear puntajes\n"
            "`/language` — Cambiar idioma\n"
            "`/cancel` — Cancelar partida"
        ),
        # cmd_como_jugar
        "comojugar": (
            "🕵️ *¿Cómo se juega El Impostor?*\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "*📋 Objetivo*\n"
            "El grupo debe eliminar a todos los impostores\\. "
            "Los impostores deben pasar desapercibidos o adivinar la palabra secreta\\.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "*🎮 Pasos del juego*\n\n"
            "*0\\. Iniciar el bot en privado*\n"
            "Debes ingresar a @impostortg\\_bot y apretar el botón de la parte inferior que dice `Iniciar`, luego en comandos usa `/howtoplay` para aprender sobre el bot\\.\n\n"
            "*1\\. Crear la partida*\n"
            "Alguien usa `/playimpostor` y los demás se unen con `/join` o el botón\\.\n\n"
            "*2\\. Iniciar*\n"
            "Con mínimo 3 jugadores, el creador elige una categoría y pulsa *¡Iniciar partida\\!*\n\n"
            "*3\\. Palabras secretas*\n"
            "El bot envía un mensaje privado a cada jugador:\n"
            "• Los jugadores normales reciben la *palabra secreta*\n"
            "• El/los impostor\\(es\\) NO reciben la palabra, solo la categoría 🎭\n\n"
            "*4\\. Dar pistas*\n"
            "Siguiendo el orden aleatorio, cada jugador da *una pista* sobre la palabra\\. "
            "El impostor debe inventar una pista convincente sin saber la palabra\\.\n\n"
            "*5\\. Votar*\n"
            "Cuando todos hayan dado su pista, el creador abre la votación\\. "
            "Solo los jugadores *vivos* votan\\. El más votado queda eliminado\\.\n\n"
            "*6\\. Revelación y nueva ronda*\n"
            "Se revela si el eliminado era impostor o inocente\\. "
            "Si quedan jugadores, se muestra un nuevo orden y continúa el juego\\.\n\n"
            "*7\\. Último intento del impostor*\n"
            "Si el grupo vota a un impostor, este tiene *una última oportunidad*: "
            "adivinar la palabra escribiéndola en el chat\\. "
            "Si la adivina, *todos los impostores ganan*\\.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "*🏆 ¿Quién gana?*\n\n"
            "🎉 *Grupo gana* si:\n"
            "  • Eliminan a todos los impostores\n\n"
            "🕵️ *Impostor\\(es\\) gana\\(n\\)* si:\n"
            "  • Solo queda 1 inocente junto a un impostor\n"
            "  • Un impostor adivina la palabra al ser votado\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "*👥 Impostores según jugadores*\n"
            "  • 3\\-4 jugadores → 1 impostor\n"
            "  • 5\\-6 jugadores → 1 a 3 impostores \\(al azar\\)\n"
            "  • 7\\+ jugadores → 2 a 3 impostores \\(al azar\\)\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "*📌 Comandos*\n"
            "`/playimpostor` — Crear partida\n"
            "`/join` — Unirse a la partida\n"
            "`/vote` — Abrir votación \\(creador\\)\n"
            "`/score` — Ver marcador\n"
            "`/resetimpostor` — Resetear puntajes\n"
            "`/language` — Cambiar idioma\n"
            "`/cancel` — Cancelar partida"
        ),
        "partida_activa":           "⚠️ Ya hay una partida activa\\. Usa /cancel para cancelarla primero\\.",
        "bot_no_iniciado":          "⚠️ Debes iniciar el bot primero para recibir tu palabra secreta.\n\nAbre el chat privado con el bot, presiona INICIAR y luego vuelve aquí para unirte.",
        "btn_unirse":               "✋ Unirse a la partida",
        "nueva_partida":            "🎮 *{nombre} creó una nueva partida del juego Impostor\\!*\n\nPulsen el botón o usen /join para sumarse\\.\nCuando estén listos, el creador pulsa *¡Iniciar partida\\!*",
        "sin_partida":              "⚠️ No hay ninguna partida abierta. Usa /playimpostor para crear una.",
        "partida_en_curso":         "⚠️ La partida ya está en curso, no puedes unirte ahora.",
        "ya_en_partida":            "⚠️ Ya estás en la partida.",
        "partida_llena":            "⚠️ La partida está llena \\(máximo {n} jugadores\\)\\.",
        "unido":                    "✅ *{nombre} se unió\\!*\n\n*Jugadores* \\({n}\\):\n{lista}\n\n",
        "puede_iniciar":            "_El creador puede iniciar cuando quiera\\._",
        "faltan_jugadores":         "Faltan *{n}* jugadores más para poder iniciar\\.",
        "btn_iniciar":              "🚀 ¡Iniciar partida!",
        "no_partida_espera":        "No hay partida en espera.",
        "solo_creador_iniciar":     "⚠️ Solo el creador puede iniciar la partida.",
        "pocos_jugadores":          "⚠️ Necesitas al menos 3 jugadores. Ahora hay {n}.",
        "elige_categoria":          "🗂️ *Elige una categoría:*",
        "btn_random":               "🎲 ¡Sorpréndeme! (Random)",
        "config_impostores":        "⚙️ *Configurar impostores*\n\nJugadores: *{n}*\nImpostores: *{imp}*\n\n_El creador puede elegir cuántos impostores habrá\\._",
        "btn_imp_menos":            "➖",
        "btn_imp_mas":              "➕",
        "btn_imp_confirmar":        "✅ Confirmar y elegir categoría",
        "btn_imp_random":           "🎲 Random (1-3)",
        "config_impostores_random":  "⚙️ *Configurar impostores*\n\nJugadores: *{n}*\nImpostores: *🎲 Random \\(1\\-3\\)*\n\n_El creador puede elegir cuántos impostores habrá\\._",

        "solo_creador_categoria":   "Solo el creador puede elegir la categoría.",
        "cat_sorpresa_grupo":       "🎲 *¡Categoría sorpresa\\!*",
        "cat_confirmacion":         "✅ Categoría: *{cat}*",
        "cat_grupo":                "Categoría: *{cat}*",
        "enviando_privado":         "📩 Enviando palabras en privado\\.\\.\\.",
        "eres_impostor":            "🕵️ *¡Eres el IMPOSTOR\\!*\n\nCategoría: *{cat}*\n\nNo conoces la palabra\\. Intenta descubrirla por las pistas de los demás\\. ¡No te atrapen\\! 🎭",
        "eres_inocente":            "🔑 Tu palabra secreta es:\n\n✨ *{palabra}* ✨\n\nCategoría: *{cat}*\n\n💡 *Cómo puedes describirla:*\n{pistas}\n\n_Da pistas sin decir la palabra directamente\\. ¡Encuentra al impostor\\!_ 🕵️",
        "aviso_fallidos":           "\n\n⚠️ No pude enviar mensaje a: {nombres}\n_Deben iniciar conversación con el bot primero_",
        "aviso_2rondas":            "Esta partida se juega en *2 rondas* de pistas antes de votar\\. ¡Atención\\! 👀",
        "aviso_votar":              "Cuando todos hayan dado su pista, el creador abre la votación 🗳️",
        "partida_comienza":         "🎮 *¡La partida comienza\\!*\n\n{cat}\n\n*🎲 Orden de pistas \\(elegido al azar\\):*\n{orden}\n\nCada uno da *una pista* sobre la palabra sin decirla directamente\\.\n{aviso_rondas}",
        "turno":                    "👆 *¡Es el turno de* [{nombre}](tg://user?id={uid})\\!\nEscribe tu pista en el chat\\.\n⏱️ _Tienes 1 minuto\\._",
        "turno_timeout":            "⏰ *¡Tiempo\\!* [{nombre}](tg://user?id={uid}) no dio pista a tiempo\\. Se salta su turno\\.",
        "turno_timeout_autoconf":   "⏰ *¡Tiempo\\!* Se confirmó automáticamente la última pista de [{nombre}](tg://user?id={uid})\\.",
        "quien_es_impostor":        "🗳️ *¿Quién es el impostor\\?*\n\n_Jugadores vivos \\({n}\\) — solo ellos votan:_",
        "no_partida_curso":         "⚠️ No hay partida en curso.",
        "solo_creador_votar":       "⚠️ Solo el creador puede abrir la votación.",
        "no_partida_votacion":      "La votación ya cerró.",
        "no_puedes_votar":          "No puedes votar: estás eliminado o no eres parte de esta partida.",
        "voto_ya":                  "Ya votaste.",
        "voto_ok":                  "✅ ¡Voto registrado!",
        "voto_confirmado":          "✅ *{nombre}* votó\\. {faltantes}",
        "voto_confirmado_revoto":   "✅ *{nombre}* votó en la revotación\\. {faltantes}",
        "faltan_votos":             "Faltan *{n}* votos\\.",
        "no_revotacion":            "No hay revotación activa.",
        "voto_invalido":            "Voto inválido.",
        "segundo_empate":           "⚖️ *¡Segundo empate\\!*\n\nNadie es eliminado en esta ronda\\. ¡El juego continúa\\!\n\nEl creador abre la votación cuando estén listos\\.",
        "btn_abrir_votacion":       "🗳️ ¡Abrir votación!",
        "votacion_auto":            "⏰ *¡Votación abierta automáticamente\\!*\n\n_Jugadores vivos \\({n}\\) — votan en 1 minuto:_",
        "votos_insuficientes":      "⚠️ *Nadie alcanzó 2 votos*\n\nSe reabre la votación\\.",
        "votos_timeout":            "⏰ *Tiempo de votación agotado*\n\n",
        "empate":                   "⚖️ *¡Empate\\!*\n\n{nombres} tienen *{n} votos* cada uno\\.\n\n🔁 *Revotación* — Solo entre los empatados:\n_Jugadores vivos, voten de nuevo:_",
        "nuevo_creador":            "👑 *{nombre}* es el nuevo creador y puede abrir la votación\\.",
        "resultado_votacion":       "🗳️ *Resultado de la votación:*\n\nEl grupo votó por *{nombre}*\n{etiqueta}\n\n*Votos:*\n{detalle}",
        "era_impostor":             "🕵️ ¡Era impostor\\!",
        "era_inocente":             "✅ Era inocente\\.",
        "ultima_oportunidad":       "🎯 *¡Última oportunidad, {nombre}\\!*\n\nSi adivinas la palabra secreta *¡tú y todos los impostores ganarán\\!*\n\n📝 Escribe la palabra ahora en el chat\\.\n_Categoría: {cat}_\n⏱️ _Tienes 30 segundos\\._",
        "adiv_timeout":             "⏰ *¡Tiempo\\!* [{nombre}](tg://user?id={uid}) no adivinó a tiempo\\. ¡El grupo gana\\!",
        "adivino":                  "🎯 *¡{nombre} adivinó la palabra\\!*\n\nLa palabra era *{palabra}*\\. ¡Los impostores ganan\\! 🕵️",
        "incorrecto":               "❌ *{nombre}* escribió *{texto}*\\.\\.\\. ¡Incorrecto\\!\n\n*{nombre}* queda eliminado definitivamente\\.",
        "confirmar_pista_btn":      "✅ Confirmar esta como mi pista",
        "confirmar_adivinanza_btn": "✅ Confirmar esta como mi respuesta",
        "confirmar_adivinanza_msg": "¿Confirmas *{palabra}* como tu respuesta\\?",

        "confirmar_pista_msg":      "¿Confirmas *{pista}* como tu pista\\?",
        "no_tu_turno":              "⚠️ No es tu turno.",
        "pista_confirmada":         "✅ ¡Pista confirmada!",
        "pista_auto_confirmada":    "⚠️ Tercer intento — pista enviada automáticamente\\.",
        "aviso_30s":                "⏱️ ¡Quedan *30 segundos* para tu pista\\! [{nombre}](tg://user?id={uid})",
        "aviso_15s":                "⏱️ ¡Quedan *15 segundos* para adivinar\\! [{nombre}](tg://user?id={uid})",
        "segunda_ronda":            "🔄 *¡Segunda ronda de pistas\\!*\n\nAhora sí, después de esta ronda se abrirá la votación\\.\n\n*🎲 Nuevo orden:*\n{orden}",
        "todos_dieron_pista":       "✅ *¡Todos dieron su pista\\!*\n\nEl creador puede abrir la votación 🗳️",
        "nueva_ronda_pistas":       "🔄 *¡Nueva ronda de pistas\\!*\n\n👥 Jugadores vivos: *{n}*\n\n*🎲 Nuevo orden de pistas:*\n{orden}\n\nCada uno da *una pista* sobre la palabra\\.\nCuando terminen, el creador abre la votación 🗳️",
        "grupo_gana":               "🎉 *¡El grupo ganó\\!*\n\nLos impostores eran: {impostores}\n¡Fueron eliminados sin adivinar la palabra\\!\n\n🔑 La palabra era: *{palabra}* \\({cat}\\)\n\n_Usa /playimpostor para otra ronda_",
        "impostores_ganan":         "🕵️ *¡Los impostores ganaron\\!*\n\nEran: {impostores}\n{desc}\n\n🔑 La palabra era: *{palabra}* \\({cat}\\)\n\n_Usa /playimpostor para otra ronda_",
        "desc_supervivencia":       "Los impostores sobrevivieron hasta quedar solos con un inocente\\.",
        "desc_adivino":             "Un impostor adivinó la palabra correcta\\.",
        "desc_error_voto":          "Votaron incorrectamente por *{nombre}*\\.",
        "sin_estadisticas":         "📊 No hay estadísticas aún\\. ¡Juega primero\\!",
        "marcador":                 "🏆 *Marcador del grupo:*\n\n{tabla}",
        "col_jugador":              "Jugador",
        "solo_admin_reset":         "⚠️ Solo los administradores del grupo pueden resetear los puntajes.",
        "reset_ok":                 "🔄 *Puntajes reseteados\\.*\n\nTodas las victorias y derrotas vuelven a cero\\. ¡A empezar de nuevo\\! 🎮",
        "resetroles_ok":            "🔄 *Roles reseteados\\.*\n\nTodos los contadores de impostor e inocente vuelven a cero\\. 🎭",
        "sin_partida_activa":       "⚠️ No hay ninguna partida activa.",
        "solo_creador_cancelar":    "⚠️ Solo el creador puede cancelar la partida.",
        "cancelado":                "❌ Partida cancelada\\. Usa /playimpostor para empezar otra\\.",
        "idioma_actual":            "🌐 *Idioma actual: Español*\n\nElige un idioma:",
        "idioma_cambiado_es":       "✅ Idioma cambiado a *Español*\\.",
        "idioma_cambiado_en":       "✅ Language changed to *English*\\.",
        "solo_admin_idioma":        "⚠️ Solo los administradores del grupo pueden cambiar el idioma.",
        "btn_es":                   "🇪🇸 Español",
        "btn_en":                   "🇬🇧 English",
        "start_privado":             (
            "✅ *¡Listo\\!* Ya puedes recibir tu palabra secreta\\.\n\n"
            "Vuelve al grupo y presiona *Unirse* o usa `/join` para entrar a la partida\\."
        ),
        "rivalidad_titulo":        "⚔️ *Rivalidad*\n\n",
        "rivalidad_uso":           "⚠️ Uso: `/rivalidad @usuario1 @usuario2`",
        "rivalidad_sin_datos":     "📊 No hay votos registrados entre estos jugadores aún\\.",
        "pistas_fallback":          "1. Piensa en sus características principales\n2. Recuerda dónde o cómo se usa",
        "prompt_pistas":            (
            "Genera exactamente 2 pistas para describir '{palabra}' (categoría: {categoria}) "
            "en el juego del impostor. Las pistas deben:\n"
            "- Ayudar a describir la palabra SIN decirla directamente ni usar palabras muy obvias\n"
            "- Ser cortas, de máximo 10 palabras cada una\n"
            "- Estar numeradas como 1. y 2.\n"
            "Responde SOLO con las 2 pistas, sin explicaciones."
        ),
        "cat_custom":               "⭐ Personalizado",
        "addword_ok":               "✅ Palabra *{palabra}* agregada a la categoría personalizada\\.",
        "addword_ya_existe":        "⚠️ *{palabra}* ya está en la lista\\.",
        "addword_uso":              "⚠️ Uso: `/addword <palabra>`",
        "addword_solo_admin":       "⚠️ Solo los administradores pueden agregar palabras.",
        "words_lista":              "⭐ *Palabras personalizadas* \\({n}\\):\n\n{lista}\n\n_Usa /addword para agregar más\\._",
        "words_vacia":              "📭 No hay palabras personalizadas aún\\.\n\nUsa `/addword <palabra>` para agregar\\.",
        "removeword_ok":            "🗑️ Palabra *{palabra}* eliminada\\.",
        "removeword_no_existe":     "⚠️ *{palabra}* no está en la lista\\.",
        "removeword_solo_admin":    "⚠️ Solo los administradores pueden eliminar palabras.",
        "removeword_uso":           "⚠️ Uso: `/removeword <palabra>`",
        "resetjugador_ok":          "🔄 *Puntajes de {nombre} reseteados\\.*",
        "resetjugador_no_encontrado":"⚠️ No encontré a *{nombre}* en este grupo\\.",
        "resetjugador_uso":          "⚠️ Uso: `/resetjugador @usuario` o `/resetjugador nombre`",
        "resetjugador_solo_admin":   "⚠️ Solo los administradores pueden resetear jugadores.",
        "roles_tabla":              "🎭 *Roles por jugador:*\n\n{tabla}",
        "roles_tabla_header":       "🎭 *Roles por jugador:*",
        "roles_sin_datos":          "📊 No hay datos de roles aún\\. ¡Juega primero\\!",
        "col_impostor":             "😈",
        "col_inocente":             "😇",
    },
    "en": {
        # cmd_start
        "start": (
            "🕵️ *Welcome to The Impostor Bot\\!*\n\n"
            "The game is simple:\n"
            "• Everyone gets the *same secret word*\n"
            "• Except the *impostors*, who don't know it\n"
            "• Give clues without saying it directly 🎭\n"
            "• The group votes to eliminate players each round\n\n"
            "*Commands:*\n"
            "`/playimpostor` — Create a game\n"
            "`/join` — Join the game\n"
            "`/vote` — Open voting \\(creator only\\)\n"
            "`/howtoplay` — How to play\n"
            "`/score` — View scoreboard\n"
            "`/resetimpostor` — Reset scores\n"
            "`/language` — Change language\n"
            "`/cancel` — Cancel game"
        ),
        # cmd_como_jugar
        "comojugar": (
            "🕵️ *How to play The Impostor?*\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "*📋 Objective*\n"
            "The group must eliminate all impostors\\. "
            "Impostors must blend in or guess the secret word\\.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "*🎮 Game steps*\n\n"
            "*0\\. Start the bot in private*\n"
            "Go to @impostortg\\_bot and press the `Start` button at the bottom, then use `/howtoplay` to learn about the bot\\.\n\n"
            "*1\\. Create the game*\n"
            "Someone uses `/playimpostor` and others join with `/join` or the button\\.\n\n"
            "*2\\. Start*\n"
            "With at least 3 players, the creator picks a category and taps *Start game\\!*\n\n"
            "*3\\. Secret words*\n"
            "The bot sends a private message to each player:\n"
            "• Regular players receive the *secret word*\n"
            "• The impostor\\(s\\) do NOT receive the word, only the category 🎭\n\n"
            "*4\\. Give clues*\n"
            "Following a random order, each player gives *one clue* about the word\\. "
            "The impostor must make up a convincing clue without knowing the word\\.\n\n"
            "*5\\. Vote*\n"
            "When everyone has given their clue, the creator opens voting\\. "
            "Only *alive* players vote\\. The most voted player is eliminated\\.\n\n"
            "*6\\. Reveal and new round*\n"
            "It's revealed if the eliminated player was an impostor or innocent\\. "
            "If players remain, a new order is shown and the game continues\\.\n\n"
            "*7\\. Impostor's last chance*\n"
            "If the group votes out an impostor, they get *one last chance*: "
            "guess the word by typing it in the chat\\. "
            "If they guess it, *all impostors win*\\.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "*🏆 Who wins?*\n\n"
            "🎉 *Group wins* if:\n"
            "  • They eliminate all impostors\n\n"
            "🕵️ *Impostor\\(s\\) win\\(s\\)* if:\n"
            "  • Only 1 innocent remains alongside an impostor\n"
            "  • An impostor guesses the word when voted out\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "*👥 Impostors by player count*\n"
            "  • 3\\-4 players → 1 impostor\n"
            "  • 5\\-6 players → 1 to 3 impostors \\(random\\)\n"
            "  • 7\\+ players → 2 to 3 impostors \\(random\\)\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "*📌 Commands*\n"
            "`/playimpostor` — Create game\n"
            "`/join` — Join game\n"
            "`/vote` — Open voting \\(creator\\)\n"
            "`/score` — View scoreboard\n"
            "`/resetimpostor` — Reset scores\n"
            "`/language` — Change language\n"
            "`/cancel` — Cancel game"
        ),
        "partida_activa":           "⚠️ There's already an active game\\. Use /cancel to cancel it first\\.",
        "bot_no_iniciado":          "⚠️ You need to start the bot first to receive your secret word.\n\nOpen the private chat with the bot, press START and then come back here to join.",
        "btn_unirse":               "✋ Join the game",
        "nueva_partida":            "🎮 *{nombre} created a new Impostor game\\!*\n\nPress the button or use /join to join\\.\nWhen ready, the creator presses *Start game\\!*",
        "sin_partida":              "⚠️ There's no open game. Use /playimpostor to create one.",
        "partida_en_curso":         "⚠️ The game is already in progress, you can't join now.",
        "ya_en_partida":            "⚠️ You're already in the game.",
        "partida_llena":            "⚠️ The game is full \\(maximum {n} players\\)\\.",
        "unido":                    "✅ *{nombre} joined\\!*\n\n*Players* \\({n}\\):\n{lista}\n\n",
        "puede_iniciar":            "_The creator can start whenever ready\\._",
        "faltan_jugadores":         "Need *{n}* more players to start\\.",
        "btn_iniciar":              "🚀 Start game!",
        "no_partida_espera":        "No game waiting to start.",
        "solo_creador_iniciar":     "⚠️ Only the creator can start the game.",
        "pocos_jugadores":          "⚠️ You need at least 3 players. There are {n} now.",
        "elige_categoria":          "🗂️ *Choose a category:*",
        "btn_random":               "🎲 Surprise me! (Random)",
        "config_impostores":        "⚙️ *Configure impostors*\n\nPlayers: *{n}*\nImpostors: *{imp}*\n\n_The creator can choose how many impostors there will be\\._",
        "btn_imp_menos":            "➖",
        "btn_imp_mas":              "➕",
        "btn_imp_confirmar":        "✅ Confirm and choose category",
        "btn_imp_random":           "🎲 Random (1-3)",
        "config_impostores_random":  "⚙️ *Configure impostors*\n\nPlayers: *{n}*\nImpostors: *🎲 Random \\(1\\-3\\)*\n\n_The creator can choose how many impostors there will be\\._",

        "solo_creador_categoria":   "Only the creator can choose the category.",
        "cat_sorpresa_grupo":       "🎲 *Surprise category\\!*",
        "cat_confirmacion":         "✅ Category: *{cat}*",
        "cat_grupo":                "Category: *{cat}*",
        "enviando_privado":         "📩 Sending words in private\\.\\.\\.",
        "eres_impostor":            "🕵️ *You are the IMPOSTOR\\!*\n\nCategory: *{cat}*\n\nYou don't know the word\\. Try to figure it out from others' clues\\. Don't get caught\\! 🎭",
        "eres_inocente":            "🔑 Your secret word is:\n\n✨ *{palabra}* ✨\n\nCategory: *{cat}*\n\n💡 *How you can describe it:*\n{pistas}\n\n_Give clues without saying the word directly\\. Find the impostor\\!_ 🕵️",
        "aviso_fallidos":           "\n\n⚠️ Could not message: {nombres}\n_They must start a conversation with the bot first_",
        "aviso_2rondas":            "This game is played in *2 clue rounds* before voting\\. Pay attention\\! 👀",
        "aviso_votar":              "When everyone has given their clue, the creator opens voting 🗳️",
        "partida_comienza":         "🎮 *The game begins\\!*\n\n{cat}\n\n*🎲 Clue order \\(randomly chosen\\):*\n{orden}\n\nEach player gives *one clue* about the word without saying it directly\\.\n{aviso_rondas}",
        "turno":                    "👆 *It's* [{nombre}](tg://user?id={uid})*'s turn\\!*\nWrite your clue in the chat\\.\n⏱️ _You have 1 minute\\._",
        "turno_timeout":            "⏰ *Time's up\\!* [{nombre}](tg://user?id={uid}) didn't give a clue in time\\. Skipping their turn\\.",
        "turno_timeout_autoconf":   "⏰ *Time's up\\!* [{nombre}](tg://user?id={uid})'s last clue was automatically confirmed\\.",
        "quien_es_impostor":        "🗳️ *Who is the impostor\\?*\n\n_Alive players \\({n}\\) — only they vote:_",
        "no_partida_curso":         "⚠️ There's no game in progress.",
        "solo_creador_votar":       "⚠️ Only the creator can open voting.",
        "no_partida_votacion":      "Voting has already closed.",
        "no_puedes_votar":          "You can't vote: you're eliminated or not part of this game.",
        "voto_ya":                  "You already voted.",
        "voto_ok":                  "✅ Vote registered!",
        "voto_confirmado":          "✅ *{nombre}* voted\\. {faltantes}",
        "voto_confirmado_revoto":   "✅ *{nombre}* voted in the re-vote\\. {faltantes}",
        "faltan_votos":             "*{n}* votes remaining\\.",
        "no_revotacion":            "No re-vote active.",
        "voto_invalido":            "Invalid vote.",
        "segundo_empate":           "⚖️ *Second tie\\!*\n\nNobody is eliminated this round\\. The game continues\\!\n\nThe creator opens voting when ready\\.",
        "btn_abrir_votacion":       "🗳️ Open voting!",
        "votacion_auto":            "⏰ *Voting opened automatically\\!*\n\n_Alive players \\({n}\\) — 1 minute to vote:_",
        "votos_insuficientes":      "⚠️ *Nobody reached 2 votes*\n\nVoting reopened\\.",
        "votos_timeout":            "⏰ *Voting time expired*\n\n",
        "empate":                   "⚖️ *Tie\\!*\n\n{nombres} have *{n} votes* each\\.\n\n🔁 *Re-vote* — Only tied players:\n_Alive players, vote again:_",
        "nuevo_creador":            "👑 *{nombre}* is the new creator and can open voting\\.",
        "resultado_votacion":       "🗳️ *Voting result:*\n\nThe group voted for *{nombre}*\n{etiqueta}\n\n*Votes:*\n{detalle}",
        "era_impostor":             "🕵️ Was an impostor\\!",
        "era_inocente":             "✅ Was innocent\\.",
        "ultima_oportunidad":       "🎯 *Last chance, {nombre}\\!*\n\nIf you guess the secret word *you and all impostors win\\!*\n\n📝 Write the word now in the chat\\.\n_Category: {cat}_\n⏱️ _You have 30 seconds\\._",
        "adiv_timeout":             "⏰ *Time's up\\!* [{nombre}](tg://user?id={uid}) didn't guess in time\\. The group wins\\!",
        "adivino":                  "🎯 *{nombre} guessed the word\\!*\n\nThe word was *{palabra}*\\. Impostors win\\! 🕵️",
        "incorrecto":               "❌ *{nombre}* wrote *{texto}*\\.\\.\\. Wrong\\!\n\n*{nombre}* is permanently eliminated\\.",
        "confirmar_pista_btn":      "✅ Confirm this as my clue",
        "confirmar_adivinanza_btn": "✅ Confirm this as my answer",
        "confirmar_adivinanza_msg": "Confirm *{palabra}* as your answer\\?",

        "confirmar_pista_msg":      "Confirm *{pista}* as your clue\\?",
        "no_tu_turno":              "⚠️ It's not your turn.",
        "pista_confirmada":         "✅ Clue confirmed!",
        "pista_auto_confirmada":    "⚠️ Third attempt — clue sent automatically\\.",
        "aviso_30s":                "⏱️ *30 seconds* left for your clue\\! [{nombre}](tg://user?id={uid})",
        "aviso_15s":                "⏱️ *15 seconds* left to guess\\! [{nombre}](tg://user?id={uid})",
        "segunda_ronda":            "🔄 *Second clue round\\!*\n\nAfter this round, voting will open\\.\n\n*🎲 New order:*\n{orden}",
        "todos_dieron_pista":       "✅ *Everyone gave their clue\\!*\n\nThe creator can open voting 🗳️",
        "nueva_ronda_pistas":       "🔄 *New clue round\\!*\n\n👥 Alive players: *{n}*\n\n*🎲 New clue order:*\n{orden}\n\nEach player gives *one clue* about the word\\.\nWhen done, the creator opens voting 🗳️",
        "grupo_gana":               "🎉 *The group won\\!*\n\nThe impostors were: {impostores}\nThey were eliminated without guessing the word\\!\n\n🔑 The word was: *{palabra}* \\({cat}\\)\n\n_Use /playimpostor for another round_",
        "impostores_ganan":         "🕵️ *The impostors won\\!*\n\nThey were: {impostores}\n{desc}\n\n🔑 The word was: *{palabra}* \\({cat}\\)\n\n_Use /playimpostor for another round_",
        "desc_supervivencia":       "The impostors survived until only one innocent remained\\.",
        "desc_adivino":             "An impostor guessed the correct word\\.",
        "desc_error_voto":          "They incorrectly voted for *{nombre}*\\.",
        "sin_estadisticas":         "📊 No stats yet\\. Play first\\!",
        "marcador":                 "🏆 *Group scoreboard:*\n\n{tabla}",
        "col_jugador":              "Player",
        "solo_admin_reset":         "⚠️ Only group admins can reset scores.",
        "reset_ok":                 "🔄 *Scores reset\\.*\n\nAll wins and losses back to zero\\. Let's start fresh\\! 🎮",
        "resetroles_ok":            "🔄 *Roles reset\\.*\n\nAll impostor and innocent counters back to zero\\. 🎭",
        "sin_partida_activa":       "⚠️ There's no active game.",
        "solo_creador_cancelar":    "⚠️ Only the creator can cancel the game.",
        "cancelado":                "❌ Game cancelled\\. Use /playimpostor to start another\\.",
        "idioma_actual":            "🌐 *Current language: English*\n\nChoose a language:",
        "idioma_cambiado_es":       "✅ Idioma cambiado a *Español*\\.",
        "idioma_cambiado_en":       "✅ Language changed to *English*\\.",
        "solo_admin_idioma":        "⚠️ Only group admins can change the language.",
        "btn_es":                   "🇪🇸 Español",
        "btn_en":                   "🇬🇧 English",
        "start_privado":             (
            "✅ *Ready\\!* You can now receive your secret word\\.\n\n"
            "Go back to the group and press *Join* or use `/join` to enter the game\\."
        ),
        "rivalidad_uso":           "⚠️ Usage: `/rivalidad @user1 @user2`",
        "rivalidad_sin_datos":     "📊 No votes recorded between these players yet\\.",
        "rivalidad_titulo":        "⚔️ *Rivalry*\n\n",
        "pistas_fallback":          "1. Think about its main characteristics\n2. Remember where or how it's used",
        "prompt_pistas":            (
            "Generate exactly 2 clues to describe '{palabra}' (category: {categoria}) "
            "in the impostor game. The clues must:\n"
            "- Help describe the word WITHOUT saying it directly or using very obvious words\n"
            "- Be short, maximum 10 words each\n"
            "- Be numbered as 1. and 2.\n"
            "Reply ONLY with the 2 clues, no explanations."
        ),
        "cat_custom":               "⭐ Custom",
        "addword_ok":               "✅ Word *{palabra}* added to the custom category\\.",
        "addword_ya_existe":        "⚠️ *{palabra}* is already in the list\\.",
        "addword_uso":              "⚠️ Usage: `/addword <word>`",
        "addword_solo_admin":       "⚠️ Only admins can add words.",
        "words_lista":              "⭐ *Custom words* \\({n}\\):\n\n{lista}\n\n_Use /addword to add more\\._",
        "words_vacia":              "📭 No custom words yet\\.\n\nUse `/addword <word>` to add some\\.",
        "removeword_ok":            "🗑️ Word *{palabra}* removed\\.",
        "removeword_no_existe":     "⚠️ *{palabra}* is not in the list\\.",
        "removeword_solo_admin":    "⚠️ Only admins can remove words.",
        "removeword_uso":           "⚠️ Usage: `/removeword <word>`",
        "resetjugador_ok":          "🔄 *{nombre}'s scores have been reset\\.*",
        "resetjugador_no_encontrado":"⚠️ Couldn't find *{nombre}* in this group\\.",
        "resetjugador_uso":          "⚠️ Usage: `/resetjugador @username` or `/resetjugador name`",
        "resetjugador_solo_admin":   "⚠️ Only admins can reset players.",
        "roles_tabla":              "🎭 *Roles per player:*\n\n{tabla}",
        "roles_tabla_header":       "🎭 *Roles per player:*",
        "roles_sin_datos":          "📊 No role data yet\\. Play first\\!",
        "col_impostor":             "😈",
        "col_inocente":             "😇",
    }
}

# ══════════════════════════════════════════════════════════════
# CATEGORÍAS EN AMBOS IDIOMAS
# ══════════════════════════════════════════════════════════════

CATEGORIAS = {
    "es": {
        "🐾 Animales": [
            "León", "Tigre", "Leopardo", "Guepardo", "Jaguar",
            "Elefante", "Jirafa", "Hipopótamo", "Rinoceronte", "Cebra",
            "Gorila", "Chimpancé", "Orangután", "Koala", "Canguro",
            "Panda", "Oso polar", "Oso grizzly", "Lobo", "Zorro",
            "Camello", "Bisonte", "Alce", "Ciervo", "Jabalí",
            "Delfín", "Ballena", "Orca", "Foca", "Manatí", "Nutria", "Castor",
            "Cocodrilo", "Caimán", "Iguana", "Camaleón", "Gecko",
            "Tortuga", "Serpiente", "Cobra", "Anaconda", "Dragón de Komodo",
            "Salamandra", "Rana toro",
            "Flamenco", "Pingüino", "Tucán", "Loro", "Cóndor",
            "Águila", "Búho", "Pavo real", "Pelícano", "Colibrí", "Avestruz", "Kiwi",
            "Tiburón", "Pulpo", "Medusa", "Mantarraya", "Caballito de mar",
            "Estrella de mar", "Cangrejo", "Langosta", "Pez payaso",
            "Murciélago", "Ornitorrinco", "Armadillo", "Pangolín", "Axolote",
            "Tarántula", "Escorpión", "Mantis religiosa",
        ],
        "⚽ Deportes": [
            "Fútbol", "Baloncesto", "Voleibol", "Rugby", "Hockey sobre hielo",
            "Béisbol", "Waterpolo", "Handball", "Fútbol americano",
            "Cricket", "Polo", "Ultimate Frisbee",
            "Tenis", "Pádel", "Bádminton", "Squash", "Tenis de mesa",
            "Boxeo", "Judo", "Karate", "Taekwondo", "Esgrima",
            "Lucha libre", "Sumo", "Muay Thai", "Kendo",
            "Natación", "Surf", "Remo", "Kayak",
            "Vela", "Esquí acuático", "Buceo", "Triatlón", "Natación sincronizada",
            "Escalada", "Esquí", "Snowboard", "Parapente", "Rappel",
            "Senderismo", "Ciclismo de montaña",
            "Maratón", "Salto de altura", "Lanzamiento de jabalina", "Decatlón",
            "Golf", "Arquería", "Ciclismo", "Patinaje artístico", "Gimnasia",
            "Tiro con arco", "Equitación",
        ],
        "🌍 Lugares del mundo": [
            "Machu Picchu", "Coliseo Romano", "Torre Eiffel", "Taj Mahal", "Gran Muralla China",
            "Stonehenge", "Angkor Wat", "Petra", "Cristo Redentor", "Pirámides de Giza",
            "Alhambra", "Sagrada Familia", "Big Ben", "Estatua de la Libertad", "Kremlin",
            "Times Square", "Tokio", "Venecia", "Dubái", "Bangkok",
            "Estambul", "Río de Janeiro", "Ciudad del Cabo", "Singapur", "Praga",
            "Buenos Aires", "Marrakech", "Amsterdam", "Nueva Orleans", "Kioto",
            "Sahara", "Amazonas", "Patagonia", "Islandia", "Maldivas",
            "Gran Cañón", "Siberia", "Antártida", "Serengeti", "Fiordos Noruegos",
            "Gran Barrera de Coral", "Selva Negra", "Desierto de Atacama", "Valle de la Muerte", "Galápagos",
            "Lago Titicaca", "Mar Muerto", "Río Nilo", "Lago Baikal", "Cataratas del Niágara",
            "Cataratas Victoria", "Mar Mediterráneo", "Río Amazonas", "Mar Caribe",
            "La Toscana", "Bali", "Santorini", "Cappadocia", "Polinesia Francesa",
            "Tibet", "Laponia", "Zanzibar", "Maasai Mara", "Borneo",
        ],
        "📦 Objetos cotidianos": [
            "Paraguas", "Espejo", "Gancho", "Colador", "Embudo",
            "Tijeras", "Candado", "Lupa", "Brújula", "Termómetro",
            "Reloj", "Cuaderno", "Mesa", "Silla", "Lámpara",
            "Almohada", "Cobija", "Cortina", "Jabonera", "Tapete",
            "Florero", "Portarretrato", "Canasto", "Escoba", "Trapeador",
            "Sartén", "Olla", "Cuchillo", "Tenedor", "Cuchara",
            "Rallador", "Destapador", "Corcho", "Delantal", "Licuadora",
            "Tostadora", "Microondas", "Mortero", "Espátula", "Batidora",
            "Calculadora", "Maletín", "Destornillador", "Engrapadora", "Regla",
            "Sacapuntas", "Borrador", "Clip", "Carpeta", "Sello",
            "Archivador", "Pizarrón", "Marcador", "Compás", "Resaltador",
            "Billetera", "Llavero", "Pañuelo", "Agenda",
            "Audífonos", "Cargador", "Termo", "Linterna", "Veladora",
            "Martillo", "Alicate", "Taladro", "Serrucho", "Escalera",
            "Pincel", "Rodillo", "Cinta", "Llave", "Nivel",
        ],
        "🎨 Colores": [
            "Turquesa", "Magenta", "Escarlata", "Índigo", "Negro",
            "Lavanda", "Carmesí", "Rosado", "Marfil", "Rojo",
            "Amarillo", "Violeta", "Dorado", "Plateado", "Coral", "Azul", "Blanco",
        ],
        "🌐 Países": [
            "Noruega", "Grecia", "Portugal", "Islandia", "Suecia",
            "Finlandia", "Dinamarca", "Polonia", "Hungría", "Rumania",
            "Croacia", "Serbia", "Austria", "Suiza", "Bélgica",
            "Países Bajos", "Irlanda", "Escocia", "Albania", "Montenegro",
            "Brasil", "Argentina", "Colombia", "Chile", "Perú",
            "México", "Canadá", "Cuba", "Venezuela", "Bolivia",
            "Ecuador", "Uruguay", "Paraguay", "Costa Rica", "Panamá",
            "Guatemala", "Honduras", "Jamaica", "República Dominicana", "Haití",
            "Japón", "Tailandia", "India", "China", "Corea del Sur", "Corea del Norte",
            "Vietnam", "Indonesia", "Filipinas", "Malasia", "Nepal",
            "Pakistán", "Bangladés", "Sri Lanka", "Myanmar", "Camboya",
            "Mongolia", "Kazajistán", "Uzbekistán", "Georgia", "Armenia",
            "Marruecos", "Sudáfrica", "Egipto",
            "Tanzania", "Ghana", "Senegal", "Nigeria", "Túnez",
            "Argelia", "Mozambique", "Madagascar", "Zimbabue", "Camerún",
            "Australia", "Nueva Zelanda",
            "Israel", "Irán", "Iraq", "Arabia Saudita",
        ],
        "🎌 Anime (personajes/series)": [
            "Goku", "Naruto", "Luffy", "Ichigo", "Eren Jaeger",
            "Levi Ackerman", "Edward Elric", "Spike Spiegel", "Light Yagami", "L Lawliet",
            "Sailor Moon", "Sakura Kinomoto", "Asuka Langley", "Rei Ayanami", "Mikasa Ackerman",
            "Killua", "Gon Freecss", "Meruem", "Hisoka", "Kurapika",
            "Zoro", "Sanji", "Nami", "Nico Robin", "Shanks",
            "Sasuke", "Itachi", "Kakashi", "Madara", "Hinata",
            "Tanjiro", "Nezuko", "Zenitsu", "Inosuke", "Muzan",
            "Deku", "Bakugo", "All Might", "Todoroki", "Endeavor",
            "Vegeta", "Piccolo", "Gohan", "Frieza", "Cell",
            "Saitama", "Genos", "Garou", "Bang", "Tatsumaki",
            "Dragon Ball", "One Piece", "Bleach", "Attack on Titan",
            "Fullmetal Alchemist", "Death Note", "Hunter x Hunter", "Demon Slayer", "My Hero Academia",
            "Neon Genesis Evangelion", "Cowboy Bebop", "Sword Art Online", "Tokyo Ghoul", "Fairy Tail",
            "One Punch Man", "Jujutsu Kaisen", "Chainsaw Man", "Spy x Family", "Re:Zero",
            "Steins;Gate", "Code Geass", "No Game No Life", "Overlord", "Black Clover",
            "Vinland Saga", "Mob Psycho 100", "Violet Evergarden", "Your Lie in April", "Clannad",
            "Studio Ghibli", "Shonen Jump", "Isekai", "Tsundere", "Shōnen",
            "Seinen", "Mecha", "Filler", "Mangaka",
        ],
        "⚽ Futbolistas": [
            "Pelé", "Diego Maradona", "Johan Cruyff", "Franz Beckenbauer", "Ronaldo Nazário",
            "Zinedine Zidane", "Ronaldinho", "Roberto Carlos", "Cafu", "Paolo Maldini",
            "Franco Baresi", "Marco van Basten", "Ruud Gullit", "George Best", "Bobby Charlton",
            "Michel Platini", "Eusébio", "Garrincha", "Lev Yashin", "Ferenc Puskás",
            "Thierry Henry", "Andrés Iniesta", "Xavi Hernández", "Steven Gerrard", "Frank Lampard",
            "Wayne Rooney", "Fernando Torres", "David Villa", "Kaká", "Samuel Eto'o",
            "Didier Drogba", "Gianluigi Buffon", "Carles Puyol", "John Terry", "Ashley Cole",
            "Lionel Messi", "Cristiano Ronaldo", "Neymar", "Luka Modric", "Sergio Ramos",
            "Luis Suárez", "Zlatan Ibrahimović", "Arjen Robben", "Franck Ribéry", "Iker Casillas",
            "Manuel Neuer", "Sergio Busquets", "David Silva", "Cesc Fàbregas", "Mesut Özil",
            "Kylian Mbappé", "Erling Haaland", "Vinicius Jr", "Pedri", "Gavi",
            "Rodri", "Jude Bellingham", "Phil Foden", "Bukayo Saka", "Jamal Musiala",
            "Federico Valverde", "Rafael Leão", "Victor Osimhen", "Mohamed Salah", "Sadio Mané",
            "Kevin De Bruyne", "Harry Kane", "Marcus Rashford", "Trent Alexander-Arnold", "Alphonso Davies",
        ],
        "🎤 K-Pop (idols/grupos)": [
            # Grupos femeninos actuales
            "BLACKPINK", "TWICE", "aespa", "IVE", "NewJeans",
            "ITZY", "NMIXX", "LE SSERAFIM", "MAMAMOO", "Red Velvet",
            "BABYMONSTER", "STAYC", "Kep1er", "EVERGLOW", "WEEEKLY",
            "tripleS", "(G)I-DLE", "APINK", "EXID", "AOA",
            # Grupos femeninos legendarios/2ª y 3ª generación
            "Girls Generation", "2NE1", "Wonder Girls", "T-ARA", "SISTAR",
            "4MINUTE", "f(x)", "After School", "MISS A", "Brown Eyed Girls",
            "SECRET", "KARA", "Rainbow", "Hello Venus", "Stellar",
            "GFRIEND", "MOMOLAND", "LOONA", "Oh My Girl", "MAMAMOO",
            "CLC", "Dreamcatcher", "Brave Girls", "fromis_9", "DIA",
            # Solistas femeninas
            "IU", "Sunmi", "HyunA", "Chungha", "Heize",
            "Jessi", "Somi", "Gain", "BoA", "CL",
            "Taeyeon", "Tiffany", "Hyolyn", "Ailee", "Wheein",
            "Hwasa", "Yubin", "Hyosung", "Lee Hyori", "Park Bom",
            # Integrantes BLACKPINK
            "Jennie", "Lisa", "Rosé", "Jisoo",
            # Integrantes TWICE
            "Nayeon", "Jeongyeon", "Momo", "Sana", "Jihyo",
            "Mina", "Dahyun", "Chaeyoung", "Tzuyu",
            # Integrantes aespa
            "Karina", "Giselle", "Winter", "Ningning",
            # Integrantes IVE
            "Yujin", "Gaeul", "Rei", "Wonyoung", "Liz", "Leeseo",
            # Integrantes NewJeans
            "Minji", "Hanni", "Danielle", "Haerin", "Hyein",
            # Integrantes Red Velvet
            "Irene", "Seulgi", "Wendy", "Joy", "Yeri",
            # Integrantes ITZY
            "Yeji", "Lia", "Ryujin", "Chaeryeong", "Yuna",
            # Integrantes LE SSERAFIM
            "Sakura", "Chaewon", "Yunjin", "Kazuha", "Eunchae",
            # Integrantes Girls Generation
            "Taeyeon", "Tiffany", "Yoona", "Yuri", "Sooyoung",
            "Hyoyeon", "Sunny", "Seohyun",
            # Integrantes MAMAMOO
            "Solar", "Moonbyul", "Wheein", "Hwasa",
            # Integrantes NMIXX
            "Lily", "Haewon", "Sullyoon", "Bae", "Jiwoo", "Kyujin",
            # Integrantes STAYC
            "Sumin", "Sieun", "ISA", "Seeun", "Yoon", "J",
            # Integrantes Kep1er
            "Mashiro", "Chaehyun", "Hikaru", "Dayeon", "Xiaoting", "Yeseo", "Youngeun",
            # Integrantes EVERGLOW
            "Aisha", "Sihyeon", "Mia", "Onda", "Yiren",
            # Integrantes (G)I-DLE
            "Miyeon", "Minnie", "Soojin", "Soyeon", "Yuqi", "Shuhua",
            # Integrantes EXID
            "Solji", "LE", "Hani", "Hyelin", "Jeonghwa",
            # Integrantes APINK
            "Chorong", "Bomi", "Eunji", "Namjoo", "Hayoung",
            # Integrantes GFRIEND
            "SinB", "Eunha", "Umji",
            # Integrantes BABYMONSTER
            "Ruka", "Pharita", "Asa", "Rami", "Ahyeon", "Rora", "Chiquita",
            # Integrantes Oh My Girl
            "Hyojung", "Mimi", "YooA", "Seunghee", "Jiho", "Binnie", "Arin",
            # Integrantes fromis_9
            "Hayoung", "Saerom", "Nagyung", "Jiwon", "Jisun", "Seoyeon", "Chaeyoung", "Gyuri",
            # Integrantes LOONA
            "Heejin", "Hyunjin", "Haseul", "Yeojin", "Vivi", "Kim Lip", "Jinsoul", "Choerry",
            # Integrantes Dreamcatcher
            "JiU", "SuA", "Siyeon", "Handong", "Yoohyeon", "Dami", "Gahyeon",
        ],
        "🍽️ Comidas del mundo": [
            "Pizza", "Pasta Carbonara", "Lasaña", "Risotto", "Paella",
            "Sushi", "Ramen", "Arroz frito", "Bibimbap",
            "Hamburguesa", "Hot Dog", "Asado argentino", "Peking Duck", "Shawarma",
            "Kebab", "Tacos", "Barbacoa", "Churrasco", "Cordero al horno",
            "Tom Yum", "Gazpacho", "Borscht", "Caldo de pollo",
            "Miso", "Minestrone", "Goulash", "Ceviche",
            "Croissant", "Bagel", "Pretzel", "Falafel", "Empanada",
            "Arepa", "Tortilla", "Naan", "Baguette", "Pita",
            "Curry", "Hummus", "Moussaka", "Couscous", "Kimchi",
            "Tempura", "Dim Sum", "Gyoza", "Burrito", "Enchilada",
            "Tiramisu", "Crêpe", "Waffle",
            "Cheesecake", "Macarons", "Baklava", "Mochi", "Churros",
            "Crème Brûlée", "Brownie", "Donut", "Cannoli", "Profiteroles",
            "Pancakes", "Eggs Benedict", "Granola", "Acai Bowl", "Shakshuka",
            "Nachos", "Spring Rolls", "Samosa", "Poutine",
            "Fish and Chips", "Currywurst", "Takoyaki", "Elote", "Pupusas",
        ],
        "🌟 Famosos": [
            "Tom Hanks", "Meryl Streep", "Leonardo DiCaprio", "Scarlett Johansson", "Denzel Washington",
            "Brad Pitt", "Angelina Jolie", "Johnny Depp", "Natalie Portman", "Cate Blanchett",
            "Robert Downey Jr", "Chris Evans", "Margot Robbie", "Ryan Reynolds", "Dwayne Johnson",
            "Will Smith", "Morgan Freeman", "Samuel L. Jackson", "Jennifer Lawrence", "Emma Stone",
            "Steven Spielberg", "Christopher Nolan", "Quentin Tarantino", "Martin Scorsese", "Tim Burton",
            "Michael Jackson", "Madonna", "Beyoncé", "Taylor Swift", "Rihanna",
            "Eminem", "Drake", "Bad Bunny", "J Balvin", "Shakira",
            "Ed Sheeran", "Adele", "Lady Gaga", "Justin Bieber", "Billie Eilish",
            "The Weeknd", "Kanye West", "Jay-Z", "Ariana Grande", "Dua Lipa",
            "MrBeast", "PewDiePie", "Ibai", "Auronplay", "TheGrefg",
            "Ninja", "Pokimane", "xQc", "Rubius", "Vegetta777",
            "Elon Musk", "Jeff Bezos", "Mark Zuckerberg", "Steve Jobs", "Bill Gates",
        ],
        "🎬 Películas & Series": [
            "El Padrino", "Titanic", "Schindler's List", "Pulp Fiction", "Forrest Gump",
            "El Rey León", "Matrix", "Gladiador", "Interstellar", "Inception",
            "El Señor de los Anillos", "Star Wars", "Indiana Jones", "Jurassic Park", "Alien",
            "Terminator", "RoboCop", "Blade Runner", "2001 Odisea en el espacio", "Psicosis",
            "Avatar", "Avengers Endgame", "Spider-Man", "Batman", "Superman",
            "Black Panther", "Iron Man", "Doctor Strange", "Joker", "Oppenheimer",
            "Barbie", "Top Gun", "John Wick", "Everything Everywhere", "Get Out",
            "Breaking Bad", "Game of Thrones", "The Wire", "Los Soprano", "The Office",
            "Friends", "Seinfeld", "Lost", "24", "House of Cards",
            "Stranger Things", "Black Mirror", "Peaky Blinders", "Narcos", "Dexter",
            "The Crown", "Chernobyl", "Squid Game", "Dark", "Severance",
            "Los Simpsons", "South Park", "Futurama", "Rick y Morty", "Bob's Burgers",
            "Avatar La Leyenda de Aang", "Arcane", "Bojack Horseman", "Gravity Falls", "Steven Universe",
            "Walter White", "Tony Soprano", "Daenerys Targaryen", "Jon Snow", "Tyrion Lannister",
            "Hannibal Lecter", "James Bond", "Ellen Ripley", "El Guasón",
        ],
        "💼 Profesiones": [
            "Médico", "Enfermero", "Cirujano", "Psicólogo", "Dentista",
            "Veterinario", "Farmacéutico", "Fisioterapeuta", "Paramédico", "Nutricionista",
            "Programador", "Diseñador web", "Ingeniero de software", "Hacker ético", "Analista de datos",
            "Administrador de redes", "Desarrollador móvil", "DevOps",
            "Actor", "Director de cine", "Músico", "Fotógrafo", "Ilustrador",
            "Escritor", "Periodista", "Diseñador gráfico", "Animador", "Productor musical",
            "Maestro", "Profesor universitario", "Científico", "Arqueólogo", "Astrónomo",
            "Biólogo marino", "Geólogo", "Antropólogo", "Historiador", "Filósofo",
            "Chef", "Bombero", "Policía", "Abogado", "Juez",
            "Arquitecto", "Piloto", "Astronauta", "Detective", "Diplomático",
            "Mecánico", "Electricista", "Carpintero", "Plomero", "Soldador",
            "Futbolista", "Atleta olímpico", "Entrenador personal", "Árbitro", "Escalador profesional",
            "Buzo", "Piloto de carreras", "Jinete", "Surfista profesional", "Boxeador",
        ],
        "🎮 Videojuegos": [
            "Mario", "Link", "Master Chief", "Kratos", "Geralt de Rivia",
            "Lara Croft", "Nathan Drake", "Cloud Strife", "Solid Snake", "Samus Aran",
            "Sonic", "Pikachu", "Crash Bandicoot", "Spyro", "Mega Man",
            "Dante", "Ryu", "Sub-Zero", "Scorpion", "Kazuya Mishima",
            "Arthur Morgan", "Joel", "Ellie", "Aloy",
            "Minecraft", "Fortnite", "League of Legends", "Counter-Strike", "Valorant",
            "Grand Theft Auto", "Red Dead Redemption", "The Last of Us", "God of War", "Zelda",
            "Dark Souls", "Elden Ring", "Cyberpunk 2077", "The Witcher", "Skyrim",
            "Call of Duty", "Halo", "FIFA", "NBA 2K",
            "Among Us", "Rocket League", "Overwatch", "Apex Legends", "PUBG",
            "Resident Evil", "Silent Hill", "Bioshock", "Portal", "Half-Life",
            "Super Mario", "Pokemon", "Tetris", "Pac-Man", "Space Invaders",
            "Final Fantasy", "Dragon Quest", "Monster Hunter", "Street Fighter", "Mortal Kombat",
            "PlayStation", "Xbox", "Nintendo Switch", "Game Boy", "Atari",
            "Nintendo", "Sony", "Valve", "Rockstar Games",
            "Naughty Dog", "CD Projekt Red", "FromSoftware", "Blizzard", "Epic Games",
        ],
    },
    "en": {
        "🐾 Animals": [
            "Lion", "Tiger", "Leopard", "Cheetah", "Jaguar",
            "Elephant", "Giraffe", "Hippopotamus", "Rhinoceros", "Zebra",
            "Gorilla", "Chimpanzee", "Orangutan", "Koala", "Kangaroo",
            "Panda", "Polar bear", "Grizzly bear", "Wolf", "Fox",
            "Camel", "Bison", "Moose", "Deer", "Wild boar",
            "Dolphin", "Whale", "Orca", "Seal", "Manatee", "Otter", "Beaver",
            "Crocodile", "Alligator", "Iguana", "Chameleon", "Gecko",
            "Turtle", "Snake", "Cobra", "Anaconda", "Komodo dragon",
            "Salamander", "Bullfrog",
            "Flamingo", "Penguin", "Toucan", "Parrot", "Condor",
            "Eagle", "Owl", "Peacock", "Pelican", "Hummingbird", "Ostrich", "Kiwi",
            "Shark", "Octopus", "Jellyfish", "Manta ray", "Seahorse",
            "Starfish", "Crab", "Lobster", "Clownfish",
            "Bat", "Platypus", "Armadillo", "Pangolin", "Axolotl",
            "Tarantula", "Scorpion", "Praying mantis",
        ],
        "⚽ Sports": [
            "Soccer", "Basketball", "Volleyball", "Rugby", "Ice hockey",
            "Baseball", "Water polo", "Handball", "American football",
            "Cricket", "Polo", "Ultimate Frisbee",
            "Tennis", "Padel", "Badminton", "Squash", "Table tennis",
            "Boxing", "Judo", "Karate", "Taekwondo", "Fencing",
            "Wrestling", "Sumo", "Muay Thai", "Kendo",
            "Swimming", "Surfing", "Rowing", "Kayaking",
            "Sailing", "Water skiing", "Scuba diving", "Triathlon", "Synchronized swimming",
            "Rock climbing", "Skiing", "Snowboarding", "Paragliding", "Rappelling",
            "Hiking", "Mountain biking",
            "Marathon", "High jump", "Javelin throw", "Decathlon",
            "Golf", "Archery", "Cycling", "Figure skating", "Gymnastics",
            "Horseback riding",
        ],
        "🌍 World Places": [
            "Machu Picchu", "Roman Colosseum", "Eiffel Tower", "Taj Mahal", "Great Wall of China",
            "Stonehenge", "Angkor Wat", "Petra", "Christ the Redeemer", "Pyramids of Giza",
            "Alhambra", "Sagrada Familia", "Big Ben", "Statue of Liberty", "Kremlin",
            "Times Square", "Tokyo", "Venice", "Dubai", "Bangkok",
            "Istanbul", "Rio de Janeiro", "Cape Town", "Singapore", "Prague",
            "Buenos Aires", "Marrakech", "Amsterdam", "New Orleans", "Kyoto",
            "Sahara", "Amazon", "Patagonia", "Iceland", "Maldives",
            "Grand Canyon", "Siberia", "Antarctica", "Serengeti", "Norwegian Fjords",
            "Great Barrier Reef", "Black Forest", "Atacama Desert", "Death Valley", "Galapagos",
            "Lake Titicaca", "Dead Sea", "Nile River", "Lake Baikal", "Niagara Falls",
            "Victoria Falls", "Mediterranean Sea", "Amazon River", "Caribbean Sea",
            "Tuscany", "Bali", "Santorini", "Cappadocia", "French Polynesia",
            "Tibet", "Lapland", "Zanzibar", "Maasai Mara", "Borneo",
        ],
        "📦 Everyday Objects": [
            "Umbrella", "Mirror", "Hanger", "Colander", "Funnel",
            "Scissors", "Padlock", "Magnifying glass", "Compass", "Thermometer",
            "Clock", "Notebook", "Table", "Chair", "Lamp",
            "Pillow", "Blanket", "Curtain", "Soap dish", "Doormat",
            "Vase", "Picture frame", "Basket", "Broom", "Mop",
            "Frying pan", "Pot", "Knife", "Fork", "Spoon",
            "Grater", "Bottle opener", "Cork", "Apron", "Blender",
            "Toaster", "Microwave", "Mortar", "Spatula", "Hand mixer",
            "Calculator", "Briefcase", "Screwdriver", "Stapler", "Ruler",
            "Pencil sharpener", "Eraser", "Paperclip", "Folder", "Stamp",
            "Binder", "Whiteboard", "Marker", "Drawing compass", "Highlighter",
            "Wallet", "Keychain", "Handkerchief", "Planner",
            "Headphones", "Charger", "Thermos", "Flashlight", "Candle",
            "Hammer", "Pliers", "Drill", "Handsaw", "Ladder",
            "Paintbrush", "Roller", "Tape", "Wrench", "Level",
        ],
        "🎨 Colors": [
            "Turquoise", "Magenta", "Scarlet", "Indigo", "Black",
            "Lavender", "Crimson", "Pink", "Ivory", "Red",
            "Yellow", "Violet", "Gold", "Silver", "Coral", "Blue", "White",
        ],
        "🌐 Countries": [
            "Norway", "Greece", "Portugal", "Iceland", "Sweden",
            "Finland", "Denmark", "Poland", "Hungary", "Romania",
            "Croatia", "Serbia", "Austria", "Switzerland", "Belgium",
            "Netherlands", "Ireland", "Scotland", "Albania", "Montenegro",
            "Brazil", "Argentina", "Colombia", "Chile", "Peru",
            "Mexico", "Canada", "Cuba", "Venezuela", "Bolivia",
            "Ecuador", "Uruguay", "Paraguay", "Costa Rica", "Panama",
            "Guatemala", "Honduras", "Jamaica", "Dominican Republic", "Haiti",
            "Japan", "Thailand", "India", "China", "South Korea", "North Korea",
            "Vietnam", "Indonesia", "Philippines", "Malaysia", "Nepal",
            "Pakistan", "Bangladesh", "Sri Lanka", "Myanmar", "Cambodia",
            "Mongolia", "Kazakhstan", "Uzbekistan", "Georgia", "Armenia",
            "Morocco", "South Africa", "Egypt",
            "Tanzania", "Ghana", "Senegal", "Nigeria", "Tunisia",
            "Algeria", "Mozambique", "Madagascar", "Zimbabwe", "Cameroon",
            "Australia", "New Zealand",
            "Israel", "Iran", "Iraq", "Saudi Arabia",
        ],
        "🎌 Anime (characters/series)": [
            "Goku", "Naruto", "Luffy", "Ichigo", "Eren Jaeger",
            "Levi Ackerman", "Edward Elric", "Spike Spiegel", "Light Yagami", "L Lawliet",
            "Sailor Moon", "Sakura Kinomoto", "Asuka Langley", "Rei Ayanami", "Mikasa Ackerman",
            "Killua", "Gon Freecss", "Meruem", "Hisoka", "Kurapika",
            "Zoro", "Sanji", "Nami", "Nico Robin", "Shanks",
            "Sasuke", "Itachi", "Kakashi", "Madara", "Hinata",
            "Tanjiro", "Nezuko", "Zenitsu", "Inosuke", "Muzan",
            "Deku", "Bakugo", "All Might", "Todoroki", "Endeavor",
            "Vegeta", "Piccolo", "Gohan", "Frieza", "Cell",
            "Saitama", "Genos", "Garou", "Bang", "Tatsumaki",
            "Dragon Ball", "One Piece", "Bleach", "Attack on Titan",
            "Fullmetal Alchemist", "Death Note", "Hunter x Hunter", "Demon Slayer", "My Hero Academia",
            "Neon Genesis Evangelion", "Cowboy Bebop", "Sword Art Online", "Tokyo Ghoul", "Fairy Tail",
            "One Punch Man", "Jujutsu Kaisen", "Chainsaw Man", "Spy x Family", "Re:Zero",
            "Steins;Gate", "Code Geass", "No Game No Life", "Overlord", "Black Clover",
            "Vinland Saga", "Mob Psycho 100", "Violet Evergarden", "Your Lie in April", "Clannad",
            "Studio Ghibli", "Shonen Jump", "Isekai", "Tsundere", "Shōnen",
            "Seinen", "Mecha", "Filler", "Mangaka",
        ],
        "⚽ Footballers": [
            "Pelé", "Diego Maradona", "Johan Cruyff", "Franz Beckenbauer", "Ronaldo Nazário",
            "Zinedine Zidane", "Ronaldinho", "Roberto Carlos", "Cafu", "Paolo Maldini",
            "Franco Baresi", "Marco van Basten", "Ruud Gullit", "George Best", "Bobby Charlton",
            "Michel Platini", "Eusébio", "Garrincha", "Lev Yashin", "Ferenc Puskás",
            "Thierry Henry", "Andrés Iniesta", "Xavi Hernández", "Steven Gerrard", "Frank Lampard",
            "Wayne Rooney", "Fernando Torres", "David Villa", "Kaká", "Samuel Eto'o",
            "Didier Drogba", "Gianluigi Buffon", "Carles Puyol", "John Terry", "Ashley Cole",
            "Lionel Messi", "Cristiano Ronaldo", "Neymar", "Luka Modric", "Sergio Ramos",
            "Luis Suárez", "Zlatan Ibrahimović", "Arjen Robben", "Franck Ribéry", "Iker Casillas",
            "Manuel Neuer", "Sergio Busquets", "David Silva", "Cesc Fàbregas", "Mesut Özil",
            "Kylian Mbappé", "Erling Haaland", "Vinicius Jr", "Pedri", "Gavi",
            "Rodri", "Jude Bellingham", "Phil Foden", "Bukayo Saka", "Jamal Musiala",
            "Federico Valverde", "Rafael Leão", "Victor Osimhen", "Mohamed Salah", "Sadio Mané",
            "Kevin De Bruyne", "Harry Kane", "Marcus Rashford", "Trent Alexander-Arnold", "Alphonso Davies",
        ],
        "🎤 K-Pop (idols/groups)": [
            # Current girl groups
            "BLACKPINK", "TWICE", "aespa", "IVE", "NewJeans",
            "ITZY", "NMIXX", "LE SSERAFIM", "MAMAMOO", "Red Velvet",
            "BABYMONSTER", "STAYC", "Kep1er", "EVERGLOW", "WEEEKLY",
            "tripleS", "(G)I-DLE", "APINK", "EXID", "AOA",
            # Legendary/2nd & 3rd gen girl groups
            "Girls Generation", "2NE1", "Wonder Girls", "T-ARA", "SISTAR",
            "4MINUTE", "f(x)", "After School", "MISS A", "Brown Eyed Girls",
            "SECRET", "KARA", "Rainbow", "Hello Venus", "Stellar",
            "GFRIEND", "MOMOLAND", "LOONA", "Oh My Girl", "MAMAMOO",
            "CLC", "Dreamcatcher", "Brave Girls", "fromis_9", "DIA",
            # Female soloists
            "IU", "Sunmi", "HyunA", "Chungha", "Heize",
            "Jessi", "Somi", "Gain", "BoA", "CL",
            "Taeyeon", "Tiffany", "Hyolyn", "Ailee", "Wheein",
            "Hwasa", "Yubin", "Hyosung", "Lee Hyori", "Park Bom",
            # BLACKPINK members
            "Jennie", "Lisa", "Rosé", "Jisoo",
            # TWICE members
            "Nayeon", "Jeongyeon", "Momo", "Sana", "Jihyo",
            "Mina", "Dahyun", "Chaeyoung", "Tzuyu",
            # aespa members
            "Karina", "Giselle", "Winter", "Ningning",
            # IVE members
            "Yujin", "Gaeul", "Rei", "Wonyoung", "Liz", "Leeseo",
            # NewJeans members
            "Minji", "Hanni", "Danielle", "Haerin", "Hyein",
            # Red Velvet members
            "Irene", "Seulgi", "Wendy", "Joy", "Yeri",
            # ITZY members
            "Yeji", "Lia", "Ryujin", "Chaeryeong", "Yuna",
            # LE SSERAFIM members
            "Sakura", "Chaewon", "Yunjin", "Kazuha", "Eunchae",
            # Girls Generation members
            "Taeyeon", "Tiffany", "Yoona", "Yuri", "Sooyoung",
            "Hyoyeon", "Sunny", "Seohyun",
            # MAMAMOO members
            "Solar", "Moonbyul", "Wheein", "Hwasa",
            # NMIXX members
            "Lily", "Haewon", "Sullyoon", "Bae", "Jiwoo", "Kyujin",
            # STAYC members
            "Sumin", "Sieun", "ISA", "Seeun", "Yoon", "J",
            # Kep1er members
            "Mashiro", "Chaehyun", "Hikaru", "Dayeon", "Xiaoting", "Yeseo", "Youngeun",
            # EVERGLOW members
            "Aisha", "Sihyeon", "Mia", "Onda", "Yiren",
            # (G)I-DLE members
            "Miyeon", "Minnie", "Soojin", "Soyeon", "Yuqi", "Shuhua",
            # EXID members
            "Solji", "LE", "Hani", "Hyelin", "Jeonghwa",
            # APINK members
            "Chorong", "Bomi", "Eunji", "Namjoo", "Hayoung",
            # GFRIEND members
            "SinB", "Eunha", "Umji",
            # BABYMONSTER members
            "Ruka", "Pharita", "Asa", "Rami", "Ahyeon", "Rora", "Chiquita",
            # Oh My Girl members
            "Hyojung", "Mimi", "YooA", "Seunghee", "Jiho", "Binnie", "Arin",
            # fromis_9 members
            "Hayoung", "Saerom", "Nagyung", "Jiwon", "Jisun", "Seoyeon", "Chaeyoung", "Gyuri",
            # LOONA members
            "Heejin", "Hyunjin", "Haseul", "Yeojin", "Vivi", "Kim Lip", "Jinsoul", "Choerry",
            # Dreamcatcher members
            "JiU", "SuA", "Siyeon", "Handong", "Yoohyeon", "Dami", "Gahyeon",
        ],
        "🍽️ World Foods": [
            "Pizza", "Pasta Carbonara", "Lasagna", "Risotto", "Paella",
            "Sushi", "Ramen", "Fried rice", "Bibimbap",
            "Hamburger", "Hot Dog", "Argentine BBQ", "Peking Duck", "Shawarma",
            "Kebab", "Tacos", "BBQ ribs", "Churrasco", "Roast lamb",
            "Tom Yum", "Gazpacho", "Borscht", "Chicken soup",
            "Miso soup", "Minestrone", "Goulash", "Ceviche",
            "Croissant", "Bagel", "Pretzel", "Falafel", "Empanada",
            "Arepa", "Tortilla", "Naan", "Baguette", "Pita",
            "Curry", "Hummus", "Moussaka", "Couscous", "Kimchi",
            "Tempura", "Dim Sum", "Gyoza", "Burrito", "Enchilada",
            "Tiramisu", "Crepe", "Waffle",
            "Cheesecake", "Macarons", "Baklava", "Mochi", "Churros",
            "Crème Brûlée", "Brownie", "Donut", "Cannoli", "Profiteroles",
            "Pancakes", "Eggs Benedict", "Granola", "Acai Bowl", "Shakshuka",
            "Nachos", "Spring Rolls", "Samosa", "Poutine",
            "Fish and Chips", "Currywurst", "Takoyaki", "Elote", "Pupusas",
        ],
        "🌟 Famous People": [
            "Tom Hanks", "Meryl Streep", "Leonardo DiCaprio", "Scarlett Johansson", "Denzel Washington",
            "Brad Pitt", "Angelina Jolie", "Johnny Depp", "Natalie Portman", "Cate Blanchett",
            "Robert Downey Jr", "Chris Evans", "Margot Robbie", "Ryan Reynolds", "Dwayne Johnson",
            "Will Smith", "Morgan Freeman", "Samuel L. Jackson", "Jennifer Lawrence", "Emma Stone",
            "Steven Spielberg", "Christopher Nolan", "Quentin Tarantino", "Martin Scorsese", "Tim Burton",
            "Michael Jackson", "Madonna", "Beyoncé", "Taylor Swift", "Rihanna",
            "Eminem", "Drake", "Bad Bunny", "J Balvin", "Shakira",
            "Ed Sheeran", "Adele", "Lady Gaga", "Justin Bieber", "Billie Eilish",
            "The Weeknd", "Kanye West", "Jay-Z", "Ariana Grande", "Dua Lipa",
            "MrBeast", "PewDiePie", "Ibai", "Auronplay", "TheGrefg",
            "Ninja", "Pokimane", "xQc", "Rubius", "Vegetta777",
            "Elon Musk", "Jeff Bezos", "Mark Zuckerberg", "Steve Jobs", "Bill Gates",
        ],
        "🎬 Movies & Series": [
            "The Godfather", "Titanic", "Schindler's List", "Pulp Fiction", "Forrest Gump",
            "The Lion King", "The Matrix", "Gladiator", "Interstellar", "Inception",
            "The Lord of the Rings", "Star Wars", "Indiana Jones", "Jurassic Park", "Alien",
            "Terminator", "RoboCop", "Blade Runner", "2001 A Space Odyssey", "Psycho",
            "Avatar", "Avengers Endgame", "Spider-Man", "Batman", "Superman",
            "Black Panther", "Iron Man", "Doctor Strange", "Joker", "Oppenheimer",
            "Barbie", "Top Gun", "John Wick", "Everything Everywhere", "Get Out",
            "Breaking Bad", "Game of Thrones", "The Wire", "The Sopranos", "The Office",
            "Friends", "Seinfeld", "Lost", "24", "House of Cards",
            "Stranger Things", "Black Mirror", "Peaky Blinders", "Narcos", "Dexter",
            "The Crown", "Chernobyl", "Squid Game", "Dark", "Severance",
            "The Simpsons", "South Park", "Futurama", "Rick and Morty", "Bob's Burgers",
            "Avatar The Last Airbender", "Arcane", "Bojack Horseman", "Gravity Falls", "Steven Universe",
            "Walter White", "Tony Soprano", "Daenerys Targaryen", "Jon Snow", "Tyrion Lannister",
            "Hannibal Lecter", "James Bond", "Ellen Ripley", "The Joker",
        ],
        "💼 Professions": [
            "Doctor", "Nurse", "Surgeon", "Psychologist", "Dentist",
            "Veterinarian", "Pharmacist", "Physiotherapist", "Paramedic", "Nutritionist",
            "Programmer", "Web designer", "Software engineer", "Ethical hacker", "Data analyst",
            "Network admin", "Mobile developer", "DevOps",
            "Actor", "Film director", "Musician", "Photographer", "Illustrator",
            "Writer", "Journalist", "Graphic designer", "Animator", "Music producer",
            "Teacher", "Professor", "Scientist", "Archaeologist", "Astronomer",
            "Marine biologist", "Geologist", "Anthropologist", "Historian", "Philosopher",
            "Chef", "Firefighter", "Police officer", "Lawyer", "Judge",
            "Architect", "Pilot", "Astronaut", "Detective", "Diplomat",
            "Mechanic", "Electrician", "Carpenter", "Plumber", "Welder",
            "Soccer player", "Olympic athlete", "Personal trainer", "Referee", "Pro climber",
            "Diver", "Race car driver", "Jockey", "Pro surfer", "Boxer",
        ],
        "🎮 Video Games": [
            "Mario", "Link", "Master Chief", "Kratos", "Geralt of Rivia",
            "Lara Croft", "Nathan Drake", "Cloud Strife", "Solid Snake", "Samus Aran",
            "Sonic", "Pikachu", "Crash Bandicoot", "Spyro", "Mega Man",
            "Dante", "Ryu", "Sub-Zero", "Scorpion", "Kazuya Mishima",
            "Arthur Morgan", "Joel", "Ellie", "Aloy",
            "Minecraft", "Fortnite", "League of Legends", "Counter-Strike", "Valorant",
            "Grand Theft Auto", "Red Dead Redemption", "The Last of Us", "God of War", "Zelda",
            "Dark Souls", "Elden Ring", "Cyberpunk 2077", "The Witcher", "Skyrim",
            "Call of Duty", "Halo", "FIFA", "NBA 2K",
            "Among Us", "Rocket League", "Overwatch", "Apex Legends", "PUBG",
            "Resident Evil", "Silent Hill", "Bioshock", "Portal", "Half-Life",
            "Super Mario", "Pokemon", "Tetris", "Pac-Man", "Space Invaders",
            "Final Fantasy", "Dragon Quest", "Monster Hunter", "Street Fighter", "Mortal Kombat",
            "PlayStation", "Xbox", "Nintendo Switch", "Game Boy", "Atari",
            "Nintendo", "Sony", "Valve", "Rockstar Games",
            "Naughty Dog", "CD Projekt Red", "FromSoftware", "Blizzard", "Epic Games",
        ],
    }
}

ANTHROPIC_CLIENT = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# ══════════════════════════════════════════════════════════════
# HELPERS DE IDIOMA
# ══════════════════════════════════════════════════════════════

def normalizar(texto: str) -> str:
    import unicodedata
    texto = texto.lower().strip()
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    texto = texto.replace(" ", "")  # ignorar espacios accidentales
    return texto

def esc(text):
    chars = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in chars else c for c in str(text))

def esc_link(text):
    """Escape para texto dentro de [texto](url) en MarkdownV2."""
    # Dentro de [texto](url) hay que escapar todos los chars especiales de MarkdownV2
    chars = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in chars else c for c in str(text))

def nombre(user):
    return user.first_name or user.username or str(user.id)

def get_chat_key(update):
    chat = update.effective_chat
    chat_id = chat.id
    if getattr(chat, "is_forum", False) and update.effective_message:
        thread_id = update.effective_message.message_thread_id
        return f"{chat_id}_{thread_id}" if thread_id else str(chat_id)
    return str(chat_id)

def get_thread_id(chat_key: str):
    """Extrae el thread_id del chat_key si existe (formato 'chat_id_thread_id')."""
    parts = chat_key.split("_")
    if len(parts) == 2:
        try:
            return int(parts[1])
        except ValueError:
            pass
    return None

def calcular_num_impostores(num_jugadores):
    if num_jugadores <= 4:
        return 1
    elif num_jugadores <= 6:
        return random.randint(1, 3)
    else:
        return random.randint(2, 3)

def t(chat_key: str, key: str) -> str:
    """Obtiene el texto en el idioma configurado para el grupo."""
    lang = get_idioma(chat_key)
    return TEXTOS[lang][key]

def cats(chat_key: str) -> dict:
    """Devuelve las categorías en el idioma del grupo, incluyendo Personalizado si hay palabras."""
    lang = get_idioma(chat_key)
    categorias = dict(CATEGORIAS[lang])
    palabras_custom = get_palabras_custom(chat_key)
    if palabras_custom:
        nombre_cat = TEXTOS[lang]["cat_custom"]
        categorias[nombre_cat] = palabras_custom
    return categorias

def elegir_palabra(chat_key: str, categoria: str, palabras: list) -> str:
    """
    Elige una palabra evitando repetir las usadas recientemente.
    - Las últimas N palabras de esa categoría reciben peso 0 (excluidas).
    - N = min(mitad del pool, 10). Si todas fueron usadas, se resetea.
    - El resto tiene peso uniforme → igual probabilidad entre sí.
    """
    if len(palabras) == 1:
        return palabras[0]

    excluir_n = min(len(palabras) // 2, 10)

    with get_conn() as conn:
        recientes = conn.execute(
            """SELECT palabra FROM historial
               WHERE chat_key=? AND categoria=?
               ORDER BY fecha DESC LIMIT ?""",
            (chat_key, categoria, excluir_n)
        ).fetchall()

    usadas = {r[0] for r in recientes}
    candidatas = [p for p in palabras if p not in usadas]

    if not candidatas:
        candidatas = palabras  # todas usadas → resetear

    return random.choice(candidatas)


def generar_pistas(palabra: str, categoria: str, chat_key: str) -> str:
    lang = get_idioma(chat_key)
    prompt = TEXTOS[lang]["prompt_pistas"].format(palabra=palabra, categoria=categoria)
    fallback = TEXTOS[lang]["pistas_fallback"]
    try:
        response = ANTHROPIC_CLIENT.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text.strip()
    except Exception:
        return fallback


# ══════════════════════════════════════════════════════════════
# BASE DE DATOS
# ══════════════════════════════════════════════════════════════
DB_PATH = "/data/impostor.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS partidas (
            chat_key        TEXT PRIMARY KEY,
            chat_id         INTEGER,
            estado          TEXT DEFAULT 'esperando',
            categoria       TEXT,
            palabra         TEXT,
            impostor_ids    TEXT,
            vivos           TEXT,
            ronda           INTEGER DEFAULT 1,
            creador_id      INTEGER
        );
        CREATE TABLE IF NOT EXISTS jugadores (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_key        TEXT,
            user_id         INTEGER,
            username        TEXT,
            victorias       INTEGER DEFAULT 0,
            derrotas        INTEGER DEFAULT 0,
            veces_impostor  INTEGER DEFAULT 0,
            veces_inocente  INTEGER DEFAULT 0,
            victorias_impostor INTEGER DEFAULT 0,
            victorias_inocente INTEGER DEFAULT 0,
            UNIQUE(chat_key, user_id)
        );
        CREATE TABLE IF NOT EXISTS partida_jugadores (
            chat_key    TEXT,
            user_id     INTEGER,
            username    TEXT,
            PRIMARY KEY (chat_key, user_id)
        );
        CREATE TABLE IF NOT EXISTS historial (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_key    TEXT,
            ganador     TEXT,
            palabra     TEXT,
            categoria   TEXT,
            fecha       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS config (
            chat_key    TEXT PRIMARY KEY,
            idioma      TEXT DEFAULT 'es'
        );
        CREATE TABLE IF NOT EXISTS palabras_custom (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_key    TEXT,
            palabra     TEXT,
            UNIQUE(chat_key, palabra)
        );
        CREATE TABLE IF NOT EXISTS votos_historial (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_key    TEXT,
            voter_id    INTEGER,
            voted_id    INTEGER,
            fecha       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS programacion (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_key        TEXT,
            chat_id         INTEGER,
            thread_id       INTEGER,
            hora_inicio     INTEGER,
            puntos_victoria INTEGER DEFAULT 1,
            tz_offset       INTEGER DEFAULT 0,
            mensaje_id      INTEGER,
            estado          TEXT DEFAULT 'pendiente'
        );
        CREATE TABLE IF NOT EXISTS gi_grupos (
            chat_id     INTEGER PRIMARY KEY,
            chat_title  TEXT,
            chat_key    TEXT,
            ultimo_msg  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS gi_programacion (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            idol_name       TEXT,
            file_id         TEXT,
            file_id_reveal  TEXT,
            hint1           TEXT,
            hint2           TEXT,
            hint3           TEXT,
            inicio_ts       INTEGER,
            fin_ts          INTEGER,
            tz_offset       INTEGER DEFAULT 0,
            estado          TEXT DEFAULT 'pendiente'
        );
        CREATE TABLE IF NOT EXISTS gi_rondas (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            prog_id         INTEGER,
            chat_key        TEXT,
            chat_id         INTEGER,
            idol_name       TEXT,
            file_id         TEXT,
            file_id_reveal  TEXT,
            hint1           TEXT,
            hint2           TEXT,
            hint3           TEXT,
            inicio_ts       INTEGER,
            fin_ts          INTEGER,
            estado          TEXT DEFAULT 'activa',
            pistas_dadas    INTEGER DEFAULT 0,
            puntos_actuales INTEGER DEFAULT 5,
            ganador_id      INTEGER,
            ganador_nombre  TEXT,
            mensaje_id      INTEGER
        );
        CREATE TABLE IF NOT EXISTS gi_participantes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ronda_id    INTEGER,
            chat_key    TEXT,
            user_id     INTEGER,
            username    TEXT,
            vidas       INTEGER DEFAULT 5,
            activo      INTEGER DEFAULT 1,
            UNIQUE(ronda_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS gi_marcador (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_key    TEXT,
            user_id     INTEGER,
            username    TEXT,
            puntos      INTEGER DEFAULT 0,
            victorias   INTEGER DEFAULT 0,
            UNIQUE(chat_key, user_id)
        );
    """)
    conn.commit()
    # Migración: agregar columnas nuevas si no existen (DBs antiguas)
    for col, default in [("veces_impostor", 0), ("veces_inocente", 0), ("victorias_impostor", 0), ("victorias_inocente", 0)]:
        try:
            conn.execute(f"ALTER TABLE jugadores ADD COLUMN {col} INTEGER DEFAULT {default}")
            conn.commit()
        except Exception:
            pass  # Ya existe
    try:
        conn.execute("ALTER TABLE config ADD COLUMN timezone_offset INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE gi_grupos ADD COLUMN gi_activo INTEGER DEFAULT 1")
        conn.commit()
    except Exception:
        pass  # Ya existe
    # Asegurar que grupos existentes con gi_activo=NULL queden como activos
    try:
        conn.execute("UPDATE gi_grupos SET gi_activo=1 WHERE gi_activo IS NULL")
        conn.commit()
    except Exception:
        pass
    # Migración divisiones
    for sql in [
        "ALTER TABLE gi_marcador ADD COLUMN division INTEGER DEFAULT 1",
        "ALTER TABLE gi_marcador ADD COLUMN temporada INTEGER DEFAULT 1",
        "ALTER TABLE gi_marcador ADD COLUMN victorias_temp INTEGER DEFAULT 0",
        "ALTER TABLE gi_rondas ADD COLUMN division INTEGER DEFAULT 1",
        "ALTER TABLE gi_programacion ADD COLUMN division INTEGER DEFAULT 1",
        """CREATE TABLE IF NOT EXISTS gi_temporada (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_key TEXT,
            numero INTEGER DEFAULT 1,
            estado TEXT DEFAULT 'activa',
            UNIQUE(chat_key)
        )""",
    ]:
        try:
            conn.execute(sql)
            conn.commit()
        except Exception:
            pass
    # Sanear NULL en division/temporada de gi_marcador (registros anteriores a la migración)
    try:
        conn.execute("UPDATE gi_marcador SET division=1 WHERE division IS NULL")
        conn.execute("UPDATE gi_marcador SET temporada=1 WHERE temporada IS NULL")
        conn.execute("UPDATE gi_marcador SET victorias_temp=0 WHERE victorias_temp IS NULL")
        conn.commit()
    except Exception:
        pass
    conn.close()

def get_conn():
    return sqlite3.connect(DB_PATH)

def get_idioma(chat_key: str) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT idioma FROM config WHERE chat_key=?", (chat_key,)).fetchone()
    return row[0] if row else "es"

def set_idioma(chat_key: str, idioma: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO config (chat_key, idioma) VALUES (?,?)",
            (chat_key, idioma)
        )

def get_palabras_custom(chat_key: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT palabra FROM palabras_custom WHERE chat_key=? ORDER BY id",
            (chat_key,)
        ).fetchall()
    return [r[0] for r in rows]

def add_palabra_custom(chat_key: str, palabra: str) -> bool:
    """Retorna True si se agregó, False si ya existía."""
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO palabras_custom (chat_key, palabra) VALUES (?,?)",
                (chat_key, palabra)
            )
        return True
    except sqlite3.IntegrityError:
        return False

def remove_palabra_custom(chat_key: str, palabra: str) -> bool:
    """Retorna True si se eliminó, False si no existía."""
    with get_conn() as conn:
        rows = conn.execute(
            "DELETE FROM palabras_custom WHERE chat_key=? AND palabra=?",
            (chat_key, palabra)
        ).rowcount
    return rows > 0

def get_partida(chat_key):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM partidas WHERE chat_key=?", (chat_key,)).fetchone()

def get_jugadores_activos(chat_key):
    with get_conn() as conn:
        return conn.execute(
            "SELECT user_id, username FROM partida_jugadores WHERE chat_key=?", (chat_key,)
        ).fetchall()

def get_marcador(chat_key):
    with get_conn() as conn:
        return conn.execute(
            """SELECT j.user_id, j.username, j.victorias, j.derrotas
               FROM jugadores j
               INNER JOIN partida_jugadores pj ON j.chat_key = pj.chat_key AND j.user_id = pj.user_id
               WHERE j.chat_key=? ORDER BY (j.victorias - j.derrotas) DESC, j.victorias DESC""",
            (chat_key,)
        ).fetchall()

def get_marcador_global(chat_key):
    with get_conn() as conn:
        return conn.execute(
            "SELECT user_id, username, victorias, derrotas FROM jugadores WHERE chat_key=? AND (victorias > 0 OR derrotas > 0) ORDER BY (victorias - derrotas) DESC, victorias DESC",
            (chat_key,)
        ).fetchall()

def upsert_jugador(chat_key, user_id, username):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO jugadores (chat_key, user_id, username) VALUES (?,?,?)",
            (chat_key, user_id, username)
        )
        conn.execute(
            "UPDATE jugadores SET username=? WHERE chat_key=? AND user_id=?",
            (username, chat_key, user_id)
        )

def agregar_jugador_activo(chat_key, user_id, username):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO partida_jugadores (chat_key, user_id, username) VALUES (?,?,?)",
            (chat_key, user_id, username)
        )

def actualizar_nombre_activo(chat_key, user_id, username):
    """Actualiza el nombre del jugador activo si cambió en Telegram."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE partida_jugadores SET username=? WHERE chat_key=? AND user_id=?",
            (username, chat_key, user_id)
        )
        conn.execute(
            "UPDATE jugadores SET username=? WHERE chat_key=? AND user_id=?",
            (username, chat_key, user_id)
        )

def limpiar_jugadores_activos(chat_key):
    with get_conn() as conn:
        conn.execute("DELETE FROM partida_jugadores WHERE chat_key=?", (chat_key,))

def sumar_victoria(chat_key, user_id):
    with get_conn() as conn:
        conn.execute(
            "UPDATE jugadores SET victorias = victorias + 1 WHERE chat_key=? AND user_id=?",
            (chat_key, user_id)
        )

def sumar_derrota(chat_key, user_id):
    with get_conn() as conn:
        conn.execute(
            "UPDATE jugadores SET derrotas = derrotas + 1 WHERE chat_key=? AND user_id=?",
            (chat_key, user_id)
        )

def sumar_vez_impostor(chat_key, user_id):
    with get_conn() as conn:
        conn.execute(
            "UPDATE jugadores SET veces_impostor = veces_impostor + 1 WHERE chat_key=? AND user_id=?",
            (chat_key, user_id)
        )

def sumar_vez_inocente(chat_key, user_id):
    with get_conn() as conn:
        conn.execute(
            "UPDATE jugadores SET veces_inocente = veces_inocente + 1 WHERE chat_key=? AND user_id=?",
            (chat_key, user_id)
        )

def sumar_victoria_impostor(chat_key, user_id):
    with get_conn() as conn:
        conn.execute(
            "UPDATE jugadores SET victorias_impostor = victorias_impostor + 1 WHERE chat_key=? AND user_id=?",
            (chat_key, user_id)
        )

def sumar_victoria_inocente(chat_key, user_id):
    with get_conn() as conn:
        conn.execute(
            "UPDATE jugadores SET victorias_inocente = victorias_inocente + 1 WHERE chat_key=? AND user_id=?",
            (chat_key, user_id)
        )

def get_vivos(chat_key):
    with get_conn() as conn:
        row = conn.execute("SELECT vivos FROM partidas WHERE chat_key=?", (chat_key,)).fetchone()
    if not row or not row[0]:
        return []
    return [int(i) for i in row[0].split(",")]

def set_vivos(chat_key, vivos_ids):
    with get_conn() as conn:
        conn.execute(
            "UPDATE partidas SET vivos=? WHERE chat_key=?",
            (",".join(str(i) for i in vivos_ids), chat_key)
        )

def eliminar_de_vivos(chat_key, user_id):
    vivos = get_vivos(chat_key)
    vivos = [v for v in vivos if v != user_id]
    set_vivos(chat_key, vivos)
    return vivos


# ══════════════════════════════════════════════════════════════
# COMANDOS
# ══════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════
# PROGRAMACIÓN DE PARTIDAS
# ══════════════════════════════════════════════════════════════

BOT_OWNER_ID = int(os.environ.get("BOT_OWNER_ID", "0"))

from datetime import datetime, timezone as _tz, timedelta

def _parse_fecha_hora(texto: str, tz_offset: int):
    """Parsea fecha+hora: 'DD/MM HH:MM', 'DD/MM/YYYY HH:MM' o solo 'HH:MM' (asume hoy/mañana).
    Retorna timestamp UTC o None si formato inválido.
    """
    import re as _re
    texto = texto.strip()
    local_obj = _tz(timedelta(hours=tz_offset))
    now_local = datetime.now(_tz.utc).astimezone(local_obj)
    try:
        # Formato DD/MM HH:MM o DD/MM/YYYY HH:MM
        m = _re.match(r'^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\s+(\d{1,2}):(\d{2})$', texto)
        if m:
            day, mon = int(m.group(1)), int(m.group(2))
            yr_raw = m.group(3)
            yr = int(yr_raw) if yr_raw else now_local.year
            if yr < 100: yr += 2000
            h, mi = int(m.group(4)), int(m.group(5))
            if not (1<=day<=31 and 1<=mon<=12 and 0<=h<=23 and 0<=mi<=59):
                return None
            sched = now_local.replace(year=yr, month=mon, day=day, hour=h, minute=mi, second=0, microsecond=0)
            return int(sched.astimezone(_tz.utc).timestamp())
        # Formato solo HH:MM → asume hoy o mañana
        m2 = _re.match(r'^(\d{1,2}):(\d{2})$', texto)
        if m2:
            h, mi = int(m2.group(1)), int(m2.group(2))
            if not (0<=h<=23 and 0<=mi<=59):
                return None
            sched = now_local.replace(hour=h, minute=mi, second=0, microsecond=0)
            if sched <= now_local:
                sched += timedelta(days=1)
            return int(sched.astimezone(_tz.utc).timestamp())
        return None
    except Exception:
        return None


def _parse_hora(hora_str: str, tz_offset: int):
    try:
        hora_str = hora_str.strip()
        h, m = hora_str.split(":", 1)
        h, m = int(h), int(m)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        now_utc   = datetime.now(_tz.utc)
        local_obj = _tz(timedelta(hours=tz_offset))
        now_local = now_utc.astimezone(local_obj)
        sched     = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
        if sched <= now_local:
            sched += timedelta(days=1)
        return int(sched.astimezone(_tz.utc).timestamp())
    except Exception:
        return None

def _formato_fecha_hora_local(ts: int, tz_offset: int) -> str:
    """Formatea timestamp como 'DD/MM HH:MM (UTCx)'."""
    dt    = datetime.fromtimestamp(ts, tz=_tz.utc)
    local = dt + timedelta(hours=tz_offset)
    if tz_offset == 0:   tz_label = "UTC"
    elif tz_offset > 0:  tz_label = f"UTC+{tz_offset}"
    else:                tz_label = f"UTC{tz_offset}"
    return local.strftime(f"%d/%m %H:%M ({tz_label})")


def _formato_hora_local(ts: int, tz_offset: int) -> str:
    dt    = datetime.fromtimestamp(ts, tz=_tz.utc)
    local = dt + timedelta(hours=tz_offset)
    if tz_offset == 0:   tz_label = "UTC"
    elif tz_offset > 0:  tz_label = f"UTC+{tz_offset}"
    else:                tz_label = f"UTC{tz_offset}"
    return local.strftime(f"%H:%M ({tz_label})")

def _formato_countdown(segundos: int, lang: str = "es") -> str:
    if segundos <= 0:
        return "¡Ahora!" if lang == "es" else "Now!"
    horas   = segundos // 3600
    minutos = (segundos % 3600) // 60
    if lang == "es":
        return f"{horas}h {minutos}min" if horas > 0 else f"{minutos} minutos"
    return f"{horas}h {minutos}min" if horas > 0 else f"{minutos} minutes"

def _tz_label(offset: int) -> str:
    if offset == 0:   return "UTC"
    if offset > 0:    return f"UTC+{offset}"
    return f"UTC{offset}"

def get_programa_pendiente(chat_key: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM programacion WHERE chat_key=? AND estado='pendiente' ORDER BY id DESC LIMIT 1",
            (chat_key,)
        ).fetchone()

def cancelar_programas_db(chat_key: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE programacion SET estado='cancelada' WHERE chat_key=? AND estado='pendiente'",
            (chat_key,)
        )

def _build_programa_setup_text(chat_key: str, setup: dict):
    lang   = get_idioma(chat_key)
    hora_ts = setup.get("hora_inicio")
    tz     = setup.get("tz_offset", 0)
    puntos = setup.get("puntos", 1)
    tz_lbl = _tz_label(tz)
    hora_str = _formato_hora_local(hora_ts, tz) if hora_ts else ("No configurada" if lang == "es" else "Not set")
    pts_str  = f"{puntos} punto{'s' if puntos > 1 else ''}" if lang == "es" else f"{puntos} point{'s' if puntos > 1 else ''}"

    if lang == "es":
        text = (
            "📅 *Programar partida*\n\n"
            f"⏰ Hora de inicio: *{esc(hora_str)}*\n"
            f"🏆 Victorias valen: *{esc(pts_str)}*\n"
            f"🌐 Zona horaria: *{esc(tz_lbl)}*\n\n"
            "_Configura las opciones y pulsa Programar_"
        )
        btn_confirmar = "✅ Programar"
        btn_cancelar  = "❌ Cancelar"
        btn_hora      = "⏰ Configurar hora de inicio"
        btn_puntos    = f"🏆 {'2 pts → cambiar a 1' if puntos == 2 else '1 pt → cambiar a 2'}"
    else:
        text = (
            "📅 *Schedule game*\n\n"
            f"⏰ Start time: *{esc(hora_str)}*\n"
            f"🏆 Wins worth: *{esc(pts_str)}*\n"
            f"🌐 Timezone: *{esc(tz_lbl)}*\n\n"
            "_Configure options and press Schedule_"
        )
        btn_confirmar = "✅ Schedule"
        btn_cancelar  = "❌ Cancel"
        btn_hora      = "⏰ Set start time"
        btn_puntos    = f"🏆 {'2pts → change to 1' if puntos == 2 else '1pt → change to 2'}"

    keyboard = [
        [InlineKeyboardButton(btn_hora, callback_data="programa:hora")],
        [
            InlineKeyboardButton(f"◀ {_tz_label(max(-12, tz-1))}", callback_data="programa:tz:-1"),
            InlineKeyboardButton(f"🌐 {tz_lbl}", callback_data="programa:tz:0"),
            InlineKeyboardButton(f"{_tz_label(min(14, tz+1))} ▶", callback_data="programa:tz:+1"),
        ],
        [InlineKeyboardButton(btn_puntos, callback_data="programa:puntos")],
        [
            InlineKeyboardButton(btn_confirmar, callback_data="programa:confirmar"),
            InlineKeyboardButton(btn_cancelar,  callback_data="programa:cancelar"),
        ],
    ]
    return text, keyboard

def _build_countdown_text(chat_key: str, hora_ts: int, puntos: int, tz_offset: int, segundos: int) -> str:
    lang      = get_idioma(chat_key)
    hora_str  = _formato_hora_local(hora_ts, tz_offset)
    countdown = _formato_countdown(segundos, lang)
    if lang == "es":
        pts_desc = f"victorias valen *{puntos} {'punto' if puntos == 1 else 'puntos'}*"
        return (
            "🎮 *¡PARTIDA PROGRAMADA\\!*\n\n"
            f"🕐 Inicio: *{esc(hora_str)}*\n"
            f"⏱️ Faltan: *{esc(countdown)}*\n"
            f"🏆 Las {pts_desc}\n\n"
            "_Usa /join cuando empiece para sumarte_"
        )
    pts_desc = f"wins worth *{puntos} {'point' if puntos == 1 else 'points'}*"
    return (
        "🎮 *GAME SCHEDULED\\!*\n\n"
        f"🕐 Start: *{esc(hora_str)}*\n"
        f"⏱️ Time left: *{esc(countdown)}*\n"
        f"🏆 {pts_desc}\n\n"
        "_Use /join when it starts to join_"
    )

async def _task_programa_countdown(chat_key: str, prog_id: int, bot, bot_data: dict):
    """Actualiza el countdown cada hora y lanza la partida a tiempo."""
    try:
        while True:
            with get_conn() as conn:
                prog = conn.execute(
                    "SELECT * FROM programacion WHERE id=? AND estado='pendiente'",
                    (prog_id,)
                ).fetchone()
            if not prog:
                return
            hora_ts    = prog[4]
            puntos     = prog[5]
            tz_offset  = prog[6] if prog[6] is not None else 0
            chat_id    = prog[2]
            thread_id  = prog[3]
            mensaje_id = prog[7]
            now_ts     = int(datetime.now(_tz.utc).timestamp())
            restantes  = hora_ts - now_ts

            if restantes <= 60:
                with get_conn() as conn:
                    conn.execute("UPDATE programacion SET estado='activa' WHERE id=?", (prog_id,))
                await _iniciar_partida_programada(
                    chat_key, chat_id, thread_id, puntos, tz_offset, mensaje_id, bot, bot_data
                )
                bot_data.pop(f"programa_tarea_{prog_id}", None)
                return

            if mensaje_id:
                try:
                    lang       = get_idioma(chat_key)
                    cancel_btn = "❌ Cancelar partida" if lang == "es" else "❌ Cancel game"
                    kbd        = [[InlineKeyboardButton(cancel_btn, callback_data=f"programa:cancelar_prog:{prog_id}")]]
                    texto      = _build_countdown_text(chat_key, hora_ts, puntos, tz_offset, restantes)

                    if restantes <= 300:
                        await bot.edit_message_text(
                            texto, chat_id=chat_id, message_id=mensaje_id,
                            parse_mode="MarkdownV2", reply_markup=InlineKeyboardMarkup(kbd)
                        )
                    else:
                        try:
                            await bot.delete_message(chat_id=chat_id, message_id=mensaje_id)
                        except Exception:
                            pass
                        nuevo_msg = await bot.send_message(
                            chat_id, texto, parse_mode="MarkdownV2",
                            reply_markup=InlineKeyboardMarkup(kbd),
                            message_thread_id=thread_id
                        )
                        mensaje_id = nuevo_msg.message_id
                        with get_conn() as conn:
                            conn.execute(
                                "UPDATE programacion SET mensaje_id=? WHERE id=?",
                                (mensaje_id, prog_id)
                            )

                except Exception as e:
                    logger.warning(f"[PROGRAMA] Error actualizando countdown {chat_key}: {e}")

            # Dormir el tiempo justo: despertarse 60s antes de la hora
            if restantes <= 300:
                await asyncio.sleep(60)
            else:
                await asyncio.sleep(min(3600, restantes - 60))

    except asyncio.CancelledError:
        logger.info(f"[PROGRAMA] Countdown {prog_id} cancelado")
    except Exception as e:
        logger.error(f"[PROGRAMA] Error en countdown {chat_key}: {e}", exc_info=True)


async def _iniciar_partida_programada(chat_key: str, chat_id: int, thread_id,
                                       puntos: int, tz_offset: int, mensaje_id,
                                       bot, bot_data: dict):
    """Crea el lobby de la partida programada y anuncia en el grupo."""
    partida = get_partida(chat_key)
    if partida and partida[2] not in ("terminada",):
        logger.warning(f"[PROGRAMA] Partida ya activa en {chat_key}, omitiendo inicio programado")
        return
    limpiar_jugadores_activos(chat_key)
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO partidas (chat_key, chat_id, estado, creador_id, ronda) VALUES (?,?,?,?,1)",
            (chat_key, chat_id, "esperando", 0)
        )
    if puntos > 1:
        bot_data[f"multiplicador_{chat_key}"] = puntos
    lang = get_idioma(chat_key)
    if lang == "es":
        pts_txt = f"victorias valen *{puntos} {'punto' if puntos == 1 else 'puntos'}*"
        texto   = (
            "🎮 *¡HORA DE JUGAR\\!*\n\n"
            f"La partida programada *acaba de abrirse*\\. Las {pts_txt}\\.\n\n"
            "Pulsen el botón o usen /join para unirse\\."
        )
        btn_txt = "✋ Unirse a la partida"
    else:
        pts_txt = f"wins worth *{puntos} {'point' if puntos == 1 else 'points'}*"
        texto   = (
            "🎮 *TIME TO PLAY\\!*\n\n"
            f"The scheduled game *just opened*\\. {pts_txt}\\.\n\n"
            "Press the button or use /join to join\\."
        )
        btn_txt = "✋ Join the game"
    keyboard = [[InlineKeyboardButton(btn_txt, callback_data="unirse")]]
    lobby_msg_id = None
    if mensaje_id:
        try:
            msg = await bot.edit_message_text(
                texto, chat_id=chat_id, message_id=mensaje_id,
                parse_mode="MarkdownV2", reply_markup=InlineKeyboardMarkup(keyboard)
            )
            lobby_msg_id = mensaje_id
        except Exception:
            sent = await bot.send_message(chat_id, texto, parse_mode="MarkdownV2",
                                   reply_markup=InlineKeyboardMarkup(keyboard),
                                   message_thread_id=thread_id)
            lobby_msg_id = sent.message_id
    else:
        sent = await bot.send_message(chat_id, texto, parse_mode="MarkdownV2",
                               reply_markup=InlineKeyboardMarkup(keyboard),
                               message_thread_id=thread_id)
        lobby_msg_id = sent.message_id

    # Lanzar timeout de 2 minutos para cancelar si nadie inicia
    tarea = asyncio.create_task(
        _timeout_lobby_programado(chat_key, chat_id, thread_id, lobby_msg_id, bot, bot_data)
    )
    bot_data[f"timeout_lobby_{chat_key}"] = tarea
    logger.info(f"[PROGRAMA] Partida programada iniciada en {chat_key}")


TIMEOUT_LOBBY_SEGUNDOS = 120  # 2 minutos

async def _timeout_lobby_programado(chat_key: str, chat_id: int, thread_id,
                                     mensaje_id, bot, bot_data: dict):
    """Al cumplirse 2 minutos sin iniciar:
    - Con ≥3 jugadores → inicia automáticamente (el primer jugador que se unió queda como creador)
    - Con <3 jugadores → cancela automáticamente
    """
    try:
        await asyncio.sleep(TIMEOUT_LOBBY_SEGUNDOS)

        partida = get_partida(chat_key)
        if not partida or partida[2] != "esperando":
            return  # Ya se inició o canceló por otro medio

        jugadores = get_jugadores_activos(chat_key)
        lang = get_idioma(chat_key)

        # ── Menos de 3 jugadores → cancelar ──────────────────────
        if len(jugadores) < 3:
            with get_conn() as conn:
                conn.execute(
                    "UPDATE partidas SET estado='terminada' WHERE chat_key=? AND estado='esperando'",
                    (chat_key,)
                )
            bot_data.pop(f"multiplicador_{chat_key}", None)

            if lang == "es":
                texto_exp = (
                    "⏰ *Partida expirada*\n\n"
                    f"Solo había *{len(jugadores)}* jugador{'es' if len(jugadores) != 1 else ''} "
                    "y la partida necesita al menos 3\\. "
                    "Fue cancelada automáticamente\\."
                )
            else:
                texto_exp = (
                    "⏰ *Game expired*\n\n"
                    f"Only *{len(jugadores)}* player{'s' if len(jugadores) != 1 else ''} "
                    "joined and the game needs at least 3\\. "
                    "It was automatically cancelled\\."
                )

            # Enviar aviso de expiración
            await bot.send_message(chat_id, texto_exp, parse_mode="MarkdownV2",
                                   message_thread_id=thread_id)
            # Intentar quitar botones del mensaje del lobby original
            if mensaje_id:
                try:
                    await bot.edit_message_reply_markup(
                        chat_id=chat_id, message_id=mensaje_id, reply_markup=None
                    )
                except Exception:
                    pass

            logger.info(f"[TIMEOUT] Lobby cancelado por pocos jugadores ({len(jugadores)}) en {chat_key}")
            return

        # ── 3 o más jugadores → iniciar automáticamente ──────────
        # El creador pasa a ser el primer jugador que se unió
        primer_jugador = jugadores[0]
        with get_conn() as conn:
            conn.execute(
                "UPDATE partidas SET creador_id=? WHERE chat_key=?",
                (primer_jugador[0], chat_key)
            )

        if lang == "es":
            aviso_auto = (
                f"⏰ *¡Tiempo\\!* La partida inicia automáticamente con *{len(jugadores)}* jugadores\\.\n"
                f"👑 *{esc(primer_jugador[1])}* es el creador y puede abrir la votación\\."
            )
        else:
            aviso_auto = (
                f"⏰ *Time's up\\!* Game starts automatically with *{len(jugadores)}* players\\.\n"
                f"👑 *{esc(primer_jugador[1])}* is the creator and can open voting\\."
            )

        await bot.send_message(chat_id, aviso_auto, parse_mode="MarkdownV2",
                               message_thread_id=thread_id)

        # Elegir categoría aleatoria y lanzar la partida
        categorias_disp = cats(chat_key)
        categoria = random.choice(list(categorias_disp.keys()))
        palabra   = elegir_palabra(chat_key, categoria, categorias_disp[categoria])

        num_impostores   = calcular_num_impostores(len(jugadores))
        impostores       = random.sample(jugadores, num_impostores)
        impostor_ids     = ",".join(str(i[0]) for i in impostores)
        impostor_ids_set = set(i[0] for i in impostores)
        vivos_ids        = ",".join(str(j[0]) for j in jugadores)

        with get_conn() as conn:
            conn.execute(
                "UPDATE partidas SET estado='jugando', categoria=?, palabra=?, impostor_ids=?, vivos=? WHERE chat_key=?",
                (categoria, palabra, impostor_ids, vivos_ids, chat_key)
            )

        pistas_raw = generar_pistas(palabra, categoria, chat_key)
        pistas     = "\n".join(esc(l) for l in pistas_raw.splitlines())

        lang_cat = esc(categoria)
        if lang == "es":
            texto_cat = f"Categoría: *{lang_cat}*"
        else:
            texto_cat = f"Category: *{lang_cat}*"

        fallidos = []
        for uid, uname in jugadores:
            try:
                if uid in impostor_ids_set:
                    msg_privado = t(chat_key, "eres_impostor").format(cat=esc(categoria))
                    sumar_vez_impostor(chat_key, uid)
                else:
                    msg_privado = t(chat_key, "eres_inocente").format(
                        palabra=esc(palabra), cat=esc(categoria), pistas=pistas
                    )
                    sumar_vez_inocente(chat_key, uid)
                await bot.send_message(uid, msg_privado, parse_mode="MarkdownV2")
            except Exception:
                fallidos.append(uname)

        orden = list(jugadores)
        random.shuffle(orden)
        turno_lista = "\n".join(f"  {i+1}\\. {esc(j[1])}" for i, j in enumerate(orden))

        bot_data[f"turno_{chat_key}"] = {
            "orden": [j[0] for j in orden],
            "index": 0,
            "ya_dieron_pista": set(),
            "ronda_pistas": 1,
            "jugadores_iniciales": len(jugadores),
            "intentos_pista": {}
        }

        aviso_rondas = (
            t(chat_key, "aviso_2rondas") if len(jugadores) == 3
            else t(chat_key, "aviso_votar")
        )
        aviso_fallidos = ""
        if fallidos:
            aviso_fallidos = t(chat_key, "aviso_fallidos").format(
                nombres=", ".join(esc(f) for f in fallidos)
            )

        await bot.send_message(
            chat_id,
            t(chat_key, "partida_comienza").format(
                cat=texto_cat, orden=turno_lista, aviso_rondas=aviso_rondas
            ) + aviso_fallidos,
            parse_mode="MarkdownV2",
            message_thread_id=thread_id
        )

        primer = orden[0]
        # Crear un contexto mínimo para _anunciar_turno
        class _FakeCtx:
            def __init__(self, b, bd):
                self.bot = b
                self.bot_data = bd
        fake_ctx = _FakeCtx(bot, bot_data)
        await _anunciar_turno(chat_key, primer[0], primer[1], chat_id, thread_id, fake_ctx)
        logger.info(f"[TIMEOUT] Partida auto-iniciada con {len(jugadores)} jugadores en {chat_key}")

    except asyncio.CancelledError:
        pass  # Se inició o canceló manualmente a tiempo
    except Exception as e:
        logger.error(f"[TIMEOUT] Error en timeout lobby {chat_key}: {e}", exc_info=True)





async def cmd_program(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_key = get_chat_key(update)
    user     = update.effective_user
    chat     = update.effective_chat
    es_owner = bool(BOT_OWNER_ID and user.id == BOT_OWNER_ID)
    try:
        member   = await chat.get_member(user.id)
        es_admin = member.status in ("administrator", "creator")
    except Exception:
        es_admin = False
    if not es_owner and not es_admin:
        lang = get_idioma(chat_key)
        msg  = ("⚠️ Solo el creador del bot o los administradores pueden programar partidas."
                if lang == "es" else
                "⚠️ Only the bot creator or group admins can schedule games.")
        await update.message.reply_text(msg)
        return
    setup = {
        "hora_inicio": None, "puntos": 1, "tz_offset": 0,
        "esperando_hora": False, "admin_id": user.id, "mensaje_setup_id": None,
    }
    ctx.bot_data[f"programa_setup_{chat_key}"] = setup
    text, keyboard = _build_programa_setup_text(chat_key, setup)
    msg = await update.message.reply_text(
        text, parse_mode="MarkdownV2", reply_markup=InlineKeyboardMarkup(keyboard)
    )
    setup["mensaje_setup_id"] = msg.message_id


async def btn_programa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    chat_key = get_chat_key(update)
    user     = query.from_user
    lang     = get_idioma(chat_key)

    if query.data == "programa:cancelar_prog":
        es_owner = bool(BOT_OWNER_ID and user.id == BOT_OWNER_ID)
        try:
            member   = await update.effective_chat.get_member(user.id)
            es_admin = member.status in ("administrator", "creator")
        except Exception:
            es_admin = False
        if not es_owner and not es_admin:
            await query.answer("⚠️ Solo admins pueden cancelar.", show_alert=True)
            return
        cb_parts = query.data.split(":")
        if len(cb_parts) >= 3:
            try:
                prog_id_c = int(cb_parts[2])
                t_c = ctx.bot_data.pop(f"programa_tarea_{prog_id_c}", None)
                if t_c: t_c.cancel()
                with get_conn() as conn:
                    conn.execute("UPDATE programacion SET estado='cancelada' WHERE id=?", (prog_id_c,))
            except (ValueError, IndexError):
                cancelar_programas_db(chat_key)
        else:
            tarea_old = ctx.bot_data.pop(f"programa_tarea_{chat_key}", None)
            if tarea_old: tarea_old.cancel()
            cancelar_programas_db(chat_key)
        await query.answer()
        cancel_txt = "❌ *Partida programada cancelada\\.*" if lang == "es" else "❌ *Scheduled game cancelled\\.*"
        try:
            await query.message.edit_text(cancel_txt, parse_mode="MarkdownV2")
        except Exception:
            pass
        return

    setup = ctx.bot_data.get(f"programa_setup_{chat_key}")
    if not setup:
        await query.answer(
            "Setup expirado. Usa /program de nuevo." if lang == "es" else "Setup expired. Use /program again.",
            show_alert=True
        )
        return
    if user.id != setup.get("admin_id"):
        await query.answer(
            "Solo quien usó /program puede configurar esto." if lang == "es" else "Only who used /program can configure this.",
            show_alert=True
        )
        return

    parts  = query.data.split(":")
    action = parts[1]

    if action == "hora":
        setup["esperando_hora"] = True
        alert = ("Escribe la hora en formato HH:MM en el chat (ej: 20:00)"
                 if lang == "es" else "Type the time in HH:MM format in the chat (eg: 20:00)")
        await query.answer(alert, show_alert=True)
        return

    elif action == "puntos":
        setup["puntos"] = 2 if setup.get("puntos", 1) == 1 else 1
        await query.answer()

    elif action == "tz":
        delta = int(parts[2])
        setup["tz_offset"] = max(-12, min(14, setup.get("tz_offset", 0) + delta))
        await query.answer()

    elif action == "confirmar":
        if not setup.get("hora_inicio"):
            err = ("⚠️ Debes configurar la hora de inicio primero."
                   if lang == "es" else "⚠️ You must set the start time first.")
            await query.answer(err, show_alert=True)
            return
        chat_id   = update.effective_chat.id
        thread_id = get_thread_id(chat_key)
        hora_ts   = setup["hora_inicio"]
        puntos    = setup.get("puntos", 1)
        tz        = setup.get("tz_offset", 0)
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO programacion (chat_key, chat_id, thread_id, hora_inicio, puntos_victoria, tz_offset, estado) "
                "VALUES (?,?,?,?,?,?,'pendiente')",
                (chat_key, chat_id, thread_id, hora_ts, puntos, tz)
            )
            prog_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass
        now_ts    = int(datetime.now(_tz.utc).timestamp())
        restantes = max(0, hora_ts - now_ts)
        texto_cd  = _build_countdown_text(chat_key, hora_ts, puntos, tz, restantes)
        cancel_btn = "❌ Cancelar partida" if lang == "es" else "❌ Cancel game"
        kbd_cd     = [[InlineKeyboardButton(cancel_btn, callback_data=f"programa:cancelar_prog:{prog_id}")]]
        msg_cd = await ctx.bot.send_message(
            chat_id, texto_cd, parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup(kbd_cd), message_thread_id=thread_id
        )
        with get_conn() as conn:
            conn.execute("UPDATE programacion SET mensaje_id=? WHERE id=?", (msg_cd.message_id, prog_id))
        tarea = asyncio.create_task(
            _task_programa_countdown(chat_key, prog_id, ctx.bot, ctx.bot_data)
        )
        ctx.bot_data[f"programa_tarea_{prog_id}"] = tarea
        ctx.bot_data.pop(f"programa_setup_{chat_key}", None)
        return

    elif action == "cancelar":
        ctx.bot_data.pop(f"programa_setup_{chat_key}", None)
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    text, keyboard = _build_programa_setup_text(chat_key, setup)
    try:
        await query.message.edit_text(
            text, parse_mode="MarkdownV2", reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception:
        pass


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_key = get_chat_key(update)
    chat = update.effective_chat
    # En chat privado: mensaje de "listo, vuelve al grupo"
    if chat.type == "private":
        await update.message.reply_text(t(chat_key, "start_privado"), parse_mode="MarkdownV2")
        return
    # En grupo: mensaje normal de bienvenida
    await update.message.reply_text(t(chat_key, "start"), parse_mode="MarkdownV2")


async def cmd_como_jugar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_key = get_chat_key(update)
    await update.message.reply_text(t(chat_key, "comojugar"), parse_mode="MarkdownV2")


async def cmd_idioma(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_key = get_chat_key(update)
    user = update.effective_user
    chat = update.effective_chat

    es_owner = bool(BOT_OWNER_ID and user.id == BOT_OWNER_ID)
    try:
        member = await chat.get_member(user.id)
        es_admin = member.status in ("administrator", "creator")
    except Exception:
        es_admin = False

    if not es_owner and not es_admin:
        await update.message.reply_text(t(chat_key, "solo_admin_idioma"))
        return

    lang = get_idioma(chat_key)
    keyboard = [[
        InlineKeyboardButton(t(chat_key, "btn_es"), callback_data="idioma:es"),
        InlineKeyboardButton(t(chat_key, "btn_en"), callback_data="idioma:en"),
    ]]
    await update.message.reply_text(
        t(chat_key, "idioma_actual"),
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def btn_idioma(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_key = get_chat_key(update)
    user = update.effective_user

    es_owner = bool(BOT_OWNER_ID and user.id == BOT_OWNER_ID)
    try:
        member = await update.effective_chat.get_member(user.id)
        es_admin = member.status in ("administrator", "creator")
    except Exception:
        es_admin = False

    if not es_owner and not es_admin:
        await query.answer(t(chat_key, "solo_admin_idioma"), show_alert=True)
        return

    nuevo_idioma = query.data.split(":")[1]
    set_idioma(chat_key, nuevo_idioma)
    await query.answer()

    key = "idioma_cambiado_es" if nuevo_idioma == "es" else "idioma_cambiado_en"
    await query.edit_message_text(t(chat_key, key), parse_mode="MarkdownV2")


async def cmd_nueva(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_key = get_chat_key(update)
    chat_id = update.effective_chat.id
    user = update.effective_user

    partida = get_partida(chat_key)
    if partida and partida[2] not in ("terminada",):
        await update.message.reply_text(t(chat_key, "partida_activa"))
        return

    # Cancelar timeout anterior si existía
    tarea_anterior = ctx.bot_data.pop(f"timeout_lobby_{chat_key}", None)
    if tarea_anterior:
        tarea_anterior.cancel()

    limpiar_jugadores_activos(chat_key)

    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO partidas (chat_key, chat_id, estado, creador_id, ronda) VALUES (?,?,?,?,1)",
            (chat_key, chat_id, "esperando", user.id)
        )

    upsert_jugador(chat_key, user.id, nombre(user))
    agregar_jugador_activo(chat_key, user.id, nombre(user))

    keyboard = [[InlineKeyboardButton(t(chat_key, "btn_unirse"), callback_data="unirse")]]
    msg = await update.message.reply_text(
        t(chat_key, "nueva_partida").format(nombre=esc(nombre(user))),
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    # Lanzar timeout de 2 minutos
    thread_id = get_thread_id(chat_key)
    tarea = asyncio.create_task(
        _timeout_lobby_programado(chat_key, chat_id, thread_id, msg.message_id, ctx.bot, ctx.bot_data)
    )
    ctx.bot_data[f"timeout_lobby_{chat_key}"] = tarea


async def cmd_unirse(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_key = get_chat_key(update)
    user = update.effective_user

    # Verificar que el bot esté iniciado (igual que btn_unirse)
    try:
        await ctx.bot.send_chat_action(user.id, "typing")
    except Exception:
        bot_username = (await ctx.bot.get_me()).username
        deep_link = "https://t.me/" + bot_username + "?start=join"
        await update.message.reply_text(
            t(chat_key, "bot_no_iniciado") + "\n\n👉 " + deep_link
        )
        return

    await _unirse(chat_key, user, update.message.reply_text, ctx.bot)

async def btn_unirse(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_key = get_chat_key(update)
    user = update.effective_user

    # Verificar que el bot esté iniciado intentando enviar un mensaje de prueba
    try:
        await ctx.bot.send_chat_action(user.id, "typing")
    except Exception:
        # No tiene el bot iniciado → alerta flotante con deep link
        bot_username = (await ctx.bot.get_me()).username
        await query.answer(
            t(chat_key, "bot_no_iniciado"),
            show_alert=True,
            url=f"https://t.me/{bot_username}?start=join"
        )
        return

    # Verificar condiciones de error ANTES de llamar answer()
    # (Telegram solo permite una llamada a answer() por callback)
    partida = get_partida(chat_key)
    logger.info(f"[btn_unirse] user={user.id} partida_estado={partida[2] if partida else None} activos={[j[0] for j in get_jugadores_activos(chat_key)]}")
    if not partida or partida[2] == "terminada":
        await query.answer(t(chat_key, "sin_partida"), show_alert=True)
        # Intentar quitar los botones del mensaje expirado
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    if partida[2] != "esperando":
        await query.answer(t(chat_key, "partida_en_curso"), show_alert=True)
        return
    activos = get_jugadores_activos(chat_key)
    if user.id in [j[0] for j in activos]:
        await query.answer(t(chat_key, "ya_en_partida"), show_alert=True)
        return
    if len(activos) >= MAX_JUGADORES:
        await query.answer(t(chat_key, "partida_llena").format(n=MAX_JUGADORES), show_alert=True)
        return

    await query.answer()
    await _unirse(chat_key, user, query.message.reply_text, ctx.bot)

async def _unirse(chat_key, user, reply_fn, bot=None):
    partida = get_partida(chat_key)
    if not partida:
        await reply_fn(t(chat_key, "sin_partida"))
        return
    if partida[2] != "esperando":
        await reply_fn(t(chat_key, "partida_en_curso"))
        return

    activos = get_jugadores_activos(chat_key)
    if user.id in [j[0] for j in activos]:
        await reply_fn(t(chat_key, "ya_en_partida"))
        return

    if len(activos) >= MAX_JUGADORES:
        await reply_fn(t(chat_key, "partida_llena").format(n=MAX_JUGADORES))
        return

    upsert_jugador(chat_key, user.id, nombre(user))
    agregar_jugador_activo(chat_key, user.id, nombre(user))
    activos = get_jugadores_activos(chat_key)

    lista = "\n".join(f"  {i+1}\\. {esc(j[1])}" for i, j in enumerate(activos))

    # Botón unirse siempre presente (para que no se pierda en el chat)
    btn_unirse_row = [InlineKeyboardButton(t(chat_key, "btn_unirse"), callback_data="unirse")]
    lang = get_idioma(chat_key)
    btn_cancelar_row = [InlineKeyboardButton(
        "❌ Cancelar partida" if lang == "es" else "❌ Cancel game",
        callback_data="cancelar_lobby"
    )]

    if len(activos) >= MAX_JUGADORES:
        keyboard = [btn_cancelar_row]
    elif len(activos) >= 3:
        keyboard = [
            btn_unirse_row,
            [InlineKeyboardButton(t(chat_key, "btn_iniciar"), callback_data="iniciar_partida")],
            btn_cancelar_row,
        ]
    else:
        keyboard = [btn_unirse_row, btn_cancelar_row]

    sufijo = (
        t(chat_key, "partida_llena").format(n=MAX_JUGADORES) if len(activos) >= MAX_JUGADORES
        else t(chat_key, "puede_iniciar") if len(activos) >= 3
        else t(chat_key, "faltan_jugadores").format(n=3 - len(activos))
    )
    await reply_fn(
        t(chat_key, "unido").format(nombre=esc(nombre(user)), n=len(activos), lista=lista) + sufijo,
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
    )


async def btn_cancelar_lobby(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Botón de cancelar en el post del lobby — solo funciona para el creador."""
    query    = update.callback_query
    chat_key = get_chat_key(update)
    user     = query.from_user

    partida = get_partida(chat_key)
    if not partida or partida[2] == "terminada":
        await query.answer()
        return

    if partida[8] != user.id:
        lang = get_idioma(chat_key)
        msg  = "⚠️ Solo el creador puede cancelar." if lang == "es" else "⚠️ Only the creator can cancel."
        await query.answer(msg, show_alert=True)
        return

    # Cancelar timers y limpiar estado
    for key in [f"timer_{chat_key}", f"timer_adiv_{chat_key}", f"timeout_lobby_{chat_key}"]:
        t_obj = ctx.bot_data.pop(key, None)
        if t_obj: t_obj.cancel()
    for key in [f"turno_{chat_key}", f"adivinando_{chat_key}",
                f"votos_{chat_key}", f"revotacion_{chat_key}"]:
        ctx.bot_data.pop(key, None)

    with get_conn() as conn:
        conn.execute("UPDATE partidas SET estado='terminada' WHERE chat_key=?", (chat_key,))

    await query.answer()
    lang = get_idioma(chat_key)
    try:
        await query.message.edit_text(
            t(chat_key, "cancelado"),
            parse_mode="MarkdownV2"
        )
    except Exception:
        await query.message.reply_text(t(chat_key, "cancelado"), parse_mode="MarkdownV2")


async def btn_iniciar_partida(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_key = get_chat_key(update)
    user = update.effective_user

    partida = get_partida(chat_key)
    if not partida or partida[2] != "esperando":
        await query.answer(t(chat_key, "no_partida_espera"), show_alert=True)
        return
    if partida[8] != user.id and partida[8] != 0:
        # partida[8]==0 → creada por bot (programada): cualquier admin puede iniciar
        await query.answer(t(chat_key, "solo_creador_iniciar"), show_alert=True)
        return
    if partida[8] == 0:
        # Partida programada: asignar creador al primer admin que la inicie
        try:
            member   = await update.effective_chat.get_member(user.id)
            es_admin = member.status in ("administrator", "creator")
        except Exception:
            es_admin = False
        if not es_admin and not (BOT_OWNER_ID and user.id == BOT_OWNER_ID):
            await query.answer(t(chat_key, "solo_creador_iniciar"), show_alert=True)
            return
        with get_conn() as conn:
            conn.execute("UPDATE partidas SET creador_id=? WHERE chat_key=?", (user.id, chat_key))

    jugadores = get_jugadores_activos(chat_key)
    if len(jugadores) < 3:
        await query.answer(t(chat_key, "pocos_jugadores").format(n=len(jugadores)), show_alert=True)
        return

    await query.answer()
    # Cancelar el timeout de lobby
    tarea_timeout = ctx.bot_data.pop(f"timeout_lobby_{chat_key}", None)
    if tarea_timeout:
        tarea_timeout.cancel()

    es_manual = (partida[8] != 0)  # 0 = partida programada

    # Si hay 5+ jugadores y es inicio manual → pantalla de config de impostores
    if len(jugadores) >= 5 and es_manual:
        max_imp = min(3, len(jugadores) - 2)  # máx impostores (siempre ≥2 inocentes)
        imp_default = calcular_num_impostores(len(jugadores))
        ctx.bot_data[f"imp_config_{chat_key}"] = {"imp": imp_default, "max": max_imp, "n": len(jugadores)}
        await _mostrar_config_impostores(chat_key, jugadores, query.message, ctx)
        return

    # Si no → ir directo a elegir categoría
    categorias = cats(chat_key)
    keyboard = [
        [InlineKeyboardButton(cat, callback_data=f"cat:{cat}")]
        for cat in categorias
    ]
    keyboard.append([InlineKeyboardButton(t(chat_key, "btn_random"), callback_data="cat:RANDOM")])
    await query.message.reply_text(
        t(chat_key, "elige_categoria"),
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def _mostrar_config_impostores(chat_key, jugadores, message, ctx):
    """Muestra la pantalla de configuración de número de impostores."""
    cfg = ctx.bot_data.get(f"imp_config_{chat_key}", {})
    imp = cfg.get("imp", 1)
    es_random = cfg.get("random", False)
    n   = cfg.get("n", len(jugadores))

    if es_random:
        texto = t(chat_key, "config_impostores_random").format(n=n)
        imp_label = "🎲"
    else:
        texto = t(chat_key, "config_impostores").format(n=n, imp=imp)
        imp_label = f"{'👤' * imp} {imp}"

    keyboard = [
        [
            InlineKeyboardButton(t(chat_key, "btn_imp_menos"), callback_data="imp_config:menos"),
            InlineKeyboardButton(imp_label, callback_data="imp_config:info"),
            InlineKeyboardButton(t(chat_key, "btn_imp_mas"),  callback_data="imp_config:mas"),
        ],
        [InlineKeyboardButton(t(chat_key, "btn_imp_random"), callback_data="imp_config:random")],
        [InlineKeyboardButton(t(chat_key, "btn_imp_confirmar"), callback_data="imp_config:confirmar")],
    ]
    await message.reply_text(texto, parse_mode="MarkdownV2",
                             reply_markup=InlineKeyboardMarkup(keyboard))


async def btn_imp_config(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Maneja los botones de configuración de impostores."""
    query    = update.callback_query
    chat_key = get_chat_key(update)
    user     = query.from_user
    action   = query.data.split(":")[1]

    partida = get_partida(chat_key)
    if not partida or partida[8] != user.id:
        await query.answer(t(chat_key, "solo_creador_iniciar"), show_alert=True)
        return

    cfg = ctx.bot_data.get(f"imp_config_{chat_key}")
    if not cfg:
        await query.answer()
        return

    if action == "menos":
        cfg["imp"] = max(1, cfg["imp"] - 1)
        cfg["random"] = False
        await query.answer()
    elif action == "mas":
        cfg["imp"] = min(cfg["max"], cfg["imp"] + 1)
        cfg["random"] = False
        await query.answer()
    elif action == "random":
        cfg["random"] = not cfg.get("random", False)
        await query.answer()
    elif action == "info":
        await query.answer()
        return
    elif action == "confirmar":
        # Guardar elección y pasar a categoría
        if cfg.get("random"):
            ctx.bot_data[f"num_impostores_{chat_key}"] = "random"
        else:
            ctx.bot_data[f"num_impostores_{chat_key}"] = cfg["imp"]
        ctx.bot_data.pop(f"imp_config_{chat_key}", None)
        await query.answer()

        categorias = cats(chat_key)
        keyboard = [
            [InlineKeyboardButton(cat, callback_data=f"cat:{cat}")]
            for cat in categorias
        ]
        keyboard.append([InlineKeyboardButton(t(chat_key, "btn_random"), callback_data="cat:RANDOM")])
        await query.message.reply_text(
            t(chat_key, "elige_categoria"),
            parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # Redibujar botones con el nuevo valor
    imp = cfg["imp"]
    es_random_r = cfg.get("random", False)
    if es_random_r:
        texto = t(chat_key, "config_impostores_random").format(n=cfg["n"])
        imp_label_r = "🎲"
    else:
        texto = t(chat_key, "config_impostores").format(n=cfg["n"], imp=imp)
        imp_label_r = f"{'👤' * imp} {imp}"
    keyboard = [
        [
            InlineKeyboardButton(t(chat_key, "btn_imp_menos"), callback_data="imp_config:menos"),
            InlineKeyboardButton(imp_label_r, callback_data="imp_config:info"),
            InlineKeyboardButton(t(chat_key, "btn_imp_mas"),  callback_data="imp_config:mas"),
        ],
        [InlineKeyboardButton(t(chat_key, "btn_imp_random"), callback_data="imp_config:random")],
        [InlineKeyboardButton(t(chat_key, "btn_imp_confirmar"), callback_data="imp_config:confirmar")],
    ]
    try:
        await query.message.edit_text(texto, parse_mode="MarkdownV2",
                                      reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        pass


async def btn_categoria(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_key = get_chat_key(update)
    chat_id = update.effective_chat.id
    user = update.effective_user

    partida = get_partida(chat_key)
    if not partida or partida[8] != user.id:
        await query.answer(t(chat_key, "solo_creador_categoria"), show_alert=True)
        return

    # Evitar doble ejecución: solo proceder si sigue en estado 'esperando'
    if partida[2] != "esperando":
        await query.answer()
        return

    await query.answer()

    # Marcar como 'iniciando' atómicamente para bloquear segundos clics
    with get_conn() as conn:
        updated = conn.execute(
            "UPDATE partidas SET estado='iniciando' WHERE chat_key=? AND estado='esperando'",
            (chat_key,)
        ).rowcount
    if updated == 0:
        return  # Otro proceso ya tomó el control

    try:
        categorias = cats(chat_key)
        categoria_raw = query.data.split(":", 1)[1]
        es_random = (categoria_raw == "RANDOM")
        categoria = random.choice(list(categorias.keys())) if es_random else categoria_raw

        # Verificar que la categoria exista (por si acaso llegó un valor inválido)
        if categoria not in categorias:
            logger.error(f"[btn_categoria] categoria invalida: {categoria!r}")
            with get_conn() as conn:
                conn.execute("UPDATE partidas SET estado='esperando' WHERE chat_key=?", (chat_key,))
            await query.message.reply_text("⚠️ Error al elegir categoría. Intenta de nuevo.")
            return

        texto_cat_grupo = t(chat_key, "cat_sorpresa_grupo") if es_random else t(chat_key, "cat_grupo").format(cat=esc(categoria))
        texto_cat_confirmacion = t(chat_key, "cat_sorpresa_grupo") if es_random else t(chat_key, "cat_confirmacion").format(cat=esc(categoria))

        palabra = elegir_palabra(chat_key, categoria, categorias[categoria])
        jugadores = get_jugadores_activos(chat_key)
        # Usar número configurado manualmente si existe
        num_impostores = ctx.bot_data.pop(f"num_impostores_{chat_key}", None)
        if num_impostores == "random":
            num_impostores = random.randint(1, min(3, len(jugadores) - 2))
        elif num_impostores is None:
            num_impostores = calcular_num_impostores(len(jugadores))
        num_impostores = max(1, min(num_impostores, len(jugadores) - 2))
        impostores = random.sample(jugadores, num_impostores)
        impostor_ids = ",".join(str(i[0]) for i in impostores)
        impostor_ids_set = set(i[0] for i in impostores)
        vivos_ids = ",".join(str(j[0]) for j in jugadores)

        with get_conn() as conn:
            conn.execute(
                "UPDATE partidas SET estado='jugando', categoria=?, palabra=?, impostor_ids=?, vivos=? WHERE chat_key=?",
                (categoria, palabra, impostor_ids, vivos_ids, chat_key)
            )

    except Exception as e:
        # Si algo falla, devolver la partida a 'esperando' para que se pueda reintentar
        logger.error(f"[btn_categoria] error inesperado: {e}")
        with get_conn() as conn:
            conn.execute("UPDATE partidas SET estado='esperando' WHERE chat_key=?", (chat_key,))
        await query.message.reply_text("⚠️ Ocurrió un error al iniciar la partida. Intenta de nuevo.")
        return

    await query.edit_message_text(
        f"{texto_cat_confirmacion}\n\n{t(chat_key, 'enviando_privado')}",
        parse_mode="MarkdownV2"
    )

    pistas_raw = generar_pistas(palabra, categoria, chat_key)
    pistas = "\n".join(esc(linea) for linea in pistas_raw.splitlines())

    fallidos = []
    for uid, uname in jugadores:
        try:
            if uid in impostor_ids_set:
                msg = t(chat_key, "eres_impostor").format(cat=esc(categoria))
                sumar_vez_impostor(chat_key, uid)
            else:
                msg = t(chat_key, "eres_inocente").format(
                    palabra=esc(palabra), cat=esc(categoria), pistas=pistas
                )
                sumar_vez_inocente(chat_key, uid)
            await ctx.bot.send_message(uid, msg, parse_mode="MarkdownV2")
        except Exception:
            fallidos.append(uname)

    orden = list(jugadores)
    random.shuffle(orden)
    turno_lista = "\n".join(f"  {i+1}\\. {esc(j[1])}" for i, j in enumerate(orden))

    ctx.bot_data[f"turno_{chat_key}"] = {
        "orden": [j[0] for j in orden],
        "index": 0,
        "ya_dieron_pista": set(),
        "ronda_pistas": 1,
        "jugadores_iniciales": len(jugadores),
        "intentos_pista": {}
    }

    aviso = ""
    if fallidos:
        aviso = t(chat_key, "aviso_fallidos").format(
            nombres=", ".join(esc(f) for f in fallidos)
        )

    aviso_rondas = (
        t(chat_key, "aviso_2rondas") if len(jugadores) == 3
        else t(chat_key, "aviso_votar")
    )

    thread_id = get_thread_id(chat_key)
    await ctx.bot.send_message(
        chat_id,
        t(chat_key, "partida_comienza").format(
            cat=texto_cat_grupo, orden=turno_lista, aviso_rondas=aviso_rondas
        ) + aviso,
        parse_mode="MarkdownV2",
        message_thread_id=thread_id
    )

    primer = orden[0]
    await _anunciar_turno(chat_key, primer[0], primer[1], chat_id, thread_id, ctx)


async def _timer_abrir_votacion(chat_key: str, chat_id: int, thread_id, ctx):
    """Abre la votación automáticamente después de 30 segundos."""
    await asyncio.sleep(30)
    # Verificar que la partida sigue en jugando y no se abrió ya la votación
    partida = get_partida(chat_key)
    if not partida or partida[2] != "jugando":
        return
    # Verificar que no hay votación ya abierta
    if ctx.bot_data.get(f"votos_{chat_key}") is not None:
        return

    vivos_ids = get_vivos(chat_key)
    jugadores = get_jugadores_activos(chat_key)
    vivos = [j for j in jugadores if j[0] in vivos_ids]

    keyboard = [
        [InlineKeyboardButton(f"🗳️ {j[1]}", callback_data=f"voto:{j[0]}")]
        for j in vivos
    ]
    ctx.bot_data[f"votos_{chat_key}"] = {}

    try:
        await ctx.bot.send_message(
            chat_id,
            t(chat_key, "votacion_auto").format(n=len(vivos)),
            parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup(keyboard),
            message_thread_id=thread_id
        )
    except Exception as e:
        logger.error(f"[TIMER_VOTAR] Error auto-abriendo votación {chat_key}: {e}")
        return

    # Lanzar timer de 1 minuto para auto-resolver
    tarea = asyncio.create_task(_timer_resolver_votacion(chat_key, chat_id, thread_id, ctx))
    ctx.bot_data[f"timer_votacion_{chat_key}"] = tarea
    ctx.bot_data.pop(f"timer_abrir_votacion_{chat_key}", None)


async def _timer_resolver_votacion(chat_key: str, chat_id: int, thread_id, ctx):
    """Resuelve la votación automáticamente después de 1 minuto."""
    await asyncio.sleep(60)

    partida = get_partida(chat_key)
    if not partida or partida[2] != "jugando":
        ctx.bot_data.pop(f"timer_votacion_{chat_key}", None)
        return

    votos = ctx.bot_data.get(f"votos_{chat_key}")
    if votos is None:
        ctx.bot_data.pop(f"timer_votacion_{chat_key}", None)
        return

    ctx.bot_data.pop(f"timer_votacion_{chat_key}", None)
    ctx.bot_data.pop(f"votos_{chat_key}", None)

    vivos_ids = get_vivos(chat_key)
    jugadores = get_jugadores_activos(chat_key)
    vivos = [j for j in jugadores if j[0] in vivos_ids]

    # Contar votos
    conteo = {}
    for v in votos.values():
        conteo[v] = conteo.get(v, 0) + 1

    # Filtrar solo quienes tienen 2+ votos
    con_dos_votos = {uid: cnt for uid, cnt in conteo.items() if cnt >= 2}

    class _FakeMsg:
        def __init__(self, b, ci, ti):
            self.chat = type('C', (), {'id': ci})()
            self._bot = b
            self._ti = ti
        async def reply_text(self, *a, **kw):
            kw.setdefault('message_thread_id', self._ti)
            return await self._bot.send_message(self.chat.id, *a, **kw)

    fake_msg = _FakeMsg(ctx.bot, chat_id, thread_id)

    if not con_dos_votos:
        # Nadie llegó a 2 votos
        lang = get_idioma(chat_key)

        # ¿Ya ocurrió antes? → nueva ronda de pistas
        if ctx.bot_data.get(f"votacion_sin_resultado_{chat_key}"):
            ctx.bot_data.pop(f"votacion_sin_resultado_{chat_key}", None)
            impostor_ids_raw = partida[5] or ""
            impostor_ids_set = set(int(i) for i in impostor_ids_raw.split(",") if i.strip())
            await ctx.bot.send_message(
                chat_id,
                t(chat_key, "segundo_empate"),
                parse_mode="MarkdownV2",
                message_thread_id=thread_id
            )
            await _nueva_ronda_pistas(
                chat_key, ctx, jugadores, vivos_ids,
                impostor_ids_set, partida[4], partida[3], fake_msg
            )
        else:
            # Primera vez → reabrir votación
            ctx.bot_data[f"votacion_sin_resultado_{chat_key}"] = True
            keyboard = [
                [InlineKeyboardButton(f"🗳️ {j[1]}", callback_data=f"voto:{j[0]}")]
                for j in vivos
            ]
            ctx.bot_data[f"votos_{chat_key}"] = {}
            msg_reopen = await ctx.bot.send_message(
                chat_id,
                t(chat_key, "votos_insuficientes"),
                parse_mode="MarkdownV2",
                reply_markup=InlineKeyboardMarkup(keyboard),
                message_thread_id=thread_id
            )
            # Nuevo timer de 1 minuto
            tarea2 = asyncio.create_task(_timer_resolver_votacion(chat_key, chat_id, thread_id, ctx))
            ctx.bot_data[f"timer_votacion_{chat_key}"] = tarea2
        return

    # Hay alguien con 2+ votos → proceder con la eliminación
    ctx.bot_data.pop(f"votacion_sin_resultado_{chat_key}", None)
    ctx.bot_data[f"votos_{chat_key}"] = votos  # restaurar para resolver_votacion
    await resolver_votacion(chat_key, ctx, partida, jugadores, vivos, votos, fake_msg)


async def _abrir_votacion(chat_key, ctx, message):
    vivos_ids = get_vivos(chat_key)
    jugadores = get_jugadores_activos(chat_key)
    vivos = [j for j in jugadores if j[0] in vivos_ids]

    keyboard = [
        [InlineKeyboardButton(f"🗳️ {j[1]}", callback_data=f"voto:{j[0]}")]
        for j in vivos
    ]
    ctx.bot_data[f"votos_{chat_key}"] = {}

    await message.reply_text(
        t(chat_key, "quien_es_impostor").format(n=len(vivos)),
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    # Cancelar timer previo si existe (evita doble resolución)
    prev_tv = ctx.bot_data.pop(f"timer_votacion_{chat_key}", None)
    if prev_tv:
        prev_tv.cancel()
    # Lanzar timer de 1 minuto para auto-resolver
    chat_id = message.chat.id
    thread_id = get_thread_id(chat_key)
    tarea = asyncio.create_task(_timer_resolver_votacion(chat_key, chat_id, thread_id, ctx))
    ctx.bot_data[f"timer_votacion_{chat_key}"] = tarea


async def btn_abrir_votar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_key = get_chat_key(update)
    user = update.effective_user

    partida = get_partida(chat_key)
    if not partida or partida[2] != "jugando":
        await query.answer(t(chat_key, "no_partida_votacion"), show_alert=True)
        return
    if partida[8] != user.id:
        await query.answer(t(chat_key, "solo_creador_votar"), show_alert=True)
        return

    # Cancelar timer de auto-abrir si existe
    tarea_abv = ctx.bot_data.pop(f"timer_abrir_votacion_{chat_key}", None)
    if tarea_abv:
        tarea_abv.cancel()

    await query.answer()
    await _abrir_votacion(chat_key, ctx, query.message)


async def cmd_votar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_key = get_chat_key(update)
    partida = get_partida(chat_key)
    user = update.effective_user

    if not partida or partida[2] != "jugando":
        await update.message.reply_text(t(chat_key, "no_partida_curso"))
        return
    if partida[8] != user.id:
        await update.message.reply_text(t(chat_key, "solo_creador_votar"))
        return

    # Cancelar timer de auto-abrir si existe
    tav = ctx.bot_data.pop(f"timer_abrir_votacion_{chat_key}", None)
    if tav: tav.cancel()
    await _abrir_votacion(chat_key, ctx, update.message)


async def btn_voto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_key = get_chat_key(update)
    voter_id = query.from_user.id

    partida = get_partida(chat_key)
    if not partida or partida[2] != "jugando":
        await query.answer(t(chat_key, "no_partida_votacion"), show_alert=True)
        return

    vivos_ids = get_vivos(chat_key)
    if voter_id not in vivos_ids:
        await query.answer(t(chat_key, "no_puedes_votar"), show_alert=True)
        return

    jugadores = get_jugadores_activos(chat_key)
    vivos = [j for j in jugadores if j[0] in vivos_ids]

    votado_id = int(query.data.split(":")[1])
    votos = ctx.bot_data.setdefault(f"votos_{chat_key}", {})

    if voter_id in votos:
        await query.answer(t(chat_key, "voto_ya"), show_alert=True)
        return

    votos[voter_id] = votado_id
    await query.answer(t(chat_key, "voto_ok"))

    faltantes = len(vivos) - len(votos)
    sufijo_faltantes = t(chat_key, "faltan_votos").format(n=faltantes) if faltantes > 0 else ""
    await query.message.reply_text(
        t(chat_key, "voto_confirmado").format(nombre=esc(query.from_user.first_name), faltantes=sufijo_faltantes),
        parse_mode="MarkdownV2"
    )

    if len(votos) >= len(vivos):
        # Cancelar timer de auto-resolver (ya se resuelve ahora)
        tv = ctx.bot_data.pop(f"timer_votacion_{chat_key}", None)
        if tv: tv.cancel()
        await resolver_votacion(chat_key, ctx, partida, jugadores, vivos, votos, query.message)


async def btn_revoto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_key = get_chat_key(update)
    voter_id = query.from_user.id

    partida = get_partida(chat_key)
    if not partida or partida[2] != "jugando":
        await query.answer(t(chat_key, "no_partida_votacion"), show_alert=True)
        return

    vivos_ids = get_vivos(chat_key)
    if voter_id not in vivos_ids:
        await query.answer(t(chat_key, "no_puedes_votar"), show_alert=True)
        return

    datos = ctx.bot_data.get(f"revotacion_{chat_key}")
    if not datos:
        await query.answer(t(chat_key, "no_revotacion"), show_alert=True)
        return

    votado_id = int(query.data.split(":")[1])
    if votado_id not in datos["candidatos"]:
        await query.answer(t(chat_key, "voto_invalido"), show_alert=True)
        return

    votos = ctx.bot_data.setdefault(f"votos_{chat_key}", {})
    if voter_id in votos:
        await query.answer(t(chat_key, "voto_ya"), show_alert=True)
        return

    votos[voter_id] = votado_id
    await query.answer(t(chat_key, "voto_ok"))

    vivos = datos["vivos"]
    faltantes = len(vivos) - len(votos)
    sufijo_faltantes = t(chat_key, "faltan_votos").format(n=faltantes) if faltantes > 0 else ""
    await query.message.reply_text(
        t(chat_key, "voto_confirmado_revoto").format(nombre=esc(query.from_user.first_name), faltantes=sufijo_faltantes),
        parse_mode="MarkdownV2"
    )

    if len(votos) >= len(vivos):
        ctx.bot_data.pop(f"revotacion_{chat_key}", None)

        conteo2 = {}
        for v in votos.values():
            conteo2[v] = conteo2.get(v, 0) + 1

        max_votos2 = max(conteo2.values())
        empatados2 = [uid for uid, cnt in conteo2.items() if cnt == max_votos2]
        jugadores = datos["jugadores"]

        if len(empatados2) > 1:
            await query.message.reply_text(
                t(chat_key, "segundo_empate"),
                parse_mode="MarkdownV2"
            )
            partida_fresca = get_partida(chat_key)
            vivos_ids_actual = get_vivos(chat_key)
            jugadores_frescos = get_jugadores_activos(chat_key)
            impostor_ids_set2 = set(int(i) for i in partida_fresca[5].split(","))
            await _nueva_ronda_pistas(
                chat_key, ctx, jugadores_frescos, vivos_ids_actual,
                impostor_ids_set2, partida_fresca[4], partida_fresca[3], query.message
            )
            return

        vivos_ids_actual = get_vivos(chat_key)
        vivos_actual = [j for j in jugadores if j[0] in vivos_ids_actual]
        await resolver_votacion(chat_key, ctx, partida, jugadores, vivos_actual, votos, query.message)


async def resolver_votacion(chat_key, ctx, partida, jugadores, vivos, votos, message):
    conteo = {}
    for votado in votos.values():
        conteo[votado] = conteo.get(votado, 0) + 1

    max_votos = max(conteo.values())
    empatados = [uid for uid, cnt in conteo.items() if cnt == max_votos]

    # ── Empate → revotación ──
    if len(empatados) > 1:
        vivos_ids = get_vivos(chat_key)
        jugadores_frescos = get_jugadores_activos(chat_key)
        vivos_frescos = [j for j in jugadores_frescos if j[0] in vivos_ids]
        nombre_map = {j[0]: j[1] for j in jugadores_frescos}
        nombres_empatados = " y ".join(f"*{esc(nombre_map.get(e, '?'))}*" for e in empatados)

        ctx.bot_data[f"revotacion_{chat_key}"] = {
            "candidatos": empatados,
            "partida": partida,
            "jugadores": jugadores_frescos,
            "vivos": vivos_frescos,
        }
        ctx.bot_data[f"votos_{chat_key}"] = {}

        keyboard = [
            [InlineKeyboardButton(f"🗳️ {nombre_map.get(uid, '?')}", callback_data=f"revoto:{uid}")]
            for uid in empatados
        ]
        await message.reply_text(
            t(chat_key, "empate").format(nombres=nombres_empatados, n=max_votos),
            parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # ── Sin empate: procesar eliminación ──
    eliminado_id = empatados[0]

    # Recargar todo fresco desde DB para evitar datos desactualizados
    partida = get_partida(chat_key)
    if not partida:
        return

    impostor_ids_raw = partida[5] or ""
    impostor_ids_set = set(int(i) for i in impostor_ids_raw.split(",") if i.strip())

    todos_jugadores = get_jugadores_activos(chat_key)
    vivos_ids_frescos = get_vivos(chat_key)
    vivos = [j for j in todos_jugadores if j[0] in vivos_ids_frescos]

    impostor_names_map = {j[0]: j[1] for j in todos_jugadores}
    impostores = [(uid, impostor_names_map.get(uid, str(uid))) for uid in impostor_ids_set]

    eliminado = next((j for j in vivos if j[0] == eliminado_id), None)
    if eliminado is None:
        eliminado = (eliminado_id, impostor_names_map.get(eliminado_id, str(eliminado_id)))

    logger.info(f"[resolver_votacion] eliminado_id={eliminado_id} impostor_ids={impostor_ids_raw} impostor_ids_set={impostor_ids_set} es_impostor={eliminado_id in impostor_ids_set}")
    palabra = partida[4]
    categoria = partida[3]

    nombre_map = {j[0]: j[1] for j in todos_jugadores}
    detalle_votos = "\n".join(
        f"  • {esc(nombre_map.get(v_from, '?'))} → {esc(nombre_map.get(v_to, '?'))}"
        for v_from, v_to in votos.items()
    )

    # Guardar votos en historial para estadísticas de rivalidad
    with get_conn() as conn:
        for v_from, v_to in votos.items():
            conn.execute(
                "INSERT INTO votos_historial (chat_key, voter_id, voted_id) VALUES (?,?,?)",
                (chat_key, v_from, v_to)
            )

    es_impostor = eliminado_id in impostor_ids_set
    etiqueta = t(chat_key, "era_impostor") if es_impostor else t(chat_key, "era_inocente")

    vivos_restantes_ids = eliminar_de_vivos(chat_key, eliminado_id)
    impostores_vivos = [j for j in impostores if j[0] in vivos_restantes_ids]
    inocentes_vivos_ids = [v for v in vivos_restantes_ids if v not in impostor_ids_set]

    # Transferir creador si fue eliminado
    if eliminado_id == partida[8] and vivos_restantes_ids:
        nuevo_creador = vivos_restantes_ids[0]
        with get_conn() as conn:
            conn.execute(
                "UPDATE partidas SET creador_id=? WHERE chat_key=?",
                (nuevo_creador, chat_key)
            )
        nombre_nuevo = nombre_map.get(nuevo_creador, "?")
        await message.reply_text(
            t(chat_key, "nuevo_creador").format(nombre=esc(nombre_nuevo)),
            parse_mode="MarkdownV2"
        )

    await message.reply_text(
        t(chat_key, "resultado_votacion").format(
            nombre=esc(eliminado[1]), etiqueta=etiqueta, detalle=detalle_votos
        ),
        parse_mode="MarkdownV2"
    )

    # ── Impostor votado → oportunidad de adivinar ──
    if es_impostor:
        with get_conn() as conn:
            conn.execute("UPDATE partidas SET estado='adivinando' WHERE chat_key=?", (chat_key,))

        ctx.bot_data[f"adivinando_{chat_key}"] = {
            "impostor_id": eliminado_id,
            "impostor_ids_set": impostor_ids_set,
            "palabra": palabra,
            "categoria": categoria,
            "jugadores": todos_jugadores,
            "vivos_restantes_ids": vivos_restantes_ids,
            "impostores_vivos": impostores_vivos,
            "inocentes_vivos_ids": inocentes_vivos_ids,
            "detalle_votos": detalle_votos,
            "partida": partida,
            "impostores": impostores,
        }

        await message.reply_text(
            t(chat_key, "ultima_oportunidad").format(nombre=esc(eliminado[1]), cat=esc(categoria)),
            parse_mode="MarkdownV2"
        )
        chat_id = message.chat.id
        thread_id = get_thread_id(chat_key)
        tarea = asyncio.create_task(
            _timer_adivinanza(chat_key, eliminado[0], chat_id, thread_id, ctx)
        )
        ctx.bot_data[f"timer_adiv_{chat_key}"] = tarea
        return

    # ── Inocente votado ──
    if not impostores_vivos:
        await _fin_grupo_gana(chat_key, ctx, todos_jugadores, impostores, palabra, categoria, detalle_votos, message)
        return

    inocentes_restantes = [v for v in vivos_restantes_ids if v not in impostor_ids_set]
    if len(inocentes_restantes) <= 1:
        await _fin_impostores_ganan(
            chat_key, ctx, partida, todos_jugadores, impostores,
            None, palabra, categoria, detalle_votos, message, razon="supervivencia"
        )
        return

    await _nueva_ronda_pistas(chat_key, ctx, todos_jugadores, vivos_restantes_ids, impostor_ids_set, palabra, categoria, message)


TIMER_ADIV_SEGUNDOS = 30

async def _timer_adivinanza(chat_key, impostor_id, chat_id, thread_id, ctx):
    """Si el impostor no adivina en 30s, el grupo gana automáticamente."""
    # Aviso a mitad de tiempo (15s)
    await asyncio.sleep(TIMER_ADIV_SEGUNDOS // 2)
    try:
        datos = ctx.bot_data.get(f"adivinando_{chat_key}")
        if datos and datos.get("impostor_id") == impostor_id:
            nombre_j = next((j[1] for j in datos.get("jugadores", []) if j[0] == impostor_id), "?")
            await ctx.bot.send_message(
                chat_id,
                t(chat_key, "aviso_15s").format(nombre=esc_link(nombre_j), uid=impostor_id),
                parse_mode="MarkdownV2",
                message_thread_id=thread_id
            )
    except Exception:
        pass
    # Esperar los 15s restantes
    await asyncio.sleep(TIMER_ADIV_SEGUNDOS // 2)
    try:
        await _timer_adivinanza_body(chat_key, impostor_id, chat_id, thread_id, ctx)
    except Exception as e:
        logger.error(f"[TIMER_ADIV] excepcion no capturada: {e}", exc_info=True)

async def _timer_adivinanza_body(chat_key, impostor_id, chat_id, thread_id, ctx):

    # Si ya fue respondido (datos limpiados), ignorar
    datos = ctx.bot_data.pop(f"adivinando_{chat_key}", None)
    if not datos:
        return
    # Verificar que sigue siendo el mismo impostor esperando
    if datos.get("impostor_id") != impostor_id:
        return

    partida = get_partida(chat_key)
    if not partida or partida[2] != "adivinando":
        return

    jugadores = datos["jugadores"]
    impostores = datos["impostores"]
    palabra = datos["palabra"]
    categoria = datos["categoria"]
    detalle_votos = datos["detalle_votos"]
    impostor_ids_set = datos["impostor_ids_set"]
    vivos_restantes_ids = datos["vivos_restantes_ids"]
    impostores_vivos = datos["impostores_vivos"]
    inocentes_vivos_ids = datos["inocentes_vivos_ids"]

    nombre_j = next((j[1] for j in jugadores if j[0] == impostor_id), "?")

    # Si hay un intento pendiente (escribió pero no confirmó), procesarlo automáticamente
    intento = datos.get("intento")
    if intento:
        msg_auto = await ctx.bot.send_message(
            chat_id,
            t(chat_key, "pista_auto_confirmada"),
            parse_mode="MarkdownV2",
            message_thread_id=thread_id
        )
        if normalizar(intento) == normalizar(palabra):
            msg = await ctx.bot.send_message(
                chat_id,
                t(chat_key, "adivino").format(nombre=esc(nombre_j), palabra=esc(palabra)),
                parse_mode="MarkdownV2",
                message_thread_id=thread_id
            )
            await _fin_impostores_ganan(
                chat_key, ctx, partida, jugadores, impostores,
                None, palabra, categoria, detalle_votos, msg
            )
        else:
            msg = await ctx.bot.send_message(
                chat_id,
                t(chat_key, "incorrecto").format(nombre=esc(nombre_j), texto=esc(intento.lower())),
                parse_mode="MarkdownV2",
                message_thread_id=thread_id
            )
            if not impostores_vivos:
                await _fin_grupo_gana(chat_key, ctx, jugadores, impostores, palabra, categoria, detalle_votos, msg)
                return
            inocentes_restantes = [v for v in vivos_restantes_ids if v not in impostor_ids_set]
            if len(inocentes_restantes) <= 1:
                await _fin_impostores_ganan(
                    chat_key, ctx, partida, jugadores, impostores,
                    None, palabra, categoria, detalle_votos, msg, razon="supervivencia"
                )
                return
            await _nueva_ronda_pistas(chat_key, ctx, jugadores, vivos_restantes_ids, impostor_ids_set, palabra, categoria, msg)
        return

    # Sin intento → timeout real, el impostor eliminado no adivinó
    msg_text = t(chat_key, "adiv_timeout").format(nombre=esc_link(nombre_j), uid=impostor_id)
    msg = await ctx.bot.send_message(chat_id, msg_text, parse_mode="MarkdownV2", message_thread_id=thread_id)

    # Verificar si quedan otros impostores vivos antes de declarar ganador
    if not impostores_vivos:
        await _fin_grupo_gana(chat_key, ctx, jugadores, impostores, palabra, categoria, detalle_votos, msg)
        return
    inocentes_restantes = [v for v in vivos_restantes_ids if v not in impostor_ids_set]
    if len(inocentes_restantes) <= 1:
        await _fin_impostores_ganan(
            chat_key, ctx, partida, jugadores, impostores,
            None, palabra, categoria, detalle_votos, msg, razon="supervivencia"
        )
        return
    await _nueva_ronda_pistas(chat_key, ctx, jugadores, vivos_restantes_ids, impostor_ids_set, palabra, categoria, msg)


TIMER_TURNO_SEGUNDOS = 60

async def _timer_turno(chat_key, user_id, chat_id, thread_id, ctx):
    """Callback que se ejecuta cuando expira el tiempo de turno."""
    # Aviso a mitad de tiempo (30s)
    await asyncio.sleep(TIMER_TURNO_SEGUNDOS // 2)
    try:
        turno_data = ctx.bot_data.get(f"turno_{chat_key}")
        if turno_data and turno_data.get("index") < len(turno_data.get("orden", [])):
            if turno_data["orden"][turno_data["index"]] == user_id:
                nombre_j = next((j[1] for j in get_jugadores_activos(chat_key) if j[0] == user_id), "?")
                await ctx.bot.send_message(
                    chat_id,
                    t(chat_key, "aviso_30s").format(nombre=esc_link(nombre_j), uid=user_id),
                    parse_mode="MarkdownV2",
                    message_thread_id=thread_id
                )
    except Exception:
        pass
    # Esperar los 30s restantes
    await asyncio.sleep(TIMER_TURNO_SEGUNDOS // 2)
    try:
        await _timer_turno_body(chat_key, user_id, chat_id, thread_id, ctx)
    except Exception as e:
        logger.error(f"[TIMER_TURNO] excepcion no capturada: {e}", exc_info=True)

async def _timer_turno_body(chat_key, user_id, chat_id, thread_id, ctx):

    turno_data = ctx.bot_data.get(f"turno_{chat_key}")
    if not turno_data:
        return
    orden = turno_data["orden"]
    index = turno_data["index"]
    # Si ya no es el turno de este jugador, ignorar
    if index >= len(orden) or orden[index] != user_id:
        return

    jugadores = get_jugadores_activos(chat_key)
    nombre_j = next((j[1] for j in jugadores if j[0] == user_id), "?")

    pista_pendiente = turno_data.pop("pista_pendiente", None)

    if pista_pendiente:
        # Tenía algo escrito → auto-confirmar
        await ctx.bot.send_message(
            chat_id,
            t(chat_key, "turno_timeout_autoconf").format(nombre=esc_link(nombre_j), uid=user_id),
            parse_mode="MarkdownV2",
            message_thread_id=thread_id
        )
        await ctx.bot.send_message(
            chat_id,
            f"💬 *{esc(nombre_j)}*: *{esc(pista_pendiente)}*",
            parse_mode="MarkdownV2",
            message_thread_id=thread_id
        )
    else:
        # No escribió nada → saltar turno
        await ctx.bot.send_message(
            chat_id,
            t(chat_key, "turno_timeout").format(nombre=esc_link(nombre_j), uid=user_id),
            parse_mode="MarkdownV2",
            message_thread_id=thread_id
        )

    turno_data["ya_dieron_pista"].add(user_id)
    siguiente_index = index + 1
    turno_data["index"] = siguiente_index
    turno_data.setdefault("intentos_pista", {})[user_id] = 0

    if siguiente_index >= len(orden):
        ronda_pistas = turno_data.get("ronda_pistas", 1)
        jugadores_iniciales = turno_data.get("jugadores_iniciales", len(orden))

        if jugadores_iniciales == 3 and ronda_pistas == 1:
            ctx.bot_data.pop(f"turno_{chat_key}", None)
            vivos_ids = get_vivos(chat_key)
            vivos = [j for j in jugadores if j[0] in vivos_ids]
            nuevo_orden = list(vivos)
            random.shuffle(nuevo_orden)
            turno_lista = "\n".join(f"  {i+1}\\. {esc(j[1])}" for i, j in enumerate(nuevo_orden))
            ctx.bot_data[f"turno_{chat_key}"] = {
                "orden": [j[0] for j in nuevo_orden],
                "index": 0,
                "ya_dieron_pista": set(),
                "ronda_pistas": 2,
                "jugadores_iniciales": jugadores_iniciales,
                "intentos_pista": {}
            }
            await ctx.bot.send_message(chat_id, t(chat_key, "segunda_ronda").format(orden=turno_lista), parse_mode="MarkdownV2", message_thread_id=thread_id)
            primer = nuevo_orden[0]
            await _anunciar_turno(chat_key, primer[0], primer[1], chat_id, thread_id, ctx)
            return

        ctx.bot_data.pop(f"turno_{chat_key}", None)
        await ctx.bot.send_message(
            chat_id, t(chat_key, "todos_dieron_pista"), parse_mode="MarkdownV2",
            message_thread_id=thread_id,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(chat_key, "btn_abrir_votacion"), callback_data="abrir_votar")]])
        )
        # Timer de 30s para auto-abrir votación
        tarea_abv = asyncio.create_task(_timer_abrir_votacion(chat_key, chat_id, thread_id, ctx))
        ctx.bot_data[f"timer_abrir_votacion_{chat_key}"] = tarea_abv
        return

    siguiente_id = orden[siguiente_index]
    nombre_sig = next((j[1] for j in jugadores if j[0] == siguiente_id), "?")
    await _anunciar_turno(chat_key, siguiente_id, nombre_sig, chat_id, thread_id, ctx)


async def _anunciar_turno(chat_key, user_id, nombre_j, chat_id, thread_id, ctx):
    """Anuncia el turno e inicia el timer de 1 minuto."""
    # Cancelar timer anterior si existe, pero nunca el task propio
    tarea_actual = asyncio.current_task()
    tarea_anterior = ctx.bot_data.pop(f"timer_{chat_key}", None)
    if tarea_anterior and tarea_anterior is not tarea_actual:
        tarea_anterior.cancel()

    await asyncio.shield(ctx.bot.send_message(
        chat_id,
        t(chat_key, "turno").format(nombre=esc_link(nombre_j), uid=user_id),
        parse_mode="MarkdownV2",
        message_thread_id=thread_id
    ))

    # Iniciar nuevo timer
    tarea = asyncio.create_task(_timer_turno(chat_key, user_id, chat_id, thread_id, ctx))
    ctx.bot_data[f"timer_{chat_key}"] = tarea


async def _nueva_ronda_pistas(chat_key, ctx, jugadores, vivos_ids, impostor_ids_set, palabra, categoria, message):
    vivos = [j for j in jugadores if j[0] in vivos_ids]
    orden = list(vivos)
    random.shuffle(orden)
    turno_lista = "\n".join(f"  {i+1}\\. {esc(j[1])}" for i, j in enumerate(orden))

    ctx.bot_data[f"turno_{chat_key}"] = {
        "orden": [j[0] for j in orden],
        "index": 0,
        "ya_dieron_pista": set(),
        "ronda_pistas": 2,
        "jugadores_iniciales": len(jugadores),
        "intentos_pista": {}
    }

    with get_conn() as conn:
        conn.execute("UPDATE partidas SET estado='jugando' WHERE chat_key=?", (chat_key,))

    await message.reply_text(
        t(chat_key, "nueva_ronda_pistas").format(n=len(vivos), orden=turno_lista),
        parse_mode="MarkdownV2"
    )

    primer = orden[0]
    chat_id = message.chat.id
    thread_id = get_thread_id(chat_key)
    await _anunciar_turno(chat_key, primer[0], primer[1], chat_id, thread_id, ctx)


async def btn_confirmar_pista(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_key = get_chat_key(update)
    user = query.from_user

    turno_data = ctx.bot_data.get(f"turno_{chat_key}")
    if not turno_data:
        await query.answer(t(chat_key, "no_tu_turno"), show_alert=True)
        return

    orden = turno_data["orden"]
    index = turno_data["index"]

    if user.id != orden[index]:
        await query.answer(t(chat_key, "no_tu_turno"), show_alert=True)
        return

    await query.answer(t(chat_key, "pista_confirmada"))

    # Cancelar timer activo
    tarea = ctx.bot_data.pop(f"timer_{chat_key}", None)
    if tarea:
        tarea.cancel()

    pista_texto = turno_data.pop("pista_pendiente", None)
    turno_data.setdefault("intentos_pista", {})[user.id] = 0  # resetear contador
    if pista_texto:
        try:
            await query.message.edit_text(
                f"💬 *{esc(nombre(user))}*: *{esc(pista_texto)}*",
                parse_mode="MarkdownV2"
            )
        except Exception:
            try:
                await query.message.delete()
            except Exception:
                pass  # si no se puede borrar, continuar igual
    else:
        try:
            await query.message.delete()
        except Exception:
            pass

    turno_data["ya_dieron_pista"].add(user.id)
    siguiente_index = index + 1
    turno_data["index"] = siguiente_index
    chat_id = query.message.chat.id
    thread_id = get_thread_id(chat_key)

    if siguiente_index >= len(orden):
        ronda_pistas = turno_data.get("ronda_pistas", 1)
        jugadores_iniciales = turno_data.get("jugadores_iniciales", len(orden))

        if jugadores_iniciales == 3 and ronda_pistas == 1:
            ctx.bot_data.pop(f"turno_{chat_key}", None)
            jugadores = get_jugadores_activos(chat_key)
            vivos_ids = get_vivos(chat_key)
            vivos = [j for j in jugadores if j[0] in vivos_ids]
            nuevo_orden = list(vivos)
            random.shuffle(nuevo_orden)
            turno_lista = "\n".join(f"  {i+1}\\. {esc(j[1])}" for i, j in enumerate(nuevo_orden))

            ctx.bot_data[f"turno_{chat_key}"] = {
                "orden": [j[0] for j in nuevo_orden],
                "index": 0,
                "ya_dieron_pista": set(),
                "ronda_pistas": 2,
                "jugadores_iniciales": jugadores_iniciales,
                "intentos_pista": {}
            }

            await ctx.bot.send_message(
                chat_id,
                t(chat_key, "segunda_ronda").format(orden=turno_lista),
                parse_mode="MarkdownV2",
                message_thread_id=thread_id
            )
            primer = nuevo_orden[0]
            await _anunciar_turno(chat_key, primer[0], primer[1], chat_id, thread_id, ctx)
            return

        ctx.bot_data.pop(f"turno_{chat_key}", None)
        await ctx.bot.send_message(
            chat_id,
            t(chat_key, "todos_dieron_pista"),
            parse_mode="MarkdownV2",
            message_thread_id=thread_id,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(t(chat_key, "btn_abrir_votacion"), callback_data="abrir_votar")
            ]])
        )
        # Timer de 30s para auto-abrir votación
        tarea_abv2 = asyncio.create_task(_timer_abrir_votacion(chat_key, chat_id, thread_id, ctx))
        ctx.bot_data[f"timer_abrir_votacion_{chat_key}"] = tarea_abv2
        return

    siguiente_id = orden[siguiente_index]
    jugadores = get_jugadores_activos(chat_key)
    nombre_siguiente = next((j[1] for j in jugadores if j[0] == siguiente_id), "?")

    await _anunciar_turno(chat_key, siguiente_id, nombre_siguiente, chat_id, thread_id, ctx)


async def handle_adivinanza(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat_key = get_chat_key(update)
    user = update.effective_user
    texto = update.message.text.strip()

    # ── Registrar grupo para Adivina la Idol ───────────────────
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        gi_registrar_grupo(chat.id, chat.title or "?", chat_key)

    # ── Owner: enviar mensaje a grupo ────────────────────────
    if chat and chat.type == "private" and user.id == BOT_OWNER_ID:
        destino = ctx.bot_data.get(f"owner_msg_destino_{user.id}")
        if destino and texto not in ("/cancelarmsg",):
            ctx.bot_data.pop(f"owner_msg_destino_{user.id}", None)
            try:
                await ctx.bot.send_message(destino, texto)
                await update.message.reply_text("✅ Mensaje enviado.")
            except Exception as e:
                await update.message.reply_text(f"❌ Error: {e}")
            return
        elif texto == "/cancelarmsg":
            ctx.bot_data.pop(f"owner_msg_destino_{user.id}", None)
            await update.message.reply_text("❌ Cancelado.")
            return

    # ── Captura de texto para /idol setup (solo owner en privado) ──
    if chat and chat.type == "private" and user.id == BOT_OWNER_ID:
        gi_setup_priv = ctx.bot_data.get(f"gi_setup_{user.id}")
        if gi_setup_priv and gi_setup_priv.get("esperando") and gi_setup_priv["esperando"] != "imagen":
            campo_gi = gi_setup_priv["esperando"]
            lang_gi  = gi_setup_priv.get("lang", "es")
            gi_setup_priv["esperando"] = None
            import re as _re_gi
            if campo_gi in ("inicio", "fin"):
                    tz_gi = gi_setup_priv.get("tz_offset", 0)
                    ts_gi = _parse_fecha_hora(texto, tz_gi)
                    if ts_gi:
                        gi_setup_priv[f"{campo_gi}_ts"] = ts_gi
                    else:
                        await update.message.reply_text(
                            "⚠️ Formato inválido\\. Usa HH:MM o DD/MM HH:MM",
                            parse_mode="MarkdownV2"
                        )
                        return
            else:
                # "idol" se almacena como "idol_name" para coincidir con gi_build_setup_text
                key_gi = "idol_name" if campo_gi == "idol" else campo_gi
                gi_setup_priv[key_gi] = texto
            try:
                await update.message.delete()
            except Exception:
                pass
            msg_id_gi = gi_setup_priv.get("mensaje_id")
            text_gi   = gi_build_setup_text(gi_setup_priv, lang_gi)
            kbd_gi    = gi_build_setup_keyboard(gi_setup_priv, lang_gi)
            if msg_id_gi:
                try:
                    await ctx.bot.edit_message_text(
                        text_gi, chat_id=user.id, message_id=msg_id_gi,
                        parse_mode="MarkdownV2", reply_markup=InlineKeyboardMarkup(kbd_gi)
                    )
                    return
                except Exception:
                    pass
            new_msg_gi = await update.message.reply_text(
                text_gi, parse_mode="MarkdownV2", reply_markup=InlineKeyboardMarkup(kbd_gi)
            )
            gi_setup_priv["mensaje_id"] = new_msg_gi.message_id
            return

    # ── Captura de hora para /program ──────────────────────────
    setup = ctx.bot_data.get(f"programa_setup_{chat_key}")
    if setup and setup.get("esperando_hora") and user.id == setup.get("admin_id"):
        import re as _re
        if _re.match(r"^\d{1,2}:\d{2}$", texto):
            hora_ts = _parse_hora(texto, setup.get("tz_offset", 0))
            if hora_ts:
                setup["hora_inicio"]   = hora_ts
                setup["esperando_hora"] = False
                text, keyboard = _build_programa_setup_text(chat_key, setup)
                setup_msg_id = setup.get("mensaje_setup_id")
                if setup_msg_id:
                    try:
                        await ctx.bot.edit_message_text(
                            text, chat_id=update.effective_chat.id,
                            message_id=setup_msg_id, parse_mode="MarkdownV2",
                            reply_markup=InlineKeyboardMarkup(keyboard)
                        )
                        await update.message.delete()
                        return
                    except Exception:
                        pass
                await update.message.reply_text(
                    text, parse_mode="MarkdownV2",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return
            else:
                lang = get_idioma(chat_key)
                err  = ("⚠️ Hora inválida\\. Usa formato `HH:MM` \\(ej: `20:00`\\)"
                        if lang == "es" else
                        "⚠️ Invalid time\\. Use format `HH:MM` \\(eg: `20:00`\\)")
                await update.message.reply_text(err, parse_mode="MarkdownV2")
                return

    # ── Adivina la Idol: captura respuestas de participantes ──
    if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
        partida_imp_gi  = get_partida(chat_key)
        imp_activo_gi   = partida_imp_gi and partida_imp_gi[2] in ("jugando", "adivinando")
        if not imp_activo_gi:
            gi_ronda_activa = gi_get_ronda_activa(chat_key, update.effective_chat.id)
            if gi_ronda_activa:
                gi_ronda_id   = gi_ronda_activa[0]
                gi_part_check = gi_get_participante(gi_ronda_id, user.id)
                if gi_part_check and gi_part_check[6]:   # activo=1
                    if gi_part_check[5] <= 0:            # sin vidas
                        return
                    lang_gi2 = get_idioma(chat_key)
                    ctx.bot_data[f"gi_intento_{chat_key}_{user.id}"] = texto
                    kbd_gi2 = [
                        [InlineKeyboardButton(
                            gi_t(lang_gi2, "gi_confirmar_btn"),
                            callback_data=f"gi:confirmar:{user.id}:{gi_ronda_id}"
                        )],
                        [InlineKeyboardButton(
                            gi_t(lang_gi2, "gi_btn_salir"),
                            callback_data="gi:salir"
                        )],
                    ]
                    await update.message.reply_text(
                        gi_t(lang_gi2, "gi_confirmar_msg").format(respuesta=esc(texto)),
                        parse_mode="MarkdownV2",
                        reply_markup=InlineKeyboardMarkup(kbd_gi2)
                    )
                    return

    partida = get_partida(chat_key)
    if not partida:
        return

    # ── Modo adivinanza del impostor ──
    if partida[2] == "adivinando":
        datos = ctx.bot_data.get(f"adivinando_{chat_key}")
        if not datos or user.id != datos["impostor_id"]:
            return

        # Guardar intento y mostrar botón de confirmación
        datos["intento"] = texto
        keyboard = [[InlineKeyboardButton(
            t(chat_key, "confirmar_adivinanza_btn"),
            callback_data=f"confirmar_adiv:{user.id}"
        )]]
        await update.message.reply_text(
            t(chat_key, "confirmar_adivinanza_msg").format(palabra=esc(texto)),
            parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # ── Modo pistas: detectar turno ──
    if partida[2] != "jugando":
        return

    turno_data = ctx.bot_data.get(f"turno_{chat_key}")
    if not turno_data:
        return

    orden = turno_data["orden"]
    index = turno_data["index"]

    if index >= len(orden) or user.id != orden[index]:
        return

    # Actualizar nombre por si cambió en Telegram
    actualizar_nombre_activo(chat_key, user.id, nombre(user))

    impostor_ids_raw = partida[5] or ""
    impostor_ids_set = set(int(i) for i in impostor_ids_raw.split(",") if i.strip())
    if user.id in impostor_ids_set and normalizar(texto) == normalizar(partida[4]):
        todos_jugadores = get_jugadores_activos(chat_key)
        impostores = [(uid, next((j[1] for j in todos_jugadores if j[0] == uid), str(uid))) for uid in impostor_ids_set]
        ctx.bot_data.pop(f"turno_{chat_key}", None)
        chat_id = update.effective_chat.id
        thread_id = get_thread_id(chat_key)
        await ctx.bot.send_message(
            chat_id,
            t(chat_key, "adivino").format(nombre=esc(nombre(user)), palabra=esc(partida[4])),
            parse_mode="MarkdownV2",
            message_thread_id=thread_id
        )
        msg = update.message
        nombre_map = {j[0]: j[1] for j in todos_jugadores}
        await _fin_impostores_ganan(
            chat_key, ctx, partida, todos_jugadores, impostores,
            None, partida[4], partida[3], {}, msg
        )
        return

    keyboard = [[InlineKeyboardButton(
        t(chat_key, "confirmar_pista_btn"),
        callback_data=f"confirmar_pista:{user.id}"
    )]]
    turno_data["pista_pendiente"] = texto

    intentos = turno_data.setdefault("intentos_pista", {})
    intentos[user.id] = intentos.get(user.id, 0) + 1

    if intentos[user.id] >= 3:
        # Tercer intento: confirmar automáticamente sin botón
        await update.message.reply_text(
            t(chat_key, "pista_auto_confirmada"),
            parse_mode="MarkdownV2"
        )
        # Simular confirmación directamente
        chat_id = update.effective_chat.id
        thread_id = get_thread_id(chat_key)
        turno_data.pop("pista_pendiente", None)
        intentos[user.id] = 0

        # Cancelar timer activo
        tarea = ctx.bot_data.pop(f"timer_{chat_key}", None)
        if tarea:
            tarea.cancel()

        turno_data["ya_dieron_pista"].add(user.id)
        siguiente_index = index + 1
        turno_data["index"] = siguiente_index

        # Mostrar pista en negrita en el chat
        try:
            await update.message.reply_text(
                f"💬 *{esc(nombre(user))}*: *{esc(texto)}*",
                parse_mode="MarkdownV2"
            )
        except Exception:
            pass

        if siguiente_index >= len(orden):
            ronda_pistas = turno_data.get("ronda_pistas", 1)
            jugadores_iniciales = turno_data.get("jugadores_iniciales", len(orden))

            if jugadores_iniciales == 3 and ronda_pistas == 1:
                ctx.bot_data.pop(f"turno_{chat_key}", None)
                jugadores_frescos = get_jugadores_activos(chat_key)
                vivos_ids = get_vivos(chat_key)
                vivos = [j for j in jugadores_frescos if j[0] in vivos_ids]
                nuevo_orden = list(vivos)
                random.shuffle(nuevo_orden)
                turno_lista = "\n".join(f"  {i+1}\\. {esc(j[1])}" for i, j in enumerate(nuevo_orden))
                ctx.bot_data[f"turno_{chat_key}"] = {
                    "orden": [j[0] for j in nuevo_orden],
                    "index": 0,
                    "ya_dieron_pista": set(),
                    "ronda_pistas": 2,
                    "jugadores_iniciales": jugadores_iniciales,
                    "intentos_pista": {}
                }
                await ctx.bot.send_message(chat_id, t(chat_key, "segunda_ronda").format(orden=turno_lista), parse_mode="MarkdownV2", message_thread_id=thread_id)
                primer = nuevo_orden[0]
                await _anunciar_turno(chat_key, primer[0], primer[1], chat_id, thread_id, ctx)
                return

            ctx.bot_data.pop(f"turno_{chat_key}", None)
            await ctx.bot.send_message(
                chat_id, t(chat_key, "todos_dieron_pista"), parse_mode="MarkdownV2",
                message_thread_id=thread_id,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(chat_key, "btn_abrir_votacion"), callback_data="abrir_votar")]])
            )
            # Timer de 30s para auto-abrir votación
            tarea_abv3 = asyncio.create_task(_timer_abrir_votacion(chat_key, chat_id, thread_id, ctx))
            ctx.bot_data[f"timer_abrir_votacion_{chat_key}"] = tarea_abv3
            return

        siguiente_id = orden[siguiente_index]
        jugadores_frescos = get_jugadores_activos(chat_key)
        nombre_siguiente = next((j[1] for j in jugadores_frescos if j[0] == siguiente_id), "?")
        await _anunciar_turno(chat_key, siguiente_id, nombre_siguiente, chat_id, thread_id, ctx)
        return

    await update.message.reply_text(
        t(chat_key, "confirmar_pista_msg").format(pista=esc(texto)),
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )



async def btn_confirmar_adivinanza(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_key = get_chat_key(update)
    user = query.from_user

    partida = get_partida(chat_key)
    if not partida or partida[2] != "adivinando":
        await query.answer()
        return

    datos = ctx.bot_data.get(f"adivinando_{chat_key}")
    if not datos or user.id != datos["impostor_id"]:
        await query.answer()
        return

    texto = datos.get("intento")
    if not texto:
        await query.answer()
        return

    palabra = datos["palabra"]
    jugadores = datos["jugadores"]
    categoria = datos["categoria"]
    detalle_votos = datos["detalle_votos"]
    impostores = datos["impostores"]
    vivos_restantes_ids = datos["vivos_restantes_ids"]
    impostores_vivos = datos["impostores_vivos"]
    inocentes_vivos_ids = datos["inocentes_vivos_ids"]
    impostor_ids_set = datos["impostor_ids_set"]

    ctx.bot_data.pop(f"adivinando_{chat_key}", None)
    tarea_adiv = ctx.bot_data.pop(f"timer_adiv_{chat_key}", None)
    if tarea_adiv:
        tarea_adiv.cancel()
    await query.answer(t(chat_key, "pista_confirmada"))
    await query.message.delete()

    chat_id = query.message.chat.id
    thread_id = get_thread_id(chat_key)

    async def send(text):
        return await ctx.bot.send_message(chat_id, text, parse_mode="MarkdownV2", message_thread_id=thread_id)

    if normalizar(texto) == normalizar(palabra):
        msg = await send(t(chat_key, "adivino").format(nombre=esc(nombre(user)), palabra=esc(palabra)))
        await _fin_impostores_ganan(
            chat_key, ctx, partida, jugadores, impostores,
            None, palabra, categoria, detalle_votos, msg
        )
    else:
        msg = await send(t(chat_key, "incorrecto").format(nombre=esc(nombre(user)), texto=esc(texto.lower())))
        if not impostores_vivos:
            await _fin_grupo_gana(chat_key, ctx, jugadores, impostores, palabra, categoria, detalle_votos, msg)
            return
        inocentes_restantes = [v for v in vivos_restantes_ids if v not in impostor_ids_set]
        if len(inocentes_restantes) <= 1:
            await _fin_impostores_ganan(
                chat_key, ctx, partida, jugadores, impostores,
                None, palabra, categoria, detalle_votos, msg, razon="supervivencia"
            )
            return
        await _nueva_ronda_pistas(chat_key, ctx, jugadores, vivos_restantes_ids, impostor_ids_set, palabra, categoria, msg)


async def _fin_grupo_gana(chat_key, ctx, jugadores, impostores, palabra, categoria, detalle_votos, _unused=None, bonus=False):
    logger.info(f"[FIN_GRUPO] iniciando chat_key={chat_key} palabra={palabra}")
    try:
        tarea_actual = asyncio.current_task()
        tarea = ctx.bot_data.pop(f"timer_{chat_key}", None)
        if tarea and tarea is not tarea_actual: tarea.cancel()
        tarea_adiv = ctx.bot_data.pop(f"timer_adiv_{chat_key}", None)
        if tarea_adiv and tarea_adiv is not tarea_actual: tarea_adiv.cancel()
        tarea_abv3 = ctx.bot_data.pop(f"timer_abrir_votacion_{chat_key}", None)
        if tarea_abv3 and tarea_abv3 is not tarea_actual: tarea_abv3.cancel()
        tarea_tv3 = ctx.bot_data.pop(f"timer_votacion_{chat_key}", None)
        if tarea_tv3 and tarea_tv3 is not tarea_actual: tarea_tv3.cancel()

        impostor_ids_set = set(j[0] for j in impostores)
        multiplicador = ctx.bot_data.pop(f"multiplicador_{chat_key}", 1)
        for j in jugadores:
            if j[0] not in impostor_ids_set:
                for _ in range(multiplicador):
                    sumar_victoria(chat_key, j[0])
                sumar_victoria_inocente(chat_key, j[0])
        for imp in impostores:
            sumar_derrota(chat_key, imp[0])
        if multiplicador > 1:
            logger.info(f"[FIN_GRUPO] puntos x{multiplicador} sumados")
        else:
            logger.info(f"[FIN_GRUPO] puntos sumados")

        with get_conn() as conn:
            row = conn.execute("SELECT chat_id FROM partidas WHERE chat_key=?", (chat_key,)).fetchone()
            conn.execute("INSERT INTO historial (chat_key, ganador, palabra, categoria) VALUES (?,?,?,?)",
                         (chat_key, "grupo", palabra, categoria))
            conn.execute("UPDATE partidas SET estado=\'terminada\' WHERE chat_key=?", (chat_key,))
        logger.info(f"[FIN_GRUPO] DB actualizada, row={row}")

        chat_id = row[0] if row else int(chat_key.split("_")[0])
        thread_id = get_thread_id(chat_key)
        marcador = get_marcador_global(chat_key)
        logger.info(f"[FIN_GRUPO] chat_id={chat_id} marcador={len(marcador)} jugadores")

        nombres_impostores = ", ".join(f"*{esc(i[1])}*" for i in impostores)
        texto_final = t(chat_key, "grupo_gana").format(
            impostores=nombres_impostores, palabra=esc(palabra),
            cat=esc(categoria)
        )
        logger.info(f"[FIN_GRUPO] texto generado len={len(texto_final)}, enviando...")
        await asyncio.shield(ctx.bot.send_message(chat_id, texto_final, parse_mode="MarkdownV2", message_thread_id=thread_id))
        img_buf = generar_imagen_marcador(chat_key, marcador)
        if img_buf:
            await asyncio.shield(ctx.bot.send_photo(chat_id, photo=img_buf, message_thread_id=thread_id))
        logger.info(f"[FIN_GRUPO] mensaje enviado OK")
    except BaseException as e:
        logger.error(f"[FIN_GRUPO] ERROR tipo={type(e).__name__}: {e}", exc_info=True)
        try:
            with get_conn() as conn:
                row2 = conn.execute("SELECT chat_id FROM partidas WHERE chat_key=?", (chat_key,)).fetchone()
            chat_id2 = row2[0] if row2 else int(chat_key.split("_")[0])
            thread_id2 = get_thread_id(chat_key)
            fb = (f"🎉 ¡El grupo ganó!\n\n"
                  f"Impostores: {', '.join(i[1] for i in impostores)}\n"
                  f"Palabra: {palabra} ({categoria})\n\n"
                  f"Usa /playimpostor para otra ronda")
            await ctx.bot.send_message(chat_id2, fb, message_thread_id=thread_id2)
        except Exception as e2:
            logger.error(f"[FIN_GRUPO] fallback fallido: {e2}")


async def _fin_impostores_ganan(chat_key, ctx, partida, jugadores, impostores, eliminado, palabra, categoria, detalle_votos, _unused=None, razon=None):
    logger.info(f"[FIN_IMPOSTORES] iniciando chat_key={chat_key} palabra={palabra} razon={razon}")
    try:
        tarea_actual = asyncio.current_task()
        tarea = ctx.bot_data.pop(f"timer_{chat_key}", None)
        if tarea and tarea is not tarea_actual: tarea.cancel()
        tarea_adiv = ctx.bot_data.pop(f"timer_adiv_{chat_key}", None)
        if tarea_adiv and tarea_adiv is not tarea_actual: tarea_adiv.cancel()

        impostor_ids_set = set(j[0] for j in impostores)
        multiplicador = ctx.bot_data.pop(f"multiplicador_{chat_key}", 1)
        for imp in impostores:
            for _ in range(multiplicador):
                sumar_victoria(chat_key, imp[0])
            sumar_victoria_impostor(chat_key, imp[0])
        for j in jugadores:
            if j[0] not in impostor_ids_set:
                sumar_derrota(chat_key, j[0])
        if multiplicador > 1:
            logger.info(f"[FIN_IMPOSTORES] puntos x{multiplicador} sumados")
        else:
            logger.info(f"[FIN_IMPOSTORES] puntos sumados")

        with get_conn() as conn:
            row = conn.execute("SELECT chat_id FROM partidas WHERE chat_key=?", (chat_key,)).fetchone()
            conn.execute("INSERT INTO historial (chat_key, ganador, palabra, categoria) VALUES (?,?,?,?)",
                         (chat_key, "impostor", palabra, categoria))
            conn.execute("UPDATE partidas SET estado=\'terminada\' WHERE chat_key=?", (chat_key,))
        logger.info(f"[FIN_IMPOSTORES] DB actualizada, row={row}")

        chat_id = row[0] if row else int(chat_key.split("_")[0])
        thread_id = get_thread_id(chat_key)
        marcador = get_marcador_global(chat_key)
        logger.info(f"[FIN_IMPOSTORES] chat_id={chat_id} marcador={len(marcador)} jugadores")

        nombres_impostores = ", ".join(f"*{esc(i[1])}*" for i in impostores)

        if razon == "supervivencia":
            desc = t(chat_key, "desc_supervivencia")
        elif eliminado is None:
            desc = t(chat_key, "desc_adivino")
        else:
            desc = t(chat_key, "desc_error_voto").format(nombre=esc(eliminado[1]))

        texto_final = t(chat_key, "impostores_ganan").format(
            impostores=nombres_impostores, desc=desc,
            palabra=esc(palabra), cat=esc(categoria)
        )
        logger.info(f"[FIN_IMPOSTORES] texto generado len={len(texto_final)}, enviando...")
        await asyncio.shield(ctx.bot.send_message(chat_id, texto_final, parse_mode="MarkdownV2", message_thread_id=thread_id))
        img_buf = generar_imagen_marcador(chat_key, marcador)
        if img_buf:
            await asyncio.shield(ctx.bot.send_photo(chat_id, photo=img_buf, message_thread_id=thread_id))
        logger.info(f"[FIN_IMPOSTORES] mensaje enviado OK")
    except BaseException as e:
        logger.error(f"[FIN_IMPOSTORES] ERROR tipo={type(e).__name__}: {e}", exc_info=True)
        try:
            with get_conn() as conn:
                row2 = conn.execute("SELECT chat_id FROM partidas WHERE chat_key=?", (chat_key,)).fetchone()
            chat_id2 = row2[0] if row2 else int(chat_key.split("_")[0])
            thread_id2 = get_thread_id(chat_key)
            fb = (f"🕵️ ¡Los impostores ganaron!\n\n"
                  f"Eran: {', '.join(i[1] for i in impostores)}\n"
                  f"Palabra: {palabra} ({categoria})\n\n"
                  f"Usa /playimpostor para otra ronda")
            await ctx.bot.send_message(chat_id2, fb, message_thread_id=thread_id2)
        except Exception as e2:
            logger.error(f"[FIN_IMPOSTORES] fallback fallido: {e2}")


# ── Tabla de homoglyphs: caracteres decorativos → letras latinas ──────────────
_HOMOGLYPHS = {
    # Runas nórdicas usadas como lookalikes
    'ᛕ': 'K', 'ᚱ': 'R', 'ᚦ': 'TH', 'ᚢ': 'U', 'ᚠ': 'F', 'ᚾ': 'N',
    'ᛁ': 'I', 'ᛃ': 'J', 'ᛏ': 'T', 'ᛒ': 'B', 'ᛖ': 'E', 'ᛗ': 'M',
    'ᛚ': 'L', 'ᛜ': 'NG', 'ᛞ': 'D', 'ᛟ': 'O',
    # Sílabas canadienses usadas como letras
    'ᑌ': 'U', 'ᒎ': 'J', 'ᑎ': 'N', 'ᑕ': 'C', 'ᑖ': 'C', 'ᐁ': 'E',
    'ᐃ': 'I', 'ᐅ': 'O', 'ᐊ': 'A', 'ᐸ': 'P', 'ᑭ': 'K', 'ᑯ': 'Q',
    'ᒐ': 'J', 'ᒥ': 'M', 'ᓇ': 'N', 'ᓴ': 'S', 'ᓯ': 'S', 'ᔑ': 'SH',
    'ᕐ': 'R', 'ᕙ': 'F', 'ᖃ': 'Q', 'ᖠ': 'L', 'ᗑ': 'G',
    # Cherokee usados decorativamente
    'Ꭵ': 'I', 'Ꭺ': 'A', 'Ꭻ': 'J', 'Ꭼ': 'E', 'Ꮋ': 'M', 'Ꮮ': 'L',
    'Ꮩ': 'V', 'Ꭱ': 'R', 'Ꮑ': 'N', 'Ꮪ': 'S', 'Ꮤ': 'T', 'Ꮝ': 'S',
    # Latin Extended con ganchos usados decorativamente
    'Ƴ': 'Y', 'ƴ': 'y', 'Ɣ': 'Y', 'Ʌ': 'V', 'Ɯ': 'M', 'Ƨ': 'S',
    'Ƽ': 'S', 'ƺ': 'z', 'Ɉ': 'J', 'Ƈ': 'C', 'ƈ': 'c',
    # Símbolos decorativos → eliminar
    '†': '', '‡': '', '§': '', '©': '', '®': '', '™': '',
    '°': '', '•': '', '◦': '', '▪': '', '▫': '',
    '★': '', '☆': '', '♦': '', '♣': '', '♠': '', '♥': '', '♡': '',
    '❤': '', '✿': '', '❀': '', '✦': '', '✧': '',
    '「': '', '」': '', '【': '', '】': '', '《': '', '》': '',
    '〖': '', '〗': '', '〔': '', '〕': '',
    '✞': '', '✝': '', '⊹': '', '×': 'x',
    '⌈': '', '⌉': '', '⌊': '', '⌋': '',
    '⟨': '', '⟩': '', '⟦': '', '⟧': '',
    'ꓰ': '', 'ꓱ': '',
}
# Rangos de codepoints a descartar (modificadores tiny, combinadores, etc.)
_DISCARD_RANGES = [
    (0x02B0, 0x02FF),  # Spacing Modifier Letters (superscript tiny)
    (0x0300, 0x036F),  # Combining Diacritical Marks
    (0x1AB0, 0x1AFF),  # Combining Diacritical Marks Extended
    (0x1D00, 0x1D7F),  # Phonetic Extensions (letras fonéticas tiny)
    (0x1D80, 0x1DBF),  # Phonetic Extensions Supplement
    (0x1DC0, 0x1DFF),  # Combining Diacritical Marks Supplement
    (0x20D0, 0x20FF),  # Combining Diacritical Marks for Symbols
    (0x2700, 0x27BF),  # Dingbats
    (0x2B00, 0x2BFF),  # Miscellaneous Symbols and Arrows
    (0xFE00, 0xFE0F),  # Variation Selectors
]

def limpiar_nombre_tabla(nombre):
    """Convierte nombres fancy de Telegram a texto legible para la tabla del marcador.

    Ejemplos:
      '𝓙𝓸𝓼𝓮 𝓒𝓻𝓾𝔃'              → 'Jose Cruz'
      '†𝑻𝒓𝒊𝒔𝒉† (TAK Remix)🇳🇵'  → 'Trish (TAK Remix)🇳🇵'
      '~ ᛕƳᑌᒎᎥᑎ~'               → 'KYUJIN'
    """
    import unicodedata as _ud, re as _re
    # 1. NFKC: resuelve fuentes math bold/italic/script → ASCII
    s = _ud.normalize("NFKC", nombre)
    # 2. Homoglyphs: runas/sílabas canadienses → letras latinas; símbolos → vacío
    result = ""
    for c in s:
        result += _HOMOGLYPHS.get(c, c)
    s = result
    # 3. Filtrar char a char
    clean = ""
    for c in s:
        cp = ord(c)
        cat = _ud.category(c)
        if any(lo <= cp <= hi for lo, hi in _DISCARD_RANGES):
            continue
        if 0x1F1E0 <= cp <= 0x1F1FF or 0x1F300 <= cp <= 0x1FFFF:
            clean += c; continue
        if cat.startswith('L'):
            clean += c; continue
        if cat.startswith('N'):
            clean += c; continue
        if cat == 'Zs' or c == ' ':
            clean += ' '; continue
        if c in "-.,!?()'":
            clean += c; continue
    clean = _re.sub(r'\s+', ' ', clean).strip()
    return (clean or nombre)[:7]

def generar_imagen_marcador(chat_key, jugadores):
    """Genera un PNG con la tabla del marcador y devuelve bytes."""
    try:
        FONT_SIZE = 22
        font       = _get_font(FONT_SIZE)
        font_bold  = _get_font(FONT_SIZE, bold=True)
        font_title = _get_font(26, bold=True)
        logger.info(f"[MARCADOR] Unifont={os.path.exists(_FONT_UNIFONT)} CJK={os.path.exists(_FONT_CJK_REGULAR)} NotoSans={os.path.exists(_FONT_REGULAR)}")
        # Colores
        BG     = (30,  30,  46)
        HEADER = (49,  50,  68)
        ROW_A  = (40,  40,  58)
        ROW_B  = (35,  35,  52)
        TEXT   = (220, 220, 235)
        ACCENT = (137, 180, 250)
        GREEN  = (166, 227, 161)
        RED    = (243, 139, 168)
        GRAY   = (150, 150, 170)
        GOLD   = (255, 215,   0)
        SILVER = (192, 192, 192)
        BRONZE = (205, 127,  50)
        LINE   = (69,  71,  90)

        PAD   = 20
        ROW_H = 38
        COL_W = [40, 150, 50, 50, 65]   # #, Jugador, V, D, Bal
        COLS_ES = ["#", "Jugador", "V", "D", "Bal"]
        COLS_EN = ["#", "Player",  "V", "D", "Bal"]
        lang = get_idioma(chat_key)
        COLS = COLS_EN if lang == "en" else COLS_ES

        filas = []
        for j in jugadores:
            nombre_j = limpiar_nombre_tabla(j[1])
            v, d = j[2], j[3]
            bal = v - d
            bal_str = f"+{bal}" if bal > 0 else str(bal)
            filas.append((nombre_j, v, d, bal_str))

        total_w = PAD * 2 + sum(COL_W)
        title_h = 48
        total_h = PAD + title_h + ROW_H + ROW_H * len(filas) + PAD

        img  = Image.new("RGB", (total_w, total_h), BG)
        draw = ImageDraw.Draw(img)

        # Título
        titulo = "Leaderboard" if lang == "en" else "Marcador"
        draw.text((PAD, PAD // 2 + 2), f"  {titulo}", font=font_title, fill=GOLD)

        # Header
        y = PAD + title_h
        draw.rectangle([PAD, y, total_w - PAD, y + ROW_H], fill=HEADER)
        x = PAD + 8
        for col, w in zip(COLS, COL_W):
            draw.text((x, y + 8), col, font=font_bold, fill=ACCENT)
            x += w

        # Filas
        for idx, (nombre_j, v, d, bal) in enumerate(filas):
            y += ROW_H
            draw.rectangle([PAD, y, total_w - PAD, y + ROW_H - 1], fill=ROW_A if idx % 2 == 0 else ROW_B)
            draw.line([PAD, y + ROW_H - 1, total_w - PAD, y + ROW_H - 1], fill=LINE, width=1)

            pos = idx + 1
            x = PAD + 8

            # Número de posición con color
            pos_color = GOLD if pos == 1 else SILVER if pos == 2 else BRONZE if pos == 3 else GRAY
            draw.text((x, y + 8), str(pos), font=font_bold, fill=pos_color)
            x += COL_W[0]

            draw_text_smart(draw, (x, y + 8), nombre_j[:14], FONT_SIZE, TEXT)
            x += COL_W[1]

            draw.text((x, y + 8), str(v), font=font, fill=GREEN)
            x += COL_W[2]

            draw.text((x, y + 8), str(d), font=font, fill=RED)
            x += COL_W[3]

            bal_color = GREEN if bal.startswith("+") else RED if bal.startswith("-") else GRAY
            draw.text((x, y + 8), bal, font=font_bold, fill=bal_color)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf
    except Exception as e:
        logger.error(f"[generar_imagen_marcador] error: {e}")
        return None

def generar_imagen_roles(chat_key, jugadores):
    """Genera un PNG con la tabla de roles y devuelve bytes."""
    try:
        FONT_SIZE = 24
        font       = _get_font(FONT_SIZE)
        font_bold  = _get_font(FONT_SIZE, bold=True)
        font_title = _get_font(28, bold=True)

        BG        = (22,  22,  35)
        HEADER_BG = (42,  44,  66)
        ROW_A     = (32,  33,  50)
        ROW_B     = (28,  29,  45)
        ACCENT    = (130, 170, 255)
        TEXT      = (215, 215, 230)
        GOLD      = (255, 210,  50)
        GRAY      = (130, 130, 150)
        PURPLE    = (200, 140, 255)   # impostor
        TEAL      = (100, 220, 200)   # inocente
        GREEN     = (140, 220, 140)
        LINE      = (55,  57,  80)
        TITLE_BG  = (30,  30,  50)

        lang = get_idioma(chat_key)
        if lang == "en":
            COLS  = ["#", "Player",  "Imp", "W",  "Ino", "W" ]
            titulo = "ROLES"
        else:
            COLS  = ["#", "Jugador", "Imp", "W",  "Ino", "W" ]
            titulo = "ROLES"

        COL_W = [42, 155, 52, 48, 52, 48]

        PAD      = 24
        ROW_H    = 42
        title_h  = 56
        total_w  = PAD * 2 + sum(COL_W)
        total_h  = title_h + ROW_H + ROW_H * len(jugadores) + PAD

        img  = Image.new("RGB", (total_w, total_h), BG)
        draw = ImageDraw.Draw(img)

        # Título
        draw.rectangle([0, 0, total_w, title_h], fill=TITLE_BG)
        draw.text((PAD, 12), titulo, font=font_title, fill=PURPLE)
        draw.rectangle([0, title_h - 2, total_w, title_h], fill=PURPLE)

        # Subheader con leyenda
        y = title_h
        draw.rectangle([0, y, total_w, y + ROW_H], fill=HEADER_BG)
        x = PAD
        for i, (col, w) in enumerate(zip(COLS, COL_W)):
            # Colorear headers de impostor/inocente
            col_color = PURPLE if col in ("Imp",) else TEAL if col in ("Ino",) else ACCENT
            # Las W alternan color según su columna
            if col == "W":
                col_color = PURPLE if i == 3 else TEAL
            draw.text((x, y + 10), col, font=font_bold, fill=col_color)
            x += w
        draw.line([0, y + ROW_H - 1, total_w, y + ROW_H - 1], fill=LINE, width=1)

        # Filas
        for idx, j in enumerate(jugadores):
            y += ROW_H
            draw.rectangle([0, y, total_w, y + ROW_H], fill=ROW_A if idx % 2 == 0 else ROW_B)
            draw.line([0, y + ROW_H - 1, total_w, y + ROW_H - 1], fill=LINE, width=1)

            nom  = limpiar_nombre_tabla(j[0])
            imp  = j[1]   # veces impostor
            ino  = j[2]   # veces inocente
            wimp = j[3]   # victorias impostor
            wino = j[4]   # victorias inocente

            # % de victoria como impostor
            pct_imp = str(wimp)
            pct_ino = str(wino)

            pos = idx + 1
            x = PAD

            draw.text((x, y + 10), str(pos), font=font_bold, fill=GOLD if pos == 1 else GRAY)
            x += COL_W[0]

            draw_text_smart(draw, (x, y + 10), nom[:14], FONT_SIZE, TEXT)
            x += COL_W[1]

            draw.text((x, y + 10), str(imp), font=font_bold, fill=PURPLE)
            x += COL_W[2]

            draw.text((x, y + 10), pct_imp, font=font, fill=GREEN if wimp > 0 else GRAY)
            x += COL_W[3]

            draw.text((x, y + 10), str(ino), font=font_bold, fill=TEAL)
            x += COL_W[4]

            draw.text((x, y + 10), pct_ino, font=font, fill=GREEN if wino > 0 else GRAY)

        draw.rectangle([0, total_h - 3, total_w, total_h], fill=PURPLE)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf
    except Exception as e:
        logger.error(f"[generar_imagen_roles] error: {e}")
        return None

def formatear_tabla(chat_key, jugadores):
    MEDALLAS = {1: "🥇", 2: "🥈", 3: "🥉"}
    filas = []
    for j in jugadores:
        nombre_j = limpiar_nombre_tabla(j[1])
        v = j[2]
        d = j[3]
        balance = v - d
        bal_str = f"+{balance}" if balance > 0 else str(balance)
        filas.append((nombre_j, v, d, bal_str))

    col = t(chat_key, "col_jugador")
    max_nombre = 6
    encabezado = f"    {col:<{max_nombre}}  V    D   Bal"
    separador  = "─" * len(encabezado)
    lineas = [encabezado, separador]
    for i, (nombre_j, v, d, bal) in enumerate(filas, 1):
        prefijo = MEDALLAS.get(i, f"{i:<3} ")
        pad = " " if i in MEDALLAS else ""
        lineas.append(f"{prefijo}{pad}{nombre_j:<{max_nombre}}  {v:<4} {d:<4} {bal}")
    return "```\n" + "\n".join(lineas) + "\n```"


async def cmd_puntaje(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_key = get_chat_key(update)
    jugadores = get_marcador_global(chat_key)

    if not jugadores:
        await update.message.reply_text(
            t(chat_key, "sin_estadisticas"),
            parse_mode="MarkdownV2"
        )
        return

    img_buf = generar_imagen_marcador(chat_key, jugadores)
    if img_buf:
        await update.message.reply_photo(photo=img_buf)
    else:
        tabla = formatear_tabla(chat_key, jugadores)
        await update.message.reply_text(
            t(chat_key, "marcador").format(tabla=tabla),
            parse_mode="MarkdownV2"
        )


async def cmd_resetjugador(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_key = get_chat_key(update)
    user     = update.effective_user

    if not (BOT_OWNER_ID and user.id == BOT_OWNER_ID):
        await update.message.reply_text(t(chat_key, "resetjugador_solo_admin"))
        return

    # Sin argumentos → listar todos los jugadores del grupo
    if not ctx.args and not (update.message.entities and
            any(e.type in ("mention","text_mention") for e in update.message.entities)):
        with get_conn() as conn:
            jugadores = conn.execute(
                "SELECT user_id, username FROM jugadores WHERE chat_key=? "
                "AND (victorias>0 OR derrotas>0 OR veces_impostor>0) ORDER BY username",
                (chat_key,)
            ).fetchall()
        if not jugadores:
            await update.message.reply_text("📭 No hay jugadores con estadísticas en este grupo.")
            return
        lista = "\n".join(f"  • {j[1]} (`{j[0]}`)" for j in jugadores)
        await update.message.reply_text(
            f"Jugadores en el ranking:\n\n{lista}\n\n"
            f"Usa `/resetjugador ID` con el número de ID para resetear.",
            parse_mode="Markdown"
        )
        return

    target_id   = None
    target_name = None

    # 1. text_mention: clic en nombre sin @username
    if update.message.entities:
        for e in update.message.entities:
            if e.type == "text_mention" and e.user:
                target_id   = e.user.id
                target_name = e.user.first_name
                break

    busqueda = " ".join(ctx.args).strip().lstrip("@") if ctx.args else ""

    # 2. Puede ser un user_id numérico directo
    if target_id is None and busqueda.isdigit():
        target_id = int(busqueda)
        with get_conn() as conn:
            row = conn.execute(
                "SELECT username FROM jugadores WHERE chat_key=? AND user_id=?",
                (chat_key, target_id)
            ).fetchone()
        target_name = row[0] if row else str(target_id)

    # 3. Buscar por nombre (coincidencia exacta o parcial)
    if target_id is None and busqueda:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT user_id, username FROM jugadores WHERE chat_key=? "
                "AND LOWER(username) LIKE LOWER(?) ORDER BY victorias DESC LIMIT 1",
                (chat_key, f"%{busqueda}%")
            ).fetchone()
        if row:
            target_id, target_name = row[0], row[1]

    if target_id is None:
        await update.message.reply_text(
            t(chat_key, "resetjugador_no_encontrado").format(nombre=esc(busqueda)),
            parse_mode="MarkdownV2"
        )
        return

    with get_conn() as conn:
        conn.execute(
            "UPDATE jugadores SET victorias=0, derrotas=0, veces_impostor=0, veces_inocente=0, "
            "victorias_impostor=0, victorias_inocente=0 WHERE chat_key=? AND user_id=?",
            (chat_key, target_id)
        )

    await update.message.reply_text(
        t(chat_key, "resetjugador_ok").format(nombre=esc(target_name or busqueda)),
        parse_mode="MarkdownV2"
    )


async def cmd_resetimpostor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_key = get_chat_key(update)
    user = update.effective_user
    chat = update.effective_chat

    es_owner = bool(BOT_OWNER_ID and user.id == BOT_OWNER_ID)
    try:
        member = await chat.get_member(user.id)
        es_admin = member.status in ("administrator", "creator")
    except Exception:
        es_admin = False

    if not es_owner and not es_admin:
        await update.message.reply_text(t(chat_key, "solo_admin_reset"))
        return

    with get_conn() as conn:
        conn.execute(
            "UPDATE jugadores SET victorias=0, derrotas=0 WHERE chat_key=?",
            (chat_key,)
        )

    await update.message.reply_text(t(chat_key, "reset_ok"), parse_mode="MarkdownV2")


async def cmd_resetroles(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_key = get_chat_key(update)
    user = update.effective_user
    chat = update.effective_chat

    es_owner = bool(BOT_OWNER_ID and user.id == BOT_OWNER_ID)
    try:
        member = await chat.get_member(user.id)
        es_admin = member.status in ("administrator", "creator")
    except Exception:
        es_admin = False

    if not es_owner and not es_admin:
        await update.message.reply_text(t(chat_key, "solo_admin_reset"))
        return

    with get_conn() as conn:
        conn.execute(
            "UPDATE jugadores SET veces_impostor=0, veces_inocente=0, victorias_impostor=0, victorias_inocente=0 WHERE chat_key=?",
            (chat_key,)
        )

    await update.message.reply_text(t(chat_key, "resetroles_ok"), parse_mode="MarkdownV2")


async def cmd_cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_key = get_chat_key(update)
    user = update.effective_user
    partida = get_partida(chat_key)

    if not partida or partida[2] == "terminada":
        await update.message.reply_text(t(chat_key, "sin_partida_activa"))
        return
    es_owner = bool(BOT_OWNER_ID and user.id == BOT_OWNER_ID)
    try:
        member   = await update.effective_chat.get_member(user.id)
        es_admin = member.status in ("administrator", "creator")
    except Exception:
        es_admin = False
    if partida[8] != user.id and not es_admin and not es_owner:
        await update.message.reply_text(t(chat_key, "solo_creador_cancelar"))
        return

    tarea = ctx.bot_data.pop(f"timer_{chat_key}", None)
    if tarea:
        tarea.cancel()
    tarea_adiv = ctx.bot_data.pop(f"timer_adiv_{chat_key}", None)
    if tarea_adiv:
        tarea_adiv.cancel()
    tarea_lobby = ctx.bot_data.pop(f"timeout_lobby_{chat_key}", None)
    if tarea_lobby:
        tarea_lobby.cancel()
    tarea_abv = ctx.bot_data.pop(f"timer_abrir_votacion_{chat_key}", None)
    if tarea_abv:
        tarea_abv.cancel()
    tarea_tv = ctx.bot_data.pop(f"timer_votacion_{chat_key}", None)
    if tarea_tv:
        tarea_tv.cancel()
    ctx.bot_data.pop(f"turno_{chat_key}", None)
    ctx.bot_data.pop(f"adivinando_{chat_key}", None)
    ctx.bot_data.pop(f"votos_{chat_key}", None)
    ctx.bot_data.pop(f"revotacion_{chat_key}", None)

    with get_conn() as conn:
        conn.execute("UPDATE partidas SET estado='terminada' WHERE chat_key=?", (chat_key,))
    await update.message.reply_text(t(chat_key, "cancelado"), parse_mode="MarkdownV2")


async def cmd_addword(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_key = get_chat_key(update)
    user = update.effective_user
    chat = update.effective_chat

    es_owner = bool(BOT_OWNER_ID and user.id == BOT_OWNER_ID)
    try:
        member = await chat.get_member(user.id)
        es_admin = member.status in ("administrator", "creator")
    except Exception:
        es_admin = False

    if not es_owner and not es_admin:
        await update.message.reply_text(t(chat_key, "addword_solo_admin"))
        return

    if not ctx.args:
        await update.message.reply_text(t(chat_key, "addword_uso"), parse_mode="MarkdownV2")
        return

    palabra = " ".join(ctx.args).strip()
    agregada = add_palabra_custom(chat_key, palabra)

    if agregada:
        await update.message.reply_text(
            t(chat_key, "addword_ok").format(palabra=esc(palabra)),
            parse_mode="MarkdownV2"
        )
    else:
        await update.message.reply_text(
            t(chat_key, "addword_ya_existe").format(palabra=esc(palabra)),
            parse_mode="MarkdownV2"
        )


async def cmd_removeword(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_key = get_chat_key(update)
    user = update.effective_user
    chat = update.effective_chat

    es_owner = bool(BOT_OWNER_ID and user.id == BOT_OWNER_ID)
    try:
        member = await chat.get_member(user.id)
        es_admin = member.status in ("administrator", "creator")
    except Exception:
        es_admin = False

    if not es_owner and not es_admin:
        await update.message.reply_text(t(chat_key, "removeword_solo_admin"))
        return

    if not ctx.args:
        await update.message.reply_text(t(chat_key, "removeword_uso"), parse_mode="MarkdownV2")
        return

    palabra = " ".join(ctx.args).strip()
    eliminada = remove_palabra_custom(chat_key, palabra)

    if eliminada:
        await update.message.reply_text(
            t(chat_key, "removeword_ok").format(palabra=esc(palabra)),
            parse_mode="MarkdownV2"
        )
    else:
        await update.message.reply_text(
            t(chat_key, "removeword_no_existe").format(palabra=esc(palabra)),
            parse_mode="MarkdownV2"
        )


async def cmd_words(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_key = get_chat_key(update)
    palabras = get_palabras_custom(chat_key)

    if not palabras:
        await update.message.reply_text(t(chat_key, "words_vacia"), parse_mode="MarkdownV2")
        return

    lista = "\n".join(f"  {i+1}\\. {esc(p)}" for i, p in enumerate(palabras))
    await update.message.reply_text(
        t(chat_key, "words_lista").format(n=len(palabras), lista=lista),
        parse_mode="MarkdownV2"
    )



async def cmd_roles(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_key = get_chat_key(update)
    with get_conn() as conn:
        jugadores = conn.execute(
            """SELECT username, veces_impostor, veces_inocente,
                      victorias_impostor, victorias_inocente
               FROM jugadores
               WHERE chat_key=? AND (veces_impostor > 0 OR veces_inocente > 0)
               ORDER BY (veces_impostor + veces_inocente) DESC""",
            (chat_key,)
        ).fetchall()

    if not jugadores:
        await update.message.reply_text(t(chat_key, "roles_sin_datos"), parse_mode="MarkdownV2")
        return

    img_buf = generar_imagen_roles(chat_key, jugadores)
    if img_buf:
        await update.message.reply_photo(photo=img_buf)
    else:
        # Fallback texto
        col = t(chat_key, "col_jugador")
        encabezado = f"#   {col:<6}  Imp  W   Ino  W"
        separador  = "─" * len(encabezado)
        lineas = [encabezado, separador]
        for i, j in enumerate(jugadores, 1):
            nom = limpiar_nombre_tabla(j[0])
            vi, ino, wvi, wino = j[1], j[2], j[3], j[4]
            lineas.append(f"{i:<3} {nom:<6}  {vi:<4} {wvi:<4} {ino:<4} {wino}")
        tabla = "```\n" + "\n".join(lineas) + "\n```"
        await update.message.reply_text(
            t(chat_key, "roles_tabla").format(tabla=tabla),
            parse_mode="MarkdownV2"
        )


async def cmd_rivalidad(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Muestra estadísticas de votos mutuos entre dos jugadores.
    - /rivalidad @usuario  → yo contra ese jugador
    - /rivalidad @usuario1 @usuario2 → cualquiera contra cualquiera
    """
    chat_key = get_chat_key(update)
    user = update.effective_user

    menciones = []
    if update.message.entities:
        menciones = [e for e in update.message.entities
                     if e.type in ("mention", "text_mention")]

    if not menciones:
        await update.message.reply_text(t(chat_key, "rivalidad_uso"), parse_mode="MarkdownV2")
        return

    # Resolver menciones a (user_id, nombre)
    def resolver_mencion(m):
        if m.type == "text_mention" and m.user:
            return m.user.id, m.user.first_name
        elif m.type == "mention":
            username = update.message.text[m.offset+1:m.offset+m.length]
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT user_id, username FROM jugadores WHERE chat_key=? AND LOWER(username)=LOWER(?)",
                    (chat_key, username)
                ).fetchone()
            if row:
                return row[0], row[1]
            return None, username
        return None, "?"

    if len(menciones) == 1:
        # Modo: yo contra ese jugador
        id_b, name_b = resolver_mencion(menciones[0])
        if id_b is None:
            await update.message.reply_text(
                esc(f"⚠️ No encontré a @{name_b} en este grupo."),
                parse_mode="MarkdownV2"
            )
            return
        id_a = user.id
        name_a = nombre(user)
    else:
        # Modo: cualquiera contra cualquiera
        id_a, name_a = resolver_mencion(menciones[0])
        id_b, name_b = resolver_mencion(menciones[1])
        if id_a is None:
            await update.message.reply_text(
                esc(f"⚠️ No encontré a @{name_a} en este grupo."),
                parse_mode="MarkdownV2"
            )
            return
        if id_b is None:
            await update.message.reply_text(
                esc(f"⚠️ No encontré a @{name_b} en este grupo."),
                parse_mode="MarkdownV2"
            )
            return

    with get_conn() as conn:
        a_a_b = conn.execute(
            "SELECT COUNT(*) FROM votos_historial WHERE chat_key=? AND voter_id=? AND voted_id=?",
            (chat_key, id_a, id_b)
        ).fetchone()[0]
        a_b_a = conn.execute(
            "SELECT COUNT(*) FROM votos_historial WHERE chat_key=? AND voter_id=? AND voted_id=?",
            (chat_key, id_b, id_a)
        ).fetchone()[0]

    if a_a_b == 0 and a_b_a == 0:
        await update.message.reply_text(t(chat_key, "rivalidad_sin_datos"), parse_mode="MarkdownV2")
        return

    lang = get_idioma(chat_key)
    palabra_veces = "veces" if lang == "es" else "times"
    titulo = t(chat_key, "rivalidad_titulo")

    total = a_a_b + a_b_a
    bloques_a = round(a_a_b / total * 10) if total > 0 else 0
    bloques_b = 10 - bloques_a
    barra = "🟦" * bloques_a + "🟥" * bloques_b

    na = esc(name_a[:10])
    nb = esc(name_b[:10])

    msg = (
        titulo +
        f"🟦 *{na}* → *{nb}*: *{a_a_b}* {palabra_veces}\n"
        f"🟥 *{nb}* → *{na}*: *{a_b_a}* {palabra_veces}\n\n"
        f"{barra}"
    )

    await update.message.reply_text(msg, parse_mode="MarkdownV2")


async def cmd_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_key = get_chat_key(update)
    user = update.effective_user
    chat = update.effective_chat

    # Obtener todos los usuarios registrados en el grupo
    with get_conn() as conn:
        miembros = conn.execute(
            "SELECT user_id, username FROM jugadores WHERE chat_key=?",
            (chat_key,)
        ).fetchall()

    if not miembros:
        await update.message.reply_text("⚠️ No hay usuarios registrados en este grupo aún.")
        return

    # Obtener el mensaje personalizado (texto después del comando)
    texto_extra = ""
    if ctx.args:
        texto_extra = " ".join(ctx.args)

    # Construir menciones visibles por nombre
    nombres_visibles = " · ".join(
        "[" + uname + "](tg://user?id=" + str(uid) + ")"
        for uid, uname in miembros
    )

    msg = "📢 " + nombres_visibles
    if texto_extra:
        msg += "\n\n" + texto_extra

    await update.message.reply_text(msg, parse_mode="Markdown")


async def error_handler(update, ctx):
    error = ctx.error
    if isinstance(error, Conflict):
        logger.critical("⚠️ Conflicto de instancia. Saliendo...")
        os._exit(1)
    else:
        logger.error(f"Error: {error}")


async def set_commands(app):
    from telegram import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats

    # Comandos visibles para usuarios en español
    cmds_es = [
        BotCommand("playimpostor",  "Crear una nueva partida"),
        BotCommand("join",          "Unirse a la partida"),
        BotCommand("vote",          "Abrir votacion (solo creador)"),
        BotCommand("howtoplay",     "Como se juega"),
        BotCommand("score",         "Ver marcador"),
        BotCommand("roles",         "Ver estadisticas de roles"),
        BotCommand("language",      "Cambiar idioma"),
        BotCommand("cancel",        "Cancelar partida (solo creador)"),
    ]
    # Comandos visibles para usuarios en inglés
    cmds_en = [
        BotCommand("playimpostor",  "Create a new game"),
        BotCommand("join",          "Join the game"),
        BotCommand("vote",          "Open voting (creator only)"),
        BotCommand("howtoplay",     "How to play"),
        BotCommand("score",         "View scoreboard"),
        BotCommand("roles",         "View role stats"),
        BotCommand("language",      "Change language"),
        BotCommand("cancel",        "Cancel game (creator only)"),
    ]

    # Registrar para grupos (donde se juega)
    await app.bot.set_my_commands(cmds_es, scope=BotCommandScopeAllGroupChats(), language_code="es")
    await app.bot.set_my_commands(cmds_en, scope=BotCommandScopeAllGroupChats(), language_code="en")
    await app.bot.set_my_commands(cmds_es, scope=BotCommandScopeAllGroupChats())  # fallback grupos

    # Registrar para chat privado con el bot
    await app.bot.set_my_commands(cmds_es, scope=BotCommandScopeAllPrivateChats(), language_code="es")
    await app.bot.set_my_commands(cmds_en, scope=BotCommandScopeAllPrivateChats(), language_code="en")
    await app.bot.set_my_commands(cmds_es, scope=BotCommandScopeAllPrivateChats())  # fallback privado

    logger.info("✅ Comandos registrados en Telegram (grupos + privado, ES + EN).")

async def _limpiar_partidas_zombies(app):
    """Al arrancar, marca como terminadas las partidas que quedaron activas
    tras un reinicio inesperado del bot. Notifica en el grupo."""
    estados_activos = ("jugando", "adivinando", "iniciando", "esperando")
    with get_conn() as conn:
        zombies = conn.execute(
            "SELECT chat_key, chat_id FROM partidas WHERE estado IN ({})".format(
                ",".join("?" * len(estados_activos))
            ),
            estados_activos
        ).fetchall()
        if zombies:
            conn.execute(
                "UPDATE partidas SET estado='terminada' WHERE estado IN ({})".format(
                    ",".join("?" * len(estados_activos))
                ),
                estados_activos
            )
    for chat_key, chat_id in zombies:
        try:
            thread_id = get_thread_id(chat_key)
            lang = get_idioma(chat_key)
            if lang == "es":
                msg = "⚠️ El bot se reinició y la partida fue cancelada\\.\n\n_Usa /playimpostor para comenzar una nueva\\._"
            else:
                msg = "⚠️ The bot restarted and the game was cancelled\\.\n\n_Use /playimpostor to start a new one\\._"
            await app.bot.send_message(chat_id, msg,
                                       parse_mode="MarkdownV2",
                                       message_thread_id=thread_id)
        except Exception as e:
            logger.warning(f"[ZOMBIE] No se pudo notificar {chat_key}: {e}")
    if zombies:
        logger.info(f"[ZOMBIE] {len(zombies)} partidas zombie limpiadas")

    # Restaurar tareas de countdown para programas pendientes
    pendientes = []
    with get_conn() as conn:
        pendientes = conn.execute(
            "SELECT * FROM programacion WHERE estado='pendiente'"
        ).fetchall()
    now_ts = int(datetime.now(_tz.utc).timestamp())
    for prog in pendientes:
        prog_id   = prog[0]
        chat_key_ = prog[1]
        chat_id_  = prog[2]
        thread_id_= prog[3]
        hora_ts_  = prog[4]
        puntos_   = prog[5]
        tz_       = prog[6] if prog[6] is not None else 0
        msg_id_   = prog[7]
        if hora_ts_ <= now_ts:
            # Pasó mientras el bot estaba caído → iniciar ahora
            with get_conn() as conn:
                conn.execute("UPDATE programacion SET estado='activa' WHERE id=?", (prog_id,))
            asyncio.create_task(
                _iniciar_partida_programada(chat_key_, chat_id_, thread_id_, puntos_, tz_, msg_id_, app.bot, app.bot_data)
            )
            logger.info(f"[PROGRAMA] Iniciando programa atrasado {chat_key_}")
        else:
            tarea = asyncio.create_task(
                _task_programa_countdown(chat_key_, prog_id, app.bot, app.bot_data)
            )
            app.bot_data[f"programa_tarea_{prog_id}"] = tarea
            logger.info(f"[PROGRAMA] Countdown restaurado {chat_key_} ({len(pendientes)} programas)")

        # ── Restaurar rondas activas de GI (relanzar tareas sin cerrarlas) ──
    with get_conn() as conn:
        gi_rondas_activas = conn.execute(
            "SELECT id, chat_key, chat_id FROM gi_rondas WHERE estado='activa'"
        ).fetchall()
    for r in gi_rondas_activas:
        try:
            tarea_gi_r = asyncio.create_task(
                _gi_ronda_task(r[1], r[0], app.bot, app.bot_data)
            )
            app.bot_data[f"gi_ronda_{r[1]}"] = tarea_gi_r
            logger.info(f"[IDOL] Ronda {r[0]} restaurada en {r[1]}")
        except Exception as e:
            logger.warning(f"[IDOL] No se pudo restaurar ronda {r[0]}: {e}")

    # ── Restaurar countdowns de GI pendientes ──
    with get_conn() as conn:
        gi_pendientes_prog = conn.execute(
            "SELECT * FROM gi_programacion WHERE estado='pendiente'"
        ).fetchall()
    now_gi = int(datetime.now(_tz.utc).timestamp())
    for gp in gi_pendientes_prog:
        # gi_programacion: id(0) idol_name(1) file_id(2) file_id_reveal(3) hint1(4) hint2(5) hint3(6) inicio_ts(7) fin_ts(8) tz_offset(9) estado(10)
        if gp[7] <= now_gi:
            with get_conn() as conn:
                conn.execute("UPDATE gi_programacion SET estado='activa' WHERE id=?", (gp[0],))
            asyncio.create_task(_gi_countdown(gp[0], app.bot, app.bot_data))
            logger.info(f"[IDOL] Countdown atrasado restaurado prog_id={gp[0]}")
        else:
            tarea_gi = asyncio.create_task(_gi_countdown(gp[0], app.bot, app.bot_data))
            app.bot_data[f"gi_countdown_{gp[0]}"] = tarea_gi
            logger.info(f"[IDOL] Countdown restaurado prog_id={gp[0]}")


# ══════════════════════════════════════════════════════════════
# ADIVINA LA IDOL
# ══════════════════════════════════════════════════════════════

GI_TEXTOS = {
    "es": {
        "gi_no_owner":          "⚠️ Solo el creador del bot puede usar este comando\\.",
        "gi_solo_privado":      "⚠️ Este comando solo funciona en chat privado con el bot\\.",
        "gi_grupos_vacio":      "📭 El bot no ha registrado ningún grupo aún\\.",
        "gi_sin_pistas":        "_Aún no hay pistas\\._",
        "gi_hint1_reveal":      "👥 *Pista 1:* El grupo tiene *{hint1}* miembros",
        "gi_hint2_reveal":      "🏢 *Pista 2:* Pertenece a *{hint2}*",
        "gi_hint3_reveal":      "🎤 *Pista 3:* El grupo es *{hint3}*",
        "gi_nueva_pista":       "💡 *¡Nueva pista revelada\\!*\n\n{pista}",
        "gi_btn_participar":    "✋ Participar",
        "gi_btn_salir":         "🚪 Detener participación",
        "gi_ya_participa":      "Ya estás participando.",
        "gi_unido":             "✅ ¡Ahora participas! Tienes 5 vidas.",
        "gi_salido":            "👋 Dejaste de participar.",
        "gi_no_participa":      "No estás participando en esta ronda.",
        "gi_sin_vidas":         "💀 Ya no tienes vidas en esta ronda.",
        "gi_confirmar_btn":     "✅ Confirmar respuesta",
        "gi_confirmar_msg":     "¿Confirmas *{respuesta}* como tu respuesta\\?",
        "gi_incorrecto":        "❌ Incorrecto\\. Te quedan *{vidas}* vida\\(s\\)\\.",
        "gi_eliminado":         "💀 *{nombre}* agotó sus vidas y fue eliminado\\.",
        "gi_ganador":           (
            "🎉 *¡{nombre} adivinó\\!*\n\n"
            "La idol era *{idol}*\\.\n"
            "¡Ganó *{puntos}* punto\\(s\\)\\!\n\n"
            "_Usa /giscore para ver el marcador\\._"
        ),
        "gi_ronda_sin_ganador": (
            "⏰ *¡Tiempo\\!*\n\n"
            "Nadie adivinó\\. La idol era *{idol}*\\.\n\n"
            "_Usa /giscore para ver el marcador\\._"
        ),
        "gi_ronda_caption":     (
            "🎤 *ADIVINA LA IDOL*\n\n"
            "⏰ Termina: *{fin}*\n"
            "🎯 Puntos: *{puntos}*\n"
            "{pistas}\n\n"
            "_Escribe el nombre para ganar_"
        ),
        "gi_score_vacio":       "📊 No hay puntos registrados aún\\.",
        "gi_score_titulo":      "🏆 *Marcador \\— Adivina la Idol:*\n\n{tabla}",
        "gi_reset_ok":          "🔄 Marcador de Adivina la Idol reseteado\\.",
        "gi_cancelado":         "❌ Ronda cancelada\\.",
        "gi_no_ronda":          "⚠️ No hay ronda activa en este grupo\\.",
        "gi_div_incorrecta":    "❌ Esta ronda es solo para {div}\\.",
    },
    "en": {
        "gi_no_owner":          "⚠️ Only the bot creator can use this command\\.",
        "gi_solo_privado":      "⚠️ This command only works in private chat with the bot\\.",
        "gi_grupos_vacio":      "📭 The bot hasn't registered any groups yet\\.",
        "gi_sin_pistas":        "_No hints yet\\._",
        "gi_hint1_reveal":      "👥 *Hint 1:* The group has *{hint1}* members",
        "gi_hint2_reveal":      "🏢 *Hint 2:* They belong to *{hint2}*",
        "gi_hint3_reveal":      "🎤 *Hint 3:* The group is *{hint3}*",
        "gi_nueva_pista":       "💡 *New hint revealed\\!*\n\n{pista}",
        "gi_btn_participar":    "✋ Join",
        "gi_btn_salir":         "🚪 Leave",
        "gi_ya_participa":      "You're already participating.",
        "gi_unido":             "✅ You joined! You have 5 lives.",
        "gi_salido":            "👋 You left the round.",
        "gi_no_participa":      "You're not participating in this round.",
        "gi_sin_vidas":         "💀 You have no lives left in this round.",
        "gi_confirmar_btn":     "✅ Confirm answer",
        "gi_confirmar_msg":     "Confirm *{respuesta}* as your answer\\?",
        "gi_incorrecto":        "❌ Wrong\\. You have *{vidas}* life\\(ves\\) left\\.",
        "gi_eliminado":         "💀 *{nombre}* ran out of lives and was eliminated\\.",
        "gi_ganador":           (
            "🎉 *{nombre} guessed it\\!*\n\n"
            "The idol was *{idol}*\\.\n"
            "Won *{puntos}* point\\(s\\)\\!\n\n"
            "_Use /giscore to see the scoreboard\\._"
        ),
        "gi_ronda_sin_ganador": (
            "⏰ *Time's up\\!*\n\n"
            "Nobody guessed it\\. The idol was *{idol}*\\.\n\n"
            "_Use /giscore to see the scoreboard\\._"
        ),
        "gi_ronda_caption":     (
            "🎤 *GUESS THE IDOL*\n\n"
            "⏰ Ends: *{fin}*\n"
            "🎯 Points: *{puntos}*\n"
            "{pistas}\n\n"
            "_Type the name to win_"
        ),
        "gi_score_vacio":       "📊 No points registered yet\\.",
        "gi_score_titulo":      "🏆 *Scoreboard \\— Guess the Idol:*\n\n{tabla}",
        "gi_reset_ok":          "🔄 Guess the Idol scoreboard reset\\.",
        "gi_cancelado":         "❌ Round cancelled\\.",
        "gi_no_ronda":          "⚠️ No active round in this group\\.",
        "gi_div_incorrecta":    "❌ This round is only for {div}\\.",
    }
}

# ── DB helpers GI ─────────────────────────────────────────────

def gi_get_temporada(chat_key: str) -> int:
    """Retorna el número de temporada actual del grupo (crea si no existe)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT numero FROM gi_temporada WHERE chat_key=?", (chat_key,)
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT OR IGNORE INTO gi_temporada (chat_key, numero, estado) VALUES (?,1,'activa')",
                (chat_key,)
            )
            return 1
        return row[0]


def gi_get_division(chat_key: str, user_id: int) -> int:
    """Retorna la división del jugador (1 o 2). Si no existe, devuelve 2 (nueva incorporación)."""
    temporada = gi_get_temporada(chat_key)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT division FROM gi_marcador WHERE chat_key=? AND user_id=?",
            (chat_key, user_id)
        ).fetchone()
    if not row:
        # Primera temporada → todos en primera. Posteriores → segunda
        return 1 if temporada == 1 else 2
    return row[0]


def gi_segunda_existe(chat_key: str) -> bool:
    """Retorna True si ya existe segunda división en este grupo."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM gi_marcador WHERE chat_key=? AND division=2",
            (chat_key,)
        ).fetchone()
    return row[0] > 0 if row else False


def gi_grupo_activo(chat_id: int) -> bool:
    """Retorna True si el juego GI está activo en este grupo.
    NULL se trata como activo (1) para grupos registrados antes del toggle.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(gi_activo, 1) FROM gi_grupos WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return row[0] != 0 if row else True


def gi_toggle_grupo(chat_id: int) -> bool:
    """Alterna el estado GI del grupo. Retorna el nuevo estado (True=activo)."""
    actual = gi_grupo_activo(chat_id)
    nuevo  = 0 if actual else 1
    with get_conn() as conn:
        # Forzar escritura explícita del valor (reemplaza NULL si existía)
        conn.execute(
            "UPDATE gi_grupos SET gi_activo=? WHERE chat_id=?", (nuevo, chat_id)
        )
        # Si no existía la fila (raro), asegurarse de que quede en DB
        conn.execute(
            "UPDATE gi_grupos SET gi_activo=1 WHERE chat_id=? AND gi_activo IS NULL",
            (chat_id,)
        )
    return bool(nuevo)


def gi_registrar_grupo(chat_id: int, chat_title: str, chat_key: str):
    try:
        with get_conn() as conn:
            # INSERT OR IGNORE preserva gi_activo si el grupo ya existía
            conn.execute(
                "INSERT OR IGNORE INTO gi_grupos (chat_id, chat_title, chat_key, gi_activo, ultimo_msg) VALUES (?,?,?,1,CURRENT_TIMESTAMP)",
                (chat_id, chat_title or "?", chat_key)
            )
            # Actualizar solo título, key y timestamp — sin tocar gi_activo
            conn.execute(
                "UPDATE gi_grupos SET chat_title=?, chat_key=?, ultimo_msg=CURRENT_TIMESTAMP WHERE chat_id=?",
                (chat_title or "?", chat_key, chat_id)
            )
    except Exception:
        pass

def gi_get_grupos() -> list:
    with get_conn() as conn:
        return conn.execute(
            "SELECT chat_id, chat_title, chat_key FROM gi_grupos ORDER BY ultimo_msg DESC"
        ).fetchall()

def gi_get_ronda_activa(chat_key: str, chat_id: int = None):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM gi_rondas WHERE chat_key=? AND estado='activa' ORDER BY id DESC LIMIT 1",
            (chat_key,)
        ).fetchone()
        if row:
            return row
        if chat_id is not None:
            row = conn.execute(
                "SELECT * FROM gi_rondas WHERE chat_id=? AND estado='activa' ORDER BY id DESC LIMIT 1",
                (chat_id,)
            ).fetchone()
        return row

def gi_get_participante(ronda_id: int, user_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM gi_participantes WHERE ronda_id=? AND user_id=?",
            (ronda_id, user_id)
        ).fetchone()

def gi_upsert_participante(ronda_id: int, chat_key: str, user_id: int, username: str) -> bool:
    """Retorna True si fue añadido, False si ya existía.
    Solo registra en gi_participantes; gi_marcador se crea únicamente si gana puntos.
    """
    if gi_get_participante(ronda_id, user_id):
        return False
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO gi_participantes (ronda_id, chat_key, user_id, username, vidas, activo) VALUES (?,?,?,?,5,1)",
            (ronda_id, chat_key, user_id, username)
        )
    return True

def gi_desactivar_participante(ronda_id: int, user_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE gi_participantes SET activo=0 WHERE ronda_id=? AND user_id=?",
            (ronda_id, user_id)
        )

def gi_restar_vida(ronda_id: int, user_id: int) -> int:
    with get_conn() as conn:
        conn.execute(
            "UPDATE gi_participantes SET vidas = vidas - 1 WHERE ronda_id=? AND user_id=?",
            (ronda_id, user_id)
        )
        row = conn.execute(
            "SELECT vidas FROM gi_participantes WHERE ronda_id=? AND user_id=?",
            (ronda_id, user_id)
        ).fetchone()
    return row[0] if row else 0

def gi_sumar_puntos(chat_key: str, user_id: int, username: str, puntos: int):
    with get_conn() as conn:
        # INSERT con division correcta (solo aplica si no existe aún)
        div_actual = gi_get_division(chat_key, user_id)
        temp_actual = gi_get_temporada(chat_key)
        conn.execute(
            "INSERT OR IGNORE INTO gi_marcador (chat_key, user_id, username, puntos, victorias, division, temporada, victorias_temp) VALUES (?,?,?,0,0,?,?,0)",
            (chat_key, user_id, username, div_actual, temp_actual)
        )
        conn.execute(
            "UPDATE gi_marcador SET puntos=puntos+?, victorias=victorias+1, victorias_temp=victorias_temp+1, username=? WHERE chat_key=? AND user_id=?",
            (puntos, username, chat_key, user_id)
        )

def gi_get_marcador(chat_key: str) -> list:
    with get_conn() as conn:
        return conn.execute(
            "SELECT user_id, username, puntos, victorias FROM gi_marcador WHERE chat_key=? ORDER BY puntos DESC, victorias DESC",
            (chat_key,)
        ).fetchall()

# ── Setup helpers GI ──────────────────────────────────────────

def gi_t(lang: str, key: str) -> str:
    return GI_TEXTOS.get(lang, GI_TEXTOS["es"]).get(key, f"[{key}]")

def gi_build_setup_text(setup: dict, lang: str) -> str:
    no_conf = "❌ _No configurado_" if lang == "es" else "❌ _Not set_"
    tz = setup.get("tz_offset", 0)

    img_v    = "✅ _cargada_" if setup.get("file_id") else no_conf
    img2_v   = "✅ _cargada_" if setup.get("file_id_reveal") else no_conf
    idol_v   = f"*{esc(setup['idol_name'])}*" if setup.get("idol_name") else no_conf
    h1_v     = f"*{esc(setup['hint1'])}*" if setup.get("hint1") else no_conf
    h2_v     = f"*{esc(setup['hint2'])}*" if setup.get("hint2") else no_conf
    h3_v     = f"*{esc(setup['hint3'])}*" if setup.get("hint3") else no_conf
    ini_v    = f"*{esc(_formato_fecha_hora_local(setup['inicio_ts'], tz))}*" if setup.get("inicio_ts") else no_conf
    fin_v    = f"*{esc(_formato_fecha_hora_local(setup['fin_ts'], tz))}*" if setup.get("fin_ts") else no_conf
    tz_v     = f"*{esc(_tz_label(tz))}*"
    div      = setup.get("division", 1)
    div_v    = f"*🥇 Primera División*" if div == 1 else f"*🥈 Segunda División*"

    if lang == "es":
        titulo = "🎤 *Programar: Adivina la Idol*"
        nota   = "_Configura todos los campos y pulsa Publicar\\._"
    else:
        titulo = "🎤 *Schedule: Guess the Idol*"
        nota   = "_Configure all fields and press Publish\\._"

    return (
        f"{titulo}\n\n"
        f"📸 Imagen misterio: {img_v}\n"
        f"🖼 Imagen reveal: {img2_v}\n"
        f"👤 Idol: {idol_v}\n"
        f"👥 Pista 1 \\(miembros\\): {h1_v}\n"
        f"🏢 Pista 2 \\(empresa\\): {h2_v}\n"
        f"🎤 Pista 3 \\(grupo\\): {h3_v}\n"
        f"⏰ Inicio: {ini_v}\n"
        f"⏹ Fin: {fin_v}\n"
        f"🌐 Zona horaria: {tz_v}\n"
        f"🏆 División: {div_v}\n\n"
        f"{nota}"
    )

# Slots fijos UTC para Adivina la Idol
_GI_SLOTS_UTC = [
    (10, 11), (12, 13), (14, 15), (16, 17),
    (18, 19), (20, 21), (22, 23), (0, 1),
]

def _gi_slot_ts(ini_h: int, fin_h: int, day_offset: int = 0) -> tuple:
    """Retorna (inicio_ts, fin_ts) UTC para el próximo día con ese slot horario.
    day_offset permite calcular el slot de días futuros.
    """
    now_utc = datetime.now(_tz.utc)
    ini = now_utc.replace(hour=ini_h, minute=0, second=0, microsecond=0)
    if ini <= now_utc:
        ini += timedelta(days=1)
    ini += timedelta(days=day_offset)
    fin_h_real = fin_h if fin_h > ini_h else fin_h + 24
    fin = ini + timedelta(hours=(fin_h_real - ini_h))
    return int(ini.timestamp()), int(fin.timestamp())


def _gi_slots_disponibles() -> list:
    """Retorna los próximos 8 slots que NO están ya programados (pendientes).
    Cada elemento: (ini_h, fin_h, ini_ts, fin_ts, label)
    """
    # Obtener todos los inicio_ts de programaciones pendientes
    with get_conn() as conn:
        ocupados = set(
            row[0] for row in conn.execute(
                "SELECT inicio_ts FROM gi_programacion WHERE estado='pendiente'"
            ).fetchall()
        )

    disponibles = []
    day_offset = 0
    now_utc = datetime.now(_tz.utc)

    while len(disponibles) < 8:
        for ini_h, fin_h in _GI_SLOTS_UTC:
            ini_ts, fin_ts = _gi_slot_ts(ini_h, fin_h, day_offset)
            # Saltar si ya pasó o está ocupado
            if ini_ts in ocupados or ini_ts <= now_utc.timestamp():
                continue
            ini_dt = datetime.fromtimestamp(ini_ts, tz=_tz.utc)
            lbl = f"{ini_h:02d}:00–{fin_h:02d}:00 ({ini_dt.strftime('%d/%m')})"
            disponibles.append((ini_h, fin_h, ini_ts, fin_ts, lbl))
            if len(disponibles) >= 8:
                break
        day_offset += 1
        if day_offset > 30:  # seguro contra loop infinito
            break

    return disponibles


def gi_build_setup_keyboard(setup: dict, lang: str) -> list:
    tz = setup.get("tz_offset", 0)
    lbl_img    = "📸 Imagen misterio"     if lang == "es" else "📸 Mystery image"
    lbl_rev    = "🖼 Imagen reveal"       if lang == "es" else "🖼 Reveal image"
    lbl_idol   = "👤 Nombre de la idol"   if lang == "es" else "👤 Idol name"
    lbl_h1     = "👥 Pista 1: Miembros"  if lang == "es" else "👥 Hint 1: Members"
    lbl_h2     = "🏢 Pista 2: Empresa"   if lang == "es" else "🏢 Hint 2: Company"
    lbl_h3     = "🎤 Pista 3: Grupo"     if lang == "es" else "🎤 Hint 3: Group"
    lbl_ini    = "⏰ Inicio manual"       if lang == "es" else "⏰ Start (manual)"
    lbl_fin    = "⏹ Fin manual"          if lang == "es" else "⏹ End (manual)"
    lbl_prev   = "👁 Vista previa"        if lang == "es" else "👁 Preview"
    lbl_pub    = "✅ Publicar"            if lang == "es" else "✅ Publish"
    lbl_can    = "❌ Cancelar"            if lang == "es" else "❌ Cancel"
    lbl_slots  = "🕐 Elegir horario fijo:" if lang == "es" else "🕐 Fixed time slot:"

    # Botones de slots disponibles (sin los ya programados)
    slots = _gi_slots_disponibles()
    slot_rows = []
    for i in range(0, len(slots), 2):
        row = []
        for ini_h, fin_h, ini_ts, fin_ts, lbl in slots[i:i+2]:
            row.append(InlineKeyboardButton(lbl, callback_data=f"gi:setup:slot:{ini_h}:{fin_h}"))
        slot_rows.append(row)

    rows = [
        [InlineKeyboardButton(lbl_img,  callback_data="gi:setup:imagen")],
        [InlineKeyboardButton(lbl_rev,  callback_data="gi:setup:imagen_reveal")],
        [InlineKeyboardButton(lbl_idol, callback_data="gi:setup:idol")],
        [InlineKeyboardButton(lbl_h1,   callback_data="gi:setup:hint1")],
        [InlineKeyboardButton(lbl_h2,   callback_data="gi:setup:hint2")],
        [InlineKeyboardButton(lbl_h3,   callback_data="gi:setup:hint3")],
        [InlineKeyboardButton(lbl_slots, callback_data="gi:setup:slot_header")],
    ] + slot_rows + [
        [InlineKeyboardButton(lbl_ini, callback_data="gi:setup:inicio"),
         InlineKeyboardButton(lbl_fin, callback_data="gi:setup:fin")],
        [
            InlineKeyboardButton(f"◀ {_tz_label(max(-12,tz-1))}", callback_data="gi:setup:tz:-1"),
            InlineKeyboardButton(f"🌐 {_tz_label(tz)}",           callback_data="gi:setup:tz:0"),
            InlineKeyboardButton(f"{_tz_label(min(14,tz+1))} ▶",  callback_data="gi:setup:tz:+1"),
        ],
        [InlineKeyboardButton(lbl_prev, callback_data="gi:setup:preview")],
    ]
    div = setup.get("division", 1)
    lbl_div = ("🥈 Cambiar a Segunda División" if div == 1 else "🥇 Cambiar a Primera División") if lang == "es" else \
              ("🥈 Switch to Second Division" if div == 1 else "🥇 Switch to First Division")
    rows += [
        [InlineKeyboardButton(lbl_div, callback_data="gi:setup:division")],
        [
            InlineKeyboardButton(lbl_pub, callback_data="gi:setup:publicar"),
            InlineKeyboardButton(lbl_can, callback_data="gi:setup:cancelar"),
        ],
    ]
    return rows

def gi_build_ronda_caption(chat_key: str, fin_ts: int, puntos: int,
                            pistas_dadas: int, hints: dict, tz_offset: int) -> str:
    lang = get_idioma(chat_key)
    fin_str = _formato_hora_local(fin_ts, tz_offset)
    if pistas_dadas == 0:
        pistas_txt = gi_t(lang, "gi_sin_pistas")
    else:
        lineas = []
        if pistas_dadas >= 1:
            lineas.append(gi_t(lang, "gi_hint1_reveal").format(hint1=esc(hints["hint1"])))
        if pistas_dadas >= 2:
            lineas.append(gi_t(lang, "gi_hint2_reveal").format(hint2=esc(hints["hint2"])))
        if pistas_dadas >= 3:
            lineas.append(gi_t(lang, "gi_hint3_reveal").format(hint3=esc(hints["hint3"])))
        pistas_txt = "\n".join(lineas)
    return gi_t(lang, "gi_ronda_caption").format(
        fin=esc(fin_str), puntos=puntos, pistas=pistas_txt
    )

def gi_build_ronda_keyboard(lang: str) -> list:
    return [[
        InlineKeyboardButton(gi_t(lang, "gi_btn_participar"), callback_data="gi:participar"),
        InlineKeyboardButton(gi_t(lang, "gi_btn_salir"),      callback_data="gi:salir"),
    ]]

# ── Tasks GI ──────────────────────────────────────────────────

async def _gi_countdown(prog_id: int, bot, bot_data: dict):
    """Espera hasta inicio_ts y publica la ronda en todos los grupos."""
    try:
        with get_conn() as conn:
            prog = conn.execute(
                "SELECT * FROM gi_programacion WHERE id=? AND estado='pendiente'",
                (prog_id,)
            ).fetchone()
        if not prog:
            return
        # prog: id(0) idol_name(1) file_id(2) file_id_reveal(3) hint1(4) hint2(5) hint3(6) inicio_ts(7) fin_ts(8) tz_offset(9) estado(10)
        wait = max(0, prog[7] - int(datetime.now(_tz.utc).timestamp()))
        if wait > 0:
            await asyncio.sleep(wait)

        with get_conn() as conn:
            prog = conn.execute(
                "SELECT * FROM gi_programacion WHERE id=? AND estado='pendiente'",
                (prog_id,)
            ).fetchone()
        if not prog:
            return

        with get_conn() as conn:
            conn.execute("UPDATE gi_programacion SET estado='activa' WHERE id=?", (prog_id,))

        grupos = gi_get_grupos()
        hints = {"hint1": prog[4], "hint2": prog[5], "hint3": prog[6]}

        for grp in grupos:
            chat_id  = grp[0]
            chat_key = grp[2]
            if not gi_grupo_activo(chat_id):
                logger.info(f"[IDOL] Grupo {chat_key} con GI desactivado, saltando")
                continue
            lang = get_idioma(chat_key)
            try:
                caption  = gi_build_ronda_caption(chat_key, prog[8], 5, 0, hints, prog[9])
                keyboard = gi_build_ronda_keyboard(lang)
                msg = await bot.send_photo(
                    chat_id,
                    photo=prog[2],
                    caption=caption,
                    parse_mode="MarkdownV2",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                with get_conn() as conn:
                    conn.execute(
                        "INSERT INTO gi_rondas (prog_id,chat_key,chat_id,idol_name,file_id,file_id_reveal,hint1,hint2,hint3,"
                        "inicio_ts,fin_ts,estado,pistas_dadas,puntos_actuales,mensaje_id,division) VALUES (?,?,?,?,?,?,?,?,?,?,?,'activa',0,5,?,?)",
                        (prog_id, chat_key, chat_id, prog[1], prog[2], prog[3], prog[4], prog[5],
                         prog[6], prog[7], prog[8], msg.message_id,
                         prog[11] if len(prog) > 11 and prog[11] is not None else 1)
                    )
                    ronda_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

                tarea = asyncio.create_task(_gi_ronda_task(chat_key, ronda_id, bot, bot_data))
                bot_data[f"gi_ronda_{chat_key}"] = tarea
                logger.info(f"[IDOL] Ronda iniciada en {chat_key}")
            except Exception as e:
                logger.error(f"[IDOL] Error publicando en {chat_key}: {e}")

        bot_data.pop(f"gi_countdown_{prog_id}", None)

    except asyncio.CancelledError:
        logger.info(f"[IDOL] Countdown {prog_id} cancelado")
    except Exception as e:
        logger.error(f"[IDOL] Error en countdown {prog_id}: {e}", exc_info=True)


async def _gi_ronda_task(chat_key: str, ronda_id: int, bot, bot_data: dict):
    """Maneja los intervalos de pistas y el fin de la ronda."""
    try:
        with get_conn() as conn:
            ronda_init = conn.execute("SELECT * FROM gi_rondas WHERE id=?", (ronda_id,)).fetchone()
        if not ronda_init:
            return

        # Columnas: id(0) prog_id(1) chat_key(2) chat_id(3) idol_name(4) file_id(5)
        #           file_id_reveal(6) hint1(7) hint2(8) hint3(9) inicio_ts(10) fin_ts(11)
        #           estado(12) pistas_dadas(13) puntos_actuales(14) ganador_id(15) ganador_nombre(16) mensaje_id(17)
        chat_id        = ronda_init[3]
        inicio_ts      = ronda_init[10]
        fin_ts         = ronda_init[11]
        msg_id         = ronda_init[17]
        file_id        = ronda_init[5]
        file_id_reveal = ronda_init[6]
        hints          = {"hint1": ronda_init[7], "hint2": ronda_init[8], "hint3": ronda_init[9]}
        idol_name      = ronda_init[4]

        with get_conn() as conn:
            prog = conn.execute("SELECT tz_offset FROM gi_programacion WHERE id=?", (ronda_init[1],)).fetchone()
        tz_offset = prog[0] if prog else 0

        duracion = fin_ts - inicio_ts
        # 3 pistas en 25%, 50%, 75% del tiempo
        hint_schedule = [
            (int(inicio_ts + duracion * 0.25), 3,  "gi_hint1_reveal", "hint1"),
            (int(inicio_ts + duracion * 0.50), 2,  "gi_hint2_reveal", "hint2"),
            (int(inicio_ts + duracion * 0.75), 1,  "gi_hint3_reveal", "hint3"),
        ]

        pistas_ya_dadas = ronda_init[13]  # pistas dadas antes de este arranque

        for idx, (hint_ts, nuevos_puntos, hint_key, hint_field) in enumerate(hint_schedule):
            # Saltar pistas que ya fueron publicadas antes del último reinicio
            if idx < pistas_ya_dadas:
                continue

            wait = hint_ts - int(datetime.now(_tz.utc).timestamp())
            if wait > 0:
                await asyncio.sleep(wait)

            with get_conn() as conn:
                ronda_check = conn.execute(
                    "SELECT estado FROM gi_rondas WHERE id=?", (ronda_id,)
                ).fetchone()
            if not ronda_check or ronda_check[0] != 'activa':
                return

            pistas_dadas = idx + 1
            with get_conn() as conn:
                conn.execute(
                    "UPDATE gi_rondas SET pistas_dadas=?, puntos_actuales=? WHERE id=?",
                    (pistas_dadas, nuevos_puntos, ronda_id)
                )

            lang = get_idioma(chat_key)
            caption  = gi_build_ronda_caption(chat_key, fin_ts, nuevos_puntos, pistas_dadas, hints, tz_offset)
            keyboard = gi_build_ronda_keyboard(lang)

            # Borrar el post anterior (imagen misterio o pista anterior)
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass

            # Publicar nuevo post con imagen misterio + pistas acumuladas + botones
            try:
                nuevo_msg = await bot.send_photo(
                    chat_id,
                    photo=file_id,
                    caption=caption,
                    parse_mode="MarkdownV2",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                msg_id = nuevo_msg.message_id
                with get_conn() as conn:
                    conn.execute(
                        "UPDATE gi_rondas SET mensaje_id=? WHERE id=?",
                        (msg_id, ronda_id)
                    )
            except Exception as e:
                logger.error(f"[IDOL] Error publicando pista con imagen {chat_key}: {e}")

        # Esperar hasta el fin
        wait_fin = fin_ts - int(datetime.now(_tz.utc).timestamp())
        if wait_fin > 0:
            await asyncio.sleep(wait_fin)

        with get_conn() as conn:
            ronda_fin = conn.execute(
                "SELECT estado FROM gi_rondas WHERE id=?", (ronda_id,)
            ).fetchone()
        if not ronda_fin or ronda_fin[0] != 'activa':
            return  # Alguien ganó mientras esperábamos

        with get_conn() as conn:
            conn.execute("UPDATE gi_rondas SET estado='terminada' WHERE id=?", (ronda_id,))

        lang     = get_idioma(chat_key)
        txt_fin  = gi_t(lang, "gi_ronda_sin_ganador").format(idol=esc(idol_name))
        # Editar caption de imagen misterio
        try:
            await bot.edit_message_caption(
                chat_id=chat_id, message_id=msg_id,
                caption=txt_fin, parse_mode="MarkdownV2"
            )
        except Exception:
            pass
        # Enviar imagen reveal con el texto de fin
        if file_id_reveal:
            try:
                await bot.send_photo(
                    chat_id, photo=file_id_reveal,
                    caption=txt_fin, parse_mode="MarkdownV2"
                )
            except Exception:
                await bot.send_message(chat_id, txt_fin, parse_mode="MarkdownV2")
        else:
            await bot.send_message(chat_id, txt_fin, parse_mode="MarkdownV2")

        bot_data.pop(f"gi_ronda_{chat_key}", None)

    except asyncio.CancelledError:
        logger.info(f"[IDOL] Ronda {ronda_id} cancelada en {chat_key}")
    except Exception as e:
        logger.error(f"[IDOL] Error en ronda {ronda_id} {chat_key}: {e}", exc_info=True)


# ── Comandos GI ───────────────────────────────────────────────

async def gi_cmd_grupos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != BOT_OWNER_ID:
        await update.message.reply_text("⚠️ Solo el creador del bot puede usar este comando.")
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text("⚠️ Usa este comando en chat privado con el bot.")
        return

    with get_conn() as conn:
        gi_rows  = conn.execute(
            "SELECT chat_id, chat_title FROM gi_grupos ORDER BY ultimo_msg DESC"
        ).fetchall()
        imp_rows = conn.execute(
            "SELECT DISTINCT chat_id FROM partidas WHERE chat_id IS NOT NULL"
        ).fetchall()

    seen = {}
    for chat_id, title in gi_rows:
        seen[chat_id] = title or str(chat_id)
    for (chat_id,) in imp_rows:
        if chat_id and chat_id not in seen:
            seen[chat_id] = str(chat_id)

    if not seen:
        await update.message.reply_text("📭 El bot no ha registrado ningún grupo aún.")
        return

    aviso = await update.message.reply_text("🔍 Verificando grupos...", parse_mode=None)

    grupos_activos = []
    grupos_inactivos = []

    for chat_id, title in seen.items():
        try:
            chat_obj = await ctx.bot.get_chat(chat_id)
            # Bot sigue siendo miembro — obtener link
            link = None
            if getattr(chat_obj, "username", None):
                link = f"https://t.me/{chat_obj.username}"
            else:
                try:
                    link = await ctx.bot.export_chat_invite_link(chat_id)
                except Exception:
                    pass
            # Verificar si el bot puede enviar mensajes
            puede_escribir = False
            try:
                bot_member = await ctx.bot.get_chat_member(chat_id, ctx.bot.id)
                if bot_member.status == "administrator":
                    puede_escribir = True
                elif bot_member.status == "member":
                    perms = getattr(chat_obj, "permissions", None)
                    puede_escribir = perms is None or getattr(perms, "can_send_messages", True)
            except Exception:
                puede_escribir = False
            # Usar el título actualizado de Telegram
            title_real = chat_obj.title or title
            grupos_activos.append((chat_id, title_real, link, puede_escribir))
            # Actualizar título en DB si cambió
            if title_real != title:
                with get_conn() as conn:
                    conn.execute(
                        "UPDATE gi_grupos SET chat_title=? WHERE chat_id=?",
                        (title_real, chat_id)
                    )
        except Exception:
            grupos_inactivos.append((chat_id, title))

    try:
        await aviso.delete()
    except Exception:
        pass

    if not grupos_activos:
        await update.message.reply_text("📭 El bot no está en ningún grupo actualmente.")
        return

    grupos_activos.sort(key=lambda x: x[1])

    # Un solo mensaje con lista + botones por grupo
    lineas = []
    for i, (chat_id, title, link, puede_escribir) in enumerate(grupos_activos, 1):
        gi_on = gi_grupo_activo(chat_id)
        estado_icon = "🎤" if gi_on else "🔇"
        lineas.append(f"  {i}\\. {estado_icon} *{esc(title)}*")

    suffix = f"\n_{len(grupos_inactivos)} ya no lo tienen_" if grupos_inactivos else ""
    texto = "📋 *El bot está en " + str(len(grupos_activos)) + " grupo\\(s\\):*\n\n" + "\n".join(lineas) + suffix

    # Botones: una fila por grupo con sus acciones
    keyboard = []
    for chat_id, title, link, puede_escribir in grupos_activos:
        gi_on = gi_grupo_activo(chat_id)
        toggle_lbl = "🔇" if gi_on else "🎤"
        title_short = title[:20] + "…" if len(title) > 20 else title
        row = []
        if puede_escribir:
            row.append(InlineKeyboardButton(f"💬 {title_short}", callback_data=f"owner:msg:{chat_id}"))
        row.append(InlineKeyboardButton(toggle_lbl, callback_data=f"gi:toggle:{chat_id}"))
        if link:
            row.append(InlineKeyboardButton("🔗", url=link))
        keyboard.append(row)

    await update.message.reply_text(
        texto,
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def gi_cmd_idol(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != BOT_OWNER_ID:
        await update.message.reply_text("⚠️ Solo el creador del bot puede usar este comando.")
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text("⚠️ Usa este comando en chat privado con el bot.")
        return
    lang = "es"
    setup = {
        "file_id": None, "file_id_reveal": None, "idol_name": None,
        "hint1": None, "hint2": None, "hint3": None,
        "inicio_ts": None, "fin_ts": None,
        "tz_offset": 0, "esperando": None, "mensaje_id": None, "lang": lang,
        "division": 1
    }
    ctx.bot_data[f"gi_setup_{user.id}"] = setup
    text     = gi_build_setup_text(setup, lang)
    keyboard = gi_build_setup_keyboard(setup, lang)
    msg = await update.message.reply_text(
        text, parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    setup["mensaje_id"] = msg.message_id



async def gi_cmd_fintemporada(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cierra la temporada actual y aplica ascensos/descensos."""
    user = update.effective_user
    chat = update.effective_chat
    if not BOT_OWNER_ID or user.id != BOT_OWNER_ID:
        await update.message.reply_text("⚠️ Solo el creador del bot puede usar este comando.")
        return

    chat_key = get_chat_key(update)
    lang     = get_idioma(chat_key)

    with get_conn() as conn:
        todos = conn.execute(
            "SELECT user_id, username, puntos, division, victorias_temp "
            "FROM gi_marcador WHERE chat_key=? ORDER BY puntos DESC",
            (chat_key,)
        ).fetchall()

    if not todos:
        await update.message.reply_text("❌ No hay jugadores registrados en este grupo.")
        return

    temporada_actual = gi_get_temporada(chat_key)
    segunda_existe   = gi_segunda_existe(chat_key)

    div1 = [(r[0], r[1], r[2], r[4]) for r in todos if r[3] == 1]
    div2 = [(r[0], r[1], r[2], r[4]) for r in todos if r[3] == 2]

    # ── Primera temporada: crear las dos divisiones ──
    if not segunda_existe:
        n = len(todos)
        corte = (n + 1) // 2  # impar → el del medio va a primera
        primera = [r[0] for r in todos[:corte]]
        segunda = [r[0] for r in todos[corte:]]
        with get_conn() as conn:
            for uid in primera:
                conn.execute(
                    "UPDATE gi_marcador SET division=1, puntos=0, victorias_temp=0, temporada=? WHERE chat_key=? AND user_id=?",
                    (temporada_actual + 1, chat_key, uid)
                )
            for uid in segunda:
                conn.execute(
                    "UPDATE gi_marcador SET division=2, puntos=0, victorias_temp=0, temporada=? WHERE chat_key=? AND user_id=?",
                    (temporada_actual + 1, chat_key, uid)
                )
            conn.execute(
                "INSERT OR REPLACE INTO gi_temporada (chat_key, numero, estado) VALUES (?,?,'activa')",
                (chat_key, temporada_actual + 1)
            )
        nombres_primera = ", ".join(r[1] for r in todos[:corte])
        nombres_segunda = ", ".join(r[1] for r in todos[corte:])
        texto = (
            f"🏆 *Temporada {temporada_actual} cerrada*\n\n"
            f"🥇 *Primera División* \\({len(primera)}\\):\n_{esc(nombres_primera)}_\n\n"
            f"🥈 *Segunda División* \\({len(segunda)}\\):\n_{esc(nombres_segunda)}_\n\n"
            f"⭐ Temporada {temporada_actual + 1} iniciada\\. ¡Puntos reseteados\\!"
        )
        await update.message.reply_text(texto, parse_mode="MarkdownV2")
        return

    # ── Temporadas siguientes: ascensos y descensos ──
    # Inactivos de primera (0 victorias en la temporada)
    inactivos_div1 = [r for r in div1 if r[3] == 0]
    activos_div1   = [r for r in div1 if r[3] >  0]

    # Los últimos 3 activos de primera bajan
    activos_div1_sorted = sorted(activos_div1, key=lambda r: r[2])  # asc por puntos
    bajan_activos = activos_div1_sorted[:3]
    bajan = [r[0] for r in bajan_activos] + [r[0] for r in inactivos_div1]

    # Calcular cuántos suben para equilibrar
    n_div1_nuevo = len(div1) - len(bajan)
    n_div2       = len(div2)
    total        = n_div1_nuevo + n_div2
    objetivo_div1 = (total + 1) // 2  # impar → primera tiene más
    suben_n      = max(0, objetivo_div1 - n_div1_nuevo)

    # Los mejores de segunda suben
    div2_sorted = sorted(div2, key=lambda r: r[2], reverse=True)
    suben       = [r[0] for r in div2_sorted[:suben_n]]

    with get_conn() as conn:
        for uid in bajan:
            conn.execute(
                "UPDATE gi_marcador SET division=2, puntos=0, victorias_temp=0, temporada=? WHERE chat_key=? AND user_id=?",
                (temporada_actual + 1, chat_key, uid)
            )
        for uid in suben:
            conn.execute(
                "UPDATE gi_marcador SET division=1, puntos=0, victorias_temp=0, temporada=? WHERE chat_key=? AND user_id=?",
                (temporada_actual + 1, chat_key, uid)
            )
        # Los que se quedan: solo resetear puntos
        todos_ids = set(r[0] for r in todos)
        movidos   = set(bajan + suben)
        quedan    = todos_ids - movidos
        for uid in quedan:
            conn.execute(
                "UPDATE gi_marcador SET puntos=0, victorias_temp=0, temporada=? WHERE chat_key=? AND user_id=?",
                (temporada_actual + 1, chat_key, uid)
            )
        conn.execute(
            "INSERT OR REPLACE INTO gi_temporada (chat_key, numero, estado) VALUES (?,?,'activa')",
            (chat_key, temporada_actual + 1)
        )

    def nombres(ids):
        mapping = {r[0]: r[1] for r in todos}
        return ", ".join(mapping.get(uid, str(uid)) for uid in ids) or "—"

    texto = (
        f"🏆 *Temporada {temporada_actual} cerrada*\n\n"
        f"⬇️ *Descienden a Segunda* \\({len(bajan)}\\):\n_{esc(nombres(bajan))}_\n\n"
        f"⬆️ *Ascienden a Primera* \\({len(suben)}\\):\n_{esc(nombres(suben))}_\n\n"
        f"⭐ Temporada {temporada_actual + 1} iniciada\\. ¡Puntos reseteados\\!"
    )
    await update.message.reply_text(texto, parse_mode="MarkdownV2")


def generar_imagen_giscore(chat_key: str, division: int):
    """Genera imagen PNG del marcador de Adivina la Idol para una división."""
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT user_id, username, puntos, victorias FROM gi_marcador "
                "WHERE chat_key=? AND COALESCE(division,1)=? ORDER BY puntos DESC",
                (chat_key, division)
            ).fetchall()
        if not rows:
            return None

        FONT_SIZE  = 22
        font       = _get_font(FONT_SIZE)
        font_bold  = _get_font(FONT_SIZE, bold=True)
        font_title = _get_font(26, bold=True)

        BG     = (20,  20,  36)
        HEADER = (49,  50,  68)
        ROW_A  = (30,  30,  50)
        ROW_B  = (25,  25,  44)
        TEXT   = (220, 220, 235)
        ACCENT = (137, 180, 250)
        GREEN  = (166, 227, 161)
        GRAY   = (150, 150, 170)
        GOLD   = (255, 215,   0)
        SILVER = (192, 192, 192)
        BRONZE = (205, 127,  50)
        LINE   = (69,  71,  90)
        DIV1_C = (255, 200,  50)
        DIV2_C = (160, 160, 200)
        div_color = DIV1_C if division == 1 else DIV2_C

        lang = get_idioma(chat_key)
        div_label = ("🥇 Primera División" if division == 1 else "🥈 Segunda División") if lang == "es"                     else ("🥇 First Division" if division == 1 else "🥈 Second Division")
        col_pts = "Puntos" if lang == "es" else "Points"
        col_vic = "Victorias" if lang == "es" else "Wins"
        col_jug = "Jugador" if lang == "es" else "Player"

        PAD   = 20
        ROW_H = 38
        COL_W = [36, 170, 80, 80]

        total_w = PAD * 2 + sum(COL_W)
        title_h = 52
        total_h = PAD + title_h + ROW_H + ROW_H * len(rows) + PAD

        img  = Image.new("RGB", (total_w, total_h), BG)
        draw = ImageDraw.Draw(img)

        draw.rectangle([0, 0, total_w, 4], fill=div_color)
        draw.text((PAD, PAD // 2 + 4), f"  {div_label}", font=font_title, fill=div_color)

        y = PAD + title_h
        draw.rectangle([PAD, y, total_w - PAD, y + ROW_H], fill=HEADER)
        x = PAD + 8
        for col, w in zip(["#", col_jug, col_pts, col_vic], COL_W):
            draw.text((x, y + 8), col, font=font_bold, fill=ACCENT)
            x += w

        for i, (uid, uname, puntos, victorias) in enumerate(rows):
            y += ROW_H
            draw.rectangle([PAD, y, total_w - PAD, y + ROW_H - 1], fill=ROW_A if i % 2 == 0 else ROW_B)
            draw.line([PAD, y + ROW_H - 1, total_w - PAD, y + ROW_H - 1], fill=LINE, width=1)
            pos = i + 1
            x   = PAD + 8
            pos_color = GOLD if pos == 1 else SILVER if pos == 2 else BRONZE if pos == 3 else GRAY
            draw.text((x, y + 8), str(pos), font=font_bold, fill=pos_color)
            x += COL_W[0]
            draw_text_smart(draw, (x, y + 8), limpiar_nombre_tabla(uname)[:14], FONT_SIZE, TEXT)
            x += COL_W[1]
            draw.text((x, y + 8), str(puntos), font=font_bold, fill=GREEN)
            x += COL_W[2]
            draw.text((x, y + 8), str(victorias), font=font, fill=GRAY)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf
    except Exception as e:
        logger.error(f"[generar_imagen_giscore] error: {e}")
        return None


async def gi_cmd_score(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_key = get_chat_key(update)
    lang     = get_idioma(chat_key)
    user     = update.effective_user
    is_owner = bool(BOT_OWNER_ID and user.id == BOT_OWNER_ID)
    segunda  = gi_segunda_existe(chat_key)

    async def _enviar_division(div: int):
        buf = generar_imagen_giscore(chat_key, div)
        if buf:
            await update.message.reply_photo(photo=buf)
        else:
            with get_conn() as conn:
                rows = conn.execute(
                    "SELECT username, puntos, victorias FROM gi_marcador "
                    "WHERE chat_key=? AND COALESCE(division,1)=? ORDER BY puntos DESC",
                    (chat_key, div)
                ).fetchall()
            if not rows:
                div_lbl = ("Primera División" if div == 1 else "Segunda División") if lang == "es"                           else ("First Division" if div == 1 else "Second Division")
                await update.message.reply_text(
                    gi_t(lang, "gi_score_vacio") + f" ({div_lbl})",
                    parse_mode="MarkdownV2"
                )
                return
            MEDALLAS = {1: "🥇", 2: "🥈", 3: "🥉"}
            lineas = []
            MEDS = {1:"🥇", 2:"🥈", 3:"🥉"}
            for i, r in enumerate(rows, 1):
                lineas.append(f"{MEDS.get(i, str(i))} {r[0]} - {r[1]} pts ({r[2]} victorias)")
            await update.message.reply_text("\n".join(lineas))

    if is_owner and segunda:
        await _enviar_division(1)
        await _enviar_division(2)
    elif segunda:
        div = gi_get_division(chat_key, user.id)
        await _enviar_division(div)
    else:
        await _enviar_division(1)


async def gi_cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != BOT_OWNER_ID:
        await update.message.reply_text("⚠️ Solo el creador del bot puede resetear el marcador.")
        return
    chat_key = get_chat_key(update)
    lang = get_idioma(chat_key)
    with get_conn() as conn:
        conn.execute("DELETE FROM gi_marcador WHERE chat_key=?", (chat_key,))
    await update.message.reply_text(gi_t(lang, "gi_reset_ok"), parse_mode="MarkdownV2")


async def gi_cmd_cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != BOT_OWNER_ID:
        await update.message.reply_text("⚠️ Solo el creador del bot puede cancelar una ronda.")
        return
    chat_key = get_chat_key(update)
    lang  = get_idioma(chat_key)
    ronda = gi_get_ronda_activa(chat_key)
    if not ronda:
        await update.message.reply_text(gi_t(lang, "gi_no_ronda"), parse_mode="MarkdownV2")
        return
    with get_conn() as conn:
        conn.execute("UPDATE gi_rondas SET estado='terminada' WHERE id=?", (ronda[0],))
    tarea = ctx.bot_data.pop(f"gi_ronda_{chat_key}", None)
    if tarea:
        tarea.cancel()
    await update.message.reply_text(gi_t(lang, "gi_cancelado"), parse_mode="MarkdownV2")


# ── Callbacks GI ──────────────────────────────────────────────

async def gi_btn_cancelar_prog(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cancela una programación GI pendiente por ID."""
    query = update.callback_query
    user  = query.from_user
    if user.id != BOT_OWNER_ID:
        await query.answer("⚠️ Solo el owner puede cancelar.", show_alert=True)
        return

    prog_id = int(query.data.split(":")[2])

    with get_conn() as conn:
        prog = conn.execute(
            "SELECT idol_name, estado FROM gi_programacion WHERE id=?", (prog_id,)
        ).fetchone()

    if not prog or prog[1] != 'pendiente':
        await query.answer("Esta programación ya no está pendiente.", show_alert=True)
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    # Cancelar tarea de countdown si existe
    tarea = ctx.bot_data.pop(f"gi_countdown_{prog_id}", None)
    if tarea:
        tarea.cancel()

    with get_conn() as conn:
        conn.execute("UPDATE gi_programacion SET estado='cancelada' WHERE id=?", (prog_id,))

    await query.answer(f"✅ Cancelado: {prog[0]}", show_alert=True)

    # Actualizar el mensaje: quitar el botón de la programación cancelada
    with get_conn() as conn:
        pendientes = conn.execute(
            "SELECT id, idol_name, inicio_ts, fin_ts, tz_offset FROM gi_programacion WHERE estado='pendiente' ORDER BY inicio_ts"
        ).fetchall()

    if not pendientes:
        try:
            await query.message.edit_text("✅ Todas las programaciones canceladas\\.", parse_mode="MarkdownV2")
        except Exception:
            pass
        return

    lineas = []
    keyboard = []
    for p in pendientes:
        pid, idol, ini_ts, fin_ts, tz = p
        ini_str = _formato_hora_local(ini_ts, tz or 0)
        fin_str = _formato_hora_local(fin_ts, tz or 0)
        lineas.append(f"  *{esc(idol)}* — {esc(ini_str)} → {esc(fin_str)}")
        keyboard.append([
            InlineKeyboardButton(f"✏️ Editar: {idol}", callback_data=f"gi:editprog:{pid}"),
            InlineKeyboardButton(f"❌ Cancelar: {idol}", callback_data=f"gi:cancelarprog:{pid}"),
        ])

    texto = "📋 *Programaciones pendientes:*\n\n" + "\n".join(lineas)
    try:
        await query.message.edit_text(
            texto, parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception:
        pass


async def owner_btn_msg_grupo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user  = query.from_user
    if user.id != BOT_OWNER_ID:
        await query.answer("Solo el owner.", show_alert=True)
        return

    chat_id_dest = int(query.data.split(":")[2])
    ctx.bot_data[f"owner_msg_destino_{user.id}"] = chat_id_dest

    with get_conn() as conn:
        row = conn.execute(
            "SELECT chat_title FROM gi_grupos WHERE chat_id=?", (chat_id_dest,)
        ).fetchone()
    nombre_grupo = row[0] if row else str(chat_id_dest)

    await query.answer()
    linea1 = "\U0001f4dd Escribe el mensaje para *" + esc(nombre_grupo) + "*\\.\\n\\n"
    linea2 = "_Escribe en el chat y se enviar\u00e1 directamente\\._\\n\\n"
    linea3 = "Usa /cancelarmsg para cancelar\\."
    await query.message.reply_text(linea1 + linea2 + linea3, parse_mode="MarkdownV2")


async def gi_btn_toggle_grupo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Activa/desactiva Adivina la Idol en un grupo específico."""
    query = update.callback_query
    user  = query.from_user
    if user.id != BOT_OWNER_ID:
        await query.answer("Solo el owner.", show_alert=True)
        return

    chat_id_tog = int(query.data.split(":")[2])
    nuevo_estado = gi_toggle_grupo(chat_id_tog)

    estado_txt = "✅ activado" if nuevo_estado else "🔇 desactivado"
    await query.answer(f"Adivina la Idol {estado_txt} en ese grupo.", show_alert=True)

    # Actualizar botón y caption del mensaje
    try:
        markup = query.message.reply_markup
        if markup:
            new_rows = []
            for row in markup.inline_keyboard:
                new_row = []
                for btn in row:
                    if btn.callback_data and btn.callback_data == query.data:
                        lbl = "🔇 Desactivar Idol" if nuevo_estado else "🎤 Activar Idol"
                        new_row.append(InlineKeyboardButton(lbl, callback_data=query.data))
                    else:
                        new_row.append(btn)
                new_rows.append(new_row)
            # Actualizar caption con nuevo estado
            gi_estado_txt = "🎤 Idol: activo" if nuevo_estado else "🔇 Idol: desactivado"
            old_text = query.message.text or ""
            # Reemplazar línea de estado o agregar al final
            import re as _re
            new_text = _re.sub(r"[🎤🔇] Idol: (?:activo|desactivado)", gi_estado_txt, old_text)
            if new_text == old_text:
                new_text = old_text + "\n" + gi_estado_txt
            await query.message.edit_text(new_text, parse_mode="MarkdownV2",
                                          reply_markup=InlineKeyboardMarkup(new_rows))
    except Exception:
        pass


async def gi_btn_editprog(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Carga una programación existente en el setup para editarla."""
    query = update.callback_query
    user  = query.from_user
    if user.id != BOT_OWNER_ID:
        await query.answer("⚠️ Solo el owner puede editar.", show_alert=True)
        return

    prog_id = int(query.data.split(":")[2])

    with get_conn() as conn:
        prog = conn.execute(
            "SELECT * FROM gi_programacion WHERE id=? AND estado='pendiente'", (prog_id,)
        ).fetchone()

    if not prog:
        await query.answer("Esta programación ya no está pendiente.", show_alert=True)
        return

    # prog: id(0) idol_name(1) file_id(2) file_id_reveal(3) hint1(4) hint2(5) hint3(6)
    #       inicio_ts(7) fin_ts(8) tz_offset(9) estado(10) division(11)
    lang = "es"
    setup = {
        "file_id":       prog[2],
        "file_id_reveal":prog[3],
        "idol_name":     prog[1],
        "hint1":         prog[4],
        "hint2":         prog[5],
        "hint3":         prog[6],
        "inicio_ts":     prog[7],
        "fin_ts":        prog[8],
        "tz_offset":     prog[9] or 0,
        "esperando":     None,
        "mensaje_id":    None,
        "lang":          lang,
        "division":      (prog[11] if len(prog) > 11 and prog[11] is not None else 1),
        "editing_prog_id": prog_id,  # flag: estamos editando, no creando
    }
    ctx.bot_data[f"gi_setup_{user.id}"] = setup

    await query.answer()
    text     = gi_build_setup_text(setup, lang)
    keyboard = gi_build_setup_keyboard(setup, lang)
    try:
        edit_header = "✏️ *Editando programación*\n\n"
        await query.message.edit_text(
            edit_header + text,
            parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        setup["mensaje_id"] = query.message.message_id
    except Exception:
        edit_header = "✏️ *Editando programación*\n\n"
        msg = await query.message.reply_text(
            edit_header + text,
            parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        setup["mensaje_id"] = msg.message_id


async def gi_btn_setup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user  = query.from_user
    if user.id != BOT_OWNER_ID:
        await query.answer("⚠️ Solo el owner puede usar esto.", show_alert=True)
        return
    setup = ctx.bot_data.get(f"gi_setup_{user.id}")
    if not setup:
        await query.answer("Setup expirado. Usa /idol de nuevo.", show_alert=True)
        return

    lang   = setup.get("lang", "es")
    parts  = query.data.split(":")   # gi:setup:action[:value]
    action = parts[2]

    if action == "imagen":
        setup["esperando"] = "imagen"
        msg = "Envía la foto MISTERIO de la idol ahora." if lang == "es" else "Send the MYSTERY photo of the idol now."
        await query.answer(msg, show_alert=True)
        return

    elif action == "imagen_reveal":
        setup["esperando"] = "imagen_reveal"
        msg = "Envía la foto REVEAL (la que se muestra al acertar)." if lang == "es" else "Send the REVEAL photo (shown when someone guesses correctly)."
        await query.answer(msg, show_alert=True)
        return

    elif action in ("idol", "hint1", "hint2", "hint3"):
        setup["esperando"] = action
        msg = "Escribe el valor en el chat." if lang == "es" else "Type the value in the chat."
        await query.answer(msg, show_alert=True)
        return

    elif action == "slot":
        # gi:setup:slot:INI_H:FIN_H — buscar el slot disponible más próximo
        if len(parts) >= 5:
            ini_h = int(parts[3])
            fin_h = int(parts[4])
            # Buscar en slots disponibles el que coincida con ini_h/fin_h
            slots = _gi_slots_disponibles()
            match = next((s for s in slots if s[0] == ini_h and s[1] == fin_h), None)
            if match:
                setup["inicio_ts"] = match[2]
                setup["fin_ts"]    = match[3]
            await query.answer()
        else:
            await query.answer()
            return

    elif action == "slot_header":
        await query.answer()
        return

    elif action == "division":
        setup["division"] = 2 if setup.get("division", 1) == 1 else 1
        await query.answer()

    elif action in ("inicio", "fin"):
        setup["esperando"] = action
        msg = ("Escribe fecha y hora: DD/MM HH:MM (ej: 25/03 20:00)\no solo hora HH:MM para hoy/mañana"
               if lang == "es" else
               "Enter date and time: DD/MM HH:MM (eg: 25/03 20:00)\nor just HH:MM for today/tomorrow")
        await query.answer(msg, show_alert=True)
        return

    elif action == "tz":
        delta = int(parts[3])
        setup["tz_offset"] = max(-12, min(14, setup.get("tz_offset", 0) + delta))
        await query.answer()

    elif action == "preview":
        await query.answer()
        if not setup.get("file_id"):
            msg = "⚠️ Primero agrega una imagen." if lang == "es" else "⚠️ Add an image first."
            await query.message.reply_text(msg)
            return
        tz      = setup.get("tz_offset", 0)
        fin_ts  = setup.get("fin_ts") or int(datetime.now(_tz.utc).timestamp()) + 3600
        hints   = {
            "hint1": setup.get("hint1") or "?",
            "hint2": setup.get("hint2") or "?",
            "hint3": setup.get("hint3") or "?",
        }
        # Para la preview, usamos chat_key fake para obtener idioma del owner (es)
        caption_preview = gi_build_ronda_caption("gi_preview", fin_ts, 5, 0, hints, tz)
        # Override idioma para preview (siempre es del owner)
        lang_preview = lang
        fin_str = _formato_hora_local(fin_ts, tz)
        cap = gi_t(lang_preview, "gi_ronda_caption").format(
            fin=esc(fin_str), puntos=5, pistas=gi_t(lang_preview, "gi_sin_pistas")
        )
        try:
            await query.message.reply_photo(
                photo=setup["file_id"],
                caption=cap,
                parse_mode="MarkdownV2"
            )
        except Exception as e:
            await query.message.reply_text(f"⚠️ Error en preview: {e}")
        return

    elif action == "publicar":
        campos_req = ["file_id", "file_id_reveal", "idol_name", "hint1", "hint2", "hint3", "inicio_ts", "fin_ts"]
        faltantes  = [c for c in campos_req if not setup.get(c)]
        if faltantes:
            msg = ("⚠️ Faltan: " if lang == "es" else "⚠️ Missing: ") + ", ".join(faltantes)
            await query.answer(msg, show_alert=True)
            return
        if setup["fin_ts"] <= setup["inicio_ts"]:
            msg = "⚠️ La hora de fin debe ser después del inicio." if lang == "es" else "⚠️ End time must be after start time."
            await query.answer(msg, show_alert=True)
            return
        grupos = gi_get_grupos()
        if not grupos:
            msg = "⚠️ No hay grupos registrados." if lang == "es" else "⚠️ No registered groups."
            await query.answer(msg, show_alert=True)
            return

        editing_id = setup.get("editing_prog_id")
        div_prog = setup.get("division", 1)
        with get_conn() as conn:
            if editing_id:
                conn.execute(
                    "UPDATE gi_programacion SET idol_name=?,file_id=?,file_id_reveal=?,"
                    "hint1=?,hint2=?,hint3=?,inicio_ts=?,fin_ts=?,tz_offset=?,division=?,estado='pendiente' WHERE id=?",
                    (setup["idol_name"], setup["file_id"], setup["file_id_reveal"], setup["hint1"],
                     setup["hint2"], setup["hint3"], setup["inicio_ts"], setup["fin_ts"],
                     setup.get("tz_offset", 0), div_prog, editing_id)
                )
                prog_id = editing_id
                old_task = ctx.bot_data.pop(f"gi_countdown_{prog_id}", None)
                if old_task:
                    old_task.cancel()
            else:
                conn.execute(
                    "INSERT INTO gi_programacion (idol_name,file_id,file_id_reveal,hint1,hint2,hint3,inicio_ts,fin_ts,tz_offset,division,estado) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,'pendiente')",
                    (setup["idol_name"], setup["file_id"], setup["file_id_reveal"], setup["hint1"],
                     setup["hint2"], setup["hint3"], setup["inicio_ts"], setup["fin_ts"],
                     setup.get("tz_offset", 0), div_prog)
                )
                prog_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass

        tz       = setup.get("tz_offset", 0)
        ini_str  = _formato_hora_local(setup["inicio_ts"], tz)
        fin_str2 = _formato_hora_local(setup["fin_ts"], tz)
        conf_txt = (
            f"✅ *¡Programado\\!*\n\n"
            f"👤 Idol: *{esc(setup['idol_name'])}*\n"
            f"⏰ Inicio: *{esc(ini_str)}*\n"
            f"⏹ Fin: *{esc(fin_str2)}*\n"
            f"📢 Grupos: *{len(grupos)}*"
        )
        await ctx.bot.send_message(user.id, conf_txt, parse_mode="MarkdownV2")

        tarea = asyncio.create_task(_gi_countdown(prog_id, ctx.bot, ctx.bot_data))
        ctx.bot_data[f"gi_countdown_{prog_id}"] = tarea
        ctx.bot_data.pop(f"gi_setup_{user.id}", None)
        return

    elif action == "cancelar":
        ctx.bot_data.pop(f"gi_setup_{user.id}", None)
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    # Redibujar menú
    text     = gi_build_setup_text(setup, lang)
    keyboard = gi_build_setup_keyboard(setup, lang)
    try:
        await query.message.edit_text(
            text, parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception:
        pass


async def gi_btn_participar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    chat_key = get_chat_key(update)
    user     = query.from_user
    lang     = get_idioma(chat_key)
    action   = query.data   # "gi:participar" or "gi:salir"

    chat_id_gi = update.effective_chat.id if update.effective_chat else None
    ronda = gi_get_ronda_activa(chat_key, chat_id_gi)
    if not ronda:
        await query.answer(gi_t(lang, "gi_no_ronda").replace("\\.", ".").replace("\\", ""), show_alert=True)
        return

    chat_key = ronda[2]  # usar chat_key real de la ronda
    ronda_id = ronda[0]

    # Verificar división del jugador vs división de la ronda
    div_ronda = ronda[18] if len(ronda) > 18 and ronda[18] else 1
    if action == "gi:participar" and gi_segunda_existe(chat_key):
        div_jugador = gi_get_division(chat_key, user.id)
        if div_jugador != div_ronda:
            nombre_div = ("Primera División" if div_ronda == 1 else "Segunda División") if lang == "es" \
                         else ("First Division" if div_ronda == 1 else "Second Division")
            msg_div = gi_t(lang, "gi_div_incorrecta").format(div=nombre_div)
            # Quitar escapes de MarkdownV2 para show_alert (Telegram no acepta MD aquí)
            msg_div_plain = msg_div.replace("\\.", ".").replace("\\", "")
            await query.answer(msg_div_plain, show_alert=True)
            return

    if action == "gi:participar":
        participante = gi_get_participante(ronda_id, user.id)
        if participante:
            if participante[6]:  # ya activo
                await query.answer(gi_t(lang, "gi_ya_participa"), show_alert=True)
                return
            # Existía pero salió → reactivar conservando las vidas que tenía
            with get_conn() as conn:
                conn.execute(
                    "UPDATE gi_participantes SET activo=1, username=? WHERE ronda_id=? AND user_id=?",
                    (nombre(user), ronda_id, user.id)
                )
            await query.answer(gi_t(lang, "gi_unido"), show_alert=True)
            return
        gi_upsert_participante(ronda_id, chat_key, user.id, nombre(user))
        await query.answer(gi_t(lang, "gi_unido"), show_alert=True)

    elif action == "gi:salir":
        participante = gi_get_participante(ronda_id, user.id)
        if not participante or not participante[6]:
            await query.answer(gi_t(lang, "gi_no_participa"), show_alert=True)
            return
        gi_desactivar_participante(ronda_id, user.id)
        await query.answer(gi_t(lang, "gi_salido"), show_alert=True)


async def gi_btn_confirmar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    chat_key = get_chat_key(update)
    user     = query.from_user
    lang     = get_idioma(chat_key)

    # callback: gi:confirmar:{user_id}:{ronda_id}
    parts = query.data.split(":")
    if len(parts) < 4:
        await query.answer()
        return
    target_uid = int(parts[2])
    ronda_id   = int(parts[3])

    if user.id != target_uid:
        await query.answer("Esta confirmación no es tuya.", show_alert=True)
        return

    intento_key = f"gi_intento_{chat_key}_{user.id}"
    respuesta   = ctx.bot_data.pop(intento_key, None)
    if not respuesta:
        await query.answer()
        return

    with get_conn() as conn:
        ronda = conn.execute(
            "SELECT * FROM gi_rondas WHERE id=? AND estado='activa'", (ronda_id,)
        ).fetchone()

    if not ronda:
        await query.answer("La ronda ya terminó.", show_alert=True)
        return

    # Usar chat_key real de la ronda (correcto en foros/topics)
    chat_key        = ronda[2]
    idol_name       = ronda[4]
    file_id_reveal  = ronda[6]
    puntos_actuales = ronda[14]
    chat_id         = ronda[3]
    msg_id_ronda    = ronda[17]

    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        pass

    if normalizar(respuesta) == normalizar(idol_name):
        # ¡CORRECTO!
        with get_conn() as conn:
            conn.execute(
                "UPDATE gi_rondas SET estado='terminada', ganador_id=?, ganador_nombre=? WHERE id=?",
                (user.id, nombre(user), ronda_id)
            )
        tarea = ctx.bot_data.pop(f"gi_ronda_{chat_key}", None)
        if tarea:
            tarea.cancel()
        gi_sumar_puntos(chat_key, user.id, nombre(user), puntos_actuales)

        txt_ganador = gi_t(lang, "gi_ganador").format(
            nombre=esc(nombre(user)), idol=esc(idol_name), puntos=puntos_actuales
        )
        try:
            await ctx.bot.edit_message_caption(
                chat_id=chat_id, message_id=msg_id_ronda,
                caption=txt_ganador, parse_mode="MarkdownV2"
            )
        except Exception:
            await ctx.bot.send_message(chat_id, txt_ganador, parse_mode="MarkdownV2")
        # Enviar imagen reveal con el texto del ganador como caption
        if file_id_reveal:
            try:
                await ctx.bot.send_photo(
                    chat_id, photo=file_id_reveal,
                    caption=txt_ganador, parse_mode="MarkdownV2"
                )
            except Exception:
                pass
    else:
        # INCORRECTO
        vidas = gi_restar_vida(ronda_id, user.id)
        if vidas <= 0:
            gi_desactivar_participante(ronda_id, user.id)
            txt_elim = gi_t(lang, "gi_eliminado").format(nombre=esc(nombre(user)))
            await ctx.bot.send_message(chat_id, txt_elim, parse_mode="MarkdownV2")
        else:
            txt_mal = gi_t(lang, "gi_incorrecto").format(vidas=vidas)
            await ctx.bot.send_message(chat_id, txt_mal, parse_mode="MarkdownV2")


async def gi_handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Recibe fotos en privado del owner para el setup de /idol."""
    if not update.message or update.effective_chat.type != "private":
        return
    user = update.effective_user
    if user.id != BOT_OWNER_ID:
        return
    setup = ctx.bot_data.get(f"gi_setup_{user.id}")
    if not setup or setup.get("esperando") not in ("imagen", "imagen_reveal"):
        return

    photo = update.message.photo[-1]
    campo_foto = setup.get("esperando", "imagen")
    if campo_foto == "imagen_reveal":
        setup["file_id_reveal"] = photo.file_id
    else:
        setup["file_id"] = photo.file_id
    setup["esperando"] = None
    lang   = setup.get("lang", "es")
    msg_id = setup.get("mensaje_id")

    try:
        await update.message.delete()
    except Exception:
        pass

    text     = gi_build_setup_text(setup, lang)
    keyboard = gi_build_setup_keyboard(setup, lang)
    if msg_id:
        try:
            await ctx.bot.edit_message_text(
                text, chat_id=user.id, message_id=msg_id,
                parse_mode="MarkdownV2", reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        except Exception:
            pass
    msg = await update.message.reply_text(
        text, parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    setup["mensaje_id"] = msg.message_id


async def gi_cmd_cancelar_prog(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Lista programaciones GI pendientes y permite cancelarlas (solo owner, privado)."""
    user = update.effective_user
    if user.id != BOT_OWNER_ID:
        await update.message.reply_text("⚠️ Solo el creador del bot puede usar este comando.")
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text("⚠️ Usa este comando en chat privado con el bot.")
        return

    with get_conn() as conn:
        pendientes = conn.execute(
            "SELECT id, idol_name, inicio_ts, fin_ts, tz_offset FROM gi_programacion WHERE estado='pendiente' ORDER BY inicio_ts"
        ).fetchall()

    if not pendientes:
        await update.message.reply_text("📭 No hay programaciones pendientes\\.", parse_mode="MarkdownV2")
        return

    lineas = []
    keyboard = []
    for prog in pendientes:
        pid, idol, ini_ts, fin_ts, tz = prog
        ini_str = _formato_hora_local(ini_ts, tz or 0)
        fin_str = _formato_hora_local(fin_ts, tz or 0)
        lineas.append(f"  *{esc(idol)}* — {esc(ini_str)} → {esc(fin_str)}")
        keyboard.append([
            InlineKeyboardButton(f"✏️ Editar: {idol}", callback_data=f"gi:editprog:{pid}"),
            InlineKeyboardButton(f"❌ Cancelar: {idol}", callback_data=f"gi:cancelarprog:{pid}"),
        ])

    texto = "📋 *Programaciones pendientes:*\n\n" + "\n".join(lineas)
    await update.message.reply_text(
        texto, parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )



async def _shutdown(app):
    """Cancela tareas pendientes al apagar el bot limpiamente."""
    current = asyncio.current_task()
    tasks = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    logger.info(f"[SHUTDOWN] {len(tasks)} tareas canceladas")


def main():
    init_db()
    _init_fonts()

    app = Application.builder().token(TOKEN).post_init(_limpiar_partidas_zombies).post_stop(_shutdown).build()

    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("playimpostor", cmd_nueva))
    app.add_handler(CommandHandler("join",        cmd_unirse))
    app.add_handler(CommandHandler("vote",         cmd_votar))
    app.add_handler(CommandHandler("score",       cmd_puntaje))
    app.add_handler(CommandHandler("cancel",      cmd_cancelar))
    app.add_handler(CommandHandler("howtoplay",     cmd_como_jugar))
    app.add_handler(CommandHandler("resetimpostor", cmd_resetimpostor))
    app.add_handler(CommandHandler("resetjugador",  cmd_resetjugador))
    app.add_handler(CommandHandler("resetroles",    cmd_resetroles))
    app.add_handler(CommandHandler("language",        cmd_idioma))
    app.add_handler(CommandHandler("program",             cmd_program))
    app.add_handler(CommandHandler("rivalidad",          cmd_rivalidad))
    app.add_handler(CommandHandler("all",               cmd_all))
    app.add_handler(CommandHandler("roles",             cmd_roles))
    app.add_handler(CommandHandler("addword",           cmd_addword))
    app.add_handler(CommandHandler("removeword",        cmd_removeword))
    app.add_handler(CommandHandler("words",             cmd_words))
    app.add_handler(CommandHandler("grupos",            gi_cmd_grupos))
    app.add_handler(CommandHandler("idol",              gi_cmd_idol))
    app.add_handler(CommandHandler("giscore",           gi_cmd_score))
    app.add_handler(CommandHandler("gireset",           gi_cmd_reset))
    app.add_handler(CommandHandler("gicancel",          gi_cmd_cancelar))
    app.add_handler(CommandHandler("giprog",             gi_cmd_cancelar_prog))
    app.add_handler(CommandHandler("fintemporada",       gi_cmd_fintemporada))

    app.add_handler(CallbackQueryHandler(btn_cancelar_lobby,    pattern="^cancelar_lobby$"))
    app.add_handler(CallbackQueryHandler(btn_unirse,          pattern="^unirse$"))
    app.add_handler(CallbackQueryHandler(btn_iniciar_partida, pattern="^iniciar_partida$"))
    app.add_handler(CallbackQueryHandler(btn_categoria,       pattern="^cat:"))
    app.add_handler(CallbackQueryHandler(btn_confirmar_pista,       pattern="^confirmar_pista:"))
    app.add_handler(CallbackQueryHandler(btn_confirmar_adivinanza,   pattern="^confirmar_adiv:"))
    app.add_handler(CallbackQueryHandler(btn_abrir_votar,     pattern="^abrir_votar$"))
    app.add_handler(CallbackQueryHandler(btn_voto,            pattern="^voto:"))
    app.add_handler(CallbackQueryHandler(btn_revoto,          pattern="^revoto:"))
    app.add_handler(CallbackQueryHandler(btn_programa,         pattern="^programa:"))
    app.add_handler(CallbackQueryHandler(gi_btn_cancelar_prog, pattern="^gi:cancelarprog:"))
    app.add_handler(CallbackQueryHandler(gi_btn_editprog,     pattern="^gi:editprog:"))
    app.add_handler(CallbackQueryHandler(gi_btn_toggle_grupo, pattern="^gi:toggle:"))
    app.add_handler(CallbackQueryHandler(btn_imp_config,      pattern="^imp_config:"))
    app.add_handler(CallbackQueryHandler(owner_btn_msg_grupo, pattern="^owner:msg:"))
    app.add_handler(CallbackQueryHandler(gi_btn_setup,        pattern="^gi:setup:"))
    app.add_handler(CallbackQueryHandler(gi_btn_participar,   pattern="^gi:participar$|^gi:salir$"))
    app.add_handler(CallbackQueryHandler(gi_btn_confirmar,    pattern="^gi:confirmar:"))
    app.add_handler(CallbackQueryHandler(btn_idioma,          pattern="^idioma:"))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, gi_handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_adivinanza))
    app.add_error_handler(error_handler)

    logger.info("🤖 Bot iniciado...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
