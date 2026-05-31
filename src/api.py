import src.agent as agent

import os
from dotenv import load_dotenv

load_dotenv()
groq_key=os.getenv("API_KEY")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

location=BASE_DIR+"/../models/data.pth"

tts=True

agent.reset(groq_key, location, tts=tts)


from fastapi import FastAPI
from fastapi import Body
import os

app = FastAPI()

@app.post("/chat")
async def chat(data: dict):
    qstn=data["message"]
    if qstn == "/reset":
        agent.reset(groq_key, location, tts=tts)

        return {
            "response": "Agent state has been reset."
        }

    elif qstn == "/shutdown":
        os._exit(0)

    else:
        response = agent.agent(user_input=qstn)

        return {
            "response": response
        }
