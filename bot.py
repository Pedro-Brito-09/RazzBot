import discord
from discord import app_commands
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
import time
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

# Twemoji's trophy gold, so the bar matches the 🏆 in the title.
LEADERBOARD_COLOR = discord.Color(0xFFCC4D)
MAP_COLOR = discord.Color.blurple()
PROFILE_COLOR = discord.Color(0x9B59B6)
MAX_LEADERBOARD_ROWS = 10
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

# TODO placeholder: swap for the real deeplink once the format is known.
PLAY_URL_TEMPLATE = "https://www.roblox.com/games/0?mapId={id}"
# A community map's leaderboard is keyed by the map ID on its own.
MAP_LEADERBOARD_KEY = "{id}"
# How long the Leaderboard button stays clickable.
MAP_VIEW_TIMEOUT = 900
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

async def request_json(url, headers=None, label="", json_body=None):
    """Fetch JSON with retries. POSTs when json_body is given, else GETs.

    Returns None on any failure, never raises.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            session = await get_session()
            if json_body is not None:
                request = session.post(url, headers=headers, json=json_body)
            else:
                request = session.get(url, headers=headers)
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

async def fetch_ordered_entry(entry_key, datastore="Data", scope="Wins"):
    """Read one entry from an ordered datastore, which has its own endpoint.

    Returns the integer value, or None. int64 fields often arrive as strings
    in JSON, so the value is coerced.
    """
    url = (
        f"https://apis.roblox.com/cloud/v2/universes/8993151589/"
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

def format_position(pos):
    return f"`#{pos + 1}`"

def resolve_medal(entry, pos):
    """The medal for a row. Single place to change when the rule changes.

    Currently awarded purely by finishing position; takes the whole entry so
    a value based rule can be dropped in without touching the renderers.
    """
    return get_medal_emoji(pos)

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
            "user_id": user_id,
            "name": name,
            "country": country,
            "time": format_time(value) if isinstance(value, (int, float)) else "--:--.---",
            "medal": resolve_medal(entry, pos),
        })
    return rows

def render_leaderboard_table(rows, *, show_country=True, show_medals=False):
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
    header += f"{'TIME':>9}"

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
        line += f"{r['time']:>9}"
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
                 timeout=MAP_VIEW_TIMEOUT):
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

        container = discord.ui.Container(accent_colour=PROFILE_COLOR)
        if headshot:
            container.add_item(discord.ui.Section(
                discord.ui.TextDisplay(heading),
                accessory=discord.ui.Thumbnail(media=headshot),
            ))
        else:
            container.add_item(discord.ui.TextDisplay(heading))

        # Buttons sit directly under the equipped tag/skin line.
        row = discord.ui.ActionRow()
        maps_button = discord.ui.Button(
            label="Created maps",
            style=discord.ButtonStyle.secondary,
            emoji="🗺️",
        )
        maps_button.callback = self.show_created_maps
        row.add_item(maps_button)
        container.add_item(row)

        container.add_item(discord.ui.Separator())

        stars = format_number(data.get("Stars") or 0)
        all_time = format_number(dig(data, "Stats", "AllTimeStars", default=0))
        currency = f"⭐  **{stars}** Stars\n-# {all_time} earned all time"

        # Medals are stored as lists of map IDs, so the count is the length.
        medal_counts = [
            (get_medal_emoji(i), len(dig(data, "Medals", tier, default=[]) or []))
            for i, tier in enumerate(("Diamond", "Gold", "Silver", "Bronze"))
        ]
        medals = "  ·  ".join(f"{emoji} **{count}**" for emoji, count in medal_counts)
        container.add_item(discord.ui.TextDisplay(f"{currency}\n\n{medals}"))

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

    async def show_created_maps(self, interaction):
        await interaction.response.defer(thinking=True)

        view = await build_created_maps_view(self.user_id, self.username)
        if view is None:
            await interaction.followup.send(
                f"**{self.username}** hasn't created any community maps.",
                ephemeral=True,
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

async def build_created_maps_view(user_id, username, limit=10):
    """List the community maps a player has created, most played first."""
    maps_by_id = await get_community_maps()
    if not maps_by_id:
        return None

    owned = [m for m in maps_by_id.values() if m.get("Creator") == user_id]
    if not owned:
        return None
    owned.sort(key=lambda m: m.get("Plays") or 0, reverse=True)

    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=MAP_COLOR)
    container.add_item(discord.ui.TextDisplay(
        f"## 🗺️ Maps by {username}\n"
        f"-# {len(owned)} map{'s' if len(owned) != 1 else ''} created"
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

    headshot = (await fetch_headshots([user_id])).get(user_id)
    wins = await fetch_ordered_entry(str(user_id), datastore="Data", scope="Wins")
    return ProfileView(
        entry, user_id=user_id, username=username,
        headshot=headshot, wins=wins,
    )

class MapView(discord.ui.LayoutView):
    """Components V2 map card: details, creator headshot, and two buttons."""

    def __init__(self, entry, *, headshot=None, creator_text="Unknown",
                 creator_id=None, creator_name=None, timeout=MAP_VIEW_TIMEOUT):
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
        row.add_item(discord.ui.Button(
            label="Play",
            style=discord.ButtonStyle.link,
            url=PLAY_URL_TEMPLATE.format(id=map_id),
            emoji="▶️",
        ))
        leaderboard_button = discord.ui.Button(
            label="Leaderboard",
            style=discord.ButtonStyle.secondary,
            emoji="🏆",
        )
        leaderboard_button.callback = self.show_leaderboard
        row.add_item(leaderboard_button)

        # Text can't trigger a bot action, so the creator gets a button.
        if creator_id:
            label = creator_name or str(creator_id)
            creator_button = discord.ui.Button(
                label=label[:78],
                style=discord.ButtonStyle.secondary,
                emoji="👤",
            )
            creator_button.callback = self.show_creator_profile
            row.add_item(creator_button)

        container.add_item(row)

        self.add_item(container)

    async def show_leaderboard(self, interaction):
        await interaction.response.defer(thinking=True)

        map_id = self.entry.get("Id")
        key = MAP_LEADERBOARD_KEY.format(id=map_id)
        leaderboard = await fetch_entry(key, datastore="Leaderboards")

        embed = None
        if isinstance(leaderboard, list):
            embed = await build_leaderboard_embed(
                leaderboard,
                title=f"🏆 {self.entry.get('Name') or 'Unnamed Map'}",
                subtitle=f"Map #{map_id}",
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

    title = f"🏆 Leaderboard — {get_todays_date()}"
    subtitle = f"Daily Cup #{index}"

    lb_embed = await build_leaderboard_embed(
        leaderboard, title=title, subtitle=subtitle, show_medals=True,
    )
    if lb_embed is None:
        await ctx.send("The leaderboard came back empty.")
        return

    await ctx.send(embed=lb_embed)

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
@app_commands.describe(username="Roblox username")
async def profile_command(ctx, username: str):
    await ctx.defer()

    user_id, canonical = await fetch_user_id(username)
    if not user_id:
        await ctx.send(f"No Roblox user named `{username}`.")
        return

    view = await build_profile_view(user_id, canonical)
    if view is None:
        await ctx.send(f"No game data saved for **{canonical}**.")
        return

    view.message = await ctx.send(view=view)

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
