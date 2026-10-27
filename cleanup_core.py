"""Pure logic for cleanup.py: config, date window, selection rule, manifest and progress files.

Nothing in here talks to Telegram, so it can be unit-tested without Telethon or a login.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

MAX_BATCH_SIZE = 100  # messages.deleteMessages accepts at most 100 IDs per call
MIN_DELAY_SECONDS = 0.5
SELECTION_RULE = "imported-on-day"  # recorded in the manifest so a manifest from another rule is refused


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    self_user_id: int
    target_user_id: int
    date: date
    timezone: str
    batch_size: int
    delay_seconds: float
    flood_wait_margin_seconds: float
    max_retries: int
    session_name: str

    def require_ids(self) -> None:
        if self.self_user_id <= 0 or self.target_user_id <= 0:
            raise ConfigError(
                "self_user_id and target_user_id must be set in config.json "
                "(run `python cleanup.py identify` to find them)."
            )
        if self.self_user_id == self.target_user_id:
            raise ConfigError("self_user_id and target_user_id must differ (that would be Saved Messages).")


def load_config(path: Path) -> Config:
    try:
        raw = json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise ConfigError(f"{path} not found. Copy config.example.json to config.json first.") from None
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path} is not valid JSON: {e}") from None

    def get(key, kind):
        if key not in raw:
            raise ConfigError(f"config.json is missing '{key}'")
        value = raw[key]
        if kind is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise ConfigError(f"config.json '{key}' must be {kind.__name__}")
        return value

    try:
        day = date.fromisoformat(get("date", str))
    except ValueError:
        raise ConfigError("config.json 'date' must look like 2026-10-27") from None
    tz_name = get("timezone", str)
    try:
        ZoneInfo(tz_name)
    except Exception:
        raise ConfigError(f"Unknown timezone {tz_name!r}") from None

    cfg = Config(
        self_user_id=get("self_user_id", int),
        target_user_id=get("target_user_id", int),
        date=day,
        timezone=tz_name,
        batch_size=get("batch_size", int),
        delay_seconds=get("delay_seconds", float),
        flood_wait_margin_seconds=get("flood_wait_margin_seconds", float),
        max_retries=get("max_retries", int),
        session_name=get("session_name", str),
    )
    if not 1 <= cfg.batch_size <= MAX_BATCH_SIZE:
        raise ConfigError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    if cfg.delay_seconds < MIN_DELAY_SECONDS:
        raise ConfigError(f"delay_seconds must be at least {MIN_DELAY_SECONDS}")
    if cfg.flood_wait_margin_seconds < 0 or cfg.max_retries < 0:
        raise ConfigError("flood_wait_margin_seconds and max_retries must not be negative")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cfg.session_name):
        raise ConfigError("session_name may only contain letters, digits, '_' and '-'")
    return cfg


def day_window_utc(day: date, tz_name: str) -> tuple[datetime, datetime]:
    """Return [start, end) in UTC covering one calendar day in the given timezone."""
    tz = ZoneInfo(tz_name)
    start_local = datetime.combine(day, time.min, tzinfo=tz)
    end_local = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def in_window(when: datetime, start: datetime, end: datetime) -> bool:
    if when.tzinfo is None:
        raise ValueError("refusing to compare a naive datetime")
    return start <= when < end


@dataclass(frozen=True)
class MessageFacts:
    """The fields the selection rule looks at, extracted from a Telethon message.

    `date` is when the message appeared in this chat; for an imported message that is the import time
    ("Imported 15:07"). The original pre-import timestamp is deliberately not read at all.
    """
    id: int
    date: datetime
    out: bool
    peer_id: int | None  # the chat the message lives in (for a private chat: the other user's ID)
    from_id: int | None  # Telegram often omits this in private chats
    kind: str
    imported: bool  # fwd_from.imported: created by a chat import, not sent in Telegram


def is_deletable(m: MessageFacts, cfg: Config, start: datetime, end: datetime) -> bool:
    """The one rule deciding what gets deleted: appeared on the configured day AND is imported.

    Both directions are selected: an import contains both people's messages.
    """
    if not m.imported:  # real Telegram messages are never touched
        return False
    if m.peer_id != cfg.target_user_id:  # in the one verified private chat
        return False
    if m.from_id not in (None, cfg.self_user_id, cfg.target_user_id):
        return False
    return in_window(m.date, start, end)


def chunks(items: list[int], size: int) -> list[list[int]]:
    if not 1 <= size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch size must be 1..{MAX_BATCH_SIZE}")
    return [items[i:i + size] for i in range(0, len(items), size)]


def ids_digest(ids) -> str:
    return hashlib.sha256(",".join(str(i) for i in sorted(set(ids))).encode()).hexdigest()


def write_json_private(path: Path, obj) -> None:
    """Atomically write JSON readable only by the current user (0600)."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_json(path: Path):
    return json.loads(Path(path).read_text())


def manifest_problems(manifest: dict, cfg: Config) -> list[str]:
    """Reasons the manifest can't be used with this config (empty list = OK)."""
    problems = []
    expected = {
        "self_user_id": cfg.self_user_id,
        "target_user_id": cfg.target_user_id,
        "date": cfg.date.isoformat(),
        "timezone": cfg.timezone,
        "selection_rule": SELECTION_RULE,
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            problems.append(f"manifest {key}={manifest.get(key)!r} but config has {value!r}")
    ids = [m["id"] for m in manifest.get("matches", [])]
    if len(ids) != len(set(ids)):
        problems.append("manifest contains duplicate IDs")
    if ids_digest(ids) != manifest.get("match_ids_sha256"):
        problems.append("manifest ID list does not match its checksum (was it edited?)")
    if manifest.get("match_count") != len(ids):
        problems.append("manifest match_count does not match its ID list")
    return problems


@dataclass
class Progress:
    manifest_sha256: str
    deleted: list[int] = field(default_factory=list)  # confirmed gone after our delete call
    already_absent: list[int] = field(default_factory=list)  # gone before we tried (deleted elsewhere)
    failed: dict[str, str] = field(default_factory=dict)  # id -> reason, retried on next run
    batches_completed: int = 0

    def done_ids(self) -> set[int]:
        return set(self.deleted) | set(self.already_absent)


def load_progress(path: Path, manifest_sha256: str) -> Progress:
    path = Path(path)
    if not path.exists():
        return Progress(manifest_sha256=manifest_sha256)
    raw = read_json(path)
    if raw.get("manifest_sha256") != manifest_sha256:
        raise ValueError(
            f"{path.name} belongs to a different manifest. "
            "Run `python cleanup.py scan` again to start a fresh, consistent run."
        )
    return Progress(**raw)


def save_progress(path: Path, progress: Progress) -> None:
    done = progress.done_ids()
    progress.failed = {k: v for k, v in progress.failed.items() if int(k) not in done}
    write_json_private(path, asdict(progress))


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"
