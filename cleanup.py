#!/usr/bin/env python3
"""Delete messages that APPEARED on ONE day in ONE private Telegram chat AND are marked IMPORTED
(e.g. a WhatsApp chat import), for both people. The original pre-import date is ignored, and
messages actually sent in Telegram are never touched. Only that day's messages are read.

Commands (`preview` is the default; preview and scan are both dry runs):
  identify  log in; show this account's numeric ID and recent private chats with their IDs
  preview   DRY RUN step 1: check only the newest 100 messages (seconds); writes nothing
  scan      DRY RUN step 2: check all of the day's messages, write data/manifest.json; deletes nothing
  delete    re-check the manifest against Telegram, ask for 3 typed confirmations, then delete
  verify    re-read the chat and check what is gone and what remains
  logout    end this login on Telegram's side and remove the local session file

API credentials come from TG_API_ID / TG_API_HASH if set, otherwise hidden prompts.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import logging
import os
import random
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from telethon import TelegramClient, errors, functions, types, utils

import cleanup_core as core

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CONFIG = ROOT / "config.json"
MANIFEST = DATA / "manifest.json"
PROGRESS = DATA / "progress.json"
VERIFY_REPORT = DATA / "verify_report.json"
LOG_FILE = DATA / "cleanup.log"

PREVIEW_LIMIT = 100  # messages read by the quick dry run (one request)
VERIFY_SAMPLE_SIZE = 300  # manifest IDs looked up one by one during verify (lookups by ID are rate-limited)
RETRYABLE = (ConnectionError, OSError, asyncio.TimeoutError, errors.ServerError, errors.TimedOutError)

log = logging.getLogger("cleanup")


class Abort(Exception):
    """A check failed. Stop without deleting anything (further)."""


# ---------------------------------------------------------------- setup

def prepare_data_dir() -> None:
    os.umask(0o077)  # every file we or Telethon create (session, manifest, logs) is owner-only
    DATA.mkdir(mode=0o700, exist_ok=True)
    os.chmod(DATA, 0o700)
    handler = logging.FileHandler(LOG_FILE)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.WARNING)
    log.setLevel(logging.INFO)


def lock_down_session(cfg: core.Config) -> None:
    for path in DATA.glob(f"{cfg.session_name}.session*"):
        os.chmod(path, 0o600)


def api_credentials() -> tuple[int, str]:
    api_id = os.environ.get("TG_API_ID") or getpass.getpass("API ID from my.telegram.org (hidden): ")
    api_hash = os.environ.get("TG_API_HASH") or getpass.getpass("API hash from my.telegram.org (hidden): ")
    api_id, api_hash = api_id.strip(), api_hash.strip()
    if not api_id.isdigit():
        raise Abort("API ID must be a number.")
    if len(api_hash) != 32:
        raise Abort("API hash should be 32 characters long.")
    return int(api_id), api_hash


def make_client(cfg: core.Config) -> TelegramClient:
    api_id, api_hash = api_credentials()
    return TelegramClient(str(DATA / cfg.session_name), api_id, api_hash)


@contextlib.asynccontextmanager
async def logged_in(cfg: core.Config, *, allow_login: bool):
    client = make_client(cfg)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            if not allow_login:
                raise Abort("This session is not logged in. Run `python cleanup.py identify` first.")
            print("Logging in. Telegram will send a code to this account's Telegram app (or by SMS).")
            print("Type it here yourself. Never forward the code or send it in any chat.\n")
            await client.start(
                phone=lambda: input("Phone number of the account (international format, e.g. +15551234567): "),
                code_callback=lambda: getpass.getpass("Login code (hidden): "),
                password=lambda: getpass.getpass("Two-step verification password (hidden): "),
            )
        lock_down_session(cfg)
        me = await client.get_me()
        if me is None or me.bot:
            raise Abort("The logged-in session is not a normal user account.")
        if cfg.self_user_id and me.id != cfg.self_user_id:
            raise Abort(f"Logged in as user {me.id}, but config.json self_user_id is {cfg.self_user_id}. "
                        "Wrong account.")
        yield client, me
    finally:
        await client.disconnect()
        lock_down_session(cfg)


async def verified_target(client: TelegramClient, cfg: core.Config, me: types.User) -> types.User:
    """Resolve target_user_id and make sure it is a normal one-to-one chat with a real person."""
    try:
        entity = await client.get_entity(types.PeerUser(cfg.target_user_id))
    except (ValueError, errors.RPCError):
        entity = None
        async for dialog in client.iter_dialogs():
            if isinstance(dialog.entity, types.User) and dialog.entity.id == cfg.target_user_id:
                entity = dialog.entity
                break
    if not isinstance(entity, types.User) or entity.id != cfg.target_user_id:
        raise Abort(f"No private chat with user {cfg.target_user_id} found on this account.")
    if entity.is_self or entity.id == me.id:
        raise Abort("Target is this account itself (Saved Messages).")
    if entity.bot:
        raise Abort("Target is a bot, not a person.")
    if entity.deleted:
        raise Abort("Target is a deleted account.")
    return entity


# ---------------------------------------------------------------- message helpers

def name_of(user: types.User) -> str:
    return " ".join(filter(None, [user.first_name, user.last_name])) or "(no name)"


def describe(user: types.User) -> str:
    handle = f" @{user.username}" if user.username else ""
    return f"{name_of(user)}{handle} [id {user.id}]"


def row(label: str, n: int, note: str = "") -> None:
    print(f"  {label:<44}{n:>9,}   {note}".rstrip())


def kind_of(m) -> str:
    if isinstance(m, types.MessageService):
        return "service:" + type(m.action).__name__.removeprefix("MessageAction")
    # Order matters: gif/video_note are videos, voice is audio, all of them are documents.
    for attr in ("sticker", "gif", "voice", "video_note", "video", "audio", "photo",
                 "poll", "geo", "contact", "dice", "document"):
        if getattr(m, attr, None):
            return attr
    if isinstance(m.media, types.MessageMediaWebPage):
        return "text+link"
    if m.media:
        return "media:" + type(m.media).__name__.removeprefix("MessageMedia")
    return "text"


def facts(m) -> core.MessageFacts:
    fwd = getattr(m, "fwd_from", None)  # MessageService has no fwd_from
    return core.MessageFacts(
        id=m.id,
        date=m.date,
        out=bool(m.out),
        peer_id=utils.get_peer_id(m.peer_id) if m.peer_id else None,
        from_id=utils.get_peer_id(m.from_id) if m.from_id else None,
        kind=kind_of(m),
        imported=bool(fwd and fwd.imported),
    )


def preview(m, width: int = 40) -> str:
    text = "" if isinstance(m, types.MessageService) else (m.message or "")
    text = " ".join(text.split())
    return text[:width] + ("…" if len(text) > width else "")


def local(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z")


async def call(client: TelegramClient, cfg: core.Config, request, what: str):
    """Send one request; wait out rate limits exactly (+margin) and retry transient failures."""
    attempt = 0
    while True:
        try:
            return await client(request)
        except errors.FloodError as e:
            seconds = getattr(e, "seconds", None) or 60
            wait = seconds + cfg.flood_wait_margin_seconds
            log.warning("rate limited during %s: Telegram requires %ss", what, seconds)
            print(f"\n  Telegram rate limit: required wait {seconds}s, waiting {wait:.0f}s "
                  f"(Ctrl+C is safe; re-run to resume)...", flush=True)
            await asyncio.sleep(wait)
        except RETRYABLE as e:
            attempt += 1
            if attempt > cfg.max_retries:
                log.error("%s failed after %d retries: %s", what, cfg.max_retries, type(e).__name__)
                raise
            backoff = min(60, 2 ** attempt)
            log.warning("%s: %s, retry %d/%d in %ss", what, type(e).__name__, attempt, cfg.max_retries, backoff)
            print(f"\n  Network/server problem ({type(e).__name__}); "
                  f"retry {attempt}/{cfg.max_retries} in {backoff}s...", flush=True)
            await asyncio.sleep(backoff)
            if not client.is_connected():
                await client.connect()


async def fetch_by_id(client, cfg, ids: list[int], what: str) -> dict[int, core.MessageFacts]:
    """Look messages up by ID. Returns only the ones that still exist (in any chat)."""
    found = {}
    for chunk in core.chunks(ids, core.MAX_BATCH_SIZE):
        request = functions.messages.GetMessagesRequest(id=[types.InputMessageID(i) for i in chunk])
        result = await call(client, cfg, request, what)
        for m in getattr(result, "messages", []):
            if not isinstance(m, types.MessageEmpty):
                found[m.id] = facts(m)
    return found


async def scan_window(client, target, start: datetime, end: datetime, limit: int | None = None):
    """Walk the chat newest-first and collect every message that appeared inside [start, end).

    Stops at the first message from before `start`, so older history is never read
    (or after `limit` messages, for the quick preview).
    """
    after_end, window, previews, before = 0, [], {}, None
    async for m in client.iter_messages(target, limit=limit):
        if m.date >= end:
            after_end += 1
            continue
        if m.date < start:
            before = facts(m)
            break
        window.append(facts(m))
        previews[m.id] = preview(m)
        if len(window) % 500 == 0:
            print(f"\r  read {len(window):,} messages from that day...", end="", flush=True)
    print("\r" + " " * 60 + "\r", end="", flush=True)
    return after_end, window, previews, before


def stamp(f: core.MessageFacts | None) -> dict | None:
    return {"id": f.id, "date_utc": f.date.isoformat()} if f else None


# ---------------------------------------------------------------- commands

async def cmd_identify(cfg: core.Config) -> None:
    async with logged_in(cfg, allow_login=True) as (client, me):
        phone = f"+…{me.phone[-4:]}" if me.phone else "hidden"
        print(f"\nLogged in as: {describe(me)}   phone {phone}")
        print(f"  -> config.json  \"self_user_id\": {me.id}\n")
        print("Recent private chats. Find the recipient and put that number in \"target_user_id\":")
        async for dialog in client.iter_dialogs(limit=40):
            e = dialog.entity
            if isinstance(e, types.User) and not e.bot and not e.is_self:
                print(f"  {e.id:>14}   {describe(e)}")
        print(f"\nSession saved to {DATA}/ (owner-only permissions). Next: fill config.json, then "
              "`python cleanup.py scan`.")


def print_header(me, target, cfg: core.Config, start: datetime, end: datetime) -> None:
    print(f"Account: {describe(me)}")
    print(f"Chat:    {describe(target)}  (private one-to-one chat)")
    print(f"Day:     messages that appeared on {cfg.date} in {cfg.timezone} = "
          f"{start:%Y-%m-%d %H:%M}Z → {end:%Y-%m-%d %H:%M}Z (UTC)")


def print_sample(title: str, rows, me, target, tz: ZoneInfo, previews: dict) -> None:
    print(title)
    for f in rows:
        sender = name_of(me) if f.out else name_of(target)
        origin = "IMPORTED" if f.imported else "real"
        print(f"  id {f.id:<10} {local(f.date, tz)}  {origin:<8} {sender[:14]:<14} {f.kind:<10} "
              f"{previews.get(f.id, '')!r}")


async def cmd_preview(cfg: core.Config, limit: int = PREVIEW_LIMIT) -> None:
    start, end = core.day_window_utc(cfg.date, cfg.timezone)
    tz = ZoneInfo(cfg.timezone)
    async with logged_in(cfg, allow_login=True) as (client, me):
        target = await verified_target(client, cfg, me)
        print_header(me, target, cfg, start, end)
        total = (await client.get_messages(target, limit=0)).total
        after_end, window, previews, before = await scan_window(client, target, start, end, limit=limit)

    selected = [f for f in window if core.is_deletable(f, cfg, start, end)]
    kept = [f for f in window if not core.is_deletable(f, cfg, start, end)]
    print("\n======  QUICK PREVIEW (dry run step 1) — nothing deleted, nothing written  ======")
    print(f"Checked the newest {after_end + len(window) + bool(before):,} of {total:,} messages in this chat.")
    row(f"appeared after {cfg.date}", after_end, "kept")
    row(f"appeared {cfg.date}, IMPORTED", len(selected), "<- would be deleted")
    row(f"appeared {cfg.date}, sent normally in Telegram", len(kept), "kept")
    if before:
        print(f"Reached the last message before {cfg.date} (id {before.id}, {local(before.date, tz)}); "
              "nothing from there back is in scope.")
    else:
        print(f"Stopped after {limit} messages; there are more from {cfg.date}. "
              "The full scan reads all of them (about 1 second per 100).")
    if selected:
        print_sample("Would delete (newest 5):", selected[:5], me, target, tz, previews)
    if kept:
        print_sample("Would keep (newest 5):", kept[:5], me, target, tz, previews)
    if not selected:
        print("No imported messages from that day among these. Don't continue until that makes sense.")
    print("\nLooks right? Step 2, the full dry run that writes the manifest: python cleanup.py scan")


async def cmd_scan(cfg: core.Config) -> None:
    start, end = core.day_window_utc(cfg.date, cfg.timezone)
    tz = ZoneInfo(cfg.timezone)
    async with logged_in(cfg, allow_login=True) as (client, me):
        target = await verified_target(client, cfg, me)
        print_header(me, target, cfg, start, end)
        print(f"Reading every message from {cfg.date} (about 1 second per 100 messages)...")
        total = (await client.get_messages(target, limit=0)).total
        after_end, window, previews, before = await scan_window(client, target, start, end)
        oldest = await client.get_messages(target, limit=1, reverse=True)

    matches = sorted((f for f in window if core.is_deletable(f, cfg, start, end)), key=lambda f: f.id)
    match_ids = [f.id for f in matches]
    match_set = set(match_ids)
    kept = [f for f in window if f.id not in match_set]
    from_me = sum(1 for f in matches if f.out)
    count_before = total - after_end - len(window)
    digest = core.ids_digest(match_ids)

    manifest = {
        "manifest_version": 2,
        "note": "Message IDs and metadata only. This is NOT a backup of message contents.",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "self_user_id": me.id,
        "target_user_id": target.id,
        "date": cfg.date.isoformat(),
        "timezone": cfg.timezone,
        "selection_rule": core.SELECTION_RULE,
        "window_start_utc": start.isoformat(),
        "window_end_utc": end.isoformat(),
        "chat_total_at_scan": total,
        "count_before_window": count_before,
        "count_in_window": len(window),
        "count_after_window": after_end,
        "preserved_in_window_ids": sorted(f.id for f in kept),
        "sentinel_before": stamp(before),
        "oldest_message": stamp(facts(oldest[0]) if oldest else None),
        "kinds": dict(Counter(f.kind for f in matches).most_common()),
        "match_count": len(matches),
        "match_count_shown_as_sent_by_self": from_me,
        "match_count_shown_as_sent_by_target": len(matches) - from_me,
        "match_ids_sha256": digest,
        "matches": [{"id": f.id, "date_utc": f.date.isoformat(), "out": f.out, "kind": f.kind}
                    for f in matches],
    }
    if PROGRESS.exists() and core.read_json(PROGRESS).get("manifest_sha256") != digest:
        archived = DATA / f"progress.superseded-{datetime.now():%Y%m%d-%H%M%S}.json"
        PROGRESS.rename(archived)
        print(f"(Previous progress file belonged to an older manifest; moved to {archived.name})")
    core.write_json_private(MANIFEST, manifest)
    log.info("scan: %d matches, %d kept on that day, %d before, %d after, digest %s",
             len(matches), len(kept), count_before, after_end, digest[:12])

    print("\n================  FULL DRY RUN (step 2) — NOTHING WAS DELETED  ================")
    row("Messages in this chat right now", total)
    row(f"appeared before {cfg.date} (not read)", count_before, "kept")
    row(f"appeared {cfg.date}", len(window))
    row("  IMPORTED", len(matches), "<- WOULD BE DELETED")
    row(f"    shown as sent by {name_of(me)}", from_me)
    row(f"    shown as sent by {name_of(target)}", len(matches) - from_me)
    row("  sent normally in Telegram", len(kept), "kept")
    row(f"appeared after {cfg.date}", after_end, "kept")
    print(f"Would delete {len(matches):,}; would keep {total - len(matches):,}.")
    if matches:
        first, last = min(matches, key=lambda f: f.date), max(matches, key=lambda f: f.date)
        print(f"Earliest match appeared {local(first.date, tz)}; latest {local(last.date, tz)}")
        print("By type:  " + ", ".join(f"{k} {v:,}" for k, v in manifest["kinds"].items()))
        print_sample("Sample (first 3 and last 3):",
                     matches[:3] + matches[-3:] if len(matches) > 6 else matches, me, target, tz, previews)
    else:
        print("No imported messages appeared that day. Nothing would be deleted.")
    print(f"\nManifest written to {MANIFEST}")
    print("It lists message IDs and metadata only. It is NOT a backup of what the messages said;")
    print("nothing can be restored from it. Export the chat first if you want to keep the content.")
    print("\nNext (only after both of you have reviewed this): python cleanup.py delete")


async def cmd_delete(cfg: core.Config) -> None:
    if not MANIFEST.exists():
        raise Abort("No manifest yet. Run the dry run first: python cleanup.py scan")
    manifest = core.read_json(MANIFEST)
    problems = core.manifest_problems(manifest, cfg)
    if problems:
        raise Abort("Manifest check failed:\n  " + "\n  ".join(problems))
    try:
        progress = core.load_progress(PROGRESS, manifest["match_ids_sha256"])
    except ValueError as e:
        raise Abort(str(e)) from None
    start, end = core.day_window_utc(cfg.date, cfg.timezone)
    manifest_ids = sorted(m["id"] for m in manifest["matches"])
    # Newest first, so the chat visibly empties from the bottom up.
    pending = sorted((i for i in manifest_ids if i not in progress.done_ids()), reverse=True)
    if not pending:
        print("Every message in the manifest is already confirmed gone. Run: python cleanup.py verify")
        return

    async with logged_in(cfg, allow_login=False) as (client, me):
        target = await verified_target(client, cfg, me)
        print(f"Pre-check: re-reading {cfg.date} from Telegram to re-validate {len(pending):,} manifest IDs "
              "(nothing is deleted during this step)...")
        _, window, _, _ = await scan_window(client, target, start, end)
        by_id = {f.id: f for f in window}
        to_delete, absent, mismatched = [], [], []
        for i in pending:
            f = by_id.get(i)
            if f is None:
                absent.append(i)
            elif core.is_deletable(f, cfg, start, end):
                to_delete.append(i)
            else:
                mismatched.append(i)
        if absent:
            # Not in that day's history any more: confirm by ID that they're really gone, not elsewhere.
            still_exist = await fetch_by_id(client, cfg, absent, "confirm absent IDs")
            mismatched += sorted(still_exist)
            absent = [i for i in absent if i not in still_exist]
        if mismatched:
            log.error("pre-check: %d manifest IDs fail the selection rule", len(mismatched))
            raise Abort(f"{len(mismatched)} manifest IDs no longer pass the checks (wrong chat, not imported, "
                        "or not imported that day). Nothing was deleted. Run `scan` again.")
        if absent:
            progress.already_absent.extend(absent)
            core.save_progress(PROGRESS, progress)
        manifest_set = set(manifest_ids)
        unlisted = sum(1 for f in window if core.is_deletable(f, cfg, start, end) and f.id not in manifest_set)
        batches = len(core.chunks(to_delete, cfg.batch_size))

        print("\n================  PRE-CHECK PASSED (nothing deleted yet)  ================")
        print(f"Account (deleting from): {describe(me)}")
        print(f"Chat:                    {describe(target)}  (private one-to-one chat)")
        print(f"Imported on:             {cfg.date} in {cfg.timezone}  "
              f"({start:%Y-%m-%d %H:%M}Z → {end:%Y-%m-%d %H:%M}Z)")
        row("In manifest", len(manifest_ids))
        row("  deleted in earlier runs", len(progress.deleted))
        row("  already gone (deleted some other way)", len(progress.already_absent))
        row("  WILL DELETE NOW, for BOTH people", len(to_delete))
        if unlisted:
            row("Imported that day but not in manifest", unlisted, "kept (re-run scan to include)")
        print("Everything else stays: every message sent normally in Telegram, by either of you, "
              "and anything not imported that day.")
        print(f"{batches} batch(es) of up to {cfg.batch_size}, {cfg.delay_seconds:g}s apart; "
              f"at least {core.format_duration(batches * cfg.delay_seconds)} plus any rate-limit waits.")
        if not to_delete:
            print("Nothing left to delete. Run: python cleanup.py verify")
            return
        print("\nWARNING: This permanently deletes these messages for both of you (revoke=True).")
        print("It cannot be undone, and the manifest is not a content backup.")
        confirm(len(to_delete), me, target, cfg)
        await delete_batches(client, cfg, to_delete, progress)


def confirm(n: int, me: types.User, target: types.User, cfg: core.Config) -> None:
    if not sys.stdin.isatty():
        raise Abort("Confirmations must be typed in an interactive terminal.")
    steps = [
        (f"ACCOUNT OWNER {describe(me)}: {n:,} imported messages (yours and theirs) will be deleted",
         "OWNER APPROVES"),
        (f"OTHER PERSON {describe(target)}: they will disappear from your chat too",
         "OTHER PERSON APPROVES"),
        ("FINAL: this cannot be undone", f"DELETE {n} MESSAGES"),
    ]
    for step, (who, phrase) in enumerate(steps, 1):
        print(f"\n[{step}/3] {who}.")
        if input(f"      Type exactly  {phrase}  and press Enter: ").strip() != phrase:
            raise Abort("Confirmation did not match. Nothing was deleted.")
    log.info("deletion confirmed for %d messages", n)


async def delete_batches(client, cfg: core.Config, ids: list[int], progress: core.Progress) -> None:
    batches = core.chunks(ids, cfg.batch_size)
    client.flood_sleep_threshold = 0  # surface every FloodWait to call() so it waits the exact time + margin
    started = time.monotonic()
    print(f"\nDeleting {len(ids):,} messages in {len(batches)} batches. Ctrl+C is safe; "
          "re-run `python cleanup.py delete` to resume.\n")
    for n, batch in enumerate(batches, 1):
        request = functions.messages.DeleteMessagesRequest(id=batch, revoke=True)
        result = await call(client, cfg, request, f"delete batch {n}/{len(batches)}")
        if result.pts_count == len(batch):
            gone, left = batch, []  # Telegram reports every ID in the batch as deleted
        else:
            # Fewer reported than sent (e.g. a retried batch): look each one up instead of assuming.
            present = await fetch_by_id(client, cfg, batch, f"check batch {n}")
            gone = [i for i in batch if i not in present]
            left = [i for i in batch if i in present]
        progress.deleted.extend(gone)
        for i in left:
            progress.failed[str(i)] = "still present after delete call"
        progress.batches_completed += 1
        core.save_progress(PROGRESS, progress)
        log.info("batch %d/%d: sent %d, reported deleted %d, confirmed gone %d, still present %d",
                 n, len(batches), len(batch), result.pts_count, len(gone), len(left))

        remaining = sum(len(b) for b in batches[n:])
        eta = (time.monotonic() - started) / n * (len(batches) - n)
        bar = "#" * (n * 30 // len(batches))
        print(f"\r  [{bar:<30}] batch {n}/{len(batches)} | deleted {len(ids) - remaining - len(left):,} "
              f"| remaining {remaining:,} | ETA {core.format_duration(eta)}   ", end="", flush=True)
        if left:
            print()
            raise Abort(f"{len(left)} messages in batch {n} still exist after the delete call. Stopped. "
                        "Re-run `python cleanup.py delete` to retry them; see data/cleanup.log.")
        if n < len(batches):
            await asyncio.sleep(cfg.delay_seconds)
    print(f"\n\nDone in {core.format_duration(time.monotonic() - started)}. Now run: python cleanup.py verify")


async def cmd_verify(cfg: core.Config) -> None:
    if not MANIFEST.exists():
        raise Abort("No manifest to verify against. Run `python cleanup.py scan` first.")
    manifest = core.read_json(MANIFEST)
    problems = core.manifest_problems(manifest, cfg)
    if problems:
        raise Abort("Manifest check failed:\n  " + "\n  ".join(problems))
    try:
        progress = core.load_progress(PROGRESS, manifest["match_ids_sha256"])
    except ValueError:
        progress = core.Progress(manifest_sha256=manifest["match_ids_sha256"])
    start, end = core.day_window_utc(cfg.date, cfg.timezone)
    manifest_ids = {m["id"] for m in manifest["matches"]}
    sample = random.sample(sorted(manifest_ids), min(VERIFY_SAMPLE_SIZE, len(manifest_ids)))
    sentinels = {k: manifest[k]["id"] for k in ("sentinel_before", "oldest_message") if manifest.get(k)}

    async with logged_in(cfg, allow_login=False) as (client, me):
        target = await verified_target(client, cfg, me)
        print(f"Re-reading {describe(target)} for {cfg.date}...")
        total_now = (await client.get_messages(target, limit=0)).total
        after_end, window, _, _ = await scan_window(client, target, start, end)
        sample_present = await fetch_by_id(client, cfg, sample, "verify sample")
        sentinel_present = await fetch_by_id(client, cfg, list(sentinels.values()), "verify older messages")

    window_ids = {f.id for f in window}
    still_in_day = sorted(manifest_ids & window_ids)
    preserved = set(manifest["preserved_in_window_ids"])
    preserved_missing = sorted(preserved - window_ids)
    count_before_now = total_now - after_end - len(window)
    new_unlisted = sorted(f.id for f in window if core.is_deletable(f, cfg, start, end) and f.id not in manifest_ids)

    checks = [
        ("Manifest messages gone from that day's history", not still_in_day,
         f"{len(manifest_ids) - len(still_in_day):,}/{len(manifest_ids):,} gone"),
        ("Random sample looked up one by one by ID", not sample_present,
         f"{len(sample)} checked, {len(sample_present)} still exist"),
        ("Messages sent normally in Telegram that day all present", not preserved_missing,
         f"{len(preserved) - len(preserved_missing):,}/{len(preserved):,} present"),
        (f"Message count before {cfg.date} unchanged", count_before_now == manifest["count_before_window"],
         f"{count_before_now:,} now, {manifest['count_before_window']:,} at scan"),
    ]
    for key, label in (("sentinel_before", f"Last message before {cfg.date} still exists"),
                       ("oldest_message", "Oldest message in the chat still exists")):
        if key in sentinels:
            checks.append((label, sentinels[key] in sentinel_present, f"id {sentinels[key]}"))

    report = {
        "verified_utc": datetime.now(timezone.utc).isoformat(),
        "checks": [{"check": c, "passed": ok, "detail": d} for c, ok, d in checks],
        "manifest_ids_still_present": sorted(set(still_in_day) | set(sample_present)),
        "preserved_ids_missing": preserved_missing,
        "unlisted_matching_ids": new_unlisted,
        "progress": {"deleted": len(progress.deleted), "already_absent": len(progress.already_absent),
                     "failed": progress.failed},
    }
    core.write_json_private(VERIFY_REPORT, report)
    log.info("verify: %s", {c: ok for c, ok, _ in checks})

    print("\n================  VERIFICATION (as seen from this account)  ================")
    for check, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {check}: {detail}")
    if new_unlisted:
        print(f"  [NOTE] {len(new_unlisted)} imported messages appeared after the scan and were not deleted. "
              "Run scan + delete again if they should go too.")
    if progress.failed:
        print(f"  [NOTE] {len(progress.failed)} IDs failed earlier; see {PROGRESS.name} and run delete again.")
    print(f"\nReport saved to {VERIFY_REPORT}")
    print("This checks the logged-in account's view only. The other person must also look on their own device.")


async def cmd_logout(cfg: core.Config) -> None:
    session_file = DATA / f"{cfg.session_name}.session"
    if not session_file.exists():
        print("No session file; nothing to log out.")
        return
    client = make_client(cfg)
    await client.connect()
    if await client.is_user_authorized():
        await client.log_out()  # ends the login on Telegram's side and deletes the session file
    else:
        await client.disconnect()
        session_file.unlink(missing_ok=True)
    print("Logged out; the local session file is removed.")


COMMANDS = {"identify": cmd_identify, "preview": cmd_preview, "scan": cmd_scan, "delete": cmd_delete,
            "verify": cmd_verify, "logout": cmd_logout}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="preview", choices=COMMANDS)
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--limit", type=int, default=PREVIEW_LIMIT,
                        help=f"messages checked by `preview` (default {PREVIEW_LIMIT})")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    prepare_data_dir()
    try:
        cfg = core.load_config(args.config)
        if args.command not in ("identify", "logout"):
            cfg.require_ids()
        kwargs = {"limit": args.limit} if args.command == "preview" else {}
        asyncio.run(COMMANDS[args.command](cfg, **kwargs))
    except (Abort, core.ConfigError) as e:
        log.error("stopped: %s", e)
        print(f"\nSTOPPED: {e}", file=sys.stderr)
        sys.exit(1)
    except errors.RPCError as e:
        log.error("telegram error: %s", type(e).__name__)
        print(f"\nSTOPPED: Telegram error {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted. Progress is saved after every finished batch in data/progress.json; "
              "run the same command again to resume.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
