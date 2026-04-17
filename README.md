# LoL Discord Stats Bot

Tracks your friend group's League of Legends stats and posts daily recaps + weekly digests to a Discord channel.

## Stats Tracked
- Win rate (with visual bar)
- Hours played
- KDA (Kills/Deaths/Assists)
- Top champions played
- LP / rank changes
- Weekly leaderboard
- Daily/weekly awards (best WR, most games, best KDA, most hours, highest rank)

## Setup

### 1. Prerequisites
```bash
cd ~
git clone <your-repo> lol-discord-bot  # or copy files
cd lol-discord-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Get your API keys

**Riot API Key:**
- Go to https://developer.riotgames.com
- Sign in and copy your Development API Key (expires every 24h)
- For permanent use: register a personal app for a Production key

**Discord Bot Token:**
- Go to https://discord.com/developers/applications
- Create a new application → Bot → Reset Token → copy it
- Under OAuth2 → URL Generator: select `bot` scope + `Send Messages`, `Embed Links`, `Read Message History` permissions
- Invite the bot to your server with the generated URL

**Channel ID:**
- In Discord: Settings → Advanced → Enable Developer Mode
- Right-click your target channel → Copy Channel ID

### 3. Configure

```bash
cp .env.example .env
nano .env
```

Fill in:
```
DISCORD_TOKEN=your_bot_token
CHANNEL_ID=your_channel_id
RIOT_API_KEY=your_riot_api_key
REGION=na1          # na1, euw1, kr, eun1, br1, la1, la2, oc1, tr1, ru
ROUTING=americas    # americas (na/br/la), europe (euw/eun/tr/ru), asia (kr/jp)
```

Then edit `config.py` and fill in your friend group's Riot IDs:
```python
SUMMONERS = [
    "YourName#NA1",
    "Friend1#NA1",
    "Friend2#NA1",
]
```

You can also adjust the schedule:
```python
DAILY_HOUR = 8      # 8 AM daily recap
WEEKLY_DAY = "mon"  # Monday weekly digest
```

### 4. Test it

```bash
source venv/bin/activate
python bot.py
```

In Discord, run:
- `!daily` — force a daily recap right now
- `!weekly` — force a weekly digest right now
- `!stats Name#TAG` — look up any player

### 5. Deploy on CM3588 with systemd

```bash
# Edit the service file
nano lol-bot.service
# Replace YOUR_USERNAME with your actual username (e.g. friendlycore)

# Install
sudo cp lol-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable lol-bot
sudo systemctl start lol-bot

# Check status
sudo systemctl status lol-bot
journalctl -u lol-bot -f   # live logs
```

## Commands

| Command | Access | Description |
|---|---|---|
| `!daily` | Admin | Force post a daily recap |
| `!weekly` | Admin | Force post a weekly digest |
| `!stats Name#TAG` | Everyone | Look up any player (last 24h) |
| `!lolhelp` | Everyone | Show command list |

## Notes

- **Rate limits:** Riot's dev key is limited to 20 req/s and 100 req/2min. The bot uses a semaphore to throttle concurrent match fetches. With more than ~6 players, consider requesting a production key.
- **Match data lag:** Riot's match history API can have a 5–10 minute delay after a game ends.
- **Regions:** Make sure REGION and ROUTING match your friends' server. NA1 → americas, EUW1/EUN1 → europe, KR → asia.
