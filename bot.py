"""Telegram bot powered by Claude Code subprocess."""

import asyncio
import logging
import os
import subprocess
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_IDS = {int(uid) for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",") if uid.strip()}
CLAUDE_CMD = os.environ.get("CLAUDE_CMD", "claude")
MAX_MESSAGE_LENGTH = 4096

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)


def is_allowed(user_id: int) -> bool:
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS


def call_claude(prompt: str) -> str:
    """Call claude CLI as a subprocess and return the response text."""
    try:
        # Remove CLAUDECODE env var so claude doesn't think it's nested
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = subprocess.run(
            [CLAUDE_CMD, "-p", prompt, "--output-format", "text"],
            capture_output=True,
            text=True,
            timeout=300,
            encoding="utf-8",
            env=env,
        )
        if result.returncode != 0:
            error = result.stderr.strip() if result.stderr else "Unknown error"
            return f"Claude error (exit {result.returncode}): {error}"
        return result.stdout.strip() or "(empty response)"
    except subprocess.TimeoutExpired:
        return "Claude timed out after 5 minutes."
    except FileNotFoundError:
        return "Error: `claude` CLI not found. Make sure Claude Code is installed and on PATH."
    except Exception as e:
        return f"Error calling Claude: {e}"


async def call_claude_async(prompt: str) -> str:
    """Run the blocking claude call in a thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, call_claude, prompt)


def chunk_message(text: str) -> list[str]:
    """Split text into chunks that fit Telegram's message limit."""
    if len(text) <= MAX_MESSAGE_LENGTH:
        return [text]
    chunks = []
    while text:
        if len(text) <= MAX_MESSAGE_LENGTH:
            chunks.append(text)
            break
        # Try to split at a newline
        split_at = text.rfind("\n", 0, MAX_MESSAGE_LENGTH)
        if split_at == -1:
            # Fall back to splitting at a space
            split_at = text.rfind(" ", 0, MAX_MESSAGE_LENGTH)
        if split_at == -1:
            split_at = MAX_MESSAGE_LENGTH
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("Not authorized.")
        return
    await update.message.reply_text(
        "Hello! I'm a Claude AI assistant.\n\n"
        "Send me any message and I'll respond using Claude Code with full tool access "
        "(web search, calendar, etc.).\n\n"
        "Commands:\n"
        "/start - This message\n"
        "/help - Show help\n"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        "Just send me a message! I use Claude Code under the hood, "
        "so I have access to web search, your Google Calendar, and other tools.\n\n"
        "Each message is independent (no conversation memory between messages).\n\n"
        "Tip: Be specific in your requests for best results."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("Not authorized.")
        return

    user_message = update.message.text
    if not user_message:
        return

    logger.info(f"Message from {update.effective_user.username} ({user_id}): {user_message[:100]}")

    # Send typing indicator
    await update.message.chat.send_action("typing")

    # Keep sending typing every 5s while Claude processes
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
        response = await call_claude_async(user_message)
    finally:
        stop_typing.set()
        typing_task.cancel()

    # Send response in chunks
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Bot starting... Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
