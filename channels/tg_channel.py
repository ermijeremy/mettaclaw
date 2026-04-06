import asyncio
import time
import threading
import time
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command

import yaml
import os

class _TelegramChannel:
    """Telegram bot channel with windowed batching and bot-tag gating using aiogram."""

    def __init__(self, config_path=None):
        self.config_path = os.path.join(os.path.dirname(__file__),  "..", "memory", "telegram_profile.yaml")
        self.policy_path= os.path.join(os.path.dirname(__file__), "..", "memory", "policy.md")
        self.running = False
        self.thread = None
        self.loop = None
        self.bot = None
        self.dp = None
        self.connected = False
        self.chat_id = None
        self.bot_username = None
        self.bot_id = None
        self.msg_lock = threading.Lock()
        
        # Default settings
        self.window_seconds = 60
        self.reply_only_on_tag = True
        self.reply_on_reply = True
        self.admin_ids = []
        self.dm_enabled = False
        
        # Policy messages
        self.start_msg = "Telegram mode active."
        self.about_msg = "I am a MeTTaClaw agent."
        self.privacy_msg = "No sensitive data is stored."
        
        # Load config and policies if they exist
        self.load_config(self.config_path)
        self.load_policies()

        # self.local_memory = self._load_local_memory()
        self._muted_users = {}
        self._user_msg_rates = {}
        
        # Windowed batching state
        self._message_buffer = []  # List of (timestamp, name, text, message_id)
        self._should_reply = False
        self._last_processed_window = None
        self._reply_to_id = None
        self._polling_task = None

    def load_config(self, config_path):
        """Load bot configuration from a YAML file."""
        if not os.path.exists(config_path):
            print(f"Config file {config_path} not found. Using defaults.")
            logging.warning(f"Config file {config_path} not found. Using defaults.")
            return

        try:
            with open(config_path, "r") as f:
                config = yaml.safe_load(f)
            
            tg_cfg = config.get("telegram", {})
            self.window_seconds = tg_cfg.get("batching", {}).get("window_seconds", 60)
            self.reply_only_on_tag = tg_cfg.get("reply_only_when_directly_tagged", True)
            self.reply_on_reply = tg_cfg.get("reply_on_reply_to_bot", True)
            self.dm_enabled = tg_cfg.get("dm_support", {}).get("enabled", False)
            self.admin_ids = config.get("admin_controls", {}).get("admin_ids", [])

            logging.info(f"Loaded config from {config_path}: window={self.window_seconds}s, tag_only={self.reply_only_on_tag}")
        except Exception as e:
            logging.error(f"Error loading config {config_path}: {e}")

    def load_policies(self):
        """Load and parse policy sections from a markdown file."""
        
        if not os.path.exists(self.policy_path):
            logging.warning(f"Policy file {self.policy_path} not found. Using defaults.")
            return

        try:
            with open(self.policy_path, "r") as f:
                content = f.read()
            
            sections = {}
            current_section = None
            current_text = []
            
            for line in content.split("\n"):
                if line.startswith("# "):
                    if current_section:
                        sections[current_section] = "\n".join(current_text).strip()
                    current_section = line[2:].strip().upper()
                    current_text = []
                elif current_section:
                    current_text.append(line)
            
            if current_section:
                sections[current_section] = "\n".join(current_text).strip()
            
            self.start_msg = sections.get("START", self.start_msg)
            self.about_msg = sections.get("ABOUT", self.about_msg)
            self.privacy_msg = sections.get("PRIVACY", self.privacy_msg)
            
            logging.info(f"Loaded policies from {self.policy_path}: sections={list(sections.keys())}")
        except Exception as e:
            logging.error(f"Error loading policies {self.policy_path}: {e}")

    def get_last_message(self):
        """Retrieve and consume the most recent processed window, thread-safe."""
        with self.msg_lock:
            tmp = self._last_processed_window
            self._last_processed_window = None
            return tmp

    async def _start_cmd(self, message: types.Message):
        """Handle the /start command with interactive buttons."""
        if message.chat is not None:
            self.chat_id = message.chat.id
        
        # Create buttons
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        builder = InlineKeyboardBuilder()
        builder.button(text="ℹ️ About", callback_data="show_about")
        builder.button(text="🛡️ Privacy", callback_data="show_privacy")
        
        await message.answer(self.start_msg, reply_markup=builder.as_markup())

    async def _about_cmd(self, message: types.Message):
        """Handle /about command."""
        await message.answer(self.about_msg)

    async def _privacy_cmd(self, message: types.Message):
        """Handle /privacy command."""
        await message.answer(self.privacy_msg)

    async def _kill_cmd(self, message: types.Message):
        """Handle global kill switch (admin only)."""
        user_id = message.from_user.id if message.from_user else None
        if user_id in self.admin_ids:
            await message.answer("⚠️ Global Kill Switch activated. Shutting down...")
            logging.critical(f"KILLED by admin {user_id}")
            self.stop()
            os._exit(0)
        else:
            await message.answer("❌ Access denied. Admin only.")

    async def _on_callback_query(self, callback: types.CallbackQuery):
        """Handle button clicks."""
        if callback.data == "show_about":
            await callback.message.answer(self.about_msg)
        elif callback.data == "show_privacy":
            await callback.message.answer(self.privacy_msg)
        await callback.answer()

    async def _on_message(self, message: types.Message):
        """Capture group messages into the buffer; flag reply if bot is tagged."""
        if message.text is None:
            return
        
        # Check DM support
        if message.chat.type == "private" and not self.dm_enabled:
            return
        
        # Filter out messages from other bots
        if message.from_user and (message.from_user.is_bot or self.is_user_muted(message.from_user.id)):
            return

        if message.chat is not None:
            self.chat_id = message.chat.id
            
        user = message.from_user
        name = "unknown user" if user is None else (user.full_name or user.username or str(user.id))
        text = message.text
        
        with self.msg_lock:
            self._message_buffer.append((time.time(), name, text, message.message_id))
            
            # Use rules from config
            is_tagged = self.bot_username and f"@{self.bot_username}" in text
            is_reply = (self.reply_on_reply and 
                        message.reply_to_message and 
                        message.reply_to_message.from_user and 
                        message.reply_to_message.from_user.id == self.bot_id)
            
            if not self.reply_only_on_tag or is_tagged or is_reply:
                self._should_reply = True
            
            if is_tagged or is_reply:
                self._last_processed_window = f"{name}: {text}"
                self._reply_to_id = message.message_id


    async def _window_manager(self):
        """Every window_seconds, batch buffered messages and surface them if bot was tagged."""
        while self.running:
            await asyncio.sleep(self.window_seconds)
            with self.msg_lock:
                if not self._message_buffer:
                    continue
                
                if self._should_reply:
                    # Batch messages
                    batched = "\n".join([f"{m[1]}: {m[2]}" for m in self._message_buffer])
                    self._last_processed_window = batched
                    # Use the last message's id for reply threading
                    self._reply_to_id = self._message_buffer[-1][3]
                    self._should_reply = False
                
                # Clear buffer (Retention rules apply: only keep for the window)
                self._message_buffer = []

    def is_user_muted(self, user_id):
        """Feature: User mute / cool-down after repeated abuse."""
        if user_id in self._muted_users:
            if time.time() < self._muted_users[user_id]:
                return True
            else:
                del self._muted_users[user_id]
                
        now = time.time()
        history = self._user_msg_rates.get(user_id, [])
        history = [ts for ts in history if now - ts < 10]
        history.append(now)
        self._user_msg_rates[user_id] = history
        
        if len(history) > 5:
            logging.warning(f"User {user_id} muted for spamming.")
            self._muted_users[user_id] = now + 120 # 2 minute cool-down
            return True
            
        return False

    async def _on_media_rejected(self, message: types.Message):
        """Feature: Block files, images, audio, voice notes."""
        logging.info("Denied capability invoked: Media/File uploaded. Discarding.")
        # Silently discard to prevent abuse surface / leakage
        pass

    async def _runner(self, token):
        """Build the aiogram bot, start polling, and run until stopped."""
        self.bot = Bot(token=token)
        self.dp = Dispatcher()
        
        try:
            # Get bot info for tag detection
            bot_info = await self.bot.get_me()
            self.bot_username = bot_info.username
            self.bot_id = bot_info.id
            
            self.dp.message.register(self._start_cmd, Command("start"))
            self.dp.message.register(self._about_cmd, Command("about"))
            self.dp.message.register(self._privacy_cmd, Command("privacy"))
            self.dp.message.register(self._kill_cmd, Command("kill"))
            self.dp.callback_query.register(self._on_callback_query)
            self.dp.message.register(self._on_message, F.text)
            self.dp.message.register(self._on_media_rejected, ~F.text)

            
            self.connected = True
            
            # Start window manager
            asyncio.create_task(self._window_manager())
            
            # Start polling as a task so we can cancel it
            self._polling_task = asyncio.create_task(self.dp.start_polling(self.bot, skip_updates=True, handle_signals=False))
            await self._polling_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.error(f"Telegram runner error: {e}")
        finally:
            self.connected = False
            await self.bot.session.close()

    def _thread_main(self, token):
        """Create a dedicated asyncio event loop and run the bot in it."""
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._runner(token))
        except Exception as e:
            logging.error(f"Telegram runner error in thread: {e}")
        finally:
            loop.close()
        self.loop = None

    def start(self, token, chat_id=None, config_path=None):
        """Launch the Telegram bot on a daemon thread and begin polling."""
        self.running = True
        self.chat_id = chat_id
        # Reload config if path provided
        if config_path is None:
            self.load_config(self.config_path)
            
        self.thread = threading.Thread(target=self._thread_main, args=(token,), daemon=True)
        self.thread.start()
        return self.thread
        # return "OK"

    def stop(self):
        """Signal the polling loop to stop gracefully."""
        self.running = False
        if self.loop and self._polling_task:
            self.loop.call_soon_threadsafe(self._polling_task.cancel)

    def send_message(self, text):
        """Send a text message to the active chat, dispatched to the bot's event loop."""
        text = text.replace("\\n", "\n")
        if not self.connected or self.bot is None or self.loop is None or self.chat_id is None:
            return
        
        fut = asyncio.run_coroutine_threadsafe(
            self.bot.send_message(chat_id=self.chat_id, text=text, reply_to_message_id=self._reply_to_id),
            self.loop,
        )
        try:
            fut.result(timeout=10)
        except Exception:
            pass

_channel = _TelegramChannel()

def getLastMessage():
    """Return the last processed batch window."""
    last_msg = _channel.get_last_message()
    if last_msg is None:
        return ""
        
    return str(last_msg)

def start_telegram(token, chat_id=None):
    """Initialize and start the Telegram bot."""
    return _channel.start(token, chat_id)

def stop_telegram():
    """Stop the Telegram bot."""
    _channel.stop()

def send_message(text):
    """Send a message to the active Telegram chat."""
    _channel.send_message(text)