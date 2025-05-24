import os, json, random
from flask import Flask, request
from telegram import Bot, Update
from telegram.ext import Dispatcher, CommandHandler
from pymongo import MongoClient
from dotenv import load_dotenv

# Carga .env si existe (para desarrollo local)
load_dotenv()

# Inicializa Telegram y Flask
TOKEN = os.getenv("TELEGRAM_TOKEN")
bot = Bot(token=TOKEN)
app = Flask(__name__)
dp = Dispatcher(bot, None, workers=0, use_context=True)

# Configura MongoDB Atlas
MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client.get_default_database()
cards_col = db.cards
inv_col   = db.collection

# Pobla cartas en DB si no hay
if cards_col.count_documents({}) == 0:
    with open("cartas.json") as f:
        cards_col.insert_many(json.load(f))

# Handlers
def start(update, context):
    update.message.reply_text("👋 ¡Bienvenido! Usa /reclamar para una carta.")

def reclamar(update, context):
    uid = str(update.effective_user.id)
    carta = cards_col.aggregate([{"$sample": {"size": 1}}]).next()
    inv_col.update_one({"_id": uid}, {"$push": {"cards": carta["id"]}}, upsert=True)
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
        return update.message.reply_text("❌ Aún no tienes cartas.")
    from collections import Counter
    cnt = Counter(doc["cards"])
    lines = [f"- {cid}: {cnt[cid]}×" for cid in cnt]
    update.message.reply_text("📋 Tu colección:\n" + "\n".join(lines))

dp.add_handler(CommandHandler("start", start))
dp.add_handler(CommandHandler("reclamar", reclamar))
dp.add_handler(CommandHandler("coleccion", coleccion))

# Webhook endpoint
@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    dp.process_update(update)
    return "OK"

# Health check
@app.route("/", methods=["GET"])
def health():
    return "OK", 200
