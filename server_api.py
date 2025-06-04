from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Any, Tuple, Optional # 型ヒントの追加
import random
import logging
import json
import os
import time 
import uuid 
import re # Geminiレスポンス解析用

# Gemini APIライブラリのインポートと環境変数読み込み
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv() # .envファイルから環境変数をロード

app = FastAPI()
# messages = [] # 王くんのコードではletters.jsonで管理のため、このグローバル変数は不要

# --- File Paths and Directory Setup ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
LETTERS_FILE = os.path.join(DATA_DIR, "letters.json")
os.makedirs(DATA_DIR, exist_ok=True)

LETTER_RECEIVE_COOLDOWN_SECONDS = 60 

# --- Logging Configuration ---
# 王くんのロギング設定を流用・整理
logger = logging.getLogger("bottlemail_server_gemini") # ロガー名を少し変更
logger.setLevel(logging.INFO)
log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

if not logger.handlers: # ハンドラの重複登録を防ぐ
    # ファイルハンドラ
    file_log_handler = logging.FileHandler("server.log", encoding="utf-8") # server.logは王くんのコード通り
    file_log_handler.setFormatter(log_formatter)
    logger.addHandler(file_log_handler)
    # ストリームハンドラ (コンソール出力)
    stream_log_handler = logging.StreamHandler()
    stream_log_handler.setFormatter(log_formatter)
    logger.addHandler(stream_log_handler)

# --- Gemini API Configuration ---
GEMINI_API_KEY_CONFIGURED = False
gemini_model = None
MODERATION_REJECTED_ID = "MODERATION_REJECTED" # モデレーションにより拒否された場合の特別なID

try:
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    if not GEMINI_API_KEY:
        logger.error("環境変数 'GEMINI_API_KEY' が設定されていません。Geminiの機能は利用できません。")
    else:
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_model = genai.GenerativeModel('gemini-2.5-flash-preview-05-20') # 指示されたモデル
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
    except (json.JSONDecodeError, FileNotFoundError) as e: # エラー型を具体的に
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
    if user_id not in users_data: # 既存データを上書きしないように確認
        users_data[user_id] = {"id": user_id}
        users_data[user_id].setdefault("preferences", {"emotion": "未設定", "custom": "未設定"})
        users_data[user_id].setdefault("unopenedLetterIds", [])
        users_data[user_id].setdefault("receivedLetterIds", [])
        users_data[user_id].setdefault("sentLetterIds", [])
        users_data[user_id].setdefault("registered_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        users_data[user_id].setdefault("last_letter_retrieved_at", 0)
        logger.info(f"新規ユーザー {user_id} の情報を初期化しました。")


# --- New Gemini Helper Function for Matching and Moderation ---
async def get_intelligent_match_with_moderation(
    message_text: str,
    all_users_data: Dict[str, Dict[str, Any]],
    sender_id_to_exclude: str
) -> Tuple[Optional[str], str]: # (recipient_id or MODERATION_REJECTED_ID or None, reason)
    
    if not GEMINI_API_KEY_CONFIGURED or not gemini_model:
        logger.warning("Gemini API未設定。マッチング処理をスキップします。")
        # フォールバックとして「該当者なし」を返すか、エラーを明確にするか。
        # ここでは「該当者なし」として扱うが、実際はエラー処理を検討。
        return None, "システムエラー (Gemini API未設定)"

    candidate_profiles_for_prompt = []
    for uid, u_data in all_users_data.items():
        if uid == sender_id_to_exclude:
            continue
        prefs = u_data.get("preferences", {"emotion": "未設定", "custom": "未設定"})
        emotion_pref = prefs.get("emotion", "未設定")
        custom_pref = prefs.get("custom", "未設定")
        
        profile_desc_parts = [f'user_id: "{uid}"']
        profile_desc_parts.append(f'希望感情(emotion): "{emotion_pref}"')
        profile_desc_parts.append(f'補足情報(custom): "{custom_pref}"')

        if emotion_pref == "未設定" and (custom_pref == "未設定" or not custom_pref.strip()):
            profile_desc_parts.append("(このユーザーは特に希望がなく、どんなメッセージでも受け入れます)")
        elif emotion_pref == "未設定":
            profile_desc_parts.append("(このユーザーの希望感情は未設定です。補足情報を参照してください)")
        
        candidate_profiles_for_prompt.append(", ".join(profile_desc_parts))
    
    if not candidate_profiles_for_prompt:
        return None, "受信者候補がいません"

    formatted_profiles_str = "\n".join([f"- {p}" for p in candidate_profiles_for_prompt])

    prompt = f"""
    あなたは受信したメッセージを分析し、多数の受信希望者の中から最も適切な一人に割り当てるAIです。

    メッセージ内容:
    "{message_text}"

    受信希望者のリスト (user_id、希望感情(emotion)、補足情報(custom)):
    {formatted_profiles_str}

    選定ロジック:
    1. まず、メッセージ内容を深く分析し、その主題、雰囲気、暗示されている感情などを把握してください。
    2. 次に、各受信希望者の「希望感情(emotion)」とメッセージ内容の分析結果を照合します。これが最も重要なマッチング基準です。
    3. 「希望感情(emotion)」が「未設定」のユーザーは、どんなメッセージでも受け入れる可能性がありますが、明確な希望感情を持つユーザーとのマッチングを優先してください。ただし、メッセージ内容と「補足情報(custom)」が非常に強く合致する場合は、「未設定」のユーザーも有力な候補となります。
    4. 「補足情報(custom)」は、特に「希望感情(emotion)」のマッチングが複数あった場合や、完全一致がない場合の重要な判断材料となります。メッセージ内容と「補足情報(custom)」の関連性を評価してください。
    5. 上記を総合的に判断し、このメッセージを受け取るのに最も相応しいユーザーを一人だけ選んでください。

    回答は以下の形式で、user_idと選定理由のみを返してください。
    user_id: [選ばれたユーザーのID または "該当者なし"]
    理由: [選定理由]
    """
    try:
        logger.info(f"Gemini APIへマッチングリクエスト送信 (メッセージ冒頭: '{message_text[:30]}...')")
        # generate_content_async inherently uses safety filters.
        response = await gemini_model.generate_content_async(prompt)

        if response.prompt_feedback.block_reason:
            logger.warning(f"Gemini API呼び出しが安全フィルターによりブロックされました: {response.prompt_feedback.block_reason} (メッセージ: '{message_text[:30]}...')")
            # ② フィルタリング機能: 不適切な内容の場合の処理
            return MODERATION_REJECTED_ID, f"コンテンツが不適切と判断 ({response.prompt_feedback.block_reason})"

        response_text = response.text.strip()
        logger.info(f"Gemini APIからマッチングレスポンス受信: {response_text}")

        chosen_user_id_match = re.search(r"user_id:\s*(.+)", response_text, re.IGNORECASE)
        reason_match = re.search(r"理由:\s*(.+)", response_text, re.IGNORECASE)

        chosen_user_id_str = chosen_user_id_match.group(1).strip() if chosen_user_id_match else "該当者なし"
        reason_str = reason_match.group(1).strip() if reason_match else "選定理由の解析に失敗"
        
        if not chosen_user_id_match:
             logger.warning(f"Geminiからの応答でuser_idを解析できませんでした: {response_text}")
        
        if chosen_user_id_str.lower() == "該当者なし":
            return None, reason_str # None signifies no specific user chosen
        
        # Geminiが返したIDが実在するか確認
        if chosen_user_id_str not in all_users_data:
            logger.warning(f"Geminiが返したuser_id '{chosen_user_id_str}' はusers_dataに存在しません。'該当者なし'として扱います。")
            return None, f"Geminiが選択したユーザー({chosen_user_id_str})は無効。理由: {reason_str}"

        return chosen_user_id_str, reason_str

    except Exception as e:
        logger.error(f"Gemini APIマッチング呼び出し中に予期せぬエラー: {e}")
        return None, f"システムエラー (Gemini API呼び出し失敗: {type(e).__name__})"


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
    data = await request.json()
    message_text = data.get("message")
    title = data.get("title", "No Title")
    sender_id = data.get("userId", "unknown_sender") # server_api.py側のキー名に合わせる
    
    if not message_text or sender_id == "unknown_sender":
        raise HTTPException(status_code=400, detail="Message content and valid userId are required")

    if sender_id not in users_data:
        logger.warning(f"Sender {sender_id} not found. Initializing user.")
        initialize_user_fields(sender_id)
        # save_json_data(USERS_FILE, users_data) # 後でまとめて保存

    letter_id = f"letter-{uuid.uuid4()}"
    current_time_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Geminiによる分析と受信者選定
    # recipient_id_selectedは、ユーザーID文字列、MODERATION_REJECTED_ID、またはNoneを返す
    recipient_id_selected, reason_for_choice = await get_intelligent_match_with_moderation(
        message_text, users_data, sender_id
    )

    new_letter = {
        "id": letter_id,
        "date_sent": current_time_iso,
        "date_received": 0,
        "sender_id": sender_id,
        "recipient_id": ["waiting"], # 初期値
        "title": title,
        "content": message_text,
        # "emotion_tag_inferred": "TBD", # 推測タグも保存する場合 (get_intelligent_match_with_moderation の返り値に追加が必要)
        "routing_info": {"reason": reason_for_choice, "gemini_choice": recipient_id_selected or "該当者なし"} # ルーティング情報を保存
    }

    final_recipient_assigned = False

    if recipient_id_selected == MODERATION_REJECTED_ID:
        # ② フィルタリング機能: recipient_idをrejectedに
        new_letter["recipient_id"] = ["rejected"]
        logger.info(f"Letter {letter_id} from {sender_id} REJECTED by moderation. Reason: {reason_for_choice}")
    elif recipient_id_selected and recipient_id_selected in users_data: # Noneでもなく、実在するID
        # ① マッチング機能: 選ばれたユーザーに設定
        new_letter["recipient_id"] = [recipient_id_selected]
        users_data[recipient_id_selected].setdefault("unopenedLetterIds", []).append(letter_id)
        logger.info(f"Letter {letter_id} from {sender_id} routed to {recipient_id_selected}. Reason: {reason_for_choice}")
        final_recipient_assigned = True
    else: # 該当者なし (recipient_id_selected is None) またはエラー
        new_letter["recipient_id"] = ["no_suitable_recipient"] # または "waiting" のまま
        logger.info(f"Letter {letter_id} from {sender_id}: No suitable recipient found or error. Reason: {reason_for_choice}. Status set to 'no_suitable_recipient'.")
    
    letters_data[letter_id] = new_letter
    users_data[sender_id].setdefault("sentLetterIds", []).append(letter_id)

    save_json_data(LETTERS_FILE, letters_data)
    save_json_data(USERS_FILE, users_data)

    return {
        "status": "received_and_processed", 
        "letter_id": letter_id,
        "recipient_status": new_letter["recipient_id"][0] # "rejected", user_id, or "no_suitable_recipient"
    }


# --- 以下のエンドポイントは王くんのコードからほぼそのまま (細かいログやレスポンス調整は可能性あり) ---
@app.get("/receive_unopened/{client_id}")
def get_unopened_letters(client_id: str):
    if client_id not in users_data:
        raise HTTPException(status_code=404, detail="User not found")
    user = users_data[client_id]
    current_timestamp = time.time()
    last_retrieved_timestamp = user.get("last_letter_retrieved_at", 0)

    if current_timestamp < last_retrieved_timestamp + LETTER_RECEIVE_COOLDOWN_SECONDS:
        remaining_cooldown = int((last_retrieved_timestamp + LETTER_RECEIVE_COOLDOWN_SECONDS) - current_timestamp)
        logger.info(f"User {client_id} is in cooldown. Remaining: {remaining_cooldown}s")
        return {"status": "cooldown", "letter": None, "cooldown_remaining_seconds": remaining_cooldown}

    unopened_ids = user.get("unopenedLetterIds", [])
    if not unopened_ids:
        logger.info(f"No unopened letters for {client_id}.")
        return {"status": "no_new_letters", "letter": None}

    letter_id_to_deliver = unopened_ids[0] 
    if letter_id_to_deliver in letters_data:
        letter_details = letters_data[letter_id_to_deliver]
        logger.info(f"📬 Offering letter {letter_id_to_deliver} to {client_id}")
        return {"status": "new_letter_available", **letter_details}
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
    response_status = "error_letter_not_in_unopened"
    client_formatted_letter = None

    if letter_id in user.get("unopenedLetterIds", []):
        user["unopenedLetterIds"].remove(letter_id)
        user.setdefault("receivedLetterIds", []).append(letter_id)
        user["last_letter_retrieved_at"] = time.time()
        if letter_id in letters_data:
            letters_data[letter_id]["date_received"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            save_json_data(LETTERS_FILE, letters_data) # letters.json 更新はここ
            # クライアントに返す手紙詳細を整形
            lt = letters_data[letter_id]
            client_formatted_letter = {
                "id": lt.get("id"), "title": lt.get("title"), "content": lt.get("content"),
                "date_received": lt.get("date_received"), "sender_id": lt.get("sender_id"),
                # "emotion_tag_inferred": lt.get("routing_info", {}).get("inferred_tag") # 必要なら
            }
        save_json_data(USERS_FILE, users_data) # users.json 更新
        logging.info(f"Letter {letter_id} marked as opened by {client_id}. Cooldown started.")
        response_status = "marked_opened_and_in_received" if client_formatted_letter else "marked_opened_but_letter_details_missing"

    elif letter_id in user.get("receivedLetterIds", []):
        logging.info(f"Letter {letter_id} already in received for {client_id}.")
        response_status = "already_in_received"
        if letter_id in letters_data: # 既に開封済みでも詳細を返す
            lt = letters_data[letter_id]
            client_formatted_letter = {
                "id": lt.get("id"), "title": lt.get("title"), "content": lt.get("content"),
                "date_received": lt.get("date_received"), "sender_id": lt.get("sender_id"),
            }
        else:
            response_status = "already_in_received_but_letter_details_missing"
    else:
        logging.warning(f"Letter {letter_id} not found in unopened for {client_id} to mark.")
        raise HTTPException(status_code=404, detail="Letter not found in user's unopened list")
    
    return {"status": response_status, 
            "letter": client_formatted_letter, 
            "letter_id": letter_id if not client_formatted_letter else None}
    
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
                "sender_id": ld.get("sender_id"),
                # "emotion_tag_inferred": ld.get("routing_info", {}).get("inferred_tag") # 必要なら
            })
        else:
            logging.warning(f"Letter ID {letter_id} in {client_id}'s received, but not in letters_data.")
    letters_in_box_details.sort(key=lambda x: x.get("date_received", ""), reverse=True)
    logging.info(f"📬 Fetched {len(letters_in_box_details)} letters from letterbox for {client_id}")
    return letters_in_box_details

class PreferencesPayload(BaseModel):
    emotion: str
    custom: str

@app.post("/update_preferences/{client_id}")
async def update_preferences_endpoint(client_id: str, payload: PreferencesPayload):
    if client_id not in users_data: initialize_user_fields(client_id)
    if client_id not in users_data: raise HTTPException(status_code=404, detail="User not found after init attempt")
    user = users_data[client_id]
    user["preferences"] = {"emotion": payload.emotion, "custom": payload.custom}
    save_json_data(USERS_FILE, users_data)
    logging.info(f"Preferences updated for user {client_id}: {user['preferences']}")
    return {"status": "preferences_updated", "user_id": client_id, "updated_preferences": user["preferences"]}