import os
import json
import random
import logging

from flask import Flask, request
from telegram import Bot, Update
from telegram.ext import Dispatcher, CommandHandler
from pymongo import MongoClient
from dotenv import load_dotenv

# ─── Configuración de logging ────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Carga de variables de entorno (para dev local) ─────────────────────────────
load_dotenv()

# ─── Configuración de Telegram y Flask ───────────────────────────────────────────
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    logger.error("TELEGRAM_TOKEN no está definido en el entorno")
bot = Bot(token=TOKEN)
app = Flask(__name__)
dp = Dispatcher(bot, None, workers=0, use_context=True)

# ─── Endpoint de diagnóstico ─────────────────────────────────────────────────────
@app.route("/_debug", methods=["GET"])
def debug():
    cwd = os.getcwd()
    files = os.listdir(cwd)
    env = {k: ("****" if k not in ["PYTHONPATH"] else v) for k, v in os.environ.items()}
    return {"cwd": cwd, "files": files, "env": env}

# ─── Configuración de MongoDB ─────────────────────────────────────────────────────
MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    logger.error("MONGO_URI no está definido en el entorno")
client = MongoClient(MONGO_URI)

# Selección explícita de base de datos
db_name = os.getenv("MONGO_DB")
if db_name:
    db = client[db_name]
else:
    db = client.get_default_database()

cards_col = db.cards       # colección de cartas
inv_col   = db.collection  # colección de inventarios

# Poblar cartas si la colección está vacía
if cards_col.count_documents({}) == 0:
    with open("cartas.json", "r") as f:
        cards = json.load(f)
    cards_col.insert_many(cards)
    logger.info(f"Insertadas {len(cards)} cartas en MongoDB")

# ─── Handlers de comandos ────────────────────────────────────────────────────────
def start(update, context):
    update.message.reply_text(
        "👋 ¡Bienvenido al bot coleccionista de cartas!\n"
        "Usa /reclamar para obtener una carta aleatoria."
    )

def reclamar(update, context):
    uid = str(update.effective_user.id)
    carta = cards_col.aggregate([{"$sample": {"size": 1}}]).next()
    inv_col.update_one(
        {"_id": uid},
        {"$push": {"cards": carta["id"]}},
        upsert=True
    )
    context.bot.send_photo(
        chat_id=update.effective_chat.id,
        photo=carta["imagen_url"],
        caption=f"🎴 *{carta['nombre']}* (_{carta['rareza']}_)",
        parse_mode="Markdown"
    )

def coleccion(update, context):
    uid = str(update.effective_user.id)
    doc = inv_col.find_one({"_id": uid}) or {"cards": []}
    if not doc["cards"]:
        update.message.reply_text("❌ Aún no tienes cartas. Usa /reclamar.")
        return
    from collections import Counter
    cnt = Counter(doc["cards"])
    lines = [f"- {cid}: {cnt[cid]}×" for cid in cnt]
    update.message.reply_text("📋 Tu colección:\n" + "\n".join(lines))

# Registrar handlers
dp.add_handler(CommandHandler("start", start))
dp.add_handler(CommandHandler("reclamar", reclamar))
dp.add_handler(CommandHandler("coleccion", coleccion))

# ─── Endpoint para Webhook ───────────────────────────────────────────────────────
@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    dp.process_update(update)
    return "OK"

# ─── Health Check ─────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return "OK", 200

# ─── Arranque local (solo para pruebas con python main.py) ────────────────────────
if __name__ == "__main__":
    # En local, podrías configurar webhook así:
    # webhook_url = f"https://localhost:port/{TOKEN}"
    # bot.set_webhook(webhook_url)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
