import os
import threading
from flask import Flask, request
from telegram import (
    Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto
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

# --- Utilidades ---
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

# --- SISTEMA DROP 2 CARTAS ---
drops_activos = {}  # chat_id -> info_drop

def comando_idolday(update, context):
    usuario_id = update.message.from_user.id
    chat_id = update.effective_chat.id
    ahora = datetime.utcnow()

    if update.effective_chat.type not in ["group", "supergroup"]:
        context.bot.send_message(chat_id=chat_id, text="Este comando solo se puede usar en grupos.")
        return

    # Control de tirada diaria o con bono
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

    # Pool de cartas, 2 al azar
    pool = [c for c in cartas]
    if len(pool) < 2:
        context.bot.send_message(chat_id=chat_id, text="No hay suficientes cartas para el drop.")
        return
    cartas_drop = random.sample(pool, 2)

    # Prepara info para la base de datos y los botones
    cartas_info = []
    media = []
    ids_cartas = []
    for carta in cartas_drop:
        nombre = carta['nombre']
        version = carta['version']
        grupo = carta.get('grupo', '')
        imagen_url = carta.get('imagen')
        # id de carta incremental (para no repetir)
        doc_cont = col_contadores.find_one({"nombre": nombre, "version": version})
        if doc_cont:
            nuevo_id = doc_cont['contador'] + 1
            col_contadores.update_one({"nombre": nombre, "version": version}, {"$inc": {"contador": 1}})
        else:
            nuevo_id = 1
            col_contadores.insert_one({"nombre": nombre, "version": version, "contador": 1})
        ids_cartas.append(nuevo_id)
        caption = f"#{nuevo_id} [{version}] {nombre} - {grupo}"
        media.append(InputMediaPhoto(media=imagen_url, caption=caption))
        cartas_info.append({
            "nombre": nombre, "version": version, "id": nuevo_id, "grupo": grupo,
            "imagen": imagen_url, "reclamada": False, "por": None, "momento": None
        })

    # Envía media group
    msgs = context.bot.send_media_group(chat_id=chat_id, media=media)
    mensaje_id_base = msgs[0].message_id

    # Envía los botones de pick
    teclado = [
        [
            InlineKeyboardButton("1", callback_data=f"drop_{chat_id}_0"),
            InlineKeyboardButton("2", callback_data=f"drop_{chat_id}_1")
        ]
    ]
    msg_botones = context.bot.send_message(chat_id=chat_id, text="¡Drop! Solo quien usó /idolday puede reclamar en los primeros 10 segundos.",
                                           reply_markup=InlineKeyboardMarkup(teclado))

    # Guarda estado
    info_drop = {
        'usuario_id': usuario_id,
        'cartas': cartas_info,
        'timestamp': ahora,
        'primer_reclamo_usuario': None,
        'puede_segundo_pick': False,
        'msg_botones_id': msg_botones.message_id,
        'pick_hecho_usuario': 0,  # cuantos picks lleva el dueño (máximo 2, pero el 2do solo tras 30s y con bono)
        'bono_usado': False,
        'activo': True
    }
    drops_activos[chat_id] = info_drop

    # Timer para liberar cartas tras 10s
    def liberar_picks():
        if drops_activos.get(chat_id) and drops_activos[chat_id]['activo']:
            context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_botones.message_id,
                text="¡Ya cualquiera puede reclamar las cartas disponibles!",
                reply_markup=InlineKeyboardMarkup(teclado_drop(chat_id))
            )
    threading.Timer(10, liberar_picks).start()

    # Timer para cerrar el drop tras 60s
    def cerrar_drop():
        drop = drops_activos.get(chat_id)
        if drop and drop['activo']:
            drop['activo'] = False
            context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_botones.message_id,
                text="⏳ Drop finalizado. Ya no se pueden reclamar cartas.",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("❌", callback_data="nada"),
                        InlineKeyboardButton("❌", callback_data="nada")
                    ]
                ])
            )
            drops_activos.pop(chat_id, None)
    threading.Timer(60, cerrar_drop).start()

def teclado_drop(chat_id):
    # Genera teclado según cartas reclamadas (para editar botones)
    drop = drops_activos.get(chat_id)
    if not drop:
        return [
            [
                InlineKeyboardButton("❌", callback_data="nada"),
                InlineKeyboardButton("❌", callback_data="nada")
            ]
        ]
    botones = []
    for idx, carta in enumerate(drop['cartas']):
        if carta['reclamada']:
            botones.append(InlineKeyboardButton("❌", callback_data="nada"))
        else:
            botones.append(InlineKeyboardButton(str(idx+1), callback_data=f"drop_{chat_id}_{idx}"))
    return [botones]

def manejar_drop_reclamo(update, context):
    query = update.callback_query
    data = query.data
    usuario_id = query.from_user.id
    partes = data.split("_")
    if len(partes) != 3:
        query.answer()
        return
    chat_id = int(partes[1])
    carta_idx = int(partes[2])

    drop = drops_activos.get(chat_id)
    if not drop or not drop['activo']:
        query.answer("Drop finalizado.", show_alert=True)
        return

    ahora = datetime.utcnow()
    carta = drop['cartas'][carta_idx]
    segundos = (ahora - drop['timestamp']).total_seconds()

    # Ya reclamada
    if carta['reclamada']:
        query.answer("Esta carta ya fue reclamada.", show_alert=True)
        return

    # Durante 10s solo el dueño puede pickear una
    if segundos < 10:
        if usuario_id != drop['usuario_id']:
            query.answer(f"Aún no puedes reclamar esta carta, espera {int(10-segundos)}s.", show_alert=True)
            return
        if drop['pick_hecho_usuario'] >= 1:
            query.answer("Ya reclamaste tu carta. Si tienes bono, espera 30s para reclamar otra.", show_alert=True)
            return
        # Reclama su primera carta
        drop['pick_hecho_usuario'] += 1
        drop['primer_reclamo_usuario'] = ahora
    else:
        # Después de 10s, cualquiera puede reclamar las no tomadas
        if usuario_id == drop['usuario_id']:
            # Dueño puede hacer segundo pick solo si tiene bono, solo después de 30s desde el primer pick
            if drop['pick_hecho_usuario'] == 1:
                user_doc = col_usuarios.find_one({"user_id": usuario_id})
                bono_actual = user_doc.get('bono', 0) if user_doc else 0
                if not drop['bono_usado'] and bono_actual > 0:
                    t_wait = 30 - (ahora - drop['primer_reclamo_usuario']).total_seconds()
                    if t_wait > 0:
                        query.answer(f"Debes esperar {int(t_wait)}s para usar tu bono.", show_alert=True)
                        return
                    # Gasta bono y permite segundo pick
                    col_usuarios.update_one({"user_id": usuario_id}, {"$inc": {"bono": -1}})
                    drop['bono_usado'] = True
                    drop['pick_hecho_usuario'] += 1
                elif drop['bono_usado']:
                    query.answer("Ya reclamaste tu bono.", show_alert=True)
                    return
        # Si es otro usuario, puede tomar si está libre (y segundos >=10)
        # No hay reglas extras

    # Marca carta reclamada
    carta['reclamada'] = True
    carta['por'] = usuario_id
    carta['momento'] = ahora

    # Guardar en la base de datos
    existente = col_cartas_usuario.find_one({"user_id": usuario_id, "nombre": carta['nombre'], "version": carta['version'], "card_id": carta['id']})
    if existente:
        col_cartas_usuario.update_one(
            {"user_id": usuario_id, "nombre": carta['nombre'], "version": carta['version'], "card_id": carta['id']},
            {"$inc": {"count": 1}}
        )
    else:
        col_cartas_usuario.insert_one(
            {"user_id": usuario_id, "nombre": carta['nombre'], "version": carta['version'], "card_id": carta['id'], "count": 1}
        )

    # Edita botones
    context.bot.edit_message_reply_markup(
        chat_id=query.message.chat_id,
        message_id=drop['msg_botones_id'],
        reply_markup=InlineKeyboardMarkup(teclado_drop(chat_id))
    )

    # Mensaje al grupo
    usuario_m = f"@{query.from_user.username}" if query.from_user.username else query.from_user.full_name
    grupo = carta['grupo']
    context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"{usuario_m} tomó la carta #{carta['id']} [{carta['version']}] {carta['nombre']} - {grupo} !"
    )
    query.answer("¡Carta reclamada!", show_alert=False)

    # Si ya no quedan cartas, termina el drop
    if all(c['reclamada'] for c in drop['cartas']):
        drop['activo'] = False
        drops_activos.pop(chat_id, None)
        context.bot.edit_message_text(
            chat_id=query.message.chat_id,
            message_id=drop['msg_botones_id'],
            text="Todas las cartas fueron reclamadas.",
            reply_markup=InlineKeyboardMarkup(teclado_drop(chat_id))
        )

# --- TUS FUNCIONES ANTIGUAS ABAJO (NO MODIFICADAS) ---
# ---- RECLAMO DE DROP CLÁSICO (por si tienes drops individuales) ----
def manejador_reclamar(update, context):
    query = update.callback_query
    usuario_click = query.from_user.id
    data = query.data
    partes = data.split("_")
    if len(partes) != 3:
        query.answer()
        return
    chat_id = partes[1]
    id_usuario = int(partes[2])
    clave = f"{chat_id}_{id_usuario}"
    if usuario_click != id_usuario:
        query.answer(text="❌ Este drop no te pertenece.", show_alert=True)
        return
    if clave not in reclamos_pendientes:
        query.answer(text="No hay carta que reclamar.", show_alert=True)
        return
    carta = reclamos_pendientes[clave]
    nombre = carta['nombre']
    version = carta['version']
    cid = carta['id']
    grupo = grupo_de_carta(nombre, version)
    existente = col_cartas_usuario.find_one({"user_id": id_usuario, "nombre": nombre, "version": version, "card_id": cid})
    if existente:
        col_cartas_usuario.update_one(
            {"user_id": id_usuario, "nombre": nombre, "version": version, "card_id": cid},
            {"$inc": {"count": 1}}
        )
    else:
        col_cartas_usuario.insert_one(
            {"user_id": id_usuario, "nombre": nombre, "version": version, "card_id": cid, "count": 1}
        )
    del reclamos_pendientes[clave]
    try:
        query.edit_message_reply_markup(reply_markup=None)
    except:
        pass
    query.answer(text="✅ Carta reclamada.", show_alert=True)

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
        "<b>/idolday</b> - Drop de 2 cartas (solo una diaria por usuario)\n"
        "<b>/album</b> - Muestra tu colección de cartas ordenada por grupo, nombre y número.\n"
        "<b>/giveidol</b> - Regala una carta a otro usuario (usando @usuario o respuesta).\n"
        "<b>/miid</b> - Muestra tu ID de Telegram.\n"
        "<b>/bonoidolday</b> - Da bonos de tiradas de /idolday a un usuario (solo admins).\n"
        "<b>/comandos</b> - Muestra esta lista de comandos y para qué sirve cada uno.\n"
    )
    update.message.reply_text(texto, parse_mode='HTML')

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

# --- Handlers clásicos ---
dispatcher.add_handler(CommandHandler('idolday', comando_idolday))
dispatcher.add_handler(CommandHandler('album', comando_album))
dispatcher.add_handler(CommandHandler('miid', lambda u,c: u.message.reply_text(f"Tu ID de Telegram es: {u.effective_user.id}")))
dispatcher.add_handler(CommandHandler('bonoidolday', comando_bonoidolday))
dispatcher.add_handler(CommandHandler('giveidol', comando_giveidol))
dispatcher.add_handler(CommandHandler('comandos', comando_comandos))
dispatcher.add_handler(CallbackQueryHandler(manejador_callback))

def manejador_callback(update, context):
    query = update.callback_query
    data = query.data
    # --- Drop de 2 cartas (nuevo) ---
    if data.startswith("drop_"):
        manejar_drop_reclamo(update, context)
        return
    # --- Reclamo clásico (drop individual, por si lo usas) ---
    if data.startswith("reclamar"):
        manejador_reclamar(update, context)
        return
    # --- Navegación de álbum ---
    if data.startswith("vercarta"):
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
