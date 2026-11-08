# Session Scribe

A small Windows app that records Discord tabletop sessions **without a bot in the channel**, labels who said what using character tags, and transcribes everything locally on your PC.

- Records what you hear in Discord, plus your own mic, as two separate audio tracks.
- Uses the Discord desktop app's local connection to see who is in your voice channel and who is speaking at each moment. Nothing joins the call.
- Tag each Discord user with their character. Tags are remembered per campaign.
- Ignore users (bots are ignored by default), so a music bot never ends up in the transcript.
- Pause and resume for breaks. Paused time is not recorded and shows up as a break marker in the transcript.
- Transcribes on your computer with [faster-whisper](https://github.com/SYSTRAN/faster-whisper). It's slow but free and private, and it picks up where it left off if interrupted.
- Everything stays in a local folder (`Documents\SessionScribe\sessions` by default).

## What you need

- Windows 10 or 11 with the **Discord desktop app** (not the browser version)
- [Python 3.12](https://www.python.org/downloads/windows/) (tick "Add python.exe to PATH" during install)
- Headphones, so your mic doesn't also pick up everyone else

## One-time setup

### 1. Create a Discord application (free, takes 2 minutes)

This is what lets the app ask your Discord client who is speaking. It is not a bot and never joins a server.

1. Go to <https://discord.com/developers/applications> and click **New Application**. Name it something like "Session Scribe".
2. Open **OAuth2** in the sidebar.
3. Under **Redirects**, add `http://localhost` and save.
4. Copy the **Client ID**, then click **Reset Secret** and copy the **Client Secret**.

Create the application while logged in as the same Discord account you play on.

### 2. Install and run

1. Download or clone this repository.
2. Double-click **`Session Scribe.bat`**. The first run creates a Python environment and installs packages, which takes a few minutes.
3. In the app, click **Settings**, paste the Client ID and Client Secret, and save.
4. Join a voice channel in Discord and click **Connect to Discord**. Discord shows a popup asking you to authorize the app. Approve it once and the app remembers you.

### 3. Test it (recommended before a real session)

Join a voice channel with someone, then double-click **`Check Setup.bat`**. It:

- connects to Discord, lists who is in your channel, and prints live "started/stopped speaking" lines for 20 seconds
- records a few seconds of Discord audio and your mic and reports the levels

If all of that works, you're set.

## Campaigns

The **Campaigns** tab keeps a list of who plays which character in each campaign. Create a campaign, then add players from the Discord users Session Scribe has seen in voice with you and type their character names. Tick **Ignore** for bots. Click **Use for next session** (or pick the campaign on the Session tab) and those characters label the next transcript automatically.

Someone you've never been in voice with while the app was connected won't be in the list yet. Once they join a call, tag them on the Session tab or add them here.

## Running a session

1. Pick the **campaign**. Its character list is applied automatically.
2. Check the table of people in the voice channel. Fill in any missing characters (for yourself, type "GM") and tick **Ignore** for anyone who shouldn't be transcribed. Changes are saved to the campaign.
3. Click **Record**. Use **Pause** during breaks and **Resume** when you start again.
4. Click **End Session** when you're done. Recording stops and transcription starts right away in the background. The progress bar is under **Sessions**. Pausing never ends the session or starts transcription.

When it's done, **Open transcript** opens `transcript.md`:

```markdown
**[00:12:04] GM (Erin):** The door creaks open onto the flooded archive.
**[00:12:09] Ash (Kira):** I'm lighting a torch before anyone goes in.
*[01:30:00] Break (15m)*
```

Once you've turned a session into notes, click **Delete** to remove its recordings and transcript. The folder goes to the Recycle Bin, so empty it to free the disk space.

If you fix a tag afterwards, edit `session.json` in the session folder and click **Relabel**. That reruns only the labelling, not the slow transcription.

## Session folder

```
Documents\SessionScribe\sessions\2026-11-08_1900_Mythropolis\
  session.json        cast, character tags, ignored users
  events.jsonl        who spoke when (written live during the session)
  audio\discord_000.wav, discord_001.wav, ...   10-minute chunks of what you heard
  audio\mic_000.wav, ...                        your microphone
  transcribe_cache\   finished transcription per chunk (lets a stopped job resume)
  transcript.md       the readable transcript
  transcript.json     the same, structured, for later automation
```

Audio is saved in 10-minute pieces, and each piece is closed as soon as it's full. If the app or computer crashes, you lose at most the last few minutes, and the session is marked "interrupted" but can still be transcribed.

## Settings

| Setting | Notes |
| --- | --- |
| Microphone | Which mic to record your voice from. "Windows default" follows whatever Windows is set to. |
| Record Discord from | The headset or speakers you hear Discord through. |
| Speech model | `medium.en` is a good balance. `small.en` is much faster. `large-v3` is the most accurate and slowest. The model downloads on first use. |
| Run transcription on | "Automatic" uses an NVIDIA graphics card if its libraries are installed (see below) and otherwise falls back to the CPU on its own. |
| Spelling help | Comma-separated names and terms (NPCs, places, factions). Player and character names are added automatically. |
| Record my microphone | Turn off if you only want what you hear. |

Settings live in `%APPDATA%\SessionScribe\config.json`, and the Discord login in `discord_token.json` next to it. The log file is `session-scribe.log` in the same folder.

## Command line

```
.venv\Scripts\python -m session_scribe check
.venv\Scripts\python -m session_scribe transcribe "C:\...\sessions\2026-11-08_1900_Mythropolis"
.venv\Scripts\python -m session_scribe relabel   "C:\...\sessions\2026-11-08_1900_Mythropolis"
```

## How speaker labels work, and their limits

The "discord" track is a single mix of everyone you hear. While recording, the app logs every Discord "started speaking" and "stopped speaking" event. After transcription, each word goes to whichever tagged, non-ignored person was speaking at that moment. Your mic track is always labelled as you.

- If two players talk over each other, words go to whoever was talking most during each word, so crosstalk can get mixed up.
- An ignored music bot is excluded whenever it's the only one "speaking". If a player talks over music, stray lyrics may land in that player's line.
- If Discord disconnects mid-session, recording continues. Lines from that stretch are labelled "Unknown speaker".
- Discord only reports people in your current channel, so stay in the session's voice channel while recording.

## Troubleshooting

- **"Could not find the Discord desktop app"**: Discord must be running. The browser version doesn't expose the local connection.
- **Authorization fails or mentions scopes**: make sure the developer app was created by the account you're logged into, and that `http://localhost` is listed under OAuth2 Redirects exactly as in Settings. Click **Log out of Discord** in Settings and connect again.
- **Discord track is silent**: in Settings, set "Record Discord from" to the headset or speakers Discord plays through (Discord, User Settings, Voice & Video, Output Device).
- **Transcription is very slow**: switch to `small.en`, or set up your graphics card (below). It still runs in the background and resumes if interrupted.
- **"cublas64_12.dll is not found"**: the app found an NVIDIA card but not its libraries, and switches to the CPU on its own. To use the card instead, see below.

## Optional: faster transcription on an NVIDIA graphics card

With an NVIDIA card, transcription can run many times faster. Close the app, then in PowerShell:

```powershell
cd C:\path\to\session-scribe
.venv\Scripts\python -m pip install -r requirements-gpu.txt
```

This downloads about 1 GB of NVIDIA libraries into the app's own environment, so nothing else on your PC changes. Leave "Run transcription on" set to Automatic. If it ever has trouble, set it to "CPU only".

## Please tell your players

Nobody in the channel can see that you're recording, so let your table know before you start.

## Roadmap

- Generate session notes with Claude and file them in the Obsidian vault (after the recorder has been tested)
- Optional backup of audio to Google Cloud Storage
