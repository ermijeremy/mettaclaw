import asyncio
import time
import threading
import logging
import re
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command

import yaml
import os

def _safe_int(value, default):
    try:
        return int(value)
    except Exception:
        return default

def _normalize_user_id(value):
    try:
        return int(value)
    except Exception:
        return None

log_file_path = os.path.join(os.path.dirname(__file__), "..", "telegram_bot.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(log_file_path), logging.StreamHandler()],
)


class _TelegramChannel:
    """Telegram bot channel with windowed batching and bot-tag gating using aiogram."""

    def __init__(self, config_path=None):
        self.config_path = os.path.join(
            os.path.dirname(__file__), "..", "memory", "telegram_profile.yaml"
        )
        self.policy_path = os.path.join(
            os.path.dirname(__file__), "..", "memory", "policy.md"
        )
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
        self.window_seconds = 5
        self.reply_only_on_tag = True
        self.reply_on_reply = True
        self.admin_ids = []
        self.dm_enabled = False
        self.dm_treat_as_direct_tag = True
        self.purge_memory_enabled = True
        self.memory_inspect_enabled = True
        self.memory_delete_enabled = True
        self.memory_collection_name = "memories"
        self.chroma_db_path = os.path.join(os.path.dirname(__file__), "..", "chroma_db")
        self.history_path = os.path.join(os.path.dirname(__file__), "..", "memory", "history.metta")

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
        self._user_mute_counts = {}

        # Windowed batching state (per-chat)
        self._message_buffers = {}
        self._should_reply = {}
        self._reply_to_ids = {}
        self._paused_chats = set()
        self.search_disabled = False
        self._last_processed_window = None
        self._reply_to_id = None
        self._ready_windows = []
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
            self.window_seconds = tg_cfg.get("batching", {}).get("window_seconds", 10)
            self.reply_only_on_tag = tg_cfg.get("reply_only_when_directly_tagged", True)
            self.reply_on_reply = tg_cfg.get("reply_on_reply_to_bot", True)
            self.dm_enabled = tg_cfg.get("dm_support", {}).get("enabled", False)
            self.dm_treat_as_direct_tag = tg_cfg.get("dm_support", {}).get("if_enabled_treat_as_direct_tag", True)
            admin_cfg = config.get("admin_controls", {})
            self.admin_ids = [
                uid for uid in (_normalize_user_id(v) for v in admin_cfg.get("admin_ids", [])) if uid is not None
            ]
            self.purge_memory_enabled = admin_cfg.get("purge_memory", True)
            self.memory_inspect_enabled = admin_cfg.get("memory_inspect", True)
            self.memory_delete_enabled = admin_cfg.get("memory_delete", True)

            learning_cfg = config.get("internal_learning", {}).get("durable_memory", {})
            self.memory_collection_name = learning_cfg.get("collection_name", "memories")
            configured_db_path = learning_cfg.get("db_path")
            if configured_db_path:
                if os.path.isabs(configured_db_path):
                    self.chroma_db_path = configured_db_path
                else:
                    # Resolve relative DB paths against repo root, not process cwd.
                    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
                    self.chroma_db_path = os.path.abspath(os.path.join(repo_root, configured_db_path))

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
            if self._ready_windows:
                ready_chat_id, text, reply_id = self._ready_windows.pop(0)
                self.chat_id = ready_chat_id
                self._reply_to_id = reply_id
                return text
            return None

    async def _start_cmd(self, message: types.Message):
        """Handle the /start command with interactive buttons."""
        if message.chat is not None:
            self.chat_id = message.chat.id

        from aiogram.utils.keyboard import InlineKeyboardBuilder
        builder = InlineKeyboardBuilder()
        builder.button(text="ℹ️ About", callback_data="show_about")
        builder.button(text="🛡️ Privacy", callback_data="show_privacy")

        if message.from_user and message.from_user.id in self.admin_ids:
            builder.button(text="⚙️ Admin Panel", callback_data="admin_panel")

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

    async def _memory_cmd_help(self, message: types.Message):
        """Show memory admin subcommands."""
        if not self._is_admin(message.from_user):
            return await message.answer("❌ Access denied.")
        lines = ["🧠 Memory Admin Commands (Chroma + history):"]
        if self.memory_inspect_enabled:
            lines.append("/memory_list [limit] - List recent memory ids")
            lines.append("/memory_get <id> - Inspect a memory record")
            lines.append("/memory_stats - Show memory collection stats")
            lines.append("/history_list [limit] - List recent history entries")
            lines.append("/history_get <index> - Inspect one history entry")
            lines.append("/history_stats - Show history file stats")
        if self.memory_delete_enabled:
            lines.append("/memory_delete <id> - Delete one memory record")
            lines.append("/history_delete <index> - Delete one history entry")
        if self.purge_memory_enabled:
            lines.append("/purge --yes - Purge all memory records")
            lines.append("/history_purge --yes - Purge history")
        if len(lines) == 1:
            lines.append("Memory admin commands are disabled by config.")
        await message.answer("\n".join(lines))

    def _read_history_entries(self):
        """ Parse top-level history entries in memory/history.metta. """
        if not os.path.exists(self.history_path):
            return []

        with open(self.history_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()

        starts = list(re.finditer(r'^\("\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"', text, re.MULTILINE))
        if not starts:
            return []

        entries = []
        for i, match in enumerate(starts):
            start = match.start()
            end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
            raw = text[start:end].strip()
            if not raw:
                continue
            ts = match.group(0)[2:21]
            entries.append({"timestamp": ts, "raw": raw})
        return entries

    def _write_history_entries(self, entries):
        os.makedirs(os.path.dirname(self.history_path), exist_ok=True)
        with open(self.history_path, "w", encoding="utf-8") as f:
            if entries:
                f.write("\n\n".join(entry["raw"].strip() for entry in entries).strip() + "\n")

    async def _history_list_cmd(self, message: types.Message):
        """List recent history entries."""
        if not self._is_admin(message.from_user):
            return await message.answer("❌ Access denied.")
        if not self.memory_inspect_enabled:
            return await message.answer("⚠️ History inspect commands are disabled by config.")

        args = (message.text or "").split()
        limit = 10
        if len(args) > 1:
            limit = max(1, min(_safe_int(args[1], 10), 50))

        try:
            entries = self._read_history_entries()
            total = len(entries)
            if total == 0:
                return await message.answer("ℹ️ history.metta has no parsed entries.")

            selected = entries[-limit:]
            base_index = total - len(selected) + 1
            lines = [f"📜 history: showing {len(selected)}/{total} latest entries"]
            for offset, entry in enumerate(selected):
                idx = base_index + offset
                snippet = " ".join(entry["raw"].splitlines())
                if len(snippet) > 100:
                    snippet = snippet[:97] + "..."
                lines.append(f"- #{idx} | {entry['timestamp']} | {snippet}")

            out = "\n".join(lines)
            if len(out) > 3900:
                out = out[:3897] + "..."
            await message.answer(out)
        except Exception as e:
            await message.answer(f"❌ Failed to list history entries: {e}")

    async def _history_get_cmd(self, message: types.Message):
        """Inspect one history entry, 1-based index."""
        if not self._is_admin(message.from_user):
            return await message.answer("❌ Access denied.")
        if not self.memory_inspect_enabled:
            return await message.answer("⚠️ History inspect commands are disabled by config.")

        args = (message.text or "").split(maxsplit=1)
        if len(args) < 2:
            return await message.answer("Usage: /history_get <index>")

        idx = _safe_int(args[1].strip(), -1)
        if idx < 1:
            return await message.answer("Usage: /history_get <index>")

        try:
            entries = self._read_history_entries()
            total = len(entries)
            if total == 0:
                return await message.answer("ℹ️ history.metta has no parsed entries.")
            if idx > total:
                return await message.answer(f"ℹ️ history entry index out of range: {idx} (max {total})")

            entry = entries[idx - 1]
            out = f"📜 history entry #{idx}\nTimestamp: {entry['timestamp']}\n\n{entry['raw']}"
            if len(out) > 3900:
                out = out[:3897] + "..."
            await message.answer(out)
        except Exception as e:
            await message.answer(f"❌ Failed to inspect history entry: {e}")

    async def _history_stats_cmd(self, message: types.Message):
        """Show basic stats for history."""
        if not self._is_admin(message.from_user):
            return await message.answer("❌ Access denied.")
        if not self.memory_inspect_enabled:
            return await message.answer("⚠️ History inspect commands are disabled by config.")

        try:
            entries = self._read_history_entries()
            size_bytes = os.path.getsize(self.history_path) if os.path.exists(self.history_path) else 0
            latest = entries[-1]["timestamp"] if entries else "n/a"
            lines = [
                "📜 History Stats",
                f"Path: {self.history_path}",
                f"Entries: {len(entries)}",
                f"Size: {size_bytes} bytes",
                f"Latest timestamp: {latest}",
            ]
            await message.answer("\n".join(lines))
        except Exception as e:
            await message.answer(f"❌ Failed to inspect history stats: {e}")

    async def _history_delete_cmd(self, message: types.Message):
        """Delete one history by 1-based index id."""
        if not self._is_admin(message.from_user):
            return await message.answer("❌ Access denied.")
        if not self.memory_delete_enabled:
            return await message.answer("⚠️ History delete command is disabled by config.")

        args = (message.text or "").split(maxsplit=1)
        if len(args) < 2:
            return await message.answer("Usage: /history_delete <index>")

        idx = _safe_int(args[1].strip(), -1)
        if idx < 1:
            return await message.answer("Usage: /history_delete <index>")

        try:
            entries = self._read_history_entries()
            total = len(entries)
            if total == 0:
                return await message.answer("ℹ️ history.metta has no parsed entries.")
            if idx > total:
                return await message.answer(f"ℹ️ history entry index out of range: {idx} (max {total})")

            removed = entries.pop(idx - 1)
            self._write_history_entries(entries)
            await message.answer(f"✅ Deleted history entry #{idx} ({removed['timestamp']}).")
        except Exception as e:
            await message.answer(f"❌ Failed to delete history entry: {e}")

    async def _history_purge_cmd(self, message: types.Message):
        """Purge history content."""
        if not self._is_admin(message.from_user):
            return await message.answer("❌ Access denied.")
        if not self.purge_memory_enabled:
            return await message.answer("⚠️ History purge is disabled by config.")

        args = (message.text or "").split()
        if "--yes" not in args:
            return await message.answer("⚠️ Confirm purge with: /history_purge --yes")

        try:
            self._write_history_entries([])
            await message.answer("🗑️ history.metta purged successfully.")
        except Exception as e:
            await message.answer(f"❌ Failed to purge history.metta: {e}")

    def _get_memory_collection(self):
        """Get the persistent memories collection."""
        import chromadb

        client = chromadb.PersistentClient(path=self.chroma_db_path)

        preferred_names = [self.memory_collection_name, "memories", "memory"]
        seen = set()
        candidate_names = [n for n in preferred_names if n and not (n in seen or seen.add(n))]

        # Pick an existing non-empty collection first, otherwise fallback to configured/default one.
        existing = {c.name: c for c in client.list_collections()}
        selected_name = None
        max_count = -1
        for name in candidate_names:
            collection = existing.get(name)
            if collection is None:
                continue
            count = collection.count()
            if count > max_count:
                max_count = count
                selected_name = name

        if selected_name is None:
            selected_name = self.memory_collection_name or "memories"

        return client, client.get_or_create_collection(name=selected_name)

    def _is_admin(self, user):
        user_id = getattr(user, "id", None)
        user_id = _normalize_user_id(user_id)
        return user_id is not None and user_id in self.admin_ids

    async def _memory_list_cmd(self, message: types.Message):
        """List recent memory ids."""
        if not self._is_admin(message.from_user):
            return await message.answer("❌ Access denied.")
        if not self.memory_inspect_enabled:
            return await message.answer("⚠️ Memory inspect commands are disabled by config.")

        args = (message.text or "").split()
        limit = 10
        if len(args) > 1:
            limit = max(1, min(_safe_int(args[1], 10), 50))

        try:
            _, collection = self._get_memory_collection()
            total = collection.count()

            if total == 0:
                return await message.answer("ℹ️ No memory records found.")

            limit = min(limit, total)
            offset = max(0, total - limit)
            rows = collection.get(
                limit=limit,
                offset=offset,
                include=["documents", "metadatas"],
            )

            ids = rows.get("ids", [])
            docs = rows.get("documents", [])
            metas = rows.get("metadatas", [])

            lines = [f"🧠 Memories: showing {len(ids)}/{total} latest records"]
            for i, mem_id in enumerate(ids):
                meta = metas[i] if i < len(metas) and metas[i] else {}
                ts = meta.get("timestamp") or meta.get("time") or "n/a"
                doc = docs[i] if i < len(docs) and docs[i] else ""
                snippet = doc.replace("\n", " ").strip()
                if len(snippet) > 100:
                    snippet = snippet[:97] + "..."
                lines.append(f"- {mem_id} | {ts} | {snippet}")

            out = "\n".join(lines)
            if len(out) > 3900:
                out = out[:3897] + "..."
            await message.answer(out)
        except Exception as e:
            await message.answer(f"❌ Failed to list memories: {e}")

    async def _memory_stats_cmd(self, message: types.Message):
        """Inspect basic memory stats."""
        if not self._is_admin(message.from_user):
            return await message.answer("❌ Access denied.")
        if not self.memory_inspect_enabled:
            return await message.answer("⚠️ Memory inspect commands are disabled by config.")

        try:
            client, collection = self._get_memory_collection()
            names = [c.name for c in client.list_collections()]
            total = collection.count()
            lines = [
                "🧠 Memory Store Stats",
                f"DB path: {self.chroma_db_path}",
                f"Active collection: {collection.name}",
                f"Record count: {total}",
                f"Collections: {', '.join(names) if names else 'none'}",
            ]
            await message.answer("\n".join(lines))
        except Exception as e:
            await message.answer(f"❌ Failed to inspect memory stats: {e}")

    async def _memory_get_cmd(self, message: types.Message):
        """Inspect one memory record by id."""
        if not self._is_admin(message.from_user):
            return await message.answer("❌ Access denied.")
        if not self.memory_inspect_enabled:
            return await message.answer("⚠️ Memory inspect commands are disabled by config.")

        args = (message.text or "").split(maxsplit=1)
        if len(args) < 2:
            return await message.answer("Usage: /memory_get <id>")

        mem_id = args[1].strip()
        if not mem_id:
            return await message.answer("Usage: /memory_get <id>")

        try:
            _, collection = self._get_memory_collection()
            rows = collection.get(ids=[mem_id], include=["documents", "metadatas"])
            ids = rows.get("ids", [])

            if not ids:
                return await message.answer(f"ℹ️ Memory id not found: {mem_id}")

            doc = (rows.get("documents") or [""])[0] or ""
            meta = (rows.get("metadatas") or [{}])[0] or {}
            ts = meta.get("timestamp") or meta.get("time") or "n/a"

            out = f"🧠 Memory Record\nID: {mem_id}\nTimestamp: {ts}\nText:\n{doc}"
            if len(out) > 3900:
                out = out[:3897] + "..."
            await message.answer(out)
        except Exception as e:
            await message.answer(f"❌ Failed to inspect memory: {e}")

    async def _memory_delete_cmd(self, message: types.Message):
        """Delete one memory record by id."""
        if not self._is_admin(message.from_user):
            return await message.answer("❌ Access denied.")
        if not self.memory_delete_enabled:
            return await message.answer("⚠️ Memory delete command is disabled by config.")

        args = (message.text or "").split(maxsplit=1)
        if len(args) < 2:
            return await message.answer("Usage: /memory_delete <id>")

        mem_id = args[1].strip()
        if not mem_id:
            return await message.answer("Usage: /memory_delete <id>")

        try:
            _, collection = self._get_memory_collection()
            probe = collection.get(ids=[mem_id], include=[])
            if not probe.get("ids"):
                return await message.answer(f"ℹ️ Memory id not found: {mem_id}")

            collection.delete(ids=[mem_id])
            await message.answer(f"✅ Deleted memory record: {mem_id}")
        except Exception as e:
            await message.answer(f"❌ Failed to delete memory: {e}")

    async def _pause_cmd(self, message: types.Message):
        """Handle /pause command (admin only)."""
        if not self._is_admin(message.from_user):
            return await message.answer("❌ Access denied.")

        target_chat = message.chat.id
        args = message.text.split()
        if len(args) > 1:
            target_chat = args[1]

        if target_chat in self._paused_chats:
            self._paused_chats.remove(target_chat)
            await message.answer(f"▶️ Chat {target_chat} unpaused.")
        else:
            self._paused_chats.add(target_chat)
            await message.answer(f"⏸️ Chat {target_chat} paused.")

    async def _togglesearch_cmd(self, message: types.Message):
        """Handle /togglesearch command (admin only)."""
        if not self._is_admin(message.from_user):
            return await message.answer("❌ Access denied.")

        self.search_disabled = not self.search_disabled
        state = "DISABLED" if self.search_disabled else "ENABLED"
        await message.answer(f"🔍 Web search is now {state}.")

    async def _purge_cmd(self, message: types.Message):
        """Handle /purge command (admin only)."""
        if not self._is_admin(message.from_user):
            return await message.answer("❌ Access denied.")
        if not self.purge_memory_enabled:
            return await message.answer("⚠️ Memory purge is disabled by config.")

        args = (message.text or "").split()
        if "--yes" not in args:
            return await message.answer("⚠️ Confirm purge with: /purge --yes")

        try:
            client, collection = self._get_memory_collection()
            client.delete_collection(collection.name)
            client.get_or_create_collection(name=collection.name)
            await message.answer("🗑️ Long-term memory purged successfully.")
        except Exception as e:
            await message.answer(f"❌ Failed to purge memory: {e}")

    async def _on_callback_query(self, callback: types.CallbackQuery):
        """Handle button clicks."""
        if callback.data == "show_about":
            await callback.message.answer(self.about_msg)
        elif callback.data == "show_privacy":
            await callback.message.answer(self.privacy_msg)
        elif callback.data == "admin_panel":
            if self._is_admin(callback.from_user):
                cmd_list = (
                    "🛠 **Admin Commands:**\n"
                    "/pause [chat_id] - Pause/unpause a chat\n"
                    "/togglesearch - Enable/Disable Web Search\n"
                    "/kill - Shutdown Bot globally"
                )
                if self.memory_inspect_enabled:
                    cmd_list += "\n/memory_list [limit] - List memory records"
                    cmd_list += "\n/memory_get <id> - Inspect one memory"
                    cmd_list += "\n/memory_stats - Show memory stats"
                    cmd_list += "\n/history_list [limit] - List history entries"
                    cmd_list += "\n/history_get <index> - Inspect one history entry"
                    cmd_list += "\n/history_stats - Show history stats"
                if self.memory_delete_enabled:
                    cmd_list += "\n/memory_delete <id> - Delete one memory"
                    cmd_list += "\n/history_delete <index> - Delete one history entry"
                if self.purge_memory_enabled:
                    cmd_list += "\n/purge --yes - Wipe all memory records"
                    cmd_list += "\n/history_purge --yes - Wipe history.metta"
                await callback.message.answer(cmd_list)
            else:
                await callback.message.answer("❌ Access denied.")
        await callback.answer()

    async def _on_message(self, message: types.Message):
        """Capture group messages into the buffer; flag reply if bot is tagged."""
        if message.text is None:
            return

        if message.chat.id in self._paused_chats:
            return

        # Check DM support
        if message.chat.type == "private":
            if getattr(message.from_user, "id", None) not in self.admin_ids and not self.dm_enabled:
                return

        # Filter out messages from other bots
        if message.from_user:
            if message.from_user.is_bot:
                return
            if await self.is_user_muted(message.from_user):
                return

        if message.chat is not None:
            chat_id = message.chat.id

        user = message.from_user
        name = "unknown user" if user is None else (user.full_name or user.username or str(user.id))
        text = message.text

        with self.msg_lock:
            if chat_id not in self._message_buffers:
                self._message_buffers[chat_id] = []
                self._should_reply[chat_id] = False

            self._message_buffers[chat_id].append((time.time(), name, text, message.message_id))

            # Limiting to 50 msg per chat
            self._message_buffers[chat_id] = self._message_buffers[chat_id][-50:]

            # Use rules from config
            is_tagged = self.bot_username and f"@{self.bot_username}" in text
            is_dm_direct = (
                message.chat.type == "private"
                and self.dm_enabled
                and self.dm_treat_as_direct_tag
            )
            is_reply = (
                self.reply_on_reply
                and message.reply_to_message
                and message.reply_to_message.from_user
                and message.reply_to_message.from_user.id == self.bot_id
            )

            if not self.reply_only_on_tag or is_tagged or is_reply or is_dm_direct:
                self._should_reply[chat_id] = True

    async def _window_manager(self):
        """Every window_seconds, batch buffered messages and surface them if bot was tagged."""
        while self.running:
            await asyncio.sleep(self.window_seconds)
            with self.msg_lock:
                for chat_id in list(self._message_buffers.keys()):
                    buffer = self._message_buffers[chat_id]
                    if not buffer:
                        continue

                    if self._should_reply.get(chat_id, False):
                        batched = "\n".join([f"{m[1]}: {m[2]}" for m in buffer])
                        reply_id = buffer[-1][3]
                        self._ready_windows.append((chat_id, batched, reply_id))

                    self._message_buffers[chat_id] = []
                    self._should_reply[chat_id] = False

    async def is_user_muted(self, user: types.User):
        """Feature: User mute / cool-down after repeated abuse."""
        user_id = user.id
        if user_id in self._muted_users:
            if time.time() < self._muted_users[user_id]:
                return True
            else:
                del self._muted_users[user_id]

        now = time.time()
        history = self._user_msg_rates.get(user_id, [])
        history = [ts for ts in history if now - ts < 10]  # 10 second window for rate limiting
        history.append(now)
        self._user_msg_rates[user_id] = history

        if len(history) > 5:
            mute_count = self._user_mute_counts.get(user_id, 0) + 1
            self._user_mute_counts[user_id] = mute_count

            username = user.username or user.full_name or str(user_id)
            logging.warning(f"User with id: {user_id} | username: {username} muted for spamming.")
            self._muted_users[user_id] = now + 120  # 2 minute cool-down

            if mute_count >= 3:
                for admin_id in self.admin_ids:
                    try:
                        alert_msg = (
                            f"🚨 **Spam Alert** 🚨\n"
                            f"User @{username} (ID: {user_id}) has been temporarily muted for spamming.\n"
                            f"Total times muted: {mute_count}"
                        )
                        await self.bot.send_message(chat_id=admin_id, text=alert_msg)
                    except Exception as e:
                        logging.error(f"Failed to notify admin {admin_id}: {e}")

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
            self.dp.message.register(self._pause_cmd, Command("pause"))
            self.dp.message.register(self._togglesearch_cmd, Command("togglesearch"))
            self.dp.message.register(self._memory_cmd_help, Command("memory"))
            self.dp.message.register(self._memory_list_cmd, Command("memory_list"))
            self.dp.message.register(self._memory_get_cmd, Command("memory_get"))
            self.dp.message.register(self._memory_stats_cmd, Command("memory_stats"))
            self.dp.message.register(self._memory_delete_cmd, Command("memory_delete"))
            self.dp.message.register(self._history_list_cmd, Command("history_list"))
            self.dp.message.register(self._history_get_cmd, Command("history_get"))
            self.dp.message.register(self._history_stats_cmd, Command("history_stats"))
            self.dp.message.register(self._history_delete_cmd, Command("history_delete"))
            self.dp.message.register(self._history_purge_cmd, Command("history_purge"))
            self.dp.message.register(self._purge_cmd, Command("purge"))
            self.dp.callback_query.register(self._on_callback_query)
            self.dp.message.register(self._on_message, F.text)
            self.dp.message.register(self._on_media_rejected, ~F.text)

            self.connected = True

            # Start window manager
            asyncio.create_task(self._window_manager())

            # Start polling as a task so we can cancel it
            self._polling_task = asyncio.create_task(
                self.dp.start_polling(self.bot, skip_updates=True, handle_signals=False)
            )
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
    # Keep timeout above window size to avoid polling race at exact boundary.
    timeout = max(6, int(_channel.window_seconds) + 2)
    start_time = time.time()
    while time.time() - start_time < timeout:
        last_msg = _channel.get_last_message()
        if last_msg is not None:
            return str(last_msg)

        time.sleep(1)

    return ""


def start_telegram(token, chat_id=None):
    """Initialize and start the Telegram bot."""
    if isinstance(token, list) and len(token) > 0:
        token = str(token[0])

    token = str(token).strip("\"' ")

    if isinstance(chat_id, list) and len(chat_id) > 0:
        chat_id = str(chat_id[0])

    if chat_id is not None:
        chat_id = str(chat_id).strip("\"' ")

    return _channel.start(token, chat_id)


def stop_telegram():
    """Stop the Telegram bot."""
    _channel.stop()


def send_message(text):
    """Send a message to the active Telegram chat."""
    _channel.send_message(text)

def is_search_disabled():
    """Check if admin disabled searching."""
    return _channel.search_disabled

def alert_ethics_violation(tool_name):
    """Allow MeTTa to trigger an ethics alert DM to admins."""
    if _channel.loop and _channel.bot:
        for admin_id in _channel.admin_ids:
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    _channel.bot.send_message(chat_id=admin_id, text=f"🚨 Ethics Pass Triggered!\nAction Blocked: {tool_name}"),
                    _channel.loop
                )
            except Exception:
                logging.error(f"Failed to send ethics alert to admin {admin_id} for tool {tool_name}")
