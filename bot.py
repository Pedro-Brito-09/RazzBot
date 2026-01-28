import discord
from discord.ext import commands
import os
import aiohttp
import json
import base64
import zstandard as zstd

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
        current_index = ids.index(current_id)
        next_index = (current_index + 1) % len(ids)
        next_map = {"Id": ids[next_index]}
    else:
        next_map = {"Id": ids[0]}
    return current_map, next_map

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
    leaderboard = await fetch_entry(f"DailyCup_{current_map['Id']}", datastore="Leaderboards")
    next_map_info = await fetch_entry(str(next_map['Id']), datastore="Community Maps")
    msg = (
        f"Current map ID: {current_map['Id']}\n"
        f"Next map ID: {next_map['Id']}\n"
    )
    if leaderboard:
        msg += f"Leaderboard sample: {leaderboard[:5]}\n"
    if next_map_info:
        msg += f"Next map info: {next_map_info}\n"
    await ctx.send(msg)

@bot.command()
async def ping(ctx):
    await ctx.send("hello fuckers")

bot.run(TOKEN)
