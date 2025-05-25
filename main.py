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

client = MongoClient(MONGO_URI)
db = client['karuta_bot']
col_usuarios = db['usuarios']
col_cartas_usuario = db['cartas_usuario']
col_contadores = db['contadores']

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

def es_admin(chat, user_id, context):
    try:
        admins = context.bot.get_chat_administrators(chat.id)
        return any(admin.user.id == user_id for admin in admins)
    except Exception:
        return False

def comando_idolday(update, context):
    if update.effective_chat.type == "private":
        context.bot.send_message(chat_id=update.effective_chat.id, text="Usa este comando en un grupo.")
        return

    usuario_id = update.message.from_user.id
    chat_id = update.effective_chat.id
    ahora = datetime.utcnow()
    usuario = col_usuarios.find_one({"user_id": usuario_id})

    # Usar bono si tiene
    bonos = usuario.get("bonos", 0) if usuario else 0
    puede_reclamar = False

    if bonos > 0:
        puede_reclamar = True
        col_usuarios.update_one({"user_id": usuario_id}, {"$inc": {"bonos": -1}}, upsert=True)
    else:
        # Verificar límite diario
        if usuario and 'last_idolday' in usuario:
            ultimo = usuario['last_idolday']
            if isinstance(ultimo, str):
                ultimo = datetime.fromisoformat(ultimo)
            diferencia = ahora - ultimo
            if diferencia.total_seconds() < 86400:  # menos de 24 horas
                faltante = 86400 - diferencia.total_seconds()
                horas = int(faltante // 3600)
                minutos = int((faltante % 3600) // 60)
                context.bot.send_message(chat_id=chat_id, text=f"Ya usaste /idolday hoy. Intenta de nuevo en {horas}h {minutos}m.")
                return
        puede_reclamar = True

    if not puede_reclamar:
        return

    cartas_v1 = [c for c in cartas if c.get('version') == 'V1']
    cartas_v2 = [c for c in cartas if c.get('version') == 'V2']
    carta = None
    if cartas_v2 and random.random() < 0.10:
        carta = random.choice(cartas_v2)
    else:
        if cartas_v1:
            carta = random.choice(cartas_v1)
        elif cartas_v2:
            carta = random.choice(cartas_v2)
    if not carta:
        context.bot.send_message(chat_id=chat_id, text="No hay cartas disponibles en este momento.")
        return

    nombre = carta['nombre']
    version = carta['version']
    imagen_url = carta.get('imagen')

    doc_cont = col_contadores.find_one({"nombre": nombre, "version": version})
    if doc_cont:
        nuevo_id = doc_cont['contador'] + 1
        col_contadores.update_one({"nombre": nombre, "version": version}, {"$inc": {"contador": 1}})
    else:
        nuevo_id = 1
        col_contadores.insert_one({"nombre": nombre, "version": version, "contador": 1})

    reclamos_pendientes[usuario_id] = {"nombre": nombre, "version": version, "id": nuevo_id}

    if bonos == 0:
        col_usuarios.update_one({"user_id": usuario_id}, {"$set": {"last_idolday": ahora.isoformat()}}, upsert=True)

    texto = f"<b>Carta obtenida:</b>\n#{nuevo_id} {version} <b>{nombre}</b>"
    teclado = InlineKeyboardMarkup([[InlineKeyboardButton("✨ Reclamar ✨", callback_data=f"reclamar_{usuario_id}")]])
    if imagen_url:
        try:
            context.bot.send_photo(chat_id=chat_id, photo=imagen_url, caption=texto, parse_mode='HTML', reply_markup=teclado)
        except Exception:
            context.bot.send_message(chat_id=chat_id, text=texto, parse_mode='HTML', reply_markup=teclado)
    else:
        context.bot.send_message(chat_id=chat_id, text=texto, parse_mode='HTML', reply_markup=teclado)

def manejador_reclamar(update, context):
    query = update.callback_query
    usuario_click = query.from_user.id
    data = query.data
    partes = data.split("_")
    if len(partes) != 2:
        query.answer()
        return
    id_usuario = int(partes[1])
    if usuario_click != id_usuario:
        query.answer(text="Solo puedes reclamar tu propia carta.", show_alert=True)
        return
    if id_usuario not in reclamos_pendientes:
        query.answer(text="No hay carta que reclamar.", show_alert=True)
        return
    carta = reclamos_pendientes[id_usuario]
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
    del reclamos_pendientes[id_usuario]
    try:
        query.edit_message_reply_markup(reply_markup=None)
    except:
        pass
    query.answer(text="Carta reclamada.")

def comando_album(update, context):
    if update.effective_chat.type == "private":
        context.bot.send_message(chat_id=update.effective_chat.id, text="Usa este comando en un grupo.")
        return
    usuario_id = update.message.from_user.id
    chat_id = update.effective_chat.id
    cartas_usuario = list(col_cartas_usuario.find({"user_id": usuario_id}))
    if not cartas_usuario:
        context.bot.send_message(chat_id=chat_id, text="Tu álbum está vacío.\n¡Usa /idolday para reclamar tu primera carta!")
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
    texto = "<b>Tu Álbum:</b>\n"
    for carta in lista_cartas[inicio:fin]:
        cid = carta.get('card_id', '')
        version = carta.get('version', '')
        nombre = carta.get('nombre', '')
        cnt = carta.get('count', 1)
        texto += f"#{cid} {version} {nombre}  <b>x{cnt}</b>\n"
    texto += f"\nPágina {pagina}/{paginas}"
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
            context.bot.send_message(chat_id=chat_id, text=texto, parse_mode='HTML', reply_markup=teclado)
    else:
        context.bot.send_message(chat_id=chat_id, text=texto, parse_mode='HTML', reply_markup=teclado)

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

def comando_bonoidolday(update, context):
    chat = update.effective_chat
    from_user = update.effective_user
    if chat.type == "private":
        update.message.reply_text("Este comando solo funciona en grupos.")
        return
    if not es_admin(chat, from_user.id, context):
        update.message.reply_text("Solo los administradores pueden usar este comando.")
        return

    args = context.args
    if len(args) < 2:
        update.message.reply_text("Uso: /bonoidolday <usuario> <cantidad>")
        return

    usuario_str, cantidad_str = args[0], args[1]
    try:
        cantidad = int(cantidad_str)
        if cantidad < 1:
            raise ValueError
    except ValueError:
        update.message.reply_text("La cantidad debe ser un número positivo.")
        return

    # Buscar por username o ID
    usuario_id = None
    if usuario_str.startswith('@'):
        usuario_str = usuario_str[1:]
    if usuario_str.isdigit():
        usuario_id = int(usuario_str)
    else:
        # Buscar el usuario en el grupo
        found = False
        try:
            miembros = context.bot.get_chat_administrators(chat.id) + context.bot.get_chat(chat.id).get_members()
        except Exception:
            miembros = []
        for miembro in miembros:
            user = miembro.user if hasattr(miembro, "user") else miembro
            if user.username and user.username.lower() == usuario_str.lower():
                usuario_id = user.id
                found = True
                break
        if not found:
            # Búsqueda fallback (menos eficiente)
            try:
                miembros = context.bot.get_chat(chat.id).get_members()
                for user in miembros:
                    if user.username and user.username.lower() == usuario_str.lower():
                        usuario_id = user.id
                        found = True
                        break
            except Exception:
                pass

    if not usuario_id:
        update.message.reply_text("No se pudo encontrar al usuario. Usa /bonoidolday @usuario 3 o /bonoidolday <id> <cantidad>")
        return

    col_usuarios.update_one({"user_id": usuario_id}, {"$inc": {"bonos": cantidad}}, upsert=True)
    update.message.reply_text(f"Se otorgaron {cantidad} tiradas de /idolday a {usuario_str}.")

# Registrar comandos y callbacks
dispatcher.add_handler(CommandHandler('idolday', comando_idolday))
dispatcher.add_handler(CommandHandler('album', comando_album))
dispatcher.add_handler(CommandHandler('bonoidolday', comando_bonoidolday))
dispatcher.add_handler(CallbackQueryHandler(manejador_callback))

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    global primer_mensaje
    update = Update.de_json(request.get_json(force=True), bot)
    if primer_mensaje and update.message:
        try:
            bot.send_message(chat_id=update.effective_chat.id, text="🤖 ¡Karuta Bot está activo en este grupo!")
        except:
            pass
        primer_mensaje = False
    dispatcher.process_update(update)
    return 'OK'

@app.route("/", methods=["GET"])
def home():
    return "Karuta Bot activo. Versión Telegram - Render.com"

if __name__ == '__main__':
    puerto = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=puerto)
