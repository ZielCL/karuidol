import os
import json
import random
from datetime import datetime
from flask import Flask, request
import requests
from pymongo import MongoClient, ReturnDocument

# Configuración de entorno
TOKEN = os.environ.get('TELEGRAM_TOKEN')
MONGO_URI = os.environ.get('MONGO_URI')
MONGO_DB = os.environ.get('MONGO_DB')

if not TOKEN or not MONGO_URI or not MONGO_DB:
    raise RuntimeError("Faltan TELEGRAM_TOKEN, MONGO_URI o MONGO_DB")

BOT_URL = f"https://api.telegram.org/bot{TOKEN}/"

# Base de datos
client = MongoClient(MONGO_URI)
db = client[MONGO_DB]
db.user_cards.create_index([("group_id", 1), ("nombre", 1), ("version", 1), ("appearance_id", 1)], unique=True)
db.appearance_counts.create_index([("group_id", 1), ("nombre", 1), ("version", 1)], unique=True)
db.daily_usage.create_index("user_id", unique=True)

# Cargar cartas.json
try:
    with open("cartas.json", "r", encoding="utf-8") as f:
        cards = json.load(f)
except Exception as e:
    print("❌ Error al cargar cartas.json:", e)
    cards = []

# Flask app
app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "Bot activo."

@app.route(f"/{TOKEN}", methods=["POST"])
def telegram_webhook():
    try:
        update = request.get_json()

        # Confirmamos que llegó bien la petición
        print("✅ Update recibido:", update)

        # --- MENSAJE ---
        if "message" in update:
            message = update["message"]
            chat = message.get("chat", {})
            chat_type = chat.get("type")
            if chat_type not in ["group", "supergroup"]:
                return "ok", 200

            text = message.get("text", "")
            command = text.split()[0]
            if command not in ["/idolday", f"/idolday@{os.environ.get('BOT_USERNAME', '')}"]:
                return "ok", 200

            user_id = message["from"]["id"]
            today = datetime.utcnow().strftime("%Y-%m-%d")
            usage = db.daily_usage.find_one({"user_id": user_id})
            if usage and usage.get("date") == today:
                name = message["from"].get("first_name", "Usuario")
                requests.post(BOT_URL + "sendMessage", json={
                    "chat_id": chat["id"],
                    "reply_to_message_id": message["message_id"],
                    "text": f"{name}, ya usaste /idolday hoy."
                })
                return "ok", 200

            # Elegir carta aleatoria por probabilidad
            if random.random() < 0.9:
                posibles = [c for c in cards if c.get("version") == "V1"]
            else:
                posibles = [c for c in cards if c.get("version") == "V2"]
            carta = random.choice(posibles if posibles else cards)

            group_id = chat["id"]
            resultado = db.appearance_counts.find_one_and_update(
                {"group_id": group_id, "nombre": carta["nombre"], "version": carta["version"]},
                {"$inc": {"count": 1}},
                upsert=True,
                return_document=ReturnDocument.AFTER
            )
            appearance_id = resultado["count"]
            caption = f'{carta["nombre"]} [#{appearance_id} {carta["version"]}]'
            callback_data = f'{carta["nombre"]}|{carta["version"]}|{appearance_id}'

            reply_markup = {
                "inline_keyboard": [[{"text": "🎁 Reclamar", "callback_data": callback_data}]]
            }

            requests.post(BOT_URL + "sendPhoto", json={
                "chat_id": group_id,
                "photo": carta["imagen_url"],
                "caption": caption,
                "reply_markup": reply_markup
            })

            db.daily_usage.update_one(
                {"user_id": user_id},
                {"$set": {"date": today}},
                upsert=True
            )

        # --- BOTÓN INLINE ---
        if "callback_query" in update:
            callback = update["callback_query"]
            from_user = callback["from"]
            message = callback["message"]
            data = callback["data"]
            chat_id = message["chat"]["id"]
            message_id = message["message_id"]
            callback_id = callback["id"]

            nombre, version, appearance_id = data.split("|")
            appearance_id = int(appearance_id)

            # Ya reclamada?
            ya = db.user_cards.find_one({
                "group_id": chat_id,
                "nombre": nombre,
                "version": version,
                "appearance_id": appearance_id
            })
            if ya:
                requests.post(BOT_URL + "answerCallbackQuery", json={
                    "callback_query_id": callback_id,
                    "text": "Esta carta ya fue reclamada."
                })
                return "ok", 200

            # Guardar reclamo
            db.user_cards.insert_one({
                "user_id": from_user["id"],
                "group_id": chat_id,
                "nombre": nombre,
                "version": version,
                "appearance_id": appearance_id,
                "claimed_at": datetime.utcnow()
            })

            quien = f'@{from_user["username"]}' if from_user.get("username") else from_user.get("first_name", "")
            nuevo_caption = f"{nombre} [#{appearance_id} {version}]\nReclamada por {quien}"

            # Editar mensaje original
            requests.post(BOT_URL + "editMessageCaption", json={
                "chat_id": chat_id,
                "message_id": message_id,
                "caption": nuevo_caption,
                "reply_markup": {"inline_keyboard": []}
            })

            # Responder callback
            requests.post(BOT_URL + "answerCallbackQuery", json={
                "callback_query_id": callback_id,
                "text": "¡Carta reclamada!"
            })

    except Exception as e:
        print("❌ Error procesando update:", e)

    return "ok", 200

# Flask run
if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)), host="0.0.0.0")
