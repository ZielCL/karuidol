import os
import json
import random
import datetime
import requests
from flask import Flask, request
from pymongo import MongoClient, ReturnDocument
from bson.objectid import ObjectId

app = Flask(__name__)

# Configuración desde variables de entorno
TOKEN = os.environ.get("TELEGRAM_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")
MONGO_DB = os.environ.get("MONGO_DB")
BOT_USERNAME = os.environ.get("BOT_USERNAME")  # opcional

if not TOKEN or not MONGO_URI or not MONGO_DB:
    raise RuntimeError("Faltan variables de entorno requeridas.")

# Conexión MongoDB
client = MongoClient(MONGO_URI)
db = client[MONGO_DB]
daily_claims = db.daily_claims
card_appearances = db.card_appearances
card_drops = db.card_drops

# Cargar cartas desde JSON
with open("cartas.json", "r", encoding="utf-8") as f:
    cartas = json.load(f)

cartas_v1 = [c for c in cartas if c.get("version") == "V1"]
cartas_v2 = [c for c in cartas if c.get("version") == "V2"]

@app.route("/", methods=["GET"])
def home():
    return "Bot activo."

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    update = request.get_json()
    print("✅ Webhook recibido:")
    print(json.dumps(update, indent=2), flush=True)

    if "callback_query" in update:
        query = update["callback_query"]
        user_id = query["from"]["id"]
        query_id = query["id"]
        data = query.get("data", "")

        if data.startswith("claim_"):
            drop_id = data.split("_", 1)[1]
            drop = card_drops.find_one({"_id": ObjectId(drop_id)})

            if drop and not drop.get("claimed"):
                card_drops.update_one(
                    {"_id": ObjectId(drop_id)},
                    {"$set": {"claimed": True, "claimed_by": user_id}}
                )
                requests.post(f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery", data={
                    "callback_query_id": query_id,
                    "text": "¡Carta reclamada con éxito! 🎉"
                })
            else:
                requests.post(f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery", data={
                    "callback_query_id": query_id,
                    "text": "Lo siento, esta carta ya fue reclamada."
                })
        return "ok", 200

    if "message" in update:
        msg = update["message"]
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        chat_type = chat.get("type")
        user = msg.get("from", {})
        user_id = user.get("id")
        text = msg.get("text", "")

        # Solo en grupos
        if chat_type != "group" and chat_type != "supergroup":
            return "ok", 200

        if text:
            cmd = text.split()[0].lower()
            if cmd == "/idolday" or (BOT_USERNAME and cmd == f"/idolday@{BOT_USERNAME.lower()}"):
                today = datetime.datetime.utcnow().date().isoformat()

                if daily_claims.find_one({"user_id": user_id, "group_id": chat_id, "date": today}):
                    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data={
                        "chat_id": chat_id,
                        "text": "Ya has reclamado tu carta diaria hoy."
                    })
                    return "ok", 200

                carta = random.choice(cartas_v2 if random.random() < 0.1 else cartas_v1)
                nombre = carta.get("nombre", "Desconocida")
                version = carta.get("version", "")
                rareza = carta.get("rareza", "")

                record = card_appearances.find_one_and_update(
                    {"group_id": chat_id, "card_name": nombre, "version": version},
                    {"$inc": {"count": 1}},
                    upsert=True,
                    return_document=ReturnDocument.AFTER
                )
                appearance_id = record.get("count", 1)

                caption = f"{nombre} [{appearance_id} {version}]"
                if rareza:
                    caption += f"\nRareza: {rareza}"

                drop = card_drops.insert_one({
                    "group_id": chat_id,
                    "card_name": nombre,
                    "version": version,
                    "appearance_id": appearance_id,
                    "claimed": False
                })
                drop_id = str(drop.inserted_id)

                keyboard = {
                    "inline_keyboard": [[
                        {"text": "🎁 Reclamar", "callback_data": f"claim_{drop_id}"}
                    ]]
                }

                requests.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto", data={
                    "chat_id": chat_id,
                    "photo": carta.get("imagen_url"),
                    "caption": caption,
                    "reply_markup": json.dumps(keyboard)
                })

                daily_claims.insert_one({
                    "group_id": chat_id,
                    "user_id": user_id,
                    "date": today
                })

    return "ok", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
