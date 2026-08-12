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
EOF
sudo chown root:razzbot /etc/razzbot.env
sudo chmod 640 /etc/razzbot.env
```

Never commit this file. `.env` is gitignored.

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
- **Privileged intent:** `bot.py` sets `intents.message_content = True`. Enable
  *Message Content Intent* under Bot → Privileged Gateway Intents in the Discord
  developer portal, or login fails with `PrivilegedIntentsRequired`.
- **Oracle idle reclamation:** free-tier instances can be reclaimed when idle for 7 days.
  A Discord bot's gateway traffic is usually enough to stay above the threshold, but
  keep an eye on it.
- **Memory:** the unit caps the service at 400 MB (`MemoryMax`). A discord.py bot this
  size sits around 80–120 MB, so that's headroom, not a squeeze.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `Improper token has been passed` | `TOKEN` in `/etc/razzbot.env` is wrong, or has quotes around it — systemd `EnvironmentFile` does not strip them |
| `PrivilegedIntentsRequired` | Message Content Intent not enabled in the dev portal |
| Service restarts in a loop | `journalctl -u razzbot -n 50` — usually a traceback from `!maps` |
| `python3: command not found` in the unit | venv wasn't created at `/opt/razzbot/.venv` |
