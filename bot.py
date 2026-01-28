import discord
from discord.ext import commands
import os
import aiohttp
import json
import base64

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
                return None
            data = await resp.json()
            value = data.get("value")
            if not value:
                return None
            if isinstance(value, dict) and value.get("t") == "buffer" and "zbase64" in value:
                decoded = base64.b64decode(value["zbase64"])
                try:
                    return json.loads(decoded)
                except:
                    return decoded
            return value

def compute_maps(submissions, todays_map):
    accepted = [s for s in submissions if isinstance(s, dict) and s.get("Status") == "Accepted"]
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
    
    # see what submissions actually look like TEMPORARY
    await ctx.send(f"Submissions type: {type(submissions)}")
    await ctx.send(f"Submissions sample: {submissions[:5] if isinstance(submissions, list) else submissions}")

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
