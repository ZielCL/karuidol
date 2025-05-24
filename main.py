import os
import json
import random
from flask import Flask, request
from telegram import Bot, Update
from telegram.ext import Dispatcher, CommandHandler
from pymongo import MongoClient
from dotenv import load_dotenv

# Carga variables de entorno desde .env (útil en desarrollo local)
load_dotenv()

# ─── Configuración de Telegram y Flask ────────────────────────────────────────────
TOKEN = os.getenv("TELEGRAM_TOKEN")
bot = Bot(token=TOKEN)
app = Flask(__name__)
dp = Dispatcher(bot, None, workers=0, use_context=True)

# ─── Configuración de MongoDB ─────────────────────────────────────────────────────
MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI)

# Selecciona la base de datos explícitamente
db_name = os.getenv("MONGO_DB")  # ej. "karuta"
if db_name:
    db = client[db_name]
else:
    db = client.get_default_database()

cards_col = db.cards       # colección de cartas
inv_col   = db.collection  # colección de inventarios de usuarios

# Pobla la colección de cartas si está vacía
if cards_col.count_documents({}) == 0:
    with open("cartas.json", "r") as f:
        cards = json.load(f)
    cards_col.insert_many(cards)

# ─── Handlers de comandos ─────────────────────────────────────────────────────────
def start(update, context):
    update.message.reply_text(
        "👋 ¡Bienvenido al bot coleccionista de cartas!\n"
        "Usa /reclamar para obtener una carta aleatoria."
    )

def reclamar(update, context):
    uid = str(update.effective_user.id)
    # Selecciona una carta al azar
    carta = cards_col.aggregate([{"$sample": {"size": 1}}]).next()
    # Guarda en el inventario del usuario
    inv_col.update_one(
        {"_id": uid},
        {"$push": {"cards": carta["id"]}},
        upsert=True
    )
    # Envía la imagen y la descripción
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
        return update.message.reply_text("❌ Aún no tienes cartas. Usa /reclamar.")
    from collections import Counter
    cnt = Counter(doc["cards"])
    lines = [f"- {cid}: {cnt[cid]}×" for cid in cnt]
    update.message.reply_text("📋 Tu colección:\n" + "\n".join(lines))

# Registra los handlers
dp.add_handler(CommandHandler("start", start))
dp.add_handler(CommandHandler("reclamar", reclamar))
dp.add_handler(CommandHandler("coleccion", coleccion))

# ─── Endpoint para Webhook ────────────────────────────────────────────────────────
@app.route("/_debug", methods=["GET"])
def debug():
    cwd = os.getcwd()
    files = os.listdir(cwd)
    env = {k: ("****" if k not in ["PYTHONPATH"] else v) for k, v in os.environ.items()}
    return {"cwd": cwd, "files": files, "env": env}

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    dp.process_update(update)
    return "OK"

# ─── Health Check ─────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return "OK", 200
