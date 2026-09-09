"""
👍❤️🔥😂😮👎 reaction-button feature for Delivery Bot — attached to the video
message itself, right where someone actually watches it.

One reaction per user per video — tapping the same emoji again removes it,
tapping a different one switches it. Counts are shown on the buttons
themselves (e.g. "👍 12"), and update live on every tap.

Callback data format: rx_<code>_<video_id>, e.g. rx_like_ab12cd34
Keep this under Telegram's 64-byte callback_data limit — short codes
(not raw emoji) keep every row comfortably inside that even for 8-char ids.
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import db

EMOJI_CODES = [
    ("like", "👍"),
    ("love", "❤️"),
    ("fire", "🔥"),
    ("laugh", "😂"),
    ("wow", "😮"),
    ("dislike", "👎"),
]
CODE_TO_EMOJI = dict(EMOJI_CODES)
CALLBACK_PREFIX = "rx_"


def build_rows(video_id: str, user_id: int) -> list:
    """Two rows of 3 InlineKeyboardButtons with live counts, the viewer's
    own pick (if any) marked with a leading dot."""
    counts = db.get_reaction_counts(video_id)
    mine = db.get_user_reaction(video_id, user_id)
    buttons = []
    for code, emoji in EMOJI_CODES:
        n = counts.get(code, 0)
        label = f"{emoji} {n}" if n else emoji
        if mine == code:
            label = f"• {label}"
        buttons.append(InlineKeyboardButton(label, callback_data=f"{CALLBACK_PREFIX}{code}_{video_id}"))
    return [buttons[:3], buttons[3:]]


def is_reaction_tap(callback_data: str) -> bool:
    return bool(callback_data) and callback_data.startswith(CALLBACK_PREFIX)


async def handle_tap(query, rebuild_markup) -> None:
    """Call from a bot's callback router when is_reaction_tap(query.data) is
    True. `rebuild_markup(video_id, user_id) -> InlineKeyboardMarkup` builds
    that bot's *full* keyboard (e.g. the "Back to Catalog" button plus these
    reaction rows) so the whole message stays consistent after the edit,
    not just the reaction rows in isolation."""
    parts = query.data.split("_", 2)
    if len(parts) != 3:
        await query.answer()
        return
    _, code, video_id = parts
    if code not in CODE_TO_EMOJI:
        await query.answer()
        return

    user_id = query.from_user.id
    now_set = db.toggle_reaction(video_id, user_id, code)
    await query.answer(f"{CODE_TO_EMOJI[code]} reacted!" if now_set else "Reaction removed")

    try:
        await query.edit_message_reply_markup(reply_markup=rebuild_markup(video_id, user_id))
    except Exception:
        # Message may have been auto-deleted, or Telegram reports "message is
        # not modified" if counts happened to end up the same — either way,
        # nothing the viewer needs to see an error about.
        pass
