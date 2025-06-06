from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Dict, Any, Tuple, Optional
import logging
import json
import os
import time 
import uuid 
import re
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# --- File Paths, Logging, Gemini Config (変更なし) ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
LETTERS_FILE = os.path.join(DATA_DIR, "letters.json")
os.makedirs(DATA_DIR, exist_ok=True)

LETTER_RECEIVE_COOLDOWN_SECONDS = 60 

logger = logging.getLogger("bottlemail_server_final")
logger.setLevel(logging.INFO)
log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

if not logger.handlers:
    file_log_handler = logging.FileHandler("server.log", encoding="utf-8")
    file_log_handler.setFormatter(log_formatter)
    logger.addHandler(file_log_handler)
    stream_log_handler = logging.StreamHandler()
    stream_log_handler.setFormatter(log_formatter)
    logger.addHandler(stream_log_handler)

GEMINI_API_KEY_CONFIGURED = False
gemini_model = None

try:
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    if not GEMINI_API_KEY:
        logger.error("環境変数 'GEMINI_API_KEY' が設定されていません。Geminiの機能は利用できません。")
    else:
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_model = genai.GenerativeModel('gemini-2.5-flash-preview-05-20')
        GEMINI_API_KEY_CONFIGURED = True
        logger.info("Gemini APIキーが正常に設定され、モデルが初期化されました。")
except Exception as e:
    logger.error(f"Gemini APIの設定中にエラーが発生しました: {e}")

# --- JSON Helper & User Initialization (変更なし) ---
def load_json_data(filepath: str, default_data: Any = {}) -> Any:
    if not os.path.exists(filepath):
        save_json_data(filepath, default_data)
        return default_data
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        logger.error(f"{filepath} の読み込みまたはデコード中にエラー: {e}. デフォルトデータを返します。")
        save_json_data(filepath, default_data)
        return default_data

def save_json_data(filepath: str, data: Any):
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except IOError as e:
        logger.error(f"{filepath} へのJSONデータ書き込みエラー: {e}")

users_data: Dict[str, Dict[str, Any]] = load_json_data(USERS_FILE, {})
letters_data: Dict[str, Dict[str, Any]] = load_json_data(LETTERS_FILE, {})

def initialize_user_fields(user_id: str):
    if user_id not in users_data:
        users_data[user_id] = {"id": user_id}
        users_data[user_id].setdefault("preferences", {"emotion": "未設定", "custom": "未設定"})
        users_data[user_id].setdefault("unopenedLetterIds", [])
        users_data[user_id].setdefault("receivedLetterIds", [])
        users_data[user_id].setdefault("sentLetterIds", [])
        users_data[user_id].setdefault("registered_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        users_data[user_id].setdefault("last_letter_retrieved_at", 0)
        logger.info(f"新規ユーザー {user_id} の情報を初期化しました。")


# --- Gemini Helper Functions ---

# ★★★ 1. 新しい専用フィルタリング関数 ★★★
async def is_message_inappropriate(message_title: str, message_text: str) -> bool:
    """メッセージが不適切かどうかを判断する。不適切な場合は True を返す。"""
    if not GEMINI_API_KEY_CONFIGURED or not gemini_model:
        logger.warning("Gemini API未設定のため、フィルタリングをスキップします。")
        return False

    combined_text = f"タイトル: {message_title}\n\n内容: {message_text}"

    prompt = f"""
    あなたは、投稿されるコンテンツを審査する、非常に厳格なコンテンツモデレーターです。
    以下の「審査対象テキスト」に、暴力的な表現、攻撃的な表現（暴言、罵倒、脅迫、他者を著しく不快にさせる言葉）、性的な表現、その他、一般的に不適切とみなされる内容が含まれているかどうかを判断してください。
    特に、「死ね」「消えろ」「殺す」などの直接的な危害を加える言葉や、他者を貶めるような攻撃的な言葉には、最も厳しく対処してください。

    含まれている場合は「はい」、含まれていない場合は「いいえ」とだけ、一言で答えてください。

    審査対象テキスト:
    "{combined_text}"
    """
    try:
        logger.info(f"Gemini APIへフィルタリングリクエスト送信 (メッセージ冒頭: '{combined_text[:30]}...')")
        safety_settings = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_LOW_AND_ABOVE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_LOW_AND_ABOVE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_LOW_AND_ABOVE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_LOW_AND_ABOVE"},
        ]
        response = await gemini_model.generate_content_async(prompt, safety_settings=safety_settings)

        if response.prompt_feedback.block_reason:
            logger.warning(f"フィルタリングAPI呼び出しが安全フィルターでブロックされました: {response.prompt_feedback.block_reason}")
            return True

        answer = response.text.strip()
        logger.info(f"Gemini APIからフィルタリング応答受信: '{answer}'")
        return "はい" in answer

    except Exception as e:
        logger.error(f"Gemini APIフィルタリング呼び出し中にエラー: {e}")
        return False

# ★★★ 2. マッチングに専念するよう修正された関数 ★★★
async def analyze_and_match_message(
    message_title: str, message_text: str, sender_emotion: str,
    current_users_data: Dict[str, Dict[str, Any]], sender_id_to_exclude: str
) -> Tuple[str, str]:
    
    if not GEMINI_API_KEY_CONFIGURED or not gemini_model:
        logger.warning("Gemini API未設定のため、宛先選定をスキップします。")
        return "該当者なし", "システムエラー (Gemini API未設定)"

    candidate_profiles_for_prompt = []
    for uid, u_data in current_users_data.items():
        if uid == sender_id_to_exclude: continue
        prefs = u_data.get("preferences", {"emotion": "未設定", "custom": "未設定"})
        custom_pref = prefs.get("custom", "未設定").strip() 
        profile_desc_parts = [f'user_id: "{uid}"', f'希望する手紙の種類(custom): "{custom_pref}"']
        if not custom_pref or custom_pref == "未設定":
            profile_desc_parts.append("(このユーザーは特に希望する手紙の種類を指定しておらず、どんなメッセージでも受け入れます)")
        candidate_profiles_for_prompt.append(", ".join(profile_desc_parts))
    
    if not candidate_profiles_for_prompt:
        return "該当者なし", "受信者候補がいません"

    formatted_profiles_str = "\n".join([f"- {p}" for p in candidate_profiles_for_prompt])
    
    prompt = f"""
    あなたは受信した「瓶レター」を、その手紙に最も相応しい一人の受信者に届ける、心のこもった仲介AIです。手紙は既に不適切でないか審査済みです。

    提供情報:
    1.  送信された手紙:
        -   タイトル: "{message_title}"
        -   メッセージ内容: "{message_text}"
        -   送信者の現在の感情(emotion): "{sender_emotion}" (これはメッセージの背景にある重要な文脈です)

    2.  受信希望者のリスト:
        -   各ユーザーの `user_id` と、彼らが「希望する手紙の種類(custom)」が記載されています。
        {formatted_profiles_str}

    あなたのタスク:
    あなたのゴールは、送信者と受信者の間に最も「意味のある繋がり」や「面白い化学反応」が生まれそうなペアを見つけることです。以下の要素を同等に考慮し、総合的に最適なマッチングを判断してください。

    選定基準:
    -   手紙の全体像の理解: 手紙の「タイトル」と「メッセージ内容」の両方を同等に重視し、「送信者の現在の感情(emotion)」と合わせて、「どのような手紙が送られてきたか」を深く理解してください。
    -   メッセージと受信者の希望（`custom`）の合致度: 上記で理解した手紙の全体像が、受信者の「希望する手紙の種類(custom)」にどれだけ応えているか評価してください。
    -   感情の共鳴と相互作用: 送信者の「現在の感情(emotion)」と、受信者の「希望する手紙の種類(custom)」の間に生まれる関係性を評価してください。
    -   総合的な判断: 上記の基準を同列に扱い、人間的な観点から最も興味深いペアリングを一つ選んでください。受信者の`custom`が「未設定」の場合は、他の希望者がより良いマッチングでない限り、どんな手紙でも受け入れる候補となります。

    上記を総合的に判断し、選ばれたユーザーの `user_id` と、その選定理由を簡潔に述べてください。
    適切な受信者が見つからない場合は、`user_id` として「該当者なし」と回答してください。

    回答形式 (他の言葉は含めないでください):
    user_id: [選ばれたuser_id または "該当者なし"]
    理由: [選定理由 または "適切な受信者が見つかりませんでした"]
    """
    try:
        logger.info(f"Gemini APIへマッチングリクエスト送信 (タイトル: '{message_title}', メッセージ冒頭: '{message_text[:30]}...')")
        response = await gemini_model.generate_content_async(prompt)
        
        response_text = response.text.strip()
        logger.info(f"Gemini APIからマッチングレスポンス受信: {response_text}")
        
        chosen_user_id_match = re.search(r"user_id:\s*(.+)", response_text, re.IGNORECASE)
        reason_match = re.search(r"理由:\s*(.+)", response_text, re.IGNORECASE)

        chosen_user_id_str = chosen_user_id_match.group(1).strip() if chosen_user_id_match else "該当者なし"
        reason_str = reason_match.group(1).strip() if reason_match else "選定理由の解析に失敗しました。"
        
        if not chosen_user_id_match: logger.warning(f"Geminiレスポンスからuser_idを解析できませんでした: {response_text}")
        if chosen_user_id_str != "該当者なし" and chosen_user_id_str not in current_users_data :
             logger.warning(f"Geminiが選択したuser_id '{chosen_user_id_str}' は不明なユーザーです。'該当者なし'として処理します。")
             chosen_user_id_str = "該当者なし"
             reason_str = f"システム判断: Geminiが選択したユーザー({chosen_user_id_str})は無効です。"

        return chosen_user_id_str, reason_str
    except Exception as e:
        logger.error(f"Gemini APIマッチング呼び出し中にエラー: {e}")
        return "該当者なし", f"システムエラー (APIエラー: {type(e).__name__})"


@app.on_event("startup")
async def startup_event():
    logger.info("アプリケーション起動。ユーザーデータ (users.json) およびレターデータ (letters.json) はロード済みです。")

# --- FastAPI Endpoints ---
@app.post("/check_user/{client_id}")
async def check_or_register_user(client_id: str):
    # (変更なし)
    if client_id in users_data:
        logger.info(f"User checked: {client_id} (Existing)")
        return {"is_new_user": False, "user_id": client_id, "details": users_data[client_id]}
    else:
        initialize_user_fields(client_id)
        save_json_data(USERS_FILE, users_data)
        logger.info(f"✨ New user registered via check: {client_id}")
        return {"is_new_user": True, "user_id": client_id, "details": users_data[client_id]}

@app.post("/send")
async def send_message(request: Request):
    try:
        data = await request.json()
    except Exception:
        logger.warning("不正なJSON形式のリクエストを受信しました。")
        return JSONResponse(status_code=400, content={"status": "error", "detail": "Invalid JSON format."})

    message_text = data.get("message")
    title = data.get("title", "No Title")
    sender_id = data.get("userId", "unknown_sender")
    client_ip = request.client.host if request.client else "N/A"

    if not message_text or sender_id == "unknown_sender":
        logger.warning(f"不正なリクエスト: 'message' または 'userId' がありません。IP={client_ip}, 受信データ={data}")
        return JSONResponse(status_code=400, content={"status": "error", "detail": "message and userId are required"})

    if sender_id not in users_data:
        logger.warning(f"Sender {sender_id} not found. Initializing user.")
        initialize_user_fields(sender_id)

    logger.info(f"📩 受信: from={sender_id}, ip={client_ip}, title='{title}', message='{message_text}'")

    letter_id = f"letter-{uuid.uuid4()}"
    current_time_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # ★★★ 3. /send のロジック変更: 最初にフィルタリングを実施 ★★★
    if await is_message_inappropriate(title, message_text):
        logger.info(f"メッセージ (from={sender_id}, title='{title}') は不適切と判断され、破棄されます。")
        new_letter = {
            "id": letter_id, "date_sent": current_time_iso, "date_received": 0,
            "sender_id": sender_id, "recipient_id": ["rejected"], # ★ recipient_idをrejectedに
            "title": title, "content": message_text,
            "routing_info": {"reason": "コンテンツフィルタリングにより拒否"}
        }
        letters_data[letter_id] = new_letter
        users_data[sender_id].setdefault("sentLetterIds", []).append(letter_id)
        save_json_data(LETTERS_FILE, letters_data)
        save_json_data(USERS_FILE, users_data)
        return {"status": "received_but_rejected", "letter_id": letter_id}

    # --- フィルタリングを通過した場合のみ、マッチング処理に進む ---
    
    sender_prefs = users_data.get(sender_id, {}).get("preferences", {})
    sender_emotion = sender_prefs.get("emotion", "未設定")

    chosen_user_id, reason_for_selection = await analyze_and_match_message(
        message_title=title,
        message_text=message_text,
        sender_emotion=sender_emotion,
        current_users_data=users_data,
        sender_id_to_exclude=sender_id
    )

    new_letter = {
        "id": letter_id, "date_sent": current_time_iso, "date_received": 0,
        "sender_id": sender_id, "recipient_id": ["waiting"], 
        "title": title, "content": message_text,
        "routing_info": {"reason": reason_for_selection, "gemini_choice": chosen_user_id}
    }
    final_recipient_status = "error_in_processing" 

    if chosen_user_id and chosen_user_id != "該当者なし": 
        new_letter["recipient_id"] = [chosen_user_id]
        users_data[chosen_user_id].setdefault("unopenedLetterIds", []).append(letter_id)
        final_recipient_status = chosen_user_id
        logger.info(f"Letter {letter_id} (from {sender_id}) routed to {chosen_user_id}. Reason: {reason_for_selection}")
    else: 
        new_letter["recipient_id"] = ["no_suitable_recipient"]
        final_recipient_status = "no_suitable_recipient"
        logger.info(f"Letter {letter_id} (from {sender_id}): No suitable recipient. Gemini choice: '{chosen_user_id}', Reason: '{reason_for_selection}'. Status set to 'no_suitable_recipient'.")

    letters_data[letter_id] = new_letter
    users_data[sender_id].setdefault("sentLetterIds", []).append(letter_id)
    
    save_json_data(LETTERS_FILE, letters_data)
    save_json_data(USERS_FILE, users_data)

    logger.info(f"📩 Message processing complete: from={sender_id}, letter_id='{letter_id}', final_recipient_status='{final_recipient_status}'")
    
    return {
        "status": "received_and_saved", 
        "letter_id": letter_id,
        "assigned_recipient_status": final_recipient_status
    }


# --- 王くんの他のエンドポイント (PreferencesPayloadの定義をトップレベルに移動) ---
# ... (前の回答の完全なコードを参考にしてください) ...

class PreferencesPayload(BaseModel):
    emotion: str
    custom: str

@app.get("/receive_unopened/{client_id}")
def get_unopened_letters(client_id: str):
    # (変更なし)
    if client_id not in users_data: raise HTTPException(status_code=404, detail="User not found")
    user = users_data[client_id]
    current_timestamp = time.time()
    last_retrieved_timestamp = user.get("last_letter_retrieved_at", 0)
    cooldown_setting = LETTER_RECEIVE_COOLDOWN_SECONDS 
    if current_timestamp < last_retrieved_timestamp + cooldown_setting:
        remaining_cooldown = int((last_retrieved_timestamp + cooldown_setting) - current_timestamp)
        return {"status": "cooldown", "letter": None, "cooldown_remaining_seconds": remaining_cooldown}
    unopened_ids = user.get("unopenedLetterIds", [])
    if not unopened_ids: return {"status": "no_new_letters", "letter": None}
    letter_id_to_deliver = unopened_ids[0] 
    if letter_id_to_deliver in letters_data:
        ld = letters_data[letter_id_to_deliver]
        return {"status": "new_letter_available", "id": ld.get("id"), "date_sent": ld.get("date_sent"), "date_received": ld.get("date_received"), "sender_id": ld.get("sender_id"), "title": ld.get("title"), "content": ld.get("content")}
    else:
        logger.warning(f"Stale Letter ID {letter_id_to_deliver} in {client_id}'s unopened list. Removing.")
        try: user["unopenedLetterIds"].pop(0)
        except IndexError: logger.error(f"IndexError for {client_id} unopenedLetterIds (stale check).")
        save_json_data(USERS_FILE, users_data)
        return {"status": "stale_letter_removed", "letter": None}

@app.post("/mark_letter_opened/{client_id}/{letter_id}")
async def mark_letter_opened(client_id: str, letter_id: str):
    # (変更なし)
    if client_id not in users_data: raise HTTPException(status_code=404, detail="User not found")
    user = users_data[client_id]
    response_status, client_formatted_letter = "error_letter_not_found_in_user_lists", None
    if letter_id in user.get("unopenedLetterIds", []):
        user["unopenedLetterIds"].remove(letter_id)
        user.setdefault("receivedLetterIds", []).append(letter_id)
        user["last_letter_retrieved_at"] = time.time()
        if letter_id in letters_data:
            letters_data[letter_id]["date_received"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            save_json_data(LETTERS_FILE, letters_data) 
            lt = letters_data[letter_id]
            client_formatted_letter = { "id": lt.get("id"), "title": lt.get("title"), "content": lt.get("content"), "date_received": lt.get("date_received"), "sender_id": lt.get("sender_id") }
            response_status = "marked_opened_and_in_received"
        else: 
            logger.error(f"Letter {letter_id} was in unopened for {client_id} but not in letters_data.")
            response_status = "marked_opened_but_letter_details_missing"
        save_json_data(USERS_FILE, users_data)
        logging.info(f"Letter {letter_id} marked as opened by {client_id}. Cooldown started.")
    elif letter_id in user.get("receivedLetterIds", []):
        logging.info(f"Letter {letter_id} already in received for {client_id}.")
        response_status = "already_in_received"
        if letter_id in letters_data:
            lt = letters_data[letter_id]
            client_formatted_letter = { "id": lt.get("id"), "title": lt.get("title"), "content": lt.get("content"), "date_received": lt.get("date_received"), "sender_id": lt.get("sender_id") }
        else:
            response_status = "already_in_received_but_letter_details_missing"
    else:
        raise HTTPException(status_code=404, detail="Letter not found in user's lists for marking.")
    return {"status": response_status, "letter": client_formatted_letter, "letter_id": letter_id }
    
@app.get("/letterbox/{client_id}")
def get_letterbox_contents(client_id: str):
    # (変更なし)
    if client_id not in users_data: raise HTTPException(status_code=404, detail="User not found")
    user = users_data[client_id]
    letterbox_ids = user.get("receivedLetterIds", [])
    letters_in_box_details = []
    for letter_id in letterbox_ids:
        if letter_id in letters_data:
            ld = letters_data[letter_id]
            letters_in_box_details.append({ "id": ld.get("id"), "title": ld.get("title"), "content": ld.get("content"), "date_received": ld.get("date_received", ld.get("date_sent","").split("T")[0]), "sender_id": ld.get("sender_id") })
        else:
            logging.warning(f"Letter ID {letter_id} in {client_id}'s received, but not in letters_data.")
    letters_in_box_details.sort(key=lambda x: x.get("date_received", ""), reverse=True)
    logging.info(f"📬 Fetched {len(letters_in_box_details)} letters from letterbox for {client_id}")
    return letters_in_box_details

@app.post("/update_preferences/{client_id}")
async def update_preferences_endpoint(client_id: str, payload: PreferencesPayload):
    # (変更なし)
    if client_id not in users_data: initialize_user_fields(client_id)
    if client_id not in users_data: raise HTTPException(status_code=404, detail="User not found after init attempt")
    user = users_data[client_id]
    user["preferences"] = {"emotion": payload.emotion, "custom": payload.custom}
    save_json_data(USERS_FILE, users_data)
    logging.info(f"Preferences updated for user {client_id}: {user['preferences']}")
    return {"status": "preferences_updated", "user_id": client_id, "updated_preferences": user["preferences"]}