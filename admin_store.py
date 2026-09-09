import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
ADMINS_FILE = BASE / 'managed_admins.json'
EDITOR_FILE = BASE / 'bot_editor_store.json'

DEFAULT_TEMPLATES = {
    'admin_start': '🛠 Admin Bot',
    'storage_start': '📥 Storage Bot\n\nSend me a video to store it. I will copy it to the primary and backup channels, then guide you through the upload details.',
    'catalog_start': '👋 Welcome!\nUse the menu below to browse uploads.',
    'delivery_start': '👋 Heyyy! Welcome in ✨\n\n🍿 Ready to pick something? Choose your vibe below 👇',
}

def _load(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default.copy() if isinstance(default, dict) else default

def _save(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)

def bootstrap_admins(config_ids):
    data = _load(ADMINS_FILE, {})
    changed = False
    for uid in config_ids or []:
        try:
            key = str(int(uid))
        except (TypeError, ValueError):
            continue
        if key not in data:
            data[key] = 'owner'
            changed = True
    if changed or not ADMINS_FILE.exists():
        _save(ADMINS_FILE, data)
    return data

def admin_ids(config_ids=()):
    data = bootstrap_admins(config_ids)
    out = set()
    for uid in list(data):
        try: out.add(int(uid))
        except (TypeError, ValueError): pass
    return out

def add_admin(user_id, role='admin'):
    data = _load(ADMINS_FILE, {})
    data[str(int(user_id))] = str(role or 'admin')
    _save(ADMINS_FILE, data)

def remove_admin(user_id, protected_ids=()):
    uid = str(int(user_id))
    protected = {str(int(x)) for x in (protected_ids or []) if str(x).lstrip('-').isdigit()}
    if uid in protected:
        return False
    data = _load(ADMINS_FILE, {})
    existed = uid in data
    data.pop(uid, None)
    _save(ADMINS_FILE, data)
    return existed

def list_admins(config_ids=()):
    return bootstrap_admins(config_ids)

def templates():
    data = _load(EDITOR_FILE, {})
    return {**DEFAULT_TEMPLATES, **data}

def get_template(name, default=None):
    return templates().get(name, default if default is not None else '')

def set_template(name, text):
    data = _load(EDITOR_FILE, {})
    data[str(name)] = str(text)
    _save(EDITOR_FILE, data)
