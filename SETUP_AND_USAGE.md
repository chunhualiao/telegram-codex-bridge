# Telegram Codex Bridge Setup And Usage

This document explains how the Telegram-to-Codex bridge is structured, how it works, how to run it, and how to use it safely.

## Recommended Install Order On A New Computer

Use this order. It avoids the most common setup mistakes.

1. Confirm prerequisites first.
   Required:
   - `python3`
   - `codex`
   - a working Codex login
   - normal outbound network access to both Telegram and Codex services

2. Copy the project directory onto the new machine.

3. Create `.env` from `.env.example`.

4. Create the Telegram bot with BotFather and send it one test message from the Telegram account that should be allowed to use the bridge.

5. Fetch `getUpdates` manually from a normal shell on that computer to discover the real `chat.id`.

6. Put the bot token and the allowed chat ID into `.env`.

7. Run the bridge in the foreground first:

```bash
cd /path/to/telegram-codex-bridge
python3 bridge.py
```

8. Wait for the bot to send:

```text
Telegram Codex bridge is online.
```

9. From Telegram, send `/status` and confirm the bot replies.

10. Only after that works, move to a background process or LaunchAgent.

Do not start with `nohup` or `launchctl`. First prove that the bridge works in the foreground with real network access.

## What This Is

The bridge lets you talk to a local `codex` CLI session through Telegram.

Instead of opening a terminal and typing into Codex directly, you send a Telegram message to your bot. The bridge receives that message, runs `codex exec` on this computer, and sends Codex's reply back to Telegram.

The bridge keeps one persisted Codex conversation thread, so follow-up messages continue the same context unless you reset it.

## High-Level Flow

The system has four parts:

1. Telegram bot
   Telegram receives your message and makes it available through the Bot API.

2. Local bridge script
   The Python script polls Telegram for new messages.

3. Local Codex CLI
   The bridge runs `codex exec` or `codex exec resume`.

4. Local state files
   The bridge stores the Telegram update offset and the Codex thread ID on disk.

Message flow:

1. You send a message to the Telegram bot.
2. `bridge.py` polls `getUpdates` from Telegram.
3. The bridge verifies the message came from the one allowed Telegram chat ID.
4. The bridge sends `Running Codex...` back to Telegram.
5. The bridge runs local Codex with your message as the prompt.
6. Codex returns a final reply.
7. The bridge sends that reply back to Telegram.
8. If Codex started a new thread, the bridge saves the thread ID and reuses it on the next message.

## Files In This Project

Project directory:

`/path/to/telegram-codex-bridge`

Important files:

- `bridge.py`
  Main bridge process. Polls Telegram, invokes Codex, stores state.

- `run-bridge.sh`
  Small shell wrapper that starts the Python bridge from the correct directory.

- `.env.example`
  Template showing required and optional environment variables.

- `.env`
  Local configuration file with your real bot token and allowed chat/user IDs. Never commit this file.

- `README.md`
  Shorter quick-start guide.

- `com.example.telegram-codex-bridge.plist`
  LaunchAgent template stored in the project.

- `~/Library/LaunchAgents/com.example.telegram-codex-bridge.plist`
  LaunchAgent file in the standard per-user macOS location.

State files:

- `state/telegram_offset.txt`
  Stores the last processed Telegram update ID so messages are not replayed.

- `state/codex_thread.txt`
  Stores the active Codex thread ID after the first successful Codex run.

- `state/bridge.lock`
  Stores the bridge process PID to prevent multiple bridge instances from running at once.

- `state/stdout.log`
  Standard output log for background runs.

- `state/stderr.log`
  Error output log for background runs.

## Local Configuration

The active local configuration lives in `.env`.

Typical values include:

- `TELEGRAM_BOT_TOKEN`
  The Telegram bot token created through BotFather.

- `TELEGRAM_ALLOWED_CHAT_ID`
  The one Telegram chat allowed to use this bridge.

- `TELEGRAM_ALLOWED_USER_ID`
  The one Telegram user allowed to use this bridge.

- `TELEGRAM_PASSPHRASE`
  The passphrase required to unlock the bridge after inactivity.

- `CODEX_WORKDIR=/path/to/workdir`
  Codex runs rooted at `/path/to/workdir`.

- `CODEX_FLAGS=--full-auto --json`
  `--json` is required because the bridge parses Codex JSON events.
  `--full-auto` reduces interaction friction so Codex can act without a local interactive approval UI.
  These flags must be passed to `codex exec` or `codex exec resume`, not to top-level `codex`.

- `CODEX_SYSTEM_PROMPT=...`
  Prepended only when the first Telegram message starts a new Codex thread.

- `CODEX_MAX_RUNTIME_SECONDS=900`
  Hard cap for one Codex request before the bridge stops it.

- `CODEX_IDLE_TIMEOUT_SECONDS=120`
  Stop a Codex request if it produces no output for too long.

- `TELEGRAM_POLL_TIMEOUT=30`
  Long-poll duration for Telegram.

- `TELEGRAM_INACTIVITY_TIMEOUT_SECONDS=3600`
  Lock the bridge after one hour without accepted activity.

- `METER_PRICE_LOOKUP=auto`
  Automatic pricing lookup for `/meter`. OpenAI-style model IDs try official OpenAI pricing first, then OpenRouter.

- `METER_PRICE_MODEL=gpt-5.4`
  Optional model override if the bridge cannot detect the Codex model on its own.

- `METER_PRICE_CACHE_TTL_SECONDS=86400`
  Cache pricing lookups under `state/pricing_cache.json` for one day.

- `METER_PRICE_INPUT_PER_1M_TOKENS=1.25`
  Optional manual input-token fallback if automatic lookup fails.

- `METER_PRICE_OUTPUT_PER_1M_TOKENS=10.00`
  Optional manual output-token fallback if automatic lookup fails.

## Prerequisites

The bridge depends on these local capabilities:

- Python 3
- Codex CLI installed and available on `PATH`
- Codex authenticated on the local machine
- network access to `api.telegram.org`
- network access for Codex itself

Quick checks:

```bash
python3 --version
codex --help
```

Telegram reachability check:

```bash
curl -sS "https://api.telegram.org/bot<YOUR_TOKEN>/getMe"
```

If that `curl` fails, the bridge will not work.

## How The Bridge Script Works

`bridge.py` does the following:

1. Loads `.env`
   The script reads key-value pairs from `.env` and inserts them into the process environment.

2. Verifies required configuration
   It exits immediately if `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_ID`, `TELEGRAM_ALLOWED_USER_ID`, or `TELEGRAM_PASSPHRASE` is missing.

3. Creates the state directory
   The script ensures `state/` exists.

4. Acquires a lock
   It writes its PID into `state/bridge.lock`.
   If another running process already owns the lock, startup fails.

5. Announces itself
   On successful startup it sends `Telegram Codex bridge is online.` to the allowed Telegram chat.

6. Polls Telegram
   It repeatedly calls the Telegram Bot API `getUpdates`.

7. Filters messages
   It only handles text, image, or voice messages from the configured `TELEGRAM_ALLOWED_CHAT_ID` and `TELEGRAM_ALLOWED_USER_ID`, and only in a private chat.

8. Enforces inactivity locking
   If there has been no accepted activity for the configured timeout, the next message must be the configured passphrase before normal commands resume.

9. Handles commands
   It processes `/start`, `/status`, and `/reset` directly.

10. Runs Codex
   For normal text messages it runs local `codex exec`.

11. Persists the Codex thread
   If Codex emits a `thread.started` JSON event, the script saves that thread ID.

12. Resumes the thread later
   On later messages it runs `codex exec resume <thread_id>`.

13. Handles Telegram polling conflicts
   The bridge keeps a machine-wide lock for the Telegram bot token under
   `~/.telegram-bridge-locks/` so different repos cannot poll the same bot at
   the same time on one machine. If Telegram still returns `HTTP 409 Conflict`,
   the bridge treats that as a duplicate poller problem and exits after a few
   retries instead of looking healthy while never receiving updates.

## Exact Codex Behavior

When there is no saved Codex thread:

- the bridge optionally prepends `CODEX_SYSTEM_PROMPT`
- then it runs a new `codex exec ... <prompt>`
- then it saves the new thread ID if one is reported

When there is already a saved Codex thread:

- the bridge runs `codex exec resume <thread_id> ... <prompt>`
- this continues the same conversation context

The working command structure is:

```bash
codex -C /desired/workdir exec [resume <thread_id>] --full-auto --json --skip-git-repo-check --output-last-message <tempfile> "<prompt>"
```

Important CLI rule:

- `-C` belongs to top-level `codex`
- `--json` belongs to `exec` or `exec resume`

If those are placed incorrectly, Codex will fail with argument parsing errors.

The bridge uses `--output-last-message <tempfile>` so it can capture just Codex's final user-facing response and send that back to Telegram.

If Codex fails:

- the bridge returns `Codex command failed.`
- it appends the tail of the command output so you can see the relevant error

## Supported Telegram Commands

The bridge currently supports four explicit commands.

### `/start`

Purpose:
Returns a short readiness message.

Expected response:

```text
Bridge is running. Send a prompt, `/reset`, `/status`, or `/meter`.
```

### `/status`

Purpose:
Shows whether a Codex thread is currently saved and which workdir the bridge uses.

Expected response when no thread exists:

```text
Status: no active Codex thread
Workdir: /path/to/workdir
```

Expected response when a thread exists:

```text
Status: <thread-id>
Workdir: /path/to/workdir
```

### `/meter`

Purpose:
Shows cumulative bridge-side token usage meters.

Response includes:

- total input and output tokens
- how many tokens came from exact Codex usage fields versus bridge-side estimates
- estimated API cost with automatic pricing lookup from OpenAI/OpenRouter, or the manual fallback values if lookup fails

### `/reset`

Purpose:
Deletes the saved Codex thread ID so the next message starts a fresh conversation.

Expected response:

```text
Cleared the saved Codex thread. The next message starts a new session.
```

## Normal User Workflow

You usually do not need to think about the implementation.

Typical usage:

1. Open the Telegram chat with your bot.
2. Send `/status` if you want to see whether the bridge is already in an active conversation.
3. Send a normal message in plain English.
4. Wait for:
   - `Running Codex...`
   - then Codex's actual reply
5. Send follow-up questions naturally.
6. If you want a fresh conversation, send `/reset`.

Example prompts:

- `What directory are you operating in?`
- `Summarize the repo in ~/workspace-svcaf`
- `Find why pytest is failing in ~/projects/myapp`
- `Create a file called ~/Desktop/telegram-test.txt with the text hello from telegram`
- `Review the code in ~/repos/foo and list the biggest risks`

## What Responses To Expect

For a normal prompt, the Telegram conversation typically looks like this:

1. You send:

```text
Summarize the repo in ~/workspace-svcaf
```

2. The bot sends:

```text
Running Codex...
```

3. The bot then sends Codex's final answer.

That answer may be:

- a short explanation
- a list of findings
- confirmation that files were changed
- an error message if Codex or the bridge failed

## Security Model

The current security model is simple:

- only one Telegram chat ID is allowed
- messages from other chats are ignored
- the bot token is stored locally in `.env`
- Codex runs with the permissions implied by its own flags and your local environment

Important implications:

- Anyone who gets the bot token can interact with the bot through Telegram's API unless you rotate the token.
- Anyone who gains control of the allowed Telegram account can use the bridge.
- Because `CODEX_FLAGS` includes `--full-auto`, Codex is optimized for low-friction operation rather than maximum approval strictness.

If you want stricter behavior later, remove `--full-auto` from `.env` and adjust the bridge design accordingly.

## How To Start The Bridge

### Option 1: Manual foreground run

Useful for testing.

```bash
cd /path/to/telegram-codex-bridge
python3 bridge.py
```

This keeps the process attached to your terminal.

This should be the first run on any new computer because it exposes real startup failures immediately.

### Option 2: Manual background run

This is the method currently used.

```bash
cd /path/to/telegram-codex-bridge
nohup ./run-bridge.sh >state/stdout.log 2>state/stderr.log </dev/null &
```

This starts the bridge in the background and writes logs into `state/`.

Use this only after a successful foreground run.

## How To Stop The Bridge

Use the PID from `state/bridge.lock`.

Example:

```bash
kill "$(cat /path/to/telegram-codex-bridge/state/bridge.lock)"
```

After a clean shutdown, the lock file should be removed automatically.

If the process crashes and leaves a stale lock file behind, delete the stale lock file only after confirming that no bridge process is actually running.

## macOS LaunchAgent Status

A LaunchAgent plist was created and placed in:

`~/Library/LaunchAgents/com.example.telegram-codex-bridge.plist`

However, during setup from the automation environment, `launchctl load`, `launchctl bootstrap`, and related commands returned macOS `Input/output error` or privilege-related failures even though the plist itself passed validation.

What this means:

- the plist file is present
- the plist syntax is valid
- the bridge was not successfully registered through `launchctl` from this execution context
- the bridge was started with a normal background `nohup` process instead

If you want to retry LaunchAgent registration manually from your own terminal session, use:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.example.telegram-codex-bridge.plist 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.telegram-codex-bridge.plist
launchctl kickstart -k gui/$(id -u)/com.example.telegram-codex-bridge
```

Then check:

```bash
launchctl print gui/$(id -u)/com.example.telegram-codex-bridge
```

Treat LaunchAgent setup as optional until foreground execution is proven. It is a convenience layer, not the first debugging step.

## Logs And Troubleshooting

Main runtime files:

- `state/stdout.log`
- `state/stderr.log`

Useful checks:

Check the latest logs:

```bash
tail -n 50 /path/to/telegram-codex-bridge/state/stdout.log
tail -n 50 /path/to/telegram-codex-bridge/state/stderr.log
```

Start in the foreground when debugging:

```bash
cd /path/to/telegram-codex-bridge
rm -f state/bridge.lock
python3 bridge.py
```

That command is the fastest way to determine whether the bridge can really reach Telegram and Codex.

Check whether the bridge thinks a process already owns the lock:

```bash
cat /path/to/telegram-codex-bridge/state/bridge.lock
```

Check which Codex thread is active:

```bash
cat /path/to/telegram-codex-bridge/state/codex_thread.txt
```

Reset the Telegram conversation state:

```bash
rm -f /path/to/telegram-codex-bridge/state/codex_thread.txt
```

Be careful with that last command. It only resets conversation context; it does not stop the process.

## Common Failure Modes

### Bot receives messages but never replies

This is a common first-time setup failure mode.

Typical causes:

- the bridge process is not actually running
- a stale `bridge.lock` file prevents restart
- the running bridge process was started in an environment without working network access
- the bridge was backgrounded before a successful foreground test

What to do:

1. Check whether `state/bridge.lock` exists.
2. Verify whether that PID is real.
3. If the PID is stale, remove `state/bridge.lock`.
4. Start the bridge in the foreground.
5. Send `/status` again.

Do not assume that a background PID or a lock file means the bridge is healthy.

### Codex argument parsing errors

This is another common setup failure mode.

Observed failures included:

- `unexpected argument '-C' found`
- `a value is required for '--cd <DIR>' but none was supplied`
- `unexpected argument '--json' found`

Cause:

- flags were being inserted at the wrong command level
- top-level `codex` options and `exec` options are not interchangeable
- `exec resume` is stricter than it looks

Working rule:

- `codex -C <DIR> exec ...`
- then optional `resume <thread_id>`
- then `exec`-level flags such as `--json`

If you change the bridge later, preserve that order.

### No response in Telegram

Possible causes:

- bridge process is not running
- network access to Telegram failed
- bot token was revoked or changed
- you are messaging from a Telegram account whose chat ID does not match `TELEGRAM_ALLOWED_CHAT_ID`
- the bridge is stuck behind a stale lock file

### `/status` works but normal prompts fail

Possible causes:

- local Codex login or network access problem
- Codex CLI failure
- workdir path issue

In that case the bot usually returns a `Codex command failed.` message with command output.

### Bridge says another instance is already running

Possible causes:

- there really is another bridge process
- `bridge.lock` is stale after a crash

Verify before deleting the lock file.

### Background process starts and then silently dies

Possible causes:

- it inherited a restricted environment
- it could not reach Telegram or Codex
- it hit a lock-file conflict

What to do:

- stop background attempts
- remove a stale lock if needed
- run `python3 bridge.py` in the foreground
- verify `/status` works
- only then background it again

### LaunchAgent commands fail even though the plist is valid

This can also happen during setup.

Possible causes:

- macOS launchd domain/permission quirks in the shell context used to install it
- trying to debug LaunchAgent behavior before the bridge itself is proven healthy

What to do:

- validate the plist with `plutil -lint`
- ignore LaunchAgent at first
- prove foreground execution works
- then retry `launchctl bootstrap` manually from your own logged-in shell

### Telegram `409 Conflict`

This is another setup issue worth checking explicitly.

Cause:

- more than one process was polling `getUpdates` for the same bot token

Observed source of the problem:

- repeated restart attempts left multiple `bridge.py` processes alive at the same time

What to do:

1. List all bridge processes.
2. Kill all of them.
3. Remove `state/bridge.lock`.
4. If needed, remove the matching token lock under `~/.telegram-bridge-locks/`
   only after confirming no bridge process is still alive.
5. Start exactly one bridge process.

Do not start another instance until you have confirmed the first one exited cleanly.

## Mistakes To Avoid Next Time

These are the concrete mistakes from the first install and how to avoid them.

1. Do not trust a lock file by itself.
   `state/bridge.lock` only records a PID. It does not prove the bridge is healthy.

2. Do not start by backgrounding the process.
   Always do one clean foreground run first.

3. Do not debug LaunchAgent before the script itself works.
   Launchd adds another failure layer and hides the real cause.

4. Do not assume network access from an automation or sandbox context matches the real machine.
   The bridge needs real outbound network access to Telegram and Codex.

5. Do not forget to send the bot a message before looking up `chat.id`.
   `getUpdates` will be empty until the bot has actually received a message.

6. Do not skip the direct Telegram API smoke tests.
   `getUpdates` and `sendMessage` are the fastest way to isolate Telegram-side problems from bridge-side problems.

7. Do not stack multiple restart attempts.
   If you launch the bridge several times without cleaning up, Telegram will answer with `409 Conflict` because multiple pollers are active.

8. Do not move Codex flags around casually.
   `-C` and `--json` belong to different command levels and wrong placement breaks the bridge immediately.

## Minimal Smoke Test For A New Install

Run these checks in order on the new machine:

1. Telegram API:

```bash
curl -sS "https://api.telegram.org/bot<YOUR_TOKEN>/getMe"
```

2. Bot updates after sending a Telegram message:

```bash
curl -sS "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates"
```

3. Direct bot reply:

```bash
curl -sS -X POST "https://api.telegram.org/bot<YOUR_TOKEN>/sendMessage" \
  -d chat_id=<YOUR_CHAT_ID> \
  --data-urlencode "text=telegram bridge smoke test"
```

4. Foreground bridge:

```bash
cd /path/to/telegram-codex-bridge
python3 bridge.py
```

5. Telegram command test:

```text
/status
```

6. Normal prompt test:

```text
What directory are you operating in?
```

If all six steps work, the installation is healthy.

## Current Working Operational Pattern On This Mac

The setup is working now with this pattern:

1. Keep only one live `bridge.py` process.
2. Prefer foreground execution while validating changes.
3. Use `/status` to confirm the bot is responsive before testing normal prompts.
4. If a restart is needed:

```bash
kill "$(cat /path/to/telegram-codex-bridge/state/bridge.lock)" 2>/dev/null || true
rm -f /path/to/telegram-codex-bridge/state/bridge.lock
cd /path/to/telegram-codex-bridge
python3 bridge.py
```

5. Only move back to a background process after confirming Telegram replies again.

### Old Telegram messages replay

Possible cause:

- `telegram_offset.txt` is missing or was reset

## How Users Should Use It

Best practices:

- Use `/status` first if you are not sure whether the previous conversation is still active.
- Use `/reset` before switching to a completely different task.
- Write prompts as if you were directly instructing a coding agent on this machine.
- Mention exact directories when you want Codex to inspect or edit a specific repo.
- Be explicit when asking for file edits, tests, or reviews.

Good prompt style:

- `Review ~/repos/app and list the top 5 risks`
- `In ~/workspace/foo, explain why npm test is failing`
- `Create a shell script in ~/bin that backs up my notes folder`

Less good prompt style:

- `fix it`
- `look around`
- `do the thing`

The bridge does not add much interpretation beyond forwarding your request into Codex, so precise prompts produce better results.

## Recommended Safe Usage Pattern

If you want to minimize surprise:

1. Ask for inspection first.
2. Ask for a plan second.
3. Ask for edits third.
4. Ask for verification last.

Example:

1. `Inspect ~/repos/foo and explain the bug`
2. `Propose the smallest fix`
3. `Implement it`
4. `Run the relevant tests`

## Summary

This setup gives you a private Telegram front end to a local Codex CLI workflow.

Core properties:

- single authorized Telegram chat
- persistent Codex conversation across messages
- local disk-based state
- background process currently started with `nohup`
- LaunchAgent file prepared but not fully registered from the automation environment

For quick usage:

1. Send `/status`
2. Send a normal request
3. Send follow-up questions naturally
4. Send `/reset` when you want a fresh conversation
