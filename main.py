import os
from flask import Flask, request
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Dispatcher, CommandHandler, CallbackQueryHandler
import json
import random
import threading
from datetime import datetime, timedelta
from pymongo import MongoClient
from dotenv import load_dotenv
import re

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
reclamos_pendientes = {}

# Nuevo: Manejo de drops múltiples
drops_pendientes = {}  # chat_id: info_drop

# MongoDB setup
client = MongoClient(MONGO_URI)
db = client['karuta_bot']
col_usuarios = db['usuarios']
col_cartas_usuario = db['cartas_usuario']
col_contadores = db['contadores']

# Cargar cartas.json
if not os.path.isfile('cartas.json'):
    cartas_ejemplo = [
        {"nombre": "Tzuyu", "grupo": "Twice", "version": "V1", "rareza": "Común", "imagen": "https://example.com/tzuyu_v1.jpg"},
        {"nombre": "Tzuyu", "grupo": "Twice", "version": "V2", "rareza": "Rara", "imagen": "https://example.com/tzuyu_v2.jpg"},
        {"nombre": "Lisa", "grupo": "BLACKPINK", "version": "V1", "rareza": "Común", "imagen": "https://example.com/lisa_v1.jpg"}
    ]
    with open('cartas.json', 'w') as f:
        json.dump(cartas_ejemplo, f, indent=2)
with open('cartas.json', 'r') as f:
    cartas = json.load(f)

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

def comando_miid(update, context):
    usuario = update.effective_user
    update.message.reply_text(f"Tu ID de Telegram es: {usuario.id}")

# =============== DROP DE 3 CARTAS ====================

def comando_idolday(update, context):
    usuario_id = update.message.from_user.id
    chat_id = update.effective_chat.id
    ahora = datetime.utcnow()

    if update.effective_chat.type not in ["group", "supergroup"]:
        context.bot.send_message(chat_id=chat_id, text="Este comando solo se puede usar en grupos.")
        return

    # No permitir nuevo drop si hay un drop pendiente sin terminar
    if chat_id in drops_pendientes:
        context.bot.send_message(chat_id=chat_id, text="Ya hay un drop activo en este grupo. Reclamen las cartas antes de hacer un nuevo /idolday.")
        return

    user_doc = col_usuarios.find_one({"user_id": usuario_id})
    bono = user_doc.get('bono', 0) if user_doc else 0
    last = user_doc.get('last_idolday') if user_doc else None
    puede_tirar = False
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

    # Elegir 3 cartas (pueden repetirse versiones)
    cartas_v1 = [c for c in cartas if c.get('version') == 'V1']
    cartas_v2 = [c for c in cartas if c.get('version') == 'V2']
    pool = cartas_v1 if cartas_v1 else cartas
    elegidas = []
    for i in range(3):
        if cartas_v2 and random.random() < 0.10:
            carta = random.choice(cartas_v2)
        else:
            carta = random.choice(pool)
        elegidas.append(carta)

    drops = []
    drop_info = []
    for carta in elegidas:
        nombre = carta['nombre']
        version = carta['version']
        grupo = carta.get('grupo', '')
        imagen_url = carta.get('imagen')
        doc_cont = col_contadores.find_one({"nombre": nombre, "version": version})
        if doc_cont:
            nuevo_id = doc_cont['contador'] + 1
            col_contadores.update_one({"nombre": nombre, "version": version}, {"$inc": {"contador": 1}})
        else:
            nuevo_id = 1
            col_contadores.insert_one({"nombre": nombre, "version": version, "contador": 1})

        drop_info.append({
            "nombre": nombre,
            "version": version,
            "grupo": grupo,
            "card_id": nuevo_id,
            "imagen_url": imagen_url,
            "reclamada": None  # user_id que la reclamó o None
        })

        texto = f"#{nuevo_id} [{version}] {nombre} - {grupo}"
        drops.append(InputMediaPhoto(media=imagen_url, caption=texto, parse_mode='HTML'))

    # Enviar media_group (3 imágenes)
    media_msgs = context.bot.send_media_group(chat_id=chat_id, media=drops)
    media_msg_ids = [msg.message_id for msg in media_msgs]

    # Botones de reclamo (uno por carta, todos activos solo para el usuario)
    botones = [
        [InlineKeyboardButton(f"1️⃣", callback_data=f"drop_{chat_id}_{usuario_id}_0")],
        [InlineKeyboardButton(f"2️⃣", callback_data=f"drop_{chat_id}_{usuario_id}_1")],
        [InlineKeyboardButton(f"3️⃣", callback_data=f"drop_{chat_id}_{usuario_id}_2")],
    ]
    texto_botones = (
        "<b>¡Elige tu carta!</b>\n"
        "Solo el usuario que usó /idolday puede reclamar durante 10 segundos.\n"
        "Luego, cualquiera podrá reclamar las cartas restantes.\n"
        "Si el dueño tiene un <code>/bonoidolday</code>, puede reclamar otra carta después de 30 segundos."
    )
    mensaje_botones = context.bot.send_message(
        chat_id=chat_id, text=texto_botones, reply_markup=InlineKeyboardMarkup(botones), parse_mode='HTML'
    )
    botones_msg_id = mensaje_botones.message_id

    # Guardar en drops_pendientes el estado
    drops_pendientes[chat_id] = {
        "dueno": usuario_id,
        "cartas": drop_info,
        "botones_msg_id": botones_msg_id,
        "media_msg_ids": media_msg_ids,
        "hora_inicio": datetime.utcnow(),
        "reclamos": [],
        "liberado": False,
        "bloqueados": [False, False, False],
        "primer_reclamo": None,
        "puede_reclamar_otra": False
    }

    # Después de 10s, liberar para todos
    def liberar():
        info = drops_pendientes.get(chat_id)
        if info:
            info["liberado"] = True

    t = threading.Timer(10, liberar)
    t.start()

def manejar_drop_reclamo(update, context):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    partes = data.split("_")
    if len(partes) != 4:
        query.answer()
        return
    _, chat_id_str, dueno_id_str, idx_str = partes
    chat_id = int(chat_id_str)
    dueno_id = int(dueno_id_str)
    idx = int(idx_str)

    info = drops_pendientes.get(chat_id)
    if not info or idx not in [0, 1, 2]:
        query.answer("Ya no hay drop activo.", show_alert=True)
        return

    carta_info = info["cartas"][idx]
    if info["bloqueados"][idx]:
        query.answer("❌ Esta carta ya fue reclamada.", show_alert=True)
        return

    ahora = datetime.utcnow()
    tiempo = (ahora - info["hora_inicio"]).total_seconds()

    # 1. Durante los primeros 10s solo el dueño puede reclamar
    if tiempo < 10:
        if user_id != dueno_id:
            query.answer(f"Aún no puedes reclamar esta carta, te quedan {int(10-tiempo)} segundos.", show_alert=True)
            return
        # Si ya reclamó una, pero tiene bono, debe esperar 30s para otro pick
        if info["primer_reclamo"] is not None:
            user_doc = col_usuarios.find_one({"user_id": user_id})
            bono = user_doc.get('bono', 0) if user_doc else 0
            if bono <= 0:
                query.answer("Solo puedes reclamar una carta en los primeros 10 segundos.", show_alert=True)
                return
            else:
                tiempo_primer_pick = (ahora - info["primer_reclamo"]).total_seconds()
                if tiempo_primer_pick < 30:
                    query.answer(f"Debes esperar {int(30-tiempo_primer_pick)} segundos para reclamar otra carta con tu bono.", show_alert=True)
                    return
                # Cuando cumple el delay de bono, le quita uno
                col_usuarios.update_one({"user_id": user_id}, {"$inc": {"bono": -1}}, upsert=True)
                info["puede_reclamar_otra"] = True
    else:
        # Luego de los 10 segundos, cualquiera puede reclamar cartas no reclamadas
        # Si es el dueño, pero ya reclamó (o ya usó bono), no puede volver a reclamar
        if user_id == dueno_id:
            if info["primer_reclamo"] is not None and not info.get("puede_reclamar_otra", False):
                query.answer("Ya reclamaste tu(s) carta(s) en este drop.", show_alert=True)
                return

    # Reclamar carta y bloquear botón
    info["bloqueados"][idx] = True
    carta_info["reclamada"] = user_id
    if user_id == dueno_id:
        if info["primer_reclamo"] is None:
            info["primer_reclamo"] = ahora
            info["puede_reclamar_otra"] = False
        else:
            info["puede_reclamar_otra"] = False  # No más bonos en este drop

    # Añadir carta al usuario
    existe = col_cartas_usuario.find_one({
        "user_id": user_id,
        "nombre": carta_info["nombre"],
        "version": carta_info["version"],
        "card_id": carta_info["card_id"]
    })
    if existe:
        col_cartas_usuario.update_one(
            {"user_id": user_id, "nombre": carta_info["nombre"], "version": carta_info["version"], "card_id": carta_info["card_id"]},
            {"$inc": {"count": 1}}
        )
    else:
        col_cartas_usuario.insert_one({
            "user_id": user_id,
            "nombre": carta_info["nombre"],
            "version": carta_info["version"],
            "card_id": carta_info["card_id"],
            "count": 1
        })

    # Editar botones: botón ahora deshabilitado
    botones = []
    for i in range(3):
        txt = ["1️⃣", "2️⃣", "3️⃣"][i]
        if info["bloqueados"][i]:
            btn = InlineKeyboardButton(f"{txt} (Reclamada)", callback_data="none", disabled=True)
        else:
            btn = InlineKeyboardButton(txt, callback_data=f"drop_{chat_id}_{dueno_id}_{i}")
        botones.append([btn])
    context.bot.edit_message_reply_markup(
        chat_id=chat_id,
        message_id=info["botones_msg_id"],
        reply_markup=InlineKeyboardMarkup(botones)
    )

    # Mensaje de confirmación
    nom_card = f"#{carta_info['card_id']} [{carta_info['version']}] {carta_info['nombre']} - {carta_info['grupo']}"
    query.answer(f"¡{query.from_user.first_name} tomaste la carta {nom_card}!", show_alert=True)
    context.bot.send_message(chat_id=chat_id, text=f"@{query.from_user.username or query.from_user.first_name} tomó la carta {nom_card} !")

    # Si ya se reclamaron todas, elimina el drop
    if all(info["bloqueados"]):
        drops_pendientes.pop(chat_id, None)

def manejador_callback(update, context):
    query = update.callback_query
    data = query.data
    if data.startswith("drop_"):
        manejar_drop_reclamo(update, context)
        return
    # ========== lo demás igual que tu handler ==========
    elif data.startswith("reclamar"):
        manejador_reclamar(update, context)
        return
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
    partes = data.split("_")
    if len(partes) != 3:
        return
    modo, pagina, uid = partes
    pagina = int(pagina); usuario_id = int(uid)
    if query.from_user.id != usuario_id:
        query.answer(text="Este álbum no es tuyo.", show_alert=True)
        return
    if modo == 'lista':
        cartas_usuario = list(col_cartas_usuario.find({"user_id": usuario_id}))
        def sort_key(x):
            grupo = grupo_de_carta(x.get('nombre',''), x.get('version','')) or ""
            return (
                grupo.lower(),
                x.get('nombre','').lower(),
                x.get('card_id', 0)
            )
        cartas_usuario.sort(key=sort_key)
        enviar_lista_pagina(query.message.chat_id, usuario_id, cartas_usuario, pagina, context, editar=True, mensaje=query.message)

# ============= RESTO DE FUNCIONES Y HANDLERS IGUAL =============

# ...aquí tus comandos antiguos como /album, /giveidol, etc., no se modifican...

# /album: Muestra tu colección de cartas ordenadas por grupo, nombre y número
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
        id_carta_album = f"#{cid} [{version}] {nombre} - {grupo}"
        botones.append([InlineKeyboardButton(id_carta_album, callback_data=f"vercarta_{usuario_id}_{idx}")])
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
        except:
            context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado, parse_mode='HTML')
    else:
        context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado, parse_mode='HTML')

def mostrar_carta_individual(chat_id, usuario_id, lista_cartas, idx, context, mensaje_a_editar=None, query=None):
    carta = lista_cartas[idx]
    cid = carta.get('card_id', '')
    version = carta.get('version', '')
    nombre = carta.get('nombre', '')
    grupo = grupo_de_carta(nombre, version)
    imagen_url = imagen_de_carta(nombre, version)
    id_carta = f"#{cid} [{version}] {nombre} - {grupo}"
    texto = f"<b>{id_carta}</b>"

    botones = []
    if idx > 0:
        botones.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"vercarta_{usuario_id}_{idx-1}"))
    if idx < len(lista_cartas)-1:
        botones.append(InlineKeyboardButton("Siguiente ➡️", callback_data=f"vercarta_{usuario_id}_{idx+1}"))
    teclado = InlineKeyboardMarkup([botones] if botones else None)
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

# /bonoidolday: Da bonos de tiradas a un usuario
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
        "<b>/idolday</b> - Obtén una carta aleatoria diaria (drop triple).\n"
        "<b>/album</b> - Muestra tu colección de cartas ordenada por grupo, nombre y número.\n"
        "<b>/giveidol</b> - Regala una carta a otro usuario (usando @usuario o respuesta).\n"
        "<b>/miid</b> - Muestra tu ID de Telegram.\n"
        "<b>/bonoidolday</b> - Da bonos de tiradas de /idolday a un usuario (solo admins).\n"
        "<b>/comandos</b> - Muestra esta lista de comandos y para qué sirve cada uno.\n"
    )
    update.message.reply_text(texto, parse_mode='HTML')

# HANDLERS
dispatcher.add_handler(CommandHandler('idolday', comando_idolday))
dispatcher.add_handler(CommandHandler('album', comando_album))
dispatcher.add_handler(CommandHandler('miid', comando_miid))
dispatcher.add_handler(CommandHandler('bonoidolday', comando_bonoidolday))
# agrega aquí tu /giveidol y otros si tienes
dispatcher.add_handler(CommandHandler('comandos', comando_comandos))
dispatcher.add_handler(CallbackQueryHandler(manejador_callback))

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    global primer_mensaje
    update = Update.de_json(request.get_json(force=True), bot)
    if primer_mensaje and update.message:
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
