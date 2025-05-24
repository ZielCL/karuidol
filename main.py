import os
import json
import random
from datetime import datetime
import requests
from flask import Flask, request
from pymongo import MongoClient, ReturnDocument

# Configuración inicial: variables de entorno y constantes
TOKEN = os.environ.get('TELEGRAM_TOKEN')
MONGO_URI = os.environ.get('MONGO_URI')
MONGO_DB = os.environ.get('MONGO_DB')
if not TOKEN or not MONGO_URI or not MONGO_DB:
    raise RuntimeError("Faltan variables de entorno TELEGRAM_TOKEN, MONGO_URI o MONGO_DB")

BOT_URL = f"https://api.telegram.org/bot{TOKEN}/"

# Conexión a la base de datos MongoDB Atlas
client = MongoClient(MONGO_URI)
db = client[MONGO_DB]
# Crear índices para las colecciones
db.user_cards.create_index([("group_id", 1), ("nombre", 1), ("version", 1), ("appearance_id", 1)], unique=True)
db.appearance_counts.create_index([("group_id", 1), ("nombre", 1), ("version", 1)], unique=True)
db.daily_usage.create_index("user_id", unique=True)

# Cargar la lista de cartas desde cartas.json
cards = []
try:
    with open('cartas.json', 'r', encoding='utf-8') as f:
        cards = json.load(f)
except Exception as e:
    print(f"Error al cargar cartas.json: {e}")

# Inicializar la aplicación Flask
app = Flask(__name__)

# Ruta raíz (opcional, para verificar que el servidor funciona)
@app.route('/', methods=['GET'])
def home():
    return "Bot is running!"

# Ruta del webhook de Telegram (usando el token como parte de la URL por seguridad)
@app.route(f'/{TOKEN}', methods=['POST'])
def telegram_webhook():
    update = request.get_json()
    if not update:
        return "ok"
    # Manejo de mensajes entrantes
    if "message" in update:
        message = update["message"]
        chat = message.get("chat", {})
        chat_type = chat.get("type")
        # Ignorar mensajes que no sean de grupos o supergrupos
        if chat_type not in ["group", "supergroup"]:
            return "ok"
        text = message.get("text", "")
        if not text:
            return "ok"
        # Verificar si es el comando /idolday (con o sin @ del bot)
        command = text.split()[0]
        if command == "/idolday" or command.startswith("/idolday@"):
            user_id = message.get("from", {}).get("id")
            if not user_id:
                return "ok"
            # Verificar uso diario del comando por usuario
            today_str = datetime.utcnow().strftime("%Y-%m-%d")
            usage = db.daily_usage.find_one({"user_id": user_id})
            if usage and usage.get("date") == today_str:
                # Ya usó el comando hoy, avisar al usuario
                name = message["from"].get("username") or message["from"].get("first_name", "Usuario")
                warning_text = f"{name}, ya has usado /idolday hoy."
                requests.post(BOT_URL + "sendMessage", json={
                    "chat_id": chat.get("id"),
                    "reply_to_message_id": message.get("message_id"),
                    "text": warning_text
                })
            else:
                # Seleccionar una carta aleatoria con probabilidad 90% V1, 10% V2
                if random.random() < 0.9:
                    filtered_cards = [c for c in cards if c.get("version") == "V1"]
                else:
                    filtered_cards = [c for c in cards if c.get("version") == "V2"]
                if not filtered_cards:
                    filtered_cards = cards
                card = random.choice(filtered_cards)
                group_id = chat.get("id")
                # Incrementar el contador de apariciones de esta carta en este grupo
                result = db.appearance_counts.find_one_and_update(
                    {"group_id": group_id, "nombre": card.get("nombre"), "version": card.get("version")},
                    {"$inc": {"count": 1}},
                    upsert=True,
                    return_document=ReturnDocument.AFTER
                )
                appearance_id = result["count"]
                # Enviar la imagen de la carta con caption y botón de reclamo
                caption = f"{card.get('nombre')} [#{appearance_id} {card.get('version')}]"
                callback_data = f"{card.get('nombre')}|{card.get('version')}|{appearance_id}"
                reply_markup = {
                    "inline_keyboard": [[{"text": "🎁 Reclamar", "callback_data": callback_data}]]
                }
                resp = requests.post(BOT_URL + "sendPhoto", json={
                    "chat_id": group_id,
                    "photo": card.get("imagen_url"),
                    "caption": caption,
                    "reply_markup": reply_markup
                })
                if resp.status_code == 200:
                    # Marcar que el usuario ya usó su /idolday hoy
                    db.daily_usage.update_one(
                        {"user_id": user_id},
                        {"$set": {"date": today_str}},
                        upsert=True
                    )
    # Manejo de Callback Query (botón "Reclamar")
    if "callback_query" in update:
        callback = update["callback_query"]
        callback_id = callback.get("id")
        from_user = callback.get("from", {})
        query_msg = callback.get("message", {})
        data = callback.get("data", "")
        if not data or not query_msg:
            if callback_id:
                requests.post(BOT_URL + "answerCallbackQuery", json={"callback_query_id": callback_id})
            return "ok"
        # Formato esperado del data: "Nombre|Versión|ID"
        parts = data.split("|")
        if len(parts) != 3:
            if callback_id:
                requests.post(BOT_URL + "answerCallbackQuery", json={"callback_query_id": callback_id})
            return "ok"
        card_name, card_version, appearance_str = parts
        try:
            appearance_id = int(appearance_str)
        except ValueError:
            appearance_id = appearance_str
        chat_id = query_msg.get("chat", {}).get("id")
        message_id = query_msg.get("message_id")
        user_id = from_user.get("id")
        if not chat_id or not message_id or not user_id:
            if callback_id:
                requests.post(BOT_URL + "answerCallbackQuery", json={"callback_query_id": callback_id})
            return "ok"
        # Verificar si la carta ya fue reclamada
        existing = db.user_cards.find_one({
            "group_id": chat_id,
            "nombre": card_name,
            "version": card_version,
            "appearance_id": appearance_id
        })
        if existing:
            if callback_id:
                requests.post(BOT_URL + "answerCallbackQuery", json={
                    "callback_query_id": callback_id,
                    "text": "Esta carta ya fue reclamada.",
                    "show_alert": False
                })
            return "ok"
        # Registrar el reclamo en la base de datos
        db.user_cards.insert_one({
            "user_id": user_id,
            "group_id": chat_id,
            "nombre": card_name,
            "version": card_version,
            "appearance_id": appearance_id,
            "claimed_at": datetime.utcnow()
        })
        # Editar el mensaje de la carta para mostrar quién la reclamó
        claimer_name = from_user.get("username") or from_user.get("first_name", "")
        claimer_display = f"@{from_user['username']}" if from_user.get("username") else claimer_name
        new_caption = f"{card_name} [#{appearance_id} {card_version}]\nReclamada por {claimer_display}"
        requests.post(BOT_URL + "editMessageCaption", json={
            "chat_id": chat_id,
            "message_id": message_id,
            "caption": new_caption,
            "reply_markup": {"inline_keyboard": []}
        })
        # Responder la callback query para confirmar al usuario
        if callback_id:
            requests.post(BOT_URL + "answerCallbackQuery", json={
                "callback_query_id": callback_id,
                "text": "¡Carta reclamada!",
                "show_alert": False
            })
    return "ok"

if __name__ == "__main__":
    # Ejecutar la aplicación Flask
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
