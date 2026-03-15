import threading

from bridge import TelegramCodexBridge


def make_bridge() -> TelegramCodexBridge:
    return TelegramCodexBridge.__new__(TelegramCodexBridge)


def make_configured_bridge() -> TelegramCodexBridge:
    bridge = make_bridge()
    bridge.workdir = "/tmp/workdir"
    bridge.codex_flags = ["--full-auto", "--json"]
    bridge.codex_model_override = ""
    bridge.system_prompt = "default prompt"
    bridge.codex_max_runtime = 900
    bridge.codex_idle_timeout = 120
    bridge.codex_command_idle_timeout = 600
    bridge.poll_timeout = 30
    bridge.progress_mode = "edit"
    bridge.progress_interval = 15
    bridge.progress_edit_interval = 2.0
    bridge.auth_required = True
    bridge.passphrase = "secret-passphrase"
    bridge.inactivity_timeout = 3600
    bridge.conflict_exit_threshold = 3
    bridge.transcribe_model = "whisper-1"
    bridge.transcribe_prompt = "transcribe prompt"
    bridge.meter_price_model = ""
    bridge.meter_price_input_per_million = 0.0
    bridge.meter_price_output_per_million = 0.0
    bridge.pricing_lookup_preference = "auto"
    bridge.pricing_cache_ttl_seconds = 86400
    bridge.jobs_lock = threading.Lock()
    bridge.jobs = {}
    bridge.recent_job_ids = []
    bridge.next_job_number = 0
    bridge.interactive_job_id = None
    bridge.config_defaults = bridge.capture_runtime_config_defaults()
    bridge.runtime_config_overrides = {}
    bridge.detected_model_name = bridge.detect_model_name()
    return bridge
