import discord
from discord.ext import commands
import os
import aiohttp
import json
import base64
import zstandard as zstd
import traceback
import asyncio
import io
import socket
from urllib.parse import quote
from datetime import datetime, timedelta, timezone

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

TOKEN = os.getenv("TOKEN")
API_KEY = os.getenv("API_KEY")
# Optional: syncing to one guild makes slash commands appear instantly.
# A global sync (no GUILD_ID) can take up to an hour to propagate.
GUILD_ID = os.getenv("GUILD_ID")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

_synced = False
_session = None

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

async def request_json(url, headers=None, label=""):
    """GET JSON with retries. Returns None on any failure, never raises."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            session = await get_session()
            async with session.get(url, headers=headers) as resp:
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
        f"https://apis.roblox.com/cloud/v2/universes/8993151589/"
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

def compute_maps(submissions, todays_map):
    accepted = [
        s for s in submissions
        if isinstance(s, dict) and s.get("Status") == "Accepted" and s.get("Id") is not None
    ]
    if not accepted:
        return None, None
    accepted.sort(key=lambda x: x.get("Timestamp", 0))
    ids = [s["Id"] for s in accepted]

    current_id = todays_map.get("Id") if todays_map else None
    if current_id is None:
        current_id = ids[-1]
    current_map = {"Id": current_id}

    if current_id in ids:
        # TodaysMap carries its own Index; fall back to the map's position in the
        # rotation when it's missing, so a partial entry can't crash the command.
        current_index = todays_map.get("Index")
        if not isinstance(current_index, int):
            current_index = ids.index(current_id)
        next_map = {"Id": ids[(current_index + 1) % len(ids)]}
    else:
        next_map = {"Id": ids[0]}
    return current_map, next_map

def get_todays_date():
    now = datetime.now(timezone.utc)
    shifted = now - timedelta(hours=9)

    return shifted.strftime("%d/%m/%Y")

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

async def fetch_username(user_id):
    url = f"https://users.roblox.com/v1/users/{user_id}"
    data = await request_json(url, label=f"fetch_username({user_id})")
    return data.get("name") if data else None

async def get_lb_entry(leaderboard, pos):
    entry = leaderboard[pos]
    if not isinstance(entry, dict):
        return None

    user_id = entry.get("UserId")
    value = entry.get("Value")

    name = await fetch_username(user_id) if user_id is not None else None
    if not name:
        # A failed username lookup shouldn't drop the whole row.
        name = f"User {user_id}" if user_id is not None else "Unknown"

    medal = get_medal_emoji(pos)
    flag = country_code_to_emoji(entry.get("Country"))
    time_text = format_time(value) if isinstance(value, (int, float)) else "--:--.---"

    prefix = f"{medal}・" if medal else ""
    flag_text = f"{flag} " if flag else ""
    return f"{prefix}{flag_text}{name}・{time_text}"

@bot.event
async def on_ready():
    global _synced
    print(f"Logged in as {bot.user}")

    # on_ready fires again on every reconnect; only sync once per process.
    if _synced:
        return
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(synced)} slash command(s) to guild {GUILD_ID}")
        else:
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} slash command(s) globally "
                  f"(may take up to an hour to show up)")
        _synced = True
    except Exception:
        print("Slash command sync failed:")
        traceback.print_exc()

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

@bot.hybrid_command(description="Show the current and next daily cup map, plus today's leaderboard")
async def maps(ctx):
    # The Roblox lookups take longer than the 3s interaction deadline.
    await ctx.defer()

    submissions = await fetch_entry("Submissions")
    if not submissions:
        await ctx.send("Failed to fetch submissions from Roblox cloud.")
        return

    todays_map = await fetch_entry("TodaysMap") or {}

    current_map, next_map = compute_maps(submissions, todays_map)
    if not current_map or not next_map:
        await ctx.send("No accepted maps found.")
        return

    await ctx.send(
        f"Current map ID: {current_map['Id']}\n"
        f"Next map ID: {next_map['Id']}"
    )

    index = todays_map.get("Index")
    if index is None:
        await ctx.send("No daily cup index set, so there's no leaderboard to show.")
        return

    leaderboard = await fetch_entry(f"DailyCup_{index}", datastore="Leaderboards")
    if not leaderboard:
        await ctx.send(f"No leaderboard found for `DailyCup_{index}`.")
        return

    entries = []
    for pos in range(min(len(leaderboard), 4)):
        line = await get_lb_entry(leaderboard, pos)
        if line:
            entries.append(line)

    if not entries:
        await ctx.send("The leaderboard came back empty.")
        return

    lb_embed = discord.Embed(
        title=f"Leaderboard - {get_todays_date()}",
        description="\n".join(entries),
        color=discord.Color.purple()
    )
    await ctx.send(embed=lb_embed)

@bot.hybrid_command(description="Check that the bot is alive")
async def ping(ctx):
    await ctx.send("hello fuckers")

_original_close = bot.close

async def _close_with_session():
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    await _original_close()

bot.close = _close_with_session

bot.run(TOKEN)
