from pymongo import MongoClient
from config import MONGO_URI

client = None
db = None

def init_db():
    global client, db
    client = MongoClient(MONGO_URI)
    db = client['karuta_bot']

def get_col(name):
    global db
    return db[name]
