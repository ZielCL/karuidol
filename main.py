import os
import threading
import time
from flask import Flask, request
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
from datetime import datetime
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


def revisar_sets_completados(usuario_id, context):
    """
    Revisa si el usuario completó algún set y entrega premios proporcionales,
    enviando la alerta SOLO por privado.
    """
    sets = obtener_sets_disponibles()
    cartas_usuario = list(col_cartas_usuario.find({"user_id": usuario_id}))
    cartas_usuario_unicas = set((c["nombre"], c["version"]) for c in cartas_usuario)

    doc_usuario = col_usuarios.find_one({"user_id": usuario_id}) or {}
    sets_premiados = set(doc_usuario.get("sets_premiados", []))

    premios = []
    for s in sets:
        cartas_set_unicas = set((c["nombre"], c["version"]) for c in cartas if (c.get("set") == s or c.get("grupo") == s))
        if cartas_set_unicas and cartas_set_unicas.issubset(cartas_usuario_unicas) and s not in sets_premiados:
            monto = 500 * len(cartas_set_unicas)  # Puedes ajustar este factor
            premios.append((s, monto))
            sets_premiados.add(s)
            col_usuarios.update_one(
                {"user_id": usuario_id},
                {
                    "$inc": {"kponey": monto},
                    "$set": {"sets_premiados": list(sets_premiados)}
                },
                upsert=True
            )
            # ALERTA PRIVADA:
            try:
                context.bot.send_message(
                    chat_id=usuario_id,
                    text=f"🎉 ¡Completaste el set <b>{s}</b>!\nPremio: <b>+{monto} Kponey 🪙</b>",
                    parse_mode="HTML"
                )
            except Exception:
                pass  # usuario bloqueó el bot, etc.
    return premios








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
    last = user_doc.get('last_idolday')
    ahora = datetime.utcnow()
    cooldown_listo = False
    bono_listo = False

    if last:
        diferencia = ahora - last
        cooldown_listo = diferencia.total_seconds() >= 6 * 3600  # 6 horas
    else:
        cooldown_listo = True

    if bono and bono > 0:
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
    usuario_id = update.message.from_user.id
    chat_id = update.effective_chat.id
    ahora = datetime.utcnow()
    ahora_ts = time.time()
    user_doc = col_usuarios.find_one({"user_id": usuario_id})
    bono = user_doc.get('bono', 0) if user_doc else 0
    last = user_doc.get('last_idolday') if user_doc else None
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
    puede_tirar = False
    cooldown_listo, bono_listo = puede_usar_idolday(usuario_id)

    if cooldown_listo:
        puede_tirar = True
        col_usuarios.update_one(
            {"user_id": usuario_id},
            {"$set": {"last_idolday": ahora}},
            upsert=True
        )
    elif bono_listo:
        puede_tirar = True
        col_usuarios.update_one(
            {"user_id": usuario_id},
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
        "dueño": usuario_id,
        "chat_id": chat_id,
        "mensaje_id": msg_botones.message_id,
        "inicio": time.time(),
        "msg_botones": msg_botones,
        "usuarios_reclamaron": [],
        "expirado": False,
        "primer_reclamo_dueño": None,
    }

    col_usuarios.update_one(
        {"user_id": usuario_id},
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


def manejador_reclamar(update, context):
    print("Entrando a manejador_reclamar...")
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
    solo_dueño = tiempo_desde_drop < 15
    puede_reclamar = False

    user_doc = col_usuarios.find_one({"user_id": usuario_click}) or {}
    bono = user_doc.get('bono', 0)

    # DUEÑO DEL DROP
    if usuario_click == drop["dueño"]:
        primer_reclamo = drop.get("primer_reclamo_dueño")
        if primer_reclamo is None:
            puede_reclamar = True
            drop["primer_reclamo_dueño"] = ahora
        else:
            tiempo_faltante = 15 - (ahora - drop["primer_reclamo_dueño"])
            if tiempo_faltante > 0:
                segundos_faltantes = int(round(tiempo_faltante))
                query.answer(
                    f"Te quedan {segundos_faltantes} segundos para poder reclamar la otra (si tienes bono).",
                    show_alert=True
                )
                return
            if bono < 1:
                query.answer("Necesitas al menos 1 bono para reclamar la segunda carta.", show_alert=True)
                return
            puede_reclamar = True
            col_usuarios.update_one({"user_id": usuario_click}, {"$inc": {"bono": -1}}, upsert=True)
    # NO DUEÑO DEL DROP
    if not solo_dueño and carta["usuario"] is None:
    cooldown_listo, bono_listo = puede_usar_idolday(usuario_click)
    ahora = datetime.utcnow()
    if cooldown_listo:
        puede_reclamar = True
        # Ahora al reclamar carta ajena usando su idolday diario, ponemos cooldown igual que si usara /idolday
        col_usuarios.update_one(
            {"user_id": usuario_click},
            {"$set": {"last_idolday": ahora}},
            upsert=True
        )
    elif bono_listo:
        puede_reclamar = True
        col_usuarios.update_one({"user_id": usuario_click}, {"$inc": {"bono": -1}}, upsert=True)
    else:
        segundos_faltantes = int(15 - tiempo_desde_drop)
        if segundos_faltantes < 0:
            segundos_faltantes = 0
        query.answer("Solo puedes reclamar cartas si tienes disponible tu /idolday o tienes un bono disponible.", show_alert=True)
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

    # Registrar la carta en la colección del usuario
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
    context.bot.send_message(
        chat_id=drop["chat_id"],
        text=f"{user_mention} tomaste la carta <code>{id_unico}</code> #{nuevo_id} [{version}] {nombre} - {grupo}, {frase_estado} está en <b>{estado.lower()}</b>!",
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

# ----------------- Resto de funciones: album, paginación, etc. -----------------
# Aquí pego la versión adaptada de /album para usar id_unico, estrellas y letra pegada a la izquierda:
@cooldown_critico
def comando_album(update, context):
    usuario_id = update.message.from_user.id
    chat_id = update.effective_chat.id
    cartas_usuario = list(col_cartas_usuario.find({"user_id": usuario_id}))
    if not cartas_usuario:
        context.bot.send_message(chat_id=chat_id, text="Tu álbum está vacío.")
        return
    def sort_key(x):
        grupo = grupo_de_carta(x.get('nombre',''), x.get('version','')) or ""
        return (
            grupo.lower(),
            x.get('nombre','').lower(),
            x.get('card_id', 0)
        )
    cartas_usuario.sort(key=sort_key)
    pagina = 1
    enviar_lista_pagina(chat_id, usuario_id, cartas_usuario, pagina, context)

def enviar_lista_pagina(chat_id, usuario_id, lista_cartas, pagina, context, editar=False, mensaje=None, filtro=None):
    total = len(lista_cartas)
    por_pagina = 10
    paginas = (total - 1) // por_pagina + 1
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
            # Visual según rareza (puedes ajustar los emojis si quieres)
            if estrellas == "★★★":
                icon = "🌟"
            elif estrellas == "★★☆":
                icon = "⭐"
            elif estrellas == "★☆☆":
                icon = "🔸"
            else:
                icon = "⚪"
            texto += (
                f"{icon} <b>{nombre}</b> [{version}] {grupo}\n"
                f"   <code>{id_unico}</code> · [{estrellas}] · <b>#{cid}</b>\n"
            )
        texto += "\n<i>Usa <code>/ampliar &lt;id_unico&gt;</code> para ver detalles de cualquier carta.</i>"

    nav = []
    if pagina > 1:
        nav.append(InlineKeyboardButton("« Anterior", callback_data=f"lista_{pagina-1}_{usuario_id}"))
    if pagina < paginas:
        nav.append(InlineKeyboardButton("Siguiente »", callback_data=f"lista_{pagina+1}_{usuario_id}"))
    teclado = InlineKeyboardMarkup([nav]) if nav else None
    if editar and mensaje:
        try:
            mensaje.edit_text(texto, reply_markup=teclado, parse_mode='HTML')
        except Exception as e:
            context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado, parse_mode='HTML')
    else:
        context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado, parse_mode='HTML')


@cooldown_critico
def comando_inventario(update, context):
    usuario_id = update.message.from_user.id
    chat_id = update.effective_chat.id

    # Catálogo de objetos con descripciones
    catalogo = {
        "bono_idolday": "🎟️ Bono Idolday\nPermite tirar un /idolday adicional.",
        "ticket_intercambio": "🔁 Ticket de intercambio\nPermite intercambiar cartas con otro usuario.",
        "cofre_misterioso": "🎁 Cofre Misterioso\n¡Usa /abrir para obtener una recompensa sorpresa!",
        "lightstick": "💡 Lightstick\nMejora el estado de una carta:\n• ☆☆☆ → ★☆☆ (100%)\n• ★☆☆ → ★★☆ (70%)\n• ★★☆ → ★★★ (40%)",
        # Agrega más objetos aquí si lo deseas
    }
#----------------------------------------------------

def mostrar_mercado_pagina(
    chat_id, pagina=1, context=None, mensaje=None, editar=False,
    filtro=None, valor_filtro=None, orden=None, user_id=None
):
    if user_id is None:
        return

    query = {}
    if filtro == "estrellas" and valor_filtro:
        query["estrellas"] = valor_filtro
    if filtro == "grupo" and valor_filtro:
        query["grupo"] = valor_filtro

    cartas = list(col_mercado.find(query))

    # Orden
    if orden == "mayor":
        cartas.sort(key=lambda c: c.get("card_id", 0), reverse=True)
    elif orden == "menor":
        cartas.sort(key=lambda c: c.get("card_id", 0))

    por_pagina = 10
    total = len(cartas)
    paginas = max(1, (total - 1) // por_pagina + 1)
    if pagina < 1: pagina = 1
    if pagina > paginas: pagina = paginas
    inicio = (pagina - 1) * por_pagina
    fin = min(inicio + por_pagina, total)

    if filtro and valor_filtro:
        texto = f"<b>🛒 Cartas en el mercado (página {pagina}/{paginas}) — Filtrado por: {valor_filtro}</b>\n"
    else:
        texto = f"<b>🛒 Cartas en el mercado (página {pagina}/{paginas})</b>\n"
    texto += "━━━━━━━━━━━━━━━━━\n"

    if total == 0:
        texto += "⚠️ <b>No hay cartas a la venta en el mercado.</b>\n"
        texto += "Usa <code>/vender &lt;id_unico&gt;</code> para poner la tuya."
    else:
        for c in cartas[inicio:fin]:
            estrellas = c.get('estrellas', '★??')
            id_unico = c.get('id_unico', '')
            nombre = c.get('nombre', '')
            version = c.get('version', '')
            card_id = c.get('card_id', '')
            precio = precio_carta_karuta(nombre, version, c.get('estado', ''), id_unico=id_unico)
            # Emoji por rareza
            if estrellas == "★★★":
                icon = "🌟"
            elif estrellas == "★★☆":
                icon = "⭐"
            elif estrellas == "★☆☆":
                icon = "🔸"
            else:
                icon = "⚪"
            texto += (
                f"{icon} <b>{nombre}</b> [{version}] · <b>#{card_id}</b> · [{estrellas}]\n"
                f"   <b>💲{precio}</b>\n"
                f"   <code>/comprar {id_unico}</code>\n"
                "━━━━━━━━━━━━━━━━━━\n"
            )
        if fin < total:
            texto += f"Y {total-fin} más...\n"

    # --------- Teclado ----------
    matriz = []
    # Solo un botón para Filtros (al inicio), nunca muestra los 3 juntos aquí
    matriz.append([InlineKeyboardButton("🔍 Filtrar / Ordenar", callback_data=f"mercado_filtro_{user_id}")])

    # Navegación
    nav = []
    if pagina > 1:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"mercado_{pagina-1}_{user_id}" + (f"_{orden}" if orden else "")))
    if pagina < paginas:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"mercado_{pagina+1}_{user_id}" + (f"_{orden}" if orden else "")))
    if nav:
        matriz.append(nav)
    # Solo muestra "❌ Quitar filtro" si hay filtro activo
    if filtro:
        matriz.append([InlineKeyboardButton("❌ Quitar filtro", callback_data=f"mercado_1_{user_id}")])

    teclado = InlineKeyboardMarkup(matriz)

    # Edita o envía el mensaje
    if editar and mensaje is not None:
        try:
            mensaje.edit_text(texto, reply_markup=teclado, parse_mode="HTML")
        except Exception:
            context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado, parse_mode="HTML")
    else:
        context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado, parse_mode="HTML")

    
#----------Comando FAV1---------------
@cooldown_critico
def comando_favoritos(update, context):
    usuario_id = update.message.from_user.id
    doc = col_usuarios.find_one({"user_id": usuario_id})
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
    usuario_id = update.message.from_user.id
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

    doc = col_usuarios.find_one({"user_id": usuario_id}) or {}
    favoritos = doc.get("favoritos", [])

    key = {"nombre": nombre, "version": version}
    if key in favoritos:
        favoritos = [f for f in favoritos if not (f["nombre"] == nombre and f["version"] == version)]
        col_usuarios.update_one({"user_id": usuario_id}, {"$set": {"favoritos": favoritos}}, upsert=True)
        update.message.reply_text(f"❌ Quitaste de favoritos: <code>[{version}] {nombre}</code>", parse_mode="HTML")
    else:
        favoritos.append(key)
        col_usuarios.update_one({"user_id": usuario_id}, {"$set": {"favoritos": favoritos}}, upsert=True)
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
        f"💳 <b>Precio de carta [{id_unico}]</b>\n"
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
    usuario_id = update.message.from_user.id
    chat_id = update.effective_chat.id

    if not context.args:
        update.message.reply_text("Usa: /vender <id_unico>")
        return
    id_unico = context.args[0].strip()
    carta = col_cartas_usuario.find_one({"user_id": usuario_id, "id_unico": id_unico})
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
    col_cartas_usuario.delete_one({"user_id": usuario_id, "id_unico": id_unico})

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
       "vendedor_id": usuario_id,
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

#----------Ver cartas en venta------------------
@cooldown_critico
def comando_mercado(update, context):
    chat_id = update.effective_chat.id
    user_id = update.message.from_user.id
    mostrar_mercado_pagina(chat_id, pagina=1, context=context, mensaje=None, editar=False, user_id=user_id)


#----------Comprar carta del mercado------------------
@cooldown_critico
def comando_comprar(update, context):
    usuario_id = update.message.from_user.id
    if not context.args:
        update.message.reply_text("Usa: /comprar <id_unico>")
        return
    id_unico = context.args[0].strip()
    # Transacción atómica: solo uno puede comprarla
    carta = col_mercado.find_one_and_delete({"id_unico": id_unico})
    if not carta:
        update.message.reply_text("Esa carta ya no está disponible o ya fue comprada.")
        return
    if carta["vendedor_id"] == usuario_id:
        update.message.reply_text("No puedes comprar tu propia carta.")
        # Devuelve la carta al mercado si el vendedor intentó comprarla
        col_mercado.insert_one(carta)
        return

    usuario = col_usuarios.find_one({"user_id": usuario_id}) or {}
    saldo = usuario.get("kponey", 0)
    precio = carta["precio"]

    if saldo < precio:
        update.message.reply_text(f"No tienes suficiente Kponey. Precio: {precio}, tu saldo: {saldo}")
        # Devuelve la carta al mercado si el usuario no tiene saldo suficiente
        col_mercado.insert_one(carta)
        return

    # Transacción de dinero
    col_usuarios.update_one({"user_id": usuario_id}, {"$inc": {"kponey": -precio}}, upsert=True)
    col_usuarios.update_one({"user_id": carta["vendedor_id"]}, {"$inc": {"kponey": precio}}, upsert=True)

    # --- 👇 CORRECCIÓN AQUÍ: asegura que card_id esté correcto 👇 ---
    card_id = carta.get("card_id")
    if not card_id:
        card_id = extraer_card_id_de_id_unico(carta.get("id_unico"))
        carta["card_id"] = card_id
    # --------------------------------------------------------------

    # Preparar carta para el inventario del usuario
    carta['user_id'] = usuario_id
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
    revisar_sets_completados(usuario_id, context)
    
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
    usuario_id = update.message.from_user.id
    if not context.args:
        update.message.reply_text("Usa: /retirar <id_unico>")
        return
    id_unico = context.args[0].strip()
    carta = col_mercado.find_one({"id_unico": id_unico, "vendedor_id": usuario_id})
    if not carta:
        update.message.reply_text("No tienes esa carta en el mercado.")
        return
    # Devolver carta al usuario
    col_mercado.delete_one({"id_unico": id_unico})
    carta['user_id'] = usuario_id
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
    
#---------filtros de mercado "grupo"------------------------------------------------

def mostrar_filtros_grupo(
    chat_id, context, mensaje=None, editar=False, pagina=1, user_id=None
):
    grupos = sorted({c.get("grupo", "") for c in col_mercado.find() if c.get("grupo")})
    por_pagina = 4
    total = len(grupos)
    paginas = max(1, (total - 1) // por_pagina + 1)
    if pagina < 1: pagina = 1
    if pagina > paginas: pagina = paginas
    inicio = (pagina - 1) * por_pagina
    fin = min(inicio + por_pagina, total)
    grupos_pagina = grupos[inicio:fin]

    matriz = []
    for g in grupos_pagina:
        matriz.append([InlineKeyboardButton(g, callback_data=f"mercado_grupo_{g}_{user_id}")])

    nav = []
    if pagina > 1:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"mercado_filtropagegrupo_{pagina-1}_{user_id}"))
    if pagina < paginas:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"mercado_filtropagegrupo_{pagina+1}_{user_id}"))
    if nav:
        matriz.append(nav)
    matriz.append([InlineKeyboardButton("❌ Quitar filtro", callback_data=f"mercado_1_{user_id}")])

    teclado = InlineKeyboardMarkup(matriz)
    # --- Aquí NO cambias el texto, solo los botones ---
    if editar and mensaje is not None:
        try:
            mensaje.edit_reply_markup(reply_markup=teclado)
        except Exception as e:
            print("Error edit_reply_markup en mostrar_filtros_grupo:", e)




#--------------------------------------------------------------------------------


#---------Dinero del bot------------
@cooldown_critico
def comando_saldo(update, context):
    usuario_id = update.message.from_user.id
    usuario = col_usuarios.find_one({"user_id": usuario_id}) or {}
    kponey = usuario.get("kponey", 0)
    update.message.reply_text(f"💸 <b>Tus Kponey:</b> <code>{kponey}</code>", parse_mode="HTML")

#---------Para dar dinero------------
def comando_darKponey(update, context):
    if not es_admin(update):
        update.message.reply_text("Solo admins pueden usar este comando.")
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



def mostrar_carta_individual(chat_id, usuario_id, lista_cartas, idx, context, mensaje_a_editar=None, query=None):
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
    usuario_id = update.message.from_user.id
    id_unico = context.args[0].strip()

    # 1. Busca en inventario
    carta = col_cartas_usuario.find_one({"user_id": usuario_id, "id_unico": id_unico})
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
    version = carta.get('version', '')
    grupo = grupo_de_carta(nombre, version)
    estrellas = carta.get('estrellas', '★??')
    estado = carta.get('estado', '')
    card_id = carta.get('card_id', '')
    total_copias = col_cartas_usuario.count_documents({"nombre": nombre, "version": version})

    # Saber si es favorita (solo si está en el álbum)
    doc_user = col_usuarios.find_one({"user_id": usuario_id}) or {}
    favoritos = doc_user.get("favoritos", [])
    es_fav = any(fav.get("nombre") == nombre and fav.get("version") == version for fav in favoritos)
    estrella_fav = "⭐ " if es_fav else ""

    # --- Corrige aquí: usa el precio guardado si está en mercado ---
    precio = precio_carta_karuta(nombre, version, estado, id_unico=id_unico)

    # Texto bonito
    texto = (
        f"💳 <b>Precio de carta [{id_unico}]</b>\n"
        f"• Nombre: {estrella_fav}<b>{nombre}</b>\n"
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



def comando_giveidol(update, context):
    # Uso: /giveidol <id_unico> @usuario_destino
    if len(context.args) < 2:
        update.message.reply_text("Uso: /giveidol <id_unico> @usuario_destino")
        return
    id_unico = context.args[0].strip()
    user_dest = context.args[1].strip()
    usuario_id = update.message.from_user.id
    chat = update.effective_chat

    # Buscar la carta exacta del usuario por id_unico
    carta = col_cartas_usuario.find_one({"user_id": usuario_id, "id_unico": id_unico})
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
    if usuario_id == target_user_id:
        update.message.reply_text("No puedes regalarte cartas a ti mismo.")
        return

    # Quitar carta al remitente
    col_cartas_usuario.delete_one({"user_id": usuario_id, "id_unico": id_unico})

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
    usuario_id = update.effective_user.id
    chat_id = update.effective_chat.id
    sets = obtener_sets_disponibles()
    cartas_usuario = list(col_cartas_usuario.find({"user_id": usuario_id}))
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
    usuario_id = update.effective_user.id
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
    usuario_id = update.effective_user.id
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
    cartas_usuario = list(col_cartas_usuario.find({"user_id": usuario_id}))
    cartas_usuario_unicas = set((c["nombre"], c["version"]) for c in cartas_usuario)

    # Trae favoritos del usuario
    user_doc = col_usuarios.find_one({"user_id": usuario_id}) or {}
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
    usuario_id = query.from_user.id
    carta = col_cartas_usuario.find_one({"user_id": usuario_id, "id_unico": id_unico})
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

    col_cartas_usuario.delete_one({"user_id": usuario_id, "id_unico": id_unico})
    estrellas = carta.get('estrellas', '★??')
    col_mercado.insert_one({
       "id_unico": id_unico,
       "vendedor_id": usuario_id,
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

#-------------mostrar_menu_filtros------------
def mostrar_menu_filtros(user_id, query):
    botones = [
        [InlineKeyboardButton("📊 Por Estado", callback_data=f"mercado_filtro_estado_{user_id}")],
        [InlineKeyboardButton("👥 Por Grupo", callback_data=f"mercado_filtro_grupo_{user_id}")],
        [InlineKeyboardButton("🔢 Ordenar por #n", callback_data=f"mercado_ordenar_numero_{user_id}")],
        [InlineKeyboardButton("❌ Quitar filtro", callback_data=f"mercado_1_{user_id}")]
    ]
    teclado = InlineKeyboardMarkup(botones)
    try:
        query.edit_message_reply_markup(reply_markup=teclado)
    except Exception as e:
        print("Error edit_message_reply_markup en mostrar_menu_filtros:", e)
    query.answer()



def manejador_callback(update, context):
    query = update.callback_query
    data = query.data

    # Función auxiliar para extraer user_id de data tipo "mercado_*_<user_id>"
    def get_uid(data):
        partes = data.split("_")
        if partes[-1].isdigit():
            return int(partes[-1])
        return None

    # --- CONTROL DE USUARIO EN MENÚ DE MERCADO ---
    if data.startswith("mercado"):
        uid = get_uid(data)
        if uid is not None and query.from_user.id != uid:
            query.answer("Solo la persona que abrió este menú puede interactuar aquí.", show_alert=True)
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

    # ====== MENÚ PRINCIPAL DE FILTROS DEL MERCADO ======
    if data.startswith("mercado_filtro_"):
        user_id = get_uid(data)
        mostrar_menu_filtros(user_id, query)
        return

    # Botones de filtro por estado (estrellas visuales)
# ---- Filtro por estado (vertical, con texto) ----
    if data.startswith("mercado_filtro_estado_"):
        user_id = get_uid(data)
        botones = [
            [InlineKeyboardButton("★★★", callback_data=f"mercado_estado_3_{user_id}")],
            [InlineKeyboardButton("★★☆", callback_data=f"mercado_estado_2_{user_id}")],
            [InlineKeyboardButton("★☆☆", callback_data=f"mercado_estado_1_{user_id}")],
            [InlineKeyboardButton("☆☆☆", callback_data=f"mercado_estado_0_{user_id}")],
            [InlineKeyboardButton("❌ Quitar filtro", callback_data=f"mercado_1_{user_id}")]
        ]
        teclado = InlineKeyboardMarkup(botones)
        try:
            query.edit_message_reply_markup(reply_markup=teclado)
        except Exception as e:
            print("Error edit_message_reply_markup en filtro_estado:", e)
        query.answer()
        return




# ---- Filtro por grupo (abre submenú paginado) ----
    if data.startswith("mercado_filtro_grupo_"):
        user_id = get_uid(data)
        mostrar_filtros_grupo(
            query.message.chat_id,
            context,
            mensaje=query.message,
            editar=True,
            pagina=1,
            user_id=user_id
        )
        query.answer()
        return




    # Navegación en el paginado de grupos (flechas)
    if data.startswith("mercado_filtropagegrupo_"):
        partes = data.split("_")
        pagina = int(partes[-2])
        user_id = int(partes[-1])
        mostrar_filtros_grupo(query.message.chat_id, context, mensaje=query.message, editar=True, pagina=pagina, user_id=user_id)
        query.answer()
        return

    # Selección de grupo específico
    if data.startswith("mercado_grupo_"):
        partes = data.split("_")
        grupo = "_".join(partes[2:-1])
        user_id = int(partes[-1])
        mostrar_mercado_pagina(
            query.message.chat_id, 1, context, query.message, True,
            filtro="grupo", valor_filtro=grupo, user_id=user_id
        )
        query.answer()
        return

    # Menú de orden por número
    if data.startswith("mercado_ordenar_numero_"):
        user_id = get_uid(data)
        botones = [
            [
                InlineKeyboardButton("⬆️ Menor a mayor", callback_data=f"mercado_orden_numero_menor_{user_id}"),
                InlineKeyboardButton("⬇️ Mayor a menor", callback_data=f"mercado_orden_numero_mayor_{user_id}")
            ],
            [InlineKeyboardButton("❌ Quitar filtro", callback_data=f"mercado_1_{user_id}")]
        ]
        teclado = InlineKeyboardMarkup(botones)
        try:
            query.edit_message_reply_markup(reply_markup=teclado)
        except Exception:
            try:
                query.message.edit_reply_markup(reply_markup=teclado)
            except Exception:
                query.message.reply_text("Elige el orden:", reply_markup=teclado)
        query.answer()
        return

    # Ordenar por número (menor-mayor)
    if data.startswith("mercado_orden_numero_menor_"):
        user_id = get_uid(data)
        mostrar_mercado_pagina(
            query.message.chat_id, 1, context, query.message, True,
            orden="menor", user_id=user_id
        )
        query.answer()
        return

    # Ordenar por número (mayor-menor)
    if data.startswith("mercado_orden_numero_mayor_"):
        user_id = get_uid(data)
        mostrar_mercado_pagina(
            query.message.chat_id, 1, context, query.message, True,
            orden="mayor", user_id=user_id
        )
        query.answer()
        return

    # Filtrar por estrellas visuales (estado)
    if data.startswith("mercado_estado_"):
        partes = data.split("_")
        estrellas_idx = int(partes[2])
        user_id = int(partes[3])
        estrellas_map = {3: "★★★", 2: "★★☆", 1: "★☆☆", 0: "☆☆☆"}
        valor_filtro = estrellas_map[estrellas_idx]
        mostrar_mercado_pagina(
            query.message.chat_id, 1, context, query.message, True,
            filtro="estrellas", valor_filtro=valor_filtro, user_id=user_id
        )
        query.answer()
        return

    # Quitar filtro (volver a listado original)
    if data.startswith("mercado_1_"):
        user_id = get_uid(data)
        mostrar_mercado_pagina(
            query.message.chat_id, 1, context, query.message, True,
            user_id=user_id
        )
        query.answer()
        return

    # Navegación paginada (mercado)
    import re
    m = re.match(r"mercado_(\d+)_(\d+)(?:_(menor|mayor))?$", data)
    if m:
        pagina = int(m.group(1))
        user_id = int(m.group(2))
        orden = m.group(3)
        mostrar_mercado_pagina(
            query.message.chat_id, pagina, context, query.message, True,
            orden=orden, user_id=user_id
        )
        query.answer()
        return

    # Botón para "eliminar menú del mercado"
    if data.startswith("mercado_volver_"):
        try:
            query.message.delete()
        except Exception:
            pass
        query.answer()
        return

    # ====== RESTO DE CALLBACKS DEL SISTEMA ======

    # --- VER CARTA INDIVIDUAL ---
    if data.startswith("vercarta"):
        partes = data.split("_")
        if len(partes) != 3:
            query.answer()
            return
        usuario_id = int(partes[1])
        id_unico = partes[2]
        if query.from_user.id != usuario_id:
            query.answer(text="Solo puedes ver tus propias cartas.", show_alert=True)
            return
        carta = col_cartas_usuario.find_one({"user_id": usuario_id, "id_unico": id_unico})
        if not carta:
            query.answer(text="Esa carta no existe.", show_alert=True)
            return
        mostrar_carta_individual(
            query.message.chat_id,
            usuario_id,
            [carta],
            0,
            context,
            query=query
        )
        query.answer()
        return

    # --- PAGINACIÓN ÁLBUM ---
    if data.startswith("albumlista_"):
        partes = data.split("_")
        if len(partes) != 2:
            return
        usuario_id = int(partes[1])
        if query.from_user.id != usuario_id:
            query.answer(text="Solo puedes ver tu propio álbum.", show_alert=True)
            return
        cartas_usuario = list(col_cartas_usuario.find({"user_id": usuario_id}))
        def sort_key(x):
            grupo = grupo_de_carta(x.get('nombre', ''), x.get('version', '')) or ""
            return (
                grupo.lower(),
                x.get('nombre', '').lower(),
                x.get('card_id', 0)
            )
        cartas_usuario.sort(key=sort_key)
        pagina = 1
        enviar_lista_pagina(
            query.message.chat_id,
            usuario_id,
            cartas_usuario,
            pagina,
            context,
            editar=True,
            mensaje=query.message
        )
        query.answer()
        return

    # --- REGALAR CARTA ---
    if data.startswith("regalar_"):
        partes = data.split("_")
        if len(partes) != 3:
            query.answer()
            return
        usuario_id = int(partes[1])
        idx = int(partes[2])
        if query.from_user.id != usuario_id:
            query.answer(text="Solo puedes regalar tus propias cartas.", show_alert=True)
            return
        cartas_usuario = list(col_cartas_usuario.find({"user_id": usuario_id}))
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
        SESIONES_REGALO[usuario_id] = {
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
        usuario_id = int(partes[2])
        filtro = partes[3].strip().lower() if len(partes) > 3 and partes[3] else None
        if query.from_user.id != usuario_id:
            query.answer(text="Este álbum no es tuyo.", show_alert=True)
            return
        cartas_usuario = list(col_cartas_usuario.find({"user_id": usuario_id}))
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
            usuario_id,
            cartas_usuario,
            pagina,
            context,
            editar=True,
            mensaje=query.message,
            filtro=filtro
        )
        query.answer()
        return

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
    

dispatcher.add_handler(CallbackQueryHandler(callback_ampliar_vender, pattern="^ampliar_vender_"))
dispatcher.add_handler(CallbackQueryHandler(manejador_callback))
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
dispatcher.add_handler(CommandHandler('inventario', comando_inventario))
dispatcher.add_handler(CommandHandler('kponey', comando_saldo))
dispatcher.add_handler(CommandHandler('darKponey', comando_darKponey))
dispatcher.add_handler(CommandHandler('fav', comando_fav))
dispatcher.add_handler(CommandHandler('favoritos', comando_favoritos))
dispatcher.add_handler(CommandHandler('precio', comando_precio))
dispatcher.add_handler(CommandHandler('vender', comando_vender))
dispatcher.add_handler(CommandHandler('mercado', comando_mercado))
dispatcher.add_handler(CommandHandler('comprar', comando_comprar))
dispatcher.add_handler(CommandHandler('retirar', comando_retirar))

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
