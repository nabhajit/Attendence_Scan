"""Loads the nine sample documents from sample_data/ (see DATA_MODEL.md) into your database.
Usage (from repository root):  python sample_data/sample_seed.py

Equivalent with the MongoDB tools (from repository root):
  mongoimport --uri "$MONGO_URI" --db "$MONGO_DB" -c employees       --jsonArray --drop --file sample_data/employees.json
  mongoimport --uri "$MONGO_URI" --db "$MONGO_DB" -c attendance_logs --jsonArray --drop --file sample_data/attendance_logs.json

The samples show document SHAPES only. The grader uses a much larger hidden dataset with cases they do not show.
Passing on this sample does not mean your analytics are correct - re-read the business rules."""
import os
import pathlib

from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
here = pathlib.Path(__file__).parent
opts = json_util.JSONOptions(tz_aware=True)
db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)[os.getenv("MONGO_DB", "attendance_db")]

for name in ("employees", "attendance_logs"):
    docs = json_util.loads((here / f"{name}.json").read_text(), json_options=opts)
    db[name].delete_many({})
    db[name].insert_many(docs)
    print(f"loaded {len(docs):>2} documents into '{db.name}.{name}'")
