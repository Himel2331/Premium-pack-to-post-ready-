import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import emoji
from dotenv import load_dotenv
from telegram import BotCommand, MessageEntity, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("telegram-premium-emoji-converter")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))
DATA_DIR = Path(os.getenv("DATA_DIR", "/tmp/telegram-premium-emoji-converter")).expanduser()
DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "bot_data.sqlite3"))).expanduser()
AUTO_SELECT_PACK_LINKS = os.getenv("AUTO_SELECT_PACK_LINKS", "true").strip().lower() == "true"
AUTO_CONVERT_TEXT = os.getenv("AUTO_CONVERT_TEXT", "true").strip().lower() == "true"
AUTO_CONVERT_CAPTIONS = os.getenv("AUTO_CONVERT_CAPTIONS", "true").strip().lower() == "true"
MAX_IDS_PER_LOOKUP = int(os.getenv("MAX_IDS_PER_LOOKUP", "200"))
MAX_OUTPUT_IDS = int(os.getenv("MAX_OUTPUT_IDS", "500"))
PREMIUM_ERROR_HINT = (
    "Premium emoji পাঠাতে Telegram permission লাগবে। Bot owner-এর Telegram Premium থাকা বা bot-এর custom emoji permission সক্রিয় থাকা দরকার।"
)

HELP_TEXT = (
    "আমি text/caption-এর emoji premium custom emoji-তে convert করতে পারি।\n\n"
    "মূল ফিচার:\n"
    "- addemoji pack select করা\n"
    "- raw custom_emoji_id দিলে premium emoji বানানো\n"
    "- selected pack থাকলে normal emoji → matching premium emoji\n"
    "- text, photo caption, video caption, document caption, animation/audio/voice caption support\n"
    "- reply করে /convert দিলেও কাজ করবে\n\n"
    "Commands:\n"
    "/start - শুরু\n"
    "/help - help text\n"
    "/ping - bot live কিনা\n"
    "/id - chat এবং user id\n"
    "/setpack <addemoji_link_or_set_name> - current pack select\n"
    "/currentpack - current selected pack দেখাবে\n"
    "/clearpack - selected pack remove করবে\n"
    "/packinfo [link_or_set_name] - pack info\n"
    "/ids <link_or_set_name> - pack-এর ID list\n"
    "/convert <text> - text convert করবে\n\n"
    "Usage examples:\n"
    "1) /setpack https://t.me/addemoji/vector_icons_by_fStikBot\n"
    "2) 🙂 Hello 😎\n"
    "3) 5219899949281453881 5222472119295684375\n"
    "4) কোনো photo/video/file caption সহ পাঠান — caption convert করে resend করব\n"
    "5) কোনো message-এ reply দিয়ে /convert দিন"
)

BOT_COMMANDS = [
    BotCommand("start", "Start the bot"),
    BotCommand("help", "Show help"),
    BotCommand("ping", "Check bot status"),
    BotCommand("id", "Show chat and user id"),
    BotCommand("setpack", "Select a premium emoji pack"),
    BotCommand("currentpack", "Show current selected pack"),
    BotCommand("clearpack", "Clear selected pack"),
    BotCommand("packinfo", "Show pack info"),
    BotCommand("ids", "Extract IDs from a pack"),
    BotCommand("convert", "Convert text or replied message"),
]

ADD_LINK_RE = re.compile(
    r"(?:https?://)?(?:t(?:elegram)?\.me)/(addemoji|addstickers)/([A-Za-z0-9_]+)",
    re.IGNORECASE,
)
CUSTOM_ID_RE = re.compile(r"(?<!\d)(\d{15,30})(?!\d)")


@dataclass
class PackRecord:
    set_name: str
    link: str
    title: str
    sticker_type: str
    total_items: int
    emoji_to_ids: Dict[str, List[str]]
    id_to_emoji: Dict[str, str]
    updated_at: float


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


class Storage:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        ensure_data_dir()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS packs (
                    set_name TEXT PRIMARY KEY,
                    link TEXT NOT NULL,
                    title TEXT NOT NULL,
                    sticker_type TEXT NOT NULL,
                    total_items INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_settings (
                    chat_id INTEGER PRIMARY KEY,
                    set_name TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.commit()

    def save_pack(self, pack: PackRecord) -> None:
        payload = json.dumps(
            {
                "emoji_to_ids": pack.emoji_to_ids,
                "id_to_emoji": pack.id_to_emoji,
            },
            ensure_ascii=False,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO packs (set_name, link, title, sticker_type, total_items, payload_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(set_name) DO UPDATE SET
                    link = excluded.link,
                    title = excluded.title,
                    sticker_type = excluded.sticker_type,
                    total_items = excluded.total_items,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    pack.set_name,
                    pack.link,
                    pack.title,
                    pack.sticker_type,
                    pack.total_items,
                    payload,
                    pack.updated_at,
                ),
            )
            conn.commit()

    def get_pack(self, set_name: str) -> Optional[PackRecord]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM packs WHERE set_name = ?", (set_name,)).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload_json"])
        return PackRecord(
            set_name=row["set_name"],
            link=row["link"],
            title=row["title"],
            sticker_type=row["sticker_type"],
            total_items=row["total_items"],
            emoji_to_ids={k: list(v) for k, v in payload.get("emoji_to_ids", {}).items()},
            id_to_emoji={k: str(v) for k, v in payload.get("id_to_emoji", {}).items()},
            updated_at=float(row["updated_at"]),
        )

    def set_chat_pack(self, chat_id: int, set_name: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO chat_settings (chat_id, set_name, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    set_name = excluded.set_name,
                    updated_at = excluded.updated_at
                """,
                (chat_id, set_name, time.time()),
            )
            conn.commit()

    def clear_chat_pack(self, chat_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM chat_settings WHERE chat_id = ?", (chat_id,))
            conn.commit()

    def get_chat_pack(self, chat_id: int) -> Optional[PackRecord]:
        with self._connect() as conn:
            row = conn.execute("SELECT set_name FROM chat_settings WHERE chat_id = ?", (chat_id,)).fetchone()
        if not row:
            return None
        return self.get_pack(str(row["set_name"]))


storage = Storage(DB_PATH)


def chunk_text(text: str, max_length: int = 4000) -> List[str]:
    text = text or ""
    if len(text) <= max_length:
        return [text]

    chunks: List[str] = []
    current = ""
    for line in text.splitlines(True):
        if len(current) + len(line) > max_length:
            if current:
                chunks.append(current)
                current = ""
        if len(line) > max_length:
            start = 0
            while start < len(line):
                chunks.append(line[start : start + max_length])
                start += max_length
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


async def send_text_chunks(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return

    safe_text = (text or "").strip() or "কোনো data পাওয়া যায়নি।"
    for chunk in chunk_text(safe_text):
        await context.bot.send_message(
            chat_id=chat.id,
            text=chunk,
            reply_to_message_id=message.message_id,
            disable_web_page_preview=True,
        )


def extract_pack_target(raw_text: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    text = (raw_text or "").strip()
    if not text:
        return None, None, None

    match = ADD_LINK_RE.search(text)
    if match:
        kind = match.group(1).lower()
        set_name = match.group(2)
        full_link = match.group(0)
        if not full_link.startswith("http"):
            full_link = f"https://{full_link}"
        return kind, set_name, full_link

    parsed = urlparse(text)
    if parsed.netloc.lower() in {"t.me", "telegram.me"}:
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"addemoji", "addstickers"}:
            return parts[0], parts[1], text

    set_name = text.strip().split()[0]
    if re.fullmatch(r"[A-Za-z0-9_]+", set_name):
        return None, set_name, f"https://t.me/addemoji/{set_name}"

    return None, None, None


async def fetch_pack_record(bot, set_name: str, provided_link: Optional[str] = None) -> PackRecord:
    sticker_set = await bot.get_sticker_set(name=set_name)
    stickers = list(sticker_set.stickers or [])
    sticker_type = getattr(sticker_set, "sticker_type", None) or "unknown"
    title = sticker_set.title or set_name
    link = provided_link or f"https://t.me/addemoji/{set_name}"

    emoji_to_ids: Dict[str, List[str]] = defaultdict(list)
    id_to_emoji: Dict[str, str] = {}

    for sticker in stickers:
        custom_emoji_id = getattr(sticker, "custom_emoji_id", None)
        sticker_emoji = getattr(sticker, "emoji", None) or "🙂"
        if custom_emoji_id:
            custom_emoji_id = str(custom_emoji_id)
            emoji_to_ids[sticker_emoji].append(custom_emoji_id)
            id_to_emoji[custom_emoji_id] = sticker_emoji

    return PackRecord(
        set_name=set_name,
        link=link,
        title=title,
        sticker_type=sticker_type,
        total_items=len(stickers),
        emoji_to_ids={k: list(v) for k, v in emoji_to_ids.items()},
        id_to_emoji=id_to_emoji,
        updated_at=time.time(),
    )


async def get_or_fetch_pack(bot, set_name: str, provided_link: Optional[str] = None, force_refresh: bool = False) -> PackRecord:
    if not force_refresh:
        cached = storage.get_pack(set_name)
        if cached:
            return cached
    pack = await fetch_pack_record(bot, set_name, provided_link=provided_link)
    storage.save_pack(pack)
    return pack


def build_pack_info_text(pack: PackRecord) -> str:
    return (
        "Pack info\n"
        f"title: {pack.title}\n"
        f"set_name: {pack.set_name}\n"
        f"link: {pack.link}\n"
        f"type: {pack.sticker_type}\n"
        f"total_items: {pack.total_items}\n"
        f"total_ids_found: {len(pack.id_to_emoji)}"
    )


def build_pack_id_text(pack: PackRecord) -> str:
    ids = list(pack.id_to_emoji.keys())[:MAX_OUTPUT_IDS]
    lines = [build_pack_info_text(pack), "", "custom_emoji_id list:"]
    if ids:
        lines.extend(ids)
    else:
        lines.append("No custom_emoji_id found in this pack.")
    if len(pack.id_to_emoji) > len(ids):
        lines.extend(
            [
                "",
                f"Note: output limited to first {len(ids)} IDs. Increase MAX_OUTPUT_IDS if needed.",
            ]
        )
    return "\n".join(lines)


async def resolve_custom_id_to_emoji(bot, custom_ids: Iterable[str]) -> Dict[str, str]:
    ids = []
    seen = set()
    for custom_id in custom_ids:
        value = str(custom_id).strip()
        if value and value not in seen:
            seen.add(value)
            ids.append(value)

    result: Dict[str, str] = {}
    if not ids:
        return result

    for start in range(0, len(ids), MAX_IDS_PER_LOOKUP):
        batch = ids[start : start + MAX_IDS_PER_LOOKUP]
        stickers = await bot.get_custom_emoji_stickers(custom_emoji_ids=batch)
        for sticker in stickers or []:
            custom_emoji_id = getattr(sticker, "custom_emoji_id", None)
            sticker_emoji = getattr(sticker, "emoji", None) or "🙂"
            if custom_emoji_id:
                result[str(custom_emoji_id)] = sticker_emoji
    return result


@dataclass
class ConversionResult:
    text: str
    entities: List[MessageEntity]
    converted_count: int
    converted_ids_count: int
    converted_emoji_count: int
    unresolved_ids: List[str]
    used_pack_name: Optional[str]


async def convert_text_to_premium(
    bot,
    text: str,
    pack: Optional[PackRecord],
) -> ConversionResult:
    source = text or ""
    if not source:
        return ConversionResult(
            text="",
            entities=[],
            converted_count=0,
            converted_ids_count=0,
            converted_emoji_count=0,
            unresolved_ids=[],
            used_pack_name=pack.set_name if pack else None,
        )

    id_matches = list(CUSTOM_ID_RE.finditer(source))
    id_lookup = await resolve_custom_id_to_emoji(bot, [m.group(1) for m in id_matches])
    emoji_matches = {m["match_start"]: m for m in emoji.emoji_list(source)}
    local_counters: Dict[str, int] = defaultdict(int)

    out_parts: List[str] = []
    entities: List[MessageEntity] = []
    converted_count = 0
    converted_ids_count = 0
    converted_emoji_count = 0
    unresolved_ids: List[str] = []

    id_match_by_start = {m.start(): m for m in id_matches}
    i = 0
    while i < len(source):
        id_match = id_match_by_start.get(i)
        if id_match:
            custom_id = id_match.group(1)
            fallback = id_lookup.get(custom_id)
            if fallback:
                current_output = "".join(out_parts)
                offset = utf16_len(current_output)
                out_parts.append(fallback)
                entities.append(
                    MessageEntity(
                        type=MessageEntity.CUSTOM_EMOJI,
                        offset=offset,
                        length=utf16_len(fallback),
                        custom_emoji_id=custom_id,
                    )
                )
                converted_count += 1
                converted_ids_count += 1
            else:
                unresolved_ids.append(custom_id)
                out_parts.append(id_match.group(0))
            i = id_match.end()
            continue

        emoji_match = emoji_matches.get(i)
        if emoji_match and pack:
            base_emoji = emoji_match["emoji"]
            choices = pack.emoji_to_ids.get(base_emoji)
            if choices:
                idx = local_counters[base_emoji] % len(choices)
                custom_id = choices[idx]
                local_counters[base_emoji] += 1
                current_output = "".join(out_parts)
                offset = utf16_len(current_output)
                out_parts.append(base_emoji)
                entities.append(
                    MessageEntity(
                        type=MessageEntity.CUSTOM_EMOJI,
                        offset=offset,
                        length=utf16_len(base_emoji),
                        custom_emoji_id=custom_id,
                    )
                )
                converted_count += 1
                converted_emoji_count += 1
                i = int(emoji_match["match_end"])
                continue

        out_parts.append(source[i])
        i += 1

    return ConversionResult(
        text="".join(out_parts),
        entities=entities,
        converted_count=converted_count,
        converted_ids_count=converted_ids_count,
        converted_emoji_count=converted_emoji_count,
        unresolved_ids=unresolved_ids,
        used_pack_name=pack.set_name if pack else None,
    )


def get_current_pack_for_chat(chat_id: int) -> Optional[PackRecord]:
    return storage.get_chat_pack(chat_id)


async def send_conversion_result(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conversion: ConversionResult,
) -> None:
    chat = update.effective_chat
    message = update.effective_message
    if not chat or not message:
        return

    if not conversion.text.strip():
        await send_text_chunks(update, context, "কনভার্ট করার মতো text পাওয়া যায়নি।")
        return

    try:
        await context.bot.send_message(
            chat_id=chat.id,
            text=conversion.text,
            entities=conversion.entities or None,
            reply_to_message_id=message.message_id,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        logger.warning("Failed to send premium emoji text: %s", exc)
        await send_text_chunks(update, context, f"Send করতে সমস্যা হয়েছে। {PREMIUM_ERROR_HINT}\nError: {exc}")
        return

    if conversion.unresolved_ids:
        unresolved_preview = ", ".join(conversion.unresolved_ids[:10])
        extra = "" if len(conversion.unresolved_ids) <= 10 else f" ... +{len(conversion.unresolved_ids) - 10} more"
        await send_text_chunks(
            update,
            context,
            f"কিছু ID resolve করা যায়নি: {unresolved_preview}{extra}",
        )


def get_message_body_and_entities(message) -> Tuple[str, Sequence[MessageEntity], str]:
    if message.text:
        return message.text, message.entities or [], "text"
    if message.caption:
        return message.caption, message.caption_entities or [], "caption"
    return "", [], "none"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_text_chunks(update, context, HELP_TEXT)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_text_chunks(update, context, HELP_TEXT)


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pack = get_current_pack_for_chat(update.effective_chat.id) if update.effective_chat else None
    extra = f"\ncurrent_pack: {pack.set_name}" if pack else "\ncurrent_pack: none"
    await send_text_chunks(update, context, f"pong{extra}")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else "unknown"
    user_id = update.effective_user.id if update.effective_user else "unknown"
    await send_text_chunks(update, context, f"chat_id: {chat_id}\nuser_id: {user_id}")


async def cmd_setpack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return

    raw = " ".join(context.args or []).strip()
    if not raw and message.reply_to_message and message.reply_to_message.text:
        raw = message.reply_to_message.text.strip()

    if not raw:
        await send_text_chunks(update, context, "Usage:\n/setpack https://t.me/addemoji/vector_icons_by_fStikBot")
        return

    _, set_name, link = extract_pack_target(raw)
    if not set_name:
        await send_text_chunks(update, context, "Valid addemoji link বা set_name পাইনি।")
        return

    try:
        await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
        pack = await get_or_fetch_pack(context.bot, set_name, provided_link=link, force_refresh=True)
        if pack.sticker_type != "custom_emoji":
            await send_text_chunks(update, context, "এই pack custom emoji pack না। /setpack এর জন্য addemoji pack লাগবে।")
            return
        storage.set_chat_pack(chat.id, pack.set_name)
        await send_text_chunks(
            update,
            context,
            f"Pack selected successfully.\n\n{build_pack_info_text(pack)}\n\nএখন text/emoji/caption পাঠালেই convert করার চেষ্টা করব.",
        )
    except TelegramError as exc:
        logger.exception("Failed to set pack")
        await send_text_chunks(update, context, f"Pack set করতে সমস্যা হয়েছে: {exc}")


async def cmd_currentpack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return
    pack = get_current_pack_for_chat(chat.id)
    if not pack:
        await send_text_chunks(update, context, "এই chat-এ এখনো কোনো pack select করা হয়নি।")
        return
    await send_text_chunks(update, context, build_pack_info_text(pack))


async def cmd_clearpack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return
    storage.clear_chat_pack(chat.id)
    await send_text_chunks(update, context, "Current pack cleared.")


async def cmd_packinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return

    raw = " ".join(context.args or []).strip()
    pack: Optional[PackRecord] = None
    if raw:
        _, set_name, link = extract_pack_target(raw)
        if not set_name:
            await send_text_chunks(update, context, "Valid pack target পাইনি।")
            return
        try:
            pack = await get_or_fetch_pack(context.bot, set_name, provided_link=link, force_refresh=True)
        except TelegramError as exc:
            await send_text_chunks(update, context, f"Pack info আনতে সমস্যা হয়েছে: {exc}")
            return
    else:
        pack = get_current_pack_for_chat(chat.id)
        if not pack:
            await send_text_chunks(update, context, "কোনো pack select করা নেই। Argument দিন বা আগে /setpack করুন।")
            return

    await send_text_chunks(update, context, build_pack_info_text(pack))


async def cmd_ids(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    raw = " ".join(context.args or []).strip()
    if not raw and message.reply_to_message and message.reply_to_message.text:
        raw = message.reply_to_message.text.strip()
    if not raw:
        await send_text_chunks(update, context, "Usage:\n/ids https://t.me/addemoji/vector_icons_by_fStikBot")
        return

    _, set_name, link = extract_pack_target(raw)
    if not set_name:
        await send_text_chunks(update, context, "Valid pack target পাইনি।")
        return

    try:
        pack = await get_or_fetch_pack(context.bot, set_name, provided_link=link, force_refresh=True)
        if pack.sticker_type != "custom_emoji":
            await send_text_chunks(
                update,
                context,
                f"{build_pack_info_text(pack)}\n\nNote: এটি custom emoji pack না, তাই custom_emoji_id list নেই।",
            )
            return
        await send_text_chunks(update, context, build_pack_id_text(pack))
    except TelegramError as exc:
        await send_text_chunks(update, context, f"IDs বের করতে সমস্যা হয়েছে: {exc}")


async def cmd_convert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return

    raw = " ".join(context.args or []).strip()
    if raw:
        pack = get_current_pack_for_chat(chat.id)
        conversion = await convert_text_to_premium(context.bot, raw, pack=pack)
        if conversion.converted_count == 0:
            hint = " কোনো matching emoji/id পাইনি।"
            if not pack:
                hint += " Normal emoji convert করতে আগে /setpack করুন।"
            await send_text_chunks(update, context, f"কিছু convert হয়নি।{hint}")
            return
        await send_conversion_result(update, context, conversion)
        return

    replied = message.reply_to_message
    if not replied:
        await send_text_chunks(update, context, "Usage: /convert <text> অথবা কোনো message-এ reply দিয়ে /convert দিন।")
        return

    pack = get_current_pack_for_chat(chat.id)

    if replied.text:
        conversion = await convert_text_to_premium(context.bot, replied.text, pack=pack)
        if conversion.converted_count == 0:
            hint = "কিছু convert হয়নি।"
            if not pack and not CUSTOM_ID_RE.search(replied.text):
                hint += " Normal emoji convert করতে আগে /setpack করুন।"
            await send_text_chunks(update, context, hint)
            return
        await send_conversion_result(update, context, conversion)
        return

    if replied.caption or replied.photo or replied.video or replied.document or replied.animation or replied.audio or replied.voice:
        await process_media_with_caption_source(update, context, replied, reply_to_message_id=message.message_id)
        return

    await send_text_chunks(update, context, "Reply করা message-এ text/caption/media পাইনি।")


async def maybe_handle_pack_link(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str) -> bool:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return False

    kind, set_name, link = extract_pack_target(raw_text)
    if not set_name:
        return False

    try:
        pack = await get_or_fetch_pack(context.bot, set_name, provided_link=link, force_refresh=True)
        if kind == "addemoji" or pack.sticker_type == "custom_emoji":
            if AUTO_SELECT_PACK_LINKS and pack.sticker_type == "custom_emoji":
                storage.set_chat_pack(chat.id, pack.set_name)
                await send_text_chunks(
                    update,
                    context,
                    f"Pack selected automatically.\n\n{build_pack_info_text(pack)}\n\nএখন normal emoji পাঠালেও selected pack দিয়ে convert করার চেষ্টা করব.",
                )
            else:
                await send_text_chunks(update, context, build_pack_info_text(pack))
        else:
            await send_text_chunks(update, context, f"{build_pack_info_text(pack)}\n\nএই pack custom emoji pack না।")
        return True
    except TelegramError as exc:
        await send_text_chunks(update, context, f"Pack fetch করতে সমস্যা হয়েছে: {exc}")
        return True


async def process_media_with_caption_source(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    source_message,
    reply_to_message_id: Optional[int] = None,
) -> None:
    chat = update.effective_chat
    trigger_message = update.effective_message
    if not chat or not trigger_message:
        return

    caption = source_message.caption or ""
    if not caption:
        await send_text_chunks(update, context, "এই media message-এ caption নেই। Caption থাকলে convert করে resend করতাম।")
        return

    pack = get_current_pack_for_chat(chat.id)
    conversion = await convert_text_to_premium(context.bot, caption, pack=pack)
    if conversion.converted_count == 0:
        hint = "Caption-এ কোনো convertible emoji/id পাইনি।"
        if not pack and not CUSTOM_ID_RE.search(caption):
            hint += " Normal emoji convert করতে আগে /setpack করুন।"
        await send_text_chunks(update, context, hint)
        return

    kwargs = {
        "chat_id": chat.id,
        "caption": conversion.text,
        "caption_entities": conversion.entities or None,
        "reply_to_message_id": reply_to_message_id or trigger_message.message_id,
    }

    try:
        if source_message.photo:
            await context.bot.send_photo(photo=source_message.photo[-1].file_id, **kwargs)
        elif source_message.video:
            await context.bot.send_video(video=source_message.video.file_id, **kwargs)
        elif source_message.document:
            await context.bot.send_document(document=source_message.document.file_id, **kwargs)
        elif source_message.animation:
            await context.bot.send_animation(animation=source_message.animation.file_id, **kwargs)
        elif source_message.audio:
            await context.bot.send_audio(audio=source_message.audio.file_id, **kwargs)
        elif source_message.voice:
            await context.bot.send_voice(voice=source_message.voice.file_id, **kwargs)
        else:
            await send_text_chunks(update, context, "এই media type এখনো caption resend-এর জন্য supported না।")
            return
    except BadRequest as exc:
        logger.warning("Failed to resend media with premium caption: %s", exc)
        await send_text_chunks(update, context, f"Caption সহ media resend করতে সমস্যা হয়েছে। {PREMIUM_ERROR_HINT}\nError: {exc}")
        return

    if conversion.unresolved_ids:
        unresolved_preview = ", ".join(conversion.unresolved_ids[:10])
        extra = "" if len(conversion.unresolved_ids) <= 10 else f" ... +{len(conversion.unresolved_ids) - 10} more"
        await send_text_chunks(update, context, f"কিছু ID resolve করা যায়নি: {unresolved_preview}{extra}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat or not message.text:
        return

    text = message.text.strip()
    if not text or text.startswith("/"):
        return

    if await maybe_handle_pack_link(update, context, text):
        return

    if not AUTO_CONVERT_TEXT:
        await send_text_chunks(update, context, "AUTO_CONVERT_TEXT disabled. /convert ব্যবহার করুন।")
        return

    pack = get_current_pack_for_chat(chat.id)
    conversion = await convert_text_to_premium(context.bot, text, pack=pack)
    if conversion.converted_count == 0:
        if CUSTOM_ID_RE.search(text):
            await send_text_chunks(update, context, "ID detect করেছি, কিন্তু valid custom emoji resolve করতে পারিনি।")
            return
        if not pack:
            await send_text_chunks(update, context, "Normal emoji convert করতে আগে /setpack করুন, অথবা raw custom_emoji_id পাঠান।")
            return
        await send_text_chunks(update, context, "Selected pack-এ matching premium emoji পাইনি।")
        return
    await send_conversion_result(update, context, conversion)


async def handle_supported_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    if not AUTO_CONVERT_CAPTIONS:
        return

    if message.caption:
        await process_media_with_caption_source(update, context, message)


async def post_init(application: Application) -> None:
    try:
        await application.bot.set_my_commands(BOT_COMMANDS)
        logger.info("Bot commands registered")
    except Exception:
        logger.exception("Failed to register bot commands")


def _health_payload() -> bytes:
    payload = {
        "ok": True,
        "service": "telegram-premium-emoji-converter",
        "mode": "polling",
        "db_path": str(DB_PATH),
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def run_render_health_server() -> None:
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path not in ("/", "/healthz"):
                self.send_response(404)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(b'{"ok": false, "error": "not_found"}')
                return

            body = _health_payload()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            return

    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    logger.info("Health server started on port %s", PORT)
    server.serve_forever()


def build_app() -> Application:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("ping", cmd_ping))
    application.add_handler(CommandHandler("id", cmd_id))
    application.add_handler(CommandHandler("setpack", cmd_setpack))
    application.add_handler(CommandHandler("currentpack", cmd_currentpack))
    application.add_handler(CommandHandler("clearpack", cmd_clearpack))
    application.add_handler(CommandHandler("packinfo", cmd_packinfo))
    application.add_handler(CommandHandler("ids", cmd_ids))
    application.add_handler(CommandHandler("convert", cmd_convert))

    media_filter = filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.ANIMATION | filters.AUDIO | filters.VOICE
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(media_filter, handle_supported_media))

    return application


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing")

    threading.Thread(target=run_render_health_server, daemon=True).start()
    app = build_app()

    logger.info("Starting Telegram Premium Emoji Converter Bot in polling mode")
    logger.info("DB path: %s", DB_PATH)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    finally:
        try:
            loop.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
