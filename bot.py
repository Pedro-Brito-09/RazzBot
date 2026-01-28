import discord
from discord.ext import commands
import os
import aiohttp
import json
import base64
import zstandard as zstd
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
                return None
            data = await resp.json()
            value = data.get("value")
            if not value:
                return None
            if isinstance(value, dict) and value.get("t") == "buffer" and "zbase64" in value:
                return decode_buffer(value["zbase64"])
            return value

def compute_maps(submissions, todays_map):
    accepted = [s for s in submissions if isinstance(s, dict) and s.get("Status") == "Accepted"]
    if not accepted:
        return None, None
    accepted.sort(key=lambda x: x.get("Timestamp", 0))
    current_id = todays_map.get("Id") if todays_map else accepted[-1]["Id"]
    current_map = {"Id": current_id}
    ids = [s["Id"] for s in accepted]
    if current_id in ids:
        current_index = todays_map.get("Index")
        next_index = (current_index + 1) % len(ids)
        next_map = {"Id": ids[next_index]}
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
    code = code.upper()
    return ''.join(chr(127397 + ord(c)) for c in code)

def get_medal_emoji(pos):
    if pos == 0:
        return ":DiamondMedal:1466201150314385471:"
    elif pos == 1:
        return ":GoldMedal:1466201173877981449:"
    elif pos == 2:
        return ":SilverMedal:1466201197840044065:"
    elif pos == 3:
        return ":BronzeMedal:1466201227997089873:"
    else:
        return ""

async def get_lb_entry(leaderboard, pos):
    url = f"https://users.roblox.com/v1/users/{leaderboard[pos].UserId}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            return f"{get_medal_emoji(pos)}・{country_code_to_emoji(leaderboard[pos].Country)} {data.name}・{format_time(leaderboard[pos].Value)}"

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

@bot.command()
async def maps(ctx):
    submissions = await fetch_entry("Submissions")
    todays_map = await fetch_entry("TodaysMap") or {}
    if not submissions:
        await ctx.send("Failed to fetch submissions from Roblox cloud.")
        return
    current_map, next_map = compute_maps(submissions, todays_map)
    if not current_map or not next_map:
        await ctx.send("No accepted maps found.")
        return
    leaderboard = await fetch_entry(f"DailyCup_{todays_map.get("Index")}", datastore="Leaderboards")
    all_maps_info = await fetch_entry("Ids", datastore="Community Maps")
    map_info = list(m for m in all_maps_info if m.get("Id") == next_map.get("Id"))[0]
    
    msg = (
        f"Current map ID: {current_map['Id']}\n"
        f"Next map ID: {next_map['Id']}\n"
    )

    if leaderboard:
        lb_desc = ""
        lb_embed = discord.Embed(
            title = f"Leaderboard - {get_todays_date()}",
            description = f"{await get_lb_entry(leaderboard, 0)}\n{await get_lb_entry(leaderboard, 1)}\n{await get_lb_entry(leaderboard, 2)}\n{await get_lb_entry(leaderboard, 3)}",
            color = discord.Color.purple()
        )
    
        await ctx.send(embed = lb_embed)

@bot.command()
async def ping(ctx):
    await ctx.send("hello fuckers")

bot.run(TOKEN)
