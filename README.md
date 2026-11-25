# Jeff Bot — LoL Discord AI Coach & Stats Bot

Tracks your friend group's League of Legends stats, posts scheduled daily recaps, weekly digests, rank alerts, and automatic post-game squad debriefs, and layers **DeepSeek AI coaching** on top of Riot's Match-v5 + Match Timeline + Spectator-v5 APIs.

## Features
- **Timeline VOD Reviews (`/review`)**: Extracts @10/@15 lane gold/CS diffs vs. lane opponent, early kills/gank deaths, objective-window throw deaths, death clusters, and item timings — then delivers a 5-second DeepSeek VOD breakdown.
- **Wins vs. Losses Coaching (`/coach`) & Pool Audit (`/pool`)**: Pinpoints the exact stat separating your wins from your losses, flags **Climb Champs** vs. **LP Bleed traps**, and tracks role win rates.
- **Weekly Coaching Goals (`/goal`)**: Assigns each player one measurable weekly target (deaths, CS/min, vision, pinks, KP), tracks live progress, and grades everyone in Monday's Weekly Digest.
- **Loading-Screen Gameplan (`/scout`)**: Scouts all 10 players in your live match (rank, WR, mastery, tilt/win streaks) and generates a 30-second gameplan + situational build tips.
- **Auto Squad-Game Debriefs & Draft Builder (`/debrief`, `/comp`)**: Automatically posts a combined scoreboard + MVP + AI debrief whenever 2+ friends finish a game together, and builds synergistic 4-man comps from your real champ pools.
- **Interactive Q&A (`/ask`) & Banter (`/roast`)**: Ask Coach Jeff anything grounded in your squad's real tracked stats.
- **SQLite Caching (`jeff_bot.db`)**: Caches immutable matches & timelines forever, deduplicates shared squad fetches, and tracks real LP deltas (`▲ +34 LP` / `▼ -18 LP`).

## Setup

### 1. Install dependencies
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Get your API keys
1. **Discord Bot Token & IDs** (`https://discord.com/developers/applications`)
   - Create an app → **Bot** → **Reset Token** and copy it *(no Privileged Intents needed)*.
   - **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`; permissions `Send Messages`, `Embed Links`, `Read Message History`.
   - Enable **Developer Mode** in Discord → copy Server ID (`GUILD_ID`) and Channel ID (`CHANNEL_ID`).
2. **Riot API Key** (`https://developer.riotgames.com`)
   - Use the 24h **Development API Key** immediately (refresh anytime in Discord with `/setriotkey`), and apply for a **Personal API Key** for permanent access.
3. **DeepSeek API Key** (`https://platform.deepseek.com/api_keys`)
   - Paste into `DEEPSEEK_API_KEY` in `.env`.

### 3. Verify & Run
```bash
cp env.example .env
# Fill in your keys & IDs
source venv/bin/activate
python bot.py --check
python bot.py
```

## Slash Commands

| Command | Access | Description |
|---|---|---|
| `/review player [game_ago]` | Everyone | Timeline VOD review (@10/@15 diffs, throw deaths, item spikes + DeepSeek) |
| `/coach player [days]` | Everyone | Wins-vs-Losses coaching breakdown (default 14d) |
| `/pool player [days]` | Everyone | Climb Champs vs. LP Bleed audit + role breakdown (default 30d) |
| `/goal player [refresh]` | Everyone | View or generate your measurable weekly coaching goal |
| `/scout player` | Everyone | Live loading-screen scout (all 10 players) + 30s gameplan |
| `/debrief` | Everyone | Combined squad scoreboard & AI debrief for your latest group game |
| `/comp [notes]` | Everyone | Suggest 4-man squad comps using champs your group actually plays |
| `/ask question` | Everyone | Ask Coach Jeff anything grounded in your squad's stats |
| `/roast player` | Everyone | Playful data-backed roast of last 7 days |
| `/stats player [days] [queue]` | Everyone | Player stats (1–30d, filter by queue) |
| `/lastgame player` | Everyone | Quick breakdown of latest match |
| `/synergy [days]` | Everyone | Squad & duo win rates together vs. solo |
| `/versus player1 player2` | Everyone | 7-day head-to-head comparison |
| `/damage` • `/live` • `/mastery` | Everyone | Damage leaderboard, live game check, top 10 mastery |
| `/roster list \| add \| remove` | Everyone | Manage tracked Riot IDs live |
| `/daily` • `/weekly` • `/setriotkey` | Admin | Force recaps or hot-swap Riot API key |
| `/status` • `/lolhelp` | Everyone | Bot health diagnostics & command list |
