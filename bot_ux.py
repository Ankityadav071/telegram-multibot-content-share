"""Video Vault final chat UX helpers.
Pure presentation helpers; does not alter database or upload behavior.
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def welcome_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Catalogue", callback_data="ux_catalogue"),
         InlineKeyboardButton("📦 Upload", callback_data="ux_upload")],
        [InlineKeyboardButton("📅 Schedule", callback_data="ux_schedule"),
         InlineKeyboardButton("📊 Dashboard", callback_data="ux_dashboard")],
        [InlineKeyboardButton("❓ Help", callback_data="ux_help")],
    ])

def status_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="ux_refresh"),
         InlineKeyboardButton("🏠 Home", callback_data="ux_home")]
    ])

def progress_text(done, total, label="Processing", detail=""):
    total=max(int(total or 0),1); done=max(0,min(int(done or 0),total))
    filled=round(done/total*10)
    bar="▰"*filled+"▱"*(10-filled)
    pct=int(round(done*100/total))
    state="✨ Done" if done >= total else "⏳ Working…"
    extra=f"\n{detail}" if detail else ""
    return f"{state}\n\n{bar}  <b>{pct}%</b>  ·  {done}/{total}\n🔄 {label}{extra}"

def success_text(label="Saved"):
    return f"✅ <b>{label} complete</b>\n\nEverything is safely in place. ✨"

def error_text(label="Something went wrong", hint="Please try again."):
    return f"⚠️ <b>{label}</b>\n\n{hint}\n\n💡 If it keeps happening, open Help and send the error."

def sending_text(kind="file", done=0, total=1):
    icons={"video":"🎬","image":"🖼️","file":"📄","batch":"📦"}
    icon=icons.get(kind,"📤")
    total=max(int(total or 1),1); done=max(0,min(int(done or 0),total))
    if done >= total:
        return f"{icon} <b>Sent</b>  ·  {done}/{total}\n\n✅ Delivered successfully"
    return f"{icon} <b>Sending…</b>  ·  {done}/{total}\n\n⬆️ Please keep this chat open"

def checking_text(subject="membership"):
    return f"🔎 <b>Checking {subject}…</b>\n\n⏳ Just a moment — I'm verifying it now."

def verified_text(subject="membership"):
    return f"✅ <b>{subject.title()} verified</b>\n\n✨ You're all set."

def destructive_confirmation_text(action, count=None):
    scope = "this item" if count is None else ("1 item" if count == 1 else f"{count} items")
    return f"⚠️ Confirm {action}\n\nThis will affect {scope}.\nPlease confirm before continuing."

def action_success_text(action):
    return f"✅ {action} completed successfully."

def action_failure_text(action):
    return f"⚠️ Couldn't complete {action}. Please try again."

# V6.5 UX presentation helpers — no persistence/API behavior.
def step_menu(current, total, title, subtitle=""):
    current=max(1,int(current)); total=max(current,int(total))
    bar="".join("●" if i<=current else "○" for i in range(total))
    text=f"{bar}\n<b>{title}</b>"
    if subtitle: text+=f"\n{subtitle}"
    return text

def compact_action_label(action):
    return {
        "upload":"📤 Upload","cover":"🖼️ Cover","details":"📝 Details",
        "save":"💾 Save","schedule":"📅 Schedule","cancel":"↩️ Cancel",
        "back":"‹ Back",
    }.get(action, action)

# V6.6 admin UX presentation helpers — no data mutation.
def admin_section_text(title, count=None, hint=""):
    suffix = f" · {count}" if count is not None else ""
    text = f"<b>{title}{suffix}</b>"
    if hint:
        text += f"\n{hint}"
    return text

def admin_empty_text(section="content"):
    return {
        "content": "📭 No content here yet.",
        "scheduled": "📅 No scheduled content.",
        "failed": "⚠️ No failed jobs.",
        "activity": "🧾 No recent activity.",
    }.get(section, "📭 Nothing to show.")

# V6.7 user-facing UX presentation helpers — no persistence changes.
def profile_header_text(name, subtitle="", stats=None):
    text = f"<b>👤 {name}</b>"
    if subtitle:
        text += f"\n{subtitle}"
    if stats:
        text += "\n" + "  ·  ".join(f"{k}: {v}" for k,v in stats.items())
    return text

def catalogue_item_text(title, meta="", status=""):
    text = f"<b>{title}</b>"
    if meta:
        text += f"\n{meta}"
    if status:
        text += f"\n{status}"
    return text

def navigation_label(action):
    return {
        "home":"⌂ Home","catalogue":"▦ Catalogue","profile":"👤 Profile",
        "search":"🔎 Search","back":"‹ Back","next":"Next ›",
    }.get(action, action)

# V6.8 micro-interaction copy helpers — presentation only.
def transient_status_text(state, detail=""):
    labels = {
        "working": "⏳ Working…",
        "success": "✅ Done",
        "warning": "⚠️ Needs attention",
        "error": "❌ Something went wrong",
        "cancelled": "↩️ Cancelled",
    }
    return labels.get(state, "⏳ Working…") + (f"\n{detail}" if detail else "")

def action_feedback_text(action, result="success"):
    if result == "success":
        return f"✅ {action} complete."
    if result == "cancelled":
        return f"↩️ {action} cancelled."
    return f"⚠️ {action} needs attention."

# V6.9 personality/copy layer — presentation only.
def chatty_message(kind, name=None, detail=""):
    who=f" {name}" if name else ""
    messages={
        "welcome":f"Hey{who} ✨ I'm ready. Send me a video, image, or file and I'll handle the boring bits.",
        "received":"Okii, got it 👀📥 I'm checking your upload now…",
        "processing":"⏳ Putting everything together…",
        "cover_ready":"Ooo, cover is ready 🖼️✨",
        "details_ready":"Nicee ✨ details are ready. One little check before we save it.",
        "saving":"Saving it safely now 💾💕",
        "saved":"✅ Saved successfully!",
        "scheduled":"Locked in 📅✨ I'll keep the schedule as-is.",
        "cancelled":"↩️ Cancelled safely. Nothing was changed.",
        "nothing_found":"🔎 Nothing matched that search.",
        "try_again":"That didn't go through this time 😭 Please try again.",
        "permission":"🔒 You don't have permission for that.",
        "busy":"⏳ I'm already working on it. Please wait a moment.",
        "confirm":"Just checking before I do anything important 👀",
    }
    text=messages.get(kind,"Okii, working on it ✨")
    return text+(f"\n{detail}" if detail else "")

def chatty_success(action, detail=""):
    return chatty_message("saved", detail=f"{action} ✨"+(f"\n{detail}" if detail else ""))

def chatty_failure(detail=""):
    return chatty_message("try_again", detail)

# V7.2 GODMODE UX layer — presentation/command discoverability only.
# Kept dependency-light so every bot can import it safely.
def brand_header(title, subtitle=""):
    text = f"🎞️ <b>VIDEO VAULT</b>  ·  {title}"
    if subtitle:
        text += f"\n<i>{subtitle}</i>"
    return text

def metric_line(label, value, icon="•"):
    return f"{icon} <b>{label}</b>  {value}"

def footer_hint(text="Use the buttons below or /menu anytime."):
    return f"\n\n<code>VIDEO VAULT</code>  ·  {text}"

def command_cheatsheet(commands):
    lines=["🧭 <b>Quick Commands</b>"]
    for cmd, desc in commands:
        lines.append(f"/{cmd} — {desc}")
    return "\n".join(lines)


# V7.5 premium interaction states — concise, editable-message friendly.
def delivery_state_text(state, item=None, done=None, total=None):
    icons = {
        "checking": "🔎", "verified": "🔐", "preparing": "🎬",
        "sending": "📤", "complete": "✅", "paused": "⏸️",
        "failed": "⚠️", "cancelled": "↩️",
    }
    labels = {
        "checking": "Checking access…", "verified": "Access confirmed",
        "preparing": "Preparing your video…", "sending": "Sending…",
        "complete": "Delivery complete", "paused": "Delivery paused",
        "failed": "Delivery needs attention", "cancelled": "Delivery cancelled",
    }
    icon=icons.get(state, "⏳"); label=labels.get(state, "Working…")
    text=f"{icon} <b>{label}</b>"
    if item: text += f"\n\n🎞️ {item}"
    if done is not None and total is not None:
        total=max(int(total or 1),1); done=max(0,min(int(done or 0),total))
        filled=round(done/total*10)
        text += f"\n\n{'▰'*filled}{'▱'*(10-filled)}  <b>{done}/{total}</b>"
    return text

def upload_state_text(state, item=None, done=None, total=None):
    labels={
        "received":"📥 <b>Media received</b>",
        "detecting":"🔎 <b>Detecting upload type…</b>",
        "processing":"⚙️ <b>Processing media…</b>",
        "saving":"💾 <b>Saving safely…</b>",
        "complete":"✅ <b>Upload complete</b>",
        "failed":"⚠️ <b>Upload needs attention</b>",
    }
    text=labels.get(state,"⏳ <b>Working…</b>")
    if item: text += f"\n\n{item}"
    if done is not None and total is not None:
        total=max(int(total or 1),1); done=max(0,min(int(done or 0),total))
        filled=round(done/total*10)
        text += f"\n\n{'▰'*filled}{'▱'*(10-filled)}  <b>{done}/{total}</b>"
    return text
