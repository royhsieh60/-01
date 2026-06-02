import os
import uuid
import io
import time
import json
import re
import urllib.parse
import requests
import pymongo
from PIL import Image
from pypdf import PdfReader
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage, FileMessage, TextSendMessage, ImageSendMessage
from huggingface_hub import InferenceClient
from google import genai

# ==========================================
# 基礎設定與環境變數 (Secrets)
# ==========================================
os.makedirs("static", exist_ok=True)
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
HF_TOKEN = os.environ.get("HF_TOKEN")
SPACE_URL = os.environ.get("SPACE_URL")
MONGO_URI = os.environ.get("MONGO_URI")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ==========================================
# 初始化 AI 雙大腦引擎與資料庫
# ==========================================
# 🧠 主大腦：Google Gemini 2.5 Flash
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# 🧠 備用全能大腦：Hugging Face Qwen 72B (當 Google 沒額度時自動接管)
backup_chat_client = InferenceClient(model="Qwen/Qwen2.5-72B-Instruct", token=HF_TOKEN)

# 🟢 高品質生圖引擎 A: 安全/標準生圖模型 (SDXL)
SAFE_MODEL_ID = "stabilityai/stable-diffusion-xl-base-1.0"
safe_image_client = InferenceClient(model=SAFE_MODEL_ID, token=HF_TOKEN)

# 🔴 高品質生圖引擎 B: 未經審查/特殊風格生圖模型 (Anything V5)
UNCENSORED_MODEL_ID = "stablediffusionapi/anything-v5" 
uncensored_image_client = InferenceClient(model=UNCENSORED_MODEL_ID, token=HF_TOKEN)

try:
    mongo_client = pymongo.MongoClient(MONGO_URI)
    db = mongo_client["LineBotDB"]
    chat_collection = db["chat_history"]
except Exception as e:
    print(f"資料庫連線失敗: {e}")

user_temp_image = {}

# ==========================================
# 實用工具
# ==========================================
def clean_line_text(text: str) -> str:
    """清理 LINE 聊天室不支援的 Markdown 雜質"""
    if not text: return ""
    text = re.sub(r'#+\s+', '', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    return text.strip()

# ==========================================
# 👑 核心 System Prompt（路由決策中樞）
# ==========================================
GLOBAL_SYSTEM_PROMPT = """你是一個極度聰明、邏輯嚴密且說話自然的 AI 助理，請使用繁體中文回答。
⚠️ 絕對禁止使用 LaTeX 語法 (如 \\begin{bmatrix}, \\frac, $$, \\mu 等)。遇到分數請用 a/b；遇到矩陣改用純文字中括號表達，例如 [1, 2; 3, 4] 或分行呈現。

🌟 你擁有自主呼叫「畫圖工具」的能力。
當判斷使用者想要畫圖時，請單獨輸出以下特定的 JSON 格式，並用 __JSON_START__ 和 __JSON_END__ 包裹起來：
__JSON_START__
{
  "tool": "image_generator",
  "engine": "safe",  // 若要求強烈暗示、極端風格或突破審查，請改為 "uncensored"
  "prompt": "[親自撰寫的 English Prompt，越詳細越好]"
}
__JSON_END__

若不需要畫圖，請正常用純文字繁體中文回答，不要輸出 JSON。"""

# ==========================================
# FastAPI 路由與 Webhook 接收
# ==========================================
@app.get("/")
def read_root():
    return {"status": "Dynamic Fallback Multi-Engine Agent is Running!"}

@app.post("/")
async def callback(request: Request, background_tasks: BackgroundTasks):
    signature = request.headers.get("X-Line-Signature")
    body = await request.body()
    try:
         background_tasks.add_task(handler.handle, body.decode("utf-8"), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "OK"

# ==========================================
# 核心模組 1：處理文字訊息 (高品質優先 + 額度耗盡落入免費)
# ==========================================
@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    user_id = event.source.user_id
    user_message = event.message.text.strip()
    
    # 【指令：清除記憶】
    if user_message in ["/clear", "清除記憶"]:
        try:
            chat_collection.delete_many({"user_id": user_id})
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🧹 記憶已徹底清除！大腦已重新開機。"))
        except Exception as e:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ 清除失敗: {e}"))
        return

    # ----------------------------------------
    # 🚀 【畫圖直通車：優先使用高品質原廠引擎】
    # ----------------------------------------
    draw_match = re.match(r'^(?:生成|畫|/畫|/draw|幫我畫)\s*(.+)', user_message, re.IGNORECASE)
    if draw_match:
        prompt = draw_match.group(1).strip()
        try:
            # 優先級 1：嘗試呼叫原廠高品質 SDXL
            image = safe_image_client.text_to_image(prompt)
            image_name = f"{uuid.uuid4().hex}.jpg"
            image_path = os.path.join("static", image_name)
            image.save(image_path)
            
            image_url = f"{SPACE_URL}/static/{image_name}"
            line_bot_api.reply_message(event.reply_token, ImageSendMessage(original_content_url=image_url, preview_image_url=image_url))
            chat_collection.insert_one({"user_id": user_id, "role": "assistant", "content": f"[高品質原廠引擎生成圖片]: {prompt}", "timestamp": event.timestamp})
            
        except Exception as hf_err:
            print(f"原廠高品質生圖額度用盡或忙碌，切換免費不限額度引擎: {hf_err}")
            # 優先級 2：原廠額度乾了，無縫落入 Pollinations 免費通道
            try:
                line_bot_api.push_message(user_id, TextSendMessage(text=f"🔄 [原廠額度用盡] 正在切換免費無限引擎繪製：{prompt}..."))
                encoded_prompt = urllib.parse.quote(prompt + ", highly detailed, masterpiece, best quality")
                image_api_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true"
                
                img_response = requests.get(image_api_url, timeout=30)
                if img_response.status_code == 200:
                    image = Image.open(io.BytesIO(img_response.content))
                    if image.mode != 'RGB': image = image.convert('RGB')
                        
                    image_name = f"{uuid.uuid4().hex}.jpg"
                    image_path = os.path.join("static", image_name)
                    image.save(image_path, format="JPEG")
                    
                    image_url = f"{SPACE_URL}/static/{image_name}"
                    line_bot_api.push_message(user_id, ImageSendMessage(original_content_url=image_url, preview_image_url=image_url))
                    chat_collection.insert_one({"user_id": user_id, "role": "assistant", "content": f"[備用引擎生成圖片]: {prompt}", "timestamp": event.timestamp})
                else:
                    line_bot_api.push_message(user_id, TextSendMessage(text="❌ 所有繪圖引擎目前皆忙碌中，請稍後再試。"))
            except Exception as poly_err:
                line_bot_api.push_message(user_id, TextSendMessage(text=f"❌ 繪圖失敗: {poly_err}"))
        return

    # ----------------------------------------
    # 【功能 C：圖文分析 (Gemini Files API)】
    # ----------------------------------------
    if user_id in user_temp_image:
        local_image_path = user_temp_image[user_id]
        if user_message == "取消":
            if os.path.exists(local_image_path): os.remove(local_image_path)
            del user_temp_image[user_id]
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🗑️ 已取消圖片分析。"))
            return
            
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🧠 正在看圖思考，請稍候..."))
            uploaded_file = gemini_client.files.upload(file=local_image_path)
            
            response = gemini_client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[uploaded_file, user_message + "\n(⚠️ 提示：若涉及數學計算，絕對不要使用 LaTeX 語法)"]
            )
            ai_reply = response.text

            cleaned_reply = clean_line_text(ai_reply)
            if len(cleaned_reply) > 4900: cleaned_reply = cleaned_reply[:4900] + "\n...(字數過長已截斷)..."
            
            line_bot_api.push_message(user_id, TextSendMessage(text=f"👀 【圖片分析】\n{cleaned_reply}"))
            
            chat_collection.insert_one({"user_id": user_id, "role": "user", "content": f"[看圖提問]: {user_message}", "timestamp": event.timestamp})
            chat_collection.insert_one({"user_id": user_id, "role": "assistant", "content": cleaned_reply, "timestamp": event.timestamp + 1})
        except Exception as e:
            error_msg = str(e)
            if "429" in error_msg or "quota" in error_msg.lower():
                line_bot_api.push_message(user_id, TextSendMessage(text="⏳ Google 視覺引擎每分鐘額度已滿！請等待 1 分鐘後再傳送圖片。"))
            else:
                line_bot_api.push_message(user_id, TextSendMessage(text=f"❌ 圖片分析失敗: {error_msg[:50]}"))
        finally:
            if os.path.exists(local_image_path): os.remove(local_image_path)
            del user_temp_image[user_id]
        return

    # ----------------------------------------
    # 【功能 D：智能對話與自然語言生圖決策 (雙大腦 + 雙生圖落入重試)】
    # ----------------------------------------
    try:
        chat_collection.insert_one({"user_id": user_id, "role": "user", "content": user_message, "timestamp": event.timestamp})
        history_cursor = chat_collection.find({"user_id": user_id}).sort("timestamp", -1).limit(6)
        recent_history = list(history_cursor)[::-1]
    except Exception as db_err:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"💽 資料庫連線異常。"))
        return

    ai_reply = ""
    engine_used = "Gemini"
    
    try:
        conversation_context = f"{GLOBAL_SYSTEM_PROMPT}\n\n【近期歷史對話紀錄】\n"
        for msg in recent_history:
            sender = "User" if msg["role"] == "user" else "AI"
            conversation_context += f"{sender}: {msg['content']}\n"
        conversation_context += f"\nUser: {user_message}\nAI:"
        
        response = gemini_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=conversation_context
        )
        ai_reply = response.text
        
    except Exception as api_err:
        last_error = str(api_err)
        if "429" in last_error or "quota" in last_error.lower():
            try:
                engine_used = "Qwen 備用大腦"
                qwen_messages = [{"role": "system", "content": GLOBAL_SYSTEM_PROMPT}]
                for msg in recent_history:
                    qwen_messages.append({"role": msg["role"], "content": msg["content"]})
                
                qwen_response = backup_chat_client.chat_completion(messages=qwen_messages, max_tokens=800, temperature=0.7)
                ai_reply = qwen_response.choices[0].message.content
            except Exception as qwen_err:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ 主副大腦皆無法連線，請等待 1 分鐘後再試。"))
                return
        elif "400" in last_error or "safety" in last_error.lower():
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 觸發 Google 安全審查機制，請輸入「清除記憶」後再試一次！"))
            return
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ 伺服器異常: {last_error[:50]}"))
            return

    # 🌟 核心改進：智能解析 JSON 並執行高品質生圖與免費生圖熔斷切換
    if "__JSON_START__" in ai_reply:
        try:
            match = re.search(r'__JSON_START__(.*?)__JSON_END__', ai_reply, re.DOTALL)
            if match:
                tool_json = json.loads(match.group(1).strip())
                english_prompt = tool_json.get("prompt", "")
                selected_engine = tool_json.get("engine", "safe")
                
                mode_name = "標準模式" if selected_engine == "safe" else "未審查模式"
                prefix_msg = "" if engine_used == "Gemini" else "🔄 [已自動切換備用大腦]\n"
                
                # 優先嘗試原廠高品質生圖
                try:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"{prefix_msg}🎨 啟動原廠高品質 [{mode_name}] 引擎繪製中..."))
                    if selected_engine == "uncensored":
                        image = uncensored_image_client.text_to_image(english_prompt)
                    else:
                        image = safe_image_client.text_to_image(english_prompt)

                    image_name = f"{uuid.uuid4().hex}.jpg"
                    image_path = os.path.join("static", image_name)
                    image.save(image_path)
                    
                    image_url = f"{SPACE_URL}/static/{image_name}"
                    line_bot_api.push_message(user_id, ImageSendMessage(original_content_url=image_url, preview_image_url=image_url))
                    
                    memory_text = f"[AI 使用 {selected_engine} 高品質原廠引擎畫圖成功。提示詞: {english_prompt}]"
                    chat_collection.insert_one({"user_id": user_id, "role": "assistant", "content": memory_text, "timestamp": event.timestamp + 1})
                    
                except Exception as tool_hf_err:
                    print(f"智能路由原廠生圖額度用盡，自動降級切換免費引擎: {tool_hf_err}")
                    # 如果原廠額度用盡 (402/503)，立刻無痛切換至免費 Pollinations 引擎
                    line_bot_api.push_message(user_id, TextSendMessage(text="🔄 [原廠生圖額度耗盡] 正在幫您切換免費無限引擎備援出圖..."))
                    
                    if selected_engine == "uncensored":
                        english_prompt += ", highly detailed, uncensored, no limits"

                    encoded_prompt = urllib.parse.quote(english_prompt)
                    image_api_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true"
                    
                    img_response = requests.get(image_api_url, timeout=30)
                    if img_response.status_code == 200:
                        image = Image.open(io.BytesIO(img_response.content))
                        if image.mode != 'RGB': image = image.convert('RGB')
                            
                        image_name = f"{uuid.uuid4().hex}.jpg"
                        image_path = os.path.join("static", image_name)
                        image.save(image_path, format="JPEG")
                        
                        image_url = f"{SPACE_URL}/static/{image_name}"
                        line_bot_api.push_message(user_id, ImageSendMessage(original_content_url=image_url, preview_image_url=image_url))
                        
                        memory_text = f"[AI 使用 {selected_engine} 備用免費引擎畫圖成功。提示詞: {english_prompt}]"
                        chat_collection.insert_one({"user_id": user_id, "role": "assistant", "content": memory_text, "timestamp": event.timestamp + 1})
                    else:
                        line_bot_api.push_message(user_id, TextSendMessage(text="❌ 備用生圖伺服器也忙碌中，請換個提示詞試試。"))
                        
        except Exception as tool_err:
            line_bot_api.push_message(user_id, TextSendMessage(text="❌ 繪圖功能轉換失敗，請稍後再試。"))
    else:
        # 一般純對話
        cleaned_reply = clean_line_text(ai_reply)
        if engine_used != "Gemini":
            cleaned_reply = f"🔄 [已自動切換備用大腦]\n{cleaned_reply}"
            
        try:
            chat_collection.insert_one({"user_id": user_id, "role": "assistant", "content": cleaned_reply, "timestamp": event.timestamp + 1})
        except: pass
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=cleaned_reply))

# ==========================================
# 核心模組 2：接收圖片訊息 (暫存本地端)
# ==========================================
@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    user_id = event.source.user_id
    message_id = event.message.id
    try:
        message_content = line_bot_api.get_message_content(message_id)
        image_bytes = b""
        for chunk in message_content.iter_content(): image_bytes += chunk
            
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode != 'RGB': img = img.convert('RGB')
        img.thumbnail((1224, 1224))
        
        local_path = os.path.join("static", f"{uuid.uuid4().hex}.jpg")
        img.save(local_path, format="JPEG", quality=85)
        
        user_temp_image[user_id] = local_path
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🖼️ 圖片已暫存！\n請直接輸入你想對這張圖做什麼（如：解題），若傳錯請輸入「取消」。"))
    except Exception as e:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ 圖片接收失敗。"))

# ==========================================
# 核心模組 3：處理檔案訊息 (雙大腦 PDF 備援機制)
# ==========================================
@handler.add(MessageEvent, message=FileMessage)
def handle_file_message(event):
    user_id = event.source.user_id
    message_id = event.message.id
    file_name = event.message.file_name
    
    if not file_name.lower().endswith('.pdf'):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 目前只支援 PDF 檔案喔！"))
        return
        
    local_pdf_path = os.path.join("static", f"{uuid.uuid4().hex}.pdf")
    try:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"📄 正在上傳並閱讀 {file_name}...\n請稍候片刻。"))
        
        message_content = line_bot_api.get_message_content(message_id)
        with open(local_pdf_path, "wb") as f:
            for chunk in message_content.iter_content(): f.write(chunk)
                
        prompt = "請幫我全面分析並總結這份 PDF 文件內容，並用繁體中文條理分明地回覆。\n⚠️ 注意：若內容涉及數學公式或計算，絕對不要使用 LaTeX 語法，一律改用純文字與一般鍵盤符號。"
        ai_reply = ""
        engine_used = "Gemini"

        try:
            uploaded_pdf = gemini_client.files.upload(file=local_pdf_path)
            response = gemini_client.models.generate_content(model='gemini-2.5-flash', contents=[uploaded_pdf, prompt])
            ai_reply = response.text
        except Exception as e:
            if "429" in str(e) or "quota" in str(e).lower():
                engine_used = "Qwen 備用大腦"
                reader = PdfReader(local_pdf_path)
                extracted_text = ""
                for page in reader.pages:
                    if page.extract_text(): extracted_text += page.extract_text() + "\n"
                
                if not extracted_text.strip():
                    raise ValueError("備用引擎無法從此 PDF (可能為純圖片掃描) 提取文字，請等待 1 分鐘後使用 Gemini 再試。")
                
                if len(extracted_text) > 3000: extracted_text = extracted_text[:3000] + "\n...(文章過長已截斷)..."
                
                fallback_messages = [{"role": "user", "content": f"{prompt}\n\n文件內容：\n{extracted_text}"}]
                try:
                    qwen_response = backup_chat_client.chat_completion(messages=fallback_messages, max_tokens=1000, temperature=0.5)
                    ai_reply = qwen_response.choices[0].message.content
                except Exception as qwen_err:
                    raise ValueError("雙大腦皆滿載，無法解析文件文字。")
            else:
                raise e

        cleaned_reply = clean_line_text(ai_reply)
        if len(cleaned_reply) > 4900: cleaned_reply = cleaned_reply[:4900] + "\n...(文章過長已截斷)..."
        
        if engine_used != "Gemini":
            cleaned_reply = f"🔄 [已自動切換備用大腦]\n{cleaned_reply}"

        line_bot_api.push_message(user_id, TextSendMessage(text=f"📊 【PDF 分析結果】\n\n{cleaned_reply}"))
        
        chat_collection.insert_one({"user_id": user_id, "role": "user", "content": f"[上傳 PDF: {file_name}]", "timestamp": event.timestamp})
        chat_collection.insert_one({"user_id": user_id, "role": "assistant", "content": cleaned_reply, "timestamp": event.timestamp + 1})
    except Exception as e:
        line_bot_api.push_message(user_id, TextSendMessage(text=f"❌ PDF 解析失敗: {str(e)[:100]}"))
    finally:
        if os.path.exists(local_pdf_path): os.remove(local_pdf_path)