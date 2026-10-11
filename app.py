import asyncio
import functools
import hmac
import math
import os
import re
import socket
import threading
import time
import uuid
from typing import Dict, Any, List

from flask import Flask, render_template_string, request, jsonify, Response
from werkzeug.exceptions import HTTPException, InternalServerError
import aiohttp
from telethon import TelegramClient, errors, functions, types
from telethon.tl.types import Channel, Chat, User

__author__ = "Kero309x"
__github__ = "https://github.com/Kero309x"
__version__ = "1.0"

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Public URL used for SEO tags (canonical, Open Graph, sitemap).
# Set SITE_URL env var when deploying to a real domain.
SITE_URL = os.environ.get(
    "SITE_URL", "https://github.com/Kero309x/Telegram-Group-Leaver-Pro"
).rstrip("/")
# The GitHub repo has no sitemap.xml, so robots.txt only advertises one on custom domains.
SITE_URL_IS_CUSTOM = "SITE_URL" in os.environ

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


# ---------------------------------------------------------------------------
# Server-side input validation helpers.
# They raise InputValidationError; @json_errors turns that into a 400 JSON
# reply so bad input never produces an HTML 500/400 page the frontend would
# surface as a bogus "Network Error".
# ---------------------------------------------------------------------------

MAX_TASK_IDS = 5000


class InputValidationError(ValueError):
    """Raised by the _get_* helpers; routes convert it to 400 JSON."""


def _get_str(data: Dict[str, Any], key: str, default: str = "", max_len: int = 256,
             required: bool = False, pattern: str = None) -> str:
    """Reads a bounded string from the request payload."""
    raw = data.get(key, default)
    if raw is None:
        raw = default
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        raise InputValidationError(f"{key} must be a string")
    if not isinstance(raw, str):
        raw = str(raw)
    value = raw.strip()
    if required and not value:
        raise InputValidationError(f"{key} is required")
    if len(value) > max_len:
        raise InputValidationError(f"{key} must be at most {max_len} characters")
    if pattern and value and not re.fullmatch(pattern, value):
        raise InputValidationError(f"{key} has an invalid format")
    return value


def _get_int(data: Dict[str, Any], key: str, default, lo: int, hi: int) -> int:
    """Reads an integer from the payload, rejecting anything out of range."""
    raw = data.get(key, default)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raw = default
    if isinstance(raw, bool):
        raise InputValidationError(f"{key} must be an integer")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise InputValidationError(f"{key} must be an integer")
    if value < lo or value > hi:
        raise InputValidationError(f"{key} must be between {lo} and {hi}")
    return value


def _get_float(data: Dict[str, Any], key: str, default, lo: float, hi: float) -> float:
    """Reads a finite float from the payload, rejecting anything out of range."""
    raw = data.get(key, default)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raw = default
    if isinstance(raw, bool):
        raise InputValidationError(f"{key} must be a number")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise InputValidationError(f"{key} must be a number between {lo} and {hi}")
    if not math.isfinite(value):
        raise InputValidationError(f"{key} must be a finite number")
    if value < lo or value > hi:
        raise InputValidationError(f"{key} must be between {lo} and {hi}")
    return value


def _get_delays(data: Dict[str, Any], default_min, default_max):
    """Validates the min/max execution delay pair."""
    min_delay = _get_float(data, "min_delay", default_min, 0, 60)
    max_delay = _get_float(data, "max_delay", default_max, 0, 120)
    if min_delay > max_delay:
        raise InputValidationError("min_delay cannot be greater than max_delay")
    return min_delay, max_delay


def _get_id_list(data: Dict[str, Any], key: str) -> List[str]:
    """Validates a bounded list of dialog ids (strings or ints only)."""
    raw = data.get(key) or []
    if not isinstance(raw, list):
        raise InputValidationError(f"{key} must be a list")
    if len(raw) > MAX_TASK_IDS:
        raise InputValidationError(f"{key} may contain at most {MAX_TASK_IDS} entries")
    ids = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            raise InputValidationError(f"{key} must contain only dialog ids")
        text = str(item).strip()
        if not text or len(text) > 64:
            raise InputValidationError(f"{key} contains an invalid dialog id")
        ids.append(text)
    return ids


def sanitize_phone_digits(phone) -> str:
    """Extracts digits only so a phone number can never escape tg_sessions/.

    Returns None when the result is not a plausible phone length; callers
    must treat that as a validation failure.
    """
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) < 7 or len(digits) > 15:
        return None
    return digits


def json_errors(fn):
    """Converts InputValidationError into a clean 400 JSON response."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except InputValidationError as exc:
            return jsonify({"status": "error", "error": str(exc), "message": str(exc)}), 400
    return wrapper


# ---------------------------------------------------------------------------
# Optional dashboard PIN lock (env DASHBOARD_PIN). Zero behaviour change
# when the variable is unset or empty.
# ---------------------------------------------------------------------------

PIN_EXEMPT_PATHS = ("/", "/robots.txt", "/sitemap.xml")


def _dashboard_pin() -> str:
    return os.environ.get("DASHBOARD_PIN", "").strip()


@app.before_request
def require_dashboard_pin():
    """Gates every /api/* route behind X-Dashboard-Pin when DASHBOARD_PIN is set."""
    pin = _dashboard_pin()
    if not pin:
        return None
    path = request.path
    if not path.startswith("/api/"):
        return None
    if path in PIN_EXEMPT_PATHS or path.startswith("/static/"):
        return None
    supplied = request.headers.get("X-Dashboard-Pin", "")
    if not hmac.compare_digest(supplied.encode("utf-8"), pin.encode("utf-8")):
        return jsonify({"status": "error", "error": "pin_required",
                        "message": "Dashboard PIN required."}), 401
    return None


# ---------------------------------------------------------------------------
# send_code / verify abuse protection: in-memory per-IP cooldowns
# (1 attempt / 60s for send_code, 1 attempt / 30s for verify).
# ---------------------------------------------------------------------------

SEND_CODE_COOLDOWN_SECONDS = 60
VERIFY_CODE_COOLDOWN_SECONDS = 30
_send_code_last_attempt: Dict[str, float] = {}
_verify_last_attempt: Dict[str, float] = {}

_TRUTHY_ENV_VALUES = ("1", "true", "yes", "on")


def _client_ip() -> str:
    """Client IP. X-Forwarded-For is only trusted when TRUST_PROXY is set."""
    forwarded = ""
    trust_proxy = os.environ.get("TRUST_PROXY", "").strip().lower()
    if trust_proxy in _TRUTHY_ENV_VALUES:
        forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.remote_addr or "unknown")[:64]


def _cooldown_remaining(registry: Dict[str, float], window: int) -> int:
    """Seconds this IP must still wait; records the attempt when the window is free.

    Returns 0 (and records the attempt) when the request may proceed, so callers
    must only invoke this after every other validation has passed.
    """
    now = time.time()
    ip = _client_ip()
    for key, ts in list(registry.items()):
        if now - ts >= window:
            registry.pop(key, None)
    last = registry.get(ip)
    if last is not None and (now - last) < window:
        return int(math.ceil(window - (now - last)))
    registry[ip] = now
    return 0


def _too_many_requests(remaining: int, message: str):
    """429 JSON reply carrying a Retry-After header for well-behaved clients."""
    response = jsonify({"status": "error", "error": "too_many_requests",
                        "message": message})
    response.headers["Retry-After"] = str(max(remaining, 1))
    return response, 429



@app.after_request
def set_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response


# ---------------------------------------------------------------------------
# Error handlers: /api/* always answers with JSON (the frontend does
# res.json() unconditionally), everything else keeps Flask's default HTML page.
# ---------------------------------------------------------------------------

_API_ERROR_NAMES = {
    404: ("not_found", "Endpoint not found."),
    405: ("method_not_allowed", "Method not allowed for this endpoint."),
    500: ("internal_error", "Internal server error."),
}


def _wants_json_error() -> bool:
    return request.path.startswith("/api/")


@app.errorhandler(404)
def handle_not_found(err):
    if _wants_json_error():
        name, message = _API_ERROR_NAMES[404]
        return jsonify({"error": name, "message": message}), 404
    return err.get_response()


@app.errorhandler(405)
def handle_method_not_allowed(err):
    if _wants_json_error():
        name, message = _API_ERROR_NAMES[405]
        return jsonify({"error": name, "message": message}), 405
    return err.get_response()


@app.errorhandler(500)
def handle_internal_error(err):
    if _wants_json_error():
        name, message = _API_ERROR_NAMES[500]
        return jsonify({"error": name, "message": message}), 500
    return err.get_response()


@app.errorhandler(Exception)
def handle_unexpected_error(err):
    """Last resort so no /api/* path can ever surface an HTML error page."""
    if isinstance(err, HTTPException):
        if _wants_json_error():
            name, message = _API_ERROR_NAMES.get(
                err.code, (err.name.lower().replace(" ", "_"), err.description or err.name))
            return jsonify({"error": name, "message": message}), err.code
        return err.get_response()
    app.logger.exception("Unhandled exception on %s", request.path)
    if _wants_json_error():
        return jsonify({"error": "internal_error", "message": "Internal server error."}), 500
    return InternalServerError().get_response()


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


# ---------------------------------------------------------------------------
# Session lifecycle: tasks disconnect their client in `finally` while the
# entry stays in sessions_store, so every code path that reuses a stored
# client must go through ensure_connected / run_with_connection_retry first.
# ---------------------------------------------------------------------------

class SessionExpiredError(Exception):
    """A stored Telethon session can no longer be revived (login is gone)."""


async def ensure_connected(session_entry, force_reconnect=False, require_authorized=True):
    """Lazily (re)connects a stored client and verifies it is still usable.

    Connection state is mirrored in ``session_entry["connected"]`` and the
    authorization state in ``session_entry["authorized"]``. Raises
    SessionExpiredError when the login itself is gone (routes turn that into
    a 401 {"error": "session_expired"}) and ConnectionError when the
    transport cannot be re-established.
    """
    client = session_entry.get("client")
    if client is None:
        session_entry["connected"] = False
        raise SessionExpiredError("session has no client attached")
    try:
        if force_reconnect and client.is_connected():
            try:
                await client.disconnect()
            except Exception:
                pass
            session_entry["connected"] = False
        if not client.is_connected():
            await client.connect()
        authorized = await client.is_user_authorized()
    except SessionExpiredError:
        raise
    except (ConnectionError, OSError) as exc:
        session_entry["connected"] = False
        raise ConnectionError(str(exc) or exc.__class__.__name__) from exc
    except Exception as exc:
        # A rejected/revoked auth key surfaces as an RPC error during the
        # authorization probe; treat it as a login problem, not a transport one.
        session_entry["connected"] = bool(client.is_connected())
        session_entry["authorized"] = False
        raise SessionExpiredError(str(exc)) from exc
    session_entry["connected"] = True
    session_entry["authorized"] = authorized
    if require_authorized and not authorized:
        raise SessionExpiredError("telegram login is no longer valid")
    return client


async def _noop_operation(client):
    """Placeholder operation used to validate a session without side effects."""
    return None


async def run_with_connection_retry(session_entry, operation, **ensure_kwargs):
    """Runs ``operation(client)`` with connectivity guaranteed.

    The first use is wrapped in exactly one retry: on ConnectionError the
    client is force-reconnected and the operation retried once.
    """
    try:
        client = await ensure_connected(session_entry, **ensure_kwargs)
        return await operation(client)
    except SessionExpiredError:
        raise
    except (ConnectionError, OSError):
        client = await ensure_connected(session_entry, force_reconnect=True, **ensure_kwargs)
        return await operation(client)


def session_error_response(exc):
    """Maps session/transport failures to clean JSON replies (never HTML)."""
    if isinstance(exc, SessionExpiredError):
        return jsonify({"status": "error", "error": "session_expired",
                        "message": "Session expired. Please log in again."}), 401
    # TimeoutError must be checked before (ConnectionError, OSError): on
    # Python 3.10+ TimeoutError is an OSError subclass, so the order matters.
    if isinstance(exc, TimeoutError):
        return jsonify({"status": "error", "error": "timeout",
                        "message": "The operation timed out. Please try again."}), 504
    if isinstance(exc, (ConnectionError, OSError)):
        return jsonify({"status": "error", "error": "connection_failed",
                        "message": "Could not reach Telegram just now. Please try again."}), 503
    return jsonify({"status": "error", "error": "internal_error",
                    "message": str(exc)}), 500



# A single item never sleeps longer than this in FloodWait backoffs in total;
# beyond it the task is aborted so a repeated-flood loop cannot hang forever.
FLOOD_WAIT_BUDGET_SECONDS = 600


def _abort_task_for_flood(task, message: str) -> None:
    """Marks the task as errored and records the reason in its log."""
    if task is None:
        return
    task["status"] = "error"
    task["error_message"] = message
    task["logs"].append(f"[{time.strftime('%H:%M:%S')}] {message}")


async def execute_leave_or_delete(client: TelegramClient, entity, max_retries=3, task=None) -> tuple:
    """
    Completely and permanently leaves channels/groups or stops & wipes bots from the chat list.

    Returns (success: bool, status_msg: str, warnings: list[str]).
    Success reflects the actual removal step: best-effort cleanup steps that fail
    only append "[!] <step> failed: <err>" warnings, while a failed removal step
    surfaces as success=False through the retry/classification handlers below.
    """
    is_bot = isinstance(entity, User) and getattr(entity, "bot", False)
    warnings = []
    step = "resolve entity"
    attempt = 0
    flood_slept = 0

    while attempt < max_retries:
        attempt += 1
        step = "resolve entity"
        try:
            # 1. Resolve InputPeer / InputEntity properly
            input_entity = await client.get_input_entity(entity)

            if is_bot:
                # A. Stop / Block the Bot (Official Stop Bot) - best effort
                step = "block bot"
                try:
                    await client(functions.contacts.BlockRequest(id=input_entity))
                except errors.FloodWaitError:
                    raise
                except Exception as exc:
                    warnings.append(f"[!] Block failed: {exc}")

                # B. Permanently wipe entire chat history in both directions (Revoke + Clear)
                step = "wipe history"
                try:
                    await client(functions.messages.DeleteHistoryRequest(
                        peer=input_entity,
                        max_id=0,
                        just_clear=False,
                        revoke=True
                    ))
                except errors.FloodWaitError:
                    raise
                except Exception as exc:
                    warnings.append(f"[!] History wipe failed: {exc}")

                # C. Removal step: the dialog must actually disappear from the
                # chat list, otherwise the item counts as failed.
                step = "remove dialog"
                await client.delete_dialog(input_entity, revoke=True)
                return True, "Bot Stopped & Completely Wiped (100% Cleared)", warnings

            elif isinstance(entity, Channel):
                step = "leave channel"
                await client(functions.channels.LeaveChannelRequest(input_entity))
                step = "remove dialog"
                try:
                    await client.delete_dialog(input_entity, revoke=True)
                except errors.FloodWaitError:
                    raise
                except Exception as exc:
                    warnings.append(f"[!] Dialog removal failed: {exc}")
                return True, "Left & Removed Channel", warnings

            elif isinstance(entity, Chat):
                # Removal step for basic groups: leaving must succeed.
                step = "leave group"
                await client(functions.messages.DeleteChatUserRequest(
                    chat_id=entity.id,
                    user_id=types.InputUserSelf(),
                    revoke_history=True
                ))
                step = "remove dialog"
                try:
                    await client.delete_dialog(input_entity, revoke=True)
                except errors.FloodWaitError:
                    raise
                except Exception as exc:
                    warnings.append(f"[!] Dialog removal failed: {exc}")
                return True, "Left & Removed Group", warnings

            else:
                step = "remove dialog"
                await client.delete_dialog(input_entity, revoke=True)
                return True, "Cleaned Dialog", warnings

        except errors.FloodWaitError as e:
            wait = int(getattr(e, "seconds", 0) or 0)
            stamp = time.strftime("%H:%M:%S")
            if wait > 300:
                _abort_task_for_flood(task, "FloodWait too long (>300s) — aborted for safety")
                return False, "FloodWait too long (>300s) — aborted for safety", warnings
            if task is not None:
                task["current_delay"] = wait
                task["logs"].append(f"[{stamp}] FloodWait: sleeping {wait}s (rate limit)")
            flood_slept += wait
            if flood_slept > FLOOD_WAIT_BUDGET_SECONDS:
                message = f"FloodWait budget exhausted (>{FLOOD_WAIT_BUDGET_SECONDS}s) — aborted for safety"
                _abort_task_for_flood(task, message)
                return False, message, warnings
            await asyncio.sleep(wait)
            # A rate-limit wait must not consume a retry attempt.
            attempt -= 1
            continue

        except (errors.ChatAdminRequiredError, errors.rpcerrorlist.ChatAdminRequiredError):
            return False, "Skipped (Admin/Owner Rights Required)", warnings

        except (errors.UserNotParticipantError, errors.rpcerrorlist.UserNotParticipantError):
            return True, "Already Left/Cleaned", warnings

        except errors.rpcerrorlist.ChatForbiddenError:
            return True, "Forbidden/Deleted", warnings

        except Exception as e:
            err_str = str(e)
            warnings.append(f"[!] {step} failed: {err_str[:120]}")
            if attempt < max_retries:
                await asyncio.sleep(1.5 ** attempt)
            else:
                return False, f"Error: {err_str[:45]}", warnings

    return False, "Max retries reached", warnings


async def execute_leaving_process(task_id: str, client: TelegramClient, targets: List[Any], bot_token: str, log_user_id: str, session_entry=None):
    """Executes the batch removal process with real-time feedback."""
    task = tasks_store[task_id]
    total = len(targets)
    exclude_ids = {str(x) for x in task.get("exclude_ids", [])}
    protected_count = 0
    task["total"] = total
    task["status"] = "running"
    task["logs"].append(f"[{time.strftime('%H:%M:%S')}] Started task for {total} selected item(s)...")
    if exclude_ids:
        task["logs"].append(f"[{time.strftime('%H:%M:%S')}] {len(exclude_ids)} protected favorite(s) excluded from cleanup.")

    left_count = 0
    failed_count = 0

    try:
        if session_entry is not None:
            # Safety net: the client may have been disconnected between the
            # route check and this coroutine actually starting.
            await ensure_connected(session_entry)

        for idx, entity in enumerate(targets, start=1):
            if task.get("cancelled", False):
                task["status"] = "cancelled"
                task["logs"].append(f"[{time.strftime('%H:%M:%S')}] Task stopped by user.")
                break

            name = entity_display_name(entity)

            if str(entity.id) in exclude_ids:
                protected_count += 1
                task["protected"] = protected_count
                task["current_index"] = idx
                task["current_name"] = name
                task["logs"].append(f"[{time.strftime('%H:%M:%S')}] [{idx}/{total}] [Protected] Favorite skipped: {name}")
                continue

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

            success, detail, item_warnings = await execute_leave_or_delete(client, entity, task=task)

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

            for warning in item_warnings:
                task["logs"].append(f"[{time_short}] [{idx}/{total}] {warning}")

            log_msg = (
                f"<b>TG Cleaner Log</b>\n"
                f"<b>Target:</b> {name} (<i>{etype}</i>)\n"
                f"<b>Status:</b> {'Success' if success else 'Failed'} - {detail}\n"
                f"<b>Time:</b> {timestamp}\n"
                f"<i>Powered by @Kero309x</i>"
            )
            asyncio.create_task(send_log_to_bot(bot_token, log_user_id, log_msg))

            if task.get("status") == "error":
                # The item aborted the whole task (e.g. oversized FloodWait).
                task["logs"].append(f"[{time.strftime('%H:%M:%S')}] Task aborted.")
                break

        if task["status"] not in ("cancelled", "error"):
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

    except SessionExpiredError:
        task["status"] = "error"
        task["logs"].append(f"[{time.strftime('%H:%M:%S')}] Session expired - please log in again.")
    except Exception as e:
        task["status"] = "error"
        task["logs"].append(f"[{time.strftime('%H:%M:%S')}] Critical error: {str(e)}")
    finally:
        if session_entry is not None:
            session_entry["connected"] = False
        try:
            await client.disconnect()
        except Exception:
            pass


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="theme-color" content="#020617">
    <title>TG Cleaner Pro - Bulk Leave Telegram Groups, Channels &amp; Bots</title>
    <meta name="description" content="Clean your Telegram account in one click: batch-leave unwanted channels and supergroups, block and wipe bots, with real MTProto API, speed control and live logs.">
    <meta name="keywords" content="telegram cleaner, leave telegram groups, bulk leave telegram channels, delete telegram bots, telegram cleanup tool, MTProto, TG Cleaner Pro">
    <meta name="author" content="Kero309x">
    <meta name="robots" content="index, follow, max-image-preview:large">
    <meta name="application-name" content="TG Cleaner Pro">
    <link rel="canonical" href="{{ site_url }}/">

    <meta property="og:type" content="website">
    <meta property="og:site_name" content="TG Cleaner Pro">
    <meta property="og:title" content="TG Cleaner Pro - Bulk Leave Telegram Groups, Channels &amp; Bots">
    <meta property="og:description" content="Batch-leave Telegram channels and supergroups, block and wipe bots - real MTProto execution with speed control and live logs.">
    <meta property="og:url" content="{{ site_url }}/">
    <meta property="og:image" content="https://opengraph.githubassets.com/1/Kero309x/Telegram-Group-Leaver-Pro">
    <meta property="og:locale" content="en_US">

    <meta name="twitter:card" content="summary_large_image">
    <meta name="twitter:title" content="TG Cleaner Pro - Bulk Leave Telegram Groups, Channels &amp; Bots">
    <meta name="twitter:description" content="Batch-leave Telegram channels and supergroups, block and wipe bots - real MTProto execution with speed control and live logs.">
    <meta name="twitter:image" content="https://opengraph.githubassets.com/1/Kero309x/Telegram-Group-Leaver-Pro">

    <link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
    <link rel="manifest" href="/static/site.webmanifest">
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <link rel="preconnect" href="https://cdnjs.cloudflare.com">
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

        button:focus-visible, a:focus-visible, input:focus-visible, select:focus-visible, [role="tab"]:focus-visible {
            outline: none;
            box-shadow: 0 0 0 2px #020617, 0 0 0 4px rgba(34, 211, 238, 0.75);
        }

        @keyframes toast-in { from { opacity: 0; transform: translateX(18px) scale(0.97); } to { opacity: 1; transform: translateX(0) scale(1); } }
        @keyframes toast-out { from { opacity: 1; } to { opacity: 0; transform: translateX(18px) scale(0.97); } }
        .toast-in { animation: toast-in 0.28s cubic-bezier(0.16, 1, 0.3, 1); }
        .toast-out { animation: toast-out 0.22s ease forwards; }

        @keyframes star-pop { 0% { transform: scale(1); } 45% { transform: scale(1.35) rotate(12deg); } 100% { transform: scale(1); } }
        .fav-btn.pop i { animation: star-pop 0.32s ease; }

        @keyframes progress-shimmer { to { transform: translateX(100%); } }
        .progress-fill { box-shadow: 0 0 12px rgba(34, 211, 238, 0.55), 0 0 24px rgba(168, 85, 247, 0.3); }
        .progress-fill::after { content: ""; position: absolute; inset: 0; background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.3), transparent); transform: translateX(-100%); animation: progress-shimmer 1.8s linear infinite; }

        .chat-row:active { transform: scale(0.997); }
    </style>
    <script type="application/ld+json">
    {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "name": "TG Cleaner Pro",
        "description": "Batch-leave Telegram channels and supergroups, block and wipe bots - real MTProto execution with speed control and live logs.",
        "url": "{{ site_url }}/",
        "image": "https://opengraph.githubassets.com/1/Kero309x/Telegram-Group-Leaver-Pro",
        "author": { "@type": "Person", "name": "Kero309x", "url": "https://github.com/Kero309x" },
        "license": "https://opensource.org/licenses/MIT",
        "applicationCategory": "UtilitiesApplication",
        "operatingSystem": "Windows, Linux, macOS, Android (Termux)",
        "softwareVersion": "1.0",
        "offers": { "@type": "Offer", "price": "0", "priceCurrency": "USD" }
    }
    </script>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen py-4 sm:py-8 px-3 sm:px-6 flex flex-col justify-between selection:bg-cyan-500 selection:text-slate-950">
    <div class="max-w-4xl w-full mx-auto space-y-4 sm:space-y-6">
        
        <!-- Header -->
        <header class="sticky top-0 sm:top-3 z-40 bg-slate-900/95 backdrop-blur-md border border-slate-800/80 rounded-2xl p-4 sm:p-5 shadow-2xl flex flex-col sm:flex-row items-center justify-between gap-3 sm:gap-4">
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
                    <label for="api_id" class="block text-xs font-medium text-slate-300 mb-1.5">API ID <span class="text-rose-400">*</span></label>
                    <input id="api_id" oninput="saveInputs()" type="number" placeholder="e.g. 12345678" class="w-full bg-slate-950 border border-slate-800 rounded-xl px-3.5 py-2.5 text-sm focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500 focus:outline-none transition">
                </div>
                <div>
                    <label for="api_hash" class="block text-xs font-medium text-slate-300 mb-1.5">API Hash <span class="text-rose-400">*</span></label>
                    <input id="api_hash" oninput="saveInputs()" type="text" placeholder="e.g. 0123456789abcdef..." class="w-full bg-slate-950 border border-slate-800 rounded-xl px-3.5 py-2.5 text-sm focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500 focus:outline-none transition">
                </div>
            </div>

            <div>
                <label for="phone" class="block text-xs font-medium text-slate-300 mb-1.5">Phone Number (With country code +) <span class="text-rose-400">*</span></label>
                <input id="phone" oninput="saveInputs()" type="text" placeholder="+1234567890" class="w-full bg-slate-950 border border-slate-800 rounded-xl px-3.5 py-2.5 text-sm focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500 focus:outline-none transition">
            </div>

            <div class="grid grid-cols-1 md:grid-cols-2 gap-3 sm:gap-4">
                <div>
                    <label for="bot_token" class="block text-xs font-medium text-slate-300 mb-1.5">Bot Token <span class="text-slate-500">(Optional for logs)</span></label>
                    <input id="bot_token" oninput="saveInputs()" type="text" placeholder="123456:ABC-DEF..." class="w-full bg-slate-950 border border-slate-800 rounded-xl px-3.5 py-2.5 text-sm focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500 focus:outline-none transition">
                </div>
                <div>
                    <label for="log_user_id" class="block text-xs font-medium text-slate-300 mb-1.5">Log Telegram User ID <span class="text-slate-500">(Optional)</span></label>
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
                    <label for="min_delay" class="block text-[11px] text-slate-400 mb-0.5">Min Delay (sec)</label>
                    <input id="min_delay" oninput="syncDelayInputs(this.value, 'min');" type="number" step="0.5" value="3" class="w-full bg-slate-900 border border-slate-800 rounded-lg px-3 py-2 text-xs text-slate-200 focus:outline-none focus:border-cyan-500">
                </div>
                <div>
                    <label for="max_delay" class="block text-[11px] text-slate-400 mb-0.5">Max Delay (sec)</label>
                    <input id="max_delay" oninput="syncDelayInputs(this.value, 'max');" type="number" step="0.5" value="6" class="w-full bg-slate-900 border border-slate-800 rounded-lg px-3 py-2 text-xs text-slate-200 focus:outline-none focus:border-cyan-500">
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
                <label for="otp_code" class="block text-xs font-medium text-slate-300 mb-1.5">Enter Verification Code (Sent inside Telegram app)</label>
                <input id="otp_code" type="text" placeholder="12345" class="w-full bg-slate-950 border border-slate-800 rounded-xl px-4 py-3 text-base sm:text-sm focus:border-cyan-500 focus:outline-none font-mono">
            </div>
            <div id="pwd-container" class="hidden">
                <label for="password_2fa" class="block text-xs font-medium text-slate-300 mb-1.5">Enter 2FA Password</label>
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
                        <option value="custom">Custom</option>
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
                <input id="search-input" onkeyup="filterChats()" type="text" aria-label="Search dialogs by name or username" placeholder="Search by name, username, or bot..." class="w-full bg-slate-950 border border-slate-800 rounded-xl pl-10 pr-4 py-2.5 text-sm focus:border-cyan-500 focus:outline-none">
            </div>

            <!-- Dialog Filter & Favorites Bar -->
            <div class="bg-slate-950/70 border border-slate-800 rounded-xl p-2.5 space-y-2">
                <div class="flex flex-wrap items-center gap-1.5">
                    <div class="flex flex-wrap items-center gap-1.5" role="tablist" aria-label="Filter dialogs">
                        <button type="button" role="tab" data-filter="all" aria-selected="true" onclick="setChatFilter('all')" class="chat-filter-btn px-3 py-2 min-h-[36px] rounded-lg border text-xs font-medium transition flex items-center gap-1.5 bg-cyan-950/60 border-cyan-500/70 text-cyan-300">
                            All <span id="cnt-all" class="filter-count text-[10px] font-bold bg-slate-950/70 border border-slate-800 px-1.5 py-0.5 rounded-full min-w-[1.4rem] text-center">0</span>
                        </button>
                        <button type="button" role="tab" data-filter="favorites" aria-selected="false" onclick="setChatFilter('favorites')" class="chat-filter-btn px-3 py-2 min-h-[36px] rounded-lg border text-xs font-medium transition flex items-center gap-1.5 bg-slate-900 border-slate-800 text-slate-400 hover:text-slate-200 hover:border-slate-700">
                            <i class="fa-solid fa-star text-[10px] text-amber-400"></i> Favorites <span id="cnt-favorites" class="filter-count text-[10px] font-bold bg-slate-950/70 border border-slate-800 px-1.5 py-0.5 rounded-full min-w-[1.4rem] text-center">0</span>
                        </button>
                        <button type="button" role="tab" data-filter="Channel" aria-selected="false" onclick="setChatFilter('Channel')" class="chat-filter-btn px-3 py-2 min-h-[36px] rounded-lg border text-xs font-medium transition flex items-center gap-1.5 bg-slate-900 border-slate-800 text-slate-400 hover:text-slate-200 hover:border-slate-700">
                            Channels <span id="cnt-Channel" class="filter-count text-[10px] font-bold bg-slate-950/70 border border-slate-800 px-1.5 py-0.5 rounded-full min-w-[1.4rem] text-center">0</span>
                        </button>
                        <button type="button" role="tab" data-filter="Group" aria-selected="false" onclick="setChatFilter('Group')" class="chat-filter-btn px-3 py-2 min-h-[36px] rounded-lg border text-xs font-medium transition flex items-center gap-1.5 bg-slate-900 border-slate-800 text-slate-400 hover:text-slate-200 hover:border-slate-700">
                            Groups <span id="cnt-Group" class="filter-count text-[10px] font-bold bg-slate-950/70 border border-slate-800 px-1.5 py-0.5 rounded-full min-w-[1.4rem] text-center">0</span>
                        </button>
                        <button type="button" role="tab" data-filter="Bot" aria-selected="false" onclick="setChatFilter('Bot')" class="chat-filter-btn px-3 py-2 min-h-[36px] rounded-lg border text-xs font-medium transition flex items-center gap-1.5 bg-slate-900 border-slate-800 text-slate-400 hover:text-slate-200 hover:border-slate-700">
                            Bots <span id="cnt-Bot" class="filter-count text-[10px] font-bold bg-slate-950/70 border border-slate-800 px-1.5 py-0.5 rounded-full min-w-[1.4rem] text-center">0</span>
                        </button>
                    </div>
                    <button type="button" onclick="invertSelection()" title="Invert selection of visible items" class="px-3 py-2 min-h-[36px] rounded-lg border border-dashed border-slate-700 bg-slate-900 text-slate-400 hover:text-slate-200 hover:border-slate-500 text-xs font-medium transition flex items-center gap-1.5">
                        <i class="fa-solid fa-arrow-right-arrow-left text-[10px]"></i> Invert selection
                    </button>
                </div>
                <div class="flex flex-wrap items-center gap-1.5 pt-2 border-t border-slate-800/70">
                    <button type="button" onclick="favoriteAllSelected()" class="px-3 py-2 min-h-[36px] rounded-lg bg-amber-950/40 border border-amber-800/50 text-amber-300 hover:bg-amber-900/50 text-xs font-medium transition flex items-center gap-1.5">
                        <i class="fa-solid fa-star text-[10px]"></i> Favorite all selected
                    </button>
                    <button type="button" onclick="unfavoriteAll()" class="px-3 py-2 min-h-[36px] rounded-lg bg-slate-900 border border-slate-800 text-slate-400 hover:text-slate-200 hover:border-slate-700 text-xs font-medium transition flex items-center gap-1.5">
                        <i class="fa-regular fa-star text-[10px]"></i> Unfavorite all
                    </button>
                    <span class="text-[11px] text-slate-500 ml-auto hidden sm:flex items-center gap-1"><i class="fa-solid fa-shield-halved"></i> Favorites auto-save in this browser</span>
                </div>
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
                        <input id="live-speed-slider" oninput="changeLiveSpeed(this.value)" type="range" min="0" max="15" step="0.5" value="3" class="w-14 sm:w-16 accent-cyan-500 cursor-pointer">
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
                <div id="progress-bar" class="progress-fill relative overflow-hidden bg-gradient-to-r from-cyan-500 to-purple-500 h-full w-0 transition-all duration-300"></div>
            </div>

            <p id="current-action" class="text-[11px] sm:text-xs text-slate-400 truncate font-mono">Preparing task...</p>

            <!-- Console Box -->
            <div class="bg-slate-950 border border-slate-800 rounded-xl p-3 sm:p-4 h-56 sm:h-64 overflow-y-auto font-mono text-[11px] sm:text-xs text-slate-300 space-y-1" id="logs-box">
            </div>
        </section>

    </div>

    <!-- Toast Notification Stack -->
    <div id="toast-container" class="fixed top-3 right-3 sm:top-4 sm:right-4 z-[60] flex flex-col gap-2 w-[calc(100vw-1.5rem)] max-w-xs sm:max-w-sm pointer-events-none" role="status" aria-live="polite"></div>

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

    <!-- Dashboard PIN Modal (only shown when the server requires X-Dashboard-Pin) -->
    <div id="pin-modal" class="fixed inset-0 z-[70] bg-slate-950/80 backdrop-blur-md flex items-center justify-center p-4 hidden">
        <div class="bg-slate-900 border border-slate-800 rounded-2xl max-w-sm w-full p-6 shadow-2xl space-y-4">
            <div class="flex items-center space-x-3">
                <div class="w-10 h-10 rounded-xl flex items-center justify-center flex-shrink-0 bg-amber-950 border border-amber-800 text-amber-400">
                    <i class="fa-solid fa-lock text-base"></i>
                </div>
                <div>
                    <h3 class="text-base font-bold text-slate-100">Dashboard PIN Required</h3>
                    <p class="text-xs text-slate-400">Enter the dashboard PIN to unlock the API</p>
                </div>
            </div>
            <input id="pin-input" type="password" inputmode="numeric" autocomplete="off" maxlength="64" placeholder="PIN" class="w-full bg-slate-950 border border-slate-800 rounded-xl px-4 py-3 text-sm font-mono focus:border-cyan-500 focus:outline-none">
            <div class="flex items-center justify-end gap-2.5 pt-1">
                <button type="button" id="pin-cancel-btn" class="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-semibold rounded-xl transition">
                    Cancel
                </button>
                <button type="button" id="pin-submit-btn" class="px-5 py-2 bg-cyan-600 hover:bg-cyan-500 text-white text-xs font-semibold rounded-xl transition shadow-lg shadow-cyan-500/25">
                    Unlock
                </button>
            </div>
        </div>
    </div>

    <!-- Dialogs load-failure template (cloned by renderDialogsError, never interpolated HTML) -->
    <template id="dialogs-error-template">
        <div class="p-6 text-center space-y-3">
            <p class="text-rose-400 text-sm" data-dialogs-error-msg></p>
            <button type="button" id="retry-dialogs-btn" class="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-semibold rounded-xl transition border border-slate-700/60">
                <i class="fa-solid fa-rotate-right mr-1.5"></i> Retry
            </button>
        </div>
    </template>

    <!-- Footer Credit -->
    <footer class="text-center text-xs text-slate-500 pt-6 pb-2 space-y-1">
        <p>Telegram Groups, Channels & Bots Cleaner • Engineered by <a href="https://github.com/Kero309x" target="_blank" class="text-slate-400 font-semibold hover:text-cyan-400 transition">@Kero309x</a></p>
        <p class="text-[10px] sm:text-[11px] text-slate-600">Cross-Platform Clean Architecture • MIT License</p>
    </footer>

    <script>
        let currentSessionId = localStorage.getItem('tg_session_id') || null;
        let currentTaskId = null;
        let currentTaskStatus = null;
        let pollInterval = null;
        let pollFailures = 0;
        let loadedChats = [];
        let currentLogs = [];
        let modalConfirmCallback = null;
        let favoriteIds = new Set();
        let activeChatFilter = 'all';

        const FIELD_KEYS = ['api_id', 'api_hash', 'phone', 'bot_token', 'log_user_id', 'min_delay', 'max_delay'];

        const SPEED_PRESETS = {
            'instant': { min: 0, max: 1, label: 'Instant (0-1s)' },
            'fast': { min: 1, max: 3, label: 'Fast (1-3s)' },
            'safe': { min: 3, max: 6, label: 'Safe (3-6s)' },
            'stealth': { min: 8, max: 15, label: 'Stealth (8-15s)' }
        };

        const FAV_STORAGE_KEY = 'tg_favs';

        const FILTER_BTN_BASE = 'chat-filter-btn px-3 py-2 min-h-[36px] rounded-lg border text-xs font-medium transition flex items-center gap-1.5';
        const FILTER_BTN_ACTIVE = FILTER_BTN_BASE + ' bg-cyan-950/60 border-cyan-500/70 text-cyan-300';
        const FILTER_BTN_IDLE = FILTER_BTN_BASE + ' bg-slate-900 border-slate-800 text-slate-400 hover:text-slate-200 hover:border-slate-700';

        const FAV_BTN_BASE = 'fav-btn flex-shrink-0 w-9 h-9 sm:w-8 sm:h-8 rounded-lg flex items-center justify-center text-sm transition active:scale-90';

        const TOAST_STYLES = {
            success: { bar: 'border-l-emerald-500', icon: 'fa-circle-check', iconClass: 'text-emerald-400' },
            error: { bar: 'border-l-rose-500', icon: 'fa-circle-exclamation', iconClass: 'text-rose-400' },
            info: { bar: 'border-l-cyan-500', icon: 'fa-circle-info', iconClass: 'text-cyan-400' }
        };

        const BADGE_STYLES = {
            idle: ["hidden sm:inline-block px-3 py-1.5 text-xs font-semibold rounded-full bg-slate-800 text-slate-400 border border-slate-700/50", "px-2.5 py-1 text-[11px] font-semibold rounded-full bg-slate-800 text-slate-400 border border-slate-700/50"],
            running: ["hidden sm:inline-block px-3 py-1.5 text-xs font-semibold rounded-full bg-cyan-900/50 text-cyan-400 border border-cyan-700/50", "px-2.5 py-1 text-[11px] font-semibold rounded-full bg-cyan-900/50 text-cyan-400 border border-cyan-700/50"],
            error: ["hidden sm:inline-block px-3 py-1.5 text-xs font-semibold rounded-full bg-rose-900/50 text-rose-400 border border-rose-700/50", "px-2.5 py-1 text-[11px] sm:px-3 sm:py-1.5 sm:text-xs font-semibold rounded-full bg-rose-900/50 text-rose-400 border border-rose-700/50"],
            success: ["hidden sm:inline-block px-3 py-1.5 text-xs font-semibold rounded-full bg-emerald-900/50 text-emerald-400 border border-emerald-700/50", "px-2.5 py-1 text-[11px] sm:px-3 sm:py-1.5 sm:text-xs font-semibold rounded-full bg-emerald-900/50 text-emerald-400 border border-emerald-700/50"]
        };

        function setBadge(text, kind) {
            const style = BADGE_STYLES[kind] || BADGE_STYLES.idle;
            const b = document.getElementById('badge');
            const bm = document.getElementById('badge-mobile');
            if (b) { b.className = style[0]; b.innerText = text; }
            if (bm) { bm.className = style[1]; bm.innerText = text; }
        }

        function stopPolling() {
            if (pollInterval) {
                clearInterval(pollInterval);
                pollInterval = null;
            }
        }

        function escapeHtml(value) {
            const entities = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
            return String(value).replace(/[&<>"']/g, ch => entities[ch]);
        }

        // ---- Optional dashboard PIN lock (active only when the server demands it) ----
        const PIN_STORAGE_KEY = 'tg_dashboard_pin';
        let pinPromptPromise = null;

        function promptForDashboardPin() {
            if (pinPromptPromise) return pinPromptPromise;
            pinPromptPromise = new Promise(resolve => {
                const modal = document.getElementById('pin-modal');
                const input = document.getElementById('pin-input');
                const finish = value => {
                    if (modal) modal.classList.add('hidden');
                    if (input) input.value = '';
                    document.removeEventListener('keydown', onEscape);
                    pinPromptPromise = null;
                    resolve(value);
                };
                const onEscape = e => { if (e.key === 'Escape') finish(null); };
                if (!modal || !input) { finish(null); return; }
                document.getElementById('pin-submit-btn').onclick = () => {
                    const entered = input.value.trim();
                    if (!entered) { input.focus(); return; }
                    finish(entered);
                };
                document.getElementById('pin-cancel-btn').onclick = () => finish(null);
                input.onkeydown = e => {
                    if (e.key === 'Enter') {
                        e.preventDefault();
                        document.getElementById('pin-submit-btn').click();
                    }
                };
                document.addEventListener('keydown', onEscape);
                modal.classList.remove('hidden');
                input.focus();
            });
            return pinPromptPromise;
        }

        // Wrap the native fetch exactly once: attach the stored PIN header to
        // every request and, on a pin_required 401, ask for the PIN and replay
        // the failed request a single time (no retry loop).
        const nativeFetch = window.fetch.bind(window);
        window.fetch = async function (input, init) {
            const opts = init ? Object.assign({}, init) : {};
            const fallbackHeaders = (input && typeof input === 'object' && !(input instanceof Request)) ? input.headers : undefined;
            const headers = new Headers(opts.headers || fallbackHeaders);
            const storedPin = sessionStorage.getItem(PIN_STORAGE_KEY);
            if (storedPin) headers.set('X-Dashboard-Pin', storedPin);
            opts.headers = headers;

            let res = await nativeFetch(input, opts);
            if (res.status === 401) {
                let payload = null;
                try { payload = await res.clone().json(); } catch (e) {}
                if (payload && payload.error === 'pin_required') {
                    const entered = await promptForDashboardPin();
                    if (entered) {
                        sessionStorage.setItem(PIN_STORAGE_KEY, entered);
                        headers.set('X-Dashboard-Pin', entered);
                        res = await nativeFetch(input, opts); // single replay
                        let second = null;
                        try { second = await res.clone().json(); } catch (e) {}
                        if (second && second.error === 'pin_required') {
                            sessionStorage.removeItem(PIN_STORAGE_KEY);
                            showToast('Dashboard PIN is incorrect', 'error');
                        }
                    }
                }
            }
            return res;
        };

        function isSessionExpired(res, payload) {
            return !!(res && res.status === 401 && payload && payload.error === 'session_expired');
        }

        // Exit path for session_expired: tear the session down and reset the
        // step wizard to step 1 instead of leaving a dead-end Retry button.
        function handleSessionExpired() {
            stopPolling();
            currentTaskId = null;
            currentTaskStatus = null;
            const staleSession = currentSessionId || localStorage.getItem('tg_session_id');
            localStorage.removeItem('tg_session_id');
            currentSessionId = null;
            if (staleSession) {
                fetch('/api/auth/logout', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ session_id: staleSession })
                }).catch(() => {});
            }
            document.getElementById('step-verify').classList.add('hidden');
            document.getElementById('step-select').classList.add('hidden');
            document.getElementById('step-progress').classList.add('hidden');
            document.getElementById('step-auth').classList.remove('hidden');
            setBadge('Idle', 'idle');
            showToast('Session expired — please log in again', 'error');
        }

        function showToast(message, type = 'info') {
            const container = document.getElementById('toast-container');
            if (!container) return;
            const style = TOAST_STYLES[type] || TOAST_STYLES.info;
            const toast = document.createElement('div');
            toast.className = `pointer-events-auto flex items-start gap-2.5 w-full bg-slate-900/95 backdrop-blur-md border border-slate-700/70 ${style.bar} border-l-4 rounded-xl shadow-2xl px-3.5 py-3 toast-in`;
            toast.innerHTML = `
                <i class="fa-solid ${style.icon} ${style.iconClass} text-sm mt-0.5 flex-shrink-0"></i>
                <span class="flex-1 text-xs leading-relaxed text-slate-200 break-words">${escapeHtml(message)}</span>
                <button type="button" aria-label="Dismiss notification" class="text-slate-500 hover:text-slate-200 transition flex-shrink-0 px-1 -my-0.5">
                    <i class="fa-solid fa-xmark text-xs"></i>
                </button>
            `;
            const dismiss = () => {
                if (toast.dataset.closing) return;
                toast.dataset.closing = '1';
                toast.classList.remove('toast-in');
                toast.classList.add('toast-out');
                setTimeout(() => toast.remove(), 240);
            };
            toast.querySelector('button').addEventListener('click', dismiss);
            container.prepend(toast);
            while (container.children.length > 4) container.lastElementChild.remove();
            setTimeout(dismiss, 4200);
        }

        function loadFavorites() {
            try {
                const raw = localStorage.getItem(FAV_STORAGE_KEY);
                const parsed = raw ? JSON.parse(raw) : [];
                return new Set(Array.isArray(parsed) ? parsed.map(v => String(v)) : []);
            } catch (e) {
                return new Set();
            }
        }

        function saveFavorites() {
            try {
                localStorage.setItem(FAV_STORAGE_KEY, JSON.stringify(Array.from(favoriteIds)));
            } catch (e) {}
        }

        function toggleFavorite(id) {
            const nowFavorite = !favoriteIds.has(id);
            if (nowFavorite) favoriteIds.add(id); else favoriteIds.delete(id);
            saveFavorites();
            applyFavoriteStates();
            const btn = document.querySelector(`[data-fav-btn="${id}"]`);
            if (btn) {
                btn.classList.remove('pop');
                void btn.offsetWidth;
                btn.classList.add('pop');
            }
            showToast(nowFavorite ? 'Added to favorites and protected from cleanup' : 'Removed from favorites', nowFavorite ? 'success' : 'info');
        }

        function applyFavoriteStates() {
            document.querySelectorAll('.chat-row').forEach(row => {
                const id = row.dataset.id;
                const isFav = favoriteIds.has(id);
                row.dataset.fav = isFav ? '1' : '0';
                row.classList.toggle('border-l-amber-400/70', isFav);
                row.classList.toggle('bg-amber-950/10', isFav);
                row.classList.toggle('border-l-transparent', !isFav);

                const btn = row.querySelector('.fav-btn');
                if (btn) {
                    btn.className = FAV_BTN_BASE + (isFav
                        ? ' text-amber-400 bg-amber-950/50 border border-amber-500/40'
                        : ' text-slate-600 hover:text-amber-400 hover:bg-slate-800 border border-transparent');
                    btn.innerHTML = isFav ? '<i class="fa-solid fa-star" aria-hidden="true"></i>' : '<i class="fa-regular fa-star" aria-hidden="true"></i>';
                    btn.setAttribute('aria-pressed', isFav ? 'true' : 'false');
                }

                const cb = row.querySelector('.chat-checkbox');
                if (cb) {
                    cb.disabled = isFav;
                    if (isFav) cb.checked = false;
                }
            });
            updateFilterCounts();
            updateCount();
        }

        function updateFilterCounts() {
            const counts = { all: loadedChats.length, favorites: 0, Channel: 0, Group: 0, Bot: 0 };
            loadedChats.forEach(c => {
                if (favoriteIds.has(c.id)) counts.favorites++;
                if (c.type === 'Channel') counts.Channel++;
                else if (c.type === 'Bot') counts.Bot++;
                else if (c.type === 'Group' || c.type === 'Supergroup') counts.Group++;
            });
            const countIds = { all: 'cnt-all', favorites: 'cnt-favorites', Channel: 'cnt-Channel', Group: 'cnt-Group', Bot: 'cnt-Bot' };
            Object.entries(countIds).forEach(([key, elId]) => {
                const el = document.getElementById(elId);
                if (el) el.innerText = counts[key];
            });
        }

        function setChatFilter(filter) {
            activeChatFilter = filter;
            document.querySelectorAll('.chat-filter-btn').forEach(b => {
                const active = b.dataset.filter === filter;
                b.setAttribute('aria-selected', active ? 'true' : 'false');
                b.className = active ? FILTER_BTN_ACTIVE : FILTER_BTN_IDLE;
            });
            applyChatVisibility();
        }

        function applyChatVisibility() {
            const q = document.getElementById('search-input').value.toLowerCase();
            document.querySelectorAll('.chat-row').forEach(row => {
                const text = row.innerText.toLowerCase();
                const isFav = row.dataset.fav === '1';
                const type = row.dataset.type || '';
                let passFilter = true;
                if (activeChatFilter === 'favorites') passFilter = isFav;
                else if (activeChatFilter === 'Channel') passFilter = type === 'Channel';
                else if (activeChatFilter === 'Group') passFilter = type === 'Group' || type === 'Supergroup';
                else if (activeChatFilter === 'Bot') passFilter = type === 'Bot';
                row.style.display = (passFilter && text.includes(q)) ? 'flex' : 'none';
            });
        }

        function invertSelection() {
            let selectedCount = 0;
            document.querySelectorAll('.chat-row').forEach(row => {
                if (row.style.display === 'none') return;
                const cb = row.querySelector('.chat-checkbox');
                if (!cb || cb.disabled) return;
                cb.checked = !cb.checked;
                if (cb.checked) selectedCount++;
            });
            updateCount();
            showToast(`Selection inverted: ${selectedCount} item(s) now selected`, 'info');
        }

        function favoriteAllSelected() {
            const checked = Array.from(document.querySelectorAll('.chat-checkbox:checked'));
            if (checked.length === 0) {
                showToast('No items selected to favorite', 'error');
                return;
            }
            checked.forEach(cb => favoriteIds.add(cb.value));
            saveFavorites();
            applyFavoriteStates();
            showToast(`${checked.length} selected item(s) added to favorites and protected`, 'success');
        }

        function unfavoriteAll() {
            if (favoriteIds.size === 0) {
                showToast('No favorites to clear', 'info');
                return;
            }
            const cleared = favoriteIds.size;
            favoriteIds.clear();
            saveFavorites();
            applyFavoriteStates();
            showToast(`${cleared} favorite(s) cleared`, 'info');
        }

        function renderChatSkeleton() {
            const container = document.getElementById('chats-container');
            const skeletonRow = `
                <div class="flex items-center justify-between p-3.5 border-b border-slate-800/80 animate-pulse">
                    <div class="flex items-center space-x-3">
                        <div class="w-5 h-5 rounded bg-slate-800"></div>
                        <div class="space-y-2">
                            <div class="h-3 bg-slate-800 rounded w-40 sm:w-56"></div>
                            <div class="h-2.5 bg-slate-800/70 rounded w-24 sm:w-32"></div>
                        </div>
                    </div>
                    <div class="w-8 h-8 rounded-lg bg-slate-800/70"></div>
                </div>
            `;
            container.innerHTML = Array.from({ length: 6 }).map(() => skeletonRow).join('') +
                `<div class="p-4 text-center text-[11px] text-slate-500"><i class="fa-solid fa-spinner fa-spin mr-1.5"></i> Fetching your joined channels, groups and active bots...</div>`;
        }

        function initApp() {
            restoreInputs();
            favoriteIds = loadFavorites();
        }

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

        const SPEED_BTN_IDLE = "speed-btn py-2.5 px-3 rounded-lg border border-slate-800 bg-slate-900 text-xs font-medium hover:border-slate-700 transition";
        const SPEED_BTN_ACTIVE = "speed-btn py-2.5 px-3 rounded-lg border border-cyan-500 bg-cyan-950/40 text-cyan-300 text-xs font-medium transition";
        const LIVE_SLIDER_MAX = 15;

        function matchSpeedPreset(min, max) {
            const minN = parseFloat(min);
            const maxN = parseFloat(max);
            if (!isFinite(minN) || !isFinite(maxN)) return null;
            const found = Object.keys(SPEED_PRESETS).find(
                key => minN === SPEED_PRESETS[key].min && maxN === SPEED_PRESETS[key].max);
            return found || null;
        }

        function clampLiveSpeed(val) {
            const n = parseFloat(val);
            if (!isFinite(n)) return null;
            return Math.min(Math.max(n, 0), LIVE_SLIDER_MAX);
        }

        function setLiveSpeedUI(val) {
            const clamped = clampLiveSpeed(val);
            if (clamped === null) return;
            const slider = document.getElementById('live-speed-slider');
            if (slider) slider.value = clamped;
            const label = document.getElementById('live-speed-val');
            if (label) label.innerText = clamped + 's';
        }

        // Single source of truth for the whole speed UI: mirrors the step-1
        // delays into step 3, then refreshes preset select, button highlight,
        // label and the live slider from those values.
        function syncSpeedUI() {
            const minEl = document.getElementById('min_delay');
            const maxEl = document.getElementById('max_delay');
            if (!minEl || !maxEl) return;
            const min = minEl.value;
            const max = maxEl.value;

            const s3min = document.getElementById('step3-min-delay');
            const s3max = document.getElementById('step3-max-delay');
            if (s3min) s3min.value = min;
            if (s3max) s3max.value = max;

            const preset = matchSpeedPreset(min, max);
            document.querySelectorAll('.speed-btn').forEach(b => { b.className = SPEED_BTN_IDLE; });
            if (preset) {
                const activeBtn = document.getElementById('speed-btn-' + preset);
                if (activeBtn) activeBtn.className = SPEED_BTN_ACTIVE;
            }

            const label = document.getElementById('speed-label');
            if (label) label.innerText = preset ? SPEED_PRESETS[preset].label : `Custom (${min}-${max}s)`;

            const select = document.getElementById('select-speed-preset');
            if (select) select.value = preset || 'custom';

            setLiveSpeedUI(min);
        }

        function setSpeedMode(mode) {
            const p = SPEED_PRESETS[mode];
            if (!p) return;
            document.getElementById('min_delay').value = p.min;
            document.getElementById('max_delay').value = p.max;
            saveInputs();
            syncSpeedUI();
        }

        function applySpeedPreset(mode) {
            setSpeedMode(mode);
        }

        function syncDelayInputs(val, type) {
            const target = document.getElementById(type === 'min' ? 'min_delay' : 'max_delay');
            if (target) target.value = val;
            saveInputs();
            syncSpeedUI();
        }

        async function changeLiveSpeed(val) {
            const clamped = clampLiveSpeed(val);
            if (clamped === null) return;
            setLiveSpeedUI(clamped);
            if (!currentTaskId) return;
            try {
                await fetch(`/api/task/speed/${currentTaskId}`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ min_delay: clamped, max_delay: clamped + 1.5 })
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
            const minEl = document.getElementById('min_delay');
            const maxEl = document.getElementById('max_delay');
            if (minEl && !minEl.value) minEl.value = 3;
            if (maxEl && !maxEl.value) maxEl.value = 6;
            syncSpeedUI();
        }

        function clearSavedData() {
            showCustomModal("Reset Credentials", "Are you sure you want to reset all stored API credentials?", "danger", async () => {
                const staleSession = localStorage.getItem('tg_session_id');
                if (staleSession) {
                    try {
                        await fetch('/api/auth/logout', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({ session_id: staleSession })
                        });
                    } catch (e) {}
                }
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
                showToast('Please fill in API ID, API Hash, and Phone Number before continuing.', 'error');
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
                    showToast('Login code sent — check your Telegram app for the code', 'success');
                } else if (json.status === 'logged_in') {
                    currentSessionId = json.session_id;
                    localStorage.setItem('tg_session_id', currentSessionId);
                    document.getElementById('step-auth').classList.add('hidden');
                    showToast('Session restored — loading your dialogs', 'success');
                    loadChatsList();
                } else {
                    showToast(json.message || 'Failed to connect to Telegram.', 'error');
                }
            } catch (err) {
                showToast('Network error: ' + String(err), 'error');
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
                    showToast('Login verified — loading your dialogs', 'success');
                    loadChatsList();
                } else {
                    showToast(json.message || 'Invalid OTP code or password.', 'error');
                }
            } catch (err) {
                showToast('Network error: ' + String(err), 'error');
            } finally {
                btn.disabled = false;
                btn.innerHTML = '<i class="fa-solid fa-arrow-right mr-2"></i> Confirm Login & Show My Dialogs';
            }
        }

        function renderDialogsError(message) {
            const container = document.getElementById('chats-container');
            const template = document.getElementById('dialogs-error-template');
            container.textContent = '';
            if (template) {
                const node = template.content.cloneNode(true);
                const msg = node.querySelector('[data-dialogs-error-msg]');
                if (msg) msg.textContent = message;
                const retry = node.querySelector('#retry-dialogs-btn');
                if (retry) retry.addEventListener('click', () => loadChatsList());
                container.appendChild(node);
            } else {
                const fallback = document.createElement('div');
                fallback.className = 'p-6 text-center text-rose-400';
                fallback.textContent = message;
                container.appendChild(fallback);
            }
        }

        async function loadChatsList() {
            document.getElementById('step-select').classList.remove('hidden');
            renderChatSkeleton();

            try {
                const res = await fetch(`/api/dialogs/${currentSessionId}`);
                const json = await res.json();
                if (json.status === 'success') {
                    loadedChats = json.chats;
                    renderChats(loadedChats);
                } else if (isSessionExpired(res, json) || json.error === 'session_not_found') {
                    // Routes straight to the login step, so Retry can never loop.
                    handleSessionExpired();
                } else {
                    renderDialogsError(json.message || 'Failed to load dialogs');
                    showToast(json.message || 'Failed to load dialogs', 'error');
                }
            } catch (e) {
                renderDialogsError('Failed to load dialogs: ' + String(e));
                showToast('Failed to load dialogs: ' + String(e), 'error');
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

                const isFav = favoriteIds.has(chat.id);
                const safeId = escapeHtml(chat.id);
                const safeName = escapeHtml(chat.name);
                const safeType = escapeHtml(chat.type);
                const safeUsername = chat.username ? escapeHtml(chat.username) : '';

                return `
                <div class="chat-row flex items-center justify-between p-3 sm:p-3.5 border-b border-l-2 border-slate-800/80 ${isFav ? 'border-l-amber-400/70 bg-amber-950/10' : 'border-l-transparent'} hover:bg-slate-900/60 active:bg-slate-900 transition cursor-pointer select-none" data-id="${safeId}" data-type="${safeType}" data-fav="${isFav ? '1' : '0'}" onclick="toggleRow(event, '${safeId}')">
                    <div class="flex items-center space-x-3 min-w-0 flex-1">
                        <input type="checkbox" value="${safeId}" data-type="${safeType}" ${isFav ? 'disabled' : ''} aria-label="Select ${safeName} for cleanup" class="chat-checkbox w-5 h-5 sm:w-4 sm:h-4 rounded bg-slate-950 border-slate-700 text-cyan-500 focus:ring-0 focus:ring-offset-0 cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed" onclick="event.stopPropagation(); updateCount();">
                        <div class="min-w-0">
                            <p class="text-xs sm:text-sm font-medium text-slate-200 truncate flex items-center gap-1.5">
                                <i class="fa-solid ${icon} text-[11px] text-slate-400"></i>
                                <span class="truncate">${safeName}</span>
                                ${isFav ? '<i class="fa-solid fa-star text-[9px] text-amber-400 flex-shrink-0" title="Protected favorite"></i>' : ''}
                            </p>
                            <p class="text-[11px] sm:text-xs text-slate-500 flex items-center gap-1.5 sm:gap-2 truncate">
                                <span class="text-slate-400 font-mono truncate">${safeUsername ? '@' + safeUsername : 'ID: ' + safeId}</span>
                                <span>•</span>
                                <span class="px-1.5 sm:px-2 py-0.5 rounded text-[9px] sm:text-[10px] flex-shrink-0 ${badgeClass}">${safeType}</span>
                                ${isFav ? '<span class="px-1.5 sm:px-2 py-0.5 rounded text-[9px] sm:text-[10px] flex-shrink-0 bg-amber-950 text-amber-300 border border-amber-800/60">Protected</span>' : ''}
                            </p>
                        </div>
                    </div>
                    <button type="button" data-fav-btn="${safeId}" onclick="event.stopPropagation(); toggleFavorite('${safeId}')" aria-pressed="${isFav}" aria-label="${isFav ? 'Remove' : 'Add'} ${safeName} ${isFav ? 'from' : 'to'} favorites" title="${isFav ? 'Unfavorite (unprotect)' : 'Favorite (protect from cleanup)'}" class="${FAV_BTN_BASE} ${isFav ? 'text-amber-400 bg-amber-950/50 border border-amber-500/40' : 'text-slate-600 hover:text-amber-400 hover:bg-slate-800 border border-transparent'}">
                        <i class="${isFav ? 'fa-solid' : 'fa-regular'} fa-star" aria-hidden="true"></i>
                    </button>
                </div>
                `;
            }).join('');

            applyFavoriteStates();
        }

        function toggleRow(event, id) {
            if (event.target.closest('input, button')) return;
            const cb = document.querySelector(`input[value="${id}"]`);
            if (cb && !cb.disabled) {
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
                if (cb.disabled) return;
                if (cb.closest('.chat-row').style.display !== 'none') {
                    cb.checked = state;
                }
            });
            updateCount();
        }

        function selectByType(type) {
            document.querySelectorAll('.chat-checkbox').forEach(cb => {
                const row = cb.closest('.chat-row');
                if (!row || row.style.display === 'none') return;
                if (cb.disabled) return;
                const cbType = cb.dataset.type || '';
                // Mirrors applyChatVisibility: supergroups count as groups.
                const matches = type === 'Group'
                    ? (cbType === 'Group' || cbType === 'Supergroup')
                    : cbType === type;
                cb.checked = matches;
            });
            updateCount();
        }

        function filterChats() {
            applyChatVisibility();
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
            const favCount = favoriteIds.size;

            const breakdown = `
                <div class="space-y-2">
                    <p class="font-semibold text-slate-200">You are about to clean <b>${checked.length}</b> total item(s):</p>
                    <ul class="list-disc list-inside text-xs text-slate-400 space-y-1">
                        ${channelCount > 0 ? `<li><b>${channelCount}</b> Channels (Leave)</li>` : ''}
                        ${groupCount > 0 ? `<li><b>${groupCount}</b> Groups (Leave)</li>` : ''}
                        ${botCount > 0 ? `<li><b>${botCount}</b> Bots (Stop & Complete Wipe)</li>` : ''}
                    </ul>
                    ${favCount > 0 ? `<p class="text-xs text-amber-400 flex items-center gap-1.5 pt-1 border-t border-slate-800"><i class="fa-solid fa-star"></i> <b>${favCount}</b> favorites protected &mdash; excluded from cleanup.</p>` : ''}
                    <p class="text-xs text-cyan-400 pt-1">Execution Delay: ${escapeHtml(minD)} - ${escapeHtml(maxD)} seconds per item.</p>
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
                        exclude_ids: Array.from(favoriteIds),
                        min_delay: minD,
                        max_delay: maxD
                    })
                });
                const json = await res.json();
                if (json.status === 'success') {
                    currentTaskId = json.task_id;
                    currentTaskStatus = 'starting';
                    document.getElementById('step-select').classList.add('hidden');
                    setLiveSpeedUI(minD);
                    showProgress();
                    showToast(`Cleanup task started for ${checked.length} item(s)`, 'success');
                } else if (isSessionExpired(res, json)) {
                    handleSessionExpired();
                } else if (res.status === 409 && json.error === 'task_already_running' && json.task_id) {
                    // Re-attach to the task the server is already running.
                    currentTaskId = json.task_id;
                    currentTaskStatus = 'running';
                    document.getElementById('step-select').classList.add('hidden');
                    showProgress();
                    showToast('A cleanup task is already running for this session', 'info');
                } else {
                    showToast(json.message || 'Failed to start task.', 'error');
                    resetLeaveButton();
                }
            } catch (err) {
                showToast('Network error: ' + String(err), 'error');
                resetLeaveButton();
            }
        }

        function resetLeaveButton() {
            const btn = document.getElementById('btn-leave-selected');
            btn.disabled = false;
            btn.innerHTML = '<i class="fa-solid fa-trash-can mr-2"></i> Leave & Clean Selected';
        }

        function showProgress() {
            document.getElementById('step-progress').classList.remove('hidden');
            setBadge('Running', 'running');
            pollFailures = 0;
            stopPolling();
            pollInterval = setInterval(updateTaskStatus, 1500);
        }

        async function updateTaskStatus() {
            if (!currentTaskId) return;
            let task = null;
            try {
                const res = await fetch(`/api/task/status/${currentTaskId}`);
                if (!res.ok) {
                    let err = null;
                    try { err = await res.clone().json(); } catch (e) {}
                    if (isSessionExpired(res, err)) {
                        stopPolling();
                        handleSessionExpired();
                        return;
                    }
                    // e.g. 404 after a server restart: kill the badge, no zombie "Running".
                    stopPolling();
                    currentTaskStatus = 'error';
                    setBadge('ERROR', 'error');
                    showToast('Task not found — server may have restarted', 'error');
                    return;
                }
                pollFailures = 0;
                task = await res.json();
            } catch (err) {
                pollFailures++;
                if (pollFailures >= 3) {
                    stopPolling();
                    currentTaskStatus = 'error';
                    setBadge('ERROR', 'error');
                    showToast('Lost contact with the server — task status unavailable', 'error');
                }
                return;
            }

            currentTaskStatus = task.status;

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
            // Log lines carry Telegram-sourced entity names: build them with
            // textContent so a hostile title can never inject markup.
            logsBox.textContent = '';
            const logFragment = document.createDocumentFragment();
            currentLogs.forEach(line => {
                const row = document.createElement('div');
                row.textContent = line;
                logFragment.appendChild(row);
            });
            logsBox.appendChild(logFragment);
            logsBox.scrollTop = logsBox.scrollHeight;

            if (['completed', 'cancelled', 'error'].includes(task.status)) {
                stopPolling();
                if (task.status === 'completed') {
                    setBadge('COMPLETED', 'success');
                } else {
                    setBadge(task.status.toUpperCase(), 'error');
                }
            }
        }

        function exportLogs() {
            if (!currentLogs || currentLogs.length === 0) {
                showToast('No logs available to export yet', 'info');
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
            showToast('Stop requested — task will halt after the current item', 'info');
        }

        function taskIsStillActive() {
            return !!currentTaskId && (currentTaskStatus === 'running' || currentTaskStatus === 'starting');
        }

        function returnToSelection() {
            if (taskIsStillActive()) {
                showCustomModal(
                    "Task Running",
                    "A cleanup task is still running. Stop it and go back?",
                    "danger",
                    stopTaskAndReturnToSelection
                );
                return;
            }
            navigateToSelection();
        }

        async function stopTaskAndReturnToSelection() {
            const taskId = currentTaskId;
            if (taskId) {
                try {
                    await fetch(`/api/task/stop/${taskId}`, { method: 'POST' });
                } catch (e) {}
                showToast('Stop requested — waiting for the task to halt', 'info');
                for (let i = 0; i < 10; i++) {
                    try {
                        const res = await fetch(`/api/task/status/${taskId}`);
                        if (res.ok) {
                            const t = await res.json();
                            if (['cancelled', 'completed', 'error'].includes(t.status)) break;
                        }
                    } catch (e) {}
                    await new Promise(resolve => setTimeout(resolve, 500));
                }
            }
            navigateToSelection();
        }

        function navigateToSelection() {
            stopPolling();
            currentTaskId = null;
            currentTaskStatus = null;
            document.getElementById('step-progress').classList.add('hidden');
            document.getElementById('step-select').classList.remove('hidden');
            setBadge('Selection Mode', 'idle');
            resetLeaveButton();
            loadChatsList();
        }

        document.addEventListener('DOMContentLoaded', initApp);
    </script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE, local_ip=get_local_ip(), site_url=SITE_URL)


@app.route("/robots.txt")
def robots_txt():
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
    )
    if SITE_URL_IS_CUSTOM:
        body += f"Sitemap: {SITE_URL}/sitemap.xml\n"
    return Response(body, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{SITE_URL}/</loc><changefreq>weekly</changefreq>"
        "<priority>1.0</priority></url>\n"
        "</urlset>"
    )
    return Response(xml, mimetype="application/xml")


@app.route("/api/auth/send_code", methods=["POST"])
@json_errors
def send_code():
    data = request.get_json(silent=True) or {}

    # Shape/format first: garbage input must 400 before it can consume the
    # per-IP cooldown, and before any number parsing can raise.
    api_id = _get_int(data, "api_id", None, 1, 10 ** 9)
    api_hash = _get_str(data, "api_hash", required=True, max_len=128)
    phone = _get_str(data, "phone", required=True, max_len=32, pattern=r"\+?\d{7,15}")
    bot_token = _get_str(data, "bot_token", max_len=128)
    log_user_id = _get_str(data, "log_user_id", max_len=64)
    min_delay, max_delay = _get_delays(data, 3, 6)

    digits = sanitize_phone_digits(phone)
    if digits is None:
        raise InputValidationError("phone must contain between 7 and 15 digits")

    # Every validation passed: only now consume the per-IP cooldown window
    # (1 attempt / 60s) right before the code is actually sent.
    remaining = _cooldown_remaining(_send_code_last_attempt, SEND_CODE_COOLDOWN_SECONDS)
    if remaining:
        return _too_many_requests(remaining, "Too many login attempts. Please wait 60 seconds.")

    session_id = str(uuid.uuid4())
    session_file = os.path.join("tg_sessions", f"sess_{digits}")

    async def _async_auth():
        for old_id, old_s in list(sessions_store.items()):
            if old_s.get("phone") == phone and "client" in old_s:
                try:
                    await old_s["client"].disconnect()
                except Exception:
                    pass
                old_s["connected"] = False
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
                "log_user_id": log_user_id, "min_delay": min_delay, "max_delay": max_delay,
                "connected": True, "authorized": True
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
            "max_delay": max_delay,
            "connected": True,
            "authorized": False
        }
        return {"status": "code_sent", "session_id": session_id}

    future = asyncio.run_coroutine_threadsafe(_async_auth(), loop)
    try:
        res = future.result(timeout=25)
        return jsonify(res)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Connection error: {str(e)}"}), 500


@app.route("/api/auth/verify", methods=["POST"])
@json_errors
def verify_code():
    data = request.get_json(silent=True) or {}
    session_id = _get_str(data, "session_id", required=True, max_len=64)
    code = _get_str(data, "code", max_len=64)
    password = _get_str(data, "password", max_len=1024)
    if not code and not password:
        raise InputValidationError("verification code or password is required")

    # Shape is valid: consume the per-IP verify cooldown (1 attempt / 30s)
    # before touching the session store or Telegram.
    remaining = _cooldown_remaining(_verify_last_attempt, VERIFY_CODE_COOLDOWN_SECONDS)
    if remaining:
        return _too_many_requests(remaining, "Too many verification attempts. Please wait 30 seconds.")

    sess = sessions_store.get(session_id)
    if not sess:
        return jsonify({"status": "error", "error": "session_not_found",
                        "message": "Invalid or expired session. Please connect again."}), 400

    phone = sess["phone"]
    phone_code_hash = sess.get("phone_code_hash")
    if not password and not phone_code_hash:
        return jsonify({"status": "error", "error": "session_not_found",
                        "message": "Session is no longer awaiting a code. Please connect again."}), 400

    async def _async_sign_in(client):
        try:
            if password:
                await client.sign_in(password=password)
            else:
                await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except errors.SessionPasswordNeededError:
            return {"status": "2fa_required"}
        except (ConnectionError, OSError):
            raise  # let run_with_connection_retry reconnect and retry once
        except Exception as e:
            return {"status": "error", "message": str(e)}

        sess["authorized"] = True
        return {"status": "success", "session_id": session_id}

    # The user is not authorized yet at this point, so skip that probe.
    future = asyncio.run_coroutine_threadsafe(
        run_with_connection_retry(sess, _async_sign_in, require_authorized=False), loop)
    try:
        res = future.result(timeout=25)
        return jsonify(res)
    except Exception as e:
        return session_error_response(e)


@app.route("/api/auth/logout", methods=["POST"])
@json_errors
def logout_session():
    """Best-effort session teardown so the frontend always has an exit path.

    Idempotent by design: an unknown or already-removed session_id still 200s.
    """
    data = request.get_json(silent=True) or {}
    session_id = _get_str(data, "session_id", max_len=64)

    sess = sessions_store.pop(session_id, None) if session_id else None
    if sess is not None:
        sess["connected"] = False
        client = sess.get("client")
        if client is not None:
            async def _disconnect():
                try:
                    await client.disconnect()
                except Exception:
                    pass

            try:
                asyncio.run_coroutine_threadsafe(_disconnect(), loop).result(timeout=10)
            except Exception:
                pass
    return jsonify({"status": "ok"})


@app.route("/api/dialogs/<session_id>")
def get_dialogs_list(session_id):
    sess = sessions_store.get(session_id)
    if not sess:
        return jsonify({"status": "error", "error": "session_not_found",
                        "message": "Session expired or not found. Please connect again."}), 404

    async def _async_get_dialogs(client):
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

    future = asyncio.run_coroutine_threadsafe(run_with_connection_retry(sess, _async_get_dialogs), loop)
    try:
        res = future.result(timeout=40)
        return jsonify(res)
    except Exception as e:
        return session_error_response(e)


@app.route("/api/task/start", methods=["POST"])
@json_errors
def start_task():
    data = request.get_json(silent=True) or {}
    session_id = _get_str(data, "session_id", required=True, max_len=64)
    target_ids = _get_id_list(data, "target_ids")
    exclude_ids = _get_id_list(data, "exclude_ids")
    min_delay, max_delay = _get_delays(data, 3.0, 6.0)

    # Single-task guard: one cleanup task per session at a time.
    for existing_id, existing in tasks_store.items():
        if existing.get("session_id") == session_id and existing.get("status") in ("starting", "running"):
            return jsonify({"status": "error", "error": "task_already_running",
                            "task_id": existing_id,
                            "message": "A cleanup task is already running for this session."}), 409

    sess = sessions_store.get(session_id)
    if not sess:
        return jsonify({"status": "error", "error": "session_expired",
                        "message": "Session expired. Please reconnect."}), 401

    cached_entities = sess.get("cached_entities", {})
    selected_entities = [cached_entities[cid] for cid in target_ids if cid in cached_entities]

    if not selected_entities:
        return jsonify({"status": "error", "error": "no_targets",
                        "message": "No valid chats or bots selected"}), 400

    # Revive the stored client before committing to a task: a dead session
    # must fail fast with clean JSON instead of an always-error task.
    future = asyncio.run_coroutine_threadsafe(
        run_with_connection_retry(sess, _noop_operation), loop)
    try:
        future.result(timeout=25)
    except Exception as e:
        return session_error_response(e)

    task_id = str(uuid.uuid4())
    tasks_store[task_id] = {
        "status": "starting", "total": len(selected_entities), "current_index": 0, "current_name": "",
        "session_id": session_id,
        "min_delay": min_delay, "max_delay": max_delay, "current_delay": min_delay,
        "left": 0, "failed": 0, "protected": 0, "exclude_ids": sorted(set(exclude_ids)),
        "logs": [], "cancelled": False
    }

    asyncio.run_coroutine_threadsafe(
        execute_leaving_process(
            task_id, sess["client"], selected_entities,
            sess.get("bot_token", ""), sess.get("log_user_id", ""), session_entry=sess
        ),
        loop
    )

    return jsonify({"status": "success", "task_id": task_id})


@app.route("/api/task/speed/<task_id>", methods=["POST"])
@json_errors
def change_speed(task_id):
    data = request.get_json(silent=True) or {}
    min_delay, max_delay = _get_delays(data, 2.0, 5.0)
    task = tasks_store.get(task_id)
    if task:
        task["min_delay"] = min_delay
        task["max_delay"] = max_delay
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
