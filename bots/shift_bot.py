"""Helping Hands Telegram Shift Bot - worker shift management via Telegram"""
import os
import asyncio
import logging
from datetime import datetime
import httpx
from notion_client import AsyncClient as NotionClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_SHIFTS_DB = os.getenv("NOTION_SHIFTS_DB", "")
ALLOWED_CHAT_IDS = os.getenv("ALLOWED_CHAT_IDS", "").split(",")
API_BASE = os.getenv("API_BASE_URL", "https://helpinghands.com.au/api")

notion = NotionClient(auth=NOTION_TOKEN)
BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

COMMANDS = {
    "/start": "Welcome to Helping Hands Shift Bot",
    "/shifts": "View your upcoming shifts",
    "/checkin": "Check in to your current shift",
    "/checkout": "Check out and complete your shift",
    "/report": "Submit incident or daily report",
    "/invoice": "Request monthly invoice summary",
    "/help": "Show all available commands"
}


class ShiftBot:
    """Telegram bot for Helping Hands NDIS worker management."""

    def __init__(self):
        self.offset = 0
        self.active_shifts = {}  # chat_id -> shift_id mapping

    async def send_message(self, chat_id: str, text: str, parse_mode: str = "HTML"):
        """Send a Telegram message."""
        async with httpx.AsyncClient() as client:
            try:
                await client.post(f"{BASE_URL}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": parse_mode
                })
            except Exception as e:
                logger.error(f"Send failed: {e}")

    async def get_updates(self):
        """Long poll for Telegram updates."""
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.get(f"{BASE_URL}/getUpdates", params={
                    "offset": self.offset,
                    "timeout": 25,
                    "allowed_updates": ["message"]
                })
                data = resp.json()
                return data.get("result", [])
            except Exception as e:
                logger.error(f"Get updates failed: {e}")
                return []

    def is_authorized(self, chat_id: str) -> bool:
        """Check if user is authorized."""
        return not ALLOWED_CHAT_IDS or str(chat_id) in ALLOWED_CHAT_IDS or ALLOWED_CHAT_IDS == [""]:

    async def handle_start(self, chat_id: str, user_name: str):
        msg = (
            f"Welcome to <b>Helping Hands Shift Bot</b>, {user_name}!\n\n"
            f"ABN: 65681861276\n"
            f"Business: Helping Hands Support Services\n\n"
            f"Available commands:\n"
        )
        for cmd, desc in COMMANDS.items():
            msg += f"{cmd} - {desc}\n"
        await self.send_message(chat_id, msg)

    async def handle_shifts(self, chat_id: str):
        """Show upcoming shifts from Notion."""
        try:
            today = datetime.utcnow().date().isoformat()
            resp = await notion.databases.query(
                database_id=NOTION_SHIFTS_DB,
                filter={
                    "and": [
                        {"property": "Status", "select": {"equals": "Scheduled"}},
                        {"property": "Date", "date": {"on_or_after": today}}
                    ]
                },
                sorts=[{"property": "Date", "direction": "ascending"}],
                page_size=5
            )
            shifts = resp.get("results", [])
            if not shifts:
                await self.send_message(chat_id, "No upcoming shifts found.")
                return
            msg = "<b>Upcoming Shifts:</b>\n\n"
            for s in shifts:
                props = s["properties"]
                client_name = props.get("Client", {}).get("rich_text", [{}])[0].get("plain_text", "?")
                date_val = props.get("Date", {}).get("date", {}).get("start", "?")
                start_t = props.get("Start Time", {}).get("rich_text", [{}])[0].get("plain_text", "?")
                end_t = props.get("End Time", {}).get("rich_text", [{}])[0].get("plain_text", "?")
                support = props.get("Support Type", {}).get("select", {}).get("name", "?")
                msg += f"Date: {date_val}\nClient: {client_name}\nTime: {start_t}-{end_t}\nType: {support}\n\n"
            await self.send_message(chat_id, msg)
        except Exception as e:
            await self.send_message(chat_id, f"Error fetching shifts: {e}")

    async def handle_checkin(self, chat_id: str):
        """Record shift check-in."""
        now = datetime.utcnow().strftime("%H:%M")
        self.active_shifts[chat_id] = {"checkin": now, "date": datetime.utcnow().date().isoformat()}
        await self.send_message(chat_id, f"Checked in at {now}. Have a great shift! Send /checkout when done.")

    async def handle_checkout(self, chat_id: str):
        """Record shift check-out."""
        if chat_id not in self.active_shifts:
            await self.send_message(chat_id, "No active shift found. Use /checkin first.")
            return
        now = datetime.utcnow().strftime("%H:%M")
        checkin = self.active_shifts.pop(chat_id)
        await self.send_message(chat_id,
            f"Shift completed!\nCheck-in: {checkin['checkin']}\nCheck-out: {now}\n"
            f"Remember to submit any incident reports with /report")

    async def handle_help(self, chat_id: str):
        msg = "<b>Commands:</b>\n"
        for cmd, desc in COMMANDS.items():
            msg += f"{cmd} - {desc}\n"
        await self.send_message(chat_id, msg)

    async def process_update(self, update: dict):
        """Route incoming update to correct handler."""
        msg = update.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "").strip()
        user = msg.get("from", {})
        user_name = user.get("first_name", "Worker")

        if not chat_id or not text:
            return

        if not self.is_authorized(chat_id):
            await self.send_message(chat_id, "Unauthorized. Contact your administrator.")
            return

        cmd = text.split()[0].lower()
        if cmd == "/start":
            await self.handle_start(chat_id, user_name)
        elif cmd == "/shifts":
            await self.handle_shifts(chat_id)
        elif cmd == "/checkin":
            await self.handle_checkin(chat_id)
        elif cmd == "/checkout":
            await self.handle_checkout(chat_id)
        elif cmd == "/help":
            await self.handle_help(chat_id)
        else:
            await self.send_message(chat_id, f"Unknown command. Use /help to see available commands.")

    async def run(self):
        """Start the bot polling loop."""
        logger.info("Helping Hands Shift Bot started")
        while True:
            updates = await self.get_updates()
            for update in updates:
                self.offset = update["update_id"] + 1
                await self.process_update(update)
            if not updates:
                await asyncio.sleep(1)


if __name__ == "__main__":
    bot = ShiftBot()
    asyncio.run(bot.run())
