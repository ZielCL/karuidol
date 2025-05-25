import os
from flask import Flask, request
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Dispatcher, CommandHandler, CallbackQueryHandler
import json
import random
from datetime import datetime, timedelta
import pymongo
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

# Estado global para mensajes y reclamos pendientes
primer_mensaje = True
reclamos_pendientes = {}

# Configuración de MongoDB
client = MongoClient(MONGO_URI)
db = client['karuta_bot']
col_usuarios = db['usuarios']
col_cartas_usuario = db['cartas_usuario']
col_contadores = db['contadores']

# Cargar o crear cartas.json con ejemplos
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

# Función auxiliar: obtener URL de imagen de carta por nombre y versión
def imagen_de_carta(nombre, version):
    for carta in cartas:
        if carta['nombre'] == nombre and carta['version'] == version:
            return carta['imagen']
    return None

# Comando /idolday: permite obtener una carta aleatoria una vez al día
def comando_idolday(update, context):
    usuario_id = update.message.from_user.id
    chat_id = update.effective_chat.id
    ahora = datetime.utcnow()
    # Verificar límite diario
    usuario = col_usuarios.find_one({"user_id": usuario_id})
    if usuario and 'last_idolday' in usuario:
        ultimo = usuario['last_idolday']
        diferencia = ahora - ultimo
        if diferencia.total_seconds() < 86400:  # menos de 24 horas
            faltante = 86400 - diferencia.total_seconds()
            horas = int(faltante // 3600)
            minutos = int((faltante % 3600) // 60)
            context.bot.send_message(chat_id=chat_id, text=f"Ya usaste /idolday hoy. Intenta de nuevo en {horas}h {minutos}m.")
            return
    # Seleccionar carta aleatoria: V1 90%, V2 10%
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
    # Obtener ID incremental por nombre y versión
    doc_cont = col_contadores.find_one({"nombre": nombre, "version": version})
    if doc_cont:
        nuevo_id = doc_cont['contador'] + 1
        col_contadores.update_one({"nombre": nombre, "version": version}, {"$inc": {"contador": 1}})
    else:
        nuevo_id = 1
        col_contadores.insert_one({"nombre": nombre, "version": version, "contador": 1})
    # Guardar reclamo pendiente
    reclamos_pendientes[usuario_id] = {"nombre": nombre, "version": version, "id": nuevo_id}
    # Actualizar última vez usado
    col_usuarios.update_one({"user_id": usuario_id}, {"$set": {"last_idolday": ahora}}, upsert=True)
    # Enviar carta con botón Reclamar
    texto = f"Carta obtenida: #{nuevo_id} {version} {nombre}"
    teclado = InlineKeyboardMarkup([[InlineKeyboardButton("Reclamar", callback_data=f"reclamar_{usuario_id}")]])
    if imagen_url:
        try:
            context.bot.send_photo(chat_id=chat_id, photo=imagen_url, caption=texto, reply_markup=teclado)
        except:
            context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado)
    else:
        context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado)

# Manejar clic en "Reclamar"
def manejador_reclamar(update, context):
    query = update.callback_query
    usuario_click = query.from_user.id
    data = query.data  # formato "reclamar_{usuario_id}"
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
    # Recuperar carta pendiente
    carta = reclamos_pendientes[id_usuario]
    nombre = carta['nombre']; version = carta['version']; cid = carta['id']
    # Guardar carta en BD
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
    # Eliminar reclamo pendiente y deshabilitar botón
    del reclamos_pendientes[id_usuario]
    try:
        query.edit_message_reply_markup(reply_markup=None)
    except:
        pass
    query.answer(text="Carta reclamada.")

# Comando /album: muestra las cartas del usuario en lista
def comando_album(update, context):
    usuario_id = update.message.from_user.id
    chat_id = update.effective_chat.id
    cartas_usuario = list(col_cartas_usuario.find({"user_id": usuario_id}))
    if not cartas_usuario:
        context.bot.send_message(chat_id=chat_id, text="Tu álbum está vacío.")
        return
    # Ordenar por cantidad descendente
    cartas_usuario.sort(key=lambda x: x.get('count', 0), reverse=True)
    pagina = 1
    enviar_lista_pagina(chat_id, usuario_id, cartas_usuario, pagina, context)

# Enviar o editar página de lista de álbum
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
        texto += f"#{cid} {version} {nombre} Cant: {cnt}\n"
    texto += f"\nPágina {pagina}/{paginas}"
    # Botones de navegación
    botones = []
    nav = []
    if pagina > 1:
        nav.append(InlineKeyboardButton("« Anterior", callback_data=f"lista_{pagina-1}_{usuario_id}"))
    if pagina < paginas:
        nav.append(InlineKeyboardButton("Siguiente »", callback_data=f"lista_{pagina+1}_{usuario_id}"))
    if nav:
        botones.append(nav)
    # Botón cambiar a Modo Álbum
    botones.append([InlineKeyboardButton("Modo Álbum", callback_data=f"album_1_{usuario_id}")])
    teclado = InlineKeyboardMarkup(botones)
    if editar and mensaje:
        try:
            mensaje.edit_text(texto, reply_markup=teclado)
        except:
            context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado)
    else:
        context.bot.send_message(chat_id=chat_id, text=texto, reply_markup=teclado)

# Mostrar página de álbum con imágenes
def mostrar_album(query, context, pagina, usuario_id):
    cartas_usuario = list(col_cartas_usuario.find({"user_id": usuario_id}))
    if not cartas_usuario:
        query.answer(text="Álbum vacío.", show_alert=True)
        return
    cartas_usuario.sort(key=lambda x: x.get('count', 0), reverse=True)
    por_pagina = 6
    total = len(cartas_usuario)
    paginas = (total - 1) // por_pagina + 1
    if pagina < 1: pagina = 1
    if pagina > paginas: pagina = paginas
    inicio = (pagina - 1) * por_pagina
    fin = min(inicio + por_pagina, total)
    # Enviar media group (imágenes)
    media = []
    for carta in cartas_usuario[inicio:fin]:
        nombre = carta['nombre']; version = carta['version']
        cid = carta['card_id']; cnt = carta['count']
        url = imagen_de_carta(nombre, version)
        caption = f"#{cid} {version} {nombre} (x{cnt})"
        if url:
            media.append(InputMediaPhoto(url, caption=caption))
    if media:
        context.bot.send_media_group(chat_id=query.message.chat_id, media=media)
    # Botones de navegación
    botones = []
    nav = []
    if pagina > 1:
        nav.append(InlineKeyboardButton("« Anterior", callback_data=f"album_{pagina-1}_{usuario_id}"))
    if pagina < paginas:
        nav.append(InlineKeyboardButton("Siguiente »", callback_data=f"album_{pagina+1}_{usuario_id}"))
    if nav:
        botones.append(nav)
    botones.append([InlineKeyboardButton("Modo Lista", callback_data=f"lista_1_{usuario_id}")])
    teclado = InlineKeyboardMarkup(botones)
    context.bot.send_message(chat_id=query.message.chat_id, text=f"Álbum Página {pagina}/{paginas}", reply_markup=teclado)

# Manejador de CallbackQuery (botones)
def manejador_callback(update, context):
    query = update.callback_query
    query.answer()  # quitar signo de carga
    data = query.data
    # Reclamar carta
    if data.startswith("reclamar"):
        manejador_reclamar(update, context)
        return
    partes = data.split("_")
    if len(partes) != 3:
        return
    modo, pagina, uid = partes
    pagina = int(pagina); usuario_id = int(uid)
    # Verificar usuario
    if query.from_user.id != usuario_id:
        query.answer(text="Este álbum no es tuyo.", show_alert=True)
        return
    if modo == 'lista':
        # Lista de cartas paginada (texto)
        cartas_usuario = list(col_cartas_usuario.find({"user_id": usuario_id}))
        cartas_usuario.sort(key=lambda x: x.get('count', 0), reverse=True)
        total = len(cartas_usuario)
        por_pagina = 10
        paginas = (total - 1)//por_pagina + 1 if total>0 else 1
        if pagina < 1: pagina = 1
        if pagina > paginas: pagina = paginas
        inicio = (pagina - 1)*por_pagina
        fin = min(inicio+por_pagina, total)
        texto = ""
        for carta in cartas_usuario[inicio:fin]:
            cid = carta['card_id']; version = carta['version']
            nombre = carta['nombre']; cnt = carta['count']
            texto += f"#{cid} {version} {nombre} Cant: {cnt}\n"
        texto += f"\nPágina {pagina}/{paginas}"
        botones = []
        nav = []
        if pagina > 1:
            nav.append(InlineKeyboardButton("« Anterior", callback_data=f"lista_{pagina-1}_{usuario_id}"))
        if pagina < paginas:
            nav.append(InlineKeyboardButton("Siguiente »", callback_data=f"lista_{pagina+1}_{usuario_id}"))
        if nav:
            botones.append(nav)
        botones.append([InlineKeyboardButton("Modo Álbum", callback_data=f"album_1_{usuario_id}")])
        teclado = InlineKeyboardMarkup(botones)
        try:
            query.edit_message_text(texto, reply_markup=teclado)
        except:
            context.bot.send_message(chat_id=query.message.chat_id, text=texto, reply_markup=teclado)
    elif modo == 'album':
        # Mostrar modo álbum con imágenes
        # Borrar mensaje de lista anterior (si existe)
        try:
            context.bot.delete_message(chat_id=query.message.chat_id, message_id=query.message.message_id)
        except:
            pass
        mostrar_album(query, context, pagina, usuario_id)

# Registrar comandos y callbacks
dispatcher.add_handler(CommandHandler('idolday', comando_idolday))
dispatcher.add_handler(CommandHandler('album', comando_album))
dispatcher.add_handler(CallbackQueryHandler(manejador_callback))

# Ruta para webhook de Telegram
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

if __name__ == '__main__':
    puerto = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=puerto)
