# no i didnt vibe code this i just wanna organize it a bit
# imports
import discord
from discord.ext import commands
import os
import aiohttp
import json
from datetime import datetime

# railway variable stuff
TOKEN = os.getenv("TOKEN")
API_KEY = os.getenv("API_KEY")
UNIVERSE = "8993151589"
DATASTORE_NAME = "Daily Cup Submissions"
SCOPE = "global"

# intents and bot definition
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents) # bot MUST use ! prefix

async def fetch_entry(entry_key):
   url = (
        f"https://apis.roblox.com/datastores/v1/universes/{UNIVERSE_ID}"
        f"/standard-datastores/{DATASTORE_NAME}/entries/entry"
        f"?scope={SCOPE}&entryKey={entry_key}"
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
            return json.loads(value)

def compute_maps(submissions, todays_map):
    accepted = [s for s in submissions if s.get("Status") == "Accepted"]
    accepted.sort(key=lambda x: x["Timestamp"])
    if not accepted:
        return None, None
    current_index = todays_map.get("Index", 0) if todays_map else 0
    next_index = (current_index + 1) % len(accepted)
    current_map = {
        "Date": datetime.utcnow().strftime("%Y-%m-%d"),
        "Id": accepted[current_index]["Id"],
        "Index": current_index
    }
    next_map = {
        "Date": datetime.utcnow().strftime("%Y-%m-%d"),
        "Id": accepted[next_index]["Id"],
        "Index": next_index
    }
    return current_map, next_map


# bot functions
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')

@bot.command()
async def maps(ctx):
    submissions = await fetch_entry("Submissions")
    todays_map = await fetch_entry("TodaysMap")
    if submissions is None:
        await ctx.send("Failed to fetch submissions from Roblox cloud.")
        return
    current_map, next_map = compute_maps(submissions, todays_map)
    if not current_map or not next_map:
        await ctx.send("No accepted maps found.")
        return
    await ctx.send(
        f"Current map ID: {current_map['Id']} (Index: {current_map['Index']})\n"
        f"Next map ID: {next_map['Id']} (Index: {next_map['Index']})"
    )

@bot.command()
async def ping(ctx):
    await ctx.send("hello fuckers")

bot.run(TOKEN)
