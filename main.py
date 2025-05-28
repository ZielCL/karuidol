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

# Cargar cartas.json
if not os.path.isfile('cartas.json'):
    raise ValueError("No se encontró el archivo cartas.json")
with open('cartas.json', 'r') as f:
    cartas = json.load(f)

DROPS_ACTIVOS = {}

# Estados de carta
ESTADOS_CARTA = [
    ("Excelente", "★★★"),
    ("Buen estado", "★★☆"),
    ("Mal estado", "★☆☆"),
    ("Muy mal estado", "☆☆☆")
]
ESTADO_LISTA = ["Excelente", "Buen estado", "Mal estado", "Muy mal estado"]

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
    user_doc = col_usuarios.find_one({"user_id": user_id})
    if not user_doc:
        return True
    bono = user_doc.get('bono', 0)
    last = user_doc.get('last_idolday')
    ahora = datetime.utcnow()
    if bono and bono > 0:
        return True
    if not last:
        return True
    diferencia = ahora - last
    if diferencia.total_seconds() >= 86400:
        return True
    return False
def desbloquear_drop(drop_id):
    # Espera 30 segundos para bloquear el drop (puedes cambiar el tiempo si quieres)
    data = DROPS_ACTIVOS.get(drop_id)
    if not data or data.get("expirado"):
        return
    tiempo_inicio = data["inicio"]
    while True:
        ahora = time.time()
        elapsed = ahora - tiempo_inicio
        if elapsed >= 30:
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
    user_doc = col_usuarios.find_one({"user_id": usuario_id})
    bono = user_doc.get('bono', 0) if user_doc else 0
    last = user_doc.get('last_idolday') if user_doc else None
    puede_tirar = False

    if update.effective_chat.type not in ["group", "supergroup"]:
        context.bot.send_message(chat_id=chat_id, text="Este comando solo se puede usar en grupos.")
        return

    if bono and bono > 0:
        puede_tirar = True
        col_usuarios.update_one({"user_id": usuario_id}, {"$inc": {"bono": -1}}, upsert=True)
    elif last:
        diferencia = ahora - last
        if diferencia.total_seconds() >= 86400:
            puede_tirar = True
    else:
        puede_tirar = True

    if not puede_tirar:
        if last:
            faltante = 86400 - (ahora - last).total_seconds()
            horas = int(faltante // 3600)
            minutos = int((faltante % 3600) // 60)
            context.bot.send_message(chat_id=chat_id, text=f"Ya usaste /idolday hoy. Intenta de nuevo en {horas}h {minutos}m.")
        else:
            context.bot.send_message(chat_id=chat_id, text=f"Ya usaste /idolday hoy.")
        return

    # SOLO cartas en estado "Excelente"
    cartas_excelentes = [c for c in cartas if c.get("estado") == "Excelente estado"]
    if len(cartas_excelentes) < 2:
        cartas_excelentes = cartas_excelentes * 2

    cartas_drop = random.choices(cartas_excelente estado, k=2)
    media_group = []
    cartas_info = []
    for carta in cartas_drop:
        nombre = carta['nombre']
        version = carta['version']
        grupo = carta.get('grupo', '')
        imagen_url = carta.get('imagen')
        # NO SE GENERA ID NI ESTADO NI ESTRELLAS EN EL DROP
        caption = f"<b>{nombre}</b>\n{grupo} [{version}]"
        media_group.append(InputMediaPhoto(media=imagen_url, caption=caption, parse_mode="HTML"))
        cartas_info.append({
            "nombre": nombre,
            "version": version,
            "grupo": grupo,
            "imagen": imagen_url,
            "reclamada": False,
            "usuario": None,
            "hora_reclamada": None,
        })

    msgs = context.bot.send_media_group(chat_id=chat_id, media=media_group)
    main_msg = msgs[0]

    texto_drop = f"@{update.effective_user.username or update.effective_user.first_name} está dropeando 2 cartas!"
    msg_botones = context.bot.send_message(
        chat_id=chat_id,
        text=texto_drop,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("1️⃣", callback_data=f"reclamar_{main_msg.chat_id}_{main_msg.message_id}_0"),
                InlineKeyboardButton("2️⃣", callback_data=f"reclamar_{main_msg.chat_id}_{main_msg.message_id}_1"),
            ]
        ])
    )

    drop_id = crear_drop_id(chat_id, main_msg.message_id)
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
    solo_dueño = tiempo_desde_drop < 15
    puede_reclamar = False

    user_doc = col_usuarios.find_one({"user_id": usuario_click}) or {}
    bono = user_doc.get('bono', 0)

    if usuario_click == drop["dueño"]:
        primer_reclamo = drop.get("primer_reclamo_dueño")
        if primer_reclamo is None:
            puede_reclamar = True
            drop["primer_reclamo_dueño"] = ahora
        else:
            if tiempo_desde_drop < 15:
                query.answer("Solo puedes reclamar una carta antes de 15 segundos. Espera a que pasen 15 segundos para reclamar la otra (si tienes bono).", show_alert=True)
                return
            if bono < 1:
                query.answer("Necesitas al menos 1 bono para reclamar la segunda carta.", show_alert=True)
                return
            puede_reclamar = True
            col_usuarios.update_one({"user_id": usuario_click}, {"$inc": {"bono": -1}}, upsert=True)
    elif not solo_dueño and carta["usuario"] is None:
        if puede_usar_idolday(usuario_click):
            puede_reclamar = True
        else:
            query.answer("Solo puedes reclamar cartas si tienes disponible tu /idolday o tienes un bono disponible.", show_alert=True)
            return
    else:
        segundos_faltantes = int(15 - tiempo_desde_drop)
        if segundos_faltantes < 0:
            segundos_faltantes = 0
        query.answer(f"Aún no puedes reclamar esta carta, te quedan {segundos_faltantes} segundos para poder reclamar.", show_alert=True)
        return

    if not puede_reclamar:
        query.answer("No puedes reclamar esta carta.", show_alert=True)
        return

    # --- Aquí SÍ generamos id_unico, estado y estrellas ---
    nombre = carta['nombre']
    version = carta['version']
    grupo = carta['grupo']

    doc_cont = col_contadores.find_one({"nombre": nombre, "version": version})
    if doc_cont:
        nuevo_id = doc_cont['contador'] + 1
        col_contadores.update_one({"nombre": nombre, "version": version}, {"$inc": {"contador": 1}})
    else:
        nuevo_id = 1
        col_contadores.insert_one({"nombre": nombre, "version": version, "contador": 1})

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
    # SOLO mostrar estado como texto, NO estrellas.
    context.bot.send_message(
        chat_id=drop["chat_id"],
        text=f"{user_mention} tomaste la carta <code>{id_unico}</code> #{nuevo_id} [{version}] {nombre} - {grupo}, Genial! está en <b>{estado.lower()}</b>!",
        parse_mode='HTML'
    )
    query.answer("¡Carta reclamada!", show_alert=True)

# ----------------- Resto de funciones: album, paginación, etc. -----------------
# Aquí pego la versión adaptada de /album para usar id_unico, estrellas y letra pegada a la izquierda:

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

def enviar_lista_pagina(chat_id, usuario_id, lista_cartas, pagina, context, editar=False, mensaje=None):
    total = len(lista_cartas)
    por_pagina = 10
    paginas = (total - 1) // por_pagina + 1
    if pagina < 1: pagina = 1
    if pagina > paginas: pagina = paginas
    inicio = (pagina - 1) * por_pagina
    fin = min(inicio + por_pagina, total)
    botones = []
    for idx, carta in enumerate(lista_cartas[inicio:fin], start=inicio):
        cid = carta.get('card_id', '')
        version = carta.get('version', '')
        nombre = carta.get('nombre', '')
        grupo = grupo_de_carta(nombre, version)
        id_unico = carta.get('id_unico', 'xxxx')
        estrellas = carta.get('estrellas', '★??')   # Siempre del registro, nunca lo calcules tú
        texto_boton = f"{id_unico} [{estrellas}] #{cid} [{version}] {nombre} - {grupo}"
        botones.append([InlineKeyboardButton(texto_boton, callback_data=f"vercarta_{usuario_id}_{idx}")])
    texto = f"<b>Página {pagina}/{paginas}</b>"
    nav = []
    if pagina > 1:
        nav.append(InlineKeyboardButton("« Anterior", callback_data=f"lista_{pagina-1}_{usuario_id}"))
    if pagina < paginas:
        nav.append(InlineKeyboardButton("Siguiente »", callback_data=f"lista_{pagina+1}_{usuario_id}"))
    if nav:
        botones.append(nav)
    teclado = InlineKeyboardMarkup(botones)
    if editar and mensaje:
        try:
            mensaje.edit_text(texto, reply_markup=teclado, parse_mode='HTML')
        except Exception as e:
            context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado, parse_mode='HTML')
    else:
        context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado, parse_mode='HTML')


def mostrar_carta_individual(chat_id, usuario_id, lista_cartas, idx, context, mensaje_a_editar=None, query=None):
    carta = lista_cartas[idx]
    cid = carta.get('card_id', '')
    version = carta.get('version', '')
    nombre = carta.get('nombre', '')
    grupo = grupo_de_carta(nombre, version)
    imagen_url = carta.get('imagen', imagen_de_carta(nombre, version))
    id_unico = carta.get('id_unico', '')
    estrellas = carta.get('estrellas', '★??')
    id_carta = f"<code>{id_unico}</code> [{estrellas}] #{cid} [{version}] {nombre} - {grupo}"
    texto = f"{id_carta}"
    botones = []
    if idx > 0:
        botones.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"vercarta_{usuario_id}_{idx-1}"))
    botones.append(InlineKeyboardButton("📒 Album", callback_data=f"albumlista_{usuario_id}"))
    if idx < len(lista_cartas)-1:
        botones.append(InlineKeyboardButton("Siguiente ➡️", callback_data=f"vercarta_{usuario_id}_{idx+1}"))
    teclado = InlineKeyboardMarkup([botones])
    if query is not None:
        try:
            query.edit_message_media(
                media=InputMediaPhoto(media=imagen_url, caption=texto, parse_mode='HTML'),
                reply_markup=teclado
            )
        except Exception as e:
            query.answer(text="No se pudo actualizar la imagen.", show_alert=True)
    else:
        context.bot.send_photo(chat_id=chat_id, photo=imagen_url, caption=texto, reply_markup=teclado, parse_mode='HTML')

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

def comando_comandos(update, context):
    texto = (
        "📋 <b>Lista de comandos disponibles:</b>\n"
        "\n"
        "<b>/idolday</b> - Drop de 2 cartas con botones.\n"
        "<b>/album</b> - Muestra tu colección de cartas.\n"
        "<b>/giveidol</b> - Regala una carta usando el ID único (ej: <code>/giveidol f4fg1 @usuario</code>).\n"
        "<b>/miid</b> - Muestra tu ID de Telegram.\n"
        "<b>/bonoidolday</b> - Da bonos de tiradas de /idolday a un usuario (solo admins).\n"
        "<b>/setsprogreso</b> - Progreso de sets/colecciones.\n"
        "<b>/set</b> - Detalles de un set.\n"
        "<b>/comandos</b> - Muestra esta lista de comandos.\n"
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

def comando_setsprogreso(update, context):
    mostrar_setsprogreso(update, context, pagina=1)

def mostrar_setsprogreso(update, context, pagina=1, mensaje=None, editar=False):
    usuario_id = update.effective_user.id
    chat_id = update.effective_chat.id
    sets = obtener_sets_disponibles()
    cartas_usuario = list(col_cartas_usuario.find({"user_id": usuario_id}))
    cartas_usuario_set = set((c["nombre"], c["version"]) for c in cartas_usuario)
    por_pagina = 5
    total = len(sets)
    paginas = (total - 1) // por_pagina + 1
    if pagina < 1: pagina = 1
    if pagina > paginas: pagina = paginas
    inicio = (pagina - 1) * por_pagina
    fin = min(inicio + por_pagina, total)
    texto = "<b>📚 Progreso de sets/colecciones:</b>\n\n"
    for s in sets[inicio:fin]:
        cartas_set = [c for c in cartas if (c.get("set") == s or c.get("grupo") == s)]
        total_set = len(cartas_set)
        usuario_tiene = sum(1 for c in cartas_set if (c["nombre"], c["version"]) in cartas_usuario_set)
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
    cartas_set = [c for c in cartas if (c.get("set") == set_name or c.get("grupo") == set_name)]
    por_pagina = 8
    total = len(cartas_set)
    paginas = (total - 1) // por_pagina + 1
    if pagina < 1: pagina = 1
    if pagina > paginas: pagina = paginas
    inicio = (pagina - 1) * por_pagina
    fin = min(inicio + por_pagina, total)
    cartas_usuario = list(col_cartas_usuario.find({"user_id": usuario_id}))
    cartas_usuario_set = set((c["nombre"], c["version"]) for c in cartas_usuario)
    usuario_tiene = sum(1 for c in cartas_set if (c["nombre"], c["version"]) in cartas_usuario_set)
    bloques = 10
    bloques_llenos = int((usuario_tiene / len(cartas_set)) * bloques) if len(cartas_set) > 0 else 0
    barra = "🟩" * bloques_llenos + "⬜" * (bloques - bloques_llenos)
    texto = f"<b>🌟 Set: {set_name}</b> <b>({usuario_tiene}/{len(cartas_set)})</b>\n{barra}\n\n"
    for carta in cartas_set[inicio:fin]:
        key = (carta["nombre"], carta["version"])
        if key in cartas_usuario_set:
            texto += f"✅ <b>{carta['nombre']} [{carta['version']}]</b>\n"
        else:
            texto += f"❌ {carta['nombre']} [{carta['version']}]\n"
    texto += f"\nPágina {pagina}/{paginas}"
    if usuario_tiene == len(cartas_set) and len(cartas_set) > 0:
        texto += "\n🎉 <b>¡Completaste este set!</b> 🎉"
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
def manejador_callback(update, context):
    query = update.callback_query
    data = query.data

    if data.startswith("reclamar"):
        manejador_reclamar(update, context)
    elif data == "expirado":
        query.answer("Este drop ha expirado.", show_alert=True)
    elif data == "reclamada":
        query.answer("Esta carta ya fue reclamada.", show_alert=True)
    elif data.startswith("vercarta"):
        partes = data.split("_")
        if len(partes) != 3:
            return
        usuario_id = int(partes[1])
        idx = int(partes[2])
        if query.from_user.id != usuario_id:
            query.answer(text="Solo puedes ver tus propias cartas.", show_alert=True)
            return
        cartas_usuario = list(col_cartas_usuario.find({"user_id": usuario_id}))
        def sort_key(x):
            grupo = grupo_de_carta(x.get('nombre',''), x.get('version','')) or ""
            return (
                grupo.lower(),
                x.get('nombre','').lower(),
                x.get('card_id', 0)
            )
        cartas_usuario.sort(key=sort_key)
        mostrar_carta_individual(query.message.chat_id, usuario_id, cartas_usuario, idx, context, query=query)
        query.answer()
        return
    elif data.startswith("albumlista_"):
        partes = data.split("_")
        if len(partes) != 2:
            return
        usuario_id = int(partes[1])
        if query.from_user.id != usuario_id:
            query.answer(text="Solo puedes ver tu propio álbum.", show_alert=True)
            return
        cartas_usuario = list(col_cartas_usuario.find({"user_id": usuario_id}))
        def sort_key(x):
            grupo = grupo_de_carta(x.get('nombre',''), x.get('version','')) or ""
            return (
                grupo.lower(),
                x.get('nombre','').lower(),
                x.get('card_id', 0)
            )
        cartas_usuario.sort(key=sort_key)
        pagina = 1
        enviar_lista_pagina(query.message.chat_id, usuario_id, cartas_usuario, pagina, context, editar=True, mensaje=query.message)
        query.answer()
        return

    # ---- SETS Y PROGRESO ----
    if data.startswith("setsprogreso_"):
        pagina = int(data.split("_")[1])
        mostrar_setsprogreso(update, context, pagina=pagina, mensaje=query.message, editar=True)
        query.answer()
        return
    if data.startswith("setlist_"):
        pagina = int(data.split("_")[1])
        mostrar_lista_set(update, context, pagina=pagina, mensaje=query.message, editar=True)
        query.answer()
        return
    if data.startswith("setdet_"):
        partes = data.split("_")
        set_name = "_".join(partes[1:-1])
        pagina = int(partes[-1])
        mostrar_detalle_set(update, context, set_name, pagina=pagina, mensaje=query.message, editar=True)
        query.answer()
        return

    # --- PAGINACIÓN DE ÁLBUM ---
    partes = data.split("_")
    if len(partes) == 3 and partes[0] == "lista":
        pagina = int(partes[1])
        usuario_id = int(partes[2])
        if query.from_user.id != usuario_id:
            query.answer(text="Este álbum no es tuyo.", show_alert=True)
            return
        cartas_usuario = list(col_cartas_usuario.find({"user_id": usuario_id}))
        def sort_key(x):
            grupo = grupo_de_carta(x.get('nombre',''), x.get('version','')) or ""
            return (
                grupo.lower(),
                x.get('nombre','').lower(),
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
            mensaje=query.message
        )
        query.answer()

# --------- HANDLERS ---------
dispatcher.add_handler(CommandHandler('idolday', comando_idolday))
dispatcher.add_handler(CommandHandler('album', comando_album))
dispatcher.add_handler(CommandHandler('miid', comando_miid))
dispatcher.add_handler(CommandHandler('bonoidolday', comando_bonoidolday))
dispatcher.add_handler(CommandHandler('comandos', comando_comandos))
dispatcher.add_handler(CommandHandler('giveidol', comando_giveidol))
dispatcher.add_handler(CommandHandler('setsprogreso', comando_setsprogreso))
dispatcher.add_handler(CommandHandler('set', comando_set_detalle))
dispatcher.add_handler(CallbackQueryHandler(manejador_callback))

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
