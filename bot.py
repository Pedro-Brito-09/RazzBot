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
    allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
    allowed_contexts=app_commands.AppCommandContext(
        guild=True, dm_channel=True, private_channel=True
    ),
)

_synced = False
_session = None
_account_link_lock = asyncio.Lock()
_link_code_expirations = {}
_link_code_tasks = {}

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

def decode_buffer(value):
    compressed = base64.b64decode(value)
    dctx = zstd.ZstdDecompressor()
    try:
        decoded_bytes = dctx.decompress(compressed)
    except zstd.ZstdError:
        # Frames written without the content size in the header can't be
        # decompressed one-shot; stream them instead.
        decoded_bytes = dctx.stream_reader(io.BytesIO(compressed)).read()
    try:
        return json.loads(decoded_bytes)
    except Exception:
        return decoded_bytes

async def fetch_entry(entry_key, datastore="Daily Cup Submissions"):
    # Datastore names contain spaces ("Daily Cup Submissions"), so encode
    # both segments rather than relying on the client to fix up the URL.
    url = (
        f"https://apis.roblox.com/cloud/v2/universes/{UNIVERSE_ID}/"
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
    if isinstance(value, dict) and value.get("t") == "buffer" and "zbase64" in value:
        return decode_buffer(value["zbase64"])
    return value

def user_restriction_url(user_id):
    return (
        f"https://apis.roblox.com/cloud/v2/universes/{UNIVERSE_ID}"
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

    reason = restriction.get("displayReason") or restriction.get("privateReason")
    if reason:
        line += f"\n-# {reason}"
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
        f"https://apis.roblox.com/cloud/v2/universes/{UNIVERSE_ID}/"
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

def data_store_entry_url(entry_key, datastore):
    return (
        f"https://apis.roblox.com/cloud/v2/universes/{UNIVERSE_ID}/"
        f"data-stores/{quote(datastore, safe='')}/entries/"
        f"{quote(str(entry_key), safe='')}"
    )

async def fetch_entry_resource(entry_key, datastore):
    """Fetch the full Open Cloud entry, including its concurrency ETag."""
    url = data_store_entry_url(entry_key, datastore)
    headers = {"x-api-key": API_KEY, "Accept": "application/json"}
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
    entry_key, datastore, value, *, etag=None, allow_missing=False
):
    """Update an existing entry without silently replacing a newer version."""
    url = data_store_entry_url(entry_key, datastore)
    if allow_missing:
        url += "?allowMissing=true"
    headers = {
        "x-api-key": API_KEY,
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
    if isinstance(value, dict) and value.get("t") == "buffer" and "zbase64" in value:
        return decode_buffer(value["zbase64"])
    return value

def encode_entry_value(original, new_value):
    """Re-encode a value in whatever envelope the entry already used.

    Community Maps and the leaderboards are stored as zstd-compressed
    buffers, so writing plain JSON back would make them unreadable.
    """
    if isinstance(original, dict) and original.get("t") == "buffer":
        raw = json.dumps(new_value, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
        compressed = zstd.ZstdCompressor().compress(raw)
        envelope = {
            "t": "buffer",
            "zbase64": base64.b64encode(compressed).decode("ascii"),
        }
        # Open Cloud rejects the whole write when "m" is null, so carry it
        # over only when the entry actually has one.
        if original.get("m") is not None:
            envelope["m"] = original["m"]
        return envelope
    return new_value

async def update_entry_with_retry(entry_key, datastore, transform, *, attempts=4):
    """Read-modify-write guarded by the ETag -- Open Cloud's UpdateAsync.

    transform(decoded_value) returns the new value, or None to leave the
    entry alone. Returns "ok", "skipped", "conflict", "missing" or "error".
    """
    for attempt in range(1, attempts + 1):
        status, resource = await fetch_entry_resource(entry_key, datastore)
        if status != "ok":
            return status

        raw_value = resource.get("value")
        new_value = transform(decode_entry_value(raw_value))
        if new_value is None:
            return "skipped"

        result = await update_entry_resource(
            entry_key, datastore,
            encode_entry_value(raw_value, new_value),
            etag=resource.get("etag"),
        )
        if result != "conflict":
            return result

        # The game wrote underneath us; re-read and reapply.
        print(f"update_entry_with_retry({datastore}/{entry_key}) "
              f"conflict on attempt {attempt}")
        await asyncio.sleep(0.5 * attempt)
    return "conflict"

async def delete_entry_resource(entry_key, datastore):
    """Delete one datastore entry. True when it's gone."""
    url = data_store_entry_url(entry_key, datastore)
    headers = {"x-api-key": API_KEY, "Accept": "application/json"}
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
    _maps_cache["by_id"] = None
    _maps_cache["at"] = 0.0

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
                "Codes", ACCOUNT_LINK_DATASTORE
            )
            if fetch_status == "missing":
                return "absent"
            if fetch_status == "error":
                return "error"

            codes = decode_entry_value(resource.get("value"))
            if not isinstance(codes, dict):
                return "absent"
            if str(codes.get(code)) != str(account_id):
                return "absent"

            del codes[code]
            result = await update_entry_resource(
                "Codes", ACCOUNT_LINK_DATASTORE, codes,
                etag=resource.get("etag"),
            )
            if result == "ok":
                return "deleted"
            if result != "conflict":
                return "error"

        return "error"

async def expire_link_code(code, account_id, expires_at):
    delay = max(0, expires_at - time.time())
    await asyncio.sleep(delay)

    # Keep retrying temporary Open Cloud failures after expiry. The guarded
    # delete cannot remove a replacement code or another user's code.
    while True:
        result = await delete_link_code(code, account_id)
        if result in ("deleted", "absent"):
            break
        await asyncio.sleep(30)

    _link_code_expirations.pop(code, None)
    _link_code_tasks.pop(code, None)

def ensure_link_code_expiry(code, account_id, expires_at=None):
    """Return a code's Unix expiry and ensure its cleanup task is running."""
    existing_expiry = _link_code_expirations.get(code)
    if existing_expiry is not None:
        return existing_expiry

    expires_at = int(expires_at or (time.time() + LINK_CODE_TTL))
    _link_code_expirations[code] = expires_at
    _link_code_tasks[code] = asyncio.create_task(
        expire_link_code(code, account_id, expires_at)
    )
    return expires_at

def cancel_link_code_expiry(code):
    """Cancel and forget a code's scheduled cleanup after verification."""
    _link_code_expirations.pop(code, None)
    task = _link_code_tasks.pop(code, None)
    if task is not None and not task.done():
        task.cancel()

async def create_link_code(account_id, *, previous_code=None):
    """Add or reuse a code; returns (status, code, Unix expiry)."""
    async with _account_link_lock:
        for _ in range(MAX_ATTEMPTS):
            fetch_status, resource = await fetch_entry_resource(
                "Codes", ACCOUNT_LINK_DATASTORE
            )
            if fetch_status == "error":
                return "error", None, None

            resource = resource or {}
            codes = decode_entry_value(resource.get("value"))
            if not isinstance(codes, dict):
                codes = {}

            active_code = find_account_code(codes, account_id)
            if previous_code is not None:
                if previous_code in codes:
                    expires_at = ensure_link_code_expiry(
                        previous_code, account_id
                    )
                    return "pending", previous_code, expires_at
                if active_code is not None:
                    expires_at = ensure_link_code_expiry(active_code, account_id)
                    return "active", active_code, expires_at
            elif active_code is not None:
                expires_at = ensure_link_code_expiry(active_code, account_id)
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
                "Codes", ACCOUNT_LINK_DATASTORE, codes,
                etag=resource.get("etag"),
                allow_missing=fetch_status == "missing",
            )
            if result == "ok":
                expires_at = ensure_link_code_expiry(code, account_id)
                return "created", code, expires_at
            if result != "conflict":
                return "error", None, None

        return "error", None, None

async def fetch_linked_user_id(account_id):
    linked = await fetch_entry("Linked", datastore=ACCOUNT_LINK_DATASTORE)
    if not isinstance(linked, dict):
        return None
    user_id = linked.get(str(account_id))
    try:
        return int(user_id)
    except (TypeError, ValueError):
        return None

_owned_badges_cache = {}

async def fetch_universe_badges():
    """Every badge the game defines. Public, no auth needed."""
    badges, cursor = [], None
    while True:
        url = (f"https://badges.roblox.com/v1/universes/{UNIVERSE_ID}/badges"
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
    cached = _owned_badges_cache.get(user_id)
    if cached and now - cached[0] < OWNED_BADGES_CACHE_TTL:
        return cached[1]

    semaphore = asyncio.Semaphore(BADGE_CONCURRENCY)
    results = await asyncio.gather(*(
        _badge_is_owned(user_id, badge_id, semaphore) for badge_id in badge_ids
    ))
    owned = {badge_id for badge_id, has in results if has}
    _owned_badges_cache[user_id] = (now, owned)
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
        f"https://apis.roblox.com/cloud/v2/universes/{UNIVERSE_ID}/"
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
        f"https://apis.roblox.com/cloud/v2/universes/{UNIVERSE_ID}/"
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

_maps_cache = {"at": 0.0, "by_id": None}

async def get_community_maps():
    """Community maps indexed by Id, cached for MAPS_CACHE_TTL seconds.

    On a failed refresh the previous index is kept and returned, so a blip
    upstream doesn't take the command down.
    """
    now = time.monotonic()
    cached = _maps_cache["by_id"]
    if cached and now - _maps_cache["at"] < MAPS_CACHE_TTL:
        return cached

    entries = await fetch_entry("Ids", datastore="Community Maps")
    if not entries:
        return cached

    by_id = {}
    for e in entries:
        if isinstance(e, dict) and e.get("Id") is not None:
            by_id[e["Id"]] = {k: e.get(k) for k in MAP_FIELDS}

    _maps_cache["by_id"] = by_id
    _maps_cache["at"] = now
    print(f"community maps indexed: {len(by_id)} entries")
    return by_id

class ProfileView(discord.ui.LayoutView):
    """Components V2 profile card built from a Main_Data entry."""

    def __init__(self, entry, *, user_id, username, headshot=None, wins=None,
                 restriction=None, timeout=MAP_VIEW_TIMEOUT):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.username = username
        self.message = None

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

    async def show_badges(self, interaction):
        await interaction.response.defer(thinking=True)

        view = await build_badges_view(self.user_id, self.username)
        if view is None:
            await interaction.followup.send(
                "Couldn't fetch the game's badge list.", ephemeral=True
            )
            return

        await interaction.followup.send(view=view)

    async def show_created_maps(self, interaction):
        await interaction.response.defer(thinking=True)

        view = await build_created_maps_view(self.user_id, self.username)
        if view is None:
            await interaction.followup.send(
                f"**{self.username}** has no public maps...", ephemeral=True
            )
            return

        await interaction.followup.send(view=view)

    async def on_timeout(self):
        for item in self.walk_children():
            if isinstance(item, discord.ui.Button) and item.style is not discord.ButtonStyle.link:
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

async def build_badges_view(user_id, username):
    """Every game badge, marked owned or not for this player."""
    badges = await fetch_universe_badges()
    if not badges:
        return None

    owned_ids = await fetch_owned_badge_ids(user_id, [b["id"] for b in badges])
    owned = [b for b in badges if b["id"] in owned_ids]

    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=PROFILE_COLOR)
    container.add_item(discord.ui.TextDisplay(
        f"## 🎖️ Badges — {username}\n-# {len(owned)} of {len(badges)} earned"
    ))

    if not owned:
        container.add_item(discord.ui.TextDisplay("-# No badges earned yet."))
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

    view.add_item(container)
    return view

async def build_created_maps_view(user_id, username, limit=10):
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

    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=MAP_COLOR)
    container.add_item(discord.ui.TextDisplay(
        f"## 🗺️ Maps by {username}\n"
        f"-# {len(owned)} public map{'s' if len(owned) != 1 else ''}"
    ))
    container.add_item(discord.ui.Separator())

    lines = []
    for m in owned[:limit]:
        name = m.get("Name") or "Unnamed Map"
        star = " 🌟" if m.get("Featured") else ""
        lines.append(
            f"`#{m.get('Id')}` **{name}**{star}\n"
            f"-# ▶️ {format_number(m.get('Plays') or 0)} plays"
            f"  ·  ⭐ {format_number(m.get('Favorites') or 0)}"
        )
    container.add_item(discord.ui.TextDisplay("\n".join(lines)))

    if len(owned) > limit:
        container.add_item(discord.ui.TextDisplay(
            f"-# and {len(owned) - limit} more"
        ))

    view.add_item(container)
    return view

async def build_profile_view(user_id, username):
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
        headshot=headshot, wins=wins, restriction=restriction,
    )

class MapView(discord.ui.LayoutView):
    """Components V2 map card with configurable action buttons."""

    def __init__(self, entry, *, headshot=None, creator_text="Unknown",
                 creator_id=None, creator_name=None, timeout=MAP_VIEW_TIMEOUT,
                 play_label="Play in Challenge Mode",
                 play_url_template=PLAY_URL_TEMPLATE,
                 show_leaderboard=True, show_creator=True):
        super().__init__(timeout=timeout)
        self.entry = entry
        self.message = None
        self.creator_id = creator_id
        self.creator_name = creator_name

        map_id = entry.get("Id")
        name = entry.get("Name") or "Unnamed Map"
        if entry.get("Featured"):
            name = f"{name} 🌟"

        plays = entry.get("Plays") or 0
        favorites = entry.get("Favorites") or 0
        playstyle = (entry.get("Playstyle") or "Unknown").upper()
        privacy = entry.get("Privacy")

        heading = f"## {name}\n-# by {creator_text}"

        container = discord.ui.Container(accent_colour=MAP_COLOR)
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

        # A separate message, so an embed is fine even though the map card
        # it came from is Components V2.
        await interaction.followup.send(embed=embed)

    async def show_creator_profile(self, interaction):
        await interaction.response.defer(thinking=True)

        name = self.creator_name or str(self.creator_id)
        view = await build_profile_view(self.creator_id, name)
        if view is None:
            await interaction.followup.send(
                f"No game data saved for **{name}**.", ephemeral=True
            )
            return

        await interaction.followup.send(view=view)

    async def on_timeout(self):
        for item in self.children:
            if item.style is not discord.ButtonStyle.link:
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

class DailyCupMapView(discord.ui.View):
    """The Daily Cup card's single experience Play action."""

    def __init__(self):
        super().__init__(timeout=None)
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

@bot.event
async def on_ready():
    global _synced
    print(f"Logged in as {bot.user}")

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

class AccountLinkView(discord.ui.View):
    def __init__(self, account_id, code, expires_at):
        super().__init__(timeout=900)
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
        user_id = await fetch_linked_user_id(self.account_id)
        if user_id is None:
            await interaction.followup.send(
                "Verification has not completed yet. Enter the code in the "
                "verification game, then try again.",
                ephemeral=True,
            )
            return

        cancel_link_code_expiry(self.code)
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
            cancel_link_code_expiry(old_code)
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

@bot.hybrid_command(name="map", description="Show info about a community map by its ID")
@app_commands.describe(map_id="The community map ID")
async def map_command(ctx, map_id: int):
    await ctx.defer()

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
    if creator:
        creator_text = f"@{creator}"
    elif creator_id:
        creator_text = f"User {creator_id}"
    else:
        creator_text = "Unknown"

    headshot = None
    if creator_id:
        headshot = (await fetch_headshots([creator_id])).get(creator_id)

    view = MapView(
        entry, headshot=headshot, creator_text=creator_text,
        creator_id=creator_id, creator_name=creator,
    )
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

@bot.hybrid_command(description="Check that the bot is alive")
async def ping(ctx):
    await ctx.send("hello fuckers")

def is_admin():
    """Prefix-command gate. Stays silent for everyone else."""
    async def predicate(ctx):
        return ctx.author.id == ADMIN_USER_ID
    return commands.check(predicate)

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
    """!ban <player> [duration] [reason] -- duration like 7d, 12h, or perm."""
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
    """!unban <player>"""
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
    await ctx.send(f"{icon} **{state['name']}** (`{map_id}`) is now {word}.")

@bot.command(name="feature", hidden=True)
@is_admin()
async def admin_feature(ctx, map_id: int):
    """!feature <map_id>"""
    await set_map_featured(ctx, map_id, True)

@bot.command(name="unfeature", hidden=True)
@is_admin()
async def admin_unfeature(ctx, map_id: int):
    """!unfeature <map_id>"""
    await set_map_featured(ctx, map_id, False)

@bot.command(name="deletemap", hidden=True)
@is_admin()
async def admin_delete_map(ctx, map_id: int, confirm: str = ""):
    """!deletemap <map_id> confirm -- removes it from the index and its key."""
    if confirm.strip().lower() != "confirm":
        await ctx.send(
            f"This permanently removes map `{map_id}` from the index of "
            f"~85k maps and deletes its entry. There is no undo.\n"
            f"Run `!deletemap {map_id} confirm` if you're sure."
        )
        return

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

    async with ctx.typing():
        result = await update_entry_with_retry("Ids", "Community Maps", transform)

        if not state["found"]:
            await ctx.send(f"No community map with ID `{map_id}`.")
            return
        if result != "ok":
            await ctx.send(
                f"Failed to remove `{map_id}` from the index ({result}). "
                f"Nothing was deleted — check the logs."
            )
            return

        invalidate_maps_cache()
        # Only touch the map's own entry once the index write succeeded.
        key_deleted = await delete_entry_resource(str(map_id), "Community Maps")

    lines = [f"🗑️ Removed **{state['name']}** (`{map_id}`) from the map index."]
    lines.append(
        "-# Its own entry was deleted." if key_deleted
        else f"-# ⚠️ The index was updated but deleting key `{map_id}` failed."
    )
    await ctx.send("\n".join(lines))

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

_original_close = bot.close

async def _close_with_session():
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    await _original_close()

bot.close = _close_with_session

bot.run(TOKEN)
