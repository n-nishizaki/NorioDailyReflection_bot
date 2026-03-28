import os
import json
from datetime import datetime
from dotenv import load_dotenv
from contextlib import asynccontextmanager

import anthropic
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from fastapi import FastAPI, Request

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")

# --- セッション管理（メモリ） ---
sessions = {}

# --- Google Sheets 初期化 ---
def get_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds_json = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID).sheet1

# --- Sheets に書き込み ---
def append_to_sheet(structured: dict):
    sheet = get_sheet()
    if sheet.row_count == 0 or sheet.cell(1, 1).value != "日付":
        sheet.insert_row([
            "日付", "カテゴリ", "実施したこと", "結果・気づき",
            "障害・ペンディング", "次のアクション", "エネルギー", "パターン仮説"
        ], 1)
    sheet.append_row([
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        ", ".join(structured.get("category", [])),
        ", ".join(structured.get("actions", [])),
        ", ".join(structured.get("outcomes", [])),
        ", ".join(structured.get("blockers", [])),
        ", ".join(structured.get("next_actions", [])),
        structured.get("energy_level", ""),
        structured.get("pattern", "")
    ])

# --- Claude API呼び出し ---
def call_claude(messages: list) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system="""あなたは日々の振り返りをサポートするコーチです。
ユーザーが「今日の振り返り」と言ったら、以下の5ステップで順番にヒアリングしてください。

ステップ1: 今日のエネルギーレベルを1〜5で教えてもらう
ステップ2: 今日やったことを聞く（副業・転職活動・回復、何でも）
ステップ3: 手応えがあったこと・なかったことを聞く
ステップ4: 引っかかっていること・明日に持ち越すことを聞く
ステップ5: ステップ1〜4を踏まえて、今日の行動パターンをClaudeが仮説として一言で返し、ユーザーに確認する

【重要なルール】
- 1回のメッセージで1つの質問のみ
- ステップを飛ばさない
- ステップ5が終わったら「記録します」と言い、JSON形式で以下を出力する（他の文章は不要）:

SAVE_DATA:
{
  "category": ["副業", "転職", "回復"] のうち該当するもの,
  "actions": ["実施したこと"],
  "outcomes": ["結果・気づき"],
  "blockers": ["ペンディング（なければ空リスト）"],
  "next_actions": ["次のアクション（なければ空リスト）"],
  "energy_level": 数値,
  "pattern": "今日のパターン仮説"
}""",
        messages=messages
    )
    return response.content[0].text

# --- Telegram ハンドラ ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    # セッション初期化
    if user_id not in sessions:
        sessions[user_id] = []

    # 「今日の振り返り」でセッションリセット
    if "今日の振り返り" in text:
        sessions[user_id] = []

    # 会話履歴に追加
    sessions[user_id].append({"role": "user", "content": text})

    try:
        reply = call_claude(sessions[user_id])

        # SAVE_DATAが含まれていたら保存処理
        if "SAVE_DATA:" in reply:
            # ユーザーへの返信部分とJSON部分を分離
            parts = reply.split("SAVE_DATA:")
            user_reply = parts[0].strip()
            json_str = parts[1].strip()

            # JSON保存
            start = json_str.find("{")
            end = json_str.rfind("}") + 1
            structured = json.loads(json_str[start:end])
            append_to_sheet(structured)

            # セッションリセット
            sessions[user_id] = []

            await update.message.reply_text(user_reply if user_reply else "記録しました！お疲れさまでした。")
            await update.message.reply_text("✅ Google Sheetsに保存しました。また明日！")
        else:
            # 会話継続
            sessions[user_id].append({"role": "assistant", "content": reply})
            await update.message.reply_text(reply)

    except Exception as e:
        await update.message.reply_text(f"エラーが発生しました: {str(e)}")

# --- Application初期化 ---
telegram_app = Application.builder().token(TELEGRAM_TOKEN).updater(None).build()
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

@asynccontextmanager
async def lifespan(app: FastAPI):
    await telegram_app.initialize()
    await telegram_app.start()
    yield
    await telegram_app.stop()
    await telegram_app.shutdown()

app = FastAPI(lifespan=lifespan)

@app.post(f"/webhook/{TELEGRAM_TOKEN}")
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}

@app.get("/")
def health():
    return {"status": "ok"}