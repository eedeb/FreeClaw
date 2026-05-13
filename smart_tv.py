import requests
import json


TOKEN = ""
url = ""


headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}
def tv_on():
    url = "https://home.eedeb.dev/api/services/media_player/turn_on"

    data = {
        "entity_id": "media_player.smart_tv"
    }

    r = requests.post(url, headers=headers, json=data)

    print(r.status_code)
    print(r.text)
def tv_off():
    url = "https://home.eedeb.dev/api/services/media_player/turn_off"

    data = {
        "entity_id": "media_player.smart_tv"
    }

    r = requests.post(url, headers=headers, json=data)

    print(r.status_code)
    print(r.text)
def volume_down():
    url = "https://home.eedeb.dev/api/services/media_player/volume_down"

    data = {
        "entity_id": "media_player.smart_tv",
    }

    r = requests.post(url, headers=headers, json=data)

    print(r.status_code)
    print(r.text)
def volume_up():
    url = "https://home.eedeb.dev/api/services/media_player/volume_up"

    data = {
        "entity_id": "media_player.smart_tv",
    }

    r = requests.post(url, headers=headers, json=data)

    print(r.status_code)
    print(r.text)
    
def play_youtube(media_id):
    url = "https://home.eedeb.dev/api/services/media_player/play_media"

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
