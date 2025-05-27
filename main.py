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
    cartas_ejemplo = [
        {"nombre": "Tzuyu", "grupo": "Twice", "version": "V1", "rareza": "Común", "imagen": "https://example.com/tzuyu_v1.jpg"},
        {"nombre": "Suho", "grupo": "EXO", "version": "V1", "rareza": "Rara", "imagen": "https://example.com/suho_v1.jpg"},
        {"nombre": "Lisa", "grupo": "BLACKPINK", "version": "V1", "rareza": "Común", "imagen": "https://example.com/lisa_v1.jpg"}
    ]
    with open('cartas.json', 'w') as f:
        json.dump(cartas_ejemplo, f, indent=2)
with open('cartas.json', 'r') as f:
    cartas = json.load(f)

DROPS_ACTIVOS = {}

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

    cartas_disponibles = cartas if len(cartas) >= 2 else cartas * 2
    cartas_drop = random.sample(cartas_disponibles, 2)
    cartas_info = []
    media_group = []
    for carta in cartas_drop:
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
        cartas_info.append({
            "nombre": nombre,
            "version": version,
            "grupo": grupo,
            "imagen": imagen_url,
            "card_id": nuevo_id,
            "reclamada": False,
            "usuario": None,
            "hora_reclamada": None,
        })
        caption = f"<b>#{nuevo_id} [{version}] {nombre} - {grupo}</b>"
        media_group.append(InputMediaPhoto(media=imagen_url, caption=caption, parse_mode="HTML"))

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
        "primer_reclamo_dueño": None,  # Para controlar segunda carta tras 10s y solo con bono
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

def desbloquear_drop(drop_id):
    data = DROPS_ACTIVOS[drop_id]
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
    if not drop or drop["expirado"]:
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
    except:
        pass
    drop["expirado"] = True

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
    solo_dueño = tiempo_desde_drop < 10
    puede_reclamar = False

    user_doc = col_usuarios.find_one({"user_id": usuario_click}) or {}
    bono = user_doc.get('bono', 0)

    if usuario_click == drop["dueño"]:
        primer_reclamo = drop.get("primer_reclamo_dueño")
        if primer_reclamo is None:
            puede_reclamar = True
            drop["primer_reclamo_dueño"] = ahora
        else:
            if tiempo_desde_drop < 10:
                query.answer("Solo puedes reclamar una carta antes de 10 segundos. Espera a que pasen 10 segundos para reclamar la otra (si tienes bono).", show_alert=True)
                return
            if bono < 1:
                query.answer("Necesitas al menos 1 bono para reclamar la segunda carta.", show_alert=True)
                return
            puede_reclamar = True
            col_usuarios.update_one({"user_id": usuario_click}, {"$inc": {"bono": -1}}, upsert=True)
    elif not solo_dueño and carta["usuario"] is None:
        puede_reclamar = True
    else:
        segundos_faltantes = int(10 - tiempo_desde_drop)
        if segundos_faltantes < 0:
            segundos_faltantes = 0
        query.answer(f"Aún no puedes reclamar esta carta, te quedan {segundos_faltantes} segundos para poder reclamar.", show_alert=True)
        return

    if not puede_reclamar:
        query.answer("No puedes reclamar esta carta.", show_alert=True)
        return

    carta["reclamada"] = True
    carta["usuario"] = usuario_click
    carta["hora_reclamada"] = ahora
    drop["usuarios_reclamaron"].append(usuario_click)

    nombre = carta['nombre']
    version = carta['version']
    grupo = carta['grupo']
    cid = carta['card_id']
    existente = col_cartas_usuario.find_one({
        "user_id": usuario_click,
        "nombre": nombre,
        "version": version,
        "card_id": cid
    })
    if existente:
        col_cartas_usuario.update_one(
            {"user_id": usuario_click, "nombre": nombre, "version": version, "card_id": cid},
            {"$inc": {"count": 1}}
        )
    else:
        col_cartas_usuario.insert_one(
            {"user_id": usuario_click, "nombre": nombre, "version": version, "grupo": grupo, "card_id": cid, "count": 1}
        )

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
    context.bot.send_message(
        chat_id=drop["chat_id"],
        text=f"{user_mention} tomaste la carta #{cid} [{version}] {nombre} - {grupo} !"
    )
    query.answer("¡Carta reclamada!", show_alert=True)

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

def comando_miid(update, context):
    usuario = update.effective_user
    update.message.reply_text(f"Tu ID de Telegram es: {usuario.id}")

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

def comando_giveidol(update, context):
    if len(context.args) < 1:
        update.message.reply_text(
            "Uso: /giveidol <carta> <@usuario o responde al usuario>\n"
            "Ejemplo: /giveidol #7V1TzuyuTwice @destino"
        )
        return

    usuario_id = update.message.from_user.id
    chat = update.effective_chat
    carta_arg = context.args[0]
    if not carta_arg.startswith("#"):
        update.message.reply_text("Debes indicar la carta con el formato #IDVnNombreGrupo. Ejemplo: /giveidol #7V1TzuyuTwice @destino")
        return

    carta_arg = carta_arg[1:]
    m = re.match(r'(\d+)(V\d+)([^\s]+)', carta_arg)
    if not m:
        update.message.reply_text("Formato incorrecto. Usa: /giveidol #7V1TzuyuTwice @destino")
        return
    card_id, version, resto = m.group(1), m.group(2), m.group(3)
    card_id_int = int(card_id)
    version = version.upper()
    carta_en_db = None
    for c in cartas:
        possible = f"{c['nombre']}{c['grupo']}".replace(" ", "").lower()
        if resto.lower() == possible and c['version'] == version:
            carta_en_db = c
            break
    if not carta_en_db:
        update.message.reply_text("No se encontró una carta con ese nombre/grupo/version.")
        return
    nombre = carta_en_db['nombre']
    grupo = carta_en_db['grupo']
    target_user_id = None
    username_dest = None
    full_name_dest = None
    if update.message.reply_to_message:
        target_user_id = update.message.reply_to_message.from_user.id
        username_dest = update.message.reply_to_message.from_user.username
        full_name_dest = update.message.reply_to_message.from_user.full_name
    elif len(context.args) >= 2:
        usuario_mention = context.args[1]
        if usuario_mention.startswith("@"):
            username_dest = usuario_mention[1:].lower()
            posible = col_usuarios.find_one({"username": username_dest})
            if posible:
                target_user_id = posible["user_id"]
            if not target_user_id:
                try:
                    member = context.bot.get_chat_member(chat.id, username_dest)
                    if member and member.user and member.user.username and member.user.username.lower() == username_dest:
                        target_user_id = member.user.id
                        full_name_dest = member.user.full_name
                except Exception:
                    pass
        else:
            try:
                target_user_id = int(usuario_mention)
            except:
                pass
    elif update.message.entities:
        for entity in update.message.entities:
            if entity.type == "text_mention" and entity.user:
                target_user_id = entity.user.id
                username_dest = entity.user.username
                full_name_dest = entity.user.full_name
                break

    if not target_user_id:
        update.message.reply_text("No pude identificar al usuario destino. Usa @username (que haya hablado al menos una vez), responde al usuario, o menciona a alguien que esté en el grupo.")
        return
    if usuario_id == target_user_id:
        update.message.reply_text("No puedes regalarte cartas a ti mismo.")
        return

    carta = col_cartas_usuario.find_one({
        "user_id": usuario_id,
        "card_id": card_id_int,
        "version": version,
        "nombre": nombre
    })
    if not carta or carta.get("count", 1) < 1:
        update.message.reply_text("No tienes esa carta para regalar.")
        return

    if carta["count"] > 1:
        col_cartas_usuario.update_one(
            {"user_id": usuario_id, "card_id": card_id_int, "version": version, "nombre": nombre},
            {"$inc": {"count": -1}}
        )
    else:
        col_cartas_usuario.delete_one(
            {"user_id": usuario_id, "card_id": card_id_int, "version": version, "nombre": nombre}
        )

    existente = col_cartas_usuario.find_one(
        {"user_id": target_user_id, "card_id": card_id_int, "version": version, "nombre": nombre}
    )
    if existente:
        col_cartas_usuario.update_one(
            {"user_id": target_user_id, "card_id": card_id_int, "version": version, "nombre": nombre},
            {"$inc": {"count": 1}}
        )
    else:
        col_cartas_usuario.insert_one(
            {
                "user_id": target_user_id,
                "nombre": nombre,
                "version": version,
                "card_id": card_id_int,
                "count": 1
            }
        )

    if target_user_id:
        try:
            user_dest_data = context.bot.get_chat_member(chat.id, target_user_id).user
            if user_dest_data.username:
                col_usuarios.update_one(
                    {"user_id": target_user_id},
                    {"$set": {"username": user_dest_data.username.lower()}},
                    upsert=True
                )
            if not username_dest:
                username_dest = user_dest_data.username
            if not full_name_dest:
                full_name_dest = user_dest_data.full_name
        except:
            pass

    id_carta_give = f"#{card_id}{version}{nombre}{grupo}"
    dest_mention = f"@{username_dest}" if username_dest else (full_name_dest if full_name_dest else "el usuario")
    update.message.reply_text(
        f"🎁 ¡Carta <b>{id_carta_give}</b> enviada correctamente a {dest_mention}!",
        parse_mode='HTML'
    )

    try:
        notif = (
            f"🎉 <b>¡Has recibido una carta!</b>\n"
            f"Te han regalado <b>{id_carta_give}</b>.\n"
            f"¡Revisa tu álbum con <code>/album</code>!"
        )
        context.bot.send_message(chat_id=target_user_id, text=notif, parse_mode='HTML')
    except Exception:
        try:
            context.bot.send_message(chat_id=chat.id, text=f"¡{dest_mention}, te han regalado <b>{id_carta_give}</b>!", parse_mode='HTML')
        except:
            pass

def comando_comandos(update, context):
    texto = (
        "📋 <b>Lista de comandos disponibles:</b>\n"
        "\n"
        "<b>/idolday</b> - Drop de 2 cartas con botones.\n"
        "<b>/album</b> - Muestra tu colección de cartas.\n"
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
dispatcher.add_handler(CommandHandler('giveidol', comando_giveidol))
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
