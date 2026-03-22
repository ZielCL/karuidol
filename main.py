import os
import threading
import time
import telegram
import re
from telegram import InlineQueryResultPhoto
from telegram.ext import InlineQueryHandler
from flask import Flask, request, jsonify, redirect
from telegram.error import BadRequest, RetryAfter
from telegram import ParseMode
from translations import translations
from telegram.ext import MessageHandler, Filters
from telegram import (
    Bot,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.ext import Dispatcher, CommandHandler, CallbackQueryHandler
import json
import uuid
import logging
import urllib.parse
import random
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from datetime import datetime, timedelta
from pymongo import MongoClient
from dotenv import load_dotenv
import re
import string
import math
from PIL import Image, ImageDraw, ImageFont
import requests
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from functools import wraps

load_dotenv()

TOKEN = os.getenv('TELEGRAM_TOKEN')
if not TOKEN:
    raise ValueError("No se encontró el token de Telegram")
MONGO_URI = os.getenv('MONGO_URI')
if not MONGO_URI:
    raise ValueError("No se encontró la URI de MongoDB")

# ─── IDs de admin desde .env (nunca hardcodeados) ───────────────────────────
ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID', '0'))
TU_USER_ID    = int(os.getenv('ADMIN_USER_ID', '0'))
ADMIN_IDS     = [ADMIN_USER_ID]
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)

bot = Bot(TOKEN)
dispatcher = Dispatcher(bot, None, use_context=True)

primer_mensaje = True

# MongoDB setup
client = MongoClient(MONGO_URI)
db = client['karuta_bot']
col_usuarios        = db['usuarios']
col_cartas_usuario  = db['cartas_usuario']
col_sorteos         = db['sorteos']
col_contadores      = db['contadores']
col_mercado         = db['mercado_cartas']
col_historial_ventas= db['historial_ventas']
col_drops_log       = db['drops_log']
col_temas_comandos  = db.temas_comandos

# Índices
col_mercado.create_index("id_unico", unique=True)
col_cartas_usuario.create_index("id_unico", unique=True)
col_cartas_usuario.create_index("user_id")
col_cartas_usuario.create_index([("user_id", 1), ("id_unico", 1)])  # NUEVO: índice compuesto
col_mercado.create_index("vendedor_id")
col_usuarios.create_index("user_id", unique=True)
col_usuarios.create_index("username")   # NUEVO: para búsquedas por username

from pymongo import ASCENDING
col_mercado.create_index(
    [("fecha", ASCENDING)],
    expireAfterSeconds=7*24*60*60
)

# ─── Manejador de errores global ─────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')
logger = logging.getLogger(__name__)

def error_handler(update, context):
    from telegram.error import TimedOut, NetworkError
    if isinstance(context.error, (TimedOut, NetworkError)):
        logger.warning(f"[TIMEOUT/NETWORK] {context.error}")
        return
    logger.error(f"[ERROR GLOBAL] Update {update}: {context.error}", exc_info=True)
    try:
        if update and update.effective_message:
            update.effective_message.reply_text(
                "⚠️ Ocurrió un error inesperado. Por favor intenta de nuevo."
            )
    except Exception:
        pass

dispatcher.add_error_handler(error_handler)
# ─────────────────────────────────────────────────────────────────────────────

ID_GRUPOS_PERMITIDOS = [
    -1002636853982,
    -0,
]

def grupo_oficial(func):
    @wraps(func)
    def wrapper(update, context, *args, **kwargs):
        chat = update.effective_chat
        if chat.type == 'private':
            return func(update, context, *args, **kwargs)
        if chat.id in ID_GRUPOS_PERMITIDOS:
            return func(update, context, *args, **kwargs)
        try:
            update.message.reply_text("🚫 Este bot solo puede usarse en grupos oficiales.")
        except Exception:
            pass
        return
    return wrapper

COMANDOS_POR_TEMA = {
    "album2": [5],
    "album":  [5],
    "mercado": [706]
}

def solo_en_temas_permitidos(nombre_comando):
    def decorador(func):
        @wraps(func)
        def wrapper(update, context, *args, **kwargs):
            if update.message and update.message.chat.type in ["group", "supergroup"]:
                thread_id = getattr(update.message, "message_thread_id", None)
                permitidos = COMANDOS_POR_TEMA.get(nombre_comando, [])
                if thread_id is None or thread_id not in permitidos:
                    update.message.reply_text("❌ Este comando solo se puede usar en los temas oficiales del grupo.")
                    return
            return func(update, context, *args, **kwargs)
        return wrapper
    return decorador

def solo_en_chat_general(func):
    @wraps(func)
    def wrapper(update, context, *args, **kwargs):
        if update.message and update.message.chat.type in ["group", "supergroup"]:
            if getattr(update.message, "message_thread_id", None) is not None:
                update.message.reply_text("Este comando solo puede usarse en el tema idolday (drops)")
                return
        return func(update, context, *args, **kwargs)
    return wrapper

ID_CHAT_GENERAL = -1002636853982

FRASES_PERMITIDAS = [
    "está dropeando",
    "tomaste la carta",
    "reclamó la carta",
    "Favoritos de esta carta",
    "Regla básica",
]

def borrar_mensajes_no_idolday(update, context):
    msg = update.effective_message
    try:
        if msg.chat_id == ID_CHAT_GENERAL and msg.message_thread_id is None:
            texto = (msg.text or msg.caption or "").lower()
            if (
                texto.startswith("/idolday") or
                any(frase in texto for frase in FRASES_PERMITIDAS)
            ):
                return
            def borrar_msg():
                try:
                    msg.delete()
                except Exception as e:
                    print("[Borrador mensajes] Error al borrar:", e)
            threading.Timer(3, borrar_msg).start()
    except Exception as e:
        print("[Borrador mensajes] Error:", e)

def log_command(func):
    @wraps(func)
    def wrapper(update, context, *args, **kwargs):
        user = update.effective_user
        chat = update.effective_chat
        logging.info(
            f"Comando: {func.__name__} | Usuario: {user.id} ({user.username}) | Chat: {chat.id}"
        )
        return func(update, context, *args, **kwargs)
    return wrapper

# ─── LOCKS para drops (evita race condition) ─────────────────────────────────
_drop_locks = {}
_drop_locks_mutex = threading.Lock()

def get_drop_lock(drop_id):
    with _drop_locks_mutex:
        if drop_id not in _drop_locks:
            _drop_locks[drop_id] = threading.Lock()
        return _drop_locks[drop_id]
# ─────────────────────────────────────────────────────────────────────────────

# ─── LIMPIEZA PERIÓDICA DE DROPS EN RAM ──────────────────────────────────────
DROPS_ACTIVOS = {}

def limpiar_drops_viejos():
    while True:
        try:
            ahora = time.time()
            with _drop_locks_mutex:
                expirados = [
                    k for k, v in DROPS_ACTIVOS.items()
                    if v.get("expirado") and (ahora - v.get("inicio", 0)) > 3600
                ]
            for k in expirados:
                DROPS_ACTIVOS.pop(k, None)
                _drop_locks.pop(k, None)
        except Exception as e:
            print("[limpiar_drops_viejos] Error:", e)
        time.sleep(300)

threading.Thread(target=limpiar_drops_viejos, daemon=True).start()
# ─────────────────────────────────────────────────────────────────────────────

# ─── TIMEOUT AUTOMÁTICO DE TRADES ABANDONADOS ────────────────────────────────
TRADES_EN_CURSO  = {}
TRADES_POR_USUARIO = {}
TRADE_TIMEOUT_SEG = 300  # 5 minutos

def limpiar_trades_viejos():
    while True:
        try:
            ahora = time.time()
            trades_expirados = [
                tid for tid, t in list(TRADES_EN_CURSO.items())
                if ahora - t.get("inicio", ahora) > TRADE_TIMEOUT_SEG
            ]
            for tid in trades_expirados:
                trade = TRADES_EN_CURSO.pop(tid, None)
                if trade:
                    for uid in trade.get("usuarios", []):
                        TRADES_POR_USUARIO.pop(uid, None)
                    try:
                        bot.send_message(
                            chat_id=trade["chat_id"],
                            text="⏰ El intercambio expiró por inactividad.",
                            message_thread_id=trade.get("thread_id")
                        )
                    except Exception:
                        pass
        except Exception as e:
            print("[limpiar_trades_viejos] Error:", e)
        time.sleep(60)

threading.Thread(target=limpiar_trades_viejos, daemon=True).start()
# ─────────────────────────────────────────────────────────────────────────────

COOLDOWN_USUARIO_SEG = 6 * 60 * 60
COOLDOWN_GRUPO_SEG   = 30
COOLDOWN_GRUPO       = {}

if not os.path.isfile('cartas.json'):
    raise ValueError("No se encontró el archivo cartas.json")
with open('cartas.json', 'r') as f:
    cartas = json.load(f)

# ─── SETS PRECALCULADOS (una sola vez al arrancar) ───────────────────────────
def _precalcular_sets():
    sets = {}
    for c in cartas:
        key = c.get("grupo") or c.get("set")
        if key:
            sets.setdefault(key, set()).add((c["nombre"], c["version"]))
    return sets

SETS_PRECALCULADOS = _precalcular_sets()
# ─────────────────────────────────────────────────────────────────────────────

SESIONES_REGALO = {}

ESTADOS_CARTA = [
    ("Excelente", "★★★"),
    ("Buen estado", "★★☆"),
    ("Mal estado", "★☆☆"),
    ("Muy mal estado", "☆☆☆")
]
ESTADO_LISTA = ["Excelente", "Buen estado", "Mal estado", "Muy mal estado"]

BASE_PRICE = 250
RAREZA     = 5000

ESTADO_MULTIPLICADORES = {
    "Excelente estado": 1.0,
    "Buen estado": 0.4,
    "Mal estado": 0.15,
    "Muy mal estado": 0.05
}

user_last_cmd  = {}
group_last_cmd = {}
COOLDOWN_USER  = 3
COOLDOWN_GROUP = 1

def solo_en_tema_asignado(comando):
    def decorator(func):
        @wraps(func)
        def wrapper(update, context, *args, **kwargs):
            chat_id = update.effective_chat.id if update.effective_chat else None

            # En privado: siempre permitir
            if update.effective_chat and update.effective_chat.type == "private":
                return func(update, context, *args, **kwargs)

            tema_asignado = col_temas_comandos.find_one({"chat_id": chat_id, "comando": comando})

            # Si no hay tema configurado para este comando: permitir en cualquier lugar
            if not tema_asignado:
                return func(update, context, *args, **kwargs)

            threads_permitidos = set()
            if "thread_ids" in tema_asignado:
                threads_permitidos = {str(tid) for tid in tema_asignado["thread_ids"]}
            elif "thread_id" in tema_asignado:
                threads_permitidos = {str(tema_asignado["thread_id"])}

            thread_id_actual = None
            if getattr(update, 'message', None):
                thread_id_actual = str(getattr(update.message, "message_thread_id", None))
            elif getattr(update, 'callback_query', None):
                thread_id_actual = str(getattr(update.callback_query.message, "message_thread_id", None))

            if thread_id_actual not in threads_permitidos:
                try:
                    if getattr(update, 'message', None):
                        update.message.delete()
                    elif getattr(update, 'callback_query', None):
                        update.callback_query.answer(
                            "❌ Solo disponible en los temas asignados.", show_alert=True
                        )
                except Exception:
                    pass
                return
            return func(update, context, *args, **kwargs)
        return wrapper
    return decorator

def en_tema_asignado_o_privado(comando):
    def decorator(func):
        @wraps(func)
        def wrapper(update, context, *args, **kwargs):
            chat = update.effective_chat
            chat_id = chat.id if chat else None

            # En privado: siempre permitir
            if chat and chat.type == "private":
                return func(update, context, *args, **kwargs)

            tema_asignado = col_temas_comandos.find_one({"chat_id": chat_id, "comando": comando})

            # Si no hay tema configurado: permitir en cualquier lugar del grupo
            if not tema_asignado:
                return func(update, context, *args, **kwargs)

            threads_permitidos = set()
            if "thread_ids" in tema_asignado:
                threads_permitidos = {str(tid) for tid in tema_asignado["thread_ids"]}
            elif "thread_id" in tema_asignado:
                threads_permitidos = {str(tema_asignado["thread_id"])}

            thread_id_actual = None
            if getattr(update, 'message', None):
                thread_id_actual = str(getattr(update.message, "message_thread_id", None))
            elif getattr(update, 'callback_query', None):
                thread_id_actual = str(getattr(update.callback_query.message, "message_thread_id", None))

            if thread_id_actual in threads_permitidos:
                return func(update, context, *args, **kwargs)
            try:
                if getattr(update, 'message', None):
                    update.message.delete()
                elif getattr(update, 'callback_query', None):
                    update.callback_query.answer(
                        "❌ Solo disponible en su tema asignado o en privado.", show_alert=True
                    )
            except Exception:
                pass
            return
        return wrapper
    return decorator

def mensaje_tutorial_privado(update, context):
    try:
        user_id = update.message.from_user.id
        chat_id = update.message.chat_id
        if update.message.chat.type != "private":
            return
        doc = col_usuarios.find_one({"user_id": user_id})
        lang = (getattr(update.effective_user, "language_code", "") or "").lower()
        is_es = lang.startswith("es")
        if is_es:
            if doc:
                texto = (
                    "👋 <b>¡Hola de nuevo, coleccionista!</b>\n\n"
                    "Recuerda que este bot funciona principalmente en el <a href='https://t.me/karukpop'>grupo oficial</a>.\n\n"
                    "🔹 Puedes revisar tu álbum de cartas con <b>/album</b> (aquí solo modo lectura)\n"
                    "🔹 Usa <b>/idolday</b> y los comandos de colección en el grupo oficial para jugar.\n\n"
                    "¿Tienes dudas? Pregunta en el grupo o usa /help aquí mismo."
                )
            else:
                texto = (
                    "👋 <b>¡Bienvenido a KaruKpop Bot!</b>\n\n"
                    "Este bot funciona principalmente en el <a href='https://t.me/karukpop'>grupo oficial</a>.\n\n"
                    "<b>¿Qué puedes hacer aquí?</b>\n"
                    "🔹 Colecciona cartas de idols con <b>/idolday</b>\n"
                    "🔹 Intercambia cartas usando <b>/trk</b>\n"
                    "🔹 Revisa tu álbum con <b>/album</b>\n\n"
                    "<i>¡Únete al grupo y empieza a coleccionar!</i>"
                )
        else:
            if doc:
                texto = (
                    "👋 <b>Welcome back, collector!</b>\n\n"
                    "Remember, this bot works mainly in the <a href='https://t.me/karukpop'>official group</a>.\n\n"
                    "🔹 View your card album with <b>/album</b>\n"
                    "🔹 Use <b>/idolday</b> in the group to collect cards.\n\n"
                    "Any questions? Ask in the group or use /help here."
                )
            else:
                texto = (
                    "👋 <b>Welcome to KaruKpop Bot!</b>\n\n"
                    "This bot works mainly in the <a href='https://t.me/karukpop'>official group</a>.\n\n"
                    "<b>What can you do here?</b>\n"
                    "🔹 Collect idol cards using <b>/idolday</b>\n"
                    "🔹 Trade cards using <b>/trk</b>\n"
                    "🔹 Check your album with <b>/album</b>\n\n"
                    "<i>Join the group and start collecting!</i>"
                )
        context.bot.send_message(
            chat_id=chat_id, text=texto,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
    except Exception as e:
        print("[/start privado] Error:", e)

# ─── PayPal ───────────────────────────────────────────────────────────────────
PAYPAL_CLIENT_ID     = os.environ.get("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET")
WEBHOOK_SECRET       = os.environ.get("WEBHOOK_SECRET", "")

def get_paypal_token():
    url = "https://api-m.paypal.com/v1/oauth2/token"
    resp = requests.post(url, auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET), data={"grant_type": "client_credentials"})
    resp.raise_for_status()
    return resp.json()["access_token"]

def buscar_gemas(monto):
    montos_validos = {
        "1.00": 50,   "2.00": 100,  "8.00": 500,
        "13.00": 1000, "60.00": 5000, "100.00": 10000,
    }
    try:
        key = f"{float(monto):.2f}"
        return montos_validos.get(key)
    except Exception:
        return None

@app.route("/paypal/create_order", methods=["POST"])
def create_order():
    data = request.json
    user_id   = data["user_id"]
    pack_gemas= data["pack"]
    amount    = data["amount"]
    access_token = get_paypal_token()
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    order_data = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": f"user_{user_id}_{pack_gemas}",
            "amount": {"currency_code": "USD", "value": str(amount)},
            "custom_id": str(user_id)
        }],
        "application_context": {
            "return_url": "https://karuidol.onrender.com/paypal/return",
            "cancel_url": "https://karuidol.onrender.com/paypal/cancel"
        }
    }
    resp = requests.post("https://api-m.paypal.com/v2/checkout/orders", headers=headers, json=order_data)
    resp.raise_for_status()
    order = resp.json()
    for link in order["links"]:
        if link["rel"] == "approve":
            return jsonify({"url": link["href"], "order_id": order["id"]})
    return "No approve link", 400

@app.route("/paypal/webhook", methods=["POST"])
def paypal_webhook():
    # ─── Validación de origen del webhook ────────────────────────────────────
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    # PayPal no usa ese header, pero sí puedes verificar firma PayPal aquí si lo implementas.
    # Por ahora validamos que el body sea procesable.
    data = request.json
    if not data:
        return "", 400
    # ─────────────────────────────────────────────────────────────────────────

    event_type = data.get("event_type")
    resource   = data.get("resource", {})

    if (
        event_type == "PAYMENT.CAPTURE.COMPLETED" or
        (event_type == "PAYMENT.CAPTURE.PENDING" and resource.get("status") == "COMPLETED")
    ):
        try:
            user_id        = int(resource.get("custom_id", 0))
            amount         = resource["amount"]["value"]
            pago_id        = resource.get("id")
            cantidad_gemas = buscar_gemas(amount)

            if not cantidad_gemas:
                print(f"❌ Monto no reconocido: {amount} USD")
                return "", 200

            # ─── Prevención de doble entrega con upsert atómico ──────────────
            resultado = db.historial_compras_gemas.update_one(
                {"pago_id": pago_id},
                {"$setOnInsert": {
                    "pago_id": pago_id,
                    "user_id": user_id,
                    "cantidad_gemas": cantidad_gemas,
                    "monto_usd": amount,
                    "fecha": datetime.utcnow()
                }},
                upsert=True
            )
            if resultado.matched_count > 0:
                print(f"[PayPal] Pago {pago_id} ya procesado anteriormente.")
                return "", 200
            # ─────────────────────────────────────────────────────────────────

            col_usuarios.update_one(
                {"user_id": user_id},
                {"$inc": {"gemas": cantidad_gemas}},
                upsert=True
            )

            try:
                bot.send_message(
                    chat_id=user_id,
                    text=f"🎉 ¡Compra confirmada! Has recibido {cantidad_gemas} gemas en KaruKpop.\n¡Gracias por tu apoyo! 💎"
                )
            except Exception as e:
                print("No se pudo notificar al usuario:", e)

            try:
                bot.send_message(
                    chat_id=ADMIN_USER_ID,
                    text=f"💸 Nuevo pago confirmado:\n• Usuario: <code>{user_id}</code>\n• Gemas: {cantidad_gemas}\n• Monto: ${amount} USD",
                    parse_mode="HTML"
                )
            except Exception as e:
                print("No se pudo notificar al admin:", e)

            print(f"✅ Entregadas {cantidad_gemas} gemas a user_id={user_id} por {amount} USD")
        except Exception as e:
            print("❌ Error en webhook:", e)
    return "", 200

@app.route("/paypal/return")
def paypal_return():
    order_id = request.args.get("token")
    if not order_id:
        return "Error: No se recibió el order_id de PayPal."
    try:
        access_token = get_paypal_token()
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        resp = requests.post(
            f"https://api-m.paypal.com/v2/checkout/orders/{order_id}/capture",
            headers=headers
        )
        if resp.ok:
            print("[PayPal] Orden capturada correctamente:", resp.json())
        else:
            print("[PayPal] Orden ya estaba capturada o falló:", resp.text)
        return "¡Gracias por tu compra! Puedes volver a Telegram."
    except Exception as e:
        print("[PayPal] Error capturando orden:", e)
        return "Hubo un error al procesar tu pago. Contacta soporte."

@app.route("/paypal/cancel")
def paypal_cancel():
    return "Pago cancelado."

# ─── Misiones ────────────────────────────────────────────────────────────────
def actualiza_mision_diaria(user_id, context=None):
    user_doc = col_usuarios.find_one({"user_id": user_id}) or {}
    misiones = user_doc.get("misiones", {})
    hoy_str  = datetime.utcnow().strftime('%Y-%m-%d')
    ultima   = misiones.get("ultima_mision_idolday", "")

    if ultima != hoy_str:
        misiones["idolday_hoy"]      = 0
        misiones["idolday_entregada"]= ""
        misiones["primer_drop"]      = {}

    # Misión primer drop
    premio_primer_drop = False
    if misiones.get("primer_drop", {}).get("fecha") != hoy_str:
        col_usuarios.update_one({"user_id": user_id}, {"$inc": {"kponey": 50}})
        misiones["primer_drop"] = {"fecha": hoy_str, "premio": True}
        premio_primer_drop = True
        if context:
            try:
                context.bot.send_message(
                    chat_id=user_id,
                    text="🎉 ¡Primer drop del día realizado!\nHas recibido <b>50 Kponey</b>.",
                    parse_mode="HTML"
                )
            except Exception:
                pass

    # Misión 3 drops
    misiones["idolday_hoy"] = misiones.get("idolday_hoy", 0) + 1
    misiones["ultima_mision_idolday"] = hoy_str

    mision_completada = misiones["idolday_hoy"] >= 3
    premio_tres_drops = False
    if mision_completada and misiones.get("idolday_entregada", "") != hoy_str:
        col_usuarios.update_one({"user_id": user_id}, {"$inc": {"kponey": 150}})
        if context:
            try:
                context.bot.send_message(
                    chat_id=user_id,
                    text="🎉 ¡Misión diaria completada!\nHas recibido <b>150 Kponey</b> por hacer 3 drops hoy.",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        misiones["idolday_entregada"] = hoy_str
        premio_tres_drops = True

    col_usuarios.update_one({"user_id": user_id}, {"$set": {"misiones": misiones}})
    return mision_completada, premio_tres_drops, premio_primer_drop

# ─── Cooldown de comandos ─────────────────────────────────────────────────────
def check_cooldown(update):
    now = time.time()
    uid = update.effective_user.id
    gid = update.effective_chat.id
    if uid in user_last_cmd and now - user_last_cmd[uid] < COOLDOWN_USER:
        return False, f"¡Espera {COOLDOWN_USER} segundos entre comandos!"
    if gid in group_last_cmd and now - group_last_cmd[gid] < COOLDOWN_GROUP:
        return False, "Este grupo está usando comandos muy rápido. Espera 1 segundo."
    return True, None

def cooldown_critico(func):
    @wraps(func)
    def wrapper(update, context, *args, **kwargs):
        ok, msg = check_cooldown(update)
        if not ok:
            update.message.reply_text(msg)
            return
        now = time.time()
        user_last_cmd[update.effective_user.id]  = now
        group_last_cmd[update.effective_chat.id] = now
        return func(update, context, *args, **kwargs)
    return wrapper

# ─── Imagen con número ───────────────────────────────────────────────────────
def agregar_numero_a_imagen(imagen_url, numero):
    response = requests.get(imagen_url)
    img  = Image.open(BytesIO(response.content)).convert("RGBA")
    draw = ImageDraw.Draw(img)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    font_size = int(img.height * 0.02)
    font  = ImageFont.truetype(font_path, size=font_size)
    texto = f"#{numero}"
    bbox  = draw.textbbox((0, 0), texto, font=font)
    text_width  = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    x = (img.width - text_width) // 2
    y = img.height - text_height - 8
    draw.rectangle([x-6, y-4, x-6+text_width+14, y-4+text_height+8], fill=(0,0,0,170))
    draw.text((x, y), texto, font=font, fill=(255,255,255,255))
    output = BytesIO()
    img.save(output, format="PNG")
    output.seek(0)
    return output

# ─── Catálogos de objetos ─────────────────────────────────────────────────────
CATALOGO_OBJETOS = {
    "bono_idolday": {
        "nombre": "Bono Idolday", "emoji": "🎟️",
        "desc": "Permite hacer un /idolday adicional sin esperar el cooldown.",
        "precio": 1600
    },
    "lightstick": {
        "nombre": "Lightstick", "emoji": "💡",
        "desc": "Mejora el estado de una carta.",
        "precio": 4000
    },
    "ticket_agregar_apodo": {
        "nombre": "Ticket Agregar Apodo", "emoji": "🏷️",
        "desc": 'Permite agregar un apodo personalizado a una carta.',
        "precio": 2600
    },
    "abrazo_de_bias": {
        "nombre": "Abrazo de Bias", "emoji": "🤗",
        "desc": "Reduce el cooldown de /idolday a la mitad, una vez.",
        "precio": 600
    }
}

CATALOGO_OBJETOSG = {
    "bono_idolday":        {"nombre": "Bono Idolday",        "emoji": "🎟️", "desc": "Bono extra de idolday.", "precio_gemas": 160},
    "lightstick":          {"nombre": "Lightstick",          "emoji": "💡", "desc": "Mejora cartas.",          "precio_gemas": 400},
    "ticket_agregar_apodo":{"nombre": "Ticket Agregar Apodo","emoji": "🏷️", "desc": "Apodo para carta.",       "precio_gemas": 260},
    "abrazo_de_bias":      {"nombre": "Abrazo de Bias",      "emoji": "🤗", "desc": "Reduce cooldown.",        "precio_gemas": 60},
}

# ─── Utilidades de cartas ─────────────────────────────────────────────────────
def extraer_card_id_de_id_unico(id_unico):
    if id_unico and len(id_unico) > 4:
        try:
            return int(id_unico[4:])
        except Exception:
            return None
    return None

def revisar_sets_completados(user_id, context):
    """Usa SETS_PRECALCULADOS para evitar iterar cartas.json en cada drop."""
    cartas_usuario = list(col_cartas_usuario.find({"user_id": user_id}))
    cartas_usuario_unicas = set((c["nombre"], c["version"]) for c in cartas_usuario)

    doc_usuario  = col_usuarios.find_one({"user_id": user_id}) or {}
    sets_premiados = set(doc_usuario.get("sets_premiados", []))
    premios = []

    for s, cartas_set in SETS_PRECALCULADOS.items():
        if cartas_set and cartas_set.issubset(cartas_usuario_unicas) and s not in sets_premiados:
            monto = 500 * len(cartas_set)
            premios.append((s, monto))
            sets_premiados.add(s)
            col_usuarios.update_one(
                {"user_id": user_id},
                {"$inc": {"kponey": monto}, "$set": {"sets_premiados": list(sets_premiados)}},
                upsert=True
            )
            try:
                context.bot.send_message(
                    chat_id=user_id,
                    text=f"🎉 ¡Completaste el set <b>{s}</b>!\nPremio: <b>+{monto} Kponey 🪙</b>",
                    parse_mode="HTML"
                )
            except Exception:
                pass
    return premios

PACKS_GEMAS = [
    {"pack": "x50",    "amount": 1.00,   "label": "💎 x50 Gems (USD $1)"},
    {"pack": "x100",   "amount": 2.00,   "label": "💎 x100 Gems (USD $2)"},
    {"pack": "x500",   "amount": 8.00,   "label": "💎 x500 Gems (USD $8)"},
    {"pack": "x1000",  "amount": 13.00,  "label": "💎 x1000 Gems (USD $13)"},
    {"pack": "x5000",  "amount": 60.00,  "label": "💎 x5000 Gems (USD $60)"},
    {"pack": "x10000", "amount": 100.00, "label": "💎 x10000 Gems (USD $100)"},
]

def tienda_gemas(update, context):
    user_id = update.message.from_user.id
    texto = (
        "💎 <b>Tienda de Gemas KaruKpop</b>\n\n"
        "Compra gemas de forma segura con PayPal.\n\n"
        "Elige el pack que deseas comprar:"
    )
    botones = [
        [InlineKeyboardButton(p["label"], callback_data=f"tienda_paypal_{p['pack']}_{p['amount']}")]
        for p in PACKS_GEMAS
    ]
    update.message.reply_text(texto, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(botones))

def historial_gemas_admin(update, context):
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        update.message.reply_text("No tienes permiso para usar este comando.")
        return
    if not context.args:
        update.message.reply_text("Usa: /historialgemas <@username o id_usuario>")
        return
    arg = context.args[0]
    query = {}
    if arg.startswith("@"):
        query["username"] = arg[1:].lower()
    else:
        try:
            query["user_id"] = int(arg)
        except ValueError:
            update.message.reply_text("Debes ingresar un @username válido o un ID numérico.")
            return
    compras = list(db.historial_compras_gemas.find(query).sort("fecha", -1).limit(10))
    if not compras:
        update.message.reply_text("Ese usuario no tiene compras de gemas registradas.")
        return
    msg = "🧾 *Historial de gemas:*\n\n"
    for c in compras:
        fecha = c['fecha'].strftime("%d/%m/%Y %H:%M")
        msg += f"- {c.get('cantidad_gemas','?')} gemas el {fecha}\n"
    update.message.reply_text(msg, parse_mode="Markdown")

dispatcher.add_handler(CommandHandler("historialgemas", historial_gemas_admin))

def manejador_tienda_paypal(update, context):
    query   = update.callback_query
    data    = query.data
    user_id = query.from_user.id
    partes  = data.split("_")
    # formato: tienda_paypal_x100_2.0
    pack   = partes[2]
    amount = float(partes[3])
    try:
        resp = requests.post(
            "https://karuidol.onrender.com/paypal/create_order",
            json={"user_id": user_id, "pack": pack, "amount": amount},
            timeout=10
        )
        if resp.ok:
            url = resp.json().get("url")
            if url:
                query.answer("¡Revisa tu chat privado con el bot!", show_alert=True)
                try:
                    context.bot.send_message(
                        chat_id=user_id,
                        text=(
                            f"🔗 <b>Pago de Gemas KaruKpop</b>\n\n"
                            f"Pack: <b>{pack}</b>\nMonto: <b>USD ${amount:.2f}</b>\n\n"
                            f"<a href='{url}'>Haz clic aquí para pagar con PayPal</a>\n\n"
                            "Cuando el pago esté confirmado, recibirás las gemas automáticamente."
                        ),
                        parse_mode="HTML",
                        disable_web_page_preview=True
                    )
                except Exception:
                    query.answer(
                        "No pude enviarte el link. Debes iniciar el chat privado con el bot primero.",
                        show_alert=True
                    )
            else:
                query.answer("No se pudo generar el enlace de pago.", show_alert=True)
        else:
            query.answer("Error al conectar con PayPal.", show_alert=True)
    except Exception:
        query.answer("Fallo al generar enlace de pago.", show_alert=True)

# ─── Precios ──────────────────────────────────────────────────────────────────
def precio_carta_tabla(estado_estrella, card_id):
    try:
        card_id = int(card_id)
    except Exception:
        card_id = 0
    tabla = {
        "★★★": [(1, 37500), (10, 10000), (100, 5000), (9999, 2500)],
        "★★☆": [(1, 15000), (10, 4000),  (100, 2000), (9999, 1000)],
        "★☆☆": [(1, 9000),  (10, 2400),  (100, 1200), (9999, 600)],
        "☆☆☆": [(1, 6000),  (10, 1600),  (100, 800),  (9999, 400)],
    }
    if estado_estrella not in tabla:
        return 0
    if card_id == 1:        return tabla[estado_estrella][0][1]
    elif 2 <= card_id <= 10: return tabla[estado_estrella][1][1]
    elif 11 <= card_id <= 100: return tabla[estado_estrella][2][1]
    else:                   return tabla[estado_estrella][3][1]

def precio_carta_karuta(nombre, version, estado, id_unico=None, card_id=None):
    if card_id is None and id_unico:
        card_id = extraer_card_id_de_id_unico(id_unico)
    if card_id == 1:          return 12000
    elif card_id == 2:        return 7000
    elif card_id == 3:        return 4500
    elif card_id == 4:        return 3000
    elif card_id == 5:        return 2250
    elif 6 <= card_id <= 10:  return 1500
    elif 11 <= card_id <= 100:return 600
    else:                     return 500

def obtener_grupos_del_mercado():
    return sorted({c.get("grupo", "") for c in col_mercado.find() if c.get("grupo")})

def random_id_unico(card_id):
    pool = string.ascii_lowercase + string.digits
    base = ''.join(random.choices(pool, k=4))
    return f"{base}{card_id}"

def imagen_de_carta(nombre, version):
    for carta in cartas:
        if carta['nombre'] == nombre and carta['version'] == version:
            return carta.get('imagen')
    return None

def grupo_de_carta(nombre, version):
    for carta in cartas:
        if carta['nombre'] == nombre and carta['version'] == version:
            return carta.get('grupo', '')
    return ""

def crear_drop_id(chat_id, mensaje_id):
    return f"{chat_id}_{mensaje_id}"

def es_admin(update, context=None):
    chat    = update.effective_chat
    user_id = update.effective_user.id
    if chat.type not in ["group", "supergroup"]:
        return False
    try:
        member = bot.get_chat_member(chat.id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

def puede_usar_idolday(user_id):
    user_doc        = col_usuarios.find_one({"user_id": user_id}) or {}
    bono            = user_doc.get('bono', 0)
    objetos         = user_doc.get('objetos', {})
    bonos_inventario= objetos.get('bono_idolday', 0)
    last            = user_doc.get('last_idolday')
    ahora           = datetime.utcnow()
    cooldown_listo  = False
    bono_listo      = False
    if last:
        cooldown_listo = (ahora - last).total_seconds() >= 6 * 3600
    else:
        cooldown_listo = True
    if (bono and bono > 0) or (bonos_inventario and bonos_inventario > 0):
        bono_listo = True
    return cooldown_listo, bono_listo

def expira_drop(drop_id):
    drop = DROPS_ACTIVOS.get(drop_id)
    if not drop or drop.get("expirado"):
        return
    keyboard = [[
        InlineKeyboardButton("❌", callback_data="expirado"),
        InlineKeyboardButton("❌", callback_data="expirado"),
    ]]
    try:
        bot.edit_message_reply_markup(
            chat_id=drop["chat_id"],
            message_id=drop["mensaje_id"],
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception:
        pass
    drop["expirado"] = True

def desbloquear_drop(drop_id):
    time.sleep(60)
    drop = DROPS_ACTIVOS.get(drop_id)
    if drop and not drop.get("expirado"):
        drop["expirado"] = True
        try:
            col_drops_log.insert_one({
                "evento": "expirado",
                "drop_id": drop_id,
                "cartas": drop.get("cartas", []),
                "dueño": drop.get("dueño"),
                "chat_id": drop.get("chat_id"),
                "mensaje_id": drop.get("mensaje_id"),
                "fecha": datetime.utcnow(),
                "usuarios_reclamaron": drop.get("usuarios_reclamaron", []),
            })
        except Exception:
            pass

def estados_disponibles_para_carta(nombre, version):
    return [c for c in cartas if c['nombre'] == nombre and c['version'] == version]

def get_user_lang(user_id, update):
    user = col_usuarios.find_one({"user_id": user_id})
    return (
        (user.get("lang") if user else None)
        or getattr(update.effective_user, "language_code", "")
        or "en"
    )[:2]

def t(user_id, update):
    lang = get_user_lang(user_id, update)
    return translations.get(lang, translations["en"])

# ─── Referidos ────────────────────────────────────────────────────────────────
REFERRAL_REWARDS = [
    (5,   "Abrazo de Bias x5",  {"objetos.abrazo_bias": 5}),
    (15,  "Bono Idolday x2",    {"objetos.bono_idolday": 2}),
    (30,  "Lightstick x2",      {"objetos.lightstick": 2}),
    (50,  "Abrazo de Bias x10", {"objetos.abrazo_bias": 10}),
    (70,  "Bono Idolday x5",    {"objetos.bono_idolday": 5}),
    (100, "Lightstick x6",      {"objetos.lightstick": 6}),
]

def callback_invitamenu(update, context):
    try:
        query   = update.callback_query
        user_id = query.from_user.id
        texto   = t(user_id, update)

        if query.data == "menu_invitacion":
            link = f"https://t.me/{context.bot.username}?start=ref{user_id}"
            botones = [
                [InlineKeyboardButton(texto["button_progress"], callback_data="menu_progress")],
                [InlineKeyboardButton("🔗 Compartir", url=f"https://t.me/share/url?url={link}")]
            ]
            query.edit_message_text(
                texto["invite_link"].format(link=link),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(botones),
                disable_web_page_preview=True
            )

        elif query.data == "menu_progress":
            user_doc       = col_usuarios.find_one({"user_id": user_id}) or {}
            referidos      = user_doc.get("referidos", [])
            ref_premios    = user_doc.get("ref_premios", [])
            total          = len(referidos)
            rewards_text   = ""
            premios_obtenidos = list(ref_premios)

            for cantidad, nombre_p, obj_dict in REFERRAL_REWARDS:
                if total >= cantidad:
                    if cantidad not in premios_obtenidos:
                        col_usuarios.update_one({"user_id": user_id}, {"$addToSet": {"ref_premios": cantidad}})
                        col_usuarios.update_one({"user_id": user_id}, {"$inc": obj_dict})
                        rewards_text += texto["reward_now"].format(prize=nombre_p, count=cantidad) + "\n"
                        premios_obtenidos.append(cantidad)
                    else:
                        rewards_text += texto["reward_already"].format(prize=nombre_p) + "\n"
                else:
                    rewards_text += texto["reward_locked"].format(prize=nombre_p, count=cantidad) + "\n"

            reply = texto["invite_info"].format(count=total, rewards=rewards_text)
            botones = [[InlineKeyboardButton(texto["button_invite"], callback_data="menu_invitacion")]]
            query.edit_message_text(reply, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(botones))

        query.answer()
    except Exception as e:
        print(f"[callback_invitamenu] Error: {e}")

# ─── Help ─────────────────────────────────────────────────────────────────────
@log_command
def comando_help(update, context):
    user_id = update.effective_user.id
    texto   = t(user_id, update)
    if update.message.chat.type != "private":
        update.message.reply_text(texto["help_message_group"])
        return
    faqs = [
        [InlineKeyboardButton(texto["faq_kponey"],   callback_data="help_faq_kponey")],
        [InlineKeyboardButton(texto["faq_gemas"],    callback_data="help_faq_gemas")],
        [InlineKeyboardButton(texto["faq_set"],      callback_data="help_faq_set")],
        [InlineKeyboardButton(texto["faq_mision"],   callback_data="help_faq_mision")],
        [InlineKeyboardButton(texto["commands_button"], callback_data="help_comandos")],
        [InlineKeyboardButton(texto["button_invite"],callback_data="menu_invitacion")],
        [InlineKeyboardButton(texto["button_progress"],callback_data="menu_progress")],
    ]
    context.bot.send_message(
        chat_id=update.message.chat_id,
        text=texto["help_title"],
        reply_markup=InlineKeyboardMarkup(faqs),
        parse_mode="HTML"
    )

def callback_help(update, context):
    try:
        query   = update.callback_query
        data    = query.data
        user_id = query.from_user.id
        texto   = t(user_id, update)

        textos_faq = {
            "help_faq_kponey": texto["faq_kponey_desc"],
            "help_faq_gemas":  texto["faq_gemas_desc"],
            "help_faq_set":    texto["faq_set_desc"],
            "help_faq_mision": texto["faq_mision_desc"],
        }
        faqs = [
            [InlineKeyboardButton(texto["faq_kponey"],      callback_data="help_faq_kponey")],
            [InlineKeyboardButton(texto["faq_gemas"],       callback_data="help_faq_gemas")],
            [InlineKeyboardButton(texto["faq_set"],         callback_data="help_faq_set")],
            [InlineKeyboardButton(texto["faq_mision"],      callback_data="help_faq_mision")],
            [InlineKeyboardButton(texto["commands_button"], callback_data="help_comandos")],
            [InlineKeyboardButton(texto["button_invite"],   callback_data="menu_invitacion")],
            [InlineKeyboardButton(texto["button_progress"], callback_data="menu_progress")],
        ]
        faqs_markup = InlineKeyboardMarkup(faqs)
        volver = texto["volver"]

        comandos = [
            [InlineKeyboardButton("🌸 /idolday",    callback_data="help_idolday")],
            [InlineKeyboardButton("📗 /album",      callback_data="help_album")],
            [InlineKeyboardButton("🔎 /ampliar",    callback_data="help_ampliar")],
            [InlineKeyboardButton("🎒 /inventario", callback_data="help_inventario")],
            [InlineKeyboardButton("⭐ /fav",         callback_data="help_fav")],
            [InlineKeyboardButton("🌟 /favoritos",  callback_data="help_favoritos")],
            [InlineKeyboardButton("📚 /set",         callback_data="help_set")],
            [InlineKeyboardButton("📈 /setsprogreso",callback_data="help_setsprogreso")],
            [InlineKeyboardButton("🤝 /trk",         callback_data="help_trk")],
            [InlineKeyboardButton("💰 /vender",      callback_data="help_vender")],
            [InlineKeyboardButton("🛒 /comprar",     callback_data="help_comprar")],
            [InlineKeyboardButton("🎴 /retirar",     callback_data="help_retirar")],
            [InlineKeyboardButton("⌛ /kkp",          callback_data="help_kkp")],
            [InlineKeyboardButton("💸 /precio",      callback_data="help_precio")],
            [InlineKeyboardButton(volver,            callback_data="help_volver_faq")]
        ]
        comandos_markup = InlineKeyboardMarkup(comandos)

        textos_comandos = {
            "help_idolday":     texto["help_idolday_desc"],
            "help_album":       texto["help_album_desc"],
            "help_ampliar":     texto["help_ampliar_desc"],
            "help_inventario":  texto["help_inventario_desc"],
            "help_fav":         texto["help_fav_desc"],
            "help_favoritos":   texto["help_favoritos_desc"],
            "help_set":         texto["help_set_desc"],
            "help_setsprogreso":texto["help_setsprogreso_desc"],
            "help_trk":         texto["help_trk_desc"],
            "help_vender":      texto["help_vender_desc"],
            "help_comprar":     texto["help_comprar_desc"],
            "help_retirar":     texto["help_retirar_desc"],
            "help_kkp":         texto["help_kkp_desc"],
            "help_precio":      texto["help_precio_desc"],
        }

        if data == "help_comandos":
            query.edit_message_text(texto["commands_menu"], reply_markup=comandos_markup, parse_mode="HTML")
        elif data == "help_volver_faq":
            query.edit_message_text(texto["help_title"], reply_markup=faqs_markup, parse_mode="HTML")
        elif data in textos_faq:
            query.edit_message_text(textos_faq[data], reply_markup=faqs_markup, parse_mode="HTML")
        elif data in textos_comandos:
            query.edit_message_text(textos_comandos[data], reply_markup=comandos_markup, parse_mode="HTML")
        else:
            query.answer(texto["unknown_command"])
    except Exception as e:
        print(f"[callback_help] Error: {e}")

# ─── Settema / Removetema / Vertemas ─────────────────────────────────────────
@grupo_oficial
def comando_settema(update, context):
    user_id = update.message.from_user.id
    chat_id = update.effective_chat.id
    if not es_admin(update) and user_id != TU_USER_ID:
        update.message.reply_text("Solo un administrador puede configurar esto.")
        return
    if len(context.args) < 2:
        update.message.reply_text("Uso: /settema <thread_id(s)> <comando>")
        return
    *thread_ids, comando = context.args
    try:
        thread_ids = [int(tid) for tid in thread_ids]
    except Exception:
        update.message.reply_text("Todos los thread_id deben ser numéricos.")
        return
    entry  = col_temas_comandos.find_one({"chat_id": chat_id, "comando": comando})
    nuevos = set(thread_ids)
    if entry:
        nuevos = set(entry.get("thread_ids", [])) | nuevos
    col_temas_comandos.update_one(
        {"chat_id": chat_id, "comando": comando},
        {"$set": {"thread_ids": list(nuevos)}},
        upsert=True
    )
    update.message.reply_text(
        f"✅ El comando <b>/{comando}</b> funcionará en los temas: <code>{', '.join(str(t) for t in nuevos)}</code>",
        parse_mode='HTML'
    )

@grupo_oficial
def comando_removetema(update, context):
    user_id = update.message.from_user.id
    chat_id = update.effective_chat.id
    if not es_admin(update) and user_id != TU_USER_ID:
        update.message.reply_text("Solo un administrador puede configurar esto.")
        return
    if len(context.args) != 1:
        update.message.reply_text("Uso: /removetema <comando>")
        return
    comando = context.args[0]
    res = col_temas_comandos.delete_one({"chat_id": chat_id, "comando": comando})
    if res.deleted_count:
        update.message.reply_text(f"El comando <b>/{comando}</b> ahora puede usarse en cualquier tema.", parse_mode='HTML')
    else:
        update.message.reply_text("Ese comando no tenía restricción en este grupo.")

@grupo_oficial
def comando_vertemas(update, context):
    chat_id = update.effective_chat.id
    docs    = list(col_temas_comandos.find({"chat_id": chat_id}))
    if not docs:
        update.message.reply_text("No hay restricciones configuradas para este grupo.")
        return
    texto = "<b>Restricciones de comandos por tema:</b>\n\n"
    for d in docs:
        if "thread_ids" in d:
            threads = ", ".join(f"<code>{tid}</code>" for tid in d["thread_ids"])
        elif "thread_id" in d:
            threads = f"<code>{d['thread_id']}</code>"
        else:
            threads = "<i>No asignado</i>"
        texto += f"<b>/{d['comando']}</b>: {threads}\n"
    update.message.reply_text(texto, parse_mode='HTML')

# ─── /idolday ─────────────────────────────────────────────────────────────────
@log_command
@grupo_oficial
@solo_en_chat_general
def comando_idolday(update, context):
    if update.effective_chat.type not in ["group", "supergroup"]:
        update.message.reply_text("Este comando solo está disponible en el grupo oficial.")
        return

    user_id  = update.message.from_user.id
    chat_id  = update.effective_chat.id
    thread_id= getattr(update.message, "message_thread_id", None)
    ahora    = datetime.utcnow()
    ahora_ts = time.time()
    user_doc = col_usuarios.find_one({"user_id": user_id}) or {}
    last     = user_doc.get('last_idolday')

    # Cooldown global de grupo
    ultimo_drop = COOLDOWN_GRUPO.get(chat_id, 0)
    if ahora_ts - ultimo_drop < COOLDOWN_GRUPO_SEG:
        faltante = int(COOLDOWN_GRUPO_SEG - (ahora_ts - ultimo_drop))
        try:
            update.message.delete()
        except Exception:
            pass
        try:
            msg_cd = context.bot.send_message(
                chat_id=chat_id,
                text=f"⏳ Espera {faltante} segundos antes de volver a dropear.",
                message_thread_id=thread_id
            )
            threading.Timer(10, lambda m: context.bot.delete_message(chat_id, m.message_id), args=(msg_cd,)).start()
        except Exception:
            pass
        return

    cooldown_listo, bono_listo = puede_usar_idolday(user_id)

    if cooldown_listo:
        col_usuarios.update_one({"user_id": user_id}, {"$set": {"last_idolday": ahora}}, upsert=True)
        actualiza_mision_diaria(user_id, context)
        user_doc2 = col_usuarios.find_one({"user_id": user_id}) or {}
        last_ts   = user_doc2.get("last_idolday")
        if last_ts and user_doc2.get("notify_idolday"):
            restante = max(0, 6 * 3600 - (ahora_ts - (last_ts.timestamp() if hasattr(last_ts, "timestamp") else 0)))
            if restante > 0:
                agendar_notificacion_idolday(user_id, restante, context)

    elif bono_listo:
        objetos          = user_doc.get('objetos', {})
        bonos_inventario = objetos.get('bono_idolday', 0)
        if bonos_inventario and bonos_inventario > 0:
            col_usuarios.update_one({"user_id": user_id}, {"$inc": {"objetos.bono_idolday": -1}}, upsert=True)
        else:
            col_usuarios.update_one({"user_id": user_id}, {"$inc": {"bono": -1}}, upsert=True)
        actualiza_mision_diaria(user_id, context)
    else:
        try:
            update.message.delete()
        except Exception:
            pass
        if last:
            faltante = 6*3600 - (ahora - last).total_seconds()
            h = int(faltante // 3600); m = int((faltante % 3600) // 60); s = int(faltante % 60)
            txt = f"Ya usaste /idolday. Intenta de nuevo en {h}h {m}m {s}s."
        else:
            txt = "Ya usaste /idolday."
        try:
            msg_cd = context.bot.send_message(chat_id=chat_id, text=txt, message_thread_id=thread_id)
            threading.Timer(10, lambda m: context.bot.delete_message(chat_id, m.message_id), args=(msg_cd,)).start()
        except Exception:
            pass
        return

    COOLDOWN_GRUPO[chat_id] = ahora_ts

    cartas_excelentes = [c for c in cartas if c.get("estado") == "Excelente estado"]
    if len(cartas_excelentes) < 2:
        cartas_excelentes = cartas_excelentes * 2

    # Sin repetición: sample por (nombre, version) únicos
    pool = list({(c['nombre'], c['version']): c for c in cartas_excelentes}.values())
    cartas_drop = random.sample(pool, min(2, len(pool)))
    if len(cartas_drop) < 2:
        cartas_drop = random.choices(cartas_excelentes, k=2)

    media_group = []
    cartas_info = []

    # Preparar imágenes en paralelo (no bloquea el thread principal)
    def preparar_carta(carta):
        nombre     = carta['nombre']
        version    = carta['version']
        grupo      = carta.get('grupo', '')
        imagen_url = carta.get('imagen')
        doc_cont = col_contadores.find_one_and_update(
            {"nombre": nombre, "version": version, "grupo": grupo},
            {"$inc": {"contador": 1}},
            upsert=True,
            return_document=True
        )
        nuevo_id = doc_cont['contador'] if doc_cont else 1
        try:
            imagen_con_numero = agregar_numero_a_imagen(imagen_url, nuevo_id)
        except Exception as e:
            logger.warning(f"[drop] Imagen no disponible para {nombre}: {e}")
            imagen_con_numero = None
        return nombre, version, grupo, imagen_url, nuevo_id, imagen_con_numero

    with ThreadPoolExecutor(max_workers=2) as ex:
        resultados = list(ex.map(preparar_carta, cartas_drop))

    # Restaurar cooldown si ninguna imagen se pudo cargar
    if all(r[5] is None for r in resultados):
        col_usuarios.update_one({"user_id": user_id}, {"$unset": {"last_idolday": ""}})
        update.message.reply_text("⚠️ No se pudo cargar las imágenes del drop. Tu cooldown no fue consumido, intenta de nuevo.")
        return

    for nombre, version, grupo, imagen_url, nuevo_id, imagen_con_numero in resultados:
        caption = f"<b>{nombre}</b>\n{grupo} [{version}]"
        if imagen_con_numero:
            media_group.append(InputMediaPhoto(media=imagen_con_numero, caption=caption, parse_mode="HTML"))
        else:
            media_group.append(InputMediaPhoto(media=imagen_url, caption=f"{caption}\n<i>(#número no disponible)</i>", parse_mode="HTML"))
        cartas_info.append({
            "nombre": nombre, "version": version, "grupo": grupo,
            "imagen": imagen_url, "reclamada": False, "usuario": None,
            "hora_reclamada": None, "card_id": nuevo_id
        })

    context.bot.send_media_group(chat_id=chat_id, media=media_group, message_thread_id=thread_id)

    # Enviar texto primero sin botones para obtener message_id real, luego editar
    texto_drop  = f"@{update.effective_user.username or update.effective_user.first_name} está dropeando 2 cartas!\n<i>El dueño tiene 15s de prioridad.</i>"
    msg_botones = context.bot.send_message(
        chat_id=chat_id,
        text=texto_drop,
        parse_mode="HTML",
        message_thread_id=thread_id
    )
    botones_reclamar = [
        InlineKeyboardButton("1️⃣", callback_data=f"reclamar_{chat_id}_{msg_botones.message_id}_0"),
        InlineKeyboardButton("2️⃣", callback_data=f"reclamar_{chat_id}_{msg_botones.message_id}_1"),
    ]
    try:
        context.bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=msg_botones.message_id,
            reply_markup=InlineKeyboardMarkup([botones_reclamar])
        )
    except Exception as e:
        logger.warning(f"[drop] Error al agregar botones: {e}")

    drop_id   = crear_drop_id(chat_id, msg_botones.message_id)
    drop_data = {
        "cartas": cartas_info, "dueño": user_id,
        "chat_id": chat_id, "mensaje_id": msg_botones.message_id,
        "inicio": time.time(), "msg_botones": msg_botones,
        "usuarios_reclamaron": [], "expirado": False,
        "primer_reclamo_dueño": None,
        "thread_id": thread_id,
    }
    DROPS_ACTIVOS[drop_id] = drop_data

    col_usuarios.update_one(
        {"user_id": user_id},
        {"$set": {
            "last_idolday": ahora,
            "username": (update.effective_user.username.lower() if update.effective_user.username else "")
        }},
        upsert=True
    )
    threading.Thread(target=desbloquear_drop, args=(drop_id,), daemon=True).start()

FRASES_ESTADO = {
    "Excelente estado": "Genial!",
    "Buen estado":      "Nada mal.",
    "Mal estado":       "Podría estar mejor...",
    "Muy mal estado":   "¡Oh no!"
}

# ─── Dar objeto (admin) ───────────────────────────────────────────────────────
@log_command
def comando_darobjeto(update, context):
    # Funciona desde privado Y desde grupo — solo requiere ser admin o TU_USER_ID
    user_id = update.message.from_user.id
    chat    = update.effective_chat

    es_admin_global = user_id in ADMIN_IDS
    es_admin_grupo  = False
    if chat.type in ["group", "supergroup"]:
        try:
            member = context.bot.get_chat_member(chat.id, user_id)
            es_admin_grupo = member.status in ("administrator", "creator")
        except Exception:
            pass

    if not es_admin_global and not es_admin_grupo:
        update.message.reply_text("Solo los administradores pueden usar este comando.")
        return

    # Lista de objetos válidos para mostrar en errores
    lista_objetos = "\n".join(
        f"• <code>{k}</code> — {v['emoji']} {v['nombre']}"
        for k, v in CATALOGO_OBJETOS.items()
    )

    args = context.args
    dest_id = None; objeto = None; cantidad = None; nombre_dest = None

    if update.message.reply_to_message:
        dest_user   = update.message.reply_to_message.from_user
        dest_id     = dest_user.id
        nombre_dest = dest_user.full_name
        if len(args) != 2:
            update.message.reply_text(
                f"Uso (respondiendo): /darobjeto <objeto_id> <cantidad>\n\n"
                f"<b>Objetos válidos:</b>\n{lista_objetos}", parse_mode="HTML"
            )
            return
        objeto = args[0]
        try:
            cantidad = int(args[1])
        except Exception:
            update.message.reply_text("La cantidad debe ser un número.")
            return

    elif args and args[0].startswith("@"):
        username = args[0][1:].lower()
        user_doc = col_usuarios.find_one({"username": username})
        if not user_doc:
            update.message.reply_text(f"Usuario @{username} no encontrado. Debe haber usado el bot al menos una vez.")
            return
        dest_id     = user_doc["user_id"]
        nombre_dest = f"@{username}"
        if len(args) < 3:
            update.message.reply_text(
                f"Uso: /darobjeto @usuario <objeto_id> <cantidad>\n\n"
                f"<b>Objetos válidos:</b>\n{lista_objetos}", parse_mode="HTML"
            )
            return
        objeto = args[1]
        try:
            cantidad = int(args[2])
        except Exception:
            update.message.reply_text("La cantidad debe ser un número.")
            return

    elif len(args) == 3:
        try:
            dest_id     = int(args[0])
            objeto      = args[1]
            cantidad    = int(args[2])
            nombre_dest = f"<code>{dest_id}</code>"
        except Exception:
            update.message.reply_text(
                f"Uso: /darobjeto <user_id> <objeto_id> <cantidad>\n\n"
                f"<b>Objetos válidos:</b>\n{lista_objetos}", parse_mode="HTML"
            )
            return
    else:
        update.message.reply_text(
            f"<b>Uso de /darobjeto:</b>\n"
            f"• Respondiendo: <code>/darobjeto bono_idolday 2</code>\n"
            f"• Por username: <code>/darobjeto @usuario bono_idolday 2</code>\n"
            f"• Por ID: <code>/darobjeto 123456789 bono_idolday 2</code>\n\n"
            f"<b>Objetos válidos:</b>\n{lista_objetos}",
            parse_mode="HTML"
        )
        return

    if not objeto or not cantidad or cantidad < 1:
        update.message.reply_text("Objeto y cantidad válidos son requeridos.")
        return

    # Normalizar: aceptar con espacios o guiones además del id exacto
    if objeto not in CATALOGO_OBJETOS:
        objeto_norm = objeto.lower().replace(" ", "_").replace("-", "_")
        if objeto_norm not in CATALOGO_OBJETOS:
            update.message.reply_text(
                f"❌ Objeto <code>{objeto}</code> no válido.\n\n"
                f"<b>Objetos válidos:</b>\n{lista_objetos}", parse_mode="HTML"
            )
            return
        objeto = objeto_norm

    col_usuarios.update_one({"user_id": dest_id}, {"$inc": {f"objetos.{objeto}": cantidad}}, upsert=True)
    info_obj = CATALOGO_OBJETOS[objeto]
    update.message.reply_text(
        f"✅ {info_obj['emoji']} {cantidad} x {info_obj['nombre']} entregado(s) a {nombre_dest}.",
        parse_mode='HTML'
    )
    try:
        context.bot.send_message(
            chat_id=dest_id,
            text=f"🎁 Has recibido {info_obj['emoji']} {cantidad} x {info_obj['nombre']} por parte de un admin."
        )
    except Exception:
        pass

@solo_en_tema_asignado("chatid")
@grupo_oficial
def comando_chatid(update, context):
    update.message.reply_text(f"ID de este chat/grupo: <code>{update.effective_chat.id}</code>", parse_mode="HTML")

dispatcher.add_handler(CommandHandler('chatid', comando_chatid))

@solo_en_tema_asignado("topicid")
def comando_topicid(update, context):
    topic_id = getattr(update.message, "message_thread_id", None)
    update.message.reply_text(f"Thread ID de este tema: <code>{topic_id}</code>", parse_mode="HTML")

# ─── /kkp y notificaciones ────────────────────────────────────────────────────
@log_command
@en_tema_asignado_o_privado("kkp")
def comando_kkp(update, context):
    user_id = update.message.from_user.id
    texto, reply_markup, _ = get_kkp_menu(user_id, update)
    update.message.reply_text(texto, parse_mode="HTML", reply_markup=reply_markup)

def callback_kkp_notify(update, context):
    query   = update.callback_query
    user_id = query.from_user.id
    parts   = query.data.split("|")
    action  = parts[0]
    owner_id= int(parts[1]) if len(parts) > 1 else None

    if user_id != owner_id:
        query.answer("Solo puedes usar este botón desde tu propio menú /kkp.", show_alert=True)
        return

    toggled = None
    if action == "kkp_notify_on":
        col_usuarios.update_one({"user_id": user_id}, {"$set": {"notify_idolday": True}})
        toggled = True
    elif action == "kkp_notify_off":
        col_usuarios.update_one({"user_id": user_id}, {"$set": {"notify_idolday": False}})
        toggled = False

    textos = t(user_id, update)
    msg = (
        textos["kkp_notify_toggled_on"]  if toggled is True  else
        textos["kkp_notify_toggled_off"] if toggled is False else "❓"
    )
    query.answer(msg, show_alert=True)
    texto, reply_markup, restante = get_kkp_menu(user_id, update)
    try:
        query.edit_message_text(text=texto, parse_mode="HTML", reply_markup=reply_markup)
    except Exception:
        pass
    if toggled is True and restante > 0:
        agendar_notificacion_idolday(user_id, restante, context)

def agendar_notificacion_idolday(user_id, segundos, context):
    def tarea():
        try:
            time.sleep(max(0, min(segundos, 7*3600)))
            user_doc = col_usuarios.find_one({"user_id": user_id}) or {}
            if not user_doc.get("notify_idolday"):
                return
            last = user_doc.get("last_idolday")
            now  = time.time()
            last_ts = last.timestamp() if hasattr(last, "timestamp") else 0
            if now - last_ts < 6 * 3600 - 5:
                return
            lang   = (user_doc.get("lang") or "en")[:2]
            textos = translations.get(lang, translations["en"])
            context.bot.send_message(chat_id=user_id, text=textos["kkp_notify_sent"], parse_mode="HTML")
        except Exception as e:
            print("[agendar_notificacion_idolday] Error:", e)
    threading.Thread(target=tarea, daemon=True).start()

def get_kkp_menu(user_id, update):
    user_doc = col_usuarios.find_one({"user_id": user_id}) or {}
    misiones = user_doc.get("misiones", {})
    notif    = user_doc.get("notify_idolday", False)
    textos   = t(user_id, update)

    last_idolday = user_doc.get("last_idolday")
    if last_idolday:
        last_ts  = last_idolday.timestamp() if hasattr(last_idolday, "timestamp") else 0
        restante = max(0, 6 * 3600 - (time.time() - last_ts))
    else:
        restante = 0

    def fmt(s):
        h = int(s // 3600); m = int((s % 3600) // 60); ss = int(s % 60)
        return f"{h}h {m}m {ss}s" if h > 0 else (f"{m}m {ss}s" if m > 0 else f"{ss}s")

    hoy_str       = datetime.utcnow().strftime('%Y-%m-%d')
    idolday_hoy   = misiones.get("idolday_hoy", 0) if misiones.get("ultima_mision_idolday") == hoy_str else 0
    primer_drop_done = misiones.get("primer_drop", {}).get("fecha") == hoy_str

    ahora      = datetime.utcnow()
    reset_dt   = datetime.strptime(hoy_str, "%Y-%m-%d") + timedelta(days=1)
    falta_reset= max(0, (reset_dt - ahora).total_seconds())

    texto  = "<b>⏰ Recordatorio KaruKpop</b>\n"
    texto += f"🎲 <b>/idolday</b>: "
    texto += f"Disponible en <b>{fmt(restante)}</b>\n" if restante > 0 else "<b>¡Disponible ahora!</b>\n"
    texto += "📝 <b>Misiones diarias:</b>\n"
    texto += ("✔️ Primer drop del día: ✅ <b>¡Completada! (+50 Kponey)</b>\n"
              if primer_drop_done else
              "🔸 Primer drop del día: <b>Pendiente</b>\n")
    texto += f"🔹 3 drops hoy: <b>{idolday_hoy}</b>/3"
    texto += "  ✅ <b>¡Completada! (+150 Kponey)</b>\n" if idolday_hoy >= 3 else "\n"
    texto += f"⏳ Reset misiones en: <b>{fmt(falta_reset)}</b>\n\n"

    if notif:
        texto += textos["kkp_notify_on"]
        boton  = InlineKeyboardButton(textos["kkp_notify_disable"], callback_data=f"kkp_notify_off|{user_id}")
    else:
        texto += textos["kkp_notify_off"]
        boton  = InlineKeyboardButton(textos["kkp_notify_enable"], callback_data=f"kkp_notify_on|{user_id}")

    return texto, InlineKeyboardMarkup([[boton]]), restante

# ─── Estadísticas de drops ────────────────────────────────────────────────────
@solo_en_tema_asignado("estadisticasdrops")
@grupo_oficial
def comando_estadisticasdrops(update, context):
    if not es_admin(update, context):
        update.message.reply_text("Solo administradores.")
        return
    page = 0
    if context.args and context.args[0].isdigit():
        page = int(context.args[0])
    _enviar_estadisticas(update.message, page)

def _enviar_estadisticas(message, page):
    total_reclamados= col_drops_log.count_documents({"evento": "reclamado"})
    total_expirados = col_drops_log.count_documents({"evento": "expirado"})
    pipeline = [
        {"$match": {"evento": "reclamado"}},
        {"$group": {"_id": {"user_id": "$user_id", "username": "$username"}, "total": {"$sum": 1}}},
        {"$sort": {"total": -1}},
    ]
    all_results   = list(col_drops_log.aggregate(pipeline))
    total_usuarios= len(all_results)
    por_pagina = 10; inicio = page * por_pagina; fin = inicio + por_pagina
    resultados = all_results[inicio:fin]
    ranking_texto = ""
    for i, r in enumerate(resultados, inicio + 1):
        user     = r['_id']
        username = user.get('username')
        user_text= f"@{username}" if username else f"<code>{user['user_id']}</code>"
        ranking_texto += f"{i}. {user_text} — {r['total']} cartas\n"
    texto = (
        f"📊 <b>Estadísticas de Drops</b>:\n"
        f"• Drops reclamados: <b>{total_reclamados}</b>\n"
        f"• Drops expirados: <b>{total_expirados}</b>\n\n"
        f"<b>🏆 Ranking (del {inicio+1} al {min(fin, total_usuarios)} de {total_usuarios}):</b>\n"
        f"{ranking_texto or 'Sin datos.'}"
    )
    botones = []
    if page > 0:         botones.append(InlineKeyboardButton("⬅️", callback_data=f"estadrops_{page-1}"))
    if fin < total_usuarios: botones.append(InlineKeyboardButton("➡️", callback_data=f"estadrops_{page+1}"))
    reply_markup = InlineKeyboardMarkup([botones]) if botones else None
    message.reply_text(texto, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

def callback_estadrops(update, context):
    query = update.callback_query
    page  = int(query.data.split("_")[1])
    _enviar_estadisticas(query.message, page)
    query.answer()

dispatcher.add_handler(CallbackQueryHandler(callback_estadrops, pattern=r"^estadrops_\d+"))

def get_last_monday():
    hoy = datetime.utcnow()
    lm  = hoy - timedelta(days=hoy.weekday())
    return lm.replace(hour=0, minute=0, second=0, microsecond=0)

@solo_en_tema_asignado("estadisticasdrops_semanal")
@grupo_oficial
def comando_estadisticasdrops_semanal(update, context):
    if not es_admin(update, context):
        update.message.reply_text("Solo administradores.")
        return
    inicio_semana = get_last_monday()
    fin_semana    = inicio_semana + timedelta(days=7)
    total_r = col_drops_log.count_documents({"evento": "reclamado", "fecha": {"$gte": inicio_semana, "$lt": fin_semana}})
    total_e = col_drops_log.count_documents({"evento": "expirado",  "fecha": {"$gte": inicio_semana, "$lt": fin_semana}})
    pipeline = [
        {"$match": {"evento": "reclamado", "fecha": {"$gte": inicio_semana, "$lt": fin_semana}}},
        {"$group": {"_id": {"user_id": "$user_id", "username": "$username"}, "total": {"$sum": 1}}},
        {"$sort": {"total": -1}}, {"$limit": 10}
    ]
    resultados    = list(col_drops_log.aggregate(pipeline))
    ranking_texto = ""
    for i, r in enumerate(resultados, 1):
        user      = r['_id']
        username  = user.get('username')
        user_text = f"@{username}" if username else f"<code>{user['user_id']}</code>"
        ranking_texto += f"{i}. {user_text} — {r['total']} cartas\n"
    texto = (
        f"📅 <b>Estadísticas Semanales</b>\n"
        f"• Drops reclamados: <b>{total_r}</b>\n"
        f"• Drops expirados: <b>{total_e}</b>\n\n"
        f"<b>🏆 Top 10:</b>\n{ranking_texto or 'Sin datos.'}"
    )
    update.message.reply_text(texto, parse_mode=ParseMode.HTML)

# ─── Dar gemas / Dar Kponey (admin) ──────────────────────────────────────────
@grupo_oficial
def comando_darGemas(update, context):
    if update.message.from_user.id != TU_USER_ID:
        update.message.reply_text("Solo el creador puede usar esto.")
        return
    dest_id = None
    if update.message.reply_to_message:
        dest_id = update.message.reply_to_message.from_user.id
    elif context.args and context.args[0].startswith('@'):
        u = col_usuarios.find_one({"username": context.args[0][1:].lower()})
        if not u:
            update.message.reply_text("Usuario no encontrado.")
            return
        dest_id = u["user_id"]
    elif context.args:
        try:
            dest_id = int(context.args[0])
        except ValueError:
            update.message.reply_text("Uso: /darGemas <@usuario|user_id> <cantidad>")
            return
    else:
        update.message.reply_text("Debes especificar un usuario.")
        return

    try:
        cantidad = int(context.args[-1])
    except Exception:
        update.message.reply_text("Debes indicar la cantidad.")
        return

    col_usuarios.update_one({"user_id": dest_id}, {"$inc": {"gemas": cantidad}}, upsert=True)
    update.message.reply_text(f"💎 Gemas actualizadas para <code>{dest_id}</code> ({cantidad:+})", parse_mode="HTML")

# ─── /usar ────────────────────────────────────────────────────────────────────
@log_command
@solo_en_tema_asignado("usar")
@grupo_oficial
@cooldown_critico
def comando_usar(update, context):
    OBJETOS_USABLES = {
        "abrazo_de_bias": "abrazo_de_bias",
        "lightstick":     "lightstick",
        "abrazo de bias": "abrazo_de_bias",
        "light stick":    "lightstick",
    }
    user_id = update.message.from_user.id
    if not context.args:
        update.message.reply_text('Usa: /usar <objeto>')
        return
    obj_norm = " ".join(context.args).lower().replace("_", " ").replace('"', '').strip()
    obj_id   = OBJETOS_USABLES.get(obj_norm)
    if not obj_id:
        update.message.reply_text("No tienes ese objeto en tu inventario.")
        return
    doc     = col_usuarios.find_one({"user_id": user_id}) or {}
    objetos = doc.get("objetos", {})
    if objetos.get(obj_id, 0) < 1:
        update.message.reply_text("No tienes ese objeto en tu inventario.")
        return

    if obj_id == "abrazo_de_bias":
        last = doc.get('last_idolday')
        if not last:
            update.message.reply_text("No tienes cooldown activo de /idolday.")
            return
        ahora    = datetime.utcnow()
        faltante = 6 * 3600 - (ahora - last).total_seconds()
        if faltante <= 0:
            update.message.reply_text("No tienes cooldown activo de /idolday.")
            return
        nuevo_faltante = faltante / 2
        nuevo_last     = ahora - timedelta(seconds=(6 * 3600 - nuevo_faltante))
        col_usuarios.update_one(
            {"user_id": user_id},
            {"$set": {"last_idolday": nuevo_last}, "$inc": {f"objetos.{obj_id}": -1}}
        )
        def fmt(s):
            h=int(s//3600); m=int((s%3600)//60); ss=int(s%60)
            return " ".join(filter(None, [f"{h}h" if h else "", f"{m}m" if m else "", f"{ss}s"]))
        update.message.reply_text(
            f"🤗 <b>¡Usaste Abrazo de Bias!</b>\n"
            f"Antes: <b>{fmt(faltante)}</b> → Ahora: <b>{fmt(nuevo_faltante)}</b>",
            parse_mode="HTML"
        )
        return

    if obj_id == "lightstick":
        cartas_usuario  = list(col_cartas_usuario.find({"user_id": user_id}))
        cartas_mejorables = [c for c in cartas_usuario if c.get("estrellas", "") != "★★★"]
        if not cartas_mejorables:
            update.message.reply_text("No tienes cartas que puedas mejorar.")
            return
        mostrar_lista_mejorables(update, context, user_id, cartas_mejorables, pagina=1)
        return

# ─── Reclamar carta (con lock para evitar race condition) ─────────────────────
@grupo_oficial
def manejador_reclamar(update, context):
    query         = update.callback_query
    usuario_click = query.from_user.id
    data          = query.data
    partes        = data.split("_")
    if len(partes) != 4:
        query.answer()
        return
    _, chat_id, mensaje_id, idx = partes
    chat_id    = int(chat_id)
    mensaje_id = int(mensaje_id)
    carta_idx  = int(idx)
    drop_id    = crear_drop_id(chat_id, mensaje_id)

    drop = DROPS_ACTIVOS.get(drop_id)
    if not drop:
        mensaje_fecha = getattr(query.message, "date", None)
        if mensaje_fecha:
            secs = (datetime.utcnow() - mensaje_fecha.replace(tzinfo=None)).total_seconds()
            if secs < 60:
                query.answer("⏳ El drop aún se está inicializando. Intenta en unos segundos.", show_alert=True)
                return
        query.answer("Este drop ya expiró o no existe.", show_alert=True)
        return

    # ─── LOCK para evitar race condition ─────────────────────────────────────
    lock = get_drop_lock(drop_id)
    acquired = lock.acquire(blocking=False)
    if not acquired:
        query.answer("⏳ Procesando... intenta en un momento.", show_alert=True)
        return

    try:
        # Re-leer el drop DENTRO del lock (estado fresco)
        drop = DROPS_ACTIVOS.get(drop_id)
        if not drop or drop.get("expirado"):
            query.answer("Este drop ya expiró.", show_alert=True)
            return

        carta = drop["cartas"][carta_idx]
        if carta.get("reclamada"):
            query.answer("Esta carta ya fue reclamada.", show_alert=True)
            return

        # ─── Marcar INMEDIATAMENTE dentro del lock ────────────────────────
        carta["reclamada"]     = True
        carta["usuario"]       = usuario_click
        carta["hora_reclamada"]= time.time()
        # ─────────────────────────────────────────────────────────────────

    finally:
        lock.release()

    # A partir de aquí la carta está reservada, el resto puede ir fuera del lock
    ahora    = time.time()
    thread_id= drop.get("thread_id") or getattr(query.message, "message_thread_id", None)

    if "intentos" not in carta:
        carta["intentos"] = 0
    if usuario_click != drop["dueño"]:
        carta["intentos"] += 1

    user_doc         = col_usuarios.find_one({"user_id": usuario_click}) or {}
    objetos          = user_doc.get("objetos", {})
    bonos_inventario = objetos.get('bono_idolday', 0)
    bono_legacy      = user_doc.get('bono', 0)
    last             = user_doc.get('last_idolday')
    ahora_dt         = datetime.utcnow()
    cooldown_listo   = False
    bono_listo       = False

    if last:
        cooldown_listo = (ahora_dt - last).total_seconds() >= 6 * 3600
    else:
        cooldown_listo = True

    if (bonos_inventario and bonos_inventario > 0) or (bono_legacy and bono_legacy > 0):
        bono_listo = True

    puede_reclamar = False
    tiempo_desde_drop = ahora - drop["inicio"]

    if usuario_click == drop["dueño"]:
        primer_reclamo = drop.get("primer_reclamo_dueño")
        if primer_reclamo is None:
            puede_reclamar = True
            drop["primer_reclamo_dueño"] = ahora
        else:
            tiempo_faltante = 15 - (ahora - drop["primer_reclamo_dueño"])
            if tiempo_faltante > 0:
                # Revertir reserva
                carta["reclamada"] = False; carta["usuario"] = None
                query.answer(f"Te quedan {int(round(tiempo_faltante))} segundos para reclamar la otra.", show_alert=True)
                return
            if cooldown_listo:
                puede_reclamar = True
                col_usuarios.update_one({"user_id": usuario_click}, {"$set": {"last_idolday": ahora_dt}}, upsert=True)
            elif bono_listo:
                puede_reclamar = True
                if bonos_inventario and bonos_inventario > 0:
                    col_usuarios.update_one({"user_id": usuario_click}, {"$inc": {"objetos.bono_idolday": -1}}, upsert=True)
                else:
                    col_usuarios.update_one({"user_id": usuario_click}, {"$inc": {"bono": -1}}, upsert=True)
            else:
                carta["reclamada"] = False; carta["usuario"] = None
                if last:
                    f = 6*3600 - (ahora_dt - last).total_seconds()
                    query.answer(f"No puedes reclamar: espera {int(f//3600)}h {int((f%3600)//60)}m {int(f%60)}s.", show_alert=True)
                else:
                    query.answer("No puedes reclamar. Espera el cooldown.", show_alert=True)
                return
    else:
        if tiempo_desde_drop < 15:
            carta["reclamada"] = False; carta["usuario"] = None
            query.answer(f"Aún no puedes reclamar. Te quedan {int(round(15 - tiempo_desde_drop))} segundos.", show_alert=True)
            return
        if cooldown_listo:
            puede_reclamar = True
            col_usuarios.update_one({"user_id": usuario_click}, {"$set": {"last_idolday": ahora_dt}}, upsert=True)
        elif bono_listo:
            puede_reclamar = True
            if bonos_inventario and bonos_inventario > 0:
                col_usuarios.update_one({"user_id": usuario_click}, {"$inc": {"objetos.bono_idolday": -1}}, upsert=True)
            else:
                col_usuarios.update_one({"user_id": usuario_click}, {"$inc": {"bono": -1}}, upsert=True)
        else:
            carta["reclamada"] = False; carta["usuario"] = None
            if last:
                f = 6*3600 - (ahora_dt - last).total_seconds()
                query.answer(f"No puedes reclamar: espera {int(f//3600)}h {int((f%3600)//60)}m {int(f%60)}s.", show_alert=True)
            else:
                query.answer("No puedes reclamar. Espera el cooldown.", show_alert=True)
            return

    if not puede_reclamar:
        carta["reclamada"] = False; carta["usuario"] = None
        return

    # ─── Actualizar botones ───────────────────────────────────────────────────
    teclado = []
    for i, c in enumerate(drop["cartas"]):
        if c.get("reclamada"):
            teclado.append(InlineKeyboardButton("❌", callback_data="reclamada"))
        else:
            teclado.append(InlineKeyboardButton(f"{i+1}️⃣", callback_data=f"reclamar_{chat_id}_{mensaje_id}_{i}"))
    try:
        context.bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=mensaje_id,
            reply_markup=InlineKeyboardMarkup([teclado])
        )
    except Exception as e:
        if "Message is not modified" not in str(e):
            print("[manejador_reclamar] Error botones:", e)

    # ─── Entregar carta ───────────────────────────────────────────────────────
    nombre    = carta['nombre']
    version   = carta['version']
    grupo     = carta['grupo']
    nuevo_id  = carta.get("card_id", 1)
    id_unico  = random_id_unico(nuevo_id)

    posibles_estados = [
        c for c in estados_disponibles_para_carta(nombre, version)
        if c.get("grupo") == grupo
    ]
    if not posibles_estados:
        posibles_estados = estados_disponibles_para_carta(nombre, version)
    carta_entregada = random.choice(posibles_estados)
    estado   = carta_entregada['estado']
    estrellas= carta_entregada.get('estado_estrella', '★??')
    imagen_url = carta_entregada['imagen']
    intentos = carta.get("intentos", 0)
    precio   = precio_carta_karuta(nombre, version, estado, id_unico=id_unico, card_id=nuevo_id) + 200 * max(0, intentos - 1)

    existente = col_cartas_usuario.find_one({
        "user_id": usuario_click, "nombre": nombre,
        "version": version, "card_id": nuevo_id, "estado": estado,
    })
    if existente:
        col_cartas_usuario.update_one(
            {"user_id": usuario_click, "nombre": nombre, "version": version, "card_id": nuevo_id, "estado": estado},
            {"$inc": {"count": 1}}
        )
    else:
        col_cartas_usuario.insert_one({
            "user_id": usuario_click, "nombre": nombre, "version": version,
            "grupo": grupo, "estado": estado, "estrellas": estrellas,
            "imagen": imagen_url, "card_id": nuevo_id, "count": 1,
            "id_unico": id_unico, "estado_estrella": estrellas.count("★"),
        })

    revisar_sets_completados(usuario_click, context)
    drop.setdefault("usuarios_reclamaron", []).append(usuario_click)

    try:
        col_drops_log.insert_one({
            "evento": "reclamado", "drop_id": drop_id,
            "user_id": usuario_click,
            "username": query.from_user.username if hasattr(query.from_user, "username") else "",
            "nombre": carta['nombre'], "version": carta['version'],
            "grupo": carta.get('grupo', ''), "card_id": carta.get("card_id"),
            "estado": estado, "estrellas": estrellas,
            "fecha": datetime.utcnow(), "intentos": carta.get("intentos", 0),
            "chat_id": chat_id, "mensaje_id": mensaje_id,
        })
    except Exception:
        pass

    DROPS_ACTIVOS[drop_id] = drop

    user_mention  = f"@{query.from_user.username or query.from_user.first_name}"
    frase_estado  = FRASES_ESTADO.get(estado, "")
    mensaje_extra = ""
    intentos_otros = max(0, intentos - 1)
    if intentos_otros > 0:
        mensaje_extra = f"\n💸 Esta carta fue disputada con <b>{intentos_otros}</b> intentos."

    context.bot.send_message(
        chat_id=drop["chat_id"],
        text=(
            f"{user_mention} tomaste la carta <code>{id_unico}</code> #{nuevo_id} "
            f"[{version}] {nombre} - {grupo}, {frase_estado} está en <b>{estado.lower()}</b>!\n"
            f"{mensaje_extra}"
        ),
        parse_mode='HTML',
        message_thread_id=thread_id
    )

    favoritos = []
    for user in col_usuarios.find({}):
        for fav in user.get("favoritos", []):
            if (
                fav.get("nombre", "").lower() == nombre.lower() and
                fav.get("version", "").lower() == version.lower() and
                fav.get("grupo", "").lower() == grupo.lower()
            ):
                favoritos.append(user)
                break

    if favoritos:
        nombres = [
            f"⭐ @{u.get('username', 'SinUser')}" if u.get("username") else f"⭐ ID:{u['user_id']}"
            for u in favoritos
        ]
        context.bot.send_message(
            chat_id=drop["chat_id"],
            text="👀 <b>Favoritos de esta carta:</b>\n" + "\n".join(nombres),
            parse_mode='HTML',
            message_thread_id=thread_id
        )

    query.answer("¡Carta reclamada!", show_alert=True)

def gastar_gemas(user_id, cantidad):
    doc   = col_usuarios.find_one({"user_id": user_id}) or {}
    gemas = doc.get("gemas", 0)
    if gemas < cantidad:
        return False
    col_usuarios.update_one({"user_id": user_id}, {"$inc": {"gemas": -cantidad}})
    return True

# ─── Album 2 (collage con descarga paralela) ──────────────────────────────────
def descargar_imagen_url(url, path):
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
        return True
    except Exception as e:
        print(f"[album2] Error descargando {url}: {e}")
        return False

def crear_cuadricula_cartas_urls(urls, output_path="cuadricula_album2.png"):
    from math import ceil
    imgs = []
    temp_files = []

    # ─── Descarga paralela de imágenes ───────────────────────────────────────
    temp_paths = [f"temp_album2_{i}.png" for i in range(len(urls))]
    with ThreadPoolExecutor(max_workers=5) as executor:
        resultados = list(executor.map(lambda args: descargar_imagen_url(*args), zip(urls, temp_paths)))

    for i, (ok, path) in enumerate(zip(resultados, temp_paths)):
        if ok:
            try:
                imgs.append(Image.open(path).convert("RGBA"))
                temp_files.append(path)
            except Exception:
                pass
    # ─────────────────────────────────────────────────────────────────────────

    if not imgs:
        raise ValueError("No se pudo descargar ninguna imagen.")
    ancho, alto  = imgs[0].size
    columnas     = 5
    filas        = ceil(len(imgs) / columnas)
    canvas       = Image.new("RGBA", (ancho * columnas, alto * filas), (255,255,255,0))
    for idx, img in enumerate(imgs):
        canvas.paste(img, ((idx % columnas) * ancho, (idx // columnas) * alto), img)
    canvas.save(output_path)
    for path in temp_files:
        try: os.remove(path)
        except Exception: pass
    return output_path

def mostrar_menu_grupos_album2(user_id, pagina):
    grupos  = sorted({c.get("grupo", "") for c in col_cartas_usuario.find({"user_id": user_id}) if c.get("grupo")})
    botones = []
    for grupo in grupos:
        grupo_cod = urllib.parse.quote(grupo)
        botones.append([InlineKeyboardButton(grupo, callback_data=f"album2_filtragrupo_{user_id}_{grupo_cod}")])
    return InlineKeyboardMarkup(botones)

@log_command
@solo_en_temas_permitidos("album2")
@cooldown_critico
def comando_album2(update, context):
    user_id  = update.message.from_user.id
    chat_id  = update.effective_chat.id
    thread_id= getattr(update.message, "message_thread_id", None)
    pagina   = 1
    grupo    = None
    if context.args:
        for arg in context.args:
            if arg.isdigit(): pagina = int(arg)
            else:             grupo  = arg
    mostrar_album2_uno(context.bot, chat_id, user_id, pagina, grupo=grupo, thread_id=thread_id)

def mostrar_album2_uno(bot, chat_id, user_id, pagina=1, grupo=None, thread_id=None, mensaje=None, editar=False):
    cartas_usuario = list(col_cartas_usuario.find({"user_id": user_id}))
    if grupo:
        g = grupo.lower().strip()
        cartas_usuario = [c for c in cartas_usuario if c.get("grupo", "").lower().strip() == g]
    cartas_usuario.sort(key=lambda c: (c.get('grupo', ''), c.get('nombre', '')))
    total     = len(cartas_usuario)
    por_pagina= 10
    paginas   = (total - 1) // por_pagina + 1 if total > 0 else 1
    pagina    = max(1, min(pagina, paginas))
    inicio    = (pagina - 1) * por_pagina
    cartas_pag= cartas_usuario[inicio:min(inicio + por_pagina, total)]

    if not cartas_usuario:
        bot.send_message(chat_id, "No tienes cartas en tu álbum.", message_thread_id=thread_id)
        return

    botones = []
    botones.append([InlineKeyboardButton("👥 Filtrar por Grupo", callback_data=f"album2_filtrosgrupo_{user_id}_{pagina}")])
    pag_buttons = []
    if pagina > 1:   pag_buttons.append(InlineKeyboardButton("⬅️", callback_data=f"album2_{pagina-1}_{urllib.parse.quote(grupo) if grupo else 'none'}"))
    if pagina < paginas: pag_buttons.append(InlineKeyboardButton("➡️", callback_data=f"album2_{pagina+1}_{urllib.parse.quote(grupo) if grupo else 'none'}"))
    if pag_buttons:  botones.append(pag_buttons)

    botones_cartas = [
        InlineKeyboardButton(c['nombre'], callback_data=f"ampliar_{c['id_unico']}")
        for c in cartas_pag
    ]
    filas_cartas = [botones_cartas[i:i+5] for i in range(0, len(botones_cartas), 5)]
    teclado      = InlineKeyboardMarkup(filas_cartas + botones)

    urls_imgs = [c['imagen'] for c in cartas_pag if c.get('imagen')]
    if not urls_imgs:
        bot.send_message(chat_id, "No se encontraron imágenes en esta página.", message_thread_id=thread_id)
        return
    img_path = crear_cuadricula_cartas_urls(urls_imgs, output_path=f"cuadricula_album2_{user_id}.png")
    caption  = f"🖼️ <b>Selecciona una carta</b> (página {pagina}/{paginas})"

    if editar and mensaje:
        with open(img_path, "rb") as f:
            mensaje.edit_media(media=InputMediaPhoto(f, caption=caption, parse_mode="HTML"), reply_markup=teclado)
    else:
        with open(img_path, "rb") as f:
            bot.send_photo(chat_id=chat_id, photo=f, caption=caption, parse_mode="HTML",
                           reply_markup=teclado, message_thread_id=thread_id)

def callback_album2_handler(update, context):
    query     = update.callback_query
    data      = query.data
    user_id   = query.from_user.id
    chat_id   = query.message.chat_id
    thread_id = getattr(query.message, "message_thread_id", None)
    partes    = data.split("_")

    if data.startswith("album2_filtrosgrupo_"):
        pagina  = int(partes[3]) if len(partes) > 3 else 1
        teclado = mostrar_menu_grupos_album2(user_id, pagina)
        query.message.edit_reply_markup(reply_markup=teclado)
        query.answer(); return

    elif data.startswith("album2_filtragrupo_"):
        if len(partes) < 4:
            query.answer("Error.", show_alert=True); return
        user_id_cb = int(partes[2])
        grupo = urllib.parse.unquote(partes[3])
        mostrar_album2_uno(context.bot, chat_id, user_id_cb, 1, grupo=grupo,
                           thread_id=thread_id, mensaje=query.message, editar=True)
        query.answer(); return

    elif data.startswith("album2_"):
        if len(partes) < 3:
            query.answer("Error.", show_alert=True); return
        pagina    = int(partes[1])
        grupo_cod = partes[2]
        grupo     = None if grupo_cod == "none" else urllib.parse.unquote(grupo_cod)
        mostrar_album2_uno(context.bot, chat_id, user_id, pagina, grupo=grupo,
                           thread_id=thread_id, mensaje=query.message, editar=True)
        query.answer(); return

    elif data.startswith("ampliar_"):
        id_unico = data.replace("ampliar_", "")
        comando_ampliar(update, context, id_unico)
        query.answer(); return

dispatcher.add_handler(CallbackQueryHandler(callback_album2_handler, pattern="^(ampliar_|album2_)"))

# ─── Album ────────────────────────────────────────────────────────────────────
@log_command
@solo_en_temas_permitidos("album")
@cooldown_critico
def comando_album(update, context):
    user_id  = update.effective_user.id
    chat_id  = update.effective_chat.id
    thread_id= getattr(update.message, "message_thread_id", None)
    msg = context.bot.send_message(chat_id=chat_id, text="Cargando álbum...", message_thread_id=thread_id)
    mostrar_album_pagina(update, context, chat_id, msg.message_id, user_id, pagina=1)

def mostrar_menu_estrellas_album(user_id, pagina):
    botones = [
        [InlineKeyboardButton("★★★", callback_data=f"album_filtraestrella_{user_id}_{pagina}_★★★")],
        [InlineKeyboardButton("★★☆", callback_data=f"album_filtraestrella_{user_id}_{pagina}_★★☆")],
        [InlineKeyboardButton("★☆☆", callback_data=f"album_filtraestrella_{user_id}_{pagina}_★☆☆")],
        [InlineKeyboardButton("☆☆☆", callback_data=f"album_filtraestrella_{user_id}_{pagina}_☆☆☆")],
        [InlineKeyboardButton("⬅️ Volver", callback_data=f"album_filtros_{user_id}_{pagina}")]
    ]
    return InlineKeyboardMarkup(botones)

def mostrar_menu_grupos_album(user_id, pagina, grupos):
    por_pagina = 5; total = len(grupos)
    paginas    = max(1, (total - 1) // por_pagina + 1)
    pagina     = max(1, min(pagina, paginas))
    inicio     = (pagina - 1) * por_pagina
    grupos_pag = grupos[inicio:min(inicio + por_pagina, total)]
    matriz     = [[InlineKeyboardButton(g, callback_data=f"album_filtragrupo_{user_id}_{pagina}_{g}")] for g in grupos_pag]
    nav = []
    if pagina > 1:      nav.append(InlineKeyboardButton("⬅️", callback_data=f"album_filtro_grupo_{user_id}_{pagina-1}"))
    if pagina < paginas:nav.append(InlineKeyboardButton("➡️", callback_data=f"album_filtro_grupo_{user_id}_{pagina+1}"))
    if nav: matriz.append(nav)
    matriz.append([InlineKeyboardButton("⬅️ Volver", callback_data=f"album_filtros_{user_id}_{pagina}")])
    return InlineKeyboardMarkup(matriz)

def mostrar_menu_filtros_album(user_id, pagina):
    botones = [
        [InlineKeyboardButton("⭐ Filtrar por Estado",  callback_data=f"album_filtro_estado_{user_id}_{pagina}")],
        [InlineKeyboardButton("👥 Filtrar por Grupo",   callback_data=f"album_filtro_grupo_{user_id}_1")],
        [InlineKeyboardButton("🔢 Ordenar por Número",  callback_data=f"album_filtro_numero_{user_id}_{pagina}")],
        [InlineKeyboardButton("⬅️ Volver",              callback_data=f"album_pagina_{user_id}_{pagina}_none_none_none")]
    ]
    return InlineKeyboardMarkup(botones)

def mostrar_menu_ordenar_album(user_id, pagina):
    botones = [
        [InlineKeyboardButton("⬆️ Menor a mayor", callback_data=f"album_ordennum_{user_id}_{pagina}_menor")],
        [InlineKeyboardButton("⬇️ Mayor a menor", callback_data=f"album_ordennum_{user_id}_{pagina}_mayor")],
        [InlineKeyboardButton("⬅️ Volver",        callback_data=f"album_filtros_{user_id}_{pagina}")]
    ]
    return InlineKeyboardMarkup(botones)

def mostrar_lista_mejorables(update, context, user_id, cartas_mejorables, pagina, mensaje=None, editar=False):
    por_pagina = 8; total = len(cartas_mejorables)
    paginas    = max(1, (total - 1) // por_pagina + 1)
    pagina     = max(1, min(pagina, paginas))
    inicio     = (pagina - 1) * por_pagina
    cartas_pag = cartas_mejorables[inicio:min(inicio + por_pagina, total)]
    texto      = "<b>Elige la carta que quieres mejorar:</b>\n"
    botones    = []
    for c in cartas_pag:
        nombre   = c.get("nombre", ""); version = c.get("version", "")
        estrellas= c.get("estrellas", ""); id_unico = c.get("id_unico", "")
        texto   += f"{estrellas} <b>{nombre}</b> [{version}] (<code>{id_unico}</code>)\n"
        botones.append([InlineKeyboardButton(f"{estrellas} {nombre} [{version}]", callback_data=f"mejorar_{id_unico}")])
    nav = []
    if pagina > 1:      nav.append(InlineKeyboardButton("⬅️", callback_data=f"mejorarpag_{pagina-1}_{user_id}"))
    if pagina < paginas:nav.append(InlineKeyboardButton("➡️", callback_data=f"mejorarpag_{pagina+1}_{user_id}"))
    if nav: botones.append(nav)
    teclado = InlineKeyboardMarkup(botones)
    if editar and mensaje:
        try:
            mensaje.edit_text(texto, parse_mode='HTML', reply_markup=teclado)
        except Exception:
            context.bot.send_message(chat_id=mensaje.chat_id, text=texto, parse_mode='HTML', reply_markup=teclado)
    else:
        update.message.reply_text(texto, parse_mode='HTML', reply_markup=teclado)

def inline_album_handler(update, context):
    query      = update.inline_query
    user_id    = query.from_user.id
    first_name = query.from_user.first_name or "Usuario"
    PER_PAGE   = 50
    offset     = int(query.offset) if query.offset else 0
    texto      = query.query.strip()
    partes     = texto.split(maxsplit=1)
    filtro     = partes[1].strip() if len(partes) > 1 else None
    mongo_query= {"user_id": user_id}
    if filtro:
        mongo_query["$or"] = [
            {"nombre": {"$regex": filtro, "$options": "i"}},
            {"grupo":  {"$regex": filtro, "$options": "i"}},
        ]
    total_cartas = col_cartas_usuario.count_documents(mongo_query)
    cartas_list  = list(col_cartas_usuario.find(mongo_query).sort([("grupo", 1), ("nombre", 1)]).skip(offset).limit(PER_PAGE))
    results = []
    for carta in cartas_list:
        nombre    = carta['nombre']
        estrellas = carta.get('estrellas', '')
        grupo     = carta.get('grupo', '')
        version   = carta.get('version', '')
        card_id   = carta.get('card_id', '')
        precio    = precio_carta_tabla(estrellas, card_id)
        copias    = col_cartas_usuario.count_documents({"nombre": nombre, "version": version, "grupo": grupo})
        caption   = (
            f"🎴 <b>Info de carta</b> <code>{carta['id_unico']}</code>\n"
            f"• Nombre: <b>{nombre}</b>\n• Grupo: <b>{grupo}</b>\n"
            f"• Versión: <b>{version}</b>\n• Nº: <b>#{card_id}</b>\n"
            f"• Estado: <b>{estrellas}</b>\n• Precio: <code>{precio} Kponey</code>\n"
            f"• Copias globales: <b>{copias}</b>\n<i>Carta de {first_name}</i>"
        )
        results.append(InlineQueryResultPhoto(
            id=carta['id_unico'], photo_url=carta['imagen'], thumb_url=carta['imagen'],
            title=f"{nombre} {estrellas}", caption=caption, parse_mode="HTML",
        ))
    next_offset = str(offset + PER_PAGE) if (offset + PER_PAGE) < total_cartas else ""
    try:
        update.inline_query.answer(results, cache_time=0, is_personal=True, next_offset=next_offset,
                                   switch_pm_text=f"Álbum de {first_name}", switch_pm_parameter="start")
    except BadRequest as e:
        print(f"Inline query error: {e}")

dispatcher.add_handler(InlineQueryHandler(inline_album_handler, pattern=r"^(Album|album)( |$)"))

def mostrar_album_pagina(update, context, chat_id, message_id, user_id, pagina=1,
                         filtro=None, valor_filtro=None, orden=None, solo_botones=False, thread_id=None):
    query_album = {"user_id": user_id}
    if filtro == "estrellas": query_album["estrellas"] = valor_filtro
    elif filtro == "grupo":   query_album["grupo"]     = valor_filtro

    cartas_list = list(col_cartas_usuario.find(query_album))
    if orden == "menor":    cartas_list.sort(key=lambda x: x.get("card_id", 0))
    elif orden == "mayor":  cartas_list.sort(key=lambda x: -x.get("card_id", 0))
    else:
        cartas_list.sort(key=lambda x: (x.get("grupo", "").lower(), x.get("nombre", "").lower(), x.get("card_id", 0)))

    por_pagina  = 10
    total_pag   = max(1, ((len(cartas_list) - 1) // por_pagina) + 1)
    pagina      = max(1, min(pagina, total_pag))
    inicio      = (pagina - 1) * por_pagina
    cartas_pag  = cartas_list[inicio:inicio + por_pagina]

    texto = f"📗 <b>Álbum de cartas (página {pagina}/{total_pag})</b>\n\n"
    if cartas_pag:
        for c in cartas_pag:
            idu = str(c['id_unico']).ljust(5)
            est = f"[{c.get('estrellas','?')}]".ljust(5)
            texto += f"• <code>{idu}</code> · {est} · #{c.get('card_id','?')} · [{c.get('version','?')}] · {c.get('nombre','?')} · {c.get('grupo','?')}\n"
    else:
        texto += "\n(No tienes cartas para mostrar con este filtro)\n"
    texto += '\n<i>Usa <b>/ampliar &lt;id_unico&gt;</b> para ver detalles.</i>'

    botones = []
    if not solo_botones:
        botones.append([telegram.InlineKeyboardButton("🔎 Filtrar / Ordenar", callback_data=f"album_filtros_{user_id}_{pagina}")])
    paginacion = []
    if pagina > 1:       paginacion.append(telegram.InlineKeyboardButton("⬅️", callback_data=f"album_pagina_{user_id}_{pagina-1}_{filtro or 'none'}_{valor_filtro or 'none'}_{orden or 'none'}"))
    if pagina < total_pag:paginacion.append(telegram.InlineKeyboardButton("➡️", callback_data=f"album_pagina_{user_id}_{pagina+1}_{filtro or 'none'}_{valor_filtro or 'none'}_{orden or 'none'}"))
    if paginacion and not solo_botones: botones.append(paginacion)
    teclado = telegram.InlineKeyboardMarkup(botones) if botones else None

    if solo_botones:
        try:
            context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=teclado)
        except RetryAfter as e:
            if update and hasattr(update, 'callback_query'):
                try: update.callback_query.answer(f"⏳ Espera {int(e.retry_after)}s.", show_alert=True)
                except Exception: pass
        return

    try:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=texto, reply_markup=teclado, parse_mode="HTML")
    except RetryAfter as e:
        if update and hasattr(update, 'callback_query'):
            try: update.callback_query.answer(f"⏳ Espera {int(e.retry_after)}s.", show_alert=True)
            except Exception: pass
    except telegram.error.TimedOut:
        logger.warning("[album] Timeout al editar — probablemente se aplicó igual.")
    except telegram.error.NetworkError as ex:
        logger.warning(f"[album] NetworkError: {ex}")
    except Exception as ex:
        logger.error(f"[album] Error al editar: {ex}")

# ─── Callback álbum (única definición) ───────────────────────────────────────
def manejador_callback_album(update, context):
    from telegram.error import RetryAfter, TimedOut, NetworkError

    query   = update.callback_query
    data    = query.data
    partes  = data.split("_")
    user_id = query.from_user.id

    def obtener_thread_id():
        if len(partes) > 0 and partes[-1].isdigit():
            return int(partes[-1])
        return getattr(query.message, "message_thread_id", None)

    def safe_answer(msg="✅"):
        try: query.answer(msg)
        except Exception: pass

    def handle_telegram_error(e):
        """Maneja errores de red de Telegram de forma silenciosa o con aviso."""
        if isinstance(e, RetryAfter):
            try: query.answer(f"⏳ Demasiadas solicitudes. Espera {int(e.retry_after)}s.", show_alert=True)
            except Exception: pass
        elif isinstance(e, (TimedOut, NetworkError)):
            # Timeout de red: la operación probablemente sí se completó en Telegram.
            # No mostramos error al usuario, solo logueamos.
            logger.warning(f"[album callback] Timeout/NetworkError ignorado: {e}")
        else:
            logger.error(f"[album callback] Error inesperado: {e}")

    # Validar dueño
    try:
        dueño_id = next((int(p) for p in partes if p.isdigit() and len(p) >= 5), None)
    except Exception:
        dueño_id = None
    if dueño_id and user_id != dueño_id:
        query.answer("Solo puedes interactuar con tu propio álbum.", show_alert=True)
        return

    if data.startswith("album_filtro_estado_"):
        uid = int(partes[3]); pag = int(partes[4])
        try:
            context.bot.edit_message_reply_markup(
                chat_id=query.message.chat_id, message_id=query.message.message_id,
                reply_markup=mostrar_menu_estrellas_album(uid, pag)
            )
        except Exception as e: handle_telegram_error(e)
        safe_answer(); return

    if data.startswith("album_filtraestrella_"):
        uid = int(partes[2]); pag = int(partes[3]); est = partes[4]
        try:
            mostrar_album_pagina(update, context, query.message.chat_id, query.message.message_id,
                                 uid, int(pag), filtro="estrellas", valor_filtro=est)
        except Exception as e: handle_telegram_error(e)
        safe_answer(); return

    if data.startswith("album_filtro_grupo_"):
        uid = int(partes[3]); pag = int(partes[4]) if len(partes) > 4 else 1
        grupos = sorted({c.get("grupo", "") for c in col_cartas_usuario.find({"user_id": uid}) if c.get("grupo")})
        try:
            context.bot.edit_message_reply_markup(
                chat_id=query.message.chat_id, message_id=query.message.message_id,
                reply_markup=mostrar_menu_grupos_album(uid, pag, grupos)
            )
        except Exception as e: handle_telegram_error(e)
        safe_answer(); return

    if data.startswith("album_filtragrupo_"):
        uid = int(partes[2]); pag = int(partes[3])
        grupo = "_".join(partes[4:])
        try:
            mostrar_album_pagina(update, context, query.message.chat_id, query.message.message_id,
                                 uid, int(pag), filtro="grupo", valor_filtro=grupo)
        except Exception as e: handle_telegram_error(e)
        safe_answer(); return

    if data.startswith("album_filtros_"):
        uid = int(partes[2]); pag = int(partes[3])
        try:
            context.bot.edit_message_reply_markup(
                chat_id=query.message.chat_id, message_id=query.message.message_id,
                reply_markup=mostrar_menu_filtros_album(uid, pag)
            )
        except Exception as e: handle_telegram_error(e)
        safe_answer(); return

    if data.startswith("album_filtro_numero_"):
        uid = int(partes[3]); pag = int(partes[4])
        try:
            context.bot.edit_message_reply_markup(
                chat_id=query.message.chat_id, message_id=query.message.message_id,
                reply_markup=mostrar_menu_ordenar_album(uid, pag)
            )
        except Exception as e: handle_telegram_error(e)
        safe_answer(); return

    if data.startswith("album_ordennum_"):
        uid = int(partes[2]); pag = int(partes[3]); orden = partes[4]
        try:
            mostrar_album_pagina(update, context, query.message.chat_id, query.message.message_id,
                                 uid, int(pag), orden=orden)
        except Exception as e: handle_telegram_error(e)
        safe_answer(); return

    if data.startswith("album_pagina_"):
        uid          = int(partes[2]); pag = int(partes[3])
        filtro       = partes[4] if len(partes) > 4 and partes[4] != "none" else None
        valor_filtro = partes[5] if len(partes) > 5 and partes[5] != "none" else None
        orden        = partes[6] if len(partes) > 6 and partes[6] != "none" else None
        try:
            mostrar_album_pagina(update, context, query.message.chat_id, query.message.message_id,
                                 uid, int(pag), filtro=filtro, valor_filtro=valor_filtro, orden=orden)
        except Exception as e: handle_telegram_error(e)
        safe_answer(); return

    # Paginación mejorar
    if data.startswith("mejorarpag_"):
        pag = int(partes[1]); uid = int(partes[2])
        if query.from_user.id != uid:
            query.answer("Solo puedes ver tu propio menú.", show_alert=True); return
        cartas_usuario  = list(col_cartas_usuario.find({"user_id": uid}))
        cartas_mejorables = sorted(
            [c for c in cartas_usuario if c.get("estrellas", "") != "★★★"],
            key=lambda x: (x.get("nombre", "").lower(), x.get("version", "").lower())
        )
        mostrar_lista_mejorables(update, context, uid, cartas_mejorables, pag, mensaje=query.message, editar=True)
        query.answer(); return

# ─── Trades ───────────────────────────────────────────────────────────────────
@log_command
@solo_en_tema_asignado("trk")
@cooldown_critico
def comando_trk(update, context):
    user_id  = update.message.from_user.id
    chat_id  = update.effective_chat.id
    thread_id= getattr(update.message, "message_thread_id", None)

    if update.message.reply_to_message:
        otro_id = update.message.reply_to_message.from_user.id
    elif context.args and context.args[0].startswith("@"):
        user_doc = col_usuarios.find_one({"username": context.args[0][1:].lower()})
        if not user_doc:
            update.message.reply_text("Usuario no encontrado.")
            return
        otro_id = user_doc["user_id"]
    else:
        update.message.reply_text("Debes responder a un usuario o indicar su @username.")
        return

    if otro_id == user_id:
        update.message.reply_text("No puedes hacer trade contigo mismo.")
        return
    if user_id in TRADES_POR_USUARIO or otro_id in TRADES_POR_USUARIO:
        update.message.reply_text("Uno de los dos ya tiene un intercambio pendiente.")
        return

    user_doc_a = col_usuarios.find_one({"user_id": user_id})   or {}
    user_doc_b = col_usuarios.find_one({"user_id": otro_id})   or {}
    display_a  = f"@{user_doc_a.get('username', '')}" if user_doc_a.get('username') else str(user_id)
    display_b  = f"@{user_doc_b.get('username', '')}" if user_doc_b.get('username') else str(otro_id)

    trade_id = str(uuid.uuid4())[:8]
    TRADES_EN_CURSO[trade_id] = {
        "usuarios": [user_id, otro_id],
        "chat_id": chat_id, "thread_id": thread_id,
        "id_unico": {user_id: None, otro_id: None},
        "confirmado": {user_id: False, otro_id: False},
        "estado": "esperando_id",
        "display": {user_id: display_a, otro_id: display_b},
        "inicio": time.time(),   # Para el timeout automático
    }
    TRADES_POR_USUARIO[user_id]  = trade_id
    TRADES_POR_USUARIO[otro_id]  = trade_id

    context.bot.send_message(
        chat_id=chat_id,
        text=f"🤝 <b>¡Trade iniciado!</b>\n• {display_a}\n• {display_b}\n\nAmbos deben ingresar el <b>id_unico</b> de su carta:",
        parse_mode="HTML", message_thread_id=thread_id
    )

def mensaje_trade_id(update, context):
    if not getattr(update, "message", None) or not getattr(update.message, "text", None):
        return
    user_id   = update.message.from_user.id
    chat_id   = update.message.chat_id
    thread_id = getattr(update.message, "message_thread_id", None)
    texto     = update.message.text.strip()

    if texto.lower() in ("cancel", "cancelar"):
        trade_id = TRADES_POR_USUARIO.pop(user_id, None)
        if trade_id and trade_id in TRADES_EN_CURSO:
            trade = TRADES_EN_CURSO.pop(trade_id)
            for uid in trade["usuarios"]:
                TRADES_POR_USUARIO.pop(uid, None)
            context.bot.send_message(chat_id=chat_id, text="❌ Intercambio cancelado.", message_thread_id=thread_id)
        else:
            update.message.reply_text("No tienes ningún intercambio activo.")
        return

    trade_id = TRADES_POR_USUARIO.get(user_id)
    if not trade_id: return
    trade = TRADES_EN_CURSO.get(trade_id)
    if not trade or trade["chat_id"] != chat_id or trade["thread_id"] != thread_id: return
    if trade["estado"] != "esperando_id": return

    carta = col_cartas_usuario.find_one({"user_id": user_id, "id_unico": texto})
    if not carta:
        update.message.reply_text("No tienes una carta con ese id_unico.")
        return

    trade["id_unico"][user_id] = texto
    if all(trade["id_unico"].values()):
        trade["estado"] = "confirmacion"
        mostrar_trade_resumen(context, trade_id)
    else:
        update.message.reply_text("Carta seleccionada, esperando al otro usuario...")

def mostrar_trade_resumen(context, trade_id):
    trade   = TRADES_EN_CURSO[trade_id]
    user_a, user_b = trade["usuarios"]
    id_a, id_b     = trade["id_unico"][user_a], trade["id_unico"][user_b]
    carta_a = col_cartas_usuario.find_one({"user_id": user_a, "id_unico": id_a})
    carta_b = col_cartas_usuario.find_one({"user_id": user_b, "id_unico": id_b})
    display_a = trade["display"][user_a]; display_b = trade["display"][user_b]
    texto = (
        f"🔄 <b>Propuesta de Intercambio</b>\n\n"
        f"{display_a} ofrece <b>[{carta_a['version']}] {carta_a['nombre']}</b> ({id_a})\n"
        f"{display_b} ofrece <b>[{carta_b['version']}] {carta_b['nombre']}</b> ({id_b})\n\n"
        "Ambos deben confirmar para completar el intercambio."
    )
    botones = [[
        InlineKeyboardButton("✅ Confirmar", callback_data=f"tradeconf_{trade_id}"),
        InlineKeyboardButton("❌ Cancelar",  callback_data=f"tradecancel_{trade_id}")
    ]]
    context.bot.send_message(
        chat_id=trade["chat_id"], text=texto, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(botones), message_thread_id=trade["thread_id"]
    )

def callback_trade_confirm(update, context):
    query    = update.callback_query
    data     = query.data
    partes   = data.split("_")
    trade_id = partes[1]
    user_id  = query.from_user.id
    trade    = TRADES_EN_CURSO.get(trade_id)

    if not trade or trade["estado"] != "confirmacion":
        query.answer("No hay intercambio pendiente.", show_alert=True); return
    if user_id not in trade["usuarios"]:
        query.answer("Solo los usuarios del intercambio pueden interactuar.", show_alert=True); return

    if data.startswith("tradeconf_"):
        trade["confirmado"][user_id] = True
        query.answer("Confirmaste el trade.", show_alert=True)

        if all(trade["confirmado"].values()):
            a, b     = trade["usuarios"]
            id_a, id_b = trade["id_unico"][a], trade["id_unico"][b]
            carta_a  = col_cartas_usuario.find_one_and_delete({"user_id": a, "id_unico": id_a})
            carta_b  = col_cartas_usuario.find_one_and_delete({"user_id": b, "id_unico": id_b})

            saldo_a = (col_usuarios.find_one({"user_id": a}) or {}).get("kponey", 0)
            saldo_b = (col_usuarios.find_one({"user_id": b}) or {}).get("kponey", 0)
            if saldo_a < 100 or saldo_b < 100:
                if carta_a: col_cartas_usuario.insert_one(carta_a)
                if carta_b: col_cartas_usuario.insert_one(carta_b)
                context.bot.send_message(
                    chat_id=trade["chat_id"],
                    text="❌ Uno de los usuarios no tiene suficiente Kponey (100 🪙).",
                    message_thread_id=trade["thread_id"]
                )
                for uid in trade["usuarios"]: TRADES_POR_USUARIO.pop(uid, None)
                TRADES_EN_CURSO.pop(trade_id, None)
                return

            if carta_a and carta_b:
                carta_a["user_id"] = b; carta_b["user_id"] = a
                col_cartas_usuario.insert_one(carta_a)
                col_cartas_usuario.insert_one(carta_b)
                col_usuarios.update_one({"user_id": a}, {"$inc": {"kponey": -100}})
                col_usuarios.update_one({"user_id": b}, {"$inc": {"kponey": -100}})
                revisar_sets_completados(a, context)
                revisar_sets_completados(b, context)
                txt = (
                    f"✅ ¡Intercambio realizado!\n"
                    f"{trade['display'][a]} y {trade['display'][b]} intercambiaron sus cartas.\n"
                    f"- 100 Kponey descontados a cada uno."
                )
            else:
                txt = "❌ Error: una de las cartas ya no está disponible."

            context.bot.send_message(chat_id=trade["chat_id"], text=txt, message_thread_id=trade["thread_id"])
            for uid in trade["usuarios"]: TRADES_POR_USUARIO.pop(uid, None)
            TRADES_EN_CURSO.pop(trade_id, None)

    elif data.startswith("tradecancel_"):
        context.bot.send_message(
            chat_id=trade["chat_id"], text="❌ Intercambio cancelado.",
            message_thread_id=trade["thread_id"]
        )
        for uid in trade["usuarios"]: TRADES_POR_USUARIO.pop(uid, None)
        TRADES_EN_CURSO.pop(trade_id, None)
        query.answer("Trade cancelado.", show_alert=True)

dispatcher.add_handler(CallbackQueryHandler(callback_trade_confirm, pattern=r"^trade(conf|cancel)_"))

# ─── Mejorar ──────────────────────────────────────────────────────────────────
@log_command
@solo_en_tema_asignado("mejorar")
@cooldown_critico
def comando_mejorar(update, context):
    user_id = update.message.from_user.id
    if context.args:
        id_unico = context.args[0].strip()
        carta    = col_cartas_usuario.find_one({"user_id": user_id, "id_unico": id_unico})
        if not carta:
            update.message.reply_text("No tienes esa carta.")
            return
        if carta.get("estrellas", "") == "★★★":
            update.message.reply_text("Esta carta ya tiene el máximo de estrellas.")
            return
        mostrar_lista_mejorables(update, context, user_id, [carta], pagina=1)
        return
    cartas_usuario    = list(col_cartas_usuario.find({"user_id": user_id}))
    cartas_mejorables = sorted(
        [c for c in cartas_usuario if c.get("estrellas", "") != "★★★"],
        key=lambda x: (x.get("nombre", "").lower(), x.get("version", "").lower())
    )
    if not cartas_mejorables:
        update.message.reply_text("No tienes cartas que se puedan mejorar.")
        return
    mostrar_lista_mejorables(update, context, user_id, cartas_mejorables, pagina=1)

# ─── Inventario ───────────────────────────────────────────────────────────────
@log_command
@en_tema_asignado_o_privado("inventario")
@cooldown_critico
def comando_inventario(update, context):
    user_id = update.message.from_user.id
    doc     = col_usuarios.find_one({"user_id": user_id}) or {}
    objetos = doc.get("objetos", {})
    kponey  = doc.get("kponey", 0)
    gemas   = doc.get("gemas", 0)
    texto   = "🎒 <b>Tu inventario</b>\n\n"
    tiene   = False
    for obj_id, info in CATALOGO_OBJETOS.items():
        cantidad = objetos.get(obj_id, 0)
        if cantidad > 0:
            tiene = True
            texto += f"{info['emoji']} <b>{info['nombre']}</b>: <b>{cantidad}</b>\n"
    if not tiene:
        texto += "No tienes objetos todavía.\n"
    texto += f"\n💎 <b>Gemas:</b> <code>{gemas}</code>"
    texto += f"\n💸 <b>Kponey:</b> <code>{kponey}</code>"
    update.message.reply_text(texto, parse_mode="HTML")

# ─── Tienda ───────────────────────────────────────────────────────────────────
@log_command
@solo_en_tema_asignado("tienda")
@cooldown_critico
def comando_tienda(update, context):
    user_id = update.message.from_user.id
    doc     = col_usuarios.find_one({"user_id": user_id}) or {}
    kponey  = doc.get("kponey", 0)
    texto   = "🛒 <b>Tienda de objetos</b>\n\n"
    botones = []
    for obj_id, info in CATALOGO_OBJETOS.items():
        texto += f"{info['emoji']} <b>{info['nombre']}</b> — <code>{info['precio']} Kponey</code>\n{info['desc']}\n\n"
        botones.append([InlineKeyboardButton(f"{info['emoji']} Comprar {info['nombre']}", callback_data=f"comprarobj_{obj_id}")])
    texto += f"💸 <b>Tu saldo:</b> <code>{kponey}</code>"
    update.message.reply_text(texto, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(botones))

def comprar_objeto(user_id, obj_id, context, chat_id, reply_func):
    info = CATALOGO_OBJETOS.get(obj_id)
    if not info:
        reply_func("Ese objeto no existe."); return
    doc    = col_usuarios.find_one({"user_id": user_id}) or {}
    kponey = doc.get("kponey", 0)
    precio = info['precio']
    if kponey < precio:
        reply_func("No tienes suficiente Kponey."); return
    col_usuarios.update_one({"user_id": user_id}, {"$inc": {f"objetos.{obj_id}": 1, "kponey": -precio}}, upsert=True)
    reply_func(f"¡Compraste {info['emoji']} {info['nombre']} por {precio} Kponey!", parse_mode="HTML")

@log_command
@solo_en_tema_asignado("comprarobjeto")
@cooldown_critico
def comando_comprarobjeto(update, context):
    user_id = update.message.from_user.id
    if not context.args:
        update.message.reply_text("Usa: /comprarobjeto <objeto_id>"); return
    comprar_objeto(update.message.from_user.id, context.args[0].strip(), context,
                   update.effective_chat.id,
                   lambda text, **kwargs: update.message.reply_text(text, **kwargs))

@solo_en_tema_asignado("tiendaG")
@cooldown_critico
def comando_tiendaG(update, context):
    user_id = update.message.from_user.id
    doc     = col_usuarios.find_one({"user_id": user_id}) or {}
    gemas   = doc.get("gemas", 0)
    texto   = "💎 <b>Tienda de objetos (Gemas)</b>\n\n"
    botones = []
    for obj_id, info in CATALOGO_OBJETOSG.items():
        if "precio_gemas" not in info: continue
        texto += f"{info['emoji']} <b>{info['nombre']}</b> — <code>{info['precio_gemas']} Gemas</code>\n{info['desc']}\n\n"
        botones.append([InlineKeyboardButton(f"{info['emoji']} Comprar {info['nombre']}", callback_data=f"comprarG_{obj_id}")])
    texto += f"💎 <b>Tu saldo:</b> <code>{gemas}</code>"
    update.message.reply_text(texto, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(botones))

def solo_admin(func):
    @wraps(func)
    def wrapper(update, context, *args, **kwargs):
        uid = (update.message.from_user.id if update.message else
               update.callback_query.from_user.id if update.callback_query else None)
        if uid not in ADMIN_IDS:
            if update.message:
                update.message.reply_text("Solo un admin puede usar esto.")
            elif update.callback_query:
                update.callback_query.answer("Solo un admin.", show_alert=True)
            return
        return func(update, context, *args, **kwargs)
    return wrapper

# ─── Sorteos ──────────────────────────────────────────────────────────────────
@log_command
@solo_admin
def comando_sorteo(update, context):
    args = context.args
    if len(args) < 4:
        update.message.reply_text("Uso: /sorteo <Premio> <Cantidad> <Duración horas> <Ganadores>"); return
    premio = " ".join(args[:-3])
    try:
        cantidad = int(args[-3]); duracion_horas = float(args[-2]); num_ganadores = int(args[-1])
    except Exception:
        update.message.reply_text("Cantidad, duración y ganadores deben ser números."); return

    now       = datetime.utcnow()
    fin       = now + timedelta(hours=duracion_horas)
    sorteo_id = str(int(now.timestamp() * 1000))
    thread_id = getattr(update.message, "message_thread_id", None)

    texto = (
        f"🎉 <b>Sorteo KaruKpop</b> 🎉\n"
        f"¡Participa por <b>{cantidad}x {premio}</b>!\n"
        f"Expira en <b>{duracion_horas} horas</b>. Ganadores: <b>{num_ganadores}</b>.\n\n"
        f"👥 <b>Participantes:</b>\n<i>Aún no hay participantes.</i>"
    )
    msg = update.message.reply_text(
        texto, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎉 Participar", callback_data=f"sorteopart_{sorteo_id}")]])
    )
    col_sorteos.insert_one({
        "sorteo_id": sorteo_id, "premio": premio, "cantidad": cantidad,
        "creador_id": update.message.from_user.id, "chat_id": msg.chat_id,
        "mensaje_id": msg.message_id, "fin": fin, "num_ganadores": num_ganadores,
        "participantes": [], "finalizado": False, "ganadores": [],
        "message_thread_id": thread_id,
    })
    try: update.message.delete()
    except Exception: pass

def callback_sorteo_participar(update, context):
    query    = update.callback_query
    user_id  = query.from_user.id
    username = query.from_user.username or ""
    nombre_u = (query.from_user.first_name or "").strip()
    sorteo_id= query.data.replace("sorteopart_", "")
    sorteo   = col_sorteos.find_one({"sorteo_id": sorteo_id, "finalizado": False})
    if not sorteo:
        query.answer("Este sorteo ya terminó.", show_alert=True); return
    if any(p["user_id"] == user_id for p in sorteo.get("participantes", [])):
        query.answer("🎉 Ya estás participando.", show_alert=True); return

    col_sorteos.update_one({"sorteo_id": sorteo_id}, {"$push": {"participantes": {"user_id": user_id, "username": username, "nombre": nombre_u}}})
    sorteo = col_sorteos.find_one({"sorteo_id": sorteo_id})
    participantes = sorteo.get("participantes", [])
    lista = "\n".join([f"• @{p['username']}" if p['username'] else f"• {p['nombre']}" for p in participantes]) or "<i>Aún no hay participantes.</i>"
    texto = (
        f"🎉 <b>Sorteo KaruKpop</b> 🎉\n"
        f"¡Participa por <b>{sorteo['cantidad']}x {sorteo['premio']}</b>!\n\n"
        f"👥 <b>Participantes:</b>\n{lista}"
    )
    try:
        context.bot.edit_message_text(
            chat_id=sorteo["chat_id"], message_id=sorteo["mensaje_id"], text=texto, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎉 Participar", callback_data=f"sorteopart_{sorteo_id}")]])
        )
    except Exception: pass
    query.answer("¡Estás participando!", show_alert=True)

def premio_clave(nombre_premio):
    for key, obj in CATALOGO_OBJETOS.items():
        if obj["nombre"].lower() == nombre_premio.lower():
            return key
    return nombre_premio.lower().replace(" ", "_")

def proceso_sorteos_auto(context):
    while True:
        try:
            ahora   = datetime.utcnow()
            sorteos = list(col_sorteos.find({"finalizado": False, "fin": {"$lte": ahora}}))
            for sorteo in sorteos:
                participantes = sorteo.get("participantes", [])
                num_ganadores = sorteo.get("num_ganadores", 1)
                premio_key    = premio_clave(sorteo["premio"])
                cantidad      = int(sorteo["cantidad"])

                if participantes:
                    ganadores = random.sample(participantes, min(num_ganadores, len(participantes)))
                    col_sorteos.update_one(
                        {"sorteo_id": sorteo["sorteo_id"]},
                        {"$set": {"finalizado": True, "ganadores": [g["user_id"] for g in ganadores]}}
                    )
                    for g in ganadores:
                        col_usuarios.update_one({"user_id": g["user_id"]}, {"$inc": {f"objetos.{premio_key}": cantidad}}, upsert=True)
                        try:
                            context.bot.send_message(chat_id=g["user_id"], text=f"🎉 ¡Ganaste el sorteo de <b>{cantidad}x {sorteo['premio']}</b>!", parse_mode="HTML")
                        except Exception: pass
                    ganador_texto = "\n".join([f"• @{g['username']}" if g['username'] else f"• {g['nombre']}" for g in ganadores])
                    texto_final = f"🎉 <b>Sorteo finalizado</b>\n\nGanador(es):\n{ganador_texto}\n\nPremio: <b>{cantidad}x {sorteo['premio']}</b>"
                else:
                    col_sorteos.update_one({"sorteo_id": sorteo["sorteo_id"]}, {"$set": {"finalizado": True, "ganadores": []}})
                    texto_final = "🎉 <b>Sorteo finalizado</b>\n\nNadie participó."

                try:
                    context.bot.edit_message_text(chat_id=sorteo["chat_id"], message_id=sorteo["mensaje_id"], text=texto_final, parse_mode="HTML")
                except Exception: pass
        except Exception as e:
            print("[proceso_sorteos_auto] Error:", e)
        time.sleep(60)

def iniciar_proceso_sorteos(context):
    threading.Thread(target=proceso_sorteos_auto, args=(context,), daemon=True).start()

# ─── Mercado ──────────────────────────────────────────────────────────────────
def mostrar_mercado_pagina(chat_id, message_id, context, user_id, pagina=1,
                            filtro=None, valor_filtro=None, orden=None, thread_id=None):
    query_mercado = {}
    if filtro == "estrellas": query_mercado["estrellas"] = valor_filtro
    elif filtro == "grupo":   query_mercado["grupo"]     = valor_filtro

    cartas_list = list(col_mercado.find(query_mercado))
    if orden == "menor":    cartas_list.sort(key=lambda x: x.get("card_id", 0))
    elif orden == "mayor":  cartas_list.sort(key=lambda x: -x.get("card_id", 0))
    else:
        cartas_list.sort(key=lambda x: (x.get("grupo", "").lower(), x.get("nombre", "").lower(), x.get("card_id", 0)))

    por_pagina  = 10
    total_pag   = max(1, ((len(cartas_list) - 1) // por_pagina) + 1)
    pagina      = max(1, min(pagina, total_pag))
    cartas_pag  = cartas_list[(pagina-1)*por_pagina: pagina*por_pagina]

    usuario    = col_usuarios.find_one({"user_id": user_id}) or {}
    favoritos  = usuario.get("favoritos", [])

    texto = "<b>🛒 Mercado</b>\n"
    for c in cartas_pag:
        est = f"[{c.get('estrellas','?')}]"; num = f"#{c.get('card_id','?')}"
        ver = f"[{c.get('version','?')}]";   nom = c.get('nombre','?'); grp = c.get('grupo','?')
        idu = c.get('id_unico','')
        precio   = precio_carta_tabla(c.get('estrellas','☆☆☆'), c.get('card_id', 0))
        es_fav   = any(fav.get("nombre") == c.get("nombre") and fav.get("version") == c.get("version") for fav in favoritos)
        fav_icon = " ⭐" if es_fav else ""
        vendedor_id = c.get("vendedor_id")
        vendedor_linea = ""
        if vendedor_id:
            vd = col_usuarios.find_one({"user_id": vendedor_id}) or {}
            if vd.get("username"):
                vendedor_linea = f'👤 <code>{vd["username"]}</code>\n'
        texto += f"{est} · {num} · {ver} · {nom} · {grp}{fav_icon}\n💲{precio:,}\n{vendedor_linea}<code>/comprar {idu}</code>\n\n"
    if not cartas_pag:
        texto += "\n(No hay cartas)"

    botones = [[InlineKeyboardButton("🔎 Filtrar / Ordenar", callback_data=f"mercado_filtros_{user_id}_{pagina}")]]
    paginacion = []
    if pagina > 1:       paginacion.append(InlineKeyboardButton("⬅️", callback_data=f"mercado_pagina_{user_id}_{pagina-1}_{filtro or 'none'}_{valor_filtro or 'none'}_{orden or 'none'}_{thread_id or 'none'}"))
    if pagina < total_pag:paginacion.append(InlineKeyboardButton("➡️", callback_data=f"mercado_pagina_{user_id}_{pagina+1}_{filtro or 'none'}_{valor_filtro or 'none'}_{orden or 'none'}_{thread_id or 'none'}"))
    if paginacion: botones.append(paginacion)
    teclado = InlineKeyboardMarkup(botones)

    try:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=texto, parse_mode="HTML", reply_markup=teclado)
    except RetryAfter as e:
        print(f"[mercado] Flood control: {e.retry_after}s")
    except Exception as ex:
        print("[mercado] Error:", ex)

def mostrar_menu_filtros(user_id, pagina, thread_id=None):
    botones = [
        [InlineKeyboardButton("⭐ Estado",   callback_data=f"mercado_filtro_estado_{user_id}_{pagina}_{thread_id or 'none'}")],
        [InlineKeyboardButton("👥 Grupo",    callback_data=f"mercado_filtro_grupo_{user_id}_{pagina}_1_{thread_id or 'none'}")],
        [InlineKeyboardButton("🔢 Número",   callback_data=f"mercado_filtro_numero_{user_id}_{pagina}_{thread_id or 'none'}")],
        [InlineKeyboardButton("⬅️ Volver",  callback_data=f"mercado_pagina_{user_id}_{pagina}_none_none_none_{thread_id or 'none'}")]
    ]
    return InlineKeyboardMarkup(botones)

def mostrar_menu_estrellas(user_id, pagina, thread_id=None):
    botones = [
        [InlineKeyboardButton(e, callback_data=f"mercado_filtraestrella_{user_id}_{pagina}_{e}_{thread_id or 'none'}")]
        for e in ["★★★","★★☆","★☆☆","☆☆☆"]
    ]
    botones.append([InlineKeyboardButton("⬅️ Volver", callback_data=f"mercado_filtros_{user_id}_{pagina}_{thread_id or 'none'}")])
    return InlineKeyboardMarkup(botones)

def mostrar_menu_grupos(user_id, pagina, grupos, thread_id=None):
    por_pagina = 5; total = len(grupos)
    paginas    = max(1, (total-1)//por_pagina+1)
    pagina     = max(1, min(pagina, paginas))
    inicio     = (pagina-1)*por_pagina
    grupos_pag = grupos[inicio:inicio+por_pagina]
    matriz     = [[InlineKeyboardButton(g, callback_data=f"mercado_filtragrupo_{user_id}_{pagina}_{urllib.parse.quote_plus(g)}_{thread_id or 'none'}")] for g in grupos_pag]
    nav = []
    if pagina > 1:      nav.append(InlineKeyboardButton("⬅️", callback_data=f"mercado_filtro_grupo_{user_id}_{pagina-1}_{thread_id or 'none'}"))
    if pagina < paginas:nav.append(InlineKeyboardButton("➡️", callback_data=f"mercado_filtro_grupo_{user_id}_{pagina+1}_{thread_id or 'none'}"))
    if nav: matriz.append(nav)
    matriz.append([InlineKeyboardButton("⬅️ Volver", callback_data=f"mercado_filtros_{user_id}_{pagina}_{thread_id or 'none'}")])
    return InlineKeyboardMarkup(matriz)

def mostrar_menu_ordenar(user_id, pagina, thread_id=None):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬆️ Menor a mayor", callback_data=f"mercado_ordennum_{user_id}_{pagina}_menor_{thread_id or 'none'}")],
        [InlineKeyboardButton("⬇️ Mayor a menor", callback_data=f"mercado_ordennum_{user_id}_{pagina}_mayor_{thread_id or 'none'}")],
    ])

def normalizar_nombre_carta(nombre):
    nombre = nombre.lower()
    nombre = re.sub(r"\s+", " ", nombre).strip()
    return nombre

# ─── Favoritos ────────────────────────────────────────────────────────────────
@log_command
@en_tema_asignado_o_privado("favoritos")
@cooldown_critico
def comando_favoritos(update, context):
    user_id  = update.message.from_user.id
    doc      = col_usuarios.find_one({"user_id": user_id})
    favoritos= doc.get("favoritos", []) if doc else []
    if not favoritos:
        update.message.reply_text("⭐ No tienes cartas favoritas aún.", parse_mode="HTML"); return
    texto = "⭐ <b>Tus cartas favoritas:</b>\n\n"
    for fav in favoritos:
        texto += f"<code>{fav.get('grupo','')} [{fav.get('version','')}] {fav.get('nombre','')}</code>\n"
    update.message.reply_text(texto, parse_mode="HTML")

@log_command
@solo_en_tema_asignado("fav")
@cooldown_critico
def comando_fav(update, context):
    user_id = update.message.from_user.id
    args    = context.args
    if not args or len(args) < 3:
        update.message.reply_text("Usa: /fav <grupo> [Vn] Nombre"); return
    version_idx = next((i for i, x in enumerate(args) if x.startswith("[") and x.endswith("]")), -1)
    if version_idx <= 0 or version_idx == len(args) - 1:
        update.message.reply_text("Formato: /fav Twice [V1] Dahyun"); return
    grupo   = " ".join(args[:version_idx])
    version = args[version_idx][1:-1]
    nombre  = " ".join(args[version_idx+1:]).strip()
    nombre_norm = normalizar_nombre_carta(f"{grupo} [{version}] {nombre}")
    existe = next((c for c in cartas if normalizar_nombre_carta(f"{c.get('grupo', c.get('set'))} [{c['version']}] {c['nombre']}") == nombre_norm), None)
    if not existe:
        update.message.reply_text(f"No se encontró: <code>{grupo} [{version}] {nombre}</code>", parse_mode="HTML"); return
    doc       = col_usuarios.find_one({"user_id": user_id}) or {}
    favoritos = doc.get("favoritos", [])
    ya_es_fav = any(normalizar_nombre_carta(f"{f['grupo']} [{f['version']}] {f['nombre']}") == nombre_norm for f in favoritos)
    if ya_es_fav:
        favoritos = [f for f in favoritos if normalizar_nombre_carta(f"{f['grupo']} [{f['version']}] {f['nombre']}") != nombre_norm]
        col_usuarios.update_one({"user_id": user_id}, {"$set": {"favoritos": favoritos}}, upsert=True)
        update.message.reply_text(f"❌ Quitaste de favoritos: <code>{grupo} [{version}] {nombre}</code>", parse_mode="HTML")
    else:
        favoritos.append({"grupo": grupo, "nombre": nombre, "version": version})
        col_usuarios.update_one({"user_id": user_id}, {"$set": {"favoritos": favoritos}}, upsert=True)
        update.message.reply_text(f"⭐ Añadiste a favoritos: <code>{grupo} [{version}] {nombre}</code>", parse_mode="HTML")

# ─── Precio ───────────────────────────────────────────────────────────────────
@log_command
@solo_en_tema_asignado("precio")
@cooldown_critico
def comando_precio(update, context):
    if not context.args:
        update.message.reply_text("Usa: /precio <id_unico>"); return
    id_unico    = context.args[0].strip()
    carta       = col_cartas_usuario.find_one({"id_unico": id_unico})
    if not carta:
        update.message.reply_text("No se encontró la carta."); return
    estrellas   = carta.get('estrellas', '☆☆☆')
    card_id     = carta.get('card_id') or extraer_card_id_de_id_unico(id_unico)
    total_copias= col_cartas_usuario.count_documents({"nombre": carta['nombre'], "version": carta['version']})
    precio      = precio_carta_tabla(estrellas, card_id)
    update.message.reply_text(
        f"🖼️ <b>[{id_unico}]</b>\n• Nombre: <b>{carta['nombre']}</b>\n"
        f"• Estado: <b>{estrellas}</b>\n• Nº: <b>#{card_id}</b>\n"
        f"• Precio: <code>{precio} Kponey</code>\n• Copias globales: <b>{total_copias}</b>",
        parse_mode='HTML'
    )

# ─── Vender ───────────────────────────────────────────────────────────────────
@log_command
@solo_en_tema_asignado("vender")
@cooldown_critico
def comando_vender(update, context):
    user_id = update.message.from_user.id
    if not context.args:
        update.message.reply_text("Usa: /vender <id_unico>"); return
    id_unico = context.args[0].strip()
    carta    = col_cartas_usuario.find_one({"user_id": user_id, "id_unico": id_unico})
    if not carta:
        update.message.reply_text("No tienes esa carta."); return
    if col_mercado.find_one({"id_unico": id_unico}):
        update.message.reply_text("Esta carta ya está en el mercado."); return
    estrellas = carta.get('estrellas')
    card_id   = carta.get('card_id', extraer_card_id_de_id_unico(id_unico))
    precio    = precio_carta_tabla(estrellas, card_id)
    col_cartas_usuario.delete_one({"user_id": user_id, "id_unico": id_unico})
    col_mercado.insert_one({
        "id_unico": id_unico, "vendedor_id": user_id,
        "nombre": carta['nombre'], "version": carta['version'],
        "estado": carta['estado'], "estrellas": estrellas, "precio": precio,
        "card_id": card_id, "fecha": datetime.utcnow(),
        "imagen": carta.get("imagen"), "grupo": carta.get("grupo", "")
    })
    update.message.reply_text(
        f"📦 Carta <b>{carta['nombre']} [{carta['version']}]</b> puesta en el mercado por <b>{precio} Kponey</b>.",
        parse_mode='HTML'
    )

# ─── Comprar ──────────────────────────────────────────────────────────────────
@log_command
@solo_en_tema_asignado("comprar")
@cooldown_critico
def comando_comprar(update, context):
    user_id = update.message.from_user.id
    if not context.args:
        update.message.reply_text("Usa: /comprar <id_unico>"); return
    id_unico = context.args[0].strip()
    carta    = col_mercado.find_one_and_delete({"id_unico": id_unico})
    if not carta:
        update.message.reply_text("Esa carta ya no está disponible."); return
    if carta["vendedor_id"] == user_id:
        update.message.reply_text("No puedes comprar tu propia carta.")
        col_mercado.insert_one(carta); return

    estrellas = carta.get("estrellas", "☆☆☆")
    card_id   = carta.get("card_id") or extraer_card_id_de_id_unico(carta.get("id_unico"))
    precio    = precio_carta_tabla(estrellas, card_id)
    saldo     = (col_usuarios.find_one({"user_id": user_id}) or {}).get("kponey", 0)
    if saldo < precio:
        update.message.reply_text(f"No tienes suficiente Kponey. Precio: {precio}, tu saldo: {saldo}")
        col_mercado.insert_one(carta); return

    col_usuarios.update_one({"user_id": user_id}, {"$inc": {"kponey": -precio}}, upsert=True)
    col_usuarios.update_one({"user_id": carta["vendedor_id"]}, {"$inc": {"kponey": precio}}, upsert=True)

    col_historial_ventas.insert_one({
        "carta": {"nombre": carta.get('nombre'), "version": carta.get('version'), "card_id": card_id, "estrellas": estrellas},
        "precio": precio, "comprador_id": user_id, "vendedor_id": carta["vendedor_id"],
        "fecha": datetime.utcnow()
    })

    carta['user_id'] = user_id
    for key in ['_id', 'vendedor_id', 'precio', 'fecha']:
        carta.pop(key, None)
    if not carta.get('estrellas'): carta['estrellas'] = estrellas
    if not carta.get('card_id'):   carta['card_id']   = card_id
    col_cartas_usuario.insert_one(carta)
    revisar_sets_completados(user_id, context)

    update.message.reply_text(
        f"✅ Compraste <b>{carta['nombre']} [{carta['version']}]</b> por <b>{precio} Kponey</b>.",
        parse_mode="HTML"
    )
    try:
        comprador = update.message.from_user
        txt_comp  = f"<b>{comprador.full_name}</b>"
        if comprador.username: txt_comp += f" (<code>{comprador.username}</code>)"
        context.bot.send_message(
            chat_id=carta["vendedor_id"],
            text=f"💸 Vendiste <b>{carta['nombre']} [{carta['version']}]</b> por <b>{precio} Kponey</b>.\nComprador: {txt_comp}",
            parse_mode="HTML"
        )
    except Exception: pass

# ─── Ranking mercado ──────────────────────────────────────────────────────────
@solo_en_tema_asignado("rankingmercado")
def comando_rankingmercado(update, context):
    pipeline_v = [{"$group": {"_id": "$vendedor_id",  "ventas":  {"$sum": 1}}}, {"$sort": {"ventas":  -1}}, {"$limit": 10}]
    pipeline_c = [{"$group": {"_id": "$comprador_id", "compras": {"$sum": 1}}}, {"$sort": {"compras": -1}}, {"$limit": 10}]
    top_v = list(col_historial_ventas.aggregate(pipeline_v))
    top_c = list(col_historial_ventas.aggregate(pipeline_c))
    texto = "<b>🏆 Ranking Mercado</b>\n\n<b>🔹 Top Vendedores:</b>\n"
    for i, v in enumerate(top_v, 1):
        if not v["_id"]: continue
        u = col_usuarios.find_one({"user_id": v["_id"]}) or {}
        texto += f"{i}. <code>{u.get('username', v['_id'])}</code> — {v['ventas']} ventas\n"
    texto += "\n<b>🔸 Top Compradores:</b>\n"
    for i, c in enumerate(top_c, 1):
        if not c["_id"]: continue
        u = col_usuarios.find_one({"user_id": c["_id"]}) or {}
        texto += f"{i}. <code>{u.get('username', c['_id'])}</code> — {c['compras']} compras\n"
    update.message.reply_text(texto, parse_mode="HTML")

# ─── Retirar ──────────────────────────────────────────────────────────────────
@log_command
@solo_en_tema_asignado("retirar")
def comando_retirar(update, context):
    user_id  = update.message.from_user.id
    if not context.args:
        update.message.reply_text("Usa: /retirar <id_unico>"); return
    id_unico = context.args[0].strip()
    carta    = col_mercado.find_one({"id_unico": id_unico, "vendedor_id": user_id})
    if not carta:
        update.message.reply_text("No tienes esa carta en el mercado."); return
    col_mercado.delete_one({"id_unico": id_unico})
    carta['user_id'] = user_id
    # Usar pop con default None para evitar KeyError
    for key in ['_id', 'vendedor_id', 'precio', 'fecha']:
        carta.pop(key, None)
    if not carta.get('estrellas') or carta.get('estrellas') == '★??':
        estado = carta.get('estado')
        for c in cartas:
            if c['nombre'] == carta['nombre'] and c['version'] == carta['version'] and c['estado'] == estado:
                carta['estrellas'] = c.get('estado_estrella', '★??')
                break
    col_cartas_usuario.insert_one(carta)
    update.message.reply_text("Carta retirada del mercado y devuelta a tu álbum.")

# ─── Saldo / Gemas ───────────────────────────────────────────────────────────
@log_command
@en_tema_asignado_o_privado("saldo")
@cooldown_critico
def comando_saldo(update, context):
    user_id = update.message.from_user.id
    usuario = col_usuarios.find_one({"user_id": user_id}) or {}
    update.message.reply_text(f"💸 <b>Tus Kponey:</b> <code>{usuario.get('kponey', 0)}</code>", parse_mode="HTML")

@log_command
@en_tema_asignado_o_privado("gemas")
@grupo_oficial
def comando_gemas(update, context):
    user_id = update.message.from_user.id
    usuario = col_usuarios.find_one({"user_id": user_id}) or {}
    update.message.reply_text(f"💎 <b>Tus gemas:</b> <code>{usuario.get('gemas', 0)}</code>", parse_mode="HTML")

@log_command
@grupo_oficial
def comando_darKponey(update, context):
    if update.message.from_user.id != TU_USER_ID:
        update.message.reply_text("Solo el creador puede usar esto."); return
    dest_id = None
    if update.message.reply_to_message:
        dest_id = update.message.reply_to_message.from_user.id
    elif context.args and context.args[0].startswith('@'):
        u = col_usuarios.find_one({"username": context.args[0][1:].lower()})
        if not u:
            update.message.reply_text("Usuario no encontrado."); return
        dest_id = u["user_id"]
    elif context.args:
        try: dest_id = int(context.args[0])
        except ValueError:
            update.message.reply_text("Uso: /darKponey <@usuario|user_id> <cantidad>"); return
    else:
        update.message.reply_text("Especifica un usuario."); return
    try:    cantidad = int(context.args[-1])
    except Exception:
        update.message.reply_text("Indica la cantidad."); return
    col_usuarios.update_one({"user_id": dest_id}, {"$inc": {"kponey": cantidad}}, upsert=True)
    update.message.reply_text(f"💸 Kponey actualizado para <code>{dest_id}</code> ({cantidad:+})", parse_mode="HTML")

def mostrar_carta_individual(chat_id, user_id, lista_cartas, idx, context, mensaje_a_editar=None, query=None):
    carta     = lista_cartas[idx]
    version   = carta.get('version', '')
    nombre    = carta.get('nombre', '')
    imagen_url= carta.get('imagen', imagen_de_carta(nombre, version))
    id_unico  = carta.get('id_unico', '')
    texto     = f"<b>[{version}] {nombre}</b>\nID: <code>{id_unico}</code>\n"
    if query is not None:
        try:
            query.edit_message_media(
                media=InputMediaPhoto(media=imagen_url, caption=texto, parse_mode='HTML'),
                reply_markup=query.message.reply_markup
            )
        except Exception:
            query.answer("No se pudo actualizar la imagen.", show_alert=True)
    else:
        context.bot.send_photo(chat_id=chat_id, photo=imagen_url, caption=texto, parse_mode='HTML')

@en_tema_asignado_o_privado("miid")
def comando_miid(update, context):
    update.message.reply_text(f"Tu ID de Telegram es: {update.effective_user.id}")

@log_command
@grupo_oficial
def comando_bonoidolday(update, context):
    if not es_admin(update):
        update.message.reply_text("Solo administradores."); return
    if update.message.reply_to_message:
        dest_id = update.message.reply_to_message.from_user.id
        if len(context.args) != 1:
            update.message.reply_text("Uso: /bonoidolday <cantidad>"); return
        try:    cantidad = int(context.args[0])
        except: update.message.reply_text("La cantidad debe ser un número."); return
    elif len(context.args) == 2:
        try:    dest_id = int(context.args[0]); cantidad = int(context.args[1])
        except: update.message.reply_text("Uso: /bonoidolday <user_id> <cantidad>"); return
    else:
        update.message.reply_text("Uso: /bonoidolday <user_id> <cantidad>"); return
    if cantidad < 1:
        update.message.reply_text("La cantidad debe ser mayor que 0."); return
    col_usuarios.update_one({"user_id": dest_id}, {"$inc": {"bono": cantidad}}, upsert=True)
    u = col_usuarios.find_one({"user_id": dest_id}) or {}
    mencion = f"@{u.get('username')}" if u.get("username") else f"<code>{dest_id}</code>"
    update.message.reply_text(f"✅ Bono de {cantidad} tiradas entregado a {mencion}.", parse_mode='HTML')

# ─── Ampliar ──────────────────────────────────────────────────────────────────
@log_command
@solo_en_tema_asignado("ampliar")
def comando_ampliar(update, context, id_unico=None):
    if id_unico is None:
        if not context.args:
            update.message.reply_text("Debes indicar el ID único: /ampliar <id_unico>"); return
        id_unico  = context.args[0].strip()
        user_id   = update.message.from_user.id
        enviar    = lambda **kwargs: update.message.reply_photo(**kwargs)
        chat_id   = update.message.chat_id
        thread_id = getattr(update.message, "message_thread_id", None)
    else:
        user_id   = update.effective_user.id if hasattr(update, "effective_user") else update.callback_query.from_user.id
        msg       = update.callback_query.message
        chat_id   = msg.chat_id
        thread_id = getattr(msg, "message_thread_id", None)
        enviar    = lambda **kwargs: context.bot.send_photo(chat_id=chat_id, message_thread_id=thread_id, **kwargs)

    carta = col_cartas_usuario.find_one({"user_id": user_id, "id_unico": id_unico})
    fuente= "album"
    if not carta:
        carta  = col_mercado.find_one({"id_unico": id_unico})
        fuente = "mercado"
    if not carta:
        if hasattr(update, "message") and update.message:
            update.message.reply_text("No tienes esta carta.")
        else:
            update.callback_query.answer("No tienes esta carta.", show_alert=True)
        return

    imagen_url = carta.get('imagen')
    nombre     = carta.get('nombre', '')
    apodo      = carta.get('apodo', '')
    nombre_m   = f'({apodo}) {nombre}' if apodo else nombre
    version    = carta.get('version', '')
    grupo      = carta.get('grupo', version)
    estrellas  = carta.get('estrellas', '☆☆☆')
    card_id    = carta.get('card_id') or extraer_card_id_de_id_unico(id_unico)
    total_copias = col_cartas_usuario.count_documents({"nombre": nombre, "version": version, "grupo": grupo})
    doc_user   = col_usuarios.find_one({"user_id": user_id}) or {}
    favoritos  = doc_user.get("favoritos", [])
    es_fav     = any(fav.get("nombre") == nombre and fav.get("version") == version and fav.get("grupo", version) == grupo for fav in favoritos)
    precio     = precio_carta_tabla(estrellas, card_id)

    texto = (
        f"🎴 <b>Info de carta [{id_unico}]</b>\n"
        f"• Nombre: {'⭐ ' if es_fav else ''}<b>{nombre_m}</b>\n"
        f"• Grupo: <b>{grupo}</b>\n• Versión: <b>{version}</b>\n"
        f"• Nº: <b>#{card_id}</b>\n• Estado: <b>{estrellas}</b>\n"
        f"• Precio: <code>{precio} Kponey</code>\n• Copias globales: <b>{total_copias}</b>"
    )
    teclado = InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Poner en el mercado", callback_data=f"ampliar_vender_{id_unico}")]]) if fuente == "album" else None

    try:
        enviar(photo=imagen_url, caption=texto, parse_mode='HTML', reply_markup=teclado)
    except Exception:
        enviar(caption=f"[Imagen no disponible]\n\n{texto}", parse_mode='HTML', reply_markup=teclado)

# ─── /comandos ───────────────────────────────────────────────────────────────
@log_command
@solo_en_tema_asignado("comandos")
@grupo_oficial
@cooldown_critico
def comando_comandos(update, context):
    update.message.reply_text(
        "📋 <b>Comandos disponibles:</b>\n\n"
        "/idolday — Drop de cartas\n/album — Tu colección\n/ampliar — Ver carta\n"
        "/vender — Vender carta\n/mercado — Ver mercado\n/comprar — Comprar carta\n"
        "/retirar — Retirar del mercado\n/inventario — Objetos y saldo\n"
        "/kponey — Tu saldo\n/precio — Precio de carta\n/setsprogreso — Progreso\n"
        "/set — Detalle de set\n/miid — Tu ID\n/trk — Intercambio de cartas",
        parse_mode='HTML'
    )

# ─── Mercado (comando) ────────────────────────────────────────────────────────
@log_command
@solo_en_temas_permitidos("mercado")
@cooldown_critico
def comando_mercado(update, context):
    user_id  = update.message.from_user.id
    chat_id  = update.effective_chat.id
    thread_id= getattr(update.message, "message_thread_id", None)
    msg = context.bot.send_message(chat_id=chat_id, text="🛒 Mercado (cargando...)", message_thread_id=thread_id)
    mostrar_mercado_pagina(chat_id, msg.message_id, context, user_id, pagina=1, thread_id=thread_id)

# ─── Dar / regalar cartas ─────────────────────────────────────────────────────
@log_command
@grupo_oficial
def comando_giveidol(update, context):
    if len(context.args) < 2:
        update.message.reply_text("Uso: /giveidol <id_unico> @usuario_destino"); return
    id_unico  = context.args[0].strip()
    user_dest = context.args[1].strip()
    user_id   = update.message.from_user.id
    chat      = update.effective_chat
    carta     = col_cartas_usuario.find_one({"user_id": user_id, "id_unico": id_unico})
    if not carta:
        update.message.reply_text("No tienes esa carta."); return

    target_user_id = None
    if user_dest.startswith('@'):
        posible = col_usuarios.find_one({"username": user_dest[1:].lower()})
        if posible: target_user_id = posible["user_id"]
    else:
        try: target_user_id = int(user_dest)
        except Exception: pass

    if not target_user_id:
        update.message.reply_text("No pude identificar al usuario destino."); return
    if user_id == target_user_id:
        update.message.reply_text("No puedes regalarte cartas a ti mismo."); return

    col_cartas_usuario.delete_one({"user_id": user_id, "id_unico": id_unico})
    carta["user_id"] = target_user_id
    col_cartas_usuario.insert_one(carta)
    update.message.reply_text(f"🎁 Carta [{id_unico}] enviada a <b>@{user_dest.lstrip('@')}</b>!", parse_mode='HTML')
    try:
        context.bot.send_message(chat_id=target_user_id, text=f"🎉 ¡Recibiste la carta <b>{id_unico}</b>! Revisa tu /album.", parse_mode='HTML')
    except Exception: pass

# ─── Sets / Progreso ──────────────────────────────────────────────────────────
def obtener_sets_disponibles():
    return sorted(SETS_PRECALCULADOS.keys(), key=lambda s: s.lower())

def mostrar_setsprogreso(update, context, pagina=1, mensaje=None, editar=False, thread_id=None):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    sets    = obtener_sets_disponibles()
    cartas_usuario  = list(col_cartas_usuario.find({"user_id": user_id}))
    cartas_u_unicas = set((c.get("grupo", c.get("set")), c["nombre"], c["version"]) for c in cartas_usuario)

    por_pagina = 5; total = len(sets)
    paginas    = (total-1)//por_pagina+1
    pagina     = max(1, min(pagina, paginas))
    inicio     = (pagina-1)*por_pagina; fin = min(inicio+por_pagina, total)
    texto = "<b>📚 Progreso de sets:</b>\n\n"

    for s in sets[inicio:fin]:
        cartas_set = SETS_PRECALCULADOS.get(s, set())
        total_set  = len(cartas_set)
        usuario_tiene = sum(1 for (n, v) in cartas_set if (s, n, v) in cartas_u_unicas)
        emoji = "🌟" if usuario_tiene == total_set else ("⭐" if usuario_tiene >= total_set//2 else ("🔸" if usuario_tiene > 0 else "⬜"))
        bloques_llenos = int((usuario_tiene / total_set) * 10) if total_set > 0 else 0
        barra = "🟩" * bloques_llenos + "⬜" * (10 - bloques_llenos)
        texto += f"{emoji} <b>{s}</b>: {usuario_tiene}/{total_set}\n{barra}\n\n"

    texto += f"Página {pagina}/{paginas}\nUsa <code>/set NombreSet</code> para ver detalles."
    botones = []
    if pagina > 1:       botones.append(InlineKeyboardButton("⬅️", callback_data=f"setsprogreso_{pagina-1}"))
    if pagina < paginas: botones.append(InlineKeyboardButton("➡️", callback_data=f"setsprogreso_{pagina+1}"))
    teclado = InlineKeyboardMarkup([botones]) if botones else None

    if editar and mensaje:
        try: mensaje.edit_text(texto, reply_markup=teclado, parse_mode="HTML")
        except Exception: context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado, parse_mode="HTML", message_thread_id=thread_id)
    else:
        context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado, parse_mode="HTML", message_thread_id=thread_id)

@log_command
@solo_en_tema_asignado("set")
def comando_set_detalle(update, context):
    user_id   = update.effective_user.id
    thread_id = getattr(update.message, "message_thread_id", None)
    if not context.args:
        mostrar_lista_set(update, context, pagina=1, thread_id=thread_id); return
    nombre_set = " ".join(context.args)
    sets       = obtener_sets_disponibles()
    set_match  = next((s for s in sets if s.lower() == nombre_set.lower()), None)
    if not set_match:
        mostrar_lista_set(update, context, pagina=1, error=nombre_set, thread_id=thread_id); return
    mostrar_detalle_set(update, context, set_match, user_id, pagina=1, thread_id=thread_id)

def mostrar_lista_set(update, context, pagina=1, mensaje=None, editar=False, error=None, thread_id=None):
    sets    = obtener_sets_disponibles()
    por_pagina = 8; total = len(sets)
    paginas = (total-1)//por_pagina+1
    pagina  = max(1, min(pagina, paginas))
    inicio  = (pagina-1)*por_pagina; fin = min(inicio+por_pagina, total)
    texto   = "<b>Sets disponibles:</b>\n" + "\n".join(f"• <code>{s}</code>" for s in sets[inicio:fin])
    if error: texto = f"❌ No se encontró: <b>{error}</b>\n\n" + texto
    texto  += f"\n\nEjemplo: <code>/set Twice</code>\nPágina {pagina}/{paginas}"
    botones = []
    if pagina > 1:      botones.append(InlineKeyboardButton("⬅️", callback_data=f"setlist_{pagina-1}"))
    if pagina < paginas:botones.append(InlineKeyboardButton("➡️", callback_data=f"setlist_{pagina+1}"))
    teclado = InlineKeyboardMarkup([botones]) if botones else None
    chat_id = update.effective_chat.id
    if editar and mensaje:
        try: mensaje.edit_text(texto, reply_markup=teclado, parse_mode="HTML")
        except Exception: context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado, parse_mode="HTML", message_thread_id=thread_id)
    else:
        context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado, parse_mode="HTML", message_thread_id=thread_id)

def mostrar_detalle_set(update, context, set_name, user_id, pagina=1, mensaje=None, editar=False, thread_id=None):
    chat_id    = update.effective_chat.id
    cartas_set = [c for c in cartas if (c.get("set") == set_name or c.get("grupo") == set_name)]
    # Deduplicar por (nombre, version, grupo) — usa separador que no aparezca en nombres
    vistos     = {}
    for c in cartas_set:
        key = f"{c['nombre']}|||{c['version']}|||{c.get('grupo', set_name)}"
        if key not in vistos:
            vistos[key] = c
    cartas_set_unicas = list(vistos.values())

    por_pagina = 8; total = len(cartas_set_unicas)
    paginas    = (total-1)//por_pagina+1 if total > 0 else 1
    pagina     = max(1, min(pagina, paginas))
    inicio     = (pagina-1)*por_pagina; fin = min(inicio+por_pagina, total)

    cartas_usuario  = list(col_cartas_usuario.find({"user_id": user_id}))
    cartas_u_unicas = set((c["nombre"], c["version"], c.get("grupo", set_name)) for c in cartas_usuario)
    user_doc        = col_usuarios.find_one({"user_id": user_id}) or {}
    favoritos       = user_doc.get("favoritos", [])

    usuario_tiene   = sum(1 for c in cartas_set_unicas if (c["nombre"], c["version"], c.get("grupo", set_name)) in cartas_u_unicas)
    bloques_llenos  = int((usuario_tiene / total) * 10) if total > 0 else 0
    barra = "🟩" * bloques_llenos + "⬜" * (10 - bloques_llenos)
    texto = f"<b>🌟 Set: {set_name} ({usuario_tiene}/{total})</b>\n{barra}\n\n"

    for carta in cartas_set_unicas[inicio:fin]:
        nombre  = carta["nombre"]; version = carta["version"]; grupo = carta.get("grupo", set_name)
        key_t   = (nombre, version, grupo)
        nvg     = f"{grupo} [{version}] {nombre}"
        nvg_norm= normalizar_nombre_carta(nvg)
        es_fav  = any(normalizar_nombre_carta(f"{fav.get('grupo',grupo)} [{fav.get('version',version)}] {fav.get('nombre',nombre)}") == nvg_norm for fav in favoritos)
        icon_fav= " ⭐" if es_fav else ""
        texto  += ("✅" if key_t in cartas_u_unicas else "❌") + f" {nvg}{icon_fav}\n"

    # ─── Botones de paginación usando separador seguro ────────────────────────
    # Usamos el nombre del set codificado en base64 para evitar problemas con guiones bajos
    import base64
    set_b64  = base64.urlsafe_b64encode(set_name.encode()).decode()
    botones  = []
    if pagina > 1:       botones.append(InlineKeyboardButton("⬅️", callback_data=f"setdet|{set_b64}|{user_id}|{pagina-1}"))
    if pagina < paginas: botones.append(InlineKeyboardButton("➡️", callback_data=f"setdet|{set_b64}|{user_id}|{pagina+1}"))
    teclado = InlineKeyboardMarkup([botones]) if botones else None

    if editar and mensaje:
        try: mensaje.edit_text(texto, reply_markup=teclado, parse_mode='HTML')
        except Exception: context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado, parse_mode='HTML', message_thread_id=thread_id)
    else:
        context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado, parse_mode='HTML', message_thread_id=thread_id)

# ─── Callbacks de Sets ────────────────────────────────────────────────────────
def manejador_callback_setdet(update, context):
    import base64
    query  = update.callback_query
    data   = query.data  # formato: setdet|<set_b64>|<user_id>|<pagina>
    partes = data.split("|")
    if len(partes) != 4:
        query.answer("Error en paginación", show_alert=True); return
    set_name = base64.urlsafe_b64decode(partes[1].encode()).decode()
    user_id  = int(partes[2]); pagina = int(partes[3])
    mostrar_detalle_set(update, context, set_name, user_id, pagina=pagina, mensaje=query.message, editar=True)
    query.answer()

def manejador_callback_setlist(update, context):
    query  = update.callback_query
    partes = query.data.split("_")
    if len(partes) != 2:
        query.answer("Error en paginación", show_alert=True); return
    pagina    = int(partes[1])
    thread_id = getattr(query.message, "message_thread_id", None)
    mostrar_lista_set(update, context, pagina=pagina, mensaje=query.message, editar=True, thread_id=thread_id)
    query.answer()

def manejador_callback_setsprogreso(update, context):
    query  = update.callback_query
    partes = query.data.split("_")
    if len(partes) != 2:
        query.answer("Error en paginación", show_alert=True); return
    mostrar_setsprogreso(update, context, pagina=int(partes[1]), mensaje=query.message, editar=True)
    query.answer()

# ─── Callback ampliar vender ──────────────────────────────────────────────────
def callback_ampliar_vender(update, context):
    query    = update.callback_query
    id_unico = query.data.replace("ampliar_vender_", "")
    user_id  = query.from_user.id
    carta    = col_cartas_usuario.find_one({"user_id": user_id, "id_unico": id_unico})
    if not carta:
        query.answer("No tienes esa carta.", show_alert=True); return
    if col_mercado.find_one({"id_unico": id_unico}):
        query.answer("Esta carta ya está en el mercado.", show_alert=True); return

    estrellas = carta.get('estrellas', '★??')
    card_id   = carta.get('card_id', extraer_card_id_de_id_unico(id_unico))
    precio    = precio_carta_tabla(estrellas, card_id)

    col_cartas_usuario.delete_one({"user_id": user_id, "id_unico": id_unico})
    col_mercado.insert_one({
        "id_unico": id_unico, "vendedor_id": user_id,
        "nombre": carta['nombre'], "version": carta['version'],
        "grupo": carta.get('grupo', carta['version']),
        "estado": carta['estado'], "estrellas": estrellas,
        "precio": precio, "card_id": card_id,
        "fecha": datetime.utcnow(), "imagen": carta.get("imagen")
    })
    query.answer("Carta puesta en el mercado.", show_alert=True)
    query.edit_message_caption(caption="📦 Carta puesta en el mercado.", parse_mode='HTML')

# ─── Callbacks de tienda / mejora ────────────────────────────────────────────
def callback_comprarobj(update, context):
    query  = update.callback_query
    obj_id = query.data.replace("comprarobj_", "")
    user_id= query.from_user.id
    comprar_objeto(user_id, obj_id, context, query.message.chat_id,
                   lambda text, **kwargs: query.answer(text=text, show_alert=True))

def callback_comprarG_objeto(update, context):
    query  = update.callback_query
    obj_id = query.data.replace("comprarG_", "")
    obj    = CATALOGO_OBJETOSG.get(obj_id)
    if not obj or "precio_gemas" not in obj:
        query.answer("Objeto no válido.", show_alert=True); return
    user_id = query.from_user.id
    saldo   = (col_usuarios.find_one({"user_id": user_id}) or {}).get("gemas", 0)
    precio  = obj["precio_gemas"]
    if saldo < precio:
        query.answer("No tienes suficientes gemas.", show_alert=True); return
    col_usuarios.update_one({"user_id": user_id}, {"$inc": {"gemas": -precio, f"objetos.{obj_id}": 1}})
    query.answer(f"¡Compraste {obj['emoji']} {obj['nombre']} por {precio} gemas!", show_alert=True)

def callback_mejorar_carta(update, context):
    query   = update.callback_query
    user_id = query.from_user.id
    id_unico= query.data.split("_", 1)[1]
    carta   = col_cartas_usuario.find_one({"user_id": user_id, "id_unico": id_unico})
    if not carta:
        query.answer("No tienes esa carta.", show_alert=True); return
    lightsticks = (col_usuarios.find_one({"user_id": user_id}) or {}).get("objetos", {}).get("lightstick", 0)
    if lightsticks < 1:
        query.answer("No tienes ningún Lightstick.", show_alert=True); return
    mejoras = {"☆☆☆": ("★☆☆", 1.00), "★☆☆": ("★★☆", 0.70), "★★☆": ("★★★", 0.40), "★★★": (None, 0.00)}
    est_actual = carta.get("estrellas", "")
    if est_actual not in mejoras or mejoras[est_actual][0] is None:
        query.answer("No se puede mejorar más.", show_alert=True); return
    est_nuevo, prob = mejoras[est_actual]
    texto = (
        f"Vas a usar 1 💡 Lightstick:\n<b>{carta.get('nombre','')} [{carta.get('version','')}]</b>\n"
        f"Estado actual: <b>{est_actual}</b>\nPosibilidad: <b>{int(prob*100)}%</b>\n\n¿Continuar?"
    )
    botones = [[
        InlineKeyboardButton("✅ Mejorar",  callback_data=f"confirmamejora_{id_unico}"),
        InlineKeyboardButton("❌ Cancelar", callback_data="cancelarmejora")
    ]]
    query.edit_message_text(texto, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(botones))
    query.answer()

def callback_confirmar_mejora(update, context):
    query   = update.callback_query
    user_id = query.from_user.id
    data    = query.data

    if data.startswith("confirmamejora_"):
        id_unico = data.split("_", 1)[1]
        carta    = col_cartas_usuario.find_one({"user_id": user_id, "id_unico": id_unico})
        if not carta:
            query.answer("No tienes esa carta.", show_alert=True); return
        lightsticks = (col_usuarios.find_one({"user_id": user_id}) or {}).get("objetos", {}).get("lightstick", 0)
        if lightsticks < 1:
            query.answer("No tienes ningún Lightstick.", show_alert=True); return
        mejoras    = {"☆☆☆": ("★☆☆", 1.00), "★☆☆": ("★★☆", 0.70), "★★☆": ("★★★", 0.40)}
        est_actual = carta.get("estrellas", "")
        if est_actual not in mejoras:
            query.answer("No puede mejorar.", show_alert=True); return
        est_nuevo, prob = mejoras[est_actual]
        exitosa = random.random() < prob

        if exitosa:
            carta_nueva = next((c for c in cartas if c["nombre"] == carta.get("nombre") and c["version"] == carta.get("version") and c.get("estado_estrella", "") == est_nuevo), None)
            nuevo_estado = carta_nueva.get("estado", carta.get("estado")) if carta_nueva else carta.get("estado")
            nueva_imagen = carta_nueva.get("imagen", carta.get("imagen")) if carta_nueva else carta.get("imagen")
            col_cartas_usuario.update_one(
                {"user_id": user_id, "id_unico": id_unico},
                {"$set": {"estrellas": est_nuevo, "estado": nuevo_estado, "imagen": nueva_imagen}}
            )
            resultado = f"¡Éxito! Tu carta ahora es <b>{est_nuevo}</b> — <b>{nuevo_estado}</b>."
        else:
            resultado = "Fallaste el intento. La carta se mantiene igual."

        col_usuarios.update_one({"user_id": user_id}, {"$inc": {"objetos.lightstick": -1}})
        query.edit_message_text(resultado, parse_mode="HTML")
        query.answer("¡Listo!")

    elif data == "cancelarmejora":
        query.edit_message_text("Operación cancelada.")
        query.answer("Cancelado.")

# ─── Callback mercado (única definición) ─────────────────────────────────────
def manejador_callback_mercado(update, context):
    from telegram.error import RetryAfter, BadRequest
    query   = update.callback_query
    data    = query.data
    user_id = query.from_user.id
    partes  = data.split("_")

    # Validar dueño
    try:
        dueño_id = next((int(p) for p in partes if p.isdigit() and len(p) >= 5), None)
    except Exception:
        dueño_id = None
    if dueño_id and user_id != dueño_id:
        query.answer("Solo puedes interactuar con tu propio mercado.", show_alert=True); return

    def get_thread():
        return int(partes[-1]) if partes[-1].isdigit() else None

    if data.startswith("mercado_filtros_"):
        uid = int(partes[2]); pag = int(partes[3])
        try: query.edit_message_reply_markup(reply_markup=mostrar_menu_filtros(uid, pag))
        except RetryAfter as e: query.answer(f"⏳ {int(e.retry_after)}s", show_alert=True)
        return

    if data.startswith("mercado_filtro_estado_"):
        uid = int(partes[3]); pag = int(partes[4])
        try: query.edit_message_reply_markup(reply_markup=mostrar_menu_estrellas(uid, pag))
        except RetryAfter as e: query.answer(f"⏳ {int(e.retry_after)}s", show_alert=True)
        return

    if data.startswith("mercado_filtraestrella_"):
        uid = int(partes[2]); pag = int(partes[3]); est = partes[4]; t = get_thread()
        try: mostrar_mercado_pagina(query.message.chat_id, query.message.message_id, context, uid, int(pag), filtro="estrellas", valor_filtro=est, thread_id=t)
        except RetryAfter as e: query.answer(f"⏳ {int(e.retry_after)}s", show_alert=True)
        return

    if data.startswith("mercado_filtro_grupo_"):
        uid = int(partes[-3]); pag = int(partes[-2])
        grupos = obtener_grupos_del_mercado()
        try: query.edit_message_reply_markup(reply_markup=mostrar_menu_grupos(uid, pag, grupos))
        except RetryAfter as e: query.answer(f"⏳ {int(e.retry_after)}s", show_alert=True)
        return

    if data.startswith("mercado_filtragrupo_"):
        uid = int(partes[2]); pag = int(partes[3]); grupo = urllib.parse.unquote_plus(partes[4]); t = get_thread()
        try: mostrar_mercado_pagina(query.message.chat_id, query.message.message_id, context, uid, int(pag), filtro="grupo", valor_filtro=grupo, thread_id=t)
        except RetryAfter as e: query.answer(f"⏳ {int(e.retry_after)}s", show_alert=True)
        return

    if data.startswith("mercado_filtro_numero_"):
        uid = int(partes[3]); pag = int(partes[4])
        try: query.edit_message_reply_markup(reply_markup=mostrar_menu_ordenar(uid, pag))
        except RetryAfter as e: query.answer(f"⏳ {int(e.retry_after)}s", show_alert=True)
        return

    if data.startswith("mercado_ordennum_"):
        uid = int(partes[2]); pag = int(partes[3]); orden = partes[4]; t = get_thread()
        try: mostrar_mercado_pagina(query.message.chat_id, query.message.message_id, context, uid, int(pag), orden=orden, thread_id=t)
        except RetryAfter as e: query.answer(f"⏳ {int(e.retry_after)}s", show_alert=True)
        return

    if data.startswith("mercado_pagina_"):
        uid = int(partes[2]); pag = int(partes[3])
        filtro       = partes[4] if partes[4] != "none" else None
        valor_filtro = partes[5] if partes[5] != "none" else None
        orden        = partes[6] if len(partes) > 6 and partes[6] != "none" else None
        t = get_thread()
        try: mostrar_mercado_pagina(query.message.chat_id, query.message.message_id, context, uid, int(pag), filtro=filtro, valor_filtro=valor_filtro, orden=orden, thread_id=t)
        except RetryAfter as e: query.answer(f"⏳ {int(e.retry_after)}s", show_alert=True)
        return

# ─── /setsprogreso / /set comandos ───────────────────────────────────────────
@log_command
@solo_en_tema_asignado("setsprogreso")
def comando_setsprogreso(update, context):
    thread_id = getattr(update.message, "message_thread_id", None)
    mostrar_setsprogreso(update, context, pagina=1, thread_id=thread_id)

# ─── /apodo ───────────────────────────────────────────────────────────────────
@log_command
@solo_en_tema_asignado("apodo")
@cooldown_critico
def comando_apodo(update, context):
    user_id = update.message.from_user.id
    if len(context.args) < 2:
        update.message.reply_text('Usa: /apodo <id_unico> "apodo"'); return
    id_unico = context.args[0].strip()
    apodo    = " ".join(context.args[1:]).strip('"').strip()
    if not (1 <= len(apodo) <= 8):
        update.message.reply_text("El apodo debe tener entre 1 y 8 caracteres."); return
    carta = col_cartas_usuario.find_one({"user_id": user_id, "id_unico": id_unico})
    if not carta:
        update.message.reply_text("No encontré esa carta."); return
    doc_usuario = col_usuarios.find_one({"user_id": user_id}) or {}
    if doc_usuario.get("objetos", {}).get("ticket_agregar_apodo", 0) < 1:
        update.message.reply_text("No tienes tickets para agregar apodos."); return
    col_usuarios.update_one({"user_id": user_id}, {"$inc": {"objetos.ticket_agregar_apodo": -1}})
    col_cartas_usuario.update_one({"user_id": user_id, "id_unico": id_unico}, {"$set": {"apodo": apodo}})
    update.message.reply_text(f'✅ Apodo <b>"{apodo}"</b> asignado a <code>{id_unico}</code>.', parse_mode="HTML")

# ─── Regalo ───────────────────────────────────────────────────────────────────
def handler_regalo_respuesta(update, context):
    if not getattr(update, "message", None) or not getattr(update.message, "text", None):
        return
    user_id = update.message.from_user.id
    if user_id not in SESIONES_REGALO: return
    data    = SESIONES_REGALO[user_id]
    carta   = data["carta"]
    destino = update.message.text.strip()

    if destino.lower().strip() == "cancelar":
        update.message.reply_text("❌ Regalo cancelado.")
        del SESIONES_REGALO[user_id]; return

    target_user_id = None
    if destino.startswith('@'):
        posible = col_usuarios.find_one({"username": destino[1:].lower()})
        if posible: target_user_id = posible["user_id"]
    else:
        try: target_user_id = int(destino)
        except Exception: pass

    if not target_user_id:
        update.message.reply_text("❌ No pude identificar al usuario.")
        del SESIONES_REGALO[user_id]; return
    if user_id == target_user_id:
        update.message.reply_text("No puedes regalarte cartas a ti mismo.")
        del SESIONES_REGALO[user_id]; return

    res = col_cartas_usuario.delete_one({"user_id": user_id, "id_unico": carta["id_unico"]})
    if res.deleted_count == 0:
        update.message.reply_text("Parece que ya no tienes esa carta.")
        del SESIONES_REGALO[user_id]; return

    carta["user_id"] = target_user_id
    col_cartas_usuario.insert_one(carta)
    update.message.reply_text(f"🎁 ¡Carta [{carta['id_unico']}] enviada correctamente!")
    try:
        context.bot.send_message(
            chat_id=target_user_id,
            text=f"🎉 ¡Recibiste la carta <b>{carta['id_unico']}</b> ({carta['nombre']} [{carta['version']}])!\nRevisa tu /album.",
            parse_mode='HTML'
        )
    except Exception: pass
    del SESIONES_REGALO[user_id]

# ─── Registro de handlers ─────────────────────────────────────────────────────
dispatcher.add_handler(CallbackQueryHandler(callback_kkp_notify,      pattern="^kkp_notify_"))
dispatcher.add_handler(CallbackQueryHandler(callback_sorteo_participar,pattern=r"^sorteopart_"))
dispatcher.add_handler(CallbackQueryHandler(callback_help,             pattern=r"^help_"))
dispatcher.add_handler(CallbackQueryHandler(callback_invitamenu,       pattern="^(menu_invitacion|menu_progress)$"))
dispatcher.add_handler(CallbackQueryHandler(manejador_callback_album,  pattern="^album_"))
dispatcher.add_handler(CallbackQueryHandler(manejador_reclamar,        pattern="^reclamar_"))
dispatcher.add_handler(CallbackQueryHandler(callback_comprarobj,       pattern="^comprarobj_"))
dispatcher.add_handler(CallbackQueryHandler(callback_comprarG_objeto,  pattern="^comprarG_"))
dispatcher.add_handler(CallbackQueryHandler(callback_ampliar_vender,   pattern="^ampliar_vender_"))
dispatcher.add_handler(CallbackQueryHandler(callback_mejorar_carta,    pattern="^mejorar_"))
dispatcher.add_handler(CallbackQueryHandler(callback_confirmar_mejora, pattern="^(confirmamejora_|cancelarmejora)"))
dispatcher.add_handler(CallbackQueryHandler(manejador_callback_setlist,     pattern=r"^setlist_"))
dispatcher.add_handler(CallbackQueryHandler(manejador_callback_setsprogreso,pattern=r"^setsprogreso_"))
dispatcher.add_handler(CallbackQueryHandler(manejador_callback_setdet,      pattern=r"^setdet\|"))
dispatcher.add_handler(CallbackQueryHandler(manejador_callback_mercado,     pattern="^mercado_"))
dispatcher.add_handler(CallbackQueryHandler(manejador_tienda_paypal,        pattern=r"^tienda_paypal_"))

# ─── Comandos ─────────────────────────────────────────────────────────────────
dispatcher.add_handler(CommandHandler("start",                 mensaje_tutorial_privado))
dispatcher.add_handler(CommandHandler("help",                  comando_help))
dispatcher.add_handler(CommandHandler('settema',               comando_settema))
dispatcher.add_handler(CommandHandler('removetema',            comando_removetema))
dispatcher.add_handler(CommandHandler('vertemas',              comando_vertemas))
dispatcher.add_handler(CommandHandler('kkp',                   comando_kkp))
dispatcher.add_handler(CommandHandler('sorteo',                comando_sorteo))
dispatcher.add_handler(CommandHandler('topicid',               comando_topicid))
dispatcher.add_handler(CommandHandler('mercado',               comando_mercado))
dispatcher.add_handler(CommandHandler('rankingmercado',        comando_rankingmercado))
dispatcher.add_handler(CommandHandler('tiendagemas',           tienda_gemas))
dispatcher.add_handler(CommandHandler('darGemas',              comando_darGemas))
dispatcher.add_handler(CommandHandler('gemas',                 comando_gemas))
dispatcher.add_handler(CommandHandler('estadisticasdrops',     comando_estadisticasdrops))
dispatcher.add_handler(CommandHandler("estadisticasdrops_semanal", comando_estadisticasdrops_semanal))
dispatcher.add_handler(CommandHandler('usar',                  comando_usar))
dispatcher.add_handler(CommandHandler('apodo',                 comando_apodo))
dispatcher.add_handler(CommandHandler('inventario',            comando_inventario))
dispatcher.add_handler(CommandHandler('tienda',                comando_tienda))
dispatcher.add_handler(CommandHandler("tiendaG",               comando_tiendaG))
dispatcher.add_handler(CommandHandler('comprarobjeto',         comando_comprarobjeto))
dispatcher.add_handler(CommandHandler('idolday',               comando_idolday))
dispatcher.add_handler(CommandHandler('album',                 comando_album))
dispatcher.add_handler(CommandHandler('album2',                comando_album2))
dispatcher.add_handler(CommandHandler('darobjeto',             comando_darobjeto))
dispatcher.add_handler(CommandHandler('miid',                  comando_miid))
dispatcher.add_handler(CommandHandler('bonoidolday',           comando_bonoidolday))
dispatcher.add_handler(CommandHandler('comandos',              comando_comandos))
dispatcher.add_handler(CommandHandler('trk',                   comando_trk))
dispatcher.add_handler(CommandHandler('giveidol',              comando_giveidol))
dispatcher.add_handler(CommandHandler('setsprogreso',          comando_setsprogreso))
dispatcher.add_handler(CommandHandler('set',                   comando_set_detalle))
dispatcher.add_handler(CommandHandler('ampliar',               comando_ampliar))
dispatcher.add_handler(CommandHandler('kponey',                comando_saldo))
dispatcher.add_handler(CommandHandler('darKponey',             comando_darKponey))
dispatcher.add_handler(CommandHandler('fav',                   comando_fav))
dispatcher.add_handler(CommandHandler('favoritos',             comando_favoritos))
dispatcher.add_handler(CommandHandler('precio',                comando_precio))
dispatcher.add_handler(CommandHandler('vender',                comando_vender))
dispatcher.add_handler(CommandHandler('comprar',               comando_comprar))
dispatcher.add_handler(CommandHandler('retirar',               comando_retirar))
dispatcher.add_handler(CommandHandler('mejorar',               comando_mejorar))

dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, mensaje_trade_id))
dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handler_regalo_respuesta))
dispatcher.add_handler(MessageHandler(Filters.all, borrar_mensajes_no_idolday), group=99)

# ─── Arranque ─────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def home():
    return "Bot activo."

@app.route(f"/{TOKEN}", methods=["POST"])
def telegram_webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    dispatcher.process_update(update)
    return "OK"

@app.before_first_request
def on_startup():
    """Se ejecuta una sola vez cuando Gunicorn recibe la primera request."""
    # Registrar webhook con Telegram
    webhook_url = f"https://karuidol.onrender.com/{TOKEN}"
    try:
        bot.set_webhook(url=webhook_url)
        logger.info(f"[startup] Webhook registrado: {webhook_url}")
    except Exception as e:
        logger.error(f"[startup] Error registrando webhook: {e}")

    # Iniciar proceso de sorteos en background
    iniciar_proceso_sorteos(dispatcher)
    logger.info("[startup] Bot iniciado correctamente.")

if __name__ == '__main__':
    puerto = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=puerto)
