import os
import json
import time
from flask import Flask, request
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime, timedelta
from pymongo import MongoClient

# ------------- CONFIGURACIÓN BÁSICA -------------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")
if not TOKEN:
    raise RuntimeError("No se encontró TELEGRAM_TOKEN en las variables de entorno.")
if not MONGO_URI:
    raise RuntimeError("No se encontró MONGO_URI en las variables de entorno.")

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")
app = Flask(__name__)

# Conexión a MongoDB
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["karuidol"]
coleccion_usuarios = db["usuarios"]

# --------- UTILIDADES ---------
def cargar_cartas():
    if not os.path.exists("cartas.json"):
        # Si no existe, crea un ejemplo básico para editar luego
        cartas = [
            {
                "id": 1,
                "nombre": "Tzuyu",
                "version": "V1",
                "rareza": "Común",
                "imagen": "https://i.imgur.com/example1.png"
            },
            {
                "id": 1,
                "nombre": "Tzuyu",
                "version": "V2",
                "rareza": "Rara",
                "imagen": "https://i.imgur.com/example2.png"
            }
        ]
        with open("cartas.json", "w", encoding="utf-8") as f:
            json.dump(cartas, f, ensure_ascii=False, indent=2)
    else:
        with open("cartas.json", "r", encoding="utf-8") as f:
            cartas = json.load(f)
    return cartas

CARTAS = cargar_cartas()

def elegir_carta_aleatoria():
    # 90% para V1, 10% para V2 (puedes expandirlo luego)
    import random
    v2_cartas = [c for c in CARTAS if c['version'] == "V2"]
    v1_cartas = [c for c in CARTAS if c['version'] == "V1"]
    if random.random() < 0.1 and v2_cartas:
        return random.choice(v2_cartas)
    else:
        return random.choice(v1_cartas)

def is_admin(chat_id, user_id):
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception as e:
        print("Error comprobando admin:", e)
        return False

def get_usuario(chat_id, user_id):
    user = coleccion_usuarios.find_one({"chat_id": chat_id, "user_id": user_id})
    if not user:
        user = {
            "chat_id": chat_id,
            "user_id": user_id,
            "cartas": [],
            "last_idolday": 0,
            "bonos_idolday": 0
        }
        coleccion_usuarios.insert_one(user)
    return user

def guardar_carta_usuario(chat_id, user_id, carta):
    user = get_usuario(chat_id, user_id)
    cartas = user["cartas"]
    for c in cartas:
        if c["id"] == carta["id"] and c["version"] == carta["version"]:
            c["cantidad"] += 1
            break
    else:
        nueva = {
            "id": carta["id"],
            "nombre": carta["nombre"],
            "version": carta["version"],
            "rareza": carta["rareza"],
            "cantidad": 1,
            "imagen": carta["imagen"]
        }
        cartas.append(nueva)
    coleccion_usuarios.update_one(
        {"chat_id": chat_id, "user_id": user_id},
        {"$set": {"cartas": cartas}}
    )

def set_last_idolday(chat_id, user_id, timestamp):
    coleccion_usuarios.update_one(
        {"chat_id": chat_id, "user_id": user_id},
        {"$set": {"last_idolday": timestamp}}
    )

def get_last_idolday(chat_id, user_id):
    user = get_usuario(chat_id, user_id)
    return user.get("last_idolday", 0)

def get_bonos(chat_id, user_id):
    user = get_usuario(chat_id, user_id)
    return user.get("bonos_idolday", 0)

def modificar_bonos(chat_id, user_id, cantidad):
    coleccion_usuarios.update_one(
        {"chat_id": chat_id, "user_id": user_id},
        {"$inc": {"bonos_idolday": cantidad}}
    )

# ----------- FLUJO DEL WEBHOOK/FLASK -----------

@app.route("/", methods=["GET"])
def home():
    return "Bot Karuidol activo."

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    if request.method == "POST":
        update = telebot.types.Update.de_json(request.stream.read().decode("utf-8"))
        bot.process_new_updates([update])
        print("✅ Webhook recibido:", update, flush=True)
        return "ok", 200
    return "Bot activo.", 200

# ----------- COMANDOS DEL BOT ------------

@bot.message_handler(commands=["start"])
def cmd_start(message):
    if message.chat.type not in ["group", "supergroup"]:
        bot.reply_to(message, "Este bot solo funciona en grupos.")
        return
    bot.reply_to(message, f"¡Bot <b>Karuidol</b> operativo en este grupo! Usa /idolday para obtener tu carta diaria.\n\nDesarrollado por ZielCL.")

@bot.message_handler(commands=["idolday"])
def cmd_idolday(message):
    if message.chat.type not in ["group", "supergroup"]:
        bot.reply_to(message, "Solo puedes usar este comando en grupos.")
        return
    user_id = message.from_user.id
    chat_id = message.chat.id

    ahora = int(time.time())
    user = get_usuario(chat_id, user_id)
    last_time = user.get("last_idolday", 0)
    bonos = user.get("bonos_idolday", 0)

    usado_bono = False
    # Permite usar un bono si aún no se puede reclamar normal
    if last_time != 0 and (ahora - last_time < 86400):
        if bonos > 0:
            modificar_bonos(chat_id, user_id, -1)
            usado_bono = True
        else:
            faltan = 86400 - (ahora - last_time)
            horas = faltan // 3600
            minutos = (faltan % 3600) // 60
            bot.reply_to(message, f"⏳ Ya reclamaste tu carta hoy. Próximo reclamo en {horas}h {minutos}m.")
            return

    carta = elegir_carta_aleatoria()
    guardar_carta_usuario(chat_id, user_id, carta)
    set_last_idolday(chat_id, user_id, ahora)
    texto = f"<b>{carta['nombre']} [V{carta['version'][-1]}]</b>\nID: <code>#{carta['id']}</code>\nRareza: <b>{carta['rareza']}</b>"
    if usado_bono:
        texto += "\n\n<i>Has usado un bono extra para reclamar esta carta.</i>"

    # Envía la carta con imagen y botón "Reclamar" (opcional: aquí solo muestra la carta, ya la reclama el usuario)
    bot.send_photo(
        message.chat.id,
        carta["imagen"],
        caption=texto,
        reply_to_message_id=message.message_id,
    )

@bot.message_handler(commands=["album"])
def cmd_album(message):
    if message.chat.type not in ["group", "supergroup"]:
        bot.reply_to(message, "Solo puedes usar este comando en grupos.")
        return
    user_id = message.from_user.id
    chat_id = message.chat.id
    user = get_usuario(chat_id, user_id)
    cartas = user.get("cartas", [])
    if not cartas:
        bot.reply_to(message, "No tienes cartas aún. Usa /idolday para conseguir una.")
        return
    cartas.sort(key=lambda c: (c["id"], c["version"]))
    texto = "<b>📒 Tu colección de cartas:</b>\n"
    for c in cartas:
        texto += f"• <b>#{c['id']} {c['version']} {c['nombre']}</b>   <b>x{c['cantidad']}</b>\n"
    bot.reply_to(message, texto)

# -------- ADMIN COMMAND --------

@bot.message_handler(commands=["bonoidolday"])
def cmd_bono(message):
    if message.chat.type not in ["group", "supergroup"]:
        bot.reply_to(message, "Solo puedes usar este comando en grupos.")
        return
    user_id = message.from_user.id
    chat_id = message.chat.id
    args = message.text.split()
    if not is_admin(chat_id, user_id):
        bot.reply_to(message, "Solo los administradores pueden usar este comando.")
        return
    if len(args) != 2 or not args[1].isdigit():
        bot.reply_to(message, "Uso: /bonoidolday <cantidad>")
        return
    cantidad = int(args[1])
    # Asigna bono al usuario que envió el comando (puedes cambiarlo para admins que den bono a otros)
    modificar_bonos(chat_id, user_id, cantidad)
    bot.reply_to(message, f"🎁 Has recibido <b>{cantidad}</b> bonos de idolday.")

# ----------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
