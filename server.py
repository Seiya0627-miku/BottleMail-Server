from fastapi import FastAPI, Request, HTTPException
from typing import List, Dict, Any
import random
import logging
import json
import os
import time # For timestamps

app = FastAPI()
messages = []

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
LETTERS_FILE = os.path.join(DATA_DIR, "letters.json")
os.makedirs(DATA_DIR, exist_ok=True) # ディレクトリが存在しない場合は作成

# ログ設定（ファイルと標準出力両方）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("server.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# --- Helper functions for JSON data ---
def load_json_data(filepath: str, default_data: Any = {}) -> Any:
    if not os.path.exists(filepath):
        # Create the file with default data if it doesn't exist
        save_json_data(filepath, default_data)
        return default_data
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        logging.error(f"Error reading or decoding JSON from {filepath}. Returning default.")
        # If file exists but is corrupted, potentially overwrite with default or handle error
        save_json_data(filepath, default_data) # Cautious: this might overwrite corrupted data
        return default_data

def save_json_data(filepath: str, data: Any):
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except IOError as e:
        logging.error(f"Error writing JSON data to {filepath}: {e}")


# --- Load data at startup ---
users_data: Dict[str, Dict[str, Any]] = load_json_data(USERS_FILE, {})
letters_data: Dict[str, Dict[str, Any]] = load_json_data(LETTERS_FILE, {})

# --- Check User & Register if New ---
@app.post("/check_user/{client_id}") # Using POST for potential future body if needed, GET is also fine
async def check_or_register_user(client_id: str):
    if client_id in users_data:
        logging.info(f"User checked: {client_id} (Existing)")
        return {"is_new_user": False, "user_id": client_id, "details": users_data[client_id]}
    else:
        # New user: create a default entry
        users_data[client_id] = {
            "preferences": {"emotion": "明るい", "custom": "政治の話はやだ"}, # Default preferences
            "receivedLetterIds": [],
            "sentLetterIds": [],
            "registered_at": time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime()) # UTC timestamp
        }
        save_json_data(USERS_FILE, users_data)
        logging.info(f"✨ New user registered via check: {client_id}")
        return {"is_new_user": True, "user_id": client_id, "details": users_data[client_id]}


# --- User sending letters ---
@app.post("/send")
async def send_message(request: Request):
    data = await request.json()
    message_text = data.get("message")
    title = data.get("title", "No Title") # From client
    sender_id = data.get("userId", "unknown_sender")
    client_ip = request.client.host

    if not message_text:
        raise HTTPException(status_code=400, detail="Message content is required")
    if sender_id == "unknown_sender" or not sender_id: # Ensure sender_id is valid
        raise HTTPException(status_code=400, detail="Valid userId is required")

    # Ensure user exists (should have been created by /check_user, but as a fallback)
    if sender_id not in users_data:
        users_data[sender_id] = {
            "preferences": {}, "receivedLetterIds": [], "sentLetterIds": [],
            "registered_at": time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())
        }
        logging.warning(f"User {sender_id} sent a message but was not in users.json. Added.")

    # Create letter_id (as discussed before)
    import uuid
    letter_id = f"letter-{uuid.uuid4()}" # Unique letter ID

    # Store the letter
    letters_data[letter_id] = {
        "id": letter_id, # Storing id within the object itself too
        "title": title,
        "sender_id": sender_id,
        "recipient_id": ["waiting"], # Initially "waiting"
        "date_sent": time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime()),
        "content": message_text
    }
    
    # Update sender's sentLetterIds
    users_data[sender_id].setdefault("sentLetterIds", []).append(letter_id)

    # --- Recipient Selection Logic (Placeholder for now) ---
    possible_recipients = [uid for uid in users_data.keys() if uid != sender_id]
    if possible_recipients:
        recipient_id = random.choice(possible_recipients)
        letters_data[letter_id]["recipient_id"] = [recipient_id] # Update recipient
        users_data[recipient_id].setdefault("receivedLetterIds", []).append(letter_id)
        logging.info(f"Letter {letter_id} from {sender_id} routed to {recipient_id}")
    else:
        logging.warning(f"Letter {letter_id} from {sender_id} has no recipients. Status: waiting.")
    # --- End Recipient Selection ---

    save_json_data(USERS_FILE, users_data)
    save_json_data(LETTERS_FILE, letters_data)

    logging.info(f"📩 Message received: from={sender_id}, ip={client_ip}, title='{title}', letter_id='{letter_id}'")
    return {"status": "received", "letter_id": letter_id}

# --- User receiving letters ---
@app.get("/receive/{client_id}")
def receive_message(client_id: str):
    if client_id not in users_data or not users_data[client_id].get("receivedLetterIds"):
        # If user doesn't exist or has no received letter IDs in their inbox list
        logging.info(f"No new messages for {client_id} (or user not fully registered with inbox).")
        return {"message": None, "title": None, "id": None, "sender_id": None, "date_sent": None}

    # Get the list of IDs of letters meant for this client
    user_received_ids = users_data[client_id]["receivedLetterIds"]
    
    if not user_received_ids:
        logging.info(f"Inbox empty for {client_id}.")
        return {"message": None, "title": None, "id": None, "sender_id": None, "date_sent": None}

    # Deliver the oldest unread letter (first ID in the list)
    letter_to_deliver_id = user_received_ids.pop(0) # Get and remove from their list
    
    if letter_to_deliver_id in letters_data:
        letter_content = letters_data[letter_to_deliver_id]
        save_json_data(USERS_FILE, users_data) # Update users.json because receivedLetterIds changed
        
        logging.info(f"📤 Delivering letter {letter_to_deliver_id} to {client_id}")
        return {
            "id": letter_to_deliver_id,
            "title": letter_content.get("title"),
            "message": letter_content.get("content"),
            "sender_id": letter_content.get("sender_id"),
            "date_sent": letter_content.get("date_sent")
        }
    else:
        logging.warning(f"Letter ID {letter_to_deliver_id} found in {client_id}'s inbox but not in letters_data. Removing stale ID.")
        save_json_data(USERS_FILE, users_data) # Save the modified inbox
        # Potentially try to deliver the next one, or just return no message for this poll
        return {"message": None, "title": None, "id": None, "sender_id": None, "date_sent": None}

# もし暴力的な内容があったら除外してください