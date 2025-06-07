from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
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
import asyncio # バックグラウンドタスクがメインスレッドと異なるループで動く場合の対策

load_dotenv()

app = FastAPI()

# --- File Paths, Logging, Gemini Config, etc. ---
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
MODERATION_BLOCKED_RESULT = "MODERATION_BLOCKED"

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
    # 注意: この同期的なファイル書き込みは、高頻度で呼ばれるとパフォーマンスの問題になる可能性があります。
    # 今回のプロジェクトの範囲では問題ないと判断しますが、本番環境では非同期I/OやDBを検討します。
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
async def is_message_inappropriate(message_title: str, message_text: str) -> bool:
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

async def analyze_and_match_message(
    message_title: str, message_text: str,
    current_users_data: Dict[str, Dict[str, Any]], sender_id_to_exclude: str
) -> Tuple[str, str]:
    if not GEMINI_API_KEY_CONFIGURED or not gemini_model:
        logger.warning("Gemini API未設定のため、宛先選定をスキップします。")
        return "該当者なし", "システムエラー (Gemini API未設定)"

    candidate_profiles_for_prompt = []
    for uid, u_data in current_users_data.items():
        if uid == sender_id_to_exclude: continue
        prefs = u_data.get("preferences", {"emotion": "未設定", "custom": "未設定"})
        emotion_pref = prefs.get("emotion", "未設定").strip()
        custom_pref = prefs.get("custom", "未設定").strip()
        received_count = len(u_data.get("receivedLetterIds", []))
        profile_desc_parts = [f'user_id: "{uid}"', f'現在の感情(emotion): "{emotion_pref}"', f'希望する手紙の種類(custom): "{custom_pref}"', f'現在の受信数: {received_count}']
        if (not emotion_pref or emotion_pref == "未設定") and (not custom_pref or custom_pref == "未設定"):
            profile_desc_parts.append("(このユーザーは特に希望を指定しておらず、どんなメッセージでも受け入れます)")
        candidate_profiles_for_prompt.append(", ".join(profile_desc_parts))
    
    if not candidate_profiles_for_prompt:
        return "該当者なし", "受信者候補がいません"

    formatted_profiles_str = "\n".join([f"- {p}" for p in candidate_profiles_for_prompt])
    
    prompt = f"""
    あなたは受信した「瓶レター」を、その手紙に最も相応しい一人の受信者に届ける、公平で心のこもった仲介AIです。手紙は既に不適切でないか審査済みです。

    提供情報:
    1.  送信された手紙:
        -   タイトル: "{message_title}"
        -   メッセージ内容: "{message_text}"

    2.  受信希望者のリスト:
        -   各ユーザーの `user_id`、彼らの「現在の感情(emotion)」、「希望する手紙の種類(custom)」、そして「現在の受信数」が記載されています。
        {formatted_profiles_str}

    あなたのタスク:
    あなたのゴールは、送信者と受信者の間に「意味のある繋がり」を創出しつつ、手紙が特定のユーザーに偏りすぎないように、「公平性」も考慮して最適なマッチングを行うことです。
    「手紙の数の公平性」と「マッチングの質」という2つの要素を考慮して、総合的に判断してください。優先度は「手紙の数の公平性」が高いです。

    選定基準:
    1.  公平性の考慮: 「現在の受信数」が少ないユーザーほど、手紙を受け取る優先度が高くなります。特に、「現在の受信数」が0のユーザーには、手紙の内容と現在の感情がよほど乖離してない限り、優先的に送ってください。
        ただし、受信数が少ないユーザーでも、手紙の内容と希望が全く合わない場合は、優先度を下げてください。
        例えば、現在の受信数が0のユーザーが「楽しい手紙」を希望しているのに対し、送信された手紙が「悲しい内容」の場合は、そのユーザーは候補から外してください。
    2.  マッチングの質の評価: まず、手紙の全体像（タイトル、内容）と、各受信者の「現在の感情(emotion)」および「希望する手紙の種類(custom)」を比較し、どれだけ合致するかを評価してください。手紙の内容と受信者の希望が全く異なる場合（例：楽しい手紙と、悲しい手紙を希望するユーザー）は、マッチングの質が低いと判断し、候補から外してください。
    3.  総合判断: 上記2点を踏まえ、まず「現在の受信数」が少ないユーザーを候補者グループとして選び出してください。そのグループの中で、「マッチングの質」が一定基準以上（良質または許容範囲）を最終的な受信者として選んでください。もし受信数が同じユーザーが複数いる場合は、その中で最もマッチングの質が高いと判断したユーザーを選んでください。

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


# ★★★ バックグラウンドで実行する関数を async def に修正 ★★★
async def process_letter_in_background(letter_id: str, title: str, message_text: str, sender_id: str):
    logger.info(f"バックグラウンド処理開始: letter_id={letter_id}")

    if await is_message_inappropriate(title, message_text):
        logger.info(f"メッセージ (letter_id={letter_id}) は不適切と判断され、破棄されます。")
        if letter_id in letters_data:
            letters_data[letter_id]["recipient_id"] = ["rejected"]
            letters_data[letter_id]["routing_info"] = {"reason": "コンテンツフィルタリングにより拒否"}
            save_json_data(LETTERS_FILE, letters_data)
        logger.info(f"バックグラウンド処理完了 (フィルタ済): letter_id={letter_id}")
        return

    chosen_user_id, reason_for_selection = await analyze_and_match_message(
        title, message_text, users_data, sender_id
    )
    
    letter_to_update = letters_data.get(letter_id)
    if not letter_to_update:
        logger.error(f"バックグラウンド処理エラー: letter_id={letter_id} が見つかりません。")
        return

    letter_to_update["routing_info"] = {"reason": reason_for_selection, "gemini_choice": chosen_user_id}

    if chosen_user_id and chosen_user_id != "該当者なし":
        letter_to_update["recipient_id"] = [chosen_user_id]
        if chosen_user_id in users_data:
            users_data[chosen_user_id].setdefault("unopenedLetterIds", []).append(letter_id)
            logger.info(f"バックグラウンド処理: Letter {letter_id} を {chosen_user_id} に割り当てました。")
        else:
             letter_to_update["recipient_id"] = ["error_recipient_not_found"]
             logger.error(f"バックグラウンド処理エラー: Geminiが選択したユーザー {chosen_user_id} がusers_dataに存在しません。")
    else:
        letter_to_update["recipient_id"] = ["no_suitable_recipient"]
        logger.info(f"バックグラウンド処理: Letter {letter_id} は適切な受信者が見つかりませんでした。")
    
    save_json_data(LETTERS_FILE, letters_data)
    save_json_data(USERS_FILE, users_data)
    logger.info(f"バックグラウンド処理完了: letter_id={letter_id}")

# --- Pydanticモデル定義 (トップレベルに移動) ---
class PreferencesPayload(BaseModel):
    emotion: str
    custom: str

@app.on_event("startup")
async def startup_event():
    logger.info("アプリケーション起動。ユーザーデータとレターデータはロード済みです。")

# --- FastAPI Endpoints ---
@app.post("/check_user/{client_id}")
async def check_or_register_user(client_id: str):
    if client_id in users_data:
        return {"is_new_user": False, "user_id": client_id, "details": users_data[client_id]}
    else:
        initialize_user_fields(client_id)
        save_json_data(USERS_FILE, users_data)
        return {"is_new_user": True, "user_id": client_id, "details": users_data[client_id]}

@app.post("/send")
async def send_message(request: Request, background_tasks: BackgroundTasks): # ★ 引数に BackgroundTasks を追加
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"status": "error", "detail": "Invalid JSON format."})

    message_text = data.get("message")
    title = data.get("title", "No Title")
    sender_id = data.get("userId", "unknown_sender")
    
    if not message_text or sender_id == "unknown_sender":
        return JSONResponse(status_code=400, content={"status": "error", "detail": "message and userId are required"})

    if sender_id not in users_data:
        initialize_user_fields(sender_id)

    logger.info(f"📩 受信: from={sender_id}, title='{title}', message='{message_text}'")

    letter_id = f"letter-{uuid.uuid4()}"
    current_time_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    
    new_letter = {
        "id": letter_id, "date_sent": current_time_iso, "date_received": 0,
        "sender_id": sender_id, "recipient_id": ["waiting_for_process"], 
        "title": title, "content": message_text,
        "routing_info": {"reason": "処理待ち"}
    }
    letters_data[letter_id] = new_letter
    users_data[sender_id].setdefault("sentLetterIds", []).append(letter_id)
    
    save_json_data(LETTERS_FILE, letters_data)
    save_json_data(USERS_FILE, users_data)
    
    background_tasks.add_task(
        process_letter_in_background,
        letter_id=letter_id,
        title=title,
        message_text=message_text,
        sender_id=sender_id
    )
    
    logger.info(f"📩 即時レスポンス返却: letter_id='{letter_id}'. フィルタリングとマッチングはバックグラウンドで実行します。")
    
    return {
        "status": "received_and_saved", 
        "letter_id": letter_id,
    }

@app.get("/receive_unopened/{client_id}")
def get_unopened_letters(client_id: str):
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
        letter_details_for_client = {k: v for k, v in ld.items() if k != "routing_info"}
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
    return letters_in_box_details

@app.post("/update_preferences/{client_id}")
async def update_preferences_endpoint(client_id: str, payload: PreferencesPayload):
    if client_id not in users_data: initialize_user_fields(client_id)
    if client_id not in users_data: raise HTTPException(status_code=404, detail="User not found after init attempt")
    user = users_data[client_id]
    user["preferences"] = {"emotion": payload.emotion, "custom": payload.custom}
    save_json_data(USERS_FILE, users_data)
    logging.info(f"Preferences updated for user {client_id}: {user['preferences']}")
    return {"status": "preferences_updated", "user_id": client_id, "updated_preferences": user["preferences"]}