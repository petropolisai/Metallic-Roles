import os
import sqlite3
import asyncio
import threading
from functools import wraps
from urllib.parse import urlencode, quote

import requests
import discord
from discord.ext import commands
from flask import Flask, request, redirect, session, render_template_string

# ============================================================
# CONFIG
# ============================================================
TOKEN = os.environ.get("DISCORD_TOKEN")
CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET")
REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI")  # e.g. https://yourapp.onrender.com/callback
FLASK_SECRET = os.environ.get("FLASK_SECRET_KEY", "change-this-secret-in-production")
PORT = int(os.environ.get("PORT", 8080))
DB_PATH = "metallic_roles.db"

BOT_INVITE_PERMS = 268520448  # Manage Roles + View Channel + Send Messages + Embed Links + Read History
BRAND = "METALLIC ROLES"
EMOJI_CHOICES = ["🎉", "⭐", "📚", "🛠️", "🎮", "🎁", "💎", "🔥", "📢", "✅",
                  "🎯", "🎨", "🏆", "💰", "🎵", "📺", "🕹️", "🌟", "⚡", "🍀"]

# ============================================================
# DATABASE (sync sqlite, thread-safe)
# ============================================================
DB_LOCK = threading.Lock()
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row

def init_db():
    with DB_LOCK:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS role_panels (
            panel_id TEXT PRIMARY KEY,
            guild_id TEXT,
            channel_id TEXT,
            message_id TEXT,
            name TEXT,
            description TEXT,
            color TEXT
        );
        CREATE TABLE IF NOT EXISTS role_options (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            panel_id TEXT,
            role_id TEXT,
            role_name TEXT,
            label TEXT,
            description TEXT,
            emoji TEXT
        );
        """)
        conn.commit()

def db_run(q, p=()):
    with DB_LOCK:
        cur = conn.execute(q, p)
        conn.commit()
        return cur.lastrowid

def db_fetchall(q, p=()):
    with DB_LOCK:
        cur = conn.execute(q, p)
        return [dict(r) for r in cur.fetchall()]

def db_fetchone(q, p=()):
    with DB_LOCK:
        cur = conn.execute(q, p)
        r = cur.fetchone()
        return dict(r) if r else None

# ============================================================
# DISCORD BOT
# ============================================================
intents = discord.Intents.default()
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
BOT_LOOP = None
BOT_READY = threading.Event()

def parse_color(hex_str):
    if not hex_str:
        return discord.Color(0x99AAB5)
    cleaned = str(hex_str).replace("#", "").strip()
    try:
        return discord.Color(int(cleaned, 16))
    except Exception:
        return discord.Color(0x99AAB5)

def parse_emoji(emoji_str):
    if not emoji_str:
        return None
    try:
        return discord.PartialEmoji.from_str(emoji_str.strip())
    except Exception:
        return None

def build_embed(panel: dict, options: list):
    color = parse_color(panel.get("color"))
    embed = discord.Embed(
        title=f"⚙️ {BRAND} — {panel['name'].upper()}",
        description=panel.get("description") or "",
        color=color,
    )
    if options:
        lines = []
        for o in options:
            emoji_display = o["emoji"] if o["emoji"] else "🔘"
            lines.append(f"• {emoji_display} **{o['label']}** — {o['description']}")
        embed.description += "\n\n" + "\n".join(lines)
        embed.description += "\n\n*Select or unselect options at any time to update your ping preferences!*"
    else:
        embed.description += "\n\n*No roles configured yet.*"
    embed.set_footer(text=f"{BRAND} • Select below to toggle")
    return embed

# ---------------- Discord UI (dropdown users click) ----------------
class RoleDropdown(discord.ui.Select):
    def __init__(self, panel_id, options_data):
        opts = []
        for o in options_data:
            opts.append(discord.SelectOption(
                label=o["label"][:100],
                description=(o["description"] or "")[:100],
                value=str(o["role_id"]),
                emoji=parse_emoji(o["emoji"]),
            ))
        super().__init__(
            placeholder="Select your notification roles...",
            min_values=0, max_values=len(opts) if opts else 1,
            options=opts, custom_id=f"metallicroles:{panel_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        try:
            guild = interaction.guild
            member = interaction.user
            selected_ids = set(int(v) for v in self.values)
            all_ids = set(int(o.value) for o in self.options)

            to_add, to_remove = [], []
            for rid in all_ids:
                role = guild.get_role(rid)
                if not role:
                    continue
                has_role = role in member.roles
                if rid in selected_ids and not has_role:
                    to_add.append(role)
                elif rid not in selected_ids and has_role:
                    to_remove.append(role)

            added, removed = [], []
            try:
                if to_add:
                    await member.add_roles(*to_add, reason="Metallic Roles selection")
                    added = [r.name for r in to_add]
                if to_remove:
                    await member.remove_roles(*to_remove, reason="Metallic Roles deselection")
                    removed = [r.name for r in to_remove]
            except discord.Forbidden:
                await interaction.response.send_message(
                    "❌ I can't manage one or more of these roles. Ask an admin to move my role higher.",
                    ephemeral=True)
                return

            lines = []
            if added: lines.append("✅ **Added:** " + ", ".join(added))
            if removed: lines.append("❌ **Removed:** " + ", ".join(removed))
            if not lines: lines.append("ℹ️ No changes made.")
            await interaction.response.send_message("\n".join(lines), ephemeral=True)
        except Exception as e:
            print(f"[DROPDOWN ERROR] {e}")
            try:
                await interaction.response.send_message("❌ Something went wrong. Try again.", ephemeral=True)
            except Exception:
                pass

class RoleDropdownView(discord.ui.View):
    def __init__(self, panel_id, options_data):
        super().__init__(timeout=None)
        if options_data:
            self.add_item(RoleDropdown(panel_id, options_data))

# ---------------- Bot-side async helpers used by dashboard ----------------
async def get_bot_guild_ids():
    return set(str(g.id) for g in bot.guilds)

async def get_dashboard_guild_data(guild_id):
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return None
    me = guild.me
    channels = [{"id": str(c.id), "name": c.name} for c in guild.text_channels
                if c.permissions_for(me).send_messages]
    roles = []
    for r in sorted(guild.roles, reverse=True):
        if r.is_default() or r.managed:
            continue
        roles.append({"id": str(r.id), "name": r.name, "above_bot": r >= me.top_role})
    health = {
        "manage_roles": me.guild_permissions.manage_roles,
        "top_role_name": me.top_role.name,
    }
    icon_url = str(guild.icon.url) if guild.icon else None
    return {"channels": channels, "roles": roles, "health": health,
            "guild_name": guild.name, "guild_icon": icon_url}

async def create_panel_discord_message(guild_id, channel_id, panel_data):
    guild = bot.get_guild(int(guild_id))
    channel = guild.get_channel(int(channel_id))
    embed = build_embed(panel_data, [])
    msg = await channel.send(embed=embed)
    return str(msg.id)

async def update_panel_message(panel_id):
    panel = db_fetchone("SELECT * FROM role_panels WHERE panel_id=?", (panel_id,))
    if not panel:
        return False, "Panel not found."
    guild = bot.get_guild(int(panel["guild_id"]))
    if not guild:
        return False, "Bot is not in this server anymore."
    channel = guild.get_channel(int(panel["channel_id"]))
    if not channel:
        return False, "Channel was deleted."
    options = db_fetchall("SELECT * FROM role_options WHERE panel_id=? ORDER BY id ASC", (panel_id,))
    embed = build_embed(panel, options)
    view = RoleDropdownView(panel_id, options) if options else None
    try:
        message = await channel.fetch_message(int(panel["message_id"]))
    except discord.NotFound:
        return False, "Original message was deleted. Delete and recreate this panel."
    except discord.Forbidden:
        return False, "No permission to view that channel."
    try:
        await message.edit(embed=embed, view=view)
    except discord.Forbidden:
        return False, "No permission to edit messages there."
    if view:
        bot.add_view(view, message_id=int(panel["message_id"]))
    return True, "OK"

async def delete_panel_message(guild_id, channel_id, message_id):
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return
    channel = guild.get_channel(int(channel_id))
    if not channel:
        return
    try:
        msg = await channel.fetch_message(int(message_id))
        await msg.delete()
    except (discord.NotFound, discord.Forbidden):
        pass

def run_async(coro, timeout=15):
    if BOT_LOOP is None:
        raise RuntimeError("Bot is still starting up. Please wait a few seconds and refresh.")
    future = asyncio.run_coroutine_threadsafe(coro, BOT_LOOP)
    return future.result(timeout=timeout)

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} — Metallic Roles online.")
    panels = db_fetchall("SELECT * FROM role_panels")
    for panel in panels:
        options = db_fetchall("SELECT * FROM role_options WHERE panel_id=?", (panel["panel_id"],))
        if options:
            view = RoleDropdownView(panel["panel_id"], options)
            bot.add_view(view, message_id=int(panel["message_id"]))
    try:
        await bot.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching, name="the Metallic Roles dashboard"))
    except Exception:
        pass
    BOT_READY.set()

def start_bot_thread():
    global BOT_LOOP
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    BOT_LOOP = loop
    loop.run_until_complete(bot.start(TOKEN))

# ============================================================
# FLASK APP
# ============================================================
app = Flask(__name__)
app.secret_key = FLASK_SECRET

def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if "user" not in session:
            return redirect("/login")
        return f(*a, **kw)
    return wrapper

def guild_access(f):
    @wraps(f)
    def wrapper(*a, **kw):
        guild_id = kw.get("guild_id")
        allowed = [g["id"] for g in session.get("guilds", [])]
        if guild_id is None or str(guild_id) not in allowed:
            return redirect("/dashboard")
        return f(*a, **kw)
    return wrapper

def flash_url(base, ok, msg):
    return f"{base}?ok={1 if ok else 0}&msg={quote(msg)}"

# ---------------- CSS / LAYOUT ----------------
CSS = """
:root{--bg:#1e1f22;--bg2:#2b2d31;--bg3:#313338;--text:#f2f3f5;--muted:#96989d;
--accent:#5865F2;--accent2:#99aab5;--danger:#ed4245;--success:#3ba55d;--radius:10px;}
*{box-sizing:border-box;}
body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,'Segoe UI',Roboto,sans-serif;}
nav{background:var(--bg2);padding:14px 28px;display:flex;justify-content:space-between;align-items:center;
border-bottom:1px solid #1a1b1e;}
nav .brand{font-weight:700;font-size:18px;letter-spacing:.5px;}
nav .brand span{color:var(--accent2);}
nav .user{display:flex;align-items:center;gap:10px;color:var(--muted);font-size:14px;}
nav a{color:var(--muted);text-decoration:none;font-size:14px;}
nav a:hover{color:var(--text);}
.container{max-width:1100px;margin:0 auto;padding:32px 20px;}
.card{background:var(--bg2);border-radius:var(--radius);padding:22px;margin-bottom:20px;border:1px solid #202225;}
h1{font-size:26px;margin:0 0 6px;}
h2{font-size:19px;margin:0 0 14px;}
.muted{color:var(--muted);font-size:14px;}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px;}
.guild-card{background:var(--bg3);border-radius:var(--radius);padding:18px;text-align:center;border:1px solid #202225;
transition:.15s;text-decoration:none;color:var(--text);}
.guild-card:hover{border-color:var(--accent);transform:translateY(-2px);}
.guild-card img{width:64px;height:64px;border-radius:50%;margin-bottom:10px;}
.guild-icon-fallback{width:64px;height:64px;border-radius:50%;background:linear-gradient(135deg,var(--accent),var(--accent2));
display:flex;align-items:center;justify-content:center;font-weight:700;font-size:24px;margin:0 auto 10px;}
.btn{display:inline-block;background:var(--accent);color:#fff;border:none;padding:10px 18px;border-radius:8px;
font-size:14px;font-weight:600;cursor:pointer;text-decoration:none;}
.btn:hover{opacity:.9;}
.btn.secondary{background:var(--bg3);border:1px solid #444;color:var(--text);}
.btn.danger{background:var(--danger);}
.btn.small{padding:6px 12px;font-size:13px;}
input[type=text],input[type=color],textarea,select{width:100%;padding:10px 12px;border-radius:8px;
border:1px solid #444;background:var(--bg3);color:var(--text);font-size:14px;margin-top:6px;font-family:inherit;}
textarea{resize:vertical;min-height:80px;}
label{font-size:13px;font-weight:600;color:var(--muted);display:block;margin-top:14px;}
.row{display:flex;gap:24px;flex-wrap:wrap;}
.col{flex:1;min-width:280px;}
.preview{background:#2f3136;border-left:4px solid #99aab5;border-radius:6px;padding:14px 16px;margin-top:6px;}
.preview-title{font-weight:700;margin-bottom:6px;}
.preview-desc{white-space:pre-wrap;font-size:14px;color:#dcddde;}
.preview-footer{color:var(--muted);font-size:11px;margin-top:10px;}
table{width:100%;border-collapse:collapse;margin-top:10px;}
th,td{text-align:left;padding:10px;border-bottom:1px solid #3a3b3e;font-size:14px;}
th{color:var(--muted);font-size:12px;text-transform:uppercase;}
.badge{background:var(--bg3);padding:3px 9px;border-radius:12px;font-size:12px;border:1px solid #444;}
.banner{padding:12px 16px;border-radius:8px;margin-bottom:20px;font-size:14px;}
.banner.ok{background:rgba(59,165,93,.15);border:1px solid var(--success);color:#7ee2a0;}
.banner.err{background:rgba(237,66,69,.15);border:1px solid var(--danger);color:#f39a9c;}
.emoji-row{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;}
.emoji-btn{background:var(--bg3);border:1px solid #444;border-radius:6px;padding:6px 9px;cursor:pointer;font-size:16px;}
.emoji-btn:hover{border-color:var(--accent);}
.health-ok{color:var(--success);}
.health-bad{color:var(--danger);}
.breadcrumb{color:var(--muted);font-size:13px;margin-bottom:14px;}
.breadcrumb a{color:var(--muted);}
form.inline{display:inline;}
.hero{text-align:center;padding:80px 20px;}
.hero h1{font-size:38px;}
.feature-list{max-width:500px;margin:24px auto;text-align:left;color:var(--muted);line-height:1.8;}
"""

LAYOUT_HEAD = """
<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }}</title>
<style>__CSS__</style>
</head><body>
<nav>
  <div class="brand">⚙️ METALLIC <span>ROLES</span></div>
  <div class="user">
    {% if user %}
      {% if guild %}<a href="/dashboard">Servers</a>{% endif %}
      <span>{{ user.username }}</span>
      <a href="/logout">Logout</a>
    {% endif %}
  </div>
</nav>
<div class="container">
__CONTENT__
</div>
</body></html>
""".replace("__CSS__", CSS)

def render_page(content, title="Metallic Roles", **ctx):
    full = LAYOUT_HEAD.replace("__CONTENT__", content)
    return render_template_string(full, title=title, user=session.get("user"), **ctx)

def banner_html():
    return """
    {% if request.args.get('msg') %}
    <div class="banner {{ 'ok' if request.args.get('ok')=='1' else 'err' }}">{{ request.args.get('msg') }}</div>
    {% endif %}
    """

# ============================================================
# ROUTES — AUTH
# ============================================================
@app.route("/")
def index():
    if "user" in session:
        return redirect("/dashboard")
    content = """
    <div class="hero">
      <h1>⚙️ METALLIC ROLES</h1>
      <p class="muted">The easiest way to manage self-assignable roles — no commands, just clicks.</p>
      <div class="feature-list">
        ✅ Create dropdown role menus visually<br>
        ✅ Pick colors, emojis, labels from the browser<br>
        ✅ Live-updates in Discord instantly<br>
        ✅ No slash commands to memorize
      </div>
      <a class="btn" href="/login">Login with Discord</a>
    </div>
    """
    return render_page(content, title="Metallic Roles — Login")

@app.route("/login")
def login():
    if not CLIENT_ID or not REDIRECT_URI:
        return "❌ Server misconfigured: missing DISCORD_CLIENT_ID or DISCORD_REDIRECT_URI env vars.", 500
    params = urlencode({
        "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI,
        "response_type": "code", "scope": "identify guilds",
    })
    return redirect(f"https://discord.com/oauth2/authorize?{params}")

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return redirect("/")
    try:
        token_r = requests.post("https://discord.com/api/oauth2/token", data={
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
        }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=10)
        token_r.raise_for_status()
        access_token = token_r.json()["access_token"]

        user_r = requests.get("https://discord.com/api/users/@me",
                               headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
        user = user_r.json()

        guilds_r = requests.get("https://discord.com/api/users/@me/guilds",
                                 headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
        user_guilds = guilds_r.json()
    except Exception as e:
        return f"❌ Discord login failed: {e}", 400

    try:
        bot_guild_ids = run_async(get_bot_guild_ids())
    except RuntimeError as e:
        return f"⏳ {e}", 503

    permitted = []
    for g in user_guilds:
        try:
            perms = int(g.get("permissions", 0))
        except Exception:
            perms = 0
        has_access = bool(perms & 0x10000000) or bool(perms & 0x8)
        if g["id"] in bot_guild_ids and has_access:
            permitted.append({"id": g["id"], "name": g["name"], "icon": g.get("icon")})

    session["user"] = {"id": user["id"], "username": user.get("username", "User"),
                        "avatar": user.get("avatar")}
    session["guilds"] = permitted
    return redirect("/dashboard")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

# ============================================================
# ROUTES — SERVER LIST
# ============================================================
@app.route("/dashboard")
@login_required
def dashboard():
    guilds = session.get("guilds", [])
    invite_url = ""
    if CLIENT_ID:
        invite_url = f"https://discord.com/oauth2/authorize?client_id={CLIENT_ID}&permissions={BOT_INVITE_PERMS}&scope=bot%20applications.commands"

    content = banner_html() + """
    <h1>Your Servers</h1>
    <p class="muted">Servers where you have permission and the bot is present.</p>
    {% if invite_url %}<p><a class="btn secondary" href="{{ invite_url }}" target="_blank">+ Add bot to another server</a></p>{% endif %}
    <div class="grid">
    {% for g in guilds %}
      <a class="guild-card" href="/dashboard/{{ g.id }}">
        {% if g.icon %}
          <img src="https://cdn.discordapp.com/icons/{{ g.id }}/{{ g.icon }}.png">
        {% else %}
          <div class="guild-icon-fallback">{{ g.name[0]|upper }}</div>
        {% endif %}
        <div>{{ g.name }}</div>
        <div class="muted" style="font-size:12px;margin-top:4px;">Manage →</div>
      </a>
    {% else %}
      <p class="muted">No servers found. Make sure the bot is invited and you have Manage Roles permission.</p>
    {% endfor %}
    </div>
    """
    return render_page(content, title="Metallic Roles — Servers", guilds=guilds, invite_url=invite_url)

# ============================================================
# ROUTES — GUILD DASHBOARD
# ============================================================
@app.route("/dashboard/<guild_id>")
@login_required
@guild_access
def guild_home(guild_id):
    try:
        data = run_async(get_dashboard_guild_data(guild_id))
    except RuntimeError as e:
        return f"⏳ {e}", 503
    if not data:
        return redirect(flash_url("/dashboard", False, "Bot is not in that server anymore."))

    panels = db_fetchall("SELECT * FROM role_panels WHERE guild_id=?", (guild_id,))
    panels_full = []
    for p in panels:
        count = db_fetchone("SELECT COUNT(*) as c FROM role_options WHERE panel_id=?", (p["panel_id"],))
        panels_full.append({**p, "option_count": count["c"]})

    guild = {"id": guild_id, "name": data["guild_name"], "icon": data["guild_icon"]}

    content = banner_html() + """
    <div class="breadcrumb"><a href="/dashboard">Servers</a> / {{ guild.name }}</div>
    <div class="card">
      <h2>Bot Health Check</h2>
      {% if health.manage_roles %}
        <p class="health-ok">✅ Manage Roles permission: OK</p>
      {% else %}
        <p class="health-bad">❌ Missing Manage Roles permission — grant it in Server Settings → Roles.</p>
      {% endif %}
      <p class="muted">Bot's top role: <strong>{{ health.top_role_name }}</strong> — make sure it's above every self-assignable role.</p>
    </div>

    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <h2 style="margin:0;">Role Panels</h2>
        <a class="btn" href="/dashboard/{{ guild.id }}/create">+ New Panel</a>
      </div>
      {% if panels %}
      <table>
        <tr><th>Name</th><th>Channel</th><th>Options</th><th></th></tr>
        {% for p in panels %}
        <tr>
          <td>{{ p.name }}</td>
          <td class="muted">#{{ p.channel_id }}</td>
          <td><span class="badge">{{ p.option_count }} roles</span></td>
          <td>
            <a class="btn small secondary" href="/dashboard/{{ guild.id }}/panel/{{ p.panel_id }}">Manage</a>
          </td>
        </tr>
        {% endfor %}
      </table>
      {% else %}
      <p class="muted" style="margin-top:14px;">No panels yet. Click "+ New Panel" to create your first role menu.</p>
      {% endif %}
    </div>
    """
    return render_page(content, title=f"{data['guild_name']} — Metallic Roles",
                        guild=guild, health=data["health"], panels=panels_full)

# ---------------- CREATE PANEL ----------------
@app.route("/dashboard/<guild_id>/create", methods=["GET", "POST"])
@login_required
@guild_access
def create_panel(guild_id):
    try:
        data = run_async(get_dashboard_guild_data(guild_id))
    except RuntimeError as e:
        return f"⏳ {e}", 503
    if not data:
        return redirect(flash_url("/dashboard", False, "Bot is not in that server anymore."))

    guild = {"id": guild_id, "name": data["guild_name"]}

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        color = request.form.get("color", "99AAB5").strip().lstrip("#")
        channel_id = request.form.get("channel_id", "").strip()

        if not name or not channel_id:
            return redirect(flash_url(f"/dashboard/{guild_id}/create", False, "Title and channel are required."))

        panel_data = {"name": name, "description": description, "color": color}
        try:
            message_id = run_async(create_panel_discord_message(guild_id, channel_id, panel_data))
        except Exception as e:
            return redirect(flash_url(f"/dashboard/{guild_id}/create", False, f"Failed to post panel: {e}"))

        import uuid
        panel_id = uuid.uuid4().hex[:8]
        db_run("""INSERT INTO role_panels (panel_id, guild_id, channel_id, message_id, name, description, color)
                  VALUES (?,?,?,?,?,?,?)""",
               (panel_id, guild_id, channel_id, message_id, name, description, color))

        return redirect(flash_url(f"/dashboard/{guild_id}/panel/{panel_id}", True, "Panel created! Now add some roles below."))

    content = banner_html() + """
    <div class="breadcrumb"><a href="/dashboard">Servers</a> / <a href="/dashboard/{{ guild.id }}">{{ guild.name }}</a> / New Panel</div>
    <div class="card">
      <h2>Create Role Panel</h2>
      <form method="POST">
        <div class="row">
          <div class="col">
            <label>Panel Title</label>
            <input type="text" name="name" id="fName" placeholder="NOTIFICATION ROLE PICKER" required>

            <label>Description</label>
            <textarea name="description" id="fDesc" placeholder="Choose which content drops you want to be notified for!"></textarea>

            <label>Channel</label>
            <select name="channel_id" required>
              {% for c in channels %}<option value="{{ c.id }}">#{{ c.name }}</option>{% endfor %}
            </select>

            <label>Embed Color</label>
            <div style="display:flex;gap:10px;align-items:center;">
              <input type="color" id="colorPicker" value="#99aabb" style="width:50px;padding:2px;">
              <input type="text" name="color" id="fColor" value="99AABB" style="flex:1;">
            </div>

            <div style="margin-top:20px;">
              <button class="btn" type="submit">Create Panel</button>
              <a class="btn secondary" href="/dashboard/{{ guild.id }}">Cancel</a>
            </div>
          </div>
          <div class="col">
            <label>Live Preview</label>
            <div class="preview" id="previewBox" style="border-left-color:#99aabb;">
              <div class="preview-title" id="pTitle">⚙️ METALLIC ROLES — NOTIFICATION ROLE PICKER</div>
              <div class="preview-desc" id="pDesc">Choose which content drops you want to be notified for!</div>
              <div class="preview-footer">Metallic Roles • Select below to toggle</div>
            </div>
          </div>
        </div>
      </form>
    </div>
    <script>
    const fName=document.getElementById('fName'), fDesc=document.getElementById('fDesc'),
          fColor=document.getElementById('fColor'), colorPicker=document.getElementById('colorPicker'),
          pTitle=document.getElementById('pTitle'), pDesc=document.getElementById('pDesc'),
          previewBox=document.getElementById('previewBox');
    function updatePreview(){
      pTitle.textContent = "⚙️ METALLIC ROLES — " + (fName.value || "PANEL TITLE").toUpperCase();
      pDesc.textContent = fDesc.value || "Description goes here...";
      let c = fColor.value.replace('#','');
      if(/^[0-9a-fA-F]{6}$/.test(c)){ previewBox.style.borderLeftColor = '#'+c; }
    }
    fName.addEventListener('input', updatePreview);
    fDesc.addEventListener('input', updatePreview);
    fColor.addEventListener('input', updatePreview);
    colorPicker.addEventListener('input', ()=>{ fColor.value = colorPicker.value.replace('#',''); updatePreview(); });
    </script>
    """
    return render_page(content, title="Create Panel — Metallic Roles", guild=guild, channels=data["channels"])

# ---------------- MANAGE PANEL ----------------
@app.route("/dashboard/<guild_id>/panel/<panel_id>")
@login_required
@guild_access
def manage_panel(guild_id, panel_id):
    try:
        data = run_async(get_dashboard_guild_data(guild_id))
    except RuntimeError as e:
        return f"⏳ {e}", 503
    if not data:
        return redirect(flash_url("/dashboard", False, "Bot is not in that server anymore."))

    panel = db_fetchone("SELECT * FROM role_panels WHERE panel_id=? AND guild_id=?", (panel_id, guild_id))
    if not panel:
        return redirect(flash_url(f"/dashboard/{guild_id}", False, "Panel not found."))

    options = db_fetchall("SELECT * FROM role_options WHERE panel_id=? ORDER BY id ASC", (panel_id,))
    used_role_ids = set(o["role_id"] for o in options)
    addable_roles = [r for r in data["roles"] if r["id"] not in used_role_ids]

    guild = {"id": guild_id, "name": data["guild_name"]}

    content = banner_html() + """
    <div class="breadcrumb"><a href="/dashboard">Servers</a> / <a href="/dashboard/{{ guild.id }}">{{ guild.name }}</a> / {{ panel.name }}</div>

    <div class="card">
      <h2>Panel Details</h2>
      <form method="POST" action="/dashboard/{{ guild.id }}/panel/{{ panel.panel_id }}/update">
        <div class="row">
          <div class="col">
            <label>Panel Title</label>
            <input type="text" name="name" id="fName" value="{{ panel.name }}" required>
            <label>Description</label>
            <textarea name="description" id="fDesc">{{ panel.description }}</textarea>
            <label>Embed Color</label>
            <div style="display:flex;gap:10px;align-items:center;">
              <input type="color" id="colorPicker" value="#{{ panel.color }}" style="width:50px;padding:2px;">
              <input type="text" name="color" id="fColor" value="{{ panel.color }}" style="flex:1;">
            </div>
            <div style="margin-top:16px;"><button class="btn" type="submit">Save Changes</button></div>
          </div>
          <div class="col">
            <label>Live Preview</label>
            <div class="preview" id="previewBox" style="border-left-color:#{{ panel.color }};">
              <div class="preview-title" id="pTitle">⚙️ METALLIC ROLES — {{ panel.name|upper }}</div>
              <div class="preview-desc" id="pDesc">{{ panel.description }}</div>
              <div class="preview-footer">Metallic Roles • Select below to toggle</div>
            </div>
          </div>
        </div>
      </form>
    </div>

    <div class="card">
      <h2>Current Role Options ({{ options|length }}/25)</h2>
      {% if options %}
      <table>
        <tr><th>Emoji</th><th>Label</th><th>Description</th><th>Role</th><th></th></tr>
        {% for o in options %}
        <tr>
          <td>{{ o.emoji or '🔘' }}</td>
          <td>{{ o.label }}</td>
          <td class="muted">{{ o.description }}</td>
          <td><span class="badge">{{ o.role_name }}</span></td>
          <td>
            <form class="inline" method="POST" action="/dashboard/{{ guild.id }}/panel/{{ panel.panel_id }}/option/{{ o.id }}/delete"
                  onsubmit="return confirm('Remove this role option?');">
              <button class="btn small danger" type="submit">Remove</button>
            </form>
          </td>
        </tr>
        {% endfor %}
      </table>
      {% else %}
      <p class="muted">No role options yet. Add one below.</p>
      {% endif %}
    </div>

    <div class="card">
      <h2>+ Add Role Option</h2>
      {% if addable_roles %}
      <form method="POST" action="/dashboard/{{ guild.id }}/panel/{{ panel.panel_id }}/addoption">
        <label>Role</label>
        <select name="role_id" required>
          {% for r in addable_roles %}
            <option value="{{ r.id }}" {% if r.above_bot %}disabled{% endif %}>
              {{ r.name }}{% if r.above_bot %} (⚠️ above bot — cannot use){% endif %}
            </option>
          {% endfor %}
        </select>
        <label>Label (shown in dropdown)</label>
        <input type="text" name="label" placeholder="Giveaways" required>
        <label>Description</label>
        <input type="text" name="description" placeholder="Get notified when new giveaways go live!" required>
        <label>Emoji (optional)</label>
        <input type="text" name="emoji" id="emojiInput" placeholder="🎉">
        <div class="emoji-row">
          {% for e in emojis %}
          <button type="button" class="emoji-btn" onclick="document.getElementById('emojiInput').value='{{ e }}'">{{ e }}</button>
          {% endfor %}
        </div>
        <div style="margin-top:16px;"><button class="btn" type="submit">Add Option</button></div>
      </form>
      {% else %}
      <p class="muted">All available roles have been added, or no roles exist yet.</p>
      {% endif %}
    </div>

    <div class="card">
      <h2 style="color:var(--danger);">Danger Zone</h2>
      <form method="POST" action="/dashboard/{{ guild.id }}/panel/{{ panel.panel_id }}/delete"
            onsubmit="return confirm('Delete this entire panel? This cannot be undone.');">
        <button class="btn danger" type="submit">Delete Panel</button>
      </form>
    </div>

    <script>
    const fName=document.getElementById('fName'), fDesc=document.getElementById('fDesc'),
          fColor=document.getElementById('fColor'), colorPicker=document.getElementById('colorPicker'),
          pTitle=document.getElementById('pTitle'), pDesc=document.getElementById('pDesc'),
          previewBox=document.getElementById('previewBox');
    function updatePreview(){
      pTitle.textContent = "⚙️ METALLIC ROLES — " + (fName.value || "PANEL TITLE").toUpperCase();
      pDesc.textContent = fDesc.value || "";
      let c = fColor.value.replace('#','');
      if(/^[0-9a-fA-F]{6}$/.test(c)){ previewBox.style.borderLeftColor = '#'+c; }
    }
    fName.addEventListener('input', updatePreview);
    fDesc.addEventListener('input', updatePreview);
    fColor.addEventListener('input', updatePreview);
    colorPicker.addEventListener('input', ()=>{ fColor.value = colorPicker.value.replace('#',''); updatePreview(); });
    </script>
    """
    return render_page(content, title=f"{panel['name']} — Metallic Roles",
                        guild=guild, panel=panel, options=options,
                        addable_roles=addable_roles, emojis=EMOJI_CHOICES)

@app.route("/dashboard/<guild_id>/panel/<panel_id>/update", methods=["POST"])
@login_required
@guild_access
def update_panel(guild_id, panel_id):
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    color = request.form.get("color", "99AAB5").strip().lstrip("#")
    if not name:
        return redirect(flash_url(f"/dashboard/{guild_id}/panel/{panel_id}", False, "Title is required."))

    db_run("UPDATE role_panels SET name=?, description=?, color=? WHERE panel_id=? AND guild_id=?",
           (name, description, color, panel_id, guild_id))
    try:
        ok, msg = run_async(update_panel_message(panel_id))
    except RuntimeError as e:
        return redirect(flash_url(f"/dashboard/{guild_id}/panel/{panel_id}", False, str(e)))
    return redirect(flash_url(f"/dashboard/{guild_id}/panel/{panel_id}", ok,
                               "Panel updated!" if ok else f"Saved, but live message failed: {msg}"))

@app.route("/dashboard/<guild_id>/panel/<panel_id>/addoption", methods=["POST"])
@login_required
@guild_access
def add_option(guild_id, panel_id):
    try:
        data = run_async(get_dashboard_guild_data(guild_id))
    except RuntimeError as e:
        return redirect(flash_url(f"/dashboard/{guild_id}/panel/{panel_id}", False, str(e)))

    role_id = request.form.get("role_id", "").strip()
    label = request.form.get("label", "").strip()
    description = request.form.get("description", "").strip()
    emoji = request.form.get("emoji", "").strip() or None

    role_info = next((r for r in data["roles"] if r["id"] == role_id), None)
    if not role_info:
        return redirect(flash_url(f"/dashboard/{guild_id}/panel/{panel_id}", False, "Invalid role selected."))
    if role_info["above_bot"]:
        return redirect(flash_url(f"/dashboard/{guild_id}/panel/{panel_id}", False,
                                   "That role is above the bot's role and cannot be managed."))

    existing = db_fetchone("SELECT 1 FROM role_options WHERE panel_id=? AND role_id=?", (panel_id, role_id))
    if existing:
        return redirect(flash_url(f"/dashboard/{guild_id}/panel/{panel_id}", False, "That role is already added."))

    count = db_fetchone("SELECT COUNT(*) as c FROM role_options WHERE panel_id=?", (panel_id,))
    if count["c"] >= 25:
        return redirect(flash_url(f"/dashboard/{guild_id}/panel/{panel_id}", False, "Maximum 25 options reached."))

    validated = parse_emoji(emoji) if emoji else None
    emoji_to_store = str(validated) if validated else None

    db_run("INSERT INTO role_options (panel_id, role_id, role_name, label, description, emoji) VALUES (?,?,?,?,?,?)",
           (panel_id, role_id, role_info["name"], label, description, emoji_to_store))

    try:
        ok, msg = run_async(update_panel_message(panel_id))
    except RuntimeError as e:
        return redirect(flash_url(f"/dashboard/{guild_id}/panel/{panel_id}", False, str(e)))
    return redirect(flash_url(f"/dashboard/{guild_id}/panel/{panel_id}", ok,
                               "Role added!" if ok else f"Saved, but live message failed: {msg}"))

@app.route("/dashboard/<guild_id>/panel/<panel_id>/option/<option_id>/delete", methods=["POST"])
@login_required
@guild_access
def delete_option(guild_id, panel_id, option_id):
    db_run("DELETE FROM role_options WHERE id=? AND panel_id=?", (option_id, panel_id))
    try:
        ok, msg = run_async(update_panel_message(panel_id))
    except RuntimeError as e:
        return redirect(flash_url(f"/dashboard/{guild_id}/panel/{panel_id}", False, str(e)))
    return redirect(flash_url(f"/dashboard/{guild_id}/panel/{panel_id}", ok,
                               "Role removed!" if ok else f"Removed, but live message failed: {msg}"))

@app.route("/dashboard/<guild_id>/panel/<panel_id>/delete", methods=["POST"])
@login_required
@guild_access
def delete_panel(guild_id, panel_id):
    panel = db_fetchone("SELECT * FROM role_panels WHERE panel_id=? AND guild_id=?", (panel_id, guild_id))
    if panel:
        try:
            run_async(delete_panel_message(guild_id, panel["channel_id"], panel["message_id"]))
        except Exception:
            pass
        db_run("DELETE FROM role_panels WHERE panel_id=?", (panel_id,))
        db_run("DELETE FROM role_options WHERE panel_id=?", (panel_id,))
    return redirect(flash_url(f"/dashboard/{guild_id}", True, "Panel deleted."))

# ============================================================
# STARTUP
# ============================================================
if __name__ == "__main__":
    init_db()
    threading.Thread(target=start_bot_thread, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
