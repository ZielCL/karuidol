import os
import json
import random
import datetime
import requests
from flask import Flask, request
from pymongo import MongoClient, ReturnDocument
from bson.objectid import ObjectId

# Leer variables de entorno
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
BOT_USERNAME = os.environ.get('BOT_USERNAME')  # Opcional
MONGO_URI = os.environ.get('MONGO_URI')
MONGO_DB = os.environ.get('MONGO_DB')

# Configurar cliente de MongoDB
client = MongoClient(MONGO_URI)
db = client[MONGO_DB]
daily_claims = db.daily_claims
card_appearances = db.card_appearances
card_drops = db.card_drops

# Cargar cartas desde archivo JSON
with open('cartas.json', 'r', encoding='utf-8') as f:
    cartas = json.load(f)
# Separar cartas por versión para selección
cartas_v1 = [c for c in cartas if c.get('version') == 'V1']
cartas_v2 = [c for c in cartas if c.get('version') == 'V2']

app = Flask(__name__)

@app.route('/', methods=['POST'])
def webhook():
    update = request.get_json()
    if not update:
        return 'ok', 200

    # Manejar callback queries de botones inline (reclamo de carta)
    if 'callback_query' in update:
        query = update['callback_query']
        query_id = query['id']
        user_id = query['from']['id']
        data = query.get('data', '')
        if data.startswith('claim_'):
            drop_id = data.split('_', 1)[1]
            try:
                card_drop = card_drops.find_one({'_id': ObjectId(drop_id)})
            except Exception:
                card_drop = None
            if card_drop and not card_drop.get('claimed'):
                # Asignar la carta al primer usuario que cliquea
                card_drops.update_one({'_id': ObjectId(drop_id)},
                                      {'$set': {'claimed': True, 'claimed_by': user_id}})
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
                              data={'callback_query_id': query_id,
                                    'text': '¡Carta reclamada con éxito! 🎉'})
            else:
                # Ya reclamada o no encontrada
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
                              data={'callback_query_id': query_id,
                                    'text': 'Lo siento, esta carta ya fue reclamada.'})
        return 'ok', 200

    # Manejar comandos de texto (/idolday)
    if 'message' in update:
        msg = update['message']
        chat = msg.get('chat', {})
        chat_id = chat.get('id')
        chat_type = chat.get('type')
        user = msg.get('from', {})
        user_id = user.get('id')
        text = msg.get('text', '')

        # Ignorar en chats privados
        if chat_type == 'private':
            return 'ok', 200

        if text:
            parts = text.split()
            cmd = parts[0].lower()
            # Comando /idolday (posible con @BOT_USERNAME)
            if cmd == '/idolday' or (BOT_USERNAME and cmd == f'/idolday@{BOT_USERNAME.lower()}'):
                today = datetime.datetime.utcnow().date().isoformat()
                # Verificar reclamo diario
                if daily_claims.find_one({'user_id': user_id, 'group_id': chat_id, 'date': today}):
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                  data={'chat_id': chat_id,
                                        'text': 'Ya has reclamado tu carta diaria hoy.'})
                    return 'ok', 200

                # Seleccionar carta aleatoria (90% V1, 10% V2)
                if random.random() < 0.1 and cartas_v2:
                    carta = random.choice(cartas_v2)
                else:
                    carta = random.choice(cartas_v1) if cartas_v1 else None

                if carta:
                    nombre = carta.get('nombre', 'Desconocida')
                    version = carta.get('version', '')
                    rareza = carta.get('rareza', '')
                    # Incrementar contador de apariciones
                    record = card_appearances.find_one_and_update(
                        {'group_id': chat_id, 'card_name': nombre, 'version': version},
                        {'$inc': {'count': 1}},
                        upsert=True,
                        return_document=ReturnDocument.AFTER
                    )
                    appearance_id = record.get('count', 1) if record else 1

                    # Preparar caption de la carta
                    caption = f"{nombre} ({version})\n"
                    if rareza:
                        caption += f"Rareza: {rareza}\n"
                    caption += f"ID de aparición: {appearance_id}"

                    # Registrar el drop (pendiente de reclamo)
                    result = card_drops.insert_one({
                        'group_id': chat_id,
                        'card_name': nombre,
                        'version': version,
                        'appearance_id': appearance_id,
                        'claimed': False
                    })
                    drop_id = str(result.inserted_id)

                    # Botón inline para reclamar
                    keyboard = {'inline_keyboard': [[{
                        'text': '🎁 Reclamar', 'callback_data': f'claim_{drop_id}'
                    }]]}
                    # Enviar foto con el botón
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                                  data={'chat_id': chat_id,
                                        'photo': carta.get('imagen_url'),
                                        'caption': caption,
                                        'reply_markup': json.dumps(keyboard)})

                    # Marcar reclamo diario del usuario
                    daily_claims.insert_one({
                        'group_id': chat_id,
                        'user_id': user_id,
                        'date': today
                    })
                return 'ok', 200

    return 'ok', 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
