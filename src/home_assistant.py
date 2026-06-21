import requests
import json

import os
from dotenv import load_dotenv

try:
    load_dotenv()
    TOKEN=os.getenv("HA_TOKEN")
    api_url='http://'+os.getenv("HA_URL")
except:
    TOKEN = ""
    api_url = ""


headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json"
    }


def get_entities():
    response = requests.get(api_url+'/api/states', headers=headers)
    entities = response.json()

    entity_list=[]
    for entity in entities:
        entity_id = entity["entity_id"]

        if entity_id.startswith(("switch.", "light.", "fan.", "lock.", "media_player.")):
            entity_list.append(entity_id)
    return entity_list

def execute_action(domain, service, data):

    response = requests.post(api_url+"/api/services/"+domain+'/'+service, headers=headers, json=data)
    print(response.status_code)
    print(response.text)
    return "Status: " + str(response.status_code) + "\nResponse: " + response.text