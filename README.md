# Undo a Telegram chat import

Imported a WhatsApp (or other) chat into the wrong Telegram conversation? Telegram has no "undo import" button, and *Clear history* wipes the whole chat, real messages included.

This tool deletes **only the imported messages** from one private chat, for both people, and keeps every message that was actually sent in Telegram. It's built on [Telethon](https://github.com/LonamiWebs/Telethon) and Telegram's official MTProto API, and has been used to remove a 23,000-message import from a 600,000-message chat.

**Rule.** A message is deleted only if **all** of these hold:
- it's in the one private chat you configure, identified by numeric user ID
- Telegram marks it as **imported** (`fwd_from.imported`)
- it **appeared** in the chat on the day you configure, in your timezone

"Appeared" means when the message landed in Telegram. In the bubble text `26.05.17, 12:25 Imported 15:07`, that's the `15:07`. The original (e.g. WhatsApp) date is ignored, and only that day's messages are read.

## Safety design

- **Two-step dry run.** `preview` checks the newest 100 messages in seconds. `scan` checks the whole day and writes a manifest. Neither deletes anything.
- **Separate delete command.** `delete` never runs automatically after a dry run.
- **Pre-check before deleting.** Every manifest ID is re-validated against Telegram first. This matters because in private chats Telegram's `messages.deleteMessages` takes only IDs, not a chat, and Telethon doesn't check that the IDs belong to the chat you pass.
- **Three typed confirmations.** The account owner, the other person in the chat, and a final `DELETE <N> MESSAGES`.
- **Small, confirmed batches.** At most 100 IDs per batch, with `revoke=True` so messages are deleted for both people. A batch counts as done only when Telegram confirms it; if anything is still present afterwards, the script stops.
- **Rate limits respected.** `FloodWait` is honoured exactly, plus a margin. Network errors are retried with backoff.
- **Resumable.** Progress is saved after every batch, so Ctrl+C or a dropped connection is safe; just re-run.
- **Newest first.** Deletion goes from the newest message back, so you can watch the chat empty from the bottom.
- **`verify` afterwards.** Checks that everything selected is gone, that real messages from that day remain, and that the message count before that day is unchanged.

> ⚠️ Deletion is permanent. The manifest holds message IDs and metadata only; it is **not** a backup of message contents. Keep the original WhatsApp chat or export if you want the history.

## Requirements

- Python 3.9+
- A Telegram account in the chat (either participant works)
- An API ID and hash from https://my.telegram.org → *API development tools* (any app title; platform: Desktop)

## Setup

```sh
git clone https://github.com/LidorBaum/telegram-undo-import.git && cd telegram-undo-import
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m unittest -v
cp config.example.json config.json
```

Set `date` (the day the import happened) and `timezone` (an IANA name such as `Europe/London`, `America/New_York` or `Asia/Jerusalem`) in `config.json`.

API credentials are read from `TG_API_ID` / `TG_API_HASH` if set; otherwise you're prompted with hidden input. To set them for one terminal session without leaving them in shell history:

```sh
# zsh
read -rs "TG_API_ID?API ID: "; echo; export TG_API_ID
read -rs "TG_API_HASH?API hash: "; echo; export TG_API_HASH
# bash
read -rsp "API ID: " TG_API_ID; echo; export TG_API_ID
read -rsp "API hash: " TG_API_HASH; echo; export TG_API_HASH
```

## Usage

```sh
python cleanup.py identify   # log in (code + 2FA), print your ID and your private chats' IDs
                             # -> put them in config.json: self_user_id, target_user_id
python cleanup.py preview    # dry run 1: newest 100 messages (--limit N to change)
python cleanup.py scan       # dry run 2: the whole day; writes data/manifest.json
python cleanup.py delete     # pre-check, 3 confirmations, delete (resumable)
python cleanup.py verify     # check the result
python cleanup.py logout     # end the Telegram session and delete the session file
```

On macOS, `caffeinate -i python cleanup.py delete` keeps the machine awake during a long run. If deletion is interrupted, run `python cleanup.py delete` again; it continues where it stopped.

**Sanity check before deleting.** Telegram's unread badge counts only messages from the other person. The scan's "shown as sent by <other person>" should be close to that badge, and the total is higher because your side of the import is included.

Expected speed: about 1 s per 100 messages to read, and 2 s per 100 to delete, plus any rate-limit waits.

## Configuration (`config.json`)

| Key | Meaning |
|---|---|
| `self_user_id` | Numeric ID of the logged-in account; the script refuses to run on any other account |
| `target_user_id` | Numeric ID of the other person in the private chat |
| `date` | Day the import happened, `YYYY-MM-DD` |
| `timezone` | IANA timezone the `date` is interpreted in |
| `batch_size` | IDs per delete request (1–100) |
| `delay_seconds` | Pause between batches (min 0.5) |
| `flood_wait_margin_seconds` | Added on top of Telegram's required wait |
| `max_retries` | Retries for network or server errors |
| `session_name` | Session file name inside `data/` |

## Files and privacy

Everything is stored locally in `data/`, which is created with owner-only permissions and git-ignored:

| File | Contains |
|---|---|
| `<session_name>.session` | ⚠️ A logged-in Telegram session: full access to the account. Never share it. Run `logout` when done, and check Telegram → Settings → Devices |
| `manifest.json` | IDs, timestamps, direction and type of the selected messages. No message text |
| `progress.json` | IDs confirmed deleted. Used for resuming |
| `verify_report.json` | Result of `verify` |
| `cleanup.log` | Batch results and error types. No secrets, no message text |

The API hash is never written to disk. The only Telegram calls that change anything are `messages.deleteMessages` and `auth.logOut`. The script never sends, forwards, uploads or downloads.

## Limitations

- **Private one-to-one chats only.** Not groups, channels or Secret Chats.
- **`verify` sees only the logged-in account's view.** The other person should also check their app, and may need to reopen the chat.
- **It can't reach copies outside the chat:** notifications already shown, forwards, exports.

## Tests

```sh
python -m unittest -v    # selection rule, day boundaries and DST, config validation, batching, manifest and progress files
```

## Disclaimer

Not affiliated with Telegram. Deleting messages is irreversible; review the dry run carefully. Use at your own risk.
