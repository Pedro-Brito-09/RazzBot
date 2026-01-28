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

async def fetch_entry(entry_key):
    url = (
        f"https://apis.roblox.com/cloud/v2/universes/8993151589/"
        f"data-stores/Daily%20Cup%20Submissions/entries/{entry_key}"
    )
    headers = {"x-api-key": API_KEY, "Accept": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                print(f"Failed fetching {entry_key}: {resp.status}")
                text = await resp.text()
                print(f"Response: {text[:500]}")
                return None

            data = await resp.json()
            value = data.get("value")
            if not value:
                return None

            if isinstance(value, dict) and value.get("t") == "buffer" and "zbase64" in value:
                compressed = base64.b64decode(value["zbase64"])
                try:
                    dctx = zstd.ZstdDecompressor()
                    decompressed = dctx.decompress(compressed)
                    return json.loads(decompressed.decode("utf-8"))
                except Exception as e:
                    print(f"Error decoding entry '{entry_key}': {e}")
                    return None

            return value

def compute_maps(submissions, todays_map):
    # Sort submissions by Timestamp (oldest → newest)
    submissions_sorted = sorted(
        submissions, key=lambda s: s.get("Timestamp", 0)
    )

    # Filter only accepted submissions
    accepted = [s for s in submissions_sorted if isinstance(s, dict) and s.get("Status") == "Accepted"]

    if not accepted:
        return None, None

    current_id = todays_map.get("Id") if todays_map else accepted[0]["Id"]
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

    await ctx.send(
        f"Current map ID: {current_map['Id']}\n"
        f"Next map ID: {next_map['Id']}"
    )

@bot.command()
async def ping(ctx):
    await ctx.send("hello fuckers")

bot.run(TOKEN)
