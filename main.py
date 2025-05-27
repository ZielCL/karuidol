import os
from flask import Flask, request
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Dispatcher, CommandHandler, CallbackQueryHandler
import json
import random
from datetime import datetime
from pymongo import MongoClient
from dotenv import load_dotenv
import re
import threading
import time

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
drop_activos = {}  # Guardará la info de los drops activos

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
        {"nombre": "Suho", "grupo": "Exo", "version": "V1", "rareza": "Común", "imagen": "https://example.com/suho_v1.jpg"},
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
def comando_idolday(update, context):
    usuario_id = update.message.from_user.id
    chat_id = update.effective_chat.id
    ahora = datetime.utcnow()

    if update.effective_chat.type not in ["group", "supergroup"]:
        context.bot.send_message(chat_id=chat_id, text="Este comando solo se puede usar en grupos.")
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

    # 2 cartas aleatorias, pueden ser de cualquier versión
    cartas_v1 = [c for c in cartas if c.get('version') == 'V1']
    cartas_v2 = [c for c in cartas if c.get('version') == 'V2']
    disponibles = cartas_v1 + cartas_v2 if cartas_v2 else cartas_v1
    if len(disponibles) < 2:
        context.bot.send_message(chat_id=chat_id, text="No hay suficientes cartas para dropear.")
        return

    drop = random.sample(disponibles, 2)
    cartas_drop = []
    for carta in drop:
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
        cartas_drop.append({
            "id": nuevo_id, "nombre": nombre, "version": version,
            "grupo": grupo, "imagen": imagen_url, "reclamada": False, "dueño": usuario_id
        })

    # Guardar drop activo (manejo de tiempos y botones)
    clave_drop = f"{chat_id}_{usuario_id}_{int(time.time())}"
    drop_activos[clave_drop] = {
        "cartas": cartas_drop,
        "start": time.time(),
        "bloqueado_hasta": time.time() + 10,      # Solo dueño puede reclamar 10s
        "timeout": time.time() + 60,              # Expira en 60s
        "primer_claim": None,                     # Para controlar cooldown
        "dueño": usuario_id,
        "chat_id": chat_id,
        "mensaje_id": None
    }

    # Botones y MediaGroup
    media = []
    botones = []
    for idx, carta in enumerate(cartas_drop):
        caption = f"<b>#{carta['id']} [{carta['version']}] {carta['nombre']} - {carta['grupo']}</b>"
        media.append(InputMediaPhoto(media=carta["imagen"], caption=caption, parse_mode='HTML'))
        botones.append([InlineKeyboardButton(f"Reclamar carta {idx+1}", callback_data=f"reclamar_{clave_drop}_{idx}")])
    teclado = InlineKeyboardMarkup(botones)
    msg = context.bot.send_media_group(chat_id=chat_id, media=media)
    main_msg = context.bot.send_message(chat_id=chat_id, text="¡Reclama tu carta pulsando un botón!", reply_markup=teclado)

    # Guardar mensaje_id principal para poder editar
    drop_activos[clave_drop]["mensaje_id"] = main_msg.message_id

    # Cronómetro para deshabilitar después de 60s
    def disable_drop():
        # Al expirar, deshabilita todos los botones
        try:
            context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=main_msg.message_id, reply_markup=None)
        except:
            pass
        drop_activos.pop(clave_drop, None)

    threading.Timer(60, disable_drop).start()

    # Guardar último drop para el dueño (para el bono cooldown)
    col_usuarios.update_one({"user_id": usuario_id}, {"$set": {"last_idolday": ahora, "username": update.effective_user.username.lower() if update.effective_user.username else ""}}, upsert=True)
def manejador_callback(update, context):
    query = update.callback_query
    data = query.data

    # Para reclamos del drop doble
    if data.startswith("reclamar_"):
        partes = data.split("_")
        if len(partes) != 4:
            query.answer()
            return
        _, clave_drop, idx = "_".join(partes[:3]), "_".join(partes[1:4]), int(partes[3])
        if clave_drop not in drop_activos:
            query.answer(text="Este drop ya expiró o fue reclamado.", show_alert=True)
            return
        drop = drop_activos[clave_drop]
        carta = drop["cartas"][idx]
        usuario = query.from_user.id
        ahora = time.time()

        # Chequear si ya fue reclamada
        if carta["reclamada"]:
            query.answer(text="Esta carta ya fue reclamada.", show_alert=True)
            return

        # Si aún está en los primeros 10 segundos: solo el dueño puede reclamar
        if ahora < drop["bloqueado_hasta"]:
            if usuario != drop["dueño"]:
                faltante = int(drop["bloqueado_hasta"] - ahora)
                query.answer(text=f"Aún no puedes reclamar esta carta, te quedan {faltante}s.", show_alert=True)
                return
        else:
            # Si ya reclamó una y quiere otra, revisar cooldown de bono
            if usuario == drop["dueño"]:
                if drop["primer_claim"] is not None:
                    delta = ahora - drop["primer_claim"]
                    if delta < 30:
                        query.answer(text=f"Debes esperar {30-int(delta)}s para volver a reclamar como dueño.", show_alert=True)
                        return
                    # Solo si tiene bono disponible puede reclamar otra
                    user_doc = col_usuarios.find_one({"user_id": usuario})
                    bono = user_doc.get('bono', 0) if user_doc else 0
                    if bono < 1:
                        query.answer(text="Necesitas un bono /bonoidolday para reclamar otra carta tuya.", show_alert=True)
                        return
                    col_usuarios.update_one({"user_id": usuario}, {"$inc": {"bono": -1}})
            # Si NO es dueño ni tiene bono, sólo puede reclamar después de los 10s normales
            else:
                if ahora < drop["bloqueado_hasta"]:
                    faltante = int(drop["bloqueado_hasta"] - ahora)
                    query.answer(text=f"Aún no puedes reclamar esta carta, te quedan {faltante}s.", show_alert=True)
                    return

        # Reclamar la carta y desactivar botón
        carta["reclamada"] = True
        col_cartas_usuario.insert_one({
            "user_id": usuario,
            "nombre": carta["nombre"],
            "version": carta["version"],
            "card_id": carta["id"],
            "count": 1
        })
        # Marcar primer claim para cooldown de bono
        if usuario == drop["dueño"] and drop["primer_claim"] is None:
            drop["primer_claim"] = ahora

        # Editar botones (desactivar sólo ese)
        main_msg_id = drop["mensaje_id"]
        nuevos_botones = []
        for i, c in enumerate(drop["cartas"]):
            estado = "Reclamada" if c["reclamada"] else f"Reclamar carta {i+1}"
            nuevos_botones.append([
                InlineKeyboardButton(
                    estado,
                    callback_data=f"reclamar_{clave_drop}_{i}" if not c["reclamada"] else "none",
                )
            ])
        teclado = InlineKeyboardMarkup(nuevos_botones)
        try:
            context.bot.edit_message_reply_markup(
                chat_id=drop["chat_id"],
                message_id=main_msg_id,
                reply_markup=teclado
            )
        except:
            pass

        query.answer(text=f"¡{query.from_user.first_name} tomaste la carta #{carta['id']} [{carta['version']}] {carta['nombre']} - {carta['grupo']}!", show_alert=True)
        return

    # --- Lo de siempre: callbacks del álbum, paginación, etc.
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
        "<b>/idolday</b> - Drop diario doble, con cooldowns tipo Karuta.\n"
        "<b>/album</b> - Tu colección de cartas.\n"
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
