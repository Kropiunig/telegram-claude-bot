"""Telegram bot powered by Claude Code subprocess — full PC access."""

import asyncio
import json
import logging
import os
import subprocess
import uuid
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_IDS = {int(uid) for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",") if uid.strip()}
CLAUDE_CMD = os.environ.get("CLAUDE_CMD", "claude")
WORKING_DIR = os.environ.get("CLAUDE_WORKING_DIR", str(Path.home()))
MAX_MESSAGE_LENGTH = 4096
SESSIONS_FILE = Path(__file__).parent / "sessions.json"

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)


# --- Session management ---

def load_sessions() -> dict:
    if SESSIONS_FILE.exists():
        return json.loads(SESSIONS_FILE.read_text())
    return {}


def save_sessions(sessions: dict) -> None:
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2))


def get_session_id(chat_id: int) -> str | None:
    sessions = load_sessions()
    return sessions.get(str(chat_id))


def create_session_id(chat_id: int) -> str:
    session_id = str(uuid.uuid4())
    sessions = load_sessions()
    sessions[str(chat_id)] = session_id
    save_sessions(sessions)
    return session_id


def reset_session(chat_id: int) -> None:
    sessions = load_sessions()
    sessions.pop(str(chat_id), None)
    save_sessions(sessions)


# --- Claude ---

def is_allowed(user_id: int) -> bool:
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS


def call_claude(prompt: str, chat_id: int) -> str:
    """Call claude CLI with full access and conversation memory."""
    try:
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        cmd = [CLAUDE_CMD, "-p", prompt, "--output-format", "text",
               "--dangerously-skip-permissions"]

        # Resume existing session or start new one
        session_id = get_session_id(chat_id)
        if session_id:
            cmd.extend(["--resume", session_id])
        else:
            session_id = create_session_id(chat_id)
            cmd.extend(["--session-id", session_id])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            encoding="utf-8",
            env=env,
            cwd=WORKING_DIR,
        )
        if result.returncode != 0:
            error = result.stderr.strip() if result.stderr else "Unknown error"
            # If session is corrupted, reset it and retry once
            if "session" in error.lower() or "resume" in error.lower():
                logger.warning(f"Session error for chat {chat_id}, resetting: {error}")
                reset_session(chat_id)
                return call_claude(prompt, chat_id)
            return f"Claude error (exit {result.returncode}): {error}"
        return result.stdout.strip() or "(empty response)"
    except subprocess.TimeoutExpired:
        return "Claude timed out after 5 minutes."
    except FileNotFoundError:
        return "Error: `claude` CLI not found. Make sure Claude Code is installed and on PATH."
    except Exception as e:
        return f"Error calling Claude: {e}"


async def call_claude_async(prompt: str, chat_id: int) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, call_claude, prompt, chat_id)


def chunk_message(text: str) -> list[str]:
    if len(text) <= MAX_MESSAGE_LENGTH:
        return [text]
    chunks = []
    while text:
        if len(text) <= MAX_MESSAGE_LENGTH:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, MAX_MESSAGE_LENGTH)
        if split_at == -1:
            split_at = text.rfind(" ", 0, MAX_MESSAGE_LENGTH)
        if split_at == -1:
            split_at = MAX_MESSAGE_LENGTH
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


# --- Handlers ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("Not authorized.")
        return
    await update.message.reply_text(
        "Hello! I'm your remote Claude Code assistant.\n\n"
        "I have full access to your PC — files, terminal, web search, "
        "Google Calendar, and all MCP tools.\n\n"
        "I remember our conversation until you reset it.\n\n"
        "Commands:\n"
        "/start - This message\n"
        "/reset - Clear conversation memory\n"
        "/help - Show help"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        "I'm Claude Code running on your PC via Telegram.\n\n"
        "What I can do:\n"
        "- Read/write/edit any file on your PC\n"
        "- Run terminal commands (git, python, npm, etc.)\n"
        "- Search the web\n"
        "- Access Google Calendar, Telegram, Playwright\n"
        "- Multi-step coding tasks\n\n"
        "I remember our conversation. Use /reset to start fresh."
    )


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    reset_session(update.effective_chat.id)
    await update.message.reply_text("Conversation reset. Next message starts a fresh session.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("Not authorized.")
        return

    user_message = update.message.text
    if not user_message:
        return

    chat_id = update.effective_chat.id
    logger.info(f"Message from {update.effective_user.username} ({user_id}): {user_message[:100]}")

    await update.message.chat.send_action("typing")

    stop_typing = asyncio.Event()

    async def keep_typing():
        while not stop_typing.is_set():
            try:
                await update.message.chat.send_action("typing")
            except Exception:
                pass
            await asyncio.sleep(5)

    typing_task = asyncio.create_task(keep_typing())

    try:
        response = await call_claude_async(user_message, chat_id)
    finally:
        stop_typing.set()
        typing_task.cancel()

    for chunk in chunk_message(response):
        try:
            await update.message.reply_text(chunk)
        except Exception as e:
            logger.error(f"Failed to send chunk: {e}")
            await update.message.reply_text("(Error sending part of the response)")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception while handling update: {context.error}")


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info(f"Bot starting with working dir: {WORKING_DIR}")
    logger.info("Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
