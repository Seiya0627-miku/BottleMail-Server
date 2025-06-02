from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import logging
import os
import re
import asyncio 
import google.generativeai as genai
from dotenv import load_dotenv
from typing import List, Dict, Tuple, Optional, Any

load_dotenv()

app = FastAPI()

messages: List[Tuple[str, str]] = []
receiver_profiles: List[Dict[str, str]] = []
SENDER_DATA_FILE_PATH = "sender_data.log"
sender_data_file_lock = asyncio.Lock()

logger = logging.getLogger("main_server")
logger.setLevel(logging.INFO)
main_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

if not logger.handlers:
    file_handler = logging.FileHandler("server.log", encoding="utf-8")
    file_handler.setFormatter(main_formatter)
    logger.addHandler(file_handler)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(main_formatter)
    logger.addHandler(stream_handler)

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

def parse_sender_data_log() -> Dict[str, List[str]]:
    data: Dict[str, List[str]] = {}
    current_receiver_id: Optional[str] = None
    try:
        with open(SENDER_DATA_FILE_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    current_receiver_id = None
                    continue
                
                receiver_match = re.fullmatch(r'receiver_id:\s*"(.*?)"', line)
                if receiver_match:
                    current_receiver_id = receiver_match.group(1)
                    if current_receiver_id not in data:
                        data[current_receiver_id] = []
                elif current_receiver_id: 
                    data[current_receiver_id].append(line)
    except FileNotFoundError:
        logger.info(f"{SENDER_DATA_FILE_PATH} が見つかりません。新規作成されます。")
    except Exception as e:
        logger.error(f"{SENDER_DATA_FILE_PATH} の解析中にエラー: {e}")
    return data

def format_sender_data_log_content(data: Dict[str, List[str]]) -> str:
    output_lines: List[str] = []
    for receiver_id_key, messages_list in data.items():
        output_lines.append(f'receiver_id: "{receiver_id_key}"')
        output_lines.extend(messages_list)
        output_lines.append("") 
    return "\n".join(output_lines).strip() + "\n" if output_lines else ""

async def append_to_sender_data_log(
    chosen_receiver_id_key: str,
    sender_id: str, 
    original_msg: str,
    tag: str, 
    reason: str
):
    async with sender_data_file_lock:
        log_data_structure = parse_sender_data_log()
        
        msg_for_log = original_msg.replace("\r\n", "").replace("\n", "").replace("\r", "")
        
        new_entry_line = f'read:"False", user_id: "{sender_id}", msg: "{msg_for_log}", tag: "{tag}", reason: "{reason}"'

        if chosen_receiver_id_key not in log_data_structure:
            log_data_structure[chosen_receiver_id_key] = []
        log_data_structure[chosen_receiver_id_key].append(new_entry_line)
        
        formatted_content = format_sender_data_log_content(log_data_structure)
        try:
            with open(SENDER_DATA_FILE_PATH, "w", encoding="utf-8") as f:
                f.write(formatted_content)
            logger.info(f"データを {SENDER_DATA_FILE_PATH} に書き込みました (宛先: {chosen_receiver_id_key})。")
        except Exception as e:
            logger.error(f"{SENDER_DATA_FILE_PATH} への書き込み中にエラー: {e}")

def load_receiver_data():
    global receiver_profiles
    receiver_profiles = []
    try:
        with open("receiver_data.log", "r", encoding="utf-8") as f:
            for line_number, line in enumerate(f, 1):
                line = line.strip()
                if not line: continue
                match = re.fullmatch(r'user_id:"(.*?)"\s*,\s*grasp:"(.*?)"\s*,\s*description:"(.*?)"', line)
                if match:
                    uid, grasp, desc = match.groups()
                    receiver_profiles.append({"user_id": uid, "grasp": grasp, "description": desc})
                else:
                    logger.warning(f"receiver_data.log の {line_number}行目を解析できませんでした: {line}")
        logger.info(f"{len(receiver_profiles)}件の受信者プロファイルをreceiver_data.logから読み込みました。")
    except FileNotFoundError:
        logger.error("receiver_data.log が見つかりません。受信者プロファイルは空です。")
    except Exception as e:
        logger.error(f"receiver_data.log の読み込み中にエラーが発生しました: {e}")

@app.on_event("startup")
async def startup_event():
    load_receiver_data()

MODERATION_BLOCKED_TAG = "MODERATION_BLOCKED"
UNKNOWN_EMOTION_TAG = "不明"
VALID_EMOTION_TAGS = ["悲しみ", "喜び", "楽しみ", "憂鬱"]

async def get_message_emotion_tag(text: str) -> str:
    if not GEMINI_API_KEY_CONFIGURED or not gemini_model:
        logger.warning("Gemini API未設定のため、感情タグ付けをスキップします。")
        return UNKNOWN_EMOTION_TAG 

    prompt = f"""
    以下の「メッセージ内容」を分析し、最も強く表現されている主要な感情を「悲しみ」「喜び」「楽しみ」「憂鬱」のいずれか一つだけ選んでください。
    返答は、選んだ感情の単語（例: 悲しみ）のみとし、他の言葉や記号は一切含めないでください。
    もし、これらの4つの感情のいずれにも明確に分類できない場合は、「不明」とだけ返答してください。

    メッセージ内容:
    "{text}"
    """
    try:
        logger.info(f"Gemini APIへ感情タグ付けリクエスト送信 (メッセージ冒頭: '{text[:30]}...')")
        response = await gemini_model.generate_content_async(prompt)
        
        if response.prompt_feedback.block_reason:
            logger.warning(f"Gemini感情タグ付けがブロックされました (不適切コンテンツの可能性): {response.prompt_feedback.block_reason} (メッセージ冒頭: '{text[:30]}...')")
            return MODERATION_BLOCKED_TAG
        
        emotion_tag = response.text.strip()
        if emotion_tag in VALID_EMOTION_TAGS or emotion_tag == UNKNOWN_EMOTION_TAG:
            logger.info(f"Gemini APIから感情タグ受信: {emotion_tag} (メッセージ冒頭: '{text[:30]}...')")
            return emotion_tag
        else:
            logger.warning(f"Geminiから予期しない感情タグを受信: '{emotion_tag}'。'{UNKNOWN_EMOTION_TAG}'として扱います。(メッセージ冒頭: '{text[:30]}...')")
            return UNKNOWN_EMOTION_TAG
    except Exception as e:
        logger.error(f"Gemini API感情タグ付け呼び出し中にエラー: {e} (メッセージ冒頭: '{text[:30]}...')")
        return UNKNOWN_EMOTION_TAG

async def determine_best_recipient(message_text: str, message_emotion_tag: str, profiles: List[Dict[str, str]]) -> Tuple[str, str]:
    if not GEMINI_API_KEY_CONFIGURED or not gemini_model:
        logger.warning("Gemini API未設定のため、宛先選定をスキップします。")
        return "該当者なし", "システムエラー (Gemini API未設定)"
    if not profiles:
        logger.warning("受信者プロファイルが空のため、宛先選定をスキップします。")
        return "該当者なし", "受信者データなし"

    formatted_profiles = "\n".join([
        f"- user_id: \"{p['user_id']}\", grasp: \"{p['grasp']}\", description: \"{p['description']}\"" for p in profiles
    ])

    # ★ プロンプトの「選定基準」部分を修正
    prompt = f"""
    あなたは、受け取ったメッセージを、最も適切な受信者に届けるための判断を行うAIです。

    以下の情報に基づいて、メッセージを送るのに最も相応しいユーザーを一人だけ選び、そのユーザーの「user_id」と「選定理由」を特定してください。

    提供情報:
    1. メッセージ内容: "{message_text}"
    2. メッセージから抽出された感情タグ: "{message_emotion_tag}" (このタグが「不明」の場合、メッセージ内容と受信者のdescriptionをより重視して判断してください。)
    3. 受信希望者のリスト（各ユーザーの希望感情 grasp と補足情報 description）:
    {formatted_profiles}

    選定基準:
    1.  **最優先事項:** メッセージの感情タグと受信者の「grasp」（希望感情）が完全に一致するユーザーを最優先で検討してください。
    2.  **次に考慮する事項 (上記1で一致するユーザーが複数いる場合、または完全一致するユーザーがいない場合):**
        a.  メッセージの感情タグと受信者の「grasp」が類似している、または関連性の高い感情であるユーザーを検討してください。(例: メッセージタグが「喜び」で、受信者のgraspが「楽しみ」など)
        b.  受信者の「description」（補足情報）を重要な手がかりとしてください。特に、メッセージの感情や内容と受信者のdescriptionが強く関連している場合（例: メッセージが「悲しみ」で、descriptionに「同じ悲しい状況の人と話したい」とある場合や、メッセージが「喜び」でdescriptionに「喜びを感じる話が聞きたい」とある場合）は、評価を高めてください。descriptionが「なし」または空欄の場合は、他の要素で判断してください。
        c.  メッセージ内容そのものも、受信者のdescriptionやgraspと照らし合わせて総合的な適合性を判断してください。
    3.  **総合判断:** 上記の優先順位と考慮事項を踏まえ、メッセージの送り手と受け手の間で最も良いマッチングとなりそうな相手を一人だけ選んでください。
    4.  **該当者なしの場合:** 適切な受信者が見つからない、または判断できない場合は、user_idとして「該当者なし」と回答してください。

    回答形式 (他の言葉は含めないでください):
    user_id: [選ばれたuser_id または "該当者なし"]
    理由: [選定理由 または "適切な受信者が見つかりませんでした"]
    """
    try:
        logger.info(f"Gemini APIへ宛先選定リクエスト送信 (メッセージ感情: {message_emotion_tag}, メッセージ冒頭: '{message_text[:30]}...')")
        response = await gemini_model.generate_content_async(prompt)

        if response.prompt_feedback.block_reason:
            logger.warning(f"Gemini宛先選定がブロックされました: {response.prompt_feedback.block_reason} (メッセージ冒頭: '{message_text[:30]}...')")
            return "該当者なし", f"システムエラー (コンテンツ生成ブロック: {response.prompt_feedback.block_reason})"

        response_text = response.text.strip()
        logger.info(f"Gemini APIから宛先選定レスポンス受信: {response_text}")
        
        chosen_user_id_match = re.search(r"user_id:\s*(.+)", response_text, re.IGNORECASE)
        reason_match = re.search(r"理由:\s*(.+)", response_text, re.IGNORECASE)

        chosen_user_id = chosen_user_id_match.group(1).strip() if chosen_user_id_match else "該当者なし"
        reason = reason_match.group(1).strip() if reason_match else "選定理由の解析に失敗しました。"
        
        if not chosen_user_id_match:
             logger.warning(f"Geminiからの応答でuser_idを解析できませんでした: {response_text}")
             chosen_user_id = "該当者なし"

        return chosen_user_id, reason
    except Exception as e:
        logger.error(f"Gemini API宛先選定呼び出し中にエラー: {e} (メッセージ冒頭: '{message_text[:30]}...')")
        return "該当者なし", f"システムエラー (APIエラー: {type(e).__name__})"

@app.post("/send")
async def send_message(request: Request):
    try:
        data = await request.json()
    except Exception:
        logger.warning("不正なJSON形式のリクエストを受信しました。")
        return JSONResponse(status_code=400, content={"status": "error", "detail": "Invalid JSON format."})

    msg = data.get("message")
    sender_id = data.get("userId", "unknown_sender") 
    client_ip = request.client.host if request.client else "N/A"

    if not msg or sender_id == "unknown_sender":
        logger.warning(f"不正なリクエスト: 'message' または 'userId' がありません。IP={client_ip}, 受信データ={data}")
        return JSONResponse(status_code=400, content={"status": "error", "detail": "message and userId are required"})

    logger.info(f"📩 受信: from={sender_id}, ip={client_ip}, message='{msg}'")
    
    emotion_tag = await get_message_emotion_tag(msg)

    if emotion_tag == MODERATION_BLOCKED_TAG:
        logger.info(f"メッセージ (from={sender_id}, msg='{msg[:30]}...') は不適切と判断され、処理を中断しました。")
        return {"status": "received", "detail": "Message processed but flagged."} 

    logger.info(f"🏷️ 付与された感情タグ: {emotion_tag} (from={sender_id}, message='{msg[:30]}...')")
    
    chosen_receiver_id, reason_for_selection = await determine_best_recipient(msg, emotion_tag, receiver_profiles)
    
    logger.info(f"🎯 選定された宛先ユーザー: {chosen_receiver_id}, 理由: {reason_for_selection} (message from {sender_id}='{msg[:30]}...')")

    await append_to_sender_data_log(
        chosen_receiver_id,
        sender_id,
        msg,
        emotion_tag,
        reason_for_selection
    )

    messages.append((msg, sender_id))

    return {
        "status": "received",
        "emotion_tag_assigned": emotion_tag,
        "matched_receiver": chosen_receiver_id,
        "reason": reason_for_selection
    }

@app.get("/receive/{client_id}")
def receive_message(client_id: str):
    message_to_send = None
    sender_of_message = None
    for i, (msg_content, sender) in enumerate(messages):
        if sender != client_id: 
            message_to_send = msg_content
            sender_of_message = sender
            messages.pop(i)
            break 
    if message_to_send:
        logger.info(f"📤 配信: to={client_id}, message='{message_to_send}' (from sender: {sender_of_message})")
        return {"message": message_to_send, "sender_id": sender_of_message}
    logger.info(f"📤 配信試行: to={client_id}, 利用可能なメッセージなし")
    return {"message": None, "sender_id": None}