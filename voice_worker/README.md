# Jeff Bot — Windows RTX 5070 Ti Voice Worker

Self-hosted **Chatterbox Turbo** (350M) FastAPI TTS server designed for your Windows gaming desktop (RTX 5070 Ti / 16 GB VRAM) paired with Jeff Bot running 24/7 on your CM3588.

- **Fast & Real:** Single-step decoder (~150–300ms on a 5070 Ti), ~2–3 GB VRAM, 0% GPU compute when idle.
- **Zero-Shot Voice Cloning:** Drop any clean 5–15 second `.wav` or `.mp3` clip into `voices/` (e.g. `voices/jeff.wav`, `voices/caster.wav`, `voices/friend.wav`) — no transcript required, and Jeff Bot's `/voice set` discovers new clips live without a restart.
- **Natural Reactions:** Supports `[laugh]`, `[chuckle]`, `[sigh]`, `[cough]` directly in speech text.

## Windows Setup (RTX 50-Series / Blackwell)

1. Install **Python 3.11** (64-bit) from python.org (or `winget install Python.Python.3.11`).
2. Open PowerShell in `voice_worker\` and run:
   ```powershell
   .\setup_windows.ps1
   ```
   *(Installs PyTorch CUDA 12.8 `cu128` wheels required for RTX 5070 Ti `sm_120`, plus `chatterbox-tts` and FastAPI.)*
3. Drop a 5–15 second clean speech sample into `voice_worker\voices\jeff.wav`.
4. Start the worker whenever you hop on to play:
   ```powershell
   .\start_windows.ps1
   ```
5. Allow port `8100` on your private LAN / Tailscale firewall if Windows Defender prompts (or run once as admin):
   ```powershell
   New-NetFirewallRule -DisplayName "Jeff Voice Worker" -Direction Inbound -Protocol TCP -LocalPort 8100 -Action Allow -Profile Private
   ```
6. On your **CM3588**, add to Jeff Bot's `.env`:
   ```env
   VOICE_ENABLED=true
   VOICE_WORKER_URL=http://<YOUR-WINDOWS-IP>:8100
   VOICE_DEFAULT_VOICE=jeff
   # Optional: set the same token on both machines
   # VOICE_WORKER_TOKEN=some-shared-secret
   ```

## Automatic CM3588 Fallback (When Desktop Is Off)

On your CM3588, start the optional **Kokoro-82M** fallback container in `docker-compose.yml`:
```bash
docker compose --profile kokoro up -d
```
And set in `.env`:
```env
VOICE_FALLBACK_TTS_URL=http://kokoro:8880
VOICE_FALLBACK_VOICE=am_adam
```
When your 5070 Ti PC is on, Jeff uses your cloned **Chatterbox Turbo** voice. When your PC is off, Jeff automatically falls back to **Kokoro-82M** on the CM3588 in ~1.5 seconds with zero config changes.
