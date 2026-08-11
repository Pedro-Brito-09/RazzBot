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

LEADERBOARD_COLOR = discord.Color.purple()
MAX_LEADERBOARD_ROWS = 10
# Long names get truncated so the table keeps its columns.
MAX_NAME_WIDTH = 18

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

def format_position(pos):
    return f"`#{pos + 1}`"

def resolve_medal(entry, pos):
    """The medal for a row. Single place to change when the rule changes.

    Currently awarded purely by finishing position; takes the whole entry so
    a value based rule can be dropped in without touching the renderers.
    """
    return get_medal_emoji(pos)

async def collect_leaderboard_rows(leaderboard, limit):
    """Resolve a leaderboard into plain row dicts, ready for either renderer."""
    rows = []
    for pos in range(min(len(leaderboard), limit)):
        entry = leaderboard[pos]
        if not isinstance(entry, dict):
            continue

        user_id = entry.get("UserId")
        name = await fetch_username(user_id) if user_id is not None else None
        if not name:
            # A failed username lookup shouldn't drop the whole row.
            name = f"User {user_id}" if user_id is not None else "Unknown"

        country = entry.get("Country")
        country = country.strip().upper()[:2] if isinstance(country, str) else ""

        value = entry.get("Value")
        rows.append({
            "pos": pos,
            "name": name,
            "country": country,
            "time": format_time(value) if isinstance(value, (int, float)) else "--:--.---",
            "medal": resolve_medal(entry, pos),
        })
    return rows

def render_leaderboard_table(rows, *, show_country=True):
    """Monospace table. Columns align because a code block is fixed width.

    Custom emoji do not render inside code blocks, so ranks are numbers and
    countries are their two letter codes.
    """
    name_w = min(max(len(r["name"]) for r in rows), MAX_NAME_WIDTH)
    name_w = max(name_w, len("PLAYER"))

    header = f"{'#':>2}  "
    if show_country:
        header += f"{'CC':<2}  "
    header += f"{'PLAYER':<{name_w}}  {'TIME':>9}"

    lines = [header, "-" * len(header)]
    for r in rows:
        name = r["name"]
        if len(name) > name_w:
            name = name[:name_w - 1] + "."
        line = f"{r['pos'] + 1:>2}  "
        if show_country:
            line += f"{r['country'] or '--':<2}  "
        line += f"{name:<{name_w}}  {r['time']:>9}"
        lines.append(line)

    return "```\n" + "\n".join(lines) + "\n```"

def render_leaderboard_fields(rows, *, show_country=True, show_medals=True):
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
        times.append(f"{medal} `{r['time']}`" if medal else f"`{r['time']}`")

    return [
        ("Player", "\n".join(players), True),
        ("Time", "\n".join(times), True),
    ]

def render_leaderboard_list(rows, *, show_country=True, show_medals=True):
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
        tail = f"{medal} `{r['time']}`" if medal else f"`{r['time']}`"
        lines.append(f"{' '.join(parts)} - {tail}")
    return "\n".join(lines)

async def build_leaderboard_embed(
    leaderboard,
    *,
    title,
    subtitle=None,
    limit=MAX_LEADERBOARD_ROWS,
    style="fields",
    show_medals=True,
    show_country=True,
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

    rows = await collect_leaderboard_rows(leaderboard, limit)
    if not rows:
        return None

    description = None
    fields = None
    if style == "fields":
        fields = render_leaderboard_fields(
            rows, show_country=show_country, show_medals=show_medals
        )
    elif style == "table":
        description = render_leaderboard_table(rows, show_country=show_country)
    else:
        description = render_leaderboard_list(
            rows, show_country=show_country, show_medals=show_medals
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

    total = len(leaderboard)
    if total > len(rows):
        embed.set_footer(text=f"Top {len(rows)} of {total}")
    else:
        embed.set_footer(text=f"{total} {'entry' if total == 1 else 'entries'}")
    return embed

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
async def cup(ctx):
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

    lb_embed = await build_leaderboard_embed(
        leaderboard,
        title=f"🏆 Leaderboard — {get_todays_date()}",
        subtitle=f"Daily Cup #{index}",
    )
    if lb_embed is None:
        await ctx.send("The leaderboard came back empty.")
        return

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
