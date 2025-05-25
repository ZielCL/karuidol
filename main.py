import os
import json
import random
import datetime
from flask import Flask, request
from pymongo import MongoClient
import requests

# Cargar variables de entorno
TOKEN = os.environ.get("TELEGRAM_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")
ADMIN_IDS = os.environ.get("ADMIN_IDS", "")
BASE_URL = os.environ.get("BASE_URL", "")  # URL pública del bot en Render

if not TOKEN or not MONGO_URI:
    raise RuntimeError("Faltan variables de entorno requeridas.")

ADMIN_IDS = [int(i) for i in ADMIN_IDS.split(",") if i.strip().isdigit()]

# Conexión a MongoDB
mongo = MongoClient(MONGO_URI)
db = mongo["karuidol"]
usuarios = db["usuarios"]

# Cargar cartas
CARTAS_FILE = "cartas.json"
if not os.path.exists(CARTAS_FILE):
    with open(CARTAS_FILE, "w") as f:
        json.dump([], f)
with open(CARTAS_FILE, "r") as f:
    CARTAS = json.load(f)

# App Flask
app = Flask(__name__)

def send_telegram(chat_id, text, reply_markup=None, photo=None, parse_mode="HTML"):
    url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto" if photo else f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {"chat_id": chat_id, "parse_mode": parse_mode}
    if photo:
        data["photo"] = photo
        data["caption"] = text
    else:
        data["text"] = text
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    resp = requests.post(url, data=data)
    return resp

def es_grupo(update):
    chat = update.get("message", {}).get("chat", {}) or update.get("callback_query", {}).get("message", {}).get("chat", {})
    return chat.get("type") in ["group", "supergroup", "channel", "supergroup"]

def usuario_admin(user_id):
    return user_id in ADMIN_IDS

def obtener_id_carta(nombre, version):
    # Cuenta cuántas veces existe esa carta en la colección general
    todas = list(usuarios.aggregate([
        {"$unwind": "$coleccion"},
        {"$match": {"coleccion.nombre": nombre, "coleccion.version": version}}
    ]))
    return len(todas) + 1

def elegir_carta():
    # Porcentajes de rareza
    total_prob = sum(c['rareza'] for c in CARTAS)
    dado = random.randint(1, total_prob)
    acumulado = 0
    for carta in CARTAS:
        acumulado += carta['rareza']
        if dado <= acumulado:
            return carta
    return CARTAS[0]

def now_date():
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")

@app.route("/", methods=["GET"])
def home():
    return "Bot activo."

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    update = request.get_json(force=True)
    print("✅ Webhook recibido:")
    print(json.dumps(update, indent=2, ensure_ascii=False), flush=True)

    message = update.get("message")
    callback_query = update.get("callback_query")

    if message:
        text = message.get("text", "")
        chat = message["chat"]
        user = message["from"]
        chat_id = chat["id"]
        user_id = user["id"]
        nombre = user.get("first_name", "")

        if text.startswith("/start"):
            if es_grupo(update):
                send_telegram(chat_id, f"✨ <b>¡Bot KaruIdol operativo en el grupo!</b>")
            else:
                send_telegram(chat_id, f"🌟 ¡Hola <b>{nombre}</b>! Usa los comandos solo en grupos.\nComandos disponibles: /idolday /album")
        
        elif text.startswith("/idolday"):
            if not es_grupo(update):
                send_telegram(chat_id, "🚫 Solo puedes usar este comando en grupos.")
                return "ok", 200

            user_db = usuarios.find_one({"_id": user_id, "grupo_id": chat_id})
            hoy = now_date()
            # Bonos
            bono = (user_db or {}).get("bono", 0)
            ultimo = (user_db or {}).get("ultimo_dia", "")
            if user_db and ultimo == hoy and bono < 1:
                send_telegram(chat_id, f"⏰ Ya reclamaste tu carta de hoy, espera a mañana.")
                return "ok", 200
            # Actualiza bonos y fecha
            if bono > 0:
                usuarios.update_one({"_id": user_id, "grupo_id": chat_id}, {"$inc": {"bono": -1}, "$set": {"ultimo_dia": hoy}})
            else:
                usuarios.update_one({"_id": user_id, "grupo_id": chat_id}, {"$set": {"ultimo_dia": hoy}}, upsert=True)

            carta = elegir_carta()
            num_id = obtener_id_carta(carta["nombre"], carta["version"])
            carta_str = f"<b>{carta['nombre']} [#{num_id} {carta['version']}]</b>"

            # Actualiza colección
            user_db = usuarios.find_one({"_id": user_id, "grupo_id": chat_id})
            encontrado = False
            for c in (user_db or {}).get("coleccion", []):
                if c["nombre"] == carta["nombre"] and c["version"] == carta["version"]:
                    c["cantidad"] += 1
                    encontrado = True
                    break
            if not encontrado:
                if user_db:
                    coleccion = user_db.get("coleccion", [])
                    coleccion.append({"nombre": carta["nombre"], "version": carta["version"], "cantidad": 1, "url": carta["url"]})
                    usuarios.update_one({"_id": user_id, "grupo_id": chat_id}, {"$set": {"coleccion": coleccion}})
                else:
                    usuarios.insert_one({
                        "_id": user_id,
                        "grupo_id": chat_id,
                        "nombre": nombre,
                        "coleccion": [{"nombre": carta["nombre"], "version": carta["version"], "cantidad": 1, "url": carta["url"]}],
                        "ultimo_dia": hoy
                    })
            else:
                usuarios.update_one(
                    {"_id": user_id, "grupo_id": chat_id, "coleccion.nombre": carta["nombre"], "coleccion.version": carta["version"]},
                    {"$inc": {"coleccion.$.cantidad": 1}}
                )

            # Mensaje bonito
            texto = f"""
<b>✨ ¡Has reclamado tu carta de idol diaria!</b>

┌───────────────
│ <b>{carta['nombre']} [#{num_id} {carta['version']}]</b>
└───────────────
""".strip()
            send_telegram(chat_id, texto, photo=carta["url"])
        
        elif text.startswith("/album"):
            if not es_grupo(update):
                send_telegram(chat_id, "🚫 Solo puedes ver tu álbum en grupos.")
                return "ok", 200
            user_db = usuarios.find_one({"_id": user_id, "grupo_id": chat_id})
            if not user_db or not user_db.get("coleccion"):
                send_telegram(chat_id, "📭 No tienes cartas en tu colección aún. Usa /idolday para conseguir una.")
                return "ok", 200
            coleccion = sorted(user_db["coleccion"], key=lambda x: (x["nombre"].lower(), x["version"]))
            # Modo lista: 10 por página
            lista = ""
            for idx, c in enumerate(coleccion, 1):
                lista += f"<b>{idx}.</b> <b>{c['nombre']}</b> [{c['version']}]  <code>Cant: {c['cantidad']}</code>\n"
            if not lista:
                lista = "Sin cartas."
            # Botón para cambiar modo (futuro álbum)
            reply_markup = {
                "inline_keyboard": [
                    [{"text": "🖼 Ver Carta", "callback_data": f"viewcard_1"}]
                ]
            }
            send_telegram(chat_id, f"🗂 <b>Tu colección:</b>\n\n{lista}", reply_markup=reply_markup)
        
        elif text.startswith("/bonoidolday"):
            if not es_grupo(update):
                send_telegram(chat_id, "Solo desde el grupo.")
                return "ok", 200
            if user_id not in ADMIN_IDS:
                send_telegram(chat_id, "⛔ Solo los administradores pueden usar este comando.")
                return "ok", 200
            try:
                cantidad = int(text.split(" ", 1)[1].strip())
            except Exception:
                cantidad = 1
            usuarios.update_one({"_id": user_id, "grupo_id": chat_id}, {"$inc": {"bono": cantidad}}, upsert=True)
            send_telegram(chat_id, f"✅ Bono de {cantidad} idol(s) diario(s) entregado a {user.get('first_name', 'Admin')}.")

    elif callback_query:
        data = callback_query["data"]
        user = callback_query["from"]
        chat = callback_query["message"]["chat"]
        chat_id = chat["id"]
        user_id = user["id"]
        # Mostrar carta detallada desde álbum (sólo si existe la carta)
        if data.startswith("viewcard_"):
            idx = int(data.replace("viewcard_", ""))
            user_db = usuarios.find_one({"_id": user_id, "grupo_id": chat_id})
            if user_db and len(user_db.get("coleccion", [])) >= idx:
                carta = user_db["coleccion"][idx-1]
                texto = f"""
<b>{carta['nombre']} [{carta['version']}]</b>
Cantidad: <b>{carta['cantidad']}</b>
"""
                send_telegram(chat_id, texto, photo=carta["url"])
    return "ok", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
