# main bot code will go here
import discord
from discord.ext import commands
import os

TOKEN = os.getenv("TOKEN") # we will put this in railway later

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')

@bot.command()
async def ping(ctx):
    await ctx.send("hello fuckers")

bot.run(TOKEN)
