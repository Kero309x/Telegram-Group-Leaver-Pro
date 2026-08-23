import asyncio
import os
import socket
import threading
import time
import uuid
from typing import Dict, Any, List

from flask import Flask, render_template_string, request, jsonify
import aiohttp
from telethon import TelegramClient, errors, functions, types
from telethon.tl.types import Channel, Chat, User

__author__ = "Kero309x"
__github__ = "https://github.com/Kero309x"
__version__ = "1.0"

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Global store
sessions_store: Dict[str, Dict[str, Any]] = {}
tasks_store: Dict[str, Dict[str, Any]] = {}

os.makedirs("tg_sessions", exist_ok=True)
os.makedirs("logs_export", exist_ok=True)

# Dedicated background thread event loop
loop = asyncio.new_event_loop()

def run_background_loop(loop_to_run):
    asyncio.set_event_loop(loop_to_run)
    loop_to_run.run_forever()

threading.Thread(target=run_background_loop, args=(loop,), daemon=True).start()


def get_local_ip() -> str:
    """Detects local network IP for mobile device access."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


async def send_log_to_bot(bot_token: str, chat_id: str, message: str):
    """Sends log notifications to Telegram Bot."""
    if not bot_token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with aiohttp.ClientSession() as http_sess:
            async with http_sess.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=8)):
                pass
    except Exception:
        pass


def is_dialog_target(entity) -> bool:
    """Checks whether an entity is a channel, group, supergroup, or bot."""
    if isinstance(entity, Channel):
        return True
    if isinstance(entity, Chat):
        return not getattr(entity, "deactivated", False) and not getattr(entity, "left", False)
    if isinstance(entity, User) and getattr(entity, "bot", False):
        return True
    return False


def get_entity_type(entity) -> str:
    """Classifies entity into Channel, Supergroup, Group, or Bot."""
    if isinstance(entity, Channel):
        if getattr(entity, "megagroup", False):
            return "Supergroup"
        return "Channel"
    if isinstance(entity, Chat):
        return "Group"
    if isinstance(entity, User) and getattr(entity, "bot", False):
        return "Bot"
    return "Unknown"


def entity_display_name(entity) -> str:
    """Retrieves the clean human-readable title."""
    title = getattr(entity, "title", None)
    if title:
        return title.strip()
    first = getattr(entity, "first_name", "")
    last = getattr(entity, "last_name", "")
    username = getattr(entity, "username", "")
    if first or last:
        return f"{first} {last}".strip()
    if username:
        return f"@{username}"
    return str(entity.id)


async def execute_leave_or_delete(client: TelegramClient, entity, max_retries=3) -> tuple:
    """
    Completely and permanently leaves channels/groups or stops & wipes bots from the chat list.
    Returns (success: bool, status_msg: str)
    """
    is_bot = isinstance(entity, User) and getattr(entity, "bot", False)

    for attempt in range(1, max_retries + 1):
        try:
            # 1. Resolve InputPeer / InputEntity properly
            input_entity = await client.get_input_entity(entity)

            if is_bot:
                # A. Stop / Block the Bot (Official Stop Bot)
                try:
                    await client(functions.contacts.BlockRequest(id=input_entity))
                except Exception:
                    pass

                # B. Permanently wipe entire chat history in both directions (Revoke + Clear)
                try:
                    await client(functions.messages.DeleteHistoryRequest(
                        peer=input_entity,
                        max_id=0,
                        just_clear=False,
                        revoke=True
                    ))
                except Exception:
                    pass

                # C. Remove dialog completely from the chat list
                try:
                    await client.delete_dialog(input_entity, revoke=True)
                except Exception:
                    pass

                return True, "Bot Stopped & Completely Wiped (100% Cleared)"

            elif isinstance(entity, Channel):
                await client(functions.channels.LeaveChannelRequest(input_entity))
                try:
                    await client.delete_dialog(input_entity, revoke=True)
                except Exception:
                    pass
                return True, "Left & Removed Channel"

            elif isinstance(entity, Chat):
                try:
                    await client(functions.messages.DeleteChatUserRequest(
                        chat_id=entity.id,
                        user_id=types.InputUserSelf(),
                        revoke_history=True
                    ))
                except Exception:
                    pass
                try:
                    await client.delete_dialog(input_entity, revoke=True)
                except Exception:
                    pass
                return True, "Left & Removed Group"

            else:
                await client.delete_dialog(input_entity, revoke=True)
                return True, "Cleaned Dialog"

        except errors.FloodWaitError as e:
            await asyncio.sleep(e.seconds)
            continue

        except (errors.ChatAdminRequiredError, errors.rpcerrorlist.ChatAdminRequiredError):
            return False, "Skipped (Admin/Owner Rights Required)"

        except (errors.UserNotParticipantError, errors.rpcerrorlist.UserNotParticipantError):
            return True, "Already Left/Cleaned"

        except errors.rpcerrorlist.ChatForbiddenError:
            return True, "Forbidden/Deleted"

        except Exception as e:
            err_str = str(e)
            if attempt < max_retries:
                await asyncio.sleep(1.5 ** attempt)
            else:
                return False, f"Error: {err_str[:45]}"

    return False, "Max retries reached"


async def execute_leaving_process(task_id: str, client: TelegramClient, targets: List[Any], bot_token: str, log_user_id: str):
    """Executes the batch removal process with real-time feedback."""
    task = tasks_store[task_id]
    total = len(targets)
    task["total"] = total
    task["status"] = "running"
    task["logs"].append(f"[{time.strftime('%H:%M:%S')}] Started task for {total} selected item(s)...")

    left_count = 0
    failed_count = 0

    try:
        for idx, entity in enumerate(targets, start=1):
            if task.get("cancelled", False):
                task["status"] = "cancelled"
                task["logs"].append(f"[{time.strftime('%H:%M:%S')}] Task stopped by user.")
                break

            name = entity_display_name(entity)
            etype = get_entity_type(entity)
            task["current_index"] = idx
            task["current_name"] = name

            min_d = float(task.get("min_delay", 2.0))
            max_d = float(task.get("max_delay", 5.0))
            if max_d < min_d:
                max_d = min_d

            delay = min_d + ((idx % 5) / 5.0) * (max_d - min_d) if max_d > min_d else min_d
            if idx > 1 and delay > 0:
                task["current_delay"] = round(delay, 1)
                await asyncio.sleep(delay)

            success, detail = await execute_leave_or_delete(client, entity)

            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            time_short = time.strftime("%H:%M:%S")

            if success:
                left_count += 1
                task["left"] = left_count
                task["logs"].append(f"[{time_short}] [{idx}/{total}] [✓] {detail}: {name}")
            else:
                failed_count += 1
                task["failed"] = failed_count
                task["logs"].append(f"[{time_short}] [{idx}/{total}] [✗] {detail}: {name}")

            log_msg = (
                f"<b>TG Cleaner Log</b>\n"
                f"<b>Target:</b> {name} (<i>{etype}</i>)\n"
                f"<b>Status:</b> {'Success' if success else 'Failed'} - {detail}\n"
                f"<b>Time:</b> {timestamp}\n"
                f"<i>Powered by @Kero309x</i>"
            )
            asyncio.create_task(send_log_to_bot(bot_token, log_user_id, log_msg))

        if task["status"] != "cancelled":
            task["status"] = "completed"
            final_report = (
                f"<b>Telegram Cleaning Complete</b>\n"
                f"<b>Total Processed:</b> {total}\n"
                f"<b>Successfully Cleaned/Left:</b> {left_count}\n"
                f"<b>Failed/Skipped:</b> {failed_count}\n"
                f"<i>Created by Kero309x</i>"
            )
            task["logs"].append(f"[{time.strftime('%H:%M:%S')}] Complete! Success: {left_count}, Failed: {failed_count}")
            await send_log_to_bot(bot_token, log_user_id, final_report)

    except Exception as e:
        task["status"] = "error"
        task["logs"].append(f"[{time.strftime('%H:%M:%S')}] Critical error: {str(e)}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <meta name="theme-color" content="#020617">
    <title>TG Cleaner Pro v1.0 - By Kero309x</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'Plus Jakarta Sans', sans-serif; -webkit-tap-highlight-color: transparent; }
        code, pre, .font-mono { font-family: 'JetBrains Mono', monospace; }
        ::-webkit-scrollbar { width: 5px; height: 5px; }
        ::-webkit-scrollbar-track { background: #090d16; }
        ::-webkit-scrollbar-thumb { background: #1e293b; border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background: #334155; }
    </style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen py-4 sm:py-8 px-3 sm:px-6 flex flex-col justify-between selection:bg-cyan-500 selection:text-slate-950">
    <div class="max-w-4xl w-full mx-auto space-y-4 sm:space-y-6">
        
        <!-- Header -->
        <header class="bg-slate-900/90 backdrop-blur-md border border-slate-800/80 rounded-2xl p-4 sm:p-5 shadow-2xl flex flex-col sm:flex-row items-center justify-between gap-3 sm:gap-4">
            <div class="flex items-center space-x-3.5 w-full sm:w-auto justify-between sm:justify-start">
                <div class="flex items-center space-x-3">
                    <div class="w-11 h-11 sm:w-12 sm:h-12 rounded-xl bg-gradient-to-tr from-cyan-500 via-sky-500 to-purple-600 flex items-center justify-center shadow-lg shadow-cyan-500/25 flex-shrink-0">
                        <i class="fa-brands fa-telegram text-xl sm:text-2xl text-white"></i>
                    </div>
                    <div>
                        <div class="flex items-center gap-2">
                            <h1 class="text-lg sm:text-xl font-bold bg-gradient-to-r from-cyan-400 via-sky-300 to-purple-400 bg-clip-text text-transparent">
                                TG Cleaner Pro
                            </h1>
                            <span class="px-2 py-0.5 text-[10px] font-bold rounded-full bg-purple-950 text-purple-300 border border-purple-800/60">v1.0</span>
                        </div>
                        <p class="text-slate-400 text-xs mt-0.5 flex items-center gap-1.5">
                            <span>Channels, Groups & Bots</span>
                            <span>•</span>
                            <a href="https://github.com/Kero309x" target="_blank" class="text-cyan-400 hover:underline flex items-center gap-1">
                                <i class="fa-brands fa-github text-[11px]"></i> @Kero309x
                            </a>
                        </p>
                    </div>
                </div>

                <div class="sm:hidden">
                    <span id="badge-mobile" class="px-2.5 py-1 text-[11px] font-semibold rounded-full bg-slate-800 text-slate-400 border border-slate-700/50">Idle</span>
                </div>
            </div>

            <!-- Action Controls -->
            <div class="flex items-center justify-between sm:justify-end w-full sm:w-auto gap-2 pt-2 sm:pt-0 border-t sm:border-t-0 border-slate-800/60">
                <a href="https://github.com/Kero309x" target="_blank" class="text-xs font-semibold px-3 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 transition flex items-center gap-1.5 border border-slate-700/60">
                    <i class="fa-solid fa-star text-amber-400 text-xs"></i> <span class="hidden xs:inline">Star on</span> GitHub
                </a>
                <button onclick="clearSavedData()" title="Reset credentials" class="text-xs text-slate-400 hover:text-rose-400 transition flex items-center gap-1 bg-slate-950 px-3 py-2 rounded-xl border border-slate-800">
                    <i class="fa-solid fa-rotate-left text-[10px]"></i> Reset
                </button>
                <span id="badge" class="hidden sm:inline-block px-3 py-1.5 text-xs font-semibold rounded-full bg-slate-800 text-slate-400 border border-slate-700/50">Idle</span>
            </div>
        </header>

        <!-- Network Access Banner -->
        <div class="bg-cyan-950/30 border border-cyan-800/40 rounded-xl p-3 flex items-center justify-between text-xs text-slate-300">
            <div class="flex items-center space-x-2 truncate">
                <i class="fa-solid fa-mobile-screen-button text-cyan-400 text-sm flex-shrink-0"></i>
                <span class="text-slate-400 hidden sm:inline">Mobile Wi-Fi URL:</span>
                <span class="font-mono text-cyan-300 truncate font-semibold">http://{{ local_ip }}:5000</span>
            </div>
            <button onclick="navigator.clipboard.writeText('http://{{ local_ip }}:5000'); showCustomModal('URL Copied', 'Mobile Wi-Fi URL has been copied to your clipboard!', 'success');" class="text-[11px] bg-cyan-900/60 hover:bg-cyan-800 text-cyan-200 px-2.5 py-1 rounded-lg border border-cyan-700/50 flex-shrink-0 transition">
                <i class="fa-solid fa-copy mr-1"></i> Copy
            </button>
        </div>

        <!-- Step 1: Config & Auth Form -->
        <section id="step-auth" class="bg-slate-900/70 border border-slate-800/80 rounded-2xl p-5 sm:p-6 shadow-xl space-y-4 sm:space-y-5 backdrop-blur-sm">
            <div class="border-b border-slate-800 pb-3 flex items-center justify-between">
                <h2 class="text-sm sm:text-base font-semibold text-slate-200 flex items-center">
                    <i class="fa-solid fa-key text-cyan-400 mr-2.5 text-sm"></i> 1. Telegram Credentials
                </h2>
                <span class="text-[10px] sm:text-[11px] text-emerald-400 bg-emerald-950/40 border border-emerald-800/50 px-2 py-0.5 rounded-md flex items-center gap-1">
                    <i class="fa-solid fa-floppy-disk text-[10px]"></i> Auto-Saved
                </span>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-2 gap-3 sm:gap-4">
                <div>
                    <label class="block text-xs font-medium text-slate-300 mb-1.5">API ID <span class="text-rose-400">*</span></label>
                    <input id="api_id" oninput="saveInputs()" type="number" placeholder="e.g. 12345678" class="w-full bg-slate-950 border border-slate-800 rounded-xl px-3.5 py-2.5 text-sm focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500 focus:outline-none transition">
                </div>
                <div>
                    <label class="block text-xs font-medium text-slate-300 mb-1.5">API Hash <span class="text-rose-400">*</span></label>
                    <input id="api_hash" oninput="saveInputs()" type="text" placeholder="e.g. 0123456789abcdef..." class="w-full bg-slate-950 border border-slate-800 rounded-xl px-3.5 py-2.5 text-sm focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500 focus:outline-none transition">
                </div>
            </div>

            <div>
                <label class="block text-xs font-medium text-slate-300 mb-1.5">Phone Number (With country code +) <span class="text-rose-400">*</span></label>
                <input id="phone" oninput="saveInputs()" type="text" placeholder="+1234567890" class="w-full bg-slate-950 border border-slate-800 rounded-xl px-3.5 py-2.5 text-sm focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500 focus:outline-none transition">
            </div>

            <div class="grid grid-cols-1 md:grid-cols-2 gap-3 sm:gap-4">
                <div>
                    <label class="block text-xs font-medium text-slate-300 mb-1.5">Bot Token <span class="text-slate-500">(Optional for logs)</span></label>
                    <input id="bot_token" oninput="saveInputs()" type="text" placeholder="123456:ABC-DEF..." class="w-full bg-slate-950 border border-slate-800 rounded-xl px-3.5 py-2.5 text-sm focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500 focus:outline-none transition">
                </div>
                <div>
                    <label class="block text-xs font-medium text-slate-300 mb-1.5">Log Telegram User ID <span class="text-slate-500">(Optional)</span></label>
                    <input id="log_user_id" oninput="saveInputs()" type="text" placeholder="987654321" class="w-full bg-slate-950 border border-slate-800 rounded-xl px-3.5 py-2.5 text-sm focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500 focus:outline-none transition">
                </div>
            </div>

            <!-- Speed Preset Selector -->
            <div class="bg-slate-950/70 border border-slate-800 p-4 rounded-xl space-y-3">
                <div class="flex items-center justify-between">
                    <label class="text-xs font-semibold text-slate-300 flex items-center">
                        <i class="fa-solid fa-gauge-high text-cyan-400 mr-2"></i> Execution Speed Mode
                    </label>
                    <span id="speed-label" class="text-cyan-400 font-mono text-xs">Safe (3-6s delay)</span>
                </div>
                <div class="grid grid-cols-2 sm:grid-cols-4 gap-2">
                    <button type="button" onclick="setSpeedMode('instant')" id="speed-btn-instant" class="speed-btn py-2.5 px-3 rounded-lg border border-slate-800 bg-slate-900 text-xs font-medium hover:border-amber-500 transition">
                        ⚡ Instant (0-1s)
                    </button>
                    <button type="button" onclick="setSpeedMode('fast')" id="speed-btn-fast" class="speed-btn py-2.5 px-3 rounded-lg border border-slate-800 bg-slate-900 text-xs font-medium hover:border-cyan-500 transition">
                        🚀 Fast (1-3s)
                    </button>
                    <button type="button" onclick="setSpeedMode('safe')" id="speed-btn-safe" class="speed-btn py-2.5 px-3 rounded-lg border border-cyan-500 bg-cyan-950/40 text-cyan-300 text-xs font-medium transition">
                        🛡️ Safe (3-6s)
                    </button>
                    <button type="button" onclick="setSpeedMode('stealth')" id="speed-btn-stealth" class="speed-btn py-2.5 px-3 rounded-lg border border-slate-800 bg-slate-900 text-xs font-medium hover:border-emerald-500 transition">
                        🐢 Stealth (8-15s)
                    </button>
                </div>
                <div class="grid grid-cols-2 gap-3 pt-1">
                    <div>
                        <label class="block text-[11px] text-slate-400 mb-0.5">Min Delay (sec)</label>
                        <input id="min_delay" oninput="saveInputs(); updateSpeedLabel();" type="number" step="0.5" value="3" class="w-full bg-slate-900 border border-slate-800 rounded-lg px-3 py-2 text-xs text-slate-200 focus:outline-none focus:border-cyan-500">
                    </div>
                    <div>
                        <label class="block text-[11px] text-slate-400 mb-0.5">Max Delay (sec)</label>
                        <input id="max_delay" oninput="saveInputs(); updateSpeedLabel();" type="number" step="0.5" value="6" class="w-full bg-slate-900 border border-slate-800 rounded-lg px-3 py-2 text-xs text-slate-200 focus:outline-none focus:border-cyan-500">
                    </div>
                </div>
            </div>

            <button onclick="requestAuth()" id="btn-auth" class="w-full py-3.5 bg-gradient-to-r from-cyan-500 via-sky-500 to-purple-600 hover:from-cyan-400 hover:to-purple-500 text-white font-semibold rounded-xl transition shadow-lg shadow-cyan-500/20 flex items-center justify-center text-sm">
                <i class="fa-solid fa-list-check mr-2"></i> Connect & View My Dialogs
            </button>
        </section>

        <!-- Step 2: OTP / 2FA Verification Modal -->
        <section id="step-verify" class="hidden bg-slate-900/70 border border-slate-800/80 rounded-2xl p-5 sm:p-6 shadow-xl space-y-4 backdrop-blur-sm">
            <h2 class="text-sm sm:text-base font-semibold text-slate-200 border-b border-slate-800 pb-3 flex items-center">
                <i class="fa-solid fa-shield-halved text-cyan-400 mr-2.5 text-sm"></i> 2. Verify Login Code
            </h2>
            <div id="otp-container">
                <label class="block text-xs font-medium text-slate-300 mb-1.5">Enter Verification Code (Sent inside Telegram app)</label>
                <input id="otp_code" type="text" placeholder="12345" class="w-full bg-slate-950 border border-slate-800 rounded-xl px-4 py-3 text-base sm:text-sm focus:border-cyan-500 focus:outline-none font-mono">
            </div>
            <div id="pwd-container" class="hidden">
                <label class="block text-xs font-medium text-slate-300 mb-1.5">Enter 2FA Password</label>
                <input id="password_2fa" type="password" placeholder="••••••••" class="w-full bg-slate-950 border border-slate-800 rounded-xl px-4 py-3 text-base sm:text-sm focus:border-cyan-500 focus:outline-none">
            </div>
            <button onclick="submitVerify()" id="btn-verify" class="w-full py-3.5 bg-emerald-600 hover:bg-emerald-500 text-white font-semibold rounded-xl transition shadow-lg shadow-emerald-500/20 flex items-center justify-center text-sm">
                <i class="fa-solid fa-arrow-right mr-2"></i> Confirm Login & Show My Dialogs
            </button>
        </section>

        <!-- Step 3: Chat Selection Interface -->
        <section id="step-select" class="hidden bg-slate-900/70 border border-slate-800/80 rounded-2xl p-4 sm:p-6 shadow-xl space-y-4 backdrop-blur-sm">
            <div class="flex flex-col md:flex-row md:items-center justify-between gap-3 border-b border-slate-800 pb-3 sm:pb-4">
                <div>
                    <h2 class="text-sm sm:text-base font-semibold text-slate-200 flex items-center">
                        <i class="fa-solid fa-list-check text-cyan-400 mr-2 text-sm"></i> 3. Select Chats & Bots
                    </h2>
                    <p class="text-xs text-slate-400">Choose specific channels, groups, or bots to leave or remove</p>
                </div>
                <!-- Filter buttons -->
                <div class="grid grid-cols-2 sm:flex sm:flex-wrap items-center gap-1.5 sm:gap-2">
                    <button onclick="selectAll(true)" class="px-3 py-2 bg-slate-800 hover:bg-slate-700 text-xs font-medium rounded-lg transition">Select All</button>
                    <button onclick="selectAll(false)" class="px-3 py-2 bg-slate-800 hover:bg-slate-700 text-xs font-medium rounded-lg transition">Deselect All</button>
                    <button onclick="selectByType('Channel')" class="px-3 py-2 bg-cyan-950 border border-cyan-800 text-cyan-300 hover:bg-cyan-900 text-xs font-medium rounded-lg transition">Channels</button>
                    <button onclick="selectByType('Group')" class="px-3 py-2 bg-blue-950 border border-blue-800 text-blue-300 hover:bg-blue-900 text-xs font-medium rounded-lg transition">Groups</button>
                    <button onclick="selectByType('Bot')" class="col-span-2 sm:col-span-1 px-3 py-2 bg-purple-950 border border-purple-800 text-purple-300 hover:bg-purple-900 text-xs font-medium rounded-lg transition">🤖 Bots Only</button>
                </div>
            </div>

            <!-- Inline Speed Adjuster -->
            <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2.5 bg-slate-950/80 border border-slate-800/80 p-3 rounded-xl text-xs">
                <div class="flex items-center justify-between sm:justify-start gap-2">
                    <div class="flex items-center gap-1.5">
                        <i class="fa-solid fa-bolt text-amber-400"></i>
                        <span class="text-slate-300 font-medium">Speed:</span>
                    </div>
                    <select id="select-speed-preset" onchange="applySpeedPreset(this.value)" class="bg-slate-900 border border-slate-700 text-cyan-300 rounded-lg px-2.5 py-1.5 text-xs focus:outline-none">
                        <option value="instant">⚡ Instant (0-1s)</option>
                        <option value="fast">🚀 Fast (1-3s)</option>
                        <option value="safe" selected>🛡️ Safe (3-6s) - Recommended</option>
                        <option value="stealth">🐢 Stealth (8-15s)</option>
                    </select>
                </div>
                <div class="flex items-center justify-end gap-2">
                    <span class="text-slate-400">Delay:</span>
                    <input id="step3-min-delay" oninput="syncDelayInputs(this.value, 'min')" type="number" step="0.5" value="3" class="w-12 sm:w-14 bg-slate-900 border border-slate-800 rounded px-2 py-1 text-center text-xs text-slate-200">
                    <span class="text-slate-500">to</span>
                    <input id="step3-max-delay" oninput="syncDelayInputs(this.value, 'max')" type="number" step="0.5" value="6" class="w-12 sm:w-14 bg-slate-900 border border-slate-800 rounded px-2 py-1 text-center text-xs text-slate-200">
                    <span class="text-slate-500">sec</span>
                </div>
            </div>

            <!-- Search Bar -->
            <div class="relative">
                <i class="fa-solid fa-search absolute left-3.5 top-3.5 text-slate-500 text-sm"></i>
                <input id="search-input" onkeyup="filterChats()" type="text" placeholder="Search by name, username, or bot..." class="w-full bg-slate-950 border border-slate-800 rounded-xl pl-10 pr-4 py-2.5 text-sm focus:border-cyan-500 focus:outline-none">
            </div>

            <!-- Chats Table Container -->
            <div class="bg-slate-950 border border-slate-800 rounded-xl overflow-hidden max-h-80 sm:max-h-96 overflow-y-auto" id="chats-container">
                <div class="p-8 text-center text-slate-500 text-sm">
                    <i class="fa-solid fa-spinner fa-spin mr-2"></i> Loading dialogs, channels and bots...
                </div>
            </div>

            <!-- Bottom Action Bar -->
            <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 pt-2">
                <span class="text-xs sm:text-sm text-slate-300 text-center sm:text-left">Selected to Clean: <b id="selected-counter" class="text-rose-400 text-base">0</b> items</span>
                <button onclick="promptConfirmLeaving()" id="btn-leave-selected" disabled class="w-full sm:w-auto px-6 py-3 bg-rose-600 hover:bg-rose-500 text-white text-sm font-semibold rounded-xl transition shadow-lg shadow-rose-600/20 disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center">
                    <i class="fa-solid fa-trash-can mr-2"></i> Leave & Clean Selected
                </button>
            </div>
        </section>

        <!-- Step 4: Progress & Live Logs -->
        <section id="step-progress" class="hidden bg-slate-900/70 border border-slate-800/80 rounded-2xl p-4 sm:p-6 shadow-xl space-y-4 backdrop-blur-sm">
            <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                <h2 class="text-sm sm:text-base font-semibold text-slate-200 flex items-center">
                    <i class="fa-solid fa-spinner fa-spin text-cyan-400 mr-2 text-sm"></i> Active Cleaning Progress
                </h2>
                <div class="flex flex-wrap items-center gap-2">
                    <div class="flex items-center gap-1.5 bg-slate-950 px-2.5 py-1.5 rounded-lg border border-slate-800 text-xs">
                        <i class="fa-solid fa-gauge text-cyan-400 text-[10px]"></i>
                        <span class="text-slate-400">Speed:</span>
                        <input id="live-speed-slider" oninput="changeLiveSpeed(this.value)" type="range" min="0" max="10" step="0.5" value="3" class="w-14 sm:w-16 accent-cyan-500 cursor-pointer">
                        <span id="live-speed-val" class="font-mono text-cyan-400 w-6 text-right">3s</span>
                    </div>
                    <button onclick="exportLogs()" title="Export logs as TXT" class="px-2.5 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-semibold rounded-lg transition flex items-center gap-1">
                        <i class="fa-solid fa-download text-[10px]"></i> Export
                    </button>
                    <button onclick="returnToSelection()" class="px-2.5 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-semibold rounded-lg transition">
                        <i class="fa-solid fa-arrow-left mr-1"></i> Back
                    </button>
                    <button onclick="stopTask()" class="px-3 py-1.5 bg-rose-600 hover:bg-rose-500 text-white text-xs font-semibold rounded-lg transition">
                        <i class="fa-solid fa-stop mr-1"></i> Stop
                    </button>
                </div>
            </div>

            <div class="grid grid-cols-3 gap-2 sm:gap-3 text-center">
                <div class="bg-slate-950 p-2.5 sm:p-3 rounded-xl border border-slate-800">
                    <p class="text-[10px] sm:text-xs text-slate-400">Selected</p>
                    <p id="stat-total" class="text-lg sm:text-xl font-bold text-slate-100">0</p>
                </div>
                <div class="bg-slate-950 p-2.5 sm:p-3 rounded-xl border border-slate-800">
                    <p class="text-[10px] sm:text-xs text-slate-400">Cleaned</p>
                    <p id="stat-left" class="text-lg sm:text-xl font-bold text-emerald-400">0</p>
                </div>
                <div class="bg-slate-950 p-2.5 sm:p-3 rounded-xl border border-slate-800">
                    <p class="text-[10px] sm:text-xs text-slate-400">Skipped/Failed</p>
                    <p id="stat-failed" class="text-lg sm:text-xl font-bold text-rose-400">0</p>
                </div>
            </div>

            <!-- Progress Bar -->
            <div class="w-full bg-slate-950 rounded-full h-3 border border-slate-800 overflow-hidden">
                <div id="progress-bar" class="bg-gradient-to-r from-cyan-500 to-purple-500 h-full w-0 transition-all duration-300"></div>
            </div>

            <p id="current-action" class="text-[11px] sm:text-xs text-slate-400 truncate font-mono">Preparing task...</p>

            <!-- Console Box -->
            <div class="bg-slate-950 border border-slate-800 rounded-xl p-3 sm:p-4 h-56 sm:h-64 overflow-y-auto font-mono text-[11px] sm:text-xs text-slate-300 space-y-1" id="logs-box">
            </div>
        </section>

    </div>

    <!-- Custom Glassmorphic Modal Component -->
    <div id="custom-modal" class="fixed inset-0 z-50 bg-slate-950/80 backdrop-blur-md flex items-center justify-center p-4 hidden">
        <div class="bg-slate-900 border border-slate-800 rounded-2xl max-w-md w-full p-6 shadow-2xl space-y-4">
            <div class="flex items-center space-x-3">
                <div id="modal-icon-bg" class="w-10 h-10 rounded-xl flex items-center justify-center flex-shrink-0 bg-cyan-950 border border-cyan-800 text-cyan-400">
                    <i id="modal-icon" class="fa-solid fa-info text-base"></i>
                </div>
                <div>
                    <h3 id="modal-title" class="text-base font-bold text-slate-100">Confirmation</h3>
                    <p id="modal-subtitle" class="text-xs text-slate-400">Please review before proceeding</p>
                </div>
            </div>

            <div id="modal-content" class="text-xs sm:text-sm text-slate-300 leading-relaxed bg-slate-950 p-3.5 rounded-xl border border-slate-800/80">
                Are you sure you want to proceed?
            </div>

            <div id="modal-actions" class="flex items-center justify-end gap-2.5 pt-2">
                <button onclick="closeModal()" id="modal-cancel-btn" class="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-semibold rounded-xl transition">
                    Cancel
                </button>
                <button onclick="modalConfirmCallback()" id="modal-confirm-btn" class="px-5 py-2 bg-rose-600 hover:bg-rose-500 text-white text-xs font-semibold rounded-xl transition shadow-lg shadow-rose-600/25">
                    Confirm & Start
                </button>
            </div>
        </div>
    </div>

    <!-- Footer Credit -->
    <footer class="text-center text-xs text-slate-500 pt-6 pb-2 space-y-1">
        <p>Telegram Groups, Channels & Bots Cleaner • Engineered by <a href="https://github.com/Kero309x" target="_blank" class="text-slate-400 font-semibold hover:text-cyan-400 transition">@Kero309x</a></p>
        <p class="text-[10px] sm:text-[11px] text-slate-600">Cross-Platform Clean Architecture • MIT License</p>
    </footer>

    <script>
        let currentSessionId = localStorage.getItem('tg_session_id') || null;
        let currentTaskId = null;
        let pollInterval = null;
        let loadedChats = [];
        let currentLogs = [];
        let modalConfirmCallback = null;

        const FIELD_KEYS = ['api_id', 'api_hash', 'phone', 'bot_token', 'log_user_id', 'min_delay', 'max_delay'];

        const SPEED_PRESETS = {
            'instant': { min: 0, max: 1, label: 'Instant (0-1s)' },
            'fast': { min: 1, max: 3, label: 'Fast (1-3s)' },
            'safe': { min: 3, max: 6, label: 'Safe (3-6s)' },
            'stealth': { min: 8, max: 15, label: 'Stealth (8-15s)' }
        };

        function showCustomModal(title, message, type = 'info', onConfirm = null) {
            document.getElementById('modal-title').innerText = title;
            document.getElementById('modal-content').innerHTML = message;
            const iconBg = document.getElementById('modal-icon-bg');
            const icon = document.getElementById('modal-icon');
            const cancelBtn = document.getElementById('modal-cancel-btn');
            const confirmBtn = document.getElementById('modal-confirm-btn');

            if (type === 'danger') {
                iconBg.className = "w-10 h-10 rounded-xl flex items-center justify-center flex-shrink-0 bg-rose-950 border border-rose-800 text-rose-400";
                icon.className = "fa-solid fa-triangle-exclamation text-base";
                confirmBtn.className = "px-5 py-2 bg-rose-600 hover:bg-rose-500 text-white text-xs font-semibold rounded-xl transition shadow-lg shadow-rose-600/25";
                confirmBtn.innerText = "Confirm Action";
                cancelBtn.classList.remove('hidden');
            } else if (type === 'success') {
                iconBg.className = "w-10 h-10 rounded-xl flex items-center justify-center flex-shrink-0 bg-emerald-950 border border-emerald-800 text-emerald-400";
                icon.className = "fa-solid fa-circle-check text-base";
                confirmBtn.className = "px-5 py-2 bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-semibold rounded-xl transition";
                confirmBtn.innerText = "OK";
                cancelBtn.classList.add('hidden');
            } else {
                iconBg.className = "w-10 h-10 rounded-xl flex items-center justify-center flex-shrink-0 bg-cyan-950 border border-cyan-800 text-cyan-400";
                icon.className = "fa-solid fa-circle-info text-base";
                confirmBtn.className = "px-5 py-2 bg-cyan-600 hover:bg-cyan-500 text-white text-xs font-semibold rounded-xl transition";
                confirmBtn.innerText = "OK";
                cancelBtn.classList.add('hidden');
            }

            modalConfirmCallback = () => {
                closeModal();
                if (onConfirm) onConfirm();
            };

            document.getElementById('custom-modal').classList.remove('hidden');
        }

        function closeModal() {
            document.getElementById('custom-modal').classList.add('hidden');
        }

        function setSpeedMode(mode) {
            const p = SPEED_PRESETS[mode];
            if (!p) return;
            document.getElementById('min_delay').value = p.min;
            document.getElementById('max_delay').value = p.max;
            document.getElementById('step3-min-delay').value = p.min;
            document.getElementById('step3-max-delay').value = p.max;

            document.querySelectorAll('.speed-btn').forEach(b => {
                b.className = "speed-btn py-2.5 px-3 rounded-lg border border-slate-800 bg-slate-900 text-xs font-medium hover:border-slate-700 transition";
            });
            const activeBtn = document.getElementById('speed-btn-' + mode);
            if (activeBtn) {
                activeBtn.className = "speed-btn py-2.5 px-3 rounded-lg border border-cyan-500 bg-cyan-950/40 text-cyan-300 text-xs font-medium transition";
            }
            document.getElementById('speed-label').innerText = p.label;
            saveInputs();
        }

        function applySpeedPreset(mode) {
            setSpeedMode(mode);
        }

        function syncDelayInputs(val, type) {
            if (type === 'min') {
                document.getElementById('min_delay').value = val;
                document.getElementById('step3-min-delay').value = val;
            } else {
                document.getElementById('max_delay').value = val;
                document.getElementById('step3-max-delay').value = val;
            }
            saveInputs();
            updateSpeedLabel();
        }

        function updateSpeedLabel() {
            const min = document.getElementById('min_delay').value;
            const max = document.getElementById('max_delay').value;
            document.getElementById('speed-label').innerText = `Custom (${min}-${max}s)`;
        }

        async function changeLiveSpeed(val) {
            document.getElementById('live-speed-val').innerText = val + 's';
            if (!currentTaskId) return;
            try {
                await fetch(`/api/task/speed/${currentTaskId}`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ min_delay: parseFloat(val), max_delay: parseFloat(val) + 1.5 })
                });
            } catch (e) {}
        }

        function saveInputs() {
            FIELD_KEYS.forEach(k => {
                const el = document.getElementById(k);
                if (el) localStorage.setItem('tg_' + k, el.value.trim());
            });
        }

        function restoreInputs() {
            FIELD_KEYS.forEach(k => {
                const saved = localStorage.getItem('tg_' + k);
                const el = document.getElementById(k);
                if (saved !== null && el) {
                    el.value = saved;
                }
            });
            const minVal = document.getElementById('min_delay').value || 3;
            const maxVal = document.getElementById('max_delay').value || 6;
            document.getElementById('step3-min-delay').value = minVal;
            document.getElementById('step3-max-delay').value = maxVal;
            document.getElementById('live-speed-slider').value = minVal;
            document.getElementById('live-speed-val').innerText = minVal + 's';
            updateSpeedLabel();
        }

        function clearSavedData() {
            showCustomModal("Reset Credentials", "Are you sure you want to reset all stored API credentials?", "danger", () => {
                FIELD_KEYS.forEach(k => localStorage.removeItem('tg_' + k));
                localStorage.removeItem('tg_session_id');
                location.reload();
            });
        }

        async function requestAuth() {
            saveInputs();
            const data = {
                api_id: document.getElementById('api_id').value,
                api_hash: document.getElementById('api_hash').value,
                phone: document.getElementById('phone').value,
                bot_token: document.getElementById('bot_token').value,
                log_user_id: document.getElementById('log_user_id').value,
                min_delay: document.getElementById('min_delay').value,
                max_delay: document.getElementById('max_delay').value,
            };

            if (!data.api_id || !data.api_hash || !data.phone) {
                showCustomModal("Validation Error", "Please fill in API ID, API Hash, and Phone Number before continuing.", "danger");
                return;
            }

            const btn = document.getElementById('btn-auth');
            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin mr-2"></i> Connecting to Telegram API...';

            try {
                const res = await fetch('/api/auth/send_code', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(data)
                });
                const json = await res.json();
                if (json.status === 'code_sent') {
                    currentSessionId = json.session_id;
                    localStorage.setItem('tg_session_id', currentSessionId);
                    document.getElementById('step-auth').classList.add('hidden');
                    document.getElementById('step-verify').classList.remove('hidden');
                } else if (json.status === 'logged_in') {
                    currentSessionId = json.session_id;
                    localStorage.setItem('tg_session_id', currentSessionId);
                    document.getElementById('step-auth').classList.add('hidden');
                    loadChatsList();
                } else {
                    showCustomModal("Connection Failed", json.message || "Failed to connect to Telegram.", "danger");
                }
            } catch (err) {
                showCustomModal("Network Error", String(err), "danger");
            } finally {
                btn.disabled = false;
                btn.innerHTML = '<i class="fa-solid fa-list-check mr-2"></i> Connect & View My Dialogs';
            }
        }

        async function submitVerify() {
            const data = {
                session_id: currentSessionId,
                code: document.getElementById('otp_code').value,
                password: document.getElementById('password_2fa').value
            };

            const btn = document.getElementById('btn-verify');
            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin mr-2"></i> Verifying...';

            try {
                const res = await fetch('/api/auth/verify', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(data)
                });
                const json = await res.json();
                if (json.status === '2fa_required') {
                    document.getElementById('pwd-container').classList.remove('hidden');
                    showCustomModal("2FA Required", "Your account is protected by 2-Step Verification. Please enter your 2FA password.", "info");
                } else if (json.status === 'success') {
                    document.getElementById('step-verify').classList.add('hidden');
                    loadChatsList();
                } else {
                    showCustomModal("Verification Failed", json.message || "Invalid OTP code or password.", "danger");
                }
            } catch (err) {
                showCustomModal("Network Error", String(err), "danger");
            } finally {
                btn.disabled = false;
                btn.innerHTML = '<i class="fa-solid fa-arrow-right mr-2"></i> Confirm Login & Show My Dialogs';
            }
        }

        async function loadChatsList() {
            document.getElementById('step-select').classList.remove('hidden');
            const container = document.getElementById('chats-container');
            container.innerHTML = '<div class="p-8 text-center text-slate-400 text-sm"><i class="fa-solid fa-spinner fa-spin mr-2"></i> Fetching your joined channels, groups and active bots...</div>';

            try {
                const res = await fetch(`/api/dialogs/${currentSessionId}`);
                const json = await res.json();
                if (json.status === 'success') {
                    loadedChats = json.chats;
                    renderChats(loadedChats);
                } else {
                    container.innerHTML = `<div class="p-6 text-center text-rose-400">${json.message}</div>`;
                }
            } catch (e) {
                container.innerHTML = `<div class="p-6 text-center text-rose-400">Failed to load dialogs: ${e}</div>`;
            }
        }

        function renderChats(chats) {
            const container = document.getElementById('chats-container');
            if (!chats || chats.length === 0) {
                container.innerHTML = '<div class="p-8 text-center text-slate-500">No groups, channels or bots found in this account.</div>';
                return;
            }

            container.innerHTML = chats.map(chat => {
                let badgeClass = 'bg-cyan-950 text-cyan-400 border border-cyan-800';
                let icon = 'fa-bullhorn';
                if (chat.type === 'Group' || chat.type === 'Supergroup') {
                    badgeClass = 'bg-blue-950 text-blue-400 border border-blue-800';
                    icon = 'fa-users';
                } else if (chat.type === 'Bot') {
                    badgeClass = 'bg-purple-950 text-purple-300 border border-purple-800';
                    icon = 'fa-robot';
                }

                return `
                <div class="chat-row flex items-center justify-between p-3 sm:p-3.5 border-b border-slate-800/80 hover:bg-slate-900/60 transition cursor-pointer select-none" onclick="toggleRow(event, '${chat.id}')">
                    <div class="flex items-center space-x-3 min-w-0">
                        <input type="checkbox" value="${chat.id}" data-type="${chat.type}" class="chat-checkbox w-5 h-5 sm:w-4 sm:h-4 rounded bg-slate-950 border-slate-700 text-cyan-500 focus:ring-0 focus:ring-offset-0 cursor-pointer" onclick="event.stopPropagation(); updateCount();">
                        <div class="min-w-0">
                            <p class="text-xs sm:text-sm font-medium text-slate-200 truncate flex items-center gap-1.5">
                                <i class="fa-solid ${icon} text-[11px] text-slate-400"></i>
                                <span class="truncate">${chat.name}</span>
                            </p>
                            <p class="text-[11px] sm:text-xs text-slate-500 flex items-center gap-1.5 sm:gap-2 truncate">
                                <span class="text-slate-400 font-mono truncate">${chat.username ? '@' + chat.username : 'ID: ' + chat.id}</span>
                                <span>•</span>
                                <span class="px-1.5 sm:px-2 py-0.5 rounded text-[9px] sm:text-[10px] flex-shrink-0 ${badgeClass}">${chat.type}</span>
                            </p>
                        </div>
                    </div>
                </div>
                `;
            }).join('');

            updateCount();
        }

        function toggleRow(event, id) {
            if (event.target.tagName === 'INPUT') return;
            const cb = document.querySelector(`input[value="${id}"]`);
            if (cb) {
                cb.checked = !cb.checked;
                updateCount();
            }
        }

        function updateCount() {
            const checked = document.querySelectorAll('.chat-checkbox:checked');
            document.getElementById('selected-counter').innerText = checked.length;
            const btn = document.getElementById('btn-leave-selected');
            btn.disabled = checked.length === 0;
        }

        function selectAll(state) {
            document.querySelectorAll('.chat-checkbox').forEach(cb => {
                if (cb.closest('.chat-row').style.display !== 'none') {
                    cb.checked = state;
                }
            });
            updateCount();
        }

        function selectByType(type) {
            document.querySelectorAll('.chat-checkbox').forEach(cb => {
                if (cb.dataset.type && cb.dataset.type.includes(type)) {
                    cb.checked = true;
                } else {
                    cb.checked = false;
                }
            });
            updateCount();
        }

        function filterChats() {
            const q = document.getElementById('search-input').value.toLowerCase();
            document.querySelectorAll('.chat-row').forEach(row => {
                const text = row.innerText.toLowerCase();
                row.style.display = text.includes(q) ? 'flex' : 'none';
            });
        }

        function promptConfirmLeaving() {
            const checked = Array.from(document.querySelectorAll('.chat-checkbox:checked'));
            if (checked.length === 0) return;

            let channelCount = 0, groupCount = 0, botCount = 0;
            checked.forEach(cb => {
                const t = cb.dataset.type;
                if (t === 'Channel') channelCount++;
                else if (t === 'Bot') botCount++;
                else groupCount++;
            });

            const minD = document.getElementById('step3-min-delay').value || 3;
            const maxD = document.getElementById('step3-max-delay').value || 6;

            const breakdown = `
                <div class="space-y-2">
                    <p class="font-semibold text-slate-200">You are about to clean <b>${checked.length}</b> total item(s):</p>
                    <ul class="list-disc list-inside text-xs text-slate-400 space-y-1">
                        ${channelCount > 0 ? `<li><b>${channelCount}</b> Channels (Leave)</li>` : ''}
                        ${groupCount > 0 ? `<li><b>${groupCount}</b> Groups (Leave)</li>` : ''}
                        ${botCount > 0 ? `<li><b>${botCount}</b> Bots (Stop & Complete Wipe)</li>` : ''}
                    </ul>
                    <p class="text-xs text-cyan-400 pt-1">Execution Delay: ${minD} - ${maxD} seconds per item.</p>
                </div>
            `;

            showCustomModal("Confirm Cleaning Process", breakdown, "danger", startLeavingSelected);
        }

        async function startLeavingSelected() {
            const checked = Array.from(document.querySelectorAll('.chat-checkbox:checked')).map(cb => cb.value);
            if (checked.length === 0) return;

            const minD = parseFloat(document.getElementById('step3-min-delay').value || 3);
            const maxD = parseFloat(document.getElementById('step3-max-delay').value || 6);

            const btn = document.getElementById('btn-leave-selected');
            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin mr-2"></i> Initializing...';

            try {
                const res = await fetch('/api/task/start', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        session_id: currentSessionId,
                        target_ids: checked,
                        min_delay: minD,
                        max_delay: maxD
                    })
                });
                const json = await res.json();
                if (json.status === 'success') {
                    currentTaskId = json.task_id;
                    document.getElementById('step-select').classList.add('hidden');
                    document.getElementById('live-speed-slider').value = minD;
                    document.getElementById('live-speed-val').innerText = minD + 's';
                    showProgress();
                } else {
                    showCustomModal("Task Failed", json.message || "Failed to start task.", "danger");
                    btn.disabled = false;
                    btn.innerHTML = '<i class="fa-solid fa-trash-can mr-2"></i> Leave & Clean Selected';
                }
            } catch (err) {
                showCustomModal("Network Error", String(err), "danger");
                btn.disabled = false;
                btn.innerHTML = '<i class="fa-solid fa-trash-can mr-2"></i> Leave & Clean Selected';
            }
        }

        function showProgress() {
            document.getElementById('step-progress').classList.remove('hidden');
            const b = document.getElementById('badge');
            const bm = document.getElementById('badge-mobile');
            if (b) {
                b.className = "hidden sm:inline-block px-3 py-1.5 text-xs font-semibold rounded-full bg-cyan-900/50 text-cyan-400 border border-cyan-700/50";
                b.innerText = "Running";
            }
            if (bm) {
                bm.className = "px-2.5 py-1 text-[11px] font-semibold rounded-full bg-cyan-900/50 text-cyan-400 border border-cyan-700/50";
                bm.innerText = "Running";
            }

            if (pollInterval) clearInterval(pollInterval);
            pollInterval = setInterval(updateTaskStatus, 1500);
        }

        async function updateTaskStatus() {
            if (!currentTaskId) return;
            try {
                const res = await fetch(`/api/task/status/${currentTaskId}`);
                if (!res.ok) return;
                const task = await res.json();

                document.getElementById('stat-total').innerText = task.total || 0;
                document.getElementById('stat-left').innerText = task.left || 0;
                document.getElementById('stat-failed').innerText = task.failed || 0;

                const percent = task.total > 0 ? Math.round(((task.current_index || 0) / task.total) * 100) : 0;
                document.getElementById('progress-bar').style.width = percent + '%';

                if (task.current_name) {
                    const delayInfo = task.current_delay ? ` (Delay: ${task.current_delay}s)` : '';
                    document.getElementById('current-action').innerText = `[${task.current_index}/${task.total}] Processing: ${task.current_name}${delayInfo}`;
                }

                currentLogs = task.logs || [];
                const logsBox = document.getElementById('logs-box');
                logsBox.innerHTML = currentLogs.map(l => `<div>${l}</div>`).join('');
                logsBox.scrollTop = logsBox.scrollHeight;

                if (['completed', 'cancelled', 'error'].includes(task.status)) {
                    clearInterval(pollInterval);
                    const bClass = task.status === 'completed' 
                        ? "px-2.5 py-1 text-[11px] sm:px-3 sm:py-1.5 sm:text-xs font-semibold rounded-full bg-emerald-900/50 text-emerald-400 border border-emerald-700/50"
                        : "px-2.5 py-1 text-[11px] sm:px-3 sm:py-1.5 sm:text-xs font-semibold rounded-full bg-rose-900/50 text-rose-400 border border-rose-700/50";
                    
                    const b = document.getElementById('badge');
                    const bm = document.getElementById('badge-mobile');
                    if (b) { b.className = "hidden sm:inline-block " + bClass; b.innerText = task.status.toUpperCase(); }
                    if (bm) { bm.className = bClass; bm.innerText = task.status.toUpperCase(); }
                }
            } catch (err) {}
        }

        function exportLogs() {
            if (!currentLogs || currentLogs.length === 0) {
                showCustomModal("Export Notice", "No logs available to export yet.", "info");
                return;
            }
            const blob = new Blob([currentLogs.join('\\n')], { type: 'text/plain;charset=utf-8' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `TG_Cleaner_Logs_${new Date().toISOString().slice(0,19).replace(/:/g,'-')}.txt`;
            a.click();
            URL.revokeObjectURL(url);
        }

        async function stopTask() {
            if (!currentTaskId) return;
            await fetch(`/api/task/stop/${currentTaskId}`, { method: 'POST' });
        }

        function returnToSelection() {
            if (pollInterval) clearInterval(pollInterval);
            document.getElementById('step-progress').classList.add('hidden');
            document.getElementById('step-select').classList.remove('hidden');
            const b = document.getElementById('badge');
            const bm = document.getElementById('badge-mobile');
            if (b) { b.innerText = "Selection Mode"; b.className = "hidden sm:inline-block px-3 py-1.5 text-xs font-semibold rounded-full bg-slate-800 text-slate-400"; }
            if (bm) { bm.innerText = "Selection Mode"; bm.className = "px-2.5 py-1 text-[11px] font-semibold rounded-full bg-slate-800 text-slate-400"; }
            loadChatsList();
        }

        document.addEventListener('DOMContentLoaded', restoreInputs);
    </script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE, local_ip=get_local_ip())


@app.route("/api/auth/send_code", methods=["POST"])
def send_code():
    data = request.json or {}
    api_id = data.get("api_id")
    api_hash = data.get("api_hash")
    phone = data.get("phone")
    bot_token = data.get("bot_token", "")
    log_user_id = data.get("log_user_id", "")
    min_delay = float(data.get("min_delay", 3))
    max_delay = float(data.get("max_delay", 6))

    if not api_id or not api_hash or not phone:
        return jsonify({"status": "error", "message": "Missing credentials"}), 400

    clean_phone = phone.replace("+", "").replace(" ", "").strip()
    session_id = str(uuid.uuid4())
    session_file = os.path.join("tg_sessions", f"sess_{clean_phone}")

    async def _async_auth():
        for old_id, old_s in list(sessions_store.items()):
            if old_s.get("phone") == phone and "client" in old_s:
                try:
                    await old_s["client"].disconnect()
                except Exception:
                    pass
                sessions_store.pop(old_id, None)

        client = None
        for attempt in range(3):
            try:
                client = TelegramClient(session_file, int(api_id), api_hash)
                await client.connect()
                break
            except Exception as conn_err:
                if "database is locked" in str(conn_err).lower() and attempt < 2:
                    await asyncio.sleep(1.0)
                    continue
                raise conn_err

        if await client.is_user_authorized():
            sessions_store[session_id] = {
                "client": client, "phone": phone, "bot_token": bot_token,
                "log_user_id": log_user_id, "min_delay": min_delay, "max_delay": max_delay
            }
            return {"status": "logged_in", "session_id": session_id}

        send_code_obj = await client.send_code_request(phone)
        sessions_store[session_id] = {
            "client": client,
            "phone": phone,
            "phone_code_hash": send_code_obj.phone_code_hash,
            "bot_token": bot_token,
            "log_user_id": log_user_id,
            "min_delay": min_delay,
            "max_delay": max_delay
        }
        return {"status": "code_sent", "session_id": session_id}

    future = asyncio.run_coroutine_threadsafe(_async_auth(), loop)
    try:
        res = future.result(timeout=25)
        return jsonify(res)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Connection error: {str(e)}"}), 500


@app.route("/api/auth/verify", methods=["POST"])
def verify_code():
    data = request.json or {}
    session_id = data.get("session_id")
    code = data.get("code")
    password = data.get("password")

    sess = sessions_store.get(session_id)
    if not sess:
        return jsonify({"status": "error", "message": "Invalid or expired session. Please connect again."}), 400

    client = sess["client"]
    phone = sess["phone"]
    phone_code_hash = sess["phone_code_hash"]

    async def _async_sign_in():
        try:
            if password:
                await client.sign_in(password=password)
            else:
                await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except errors.SessionPasswordNeededError:
            return {"status": "2fa_required"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

        return {"status": "success", "session_id": session_id}

    future = asyncio.run_coroutine_threadsafe(_async_sign_in(), loop)
    try:
        res = future.result(timeout=25)
        return jsonify(res)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/dialogs/<session_id>")
def get_dialogs_list(session_id):
    sess = sessions_store.get(session_id)
    if not sess:
        return jsonify({"status": "error", "message": "Session expired or not found. Please connect again."}), 404

    client = sess["client"]

    async def _async_get_dialogs():
        dialogs = await client.get_dialogs()
        chats = []
        cached_entities = {}
        for d in dialogs:
            if is_dialog_target(d.entity):
                eid_str = str(d.entity.id)
                chats.append({
                    "id": eid_str,
                    "name": entity_display_name(d.entity),
                    "username": getattr(d.entity, "username", None),
                    "type": get_entity_type(d.entity)
                })
                cached_entities[eid_str] = d.entity
        sess["cached_entities"] = cached_entities
        return {"status": "success", "chats": chats}

    future = asyncio.run_coroutine_threadsafe(_async_get_dialogs(), loop)
    try:
        res = future.result(timeout=35)
        return jsonify(res)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/task/start", methods=["POST"])
def start_task():
    data = request.json or {}
    session_id = data.get("session_id")
    target_ids = [str(x) for x in data.get("target_ids", [])]
    min_delay = float(data.get("min_delay", 3.0))
    max_delay = float(data.get("max_delay", 6.0))

    sess = sessions_store.get(session_id)
    if not sess:
        return jsonify({"status": "error", "message": "Session expired. Please reconnect."}), 400

    client = sess["client"]
    cached_entities = sess.get("cached_entities", {})
    selected_entities = [cached_entities[cid] for cid in target_ids if cid in cached_entities]

    if not selected_entities:
        return jsonify({"status": "error", "message": "No valid chats or bots selected"}), 400

    task_id = str(uuid.uuid4())
    tasks_store[task_id] = {
        "status": "starting", "total": len(selected_entities), "current_index": 0, "current_name": "",
        "min_delay": min_delay, "max_delay": max_delay, "current_delay": min_delay,
        "left": 0, "failed": 0, "logs": [], "cancelled": False
    }

    asyncio.run_coroutine_threadsafe(
        execute_leaving_process(
            task_id, client, selected_entities,
            sess["bot_token"], sess["log_user_id"]
        ),
        loop
    )

    return jsonify({"status": "success", "task_id": task_id})


@app.route("/api/task/speed/<task_id>", methods=["POST"])
def change_speed(task_id):
    data = request.json or {}
    task = tasks_store.get(task_id)
    if task:
        task["min_delay"] = float(data.get("min_delay", 2.0))
        task["max_delay"] = float(data.get("max_delay", 5.0))
        return jsonify({"status": "updated"})
    return jsonify({"status": "not_found"}), 404


@app.route("/api/task/status/<task_id>")
def get_task_status(task_id):
    task = tasks_store.get(task_id)
    if not task:
        return jsonify({"status": "not_found"}), 404
    return jsonify(task)


@app.route("/api/task/stop/<task_id>", methods=["POST"])
def stop_task(task_id):
    task = tasks_store.get(task_id)
    if task:
        task["cancelled"] = True
        return jsonify({"status": "stopping"})
    return jsonify({"status": "not_found"}), 404


if __name__ == "__main__":
    local_ip = get_local_ip()
    print("=" * 60)
    print("  Telegram Groups, Channels & Bots Cleaner Pro v1.0")
    print("  Engineered by @Kero309x (https://github.com/Kero309x)")
    print("=" * 60)
    print(f"  [+] Desktop / Localhost URL : http://127.0.0.1:5000")
    print(f"  [+] Mobile / Wi-Fi LAN URL  : http://{local_ip}:5000")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=False)
