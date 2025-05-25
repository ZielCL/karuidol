import os
import json
import random
import datetime
from flask import Flask, request
import telebot

TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("No se encontró TELEGRAM_TOKEN en las variables de entorno.")
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")
app = Flask(__name__)

# --------- Datos de cartas ---------
CARTAS_FILE = "cartas.json"
USUARIOS_FILE = "usuarios.json"

def cargar_cartas():
    if not os.path.exists(CARTAS_FILE):
        # Esqueleto base de cartas
        cartas = [
            {"id": 1, "nombre": "Tzuyu", "version": "V1", "rareza": "Común", "imagen": "https://i.imgur.com/eac1d9a8.png"},
            {"id": 1, "nombre": "Tzuyu", "version": "V2", "rareza": "Rara", "imagen": "https://i.imgur.com/eac1d9a8.png"},
            {"id": 2, "nombre": "Lisa", "version": "V1", "rareza": "Común", "imagen": "https://i.imgur.com/eac1d9a8.png"},
            {"id": 2, "nombre": "Lisa", "version": "V2", "rareza": "Rara", "imagen": "https://i.imgur.com/eac1d9a8.png"},
            # Agrega más cartas personalizadas aquí
        ]
        with open(CARTAS_FILE, "w") as f:
            json.dump(cartas, f, indent=2)
    else:
        with open(CARTAS_FILE, "r") as f:
            cartas = json.load(f)
    return cartas

def cargar_usuarios():
    if not os.path.exists(USUARIOS_FILE):
        return {}
    with open(USUARIOS_FILE, "r") as f:
        return json.load(f)

def guardar_usuarios(usuarios):
    with open(USUARIOS_FILE, "w") as f:
        json.dump(usuarios, f, indent=2)

CARTAS = cargar_cartas()

# --------- Utilidades ---------
def obtener_carta_aleatoria():
    # 90% común, 10% rara (V2)
    prob = random.random()
    if prob < 0.9:
        cartas = [c for c in CARTAS if c["version"] == "V1"]
    else:
        cartas = [c for c in CARTAS if c["version"] == "V2"]
    return random.choice(cartas)

def nombre_formato_carta(carta):
    # Personaliza el formato bonito aquí
    return f"<b>{carta['nombre']} [#{carta['id']} {carta['version']}]</b>"

def puede_reclamar(user_id, group_id, usuarios):
    hoy = datetime.date.today().isoformat()
    key = f"{group_id}_{user_id}"
    u = usuarios.get(key, {})
    last_day = u.get("last_idolday", "")
    bonos = u.get("bonos", 0)
    if last_day == hoy and bonos == 0:
        return False
    return True

def registrar_reclamo(user_id, group_id, carta, usuarios):
    hoy = datetime.date.today().isoformat()
    key = f"{group_id}_{user_id}"
    u = usuarios.get(key, {"coleccion": {}, "bonos": 0})
    # Manejo de bonos
    if u.get("last_idolday", "") == hoy and u.get("bonos", 0) > 0:
        u["bonos"] -= 1
    else:
        u["last_idolday"] = hoy
    # Clave carta: id_version
    clave = f"{carta['id']}_{carta['version']}"
    if "coleccion" not in u:
        u["coleccion"] = {}
    if clave not in u["coleccion"]:
        u["coleccion"][clave] = {"nombre": carta["nombre"], "version": carta["version"], "id": carta["id"], "cantidad": 0, "imagen": carta["imagen"]}
    u["coleccion"][clave]["cantidad"] += 1
    usuarios[key] = u
    guardar_usuarios(usuarios)
    return u

# --------- Handlers ---------
@bot.message_handler(commands=["start"])
def start_handler(message):
    bot.reply_to(message, "🤖 <b>¡Estoy operativo!</b>\nUsa <code>/idolday</code> para reclamar tu carta diaria, <code>/album</code> para ver tu colección.", parse_mode="HTML")

@bot.message_handler(commands=["idolday"])
def idolday_handler(message):
    # Solo en grupos
    if message.chat.type not in ["group", "supergroup"]:
        bot.reply_to(message, "❌ Este comando solo funciona en grupos.")
        return
    user_id = str(message.from_user.id)
    group_id = str(message.chat.id)
    usuarios = cargar_usuarios()
    if not puede_reclamar(user_id, group_id, usuarios):
        bot.reply_to(message, "⏳ Ya reclamaste tu carta hoy. Usa /bonoidolday si tienes bonos disponibles.")
        return
    carta = obtener_carta_aleatoria()
    u = registrar_reclamo(user_id, group_id, carta, usuarios)
    texto = (
        f"<b>Carta obtenida:</b>\n"
        f"{nombre_formato_carta(carta)}\n"
        f"<i>Haz clic en el nombre para ver la imagen.</i>\n"
        f"<code>Rareza: {carta['rareza']}</code>"
    )
    bot.send_photo(
        message.chat.id,
        carta["imagen"],
        caption=texto,
        parse_mode="HTML"
    )

@bot.message_handler(commands=["album"])
def album_handler(message):
    # Solo en grupos
    if message.chat.type not in ["group", "supergroup"]:
        bot.reply_to(message, "❌ Este comando solo funciona en grupos.")
        return
    user_id = str(message.from_user.id)
    group_id = str(message.chat.id)
    usuarios = cargar_usuarios()
    key = f"{group_id}_{user_id}"
    u = usuarios.get(key, {})
    coleccion = u.get("coleccion", {})
    if not coleccion:
        bot.reply_to(message, "📁 No tienes cartas aún. ¡Reclama una con /idolday!")
        return
    # Ordenar por cantidad, descendente
    lista = sorted(coleccion.values(), key=lambda c: c["cantidad"], reverse=True)
    texto = "🗂 <b>Tu colección:</b>\n\n"
    for carta in lista[:10]:
        texto += f"#{carta['id']} {carta['version']} {carta['nombre']:<12}   <b>Cant:</b> <code>{carta['cantidad']}</code>\n"
    if len(lista) > 10:
        texto += f"\n<i>Solo se muestran 10 cartas. (¡Tienes más!)</i>"
    bot.reply_to(message, texto, parse_mode="HTML")
    # Opcional: despliegue de imagen al hacer clic (con inline), se puede añadir más adelante

@bot.message_handler(commands=["bonoidolday"])
def bono_handler(message):
    # Solo admins de grupo pueden usarlo
    if message.chat.type not in ["group", "supergroup"]:
        bot.reply_to(message, "❌ Solo funciona en grupos.")
        return
    # Verifica admin con get_chat_member (solo funciona si el bot es admin)
    try:
        admin = bot.get_chat_member(message.chat.id, message.from_user.id)
        if admin.status not in ["administrator", "creator"]:
            bot.reply_to(message, "❌ Solo los administradores pueden usar este comando.")
            return
    except Exception:
        bot.reply_to(message, "❌ No pude comprobar permisos de admin.")
        return
    # Extraer cantidad
    partes = message.text.split()
    if len(partes) < 2 or not partes[1].isdigit():
        bot.reply_to(message, "Uso: <code>/bonoidolday cantidad</code>\nEjemplo: <code>/bonoidolday 2</code>", parse_mode="HTML")
        return
    cantidad = int(partes[1])
    if cantidad <= 0:
        bot.reply_to(message, "La cantidad debe ser mayor a 0.")
        return
    user_id = str(message.from_user.id)
    group_id = str(message.chat.id)
    usuarios = cargar_usuarios()
    key = f"{group_id}_{user_id}"
    u = usuarios.get(key, {"coleccion": {}, "bonos": 0})
    u["bonos"] = u.get("bonos", 0) + cantidad
    usuarios[key] = u
    guardar_usuarios(usuarios)
    bot.reply_to(message, f"🎁 Bono de <b>{cantidad}</b> uso(s) extra de <code>/idolday</code> añadido.", parse_mode="HTML")

# --------- Webhook Flask ---------
@app.route("/", methods=["GET"])
def home():
    return "Bot activo."

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_str = request.get_data(as_text=True)
        print("✅ Webhook recibido:", json_str, flush=True)
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
        return "ok", 200
    return "método no permitido", 405

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
