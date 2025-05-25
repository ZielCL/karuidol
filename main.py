import os
import json
import random
import datetime
from flask import Flask, request
import telebot
from telebot import types
from pymongo import MongoClient

# Configuración inicial (variables de entorno)
TOKEN = os.environ.get('BOT_TOKEN', 'YOUR_BOT_TOKEN')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', 'https://YOUR_URL/')
MONGO_URI = os.environ.get('MONGO_URI', 'mongodb://localhost:27017/')
bot = telebot.TeleBot(TOKEN, parse_mode='HTML')

# Conexión a MongoDB
client = MongoClient(MONGO_URI)
db = client['karuta_db']
usuarios_col = db['usuarios']
colecciones_col = db['colecciones']

# Carga de cartas desde JSON
with open('cartas.json', encoding='utf-8') as f:
    cartas_data = json.load(f)
cartas_v1 = []
cartas_v2 = []
url_por_carta = {}
for carta in cartas_data:
    nombre = carta['nombre']
    version = carta['version']
    url = carta['url']
    url_por_carta[(nombre, version)] = url
    if version.upper() == 'V1':
        cartas_v1.append(carta)
    else:
        cartas_v2.append(carta)

# Configurar webhook con Flask
bot.remove_webhook()
bot.set_webhook(url=WEBHOOK_URL + TOKEN)
app = Flask(__name__)

@app.route(f"/{TOKEN}", methods=['POST'])
def receive_update():
    data = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(data)
    bot.process_new_updates([update])
    return '', 200

@app.route("/")
def index():
    return "Bot Karuta activo", 200

# Manejadores de comandos
@bot.message_handler(commands=['start', 'help'], chat_types=['group','supergroup'])
def send_welcome(message):
    bot.reply_to(message, "<b>Bot Karuta activo.</b> Usa /album, /idolday o /bonoidolday según corresponda.")

@bot.message_handler(commands=['album'], chat_types=['group','supergroup'])
def album_handler(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    username = message.from_user.first_name
    cartas = list(colecciones_col.find({'chat_id': chat_id, 'user_id': user_id}))
    if not cartas:
        bot.reply_to(message, f"No tienes cartas en tu colección, {username}.")
        return
    cartas.sort(key=lambda x: x['nombre'])
    pagina = 1
    total = len(cartas)
    total_paginas = (total + 9) // 10

    def construir_lista(page):
        inicio = (page-1)*10
        fin = min(inicio+10, total)
        texto = f"<b>Álbum de {username} - Página {page}/{total_paginas}</b>\n"
        teclas = types.InlineKeyboardMarkup()
        for idx in range(inicio, fin):
            carta = cartas[idx]
            texto += f"{idx+1}. {carta['nombre']} {carta['version']} (x{carta['cantidad']})\n"
            btn = types.InlineKeyboardButton(
                f"Ver {carta['nombre']}", callback_data=f"show_{idx+1}"
            )
            teclas.add(btn)
        nav = []
        if page > 1:
            nav.append(types.InlineKeyboardButton("⬅️ Anterior", callback_data=f"page_{page-1}"))
        if page < total_paginas:
            nav.append(types.InlineKeyboardButton("Siguiente ➡️", callback_data=f"page_{page+1}"))
        if nav:
            teclas.add(*nav)
        return texto, teclas

    texto, teclado = construir_lista(pagina)
    bot.send_message(chat_id, texto, reply_markup=teclado)

@bot.callback_query_handler(func=lambda call: call.data.startswith("page_"))
def callback_album_page(call):
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    page = int(call.data.split('_')[1])
    cartas = list(colecciones_col.find({'chat_id': chat_id, 'user_id': user_id}))
    cartas.sort(key=lambda x: x['nombre'])
    total = len(cartas)
    total_paginas = (total + 9) // 10
    if page < 1 or page > total_paginas:
        bot.answer_callback_query(call.id, "Página inválida.")
        return
    inicio = (page-1)*10
    fin = min(inicio+10, total)
    texto = f"<b>Álbum - Página {page}/{total_paginas}</b>\n"
    teclas = types.InlineKeyboardMarkup()
    for idx in range(inicio, fin):
        carta = cartas[idx]
        texto += f"{idx+1}. {carta['nombre']} {carta['version']} (x{carta['cantidad']})\n"
        btn = types.InlineKeyboardButton(
            f"Ver {carta['nombre']}", callback_data=f"show_{idx+1}"
        )
        teclas.add(btn)
    nav = []
    if page > 1:
        nav.append(types.InlineKeyboardButton("⬅️ Anterior", callback_data=f"page_{page-1}"))
    if page < total_paginas:
        nav.append(types.InlineKeyboardButton("Siguiente ➡️", callback_data=f"page_{page+1}"))
    if nav:
        teclas.add(*nav)
    bot.edit_message_text(text=texto, chat_id=chat_id,
                          message_id=call.message.message_id, reply_markup=teclas)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("show_"))
def callback_show_card(call):
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    idx = int(call.data.split('_')[1]) - 1
    cartas = list(colecciones_col.find({'chat_id': chat_id, 'user_id': user_id}))
    cartas.sort(key=lambda x: x['nombre'])
    if idx < 0 or idx >= len(cartas):
        bot.answer_callback_query(call.id, "Carta inválida.")
        return
    carta = cartas[idx]
    nombre = carta['nombre']
    version = carta['version']
    cantidad = carta['cantidad']
    url_imagen = url_por_carta.get((nombre, version))
    if url_imagen:
        caption = f"<b>{nombre} {version}</b>\nCantidad total: {cantidad}"
        bot.send_photo(chat_id, url_imagen, caption=caption)
    bot.answer_callback_query(call.id)

@bot.message_handler(commands=['idolday'], chat_types=['group','supergroup'])
def idolday_handler(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    hoy_str = datetime.date.today().isoformat()
    usuario = usuarios_col.find_one({'chat_id': chat_id, 'user_id': user_id})
    if usuario and usuario.get('last_idolday') == hoy_str:
        if usuario.get('bonos', 0) > 0:
            usuarios_col.update_one({'chat_id': chat_id, 'user_id': user_id}, {'$inc': {'bonos': -1}})
        else:
            bot.reply_to(message, "Ya has usado /idolday hoy. ¡Inténtalo mañana!")
            return
    # Elegir carta aleatoria
    if random.random() < 0.1 and cartas_v2:
        carta = random.choice(cartas_v2)
    else:
        carta = random.choice(cartas_v1 if cartas_v1 else cartas_v2)
    nombre = carta['nombre']
    version = carta['version']
    url_imagen = carta['url']
    # Actualizar colección
    colecciones_col.update_one(
        {'chat_id': chat_id, 'user_id': user_id, 'nombre': nombre, 'version': version},
        {'$inc': {'cantidad': 1}}, upsert=True
    )
    registro = colecciones_col.find_one({'chat_id': chat_id, 'user_id': user_id,
                                         'nombre': nombre, 'version': version})
    cantidad = registro['cantidad']
    usuarios_col.update_one({'chat_id': chat_id, 'user_id': user_id},
                            {'$set': {'last_idolday': hoy_str}}, upsert=True)
    caption = f"<b>{nombre} {version}</b>\nCantidad total: {cantidad}"
    bot.send_photo(chat_id, url_imagen, caption=caption)

@bot.message_handler(commands=['bonoidolday'], chat_types=['group','supergroup'])
def bonoidolday_handler(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    miembro = bot.get_chat_member(chat_id, user_id)
    if miembro.status not in ('administrator', 'creator'):
        bot.reply_to(message, "Solo un administrador puede otorgar bonos.")
        return
    partes = message.text.split()
    if len(partes) != 2 or not partes[1].isdigit():
        bot.reply_to(message, "Uso: /bonoidolday <cantidad>")
        return
    cantidad = int(partes[1])
    if cantidad <= 0:
        bot.reply_to(message, "La cantidad debe ser un número positivo.")
        return
    usuarios_col.update_one({'chat_id': chat_id, 'user_id': user_id},
                             {'$inc': {'bonos': cantidad}}, upsert=True)
    bot.reply_to(message, f"Se han otorgado {cantidad} bono(s) a <b>{message.from_user.first_name}</b>.",
                 parse_mode='HTML')
