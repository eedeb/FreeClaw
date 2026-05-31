import src.agent as agent
tts=False
groq_key=""
location=""


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
