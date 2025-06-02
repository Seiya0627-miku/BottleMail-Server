from fastapi import FastAPI, Request
from typing import List
import random
import logging
import uuid

def generate_letter_id():
    # 方法1: UUID (ほぼ確実にユニーク)
    return str(uuid.uuid4())

app = FastAPI()
messages = []

# ログ設定（ファイルと標準出力両方）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("server.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

@app.post("/send")
async def send_message(request: Request):
    data = await request.json()
    msg = data["message"]
    title = data.get("title", "No Title")
    sender_id = data.get("userId", "unknown")
    client_ip = request.client.host

    messages.append((msg, sender_id))
    logging.info(f"📩 受信: from={sender_id}, ip={client_ip}, title='{title}', message='{msg}'")

    return {"status": "received"}

@app.get("/receive/{client_id}")
def receive_message(client_id: str):
    for msg, sender in messages:
        if sender != client_id:
            messages.remove((msg, sender))
            logging.info(f"📤 配信: to={client_id}, message='{msg}'")
            return {"message": msg}
    return {"message": None}

# もし暴力的な内容があったら除外してください