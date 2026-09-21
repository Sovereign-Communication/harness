from helper import slugify
import json


def save_slug(record):
    return slugify(record["name"]) + ".json"


def encode(record):
    return json.dumps(record, sort_keys=True)
