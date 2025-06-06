import os
import threading
import time
from flask import Flask, request, jsonify, redirect
from telegram.error import BadRequest
from telegram import (
    Bot,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.ext import Dispatcher, CommandHandler, CallbackQueryHandler
import json
import random
from datetime import datetime, timedelta
from pymongo import MongoClient
from dotenv import load_dotenv
import re
import string
import math
from PIL import Image, ImageDraw, ImageFont
import requests
from io import BytesIO

load_dotenv()

TOKEN = os.getenv('TELEGRAM_TOKEN')
if not TOKEN:
    raise ValueError("No se encontró el token de Telegram")
MONGO_URI = os.getenv('MONGO_URI')
if not MONGO_URI:
    raise ValueError("No se encontró la URI de MongoDB")

app = Flask(__name__)

bot = Bot(TOKEN)
dispatcher = Dispatcher(bot, None, use_context=True)

primer_mensaje = True

# MongoDB setup
client = MongoClient(MONGO_URI)
db = client['karuta_bot']
col_usuarios = db['usuarios']
col_cartas_usuario = db['cartas_usuario']
col_contadores = db['contadores']
col_mercado = db['mercado_cartas']
col_mercado.create_index("id_unico", unique=True)
col_cartas_usuario.create_index("id_unico", unique=True)
col_cartas_usuario.create_index("user_id")
col_mercado.create_index("vendedor_id")
col_usuarios.create_index("user_id", unique=True)
# TTL para cartas en mercado (ejemplo: 7 días)
from pymongo import ASCENDING
col_mercado.create_index(
    [("fecha", ASCENDING)],
    expireAfterSeconds=7*24*60*60  # 7 días
)

# --- Cooldowns ---
COOLDOWN_USUARIO_SEG = 6 * 60 * 60  # 6 horas en segundos
COOLDOWN_GRUPO_SEG = 30             # 30 segundos global por grupo
COOLDOWN_GRUPO = {}                 # Guarda el timestamp del último drop por grupo

# Cargar cartas.json
if not os.path.isfile('cartas.json'):
    raise ValueError("No se encontró el archivo cartas.json")
with open('cartas.json', 'r') as f:
    cartas = json.load(f)

SESIONES_REGALO = {}

DROPS_ACTIVOS = {}

# Estados de carta
ESTADOS_CARTA = [
    ("Excelente", "★★★"),
    ("Buen estado", "★★☆"),
    ("Mal estado", "★☆☆"),
    ("Muy mal estado", "☆☆☆")
]
ESTADO_LISTA = ["Excelente", "Buen estado", "Mal estado", "Muy mal estado"]

#------Precio de cartas-------
BASE_PRICE = 250
RAREZA = 5000

ESTADO_MULTIPLICADORES = {
    "Excelente estado": 1.0,
    "Buen estado": 0.4,
    "Mal estado": 0.15,
    "Muy mal estado": 0.05
}
#---------------------------
user_last_cmd = {}
group_last_cmd = {}

COOLDOWN_USER = 3    # 3 segundos mínimo entre comandos por usuario
COOLDOWN_GROUP = 1   # 1 segundo mínimo entre comandos por grupo


#----------PAYPALAPP-------------------


# Pon aquí tus credenciales de PayPal sandbox
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET")

app = Flask(__name__)

# ==== Helper: Obtener token de acceso OAuth2 de PayPal ====
def get_paypal_token():
    url = "https://api-m.paypal.com/v1/oauth2/token"
    resp = requests.post(url, auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET), data={"grant_type": "client_credentials"})
    resp.raise_for_status()
    return resp.json()["access_token"]

# ==== Endpoint para crear una orden de pago ====
@app.route("/paypal/create_order", methods=["POST"])
def create_order():
    data = request.json
    user_id = data["user_id"]
    pack_gemas = data["pack"]  # Ej: "x100"
    amount = data["amount"]    # Ej: 1.99

    access_token = get_paypal_token()
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

    order_data = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": f"user_{user_id}_{pack_gemas}",
            "amount": {"currency_code": "USD", "value": str(amount)},
            "custom_id": str(user_id)  # Así asocias el pago al user_id de Telegram
        }],
        "application_context": {
            "return_url": "https://karuidol.onrender.com/paypal/return",   # Cambia por tu url
            "cancel_url": "https://karuidol.onrender.com/paypal/cancel"    # Cambia por tu url
        }
    }

    # AQUÍ EL ENDPOINT DE PRODUCCIÓN (NO SANDBOX)
    resp = requests.post("https://api-m.paypal.com/v2/checkout/orders", headers=headers, json=order_data)
    resp.raise_for_status()
    order = resp.json()
    # Devuelve el link para redirigir al usuario a PayPal
    for link in order["links"]:
        if link["rel"] == "approve":
            return jsonify({"url": link["href"], "order_id": order["id"]})
    return "No approve link", 400

# ==== Endpoint para el webhook de PayPal (tienes que registrarlo en developer.paypal.com) ====


# --- Configuración ---
ADMIN_USER_ID = 1111798714  # <-- Cambia por tu user_id real de Telegram

# --- WEBHOOK PAYPAL: Suma gemas, guarda historial y notifica usuario y admin ---
@app.route("/paypal/webhook", methods=["POST"])
def paypal_webhook():
    data = request.json
    print("Webhook recibido:", data)

    if data.get("event_type") == "PAYMENT.CAPTURE.COMPLETED":
        resource = data["resource"]
        try:
            # 1. Extraer user_id (custom_id) y monto
            user_id = int(resource["custom_id"])
            amount = resource["amount"]["value"]
            pago_id = resource.get("id")  # ID único del pago

            # 2. Mapear monto a gemas (ajusta según tus precios reales)
            gemas_por_monto = {
                "1.00": 50,
                "2.00": 100,
                "8.00": 500,
                "13.00": 1000,
                "60.00": 5000,
                "100.00": 10000
            }
            cantidad_gemas = gemas_por_monto.get(str(amount))
            if not cantidad_gemas:
                print(f"❌ Monto no reconocido: {amount} USD")
                return "", 200

            # 3. Previene doble entrega
            if db.historial_compras_gemas.find_one({"pago_id": pago_id}):
                print("Ya entregado previamente.")
                return "", 200

            # 4. Suma gemas
            col_usuarios.update_one(
                {"user_id": user_id},
                {"$inc": {"gemas": cantidad_gemas}},
                upsert=True
            )

            # 5. Guarda historial
            db.historial_compras_gemas.insert_one({
                "pago_id": pago_id,
                "user_id": user_id,
                "cantidad_gemas": cantidad_gemas,
                "monto_usd": amount,
                "fecha": datetime.utcnow()
            })

            # 6. Notifica al usuario
            try:
                bot.send_message(
                    chat_id=user_id,
                    text=f"🎉 ¡Compra confirmada! Has recibido {cantidad_gemas} gemas en KaruKpop.\n¡Gracias por tu apoyo! 💎"
                )
            except Exception as e:
                print("No se pudo notificar al usuario:", e)

            # 7. Notifica al admin
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

# --- ENDPOINT DE RETORNO DESPUÉS DE PAGAR ---
@app.route("/paypal/return")
def paypal_return():
    return "¡Gracias por tu compra! Puedes volver a Telegram."

@app.route("/paypal/cancel")
def paypal_cancel():
    return "Pago cancelado."










#-----------------------------------------
def check_cooldown(update):
    now = time.time()
    uid = update.effective_user.id
    gid = update.effective_chat.id
    # Por usuario
    if uid in user_last_cmd and now - user_last_cmd[uid] < COOLDOWN_USER:
        return False, f"¡Espera {COOLDOWN_USER} segundos entre comandos!"
    # Por grupo
    if gid in group_last_cmd and now - group_last_cmd[gid] < COOLDOWN_GROUP:
        return False, f"Este grupo está usando comandos muy rápido. Espera 1 segundo."
    return True, None

def cooldown_critico(func):
    def wrapper(update, context, *args, **kwargs):
        ok, msg = check_cooldown(update)
        if not ok:
            update.message.reply_text(msg)
            return
        # SOLO AQUÍ actualiza el timestamp cuando el comando pasa
        now = time.time()
        uid = update.effective_user.id
        gid = update.effective_chat.id
        user_last_cmd[uid] = now
        group_last_cmd[gid] = now
        return func(update, context, *args, **kwargs)
    return wrapper

def agregar_numero_a_imagen(imagen_url, numero):
    import requests
    from PIL import Image, ImageDraw, ImageFont
    from io import BytesIO
    # Descargar imagen original
    response = requests.get(imagen_url)
    img = Image.open(BytesIO(response.content)).convert("RGBA")
    draw = ImageDraw.Draw(img)

# Elige una fuente pequeña y legible
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    font_size = int(img.height * 0.02)   # 5% de la altura de la carta (ajusta si lo quieres más pequeño)
    font = ImageFont.truetype(font_path, size=font_size)

    texto = f"#{numero}"

# Usa textbbox para medir el texto correctamente
    bbox = draw.textbbox((0, 0), texto, font=font)
    text_width, text_height = bbox[2] - bbox[0], bbox[3] - bbox[1]

    x = (img.width - text_width) // 2
    margen = 8  # Separación del borde inferior
    y = img.height - text_height - margen

# Sombra para mejor contraste
    sombra_offset = 2
    draw.text((x + sombra_offset, y + sombra_offset), texto, font=font, fill="black")
    draw.text((x, y), texto, font=font, fill="white")
# Fondo negro semitransparente para que se vea en cualquier imagen
    draw.rectangle([x-6, y-4, x-6+text_width+14, y-4+text_height+8], fill=(0,0,0,170))
    draw.text((x, y), texto, font=font, fill=(255,255,255,255))
# Guarda el resultado temporalmente
    output = BytesIO()
    img.save(output, format="PNG")
    output.seek(0)
    return output


CATALOGO_OBJETOS = {
    "bono_idolday": {
        "nombre": "Bono Idolday",
        "emoji": "🎟️",
        "desc": (
            "Permite hacer un /idolday adicional sin esperar el cooldown.\n"
            "Uso: /idolday si tienes bonos."
        ),
        "precio": 1200
    },
    "ticket_intercambio": {
        "nombre": "Ticket de Intercambio",
        "emoji": "🎫",
        "desc": (
            "Requerido para hacer un trade/intercambio de cartas.\n"
            "Se consume al usar /trade."
        ),
        "precio": 15000
    },
    "lightstick": {
        "nombre": "Lightstick",
        "emoji": "💡",
        "desc": (
            "Mejora el estado de una carta:\n"
            "• ☆☆☆ → ★☆☆: 100% de posibilidad\n"
            "• ★☆☆ → ★★☆: 70% de posibilidad\n"
            "• ★★☆ → ★★★: 40% de posibilidad\n"
            "• ★★★: No se puede mejorar más"
        ),
        "precio": 4000
    },
    "ticket_agregar_apodo": {
        "nombre": "Ticket Agregar Apodo",
        "emoji": "🏷️",
        "desc": (
            'Permite agregar un apodo personalizado a una carta usando /apodo <code>id_unico</code> "apodo"\n'
            'Máx 8 caracteres. Ejemplo: /apodo fghj7 "Mi bebe"'
        ),
        "precio": 1800
    },
    "abrazo_de_bias": {
        "nombre": "Abrazo de Bias",
        "emoji": "🤗",
        "desc": (
            "Reduce el cooldown de /idolday a la mitad, una vez.\n"
            "Uso: Cuando tengas cooldown, gasta 1 para reducir la espera."
        ),
        "precio": 600
    }
}



#--------------------------------------------------------------

def extraer_card_id_de_id_unico(id_unico):
    """
    Extrae el número de carta (card_id) del id_unico que termina con el número después de los 4 primeros caracteres.
    Ej: 'abcd1' -> 1, 'gh4h55' -> 55, '0asd100' -> 100
    """
    if id_unico and len(id_unico) > 4:
        try:
            return int(id_unico[4:])
        except:
            return None
    return None


def revisar_sets_completados(user_id, context):
    """
    Revisa si el usuario completó algún set y entrega premios proporcionales,
    enviando la alerta SOLO por privado.
    """
    sets = obtener_sets_disponibles()
    cartas_usuario = list(col_cartas_usuario.find({"user_id": user_id}))
    cartas_usuario_unicas = set((c["nombre"], c["version"]) for c in cartas_usuario)

    doc_usuario = col_usuarios.find_one({"user_id": user_id}) or {}
    sets_premiados = set(doc_usuario.get("sets_premiados", []))

    premios = []
    for s in sets:
        cartas_set_unicas = set((c["nombre"], c["version"]) for c in cartas if (c.get("set") == s or c.get("grupo") == s))
        if cartas_set_unicas and cartas_set_unicas.issubset(cartas_usuario_unicas) and s not in sets_premiados:
            monto = 500 * len(cartas_set_unicas)  # Puedes ajustar este factor
            premios.append((s, monto))
            sets_premiados.add(s)
            col_usuarios.update_one(
                {"user_id": user_id},
                {
                    "$inc": {"kponey": monto},
                    "$set": {"sets_premiados": list(sets_premiados)}
                },
                upsert=True
            )
            # ALERTA PRIVADA:
            try:
                context.bot.send_message(
                    chat_id=user_id,
                    text=f"🎉 ¡Completaste el set <b>{s}</b>!\nPremio: <b>+{monto} Kponey 🪙</b>",
                    parse_mode="HTML"
                )
            except Exception:
                pass  # usuario bloqueó el bot, etc.
    return premios



# Packs de gemas y links base
# Diccionario con los packs y sus datos
PACKS_GEMAS = [
    {"pack": "x50", "amount": 1.00, "label": "💎 x50 Gems (USD $1)"},
    {"pack": "x100", "amount": 2.00, "label": "💎 x100 Gems (USD $2)"},
    {"pack": "x500", "amount": 8.00, "label": "💎 x500 Gems (USD $8)"},
    {"pack": "x1000", "amount": 13.00, "label": "💎 x1000 Gems (USD $13)"},
    {"pack": "x5000", "amount": 60.00, "label": "💎 x5000 Gems (USD $60)"},
    {"pack": "x10000", "amount": 100.00, "label": "💎 x10000 Gems (USD $100)"},
]

# FUNCION DE TIENDA DE GEMAS
def tienda_gemas(update, context):
    user_id = update.message.from_user.id

    texto = (
        "💎 <b>Tienda de Gemas KaruKpop</b>\n\n"
        "Compra gemas de forma segura con PayPal. Las gemas se agregan automáticamente.\n\n"
        "Elige el pack que deseas comprar:"
    )
    botones = []
    for pack in PACKS_GEMAS:
        # El callback_data lleva la info del pack (ej: tienda_paypal_x100_2.00)
        botones.append([
            InlineKeyboardButton(
                pack["label"],
                callback_data=f"tienda_paypal_{pack['pack']}_{pack['amount']}"
            )
        ])
    teclado = InlineKeyboardMarkup(botones)
    update.message.reply_text(texto, parse_mode="HTML", reply_markup=teclado)


ADMIN_USER_ID = 1111798714  # <--- Reemplaza por tu propio ID

def historial_gemas_admin(update, context):
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        update.message.reply_text("No tienes permiso para usar este comando.")
        return

    if len(context.args) == 0:
        update.message.reply_text("Usa: /historialgemas <@username o id_usuario>")
        return

    arg = context.args[0]
    query = {}
    if arg.startswith("@"):
        username = arg[1:].lower()
        query["username"] = username
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

    msg = f"🧾 *Historial de gemas para {'@'+compras[0].get('username','?') if 'username' in compras[0] else compras[0].get('user_id','?')}:*\n\n"
    for c in compras:
        fecha = c['fecha'].strftime("%d/%m/%Y %H:%M")
        item = c.get("item_name", "")
        cantidad = c.get("cantidad_gemas", "?")
        msg += f"- {cantidad} gemas ({item}) el {fecha}\n"
    update.message.reply_text(msg, parse_mode="Markdown")

dispatcher.add_handler(CommandHandler("historialgemas", historial_gemas_admin))


def manejador_tienda_paypal(update, context):
    query = update.callback_query
    data = query.data  # tienda_paypal_x100_2.00
    user_id = query.from_user.id

    _, _, pack, amount = data.split("_")
    amount = float(amount)

    # Llama a tu backend para crear la orden de PayPal
    import requests
    try:
        resp = requests.post(
            "https://karuidol.onrender.com/paypal/create_order",
            json={
                "user_id": user_id,
                "pack": pack,
                "amount": amount
            },
            timeout=10
        )
        if resp.ok:
            url = resp.json().get("url")
            if url:
                query.answer()
                query.edit_message_text(
                    f"🔗 Haz clic aquí para pagar tu pack de gemas:\n\n<a href='{url}'>Pagar con PayPal</a>\n\n"
                    "Cuando el pago esté confirmado recibirás las gemas automáticamente.",
                    parse_mode="HTML", disable_web_page_preview=True
                )
            else:
                query.answer("No se pudo generar el enlace de pago.", show_alert=True)
        else:
            query.answer("Error al conectar con PayPal.", show_alert=True)
    except Exception as e:
        query.answer("Fallo al generar enlace de pago.", show_alert=True)












def obtener_grupos_del_mercado():
    # Devuelve una lista ORDENADA de todos los grupos únicos en el mercado
    return sorted({c.get("grupo", "") for c in col_mercado.find() if c.get("grupo")})



def precio_carta_karuta(nombre, version, estado, id_unico=None, card_id=None):
    """
    Calcula el precio de una carta al estilo Karuta (Discord):
    Solo depende del número de carta (print), no importa el estado ni el total de copias.
    Si en el futuro agregas rarezas (versiones), aquí puedes multiplicar el precio base.
    """
    # Determina card_id
    if card_id is None and id_unico:
        card_id = extraer_card_id_de_id_unico(id_unico)

    # SOLO versión común (V1)
    precio_base = 0
    if card_id == 1:
        precio_base = 12000
    elif card_id == 2:
        precio_base = 7000
    elif card_id == 3:
        precio_base = 4500
    elif card_id == 4:
        precio_base = 3000
    elif card_id == 5:
        precio_base = 2250
    elif 6 <= card_id <= 10:
        precio_base = 1500
    elif 11 <= card_id <= 100:
        precio_base = 600
    else:
        precio_base = 500

    # Si más adelante agregas versiones raras, aplica aquí:
    # if version == "V2":
    #     precio_base *= 2
    # elif version == "V3":
    #     precio_base *= 4
    # ... (etc)

    return precio_base



def random_id_unico(card_id):
    # 4 letras/números aleatorios + el id de carta (card_id)
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

def es_admin(update):
    chat = update.effective_chat
    user_id = update.effective_user.id
    if chat.type not in ["group", "supergroup"]:
        return False
    try:
        member = bot.get_chat_member(chat.id, user_id)
        return member.status in ("administrator", "creator")
    except:
        return False

def puede_usar_idolday(user_id):
    user_doc = col_usuarios.find_one({"user_id": user_id}) or {}
    bono = user_doc.get('bono', 0)
    objetos = user_doc.get('objetos', {})
    bonos_inventario = objetos.get('bono_idolday', 0)
    last = user_doc.get('last_idolday')
    ahora = datetime.utcnow()
    cooldown_listo = False
    bono_listo = False

    if last:
        diferencia = ahora - last
        cooldown_listo = diferencia.total_seconds() >= 6 * 3600  # 6 horas
    else:
        cooldown_listo = True

    # Hay bono por admin o por inventario
    if (bono and bono > 0) or (bonos_inventario and bonos_inventario > 0):
        bono_listo = True

    return cooldown_listo, bono_listo


def desbloquear_drop(drop_id):
    # Espera 30 segundos para bloquear el drop (puedes cambiar el tiempo si quieres)
    data = DROPS_ACTIVOS.get(drop_id)
    if not data or data.get("expirado"):
        return
    tiempo_inicio = data["inicio"]
    while True:
        ahora = time.time()
        elapsed = ahora - tiempo_inicio
        if elapsed >= 60:
            expira_drop(drop_id)
            break
        time.sleep(1)

def expira_drop(drop_id):
    drop = DROPS_ACTIVOS.get(drop_id)
    if not drop or drop.get("expirado"):
        return
    keyboard = [
        [
            InlineKeyboardButton("❌", callback_data="expirado", disabled=True),
            InlineKeyboardButton("❌", callback_data="expirado", disabled=True),
        ]
    ]
    try:
        bot.edit_message_reply_markup(
            chat_id=drop["chat_id"],
            message_id=drop["mensaje_id"],
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception:
        pass
    drop["expirado"] = True
    
def carta_estado(nombre, version, estado):
    for c in cartas:
        if c['nombre'] == nombre and c['version'] == version and c.get('estado') == estado:
            return c
    return None

def estados_disponibles_para_carta(nombre, version):
    # Devuelve todos los estados disponibles para esa carta (puede ser varios estados: Excelente, Buen estado, etc)
    return [c for c in cartas if c['nombre'] == nombre and c['version'] == version]

# -- IDOLDAY DROP 2 CARTAS (Drop siempre muestra excelente estado, pero al reclamar puede variar) ---
def comando_idolday(update, context):
    user_id = update.message.from_user.id
    chat_id = update.effective_chat.id
    ahora = datetime.utcnow()
    ahora_ts = time.time()
    user_doc = col_usuarios.find_one({"user_id": user_id}) or {}
    bono = user_doc.get('bono', 0)
    last = user_doc.get('last_idolday')
    puede_tirar = False

    # --- Cooldown global por grupo (30 seg) ---
    ultimo_drop = COOLDOWN_GRUPO.get(chat_id, 0)
    if ahora_ts - ultimo_drop < COOLDOWN_GRUPO_SEG:
        faltante = int(COOLDOWN_GRUPO_SEG - (ahora_ts - ultimo_drop))
        context.bot.send_message(
            chat_id=chat_id,
            text=f"⏳ Espera {faltante} segundos antes de volver a dropear cartas en este grupo."
        )
        return

    if update.effective_chat.type not in ["group", "supergroup"]:
        context.bot.send_message(chat_id=chat_id, text="Este comando solo se puede usar en grupos.")
        return

    # --- Cooldown por usuario (6 horas o bono) ---
    cooldown_listo, bono_listo = puede_usar_idolday(user_id)

    if cooldown_listo:
        puede_tirar = True
        col_usuarios.update_one(
            {"user_id": user_id},
            {"$set": {"last_idolday": ahora}},
            upsert=True
        )
    elif bono_listo:
        puede_tirar = True
        objetos = user_doc.get('objetos', {})
        bonos_inventario = objetos.get('bono_idolday', 0)
        if bonos_inventario and bonos_inventario > 0:
            # Gasta del inventario
            col_usuarios.update_one(
                {"user_id": user_id},
                {"$inc": {"objetos.bono_idolday": -1}},
                upsert=True
            )
        else:
            # Gasta del campo legacy (admin)
            col_usuarios.update_one(
                {"user_id": user_id},
                {"$inc": {"bono": -1}},
                upsert=True
            )
    else:
        if last:
            faltante = 6*3600 - (ahora - last).total_seconds()
            horas = int(faltante // 3600)
            minutos = int((faltante % 3600) // 60)
            segundos = int(faltante % 60)
            context.bot.send_message(
                chat_id=chat_id,
                text=f"Ya usaste /idolday. Intenta de nuevo en {horas}h {minutos}m {segundos}s."
            )
        else:
            context.bot.send_message(chat_id=chat_id, text=f"Ya usaste /idolday.")
        return


    # --- Actualiza el cooldown global ---
    COOLDOWN_GRUPO[chat_id] = ahora_ts

    # SOLO cartas en estado "Excelente estado"
    cartas_excelentes = [c for c in cartas if c.get("estado") == "Excelente estado"]
    if len(cartas_excelentes) < 2:
        cartas_excelentes = cartas_excelentes * 2

    cartas_drop = random.choices(cartas_excelentes, k=2)
    media_group = []
    cartas_info = []
    for carta in cartas_drop:
        nombre = carta['nombre']
        version = carta['version']
        grupo = carta.get('grupo', '')
        imagen_url = carta.get('imagen')
        # RESERVA EL NÚMERO DE CARTA AQUÍ
        doc_cont = col_contadores.find_one_and_update(
            {"nombre": nombre, "version": version},
            {"$inc": {"contador": 1}},
            upsert=True,
            return_document=True
        )
        nuevo_id = doc_cont['contador'] if doc_cont else 1

        # Genera la imagen con el número
        imagen_con_numero = agregar_numero_a_imagen(imagen_url, nuevo_id)

        caption = f"<b>{nombre}</b>\n{grupo} [{version}]"
        media_group.append(InputMediaPhoto(media=imagen_con_numero, caption=caption, parse_mode="HTML"))
        cartas_info.append({
            "nombre": nombre,
            "version": version,
            "grupo": grupo,
            "imagen": imagen_url,
            "reclamada": False,
            "usuario": None,
            "hora_reclamada": None,
            "card_id": nuevo_id
        })

    msgs = context.bot.send_media_group(chat_id=chat_id, media=media_group)
    # main_msg = msgs[0]  # ← Ya no se usa el mensaje de imagen para el ID

    texto_drop = f"@{update.effective_user.username or update.effective_user.first_name} está dropeando 2 cartas!"
    # Primero manda el mensaje de los botones, lo guardamos en variable para usar su message_id
    msg_botones = context.bot.send_message(
        chat_id=chat_id,
        text=texto_drop,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("1️⃣", callback_data=f"reclamar_{chat_id}_{0}_0"),  # temporal, se corregirá abajo
                InlineKeyboardButton("2️⃣", callback_data=f"reclamar_{chat_id}_{0}_1"),
            ]
        ])
    )
    # AHORA sí: actualizamos los callback_data con el message_id correcto (el del mensaje de botones)
    botones_reclamar = [
        InlineKeyboardButton("1️⃣", callback_data=f"reclamar_{chat_id}_{msg_botones.message_id}_0"),
        InlineKeyboardButton("2️⃣", callback_data=f"reclamar_{chat_id}_{msg_botones.message_id}_1"),
    ]
    context.bot.edit_message_reply_markup(
        chat_id=chat_id,
        message_id=msg_botones.message_id,
        reply_markup=InlineKeyboardMarkup([botones_reclamar])
    )

    drop_id = crear_drop_id(chat_id, msg_botones.message_id)
    DROPS_ACTIVOS[drop_id] = {
        "cartas": cartas_info,
        "dueño": user_id,
        "chat_id": chat_id,
        "mensaje_id": msg_botones.message_id,
        "inicio": time.time(),
        "msg_botones": msg_botones,
        "usuarios_reclamaron": [],
        "expirado": False,
        "primer_reclamo_dueño": None,
    }

    col_usuarios.update_one(
        {"user_id": user_id},
        {"$set": {
            "last_idolday": ahora,
            "username": update.effective_user.username.lower() if update.effective_user.username else ""
        }},
        upsert=True
    )

    threading.Thread(target=desbloquear_drop, args=(drop_id, ), daemon=True).start()


FRASES_ESTADO = {
    "Excelente estado": "Genial!",
    "Buen estado": "Nada mal.",
    "Mal estado": "Podría estar mejor...",
    "Muy mal estado": "¡Oh no!"
}

def comando_darGemas(update, context):
    TU_USER_ID = 1111798714  # <-- Reemplaza por tu verdadero ID de Telegram
    if update.message.from_user.id != TU_USER_ID:
        update.message.reply_text("Este comando solo puede usarlo el creador del bot.")
        return


    # Destinatario
    if update.message.reply_to_message:
        dest_id = update.message.reply_to_message.from_user.id
    elif context.args and context.args[0].startswith('@'):
        username = context.args[0][1:].lower()
        dest_user = col_usuarios.find_one({"username": username})
        if not dest_user:
            update.message.reply_text("Usuario no encontrado. Debe haber usado el bot antes.")
            return
        dest_id = dest_user["user_id"]
    elif context.args:
        try:
            dest_id = int(context.args[0])
        except ValueError:
            update.message.reply_text("Uso: /darGemas <@usuario|user_id> <cantidad>")
            return
    else:
        update.message.reply_text("Debes responder a un usuario o especificar @usuario o user_id.")
        return

    # Cantidad
    if update.message.reply_to_message and len(context.args) >= 1:
        try:
            cantidad = int(context.args[0])
        except:
            update.message.reply_text("Debes poner la cantidad después del comando.")
            return
    elif len(context.args) >= 2:
        try:
            cantidad = int(context.args[1])
        except:
            update.message.reply_text("La cantidad debe ser un número.")
            return
    else:
        update.message.reply_text("Debes indicar la cantidad de gemas.")
        return

    col_usuarios.update_one({"user_id": dest_id}, {"$inc": {"gemas": cantidad}}, upsert=True)
    update.message.reply_text(f"💎 Gemas actualizadas para <code>{dest_id}</code> ({cantidad:+})", parse_mode="HTML")






@cooldown_critico
def comando_usar(update, context):
    from datetime import timedelta

    def normalizar_objeto(nombre):
        return (
            nombre.lower()
            .replace("_", " ")
            .replace("-", " ")
            .replace('"', '')
            .replace("'", '')
            .strip()
        )

    OBJETOS_USABLES = {
        "abrazo_de_bias": "abrazo_de_bias",
        "lightstick": "lightstick",
        "abrazo de bias": "abrazo_de_bias",
        "light stick": "lightstick",
    }

    user_id = update.message.from_user.id

    if not context.args:
        update.message.reply_text('Usa: /usar <objeto> (ejemplo: /usar "abrazo de bias")')
        return

    obj_norm = normalizar_objeto(" ".join(context.args))
    obj_id = OBJETOS_USABLES.get(obj_norm)

    if not obj_id:
        update.message.reply_text("No tienes ese objeto en tu inventario.")
        return

    doc = col_usuarios.find_one({"user_id": user_id}) or {}
    objetos = doc.get("objetos", {})
    cantidad = objetos.get(obj_id, 0)

    if cantidad < 1:
        update.message.reply_text("No tienes ese objeto en tu inventario.")
        return

    if obj_id == "abrazo_de_bias":
        last = doc.get('last_idolday')
        if not last:
            update.message.reply_text("No tienes cooldown activo de /idolday.")
            return

        ahora = datetime.utcnow()
        diferencia = (ahora - last).total_seconds()
        cd_total = 6 * 3600  # 6 horas
        faltante = cd_total - diferencia

        if faltante <= 0:
            update.message.reply_text("No tienes cooldown activo de /idolday.")
            return

        nuevo_faltante = faltante / 2
        nuevo_last = ahora - timedelta(seconds=(cd_total - nuevo_faltante))
        col_usuarios.update_one(
            {"user_id": user_id},
            {
                "$set": {"last_idolday": nuevo_last},
                "$inc": {f"objetos.{obj_id}": -1}
            }
        )

        def formatear_tiempo(segundos):
            h = int(segundos // 3600)
            m = int((segundos % 3600) // 60)
            s = int(segundos % 60)
            partes = []
            if h > 0: partes.append(f"{h}h")
            if m > 0: partes.append(f"{m}m")
            if s > 0 or not partes: partes.append(f"{s}s")
            return " ".join(partes)

        texto = (
            f"🤗 <b>¡Usaste Abrazo de Bias!</b>\n"
            f"Tiempo restante antes: <b>{formatear_tiempo(faltante)}</b>\n"
            f"Nuevo tiempo restante: <b>{formatear_tiempo(nuevo_faltante)}</b>\n"
            f"¡Ahora puedes usar /idolday mucho antes!"
        )
        update.message.reply_text(texto, parse_mode="HTML")
        return

    if obj_id == "lightstick":
        # Busca cartas mejorables
        cartas_usuario = list(col_cartas_usuario.find({"user_id": user_id}))
        cartas_mejorables = [
            c for c in cartas_usuario if c.get("estrellas", "") != "★★★"
        ]
        if not cartas_mejorables:
            update.message.reply_text("No tienes cartas que puedas mejorar con Lightstick (todas son ★★★).")
            return
        # Llama a la función que muestra el menú de mejora
        mostrar_lista_mejorables(update, context, user_id, cartas_mejorables, pagina=1)
        return



def manejador_reclamar(update, context):
    query = update.callback_query
    usuario_click = query.from_user.id
    data = query.data
    partes = data.split("_")
    if len(partes) != 4:
        query.answer()
        return
    _, chat_id, mensaje_id, idx = partes
    chat_id = int(chat_id)
    mensaje_id = int(mensaje_id)
    carta_idx = int(idx)
    drop_id = crear_drop_id(chat_id, mensaje_id)
    drop = DROPS_ACTIVOS.get(drop_id)

    ahora = time.time()
    if not drop or drop["expirado"]:
        query.answer("Este drop ya expiró o no existe.", show_alert=True)
        return

    carta = drop["cartas"][carta_idx]
    if carta["reclamada"]:
        query.answer("Esta carta ya fue reclamada.", show_alert=True)
        return

    tiempo_desde_drop = ahora - drop["inicio"]

    # --- CONTADOR DE INTENTOS ---
    if "intentos" not in carta:
        carta["intentos"] = 0
    if usuario_click != drop["dueño"]:
        carta["intentos"] += 1

    user_doc = col_usuarios.find_one({"user_id": usuario_click}) or {}
    objetos = user_doc.get("objetos", {})
    bonos_inventario = objetos.get('bono_idolday', 0)
    bono_legacy = user_doc.get('bono', 0)
    last = user_doc.get('last_idolday')
    ahora_dt = datetime.utcnow()
    cooldown_listo = False
    bono_listo = False

    if last:
        diferencia = ahora_dt - last
        cooldown_listo = diferencia.total_seconds() >= 6 * 3600
    else:
        cooldown_listo = True

    if (bonos_inventario and bonos_inventario > 0) or (bono_legacy and bono_legacy > 0):
        bono_listo = True

    puede_reclamar = False

    # --- PRIMERA RECLAMO DEL DUEÑO ---
    if usuario_click == drop["dueño"]:
        primer_reclamo = drop.get("primer_reclamo_dueño")
        if primer_reclamo is None:
            puede_reclamar = True
            drop["primer_reclamo_dueño"] = ahora
        else:
            # Reclama la segunda carta (o más)
            tiempo_faltante = 15 - (ahora - drop["primer_reclamo_dueño"])
            if tiempo_faltante > 0:
                segundos_faltantes = int(round(tiempo_faltante))
                query.answer(
                    f"Te quedan {segundos_faltantes} segundos para poder reclamar la otra.",
                    show_alert=True
                )
                return
            # --- chequea cooldown o bono (igual que para cualquier usuario) ---
            if cooldown_listo:
                puede_reclamar = True
                col_usuarios.update_one(
                    {"user_id": usuario_click},
                    {"$set": {"last_idolday": ahora_dt}},
                    upsert=True
                )
            elif bono_listo:
                puede_reclamar = True
                # Gasta primero del inventario, luego legacy
                if bonos_inventario and bonos_inventario > 0:
                    col_usuarios.update_one(
                        {"user_id": usuario_click},
                        {"$inc": {"objetos.bono_idolday": -1}},
                        upsert=True
                    )
                else:
                    col_usuarios.update_one(
                        {"user_id": usuario_click},
                        {"$inc": {"bono": -1}},
                        upsert=True
                    )
            else:
                # No puede reclamar
                if last:
                    faltante = 6*3600 - (ahora_dt - last).total_seconds()
                    horas = int(faltante // 3600)
                    minutos = int((faltante % 3600) // 60)
                    segundos = int(faltante % 60)
                    query.answer(
                        f"No puedes reclamar: espera cooldown ({horas}h {minutos}m {segundos}s) o compra un Bono Idolday.",
                        show_alert=True
                    )
                else:
                    query.answer(
                        "No puedes reclamar: espera el cooldown o compra un Bono Idolday.",
                        show_alert=True
                    )
                return
    # --- NO ES DUEÑO DEL DROP ---
    else:
        if tiempo_desde_drop < 15:
            segundos_faltantes = int(round(15 - tiempo_desde_drop))
            query.answer(
                f"Aún no puedes reclamar esta carta, te quedan {segundos_faltantes} segundos.",
                show_alert=True
            )
            return
        if cooldown_listo:
            puede_reclamar = True
            col_usuarios.update_one(
                {"user_id": usuario_click},
                {"$set": {"last_idolday": ahora_dt}},
                upsert=True
            )
        elif bono_listo:
            puede_reclamar = True
            if bonos_inventario and bonos_inventario > 0:
                col_usuarios.update_one(
                    {"user_id": usuario_click},
                    {"$inc": {"objetos.bono_idolday": -1}},
                    upsert=True
                )
            else:
                col_usuarios.update_one(
                    {"user_id": usuario_click},
                    {"$inc": {"bono": -1}},
                    upsert=True
                )
        else:
            if last:
                faltante = 6*3600 - (ahora_dt - last).total_seconds()
                horas = int(faltante // 3600)
                minutos = int((faltante % 3600) // 60)
                segundos = int(faltante % 60)
                query.answer(
                    f"No puedes reclamar: espera cooldown ({horas}h {minutos}m {segundos}s) o compra un Bono Idolday.",
                    show_alert=True
                )
            else:
                query.answer(
                    "No puedes reclamar: espera el cooldown o compra un Bono Idolday.",
                    show_alert=True
                )
            return

    if not puede_reclamar:
        return

    # --- Aquí SÍ generamos id_unico, estado y estrellas ---
    nombre = carta['nombre']
    version = carta['version']
    grupo = carta['grupo']

    nuevo_id = carta.get("card_id", 1)
    id_unico = random_id_unico(nuevo_id)

    posibles_estados = estados_disponibles_para_carta(nombre, version)
    carta_entregada = random.choice(posibles_estados)
    estado = carta_entregada['estado']
    estrellas = carta_entregada.get('estado_estrella', '★??')
    imagen_url = carta_entregada['imagen']

    # CALCULAR EL PRECIO FINAL SOLO TEMPORALMENTE
    intentos = carta.get("intentos", 0)
    precio = precio_carta_karuta(nombre, version, estado, id_unico=id_unico, card_id=nuevo_id) + 200 * max(0, intentos - 1) # -1 para no contar el intento del dueño real (primer click de reclamo)

    existente = col_cartas_usuario.find_one({
        "user_id": usuario_click,
        "nombre": nombre,
        "version": version,
        "card_id": nuevo_id,
        "estado": estado,
    })
    if existente:
        col_cartas_usuario.update_one(
            {"user_id": usuario_click, "nombre": nombre, "version": version, "card_id": nuevo_id, "estado": estado},
            {"$inc": {"count": 1}}
        )
    else:
        col_cartas_usuario.insert_one(
            {
                "user_id": usuario_click,
                "nombre": nombre,
                "version": version,
                "grupo": grupo,
                "estado": estado,
                "estrellas": estrellas,
                "imagen": imagen_url,
                "card_id": nuevo_id,
                "count": 1,
                "id_unico": id_unico,
                "estado_estrella": estrellas.count("★"),
                # NOTA: NO guardamos el campo intentos en la base de datos
            }
        )
    revisar_sets_completados(usuario_click, context)
    carta["reclamada"] = True
    carta["usuario"] = usuario_click
    carta["hora_reclamada"] = ahora
    drop["usuarios_reclamaron"].append(usuario_click)

    teclado = []
    for i, c in enumerate(drop["cartas"]):
        if c["reclamada"]:
            teclado.append(InlineKeyboardButton("❌", callback_data="reclamada", disabled=True))
        else:
            teclado.append(InlineKeyboardButton(f"{i+1}️⃣", callback_data=f"reclamar_{chat_id}_{mensaje_id}_{i}"))
    bot.edit_message_reply_markup(
        chat_id=drop["chat_id"],
        message_id=drop["mensaje_id"],
        reply_markup=InlineKeyboardMarkup([teclado])
    )

    user_mention = f"@{query.from_user.username or query.from_user.first_name}"
    FRASES_ESTADO = {
        "Excelente estado": "Genial!",
        "Buen estado": "Nada mal.",
        "Mal estado": "Podría estar mejor...",
        "Muy mal estado": "¡Oh no!"
    }
    frase_estado = FRASES_ESTADO.get(estado, "")

    # Construir el mensaje, solo si hubo intentos reales de otros usuarios
    mensaje_extra = ""
    # intentos incluye todos los clicks de no dueños, pero el último es el click de quien reclamó
    intentos_otros = max(0, intentos - 1)
    if intentos_otros > 0:
        mensaje_extra = f"\n💸 Esta carta fue disputada con <b>{intentos_otros}</b> intentos de otros usuarios."

    context.bot.send_message(
        chat_id=drop["chat_id"],
        text=f"{user_mention} tomaste la carta <code>{id_unico}</code> #{nuevo_id} [{version}] {nombre} - {grupo}, {frase_estado} está en <b>{estado.lower()}</b>!\n"
             f"{mensaje_extra}",
        parse_mode='HTML'
    )

    # ----------- FAVORITOS DE ESTA CARTA -------------
    favoritos = list(col_usuarios.find({
        "favoritos": {"$elemMatch": {"nombre": nombre, "version": version}}
    }))
    if favoritos:
        nombres = [
            f"⭐ @{user.get('username', 'SinUser')}" if user.get("username") else f"⭐ ID:{user['user_id']}"
            for user in favoritos
        ]
        texto_favs = "👀 <b>Favoritos de esta carta:</b>\n" + "\n".join(nombres)
        context.bot.send_message(
            chat_id=drop["chat_id"],
            text=texto_favs,
            parse_mode='HTML'
        )

    query.answer("¡Carta reclamada!", show_alert=True)



def gastar_gemas(user_id, cantidad):
    doc = col_usuarios.find_one({"user_id": user_id}) or {}
    gemas = doc.get("gemas", 0)
    if gemas < cantidad:
        return False
    col_usuarios.update_one({"user_id": user_id}, {"$inc": {"gemas": -cantidad}})
    return True




# ----------------- Resto de funciones: album, paginación, etc. -----------------

def mostrar_lista_mejorables(update, context, user_id, cartas_mejorables, pagina, mensaje=None, editar=False):
    por_pagina = 8
    total = len(cartas_mejorables)
    paginas = max(1, (total - 1) // por_pagina + 1)
    if pagina < 1: pagina = 1
    if pagina > paginas: pagina = paginas
    inicio = (pagina - 1) * por_pagina
    fin = min(inicio + por_pagina, total)
    cartas_pag = cartas_mejorables[inicio:fin]

    texto = "<b>Elige la carta que quieres mejorar:</b>\n"
    botones = []
    for c in cartas_pag:
        nombre = c.get("nombre", "")
        version = c.get("version", "")
        estrellas = c.get("estrellas", "")
        id_unico = c.get("id_unico", "")
        texto += f"{estrellas} <b>{nombre}</b> [{version}] (<code>{id_unico}</code>)\n"
        botones.append([InlineKeyboardButton(
            f"{estrellas} {nombre} [{version}]", callback_data=f"mejorar_{id_unico}"
        )])

    # Botones de navegación
    nav = []
    if pagina > 1:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"mejorarpag_{pagina-1}_{user_id}"))
    if pagina < paginas:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"mejorarpag_{pagina+1}_{user_id}"))
    if nav:
        botones.append(nav)

    teclado = InlineKeyboardMarkup(botones)

    if editar and mensaje:
        try:
            mensaje.edit_text(texto, parse_mode='HTML', reply_markup=teclado)
        except Exception:
            context.bot.send_message(chat_id=mensaje.chat_id, text=texto, parse_mode='HTML', reply_markup=teclado)
    else:
        update.message.reply_text(texto, parse_mode='HTML', reply_markup=teclado)









# Aquí pego la versión adaptada de /album para usar id_unico, estrellas y letra pegada a la izquierda:
@cooldown_critico
def comando_album(update, context):
    user_id = update.message.from_user.id
    chat_id = update.effective_chat.id
    msg = context.bot.send_message(
        chat_id=chat_id,
        text="Cargando tu álbum...",
    )
    mostrar_album_pagina(chat_id, msg.message_id, context, user_id, pagina=1)



# ----------- Función principal para mostrar la lista del álbum -----------

def enviar_lista_pagina(
    chat_id, user_id, lista_cartas, pagina, context,
    editar=False, mensaje=None, filtro=None, valor_filtro=None, orden=None, mostrando_filtros=False
):
    total = len(lista_cartas)
    por_pagina = 10
    paginas = (total - 1) // por_pagina + 1 if total else 1
    if pagina < 1:
        pagina = 1
    if pagina > paginas:
        pagina = paginas
    inicio = (pagina - 1) * por_pagina
    fin = min(inicio + por_pagina, total)

    if total == 0:
        texto = (
            "📕 <b>Tu álbum está vacío.</b>\n"
            "Usa <code>/idolday</code> para conseguir tus primeras cartas.\n"
            "¡Ve coleccionando y construye tu colección!"
        )
    else:
        texto = f"<b>📗 Álbum de cartas (página {pagina}/{paginas})</b>\n\n"
        for carta in lista_cartas[inicio:fin]:
            cid = carta.get('card_id', '')
            version = carta.get('version', '')
            nombre = carta.get('nombre', '')
            grupo = grupo_de_carta(nombre, version)
            id_unico = carta.get('id_unico', 'xxxx')
            estrellas = carta.get('estrellas', '★??')
            apodo = carta.get('apodo', '')
            apodo_txt = f'· "{apodo}" ' if apodo else ''
            texto += (
                f"• <code>{id_unico}</code> · [{estrellas}] · #{cid} · [{version}] {apodo_txt}· {nombre} · {grupo}\n"
            )
        texto += "\n<i>Usa <code>/ampliar &lt;id_unico&gt;</code> para ver detalles de cualquier carta.</i>"

    # BOTONES, mismo flujo que mercado
    botones = []
    if not mostrando_filtros and not filtro:
        botones = [[InlineKeyboardButton("⚙️ Filtrar / Ordenar", callback_data=f"album_filtros_{user_id}_{pagina}")]]
    else:
        # Menú de filtros
        botones = [
            [InlineKeyboardButton("⭐ Filtrar por Estado", callback_data=f"album_filtro_estado_{user_id}_{pagina}")],
            [InlineKeyboardButton("👥 Filtrar por Grupo", callback_data=f"album_filtro_grupo_{user_id}_{pagina}")]
        ]
        # Si hay filtro activo, agrega "Quitar Filtros"
        if filtro and valor_filtro:
            botones.append([InlineKeyboardButton("❌ Quitar Filtros", callback_data=f"album_sin_filtro_{user_id}_{pagina}")])

    # Botones de paginación abajo
    paginacion = []
    if pagina > 1:
        paginacion.append(InlineKeyboardButton("⬅️", callback_data=f"album_pagina_{user_id}_{pagina-1}_{filtro or 'none'}_{valor_filtro or 'none'}"))
    if pagina < paginas:
        paginacion.append(InlineKeyboardButton("➡️", callback_data=f"album_pagina_{user_id}_{pagina+1}_{filtro or 'none'}_{valor_filtro or 'none'}"))
    if paginacion:
        botones.append(paginacion)

    teclado = InlineKeyboardMarkup(botones)

    if editar and mensaje:
        try:
            mensaje.edit_text(texto, reply_markup=teclado, parse_mode='HTML')
        except Exception:
            context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado, parse_mode='HTML')
    else:
        context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado, parse_mode='HTML')


# ----------- Menú de ESTRELLAS (Estados) para filtrar -----------

def mostrar_menu_estrellas_album(user_id, pagina):
    # Busca todas las estrellas que tiene el usuario en sus cartas
    estrellas_posibles = ["☆☆☆", "★☆☆", "★★☆", "★★★"]
    # Opción: Solo mostrar las que el usuario tiene
    # estrellas_disponibles = sorted({c.get("estrellas", "☆☆☆") for c in col_cartas_usuario.find({"user_id": user_id})})
    botones = []
    for est in estrellas_posibles:
        botones.append([
            InlineKeyboardButton(est, callback_data=f"album_filtraestrella_{user_id}_{pagina}_{est}")
        ])
    teclado = InlineKeyboardMarkup(botones)
    return teclado

# ----------- Menú de GRUPOS para filtrar -----------

def mostrar_menu_grupos_album(user_id, pagina):
    grupos = sorted({c.get("grupo", "") for c in col_cartas_usuario.find({"user_id": user_id}) if c.get("grupo")})
    botones = []
    for grupo in grupos:
        botones.append([InlineKeyboardButton(grupo, callback_data=f"album_filtragrupo_{user_id}_{pagina}_{grupo}")])
    teclado = InlineKeyboardMarkup(botones)
    return teclado

# ----------- CALLBACK GENERAL para el menú de ALBUM -----------

def manejador_callback_album(update, context):
    query = update.callback_query
    data = query.data
    partes = data.split("_")
    usuario_id = query.from_user.id

    # DEBUG LOG
    print("query.data:", data)
    print("partes:", partes)
    print("usuario_id que presionó:", usuario_id)

    # Busca user_id explícitamente en cada tipo de callback_data del álbum
    dueño_id = None
    for p in partes:
        if p.isdigit() and len(p) > 5:  # asume user_id >= 100000
            dueño_id = int(p)
            break

    print("dueño_id detectado:", dueño_id)

    if dueño_id and usuario_id != dueño_id:
        query.answer("Solo puedes interactuar con tu propio álbum.", show_alert=True)
        return

    # === 1. Determina el tipo de callback y la posición del user_id ===
    dueño_id = None
    try:
        if data.startswith("album_pagina_"):
            # album_pagina_userid_pagina_filtro_valor_orden
            if len(partes) >= 4 and partes[2].isdigit():
                dueño_id = int(partes[2])
        elif data.startswith("album_filtros_"):
            # album_filtros_userid_pagina
            if len(partes) >= 3 and partes[2].isdigit():
                dueño_id = int(partes[2])
        elif data.startswith("album_filtro_estado_") or data.startswith("album_filtro_grupo_") or data.startswith("album_filtro_numero_"):
            # album_filtro_estado_userid_pagina
            if len(partes) >= 4 and partes[2].isdigit():
                dueño_id = int(partes[2])
        elif data.startswith("album_filtraestrella_") or data.startswith("album_filtragrupo_") or data.startswith("album_ordennum_"):
            # album_filtraestrella_userid_pagina_valor
            if len(partes) >= 3 and partes[2].isdigit():
                dueño_id = int(partes[2])
        elif data.startswith("album_sin_filtro_"):
            # album_sin_filtro_userid
            if len(partes) >= 3 and partes[2].isdigit():
                dueño_id = int(partes[2])
        elif data.startswith("album_fav_"):
            if len(partes) >= 3 and partes[2].isdigit():
                dueño_id = int(partes[2])
        else:
            # Fallback: busca el primer número largo de la lista
            for part in partes:
                if part.isdigit() and len(part) >= 5:
                    dueño_id = int(part)
                    break
    except Exception:
        dueño_id = None

    # === 2. Si el que aprieta NO es el dueño, bloquear ===
    if dueño_id and usuario_id != dueño_id:
        query.answer("Solo puedes interactuar con tu propio álbum.", show_alert=True)
        return

  
    # --- Filtro por estrellas (estado) ---
    if data.startswith("album_filtro_estado_"):
        user_id = int(partes[-2])
        pagina = int(partes[-1])
        context.bot.edit_message_reply_markup(
            chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            reply_markup=mostrar_menu_estrellas_album(user_id, pagina)
        )
        return

    # --- Filtro aplicado por estrella ---
    if data.startswith("album_filtraestrella_"):
        user_id = int(partes[2])
        pagina = int(partes[3])
        estrellas = partes[4]
        mostrar_album_pagina(query.message.chat_id, query.message.message_id, context, user_id, pagina, filtro="estrellas", valor_filtro=estrellas)
        return

    # --- Filtro por grupo ---
    if data.startswith("album_filtro_grupo_"):
        user_id = int(partes[-2])
        pagina = int(partes[-1])
        grupos = sorted({c.get("grupo", "") for c in col_cartas_usuario.find({"user_id": user_id}) if c.get("grupo")})
        context.bot.edit_message_reply_markup(
            chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            reply_markup=mostrar_menu_grupos_album(user_id, pagina, grupos)
        )
        return

    # --- Filtro aplicado por grupo ---
    if data.startswith("album_filtragrupo_"):
        user_id = int(partes[2])
        pagina = int(partes[3])
        grupo = "_".join(partes[4:])
        mostrar_album_pagina(query.message.chat_id, query.message.message_id, context, user_id, pagina, filtro="grupo", valor_filtro=grupo)
        return

    # --- Menú de filtros principal ---
    if data.startswith("album_filtros_"):
        user_id = int(partes[2])
        pagina = int(partes[3])
        context.bot.edit_message_reply_markup(
            chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            reply_markup=mostrar_menu_filtros_album(user_id, pagina)
        )
        return

    # --- Filtro ordenar por número ---
    if data.startswith("album_filtro_numero_"):
        user_id = int(partes[3])
        pagina = int(partes[4])
        context.bot.edit_message_reply_markup(
            chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            reply_markup=mostrar_menu_ordenar_album(user_id, pagina)
        )
        return

    # --- Orden aplicado ---
    if data.startswith("album_ordennum_"):
        user_id = int(partes[2])
        pagina = int(partes[3])
        orden = partes[4]
        mostrar_album_pagina(query.message.chat_id, query.message.message_id, context, user_id, pagina, orden=orden)
        return

    # --- Volver al álbum completo (sin filtros) ---
    if data.startswith("album_pagina_"):
        user_id = int(partes[2])
        pagina = int(partes[3])
        filtro = partes[4] if len(partes) > 4 and partes[4] != "none" else None
        valor_filtro = partes[5] if len(partes) > 5 and partes[5] != "none" else None
        orden = partes[6] if len(partes) > 6 and partes[6] != "none" else None
        mostrar_album_pagina(query.message.chat_id, query.message.message_id, context, user_id, int(pagina), filtro=filtro, valor_filtro=valor_filtro, orden=orden)
        return





from telegram import InlineKeyboardButton, InlineKeyboardMarkup

@cooldown_critico
def comando_mejorar(update, context):
    user_id = update.message.from_user.id

    # Si se pasa un argumento, buscar esa carta y lanzar el menú de mejora SOLO para esa carta
    if context.args:
        id_unico = context.args[0].strip()
        carta = col_cartas_usuario.find_one({"user_id": user_id, "id_unico": id_unico})
        if not carta:
            update.message.reply_text("No tienes esa carta (o el id_unico no es válido).")
            return
        if carta.get("estrellas", "") == "★★★":
            update.message.reply_text("Esta carta ya tiene el máximo de estrellas.")
            return
        # Llama directo a mostrar_lista_mejorables con SOLO esa carta
        mostrar_lista_mejorables(update, context, user_id, [carta], pagina=1)
        return

    # Caso tradicional: mostrar todas las mejorables
    cartas_usuario = list(col_cartas_usuario.find({"user_id": user_id}))
    cartas_mejorables = [
        c for c in cartas_usuario
        if c.get("estrellas", "") != "★★★"
    ]
    # Ordenar por nombre y versión
    cartas_mejorables.sort(
        key=lambda x: (
            x.get("nombre", "").lower(),
            x.get("version", "").lower()
        )
    )
    if not cartas_mejorables:
        update.message.reply_text("No tienes cartas que se puedan mejorar (todas son ★★★).")
        return

    pagina = 1
    mostrar_lista_mejorables(update, context, user_id, cartas_mejorables, pagina)






@cooldown_critico
def comando_inventario(update, context):
    user_id = update.message.from_user.id
    chat_id = update.effective_chat.id

    doc = col_usuarios.find_one({"user_id": user_id}) or {}
    objetos = doc.get("objetos", {})
    kponey = doc.get("kponey", 0)
    bono = doc.get("bono", 0)
    gemas = doc.get("gemas", 0)   # ← AQUÍ

    texto = f"🎒 <b>Tu inventario</b>\n\n"
    tiene_objetos = False
    for obj_id, info in CATALOGO_OBJETOS.items():
        cantidad = objetos.get(obj_id, 0)
        if cantidad > 0:
            tiene_objetos = True
            texto += f"{info['emoji']} <b>{info['nombre']}</b>: <b>{cantidad}</b>\n"
    if not tiene_objetos:
        texto += "No tienes objetos todavía.\n"
    texto += f"\n💎 <b>Gemas:</b> <code>{gemas}</code>"   # ← AQUÍ
    texto += f"\n💸 <b>Kponey:</b> <code>{kponey}</code>"
    texto += "\n\nUsa <code>/tienda</code> para comprar objetos."
    update.message.reply_text(texto, parse_mode="HTML")





@cooldown_critico
def comando_tienda(update, context):
    user_id = update.message.from_user.id
    chat_id = update.effective_chat.id
    doc = col_usuarios.find_one({"user_id": user_id}) or {}
    kponey = doc.get("kponey", 0)

    texto = "🛒 <b>Tienda de objetos</b>\n\n"
    botones = []
    for obj_id, info in CATALOGO_OBJETOS.items():
        texto += (
            f"{info['emoji']} <b>{info['nombre']}</b> — <code>{info['precio']} Kponey</code>\n"
            f"{info['desc']}\n\n"
        )
        botones.append([InlineKeyboardButton(f"{info['emoji']} Comprar {info['nombre']}", callback_data=f"comprarobj_{obj_id}")])
    texto += f"💸 <b>Tu saldo:</b> <code>{kponey}</code>"

    teclado = InlineKeyboardMarkup(botones)
    update.message.reply_text(texto, parse_mode="HTML", reply_markup=teclado)


def comprar_objeto(user_id, obj_id, context, chat_id, reply_func):
    info = CATALOGO_OBJETOS.get(obj_id)
    if not info:
        reply_func("Ese objeto no existe.")
        return

    doc = col_usuarios.find_one({"user_id": user_id}) or {}
    kponey = doc.get("kponey", 0)
    precio = info['precio']
    if kponey < precio:
        reply_func("No tienes suficiente Kponey para este objeto.")
        return

    col_usuarios.update_one(
        {"user_id": user_id},
        {"$inc": {f"objetos.{obj_id}": 1, "kponey": -precio}},
        upsert=True
    )
    reply_func(
        f"¡Compraste {info['emoji']} {info['nombre']} por {precio} Kponey!",
        parse_mode="HTML"
    )


@cooldown_critico
def comando_comprarobjeto(update, context):
    user_id = update.message.from_user.id
    chat_id = update.effective_chat.id
    if not context.args:
        update.message.reply_text("Usa: /comprarobjeto <objeto_id>\nEjemplo: /comprarobjeto bono_idolday")
        return
    obj_id = context.args[0].strip()
    comprar_objeto(
        user_id, obj_id, context, chat_id,
        lambda text, **kwargs: update.message.reply_text(text, **kwargs)
    )




#----------------------------------------------------

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def mostrar_mercado_pagina(chat_id, message_id, context, user_id, pagina=1, filtro=None, valor_filtro=None, orden=None):
    # --- FILTRO DE CARTAS ---
    query_mercado = {}
    if filtro == "estrellas":
        query_mercado["estrellas"] = valor_filtro
    elif filtro == "grupo":
        query_mercado["grupo"] = valor_filtro

    cartas = list(col_mercado.find(query_mercado))

    # --- ORDEN ---
    if orden == "menor":
        cartas.sort(key=lambda x: x.get("card_id", 0))
    elif orden == "mayor":
        cartas.sort(key=lambda x: -x.get("card_id", 0))
    else:
        # Orden default: grupo, nombre, card_id
        cartas.sort(key=lambda x: (x.get("grupo", "").lower(), x.get("nombre", "").lower(), x.get("card_id", 0)))

    # --- PAGINACIÓN ---
    cartas_por_pagina = 10
    total_paginas = max(1, ((len(cartas) - 1) // cartas_por_pagina) + 1)
    pagina = max(1, min(pagina, total_paginas))
    inicio = (pagina - 1) * cartas_por_pagina
    fin = inicio + cartas_por_pagina
    cartas_pagina = cartas[inicio:fin]

    # --- PREPARA FAVORITOS DEL USUARIO ---
    usuario = col_usuarios.find_one({"user_id": user_id}) or {}
    favoritos = usuario.get("favoritos", [])

    # --- TEXTO LISTA ---
    texto = "<b>🛒 Mercado</b>\n"
    for c in cartas_pagina:
        estrellas = f"[{c.get('estrellas', '?')}]"
        num = f"#{c.get('card_id', '?')}"
        ver = f"[{c.get('version', '?')}]"
        nom = c.get('nombre', '?')
        grp = c.get('grupo', '?')
        precio = f"{c.get('precio', '?'):,}"
        idu = c.get('id_unico', '')

        es_fav = any(
            fav.get("nombre") == c.get("nombre") and fav.get("version") == c.get("version")
            for fav in favoritos
        )
        estrella_fav = " ⭐" if es_fav else ""

        texto += (
            f"{estrellas} · {num} · {ver} · {nom} · {grp}{estrella_fav}\n"
            f"💲{precio}\n"
            f"<code>/comprar {idu}</code>\n\n"
        )
    if not cartas_pagina:
        texto += "\n(No hay cartas para mostrar con este filtro)"

    # --- BOTONES ---
    botones = []
    botones.append([InlineKeyboardButton("🔎 Filtrar / Ordenar", callback_data=f"mercado_filtros_{user_id}_{pagina}")])
    paginacion = []
    if pagina > 1:
        paginacion.append(InlineKeyboardButton("⬅️", callback_data=f"mercado_pagina_{user_id}_{pagina-1}_{filtro or 'none'}_{valor_filtro or 'none'}_{orden or 'none'}"))
    if pagina < total_paginas:
        paginacion.append(InlineKeyboardButton("➡️", callback_data=f"mercado_pagina_{user_id}_{pagina+1}_{filtro or 'none'}_{valor_filtro or 'none'}_{orden or 'none'}"))
    if paginacion:
        botones.append(paginacion)

    teclado = InlineKeyboardMarkup(botones)
    context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=texto,
        parse_mode="HTML",
        reply_markup=teclado
    )



def mostrar_menu_filtros(user_id, pagina):
    botones = [
        [InlineKeyboardButton("⭐ Filtrar por Estado", callback_data=f"mercado_filtro_estado_{user_id}_{pagina}")],
        [InlineKeyboardButton("👥 Filtrar por Grupo", callback_data=f"mercado_filtro_grupo_{user_id}_{pagina}_1")],
        [InlineKeyboardButton("🔢 Ordenar por Número", callback_data=f"mercado_filtro_numero_{user_id}_{pagina}")],
        [InlineKeyboardButton("⬅️ Volver", callback_data=f"mercado_pagina_{user_id}_{pagina}_none_none_none")]
    ]
    return InlineKeyboardMarkup(botones)

def mostrar_menu_estrellas(user_id, pagina):
    botones = [
        [InlineKeyboardButton("★★★", callback_data=f"mercado_filtraestrella_{user_id}_{pagina}_★★★")],
        [InlineKeyboardButton("★★☆", callback_data=f"mercado_filtraestrella_{user_id}_{pagina}_★★☆")],
        [InlineKeyboardButton("★☆☆", callback_data=f"mercado_filtraestrella_{user_id}_{pagina}_★☆☆")],
        [InlineKeyboardButton("☆☆☆", callback_data=f"mercado_filtraestrella_{user_id}_{pagina}_☆☆☆")],
        [InlineKeyboardButton("⬅️ Volver", callback_data=f"mercado_filtros_{user_id}_{pagina}")]
    ]
    return InlineKeyboardMarkup(botones)

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def mostrar_menu_grupos(user_id, pagina, grupos):
    por_pagina = 5
    total = len(grupos)
    paginas = max(1, (total - 1) // por_pagina + 1)
    if pagina < 1:
        pagina = 1
    if pagina > paginas:
        pagina = paginas
    inicio = (pagina - 1) * por_pagina
    fin = min(inicio + por_pagina, total)
    grupos_pagina = grupos[inicio:fin]

    matriz = []
    for g in grupos_pagina:
        # Usa callback_data consistente con tu manejador_callback
        matriz.append([InlineKeyboardButton(g, callback_data=f"mercado_filtragrupo_{user_id}_{pagina}_{g}")])

    nav = []
    if pagina > 1:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"mercado_filtro_grupo_{user_id}_{pagina-1}"))
    if pagina < paginas:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"mercado_filtro_grupo_{user_id}_{pagina+1}"))
    if nav:
        matriz.append(nav)
    matriz.append([InlineKeyboardButton("Volver", callback_data=f"mercado_filtros_{user_id}_{pagina}")])

    return InlineKeyboardMarkup(matriz)



def mostrar_menu_ordenar(user_id, pagina):
    botones = [
        [InlineKeyboardButton("⬆️ Menor a mayor", callback_data=f"mercado_ordennum_{user_id}_{pagina}_menor")],
        [InlineKeyboardButton("⬇️ Mayor a menor", callback_data=f"mercado_ordennum_{user_id}_{pagina}_mayor")],
        [InlineKeyboardButton("⬅️ Volver", callback_data=f"mercado_filtros_{user_id}_{pagina}")]
    ]
    return InlineKeyboardMarkup(botones)


#----------Comando FAV1---------------
@cooldown_critico
def comando_favoritos(update, context):
    user_id = update.message.from_user.id
    doc = col_usuarios.find_one({"user_id": user_id})
    favoritos = doc.get("favoritos", []) if doc else []

    if not favoritos:
        update.message.reply_text("⭐ No tienes cartas favoritas aún. Usa <code>/fav [V1] Dahyun</code> para añadir una.", parse_mode="HTML")
        return

    texto = "⭐ <b>Tus cartas favoritas:</b>\n\n"
    for fav in favoritos:
        nombre = fav.get("nombre", "")
        version = fav.get("version", "")
        texto += f"<code>[{version}] {nombre}</code>\n"
    texto += "\n<i>Puedes añadir o quitar favoritos usando /fav [Vn] Nombre</i>"

    update.message.reply_text(texto, parse_mode="HTML")

#----------Comando FAV---------------
@cooldown_critico
def comando_fav(update, context):
    user_id = update.message.from_user.id
    args = context.args
    if not args:
        update.message.reply_text("Usa: /fav [Vn] Nombre\nPor ejemplo: /fav [V1] Dahyun")
        return

    # Reconstruir nombre y versión correctamente
    entrada = " ".join(args).strip()
    if not entrada.startswith("[") or "]" not in entrada:
        update.message.reply_text("Formato incorrecto. Ejemplo: /fav [V1] Dahyun")
        return

    version = entrada.split("]", 1)[0][1:]
    nombre = entrada.split("]", 1)[1].strip()

    # Busca si la carta existe en el catálogo
    existe = any(c["nombre"] == nombre and c["version"] == version for c in cartas)
    if not existe:
        update.message.reply_text(f"No se encontró la carta: [{version}] {nombre}")
        return

    doc = col_usuarios.find_one({"user_id": user_id}) or {}
    favoritos = doc.get("favoritos", [])

    key = {"nombre": nombre, "version": version}
    if key in favoritos:
        favoritos = [f for f in favoritos if not (f["nombre"] == nombre and f["version"] == version)]
        col_usuarios.update_one({"user_id": user_id}, {"$set": {"favoritos": favoritos}}, upsert=True)
        update.message.reply_text(f"❌ Quitaste de favoritos: <code>[{version}] {nombre}</code>", parse_mode="HTML")
    else:
        favoritos.append(key)
        col_usuarios.update_one({"user_id": user_id}, {"$set": {"favoritos": favoritos}}, upsert=True)
        update.message.reply_text(f"⭐ Añadiste a favoritos: <code>[{version}] {nombre}</code>", parse_mode="HTML")

#------------COMANDO PRECIO---------------------
@cooldown_critico
def comando_precio(update, context):
    if not context.args:
        update.message.reply_text("Usa: /precio <id_unico>\nEjemplo: /precio f4fg1")
        return
    id_unico = context.args[0].strip()
    carta = col_cartas_usuario.find_one({"id_unico": id_unico})
    if not carta:
        update.message.reply_text("No se encontró la carta con ese ID único en la base de datos.")
        return
    nombre = carta['nombre']
    version = carta['version']
    estado = carta['estado']
    # EXTRA: saca el card_id desde id_unico, para el precio especial
    precio = precio_carta_karuta(nombre, version, estado, id_unico=id_unico)
    total_copias = col_cartas_usuario.count_documents({"nombre": nombre, "version": version})
    texto = (
        f"🖼️ <b>Información de carta [{id_unico}]</b>\n"
        f"• Nombre: <b>{nombre}</b>\n"
        f"• Versión: <b>{version}</b>\n"
        f"• Estado: <b>{estado}</b>\n"
        f"• Precio: <code>{precio} Kponey</code>\n"
        f"• Copias globales: <b>{total_copias}</b>"
    )
    update.message.reply_text(texto, parse_mode='HTML')


#------Comando vender--------------------
@cooldown_critico
def comando_vender(update, context):
    user_id = update.message.from_user.id
    chat_id = update.effective_chat.id

    if not context.args:
        update.message.reply_text("Usa: /vender <id_unico>")
        return
    id_unico = context.args[0].strip()
    carta = col_cartas_usuario.find_one({"user_id": user_id, "id_unico": id_unico})
    if not carta:
        update.message.reply_text("No tienes esa carta en tu inventario.")
        return

    nombre = carta['nombre']
    version = carta['version']
    estado = carta['estado']
    id_unico = carta.get("id_unico", "")
    precio = precio_carta_karuta(nombre, version, estado, id_unico=id_unico)

    # Verifica si ya está en mercado
    ya = col_mercado.find_one({"id_unico": id_unico})
    if ya:
        update.message.reply_text("Esta carta ya está en el mercado.")
        return

    # Quitar de inventario y poner en mercado
    col_cartas_usuario.delete_one({"user_id": user_id, "id_unico": id_unico})

    # Busca las estrellas (corregido)
    estrellas = carta.get('estrellas')
    if not estrellas:
        # Busca las estrellas en el catálogo de cartas
        estrellas = "★??"
        for c in cartas:
            if c['nombre'] == nombre and c['version'] == version and c['estado'] == estado:
                estrellas = c.get('estado_estrella', "★??")
                break

    # --- 👇 CORRECCIÓN AQUÍ: Obtén card_id seguro 👇 ---
    card_id = carta.get('card_id', extraer_card_id_de_id_unico(id_unico))
    # -----------------------------------------------

    col_mercado.insert_one({
       "id_unico": id_unico,
       "vendedor_id": user_id,
       "nombre": nombre,
       "version": version,
       "estado": estado,
       "estrellas": estrellas,
       "precio": precio,
       "card_id": card_id,  # <---- ¡Ahora siempre se guarda!
       "fecha": datetime.utcnow(),
       "imagen": carta.get("imagen"),
       "grupo": carta.get("grupo", "")
    })
    
    update.message.reply_text(
        f"📦 Carta <b>{nombre} [{version}]</b> puesta en el mercado por <b>{precio} Kponey</b>.",
        parse_mode='HTML'
    )

#----------Comprar carta del mercado------------------
@cooldown_critico
def comando_comprar(update, context):
    user_id = update.message.from_user.id
    if not context.args:
        update.message.reply_text("Usa: /comprar <id_unico>")
        return
    id_unico = context.args[0].strip()
    # Transacción atómica: solo uno puede comprarla
    carta = col_mercado.find_one_and_delete({"id_unico": id_unico})
    if not carta:
        update.message.reply_text("Esa carta ya no está disponible o ya fue comprada.")
        return
    if carta["vendedor_id"] == user_id:
        update.message.reply_text("No puedes comprar tu propia carta.")
        # Devuelve la carta al mercado si el vendedor intentó comprarla
        col_mercado.insert_one(carta)
        return

    usuario = col_usuarios.find_one({"user_id": user_id}) or {}
    saldo = usuario.get("kponey", 0)
    precio = carta["precio"]

    if saldo < precio:
        update.message.reply_text(f"No tienes suficiente Kponey. Precio: {precio}, tu saldo: {saldo}")
        # Devuelve la carta al mercado si el usuario no tiene saldo suficiente
        col_mercado.insert_one(carta)
        return

    # Transacción de dinero
    col_usuarios.update_one({"user_id": user_id}, {"$inc": {"kponey": -precio}}, upsert=True)
    col_usuarios.update_one({"user_id": carta["vendedor_id"]}, {"$inc": {"kponey": precio}}, upsert=True)

    # --- 👇 CORRECCIÓN AQUÍ: asegura que card_id esté correcto 👇 ---
    card_id = carta.get("card_id")
    if not card_id:
        card_id = extraer_card_id_de_id_unico(carta.get("id_unico"))
        carta["card_id"] = card_id
    # --------------------------------------------------------------

    # Preparar carta para el inventario del usuario
    carta['user_id'] = user_id
    if '_id' in carta: del carta['_id']
    if 'vendedor_id' in carta: del carta['vendedor_id']
    if 'precio' in carta: del carta['precio']
    if 'fecha' in carta: del carta['fecha']
    if 'estrellas' not in carta or not carta['estrellas'] or carta['estrellas'] == '★??':
        estado = carta.get('estado')
        for c in cartas:
            if c['nombre'] == carta['nombre'] and c['version'] == carta['version'] and c['estado'] == estado:
                carta['estrellas'] = c.get('estado_estrella', '★??')
                break
        else:
            carta['estrellas'] = '★??'

    col_cartas_usuario.insert_one(carta)
    revisar_sets_completados(user_id, context)
    
    update.message.reply_text(
        f"✅ Compraste la carta <b>{carta['nombre']} [{carta['version']}]</b> por <b>{precio} Kponey</b>.",
        parse_mode="HTML"
    )

    # Notificar al vendedor (opcional)
    try:
        context.bot.send_message(
            chat_id=carta["vendedor_id"],
            text=f"💸 ¡Vendiste la carta <b>{carta['nombre']} [{carta['version']}]</b> y ganaste <b>{precio} Kponey</b>!",
            parse_mode="HTML"
        )
    except Exception:
        pass


#----------Retirar carta del mercado------------------

def comando_retirar(update, context):
    user_id = update.message.from_user.id
    if not context.args:
        update.message.reply_text("Usa: /retirar <id_unico>")
        return
    id_unico = context.args[0].strip()
    carta = col_mercado.find_one({"id_unico": id_unico, "vendedor_id": user_id})
    if not carta:
        update.message.reply_text("No tienes esa carta en el mercado.")
        return
    # Devolver carta al usuario
    col_mercado.delete_one({"id_unico": id_unico})
    carta['user_id'] = user_id
    del carta['_id']
    del carta['vendedor_id']
    del carta['precio']
    del carta['fecha']

    # --- CORRECCIÓN: asegura el campo 'estrellas' ---
    if 'estrellas' not in carta or not carta['estrellas'] or carta['estrellas'] == '★??':
        estado = carta.get('estado')
        for c in cartas:
            if c['nombre'] == carta['nombre'] and c['version'] == carta['version'] and c['estado'] == estado:
                carta['estrellas'] = c.get('estado_estrella', '★??')
                break
        else:
            carta['estrellas'] = '★??'

    col_cartas_usuario.insert_one(carta)
    update.message.reply_text("Carta retirada del mercado y devuelta a tu álbum.")
    
#--------------------------------------------------------------------------------


#---------Dinero del bot------------
@cooldown_critico
def comando_saldo(update, context):
    user_id = update.message.from_user.id
    usuario = col_usuarios.find_one({"user_id": user_id}) or {}
    kponey = usuario.get("kponey", 0)
    update.message.reply_text(f"💸 <b>Tus Kponey:</b> <code>{kponey}</code>", parse_mode="HTML")


def comando_gemas(update, context):
    user_id = update.message.from_user.id
    usuario = col_usuarios.find_one({"user_id": user_id}) or {}
    gemas = usuario.get("gemas", 0)
    update.message.reply_text(f"💎 <b>Tus gemas:</b> <code>{gemas}</code>", parse_mode="HTML")


#---------Para dar dinero------------
def comando_darKponey(update, context):
    TU_USER_ID = 1111798714  # <-- Reemplaza por tu verdadero ID de Telegram
    if update.message.from_user.id != TU_USER_ID:
        update.message.reply_text("Este comando solo puede usarlo el creador del bot.")
        return

    # Revisar si se responde a alguien (reply)
    if update.message.reply_to_message:
        dest_id = update.message.reply_to_message.from_user.id
    # Revisar si se menciona usuario (@usuario)
    elif context.args and context.args[0].startswith('@'):
        username = context.args[0][1:].lower()
        dest_user = col_usuarios.find_one({"username": username})
        if not dest_user:
            update.message.reply_text("Usuario no encontrado. Debe haber usado el bot antes.")
            return
        dest_id = dest_user["user_id"]
    # Revisar si se pasa user_id directamente
    elif context.args:
        try:
            dest_id = int(context.args[0])
        except ValueError:
            update.message.reply_text("Uso: /darKponey <@usuario|user_id> <cantidad>")
            return
    else:
        update.message.reply_text("Debes responder a un usuario o especificar @usuario o user_id.")
        return

    # Cantidad a dar/quitar (negativo para quitar)
    if update.message.reply_to_message and len(context.args) >= 1:
        try:
            cantidad = int(context.args[0])
        except:
            update.message.reply_text("Debes poner la cantidad después del comando.")
            return
    elif len(context.args) >= 2:
        try:
            cantidad = int(context.args[1])
        except:
            update.message.reply_text("La cantidad debe ser un número.")
            return
    else:
        update.message.reply_text("Debes indicar la cantidad de Kponey.")
        return

    col_usuarios.update_one({"user_id": dest_id}, {"$inc": {"kponey": cantidad}}, upsert=True)
    update.message.reply_text(f"💸 Kponey actualizado para <code>{dest_id}</code> ({cantidad:+})", parse_mode="HTML")




def mostrar_carta_individual(chat_id, user_id, lista_cartas, idx, context, mensaje_a_editar=None, query=None):
    carta = lista_cartas[idx]
    version = carta.get('version', '')
    nombre = carta.get('nombre', '')
    grupo = grupo_de_carta(nombre, version)
    imagen_url = carta.get('imagen', imagen_de_carta(nombre, version))
    id_unico = carta.get('id_unico', '')
    estrellas = carta.get('estrellas', '★??')
    estado = carta.get('estado', '')

    texto = (
    f"<b>[{version}] {nombre} {grupo}</b>\n"
    f"ID: <code>{id_unico}</code>\n"
)

    if query is not None:
        try:
            query.edit_message_media(
                media=InputMediaPhoto(media=imagen_url, caption=texto, parse_mode='HTML'),
                reply_markup=query.message.reply_markup
            )
        except Exception as e:
            query.answer(text="No se pudo actualizar la imagen.", show_alert=True)
    else:
        context.bot.send_photo(chat_id=chat_id, photo=imagen_url, caption=texto, parse_mode='HTML')

# ... Aquí pegas la versión nueva de comando_giveidol y resto de comandos extras adaptados ...
# Si quieres esa parte dime y te la entrego lista para copiar y pegar
def comando_miid(update, context):
    usuario = update.effective_user
    update.message.reply_text(f"Tu ID de Telegram es: {usuario.id}")

def comando_bonoidolday(update, context):
    user_id = update.message.from_user.id
    chat = update.effective_chat
    if chat.type not in ["group", "supergroup"]:
        update.message.reply_text("Este comando solo puede usarse en grupos.")
        return
    if not es_admin(update):
        update.message.reply_text("Solo los administradores pueden usar este comando.")
        return
    args = context.args
    if len(args) != 2:
        update.message.reply_text("Uso: /bonoidolday <user_id> <cantidad>")
        return
    try:
        dest_id = int(args[0])
        cantidad = int(args[1])
        if cantidad < 1:
            update.message.reply_text("La cantidad debe ser mayor que 0.")
            return
    except:
        update.message.reply_text("Uso: /bonoidolday <user_id> <cantidad>")
        return
    col_usuarios.update_one({"user_id": dest_id}, {"$inc": {"bono": cantidad}}, upsert=True)
    update.message.reply_text(f"✅ Bono de {cantidad} tiradas de /idolday entregado a <code>{dest_id}</code>.", parse_mode='HTML')


def comando_ampliar(update, context):
    if not context.args:
        update.message.reply_text("Debes indicar el ID único de la carta: /ampliar <id_unico>")
        return
    user_id = update.message.from_user.id
    id_unico = context.args[0].strip()

    # 1. Busca en inventario
    carta = col_cartas_usuario.find_one({"user_id": user_id, "id_unico": id_unico})
    fuente = "album"
    if not carta:
        # 2. Si no está, busca en mercado
        carta = col_mercado.find_one({"id_unico": id_unico})
        fuente = "mercado"
    if not carta:
        update.message.reply_text("No encontré esa carta en tu álbum ni en el mercado.")
        return

    # Traer datos principales
    imagen_url = carta.get('imagen', imagen_de_carta(carta['nombre'], carta['version']))
    nombre = carta.get('nombre', '')
    apodo = carta.get('apodo', '')
    nombre_mostrar = f'({apodo}) {nombre}' if apodo else nombre
    version = carta.get('version', '')
    grupo = grupo_de_carta(nombre, version)
    estrellas = carta.get('estrellas', '★??')
    estado = carta.get('estado', '')
    card_id = carta.get('card_id', '')
    total_copias = col_cartas_usuario.count_documents({"nombre": nombre, "version": version})

    # Saber si es favorita (solo si está en el álbum)
    doc_user = col_usuarios.find_one({"user_id": user_id}) or {}
    favoritos = doc_user.get("favoritos", [])
    es_fav = any(fav.get("nombre") == nombre and fav.get("version") == version for fav in favoritos)
    estrella_fav = "⭐ " if es_fav else ""

    # --- Corrige aquí: usa el precio guardado si está en mercado ---
    precio = precio_carta_karuta(nombre, version, estado, id_unico=id_unico)

    # Texto bonito
    texto = (
        f"🎴 <b>Info de carta [{id_unico}]</b>\n"
        f"• Nombre: {estrella_fav}<b>{nombre_mostrar}</b>\n"
        f"• Grupo: <b>{grupo}</b>\n"
        f"• Versión: <b>{version}</b>\n"
        f"• Nº de carta: <b>#{card_id}</b>\n"
        f"• Estado: <b>{estrellas}</b>\n"
        f"• Precio: <code>{precio} Kponey</code>\n"
        f"• Copias globales: <b>{total_copias}</b>"
    )

    # Botón de vender (solo si está en álbum)
    if fuente == "album":
        teclado = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Vender", callback_data=f"ampliar_vender_{id_unico}")]
        ])
    else:
        teclado = None

    update.message.reply_photo(
        photo=imagen_url,
        caption=texto,
        parse_mode='HTML',
        reply_markup=teclado
    )


@cooldown_critico
def comando_comandos(update, context):
    texto = (
        "📋 <b>Lista de comandos disponibles:</b>\n\n"
        "<b>🎴 Cartas</b>\n"
        "/idolday — Drop de 2 cartas en el grupo\n"
        "/album — Muestra tu colección de cartas\n"
        "/ampliar <code>id_unico</code> — Ver detalles y precio de una carta\n"
        "/giveidol <code>id_unico</code> @usuario — Regala una carta a otro usuario\n"
        "/favoritos — Muestra tus cartas favoritas\n"
        "/fav [Vn] Nombre — Añade o quita una carta de favoritos\n"
        "\n"
        "<b>🛒 Mercado</b>\n"
        "/vender <code>id_unico</code> — Vender una carta en el mercado\n"
        "/mercado — Ver cartas disponibles en el mercado\n"
        "/comprar <code>id_unico</code> — Comprar una carta del mercado\n"
        "/retirar <code>id_unico</code> — Retirar tu carta del mercado\n"
        "\n"
        "<b>💸 Economía y extras</b>\n"
        "/inventario — Ver tus objetos y saldo\n"
        "/kponey — Consultar tu saldo de Kponey\n"
        "/precio <code>id_unico</code> — Consultar el precio de una carta\n"
        "/darKponey <code>@usuario</code>|<code>user_id</code> <code>cantidad</code> — (Admin) Dar/quitar Kponey\n"
        "\n"
        "<b>🔖 Otros</b>\n"
        "/setsprogreso — Ver progreso de sets/colecciones\n"
        "/set <code>nombre_set</code> — Ver detalles de un set\n"
        "/miid — Consultar tu ID de Telegram\n"
        "/bonoidolday <code>user_id</code> <code>cantidad</code> — (Admin) Dar bonos de tiradas extra\n"
    )
    update.message.reply_text(texto, parse_mode='HTML')

def comando_mercado(update, context):
    user_id = update.message.from_user.id
    chat_id = update.effective_chat.id
    # Mensaje inicial, muestra la primera página
    msg = context.bot.send_message(
        chat_id=chat_id,
        text="Cargando mercado...",
    )
    mostrar_mercado_pagina(chat_id, msg.message_id, context, user_id, pagina=1)



def comando_giveidol(update, context):
    # Uso: /giveidol <id_unico> @usuario_destino
    if len(context.args) < 2:
        update.message.reply_text("Uso: /giveidol <id_unico> @usuario_destino")
        return
    id_unico = context.args[0].strip()
    user_dest = context.args[1].strip()
    user_id = update.message.from_user.id
    chat = update.effective_chat

    # Buscar la carta exacta del usuario por id_unico
    carta = col_cartas_usuario.find_one({"user_id": user_id, "id_unico": id_unico})
    if not carta:
        update.message.reply_text("No tienes esa carta para regalar.")
        return

    # Buscar id Telegram del destino
    if user_dest.startswith('@'):
        username_dest = user_dest[1:].lower()
        posible = col_usuarios.find_one({"username": username_dest})
        if posible:
            target_user_id = posible["user_id"]
        else:
            try:
                member = context.bot.get_chat_member(chat.id, username_dest)
                if member and member.user and member.user.username and member.user.username.lower() == username_dest:
                    target_user_id = member.user.id
            except Exception:
                target_user_id = None
    else:
        try:
            target_user_id = int(user_dest)
        except:
            target_user_id = None

    if not target_user_id:
        update.message.reply_text("No pude identificar al usuario destino. Usa @username o el ID numérico de Telegram.")
        return
    if user_id == target_user_id:
        update.message.reply_text("No puedes regalarte cartas a ti mismo.")
        return

    # Quitar carta al remitente
    col_cartas_usuario.delete_one({"user_id": user_id, "id_unico": id_unico})

    # Entregar carta al destinatario (misma id_unico)
    carta["user_id"] = target_user_id
    col_cartas_usuario.insert_one(carta)

    update.message.reply_text(
        f"🎁 ¡Carta [{id_unico}] enviada correctamente a <b>@{user_dest.lstrip('@')}</b>!",
        parse_mode='HTML'
    )
    try:
        notif = (
            f"🎉 <b>¡Has recibido una carta!</b>\n"
            f"Te han regalado <b>{id_unico}</b>.\n"
            f"¡Revisa tu álbum con <code>/album</code>!"
        )
        context.bot.send_message(chat_id=target_user_id, text=notif, parse_mode='HTML')
    except Exception:
        pass


def mostrar_album_pagina(
    chat_id, 
    message_id, 
    context, 
    user_id, 
    pagina=1, 
    filtro=None, 
    valor_filtro=None, 
    orden=None, 
    solo_botones=False,  # Para refrescar solo botones al abrir filtros
):
    # === 1. Consulta cartas del usuario y aplica filtro ===
    query_album = {"user_id": user_id}
    if filtro == "estrellas":
        query_album["estrellas"] = valor_filtro
    elif filtro == "grupo":
        query_album["grupo"] = valor_filtro

    cartas = list(col_cartas_usuario.find(query_album))
    # === 2. Ordenamiento ===
    if orden == "menor":
        cartas.sort(key=lambda x: x.get("card_id", 0))
    elif orden == "mayor":
        cartas.sort(key=lambda x: -x.get("card_id", 0))
    else:
        # Ordena por nombre y luego grupo, siempre
        cartas.sort(key=lambda x: (x.get("grupo", "").lower(), x.get("nombre", "").lower(), x.get("card_id", 0)))

    # === 3. Paginación ===
    cartas_por_pagina = 10
    total_paginas = max(1, ((len(cartas) - 1) // cartas_por_pagina) + 1)
    pagina = max(1, min(pagina, total_paginas))
    inicio = (pagina - 1) * cartas_por_pagina
    fin = inicio + cartas_por_pagina
    cartas_pagina = cartas[inicio:fin]

    texto = f"📗 <b>Álbum de cartas (página {pagina}/{total_paginas})</b>\n\n"

    ANCHO_ID = 5    
    ANCHO_EST = 5

    def corta(txt, n):
        return (txt[:n-1] + "…") if len(txt) > n else txt

    if cartas_pagina:
        for c in cartas_pagina:
            idu = str(c['id_unico']).ljust(ANCHO_ID)
            est = f"[{c.get('estrellas','?')}]".ljust(ANCHO_EST)
            num = f"#{c.get('card_id','?')}"
            ver = f"[{c.get('version','?')}]"
            nom = c.get('nombre','?')
            grp = c.get('grupo','?')
            texto += f"• <code>{idu}</code> · {est} · {num} · {ver} · {nom} · {grp}\n"
    else:
        texto += "\n(No tienes cartas para mostrar con este filtro)\n"

    texto += '\n<i>Usa <b>/ampliar &lt;id_unico&gt;</b> para ver detalles de cualquier carta.</i>'



    # === 4. Botones ===
    botones = []
    # Filtrar / Ordenar (si no estamos viendo los sub-menús de filtros)
    if not solo_botones:
        botones.append([InlineKeyboardButton("🔎 Filtrar / Ordenar", callback_data=f"album_filtros_{user_id}_{pagina}")])

    # Paginación
    paginacion = []
    if pagina > 1:
        paginacion.append(InlineKeyboardButton("⬅️", callback_data=f"album_pagina_{user_id}_{pagina-1}_{filtro or 'none'}_{valor_filtro or 'none'}_{orden or 'none'}"))
    if pagina < total_paginas:
        paginacion.append(InlineKeyboardButton("➡️", callback_data=f"album_pagina_{user_id}_{pagina+1}_{filtro or 'none'}_{valor_filtro or 'none'}_{orden or 'none'}"))
    if paginacion and not solo_botones:
        botones.append(paginacion)

    teclado = InlineKeyboardMarkup(botones)

    # Si estamos cambiando solo los botones (al entrar a filtros), editamos solo el teclado
    if solo_botones:
        context.bot.edit_message_reply_markup(
            chat_id=chat_id, 
            message_id=message_id, 
            reply_markup=teclado
        )
        return

    context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=texto,
        parse_mode="HTML",
        reply_markup=teclado
    )

def mostrar_menu_filtros_album(user_id, pagina):
    botones = [
        [InlineKeyboardButton("⭐ Filtrar por Estado", callback_data=f"album_filtro_estado_{user_id}_{pagina}")],
        [InlineKeyboardButton("👥 Filtrar por Grupo", callback_data=f"album_filtro_grupo_{user_id}_1")],
        [InlineKeyboardButton("🔢 Ordenar por Número", callback_data=f"album_filtro_numero_{user_id}_{pagina}")],
        [InlineKeyboardButton("⬅️ Volver", callback_data=f"album_pagina_{user_id}_{pagina}_none_none_none")]
    ]
    return InlineKeyboardMarkup(botones)


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
    por_pagina = 5
    total = len(grupos)
    paginas = max(1, (total - 1) // por_pagina + 1)
    if pagina < 1: pagina = 1
    if pagina > paginas: pagina = paginas
    inicio = (pagina - 1) * por_pagina
    fin = min(inicio + por_pagina, total)
    grupos_pagina = grupos[inicio:fin]

    matriz = []
    for g in grupos_pagina:
        matriz.append([InlineKeyboardButton(g, callback_data=f"album_filtragrupo_{user_id}_{pagina}_{g}")])

    nav = []
    if pagina > 1:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"album_filtro_grupo_{user_id}_{pagina-1}"))
    if pagina < paginas:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"album_filtro_grupo_{user_id}_{pagina+1}"))
    if nav:
        matriz.append(nav)
    matriz.append([InlineKeyboardButton("⬅️ Volver", callback_data=f"album_filtros_{user_id}_{pagina}")])

    return InlineKeyboardMarkup(matriz)


def mostrar_menu_ordenar_album(user_id, pagina):
    botones = [
        [InlineKeyboardButton("⬆️ Menor a mayor", callback_data=f"album_ordennum_{user_id}_{pagina}_menor")],
        [InlineKeyboardButton("⬇️ Mayor a menor", callback_data=f"album_ordennum_{user_id}_{pagina}_mayor")],
        [InlineKeyboardButton("⬅️ Volver", callback_data=f"album_filtros_{user_id}_{pagina}")]
    ]
    return InlineKeyboardMarkup(botones)






# --------- Sets/Progreso ---------
def obtener_sets_disponibles():
    sets = set()
    for carta in cartas:
        if "set" in carta:
            sets.add(carta["set"])
        elif "grupo" in carta:
            sets.add(carta["grupo"])
    return sorted(list(sets), key=lambda s: s.lower())

def mostrar_setsprogreso(update, context, pagina=1, mensaje=None, editar=False):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    sets = obtener_sets_disponibles()
    cartas_usuario = list(col_cartas_usuario.find({"user_id": user_id}))
    # El usuario puede tener varias copias/estados de una misma carta. Solo cuenta una vez cada (nombre, version).
    cartas_usuario_unicas = set((c["nombre"], c["version"]) for c in cartas_usuario)
    por_pagina = 5
    total = len(sets)
    paginas = (total - 1) // por_pagina + 1
    if pagina < 1: pagina = 1
    if pagina > paginas: pagina = paginas
    inicio = (pagina - 1) * por_pagina
    fin = min(inicio + por_pagina, total)
    texto = "<b>📚 Progreso de sets/colecciones:</b>\n\n"
    for s in sets[inicio:fin]:
        # Solo un registro por (nombre, version)
        cartas_set_unicas = set((c["nombre"], c["version"]) for c in cartas if (c.get("set") == s or c.get("grupo") == s))
        total_set = len(cartas_set_unicas)
        usuario_tiene = sum(1 for carta in cartas_set_unicas if carta in cartas_usuario_unicas)
        if usuario_tiene == 0:
            emoji = "⬜"
        elif usuario_tiene == total_set:
            emoji = "🌟"
        elif usuario_tiene >= total_set // 2:
            emoji = "⭐"
        else:
            emoji = "🔸"
        bloques = 10
        bloques_llenos = int((usuario_tiene / total_set) * bloques) if total_set > 0 else 0
        barra = "🟩" * bloques_llenos + "⬜" * (bloques - bloques_llenos)
        texto += f"{emoji} <b>{s}</b>: {usuario_tiene}/{total_set}\n{barra}\n\n"
    texto += f"Página {pagina}/{paginas}\n"
    texto += "📖 Escribe <b>/set &lt;nombre_set&gt;</b> para ver los detalles de un set.\nEjemplo: <code>/set Twice</code>"
    botones = []
    if pagina > 1:
        botones.append(InlineKeyboardButton("⬅️", callback_data=f"setsprogreso_{pagina-1}"))
    if pagina < paginas:
        botones.append(InlineKeyboardButton("➡️", callback_data=f"setsprogreso_{pagina+1}"))
    teclado = InlineKeyboardMarkup([botones]) if botones else None
    if editar and mensaje:
        try:
            mensaje.edit_text(texto, reply_markup=teclado, parse_mode="HTML")
        except Exception:
            context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado, parse_mode="HTML")
    else:
        context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado, parse_mode="HTML")

def comando_set_detalle(update, context):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not context.args:
        mostrar_lista_set(update, context, pagina=1)
        return
    nombre_set = " ".join(context.args)
    sets = obtener_sets_disponibles()
    nombre_set_normalizado = nombre_set.lower()
    set_match = None
    for s in sets:
        if s.lower() == nombre_set_normalizado:
            set_match = s
            break
    if not set_match:
        mostrar_lista_set(update, context, pagina=1, error=nombre_set)
        return
    mostrar_detalle_set(update, context, set_match, pagina=1)

def mostrar_lista_set(update, context, pagina=1, mensaje=None, editar=False, error=None):
    sets = obtener_sets_disponibles()
    por_pagina = 8
    total = len(sets)
    paginas = (total - 1) // por_pagina + 1
    if pagina < 1: pagina = 1
    if pagina > paginas: pagina = paginas
    inicio = (pagina - 1) * por_pagina
    fin = min(inicio + por_pagina, total)
    texto = "<b>Sets disponibles:</b>\n"
    texto += "\n".join([f"• <code>{s}</code>" for s in sets[inicio:fin]])
    if error:
        texto = f"❌ No se encontró el set <b>{error}</b>.\n\n" + texto
    texto += f"\n\nEjemplo de uso: <code>/set Twice</code>\nPágina {pagina}/{paginas}"
    botones = []
    if pagina > 1:
        botones.append(InlineKeyboardButton("⬅️", callback_data=f"setlist_{pagina-1}"))
    if pagina < paginas:
        botones.append(InlineKeyboardButton("➡️", callback_data=f"setlist_{pagina+1}"))
    teclado = InlineKeyboardMarkup([botones]) if botones else None
    chat_id = update.effective_chat.id
    if editar and mensaje:
        try:
            mensaje.edit_text(texto, reply_markup=teclado, parse_mode="HTML")
        except Exception:
            context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado, parse_mode="HTML")
    else:
        context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado, parse_mode="HTML")

def mostrar_detalle_set(update, context, set_name, pagina=1, mensaje=None, editar=False):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # Todas las cartas del set (puede haber repetidas por estado)
    cartas_set = [c for c in cartas if (c.get("set") == set_name or c.get("grupo") == set_name)]
    # Solo (nombre, version) únicas
    cartas_set_unicas = []
    vistos = set()
    for c in cartas_set:
        key = (c["nombre"], c["version"])
        if key not in vistos:
            cartas_set_unicas.append(c)
            vistos.add(key)

    por_pagina = 8
    total = len(cartas_set_unicas)
    paginas = (total - 1) // por_pagina + 1
    if pagina < 1: pagina = 1
    if pagina > paginas: pagina = paginas
    inicio = (pagina - 1) * por_pagina
    fin = min(inicio + por_pagina, total)

    # Cartas únicas que tiene el usuario (SIN importar el estado)
    cartas_usuario = list(col_cartas_usuario.find({"user_id": user_id}))
    cartas_usuario_unicas = set((c["nombre"], c["version"]) for c in cartas_usuario)

    # Trae favoritos del usuario
    user_doc = col_usuarios.find_one({"user_id": user_id}) or {}
    favoritos = user_doc.get("favoritos", [])

    usuario_tiene = sum(1 for c in cartas_set_unicas if (c["nombre"], c["version"]) in cartas_usuario_unicas)
    bloques = 10
    bloques_llenos = int((usuario_tiene / total) * bloques) if total > 0 else 0
    barra = "🟩" * bloques_llenos + "⬜" * (bloques - bloques_llenos)
    texto = f"<b>🌟 Set: {set_name} ({usuario_tiene}/{total})</b>\n{barra}\n\n"

    for carta in cartas_set_unicas[inicio:fin]:
        key = (carta["nombre"], carta["version"])
        nombre = carta["nombre"]
        version = carta["version"]
        nombre_version = f"[{version}] {nombre}"

        # ¿Es favorito?
        es_fav = any(fav.get("nombre") == nombre and fav.get("version") == version for fav in favoritos)
        icono_fav = " ⭐" if es_fav else ""

        # ¿El usuario tiene la carta?
        if key in cartas_usuario_unicas:
            texto += f"✅ <code>{nombre_version}</code>{icono_fav}\n"
        else:
            texto += f"❌ <code>{nombre_version}</code>{icono_fav}\n"

    # Mensaje de ayuda para favoritos
    texto += (
        "\n<i>Para añadir una carta a favoritos:</i>\n"
        "Copia el nombre (incluyendo los corchetes) y usa:\n"
        "<code>/fav [V1] Tzuyu</code>\n"
    )

    if usuario_tiene == total and total > 0:
        texto += "\n🎉 <b>¡Completaste este set!</b> 🎉"

    # Botones de paginación
    botones = []
    if pagina > 1:
        botones.append(InlineKeyboardButton("⬅️", callback_data=f"setdet_{set_name}_{pagina-1}"))
    if pagina < paginas:
        botones.append(InlineKeyboardButton("➡️", callback_data=f"setdet_{set_name}_{pagina+1}"))
    teclado = InlineKeyboardMarkup([botones]) if botones else None

    if editar and mensaje:
        try:
            mensaje.edit_text(texto, reply_markup=teclado, parse_mode='HTML')
        except Exception:
            context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado, parse_mode='HTML')
    else:
        context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado, parse_mode='HTML')



# ... Igualmente aquí puedes agregar las funciones de setsprogreso, set, etc. como hablamos ...







# --------- CALLBACKS ---------

def callback_ampliar_vender(update, context):
    query = update.callback_query
    data = query.data
    if not data.startswith("ampliar_vender_"):
        return
    id_unico = data.replace("ampliar_vender_", "")
    user_id = query.from_user.id
    carta = col_cartas_usuario.find_one({"user_id": user_id, "id_unico": id_unico})
    if not carta:
        query.answer("No tienes esa carta en tu álbum.", show_alert=True)
        return

    # Realiza venta igual que el comando /vender
    nombre = carta['nombre']
    version = carta['version']
    estado = carta['estado']
    precio = precio_carta_karuta(nombre, version, estado, id_unico=id_unico)
    card_id = carta.get("card_id", extraer_card_id_de_id_unico(id_unico))

    # Ya está en mercado?
    ya = col_mercado.find_one({"id_unico": id_unico})
    if ya:
        query.answer("Esta carta ya está en el mercado.", show_alert=True)
        return

    col_cartas_usuario.delete_one({"user_id": user_id, "id_unico": id_unico})
    estrellas = carta.get('estrellas', '★??')
    col_mercado.insert_one({
       "id_unico": id_unico,
       "vendedor_id": user_id,
       "nombre": nombre,
       "version": version,
       "estado": estado,
       "estrellas": estrellas,
       "precio": precio,
       "card_id": card_id,
       "fecha": datetime.utcnow(),
       "imagen": carta.get("imagen"),
       "grupo": carta.get("grupo", "")
    })

    query.answer("Carta puesta en el mercado.", show_alert=True)
    query.edit_message_caption(
        caption="📦 Carta puesta en el mercado.",
        parse_mode='HTML'
    )

#-------------mostrar_menu_mercado------------





def manejador_callback(update, context):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id

    # Sólo para callbacks que inician con mercado_
    if data.startswith("mercado"):
        partes = data.split("_")
        try:
            dueño_id = None
            for part in partes:
                if part.isdigit() and len(part) >= 5:
                    dueño_id = int(part)
                    break
        except Exception:
            dueño_id = None

        if dueño_id and user_id != dueño_id:
            query.answer("Solo puedes interactuar con tu propio mercado.", show_alert=True)
            return

    # Solo manejar callbacks del mercado
    if not data.startswith("mercado"):
        # ...deja el resto de tus callbacks aquí
        return

    partes = data.split("_")

    if data.startswith("mercado_filtros_"):
        user_id = int(partes[2])
        pagina = int(partes[3])
        query.edit_message_reply_markup(reply_markup=mostrar_menu_filtros(user_id, pagina))
        return

    elif data.startswith("mercado_filtro_estado_"):
        user_id = int(partes[3])
        pagina = int(partes[4])
        query.edit_message_reply_markup(reply_markup=mostrar_menu_estrellas(user_id, pagina))
        return

    elif data.startswith("mercado_filtraestrella_"):
        user_id = int(partes[2])
        pagina = int(partes[3])
        estrellas = partes[4]
        mostrar_mercado_pagina(query.message.chat_id, query.message.message_id, context, user_id, pagina, filtro="estrellas", valor_filtro=estrellas)
        return

    elif data.startswith("mercado_filtro_grupo_"):
        partes = data.split("_")
        user_id = int(partes[-2])
        pagina = int(partes[-1])
        grupos = obtener_grupos_del_mercado()  # Pon aquí tu función para obtener los grupos
        try:
            query.edit_message_reply_markup(reply_markup=mostrar_menu_grupos(user_id, pagina, grupos))
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                print("Error en menu grupos:", e)
        return


    elif data.startswith("mercado_filtragrupo_"):
        user_id = int(partes[2])
        pagina = int(partes[3])
        grupo = "_".join(partes[4:])
        mostrar_mercado_pagina(query.message.chat_id, query.message.message_id, context, user_id, pagina, filtro="grupo", valor_filtro=grupo)
        return

    elif data.startswith("mercado_filtro_numero_"):
        user_id = int(partes[3])
        pagina = int(partes[4])
        query.edit_message_reply_markup(reply_markup=mostrar_menu_ordenar(user_id, pagina))
        return

    elif data.startswith("mercado_ordennum_"):
        user_id = int(partes[2])
        pagina = int(partes[3])
        orden = partes[4]
        mostrar_mercado_pagina(query.message.chat_id, query.message.message_id, context, user_id, pagina, orden=orden)
        return

    elif data.startswith("mercado_pagina_"):
        user_id = int(partes[2])
        pagina = int(partes[3])
        filtro = partes[4] if partes[4] != "none" else None
        valor_filtro = partes[5] if partes[5] != "none" else None
        orden = partes[6] if len(partes) > 6 and partes[6] != "none" else None
        mostrar_mercado_pagina(query.message.chat_id, query.message.message_id, context, user_id, int(pagina), filtro=filtro, valor_filtro=valor_filtro, orden=orden)
        return


    #----------Album--------------

def manejador_callback_album(update, context):
    query = update.callback_query
    data = query.data
    partes = data.split("_")
    user_id = query.from_user.id

    # --- Filtro por estrellas (estado) ---
    if data.startswith("album_filtro_estado_"):
        user_id = int(partes[-2])
        pagina = int(partes[-1])
        context.bot.edit_message_reply_markup(
            chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            reply_markup=mostrar_menu_estrellas_album(user_id, pagina)
        )
        return

    # --- Filtro aplicado por estrella ---
    if data.startswith("album_filtraestrella_"):
        user_id = int(partes[2])
        pagina = int(partes[3])
        estrellas = partes[4]
        mostrar_album_pagina(query.message.chat_id, query.message.message_id, context, user_id, pagina, filtro="estrellas", valor_filtro=estrellas)
        return

    # --- Filtro por grupo ---
    if data.startswith("album_filtro_grupo_"):
    # Extrae user_id y página correctamente aunque la callback tenga _ adicionales
        partes_split = data.split("_")
        user_id = int(partes_split[3])
        if len(partes_split) > 4:
            pagina = int(partes_split[4])
        else:
            pagina = 1
        grupos = sorted({c.get("grupo", "") for c in col_cartas_usuario.find({"user_id": user_id}) if c.get("grupo")})
        context.bot.edit_message_reply_markup(
            chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            reply_markup=mostrar_menu_grupos_album(user_id, pagina, grupos)
        )
        return



    # --- Filtro aplicado por grupo ---
    if data.startswith("album_filtragrupo_"):
        user_id = int(partes[2])
        pagina = int(partes[3])
        grupo = "_".join(partes[4:])
        mostrar_album_pagina(query.message.chat_id, query.message.message_id, context, user_id, pagina, filtro="grupo", valor_filtro=grupo)
        return

    # --- Menú de filtros principal ---
    if data.startswith("album_filtros_"):
        user_id = int(partes[2])
        pagina = int(partes[3])
        context.bot.edit_message_reply_markup(
            chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            reply_markup=mostrar_menu_filtros_album(user_id, pagina)
        )
        return

    # --- Filtro ordenar por número ---
    if data.startswith("album_filtro_numero_"):
        user_id = int(partes[3])
        pagina = int(partes[4])
        context.bot.edit_message_reply_markup(
            chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            reply_markup=mostrar_menu_ordenar_album(user_id, pagina)
        )
        return

    # --- Orden aplicado ---
    if data.startswith("album_ordennum_"):
        user_id = int(partes[2])
        pagina = int(partes[3])
        orden = partes[4]
        mostrar_album_pagina(query.message.chat_id, query.message.message_id, context, user_id, pagina, orden=orden)
        return

    # --- Volver al álbum completo (sin filtros) ---
    if data.startswith("album_pagina_"):
        user_id = int(partes[2])
        pagina = int(partes[3])
        filtro = partes[4] if len(partes) > 4 and partes[4] != "none" else None
        valor_filtro = partes[5] if len(partes) > 5 and partes[5] != "none" else None
        orden = partes[6] if len(partes) > 6 and partes[6] != "none" else None
        mostrar_album_pagina(query.message.chat_id, query.message.message_id, context, user_id, int(pagina), filtro=filtro, valor_filtro=valor_filtro, orden=orden)
        return


   
    
    
    # --- RECLAMAR DROP ---
    if data.startswith("reclamar"):
        manejador_reclamar(update, context)
        return

    # --- EXPIRADO / RECLAMADA ---
    if data == "expirado":
        query.answer("Este drop ha expirado.", show_alert=True)
        return
    if data == "reclamada":
        query.answer("Esta carta ya fue reclamada.", show_alert=True)
        return

    # --- VER CARTA INDIVIDUAL ---
    if data.startswith("vercarta"):
        partes = data.split("_")
        if len(partes) != 3:
            query.answer()
            return
        user_id = int(partes[1])
        id_unico = partes[2]
        if query.from_user.id != user_id:
            query.answer(text="Solo puedes ver tus propias cartas.", show_alert=True)
            return
        carta = col_cartas_usuario.find_one({"user_id": user_id, "id_unico": id_unico})
        if not carta:
            query.answer(text="Esa carta no existe.", show_alert=True)
            return
        mostrar_carta_individual(
            query.message.chat_id,
            user_id,
            [carta],
            0,
            context,
            query=query
        )
        query.answer()
        return



    # --- REGALAR CARTA ---
    if data.startswith("regalar_"):
        partes = data.split("_")
        if len(partes) != 3:
            query.answer()
            return
        user_id = int(partes[1])
        idx = int(partes[2])
        if query.from_user.id != user_id:
            query.answer(text="Solo puedes regalar tus propias cartas.", show_alert=True)
            return
        cartas_usuario = list(col_cartas_usuario.find({"user_id": user_id}))
        def sort_key(x):
            grupo = grupo_de_carta(x.get('nombre', ''), x.get('version', '')) or ""
            return (
                grupo.lower(),
                x.get('nombre', '').lower(),
                x.get('card_id', 0)
            )
        cartas_usuario.sort(key=sort_key)
        if idx < 0 or idx >= len(cartas_usuario):
            query.answer(text="Esa carta no existe.", show_alert=True)
            return
        carta = cartas_usuario[idx]
        SESIONES_REGALO[user_id] = {
            "carta": carta,
            "msg_id": query.message.message_id,
            "chat_id": query.message.chat_id,
            "tiempo": time.time()
        }
        query.edit_message_reply_markup(reply_markup=None)
        context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"¿A quién quieres regalar esta carta?\n\n"
                 f"<b>{carta['nombre']}</b> [{carta['version']}] - {carta['estado']}\n"
                 f"ID: <code>{carta['id_unico']}</code>\n\n"
                 f"Escribe el @usuario, el ID numérico, o <b>cancelar</b> para abortar.",
            parse_mode="HTML"
        )
        query.answer()
        return

    # --- PAGINACIÓN PROGRESO SETS ---
    if data.startswith("setsprogreso_"):
        pagina = int(data.split("_")[1])
        mostrar_setsprogreso(update, context, pagina=pagina, mensaje=query.message, editar=True)
        query.answer()
        return

    # --- PAGINACIÓN LISTA SETS ---
    if data.startswith("setlist_"):
        pagina = int(data.split("_")[1])
        mostrar_lista_set(update, context, pagina=pagina, mensaje=query.message, editar=True)
        query.answer()
        return

    # --- PAGINACIÓN DETALLE SET ---
    if data.startswith("setdet_"):
        partes = data.split("_")
        set_name = "_".join(partes[1:-1])
        pagina = int(partes[-1])
        mostrar_detalle_set(update, context, set_name, pagina=pagina, mensaje=query.message, editar=True)
        query.answer()
        return

    # --- PAGINACIÓN ÁLBUM CON FILTRO ---
    partes = data.split("_", 3)
    if len(partes) >= 3 and partes[0] == "lista":
        pagina = int(partes[1])
        user_id = int(partes[2])
        filtro = partes[3].strip().lower() if len(partes) > 3 and partes[3] else None
        if query.from_user.id != user_id:
            query.answer(text="Este álbum no es tuyo.", show_alert=True)
            return
        cartas_usuario = list(col_cartas_usuario.find({"user_id": user_id}))
        if filtro:
            cartas_usuario = [
                carta for carta in cartas_usuario if
                filtro in carta.get('nombre', '').lower() or
                filtro in carta.get('grupo', '').lower() or
                filtro in carta.get('version', '').lower()
            ]
        def sort_key(x):
            grupo = grupo_de_carta(x.get('nombre', ''), x.get('version', '')) or ""
            return (
                grupo.lower(),
                x.get('nombre', '').lower(),
                x.get('card_id', 0)
            )
        cartas_usuario.sort(key=sort_key)
        enviar_lista_pagina(
            query.message.chat_id,
            user_id,
            cartas_usuario,
            pagina,
            context,
            editar=True,
            mensaje=query.message,
            filtro=filtro
        )
        query.answer()
        return

    # --- PAGINACIÓN DE MEJORAR ---
    if data.startswith("mejorarpag_"):
        partes = data.split("_")
        pagina = int(partes[1])
        user_id = int(partes[2])
        if query.from_user.id != user_id:
            query.answer("Solo puedes ver tu propio menú de mejora.", show_alert=True)
            return
        cartas_usuario = list(col_cartas_usuario.find({"user_id": user_id}))
        cartas_mejorables = [
            c for c in cartas_usuario
            if c.get("estrellas", "") != "★★★"
        ]
        # ORDENAR SIEMPRE antes de mostrar
        cartas_mejorables.sort(
            key=lambda x: (
                x.get("nombre", "").lower(),
                x.get("version", "").lower()
            )
        )
        mostrar_lista_mejorables(
            update, context, user_id, cartas_mejorables, pagina,
            mensaje=query.message, editar=True
        )
        query.answer()
        return



        

def callback_comprarobj(update, context):
    query = update.callback_query
    data = query.data
    if not data.startswith("comprarobj_"):
        return
    obj_id = data.replace("comprarobj_", "")
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    # Cambia aquí: usa query.answer para mostrar el mensaje en alerta
    def reply_func(text, **kwargs):
        query.answer(text=text, show_alert=True)

    comprar_objeto(user_id, obj_id, context, chat_id, reply_func)


   
    # --- VER CARTA INDIVIDUAL ---
    if data.startswith("vercarta"):
        partes = data.split("_")
        if len(partes) != 3:
            query.answer()
            return
        user_id = int(partes[1])
        id_unico = partes[2]
        if query.from_user.id != user_id:
            query.answer(text="Solo puedes ver tus propias cartas.", show_alert=True)
            return
        carta = col_cartas_usuario.find_one({"user_id": user_id, "id_unico": id_unico})
        if not carta:
            query.answer(text="Esa carta no existe.", show_alert=True)
            return
        mostrar_carta_individual(
            query.message.chat_id,
            user_id,
            [carta],
            0,
            context,
            query=query
        )
        query.answer()
        return




    # --- REGALAR CARTA ---
    if data.startswith("regalar_"):
        partes = data.split("_")
        if len(partes) != 3:
            query.answer()
            return
        user_id = int(partes[1])
        idx = int(partes[2])
        if query.from_user.id != user_id:
            query.answer(text="Solo puedes regalar tus propias cartas.", show_alert=True)
            return
        cartas_usuario = list(col_cartas_usuario.find({"user_id": user_id}))
        def sort_key(x):
            grupo = grupo_de_carta(x.get('nombre', ''), x.get('version', '')) or ""
            return (
                grupo.lower(),
                x.get('nombre', '').lower(),
                x.get('card_id', 0)
            )
        cartas_usuario.sort(key=sort_key)
        if idx < 0 or idx >= len(cartas_usuario):
            query.answer(text="Esa carta no existe.", show_alert=True)
            return
        carta = cartas_usuario[idx]
        SESIONES_REGALO[user_id] = {
            "carta": carta,
            "msg_id": query.message.message_id,
            "chat_id": query.message.chat_id,
            "tiempo": time.time()
        }
        query.edit_message_reply_markup(reply_markup=None)
        context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"¿A quién quieres regalar esta carta?\n\n"
                 f"<b>{carta['nombre']}</b> [{carta['version']}] - {carta['estado']}\n"
                 f"ID: <code>{carta['id_unico']}</code>\n\n"
                 f"Escribe el @usuario, el ID numérico, o <b>cancelar</b> para abortar.",
            parse_mode="HTML"
        )
        query.answer()
        return

    # --- PAGINACIÓN PROGRESO SETS ---
    if data.startswith("setsprogreso_"):
        pagina = int(data.split("_")[1])
        mostrar_setsprogreso(update, context, pagina=pagina, mensaje=query.message, editar=True)
        query.answer()
        return

    # --- PAGINACIÓN LISTA SETS ---
    if data.startswith("setlist_"):
        pagina = int(data.split("_")[1])
        mostrar_lista_set(update, context, pagina=pagina, mensaje=query.message, editar=True)
        query.answer()
        return

    # --- PAGINACIÓN DETALLE SET ---
    if data.startswith("setdet_"):
        partes = data.split("_")
        set_name = "_".join(partes[1:-1])
        pagina = int(partes[-1])
        mostrar_detalle_set(update, context, set_name, pagina=pagina, mensaje=query.message, editar=True)
        query.answer()
        return


def callback_mejorar_carta(update, context):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    if not data.startswith("mejorar_"):
        return
    id_unico = data.split("_", 1)[1]

    carta = col_cartas_usuario.find_one({"user_id": user_id, "id_unico": id_unico})
    if not carta:
        query.answer("No tienes esa carta.", show_alert=True)
        return

    user = col_usuarios.find_one({"user_id": user_id}) or {}
    objetos = user.get("objetos", {})
    lightsticks = objetos.get("lightstick", 0)
    if lightsticks < 1:
        query.answer("No tienes ningún Lightstick.", show_alert=True)
        return

    # Mejora según estado actual
    estrellas_actual = carta.get("estrellas", "")
    mejoras = {
        "☆☆☆": ("★☆☆", 1.00),
        "★☆☆": ("★★☆", 0.70),
        "★★☆": ("★★★", 0.40),
        "★★★": (None, 0.00)
    }
    if estrellas_actual not in mejoras or mejoras[estrellas_actual][0] is None:
        query.answer("Esta carta no se puede mejorar más.", show_alert=True)
        return

    estrellas_nuevo, prob = mejoras[estrellas_actual]
    prob_percent = int(prob * 100)
    texto = (
        f"Vas a usar 1 💡 Lightstick para intentar mejorar esta carta:\n"
        f"<b>{carta.get('nombre','')} [{carta.get('version','')}]</b>\n"
        f"Estado actual: <b>{estrellas_actual}</b>\n"
        f"Posibilidad de mejora: <b>{prob_percent}%</b>\n\n"
        f"¿Deseas continuar?"
    )
    botones = [
        [
            InlineKeyboardButton("✅ Mejorar", callback_data=f"confirmamejora_{id_unico}"),
            InlineKeyboardButton("❌ Cancelar", callback_data="cancelarmejora")
        ]
    ]
    query.edit_message_text(texto, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(botones))
    query.answer()



def callback_confirmar_mejora(update, context):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    if data.startswith("confirmamejora_"):
        id_unico = data.split("_", 1)[1]
        carta = col_cartas_usuario.find_one({"user_id": user_id, "id_unico": id_unico})
        if not carta:
            query.answer("No tienes esa carta.", show_alert=True)
            return
        user = col_usuarios.find_one({"user_id": user_id}) or {}
        objetos = user.get("objetos", {})
        lightsticks = objetos.get("lightstick", 0)
        if lightsticks < 1:
            query.answer("No tienes ningún Lightstick.", show_alert=True)
            return

        mejoras = {
            "☆☆☆": ("★☆☆", 1.00),
            "★☆☆": ("★★☆", 0.70),
            "★★☆": ("★★★", 0.40),
        }
        estrellas_actual = carta.get("estrellas", "")
        if estrellas_actual not in mejoras:
            query.answer("Esta carta no puede mejorar.", show_alert=True)
            return

        estrellas_nuevo, prob = mejoras[estrellas_actual]
        import random
        mejora_exitosa = random.random() < prob

        if mejora_exitosa:
            # 1. Buscar en el catálogo la carta con el nuevo estado (estrellas)
            nombre = carta.get("nombre")
            version = carta.get("version")
            # Busca el objeto carta correspondiente al nuevo estado
            carta_nueva = None
            for c in cartas:
                if (
                    c["nombre"] == nombre and
                    c["version"] == version and
                    c.get("estado_estrella", "") == estrellas_nuevo
                ):
                    carta_nueva = c
                    break
            if carta_nueva:
                nuevo_estado = carta_nueva.get("estado", carta.get("estado"))
                nueva_imagen = carta_nueva.get("imagen", carta.get("imagen"))
            else:
                # Si no la encuentra, solo cambia las estrellas
                nuevo_estado = carta.get("estado")
                nueva_imagen = carta.get("imagen")

            # 2. Actualizar todos los campos sincronizados en Mongo
            col_cartas_usuario.update_one(
                {"user_id": user_id, "id_unico": id_unico},
                {
                    "$set": {
                        "estrellas": estrellas_nuevo,
                        "estado": nuevo_estado,
                        "imagen": nueva_imagen
                    }
                }
            )
            resultado = f"¡Éxito! Tu carta ahora es <b>{estrellas_nuevo}</b> y ha mejorado a <b>{nuevo_estado}</b>."
        else:
            resultado = "Fallaste el intento de mejora. La carta se mantiene igual."

        # Gasta lightstick (SIEMPRE, falles o aciertes)
        col_usuarios.update_one({"user_id": user_id}, {"$inc": {"objetos.lightstick": -1}})
        query.edit_message_text(resultado, parse_mode="HTML")
        query.answer("¡Listo!")

    elif data == "cancelarmejora":
        query.edit_message_text("Operación cancelada.")
        query.answer("Cancelado.")




# ====== FIN MANEJADOR CALLBACK ======


#------------------------------------------------------------
from telegram.ext import MessageHandler, Filters

def handler_regalo_respuesta(update, context):
    user_id = update.message.from_user.id
    if user_id not in SESIONES_REGALO:
        return  # No está esperando nada

    data = SESIONES_REGALO[user_id]
    carta = data["carta"]
    destino = update.message.text.strip()

    # Si usuario escribe 'cancelar' (en cualquier forma)
    if destino.lower().strip() == "cancelar":
        update.message.reply_text("❌ Regalo cancelado. La carta sigue en tu álbum.")
        del SESIONES_REGALO[user_id]
        return

    # Buscar id Telegram del destino
    if destino.startswith('@'):
        username_dest = destino[1:].lower()
        posible = col_usuarios.find_one({"username": username_dest})
        if posible:
            target_user_id = posible["user_id"]
        else:
            update.message.reply_text("❌ No pude identificar al usuario destino. Usa @username (de alguien que haya usado el bot) o el ID numérico de Telegram.")
            del SESIONES_REGALO[user_id]
            return
    else:
        try:
            target_user_id = int(destino)
        except:
            update.message.reply_text("❌ No pude identificar al usuario destino. Usa @username (de alguien que haya usado el bot) o el ID numérico de Telegram.")
            del SESIONES_REGALO[user_id]
            return

    if user_id == target_user_id:
        update.message.reply_text("No puedes regalarte cartas a ti mismo.")
        del SESIONES_REGALO[user_id]
        return

    # Quitar carta al remitente (verifica que aún la tenga)
    res = col_cartas_usuario.delete_one({"user_id": user_id, "id_unico": carta["id_unico"]})

    if res.deleted_count == 0:
        update.message.reply_text("Parece que ya no tienes esa carta.")
        del SESIONES_REGALO[user_id]
        return

    # Entregar carta al destinatario (misma id_unico)
    carta["user_id"] = target_user_id
    col_cartas_usuario.insert_one(carta)

    # Notificación pública y privada
    try:
        update.message.reply_text(f"🎁 ¡Carta [{carta['id_unico']}] enviada correctamente!")
        notif = (
            f"🎉 <b>¡Has recibido una carta!</b>\n"
            f"Te han regalado <b>{carta['id_unico']}</b> ({carta['nombre']} [{carta['version']}])\n"
            f"¡Revisa tu álbum con <code>/album</code>!"
        )
        context.bot.send_message(chat_id=target_user_id, text=notif, parse_mode='HTML')
    except Exception:
        update.message.reply_text("La carta fue enviada, pero no pude notificar al usuario destino en privado.")
    del SESIONES_REGALO[user_id]

    # Entregar carta al destinatario (misma id_unico)
    carta["user_id"] = target_user_id
    col_cartas_usuario.insert_one(carta)

    # Notificación pública y privada
    try:
        update.message.reply_text(f"🎁 ¡Carta [{carta['id_unico']}] enviada correctamente!")
        notif = (
            f"🎉 <b>¡Has recibido una carta!</b>\n"
            f"Te han regalado <b>{carta['id_unico']}</b> ({carta['nombre']} [{carta['version']}])\n"
            f"¡Revisa tu álbum con <code>/album</code>!"
        )
        context.bot.send_message(chat_id=target_user_id, text=notif, parse_mode='HTML')
    except Exception:
        update.message.reply_text("La carta fue enviada, pero no pude notificar al usuario destino en privado.")
    del SESIONES_REGALO[user_id]

def comando_setsprogreso(update, context):
    mostrar_setsprogreso(update, context, pagina=1)


@cooldown_critico
def comando_apodo(update, context):
    user_id = update.message.from_user.id

    if len(context.args) < 2:
        update.message.reply_text(
            'Uso: /apodo <id_unico> "apodo con comillas"\nEjemplo: /apodo fghj7 "Mi bebe"'
        )
        return

    id_unico = context.args[0].strip()
    # Apodo puede contener espacios y comillas, así que une el resto y limpia las comillas
    apodo = " ".join(context.args[1:])
    apodo = apodo.strip('"').strip()

    if not (1 <= len(apodo) <= 8):
        update.message.reply_text("El apodo debe tener entre 1 y 8 caracteres.")
        return

    # Buscar la carta
    carta = col_cartas_usuario.find_one({"user_id": user_id, "id_unico": id_unico})
    if not carta:
        update.message.reply_text("No encontré esa carta en tu álbum.")
        return

    # Verificar que el usuario tenga el ticket
    doc_usuario = col_usuarios.find_one({"user_id": user_id}) or {}
    objetos = doc_usuario.get("objetos", {})
    ticket_apodo = objetos.get("ticket_agregar_apodo", 0)
    if ticket_apodo < 1:
        update.message.reply_text("No tienes tickets para agregar apodos. Cómpralo en /tienda.")
        return

    # Consumir ticket
    col_usuarios.update_one(
        {"user_id": user_id},
        {"$inc": {"objetos.ticket_agregar_apodo": -1}}
    )
    # Actualizar carta con apodo
    col_cartas_usuario.update_one(
        {"user_id": user_id, "id_unico": id_unico},
        {"$set": {"apodo": apodo}}
    )
    update.message.reply_text(
        f'✅ Apodo <b>"{apodo}"</b> asignado correctamente a tu carta <code>{id_unico}</code>.',
        parse_mode="HTML"
    )


dispatcher.add_handler(CallbackQueryHandler(manejador_callback_album, pattern="^album_"))
dispatcher.add_handler(CallbackQueryHandler(callback_comprarobj, pattern="^comprarobj_"))
dispatcher.add_handler(CallbackQueryHandler(callback_ampliar_vender, pattern="^ampliar_vender_"))
dispatcher.add_handler(CallbackQueryHandler(callback_mejorar_carta, pattern="^mejorar_"))
dispatcher.add_handler(CallbackQueryHandler(callback_confirmar_mejora, pattern="^(confirmamejora_|cancelarmejora)"))
dispatcher.add_handler(CallbackQueryHandler(manejador_callback, pattern="^mercado_"))
dispatcher.add_handler(CallbackQueryHandler(manejador_tienda_paypal, pattern=r"^tienda_paypal_"))
dispatcher.add_handler(CallbackQueryHandler(manejador_callback))
dispatcher.add_handler(CommandHandler('mercado', comando_mercado))
dispatcher.add_handler(CommandHandler('tiendagemas', tienda_gemas))
dispatcher.add_handler(CommandHandler('darGemas', comando_darGemas))
dispatcher.add_handler(CommandHandler('gemas', comando_gemas))
dispatcher.add_handler(CommandHandler('usar', comando_usar))
dispatcher.add_handler(CommandHandler('apodo', comando_apodo))
dispatcher.add_handler(CommandHandler('inventario', comando_inventario))
dispatcher.add_handler(CommandHandler('tienda', comando_tienda))
dispatcher.add_handler(CommandHandler('comprarobjeto', comando_comprarobjeto))
dispatcher.add_handler(CommandHandler('idolday', comando_idolday))
dispatcher.add_handler(CommandHandler('album', comando_album))
dispatcher.add_handler(CommandHandler('miid', comando_miid))
dispatcher.add_handler(CommandHandler('bonoidolday', comando_bonoidolday))
dispatcher.add_handler(CommandHandler('comandos', comando_comandos))
dispatcher.add_handler(CommandHandler('giveidol', comando_giveidol))
dispatcher.add_handler(CommandHandler('setsprogreso', comando_setsprogreso))
dispatcher.add_handler(CommandHandler('set', comando_set_detalle))
dispatcher.add_handler(MessageHandler(Filters.text & (~Filters.command), handler_regalo_respuesta))
dispatcher.add_handler(CommandHandler('ampliar', comando_ampliar))
dispatcher.add_handler(CommandHandler('kponey', comando_saldo))
dispatcher.add_handler(CommandHandler('darKponey', comando_darKponey))
dispatcher.add_handler(CommandHandler('fav', comando_fav))
dispatcher.add_handler(CommandHandler('favoritos', comando_favoritos))
dispatcher.add_handler(CommandHandler('precio', comando_precio))
dispatcher.add_handler(CommandHandler('vender', comando_vender))
dispatcher.add_handler(CommandHandler('comprar', comando_comprar))
dispatcher.add_handler(CommandHandler('retirar', comando_retirar))
dispatcher.add_handler(CommandHandler('mejorar', comando_mejorar))


def verify_paypal_ipn(data):
    verify_url = "https://ipnpb.paypal.com/cgi-bin/webscr"
    data['cmd'] = '_notify-validate'
    resp = requests.post(verify_url, data=data)
    return resp.text == "VERIFIED"

@app.route('/paypal_ipn', methods=['POST'])
def paypal_ipn():
    data = request.form.to_dict()
    print("IPN recibido:", data)

    # 1. Validación anti-fraude de PayPal
    if not verify_paypal_ipn(data):
        return "Invalid IPN", 400

    # 2. Solo pagos completados
    if data.get("payment_status") != "Completed":
        return "Ignored", 200

    # 3. Previene doble entrega (por si PayPal reintenta)
    pago_id = data.get("txn_id")
    if not pago_id:
        return "Sin txn_id", 400
    if db.historial_compras_gemas.find_one({"pago_id": pago_id}):
        return "Ya entregado", 200

    # 4. Obtén el user_id de Telegram
    user_id = data.get("custom")
    if not user_id or not user_id.isdigit():
        return "No user", 400
    user_id = int(user_id)

    # 5. Identifica el pack
    item_name = data.get("item_name", "")
    gems_map = {
        "x50 Gems": 50,
        "x100 Gems": 100,
        "x500 Gems (400 + 100 bonus)": 500,
        "x1000 Gems (850 + 150 bonus)": 1000,
        "x5000 Gems (4000 + 1000 bonus)": 5000,
        "x10000 Gems (8000 + 2000 bonus)": 10000,
    }
    cantidad_gemas = None
    for k in gems_map:
        if k in item_name:
            cantidad_gemas = gems_map[k]
            break
    if not cantidad_gemas:
        return "No pack", 400

    # 6. Busca username (si existe)
    usuario = col_usuarios.find_one({"user_id": user_id}) or {}
    username = usuario.get("username", "")

    # 7. Suma gemas y deja historial
    col_usuarios.update_one(
        {"user_id": user_id},
        {"$inc": {"gemas": cantidad_gemas}},
        upsert=True
    )
    db.historial_compras_gemas.insert_one({
        "pago_id": pago_id,
        "user_id": user_id,
        "username": username.lower() if username else "",
        "cantidad_gemas": cantidad_gemas,
        "item_name": item_name,
        "fecha": datetime.utcnow()
    })

    # 8. Envía alerta solo si el usuario existe en Telegram
    try:
        bot.send_message(
            chat_id=user_id,
            text=f"🎉 ¡Compra exitosa! Recibiste {cantidad_gemas} gemas en KaruKpop. ¡Gracias por tu apoyo!",
        )
    except Exception as e:
        print("No se pudo notificar por Telegram:", e)

    return "OK", 200
    


@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    global primer_mensaje
    update = Update.de_json(request.get_json(force=True), bot)
    if primer_mensaje and getattr(update, "message", None):
        try:
            bot.send_message(chat_id=update.effective_chat.id, text="Bot activo")
        except:
            pass
        primer_mensaje = False
    dispatcher.process_update(update)
    return 'OK'

@app.route("/", methods=["GET"])
def home():
    return "Bot activo."

if __name__ == '__main__':
    puerto = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=puerto)
