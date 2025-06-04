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
# import asyncio # sender_data_file_lock が削除されたため、現時点では明示的な利用箇所なし
import google.generativeai as genai
from dotenv import load_dotenv
# import aiofiles # sender_data.log 用だったので削除

load_dotenv()

app = FastAPI()

# messages: List[Tuple[str, str]] = [] # 王くんのコードではletters.jsonで管理のため不要

# --- File Paths and Directory Setup ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
LETTERS_FILE = os.path.join(DATA_DIR, "letters.json")
os.makedirs(DATA_DIR, exist_ok=True)

LETTER_RECEIVE_COOLDOWN_SECONDS = 60 

# --- Logging Configuration ---
logger = logging.getLogger("bottlemail_server_final") # ロガー名を更新
logger.setLevel(logging.INFO)
log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

if not logger.handlers:
    file_log_handler = logging.FileHandler("server.log", encoding="utf-8")
    file_log_handler.setFormatter(log_formatter)
    logger.addHandler(file_log_handler)
    stream_log_handler = logging.StreamHandler()
    stream_log_handler.setFormatter(log_formatter)
    logger.addHandler(stream_log_handler)

# --- Gemini API Configuration ---
GEMINI_API_KEY_CONFIGURED = False
gemini_model = None
MODERATION_BLOCKED_RESULT = "MODERATION_BLOCKED" # モデレーション結果の識別子

try:
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    if not GEMINI_API_KEY:
        logger.error("環境変数 'GEMINI_API_KEY' が設定されていません。Geminiの機能は利用できません。")
    else:
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_model = genai.GenerativeModel('gemini-2.5-flash-preview-05-20') # ユーザー指定モデル
        GEMINI_API_KEY_CONFIGURED = True
        logger.info("Gemini APIキーが正常に設定され、モデル ('gemini-2.5-flash-preview-05-20') が初期化されました。")
except Exception as e:
    logger.error(f"Gemini APIの設定中にエラーが発生しました: {e}")

# --- JSON Helper functions (from Wang-kun) ---
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

# --- Load data at startup (from Wang-kun) ---
users_data: Dict[str, Dict[str, Any]] = load_json_data(USERS_FILE, {})
letters_data: Dict[str, Dict[str, Any]] = load_json_data(LETTERS_FILE, {})

# --- User Initialization (from Wang-kun) ---
def initialize_user_fields(user_id: str):
    if user_id not in users_data:
        users_data[user_id] = {"id": user_id}
        # preferences.emotion はユーザー自身の現在の感情状態（送信時など）に使い、
        # preferences.custom を「受信したい手紙の種類」としてマッチングに使う
        users_data[user_id].setdefault("preferences", {"emotion": "未設定", "custom": "未設定"})
        users_data[user_id].setdefault("unopenedLetterIds", [])
        users_data[user_id].setdefault("receivedLetterIds", [])
        users_data[user_id].setdefault("sentLetterIds", [])
        users_data[user_id].setdefault("registered_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        users_data[user_id].setdefault("last_letter_retrieved_at", 0)
        logger.info(f"新規ユーザー {user_id} の情報を初期化しました。")

# --- sender_data.log 関連の関数は全て削除 ---

# --- Gemini Helper Function for Matching and Moderation (Updated) ---
async def analyze_and_match_message(
    message_text: str,
    current_users_data: Dict[str, Dict[str, Any]],
    sender_id_to_exclude: str
) -> Tuple[str, str]: # (chosen_user_id_str or MODERATION_BLOCKED_RESULT or "該当者なし", reason_str)
    
    if not GEMINI_API_KEY_CONFIGURED or not gemini_model:
        logger.warning("Gemini API未設定のため、分析と宛先選定をスキップします。")
        return "該当者なし", "システムエラー (Gemini API未設定)"

    candidate_profiles_for_prompt = []
    for uid, u_data in current_users_data.items():
        if uid == sender_id_to_exclude: continue
        prefs = u_data.get("preferences", {"emotion": "未設定", "custom": "未設定"})
        custom_pref = prefs.get("custom", "未設定").strip()
        # preferences.emotion は受信者の希望としては無視

        profile_desc_parts = [f'user_id: "{uid}"']
        profile_desc_parts.append(f'希望する手紙の種類(customフィールドより): "{custom_pref}"')

        if not custom_pref or custom_pref == "未設定":
            profile_desc_parts.append("(このユーザーは特に希望する手紙の種類を指定しておらず、どんなメッセージでも受け入れます)")
        
        candidate_profiles_for_prompt.append(", ".join(profile_desc_parts))
    
    if not candidate_profiles_for_prompt:
        return "該当者なし", "受信者候補がいません"

    formatted_profiles_str = "\n".join([f"- {p}" for p in candidate_profiles_for_prompt])
    
    prompt = f"""
    あなたは受信した「瓶レター」のメッセージを分析し、多数の受信希望者の中から最も適切な一人に割り当てるAIです。

    提供情報:
    1. これから分析・割り当てを行う「瓶レター」のメッセージ内容:
       "{message_text}"

    2. 受信希望者のリスト。各ユーザーは `user_id` と、彼らが「希望する手紙の種類(customフィールドより)」を持っています。これは彼らが普段どのような種類のメッセージを受け取りたいかを示しています。(ユーザーの`preferences`内の`emotion`フィールドは送信者本人の現在の感情を示すものであり、受信希望とは無関係なので無視してください。)
       {formatted_profiles_str}

    あなたのタスク:
    1.  まず、上記の「瓶レター」のメッセージ内容を深く分析し、その主題、雰囲気、トーン、および暗示されている感情や状況を総合的に把握してください。メッセージ自体に特定の感情カテゴリを付与する必要はありません。
    2.  次に、このメッセージ内容の分析結果と、各受信希望者の「希望する手紙の種類(customフィールドより)」を比較検討し、このメッセージを受け取るのに最も相応しいユーザーを一人だけ選んでください。
        - **最優先事項:** メッセージ内容の分析結果（読み取れる感情やテーマ、状況など）と、受信者の「希望する手紙の種類(customフィールドより)」が強く合致するユーザーを最優先で検討してください。
            - 「希望する手紙の種類(customフィールドより)」が具体的な感情（例：「喜び」「悲しみ」など）を示している場合、メッセージから読み取れる主要な感情とそれが一致するかを重視します。
            - 「希望する手紙の種類(customフィールドより)」がより説明的な場合（例：「同じ状況の人と話したい」「明るい話題が欲しい」など）、メッセージ内容全体との文脈的な適合性を重視します。
        - 「希望する手紙の種類(customフィールドより)」が「未設定」またはそれに類する記載（例：「どんなメッセージでも受け入れます」）のユーザーは、どんなメッセージでも受け入れる可能性がありますが、明確な希望を持ち、かつメッセージと合致するユーザーがいる場合はそちらを優先してください。
    3.  上記を総合的に判断し、選ばれたユーザーの `user_id` と、その選定理由を簡潔に述べてください。
    4.  適切な受信者が見つからない、または判断できない場合は、`user_id` として「該当者なし」と回答してください。

    回答形式 (他の言葉は含めないでください):
    user_id: [選ばれたuser_id または "該当者なし"]
    理由: [選定理由 または "適切な受信者が見つかりませんでした"]
    """
    try:
        logger.info(f"Gemini APIへ分析・宛先選定リクエスト送信 (メッセージ冒頭: '{message_text[:30]}...')")
        response = await gemini_model.generate_content_async(prompt)

        if response.prompt_feedback.block_reason:
            logger.warning(f"Gemini API呼び出しが安全フィルターによりブロックされました: {response.prompt_feedback.block_reason} (メッセージ冒頭: '{message_text[:30]}...')")
            return MODERATION_BLOCKED_RESULT, f"コンテンツフィルタリングにより拒否 ({response.prompt_feedback.block_reason})"

        response_text = response.text.strip()
        logger.info(f"Gemini APIから分析・宛先選定レスポンス受信: {response_text}")
        
        chosen_user_id_match = re.search(r"user_id:\s*(.+)", response_text, re.IGNORECASE)
        reason_match = re.search(r"理由:\s*(.+)", response_text, re.IGNORECASE)

        chosen_user_id_str = chosen_user_id_match.group(1).strip() if chosen_user_id_match else "該当者なし"
        reason_str = reason_match.group(1).strip() if reason_match else "選定理由の解析に失敗しました。"
        
        if not chosen_user_id_match: 
            logger.warning(f"Geminiレスポンスからuser_idを解析できませんでした: {response_text}")
        
        if chosen_user_id_str != "該当者なし" and chosen_user_id_str not in current_users_data :
             logger.warning(f"Geminiが選択したuser_id '{chosen_user_id_str}' は不明なユーザーです。'該当者なし'として処理します。")
             chosen_user_id_str = "該当者なし"
             reason_str = f"システム判断: Geminiが選択したユーザー({chosen_user_id_str})は無効です。"

        return chosen_user_id_str, reason_str
    except Exception as e:
        logger.error(f"Gemini API分析・宛先選定呼び出し中にエラー: {e} (メッセージ冒頭: '{message_text[:30]}...')")
        return "該当者なし", f"システムエラー (APIエラー: {type(e).__name__})"


@app.on_event("startup")
async def startup_event():
    logger.info("アプリケーション起動。ユーザーデータ (users.json) およびレターデータ (letters.json) はロード済みです。")

# --- FastAPI Endpoints ---
@app.post("/check_user/{client_id}")
async def check_or_register_user(client_id: str):
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

    logger.info(f"📩 受信: from={sender_id}, ip={client_ip}, message='{message_text}'")

    letter_id = f"letter-{uuid.uuid4()}"
    current_time_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    
    chosen_user_id, reason_for_selection = await analyze_and_match_message(
        message_text, users_data, sender_id
    )

    new_letter = {
        "id": letter_id,
        "date_sent": current_time_iso,
        "date_received": 0,
        "sender_id": sender_id,
        "recipient_id": ["waiting"], 
        "title": title,
        "content": message_text,
        # "emotion_tag_inferred" フィールドは削除
        "routing_info": {"reason": reason_for_selection, "gemini_choice": chosen_user_id}
    }
    final_recipient_status = "error_in_processing" 

    if chosen_user_id == MODERATION_BLOCKED_RESULT:
        new_letter["recipient_id"] = ["rejected"]
        final_recipient_status = "rejected"
        logger.info(f"Letter {letter_id} (from {sender_id}) REJECTED by moderation. Reason: {reason_for_selection}")
    elif chosen_user_id and chosen_user_id != "該当者なし": 
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

    # sender_data.log への記録処理は完全に削除

    logger.info(f"📩 Message processing complete: from={sender_id}, letter_id='{letter_id}', final_recipient_status='{final_recipient_status}'")
    
    return {
        "status": "received_and_processed", 
        "letter_id": letter_id,
        "assigned_recipient_status": final_recipient_status
        # "inferred_emotion_tag" はレスポンスから削除
    }

@app.get("/receive_unopened/{client_id}")
def get_unopened_letters(client_id: str):
    if client_id not in users_data:
        raise HTTPException(status_code=404, detail="User not found")
    user = users_data[client_id]
    current_timestamp = time.time()
    last_retrieved_timestamp = user.get("last_letter_retrieved_at", 0)
    cooldown_setting = LETTER_RECEIVE_COOLDOWN_SECONDS 

    if current_timestamp < last_retrieved_timestamp + cooldown_setting:
        remaining_cooldown = int((last_retrieved_timestamp + cooldown_setting) - current_timestamp)
        logger.info(f"User {client_id} is in cooldown. Remaining: {remaining_cooldown}s")
        return {"status": "cooldown", "letter": None, "cooldown_remaining_seconds": remaining_cooldown}

    unopened_ids = user.get("unopenedLetterIds", [])
    if not unopened_ids:
        logger.info(f"No unopened letters for {client_id}.")
        return {"status": "no_new_letters", "letter": None}

    letter_id_to_deliver = unopened_ids[0] 
    if letter_id_to_deliver in letters_data:
        letter_details_original = letters_data[letter_id_to_deliver]
        # クライアントに返す情報を整形（不要な内部情報を除外）
        letter_details_for_client = {
            "id": letter_details_original.get("id"),
            "date_sent": letter_details_original.get("date_sent"),
            "date_received": letter_details_original.get("date_received"), # 開封前は通常0
            "sender_id": letter_details_original.get("sender_id"),
            "title": letter_details_original.get("title"),
            "content": letter_details_original.get("content"),
            # "routing_info" や "emotion_tag_inferred" はクライアントには返さない
        }
        logger.info(f"📬 Offering letter {letter_id_to_deliver} to {client_id}")
        return {"status": "new_letter_available", **letter_details_for_client}
    else:
        logger.warning(f"Stale Letter ID {letter_id_to_deliver} in {client_id}'s unopened list. Removing.")
        try: user["unopenedLetterIds"].pop(0)
        except IndexError: logger.error(f"IndexError for {client_id} unopenedLetterIds (stale check).")
        save_json_data(USERS_FILE, users_data)
        return {"status": "stale_letter_removed", "letter": None}

@app.post("/mark_letter_opened/{client_id}/{letter_id}")
async def mark_letter_opened(client_id: str, letter_id: str):
    if client_id not in users_data: raise HTTPException(status_code=404, detail="User not found")
    user = users_data[client_id]
    response_status = "error_letter_not_found_in_user_lists"
    client_formatted_letter = None

    if letter_id in user.get("unopenedLetterIds", []):
        user["unopenedLetterIds"].remove(letter_id)
        user.setdefault("receivedLetterIds", []).append(letter_id)
        user["last_letter_retrieved_at"] = time.time()
        if letter_id in letters_data:
            letters_data[letter_id]["date_received"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            save_json_data(LETTERS_FILE, letters_data) 
            lt = letters_data[letter_id]
            client_formatted_letter = { 
                "id": lt.get("id"), "title": lt.get("title"), "content": lt.get("content"),
                "date_received": lt.get("date_received"), "sender_id": lt.get("sender_id")
            }
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
            client_formatted_letter = {
                "id": lt.get("id"), "title": lt.get("title"), "content": lt.get("content"),
                "date_received": lt.get("date_received"), "sender_id": lt.get("sender_id")
            }
        else:
            response_status = "already_in_received_but_letter_details_missing"
    else:
        logging.warning(f"Letter {letter_id} not found in unopened or received for {client_id} to mark.")
        raise HTTPException(status_code=404, detail="Letter not found in user's lists for marking.")
    
    return {"status": response_status, 
            "letter": client_formatted_letter, 
            "letter_id": letter_id }
    
@app.get("/letterbox/{client_id}")
def get_letterbox_contents(client_id: str):
    if client_id not in users_data: raise HTTPException(status_code=404, detail="User not found")
    user = users_data[client_id]
    letterbox_ids = user.get("receivedLetterIds", [])
    letters_in_box_details = []
    for letter_id in letterbox_ids:
        if letter_id in letters_data:
            ld = letters_data[letter_id]
            letters_in_box_details.append({
                "id": ld.get("id"), "title": ld.get("title"), "content": ld.get("content"), 
                "date_received": ld.get("date_received", ld.get("date_sent","").split("T")[0]), 
                "sender_id": ld.get("sender_id")
            })
        else:
            logging.warning(f"Letter ID {letter_id} in {client_id}'s received, but not in letters_data.")
    letters_in_box_details.sort(key=lambda x: x.get("date_received", ""), reverse=True)
    logging.info(f"📬 Fetched {len(letters_in_box_details)} letters from letterbox for {client_id}")
    return letters_in_box_details

class PreferencesPayload(BaseModel):
    emotion: str # ユーザー自身の現在の感情状態 (送信時などにクライアントが設定する想定)
    custom: str  # 受信したい手紙の種類・内容の希望

@app.post("/update_preferences/{client_id}")
async def update_preferences_endpoint(client_id: str, payload: PreferencesPayload):
    if client_id not in users_data: initialize_user_fields(client_id)
    if client_id not in users_data: raise HTTPException(status_code=404, detail="User not found after init attempt")
    user = users_data[client_id]
    # preferences.emotion は送信時のユーザーの感情として保存されるが、受信マッチングでは無視
    # preferences.custom が受信希望としてマッチングに使われる
    user["preferences"] = {"emotion": payload.emotion, "custom": payload.custom}
    save_json_data(USERS_FILE, users_data)
    logging.info(f"Preferences updated for user {client_id}: {user['preferences']}")
    return {"status": "preferences_updated", "user_id": client_id, "updated_preferences": user["preferences"]}