import requests
import json

import os
from dotenv import load_dotenv

try:
    load_dotenv()
    TOKEN=os.getenv("HA_TOKEN")
    api_url=os.getenv("HA_URL")
except:
    TOKEN = ""
    api_url = ""


headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}
def tv_on():
    url = api_url+"/api/services/media_player/turn_on"

    data = {
        "entity_id": "media_player.smart_tv"
    }

    r = requests.post(url, headers=headers, json=data)

    print(r.status_code)
    print(r.text)
def tv_off():
    url = api_url+"/api/services/media_player/turn_off"

    data = {
        "entity_id": "media_player.smart_tv"
    }

    r = requests.post(url, headers=headers, json=data)

    print(r.status_code)
    print(r.text)
def volume_down():
    url = api_url+"/api/services/media_player/volume_down"

    data = {
        "entity_id": "media_player.smart_tv",
    }

    r = requests.post(url, headers=headers, json=data)

    print(r.status_code)
    print(r.text)
def volume_up():
    url = api_url+"/api/services/media_player/volume_up"

    data = {
        "entity_id": "media_player.smart_tv",
    }

    r = requests.post(url, headers=headers, json=data)

    print(r.status_code)
    print(r.text)
    
def play_youtube(media_id):
    url = api_url+"/api/services/media_player/play_media"

    data = {
        "entity_id": "media_player.google_cast",
        "media_content_type": "cast",
        "media_content_id": json.dumps({
            "app_name": "youtube",
            "media_id": media_id
        })
    }

    r = requests.post(url, headers=headers, json=data)

    print(r.status_code)
    print(r.text)
