import os
from pymongo import MongoClient
from dotenv import load_dotenv
from collections import defaultdict

# Carga variables de entorno si usas .env
load_dotenv()

# Ajusta estos nombres si en tu bot usan otros
MONGO_URI = os.getenv('MONGO_URI')
DB_NAME = "karuta_bot"

client = MongoClient(MONGO_URI)
db = client[DB_NAME]
col_contadores = db['contadores']
col_cartas_usuario = db['cartas_usuario']

# --- MIGRACIÓN ---

viejos = list(col_contadores.find({"grupo": {"$exists": False}}))

for doc in viejos:
    nombre = doc["nombre"]
    version = doc["version"]
    contador_viejo = doc["contador"]
    # Busca todas las cartas que correspondan
    cartas_reales = col_cartas_usuario.find({"nombre": nombre, "version": version})

    grupos_conteo = defaultdict(int)
    for carta in cartas_reales:
        grupo = carta.get("grupo", "")
        grupos_conteo[grupo] += 1

    # Si ninguna carta tiene grupo, asigna el total viejo al grupo vacío
    if not grupos_conteo:
        grupos_conteo[""] = contador_viejo

    # Crea/actualiza el contador para cada grupo
    for grupo, cantidad in grupos_conteo.items():
        col_contadores.update_one(
            {"nombre": nombre, "version": version, "grupo": grupo},
            {"$set": {"contador": cantidad}},
            upsert=True
        )
        print(f"Actualizado: {nombre} / {version} / {grupo} → {cantidad}")

    # Borra el documento viejo
    col_contadores.delete_one({"_id": doc["_id"]})
    print(f"Borrado viejo: {nombre} / {version}")

print("Migración completa.")
