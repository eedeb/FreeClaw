import requests
import os
from dotenv import load_dotenv

try:
    load_dotenv()
    TOKEN=os.getenv("HA_TOKEN")
    api_url=os.getenv("HA_URL")
except:
    TOKEN = ""
    api_url = ""


def send_to_alexa(text):
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    }


    data = {
        "entity_id": "media_player.elliot_s_echo_dot",
        "media_content_type": "custom",
        "media_content_id": text
    }

    response = requests.post(api_url, headers=headers, json=data)

    print(response.status_code)
    print(response.text)
