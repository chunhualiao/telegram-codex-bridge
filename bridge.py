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
from collections import deque
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
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


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


def parse_bool_text(raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {raw}")


class TelegramCodexBridge:
    def __init__(self) -> None:
        load_env_file(ENV_PATH)
        self.ensure_private_directory(STATE_DIR)
        self.bot_token = require_env("TELEGRAM_BOT_TOKEN")
        self.allowed_chat_id = require_env("TELEGRAM_ALLOWED_CHAT_ID")
        self.allowed_user_id = require_env("TELEGRAM_ALLOWED_USER_ID")
        self.passphrase = require_env("TELEGRAM_PASSPHRASE")
        self.auth_required = parse_bool_text(os.environ.get("TELEGRAM_AUTH_REQUIRED", "true"))
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
        self.runtime_config_file = STATE_DIR / "runtime_config.json"
        self.progress_interval = int(os.environ.get("TELEGRAM_PROGRESS_INTERVAL", "15"))
        self.progress_edit_interval = float(os.environ.get("TELEGRAM_PROGRESS_EDIT_INTERVAL", "2"))
        self.inactivity_timeout = int(os.environ.get("TELEGRAM_INACTIVITY_TIMEOUT_SECONDS", "3600"))
        self.codex_max_runtime = int(os.environ.get("CODEX_MAX_RUNTIME_SECONDS", "900"))
        self.codex_idle_timeout = int(os.environ.get("CODEX_IDLE_TIMEOUT_SECONDS", "120"))
        self.codex_command_idle_timeout = int(
            os.environ.get("CODEX_COMMAND_IDLE_TIMEOUT_SECONDS", "600")
        )
        self.meter_price_model = os.environ.get("METER_PRICE_MODEL", "").strip()
        self.meter_price_input_per_million = parse_float_env("METER_PRICE_INPUT_PER_1M_TOKENS", 0.0)
        self.meter_price_output_per_million = parse_float_env("METER_PRICE_OUTPUT_PER_1M_TOKENS", 0.0)
        self.pricing_lookup_preference = os.environ.get("METER_PRICE_LOOKUP", "auto").strip().lower() or "auto"
        self.pricing_cache_ttl_seconds = int(os.environ.get("METER_PRICE_CACHE_TTL_SECONDS", "86400"))
        self.codex_model_override = ""
        self.conflict_exit_threshold = int(
            os.environ.get("TELEGRAM_CONFLICT_EXIT_THRESHOLD", str(DEFAULT_CONFLICT_EXIT_THRESHOLD))
        )
        self.config_defaults = self.capture_runtime_config_defaults()
        self.runtime_config_overrides = self.load_runtime_config()
        self.apply_runtime_config(self.runtime_config_overrides)
        self.detected_model_name = self.detect_model_name()
        token_fingerprint = hashlib.sha256(self.bot_token.encode("utf-8")).hexdigest()[:16]
        self.global_lock_dir = Path.home() / ".telegram-bridge-locks"
        self.global_lock_file = self.global_lock_dir / f"{token_fingerprint}.lock"
        self.global_lock_handle = None
        self.harden_state_paths()

    def ensure_private_directory(self, path: Path) -> None:
        path.mkdir(mode=PRIVATE_DIR_MODE, exist_ok=True)
        try:
            os.chmod(path, PRIVATE_DIR_MODE)
        except OSError:
            pass

    def harden_file_permissions(self, path: Path) -> None:
        try:
            if path.exists():
                os.chmod(path, PRIVATE_FILE_MODE)
        except OSError:
            pass

    def harden_state_paths(self) -> None:
        self.ensure_private_directory(STATE_DIR)
        for path in (
            self.offset_file,
            self.thread_file,
            self.lock_file,
            self.last_activity_file,
            self.lock_notice_file,
            self.pending_message_file,
            self.transcript_file,
            self.usage_meter_file,
            self.pricing_cache_file,
            self.runtime_config_file,
        ):
            self.harden_file_permissions(path)

    def write_private_text(self, path: Path, text: str, *, encoding: str = "utf-8") -> None:
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE_MODE), "w", encoding=encoding) as fh:
            fh.write(text)
        self.harden_file_permissions(path)

    def write_private_bytes(self, path: Path, data: bytes) -> None:
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE_MODE), "wb") as fh:
            fh.write(data)
        self.harden_file_permissions(path)

    def append_private_text(self, path: Path, text: str, *, encoding: str = "utf-8") -> None:
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, PRIVATE_FILE_MODE), "a", encoding=encoding) as fh:
            fh.write(text)
        self.harden_file_permissions(path)

    def sanitize_sensitive_text(self, text: str) -> str:
        sanitized = text
        bot_token = getattr(self, "bot_token", "").strip()
        openai_api_key = getattr(self, "openai_api_key", "").strip()
        sanitized = re.sub(
            r"Bearer\s+[A-Za-z0-9._-]+",
            "Bearer [redacted]",
            sanitized,
        )
        if bot_token:
            sanitized = sanitized.replace(bot_token, "[redacted-telegram-token]")
        if openai_api_key:
            sanitized = sanitized.replace(openai_api_key, "[redacted-openai-key]")
        sanitized = re.sub(
            r"https://api\.telegram\.org/(?:file/)?bot[^/\s]+",
            "https://api.telegram.org/bot[redacted-telegram-token]",
            sanitized,
        )
        return sanitized

    def log_event(self, kind: str, text: str) -> None:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {kind}: {self.sanitize_sensitive_text(text)}".replace("\r", " ").strip()
        print(line, flush=True)
        self.append_private_text(self.transcript_file, line + "\n")

    def format_recent_output_tail(self, lines: list[str]) -> str:
        snippets: list[str] = []
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            if len(line) > 240:
                line = line[:237] + "..."
            snippets.append(line)
        if not snippets:
            return "none"
        return " | ".join(snippets)

    def summarize_event_brief(self, event: dict) -> str:
        event_type = str(event.get("type") or "unknown")
        item = event.get("item")
        item_type = item.get("type") if isinstance(item, dict) else None
        if item_type:
            return f"{event_type} ({item_type})"
        return event_type

    def current_idle_timeout_seconds(self, active_item_type: str | None) -> int:
        if active_item_type == "command_execution":
            return max(self.codex_idle_timeout, self.codex_command_idle_timeout)
        return self.codex_idle_timeout

    def build_timeout_diagnostics(
        self,
        *,
        elapsed: int,
        quiet_for: int,
        idle_timeout: int | None = None,
        last_event_at: float | None,
        last_event_summary: str | None,
        recent_output_tail: list[str],
    ) -> str:
        lines = [
            f"Elapsed: {elapsed}s",
            f"Silent for: {quiet_for}s",
        ]
        if idle_timeout is not None:
            lines.append(f"Idle timeout: {idle_timeout}s")
        if last_event_at is not None:
            lines.append(
                "Last Codex event: "
                f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_event_at))} "
                f"({last_event_summary or 'unknown'})"
            )
        else:
            lines.append("Last Codex event: none captured")
        lines.append(f"Recent output tail: {self.format_recent_output_tail(recent_output_tail)}")
        return "\n".join(lines)

    def capture_runtime_config_defaults(self) -> dict:
        return {
            "codex.workdir": self.workdir,
            "codex.flags": list(self.codex_flags),
            "codex.model": self.codex_model_override,
            "codex.system_prompt": self.system_prompt,
            "codex.max_runtime_seconds": self.codex_max_runtime,
            "codex.idle_timeout_seconds": self.codex_idle_timeout,
            "codex.command_idle_timeout_seconds": self.codex_command_idle_timeout,
            "bridge.poll_timeout_seconds": self.poll_timeout,
            "bridge.progress_interval_seconds": self.progress_interval,
            "bridge.progress_edit_interval_seconds": self.progress_edit_interval,
            "bridge.inactivity_timeout_seconds": self.inactivity_timeout,
            "bridge.conflict_exit_threshold": self.conflict_exit_threshold,
            "bridge.auth_required": self.auth_required,
            "bridge.passphrase": self.passphrase,
            "voice.transcribe_model": self.transcribe_model,
            "voice.transcribe_prompt": self.transcribe_prompt,
            "meter.price_model": self.meter_price_model,
            "meter.price_input_per_1m_tokens": self.meter_price_input_per_million,
            "meter.price_output_per_1m_tokens": self.meter_price_output_per_million,
            "meter.price_lookup": self.pricing_lookup_preference,
            "meter.price_cache_ttl_seconds": self.pricing_cache_ttl_seconds,
        }

    def runtime_config_specs(self) -> dict:
        return {
            "bridge.conflict_exit_threshold": {
                "attr": "conflict_exit_threshold",
                "parser": lambda raw: self.parse_config_int(raw, minimum=1),
                "formatter": str,
                "help": "How many Telegram 409 polling conflicts before exiting.",
            },
            "bridge.auth_required": {
                "attr": "auth_required",
                "parser": lambda raw: parse_bool_text(raw),
                "formatter": self.format_config_bool,
                "help": "Whether the inactivity unlock gate is enabled.",
            },
            "bridge.inactivity_timeout_seconds": {
                "attr": "inactivity_timeout",
                "parser": lambda raw: self.parse_config_int(raw, minimum=60),
                "formatter": str,
                "help": "How long the session can sit idle before re-locking.",
            },
            "bridge.passphrase": {
                "attr": "passphrase",
                "parser": self.parse_config_string,
                "formatter": self.format_secret_config_value,
                "help": "Runtime unlock passphrase. Shown masked in config output and kept in memory only.",
            },
            "bridge.poll_timeout_seconds": {
                "attr": "poll_timeout",
                "parser": lambda raw: self.parse_config_int(raw, minimum=1),
                "formatter": str,
                "help": "Telegram long-poll timeout in seconds.",
            },
            "bridge.progress_edit_interval_seconds": {
                "attr": "progress_edit_interval",
                "parser": lambda raw: self.parse_config_float(raw, minimum=0.1),
                "formatter": self.format_config_float,
                "help": "Minimum spacing between progress message edits.",
            },
            "bridge.progress_interval_seconds": {
                "attr": "progress_interval",
                "parser": lambda raw: self.parse_config_int(raw, minimum=1),
                "formatter": str,
                "help": "How often the bridge sends heartbeat progress updates.",
            },
            "codex.flags": {
                "attr": "codex_flags",
                "parser": self.parse_config_flags,
                "formatter": self.format_config_flags,
                "help": "Additional CLI flags passed to `codex exec`.",
            },
            "codex.idle_timeout_seconds": {
                "attr": "codex_idle_timeout",
                "parser": lambda raw: self.parse_config_int(raw, minimum=10),
                "formatter": str,
                "help": "How long Codex may stay silent before the bridge stops it.",
            },
            "codex.command_idle_timeout_seconds": {
                "attr": "codex_command_idle_timeout",
                "parser": lambda raw: self.parse_config_int(raw, minimum=10),
                "formatter": str,
                "help": "How long an active Codex command execution may stay silent before the bridge stops it.",
            },
            "codex.max_runtime_seconds": {
                "attr": "codex_max_runtime",
                "parser": lambda raw: self.parse_config_int(raw, minimum=30),
                "formatter": str,
                "help": "Hard maximum runtime for a Codex request.",
            },
            "codex.model": {
                "attr": "codex_model_override",
                "parser": self.parse_config_string,
                "formatter": self.format_config_string,
                "help": "Model override injected into Codex CLI requests.",
            },
            "codex.system_prompt": {
                "attr": "system_prompt",
                "parser": self.parse_config_string,
                "formatter": self.format_config_string,
                "help": "Prepended system prompt used for new sessions.",
            },
            "codex.workdir": {
                "attr": "workdir",
                "parser": self.parse_config_string,
                "formatter": self.format_config_string,
                "help": "Working directory passed to `codex -C`.",
            },
            "meter.price_cache_ttl_seconds": {
                "attr": "pricing_cache_ttl_seconds",
                "parser": lambda raw: self.parse_config_int(raw, minimum=0),
                "formatter": str,
                "help": "How long cached pricing lookups remain valid.",
            },
            "meter.price_input_per_1m_tokens": {
                "attr": "meter_price_input_per_million",
                "parser": lambda raw: self.parse_config_float(raw, minimum=0.0),
                "formatter": self.format_config_float,
                "help": "Manual input price fallback in USD per million tokens.",
            },
            "meter.price_lookup": {
                "attr": "pricing_lookup_preference",
                "parser": self.parse_config_price_lookup,
                "formatter": self.format_config_string,
                "help": "Pricing lookup mode: auto, openai, openrouter, or manual.",
            },
            "meter.price_model": {
                "attr": "meter_price_model",
                "parser": self.parse_config_string,
                "formatter": self.format_config_string,
                "help": "Model name used for pricing lookups.",
            },
            "meter.price_output_per_1m_tokens": {
                "attr": "meter_price_output_per_million",
                "parser": lambda raw: self.parse_config_float(raw, minimum=0.0),
                "formatter": self.format_config_float,
                "help": "Manual output price fallback in USD per million tokens.",
            },
            "voice.transcribe_model": {
                "attr": "transcribe_model",
                "parser": self.parse_config_string,
                "formatter": self.format_config_string,
                "help": "OpenAI transcription model name.",
            },
            "voice.transcribe_prompt": {
                "attr": "transcribe_prompt",
                "parser": self.parse_config_string,
                "formatter": self.format_config_string,
                "help": "Prompt sent with voice transcription requests.",
            },
        }

    def runtime_config_aliases(self) -> dict:
        return {
            "conflict_threshold": "bridge.conflict_exit_threshold",
            "auth": "bridge.auth_required",
            "auth_required": "bridge.auth_required",
            "command_idle_timeout": "codex.command_idle_timeout_seconds",
            "idle_timeout": "codex.idle_timeout_seconds",
            "inactivity_timeout": "bridge.inactivity_timeout_seconds",
            "max_runtime": "codex.max_runtime_seconds",
            "model": "codex.model",
            "passphrase": "bridge.passphrase",
            "poll_timeout": "bridge.poll_timeout_seconds",
            "price_input": "meter.price_input_per_1m_tokens",
            "price_lookup": "meter.price_lookup",
            "price_model": "meter.price_model",
            "price_output": "meter.price_output_per_1m_tokens",
            "progress_edit_interval": "bridge.progress_edit_interval_seconds",
            "progress_interval": "bridge.progress_interval_seconds",
            "system_prompt": "codex.system_prompt",
            "transcribe_model": "voice.transcribe_model",
            "transcribe_prompt": "voice.transcribe_prompt",
            "workdir": "codex.workdir",
        }

    def parse_config_int(self, raw: str, *, minimum: int | None = None) -> int:
        value = int(raw.strip())
        if minimum is not None and value < minimum:
            raise ValueError(f"Value must be >= {minimum}")
        return value

    def parse_config_float(self, raw: str, *, minimum: float | None = None) -> float:
        value = float(raw.strip())
        if minimum is not None and value < minimum:
            raise ValueError(f"Value must be >= {minimum}")
        return value

    def parse_config_string(self, raw: str) -> str:
        value = raw.strip()
        if not value:
            raise ValueError("Value may not be empty.")
        return value

    def parse_config_flags(self, raw: str) -> list[str]:
        flags = shlex.split(raw)
        if not flags:
            raise ValueError("At least one flag is required.")
        if "--json" not in flags:
            flags.append("--json")
        return flags

    def parse_config_price_lookup(self, raw: str) -> str:
        value = raw.strip().lower()
        if value not in {"auto", "manual", "openai", "openrouter"}:
            raise ValueError("Allowed values: auto, manual, openai, openrouter")
        return value

    def parse_runtime_config_raw_value(self, key: str, raw_value: object) -> object:
        if key == "codex.flags":
            if not isinstance(raw_value, list):
                raise ValueError("Expected a list of flags.")
            if not all(isinstance(item, str) for item in raw_value):
                raise ValueError("Flag list must contain only strings.")
            return self.parse_config_flags(shlex.join(raw_value))
        if key in {"bridge.auth_required"}:
            if isinstance(raw_value, bool):
                return raw_value
            if isinstance(raw_value, str):
                return parse_bool_text(raw_value)
            raise ValueError("Expected a boolean value.")
        if key in {
            "bridge.conflict_exit_threshold",
            "bridge.inactivity_timeout_seconds",
            "bridge.poll_timeout_seconds",
            "bridge.progress_interval_seconds",
            "codex.idle_timeout_seconds",
            "codex.command_idle_timeout_seconds",
            "codex.max_runtime_seconds",
            "meter.price_cache_ttl_seconds",
        }:
            if isinstance(raw_value, bool) or not isinstance(raw_value, int):
                raise ValueError("Expected an integer value.")
            return self.runtime_config_specs()[key]["parser"](str(raw_value))
        if key in {
            "bridge.progress_edit_interval_seconds",
            "meter.price_input_per_1m_tokens",
            "meter.price_output_per_1m_tokens",
        }:
            if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
                raise ValueError("Expected a numeric value.")
            return self.runtime_config_specs()[key]["parser"](str(raw_value))
        if not isinstance(raw_value, str):
            raise ValueError("Expected a string value.")
        return self.runtime_config_specs()[key]["parser"](raw_value)

    def format_config_string(self, value: object) -> str:
        text = str(value)
        if len(text) > 120:
            return text[:117] + "..."
        return text

    def format_config_flags(self, value: object) -> str:
        flags = list(value) if isinstance(value, list) else [str(value)]
        return shlex.join(flags)

    def format_config_float(self, value: object) -> str:
        return f"{float(value):g}"

    def format_config_bool(self, value: object) -> str:
        return "true" if bool(value) else "false"

    def format_secret_config_value(self, value: object) -> str:
        text = str(value or "")
        if not text:
            return "(empty)"
        if len(text) <= 4:
            return "*" * len(text)
        return f"{text[:2]}{'*' * (len(text) - 4)}{text[-2:]}"

    def normalize_config_key(self, raw_key: str) -> str:
        key = raw_key.strip().lower()
        key = self.runtime_config_aliases().get(key, key)
        return key

    def default_runtime_config(self) -> dict:
        return {}

    def runtime_config_persistent_keys(self) -> set[str]:
        return {
            key
            for key in self.runtime_config_specs()
            if key not in {"bridge.passphrase"}
        }

    def load_runtime_config(self) -> dict:
        if not self.runtime_config_file.exists():
            return self.default_runtime_config()
        try:
            payload = json.loads(self.runtime_config_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return self.default_runtime_config()
        if not isinstance(payload, dict):
            return self.default_runtime_config()
        specs = self.runtime_config_specs()
        sanitized: dict[str, object] = {}
        for raw_key, raw_value in payload.items():
            key = self.normalize_config_key(str(raw_key))
            if key not in self.runtime_config_persistent_keys():
                continue
            spec = specs.get(key)
            if spec is None:
                continue
            try:
                sanitized[key] = self.parse_runtime_config_raw_value(key, raw_value)
            except (TypeError, ValueError):
                continue
        return sanitized

    def save_runtime_config(self) -> None:
        persisted: dict[str, object] = {}
        specs = self.runtime_config_specs()
        for key, value in sorted(self.runtime_config_overrides.items()):
            if key not in self.runtime_config_persistent_keys():
                continue
            spec = specs.get(key)
            if spec is None:
                continue
            persisted[key] = value
        self.write_private_text(self.runtime_config_file, json.dumps(persisted, indent=2, sort_keys=True), encoding="utf-8")

    def apply_runtime_config(self, overrides: dict) -> None:
        specs = self.runtime_config_specs()
        for key, spec in specs.items():
            default_value = self.config_defaults[key]
            value = overrides.get(key, default_value)
            if isinstance(default_value, list):
                value = list(value)
            setattr(self, spec["attr"], value)
        if "--json" not in self.codex_flags:
            self.codex_flags.append("--json")
        self.detected_model_name = self.detect_model_name()

    def set_runtime_config_value(self, raw_key: str, raw_value: str) -> tuple[str, object]:
        key = self.normalize_config_key(raw_key)
        spec = self.runtime_config_specs().get(key)
        if spec is None:
            raise KeyError(key)
        value = spec["parser"](raw_value)
        if self.config_defaults.get(key) == value:
            self.runtime_config_overrides.pop(key, None)
        else:
            self.runtime_config_overrides[key] = value
        self.save_runtime_config()
        self.apply_runtime_config(self.runtime_config_overrides)
        return key, value

    def unset_runtime_config_value(self, raw_key: str) -> str:
        key = self.normalize_config_key(raw_key)
        if key not in self.runtime_config_specs():
            raise KeyError(key)
        self.runtime_config_overrides.pop(key, None)
        self.save_runtime_config()
        self.apply_runtime_config(self.runtime_config_overrides)
        return key

    def clear_runtime_config(self) -> None:
        self.runtime_config_overrides = {}
        self.save_runtime_config()
        self.apply_runtime_config(self.runtime_config_overrides)

    def format_config_value(self, key: str, value: object) -> str:
        spec = self.runtime_config_specs()[key]
        return spec["formatter"](value)

    def format_config_overview(self) -> str:
        lines = [
            "Bridge config",
            "Commands: `/config list`, `/config show <key>`, `/config set <key> <value>`, `/config unset <key>`, `/config reset`",
            "",
        ]
        for key in sorted(self.runtime_config_specs()):
            value = getattr(self, self.runtime_config_specs()[key]["attr"])
            source = "override" if key in self.runtime_config_overrides else "default"
            lines.append(f"{key} = {self.format_config_value(key, value)} [{source}]")
        return "\n".join(lines)

    def format_config_key_details(self, raw_key: str) -> str:
        key = self.normalize_config_key(raw_key)
        spec = self.runtime_config_specs().get(key)
        if spec is None:
            raise KeyError(key)
        current_value = getattr(self, spec["attr"])
        default_value = self.config_defaults[key]
        override_value = self.runtime_config_overrides.get(key)
        lines = [
            f"Config key: {key}",
            f"Current: {self.format_config_value(key, current_value)}",
            f"Default: {self.format_config_value(key, default_value)}",
            f"Override: {self.format_config_value(key, override_value) if override_value is not None else 'none'}",
            f"Description: {spec['help']}",
        ]
        return "\n".join(lines)

    def build_reply_keyboard(self, rows: list[list[str]], *, one_time: bool = False) -> dict:
        keyboard = []
        for row in rows:
            buttons = []
            for label in row:
                buttons.append({"text": label})
            keyboard.append(buttons)
        return {
            "keyboard": keyboard,
            "resize_keyboard": True,
            "one_time_keyboard": one_time,
        }

    def build_remove_keyboard(self) -> dict:
        return {"remove_keyboard": True}

    def config_menu_markup(self, section: str = "root") -> dict:
        normalized = section.strip().lower() or "root"
        if normalized == "codex":
            return self.build_reply_keyboard(
                [
                    ["/config show codex.model", "/config show codex.workdir"],
                    ["/config show codex.flags", "/config show codex.system_prompt"],
                    ["/config show codex.idle_timeout_seconds", "/config show codex.max_runtime_seconds"],
                    ["/config menu presets", "/config"],
                ]
            )
        if normalized == "bridge":
            return self.build_reply_keyboard(
                [
                    ["/config show bridge.auth_required", "/config set bridge.auth_required true"],
                    ["/config set bridge.auth_required false", "/config show bridge.passphrase"],
                    ["/config show bridge.poll_timeout_seconds", "/config show bridge.inactivity_timeout_seconds"],
                    ["/config show bridge.progress_interval_seconds", "/config show bridge.progress_edit_interval_seconds"],
                    ["/config show bridge.conflict_exit_threshold", "/config menu presets"],
                    ["/config", "/config hide"],
                ]
            )
        if normalized == "meter":
            return self.build_reply_keyboard(
                [
                    ["/config show meter.price_model", "/config show meter.price_lookup"],
                    ["/config show meter.price_input_per_1m_tokens", "/config show meter.price_output_per_1m_tokens"],
                    ["/config set meter.price_lookup auto", "/config set meter.price_lookup manual"],
                    ["/config", "/config hide"],
                ]
            )
        if normalized == "voice":
            return self.build_reply_keyboard(
                [
                    ["/config show voice.transcribe_model", "/config show voice.transcribe_prompt"],
                    ["/config", "/config hide"],
                ]
            )
        if normalized == "presets":
            return self.build_reply_keyboard(
                [
                    ["/config set codex.idle_timeout_seconds 300", "/config set codex.max_runtime_seconds 1800"],
                    ["/config set bridge.progress_interval_seconds 30", "/config set bridge.progress_edit_interval_seconds 5"],
                    ["/config set meter.price_lookup auto", "/config set meter.price_lookup manual"],
                    ["/config", "/config hide"],
                ]
            )
        return self.build_reply_keyboard(
            [
                ["/config list", "/config reset"],
                ["/config menu codex", "/config menu bridge"],
                ["/config menu meter", "/config menu voice"],
                ["/config menu presets", "/config hide"],
            ]
        )

    def handle_config_command(self, chat_id: str, text: str) -> None:
        parts = text.split(None, 3)
        self.save_last_activity()
        if len(parts) == 1:
            self.send_message(
                chat_id,
                "Bridge config menu\nTap a section, inspect a key, then use `/config set <key> <value>` when you want to change it.",
                reply_markup=self.config_menu_markup(),
            )
            return
        subcommand = parts[1].lower()
        if subcommand == "hide":
            self.send_message(chat_id, "Config menu hidden.", reply_markup=self.build_remove_keyboard())
            return
        if subcommand == "menu":
            section = parts[2] if len(parts) >= 3 else "root"
            self.send_message(
                chat_id,
                f"Config menu: {section}",
                reply_markup=self.config_menu_markup(section),
            )
            return
        if subcommand == "list":
            self.send_message(chat_id, self.format_config_overview(), reply_markup=self.config_menu_markup())
            return
        if subcommand == "show":
            if len(parts) < 3:
                self.send_message(chat_id, "Usage: `/config show <key>`", reply_markup=self.config_menu_markup())
                return
            try:
                payload = self.format_config_key_details(parts[2])
            except KeyError:
                self.send_message(chat_id, f"Unknown config key: {parts[2]}", reply_markup=self.config_menu_markup())
                return
            self.send_message(chat_id, payload, reply_markup=self.config_menu_markup())
            return
        if subcommand == "set":
            if len(parts) < 4:
                self.send_message(chat_id, "Usage: `/config set <key> <value>`", reply_markup=self.config_menu_markup())
                return
            try:
                key, value = self.set_runtime_config_value(parts[2], parts[3])
            except KeyError:
                self.send_message(chat_id, f"Unknown config key: {parts[2]}", reply_markup=self.config_menu_markup())
                return
            except ValueError as exc:
                self.send_message(
                    chat_id,
                    f"Invalid config value for {parts[2]}: {exc}",
                    reply_markup=self.config_menu_markup(),
                )
                return
            self.send_message(
                chat_id,
                f"Updated {key} = {self.format_config_value(key, value)}",
                reply_markup=self.config_menu_markup(),
            )
            return
        if subcommand == "unset":
            if len(parts) < 3:
                self.send_message(chat_id, "Usage: `/config unset <key>`", reply_markup=self.config_menu_markup())
                return
            try:
                key = self.unset_runtime_config_value(parts[2])
            except KeyError:
                self.send_message(chat_id, f"Unknown config key: {parts[2]}", reply_markup=self.config_menu_markup())
                return
            self.send_message(chat_id, f"Removed override for {key}", reply_markup=self.config_menu_markup())
            return
        if subcommand == "reset":
            self.clear_runtime_config()
            self.send_message(chat_id, "Cleared all runtime config overrides.", reply_markup=self.config_menu_markup())
            return
        self.send_message(
            chat_id,
            "Unknown `/config` subcommand. Use `/config`, `/config list`, `/config show <key>`, "
            "`/config set <key> <value>`, `/config unset <key>`, `/config menu <section>`, or `/config reset`.",
            reply_markup=self.config_menu_markup(),
        )

    def effective_codex_flags(self) -> list[str]:
        flags = list(self.codex_flags)
        if not self.codex_model_override:
            return flags
        filtered: list[str] = []
        skip_next = False
        for flag in flags:
            if skip_next:
                skip_next = False
                continue
            if flag in {"--model", "-m"}:
                skip_next = True
                continue
            if flag.startswith("--model="):
                continue
            filtered.append(flag)
        return filtered

    def redact_user_message_for_log(self, text: str) -> str:
        stripped = text.strip()
        if stripped.startswith("/config set bridge.passphrase "):
            return "/config set bridge.passphrase [redacted]"
        return text

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
        self.write_private_text(self.lock_file, str(os.getpid()))
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
        self.write_private_text(self.offset_file, str(offset))

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
        if self.codex_model_override:
            return self.codex_model_override
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
        self.write_private_text(self.last_activity_file, str(timestamp))

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
        self.write_private_text(self.lock_notice_file, str(timestamp))

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
        self.write_private_text(self.pending_message_file, json.dumps(message), encoding="utf-8")

    def clear_pending_message(self) -> None:
        self.pending_message_file.unlink(missing_ok=True)

    def reset_unlock_state_for_restart(self) -> None:
        self.clear_last_activity()
        self.clear_lock_notice()

    def is_unlock_required(self) -> bool:
        if not self.auth_required:
            return False
        last_activity = self.load_last_activity()
        if last_activity is None:
            return True
        return (time.time() - last_activity) >= self.inactivity_timeout

    def save_thread_id(self, thread_id: str) -> None:
        self.write_private_text(self.thread_file, thread_id)

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
        self.write_private_text(self.usage_meter_file, json.dumps(meter, indent=2, sort_keys=True), encoding="utf-8")

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
        self.write_private_text(self.pricing_cache_file, json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")

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

    def sanitize_usage_snapshot(self, snapshot: dict | None) -> dict | None:
        if snapshot is None:
            return None
        input_tokens = snapshot.get("input_tokens")
        output_tokens = snapshot.get("output_tokens")
        total_tokens = snapshot.get("total_tokens")
        for value in (input_tokens, output_tokens, total_tokens):
            if value is not None and (not isinstance(value, int) or value < 0):
                return None
        # Guard against obviously bogus values from unrelated nested fields.
        # Even very large Codex turns should be far below these thresholds.
        if input_tokens is not None and input_tokens > 5_000_000:
            return None
        if output_tokens is not None and output_tokens > 1_000_000:
            return None
        if total_tokens is not None and total_tokens > 6_000_000:
            return None
        return snapshot

    def extract_usage_snapshot(self, event: dict) -> dict | None:
        input_keys = ("input_tokens", "prompt_tokens")
        output_keys = ("output_tokens", "completion_tokens")
        total_keys = ("total_tokens",)

        def extract_from_node(node: object) -> dict | None:
            if not isinstance(node, dict):
                return None
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
                return self.sanitize_usage_snapshot(snapshot)
            return None

        candidates = [
            extract_from_node(event.get("usage")),
            extract_from_node((event.get("result") or {}).get("usage") if isinstance(event.get("result"), dict) else None),
            extract_from_node((event.get("item") or {}).get("usage") if isinstance(event.get("item"), dict) else None),
        ]
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

    def send_message(self, chat_id: str, text: str, reply_markup: dict | None = None) -> int | None:
        payload = {"chat_id": chat_id, "text": text[:4000]}
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup)
        self.log_event("BOT", text[:4000])
        try:
            result = self.telegram_request("sendMessage", payload).get("result") or {}
            return result.get("message_id")
        except Exception as exc:
            sanitized = self.sanitize_sensitive_text(str(exc))
            self.log_event("WARN", f"Failed to send Telegram message: {sanitized}")
            print(f"Failed to send Telegram message: {sanitized}", file=sys.stderr)
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
                self.write_private_bytes(dest, data)
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

    def build_multipart_form_data(
        self,
        *,
        fields: dict[str, str],
        files: list[tuple[str, str, bytes, str]],
    ) -> tuple[bytes, str]:
        boundary = f"telegram-bridge-{hashlib.sha256(os.urandom(16)).hexdigest()[:24]}"
        body = bytearray()
        for key, value in fields.items():
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
            body.extend(value.encode("utf-8"))
            body.extend(b"\r\n")
        for field_name, filename, data, content_type in files:
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(
                (
                    f'Content-Disposition: form-data; name="{field_name}"; '
                    f'filename="{filename}"\r\n'
                ).encode("utf-8")
            )
            body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
            body.extend(data)
            body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))
        return bytes(body), boundary

    def transcribe_audio(self, source: Path) -> str:
        if not self.openai_api_key:
            raise RuntimeError("Voice support requires OPENAI_API_KEY in .env.")
        fields = {"model": self.transcribe_model}
        if self.transcribe_prompt:
            fields["prompt"] = self.transcribe_prompt
        body, boundary = self.build_multipart_form_data(
            fields=fields,
            files=[("file", source.name, source.read_bytes(), "audio/wav")],
        )
        req = urllib.request.Request(
            "https://api.openai.com/v1/audio/transcriptions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.openai_api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw_body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI transcription request failed.\n\n{self.sanitize_sensitive_text(raw_body[-1000:])}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenAI transcription request failed.\n\n{self.sanitize_sensitive_text(str(exc))}") from exc
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Could not parse transcription response: {exc}") from exc
        text = (payload.get("text") or "").strip()
        if text:
            return text
        if payload.get("error"):
            message = payload["error"].get("message") or raw_body
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
            self.log_event("USER", self.redact_user_message_for_log(text))
        elif has_voice:
            self.log_event("USER", "<voice message>")
        elif has_image:
            self.log_event("USER", "<image message>")
        if text == "/start":
            self.save_last_activity()
            self.send_message(
                chat_id,
                "Bridge is running. Send a prompt, `/reset`, `/status`, `/meter`, or `/config`.",
            )
            return
        if text == "/status":
            thread_id = self.load_thread_id()
            status = thread_id if thread_id else "no active Codex thread"
            self.save_last_activity()
            self.send_message(chat_id, f"Status: {status}\nWorkdir: {self.workdir}")
            return
        if text.startswith("/config"):
            self.handle_config_command(chat_id, text)
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
        base.extend(self.effective_codex_flags())
        if self.codex_model_override:
            base.extend(["--model", self.codex_model_override])
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
        recent_output_tail: deque[str] = deque(maxlen=8)
        progress_state = {"last_text": "", "last_edit_at": 0.0}
        started_at = time.time()
        last_activity_at = started_at
        last_event_at: float | None = None
        last_event_summary: str | None = None
        active_item_type: str | None = None
        timeout_reason: str | None = None
        timeout_details: str | None = None
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
                    recent_output_tail.append(line)
                    last_activity_at = time.time()
                    stripped = line.strip()
                    if not stripped.startswith("{"):
                        continue
                    try:
                        event = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    exact_usage = self.merge_usage_snapshot(exact_usage, self.extract_usage_snapshot(event))
                    last_event_at = last_activity_at
                    last_event_summary = self.summarize_event_brief(event)
                    item = event.get("item")
                    item_type = item.get("type") if isinstance(item, dict) else None
                    if event.get("type") == "item.started":
                        active_item_type = item_type
                    elif event.get("type") in {"item.completed", "item.failed"} and active_item_type == item_type:
                        active_item_type = None
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
                timeout_details = self.build_timeout_diagnostics(
                    elapsed=elapsed,
                    quiet_for=quiet_for,
                    idle_timeout=self.current_idle_timeout_seconds(active_item_type),
                    last_event_at=last_event_at,
                    last_event_summary=last_event_summary,
                    recent_output_tail=list(recent_output_tail),
                )
                self.log_event("ERROR", timeout_reason)
                self.log_event("ERROR", f"Timeout diagnostics:\n{timeout_details}")
                self.maybe_update_progress(
                    chat_id,
                    status_message_id,
                    "Running Codex...\n\nCodex hit the maximum runtime. Stopping it now...",
                    force=True,
                    state=progress_state,
                )
                break
            idle_timeout = self.current_idle_timeout_seconds(active_item_type)
            if quiet_for >= idle_timeout:
                timeout_reason = f"Codex produced no output for {idle_timeout}s and was stopped."
                timeout_details = self.build_timeout_diagnostics(
                    elapsed=elapsed,
                    quiet_for=quiet_for,
                    idle_timeout=idle_timeout,
                    last_event_at=last_event_at,
                    last_event_summary=last_event_summary,
                    recent_output_tail=list(recent_output_tail),
                )
                self.log_event("ERROR", timeout_reason)
                self.log_event("ERROR", f"Timeout diagnostics:\n{timeout_details}")
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
            timeout_message = timeout_reason
            if timeout_details:
                timeout_message = f"{timeout_reason}\n\n{timeout_details}"
            usage = self.finalize_usage(full_prompt, timeout_message, "timeout", exact_usage)
            self.maybe_update_progress(
                chat_id,
                status_message_id,
                "Running Codex...\n\nStopped. Sending timeout details...",
                force=True,
                state=progress_state,
            )
            return timeout_message, usage
        if proc.returncode != 0:
            tail = output.strip()[-1500:] if output.strip() else "Codex exited without output."
            command_failed_message = f"Codex command failed.\n\n{tail}"
            if last_event_at is not None or recent_output_tail:
                failure_details = self.build_timeout_diagnostics(
                    elapsed=int(time.time() - started_at),
                    quiet_for=int(time.time() - last_activity_at),
                    idle_timeout=self.current_idle_timeout_seconds(active_item_type),
                    last_event_at=last_event_at,
                    last_event_summary=last_event_summary,
                    recent_output_tail=list(recent_output_tail),
                )
                self.log_event("ERROR", f"Codex command failed: {tail}\n\n{failure_details}")
                failure_message = f"{command_failed_message}\n\n{failure_details}"
            else:
                self.log_event("ERROR", f"Codex command failed: {tail}")
                failure_message = command_failed_message
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
