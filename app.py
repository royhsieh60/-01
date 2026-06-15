import os
import uuid
import io
import time
import json
import re
import urllib.parse
import requests
import pymongo
import threading
import concurrent.futures
import ssl
import certifi
import base64
import traceback
from PIL import Image
from pypdf import PdfReader
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, ImageMessage, FileMessage, 
    TextSendMessage, ImageSendMessage, VideoSendMessage, QuickReply, QuickReplyButton, 
    MessageAction, TemplateSendMessage, ButtonsTemplate, CarouselTemplate, CarouselColumn
)
from huggingface_hub import InferenceClient
from google import genai
from docx import Document
from docx.shared import Inches

# ==========================================
# 👑 雙重 SSL 裝甲
# ==========================================
os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
try:
    _create_unverified_https_context = ssl._create_unverified_context
except AttributeError: pass
else: ssl._create_default_https_context = _create_unverified_https_context

# ==========================================
# 基礎設定與環境變數
# ==========================================
os.makedirs("static", exist_ok=True)
app = FastAPI()

@app.get("/static/{filename}")
async def serve_file(filename: str):
    file_path = os.path.join("static", filename)
    if os.path.exists(file_path):
        return FileResponse(file_path)
    raise HTTPException(status_code=404, detail="File not found")

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "").strip()
SPACE_URL = os.environ.get("SPACE_URL", "").strip()
MONGO_URI = os.environ.get("MONGO_URI", "").strip()

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ==========================================
# 👑 Gemini 與 HF 無限輪盤系統 (自動切換 Key)
# ==========================================
raw_gemini = os.environ.get("GEMINI_API_KEY", "").replace("\n", ",").replace("\r", ",")
GEMINI_KEYS = [k.strip().strip('"').strip("'") for k in raw_gemini.split(",") if k.strip()]
if not GEMINI_KEYS: GEMINI_KEYS = [""]
current_gemini_key_index = 0
gemini_lock = threading.Lock()

def get_gemini_client():
    with gemini_lock:
        return genai.Client(api_key=GEMINI_KEYS[current_gemini_key_index] if GEMINI_KEYS[0] else "dummy_key")

def get_next_gemini_client():
    global current_gemini_key_index
    with gemini_lock:
        current_gemini_key_index = (current_gemini_key_index + 1) % len(GEMINI_KEYS)
        return genai.Client(api_key=GEMINI_KEYS[current_gemini_key_index] if GEMINI_KEYS[0] else "dummy_key")

def gemini_generate_with_retry(contents, max_retries=len(GEMINI_KEYS)*2):
    client = get_gemini_client()
    for attempt in range(max_retries):
        try:
            return client.models.generate_content(model='gemini-2.5-flash', contents=contents).text
        except Exception as e:
            error_str = str(e).lower()
            if "429" in error_str or "quota" in error_str or "exhausted" in error_str:
                if len(GEMINI_KEYS) > 1:
                    client = get_next_gemini_client()
                    continue
                else: raise ValueError(f"Gemini 額度耗盡: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            raise e

raw_hf = os.environ.get("HF_TOKEN", "").replace("\n", ",").replace("\r", ",")
HF_KEYS = [k.strip().strip('"').strip("'") for k in raw_hf.split(",") if k.strip()]
if not HF_KEYS: HF_KEYS = [""]
current_hf_key_index = 0
hf_lock = threading.Lock()

def get_current_hf_key():
    with hf_lock: return HF_KEYS[current_hf_key_index]

def get_next_hf_key():
    global current_hf_key_index
    with hf_lock:
        current_hf_key_index = (current_hf_key_index + 1) % len(HF_KEYS)
        return HF_KEYS[current_hf_key_index]

# ==========================================
# 👑 五大對話引擎總控
# ==========================================
TEXT_ENGINES = {
    "gemini": "Gemini (主核心)",
    "gpt": "GPT (免費備援)",
    "qwen": "Qwen/Qwen2.5-72B-Instruct",
    "llama": "meta-llama/Llama-3.2-3B-Instruct",
    "mistral": "mistralai/Mistral-7B-Instruct-v0.3",
    "merge": "合併雙開 (最強)"
}

def run_hf_model(model_id, messages):
    last_err = ""
    for attempt in range(len(HF_KEYS) * 2):
        current_key = get_current_hf_key()
        try:
            client = InferenceClient(model=model_id, token=current_key, timeout=60)
            res = client.chat_completion(messages=messages, max_tokens=8192, temperature=0.7)
            return res.choices[0].message.content
        except Exception as e:
            err_str = str(e)
            if "402" in err_str or "429" in err_str:
                if len(HF_KEYS) > 1: get_next_hf_key()
            last_err = err_str
    raise ValueError(f"HF 模型執行失敗: {last_err}")

def run_gpt_free(messages):
    url = "https://text.pollinations.ai/"
    payload = {"messages": messages, "model": "openai"}
    headers = {"Content-Type": "application/json"}
    for attempt in range(3):
        try:
            res = requests.post(url, json=payload, headers=headers, timeout=60)
            if res.status_code == 200 and res.text: return res.text
        except: pass
        time.sleep(2)
    raise ValueError("GPT 引擎連線失敗")

try:
    mongo_client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000, tls=True, tlsCAFile=certifi.where(), tlsAllowInvalidCertificates=True)
    db = mongo_client["LineBotDB"]
    chat_collection = db["chat_history"]
    prefs_collection = db["user_prefs"]
except Exception as e:
    print(f"資料庫連線失敗: {e}")

user_temp_image = {}
user_last_prompt = {}

# ==========================================
# 👑 終極工具與完美亂碼淨化器
# ==========================================
def image_to_base64(img_path):
    with open(img_path, "rb") as f: return base64.b64encode(f.read()).decode('utf-8')

def base64_to_image(b64_str):
    return Image.open(io.BytesIO(base64.b64decode(b64_str)))

def show_loading_animation(user_id):
    try:
        requests.post("https://api.line.me/v2/bot/chat/loading/start", 
                      headers={"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}, 
                      json={"chatId": user_id, "loadingSeconds": 20})
    except: pass

def clean_math_text(text: str) -> str:
    """核彈級純文字數學淨化器：把所有 LaTeX 亂碼轉成完美的純文字排版"""
    if not text: return ""
    def parse_matrix(match):
        content = match.group(1)
        rows = content.split(r'\\')
        out = []
        for r in rows:
            cols = [c.strip() for c in r.split('&')]
            out.append("[ " + " | ".join(cols) + " ]")
        return "\n" + "\n".join(out) + "\n"
    
    text = re.sub(r'\\begin\{[bpBvV]?matrix\}(.*?)\\end\{[bpBvV]?matrix\}', parse_matrix, text, flags=re.DOTALL)
    while True:
        new_text = re.sub(r'\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}', r'(\1)/(\2)', text)
        if new_text == text: break
        text = new_text
        
    replacements = {
        r'\times': '×', r'\cdot': '·', r'\div': '÷', r'\sqrt': '√', r'\pi': 'π', 
        r'\theta': 'θ', r'\pm': '±', r'\infty': '∞', r'\approx': '≈',
        r'\leq': '≤', r'\geq': '≥', r'\neq': '≠', r'\{': '{', r'\}': '}', 
        r'\$': '', r'&': ' ', r'\boldsymbol': '', r'\mathbf': '',
        r'\left': '', r'\right': '', r'\text': '', r'\quad': ' ', 
        r'\[': '', r'\]': '', r'\(': '', r'\)': '', r'\\': '\n'
    }
    for k, v in replacements.items(): text = text.replace(k, v)
    text = re.sub(r'\\[a-zA-Z]+', '', text)
    return text.strip()

def process_inline_math(text: str) -> str:
    """自動尋找行內數學式 \( ... \) 並轉為純文字，確保不會亂入 Word"""
    def replacer(match):
        return clean_math_text(match.group(1))
    text = re.sub(r'\\\((.*?)\\\)', replacer, text, flags=re.DOTALL)
    text = re.sub(r'(?<!\$)\$(?!\$)(.*?)(?<!\$)\$(?!\$)', replacer, text, flags=re.DOTALL)
    return text

def clean_line_text(text: str) -> str:
    if not text: return ""
    text = re.sub(r'#+\s+', '', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    return text.strip()

def clean_history_text(text: str) -> str:
    prefixes = [r"🌟 【雙引擎終極整合解答】\n\n", r"🟢 【(.*?) 獨立解答】(.*?)\n\n", r"🔵 【(.*?) 獨立解答】(.*?)\n\n", r"🔄 \[(.*?)\]\n"]
    for p in prefixes: text = re.sub(p, "", text, flags=re.DOTALL)
    return text.strip()

# ==========================================
# 👑 10大生圖引擎總控 (3重備援防卡死)
# ==========================================
ENGINE_MODELS = {
    "auto": "black-forest-labs/FLUX.1-schnell", "sdxl": "stabilityai/stable-diffusion-xl-base-1.0",
    "anything": "stablediffusionapi/anything-v5", "z-image": "black-forest-labs/FLUX.1-schnell", 
    "flux": "black-forest-labs/FLUX.1-schnell", "anime": "cagliostrolab/animagine-xl-3.1",
    "realism": "SG161222/RealVisXL_V4.0", "3d": "goofyinventor/flux-3d-model-lora",
    "turbo": "stabilityai/sdxl-turbo", "dark": "stablediffusionapi/dark-sushi-mix"
}

ENGINE_LABELS = {
    "auto": "智能", "sdxl": "SDXL(官方)", "anything": "Anything", 
    "z-image": "Z-Image", "flux": "Flux", "anime": "動漫風", 
    "realism": "極致擬真", "3d": "3D模型", "turbo": "極速渲染", "dark": "暗黑風格"
}

def generate_image_and_save(engine, prompt, seed):
    model_id = ENGINE_MODELS.get(engine, ENGINE_MODELS["auto"])
    img = None
    last_err = ""
    
    for attempt in range(len(HF_KEYS) * 2):
        current_key = get_current_hf_key()
        try:
            client = InferenceClient(model=model_id, token=current_key, timeout=30)
            img = client.text_to_image(prompt + f" random seed {seed}")
            break
        except Exception as e:
            if "402" in str(e) or "429" in str(e):
                if len(HF_KEYS) > 1: get_next_hf_key()
            last_err += f"HF_Client: {str(e)[:40]} | "

    if img is None:
        for attempt in range(len(HF_KEYS)):
            current_key = get_current_hf_key()
            try:
                res = requests.post(f"https://api-inference.huggingface.co/models/{model_id}", headers={"Authorization": f"Bearer {current_key}"}, json={"inputs": prompt + f" random seed {seed}"}, timeout=30)
                if res.status_code == 200:
                    img = Image.open(io.BytesIO(res.content))
                    break
                elif res.status_code in [402, 429] and len(HF_KEYS) > 1:
                    get_next_hf_key()
            except Exception as e: last_err += f"HF_POST: {str(e)[:40]} | "

    if img is None:
        try:
            model_mapping = {"flux": "flux", "z-image": "flux-pro", "anime": "anime", "realism": "flux-realism", "3d": "flux-3d", "turbo": "turbo", "dark": "any-dark", "auto": "flux", "sdxl": "flux", "anything": "anime"}
            model_param = model_mapping.get(engine, "flux")
            encoded = urllib.parse.quote(prompt + " highly detailed, masterpiece")
            url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&nologo=true&model={model_param}&seed={seed}"
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"}
            res = requests.get(url, headers=headers, timeout=30)
            if res.status_code == 200 and 'image' in res.headers.get('Content-Type', '').lower():
                img = Image.open(io.BytesIO(res.content))
        except Exception as e: last_err += f"Pollinations: {str(e)[:40]}"

    if img is None: raise ValueError(f"三層生圖皆失敗: {last_err}")
            
    if img.mode != 'RGB': img = img.convert('RGB')
    name = f"{uuid.uuid4().hex}.jpg"
    path = os.path.join("static", name)
    img.save(path, format="JPEG", quality=90)
    return f"{SPACE_URL}/static/{name}"

# ==========================================
# 👑 對話框 UI 介面與防卡死批次發送器
# ==========================================
def get_quick_replies(user_id):
    pref = prefs_collection.find_one({"user_id": user_id}) or {}
    bypass_mode = pref.get("bypass_mode", False)
    lbl_bypass = "⚡直通生圖: 🟢開" if bypass_mode else "⚡直通生圖: 🔴關"
    buttons = [
        QuickReplyButton(action=MessageAction(label="⚙️ 大腦設定", text="⚙️ 大腦設定")),
        QuickReplyButton(action=MessageAction(label="🎨 生圖設定", text="🎨 生圖設定")),
        QuickReplyButton(action=MessageAction(label=lbl_bypass, text="/toggle_bypass")),
        QuickReplyButton(action=MessageAction(label="🧹 清除記憶", text="/clear"))
    ]
    return QuickReply(items=buttons)

def send_text_engine_menu(user_id):
    pref = prefs_collection.find_one({"user_id": user_id}) or {}
    current = pref.get("text_engine", "merge")
    col1 = CarouselColumn(title="👑 主力大腦引擎", text="選擇主要的思考與分析核心", actions=[
        MessageAction(label=f"{'✅ ' if current=='merge' else ''}合併雙開 (最強)", text="/engine merge"),
        MessageAction(label=f"{'✅ ' if current=='gemini' else ''}Gemini (主核心)", text="/engine gemini"),
        MessageAction(label=f"{'✅ ' if current=='gpt' else ''}GPT (免費備援)", text="/engine gpt")
    ])
    col2 = CarouselColumn(title="🤖 開源大腦引擎", text="選擇特定領域的開源模型", actions=[
        MessageAction(label=f"{'✅ ' if current=='qwen' else ''}Qwen-72B", text="/engine qwen"),
        MessageAction(label=f"{'✅ ' if current=='llama' else ''}Llama-3.2", text="/engine llama"),
        MessageAction(label=f"{'✅ ' if current=='mistral' else ''}Mistral-7B", text="/engine mistral")
    ])
    msg = TemplateSendMessage(alt_text="大腦引擎選單", template=CarouselTemplate(columns=[col1, col2]))
    push_messages_in_batches(user_id, [msg])

def send_image_engine_menu(user_id):
    pref = prefs_collection.find_one({"user_id": user_id}) or {}
    current = pref.get("image_engine", "auto")
    col1 = CarouselColumn(title="🎨 生圖引擎 (綜合)", text="強大的綜合繪圖模型", actions=[
        MessageAction(label=f"{'✅ ' if current=='auto' else ''}智能 (Auto)", text="/image_engine auto"),
        MessageAction(label=f"{'✅ ' if current=='flux' else ''}Flux", text="/image_engine flux"),
        MessageAction(label=f"{'✅ ' if current=='sdxl' else ''}SDXL (官方)", text="/image_engine sdxl")
    ])
    col2 = CarouselColumn(title="✨ 生圖引擎 (風格)", text="動漫與真實系繪圖模型", actions=[
        MessageAction(label=f"{'✅ ' if current=='anime' else ''}動漫風 (Anime)", text="/image_engine anime"),
        MessageAction(label=f"{'✅ ' if current=='realism' else ''}極致擬真", text="/image_engine realism"),
        MessageAction(label=f"{'✅ ' if current=='3d' else ''}3D 模型", text="/image_engine 3d")
    ])
    col3 = CarouselColumn(title="🚀 生圖引擎 (特殊)", text="極速與特殊風格模型", actions=[
        MessageAction(label=f"{'✅ ' if current=='turbo' else ''}極速渲染", text="/image_engine turbo"),
        MessageAction(label=f"{'✅ ' if current=='dark' else ''}暗黑風格", text="/image_engine dark"),
        MessageAction(label=f"{'✅ ' if current=='z-image' else ''}Z-Image", text="/image_engine z-image")
    ])
    msg = TemplateSendMessage(alt_text="生圖引擎選單", template=CarouselTemplate(columns=[col1, col2, col3]))
    push_messages_in_batches(user_id, [msg])

def get_batch_template():
    return TemplateSendMessage(
        alt_text="批量生成選單",
        template=ButtonsTemplate(
            text="✨ 圖像已生成！需要批量產出嗎？",
            actions=[
                MessageAction(label="🔄 再來 1 張", text="/batch 1"),
                MessageAction(label="🚀 批量 3 張", text="/batch 3"),
                MessageAction(label="🔥 批量 10 張", text="/batch 10")
            ]
        )
    )

def push_messages_in_batches(user_id, messages):
    """保證無論發生什麼事，按鍵選單都會緊跟著最後一條訊息，絕不消失卡死"""
    if not messages: return
    for i in range(0, len(messages), 5):
        batch = messages[i:i+5]
        if i + 5 >= len(messages): batch[-1].quick_reply = get_quick_replies(user_id)
        line_bot_api.push_message(user_id, batch)

# ==========================================
# 👑 終極 DOCX 解析器：精準比例縮放 + 自動洗白透明底 + 智能降級
# ==========================================
def parse_and_build_content(text, user_id):
    # 1. 預先清除行內數學式 \(...\) 與 $...$，將其轉為純文字，確保 Word 中沒有亂碼
    text = process_inline_math(text)

    doc = Document()
    doc.add_heading('AI 深度解析報告', 0)

    # 2. 攔截需要轉成圖片的大型區塊
    pattern = re.compile(r'(__CHART__.*?__CHART_END__|__GRAPH__.*?__GRAPH_END__|__IMAGE__.*?__IMAGE_END__|\\\[.*?\\\]|\$\$.*?\$\$|\\begin\{[a-zA-Z]*matrix\}.*?\\end\{[a-zA-Z]*matrix\})', re.DOTALL)
    parts = pattern.split(text)

    current_text_block = ""
    for part in parts:
        if not part.strip(): continue
        
        img_url = None
        is_math = False
        raw_content = ""
        
        if part.startswith('__CHART__'):
            raw_content = part.replace('__CHART__', '').replace('__CHART_END__', '').strip()
            img_url = f"https://quickchart.io/chart?c={urllib.parse.quote(raw_content)}"
        elif part.startswith('__GRAPH__'):
            raw_content = part.replace('__GRAPH__', '').replace('__GRAPH_END__', '').strip()
            img_url = f"https://quickchart.io/graphviz?graph={urllib.parse.quote(raw_content)}"
        elif part.startswith('__IMAGE__'):
            raw_content = part.replace('__IMAGE__', '').replace('__IMAGE_END__', '').strip()
            img_url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(raw_content + ' highly detailed')}?width=800&height=600&nologo=true"
        elif part.startswith('\\[') or part.startswith('$$') or part.startswith('\\begin'):
            raw_content = part
            if raw_content.startswith('\\['): raw_content = raw_content[2:-2]
            elif raw_content.startswith('$$'): raw_content = raw_content[2:-2]
            img_url = f"https://latex.codecogs.com/png.image?\\dpi{{300}}\\bg_white\\space {urllib.parse.quote(raw_content)}"
            is_math = True
        else:
            current_text_block += part + "\n"
            continue
        
        if current_text_block.strip():
            cleaned = clean_line_text(current_text_block)
            if cleaned: doc.add_paragraph(cleaned)
            current_text_block = ""
            
        if img_url:
            img_success = False
            try:
                res = requests.get(img_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
                if res.status_code == 200 and 'image' in res.headers.get('Content-Type', '').lower():
                    temp_img_path = f"static/temp_{uuid.uuid4().hex}.jpg"
                    with open(temp_img_path, 'wb') as f: f.write(res.content)
                    
                    try:
                        target_width = None
                        with Image.open(temp_img_path) as tmp_img:
                            # 👑 白底防護網
                            if tmp_img.mode in ('RGBA', 'LA') or (tmp_img.mode == 'P' and 'transparency' in tmp_img.info):
                                alpha = tmp_img.convert('RGBA').split()[-1]
                                bg = Image.new("RGB", tmp_img.size, (255, 255, 255))
                                bg.paste(tmp_img, mask=alpha)
                                bg.save(temp_img_path, 'JPEG', quality=95)
                            else:
                                tmp_img.convert('RGB').save(temp_img_path, 'JPEG', quality=95)
                            
                            # 👑 精準縮放演算法：依據 300 DPI 自動推算原始尺寸，絕不忽大忽小！
                            w, h = Image.open(temp_img_path).size
                            calc_width = w / 300.0
                            if calc_width > 6.0: target_width = Inches(6.0) # 最大不超過 A4 紙寬
                            elif calc_width < 0.5: target_width = Inches(0.5) # 最小不低於 0.5 吋
                            else: target_width = Inches(calc_width)

                        doc.add_picture(temp_img_path, width=target_width)
                        img_success = True
                    except Exception: pass
                    finally:
                        if os.path.exists(temp_img_path): os.remove(temp_img_path)
            except: pass

            # 👑 如果圖片因為任何原因失敗（例如數學太長、伺服器掛掉），保底為乾淨純文字，絕對不顯示全白或亂碼！
            if not img_success:
                fallback_txt = clean_math_text(raw_content) if is_math else f"[{raw_content}]"
                if fallback_txt: doc.add_paragraph(fallback_txt)

    if current_text_block.strip():
        cleaned = clean_line_text(current_text_block)
        if cleaned: doc.add_paragraph(cleaned)

    doc_name = f"AI_Report_{uuid.uuid4().hex[:8]}.docx"
    doc_path = os.path.join("static", doc_name)
    doc.save(doc_path)
    doc_url = f"{SPACE_URL}/static/{doc_name}?openExternalBrowser=1"
    
    # 👑 改善體驗：如果生了 DOCX，就不會在聊天室倒垃圾，只傳一句總結網址
    return [TextSendMessage(text=f"📝 解析結果較長或包含圖表，已封裝為 Word (.docx) 檔供您下載：\n{doc_url}")], doc_url

# ==========================================
# 👑 核心 System Prompt
# ==========================================
GLOBAL_SYSTEM_PROMPT = r"""你是一個極度聰明、邏輯嚴密的 AI 助理，請使用繁體中文回答。

【超強視覺化與圖表指令】
若需要展示數據、流程圖、上網找圖，或複雜數學大矩陣，【絕對必須】使用以下標籤包裝，系統會自動在背景將其渲染為高畫質實體圖片，並排版進 Word 文檔！

1. 【大型數學與矩陣】(例如多層矩陣、大片微積分)：
   必須使用區塊語法，例如 \[ ... \] 或 \begin{bmatrix} ... \end{bmatrix}。系統會自動抓取並渲染圖片。
   但若是【行內簡單算式】(如 a^2+b^2=c^2，分數 1/2)，請直接使用純文字，不要用 LaTeX 渲染。

2. 【圓餅圖/長條圖/折線圖】：
   __CHART__ {"type":"bar","data":{"labels":["A","B"],"datasets":[{"label":"Data","data":[10,20]}]}} __CHART_END__

3. 【流程圖/結構圖】：
   __GRAPH__ digraph G { A -> B; B -> C; } __GRAPH_END__

4. 【上網搜尋/相關插圖】：
   __IMAGE__ 蘋果電腦的產品圖 __IMAGE_END__

【最高記憶指令】
你具有完整的長期記憶！仔細閱讀【近期歷史】中用戶上傳過的圖片或對話紀錄並回答，絕對不要回答沒有記憶！

🌟 視覺工具呼叫判斷：
1. 提到「影片、動態、短片」，【必須】輸出 JSON 呼叫影片生成 (tool: "video_generator")。
2. 提到「圖片、照片、畫圖」，【必須】輸出 JSON 呼叫圖片生成 (tool: "image_generator")。
⚠️ JSON 格式 (嚴格獨立輸出)：
__JSON_START__
{"tool": "image_generator", "engine": "safe", "prompt": "[詳細 English Prompt]"}
__JSON_END__"""

# ==========================================
# FastAPI 路由與 Webhook 接收
# ==========================================
@app.get("/")
def read_root(): return {"status": "Ultimate LINE Engine (No More Bugs) is Running!"}

@app.post("/")
async def callback(request: Request, background_tasks: BackgroundTasks):
    signature = request.headers.get("X-Line-Signature")
    body = await request.body()
    try: background_tasks.add_task(handler.handle, body.decode("utf-8"), signature)
    except InvalidSignatureError: raise HTTPException(status_code=400, detail="Invalid signature")
    return "OK"

# ==========================================
# 核心模組 1：處理文字訊息
# ==========================================
def process_message_background(user_id, user_message, event_timestamp):
    messages_to_send = []
    try:
        if user_message == "⚙️ 大腦設定":
            send_text_engine_menu(user_id)
            return
        if user_message == "🎨 生圖設定":
            send_image_engine_menu(user_id)
            return

        if user_message in ["/clear", "清除記憶"]:
            chat_collection.delete_many({"user_id": user_id})
            push_messages_in_batches(user_id, [TextSendMessage(text="🧹 記憶已徹底清除！")])
            return

        if user_message == "/toggle_bypass":
            pref = prefs_collection.find_one({"user_id": user_id}) or {}
            new_state = not pref.get("bypass_mode", False)
            prefs_collection.update_one({"user_id": user_id}, {"$set": {"bypass_mode": new_state}}, upsert=True)
            status = "🟢開啟" if new_state else "🔴關閉"
            push_messages_in_batches(user_id, [TextSendMessage(text=f"⚡ 直通模式已 {status}！")])
            return

        txt_match = re.match(r'^/engine\s+(gemini|gpt|qwen|llama|mistral|merge)', user_message, re.IGNORECASE)
        if txt_match:
            target = txt_match.group(1).lower()
            prefs_collection.update_one({"user_id": user_id}, {"$set": {"text_engine": target, "bypass_mode": False}}, upsert=True)
            push_messages_in_batches(user_id, [TextSendMessage(text=f"⚙️ 思考大腦已切換為：{TEXT_ENGINES[target]}")])
            return

        img_match = re.match(r'^/image_engine\s+(auto|sdxl|anything|z-image|flux|anime|realism|3d|turbo|dark)', user_message, re.IGNORECASE)
        if img_match:
            target = img_match.group(1).lower()
            prefs_collection.update_one({"user_id": user_id}, {"$set": {"image_engine": target}}, upsert=True)
            push_messages_in_batches(user_id, [TextSendMessage(text=f"⚙️ 生圖引擎已鎖定為：{ENGINE_LABELS.get(target, target)}")])
            return

        batch_match = re.match(r'^/batch\s+(\d+)', user_message, re.IGNORECASE)
        if batch_match:
            count = min(int(batch_match.group(1)), 10)
            last_data = user_last_prompt.get(user_id)
            if not last_data:
                push_messages_in_batches(user_id, [TextSendMessage(text="❌ 找不到靈感記憶，請重新叫我畫一張。")])
                return
                
            english_prompt = last_data["prompt"]
            selected_engine = last_data["engine"]
            
            for i in range(count):
                try:
                    seed = uuid.uuid4().hex[:6]
                    image_url = generate_image_and_save(selected_engine, english_prompt, seed)
                    messages_to_send.append(ImageSendMessage(original_content_url=image_url, preview_image_url=image_url))
                except Exception as e: 
                    messages_to_send.append(TextSendMessage(text=f"❌ 生圖失敗 [RAW LOG]: {repr(e)}"))
            messages_to_send.append(get_batch_template())
            push_messages_in_batches(user_id, messages_to_send)
            return

        pref = prefs_collection.find_one({"user_id": user_id}) or {}
        current_text_engine = pref.get("text_engine", "merge")
        current_img_engine = pref.get("image_engine", "auto")
        bypass_mode = pref.get("bypass_mode", False)

        if bypass_mode:
            draw_tool = "video_generator" if any(k in user_message.lower() for k in ["影片", "動態", "短片", "動畫"]) else "image_generator"
            ai_reply = f"__JSON_START__\n{{\"tool\": \"{draw_tool}\", \"engine\": \"{current_img_engine}\", \"prompt\": \"{user_message}\"}}\n__JSON_END__"
            engine_used = "Bypass (無腦直通)"
        else:
            chat_collection.insert_one({"user_id": user_id, "role": "user", "content": user_message, "timestamp": event_timestamp})
            history_cursor = chat_collection.find({"user_id": user_id}).sort("timestamp", -1).limit(30)
            recent_history = list(history_cursor)[::-1]

            ai_reply = ""
            engine_used = current_text_engine
            
            hf_messages = [{"role": "system", "content": GLOBAL_SYSTEM_PROMPT}]
            gemini_contents = [GLOBAL_SYSTEM_PROMPT]
            
            for msg in recent_history:
                clean_hist = clean_history_text(msg['content'])
                hf_messages.append({"role": msg["role"], "content": clean_hist[:1500]})
                gemini_contents.append(f"{'User' if msg['role']=='user' else 'AI'}: {clean_hist}")
                
            try:
                if current_text_engine == "merge":
                    def run_gpt_merge(): return run_gpt_free(hf_messages)
                    def run_gemini_merge(): return gemini_generate_with_retry(gemini_contents)

                    with concurrent.futures.ThreadPoolExecutor() as executor:
                        fg = executor.submit(run_gemini_merge)
                        fgpt = executor.submit(run_gpt_merge)
                        try: gr = fg.result(timeout=40)
                        except Exception as e: gr = f"[Gemini 失敗: {str(e)}]"
                        try: gptr = fgpt.result(timeout=45)
                        except Exception as e: gptr = f"[GPT 失敗: {str(e)}]"
                    
                    merge_prompt = f"草稿一：\n{gr}\n\n草稿二：\n{gptr}\n請完美整合以上解答。若有 __MATH__, __CHART__ 標籤請原封不動保留！"
                    try: ai_reply = f"🌟 【雙引擎終極整合解答】\n\n{gemini_generate_with_retry([merge_prompt])}"
                    except: ai_reply = f"⚠️ 整合過載，提供 GPT 備援：\n\n{gptr}"

                elif current_text_engine == "gpt": ai_reply = run_gpt_free(hf_messages)
                elif current_text_engine in ["qwen", "llama", "mistral"]: ai_reply = run_hf_model(TEXT_ENGINES[current_text_engine], hf_messages)
                else: ai_reply = gemini_generate_with_retry(gemini_contents)

            except Exception as api_err:
                raw_err = traceback.format_exc()
                push_messages_in_batches(user_id, [TextSendMessage(text=f"❌ 解析失敗 [RAW LOG]: {repr(api_err)}\n\nTraceback:\n{raw_err[-500:]}")])
                return

        if "__JSON_START__" in ai_reply:
            try:
                match = re.search(r'__JSON_START__(.*?)__JSON_END__', ai_reply, re.DOTALL)
                if match:
                    tool_json = json.loads(match.group(1).strip())
                    if "video" in tool_json.get("tool", ""):
                        # 處理影片... (略，與上一版相同)
                        messages_to_send.append(TextSendMessage(text="✨ 真影片功能開發中..."))
                    else:
                        img_url = generate_image_and_save(tool_json.get("engine", "auto"), tool_json.get("prompt"), uuid.uuid4().hex[:6])
                        messages_to_send.append(ImageSendMessage(original_content_url=img_url, preview_image_url=img_url))
            except Exception as e:
                messages_to_send.append(TextSendMessage(text=f"❌ 工具執行失敗 [RAW LOG]:\n{traceback.format_exc()[-500:]}"))
            
            messages_to_send.append(get_batch_template())
            push_messages_in_batches(user_id, messages_to_send)
        else:
            if current_text_engine != engine_used and current_text_engine != "merge": ai_reply = f"🔄 [{TEXT_ENGINES[engine_used]}]\n{ai_reply}"
            try: chat_collection.insert_one({"user_id": user_id, "role": "assistant", "content": ai_reply, "timestamp": event_timestamp + 1})
            except: pass

            needs_docx = len(ai_reply) > 800 or any(tag in ai_reply for tag in ['__CHART__', '__GRAPH__', '__IMAGE__', '__MATH__', '\\[', '\\begin'])
            if needs_docx:
                line_msgs, doc_url = parse_and_build_content(ai_reply, user_id)
                push_messages_in_batches(user_id, line_msgs)
            else:
                ai_reply = clean_line_text(process_inline_math(ai_reply))
                push_messages_in_batches(user_id, [TextSendMessage(text=ai_reply)])
                
    except Exception as fatal_e:
        push_messages_in_batches(user_id, [TextSendMessage(text=f"❌ 核心運算崩潰 [RAW LOG]:\n{traceback.format_exc()[-800:]}")])

@handler.add(MessageEvent, message=TextMessage)
def handle_text_event(event):
    show_loading_animation(user_id=event.source.user_id)
    threading.Thread(target=process_message_background, args=(event.source.user_id, event.message.text.strip(), event.timestamp)).start()

# ==========================================
# 核心模組 2：接收圖片訊息
# ==========================================
@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    user_id = event.source.user_id
    show_loading_animation(user_id)
    try:
        message_content = line_bot_api.get_message_content(event.message.id)
        image_bytes = b""
        for chunk in message_content.iter_content(): image_bytes += chunk
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode != 'RGB': img = img.convert('RGB')
        img.thumbnail((2048, 2048)) 
        path = os.path.join("static", f"{uuid.uuid4().hex}.jpg")
        img.save(path, format="JPEG", quality=95)
        
        try:
            try:
                reply_text = gemini_generate_with_retry(contents=[img, "請詳細分析這張圖片的內容，如果是題目請給出詳解。"])
            except ValueError as e:
                if "429_QUOTA_EXHAUSTED" in str(e):
                    reply_text = "⚠️ Gemini API 額度耗盡 (Google 規定每分鐘 15 次限制)。GPT 備援引擎無法看圖，請等待 1 分鐘後再試！"
                else: raise e
            
            needs_docx = len(reply_text) > 800 or any(tag in reply_text for tag in ['__MATH__', '__CHART__', '__GRAPH__', '__SEARCH__', '\\[', '\\begin'])
            if needs_docx:
                line_messages, doc_url = parse_and_build_content(reply_text, user_id)
                push_messages_in_batches(user_id, line_messages)
            else:
                push_messages_in_batches(user_id, [TextSendMessage(text=clean_line_text(process_inline_math(reply_text)))])
            
            b64_img = image_to_base64(path)
            chat_collection.insert_one({"user_id": user_id, "role": "user", "content": f"[上傳了圖片]", "image_b64": b64_img, "timestamp": event.timestamp})
            chat_collection.insert_one({"user_id": user_id, "role": "assistant", "content": reply_text, "timestamp": event.timestamp+1})
        except Exception as api_e:
            push_messages_in_batches(user_id, [TextSendMessage(text=f"❌ 圖片分析失敗 [RAW LOG]:\n{traceback.format_exc()[-800:]}")])
        finally:
            if os.path.exists(path): os.remove(path)
    except Exception as e:
        push_messages_in_batches(user_id, [TextSendMessage(text=f"❌ 接收圖片失敗 [RAW LOG]:\n{traceback.format_exc()[-800:]}")])

# ==========================================
# 核心模組 3：處理檔案訊息 (直接餵給 AI，絕對不超時)
# ==========================================
def process_file_background(user_id, file_name, message_id, event_timestamp):
    local_pdf_path = os.path.join("static", f"{uuid.uuid4().hex}.pdf")
    try:
        message_content = line_bot_api.get_message_content(message_id)
        with open(local_pdf_path, "wb") as f:
            for chunk in message_content.iter_content(): f.write(chunk)
        
        reader = PdfReader(local_pdf_path)
        ext_txt = "".join([p.extract_text() or "" for p in reader.pages])
                
        prompt = "請幫我全面分析這份 PDF，用繁體中文回覆。\n⚠️ 若有數學式，請使用 LaTeX 區塊包裝在 __MATH__ 標籤內以便渲染。"
        ai_reply = ""
        pref = prefs_collection.find_one({"user_id": user_id}) or {}
        current_text_engine = pref.get("text_engine", "merge")

        try:
            if current_text_engine == "merge":
                def run_gpt_pdf(): return run_gpt_free([{"role": "user", "content": f"PDF內容：\n{ext_txt[:6000]}\n\n回答：{prompt}"}])
                def run_gemini_pdf(): return gemini_generate_with_retry([f"PDF內容：\n{ext_txt[:20000]}\n\n回答：{prompt}"])
                
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    fg = executor.submit(run_gemini_pdf)
                    fgpt = executor.submit(run_gpt_pdf)
                    try: gr = fg.result(timeout=60)
                    except Exception as e: gr = f"[Gemini 錯誤: {str(e)}]"
                    try: gptr = fgpt.result(timeout=45)
                    except Exception as e: gptr = f"[GPT 錯誤: {str(e)}]"
                
                merge_prompt = f"草稿一：\n{gr}\n\n草稿二：\n{gptr}\n請完美整合。若有 __MATH__ 標籤請原封不動保留！"
                try: ai_reply = f"🌟 【雙引擎終極整合解答】\n\n{gemini_generate_with_retry([merge_prompt])}"
                except: ai_reply = f"⚠️ 整合過載，提供 GPT 原始解析：\n\n{gptr}"

            elif current_text_engine == "gpt": ai_reply = run_gpt_free([{"role": "user", "content": f"PDF內容：\n{ext_txt[:6000]}\n\n回答：{prompt}"}])
            elif current_text_engine in ["qwen", "llama", "mistral"]: ai_reply = run_hf_model(TEXT_ENGINES[current_text_engine], [{"role": "user", "content": f"PDF內容：\n{ext_txt[:6000]}\n\n回答：{prompt}"}])
            else: ai_reply = gemini_generate_with_retry([f"PDF內容：\n{ext_txt[:20000]}\n\n回答：{prompt}"])

        except Exception as e:
            push_messages_in_batches(user_id, [TextSendMessage(text=f"❌ PDF 解析失敗 [RAW LOG]:\n{traceback.format_exc()[-500:]}")])
            return

        try:
            chat_collection.insert_one({"user_id": user_id, "role": "user", "content": f"[上傳 PDF]: {file_name}", "timestamp": event_timestamp})
            chat_collection.insert_one({"user_id": user_id, "role": "assistant", "content": ai_reply, "timestamp": event_timestamp + 1})
        except: pass

        line_messages, doc_url = parse_and_build_content(ai_reply, user_id)
        push_messages_in_batches(user_id, line_messages)
        
    except Exception as fatal_e:
        push_messages_in_batches(user_id, [TextSendMessage(text=f"❌ 處理 PDF 檔案崩潰 [RAW LOG]:\n{traceback.format_exc()[-800:]}")])

@handler.add(MessageEvent, message=FileMessage)
def handle_file_event(event):
    user_id = event.source.user_id
    file_name = event.message.file_name
    if not file_name.lower().endswith('.pdf'):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 目前只支援 PDF 檔案喔！", quick_reply=get_quick_replies(user_id)))
        return
    show_loading_animation(user_id)
    threading.Thread(target=process_file_background, args=(user_id, file_name, event.message.id, event.timestamp)).start()