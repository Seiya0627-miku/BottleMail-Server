from fastapi import FastAPI, Request
import random # randomはGemini利用により不要になるかもしれませんが、他で使う可能性を考慮し残します
import logging
import os
import google.generativeai as genai # Gemini APIライブラリ
from dotenv import load_dotenv # .envファイル利用のため

# .envファイルから環境変数を読み込む (任意)
load_dotenv()

app = FastAPI()
messages = []

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", # nameを追加してロガー名を記録
    handlers=[
        logging.FileHandler("server.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__) # ロガーインスタンスを取得

# --- Gemini API Configuration ---
GEMINI_API_KEY_CONFIGURED = False
gemini_model = None

try:
    GOOGLE_API_KEY = os.environ.get("GEMINI_API_KEY")
    if not GOOGLE_API_KEY:
        logger.error("環境変数 'GEMINI_API_KEY' が設定されていません。Geminiの機能は利用できません。")
    else:
        genai.configure(api_key=GOOGLE_API_KEY)
        # 使用するモデルを選択 (例: gemini-1.5-flash-latest, gemini-pro)
        gemini_model = genai.GenerativeModel('gemini-2.5-flash-preview-05-20')
        GEMINI_API_KEY_CONFIGURED = True
        logger.info("Gemini APIキーが正常に設定され、モデルが初期化されました。")
except Exception as e:
    logger.error(f"Gemini APIの設定中にエラーが発生しました: {e}")


async def get_actual_gemini_analysis(text: str) -> str:
    """
    実際にGemini APIを呼び出してメッセージ内容を分析する関数。
    """
    if not GEMINI_API_KEY_CONFIGURED or not gemini_model:
        logger.warning("Gemini API未設定のため、分析をスキップします。")
        return "Gemini分析結果: {API未設定のため分析スキップ}"

    # Geminiに投げるプロンプトを定義
    # このプロンプトで、どのような分析をしてほしいか、どのような形式で答えてほしいかを指示します。
    prompt = f"""
    あなたは受信した「瓶レター」の内容を分析する専門家AIです。
    以下の「メッセージ内容」を読み解き、主に感じられる「感情」、関連する「トピック」、そして「短い要約」を抽出してください。
    結果は必ず以下の形式で返してください。他の文言は含めないでください。
    「感情: [抽出された感情], トピック: [抽出されたトピック], 要約: [メッセージの短い要約]」

    メッセージ内容:
    "{text}"
    """
    try:
        logger.info(f"Gemini APIへリクエスト送信開始 (メッセージ冒頭: '{text[:30]}...')")
        # Gemini APIを非同期で呼び出す
        response = await gemini_model.generate_content_async(prompt)
        
        # 安全性に関するフィードバックを確認
        if response.prompt_feedback.block_reason:
            logger.warning(f"Geminiコンテンツ生成がブロックされました。理由: {response.prompt_feedback.block_reason}")
            return f"Gemini分析結果: {{コンテンツ生成がブロックされました: {response.prompt_feedback.block_reason}}}"
        
        analysis_text = response.text.strip()
        logger.info(f"Gemini APIからレスポンス受信: {analysis_text}")
        return f"Gemini分析結果: {{ {analysis_text} }}"
    except Exception as e:
        logger.error(f"Gemini API呼び出し中に予期せぬエラーが発生しました: {e}")
        return f"Gemini分析結果: {{エラー発生: {type(e).__name__} - {e}}}"

@app.post("/send")
async def send_message(request: Request):
    data = await request.json()
    msg = data.get("message")
    sender_id = data.get("user_id", "unknown")
    client_ip = request.client.host if request.client else "N/A"

    if not msg or not sender_id:
        logger.warning(f"不正なリクエストを受信: sender_idまたはmessageがありません。ip={client_ip}")
        return {"status": "error", "detail": "sender and message are required"}

    messages.append((msg, sender_id))
    logger.info(f"📩 受信: from={sender_id}, ip={client_ip}, message='{msg}'")

    # Geminiにメッセージを投げて分析結果を取得 (実際のAPI呼び出し)
    gemini_result = await get_actual_gemini_analysis(msg)
    
    # Geminiの分析結果をログに記録
    logger.info(f"🤖 Gemini分析 (message from {sender_id}): {gemini_result}")

    return {"status": "received", "gemini_analysis_logged": True} # 分析結果がログされたことを示す情報を返す

@app.get("/receive/{client_id}")
def receive_message(client_id: str):
    message_to_send = None
    sender_of_message = None # 送信者情報も返すように変更

    # messagesリストを逆順で探索するか、コピーして操作するなどして
    # pop時のインデックス問題を避けるのが堅実ですが、ここでは簡略化のためそのまま
    for i, (msg_content, sender) in enumerate(messages):
        if sender != client_id: 
            message_to_send = msg_content
            sender_of_message = sender
            messages.pop(i) # 配信したらリストから削除
            break 
            
    if message_to_send:
        logger.info(f"📤 配信: to={client_id}, message='{message_to_send}' (from sender: {sender_of_message})")
        return {"message": message_to_send, "sender_id": sender_of_message} # sender_idも返す
    
    logger.info(f"📤 配信試行: to={client_id}, 利用可能なメッセージなし")
    return {"message": None, "sender_id": None}