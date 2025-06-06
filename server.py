from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Any
import random
import logging
import json
import os
import time # For timestamps
import uuid # For unique letter IDs

app = FastAPI()
messages = []

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
LETTERS_FILE = os.path.join(DATA_DIR, "letters.json")
os.makedirs(DATA_DIR, exist_ok=True) # ディレクトリが存在しない場合は作成

# 手紙受け取りのクールダウン期間（秒）- テスト用に短く、本番では長くする
LETTER_RECEIVE_COOLDOWN_SECONDS = 60 # 例: 1分 (テスト用)
# LETTER_RECEIVE_COOLDOWN_SECONDS = 3600 * 3 # 例: 3時間

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

# 新規ユーザー登録時に last_letter_retrieved_at を初期化
def initialize_user_fields(user_id: str):
    users_data[user_id] = {"id": user_id} # idフィールドも追加しておく
    users_data[user_id].setdefault("preferences", {"emotion": "未設定", "custom": "未設定"})
    users_data[user_id].setdefault("unopenedLetterIds", [])
    users_data[user_id].setdefault("receivedLetterIds", []) # 名称変更を反映
    users_data[user_id].setdefault("sentLetterIds", [])
    users_data[user_id].setdefault("registered_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    users_data[user_id].setdefault("last_letter_retrieved_at", 0) # Cooldown用 (0は即時取得可能)

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
        initialize_user_fields(client_id)
        save_json_data(USERS_FILE, users_data)
        logging.info(f"✨ New user registered via check: {client_id}")
        return {"is_new_user": True, "user_id": client_id, "details": users_data[client_id]}


# --- User sending letters ---
@app.post("/send")
async def send_message(request: Request):
    data = await request.json()
    message_text = data.get("message")
    title = data.get("title", "No Title") # クライアントから来る想定
    sender_id = data.get("userId", "unknown_sender")
    
    if not message_text or sender_id == "unknown_sender":
        raise HTTPException(status_code=400, detail="Message content and valid userId are required")

    if sender_id not in users_data: # 基本的にはcheck_userで作成されるはず
        logging.warning(f"Sender {sender_id} not found in users_data during send. Forcing registration.")
        # 強制的にユーザー作成（check_userを先に呼ぶのが理想）
        initialize_user_fields(sender_id)


    letter_id = f"letter-{uuid.uuid4()}"
    current_time_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    new_letter = {
        "id": letter_id,
        "date_sent": current_time_iso,
        "date_received": 0,
        "sender_id": sender_id,
        "recipient_id": ["waiting"], # 初期状態
        "title": title,
        "content": message_text
    }
    letters_data[letter_id] = new_letter
    
    users_data[sender_id].setdefault("sentLetterIds", []).append(letter_id)

    # --- Recipient Selection and Delivery to Unopened ---
    possible_recipients = [uid for uid in users_data.keys() if uid != sender_id]
    recipient_id_selected = None
    if possible_recipients:
        recipient_id_selected = random.choice(possible_recipients)  # [*] ここを書き換える
        # 手紙の内容をフィルタリングする。ダメだったらrejectedにする
        new_letter["recipient_id"] = [recipient_id_selected] # 受信者を更新
        
        # 受信者のunopenedLetterIdsに追加
        recipient_user_data = users_data.get(recipient_id_selected)
        if recipient_user_data: # 念のため存在確認
            recipient_user_data.setdefault("unopenedLetterIds", []).append(letter_id)
            logging.info(f"Letter {letter_id} from {sender_id} added to unopened for {recipient_id_selected}")
        else: # 通常はありえない
            logging.error(f"Recipient {recipient_id_selected} not found in users_data for unopened list.")
            new_letter["recipient_id"] = ["error_recipient_not_found"]
    else:
        logging.warning(f"Letter {letter_id} from {sender_id} has no recipients. Status remains waiting.")
    # --- End Recipient Selection ---

    save_json_data(LETTERS_FILE, letters_data) # 手紙データを保存
    save_json_data(USERS_FILE, users_data)     # ユーザーデータを保存 (sentLetterIds, unopenedLetterIdsの更新)

    logging.info(f"📩 Message sent: from={sender_id}, title='{title}', letter_id='{letter_id}', routed_to='{recipient_id_selected or 'waiting'}'")
    return {"status": "received_and_saved", "letter_id": letter_id}

# --- User receiving letters ---
@app.get("/receive_unopened/{client_id}")
def get_unopened_letters(client_id: str):
    if client_id not in users_data:
        raise HTTPException(status_code=404, detail="User not found")

    if client_id not in users_data:
        initialize_user_fields(client_id)
    user = users_data[client_id]

    current_timestamp = time.time()
    last_retrieved_timestamp = user.get("last_letter_retrieved_at", 0)

    # クールダウン期間チェック
    if current_timestamp < last_retrieved_timestamp + LETTER_RECEIVE_COOLDOWN_SECONDS:
        remaining_cooldown = int((last_retrieved_timestamp + LETTER_RECEIVE_COOLDOWN_SECONDS) - current_timestamp)
        logging.info(f"User {client_id} is in cooldown. Remaining: {remaining_cooldown}s")
        return {"status": "cooldown", "message": None, "cooldown_remaining_seconds": remaining_cooldown}

    unopened_ids = user.get("unopenedLetterIds", [])
    if not unopened_ids:
        logging.info(f"No unopened letters for {client_id}.")
        return {"status": "no_new_letters", "message": None}

    # 1通だけ（一番古いもの）を返す
    letter_id_to_deliver = unopened_ids[0] 
    
    if letter_id_to_deliver in letters_data:
        letter_details = letters_data[letter_id_to_deliver]
        logging.info(f"📬 Offering letter {letter_id_to_deliver} to {client_id} (not removing from unopened yet)")
        # この時点では unopenedLetterIds からは削除しない
        return {"status": "new_letter_available", **letter_details} # 手紙詳細を返す
    else:
        # データ不整合の場合：unopenedLetterIds にはあるが letters_data にはない
        logging.warning(f"Stale Letter ID {letter_id_to_deliver} in {client_id}'s unopened list. Removing.")
        user["unopenedLetterIds"].pop(0) # 不正なIDを削除
        save_json_data(USERS_FILE, users_data)
        # 再帰的に次の未開封を探すか、今回は何もしないか。今回は何もしない。
        return {"status": "stale_letter_removed", "message": None}

# --- Mark letter as opened ---
@app.post("/mark_letter_opened/{client_id}/{letter_id}")
async def mark_letter_opened(client_id: str, letter_id: str):
    if client_id not in users_data:
        raise HTTPException(status_code=404, detail="User not found")
    
    if client_id not in users_data:
        initialize_user_fields(client_id)
    user = users_data[client_id]

    if letter_id in user.get("unopenedLetterIds", []):
        user["unopenedLetterIds"].remove(letter_id)
        user.setdefault("receivedLetterIds", []).append(letter_id) # 名称変更を反映
        user["last_letter_retrieved_at"] = time.time() # クールダウン開始時刻を記録

        # 手紙の受信日時を更新
        if letter_id in letters_data:
            current_time_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            letters_data[letter_id]["date_received"] = current_time_iso # またはクライアント側で使う日付フォーマット
            save_json_data(LETTERS_FILE, letters_data) # letters.json を更新
        else:
            logging.warning(f"Letter {letter_id} not found in letters_data when trying to mark date_received.")
            # この場合でもユーザーのリスト操作は続行する

        save_json_data(USERS_FILE, users_data)
        logging.info(f"Letter {letter_id} marked as opened by {client_id}, moved to receivedLetterIds. Cooldown started.")

        # クライアントがローカルに保存しやすいように更新された手紙情報（または少なくとも必要な情報）を返す
        if letter_id in letters_data:
            letter_to_return = letters_data[letter_id]
            # クライアントが期待するフォーマットに整形して返す
            client_formatted_letter = {
                "id": letter_to_return.get("id"),
                "title": letter_to_return.get("title"),
                "content": letter_to_return.get("content"),
                "date_received": letter_to_return.get("date_received"), # 今設定した日付
            }
            return {"status": "marked_opened_and_in_received", "letter": client_formatted_letter}
        else: # letters_dataにないのは異常系だがフォールバック
             return {"status": "marked_opened_but_letter_details_missing", "letter_id": letter_id}
    elif letter_id in user.get("receivedLetterIds", []):
        logging.info(f"Letter {letter_id} already in receivedLetterIds for {client_id}.")
        if letter_id in letters_data:
            letter_to_return = letters_data[letter_id]
            # クライアントが期待するフォーマットに整形して返す
            client_formatted_letter = {
                "id": letter_to_return.get("id"),
                "title": letter_to_return.get("title"),
                "content": letter_to_return.get("content"),
                "date_received": letter_to_return.get("date_received"), # 今設定した日付
            }
            return {"status": "already_in_received", "letter": client_formatted_letter}
        else: # letters_dataにないのは異常系だがフォールバック
             return {"status": "already_in_receivedd_but_letter_details_missing", "letter_id": letter_id}
    else:
        logging.warning(f"Letter {letter_id} not found in unopened for {client_id} to mark.")
        raise HTTPException(status_code=404, detail="Letter not found in user's unopened list")
    
# --- Get all letters for a user (for mail box) ---
@app.get("/letterbox/{client_id}")
def get_letterbox_contents(client_id: str):
    if client_id not in users_data:
        raise HTTPException(status_code=404, detail="User not found")

    if client_id not in users_data:
        initialize_user_fields(client_id)
    user = users_data[client_id]

    letterbox_ids = user.get("receivedLetterIds", [])
    
    letters_in_box_details = []
    for letter_id in letterbox_ids:
        if letter_id in letters_data:
            letter_detail = letters_data[letter_id]
            # ★ クライアントが期待するフォーマットに整形 ★
            formatted_letter = {
                "id": letter_detail.get("id"),
                "title": letter_detail.get("title"),
                "content": letter_detail.get("content"),
                "date_received": letter_detail.get("date_received", letter_detail.get("date_sent", "YYYY-MM-DD").split("T")[0]), # date_received優先、なければdate_sentから日付部分
            }
            letters_in_box_details.append(formatted_letter)
        else:
            logging.warning(f"Letter ID {letter_id} in {client_id}'s receivedLetterIds, but not found in letters_data.")

    # 新しいものが上にくるように、リストの順番をdate_receivedでソート (任意)
    letters_in_box_details.sort(key=lambda x: x.get("date_received", ""), reverse=True)

    logging.info(f"📬 Fetched {len(letters_in_box_details)} letters from letterbox for {client_id}")
    return letters_in_box_details

# リクエストボディの型を定義 (Pydanticモデル)
class PreferencesPayload(BaseModel):
    emotion: str # "感情" の文字列を期待
    custom: str # "受信好み設定" の文字列を期待

@app.post("/update_preferences/{client_id}")
async def update_preferences_endpoint(client_id: str, payload: PreferencesPayload):
    if client_id not in users_data:
        initialize_user_fields(client_id)
        if client_id not in users_data: # それでもなければエラー
             raise HTTPException(status_code=404, detail="User not found even after init attempt")
        
    user = users_data[client_id]
    user.setdefault("preferences", {"emotion": "未設定", "custom": "未設定"})
    
    # ユーザ設定を更新
    user["preferences"] = {
        "emotion": payload.emotion,
        "custom": payload.custom
    }

    save_json_data(USERS_FILE, users_data) # users.json に変更を保存
    logging.info(f"Preferences updated for user {client_id}. New custom preference: '{'preferences'}'")
    
    return {
        "status": "preferences_updated",
        "user_id": client_id,
        "updated_preferences": user["preferences"] # 更新後のpreferencesオブジェクトを返す
    }