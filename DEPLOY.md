# Deploying RazzBot on an Oracle Cloud free-tier VPS

Target: **Ubuntu 24.04 minimal, 1 vCPU / 1 GB RAM**.
Runs under systemd as an unprivileged `razzbot` user, restarts on crash and on reboot.

Requires **Python 3.12+** (`bot.py` uses PEP 701 f-strings). Ubuntu 24.04 ships 3.12 — fine.

---

## 1. Swap (do this first)

The micro instance has 1 GB and **no swap by default**. `pip install` can OOM-kill itself
without it, and a swapped-out idle bot is better than a dead one.

```bash
sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Check: `free -h` should show 1 Gi of swap.

## 2. Packages

The *minimal* image lacks pip and venv:

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip git
```

## 3. Service user + code

```bash
sudo useradd --system --home /opt/razzbot --shell /usr/sbin/nologin razzbot
sudo mkdir -p /opt/razzbot
sudo chown razzbot:razzbot /opt/razzbot
```

Then get the code in. Either clone:

```bash
sudo -u razzbot git clone <your-repo-url> /opt/razzbot
```

…or, if the repo isn't pushed anywhere, copy from your machine (run on **Windows**, PowerShell):

```bash
scp bot.py requirements.txt ubuntu@<VPS_IP>:/tmp/
```

then on the VPS:

```bash
sudo install -o razzbot -g razzbot -m 644 /tmp/bot.py /tmp/requirements.txt /opt/razzbot/
```

## 4. Virtualenv

```bash
sudo -u razzbot python3 -m venv /opt/razzbot/.venv
sudo -u razzbot /opt/razzbot/.venv/bin/pip install --no-cache-dir -r /opt/razzbot/requirements.txt
```

`--no-cache-dir` matters on a 1 GB box. All three deps ship manylinux wheels
(x86_64 and aarch64), so nothing compiles.

## 5. Secrets

```bash
sudo tee /etc/razzbot.env >/dev/null <<'EOF'
TOKEN=your_discord_bot_token_here
API_KEY=your_roblox_open_cloud_api_key_here
VERIFICATION_GAME_URL=https://www.roblox.com/games/start?placeId=120140749641241
DAILY_CUP_CHANNEL_ID=your_discord_channel_id_here
DAILY_CUP_ROLE_ID=your_daily_cup_notification_role_id_here
EOF
sudo chown root:razzbot /etc/razzbot.env
sudo chmod 640 /etc/razzbot.env
```

Never commit this file. `.env` is gitignored.

`DAILY_CUP_CHANNEL_ID` is the channel that receives the previous day's
leaderboard and the new Daily Cup map card at **09:00 UTC** each day. Enable
Discord Developer Mode, right-click the destination channel, and choose
**Copy Channel ID**. The bot needs View Channel, Send Messages, Embed Links,
and Use External Emojis permissions there.

If that channel is an **Announcement channel**, the bot publishes both messages
after posting, pushing them to every server that follows it. Publishing its own
messages needs no extra permission beyond Send Messages. Other servers still have
to follow the channel themselves — open it there and use *Follow*; the bot cannot
add followers. In an ordinary text channel the publish step is skipped silently,
and `!testcup` never publishes, so a preview can't reach followers.

Discord caps published messages at 10 per hour per channel; the Daily Cup posts
two per day.

`DAILY_CUP_ROLE_ID` is the ID of the role pinged with the new map card. If it
is omitted, the bot falls back to a role named exactly `Daily Cup Notification`.

The Open Cloud API key needs read, create, and update access to the
`AccountLinking` data store in addition to the bot's existing data-store access.

The admin commands need more: `universe.user-restriction:write` for `!ban` and
`!unban`, plus delete access on the `Community Maps` data store and the ordered
data stores for `!deletemap` and permanent bans.

Editing a map or Daily Cup leaderboard additionally needs
**`universe.place.luau-execution-session:write`** on `LUAU_PLACE_ID`. The game
stores those boards as Luau buffers, and the Open Cloud data store API can
*report* a buffer but never store one — writing through it leaves a plain table
and the game then fails reading its own entry. So `!leaderboard remove`, the
permanent-ban cup purge, and `!leaderboard restore` run their edit as a Luau
script on a real server instead, where a buffer stays a buffer. Each of those
takes 10-30s while the task boots.

`/sync` and `!roles` store their rules in a `Roles` data store, which the key
needs read, create, and update access to.

The bot also needs the **Manage
Roles** permission in Discord, and its own role must sit **above** every role
it hands out — Discord refuses otherwise, and `/sync` reports those as roles it
can't manage.

Every command also has a prefix-only `!dev_` twin that runs against the test
universe in `DEV_UNIVERSE_ID` (`!dev_map`, `!dev_cup`, `!dev_deletemap`, ...).
The same Open Cloud key must list both universes.

## 6. systemd

```bash
sudo cp /opt/razzbot/razzbot-update.service /etc/systemd/system/razzbot-update.service
sudo cp /opt/razzbot/razzbot.service /etc/systemd/system/razzbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now razzbot
```

Every start of `razzbot.service`, including startup after a reboot, first runs
`git pull --ff-only origin main` as the unprivileged `razzbot` user. If the update
fails because GitHub or the network is temporarily unavailable, systemd logs the
failure and starts the currently installed version of the bot.

Verify:

```bash
systemctl status razzbot
journalctl -u razzbot -f
```

You want `Logged in as RazzBot#....` in the log.

---

## Updating after a code change

```bash
sudo systemctl restart razzbot
```

The restart automatically runs the updater before launching the bot.

If dependencies changed, re-run the `pip install` line from step 4 before restarting.

## Notes

- **No inbound ports needed.** The bot is an outbound websocket client, so the Oracle
  security list / `iptables` rules can stay closed. Do not open anything.
- **User installs:** In the Discord Developer Portal, open **Installation**, enable both
  **User Install** and **Guild Install**, and add `applications.commands` to the User
  Install scopes. The bot always syncs its user-installable commands globally.
- **Privileged intents:** `bot.py` sets `intents.message_content = True` and
  `intents.members = True`. Enable **both** *Message Content Intent* and *Server
  Members Intent* under Bot → Privileged Gateway Intents in the Discord developer
  portal, or login fails with `PrivilegedIntentsRequired`. Without the members
  intent the member cache holds only accounts the bot has happened to see, so
  `guild.get_member` misses nearly everyone — `/sync all` skips them and
  `!linked` reports them as not in the server.
- **Oracle idle reclamation:** free-tier instances can be reclaimed when idle for 7 days.
  A Discord bot's gateway traffic is usually enough to stay above the threshold, but
  keep an eye on it.
- **Memory:** the unit caps the service at 400 MB (`MemoryMax`). A discord.py bot this
  size sits around 80–120 MB, so that's headroom, not a squeeze.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `Improper token has been passed` | `TOKEN` in `/etc/razzbot.env` is wrong, or has quotes around it — systemd `EnvironmentFile` does not strip them |
| `PrivilegedIntentsRequired` | Message Content Intent **or** Server Members Intent not enabled in the dev portal |
| `!linked` says everyone is "not in this server" | Server Members Intent is off, so the member cache is empty |
| Service restarts in a loop | `journalctl -u razzbot -n 50` — usually a traceback from `!maps` |
| `python3: command not found` in the unit | venv wasn't created at `/opt/razzbot/.venv` |
