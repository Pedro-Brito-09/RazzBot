import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import aiohttp
import json
import base64
import zstandard as zstd
import traceback
import asyncio
import io
import socket
import contextvars
import functools
import re
import secrets
import string
import time
from urllib.parse import quote
from datetime import datetime, time as datetime_time, timedelta, timezone

# aiohttp's default UA ("Python/3.x aiohttp/3.x") appears to get blackholed by
# Roblox's edge: requests that work instantly under curl never return headers.
# Send a conventional UA instead.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Staged rather than one total budget: fail fast on a dead connection, but
# still allow a slow body through. aiohttp's default total is 5 minutes.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=10, sock_connect=10, sock_read=30)
MAX_ATTEMPTS = 3

# Twemoji's trophy gold, so the bar matches the 🏆 in the title.
LEADERBOARD_COLOR = discord.Color(0xFFCC4D)
MAP_COLOR = discord.Color.blurple()
DAILY_CUP_COLOR = discord.Color(0xF1C40F)
PROFILE_COLOR = discord.Color(0x9B59B6)
MAX_LEADERBOARD_ROWS = 10
# Maps shown per page in a search result or a player's map list.
MAPS_PER_PAGE = 10
# Admin commands are prefix-only and answer to this account alone, so they
# never appear in anyone's slash command list.
ADMIN_USER_ID = 541010558653169667

GLOBAL_LEADERBOARDS = {
    "wins": ("Wins", "Wins"),
    "medals": ("Medals", "Medals"),
    "creators": ("Creator Points", "Creator Points"),
}
HEADSHOT_SIZE = "150x150"
# Custom emoji cannot render inside a code block, so the table style needs
# Unicode stand-ins for the medals.
UNICODE_MEDALS = {0: "💎", 1: "🥇", 2: "🥈", 3: "🥉"}

# The Community Maps "Ids" entry holds ~85k records, so it is fetched once and
# indexed rather than downloaded and decompressed on every lookup.
MAPS_CACHE_TTL = 600
# Only the fields the embed needs are retained, to keep the index small.
MAP_FIELDS = ("Id", "Name", "Creator", "Plays", "Favorites",
              "Playstyle", "Privacy", "Featured")

UNIVERSE_ID = 8993151589
# Every command has a !dev_ twin that runs against this universe instead.
DEV_UNIVERSE_ID = int(os.getenv("DEV_UNIVERSE_ID") or 7117693401)
# Ownership is checked one badge per request against inventory.roblox.com,
# the only endpoint that answers without authentication.
OWNED_BADGES_CACHE_TTL = 300
BADGE_CONCURRENCY = 8
BADGE_ICON_SIZE = "150x150"
# Discord decides gallery layout from the item count. Nine images make a
# balanced grid; a final group of six or fewer uses six slots instead so it
# keeps the same proportions without spending three unnecessary components.
BADGES_PER_LARGE_GALLERY = 9
BADGES_PER_SMALL_GALLERY = 6
# Short galleries get padded with this so every row keeps the same layout;
# Discord sizes images by how many are in the gallery. Swap it for any
# transparent PNG Discord's proxy can reach.
EMPTY_IMAGE_URL = "https://upload.wikimedia.org/wikipedia/commons/c/ca/1x1.png"
# A message caps at 40 components and every gallery plus each of its images
# counts. Three nine-image galleries leave room for the surrounding content
# and the summary shown when more badges were earned.
MAX_BADGE_GALLERIES = 3

# Roblox game URL for challenge mode on a community map.
PLAY_URL_TEMPLATE = (
    "https://www.roblox.com/games/start"
    "?placeId=86832525327994&launchData=Map%2F{id}"
)
# Daily Cup announcements use the cup experience rather than Challenge Mode.
DAILY_CUP_PLAY_URL_TEMPLATE = (
    "https://www.roblox.com/games/start"
    "?placeId=133478407190616"
)
# A community map's leaderboard is keyed by the map ID on its own.
MAP_LEADERBOARD_KEY = "{id}"
# How long the Leaderboard button stays clickable.
MAP_VIEW_TIMEOUT = 900
# Long names get truncated so the table keeps its columns.
MAX_NAME_WIDTH = 18
LINK_CODE_LENGTH = 8
LINK_CODE_ALPHABET = string.ascii_uppercase + string.digits
LINK_CODE_TTL = 5 * 60
ACCOUNT_LINK_DATASTORE = "AccountLinking"
LINK_CODE_EXPIRATIONS_KEY = "CodeExpirations"
# Discord role rules live here, keyed by guild.
ROLES_DATASTORE = "Roles"
ROLE_CONDITIONS = ("badge", "map")

TOKEN = os.getenv("TOKEN")
API_KEY = os.getenv("API_KEY")
VERIFICATION_GAME_URL = os.getenv(
    "VERIFICATION_GAME_URL",
    "https://www.roblox.com/games/start?placeId=120140749641241",
)
# Optional: also sync a guild-only test copy so changes appear instantly there.
# Global commands are always synced because user installs only support them.
GUILD_ID = os.getenv("GUILD_ID")
# Channel that receives the previous leaderboard and new map every day at the
# Daily Cup rollover (09:00 UTC, matching cup_day_today()).
DAILY_CUP_CHANNEL_ID = os.getenv("DAILY_CUP_CHANNEL_ID")
DAILY_CUP_ROLE_ID = os.getenv("DAILY_CUP_ROLE_ID")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    # Replaced by the /help below, which can hide the admin commands.
    help_command=None,
    allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
    allowed_contexts=app_commands.AppCommandContext(
        guild=True, dm_channel=True, private_channel=True
    ),
)

_synced = False
_session = None

# Which universe the command currently running should talk to. A ContextVar
# rather than a global so concurrent invocations can't read each other's.
_active_universe = contextvars.ContextVar("active_universe", default=None)
# Who invoked the running command, so its components can refuse everyone else.
_active_invoker = contextvars.ContextVar("active_invoker", default=None)

def current_invoker():
    return _active_invoker.get()

def current_universe():
    return _active_universe.get() or UNIVERSE_ID

def on_dev_universe():
    return current_universe() != UNIVERSE_ID

def dev_universe_note():
    """Warn on replies whenever a command isn't touching the live game."""
    if not on_dev_universe():
        return None
    return f"-# 🧪 test universe `{current_universe()}` — not the live game"

def keeps_context(method):
    """Restore the view's universe and owner around a component callback.

    A button runs in its own task long after the command returned, by which
    point the ContextVars have reset -- so without this, the buttons on a
    !dev_ card would quietly query the live universe, and any card they open
    would forget who is allowed to press it.
    """
    @functools.wraps(method)
    async def wrapper(self, *args, **kwargs):
        universe = _active_universe.set(getattr(self, "universe", None))
        invoker = _active_invoker.set(getattr(self, "owner_id", None))
        try:
            return await method(self, *args, **kwargs)
        finally:
            _active_universe.reset(universe)
            _active_invoker.reset(invoker)
    return wrapper

class OwnedView:
    """Mixin: only the person who ran the command may press the buttons."""

    def bind_owner(self):
        self.owner_id = current_invoker()

    async def interaction_check(self, interaction):
        if self.owner_id is None or interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "Only the person who ran the command can use these buttons.",
            ephemeral=True,
        )
        return False

class CardView(OwnedView, discord.ui.LayoutView):
    """A card that can navigate to others and offer a way back.

    Cards replace each other in the same message, so the card you came from
    is kept and restored rather than rebuilt -- no refetching to go back.
    """

    def __init__(self, *, parent=None, timeout=MAP_VIEW_TIMEOUT):
        super().__init__(timeout=timeout)
        self.universe = current_universe()
        self.bind_owner()
        self.message = None
        self.parent = parent

    def make_back_button(self):
        """A button returning to the card this one was opened from."""
        if self.parent is None:
            return None
        button = discord.ui.Button(
            label="Back",
            style=discord.ButtonStyle.danger,
            emoji="↩️",
        )
        button.callback = self.go_back
        return button

    def attach_back_button(self, container):
        """Add the back button on its own row, for cards without one."""
        button = self.make_back_button()
        if button is None:
            return
        row = discord.ui.ActionRow()
        row.add_item(button)
        container.add_item(row)

    @keeps_context
    async def go_back(self, interaction):
        await interaction.response.defer()
        parent = self.parent
        parent.message = interaction.message
        await interaction.edit_original_response(view=parent)

    async def on_timeout(self):
        # children are Containers on a LayoutView, so walk down to the
        # buttons. Link buttons stay enabled -- Play should always work.
        for item in self.walk_children():
            if (isinstance(item, discord.ui.Button)
                    and item.style is not discord.ButtonStyle.link):
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
_account_link_lock = asyncio.Lock()
_link_code_expirations = {}
_link_code_tasks = {}
_link_expirations_restored = False
_link_expiration_restore_task = None

async def get_session():
    """One shared session for the process, built lazily on the running loop."""
    global _session
    if _session is None or _session.closed:
        connector = aiohttp.TCPConnector(
            family=socket.AF_INET,   # apis.roblox.com has no AAAA record
            limit=10,
            ttl_dns_cache=300,
        )
        _session = aiohttp.ClientSession(
            timeout=REQUEST_TIMEOUT,
            connector=connector,
            headers={
                "User-Agent": USER_AGENT,
                # aiohttp advertises every codec it can find installed, and
                # having `zstandard` present makes it request zstd responses.
                # Pin this to what curl asks for, which is known to work.
                "Accept-Encoding": "gzip, deflate",
            },
        )
    return _session

async def request_json(url, headers=None, label="", json_body=None, method=None):
    """Fetch JSON with retries. POSTs when json_body is given, else GETs.

    Pass method to override ("PATCH", "DELETE", ...). Returns None on any
    failure, never raises.
    """
    if method is None:
        method = "POST" if json_body is not None else "GET"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            session = await get_session()
            request = session.request(method, url, headers=headers, json=json_body)
            async with request as resp:
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    print(f"{label} -> HTTP {resp.status}: {body}")
                    return None
                # Roblox doesn't always send application/json.
                return await resp.json(content_type=None)
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            print(f"{label} -> attempt {attempt}/{MAX_ATTEMPTS} "
                  f"failed: {type(e).__name__}: {e}")
            if attempt == MAX_ATTEMPTS:
                return None
            await asyncio.sleep(2 * attempt)
    return None

def decode_buffer(value, *, compressed=True):
    decoded_bytes = base64.b64decode(value)
    if not compressed:
        try:
            return json.loads(decoded_bytes)
        except Exception:
            return decoded_bytes

    dctx = zstd.ZstdDecompressor()
    try:
        decoded_bytes = dctx.decompress(decoded_bytes)
    except zstd.ZstdError:
        # Frames written without the content size in the header can't be
        # decompressed one-shot; stream them instead.
        decoded_bytes = dctx.stream_reader(io.BytesIO(decoded_bytes)).read()
    try:
        return json.loads(decoded_bytes)
    except Exception:
        return decoded_bytes

def buffer_encoding_field(value):
    """Return Roblox's raw or compressed buffer payload field."""
    if not isinstance(value, dict):
        return None
    if isinstance(value.get("zbase64"), str):
        return "zbase64"
    if isinstance(value.get("base64"), str):
        return "base64"
    return None

def is_buffer_value(value):
    return buffer_encoding_field(value) is not None

async def fetch_entry(entry_key, datastore="Daily Cup Submissions"):
    # Datastore names contain spaces ("Daily Cup Submissions"), so encode
    # both segments rather than relying on the client to fix up the URL.
    url = (
        f"https://apis.roblox.com/cloud/v2/universes/{current_universe()}/"
        f"data-stores/{quote(datastore, safe='')}/entries/{quote(str(entry_key), safe='')}"
    )
    headers = {"x-api-key": API_KEY, "Accept": "application/json"}
    data = await request_json(url, headers=headers,
                              label=f"fetch_entry({datastore}/{entry_key})")
    if not data:
        return None

    value = data.get("value")
    if not value:
        return None
    return decode_entry_value(value)

def user_restriction_url(user_id):
    return (
        f"https://apis.roblox.com/cloud/v2/universes/{current_universe()}"
        f"/user-restrictions/{quote(str(user_id), safe='')}"
    )

async def fetch_user_restriction(user_id):
    """The player's current game-join restriction, or None."""
    data = await request_json(
        user_restriction_url(user_id),
        headers={"x-api-key": API_KEY, "Accept": "application/json"},
        label=f"fetch_user_restriction({user_id})",
    )
    if not isinstance(data, dict):
        return None
    restriction = data.get("gameJoinRestriction")
    return restriction if isinstance(restriction, dict) else None

async def set_user_restriction(user_id, *, active, duration_seconds=None,
                               display_reason="", private_reason=""):
    """Ban or unban. Omitting the duration makes a ban permanent."""
    restriction = {"active": bool(active)}
    if active:
        if duration_seconds is not None:
            restriction["duration"] = f"{int(duration_seconds)}s"
        if display_reason:
            restriction["displayReason"] = display_reason[:400]
        if private_reason:
            restriction["privateReason"] = private_reason[:400]

    data = await request_json(
        user_restriction_url(user_id) + "?updateMask=gameJoinRestriction",
        headers={
            "x-api-key": API_KEY,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        label=f"set_user_restriction({user_id}, active={active})",
        json_body={"gameJoinRestriction": restriction},
        method="PATCH",
    )
    return data is not None

DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

def parse_ban_duration(text):
    """'7d' / '12h30m' / 'perm' -> seconds, or None for permanent.

    Returns ("permanent", None), ("seconds", n) or ("invalid", None).
    """
    if text is None:
        return "permanent", None
    cleaned = text.strip().lower()
    if cleaned in ("perm", "permanent", "forever", "inf", "0"):
        return "permanent", None

    matches = re.findall(r"(\d+)\s*([smhdw])", cleaned)
    if not matches or re.sub(r"\d+\s*[smhdw]\s*", "", cleaned):
        return "invalid", None

    total = sum(int(amount) * DURATION_UNITS[unit] for amount, unit in matches)
    return ("seconds", total) if total > 0 else ("invalid", None)

def format_ban_notice(restriction):
    """One line describing an active ban, or None when the player is clear."""
    if not isinstance(restriction, dict) or not restriction.get("active"):
        return None

    duration = restriction.get("duration")
    seconds = None
    if isinstance(duration, str) and duration.endswith("s"):
        try:
            seconds = int(float(duration[:-1]))
        except ValueError:
            seconds = None

    if seconds is None:
        line = "🔨 **Banned** · permanent"
    else:
        line = f"🔨 **Banned** · {format_ban_duration(seconds)}"
        # startTime plus duration gives the moment it lifts.
        start = restriction.get("startTime")
        if isinstance(start, str):
            try:
                began = datetime.fromisoformat(start.replace("Z", "+00:00"))
                ends = began + timedelta(seconds=seconds)
                line += f", ends <t:{int(ends.timestamp())}:R>"
            except ValueError:
                pass

    return line

def format_ban_duration(seconds):
    seconds = int(seconds)
    for unit, label, size in (
        ("w", "week", 604800), ("d", "day", 86400),
        ("h", "hour", 3600), ("m", "minute", 60),
    ):
        if seconds >= size and seconds % size == 0:
            count = seconds // size
            return f"{count} {label}{'s' if count != 1 else ''}"
    return f"{seconds} seconds"

def ordered_entry_url(entry_key, datastore, scope):
    return (
        f"https://apis.roblox.com/cloud/v2/universes/{current_universe()}/"
        f"ordered-data-stores/{quote(datastore, safe='')}/"
        f"scopes/{quote(scope, safe='')}/"
        f"entries/{quote(str(entry_key), safe='')}"
    )

async def delete_ordered_entry(entry_key, datastore="Data", scope="Wins"):
    """Drop one entry from an ordered datastore. True when it's gone."""
    url = ordered_entry_url(entry_key, datastore, scope)
    headers = {"x-api-key": API_KEY, "Accept": "application/json"}
    session = await get_session()
    try:
        async with session.delete(url, headers=headers) as resp:
            # 404 means it wasn't there, which is the state we wanted anyway.
            if resp.status in (200, 204, 404):
                return True
            body = (await resp.text())[:200]
            print(f"delete_ordered_entry({datastore}/{scope}/{entry_key}) "
                  f"-> HTTP {resp.status}: {body}")
            return False
    except (asyncio.TimeoutError, aiohttp.ClientError) as error:
        print(f"delete_ordered_entry({datastore}/{scope}/{entry_key}) failed: "
              f"{type(error).__name__}: {error}")
        return False

async def purge_player_from_leaderboards(user_id):
    """Remove a player from the global ordered leaderboards.

    Returns the names of the boards they were removed from. The Daily Cup
    board is a list inside a regular entry and is handled separately.
    """
    removed = []
    for scope in ("Wins", "Medals"):
        if await delete_ordered_entry(str(user_id), datastore="Data", scope=scope):
            removed.append(scope)
    return removed

def data_store_entry_url(entry_key, datastore, universe_id=None):
    return (
        f"https://apis.roblox.com/cloud/v2/universes/{universe_id or current_universe()}/"
        f"data-stores/{quote(datastore, safe='')}/entries/"
        f"{quote(str(entry_key), safe='')}"
    )

async def fetch_entry_resource(entry_key, datastore, *, universe_id=None,
                               api_key=None, fresh=False):
    """Fetch the full Open Cloud entry, including its concurrency ETag."""
    url = data_store_entry_url(entry_key, datastore, universe_id)
    headers = {"x-api-key": api_key or API_KEY, "Accept": "application/json"}
    if fresh:
        headers["Cache-Control"] = "no-cache, no-store, max-age=0"
        headers["Pragma"] = "no-cache"
    session = await get_session()
    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 404:
                return "missing", None
            if resp.status != 200:
                body = (await resp.text())[:200]
                print(
                    f"fetch_entry_resource({datastore}/{entry_key}) "
                    f"-> HTTP {resp.status}: {body}"
                )
                return "error", None
            return "ok", await resp.json(content_type=None)
    except (asyncio.TimeoutError, aiohttp.ClientError) as error:
        print(
            f"fetch_entry_resource({datastore}/{entry_key}) failed: "
            f"{type(error).__name__}: {error}"
        )
        return "error", None

async def update_entry_resource(
    entry_key, datastore, value, *, etag=None, allow_missing=False,
    universe_id=None, api_key=None
):
    """Update an existing entry without silently replacing a newer version."""
    url = data_store_entry_url(entry_key, datastore, universe_id)
    if allow_missing:
        url += "?allowMissing=true"
    headers = {
        "x-api-key": api_key or API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    payload = {"value": value}
    if etag:
        payload["etag"] = etag

    session = await get_session()
    try:
        async with session.patch(url, headers=headers, json=payload) as resp:
            if resp.status in (200, 201):
                return "ok"
            if resp.status in (409, 412):
                return "conflict"
            body = (await resp.text())[:200]
            print(
                f"update_entry_resource({datastore}/{entry_key}) "
                f"-> HTTP {resp.status}: {body}"
            )
            return "error"
    except (asyncio.TimeoutError, aiohttp.ClientError) as error:
        print(
            f"update_entry_resource({datastore}/{entry_key}) failed: "
            f"{type(error).__name__}: {error}"
        )
        return "error"

def decode_entry_value(value):
    encoding_field = buffer_encoding_field(value)
    if encoding_field is not None:
        return decode_buffer(
            value[encoding_field], compressed=encoding_field == "zbase64"
        )
    return value

def encode_entry_value(original, new_value):
    """Re-encode a value in whatever envelope the entry already used.

    Roblox may return buffers as raw `base64` or compressed `zbase64`.
    Preserve that choice so in-game buffer.tostring keeps working.
    """
    encoding_field = buffer_encoding_field(original)
    if encoding_field is not None:
        raw = json.dumps(new_value, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
        encoded = (
            zstd.ZstdCompressor().compress(raw)
            if encoding_field == "zbase64" else raw
        )
        # Keep Roblox's exact discriminator and any non-null metadata. Its
        # buffer type marker is not consistently the literal string "buffer".
        envelope = {
            key: value for key, value in original.items()
            if key not in ("base64", "zbase64") and value is not None
        }
        envelope[encoding_field] = base64.b64encode(encoded).decode("ascii")
        return envelope
    return new_value

async def update_entry_with_retry(entry_key, datastore, transform, *, attempts=4,
                                  universe_id=None, api_key=None, default=None):
    """Read-modify-write guarded by the ETag -- Open Cloud's UpdateAsync.

    transform(decoded_value) returns the new value, or None to leave the
    entry alone. Pass default to create the entry when it doesn't exist yet.
    Returns "ok", "skipped", "conflict", "missing" or "error".
    """
    for attempt in range(1, attempts + 1):
        status, resource = await fetch_entry_resource(
            entry_key, datastore, universe_id=universe_id, api_key=api_key
        )
        if status == "missing" and default is not None:
            raw_value, etag, allow_missing = default, None, True
        elif status != "ok":
            return status
        else:
            raw_value = resource.get("value")
            etag, allow_missing = resource.get("etag"), False

        new_value = transform(decode_entry_value(raw_value))
        if new_value is None:
            return "skipped"

        result = await update_entry_resource(
            entry_key, datastore,
            encode_entry_value(raw_value, new_value),
            etag=etag, allow_missing=allow_missing,
            universe_id=universe_id, api_key=api_key,
        )
        if result != "conflict":
            return result

        # The game wrote underneath us; re-read and reapply.
        print(f"update_entry_with_retry({datastore}/{entry_key}) "
              f"conflict on attempt {attempt}")
        await asyncio.sleep(0.5 * attempt)
    return "conflict"

async def delete_entry_resource(entry_key, datastore, *, universe_id=None,
                                api_key=None):
    """Delete one datastore entry. True when it's gone."""
    url = data_store_entry_url(entry_key, datastore, universe_id)
    headers = {"x-api-key": api_key or API_KEY, "Accept": "application/json"}
    session = await get_session()
    try:
        async with session.delete(url, headers=headers) as resp:
            if resp.status in (200, 204, 404):
                return True
            body = (await resp.text())[:200]
            print(f"delete_entry_resource({datastore}/{entry_key}) "
                  f"-> HTTP {resp.status}: {body}")
            return False
    except (asyncio.TimeoutError, aiohttp.ClientError) as error:
        print(f"delete_entry_resource({datastore}/{entry_key}) failed: "
              f"{type(error).__name__}: {error}")
        return False

def invalidate_maps_cache():
    _maps_cache.pop(current_universe(), None)

def decode_json_value(value):
    """Decode JSON stored in a Roblox buffer, byte string, or JSON string."""
    value = decode_entry_value(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value

def find_account_code(codes, account_id):
    account_id = str(account_id)
    for code, stored_account_id in codes.items():
        if str(stored_account_id) == account_id:
            return code
    return None

async def delete_link_code(code, account_id):
    """Delete a code only if it still maps to the expected Discord account."""
    async with _account_link_lock:
        for _ in range(MAX_ATTEMPTS):
            fetch_status, resource = await fetch_entry_resource(
                "Codes", ACCOUNT_LINK_DATASTORE, fresh=True
            )
            if fetch_status == "missing":
                return "absent"
            if fetch_status == "error":
                return "error"

            raw_codes = resource.get("value")
            codes = decode_entry_value(raw_codes)
            if not isinstance(codes, dict):
                return "absent"
            if str(codes.get(code)) != str(account_id):
                return "absent"

            del codes[code]
            result = await update_entry_resource(
                "Codes",
                ACCOUNT_LINK_DATASTORE,
                encode_entry_value(raw_codes, codes),
                etag=resource.get("etag"),
            )
            if result == "ok":
                return "deleted"
            if result != "conflict":
                return "error"

        return "error"


def link_code_expiry_record(account_id, expires_at):
    return {
        "AccountId": str(account_id),
        "ExpiresAt": int(expires_at),
    }


def parse_link_code_expiry(record, account_id):
    if not isinstance(record, dict):
        return None
    if str(record.get("AccountId")) != str(account_id):
        return None
    try:
        return int(record.get("ExpiresAt"))
    except (TypeError, ValueError):
        return None


async def save_link_code_expiry(code, account_id, expires_at):
    """Persist a timer separately so Codes remains {code: DiscordUserId}."""
    def transform(stored):
        stored = dict(stored) if isinstance(stored, dict) else {}
        stored[code] = link_code_expiry_record(account_id, expires_at)
        return stored

    return await update_entry_with_retry(
        LINK_CODE_EXPIRATIONS_KEY,
        ACCOUNT_LINK_DATASTORE,
        transform,
        default={},
    )


async def remove_link_code_expiry(code):
    def transform(stored):
        if not isinstance(stored, dict) or code not in stored:
            return None
        stored = dict(stored)
        del stored[code]
        return stored

    return await update_entry_with_retry(
        LINK_CODE_EXPIRATIONS_KEY,
        ACCOUNT_LINK_DATASTORE,
        transform,
        default={},
    )


async def expire_link_code(code, account_id, expires_at):
    try:
        delay = max(0, expires_at - time.time())
        await asyncio.sleep(delay)

        # Keep retrying temporary Open Cloud failures after expiry. The guarded
        # delete cannot remove a replacement code or another user's code.
        while True:
            try:
                result = await delete_link_code(code, account_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(
                    f"Link code {code} expiration failed: "
                    f"{type(error).__name__}: {error}"
                )
                result = "error"
            if result in ("deleted", "absent"):
                await remove_link_code_expiry(code)
                print(f"Expired account-link code {code}: {result}")
                break
            print(f"Link code {code} expiration will retry in 30 seconds")
            await asyncio.sleep(30)
    finally:
        current_task = asyncio.current_task()
        if _link_code_tasks.get(code) is current_task:
            _link_code_expirations.pop(code, None)
            _link_code_tasks.pop(code, None)


def schedule_link_code_expiry(code, account_id, expires_at):
    """Start one in-process cleanup task for a persisted expiration."""
    existing_expiry = _link_code_expirations.get(code)
    if existing_expiry is not None:
        return existing_expiry

    expires_at = int(expires_at)
    _link_code_expirations[code] = expires_at
    _link_code_tasks[code] = asyncio.create_task(
        expire_link_code(code, account_id, expires_at)
    )
    return expires_at


async def ensure_link_code_expiry(code, account_id, expires_at=None):
    """Persist and schedule a code's five-minute cleanup."""
    existing_expiry = _link_code_expirations.get(code)
    if existing_expiry is not None:
        return existing_expiry

    expires_at = int(expires_at or (time.time() + LINK_CODE_TTL))
    result = await save_link_code_expiry(code, account_id, expires_at)
    if result not in ("ok", "skipped"):
        print(
            f"Couldn't persist expiration for link code {code}: {result}; "
            "the in-process timer will still run"
        )
    return schedule_link_code_expiry(code, account_id, expires_at)


async def cancel_link_code_expiry(code):
    """Cancel and forget a code's scheduled cleanup after verification."""
    _link_code_expirations.pop(code, None)
    task = _link_code_tasks.pop(code, None)
    if task is not None and not task.done():
        task.cancel()
    await remove_link_code_expiry(code)


async def restore_link_code_expirations():
    """Restore pending five-minute timers after a process or server restart."""
    async with _account_link_lock:
        codes_status, codes_resource = await fetch_entry_resource(
            "Codes", ACCOUNT_LINK_DATASTORE, fresh=True
        )
        if codes_status == "missing":
            codes = {}
        elif codes_status == "ok":
            codes = decode_entry_value(codes_resource.get("value"))
            codes = codes if isinstance(codes, dict) else {}
        else:
            return "error"

        expiry_status, expiry_resource = await fetch_entry_resource(
            LINK_CODE_EXPIRATIONS_KEY, ACCOUNT_LINK_DATASTORE, fresh=True
        )
        if expiry_status == "ok":
            persisted = decode_entry_value(expiry_resource.get("value"))
            persisted = persisted if isinstance(persisted, dict) else {}
        elif expiry_status == "missing":
            persisted = {}
        else:
            return "error"

        # Legacy codes have no creation time, so expire them immediately. New
        # codes persist their deadline and retain the remainder across restarts.
        now = int(time.time())
        restored = {}
        timers = []
        for code, account_id in codes.items():
            expires_at = parse_link_code_expiry(
                persisted.get(code), account_id
            )
            if expires_at is None:
                expires_at = now
            restored[code] = link_code_expiry_record(account_id, expires_at)
            timers.append((code, account_id, expires_at))

        def replace_expirations(_stored):
            return restored

        save_status = await update_entry_with_retry(
            LINK_CODE_EXPIRATIONS_KEY,
            ACCOUNT_LINK_DATASTORE,
            replace_expirations,
            default={},
        )
        if save_status != "ok":
            return save_status

        for code, account_id, expires_at in timers:
            schedule_link_code_expiry(code, account_id, expires_at)
        print(f"Restored {len(timers)} account-link expiration timer(s)")
        return "ok"


async def restore_link_code_expirations_until_ready():
    """Retry restoration until Open Cloud is reachable."""
    global _link_expirations_restored
    while not _link_expirations_restored:
        try:
            status = await restore_link_code_expirations()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(
                "Account-link expiration restoration crashed: "
                f"{type(error).__name__}: {error}"
            )
            status = "error"
        if status == "ok":
            _link_expirations_restored = True
            return
        print(
            "Account-link expiration restoration failed: "
            f"{status}; retrying in 30 seconds"
        )
        await asyncio.sleep(30)

async def create_link_code(account_id, *, previous_code=None):
    """Add or reuse a code; returns (status, code, Unix expiry)."""
    async with _account_link_lock:
        for _ in range(MAX_ATTEMPTS):
            fetch_status, resource = await fetch_entry_resource(
                "Codes", ACCOUNT_LINK_DATASTORE, fresh=True
            )
            if fetch_status == "error":
                return "error", None, None

            resource = resource or {}
            raw_codes = resource.get("value")
            codes = decode_entry_value(raw_codes)
            if not isinstance(codes, dict):
                codes = {}

            active_code = find_account_code(codes, account_id)
            if previous_code is not None:
                if previous_code in codes:
                    expires_at = await ensure_link_code_expiry(
                        previous_code, account_id
                    )
                    return "pending", previous_code, expires_at
                if active_code is not None:
                    expires_at = await ensure_link_code_expiry(
                        active_code, account_id
                    )
                    return "active", active_code, expires_at
            elif active_code is not None:
                expires_at = await ensure_link_code_expiry(
                    active_code, account_id
                )
                return "active", active_code, expires_at

            code = "".join(
                secrets.choice(LINK_CODE_ALPHABET) for _ in range(LINK_CODE_LENGTH)
            )
            while code in codes:
                code = "".join(
                    secrets.choice(LINK_CODE_ALPHABET)
                    for _ in range(LINK_CODE_LENGTH)
                )

            codes[code] = str(account_id)
            result = await update_entry_resource(
                "Codes",
                ACCOUNT_LINK_DATASTORE,
                encode_entry_value(raw_codes, codes),
                etag=resource.get("etag"),
                allow_missing=fetch_status == "missing",
            )
            if result == "ok":
                expires_at = await ensure_link_code_expiry(code, account_id)
                return "created", code, expires_at
            if result != "conflict":
                return "error", None, None

        return "error", None, None

async def fetch_role_rules(guild_id):
    """The role rules for one guild, as a list."""
    rules = await fetch_entry("Rules", datastore=ROLES_DATASTORE)
    if not isinstance(rules, dict):
        return []
    guild_rules = rules.get(str(guild_id))
    return guild_rules if isinstance(guild_rules, list) else []

async def save_role_rules(guild_id, mutate):
    """Read-modify-write one guild's rules. mutate(list) returns the new list."""
    def transform(stored):
        stored = stored if isinstance(stored, dict) else {}
        current = stored.get(str(guild_id))
        current = current if isinstance(current, list) else []
        updated = mutate(list(current))
        if updated is None:
            return None
        stored[str(guild_id)] = updated
        return stored

    return await update_entry_with_retry(
        "Rules", ROLES_DATASTORE, transform, default={},
    )

async def map_finishers(map_id, cache):
    """Roblox IDs on a map's leaderboard, fetched once per sync."""
    if map_id in cache:
        return cache[map_id]
    board = await fetch_entry(
        MAP_LEADERBOARD_KEY.format(id=map_id), datastore="Leaderboards"
    )
    finishers = set()
    if isinstance(board, list):
        for row in board:
            if isinstance(row, dict) and row.get("UserId") is not None:
                try:
                    finishers.add(int(row["UserId"]))
                except (TypeError, ValueError):
                    continue
    cache[map_id] = finishers
    return finishers

def rule_role_id(rule):
    """Return an exact Discord role snowflake, rejecting damaged floats."""
    raw_role_id = rule.get("role") if isinstance(rule, dict) else None
    if isinstance(raw_role_id, bool) or isinstance(raw_role_id, float):
        # Discord snowflakes exceed JSON's safe integer range. Once Roblox
        # returns one as a float, its original digits cannot be recovered.
        return None
    if isinstance(raw_role_id, int):
        return raw_role_id if raw_role_id > 0 else None
    if isinstance(raw_role_id, str) and raw_role_id.isascii() and raw_role_id.isdigit():
        role_id = int(raw_role_id)
        return role_id if role_id > 0 else None
    return None

async def qualifying_roles(rules, roblox_id, *, map_cache):
    """Which of the rules' roles this player currently earns."""
    badge_ids = [r["value"] for r in rules if r.get("type") == "badge"]
    owned_badges = (
        await fetch_owned_badge_ids(roblox_id, badge_ids) if badge_ids else set()
    )

    earned = set()
    for rule in rules:
        kind, value = rule.get("type"), rule.get("value")
        role_id = rule_role_id(rule)
        if role_id is None:
            continue
        if kind == "badge" and value in owned_badges:
            earned.add(role_id)
        elif kind == "map" and roblox_id in await map_finishers(value, map_cache):
            earned.add(role_id)
    return earned

def manageable_role(guild, role_id):
    """The role, when the bot is actually allowed to grant or take it."""
    role = guild.get_role(role_id)
    if role is None or role.managed or role.is_default():
        return None
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        return None
    return role if role < me.top_role else None

async def sync_member_roles(member, roblox_id, rules, *, map_cache):
    """Bring one member's roles in line with the rules.

    Only roles named by a rule are ever touched, so unrelated roles are
    left alone. Returns (added, removed, blocked) as role lists.
    """
    earned = await qualifying_roles(rules, roblox_id, map_cache=map_cache)
    governed = {
        role_id for rule in rules
        if (role_id := rule_role_id(rule)) is not None
    }
    held = {role.id for role in member.roles}

    blocked = []
    add, remove = [], []
    for role_id in governed:
        role = manageable_role(member.guild, role_id)
        if role is None:
            # Missing, or above the bot in the hierarchy.
            if (role_id in earned) != (role_id in held):
                blocked.append(role_id)
            continue
        if role_id in earned and role_id not in held:
            add.append(role)
        elif role_id not in earned and role_id in held:
            remove.append(role)

    if add:
        await member.add_roles(*add, reason="RazzBot role sync")
    if remove:
        await member.remove_roles(*remove, reason="RazzBot role sync")
    return add, remove, blocked

async def fetch_linked_user_id(account_id):
    _, user_id = await fetch_linked_account(account_id)
    return user_id

def normalize_discord_id(value):
    """Normalize datastore keys to the digits in a Discord snowflake."""
    return "".join(
        character for character in str(value)
        if character.isascii() and character.isdigit()
    )

async def fetch_linked_account(account_id, *, attempts=1):
    """Return (status, Roblox user ID) for a Discord account link."""
    expected_id = normalize_discord_id(account_id)
    last_status = "error"

    for attempt in range(attempts):
        fetch_status, resource = await fetch_entry_resource(
            "Linked", ACCOUNT_LINK_DATASTORE, fresh=True
        )
        if fetch_status == "ok":
            try:
                linked = decode_json_value(resource.get("value"))
            except Exception as error:
                print(
                    "AccountLinking/Linked decode failed: "
                    f"{type(error).__name__}: {error}"
                )
                last_status = "error"
            else:
                if not isinstance(linked, dict):
                    print(
                        "AccountLinking/Linked has unexpected type: "
                        f"{type(linked).__name__}"
                    )
                    last_status = "error"
                else:
                    last_status = "missing"
                    for stored_account_id, stored_user_id in linked.items():
                        if normalize_discord_id(stored_account_id) != expected_id:
                            continue
                        try:
                            return "linked", int(stored_user_id)
                        except (TypeError, ValueError):
                            print(
                                "AccountLinking/Linked contains an invalid Roblox "
                                f"user ID for Discord account {expected_id}"
                            )
                            return "error", None

                    visible_keys = [
                        normalize_discord_id(key) for key in linked.keys()
                    ]
                    print(
                        f"AccountLinking/Linked has no key for {expected_id}; "
                        f"stored Discord IDs: {visible_keys}"
                    )
        elif fetch_status == "missing":
            last_status = "missing"
        else:
            last_status = "error"

        if attempt + 1 < attempts:
            await asyncio.sleep(1)

    return last_status, None

_owned_badges_cache = {}

async def fetch_universe_badges():
    """Every badge the game defines. Public, no auth needed."""
    badges, cursor = [], None
    while True:
        url = (f"https://badges.roblox.com/v1/universes/{current_universe()}/badges"
               f"?limit=100&sortOrder=Asc")
        if cursor:
            url += f"&cursor={quote(cursor)}"
        data = await request_json(url, label="fetch_universe_badges")
        if not data:
            break
        badges.extend(data.get("data") or [])
        cursor = data.get("nextPageCursor")
        if not cursor:
            break

    return badges

async def _badge_is_owned(user_id, badge_id, semaphore):
    async with semaphore:
        url = (f"https://inventory.roblox.com/v1/users/{user_id}"
               f"/items/Badge/{badge_id}/is-owned")
        # The body is a bare `true` or `false`, which is still valid JSON.
        owned = await request_json(url, label=f"badge_is_owned({badge_id})")
        return badge_id, owned is True

async def fetch_owned_badge_ids(user_id, badge_ids):
    """Which of badge_ids the player owns. One request per badge, so the
    result is cached briefly per player."""
    now = time.monotonic()
    cache_key = (current_universe(), user_id)
    cached = _owned_badges_cache.get(cache_key)
    if cached and now - cached[0] < OWNED_BADGES_CACHE_TTL:
        return cached[1]

    semaphore = asyncio.Semaphore(BADGE_CONCURRENCY)
    results = await asyncio.gather(*(
        _badge_is_owned(user_id, badge_id, semaphore) for badge_id in badge_ids
    ))
    owned = {badge_id for badge_id, has in results if has}
    _owned_badges_cache[cache_key] = (now, owned)
    return owned

async def fetch_badge_icons(badge_ids):
    """Badge icon URLs keyed by badge ID. One batched request."""
    ids = [str(b) for b in badge_ids if b is not None]
    if not ids:
        return {}

    url = (
        "https://thumbnails.roblox.com/v1/badges/icons"
        f"?badgeIds={','.join(ids)}&size={BADGE_ICON_SIZE}"
        "&format=Png&isCircular=false"
    )
    data = await request_json(url, label="fetch_badge_icons")
    if not data:
        return {}

    icons = {}
    for item in data.get("data", []):
        if item.get("state") == "Completed" and item.get("imageUrl"):
            icons[item.get("targetId")] = item["imageUrl"]
    return icons

async def fetch_ordered_entry(entry_key, datastore="Data", scope="Wins"):
    """Read one entry from an ordered datastore, which has its own endpoint.

    Returns the integer value, or None. int64 fields often arrive as strings
    in JSON, so the value is coerced.
    """
    url = (
        f"https://apis.roblox.com/cloud/v2/universes/{current_universe()}/"
        f"ordered-data-stores/{quote(datastore, safe='')}/"
        f"scopes/{quote(scope, safe='')}/"
        f"entries/{quote(str(entry_key), safe='')}"
    )
    headers = {"x-api-key": API_KEY, "Accept": "application/json"}
    data = await request_json(
        url, headers=headers,
        label=f"fetch_ordered_entry({datastore}/{scope}/{entry_key})",
    )
    if not data:
        return None

    value = data.get("value")
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return value if isinstance(value, (int, float)) else None

async def fetch_ordered_leaderboard(datastore="Data", scope="Wins", limit=10):
    """Return the highest-valued entries from an ordered datastore."""
    url = (
        f"https://apis.roblox.com/cloud/v2/universes/{current_universe()}/"
        f"ordered-data-stores/{quote(datastore, safe='')}/"
        f"scopes/{quote(scope, safe='')}/entries"
        f"?maxPageSize={limit}&orderBy={quote('value desc', safe='')}"
    )
    headers = {"x-api-key": API_KEY, "Accept": "application/json"}
    data = await request_json(
        url, headers=headers,
        label=f"fetch_ordered_leaderboard({datastore}/{scope})",
    )
    if not data:
        return None

    entries = data.get("orderedDataStoreEntries")
    if not isinstance(entries, list):
        return None

    leaderboard = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue

        entry_id = entry.get("id")
        try:
            user_id = int(entry_id)
        except (TypeError, ValueError):
            user_id = None

        value = entry.get("value")
        if isinstance(value, str):
            try:
                value = int(value)
            except ValueError:
                continue
        if not isinstance(value, (int, float)):
            continue

        leaderboard.append({
            "UserId": user_id,
            "Name": str(entry_id) if user_id is None and entry_id is not None else None,
            "Value": value,
        })
    return leaderboard

def cup_day_today():
    """The cup's current day. Its rollover is 9 hours behind UTC."""
    return (datetime.now(timezone.utc) - timedelta(hours=9)).date()

def get_todays_date():
    return cup_day_today().strftime("%d/%m/%Y")

def parse_cup_date(text):
    try:
        return datetime.strptime(text.strip(), "%d/%m/%Y").date()
    except (ValueError, AttributeError):
        return None

async def resolve_current_cup(todays_map):
    """Today's cup index and map ID, allowing for a stale TodaysMap.

    The entry is only written when a player joins, so after the 09:00 UTC
    rollover it can still hold yesterday's cup. When its Date isn't today's
    cup day, the live cup is the next one in the queue -- the game advances
    a single cup per join regardless of how long it sat idle.
    """
    index = todays_map.get("Index") if isinstance(todays_map, dict) else None
    map_id = todays_map.get("Id") if isinstance(todays_map, dict) else None
    if not isinstance(index, int):
        return None, None

    raw_date = todays_map.get("Date")
    stamp = parse_cup_date(raw_date)
    if stamp is None:
        print(f"TodaysMap Date unreadable ({raw_date!r}); "
              f"using stored Daily Cup #{index}")
        return index, map_id

    if stamp == cup_day_today():
        return index, map_id

    next_index = index + 1
    print(f"TodaysMap is stale (Date {stamp}, today {cup_day_today()}); "
          f"advancing to Daily Cup #{next_index}")
    return next_index, await get_cup_map_id(next_index, todays_map)

async def get_cup_map_id(index, todays_map):
    """Resolve a cup index to its map, anchored to the current rotation."""
    current_index = todays_map.get("Index") if isinstance(todays_map, dict) else None
    current_id = todays_map.get("Id") if isinstance(todays_map, dict) else None
    if index == current_index and current_id is not None:
        return current_id

    submissions = await fetch_entry("Submissions")
    if not isinstance(submissions, list):
        return None

    accepted = [
        submission for submission in submissions
        if isinstance(submission, dict)
        and submission.get("Status") == "Accepted"
        and submission.get("Id") is not None
    ]
    if not accepted:
        return None

    accepted.sort(key=lambda submission: submission.get("Timestamp", 0))
    map_ids = [submission["Id"] for submission in accepted]

    if isinstance(current_index, int) and current_id in map_ids:
        current_position = map_ids.index(current_id)
        return map_ids[(current_position + index - current_index) % len(map_ids)]

    return map_ids[index % len(map_ids)]

def format_time(time_value):
    minutes = (time_value / 60) % 60
    seconds = time_value % 60
    milliseconds = (seconds * 1000) % 1000

    return f"{int(minutes):02d}:{int(seconds):02d}.{int(milliseconds):03d}"

def country_code_to_emoji(code: str) -> str:
    if not isinstance(code, str):
        return ""
    code = code.strip().upper()
    if len(code) != 2 or not code.isalpha():
        return ""
    return ''.join(chr(127397 + ord(c)) for c in code)

def dig(data, *keys, default=None):
    """Walk nested dicts, returning default the moment anything is missing."""
    for key in keys:
        if not isinstance(data, dict):
            return default
        data = data.get(key)
        if data is None:
            return default
    return data

def format_number(value):
    """Trim the .0 off whole floats so 21167.5 and 8 both read naturally."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}" if isinstance(value, (int, float)) else "0"

def get_medal_emoji(pos):
    if pos == 0:
        return "<:DiamondMedal:1466201150314385471>"
    elif pos == 1:
        return "<:GoldMedal:1466201173877981449>"
    elif pos == 2:
        return "<:SilverMedal:1466201197840044065>"
    elif pos == 3:
        return "<:BronzeMedal:1466201227997089873>"
    else:
        return ""

def get_cup_medal_emoji(rank, total):
    """Match the game's 1-based GetMedal(Rank, Total) calculation."""
    if total <= 0:
        return ""

    diamond_slots = 1
    gold_slots = 1
    silver_slots = 1
    bronze_slots = 1

    remaining = total - (
        diamond_slots + gold_slots + silver_slots + bronze_slots
    )
    if remaining > 0:
        extra_diamond = (total * 5 // 100) - diamond_slots
        extra_gold = (total * 25 // 100) - gold_slots
        extra_silver = (total * 50 // 100) - silver_slots
        extra_bronze = (total * 90 // 100) - bronze_slots

        diamond_slots = max(diamond_slots, extra_diamond)
        gold_slots = max(gold_slots, extra_gold)
        silver_slots = max(silver_slots, extra_silver)
        bronze_slots = max(bronze_slots, extra_bronze)

    cumulative = diamond_slots
    if rank <= cumulative:
        return get_medal_emoji(0)

    cumulative += gold_slots
    if rank <= cumulative:
        return get_medal_emoji(1)

    cumulative += silver_slots
    if rank <= cumulative:
        return get_medal_emoji(2)

    cumulative += bronze_slots
    if rank <= cumulative:
        return get_medal_emoji(3)

    return ""

async def fetch_user_id(username):
    """Resolve a Roblox username to (id, canonical_name), or (None, None).

    The lookup is a POST; Roblox retired the GET equivalent.
    """
    data = await request_json(
        "https://users.roblox.com/v1/usernames/users",
        label=f"fetch_user_id({username})",
        json_body={"usernames": [username], "excludeBannedUsers": False},
    )
    if not data:
        return None, None
    users = data.get("data") or []
    if not users:
        return None, None
    # Roblox matches case-insensitively; take the name it considers canonical.
    return users[0].get("id"), users[0].get("name")

async def fetch_usernames(user_ids):
    """Resolve many user IDs to names in one request per 100."""
    unique = [uid for uid in dict.fromkeys(user_ids) if uid is not None]
    if not unique:
        return {}

    names = {}
    for start in range(0, len(unique), 100):
        chunk = unique[start:start + 100]
        data = await request_json(
            "https://users.roblox.com/v1/users",
            label=f"fetch_usernames({len(chunk)})",
            json_body={"userIds": chunk, "excludeBannedUsers": False},
        )
        if not data:
            continue
        for user in data.get("data") or []:
            if user.get("id") is not None and user.get("name"):
                names[user["id"]] = user["name"]
    return names

async def fetch_username(user_id):
    url = f"https://users.roblox.com/v1/users/{user_id}"
    data = await request_json(url, label=f"fetch_username({user_id})")
    return data.get("name") if data else None

async def resolve_command_user(ctx, username):
    """Resolve an explicit username or the caller's linked Roblox account."""
    if username:
        user_id, canonical = await fetch_user_id(username)
        if not user_id:
            await ctx.send(f"No Roblox user named `{username}`.")
            return None, None
        return user_id, canonical

    user_id = await fetch_linked_user_id(ctx.author.id)
    if user_id is None:
        await ctx.send(
            "You don't have a linked Roblox account. Use `/link` or provide "
            "a username."
        )
        return None, None

    canonical = await fetch_username(user_id)
    if canonical is None:
        await ctx.send("Your linked Roblox account could not be found.")
        return None, None
    return user_id, canonical

def format_position(pos):
    return f"`{f'#{pos + 1}':>3}`"

def resolve_medal(pos, total):
    """Resolve a cup medal using the full participant count."""
    return get_cup_medal_emoji(pos + 1, total)

async def fetch_headshots(user_ids):
    """Roblox avatar headshot URLs, keyed by user ID. One batched request."""
    ids = [str(u) for u in user_ids if u is not None]
    if not ids:
        return {}

    url = (
        "https://thumbnails.roblox.com/v1/users/avatar-headshot"
        f"?userIds={','.join(ids)}&size={HEADSHOT_SIZE}&format=Png&isCircular=true"
    )
    data = await request_json(url, label="fetch_headshots")
    if not data:
        return {}

    headshots = {}
    for item in data.get("data", []):
        if item.get("state") == "Completed" and item.get("imageUrl"):
            headshots[item.get("targetId")] = item["imageUrl"]
    return headshots

async def collect_leaderboard_rows(
    leaderboard, limit, *, value_formatter=format_time
):
    """Resolve a leaderboard into plain row dicts, ready for either renderer."""
    rows = []
    total = len(leaderboard)
    for pos in range(min(len(leaderboard), limit)):
        entry = leaderboard[pos]
        if not isinstance(entry, dict):
            continue

        user_id = entry.get("UserId")
        name = entry.get("Name")
        if not name and user_id is not None:
            name = await fetch_username(user_id)
        if not name:
            # A failed username lookup shouldn't drop the whole row.
            name = f"User {user_id}" if user_id is not None else "Unknown"

        country = entry.get("Country")
        country = country.strip().upper()[:2] if isinstance(country, str) else ""

        value = entry.get("Value")
        rows.append({
            "pos": pos,
            "user_id": user_id,
            "name": name,
            "country": country,
            "value": value_formatter(value) if isinstance(value, (int, float)) else "--",
            "medal": resolve_medal(pos, total),
        })
    return rows

def render_leaderboard_table(
    rows, *, show_country=True, show_medals=False, value_name="Time"
):
    """Monospace table. Columns align because a code block is fixed width.

    Custom emoji do not render inside code blocks, so ranks are numbers and
    countries are their two letter codes.
    """
    name_w = min(max(len(r["name"]) for r in rows), MAX_NAME_WIDTH)
    name_w = max(name_w, len("PLAYER"))

    header = f"{'#':>2}  "
    if show_country:
        header += f"{'CC':<2}  "
    header += f"{'PLAYER':<{name_w}}  "
    if show_medals:
        header += "   "
    value_width = max(len(value_name), max(len(r["value"]) for r in rows))
    header += f"{value_name.upper():>{value_width}}"

    lines = [header, "-" * len(header)]
    for r in rows:
        name = r["name"]
        if len(name) > name_w:
            name = name[:name_w - 1] + "."
        line = f"{r['pos'] + 1:>2}  "
        if show_country:
            line += f"{r['country'] or '--':<2}  "
        line += f"{name:<{name_w}}  "
        if show_medals:
            # An emoji renders about two cells wide but counts as one
            # character, so pad it to three cells with a single space.
            medal = UNICODE_MEDALS.get(r["pos"], "")
            line += f"{medal} " if medal else "   "
        line += f"{r['value']:>{value_width}}"
        lines.append(line)

    return "```\n" + "\n".join(lines) + "\n```"

def render_leaderboard_fields(
    rows, *, show_country=True, show_medals=True, value_name="Time"
):
    """Columns as side by side inline fields.

    Each field is its own column, so rows line up without a code block --
    which means custom emoji still render.
    """
    players, times = [], []
    for r in rows:
        parts = [format_position(r["pos"])]
        if show_country:
            flag = country_code_to_emoji(r["country"])
            if flag:
                parts.append(flag)
        parts.append(f"**{r['name']}**")
        players.append(" ".join(parts))

        medal = r["medal"] if show_medals else ""
        times.append(f"{medal} `{r['value']}`" if medal else f"`{r['value']}`")

    return [
        ("Player", "\n".join(players), True),
        (value_name, "\n".join(times), True),
    ]

def render_leaderboard_list(
    rows, *, show_country=True, show_medals=True, value_name="Time"
):
    """Proportional rows that keep the custom medal emoji and flag emoji."""
    lines = []
    for r in rows:
        parts = [format_position(r["pos"])]
        if show_country:
            flag = country_code_to_emoji(r["country"])
            if flag:
                parts.append(flag)
        parts.append(f"**{r['name']}**")

        medal = r["medal"] if show_medals else ""
        tail = f"{medal} `{r['value']}`" if medal else f"`{r['value']}`"
        lines.append(f"{' '.join(parts)} - {tail}")
    return "\n".join(lines)

# Keyed by universe: a !dev_ command must never poison the live cache.
_maps_cache = {}

async def get_community_maps():
    """Community maps indexed by Id, cached for MAPS_CACHE_TTL seconds.

    On a failed refresh the previous index is kept and returned, so a blip
    upstream doesn't take the command down.
    """
    universe = current_universe()
    now = time.monotonic()
    entry = _maps_cache.get(universe)
    cached = entry["by_id"] if entry else None
    if cached and now - entry["at"] < MAPS_CACHE_TTL:
        return cached

    entries = await fetch_entry("Ids", datastore="Community Maps")
    if not entries:
        return cached

    by_id = {}
    for e in entries:
        if isinstance(e, dict) and e.get("Id") is not None:
            by_id[e["Id"]] = {k: e.get(k) for k in MAP_FIELDS}

    _maps_cache[universe] = {"by_id": by_id, "at": now}
    print(f"community maps indexed: {len(by_id)} entries (universe {universe})")
    return by_id

class BadgesCardView(CardView):
    pass

class MapsCardView(CardView):
    pass

class ProfileView(CardView):
    """Components V2 profile card built from a Main_Data entry."""

    def __init__(self, entry, *, user_id, username, headshot=None, wins=None,
                 restriction=None, parent=None, timeout=MAP_VIEW_TIMEOUT):
        super().__init__(parent=parent, timeout=timeout)
        self.user_id = user_id
        self.username = username

        data = entry.get("Data") if isinstance(entry, dict) else None
        data = data if isinstance(data, dict) else {}
        meta = entry.get("MetaData") if isinstance(entry, dict) else None
        meta = meta if isinstance(meta, dict) else {}

        tag = dig(data, "Tags", "Equipped")
        skin = dig(data, "Skins", "Equipped")
        subtitle = "  ·  ".join(p for p in (
            f"🏷️ {tag}" if tag else "",
            f"🎨 {skin}" if skin else "",
        ) if p)

        heading = f"## {username}"
        if subtitle:
            heading += f"\n-# {subtitle}"

        stars = format_number(data.get("Stars") or 0)
        all_time = format_number(dig(data, "Stats", "AllTimeStars", default=0))
        currency = f"⭐  **{stars}** Stars\n-# {all_time} earned all time"

        # Medals are stored as lists of map IDs, so the count is the length.
        medal_counts = [
            (get_medal_emoji(i), len(dig(data, "Medals", tier, default=[]) or []))
            for i, tier in enumerate(("Diamond", "Gold", "Silver", "Bronze"))
        ]
        medals = "  ·  ".join(f"{emoji} **{count}**" for emoji, count in medal_counts)

        ban_notice = format_ban_notice(restriction)

        container = discord.ui.Container(accent_colour=PROFILE_COLOR)
        # A Section takes the height of its thumbnail, so the heading block
        # carries enough text to fill it -- otherwise the leftover space shows
        # as a gap above the buttons. Three text displays is the maximum.
        if headshot:
            children = [discord.ui.TextDisplay(heading)]
            # A Section allows three text displays, so the ban notice takes
            # the medals' slot and the medals move below it.
            if ban_notice:
                children.append(discord.ui.TextDisplay(ban_notice))
            children.append(discord.ui.TextDisplay(medals))
            container.add_item(discord.ui.Section(
                *children, accessory=discord.ui.Thumbnail(media=headshot)
            ))
        else:
            blocks = [heading]
            if ban_notice:
                blocks.append(ban_notice)
            blocks.append(medals)
            container.add_item(discord.ui.TextDisplay("\n\n".join(blocks)))

        # Buttons sit directly under the block above.
        row = discord.ui.ActionRow()
        maps_button = discord.ui.Button(
            label="Maps",
            style=discord.ButtonStyle.secondary,
            emoji="🗺️",
        )
        maps_button.callback = self.show_created_maps
        row.add_item(maps_button)

        badges_button = discord.ui.Button(
            label="Badges",
            style=discord.ButtonStyle.secondary,
            emoji="🎖️",
        )
        badges_button.callback = self.show_badges
        row.add_item(badges_button)

        # When this profile was opened from another card, offer the way back
        # alongside its own actions rather than on a separate row.
        back = self.make_back_button()
        if back is not None:
            row.add_item(back)

        container.add_item(row)

        container.add_item(discord.ui.Separator())

        container.add_item(discord.ui.TextDisplay(currency))

        container.add_item(discord.ui.Separator(
            visible=False, spacing=discord.SeparatorSpacing.small
        ))

        stats = data.get("Stats") if isinstance(data.get("Stats"), dict) else {}
        streak = data.get("LoginStreak") or 0
        best_streak = stats.get("HighestLoginStreak") or 0
        # Wins come from an ordered datastore, so they may be unavailable
        # even when the rest of the profile loaded.
        win_streak = f"⚡  win streak **{format_number(stats.get('WinStreak') or 0)}**"
        if wins is not None:
            wins_line = f"👑  **{format_number(wins)}** wins  ·  {win_streak}"
        else:
            wins_line = win_streak

        lines = [
            f"🎮  **{format_number(stats.get('GamesPlayed') or 0)}** games played",
            wins_line,
            f"🏁  **{format_number(stats.get('FlagsReached') or 0)}** flags reached",
            f"⚔️  **{format_number(stats.get('Kills') or 0)}** kills"
            f"  ·  ☠️  **{format_number(stats.get('Deaths') or 0)}** deaths",
            f"🏔️  furthest round **{format_number(stats.get('FurthestRound') or 0)}**",
            f"🔥  **{streak}** day login streak  ·  best **{best_streak}**",
        ]
        container.add_item(discord.ui.TextDisplay("\n".join(lines)))

        container.add_item(discord.ui.Separator(
            visible=False, spacing=discord.SeparatorSpacing.small
        ))

        footer = [f"ID {user_id}"]
        created = meta.get("ProfileCreateTime")
        if isinstance(created, (int, float)):
            footer.append(f"joined <t:{int(created)}:D>")
        last_login = data.get("LastLogin")
        if isinstance(last_login, (int, float)):
            footer.append(f"last seen <t:{int(last_login)}:R>")
        container.add_item(discord.ui.TextDisplay("-# " + "  ·  ".join(footer)))

        self.add_item(container)

    @keeps_context
    async def show_badges(self, interaction):
        # thinking=True would post a new message and
        # edit_original_response would then edit that, not the card.
        await interaction.response.defer()

        view = await build_badges_view(self.user_id, self.username, parent=self)
        if view is None:
            await interaction.followup.send(
                "Couldn't fetch the game's badge list.", ephemeral=True
            )
            return

        view.message = interaction.message
        await interaction.edit_original_response(view=view)

    @keeps_context
    async def show_created_maps(self, interaction):
        # thinking=True would post a new message and
        # edit_original_response would then edit that, not the card.
        await interaction.response.defer()

        view = await build_created_maps_view(self.user_id, self.username, parent=self)
        if view is None:
            await interaction.followup.send(
                f"**{self.username}** has no public maps...", ephemeral=True
            )
            return

        view.message = interaction.message
        await interaction.edit_original_response(view=view)

async def build_badges_view(user_id, username, parent=None):
    """Every game badge, marked owned or not for this player."""
    badges = await fetch_universe_badges()
    if not badges:
        return None

    owned_ids = await fetch_owned_badge_ids(user_id, [b["id"] for b in badges])
    owned = [b for b in badges if b["id"] in owned_ids]

    view = BadgesCardView(parent=parent)
    container = discord.ui.Container(accent_colour=PROFILE_COLOR)
    container.add_item(discord.ui.TextDisplay(
        f"## 🎖️ Badges — {username}\n-# {len(owned)} of {len(badges)} earned"
    ))

    dev_note = dev_universe_note()

    if not owned:
        container.add_item(discord.ui.TextDisplay("-# No badges earned yet."))
        if dev_note:
            container.add_item(discord.ui.TextDisplay(dev_note))
        view.add_item(container)
        return view

    container.add_item(discord.ui.Separator())

    def badge_name(badge):
        return badge.get("displayName") or badge.get("name") or "Unnamed"

    shown = owned[:MAX_BADGE_GALLERIES * BADGES_PER_LARGE_GALLERY]
    icons = await fetch_badge_icons([b["id"] for b in shown])
    galleries = 0
    start = 0
    while start < len(shown):
        remaining = len(shown) - start
        gallery_size = (
            BADGES_PER_SMALL_GALLERY
            if remaining <= BADGES_PER_SMALL_GALLERY
            else BADGES_PER_LARGE_GALLERY
        )
        batch = shown[start:start + gallery_size]
        start += len(batch)

        gallery = discord.ui.MediaGallery()
        for badge in batch:
            icon = icons.get(badge["id"])
            if icon:
                gallery.add_item(media=icon, description=badge_name(badge))
        if gallery.items:
            # Pad to the selected grid size so partial galleries keep their
            # proportions even when a badge icon could not be resolved.
            while len(gallery.items) < gallery_size:
                gallery.add_item(media=EMPTY_IMAGE_URL, description="")
            container.add_item(gallery)
            galleries += 1

    if len(owned) > len(shown):
        container.add_item(discord.ui.TextDisplay(
            f"-# and {len(owned) - len(shown)} more"
        ))

    # No icon resolved for anything: fall back to naming them.
    if not galleries:
        container.add_item(discord.ui.TextDisplay(
            "\n".join(f"✅ **{badge_name(b)}**" for b in owned)
        ))

    if dev_note:
        container.add_item(discord.ui.TextDisplay(dev_note))

    view.attach_back_button(container)
    view.add_item(container)
    return view

def search_public_maps(maps_by_id, query, limit=50):
    """Public maps whose name matches, best match first.

    Only Privacy == "Public" qualifies, which leaves out both Private and
    Unlisted maps.
    """
    needle = query.strip().lower()
    if not needle:
        return []

    scored = []
    for entry in maps_by_id.values():
        if entry.get("Privacy") != "Public":
            continue
        name = (entry.get("Name") or "").lower()
        if name == needle:
            rank = 0
        elif name.startswith(needle):
            rank = 1
        elif needle in name:
            rank = 2
        else:
            continue
        scored.append((rank, -(entry.get("Plays") or 0), entry))

    # key= avoids falling through to comparing the dicts on a tie.
    scored.sort(key=lambda item: (item[0], item[1]))
    return [entry for _, _, entry in scored[:limit]]

class MapListView(CardView):
    """A paginated list of maps, shared by search results and a player's maps."""

    def __init__(self, entries, *, title, subtitle, creators=None, parent=None):
        super().__init__(parent=parent)
        self.entries = entries
        self.title = title
        self.subtitle = subtitle
        self.creators = creators or {}
        self.page = 0
        self.render()

    @property
    def page_count(self):
        return max(1, -(-len(self.entries) // MAPS_PER_PAGE))

    def describe(self, entry):
        name = entry.get("Name") or "Unnamed Map"
        star = " 🌟" if entry.get("Featured") else ""
        creator_id = entry.get("Creator")
        creator = self.creators.get(creator_id)
        by = f"  ·  👤 {creator}" if creator else (
            f"  ·  👤 User {creator_id}" if creator_id else "")
        return (
            f"`#{entry.get('Id')}` **{name}**{star}\n"
            f"-# ▶️ {format_number(entry.get('Plays') or 0)} plays"
            f"  ·  ⭐ {format_number(entry.get('Favorites') or 0)}{by}"
        )

    def render(self):
        self.clear_items()
        container = discord.ui.Container(accent_colour=MAP_COLOR)

        heading = f"## {self.title}\n-# {self.subtitle}"
        if self.page_count > 1:
            heading += f" · page {self.page + 1}/{self.page_count}"
        container.add_item(discord.ui.TextDisplay(heading))
        container.add_item(discord.ui.Separator())

        start = self.page * MAPS_PER_PAGE
        page_entries = self.entries[start:start + MAPS_PER_PAGE]
        container.add_item(discord.ui.TextDisplay(
            "\n".join(self.describe(entry) for entry in page_entries)
        ))

        note = dev_universe_note()
        if note:
            container.add_item(discord.ui.TextDisplay(note))

        row = discord.ui.ActionRow()
        if self.page_count > 1:
            previous = discord.ui.Button(
                label="Previous", style=discord.ButtonStyle.secondary, emoji="◀️",
                disabled=self.page == 0,
            )
            previous.callback = self.previous_page
            row.add_item(previous)

            following = discord.ui.Button(
                label="Next", style=discord.ButtonStyle.secondary, emoji="▶️",
                disabled=self.page >= self.page_count - 1,
            )
            following.callback = self.next_page
            row.add_item(following)

        back = self.make_back_button()
        if back is not None:
            row.add_item(back)
        if row.children:
            container.add_item(row)

        self.add_item(container)

    async def show_page(self, interaction, page):
        self.page = max(0, min(page, self.page_count - 1))
        self.render()
        await interaction.response.edit_message(view=self)

    @keeps_context
    async def previous_page(self, interaction):
        await self.show_page(interaction, self.page - 1)

    @keeps_context
    async def next_page(self, interaction):
        await self.show_page(interaction, self.page + 1)

async def build_map_search_view(query, matches, parent=None):
    """A pick-list of maps matching a name search."""
    creators = await fetch_usernames([m.get("Creator") for m in matches])
    return MapListView(
        matches,
        title=f"🔎 Maps matching “{query}”",
        subtitle=(f"{len(matches)} result{'s' if len(matches) != 1 else ''}"
                  f" · use `/map <id>` to open one"),
        creators=creators,
        parent=parent,
    )

async def build_created_maps_view(user_id, username, parent=None):
    """List the community maps a player has created, most played first."""
    maps_by_id = await get_community_maps()
    if not maps_by_id:
        return None

    owned = [
        m for m in maps_by_id.values()
        if m.get("Creator") == user_id and m.get("Privacy") == "Public"
    ]
    if not owned:
        return None
    owned.sort(key=lambda m: m.get("Plays") or 0, reverse=True)

    creators = await fetch_usernames([user_id])
    return MapListView(
        owned,
        title=f"🗺️ Maps by {username}",
        subtitle=f"{len(owned)} public map{'s' if len(owned) != 1 else ''}",
        creators=creators,
        parent=parent,
    )

async def build_profile_view(user_id, username, parent=None):
    """Assemble a profile card, or None when the player has no saved data."""
    entry = await fetch_entry(str(user_id), datastore="Main_Data")
    if not entry:
        return None

    headshot_map, wins, restriction = await asyncio.gather(
        fetch_headshots([user_id]),
        fetch_ordered_entry(str(user_id), datastore="Data", scope="Wins"),
        fetch_user_restriction(user_id),
    )
    headshot = headshot_map.get(user_id)
    return ProfileView(
        entry, user_id=user_id, username=username,
        headshot=headshot, wins=wins, restriction=restriction, parent=parent,
    )

def build_map_container(entry, *, headshot=None, creator_text="Unknown",
                        accent_colour=MAP_COLOR, banner=None):
    """The body of a map card, without any action buttons.

    Shared by the normal map card and the delete confirmation, so the two
    can't drift apart.
    """
    map_id = entry.get("Id")
    name = entry.get("Name") or "Unnamed Map"
    if entry.get("Featured"):
        name = f"{name} 🌟"

    plays = entry.get("Plays") or 0
    favorites = entry.get("Favorites") or 0
    playstyle = (entry.get("Playstyle") or "Unknown").upper()
    privacy = entry.get("Privacy")

    heading = f"## {name}\n-# by {creator_text}"

    container = discord.ui.Container(accent_colour=accent_colour)
    if banner:
        container.add_item(discord.ui.TextDisplay(banner))
        container.add_item(discord.ui.Separator())

    # The headshot rides along as a section accessory when we have one.
    if headshot:
        container.add_item(discord.ui.Section(
            discord.ui.TextDisplay(heading),
            accessory=discord.ui.Thumbnail(media=headshot),
        ))
    else:
        container.add_item(discord.ui.TextDisplay(heading))

    container.add_item(discord.ui.Separator())

    container.add_item(discord.ui.TextDisplay(
        f"🎮  **{playstyle}**\n"
        f"▶️  **{plays:,}** plays\n"
        f"⭐  **{favorites:,}** favourites"
    ))

    # Invisible separator: breathing room without a second rule.
    container.add_item(discord.ui.Separator(
        visible=False, spacing=discord.SeparatorSpacing.small
    ))

    footer = f"ID {map_id}"
    if privacy:
        badge = {"Public": "🌐", "Private": "🔒"}.get(privacy, "")
        footer += f"  ·  {badge} {privacy}".rstrip()
    container.add_item(discord.ui.TextDisplay(f"-# {footer}"))

    container.add_item(discord.ui.Separator(
        visible=False, spacing=discord.SeparatorSpacing.small
    ))
    return container

class MapView(CardView):
    """Components V2 map card with configurable action buttons."""

    def __init__(self, entry, *, headshot=None, creator_text="Unknown",
                 creator_id=None, creator_name=None, parent=None,
                 timeout=MAP_VIEW_TIMEOUT,
                 play_label="Play in Challenge Mode",
                 play_url_template=PLAY_URL_TEMPLATE,
                 show_leaderboard=True, show_creator=True):
        super().__init__(parent=parent, timeout=timeout)
        self.entry = entry
        self.creator_id = creator_id
        self.creator_name = creator_name

        map_id = entry.get("Id")
        privacy = entry.get("Privacy")
        container = build_map_container(
            entry, headshot=headshot, creator_text=creator_text
        )

        row = discord.ui.ActionRow()
        has_actions = False
        if privacy != "Private":
            row.add_item(discord.ui.Button(
                label=play_label,
                style=discord.ButtonStyle.link,
                url=play_url_template.format(id=map_id),
                emoji="▶️",
            ))
            has_actions = True
        if show_leaderboard:
            leaderboard_button = discord.ui.Button(
                label="Leaderboard",
                style=discord.ButtonStyle.secondary,
                emoji="🏆",
            )
            leaderboard_button.callback = self.show_leaderboard
            row.add_item(leaderboard_button)
            has_actions = True

        # Text can't trigger a bot action, so the creator gets a button.
        if show_creator and creator_id:
            label = creator_name or str(creator_id)
            creator_button = discord.ui.Button(
                label=label[:78],
                style=discord.ButtonStyle.secondary,
                emoji="👤",
            )
            creator_button.callback = self.show_creator_profile
            row.add_item(creator_button)
            has_actions = True

        if has_actions:
            container.add_item(row)

        self.add_item(container)

    @keeps_context
    async def show_leaderboard(self, interaction):
        await interaction.response.defer(thinking=True)

        map_id = self.entry.get("Id")
        key = MAP_LEADERBOARD_KEY.format(id=map_id)
        embed = await build_map_leaderboard_embed(
            map_id, self.entry.get("Name") or "Unnamed Map"
        )
        if embed is None:
            await interaction.followup.send(
                f"No leaderboard found for `{key}`.", ephemeral=True
            )
            return

        # This one can't edit the card in place: the map card is a Components
        # V2 message, and Discord won't let that flag be cleared, so it can
        # never carry an embed. The leaderboard needs embed fields for its
        # columns, so it goes out as its own message.
        await interaction.followup.send(embed=embed)

    @keeps_context
    async def show_creator_profile(self, interaction):
        # thinking=True would post a new message and
        # edit_original_response would then edit that, not the card.
        await interaction.response.defer()

        name = self.creator_name or str(self.creator_id)
        view = await build_profile_view(self.creator_id, name, parent=self)
        if view is None:
            await interaction.followup.send(
                f"No game data saved for **{name}**.", ephemeral=True
            )
            return

        view.message = interaction.message
        await interaction.edit_original_response(view=view)

class DailyCupMapView(OwnedView, discord.ui.View):
    """The Daily Cup card's single experience Play action."""

    def __init__(self):
        super().__init__(timeout=None)
        self.owner_id = None   # posted by the scheduler; anyone may use it
        self.message = None
        self.add_item(discord.ui.Button(
            label="Play",
            style=discord.ButtonStyle.link,
            url=DAILY_CUP_PLAY_URL_TEMPLATE,
            emoji="▶️",
        ))

async def build_leaderboard_embed(
    leaderboard,
    *,
    title,
    subtitle=None,
    limit=MAX_LEADERBOARD_ROWS,
    style="fields",
    show_medals=False,
    show_country=True,
    show_headshot=False,
    value_name="Time",
    value_formatter=format_time,
    color=LEADERBOARD_COLOR,
):
    """Render any leaderboard-shaped list into an embed.

    Entries are dicts with UserId / Value / Country. Returns None when there
    is nothing renderable, so the caller decides what to say about it.

    style="fields" (default) puts each column in its own inline field, which
    lines up and still renders custom emoji.
    style="table" is a monospace code block: strictest alignment, but emoji
    cannot render there, so ranks are numbers and countries are codes.
    style="list" is one decorated line per row, no columns.
    """
    if not leaderboard:
        return None

    rows = await collect_leaderboard_rows(
        leaderboard, limit, value_formatter=value_formatter
    )
    if not rows:
        return None

    description = None
    fields = None
    if style == "fields":
        fields = render_leaderboard_fields(
            rows, show_country=show_country, show_medals=show_medals,
            value_name=value_name,
        )
    elif style == "table":
        description = render_leaderboard_table(
            rows, show_country=show_country, show_medals=show_medals,
            value_name=value_name,
        )
    else:
        description = render_leaderboard_list(
            rows, show_country=show_country, show_medals=show_medals,
            value_name=value_name,
        )

    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    for field_name, field_value, field_inline in (fields or []):
        embed.add_field(name=field_name, value=field_value, inline=field_inline)
    if subtitle:
        embed.set_author(name=subtitle)

    # An embed allows exactly one thumbnail, so it goes to the leader.
    if show_headshot:
        headshots = await fetch_headshots([rows[0]["user_id"]])
        leader_headshot = headshots.get(rows[0]["user_id"])
        if leader_headshot:
            embed.set_thumbnail(url=leader_headshot)

    total = len(leaderboard)
    if total > len(rows):
        embed.set_footer(text=f"Top {len(rows)} of {total}")
    else:
        embed.set_footer(text=f"{total} {'entry' if total == 1 else 'entries'}")
    return embed

async def build_map_leaderboard_embed(map_id, map_name):
    """Build the same map leaderboard used by the map card button and command."""
    key = MAP_LEADERBOARD_KEY.format(id=map_id)
    leaderboard = await fetch_entry(key, datastore="Leaderboards")
    if not isinstance(leaderboard, list):
        return None

    return await build_leaderboard_embed(
        leaderboard,
        title=f"🏆 {map_name or 'Unnamed Map'}",
        subtitle=f"Map #{map_id}",
    )

async def build_map_card(entry, **view_options):
    """Resolve a map creator and build the same card used by /map."""
    creator_id = entry.get("Creator")
    creator = await fetch_username(creator_id) if creator_id else None
    if creator:
        creator_text = f"@{creator}"
    elif creator_id:
        creator_text = f"User {creator_id}"
    else:
        creator_text = "Unknown"

    headshot = None
    if creator_id:
        headshot = (await fetch_headshots([creator_id])).get(creator_id)

    return MapView(
        entry,
        headshot=headshot,
        creator_text=creator_text,
        creator_id=creator_id,
        creator_name=creator,
        **view_options,
    )

async def build_daily_cup_map_card(entry, cup_index, cup_date):
    """Build the Daily Cup-specific embed and its one-button view."""
    creator_id = entry.get("Creator")
    creator = await fetch_username(creator_id) if creator_id else None
    if creator:
        creator_text = f"@{creator}"
    elif creator_id:
        creator_text = f"User {creator_id}"
    else:
        creator_text = "Unknown"

    headshot = None
    if creator_id:
        headshot = (await fetch_headshots([creator_id])).get(creator_id)

    map_id = entry.get("Id")
    name = entry.get("Name") or "Unnamed Map"
    plays = entry.get("Plays") or 0
    favorites = entry.get("Favorites") or 0

    embed = discord.Embed(
        title=f"🏆 {name}",
        description=(
            f"### Daily Cup #{cup_index} is live!\n"
            "Compete now and set your best time before the next Daily Cup."
        ),
        color=DAILY_CUP_COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_author(name="TODAY'S DAILY CUP")
    embed.add_field(name="Creator", value=creator_text, inline=True)
    embed.add_field(
        name="Community",
        value=f"▶️ {plays:,} plays\n⭐ {favorites:,} favourites",
        inline=True,
    )
    embed.set_footer(
        text=f"Daily Cup #{cup_index}  ·  {cup_date:%d/%m/%Y}  ·  Map ID {map_id}"
    )
    if headshot:
        embed.set_thumbnail(url=headshot)

    return embed, DailyCupMapView()

def daily_cup_role_mention(destination):
    """Resolve the configured notification role, falling back to its name."""
    try:
        role_id = int(DAILY_CUP_ROLE_ID)
    except (TypeError, ValueError):
        role_id = None

    guild = getattr(destination, "guild", None)
    if role_id is not None:
        role = guild.get_role(role_id) if guild is not None else None
        return role.mention if role is not None else f"<@&{role_id}>"

    if guild is not None:
        role = discord.utils.get(guild.roles, name="Daily Cup Notification")
        if role is not None:
            return role.mention

    print("Daily Cup notification role was not found")
    return None

async def publish_daily_cup_announcement(destination=None):
    """Post yesterday's cup leaderboard, followed by today's map card."""
    if destination is None:
        try:
            channel_id = int(DAILY_CUP_CHANNEL_ID)
        except (TypeError, ValueError):
            print("Daily Cup announcement skipped: invalid DAILY_CUP_CHANNEL_ID")
            return False

        destination = bot.get_channel(channel_id)
        if destination is None:
            destination = await bot.fetch_channel(channel_id)
    else:
        channel_id = destination.id

    todays_map = await fetch_entry("TodaysMap") or {}
    # TodaysMap only updates when a player joins, so at 09:00 UTC it can still
    # hold yesterday's cup. resolve_current_cup advances past a stale entry.
    current_index, map_id = await resolve_current_cup(todays_map)
    if current_index is None or map_id is None:
        print("Daily Cup announcement skipped: TodaysMap has no Index or Id")
        return False

    current_date = cup_day_today()
    previous_index = current_index - 1
    previous_leaderboard, previous_map_id, maps_by_id = await asyncio.gather(
        fetch_entry(
            f"DailyCup_{previous_index}", datastore="Leaderboards"
        ),
        get_cup_map_id(previous_index, todays_map),
        get_community_maps(),
    )

    leaderboard_embed = await build_leaderboard_embed(
        previous_leaderboard,
        title="🏆 Yesterday's Results",
        subtitle=f"Daily Cup #{previous_index}",
        show_medals=True,
        show_country=True,
    )
    if leaderboard_embed is None:
        print(
            "Daily Cup announcement skipped: no leaderboard for "
            f"DailyCup_{previous_index}"
        )
        return False

    if previous_map_id is not None:
        entry_count = leaderboard_embed.footer.text
        map_footer = f"Map ID: {previous_map_id}"
        leaderboard_embed.set_footer(
            text=f"{entry_count}  ·  {map_footer}" if entry_count else map_footer
        )

    if not maps_by_id:
        print("Daily Cup announcement skipped: community maps are unavailable")
        return False
    try:
        lookup_id = int(map_id)
    except (TypeError, ValueError):
        lookup_id = map_id
    entry = maps_by_id.get(lookup_id)
    if entry is None:
        print(f"Daily Cup announcement skipped: map {map_id} was not found")
        return False

    map_embed, map_view = await build_daily_cup_map_card(
        entry, current_index, current_date
    )
    role_mention = daily_cup_role_mention(destination)

    # Preserve the requested order: completed cup first, new cup second.
    await destination.send(embed=leaderboard_embed)
    map_view.message = await destination.send(
        content=(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{role_mention or ''}"
        ).rstrip(),
        embed=map_embed,
        view=map_view,
        allowed_mentions=discord.AllowedMentions(
            everyone=False,
            users=False,
            roles=True,
            replied_user=False,
        ),
    )
    print(
        f"Posted Daily Cup #{previous_index} results and "
        f"Daily Cup #{current_index} map {map_id} to channel {channel_id}"
    )
    return True

@tasks.loop(time=datetime_time(hour=9, minute=0, tzinfo=timezone.utc))
async def daily_cup_announcement():
    try:
        await publish_daily_cup_announcement()
    except Exception:
        print("Daily Cup announcement failed:")
        traceback.print_exc()

@daily_cup_announcement.before_loop
async def before_daily_cup_announcement():
    await bot.wait_until_ready()

@bot.before_invoke
async def remember_invoker(ctx):
    """Runs in the command's own task, so views built there see the caller."""
    _active_invoker.set(ctx.author.id)

@bot.event
async def on_ready():
    global _synced, _link_expiration_restore_task
    print(f"Logged in as {bot.user}")

    if (
        not _link_expirations_restored
        and (
            _link_expiration_restore_task is None
            or _link_expiration_restore_task.done()
        )
    ):
        _link_expiration_restore_task = asyncio.create_task(
            restore_link_code_expirations_until_ready()
        )

    if DAILY_CUP_CHANNEL_ID and not daily_cup_announcement.is_running():
        daily_cup_announcement.start()
        print("Daily Cup announcements scheduled for 09:00 UTC")

    # on_ready fires again on every reconnect; only sync once per process.
    if _synced:
        return
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s) globally "
              f"(may take up to an hour to show up)")
        _synced = True
    except Exception:
        print("Global slash command sync failed:")
        traceback.print_exc()
        return

    if GUILD_ID:
        # Guild commands cannot carry user-install or context metadata. Remove
        # the tree defaults only while publishing this optional test copy.
        global_contexts = bot.tree.allowed_contexts
        global_installs = bot.tree.allowed_installs
        bot.tree.allowed_contexts = app_commands.AppCommandContext()
        bot.tree.allowed_installs = app_commands.AppInstallationType()
        try:
            guild = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild)
            guild_synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(guild_synced)} test command(s) to guild {GUILD_ID}")
        except Exception:
            print(f"Test guild {GUILD_ID} command sync failed:")
            traceback.print_exc()
        finally:
            bot.tree.allowed_contexts = global_contexts
            bot.tree.allowed_installs = global_installs

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    original = getattr(error, "original", error)
    traceback.print_exception(type(original), original, original.__traceback__)
    message = f"Something broke running that command: `{type(original).__name__}: {original}`"
    # A deferred interaction has to be answered with a followup.
    if ctx.interaction is not None and ctx.interaction.response.is_done():
        await ctx.followup.send(message)
    else:
        await ctx.send(message)

async def send_cup_leaderboard(ctx, index, date_text, *, todays_map=None):
    """Post one cup's leaderboard, or explain why it isn't there."""
    leaderboard, map_id = await asyncio.gather(
        fetch_entry(f"DailyCup_{index}", datastore="Leaderboards"),
        get_cup_map_id(index, todays_map or {}),
    )
    if not leaderboard:
        await ctx.send(f"No leaderboard found for `DailyCup_{index}` ({date_text}).")
        return

    embed = await build_leaderboard_embed(
        leaderboard,
        title=f"🏆 Leaderboard — {date_text}",
        subtitle=f"Daily Cup #{index}",
        show_medals=True,
        show_country=True,
    )
    if embed is None:
        await ctx.send("The leaderboard came back empty.")
        return

    if map_id is not None:
        entry_count = embed.footer.text
        map_footer = f"Map ID: {map_id}"
        embed.set_footer(
            text=f"{entry_count}  ·  {map_footer}" if entry_count else map_footer
        )

    await ctx.send(embed=embed)

class AccountLinkView(OwnedView, discord.ui.View):
    def __init__(self, account_id, code, expires_at):
        super().__init__(timeout=900)
        self.universe = current_universe()
        self.bind_owner()
        self.account_id = account_id
        self.code = code
        self.expires_at = expires_at

        self.add_item(discord.ui.Button(
            label="Join Verification Game",
            style=discord.ButtonStyle.link,
            url=VERIFICATION_GAME_URL,
            emoji="▶️",
        ))

    @discord.ui.button(
        label="Confirm Verification",
        style=discord.ButtonStyle.success,
        emoji="✅",
    )
    @keeps_context
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.account_id:
            await interaction.response.send_message(
                "Only the user who created this code can confirm it.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        link_status, user_id = await fetch_linked_account(
            self.account_id, attempts=3
        )
        if user_id is None:
            if link_status == "error":
                message = (
                    "Couldn't read the linked-account datastore right now. "
                    "Please try again in a moment."
                )
            else:
                message = (
                    "Verification has not completed for your Discord account "
                    f"(`{self.account_id}`) yet. Enter the code in the "
                    "verification game, then try again."
                )
            await interaction.followup.send(
                message,
                ephemeral=True,
            )
            return

        await cancel_link_code_expiry(self.code)
        username = await fetch_username(user_id)
        account = f"**{username}**" if username else f"Roblox user `{user_id}`"
        self.clear_items()
        await interaction.edit_original_response(
            content=f"✅ Account linked successfully to {account}.",
            view=self,
        )

    @discord.ui.button(
        label="Regenerate Code",
        style=discord.ButtonStyle.secondary,
        emoji="🔄",
    )
    async def regenerate(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.account_id:
            await interaction.response.send_message(
                "Only the user who created this code can regenerate it.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        status, code, expires_at = await create_link_code(
            self.account_id, previous_code=self.code
        )
        if status == "pending":
            await interaction.followup.send(
                "That code is still active. Enter it in the verification game "
                "before generating another one.",
                ephemeral=True,
            )
            return
        if (
            status not in ("created", "active")
            or code is None
            or expires_at is None
        ):
            await interaction.followup.send(
                "Couldn't generate a new code. Try again in a moment.",
                ephemeral=True,
            )
            return

        old_code = self.code
        self.code = code
        self.expires_at = expires_at
        if old_code != code:
            await cancel_link_code_expiry(old_code)
        await interaction.edit_original_response(
            content=link_code_message(code, expires_at), view=self
        )

def link_code_message(code, expires_at):
    return (
        "## Link your Roblox account\n"
        "Join the verification game and enter this code:\n\n"
        f"# `{code}`\n\n"
        f"Expires <t:{expires_at}:R> (<t:{expires_at}:T>).\n"
        "The code can only be regenerated after it is consumed or expires."
    )

@bot.hybrid_command(name="link", description="Link your Discord and Roblox accounts")
async def link_command(ctx):
    if not VERIFICATION_GAME_URL:
        await ctx.send(
            "The verification game URL has not been configured yet.",
            ephemeral=ctx.interaction is not None,
        )
        return

    if ctx.interaction is not None:
        await ctx.defer(ephemeral=True)
    else:
        await ctx.defer()
    status, code, expires_at = await create_link_code(ctx.author.id)
    if (
        status not in ("created", "active")
        or code is None
        or expires_at is None
    ):
        await ctx.send("Couldn't generate a link code. Try again in a moment.")
        return

    await ctx.send(
        link_code_message(code, expires_at),
        view=AccountLinkView(ctx.author.id, code, expires_at),
        ephemeral=ctx.interaction is not None,
    )

@bot.hybrid_command(description="Show a daily cup leaderboard by date or index")
@app_commands.describe(
    date="Cup date as DD/MM/YYYY. Defaults to today.",
    index="Daily Cup index",
)
async def cup(ctx, date: str = None, index: int = None):
    # The Roblox lookups take longer than the 3s interaction deadline.
    await ctx.defer()

    todays_map = await fetch_entry("TodaysMap") or {}
    current_index = todays_map.get("Index")

    if date is not None and index is not None:
        await ctx.send("Choose either a date or an index, not both.")
        return

    # Keep prefix usage convenient: `!cup 123` means index 123, while slash
    # commands also expose a dedicated integer `index` option.
    if date is not None:
        try:
            index = int(date.strip())
        except ValueError:
            pass
        else:
            date = None

    if index is not None:
        if index < 0:
            await ctx.send("Cup index must be zero or greater.")
            return

        if isinstance(current_index, int):
            cup_date = cup_day_today() + timedelta(days=index - current_index)
            date_text = f"{cup_date:%d/%m/%Y}"
        else:
            date_text = f"Index {index}"

        await send_cup_leaderboard(
            ctx, index, date_text, todays_map=todays_map
        )
        return

    if date is not None:
        target = parse_cup_date(date)
        if target is None:
            await ctx.send("Date must look like `DD/MM/YYYY` — e.g. `10/08/2026`.")
            return
        if not isinstance(current_index, int):
            await ctx.send("Can't reach past cups: TodaysMap has no index.")
            return

        # The index advances by one per cup day, so counting days back from
        # today's index lands on that day's cup.
        days_back = (cup_day_today() - target).days
        if days_back < 0:
            await ctx.send(f"`{target:%d/%m/%Y}` is in the future.")
            return

        index = current_index - days_back
        if index < 0:
            await ctx.send(
                f"No cup on `{target:%d/%m/%Y}` — that's before the first one."
            )
            return

        await send_cup_leaderboard(
            ctx, index, f"{target:%d/%m/%Y}", todays_map=todays_map
        )
        return

    if current_index is None:
        await ctx.send("No daily cup index set, so there's no leaderboard to show.")
        return

    await send_cup_leaderboard(
        ctx, current_index, get_todays_date(), todays_map=todays_map
    )

@bot.hybrid_command(name="maps", description="Show a player's public community maps")
@app_commands.describe(username="Roblox username. Uses your linked account if omitted.")
async def maps_command(ctx, username: str = None):
    await ctx.defer()

    user_id, canonical = await resolve_command_user(ctx, username)
    if user_id is None:
        return

    view = await build_created_maps_view(user_id, canonical)
    if view is None:
        await ctx.send(f"**{canonical}** has no public maps...")
        return

    await ctx.send(view=view)

@bot.hybrid_command(name="badges", description="Show which game badges a player has earned")
@app_commands.describe(username="Roblox username. Uses your linked account if omitted.")
async def badges_command(ctx, username: str = None):
    await ctx.defer()

    user_id, canonical = await resolve_command_user(ctx, username)
    if user_id is None:
        return

    view = await build_badges_view(user_id, canonical)
    if view is None:
        await ctx.send("Couldn't fetch the game's badge list.")
        return

    await ctx.send(view=view)

@bot.hybrid_command(
    name="leaderboard",
    description="Show a map, wins, medals, or creators leaderboard",
)
@app_commands.describe(target="Community map ID, wins, medals, or creators")
async def leaderboard_command(ctx, target: str):
    await ctx.defer()

    selected = target.strip().lower()
    global_leaderboard = GLOBAL_LEADERBOARDS.get(selected)
    if global_leaderboard is not None:
        scope, value_name = global_leaderboard
        leaderboard = await fetch_ordered_leaderboard(
            datastore="Data", scope=scope, limit=MAX_LEADERBOARD_ROWS
        )
        embed = None
        if leaderboard:
            embed = await build_leaderboard_embed(
                leaderboard,
                title=f"🏆 {value_name} Leaderboard",
                subtitle=f"Data · {scope}",
                show_medals=False,
                show_country=False,
                value_name=value_name,
                value_formatter=format_number,
            )
        if embed is None:
            await ctx.send(f"No entries found for the **{value_name}** leaderboard.")
            return

        await ctx.send(embed=embed)
        return

    try:
        map_id = int(selected)
    except ValueError:
        await ctx.send("Choose a map ID, `wins`, `medals`, or `creators`.")
        return

    maps_by_id = await get_community_maps()
    if not maps_by_id:
        await ctx.send("Failed to fetch the community map list.")
        return

    entry = maps_by_id.get(map_id)
    if entry is None:
        await ctx.send(f"No community map with ID `{map_id}`.")
        return

    embed = await build_map_leaderboard_embed(
        map_id, entry.get("Name") or "Unnamed Map"
    )
    if embed is None:
        key = MAP_LEADERBOARD_KEY.format(id=map_id)
        await ctx.send(f"No leaderboard found for `{key}`.")
        return

    await ctx.send(embed=embed)

async def build_map_view(entry, parent=None):
    """Resolve a map's creator and wrap it in a card."""
    creator_id = entry.get("Creator")
    creator = await fetch_username(creator_id) if creator_id else None
    if creator:
        creator_text = f"@{creator}"
    elif creator_id:
        creator_text = f"User {creator_id}"
    else:
        creator_text = "Unknown"

    headshot = None
    if creator_id:
        headshot = (await fetch_headshots([creator_id])).get(creator_id)

    return MapView(
        entry, headshot=headshot, creator_text=creator_text,
        creator_id=creator_id, creator_name=creator, parent=parent,
    )

@bot.hybrid_command(
    name="map",
    description="Show a community map by ID, or search public maps by name",
)
@app_commands.describe(query="A map ID, or part of a map name")
async def map_command(ctx, *, query: str):
    await ctx.defer()

    maps_by_id = await get_community_maps()
    if not maps_by_id:
        await ctx.send("Failed to fetch the community map list.")
        return

    cleaned = query.strip()

    if cleaned.isdigit():
        entry = maps_by_id.get(int(cleaned))
        if entry is None:
            await ctx.send(f"No community map with ID `{cleaned}`.")
            return
        view = await build_map_view(entry)
        view.message = await ctx.send(view=view)
        return

    matches = search_public_maps(maps_by_id, cleaned)
    if not matches:
        await ctx.send(f"No public maps found for **{cleaned}**.")
        return

    if len(matches) == 1:
        view = await build_map_view(matches[0])
        view.message = await ctx.send(view=view)
        return

    view = await build_map_search_view(cleaned, matches)
    view.message = await ctx.send(view=view)

@bot.hybrid_command(name="profile", description="Show a player's profile")
@app_commands.describe(username="Roblox username. Uses your linked account if omitted.")
async def profile_command(ctx, username: str = None):
    await ctx.defer()

    user_id, canonical = await resolve_command_user(ctx, username)
    if user_id is None:
        return

    view = await build_profile_view(user_id, canonical)
    if view is None:
        await ctx.send(f"No game data saved for **{canonical}**.")
        return

    view.message = await ctx.send(view=view)

def describe_command(command, prefix):
    usage = f"{prefix}{command.qualified_name}"
    if command.signature:
        usage += f" {command.signature}"
    summary = command.description or command.short_doc or ""
    line = f"`{usage}`"
    if summary:
        line += f"\n-# {summary}"
    return line

def visible_commands(include_admin):
    """Everything worth listing, split into player and admin commands.

    The !dev_ twins are left out entirely -- they mirror commands already
    listed and only differ in which universe they hit.
    """
    players, admin = [], []
    for command in bot.commands:
        if command.name.startswith("dev_"):
            continue
        # Every admin command is registered hidden.
        (admin if command.hidden else players).append(command)

    players.sort(key=lambda c: c.qualified_name)
    admin.sort(key=lambda c: c.qualified_name)
    return players, (admin if include_admin else [])

def build_help_view(heading, subtitle, commands_list, prefix):
    container = discord.ui.Container(accent_colour=PROFILE_COLOR)
    container.add_item(discord.ui.TextDisplay(f"## {heading}\n-# {subtitle}"))
    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay(
        "\n".join(describe_command(c, prefix) for c in commands_list)
    ))

    note = dev_universe_note()
    if note:
        container.add_item(discord.ui.TextDisplay(note))

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view

@bot.hybrid_command(name="help", description="List everything this bot can do")
async def help_command(ctx):
    players, admin = visible_commands(ctx.author.id == ADMIN_USER_ID)

    await ctx.send(view=build_help_view(
        "📖 Commands", f"{len(players)} available", players, "/",
    ))

    if not admin:
        return

    # The admin list goes out privately, so running /help in a public channel
    # never advertises it.
    admin_view = build_help_view(
        "🔧 Admin commands",
        "prefix only, and only you can run them",
        admin, "!",
    )
    if ctx.interaction is not None:
        await ctx.send(view=admin_view, ephemeral=True)
        return
    try:
        await ctx.author.send(view=admin_view)
    except discord.HTTPException:
        # DMs closed; better in-channel than not at all.
        await ctx.send(view=admin_view)

def summarize_sync(added, removed, blocked):
    lines = []
    if added:
        lines.append("➕ " + ", ".join(r.mention for r in added))
    if removed:
        lines.append("➖ " + ", ".join(r.mention for r in removed))
    if not lines:
        lines.append("-# Already up to date — nothing changed.")
    if blocked:
        lines.append(f"-# ⚠️ {len(blocked)} role(s) I can't manage: "
                     + ", ".join(f"`{r}`" for r in blocked))
    return "\n".join(lines)

@bot.hybrid_command(name="sync", description="Update your roles from your Roblox progress")
@app_commands.describe(
    target="Admin only: a member to sync, or `all` for everyone linked"
)
async def sync_command(ctx, *, target: str = None):
    if ctx.guild is None:
        await ctx.send(view=simple_card("This only works inside a server.",
                                        colour=discord.Color.red()))
        return

    await ctx.defer()

    is_admin_user = ctx.author.id == ADMIN_USER_ID
    if target and not is_admin_user:
        await ctx.send(view=simple_card("Only an admin can sync someone else.",
                                        colour=discord.Color.red()))
        return

    rules = await fetch_role_rules(ctx.guild.id)
    if not rules:
        await ctx.send(view=simple_card(
            "No role rules are configured for this server.\n"
            "-# `!roles add <@role> badge <id>` to make one.",
            colour=discord.Color.greyple(),
        ))
        return

    map_cache = {}

    # Everyone linked, admin only.
    if target and target.strip().lower() == "all":
        linked = await fetch_entry("Linked", datastore=ACCOUNT_LINK_DATASTORE)
        linked = linked if isinstance(linked, dict) else {}

        synced = changed = skipped = 0
        for account_id, roblox_id in linked.items():
            member = ctx.guild.get_member(int(account_id))
            if member is None:
                skipped += 1
                continue
            try:
                added, removed, _ = await sync_member_roles(
                    member, int(roblox_id), rules, map_cache=map_cache
                )
            except discord.HTTPException:
                skipped += 1
                continue
            synced += 1
            if added or removed:
                changed += 1

        await ctx.send(view=simple_card(
            f"**{synced}** member(s) synced  ·  **{changed}** changed"
            + (f"\n-# {skipped} skipped — not in this server, or I couldn't "
               f"edit their roles." if skipped else ""),
            heading="🔄 Sync complete",
        ))
        return

    if target:
        member = await find_discord_user(ctx, target)
        if member is None or ctx.guild.get_member(member.id) is None:
            await ctx.send(view=simple_card(
                f"Couldn't find `{target.strip()}` in this server.",
                colour=discord.Color.red(),
            ))
            return
        member = ctx.guild.get_member(member.id)
    else:
        member = ctx.guild.get_member(ctx.author.id) or ctx.author

    roblox_id = await fetch_linked_user_id(member.id)
    if roblox_id is None:
        who = "You haven't" if member.id == ctx.author.id else f"**{member}** hasn't"
        await ctx.send(view=simple_card(
            f"{who} linked a Roblox account. Use `/link` first.",
            colour=discord.Color.greyple(),
        ))
        return

    try:
        added, removed, blocked = await sync_member_roles(
            member, roblox_id, rules, map_cache=map_cache
        )
    except discord.Forbidden:
        await ctx.send(view=simple_card(
            "I don't have permission to edit those roles.",
            colour=discord.Color.red(),
        ))
        return

    name, headshots = await asyncio.gather(
        fetch_username(roblox_id), fetch_headshots([roblox_id])
    )
    heading = (f"## 🔄 Roles synced\n"
               f"{member.mention}  ·  🎮 **{name or roblox_id}**")

    container = discord.ui.Container(accent_colour=PROFILE_COLOR)
    headshot = headshots.get(roblox_id)
    if headshot:
        container.add_item(discord.ui.Section(
            discord.ui.TextDisplay(heading),
            accessory=discord.ui.Thumbnail(media=headshot),
        ))
    else:
        container.add_item(discord.ui.TextDisplay(heading))

    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay(
        summarize_sync(added, removed, blocked)
    ))

    note = dev_universe_note()
    if note:
        container.add_item(discord.ui.TextDisplay(note))

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    await ctx.send(view=view, allowed_mentions=SILENT)

@bot.hybrid_command(description="Check that the bot is alive")
async def ping(ctx):
    await ctx.send("hello fuckers")

async def admin_only_predicate(ctx):
    return ctx.author.id == ADMIN_USER_ID

def is_admin():
    """Prefix-command gate. Stays silent for everyone else."""
    return commands.check(admin_only_predicate)

async def resolve_admin_target(ctx, player):
    """Username or ID -> (user_id, name). Reports and returns None on miss."""
    cleaned = player.strip()
    if cleaned.isdigit():
        user_id = int(cleaned)
        return user_id, (await fetch_username(user_id)) or str(user_id)

    user_id, canonical = await fetch_user_id(cleaned)
    if user_id is None:
        await ctx.send(f"No Roblox user named `{cleaned}`.")
        return None, None
    return user_id, canonical

@bot.command(name="ban", hidden=True)
@is_admin()
async def admin_ban(ctx, player: str, duration: str = "perm", *, reason: str = ""):
    """Ban a player. A permanent ban also clears their leaderboard entries."""
    async with ctx.typing():
        user_id, name = await resolve_admin_target(ctx, player)
        if user_id is None:
            return

        kind, seconds = parse_ban_duration(duration)
        if kind == "invalid":
            await ctx.send(
                f"Couldn't read `{duration}` as a duration. Use `7d`, `12h`, "
                f"`30m`, or `perm`."
            )
            return

        permanent = kind == "permanent"
        display_reason = reason or "Banned by an administrator."
        ok = await set_user_restriction(
            user_id,
            active=True,
            duration_seconds=None if permanent else seconds,
            display_reason=display_reason,
            private_reason=f"{display_reason} (by {ctx.author} / {ctx.author.id})",
        )
        if not ok:
            await ctx.send(
                f"Failed to ban **{name}**. Check the logs — the API key may be "
                f"missing `universe.user-restriction:write`."
            )
            return

        length = "permanently" if permanent else f"for {format_ban_duration(seconds)}"
        lines = [f"🔨 Banned **{name}** (`{user_id}`) {length}."]
        if reason:
            lines.append(f"-# {reason}")

        if permanent:
            removed = await purge_player_from_leaderboards(user_id)
            lines.append(
                "-# Removed from: " + (", ".join(removed) if removed else "nothing")
            )

        await ctx.send("\n".join(lines))

@bot.command(name="unban", hidden=True)
@is_admin()
async def admin_unban(ctx, player: str):
    """Lift a player's ban."""
    async with ctx.typing():
        user_id, name = await resolve_admin_target(ctx, player)
        if user_id is None:
            return

        if await set_user_restriction(user_id, active=False):
            await ctx.send(f"✅ Unbanned **{name}** (`{user_id}`).")
        else:
            await ctx.send(f"Failed to unban **{name}**. Check the logs.")

async def set_map_featured(ctx, map_id, featured):
    """Flip Featured on one map inside the Community Maps index."""
    state = {"name": None, "found": False, "already": False}

    def transform(entries):
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if isinstance(entry, dict) and entry.get("Id") == map_id:
                state["found"] = True
                state["name"] = entry.get("Name") or "Unnamed Map"
                if bool(entry.get("Featured")) == featured:
                    state["already"] = True
                    return None
                entry["Featured"] = featured
                return entries
        return None

    async with ctx.typing():
        result = await update_entry_with_retry("Ids", "Community Maps", transform)

    word = "featured" if featured else "unfeatured"
    if not state["found"]:
        await ctx.send(f"No community map with ID `{map_id}`.")
        return
    if state["already"]:
        await ctx.send(f"**{state['name']}** (`{map_id}`) is already {word}.")
        return
    if result != "ok":
        await ctx.send(
            f"Failed to update `{map_id}` ({result}). The map list was left "
            f"untouched — check the logs."
        )
        return

    invalidate_maps_cache()
    icon = "🌟" if featured else "☆"
    lines = [f"{icon} **{state['name']}** (`{map_id}`) is now {word}."]
    note = dev_universe_note()
    if note:
        lines.append(note)
    await ctx.send("\n".join(lines))

@bot.command(name="feature", hidden=True)
@is_admin()
async def admin_feature(ctx, map_id: int):
    """Mark a community map as featured."""
    await set_map_featured(ctx, map_id, True)

@bot.command(name="unfeature", hidden=True)
@is_admin()
async def admin_unfeature(ctx, map_id: int):
    """Take the featured mark off a community map."""
    await set_map_featured(ctx, map_id, False)

def map_delete_targets(map_id):
    """Everything keyed to a map, as (label, datastore, key) for retrying."""
    return [
        ("map entry", "Community Maps", str(map_id)),
        ("leaderboard", "Leaderboards", MAP_LEADERBOARD_KEY.format(id=map_id)),
    ]

async def perform_map_delete(map_id):
    """Drop a map from the index, then delete everything keyed to it.

    Returns (status, name, leftovers), leftovers being the delete targets
    that failed, so each can be retried on its own. The index write goes
    first, so a failure there can't leave the map half-deleted.
    """
    state = {"name": None, "found": False}

    def transform(entries):
        if not isinstance(entries, list):
            return None
        for position, entry in enumerate(entries):
            if isinstance(entry, dict) and entry.get("Id") == map_id:
                state["found"] = True
                state["name"] = entry.get("Name") or "Unnamed Map"
                return entries[:position] + entries[position + 1:]
        return None

    result = await update_entry_with_retry("Ids", "Community Maps", transform)
    if not state["found"]:
        return "missing", None, []
    if result != "ok":
        return result, state["name"], []

    invalidate_maps_cache()

    # The map's own entry and its leaderboard are both keyed by the map ID.
    targets = map_delete_targets(map_id)
    outcomes = await asyncio.gather(*(
        delete_entry_resource(key, datastore) for _, datastore, key in targets
    ))
    leftovers = [t for t, deleted in zip(targets, outcomes) if not deleted]
    return "ok", state["name"], leftovers

class DeleteMapView(CardView):
    """The map about to be deleted, with the confirmation on it."""

    def __init__(self, entry, *, headshot=None, creator_text="Unknown",
                 timeout=MAP_VIEW_TIMEOUT):
        super().__init__(timeout=timeout)
        self.entry = entry
        self.pending = []

        map_id = entry.get("Id")
        container = build_map_container(
            entry,
            headshot=headshot,
            creator_text=creator_text,
            accent_colour=discord.Color.red(),
            banner=("### ⚠️ Delete this map?\n"
                    "-# Removes it from the index of ~85k maps and deletes its "
                    "entry. There is no undo."),
        )

        row = discord.ui.ActionRow()
        confirm = discord.ui.Button(
            label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️"
        )
        confirm.callback = self.confirm_delete
        row.add_item(confirm)

        cancel = discord.ui.Button(
            label="Cancel", style=discord.ButtonStyle.secondary
        )
        cancel.callback = self.cancel_delete
        row.add_item(cancel)
        container.add_item(row)

        note = dev_universe_note()
        if note:
            container.add_item(discord.ui.TextDisplay(note))

        self.add_item(container)

    def finish(self, text, colour, *, retries=None, retry_all=False):
        """Replace the card with an outcome, plus buttons for what failed."""
        self.clear_items()
        container = discord.ui.Container(accent_colour=colour)
        container.add_item(discord.ui.TextDisplay(text))

        row = discord.ui.ActionRow()
        for target in retries or []:
            button = discord.ui.Button(
                label=f"Retry {target[0]}",
                style=discord.ButtonStyle.danger,
                emoji="🔁",
            )
            button.callback = self.make_retry(target)
            row.add_item(button)
        if retry_all:
            button = discord.ui.Button(
                label="Retry", style=discord.ButtonStyle.danger, emoji="🔁"
            )
            button.callback = self.confirm_delete
            row.add_item(button)
        if row.children:
            container.add_item(row)
        else:
            # Nothing left to retry, so the view is done.
            self.stop()

        note = dev_universe_note()
        if note:
            container.add_item(discord.ui.TextDisplay(note))
        self.add_item(container)

    def report_partial(self, name, map_id, leftovers):
        self.pending = leftovers
        if not leftovers:
            self.finish(
                f"🗑️ Deleted **{name}** (`{map_id}`).\n"
                f"-# Removed from the index, along with its entry and leaderboard.",
                discord.Color.red(),
            )
            return
        missed = " and ".join(f"its {label}" for label, _, _ in leftovers)
        self.finish(
            f"🗑️ Deleted **{name}** (`{map_id}`).\n"
            f"-# ⚠️ Index updated, but couldn't delete {missed}.",
            discord.Color.orange(),
            retries=leftovers,
        )

    def make_retry(self, target):
        """A callback retrying exactly one failed deletion."""
        label, datastore, key = target

        async def retry(interaction):
            universe = _active_universe.set(self.universe)
            invoker = _active_invoker.set(self.owner_id)
            try:
                await interaction.response.defer()
                if await delete_entry_resource(key, datastore):
                    self.pending = [t for t in self.pending if t != target]
                    self.report_partial(
                        self.entry.get("Name") or "Unnamed Map",
                        self.entry.get("Id"), self.pending,
                    )
                else:
                    missed = " and ".join(f"its {l}" for l, _, _ in self.pending)
                    self.finish(
                        f"🔁 Retrying **{label}** failed again.\n"
                        f"-# Still undeleted: {missed}. Check the logs.",
                        discord.Color.orange(),
                        retries=self.pending,
                    )
                await interaction.edit_original_response(view=self)
            finally:
                _active_universe.reset(universe)
                _active_invoker.reset(invoker)

        return retry

    @keeps_context
    async def confirm_delete(self, interaction):
        await interaction.response.defer()
        map_id = self.entry.get("Id")
        name = self.entry.get("Name") or "Unnamed Map"

        status, deleted_name, leftovers = await perform_map_delete(map_id)
        if status == "missing":
            self.finish(f"No community map with ID `{map_id}`.",
                        discord.Color.red())
        elif status != "ok":
            self.finish(
                f"Failed to remove **{name}** (`{map_id}`) from the index "
                f"({status}). Nothing was deleted — check the logs.",
                discord.Color.orange(),
                retry_all=True,
            )
        else:
            self.report_partial(deleted_name, map_id, leftovers)

        await interaction.edit_original_response(view=self)

    @keeps_context
    async def cancel_delete(self, interaction):
        await interaction.response.defer()
        name = self.entry.get("Name") or "Unnamed Map"
        self.finish(f"Cancelled. **{name}** was not touched.",
                    discord.Color.greyple())
        await interaction.edit_original_response(view=self)

@bot.command(name="deletemap", hidden=True)
@is_admin()
async def admin_delete_map(ctx, map_id: int):
    """Remove a map from the index and delete its entry. Irreversible."""
    async with ctx.typing():
        maps_by_id = await get_community_maps()
        if not maps_by_id:
            await ctx.send("Failed to fetch the community map list.")
            return

        entry = maps_by_id.get(map_id)
        if entry is None:
            await ctx.send(f"No community map with ID `{map_id}`.")
            return

        creator_id = entry.get("Creator")
        creator = await fetch_username(creator_id) if creator_id else None
        creator_text = (f"@{creator}" if creator
                        else f"User {creator_id}" if creator_id else "Unknown")
        headshot = None
        if creator_id:
            headshot = (await fetch_headshots([creator_id])).get(creator_id)

    view = DeleteMapView(entry, headshot=headshot, creator_text=creator_text)
    view.message = await ctx.send(view=view)

async def find_discord_user(ctx, query):
    """Resolve a Discord mention, ID, or username to a user."""
    cleaned = query.strip()
    mention = re.fullmatch(r"<@!?(\d+)>", cleaned)
    if mention:
        cleaned = mention.group(1)

    if cleaned.isdigit():
        user = bot.get_user(int(cleaned))
        if user is not None:
            return user
        try:
            return await bot.fetch_user(int(cleaned))
        except discord.HTTPException:
            return None

    name = cleaned.lstrip("@").lower()
    if ctx.guild is not None:
        # query_members goes over the gateway, so it works without the
        # privileged members intent.
        try:
            found = await ctx.guild.query_members(query=name, limit=10)
        except (discord.HTTPException, asyncio.TimeoutError):
            found = []
        for member in found:
            if name in (member.name.lower(), member.display_name.lower()):
                return member
        if found:
            return found[0]

    return discord.utils.find(lambda u: u.name.lower() == name, bot.users)

async def resolve_lookup(ctx, query):
    """Work out who a query refers to, on either side of a link.

    Returns (roblox_id, roblox_name, discord_user, source) where source
    names how the query was read.
    """
    linked = await fetch_entry("Linked", datastore=ACCOUNT_LINK_DATASTORE)
    linked = linked if isinstance(linked, dict) else {}

    # Linked maps Discord account -> Roblox user, so invert it for the
    # other direction.
    by_roblox = {}
    for account_id, user_id in linked.items():
        try:
            by_roblox[int(user_id)] = int(account_id)
        except (TypeError, ValueError):
            continue

    cleaned = query.strip()
    roblox_id = roblox_name = None
    account_id = None
    source = None

    if cleaned.isdigit():
        number = int(cleaned)
        if cleaned in linked:
            account_id, source = number, "Discord ID"
            roblox_id = int(linked[cleaned])
        elif number in by_roblox:
            roblox_id, source = number, "Roblox ID"
            account_id = by_roblox[number]
        else:
            # Unlinked: decide which side it is by asking Roblox first.
            roblox_name = await fetch_username(number)
            if roblox_name:
                roblox_id, source = number, "Roblox ID"
            else:
                account_id, source = number, "Discord ID"
    else:
        roblox_id, roblox_name = await fetch_user_id(cleaned.lstrip("@"))
        if roblox_id is not None:
            source = "Roblox username"
            account_id = by_roblox.get(int(roblox_id))
        else:
            discord_user = await find_discord_user(ctx, cleaned)
            if discord_user is None:
                return None, None, None, None
            account_id, source = discord_user.id, "Discord username"
            raw = linked.get(str(discord_user.id))
            roblox_id = int(raw) if raw is not None else None

    if roblox_id is not None and not roblox_name:
        roblox_name = await fetch_username(roblox_id)

    discord_user = None
    if account_id is not None:
        discord_user = bot.get_user(account_id)
        if discord_user is None:
            try:
                discord_user = await bot.fetch_user(account_id)
            except discord.HTTPException:
                discord_user = None

    return roblox_id, roblox_name, discord_user or account_id, source

@bot.command(name="lookup", hidden=True)
@is_admin()
async def admin_lookup(ctx, *, query: str):
    """Find the linked Roblox and Discord accounts behind any ID or username."""
    async with ctx.typing():
        roblox_id, roblox_name, discord_side, source = await resolve_lookup(ctx, query)

        if roblox_id is None and discord_side is None:
            await ctx.send(f"Couldn't find anyone matching `{query.strip()}`.")
            return

        headshot = None
        if roblox_id is not None:
            headshot = (await fetch_headshots([roblox_id])).get(roblox_id)

    if isinstance(discord_side, int):
        discord_line = f"`{discord_side}`\n-# account not reachable"
        account_id = discord_side
    elif discord_side is not None:
        discord_line = f"{discord_side.mention} · **{discord_side}**\n-# `{discord_side.id}`"
        account_id = discord_side.id
    else:
        discord_line = "-# not linked"
        account_id = None

    if roblox_id is not None:
        roblox_line = f"**{roblox_name or 'Unknown'}**\n-# `{roblox_id}`"
    else:
        roblox_line = "-# not linked"

    linked_both = roblox_id is not None and account_id is not None
    heading = ("## 🔗 Linked accounts" if linked_both
               else "## 🔎 Lookup\n-# these accounts are not linked")

    container = discord.ui.Container(
        accent_colour=PROFILE_COLOR if linked_both else discord.Color.greyple()
    )
    body = discord.ui.TextDisplay(
        f"{heading}\n\n"
        f"🎮 **Roblox**\n{roblox_line}\n\n"
        f"💬 **Discord**\n{discord_line}"
    )
    if headshot:
        container.add_item(discord.ui.Section(
            body, accessory=discord.ui.Thumbnail(media=headshot)
        ))
    else:
        container.add_item(body)

    container.add_item(discord.ui.TextDisplay(f"-# matched by {source}"))
    note = dev_universe_note()
    if note:
        container.add_item(discord.ui.TextDisplay(note))

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    await ctx.send(view=view)

def datastore_debug_json_default(value):
    """Keep diagnostic output JSON-serializable without discarding bytes."""
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        return {
            "pythonType": type(value).__name__,
            "utf8": text,
            "base64": base64.b64encode(raw).decode("ascii"),
        }
    return {
        "pythonType": type(value).__name__,
        "repr": repr(value),
    }

@bot.command(name="debuglinked", hidden=True)
@is_admin()
async def admin_debug_linked(ctx):
    """Attach the raw and decoded AccountLinking/Linked datastore value."""
    async with ctx.typing():
        status, resource = await fetch_entry_resource(
            "Linked", ACCOUNT_LINK_DATASTORE, fresh=True
        )
        raw_value = resource.get("value") if isinstance(resource, dict) else None
        report = {
            "universeId": current_universe(),
            "dataStore": ACCOUNT_LINK_DATASTORE,
            "entry": "Linked",
            "fetchStatus": status,
            "resource": resource,
            "rawValuePythonType": type(raw_value).__name__,
            "rawValueKeys": (
                [str(key) for key in raw_value.keys()]
                if isinstance(raw_value, dict) else None
            ),
            "rawValueFieldTypes": (
                {
                    str(key): type(value).__name__
                    for key, value in raw_value.items()
                }
                if isinstance(raw_value, dict) else None
            ),
        }

        try:
            decoded = decode_json_value(raw_value)
        except Exception as error:
            report["decodeError"] = {
                "type": type(error).__name__,
                "message": str(error),
                "traceback": traceback.format_exc(),
            }
        else:
            report["decodedPythonType"] = type(decoded).__name__
            report["decodedValue"] = decoded

        payload = json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
            default=datastore_debug_json_default,
        ).encode("utf-8")

    await ctx.send(
        "Fresh `AccountLinking/Linked` diagnostic:",
        file=discord.File(
            io.BytesIO(payload), filename="account-linking-linked-debug.json"
        ),
    )

def simple_card(text, *, colour=PROFILE_COLOR, heading=None):
    """A one-block Components V2 card, for short replies."""
    container = discord.ui.Container(accent_colour=colour)
    if heading:
        container.add_item(discord.ui.TextDisplay(f"## {heading}"))
        container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay(text))

    note = dev_universe_note()
    if note:
        container.add_item(discord.ui.TextDisplay(note))

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view

# Role mentions render inside a card, so keep them from pinging.
SILENT = discord.AllowedMentions.none()

async def role_rule_badge_names(rules):
    """Resolve badge rule values to their Roblox display names."""
    wanted = {
        int(rule["value"])
        for rule in rules
        if rule.get("type") == "badge"
        and str(rule.get("value", "")).isdigit()
    }
    if not wanted:
        return {}

    badges = await fetch_universe_badges()
    names = {}
    for badge in badges:
        badge_id = badge.get("id") if isinstance(badge, dict) else None
        try:
            badge_id = int(badge_id)
        except (TypeError, ValueError):
            continue
        if badge_id not in wanted:
            continue
        names[badge_id] = (
            badge.get("displayName") or badge.get("name") or "Unknown badge"
        )
    return names

def badge_link_parts(name):
    """Keep emoji beside, but outside, a Components V2 Markdown link."""
    name = " ".join(str(name).split())
    label, emoji = [], []
    for character in name:
        codepoint = ord(character)
        is_emoji = (
            0x1F000 <= codepoint <= 0x1FAFF
            or 0x2600 <= codepoint <= 0x27BF
            or codepoint in (0x200D, 0x20E3, 0xFE0E, 0xFE0F)
        )
        (emoji if is_emoji else label).append(character)

    label_text = " ".join("".join(label).split()) or "View badge"
    label_text = (
        label_text.replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )
    emoji_text = "".join(emoji).strip()
    return label_text, emoji_text

def describe_rule(rule, guild, *, admin=False, badge_names=None):
    role_id = rule_role_id(rule)
    role = guild.get_role(role_id) if guild and role_id is not None else None
    if role is not None:
        role_text = role.mention
    elif role_id is not None:
        role_text = "⚠️ **Role unavailable**"
        if admin:
            role_text += f" — it no longer exists in this server (`{role_id}`)"
    else:
        role_text = "⚠️ **Role unavailable**"
        if admin:
            role_text += (
                " — its ID was saved using the old number format and lost "
                "precision. Remove and recreate this rule."
            )

    if rule.get("type") == "badge":
        try:
            badge_id = int(rule.get("value"))
        except (TypeError, ValueError):
            badge_id = None
        badge_name = (badge_names or {}).get(badge_id)
        if badge_name and badge_id is not None:
            link_text, emoji_text = badge_link_parts(badge_name)
            badge_url = f"https://www.roblox.com/badges/{badge_id}/Badge"
            emoji_suffix = f" {emoji_text}" if emoji_text else ""
            condition = (
                f"Own badge **[{link_text}]({badge_url}){emoji_suffix}**"
            )
        else:
            condition = "Own badge **Unknown badge**"
    elif rule.get("type") == "map":
        map_id = rule.get("value")
        if map_id is not None:
            play_url = PLAY_URL_TEMPLATE.format(id=map_id)
            condition = f"Complete **[map #{map_id}]({play_url})**"
        else:
            condition = "Complete **Unknown map**"
    else:
        condition = "⚠️ Unknown requirement"
    lines = []
    if admin:
        lines.append(f"**Rule `{rule.get('id', '?')}`**")
    lines.extend((f"**Role:** {role_text}", f"**Requirement:** {condition}"))
    return "\n".join(lines)

class PlayerAchievementsView(CardView):
    """Toggle between a member's earned and missing achievement roles."""

    def __init__(self, rules, guild, player, *, owner_id=None):
        super().__init__()
        self.owner_id = owner_id
        self.player_name = discord.utils.escape_markdown(player.display_name)

        held_role_ids = {role.id for role in player.roles}
        configured_roles = []
        seen_role_ids = set()
        for rule in rules:
            role_id = rule_role_id(rule)
            role = guild.get_role(role_id) if role_id is not None else None
            if role is None or role_id in seen_role_ids:
                continue
            seen_role_ids.add(role_id)
            configured_roles.append(role)

        self.achieved_roles = [
            role for role in configured_roles if role.id in held_role_ids
        ]
        self.missing_roles = [
            role for role in configured_roles if role.id not in held_role_ids
        ]
        self.showing_missing = False
        self.render()

    def render(self):
        self.clear_items()
        container = discord.ui.Container(accent_colour=PROFILE_COLOR)

        if self.showing_missing:
            title = f"## ❌ {self.player_name}'s Missing Achievements"
            roles = self.missing_roles
            body = "\n".join(f"❌ {role.mention}" for role in roles)
            body = body or "✅ This player is not missing any achievements."
        else:
            title = f"## 🎭 {self.player_name}'s Achievements"
            roles = self.achieved_roles
            body = "\n".join(f"✅ {role.mention}" for role in roles)
            body = body or "This player has no achievements yet."

        container.add_item(discord.ui.TextDisplay(
            f"{title}\n-# {len(roles)} achievement{'s' if len(roles) != 1 else ''}"
        ))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(body))

        row = discord.ui.ActionRow()
        if self.showing_missing:
            button = discord.ui.Button(
                label="Achievements",
                style=discord.ButtonStyle.success,
                emoji="✔️",
            )
            button.callback = self.show_achievements
        else:
            button = discord.ui.Button(
                label="Missing",
                style=discord.ButtonStyle.danger,
                emoji="✖️",
            )
            button.callback = self.show_missing
        row.add_item(button)
        container.add_item(row)
        self.add_item(container)

    async def show_missing(self, interaction):
        self.showing_missing = True
        self.render()
        await interaction.response.edit_message(view=self)

    async def show_achievements(self, interaction):
        self.showing_missing = False
        self.render()
        await interaction.response.edit_message(view=self)


def build_role_rules_view(
    rules, guild, *, admin=False, badge_names=None, player=None, owner_id=None
):
    """Render achievement roles or a member's compact achievement checklist."""
    if player is not None:
        return PlayerAchievementsView(
            rules, guild, player, owner_id=owner_id
        )

    if not rules:
        text = "No achievement roles are configured yet."
        if admin:
            text += (
                "\n-# Add: `!roles add @Role map <map ID>` or "
                "`!roles add @Role badge <badge ID>`"
            )
        return simple_card(
            text,
            heading="🎭 Achievement Roles",
            colour=discord.Color.greyple(),
        )

    container = discord.ui.Container(accent_colour=PROFILE_COLOR)
    container.add_item(discord.ui.TextDisplay(
        f"## 🎭 Achievement Roles\n-# {len(rules)} available"
    ))
    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay(
        "\n\n".join(
            describe_rule(
                rule, guild, admin=admin, badge_names=badge_names
            )
            for rule in rules
        )
    ))
    container.add_item(discord.ui.Separator(
        visible=False, spacing=discord.SeparatorSpacing.small
    ))
    if admin:
        footer = (
            "-# Remove: `!roles remove <rule ID>`\n"
            "-# Add: `!roles add @Role map <map ID>` or "
            "`!roles add @Role badge <badge ID>`"
        )
    else:
        footer = "-# Use `/sync` to update your roles."
    container.add_item(discord.ui.TextDisplay(footer))

    note = dev_universe_note()
    if note:
        container.add_item(discord.ui.TextDisplay(note))

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view

@bot.tree.command(name="roles", description="View the server's achievement roles")
@app_commands.describe(player="Show this member's earned and missing achievements")
async def roles_slash_command(
    interaction: discord.Interaction, player: discord.Member = None
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command only works inside a server.", ephemeral=True
        )
        return

    await interaction.response.defer()
    rules = await fetch_role_rules(interaction.guild.id)
    badge_names = await role_rule_badge_names(rules) if player is None else {}
    view = build_role_rules_view(
        rules,
        interaction.guild,
        admin=interaction.user.id == ADMIN_USER_ID,
        badge_names=badge_names,
        player=player,
        owner_id=interaction.user.id,
    )
    await interaction.followup.send(view=view, allowed_mentions=SILENT)

@bot.group(name="roles", hidden=True, invoke_without_command=True)
async def admin_roles(ctx, player: discord.Member = None):
    """View achievement roles, optionally filtered to a server member."""
    if ctx.guild is None:
        await ctx.send("This command only works inside a server.")
        return
    async with ctx.typing():
        rules = await fetch_role_rules(ctx.guild.id)
        badge_names = await role_rule_badge_names(rules) if player is None else {}
    admin = ctx.author.id == ADMIN_USER_ID
    await ctx.send(
        view=build_role_rules_view(
            rules,
            ctx.guild,
            admin=admin,
            badge_names=badge_names,
            player=player,
            owner_id=ctx.author.id,
        ),
        allowed_mentions=SILENT,
    )

@admin_roles.command(name="add")
@is_admin()
async def admin_roles_add(ctx, role: discord.Role, condition: str, value: int):
    """!roles add <@role> <badge|map> <id>"""
    kind = condition.strip().lower()
    if kind not in ROLE_CONDITIONS:
        await ctx.send(view=simple_card(
            f"Condition must be one of: {', '.join(ROLE_CONDITIONS)}.",
            colour=discord.Color.red(),
        ))
        return

    if manageable_role(ctx.guild, role.id) is None:
        await ctx.send(view=simple_card(
            f"I can't manage {role.mention} — it's above me in the role list, "
            f"or I'm missing Manage Roles.",
            colour=discord.Color.red(),
        ), allowed_mentions=SILENT)
        return

    state = {"duplicate": False, "rule": None}

    def mutate(rules):
        for existing in rules:
            if (rule_role_id(existing) == role.id
                    and existing.get("type") == kind
                    and existing.get("value") == value):
                state["duplicate"] = True
                return None
        rule = {
            "id": secrets.token_hex(3),
            # Snowflakes must be strings: JSON numbers cannot represent all
            # 19 digits exactly and Roblox otherwise returns scientific notation.
            "role": str(role.id),
            "type": kind,
            "value": value,
        }
        state["rule"] = rule
        return rules + [rule]

    async with ctx.typing():
        result = await save_role_rules(ctx.guild.id, mutate)

    if state["duplicate"]:
        await ctx.send(view=simple_card("That exact rule already exists.",
                                        colour=discord.Color.greyple()))
        return
    if result != "ok":
        await ctx.send(view=simple_card(
            f"Couldn't save the rule ({result}). Check the logs.",
            colour=discord.Color.red(),
        ))
        return
    badge_names = await role_rule_badge_names([state["rule"]])
    await ctx.send(view=simple_card(
        describe_rule(
            state["rule"], ctx.guild, admin=True, badge_names=badge_names
        ),
        heading="✅ Rule added",
    ), allowed_mentions=SILENT)

@admin_roles.command(name="remove")
@is_admin()
async def admin_roles_remove(ctx, rule_id: str):
    """!roles remove <rule_id>"""
    wanted = rule_id.strip().lower()
    state = {"rule": None}

    def mutate(rules):
        for rule in rules:
            if rule.get("id") == wanted:
                state["rule"] = rule
                return [r for r in rules if r.get("id") != wanted]
        return None

    async with ctx.typing():
        result = await save_role_rules(ctx.guild.id, mutate)

    if state["rule"] is None:
        await ctx.send(view=simple_card(f"No rule with ID `{wanted}`.",
                                        colour=discord.Color.red()))
        return
    if result != "ok":
        await ctx.send(view=simple_card(
            f"Couldn't save the change ({result}). Check the logs.",
            colour=discord.Color.red(),
        ))
        return
    badge_names = await role_rule_badge_names([state["rule"]])
    await ctx.send(view=simple_card(
        describe_rule(
            state["rule"], ctx.guild, admin=True, badge_names=badge_names
        ),
        heading="🗑️ Rule removed",
        colour=discord.Color.orange(),
    ), allowed_mentions=SILENT)

@bot.command(name="testcup", hidden=True)
@commands.is_owner()
async def test_cup_announcement(ctx):
    """Owner-only preview of the scheduled Daily Cup messages."""
    async with ctx.typing():
        posted = await publish_daily_cup_announcement(ctx.channel)
    if not posted:
        await ctx.send(
            "Couldn't build the Daily Cup announcement. Check the bot logs "
            "for the missing Roblox data."
        )

def register_dev_variants():
    """Give every command a prefix-only !dev_ twin bound to DEV_UNIVERSE_ID.

    The twin reuses the original callback, so the two can never drift; only
    the active universe differs.
    """
    added = []
    for command in list(bot.commands):
        if command.name.startswith("dev_") or command.name == "help":
            continue

        def make(original_callback):
            @functools.wraps(original_callback)
            async def run_on_dev_universe(*args, **kwargs):
                token = _active_universe.set(DEV_UNIVERSE_ID)
                try:
                    # Mark the invocation so a dev run is never mistaken for
                    # a live one, whatever the command replies with.
                    ctx = args[0] if args else None
                    if isinstance(ctx, commands.Context):
                        try:
                            await ctx.message.add_reaction("🧪")
                        except discord.HTTPException:
                            pass
                    return await original_callback(*args, **kwargs)
                finally:
                    _active_universe.reset(token)
            return run_on_dev_universe

        twin = commands.Command(
            make(command.callback),
            name=f"dev_{command.name}",
            help=command.help,
            hidden=True,
            checks=[admin_only_predicate],
        )
        bot.add_command(twin)
        added.append(twin.name)

    print(f"Registered {len(added)} dev command(s) on universe {DEV_UNIVERSE_ID}")

register_dev_variants()

_original_close = bot.close

async def _close_with_session():
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    await _original_close()

bot.close = _close_with_session

bot.run(TOKEN)
