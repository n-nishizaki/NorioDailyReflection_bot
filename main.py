import os
import json
import asyncio
from datetime import datetime
from dotenv import load_dotenv

import anthropic
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from fastapi import FastAPI, Request
import uvicorn

load_dotenv()

# --- 設定 ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")

# --- Google Sheets 初期化 ---
def get_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds_json = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID).sheet1

# --- Claude で構造化 ---
def structure_with_claude(text: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{
            "role": "user",
            "content": f"""以下の振り返りテキストを構造化してください。
必ずJSON形式のみで返答し、説明文は不要です。

{{
  "category": ["副業", "転職", "回復"] のうち該当するもの（複数可）,
  "actions": ["実施したこと"],
  "outcomes": ["結果・気づき"],
  "blockers": ["障害・ペンディング（なければ空リスト）"],
  "next_actions": ["次のアクション（なければ空リスト）"],
  "energy_level": 1〜5の数値
}}

振り返りテキスト:
{text}"""
        }]
    )
    raw = response.content[0].text
    # JSON部分だけ抽出
    start = raw.find("{")
    end = raw.rfind("}") + 1
    return json.loads(raw[start:end])

# --- Sheets に書き込み ---
def append_to_sheet(raw_text: str, structured: dict):
    sheet = get_sheet()
    
    # ヘッダーがなければ追加
    if sheet.row_count == 0 or sheet.cell(1, 1).value != "日付":
        sheet.insert_row([
            "日付", "カテゴリ", "実施したこと", "結果・気づき",
            "障害・ペンディング", "次のアクション", "エネルギー", "原文"
        ], 1)
    
    sheet.append_row([
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        ", ".join(structured.get("category", [])),
        ", ".join(structured.get("actions", [])),
        ", ".join(structured.get("outcomes", [])),
        ", ".join(structured.get("blockers", [])),
        ", ".join(structured.get("next_actions", [])),
        structured.get("energy_level", ""),
        raw_text
    ])

# --- Telegram ハンドラ ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    await update.message.reply_text("受け取りました。処理中...")
    
    try:
        structured = structure_with_claude(text)
        append_to_sheet(text, structured)
        
        reply = f"""✅ 記録しました

📂 カテゴリ: {', '.join(structured.get('category', []))}
✅ 実施: {', '.join(structured.get('actions', []))}
💡 気づき: {', '.join(structured.get('outcomes', []))}
⚡ エネルギー: {structured.get('energy_level', '-')}/5"""

        if structured.get("blockers"):
            reply += f"\n🔴 ペンディング: {', '.join(structured['blockers'])}"
        if structured.get("next_actions"):
            reply += f"\n➡️ 次のアクション: {', '.join(structured['next_actions'])}"

        await update.message.reply_text(reply)
    
    except Exception as e:
        await update.message.reply_text(f"エラーが発生しました: {str(e)}")

# --- FastAPI + Telegram 起動 ---
app = FastAPI()
telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

@app.on_event("startup")
async def startup():
    await telegram_app.initialize()
    await telegram_app.start()

@app.on_event("shutdown")
async def shutdown():
    await telegram_app.stop()

@app.post(f"/webhook/{TELEGRAM_TOKEN}")
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}

@app.get("/")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
