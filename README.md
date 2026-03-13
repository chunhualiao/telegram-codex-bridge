# Telegram Codex Bridge

Use Telegram to talk to a local `codex` CLI session running on your computer.

This project runs a small Python bridge that:

- polls Telegram for new bot messages
- forwards each message into local `codex exec`
- relays Codex progress back to Telegram while work is running
- sends Codex's final reply back to Telegram
- keeps one saved Codex thread so follow-up messages continue the same conversation
- supports text, Telegram photos/image documents, and Telegram voice/audio messages

This is designed for a single authorized Telegram user talking to one local Codex environment.

For deeper implementation notes and lessons learned from the original setup, see [SETUP_AND_USAGE.md](./SETUP_AND_USAGE.md).

## Security

This bridge is intentionally simple, but it is powerful enough to be dangerous if you run it in the wrong environment.

- Use it only on a machine you control.
- Treat the Telegram bot token like a password.
- Keep `.env` private and never commit it.
- Restrict the bot to a single trusted Telegram account.
- After inactivity, the bridge locks itself and requires the configured Telegram passphrase before it will accept normal commands again.
- Be careful with `CODEX_FLAGS=--full-auto --json`; that setting reduces approval friction and is not appropriate for an untrusted environment.
- Do not expose this bridge as a multi-user service.

## Tested Configuration

This repo was tested with the following local setup:

- macOS `26.3.1` (`25D2128`)
- Python `3.14.2`
- Codex CLI `0.114.0`
- Bash `3.2.57(1)-release`
- `ffmpeg` from Homebrew at `/opt/homebrew/bin/ffmpeg`

If you use different versions, the bridge may still work, but the Codex CLI argument behavior is version-sensitive enough that you should validate the setup with a foreground run first.

## What You Need

Before you start, make sure the target computer has:

- Python 3
- `codex` installed and available on `PATH`
- `ffmpeg` installed and available on `PATH` for voice/audio support
- a working Codex login
- outbound network access to Telegram and Codex
- outbound network access to `api.openai.com` for voice transcription

Quick checks:

```bash
python3 --version
codex --help
```

Optional Telegram API smoke test:

```bash
curl -sS "https://api.telegram.org"
```

## Project Files

- `bridge.py`
  Main bridge process.

- `run-bridge.sh`
  Small wrapper script for launching the bridge from the correct directory.

- `watch-log.sh`
  Tails the local conversation log so you can watch Telegram-driven activity on the computer.

- `.env.example`
  Environment variable template.

- `com.example.telegram-codex-bridge.plist`
  Example macOS LaunchAgent plist.

- `SETUP_AND_USAGE.md`
  Detailed operational notes and troubleshooting guide.

## Step-By-Step Setup

### 1. Clone the repo

```bash
git clone https://github.com/chunhualiao/telegram-codex-bridge.git
cd telegram-codex-bridge
```

### 2. Create a Telegram bot

1. Open Telegram.
2. Start a chat with `@BotFather`.
3. Send `/newbot`.
4. Follow the prompts:
   - choose a display name
   - choose a username that ends with `bot`
5. Copy the bot token BotFather gives you.

It will look like:

```text
123456789:AA...
```

### 3. Send the bot a message

From the Telegram account that should be allowed to use the bridge:

1. Open a chat with your new bot.
2. Press `Start` or send any message, for example:

```text
hello
```

This matters because Telegram will not show a `chat.id` in `getUpdates` until the bot has actually received a message.

### 4. Find your Telegram chat ID

Run:

```bash
curl -sS "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates"
```

Replace `<YOUR_BOT_TOKEN>` with the real bot token.

In the JSON response, find:

```json
"chat": {
  "id": 123456789
}
```

That `id` is your allowed chat ID.

### 5. Create `.env`

Copy the template:

```bash
cp .env.example .env
```

Then edit `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=123456789:replace_me
TELEGRAM_ALLOWED_CHAT_ID=123456789
TELEGRAM_ALLOWED_USER_ID=123456789
TELEGRAM_PASSPHRASE=replace_me

CODEX_WORKDIR=/path/to/your/workdir
CODEX_FLAGS=--full-auto --json
CODEX_SYSTEM_PROMPT=You are talking to me through Telegram. Keep responses concise unless I ask for depth.
CODEX_MAX_RUNTIME_SECONDS=900
CODEX_IDLE_TIMEOUT_SECONDS=120
TELEGRAM_POLL_TIMEOUT=30
TELEGRAM_INACTIVITY_TIMEOUT_SECONDS=3600
OPENAI_API_KEY=sk-replace_me
OPENAI_TRANSCRIBE_MODEL=gpt-4o-mini-transcribe
OPENAI_TRANSCRIBE_PROMPT=The speaker may use Mandarin Chinese, English, or both in the same message. Transcribe both languages accurately. Preserve code, file paths, commands, technical terms, names, and numbers. Keep the transcript faithful to what was said.
METER_PRICE_LOOKUP=auto
METER_PRICE_CACHE_TTL_SECONDS=86400
```

Minimum required values:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ALLOWED_CHAT_ID`
- `TELEGRAM_ALLOWED_USER_ID`
- `TELEGRAM_PASSPHRASE`

Passphrase behavior:

- after `TELEGRAM_INACTIVITY_TIMEOUT_SECONDS` with no accepted activity, the bridge locks itself
- the next Telegram message must be the configured passphrase before normal commands resume

Recommended values:

- `CODEX_WORKDIR`
- `CODEX_FLAGS=--full-auto --json`

Required for voice support:

- `OPENAI_API_KEY`

Image support:

- send a Telegram photo or an image document
- if you include a caption, the caption is used as the prompt text
- if you send only the image, the bridge asks Codex to inspect the attached image

Optional for `/meter` API cost estimates:

- `METER_PRICE_LOOKUP=auto`
  Try OpenAI official pricing first for OpenAI-style model IDs, then OpenRouter as fallback.

- `METER_PRICE_MODEL=<model-id>`
  Override the detected Codex model if needed.

- `METER_PRICE_CACHE_TTL_SECONDS=86400`
  Cache pricing lookups locally under `state/pricing_cache.json`.

- `METER_PRICE_INPUT_PER_1M_TOKENS`
- `METER_PRICE_OUTPUT_PER_1M_TOKENS`
  Manual fallback only if automatic lookup fails.

Voice transcription defaults:

- `OPENAI_TRANSCRIBE_MODEL=gpt-4o-mini-transcribe`
- `OPENAI_TRANSCRIBE_PROMPT=...`

Important:

- `-C` belongs to top-level `codex`
- `--json` belongs to `codex exec` or `codex exec resume`
- `.env` should stay local and untracked

This repo already builds the command correctly. If you modify `bridge.py`, keep that flag placement intact.

## License

MIT. See [LICENSE](./LICENSE).

### 6. Run the bridge in the foreground first

Do this before trying `nohup`, `launchctl`, or any background setup.

```bash
python3 bridge.py
```

If startup works, your Telegram bot should send:

```text
Telegram Codex bridge is online.
```

### 7. Test the bot in Telegram

Send:

```text
/status
```

Expected reply:

```text
Status: no active Codex thread
Workdir: /your/workdir
```

Then send a normal prompt, for example:

```text
What directory are you operating in?
```

Expected behavior:

1. The bot sends:

```text
Running Codex...
```

2. While Codex is working, the bot may update that status with progress such as:

```text
Running Codex...

Connected to Codex session.
```

or:

```text
Running Codex...

Still working... 15s elapsed.
```

3. Then it sends Codex's actual reply.

If this works, the bridge is correctly installed.

### 8. Test voice input

Send a short voice message in Telegram.

Expected behavior:

1. The bot sends:

```text
Transcribing voice message...
```

2. Then it sends:

```text
Running Codex...
```

3. While Codex is working, the bot may update that status with real-time progress.

4. Then it sends Codex's actual reply.

The default transcription prompt is configured for:

- Mandarin Chinese
- English
- mixed Chinese and English in the same voice message
- technical words such as commands, code, and file paths

## How To Use The Bot

Supported Telegram commands:

- `/start`
  Returns a short readiness message.

- `/status`
  Shows the saved Codex thread ID, if one exists, plus the configured workdir.

- `/meter`
  Shows cumulative bridge token counts, how much was exact vs estimated, and estimated API cost when pricing is configured.

- `/reset`
  Deletes the saved Codex thread so the next message starts a fresh Codex session.

Normal usage:

- Send any plain text request as if you were talking directly to Codex.
- Or send a Telegram voice message.
- Follow-up messages continue the same Codex thread.
- Use `/reset` before switching to a completely different task.

While Codex is running, the bot now tries to relay progress in real time by editing the `Running Codex...` status message with milestones and heartbeat updates.

Example prompts:

- `Summarize the repo in ~/projects/foo`
- `Review ~/repos/app and list the top risks`
- `Why is pytest failing in ~/workspace/bar?`
- `Create a shell script in ~/bin that backs up my notes folder`

## How To Watch It Locally

You can monitor the Telegram-driven session directly on the computer in two ways.

### Option 1: Run the bridge in the foreground

```bash
python3 bridge.py
```

This prints local events to the terminal, including:

- inbound Telegram text
- voice-message transcripts
- real-time Codex progress
- Codex failures
- final bot replies

### Option 2: Watch the local conversation log

```bash
./watch-log.sh
```

This tails:

- `state/conversation.log`

The log includes:

- `USER:` incoming text or `<voice message>`
- `TRANSCRIPT:` speech-to-text output for voice messages
- `CODEX:` Codex execution milestones
- `BOT:` outgoing bot replies
- `ERROR:` bridge, transcription, or Codex failures

## Running In The Background

Only do this after a successful foreground test.

### Option 1: `nohup`

```bash
nohup ./run-bridge.sh >state/stdout.log 2>state/stderr.log </dev/null &
```

Logs:

- `state/stdout.log`
- `state/stderr.log`

### Option 2: LaunchAgent on macOS

This repo includes a sample plist template:

- `com.example.telegram-codex-bridge.plist`

If you use it:

1. Copy it into `~/Library/LaunchAgents/`
2. Replace `/path/to/telegram-codex-bridge` with the real path on your machine
3. Register it from your own logged-in shell

Foreground execution should always be your first validation step. Do not debug LaunchAgent behavior before the bridge itself works normally.

## How To Stop The Bridge

If the bridge is running and `state/bridge.lock` exists:

```bash
kill "$(cat state/bridge.lock)"
```

If the process is already dead but the lock file remains, remove the stale lock:

```bash
rm -f state/bridge.lock
```

## Troubleshooting

### The bot does not reply

Check these first:

1. Is the bridge process running?
2. Does `state/bridge.lock` point to a real process?
3. Does Telegram API access work from this machine?
4. Did you run the bridge in the foreground first?

Best debugging command:

```bash
rm -f state/bridge.lock
python3 bridge.py
```

The bridge now also keeps a machine-wide lock for the Telegram bot token under
`~/.telegram-bridge-locks/`. That prevents different bridge repos from polling
the same bot token at the same time.

### Telegram says `409 Conflict`

Cause:

- more than one process is polling `getUpdates` for the same bot token

Fix:

1. Kill every running `bridge.py` process.
2. Remove `state/bridge.lock`.
3. If needed, remove the matching token lock under `~/.telegram-bridge-locks/`
   only after you verify no bridge process is still running.
4. Start exactly one new bridge process.

The bridge now fails fast on repeated `409 Conflict` responses instead of
looping forever. It also waits until polling is confirmed before sending the
`Telegram Codex bridge is online.` message.

### Codex argument parsing fails

Common causes:

- flags passed to the wrong command level
- modifying command order in `bridge.py`

Working command shape:

```bash
codex -C /desired/workdir exec [resume <thread_id>] --full-auto --json --skip-git-repo-check --output-last-message <tempfile> "<prompt>"
```

### Voice transcription fails

Check these first:

1. `OPENAI_API_KEY` is set in `.env`
2. `ffmpeg` is installed and on `PATH`
3. the machine has outbound network access to `api.openai.com`
4. the bot can download files from Telegram

The bridge converts Telegram voice/audio to WAV with `ffmpeg`, then sends that audio to the OpenAI transcription API.

### `getUpdates` returns nothing

Usually this means:

- you have not sent the bot a message yet
- you are using the wrong bot token

### The bridge starts, then dies immediately

Common causes:

- stale lock file
- no network access
- Codex not installed or not logged in
- `ffmpeg` not installed for voice/audio conversion
- `OPENAI_API_KEY` missing for voice transcription
- running it in a restricted environment

Start in the foreground first and fix that before backgrounding it.

## Security Notes

- This bridge only accepts messages from one Telegram chat ID.
- Anyone with the bot token can still access the Telegram bot API unless you rotate the token.
- Anyone who controls the allowed Telegram account can use the bridge.
- `.env` contains secrets and should never be committed.
- `state/` is runtime data and should not be committed.

## Recommended First-Time Install Checklist

Use this order on a new machine:

1. Clone the repo.
2. Create the bot with BotFather.
3. Send the bot one message.
4. Get `chat.id` from `getUpdates`.
5. Fill in `.env`.
6. Run `python3 bridge.py`.
7. Confirm the bot sends `Telegram Codex bridge is online.`
8. Send `/status`.
9. Send one normal prompt.
10. Only then move to background execution.
# telegram-codex-bridge
