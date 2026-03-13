#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import selectors
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import atexit
import fcntl
import hashlib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None


BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
ENV_PATH = BASE_DIR / ".env"
DEFAULT_POLL_TIMEOUT = 30
DEFAULT_CONFLICT_EXIT_THRESHOLD = 3


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


def parse_float_env(name: str, default: float = 0.0) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise SystemExit(f"Invalid float value for {name}: {raw}")


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
        self.pending_message_file = STATE_DIR / "pending_message.json"
        self.transcript_file = STATE_DIR / "conversation.log"
        self.usage_meter_file = STATE_DIR / "usage_meter.json"
        self.pricing_cache_file = STATE_DIR / "pricing_cache.json"
        self.progress_interval = int(os.environ.get("TELEGRAM_PROGRESS_INTERVAL", "15"))
        self.progress_edit_interval = float(os.environ.get("TELEGRAM_PROGRESS_EDIT_INTERVAL", "2"))
        self.inactivity_timeout = int(os.environ.get("TELEGRAM_INACTIVITY_TIMEOUT_SECONDS", "3600"))
        self.codex_max_runtime = int(os.environ.get("CODEX_MAX_RUNTIME_SECONDS", "900"))
        self.codex_idle_timeout = int(os.environ.get("CODEX_IDLE_TIMEOUT_SECONDS", "120"))
        self.meter_price_model = os.environ.get("METER_PRICE_MODEL", "").strip()
        self.meter_price_input_per_million = parse_float_env("METER_PRICE_INPUT_PER_1M_TOKENS", 0.0)
        self.meter_price_output_per_million = parse_float_env("METER_PRICE_OUTPUT_PER_1M_TOKENS", 0.0)
        self.pricing_lookup_preference = os.environ.get("METER_PRICE_LOOKUP", "auto").strip().lower() or "auto"
        self.pricing_cache_ttl_seconds = int(os.environ.get("METER_PRICE_CACHE_TTL_SECONDS", "86400"))
        self.detected_model_name = self.detect_model_name()
        self.conflict_exit_threshold = int(
            os.environ.get("TELEGRAM_CONFLICT_EXIT_THRESHOLD", str(DEFAULT_CONFLICT_EXIT_THRESHOLD))
        )
        token_fingerprint = hashlib.sha256(self.bot_token.encode("utf-8")).hexdigest()[:16]
        self.global_lock_dir = Path.home() / ".telegram-bridge-locks"
        self.global_lock_file = self.global_lock_dir / f"{token_fingerprint}.lock"
        self.global_lock_handle = None

    def log_event(self, kind: str, text: str) -> None:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {kind}: {text}".replace("\r", " ").strip()
        print(line, flush=True)
        with self.transcript_file.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def run(self) -> None:
        self.acquire_lock()
        self.reset_unlock_state_for_restart()
        self.log_event("SYSTEM", "Bridge started")
        offset = self.verify_polling_ready(self.load_offset())
        self.send_message(self.allowed_chat_id, "Telegram Codex bridge is online.")
        conflict_count = 0
        while True:
            try:
                updates = self.get_updates(offset)
                conflict_count = 0
                for update in updates:
                    offset = max(offset, update["update_id"] + 1)
                    self.save_offset(offset)
                    self.handle_update(update)
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    conflict_count += 1
                    self.log_event(
                        "ERROR" if conflict_count >= self.conflict_exit_threshold else "WARN",
                        "Telegram getUpdates conflict; another poller is using this bot token "
                        f"({conflict_count}/{self.conflict_exit_threshold}).",
                    )
                    if conflict_count >= self.conflict_exit_threshold:
                        raise SystemExit(
                            "Telegram getUpdates conflict persisted. "
                            "Another process is polling the same bot token."
                        )
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
        self.acquire_global_lock()
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
        if self.global_lock_handle is not None:
            try:
                fcntl.flock(self.global_lock_handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                self.global_lock_handle.close()
            except OSError:
                pass
            self.global_lock_handle = None
        self.global_lock_file.unlink(missing_ok=True)

    def acquire_global_lock(self) -> None:
        self.global_lock_dir.mkdir(parents=True, exist_ok=True)
        handle = self.global_lock_file.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            owner = handle.read().strip() or "unknown owner"
            handle.close()
            raise SystemExit(
                "Another local process already owns this Telegram bot token lock "
                f"({self.global_lock_file}): {owner}"
            )
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} repo={BASE_DIR}\n")
        handle.flush()
        self.global_lock_handle = handle

    def load_offset(self) -> int:
        if not self.offset_file.exists():
            return 0
        raw = self.offset_file.read_text().strip()
        return int(raw) if raw.isdigit() else 0

    def save_offset(self, offset: int) -> None:
        self.offset_file.write_text(str(offset))

    def verify_polling_ready(self, offset: int) -> int:
        try:
            updates = self.get_updates(offset, timeout=0)
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                raise SystemExit(
                    "Startup failed: another process is already polling this Telegram bot token."
                ) from exc
            raise
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

    def detect_model_name(self) -> str:
        for index, flag in enumerate(self.codex_flags):
            if flag == "--model" and index + 1 < len(self.codex_flags):
                return self.codex_flags[index + 1]
            if flag.startswith("--model="):
                return flag.split("=", 1)[1]
            if flag == "-m" and index + 1 < len(self.codex_flags):
                return self.codex_flags[index + 1]
        env_model = os.environ.get("CODEX_MODEL", "").strip()
        if env_model:
            return env_model
        if tomllib is not None:
            config_path = Path.home() / ".codex" / "config.toml"
            try:
                config = tomllib.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError):
                config = {}
            model = config.get("model")
            if isinstance(model, str) and model.strip():
                return model.strip()
        return ""

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

    def clear_last_activity(self) -> None:
        self.last_activity_file.unlink(missing_ok=True)

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

    def load_pending_message(self) -> dict | None:
        if not self.pending_message_file.exists():
            return None
        try:
            payload = json.loads(self.pending_message_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self.pending_message_file.unlink(missing_ok=True)
            return None
        return payload if isinstance(payload, dict) else None

    def save_pending_message(self, message: dict) -> None:
        self.pending_message_file.write_text(json.dumps(message), encoding="utf-8")

    def clear_pending_message(self) -> None:
        self.pending_message_file.unlink(missing_ok=True)

    def reset_unlock_state_for_restart(self) -> None:
        self.clear_last_activity()
        self.clear_lock_notice()

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

    def default_usage_meter(self) -> dict:
        return {
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "exact_input_tokens": 0,
            "exact_output_tokens": 0,
            "estimated_input_tokens": 0,
            "estimated_output_tokens": 0,
            "input_cost_usd": 0.0,
            "output_cost_usd": 0.0,
            "updated_at": 0.0,
            "last_request": None,
            "models": {},
        }

    def load_usage_meter(self) -> dict:
        if not self.usage_meter_file.exists():
            return self.default_usage_meter()
        try:
            payload = json.loads(self.usage_meter_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return self.default_usage_meter()
        if not isinstance(payload, dict):
            return self.default_usage_meter()
        meter = self.default_usage_meter()
        meter.update(payload)
        return meter

    def save_usage_meter(self, meter: dict) -> None:
        self.usage_meter_file.write_text(json.dumps(meter, indent=2, sort_keys=True), encoding="utf-8")

    def estimate_tokens(self, text: str) -> int:
        text = text.strip()
        if not text:
            return 0
        return max(1, round(len(text) / 4))

    def calc_token_cost(self, input_tokens: int, output_tokens: int, pricing: dict | None) -> tuple[float, float]:
        if pricing is None:
            return 0.0, 0.0
        input_cost = (input_tokens / 1_000_000) * pricing["input_per_million"]
        output_cost = (output_tokens / 1_000_000) * pricing["output_per_million"]
        return input_cost, output_cost

    def normalize_model_name(self, model_name: str) -> str:
        return re.sub(r"[^a-z0-9./:-]+", "", model_name.strip().lower())

    def get_pricing_model_name(self) -> str:
        return self.meter_price_model or self.detected_model_name

    def load_pricing_cache(self) -> dict:
        if not self.pricing_cache_file.exists():
            return {}
        try:
            payload = json.loads(self.pricing_cache_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def save_pricing_cache(self, cache: dict) -> None:
        self.pricing_cache_file.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")

    def read_url_text(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": "telegram-codex-bridge/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="ignore")

    def lookup_manual_pricing(self, model_name: str) -> dict | None:
        if self.meter_price_input_per_million <= 0.0 and self.meter_price_output_per_million <= 0.0:
            return None
        return {
            "model": model_name,
            "display_model": model_name,
            "input_per_million": self.meter_price_input_per_million,
            "output_per_million": self.meter_price_output_per_million,
            "source": "manual",
            "url": "",
            "fetched_at": time.time(),
        }

    def choose_pricing_sources(self, model_name: str) -> list[str]:
        if self.pricing_lookup_preference == "official":
            return ["official", "openrouter", "manual"]
        if self.pricing_lookup_preference == "openrouter":
            return ["openrouter", "official", "manual"]
        if "/" in model_name:
            return ["openrouter", "official", "manual"]
        return ["official", "openrouter", "manual"]

    def lookup_openai_pricing(self, model_name: str) -> dict | None:
        base_model = model_name.split("/", 1)[1] if model_name.startswith("openai/") else model_name
        normalized_model = self.normalize_model_name(base_model)
        if not normalized_model or not (
            normalized_model.startswith("gpt-")
            or normalized_model.startswith("o1")
            or normalized_model.startswith("o3")
            or normalized_model.startswith("o4")
        ):
            return None
        urls = [
            f"https://developers.openai.com/api/docs/models/{urllib.parse.quote(base_model)}",
            "https://openai.com/api/pricing/",
        ]
        model_patterns = [re.escape(base_model), re.escape(base_model.replace("-", " "))]
        for url in urls:
            try:
                text = self.read_url_text(url)
            except Exception:
                continue
            compact = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))
            for pattern in model_patterns:
                match = re.search(
                    rf"(?is){pattern}.*?Input[: ]+\$?\s*([0-9]+(?:[.,][0-9]+)?)\s*/\s*1M\s*tokens.*?"
                    rf"Output[: ]+\$?\s*([0-9]+(?:[.,][0-9]+)?)\s*/\s*1M\s*tokens",
                    compact,
                )
                if not match:
                    continue
                input_per_million = float(match.group(1).replace(",", ""))
                output_per_million = float(match.group(2).replace(",", ""))
                return {
                    "model": model_name,
                    "display_model": base_model,
                    "input_per_million": input_per_million,
                    "output_per_million": output_per_million,
                    "source": "official",
                    "url": url,
                    "fetched_at": time.time(),
                }
        return None

    def lookup_openrouter_pricing(self, model_name: str) -> dict | None:
        try:
            payload = json.loads(self.read_url_text("https://openrouter.ai/api/v1/models"))
        except Exception:
            return None
        models = payload.get("data")
        if not isinstance(models, list):
            return None
        candidates = [model_name]
        if "/" not in model_name and model_name:
            candidates.append(f"openai/{model_name}")
        normalized_candidates = {self.normalize_model_name(candidate) for candidate in candidates}
        for item in models:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or "").strip()
            canonical_slug = str(item.get("canonical_slug") or "").strip()
            names = {
                self.normalize_model_name(model_id),
                self.normalize_model_name(canonical_slug),
            }
            if not any(name and name in normalized_candidates for name in names):
                continue
            pricing = item.get("pricing") or {}
            prompt = pricing.get("prompt")
            completion = pricing.get("completion")
            try:
                input_per_million = float(prompt) * 1_000_000
                output_per_million = float(completion) * 1_000_000
            except (TypeError, ValueError):
                continue
            return {
                "model": model_name,
                "display_model": model_id or model_name,
                "input_per_million": input_per_million,
                "output_per_million": output_per_million,
                "source": "openrouter",
                "url": "https://openrouter.ai/api/v1/models",
                "fetched_at": time.time(),
            }
        return None

    def resolve_pricing(self, model_name: str | None = None) -> dict | None:
        model_name = (model_name or self.get_pricing_model_name()).strip()
        if not model_name:
            return self.lookup_manual_pricing("unknown")
        cache = self.load_pricing_cache()
        cache_key = self.normalize_model_name(model_name)
        cached = cache.get(cache_key)
        if isinstance(cached, dict) and (time.time() - float(cached.get("fetched_at", 0))) < self.pricing_cache_ttl_seconds:
            return cached
        for source in self.choose_pricing_sources(model_name):
            if source == "official":
                pricing = self.lookup_openai_pricing(model_name)
            elif source == "openrouter":
                pricing = self.lookup_openrouter_pricing(model_name)
            else:
                pricing = self.lookup_manual_pricing(model_name)
            if pricing is None:
                continue
            cache[cache_key] = pricing
            self.save_pricing_cache(cache)
            return pricing
        return None

    def record_usage(self, usage: dict) -> None:
        meter = self.load_usage_meter()
        meter["requests"] += 1
        meter["input_tokens"] += usage["input_tokens"]
        meter["output_tokens"] += usage["output_tokens"]
        meter["exact_input_tokens"] += usage["exact_input_tokens"]
        meter["exact_output_tokens"] += usage["exact_output_tokens"]
        meter["estimated_input_tokens"] += usage["estimated_input_tokens"]
        meter["estimated_output_tokens"] += usage["estimated_output_tokens"]
        meter["input_cost_usd"] += usage["input_cost_usd"]
        meter["output_cost_usd"] += usage["output_cost_usd"]
        meter["updated_at"] = time.time()
        model_key = usage.get("model") or "unknown"
        models = meter.get("models")
        if not isinstance(models, dict):
            models = {}
            meter["models"] = models
        model_meter = models.get(model_key)
        if not isinstance(model_meter, dict):
            model_meter = {
                "requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
                "pricing_source": usage.get("pricing_source") or "unavailable",
            }
            models[model_key] = model_meter
        model_meter["requests"] += 1
        model_meter["input_tokens"] += usage["input_tokens"]
        model_meter["output_tokens"] += usage["output_tokens"]
        model_meter["cost_usd"] += usage["input_cost_usd"] + usage["output_cost_usd"]
        model_meter["pricing_source"] = usage.get("pricing_source") or model_meter["pricing_source"]
        meter["last_request"] = {
            "at": meter["updated_at"],
            "model": usage.get("model"),
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "exact_input_tokens": usage["exact_input_tokens"],
            "exact_output_tokens": usage["exact_output_tokens"],
            "estimated_input_tokens": usage["estimated_input_tokens"],
            "estimated_output_tokens": usage["estimated_output_tokens"],
            "input_cost_usd": usage["input_cost_usd"],
            "output_cost_usd": usage["output_cost_usd"],
            "outcome": usage["outcome"],
            "pricing_source": usage.get("pricing_source"),
            "pricing_url": usage.get("pricing_url"),
        }
        self.save_usage_meter(meter)

    def format_usage_report(self) -> str:
        meter = self.load_usage_meter()
        total_cost = meter["input_cost_usd"] + meter["output_cost_usd"]
        pricing = self.resolve_pricing()
        current_model = self.get_pricing_model_name() or "unknown"
        lines = [
            "Bridge meter",
            f"Detected model: {current_model}",
            f"Requests: {meter['requests']}",
            f"Input tokens: {meter['input_tokens']:,}",
            f"Output tokens: {meter['output_tokens']:,}",
            (
                "Exact tokens from Codex events: "
                f"in {meter['exact_input_tokens']:,}, out {meter['exact_output_tokens']:,}"
            ),
            (
                "Bridge-side estimates used: "
                f"in {meter['estimated_input_tokens']:,}, out {meter['estimated_output_tokens']:,}"
            ),
        ]
        if pricing is not None:
            lines.extend(
                [
                    f"Estimated API cost total: ${total_cost:.6f}",
                    (
                        "Current pricing: "
                        f"input ${pricing['input_per_million']:.3f}/1M, "
                        f"output ${pricing['output_per_million']:.3f}/1M"
                    ),
                    f"Pricing source: {pricing['source']}",
                ]
            )
            if pricing.get("url"):
                lines.append(f"Pricing URL: {pricing['url']}")
        else:
            lines.append("Estimated API cost: unavailable. Automatic lookup and manual fallback both failed.")
        models = meter.get("models")
        if isinstance(models, dict) and models:
            model_lines = []
            for model_name, model_meter in sorted(models.items()):
                if not isinstance(model_meter, dict):
                    continue
                model_lines.append(
                    f"{model_name}: {model_meter.get('requests', 0)} req, "
                    f"in {model_meter.get('input_tokens', 0):,}, "
                    f"out {model_meter.get('output_tokens', 0):,}, "
                    f"${model_meter.get('cost_usd', 0.0):.6f}"
                )
            if model_lines:
                lines.append("Per-model totals:")
                lines.extend(model_lines[:5])
        last_request = meter.get("last_request")
        if isinstance(last_request, dict):
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_request.get("at", 0)))
            last_total_cost = last_request.get("input_cost_usd", 0.0) + last_request.get("output_cost_usd", 0.0)
            lines.extend(
                [
                    (
                        f"Last request: {timestamp} ({last_request.get('outcome', 'unknown')}, "
                        f"model {last_request.get('model', 'unknown')})"
                    ),
                    (
                        "Last request tokens: "
                        f"in {last_request.get('input_tokens', 0):,}, "
                        f"out {last_request.get('output_tokens', 0):,}"
                    ),
                ]
            )
            if pricing is not None:
                lines.append(f"Last request estimated API cost: ${last_total_cost:.6f}")
            if last_request.get("pricing_source"):
                lines.append(f"Last request pricing source: {last_request['pricing_source']}")
        lines.append("Source: exact when Codex emits usage fields, otherwise estimated from prompt/reply text.")
        return "\n".join(lines)

    def merge_usage_snapshot(self, current: dict | None, candidate: dict | None) -> dict | None:
        if candidate is None:
            return current
        if current is None:
            return candidate
        current_score = sum(1 for key in ("input_tokens", "output_tokens", "total_tokens") if current.get(key) is not None)
        candidate_score = sum(
            1 for key in ("input_tokens", "output_tokens", "total_tokens") if candidate.get(key) is not None
        )
        if candidate_score > current_score:
            return candidate
        if candidate_score == current_score and (candidate.get("total_tokens") or 0) >= (current.get("total_tokens") or 0):
            return candidate
        return current

    def extract_usage_snapshot(self, event: dict) -> dict | None:
        candidates: list[dict] = []
        input_keys = ("input_tokens", "prompt_tokens")
        output_keys = ("output_tokens", "completion_tokens")
        total_keys = ("total_tokens",)

        def collect(node: object) -> None:
            if isinstance(node, dict):
                snapshot = {"input_tokens": None, "output_tokens": None, "total_tokens": None}
                for key in input_keys:
                    value = node.get(key)
                    if isinstance(value, (int, float)):
                        snapshot["input_tokens"] = int(value)
                        break
                for key in output_keys:
                    value = node.get(key)
                    if isinstance(value, (int, float)):
                        snapshot["output_tokens"] = int(value)
                        break
                for key in total_keys:
                    value = node.get(key)
                    if isinstance(value, (int, float)):
                        snapshot["total_tokens"] = int(value)
                        break
                if any(snapshot.values()):
                    candidates.append(snapshot)
                for value in node.values():
                    collect(value)
            elif isinstance(node, list):
                for item in node:
                    collect(item)

        collect(event)
        best: dict | None = None
        for candidate in candidates:
            best = self.merge_usage_snapshot(best, candidate)
        return best

    def finalize_usage(self, prompt: str, response_text: str, outcome: str, exact_usage: dict | None) -> dict:
        estimated_input_tokens = self.estimate_tokens(prompt)
        estimated_output_tokens = self.estimate_tokens(response_text)
        exact_input_tokens = (exact_usage or {}).get("input_tokens")
        exact_output_tokens = (exact_usage or {}).get("output_tokens")
        final_input_tokens = exact_input_tokens if exact_input_tokens is not None else estimated_input_tokens
        final_output_tokens = exact_output_tokens if exact_output_tokens is not None else estimated_output_tokens
        model_name = self.get_pricing_model_name() or "unknown"
        pricing = self.resolve_pricing(model_name)
        input_cost_usd, output_cost_usd = self.calc_token_cost(final_input_tokens, final_output_tokens, pricing)
        return {
            "model": model_name,
            "input_tokens": final_input_tokens,
            "output_tokens": final_output_tokens,
            "exact_input_tokens": exact_input_tokens or 0,
            "exact_output_tokens": exact_output_tokens or 0,
            "estimated_input_tokens": 0 if exact_input_tokens is not None else estimated_input_tokens,
            "estimated_output_tokens": 0 if exact_output_tokens is not None else estimated_output_tokens,
            "input_cost_usd": input_cost_usd,
            "output_cost_usd": output_cost_usd,
            "outcome": outcome,
            "pricing_source": pricing["source"] if pricing else "unavailable",
            "pricing_url": pricing["url"] if pricing else "",
        }

    def telegram_request(self, method: str, payload: dict) -> dict:
        data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(f"{self.api_base}/{method}", data=data)
        with urllib.request.urlopen(req, timeout=self.poll_timeout + 10) as resp:
            body = resp.read().decode()
        parsed = json.loads(body)
        if not parsed.get("ok"):
            raise RuntimeError(f"Telegram API error for {method}: {body}")
        return parsed

    def get_updates(self, offset: int, timeout: int | None = None) -> list[dict]:
        payload = {
            "timeout": str(self.poll_timeout if timeout is None else timeout),
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

    def is_image_document(self, message: dict) -> bool:
        document = message.get("document") or {}
        mime_type = str(document.get("mime_type") or "").lower()
        return mime_type.startswith("image/")

    def has_image(self, message: dict) -> bool:
        return bool(message.get("photo")) or self.is_image_document(message)

    def download_image_from_message(self, message: dict, dest_dir: Path) -> Path:
        photo_sizes = message.get("photo") or []
        document = message.get("document") or {}
        file_id = ""
        suffix = ".jpg"
        if photo_sizes:
            largest = photo_sizes[-1]
            file_id = str(largest.get("file_id") or "")
            suffix = ".jpg"
        elif self.is_image_document(message):
            file_id = str(document.get("file_id") or "")
            file_name = str(document.get("file_name") or "")
            suffix = Path(file_name).suffix or ".img"
        if not file_id:
            raise RuntimeError("Telegram image payload did not include a file_id.")
        image_path = dest_dir / f"telegram-image{suffix}"
        self.download_telegram_file(file_id, image_path)
        return image_path

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
        text = (message.get("text") or message.get("caption") or "").strip()
        has_voice = bool(message.get("voice") or message.get("audio"))
        has_image = self.has_image(message)
        if (
            chat_type != "private"
            or chat_id != self.allowed_chat_id
            or user_id != self.allowed_user_id
            or (not text and not has_voice and not has_image)
        ):
            return
        if self.is_unlock_required():
            if text == self.passphrase:
                self.save_last_activity()
                self.clear_lock_notice()
                self.send_message(chat_id, "Unlocked. Session is active again.")
                pending_message = self.load_pending_message()
                if pending_message:
                    self.clear_pending_message()
                    self.process_message(chat_id, pending_message)
            else:
                self.save_pending_message(message)
                last_notice = self.load_lock_notice()
                now = time.time()
                if last_notice is None or (now - last_notice) >= 60:
                    self.send_message(
                        chat_id,
                        "Session locked after inactivity. Send the passphrase to continue. "
                        "Your last message is queued and will run after unlock.",
                    )
                    self.save_lock_notice(now)
            return
        self.process_message(chat_id, message)

    def process_message(self, chat_id: str, message: dict) -> None:
        text = (message.get("text") or message.get("caption") or "").strip()
        has_voice = bool(message.get("voice") or message.get("audio"))
        has_image = self.has_image(message)
        if text:
            self.log_event("USER", text)
        elif has_voice:
            self.log_event("USER", "<voice message>")
        elif has_image:
            self.log_event("USER", "<image message>")
        if text == "/start":
            self.save_last_activity()
            self.send_message(chat_id, "Bridge is running. Send a prompt, `/reset`, `/status`, or `/meter`.")
            return
        if text == "/status":
            thread_id = self.load_thread_id()
            status = thread_id if thread_id else "no active Codex thread"
            self.save_last_activity()
            self.send_message(chat_id, f"Status: {status}\nWorkdir: {self.workdir}")
            return
        if text == "/meter":
            self.save_last_activity()
            self.send_message(chat_id, self.format_usage_report())
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
        if has_image and not text:
            text = "Please inspect the attached image and describe or answer based on it."
        status_message_id = self.send_message(chat_id, "Running Codex...")
        reply, usage = self.run_codex(
            text,
            chat_id=chat_id,
            status_message_id=status_message_id,
            image_message=message if has_image else None,
        )
        self.record_usage(usage)
        self.save_last_activity()
        self.send_message(chat_id, reply)

    def build_command(self, prompt: str, image_paths: list[str] | None = None) -> list[str]:
        thread_id = self.load_thread_id()
        base = ["codex", "-C", self.workdir, "exec"]
        if thread_id:
            base.extend(["resume", thread_id])
        base.extend(self.codex_flags)
        for image_path in image_paths or []:
            base.extend(["--image", image_path])
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

    def run_codex(
        self,
        prompt: str,
        *,
        chat_id: str,
        status_message_id: int | None,
        image_message: dict | None = None,
    ) -> tuple[str, dict]:
        with tempfile.NamedTemporaryFile(prefix="codex-last-message-", delete=False) as tmp:
            output_path = tmp.name
        temp_dir_obj: tempfile.TemporaryDirectory[str] | None = None
        image_paths: list[str] = []
        thread_before = self.load_thread_id()
        full_prompt = prompt
        if not thread_before and self.system_prompt:
            full_prompt = f"{self.system_prompt}\n\nUser message from Telegram:\n{prompt}"
        if image_message is not None:
            temp_dir_obj = tempfile.TemporaryDirectory(prefix="telegram-image-")
            image_path = self.download_image_from_message(image_message, Path(temp_dir_obj.name))
            image_paths.append(str(image_path))
        cmd = self.build_command(full_prompt, image_paths=image_paths)
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
        exact_usage: dict | None = None
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
                    exact_usage = self.merge_usage_snapshot(exact_usage, self.extract_usage_snapshot(event))
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
            if temp_dir_obj is not None:
                temp_dir_obj.cleanup()
        if timeout_reason:
            usage = self.finalize_usage(full_prompt, timeout_reason, "timeout", exact_usage)
            self.maybe_update_progress(
                chat_id,
                status_message_id,
                "Running Codex...\n\nStopped. Sending timeout details...",
                force=True,
                state=progress_state,
            )
            return timeout_reason, usage
        if proc.returncode != 0:
            tail = output.strip()[-1500:] if output.strip() else "Codex exited without output."
            self.log_event("ERROR", f"Codex command failed: {tail}")
            failure_message = f"Codex command failed.\n\n{tail}"
            usage = self.finalize_usage(full_prompt, failure_message, "error", exact_usage)
            self.maybe_update_progress(
                chat_id,
                status_message_id,
                "Running Codex...\n\nCodex failed. Sending error details...",
                force=True,
                state=progress_state,
            )
            return failure_message, usage
        self.log_event("CODEX", "Codex reply ready")
        self.maybe_update_progress(
            chat_id,
            status_message_id,
            "Running Codex...\n\nFinished. Sending final reply...",
            force=True,
            state=progress_state,
        )
        final_message = message or "Codex returned an empty message."
        usage = self.finalize_usage(full_prompt, final_message, "ok", exact_usage)
        return final_message, usage

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
