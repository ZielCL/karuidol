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
from datetime import datetime, timedelta
from pymongo import MongoClient
from dotenv import load_dotenv
import re

load_dotenv()
TOKEN = os.getenv('TELEGRAM_TOKEN')
MONGO_URI = os.getenv('MONGO_URI')

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

# --- Sistema DROP de 3 cartas ---
# Mantener info del drop activo
drops_pendientes = {}  # chat_id: drop_info

def comando_idolday(update, context):
    usuario_id = update.message.from_user.id
    chat_id = update.effective_chat.id
    ahora = datetime.utcnow()

    if update.effective_chat.type not in ["group", "supergroup"]:
        context.bot.send_message(chat_id=chat_id, text="Este comando solo se puede usar en grupos.")
        return

    # Controlar uso diario
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

    # Seleccionar 3 cartas distintas
    cartas_v1 = [c for c in cartas if c.get('version') == 'V1']
    cartas_v2 = [c for c in cartas if c.get('version') == 'V2']
    pool = cartas_v1 + cartas_v2
    if len(pool) < 3:
        context.bot.send_message(chat_id=chat_id, text="No hay suficientes cartas para el drop.")
        return
    cartas_drop = random.sample(pool, 3)

    ids = []
    media = []
    for carta in cartas_drop:
        nombre = carta['nombre']
        version = carta['version']
        grupo = carta.get('grupo', '')
        imagen_url = carta.get('imagen')
        # Obtener id de carta único (contador)
        doc_cont = col_contadores.find_one({"nombre": nombre, "version": version})
        if doc_cont:
            nuevo_id = doc_cont['contador'] + 1
            col_contadores.update_one({"nombre": nombre, "version": version}, {"$inc": {"contador": 1}})
        else:
            nuevo_id = 1
            col_contadores.insert_one({"nombre": nombre, "version": version, "contador": 1})
        ids.append(nuevo_id)
        caption = f"#{nuevo_id} [{version}] {nombre} - {grupo}"
        media.append(InputMediaPhoto(media=imagen_url, caption=caption))

    # Guardar info de las cartas del drop (estado: disponible, id del que la reclama, timestamps)
    drop_info = {
        'usuario_id': usuario_id,
        'cartas': [
            {"nombre": c['nombre'], "version": c['version'], "id": ids[idx], "grupo": c['grupo'],
             "imagen": c['imagen'], "reclamada": False, "por": None, "momento": None}
            for idx, c in enumerate(cartas_drop)
        ],
        'timestamp': ahora,
        'reclamos': [],  # [(user_id, idx)]
        'primer_reclamo_usuario': None,
        'puede_reclamar_segunda': False, # habilita bono tras 30s
        'mensaje_id': None,  # se llenará abajo
        'esperando_bono': False, # para bloquear segundo pick en ventana de 30s
    }

    # Enviar las 3 cartas como álbum (media_group)
    msgs = context.bot.send_media_group(chat_id=chat_id, media=media)
    mensaje_id_base = msgs[0].message_id
    drop_info['mensaje_id'] = mensaje_id_base

    teclado = [
        [
            InlineKeyboardButton("1", callback_data=f"drop_{chat_id}_0"),
            InlineKeyboardButton("2", callback_data=f"drop_{chat_id}_1"),
            InlineKeyboardButton("3", callback_data=f"drop_{chat_id}_2")
        ]
    ]
    botones_msg = context.bot.send_message(chat_id=chat_id, text="Elige una carta. Solo quien usó /idolday puede reclamar en los primeros 10 segundos.",
                            reply_markup=InlineKeyboardMarkup(teclado))
    drop_info['botones_msg_id'] = botones_msg.message_id

    drops_pendientes[chat_id] = drop_info

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

    # Validar drop
    drop = drops_pendientes.get(chat_id)
    if not drop:
        query.answer("Drop no encontrado o expirado.", show_alert=True)
        return

    carta = drop['cartas'][carta_idx]
    ahora = datetime.utcnow()
    segundos_pasados = (ahora - drop['timestamp']).total_seconds()

    # ¿Ya está reclamada?
    if carta['reclamada']:
        query.answer("Esta carta ya fue reclamada.", show_alert=True)
        return

    # Lógica: 10s solo dueño (un pick), luego cualquiera
    if segundos_pasados < 10:
        if usuario_id != drop['usuario_id']:
            faltan = int(10 - segundos_pasados)
            query.answer(f"Aún no puedes reclamar esta carta, espera {faltan}s.", show_alert=True)
            return
        # Si es el dueño, solo puede reclamar 1 vez en los primeros 10s
        if drop['primer_reclamo_usuario']:
            query.answer("Ya reclamaste una carta en este drop. Si tienes bono, espera 30 segundos para reclamar otra.", show_alert=True)
            return
    elif segundos_pasados >= 10:
        # Ahora puede cualquiera... pero si es el dueño y va por el segundo pick con bono, validar el delay de 30s
        if usuario_id == drop['usuario_id']:
            bono_actual = col_usuarios.find_one({"user_id": usuario_id}).get('bono', 0)
            # ¿El dueño ya reclamó una carta en los primeros 10s y quiere una segunda?
            if drop['primer_reclamo_usuario'] and not drop['puede_reclamar_segunda']:
                tiempo_espera = (drop['primer_reclamo_usuario'] + timedelta(seconds=30)) - ahora
                if tiempo_espera.total_seconds() > 0:
                    query.answer(f"Debes esperar {int(tiempo_espera.total_seconds())}s para reclamar otra carta.", show_alert=True)
                    return
                if bono_actual > 0:
                    drop['puede_reclamar_segunda'] = True
                    col_usuarios.update_one({"user_id": usuario_id}, {"$inc": {"bono": -1}})
            elif drop['primer_reclamo_usuario'] and drop['puede_reclamar_segunda']:
                pass  # Ya esperó y tiene bono

    # Registrar reclamo
    carta['reclamada'] = True
    carta['por'] = usuario_id
    carta['momento'] = ahora
    drop['reclamos'].append((usuario_id, carta_idx))

    if usuario_id == drop['usuario_id'] and not drop['primer_reclamo_usuario'] and segundos_pasados < 10:
        drop['primer_reclamo_usuario'] = ahora
        drop['esperando_bono'] = True

    # Guardar en la base de datos del usuario
    nombre, version, cid, grupo = carta['nombre'], carta['version'], carta['id'], carta['grupo']
    existente = col_cartas_usuario.find_one({"user_id": usuario_id, "nombre": nombre, "version": version, "card_id": cid})
    if existente:
        col_cartas_usuario.update_one(
            {"user_id": usuario_id, "nombre": nombre, "version": version, "card_id": cid},
            {"$inc": {"count": 1}}
        )
    else:
        col_cartas_usuario.insert_one(
            {"user_id": usuario_id, "nombre": nombre, "version": version, "card_id": cid, "count": 1}
        )

    # Editar botones (deshabilitar)
    teclado = []
    for i, c in enumerate(drop['cartas']):
        if c['reclamada']:
            teclado.append(InlineKeyboardButton(f"❌", callback_data="nada"))
        else:
            teclado.append(InlineKeyboardButton(str(i+1), callback_data=f"drop_{chat_id}_{i}"))
    context.bot.edit_message_reply_markup(
        chat_id=query.message.chat_id,
        message_id=drop['botones_msg_id'],
        reply_markup=InlineKeyboardMarkup([teclado])
    )

    # Notificar en el grupo
    info_carta = f"#{cid} [{version}] {nombre} - {grupo}"
    context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"@{query.from_user.username or query.from_user.first_name} tomó la carta {info_carta} !"
    )
    query.answer("¡Carta reclamada!", show_alert=False)

def manejador_callback(update, context):
    query = update.callback_query
    data = query.data
    if data.startswith("drop_"):
        manejar_drop_reclamo(update, context)
        return
    # Aquí van tus otros handlers antiguos...

# (aquí debes copiar el resto de tus comandos: /album, /giveidol, etc. No es necesario que los repita, no los modifiqué).

dispatcher.add_handler(CommandHandler('idolday', comando_idolday))
# ... demás handlers que ya tienes
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
