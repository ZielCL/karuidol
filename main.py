import os
from flask import Flask, request
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Dispatcher, CommandHandler, CallbackQueryHandler
import json
import random
from datetime import datetime, timedelta
from pymongo import MongoClient
from dotenv import load_dotenv

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

# MongoDB setup
client = MongoClient(MONGO_URI)
db = client['karuta_bot']
col_usuarios = db['usuarios']
col_cartas_usuario = db['cartas_usuario']
col_contadores = db['contadores']

# Cargar cartas.json
if not os.path.isfile('cartas.json'):
    cartas_ejemplo = [
        {"nombre": "Tzuyu", "version": "V1", "imagen": "https://example.com/tzuyu_v1.jpg"},
        {"nombre": "Tzuyu", "version": "V2", "imagen": "https://example.com/tzuyu_v2.jpg"},
        {"nombre": "Lisa", "version": "V1", "imagen": "https://example.com/lisa_v1.jpg"}
    ]
    with open('cartas.json', 'w') as f:
        json.dump(cartas_ejemplo, f, indent=2)
with open('cartas.json', 'r') as f:
    cartas = json.load(f)

def imagen_de_carta(nombre, version):
    for carta in cartas:
        if carta['nombre'] == nombre and carta['version'] == version:
            return carta['imagen']
    return None

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

# Comando /miid
def comando_miid(update, context):
    usuario = update.effective_user
    update.message.reply_text(f"Tu ID de Telegram es: {usuario.id}")

# Comando /idolday
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

    # Seleccionar carta aleatoria
    cartas_v1 = [c for c in cartas if c.get('version') == 'V1']
    cartas_v2 = [c for c in cartas if c.get('version') == 'V2']
    carta = None
    if cartas_v2 and random.random() < 0.10:
        carta = random.choice(cartas_v2)
    else:
        carta = random.choice(cartas_v1) if cartas_v1 else random.choice(cartas_v2)
    if not carta:
        context.bot.send_message(chat_id=chat_id, text="No hay cartas disponibles en este momento.")
        return

    nombre = carta['nombre']
    version = carta['version']
    imagen_url = carta.get('imagen')

    # ID incremental por nombre y version
    doc_cont = col_contadores.find_one({"nombre": nombre, "version": version})
    if doc_cont:
        nuevo_id = doc_cont['contador'] + 1
        col_contadores.update_one({"nombre": nombre, "version": version}, {"$inc": {"contador": 1}})
    else:
        nuevo_id = 1
        col_contadores.insert_one({"nombre": nombre, "version": version, "contador": 1})

    # Reclamo pendiente ahora por clave grupo_usuario
    clave = f"{chat_id}_{usuario_id}"
    reclamos_pendientes[clave] = {"nombre": nombre, "version": version, "id": nuevo_id}

    col_usuarios.update_one({"user_id": usuario_id}, {"$set": {"last_idolday": ahora}}, upsert=True)
    texto = f"<b>Carta obtenida:</b> <code>#{nuevo_id} {version} {nombre}</code>"
    teclado = InlineKeyboardMarkup([[InlineKeyboardButton("Reclamar", callback_data=f"reclamar_{chat_id}_{usuario_id}")]])
    if imagen_url:
        try:
            context.bot.send_photo(chat_id=chat_id, photo=imagen_url, caption=texto, reply_markup=teclado, parse_mode='HTML')
        except:
            context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado, parse_mode='HTML')
    else:
        context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado, parse_mode='HTML')

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
    nombre = carta['nombre']; version = carta['version']; cid = carta['id']
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
    cartas_usuario.sort(key=lambda x: x.get('count', 0), reverse=True)
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
    texto = ""
    for carta in lista_cartas[inicio:fin]:
        cid = carta.get('card_id', '')
        version = carta.get('version', '')
        nombre = carta.get('nombre', '')
        cnt = carta.get('count', 1)
        texto += f"<code>#{cid} {version} {nombre}</code>  × <b>{cnt}</b>\n"
    texto += f"\n<b>Página {pagina}/{paginas}</b>"
    botones = []
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

def manejador_callback(update, context):
    query = update.callback_query
    query.answer()
    data = query.data
    if data.startswith("reclamar"):
        manejador_reclamar(update, context)
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
        cartas_usuario.sort(key=lambda x: x.get('count', 0), reverse=True)
        enviar_lista_pagina(query.message.chat_id, usuario_id, cartas_usuario, pagina, context, editar=True, mensaje=query.message)

# /bonoidolday <user_id> <cantidad>
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

dispatcher.add_handler(CommandHandler('idolday', comando_idolday))
dispatcher.add_handler(CommandHandler('album', comando_album))
dispatcher.add_handler(CommandHandler('miid', comando_miid))
dispatcher.add_handler(CommandHandler('bonoidolday', comando_bonoidolday))
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
