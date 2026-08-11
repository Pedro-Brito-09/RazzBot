import discord
from discord.ext import commands
import os
import aiohttp
import json
import base64
import zstandard as zstd
import traceback
from datetime import datetime, timedelta, timezone

TOKEN = os.getenv("TOKEN")
API_KEY = os.getenv("API_KEY")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

def decode_buffer(value):
    compressed = base64.b64decode(value)
    dctx = zstd.ZstdDecompressor()
    decoded_bytes = dctx.decompress(compressed)
    try:
        return json.loads(decoded_bytes)
    except Exception:
        return decoded_bytes

async def fetch_entry(entry_key, datastore="Daily Cup Submissions"):
    url = (
        f"https://apis.roblox.com/cloud/v2/universes/8993151589/"
        f"data-stores/{datastore}/entries/{entry_key}"
    )
    headers = {"x-api-key": API_KEY, "Accept": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                print(f"fetch_entry({datastore}/{entry_key}) -> HTTP {resp.status}")
                return None
            data = await resp.json()
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
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                print(f"fetch_username({user_id}) -> HTTP {resp.status}")
                return None
            data = await resp.json()
            return data.get("name")

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
    print(f"Logged in as {bot.user}")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    original = getattr(error, "original", error)
    traceback.print_exception(type(original), original, original.__traceback__)
    await ctx.send(f"Something broke running that command: `{type(original).__name__}: {original}`")

@bot.command()
async def maps(ctx):
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

@bot.command()
async def ping(ctx):
    await ctx.send("hello fuckers")

bot.run(TOKEN)
