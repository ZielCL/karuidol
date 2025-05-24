import os
from flask import Flask, request

app = Flask(__name__)

# Usa tu token real o configura TOKEN como variable de entorno en Render
TOKEN = os.environ.get("TELEGRAM_TOKEN")

if not TOKEN:
    raise RuntimeError("⚠️ No se encontró TELEGRAM_TOKEN en variables de entorno.")

@app.route("/", methods=["GET"])
def home():
    return "Bot activo."

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    update_json = request.get_data(as_text=True)
    print("✅ Webhook recibido:")
    print(update_json, flush=True)  # Se verá en los logs de Render
    return "ok", 200  # Responde inmediatamente para evitar timeout

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
