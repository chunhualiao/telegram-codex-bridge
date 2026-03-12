#!/usr/bin/env python3
import json
import os
import selectors
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import atexit
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
ENV_PATH = BASE_DIR / ".env"
DEFAULT_POLL_TIMEOUT = 30


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith(("'", '"')) and value.endswith(("'", '"')) and len(value) >= 2:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


class TelegramCodexBridge:
    def __init__(self) -> None:
        load_env_file(ENV_PATH)
        STATE_DIR.mkdir(exist_ok=True)
        self.bot_token = require_env("TELEGRAM_BOT_TOKEN")
        self.allowed_chat_id = require_env("TELEGRAM_ALLOWED_CHAT_ID")
        self.allowed_user_id = require_env("TELEGRAM_ALLOWED_USER_ID")
        self.passphrase = require_env("TELEGRAM_PASSPHRASE")
        self.workdir = os.environ.get("CODEX_WORKDIR", str(Path.home()))
        self.codex_flags = shlex.split(os.environ.get("CODEX_FLAGS", "--full-auto"))
        if "--json" not in self.codex_flags:
            self.codex_flags.append("--json")
        self.system_prompt = os.environ.get("CODEX_SYSTEM_PROMPT", "").strip()
        self.poll_timeout = int(os.environ.get("TELEGRAM_POLL_TIMEOUT", DEFAULT_POLL_TIMEOUT))
        self.openai_api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        self.transcribe_model = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "whisper-1").strip() or "whisper-1"
        self.transcribe_prompt = os.environ.get(
            "OPENAI_TRANSCRIBE_PROMPT",
            "The speaker may use Mandarin Chinese, English, or both in the same message. "
            "Transcribe both languages accurately. Preserve code, file paths, commands, and technical terms.",
        ).strip()
        self.api_base = f"https://api.telegram.org/bot{self.bot_token}"
        self.file_base = f"https://api.telegram.org/file/bot{self.bot_token}"
        self.offset_file = STATE_DIR / "telegram_offset.txt"
        self.thread_file = STATE_DIR / "codex_thread.txt"
        self.lock_file = STATE_DIR / "bridge.lock"
        self.last_activity_file = STATE_DIR / "last_activity.txt"
        self.lock_notice_file = STATE_DIR / "lock_notice.txt"
        self.transcript_file = STATE_DIR / "conversation.log"
        self.progress_interval = int(os.environ.get("TELEGRAM_PROGRESS_INTERVAL", "15"))
        self.progress_edit_interval = float(os.environ.get("TELEGRAM_PROGRESS_EDIT_INTERVAL", "2"))
        self.inactivity_timeout = int(os.environ.get("TELEGRAM_INACTIVITY_TIMEOUT_SECONDS", "3600"))
        self.codex_max_runtime = int(os.environ.get("CODEX_MAX_RUNTIME_SECONDS", "900"))
        self.codex_idle_timeout = int(os.environ.get("CODEX_IDLE_TIMEOUT_SECONDS", "120"))

    def log_event(self, kind: str, text: str) -> None:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {kind}: {text}".replace("\r", " ").strip()
        print(line, flush=True)
        with self.transcript_file.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def run(self) -> None:
        self.acquire_lock()
        self.log_event("SYSTEM", "Bridge started")
        self.send_message(self.allowed_chat_id, "Telegram Codex bridge is online.")
        offset = self.skip_startup_backlog(self.load_offset())
        while True:
            try:
                updates = self.get_updates(offset)
                for update in updates:
                    offset = max(offset, update["update_id"] + 1)
                    self.save_offset(offset)
                    self.handle_update(update)
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    self.log_event("WARN", "Telegram getUpdates conflict; another poller is or was active. Retrying.")
                    time.sleep(5)
                    continue
                self.log_event("ERROR", f"Telegram polling failed: {exc}")
                self.send_message(self.allowed_chat_id, f"Bridge error: {exc}")
                time.sleep(5)
            except (socket.timeout, TimeoutError) as exc:
                self.log_event("WARN", f"Telegram polling timed out: {exc}. Retrying.")
                time.sleep(5)
                continue
            except urllib.error.URLError as exc:
                if "timed out" in str(exc).lower():
                    self.log_event("WARN", f"Telegram polling timed out: {exc}. Retrying.")
                    time.sleep(5)
                    continue
                self.log_event("ERROR", f"Telegram polling URL error: {exc}")
                self.send_message(self.allowed_chat_id, f"Bridge error: {exc}")
                time.sleep(5)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                self.log_event("ERROR", f"Bridge loop failed: {exc}")
                self.send_message(self.allowed_chat_id, f"Bridge error: {exc}")
                time.sleep(5)

    def acquire_lock(self) -> None:
        if self.lock_file.exists():
            pid_text = self.lock_file.read_text().strip()
            if pid_text.isdigit():
                pid = int(pid_text)
                try:
                    os.kill(pid, 0)
                except OSError:
                    pass
                else:
                    raise SystemExit(f"Bridge already running with PID {pid}")
        self.lock_file.write_text(str(os.getpid()))
        atexit.register(self.release_lock)

    def release_lock(self) -> None:
        self.lock_file.unlink(missing_ok=True)

    def load_offset(self) -> int:
        if not self.offset_file.exists():
            return 0
        raw = self.offset_file.read_text().strip()
        return int(raw) if raw.isdigit() else 0

    def save_offset(self, offset: int) -> None:
        self.offset_file.write_text(str(offset))

    def skip_startup_backlog(self, offset: int) -> int:
        try:
            updates = self.get_updates(offset)
        except Exception as exc:
            self.log_event("WARN", f"Could not inspect startup backlog: {exc}")
            return offset
        if not updates:
            return offset
        new_offset = max(update["update_id"] + 1 for update in updates)
        skipped = len(updates)
        self.save_offset(new_offset)
        self.log_event("SYSTEM", f"Skipped {skipped} stale Telegram update(s) from startup backlog")
        return new_offset


    def load_thread_id(self) -> str | None:
        if not self.thread_file.exists():
            return None
        value = self.thread_file.read_text().strip()
        return value or None

    def load_last_activity(self) -> float | None:
        if not self.last_activity_file.exists():
            return None
        raw = self.last_activity_file.read_text().strip()
        try:
            return float(raw)
        except ValueError:
            return None

    def save_last_activity(self, timestamp: float | None = None) -> None:
        if timestamp is None:
            timestamp = time.time()
        self.last_activity_file.write_text(str(timestamp))

    def load_lock_notice(self) -> float | None:
        if not self.lock_notice_file.exists():
            return None
        raw = self.lock_notice_file.read_text().strip()
        try:
            return float(raw)
        except ValueError:
            return None

    def save_lock_notice(self, timestamp: float | None = None) -> None:
        if timestamp is None:
            timestamp = time.time()
        self.lock_notice_file.write_text(str(timestamp))

    def clear_lock_notice(self) -> None:
        self.lock_notice_file.unlink(missing_ok=True)

    def is_unlock_required(self) -> bool:
        last_activity = self.load_last_activity()
        if last_activity is None:
            return True
        return (time.time() - last_activity) >= self.inactivity_timeout

    def save_thread_id(self, thread_id: str) -> None:
        self.thread_file.write_text(thread_id)

    def clear_thread_id(self) -> None:
        if self.thread_file.exists():
            self.thread_file.unlink()

    def telegram_request(self, method: str, payload: dict) -> dict:
        data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(f"{self.api_base}/{method}", data=data)
        with urllib.request.urlopen(req, timeout=self.poll_timeout + 10) as resp:
            body = resp.read().decode()
        parsed = json.loads(body)
        if not parsed.get("ok"):
            raise RuntimeError(f"Telegram API error for {method}: {body}")
        return parsed

    def get_updates(self, offset: int) -> list[dict]:
        payload = {
            "timeout": str(self.poll_timeout),
            "offset": str(offset),
            "allowed_updates": json.dumps(["message"]),
        }
        return self.telegram_request("getUpdates", payload).get("result", [])

    def send_message(self, chat_id: str, text: str) -> int | None:
        payload = {"chat_id": chat_id, "text": text[:4000]}
        self.log_event("BOT", text[:4000])
        try:
            result = self.telegram_request("sendMessage", payload).get("result") or {}
            return result.get("message_id")
        except Exception as exc:
            self.log_event("WARN", f"Failed to send Telegram message: {exc}")
            print(f"Failed to send Telegram message: {exc}", file=sys.stderr)
            return None

    def edit_message(self, chat_id: str, message_id: int, text: str) -> None:
        payload = {"chat_id": chat_id, "message_id": str(message_id), "text": text[:4000]}
        self.log_event("BOT", f"[edit] {text[:4000]}")
        try:
            self.telegram_request("editMessageText", payload)
        except Exception as exc:
            self.log_event("WARN", f"Could not edit Telegram message {message_id}: {exc}")

    def download_telegram_file(self, file_id: str, dest: Path) -> Path:
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                info = self.telegram_request("getFile", {"file_id": file_id}).get("result") or {}
                file_path = info.get("file_path")
                if not file_path:
                    raise RuntimeError("Telegram did not return a file path for the voice message.")
                url = f"{self.file_base}/{file_path}"
                with urllib.request.urlopen(url, timeout=self.poll_timeout + 30) as resp:
                    data = resp.read()
                dest.write_bytes(data)
                return dest
            except Exception as exc:
                last_error = exc
                if attempt == 4:
                    break
                time.sleep(1 + attempt)
        raise RuntimeError(f"Telegram file download failed after retries: {last_error}")

    def convert_audio_to_wav(self, source: Path, dest: Path) -> Path:
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-ac",
            "1",
            "-ar",
            "16000",
            str(dest),
        ]
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            tail = proc.stdout.strip()[-1000:] if proc.stdout.strip() else "ffmpeg exited without output."
            raise RuntimeError(f"ffmpeg audio conversion failed.\n\n{tail}")
        return dest

    def transcribe_audio(self, source: Path) -> str:
        if not self.openai_api_key:
            raise RuntimeError("Voice support requires OPENAI_API_KEY in .env.")
        cmd = [
            "curl",
            "-sS",
            "https://api.openai.com/v1/audio/transcriptions",
            "-H",
            f"Authorization: Bearer {self.openai_api_key}",
            "-F",
            f"file=@{source}",
            "-F",
            f"model={self.transcribe_model}",
        ]
        if self.transcribe_prompt:
            cmd.extend(["-F", f"prompt={self.transcribe_prompt}"])
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            tail = proc.stdout.strip()[-1000:] if proc.stdout.strip() else "curl exited without output."
            raise RuntimeError(f"OpenAI transcription request failed.\n\n{tail}")
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Could not parse transcription response: {exc}") from exc
        text = (payload.get("text") or "").strip()
        if text:
            return text
        if payload.get("error"):
            message = payload["error"].get("message") or proc.stdout
            raise RuntimeError(f"OpenAI transcription error: {message}")
        raise RuntimeError("OpenAI transcription returned no text.")

    def voice_prompt_from_message(self, message: dict) -> str:
        voice = message.get("voice")
        audio = message.get("audio")
        media = voice or audio
        if not media:
            raise RuntimeError("No voice or audio payload found.")
        file_id = media.get("file_id")
        if not file_id:
            raise RuntimeError("Telegram voice message did not include a file_id.")
        with tempfile.TemporaryDirectory(prefix="telegram-voice-") as temp_dir:
            temp_root = Path(temp_dir)
            source = temp_root / "source.ogg"
            wav = temp_root / "voice.wav"
            self.download_telegram_file(file_id, source)
            self.convert_audio_to_wav(source, wav)
            transcript = self.transcribe_audio(wav)
        if not transcript:
            raise RuntimeError("Voice transcription came back empty.")
        return transcript

    def handle_update(self, update: dict) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        chat_id = str(chat.get("id", ""))
        user_id = str(user.get("id", ""))
        chat_type = str(chat.get("type", ""))
        text = (message.get("text") or "").strip()
        has_voice = bool(message.get("voice") or message.get("audio"))
        if (
            chat_type != "private"
            or chat_id != self.allowed_chat_id
            or user_id != self.allowed_user_id
            or (not text and not has_voice)
        ):
            return
        if self.is_unlock_required():
            if text == self.passphrase:
                self.save_last_activity()
                self.clear_lock_notice()
                self.send_message(chat_id, "Unlocked. Session is active again.")
            else:
                last_notice = self.load_lock_notice()
                now = time.time()
                if last_notice is None or (now - last_notice) >= 60:
                    self.send_message(chat_id, "Session locked after inactivity. Send the passphrase to continue.")
                    self.save_lock_notice(now)
            return
        if text:
            self.log_event("USER", text)
        elif has_voice:
            self.log_event("USER", "<voice message>")
        if text == "/start":
            self.save_last_activity()
            self.send_message(chat_id, "Bridge is running. Send a prompt, `/reset`, or `/status`.")
            return
        if text == "/status":
            thread_id = self.load_thread_id()
            status = thread_id if thread_id else "no active Codex thread"
            self.save_last_activity()
            self.send_message(chat_id, f"Status: {status}\nWorkdir: {self.workdir}")
            return
        if text == "/reset":
            self.clear_thread_id()
            self.save_last_activity()
            self.send_message(chat_id, "Cleared the saved Codex thread. The next message starts a new session.")
            return
        if has_voice and not text:
            self.send_message(chat_id, "Transcribing voice message...")
            try:
                text = self.voice_prompt_from_message(message)
                self.log_event("TRANSCRIPT", text)
            except Exception as exc:
                self.log_event("ERROR", f"Voice transcription failed: {exc}")
                self.send_message(chat_id, f"Voice transcription failed.\n\n{exc}")
                return
        status_message_id = self.send_message(chat_id, "Running Codex...")
        reply = self.run_codex(text, chat_id=chat_id, status_message_id=status_message_id)
        self.save_last_activity()
        self.send_message(chat_id, reply)

    def build_command(self, prompt: str) -> list[str]:
        thread_id = self.load_thread_id()
        base = ["codex", "-C", self.workdir, "exec"]
        if thread_id:
            base.extend(["resume", thread_id])
        base.extend(self.codex_flags)
        base.extend(["--skip-git-repo-check", "--output-last-message"])
        return base + [prompt]

    def summarize_event(self, event: dict) -> str | None:
        event_type = event.get("type")
        if event_type == "thread.started":
            return "Connected to Codex session."
        if event_type == "turn.started":
            return "Codex is thinking..."
        if event_type == "error":
            return event.get("message") or "Codex reported an error."
        item = event.get("item")
        if not isinstance(item, dict):
            return None
        item_type = item.get("type")
        if item_type == "agent_message":
            text = (item.get("text") or "").strip()
            if text:
                preview = text if len(text) <= 300 else text[:297] + "..."
                return f"Codex drafted a reply:\n\n{preview}"
            return "Codex drafted a reply."
        if item_type:
            status = event_type.replace(".", " ")
            return f"{item_type.replace('_', ' ')}: {status}"
        return None

    def maybe_update_progress(
        self,
        chat_id: str,
        status_message_id: int | None,
        text: str,
        *,
        force: bool = False,
        state: dict,
    ) -> None:
        if not status_message_id:
            return
        now = time.time()
        if not force:
            if text == state.get("last_text"):
                return
            if now - state.get("last_edit_at", 0.0) < self.progress_edit_interval:
                return
        self.edit_message(chat_id, status_message_id, text)
        state["last_text"] = text
        state["last_edit_at"] = now

    def run_codex(self, prompt: str, *, chat_id: str, status_message_id: int | None) -> str:
        with tempfile.NamedTemporaryFile(prefix="codex-last-message-", delete=False) as tmp:
            output_path = tmp.name
        thread_before = self.load_thread_id()
        full_prompt = prompt
        if not thread_before and self.system_prompt:
            full_prompt = f"{self.system_prompt}\n\nUser message from Telegram:\n{prompt}"
        cmd = self.build_command(full_prompt)
        insert_at = len(cmd) - 1
        cmd[insert_at:insert_at] = [output_path]
        self.log_event("CODEX", "Executing Codex request")
        env = os.environ.copy()
        env["NO_COLOR"] = "1"
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        selector = selectors.DefaultSelector()
        assert proc.stdout is not None
        selector.register(proc.stdout, selectors.EVENT_READ)
        output_lines: list[str] = []
        progress_state = {"last_text": "", "last_edit_at": 0.0}
        started_at = time.time()
        last_activity_at = started_at
        timeout_reason: str | None = None
        self.maybe_update_progress(
            chat_id,
            status_message_id,
            "Running Codex...\n\nConnected to local Codex CLI.",
            force=True,
            state=progress_state,
        )
        while True:
            events = selector.select(timeout=1.0)
            if events:
                for key, _ in events:
                    line = key.fileobj.readline()
                    if line == "":
                        selector.unregister(key.fileobj)
                        continue
                    output_lines.append(line)
                    last_activity_at = time.time()
                    stripped = line.strip()
                    if not stripped.startswith("{"):
                        continue
                    try:
                        event = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "thread.started" and event.get("thread_id"):
                        self.save_thread_id(event["thread_id"])
                    summary = self.summarize_event(event)
                    if summary:
                        self.log_event("CODEX", summary)
                        self.maybe_update_progress(
                            chat_id,
                            status_message_id,
                            f"Running Codex...\n\n{summary}",
                            state=progress_state,
                        )
            if proc.poll() is not None and not selector.get_map():
                break
            elapsed = int(time.time() - started_at)
            quiet_for = int(time.time() - last_activity_at)
            if elapsed >= self.codex_max_runtime:
                timeout_reason = (
                    f"Codex exceeded the maximum runtime of {self.codex_max_runtime}s and was stopped."
                )
                self.log_event("ERROR", timeout_reason)
                self.maybe_update_progress(
                    chat_id,
                    status_message_id,
                    "Running Codex...\n\nCodex hit the maximum runtime. Stopping it now...",
                    force=True,
                    state=progress_state,
                )
                break
            if quiet_for >= self.codex_idle_timeout:
                timeout_reason = (
                    f"Codex produced no output for {self.codex_idle_timeout}s and was stopped."
                )
                self.log_event("ERROR", timeout_reason)
                self.maybe_update_progress(
                    chat_id,
                    status_message_id,
                    "Running Codex...\n\nCodex stopped producing output. Stopping it now...",
                    force=True,
                    state=progress_state,
                )
                break
            if elapsed >= self.progress_interval and quiet_for >= self.progress_interval:
                heartbeat = f"Running Codex...\n\nStill working... {elapsed}s elapsed."
                self.maybe_update_progress(
                    chat_id,
                    status_message_id,
                    heartbeat,
                    state=progress_state,
                )
        if timeout_reason:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.log_event("WARN", "Codex did not exit after SIGTERM; killing process.")
                proc.kill()
                proc.wait()
        else:
            proc.wait()
        output = "".join(output_lines)
        self.maybe_capture_thread_id(output)
        try:
            message = Path(output_path).read_text().strip()
        finally:
            Path(output_path).unlink(missing_ok=True)
        if timeout_reason:
            self.maybe_update_progress(
                chat_id,
                status_message_id,
                "Running Codex...\n\nStopped. Sending timeout details...",
                force=True,
                state=progress_state,
            )
            return timeout_reason
        if proc.returncode != 0:
            tail = output.strip()[-1500:] if output.strip() else "Codex exited without output."
            self.log_event("ERROR", f"Codex command failed: {tail}")
            self.maybe_update_progress(
                chat_id,
                status_message_id,
                "Running Codex...\n\nCodex failed. Sending error details...",
                force=True,
                state=progress_state,
            )
            return f"Codex command failed.\n\n{tail}"
        self.log_event("CODEX", "Codex reply ready")
        self.maybe_update_progress(
            chat_id,
            status_message_id,
            "Running Codex...\n\nFinished. Sending final reply...",
            force=True,
            state=progress_state,
        )
        return message or "Codex returned an empty message."

    def maybe_capture_thread_id(self, output: str) -> None:
        for line in output.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started" and event.get("thread_id"):
                self.save_thread_id(event["thread_id"])
                return


def main() -> None:
    TelegramCodexBridge().run()


if __name__ == "__main__":
    main()
