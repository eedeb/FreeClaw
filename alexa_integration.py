import requests

TOKEN = ""
url = ""

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

    response = requests.post(url, headers=headers, json=data)

    print(response.status_code)
    print(response.text)
