import os
import uuid
import asyncio
import threading
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from flask import Flask

# ============================================================
# CONFIG
# ============================================================
TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("GUILD_ID")  # optional - instant sync in one server
DB_PATH = "bot.db"
DEFAULT_COLOR = 0x57F287  # discord green

intents = discord.Intents.default()
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ============================================================
# KEEP ALIVE (Render Web Service)
# ============================================================
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()

# ============================================================
# DATABASE
# ============================================================
async def init_db():
    bot.db = await aiosqlite.connect(DB_PATH)
    bot.db.row_factory = aiosqlite.Row
    await bot.db.executescript("""
    CREATE TABLE IF NOT EXISTS role_panels (
        panel_id TEXT PRIMARY KEY,
        guild_id INTEGER,
        channel_id INTEGER,
        message_id INTEGER,
        name TEXT,
        description TEXT,
        color TEXT,
        footer TEXT
    );
    CREATE TABLE IF NOT EXISTS role_options (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        panel_id TEXT,
        role_id INTEGER,
        label TEXT,
        description TEXT,
        emoji TEXT
    );
    """)
    await bot.db.commit()

async def db_fetchall(q, p=()):
    async with bot.db.execute(q, p) as cur:
        return await cur.fetchall()

async def db_fetchone(q, p=()):
    async with bot.db.execute(q, p) as cur:
        return await cur.fetchone()

async def db_run(q, p=()):
    await bot.db.execute(q, p)
    await bot.db.commit()

# ============================================================
# HELPERS
# ============================================================
def parse_color(hex_str):
    try:
        return discord.Color(int(str(hex_str).replace("#", "").strip(), 16))
    except Exception:
        return discord.Color(DEFAULT_COLOR)

def parse_emoji(emoji_str):
    if not emoji_str:
        return None
    try:
        return discord.PartialEmoji.from_str(emoji_str)
    except Exception:
        return emoji_str

async def get_options(panel_id):
    rows = await db_fetchall(
        "SELECT * FROM role_options WHERE panel_id=? ORDER BY id ASC", (panel_id,)
    )
    return [dict(r) for r in rows]

def build_embed(panel, options, guild_name):
    color = parse_color(panel["color"])
    embed = discord.Embed(
        title=f"🔔 {panel['name']}",
        description=panel["description"] or "",
        color=color,
    )
    if options:
        lines = []
        for o in options:
            emoji = o["emoji"] or "🔘"
            lines.append(f"{emoji} **{o['label']}** — {o['description']}")
        embed.description += "\n\n" + "\n".join(lines)
        embed.description += "\n\n*Select or unselect options at any time to update your ping preferences!*"
    else:
        embed.description += "\n\n*No roles configured yet. Admin: use `/rolemenu addoption`.*"

    footer = panel["footer"] or f"{guild_name} Role Management • Select below to toggle"
    embed.set_footer(text=footer)
    return embed

async def update_panel_message(panel_id):
    panel = await db_fetchone("SELECT * FROM role_panels WHERE panel_id=?", (panel_id,))
    if not panel:
        return
    guild = bot.get_guild(panel["guild_id"])
    if not guild:
        return
    channel = guild.get_channel(panel["channel_id"])
    if not channel:
        return

    options = await get_options(panel_id)
    embed = build_embed(panel, options, guild.name)
    view = RoleDropdownView(panel_id, options) if options else None

    try:
        message = await channel.fetch_message(panel["message_id"])
        await message.edit(embed=embed, view=view)
    except discord.NotFound:
        return

    if view:
        bot.add_view(view, message_id=panel["message_id"])

# ============================================================
# UI — DROPDOWN
# ============================================================
class RoleDropdown(discord.ui.Select):
    def __init__(self, panel_id, options_data):
        opts = []
        for o in options_data:
            opts.append(
                discord.SelectOption(
                    label=o["label"][:100],
                    description=(o["description"] or "")[:100],
                    value=str(o["role_id"]),
                    emoji=parse_emoji(o["emoji"]),
                )
            )
        super().__init__(
            placeholder="Select your notification roles...",
            min_values=0,
            max_values=len(opts),
            options=opts,
            custom_id=f"rolemenu:{panel_id}",
        )
        self.panel_id = panel_id

    async def callback(self, interaction: discord.Interaction):
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

        added_names, removed_names = [], []
        try:
            if to_add:
                await member.add_roles(*to_add, reason="Role menu selection")
                added_names = [r.name for r in to_add]
            if to_remove:
                await member.remove_roles(*to_remove, reason="Role menu deselection")
                removed_names = [r.name for r in to_remove]
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have permission to manage one or more of these roles. "
                "Please ask an admin to move my role higher in Server Settings → Roles.",
                ephemeral=True,
            )
            return

        lines = []
        if added_names:
            lines.append("✅ **Added:** " + ", ".join(added_names))
        if removed_names:
            lines.append("❌ **Removed:** " + ", ".join(removed_names))
        if not lines:
            lines.append("ℹ️ No changes made.")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)


class RoleDropdownView(discord.ui.View):
    def __init__(self, panel_id, options_data):
        super().__init__(timeout=None)
        if options_data:
            self.add_item(RoleDropdown(panel_id, options_data))

# ============================================================
# MODALS
# ============================================================
class CreatePanelModal(discord.ui.Modal, title="Create Role Menu"):
    name = discord.ui.TextInput(
        label="Panel Title", placeholder="NOTIFICATION ROLE PICKER", max_length=100
    )
    description = discord.ui.TextInput(
        label="Description",
        style=discord.TextStyle.paragraph,
        placeholder="Choose which content drops you want to be notified for!",
        required=False,
        max_length=1000,
    )
    color = discord.ui.TextInput(
        label="Embed Color (hex)", placeholder="#57F287", required=False, max_length=7
    )

    def __init__(self, channel: discord.TextChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        panel_id = uuid.uuid4().hex[:8]
        color_val = str(self.color) if str(self.color).strip() else "#57F287"

        panel_data = {
            "name": str(self.name),
            "description": str(self.description),
            "color": color_val,
            "footer": None,
        }
        embed = build_embed(panel_data, [], interaction.guild.name)
        msg = await self.channel.send(embed=embed)

        await db_run(
            "INSERT INTO role_panels (panel_id, guild_id, channel_id, message_id, name, description, color, footer) VALUES (?,?,?,?,?,?,?,?)",
            (
                panel_id, interaction.guild.id, self.channel.id, msg.id,
                str(self.name), str(self.description), color_val, None,
            ),
        )

        await interaction.response.send_message(
            f"✅ Panel created in {self.channel.mention}!\n"
            f"**Panel ID:** `{panel_id}`\n\n"
            f"Now add roles with:\n`/rolemenu addoption panel_id:{panel_id} role:@Role label:\"Giveaways\" description:\"...\" emoji:🎉`",
            ephemeral=True,
        )


class EditPanelModal(discord.ui.Modal, title="Edit Role Menu"):
    name = discord.ui.TextInput(label="Panel Title", max_length=100)
    description = discord.ui.TextInput(
        label="Description", style=discord.TextStyle.paragraph, required=False, max_length=1000
    )
    color = discord.ui.TextInput(label="Embed Color (hex)", required=False, max_length=7)

    def __init__(self, panel):
        super().__init__()
        self.panel_id = panel["panel_id"]
        self.name.default = panel["name"]
        self.description.default = panel["description"]
        self.color.default = panel["color"]

    async def on_submit(self, interaction: discord.Interaction):
        color_val = str(self.color) if str(self.color).strip() else "#57F287"
        await db_run(
            "UPDATE role_panels SET name=?, description=?, color=? WHERE panel_id=?",
            (str(self.name), str(self.description), color_val, self.panel_id),
        )
        await update_panel_message(self.panel_id)
        await interaction.response.send_message("✅ Panel updated.", ephemeral=True)

# ============================================================
# AUTOCOMPLETE
# ============================================================
async def panel_autocomplete(interaction: discord.Interaction, current: str):
    rows = await db_fetchall(
        "SELECT panel_id, name FROM role_panels WHERE guild_id=?", (interaction.guild.id,)
    )
    results = []
    for r in rows:
        label = f"{r['name']} ({r['panel_id']})"
        if current.lower() in label.lower():
            results.append(app_commands.Choice(name=label[:100], value=r["panel_id"]))
    return results[:25]

# ============================================================
# COMMANDS
# ============================================================
rolemenu_group = app_commands.Group(name="rolemenu", description="Create and manage dropdown role menus")

@rolemenu_group.command(name="create", description="Create a new dropdown role menu")
@app_commands.describe(channel="Channel to post the role menu in")
@app_commands.checks.has_permissions(manage_roles=True)
async def rolemenu_create(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.send_modal(CreatePanelModal(channel))


@rolemenu_group.command(name="addoption", description="Add a role option to a dropdown menu")
@app_commands.describe(
    panel_id="The panel to add this option to",
    role="Role to assign",
    label="Name shown in dropdown (e.g. Giveaways)",
    description="Short description shown under the label",
    emoji="Emoji shown next to the option",
)
@app_commands.autocomplete(panel_id=panel_autocomplete)
@app_commands.checks.has_permissions(manage_roles=True)
async def rolemenu_addoption(
    interaction: discord.Interaction,
    panel_id: str,
    role: discord.Role,
    label: str,
    description: str,
    emoji: str = None,
):
    panel = await db_fetchone("SELECT * FROM role_panels WHERE panel_id=?", (panel_id,))
    if not panel:
        await interaction.response.send_message("❌ Panel not found.", ephemeral=True)
        return

    if role >= interaction.guild.me.top_role:
        await interaction.response.send_message(
            "❌ I can't manage this role because it's higher than or equal to my top role. "
            "Move my role above it in Server Settings → Roles.",
            ephemeral=True,
        )
        return

    existing = await db_fetchone(
        "SELECT 1 FROM role_options WHERE panel_id=? AND role_id=?", (panel_id, role.id)
    )
    if existing:
        await interaction.response.send_message("❌ That role is already in this panel.", ephemeral=True)
        return

    count_row = await db_fetchone(
        "SELECT COUNT(*) as c FROM role_options WHERE panel_id=?", (panel_id,)
    )
    if count_row["c"] >= 25:
        await interaction.response.send_message(
            "❌ This panel already has the maximum of 25 options (Discord limit).", ephemeral=True
        )
        return

    await db_run(
        "INSERT INTO role_options (panel_id, role_id, label, description, emoji) VALUES (?,?,?,?,?)",
        (panel_id, role.id, label, description, emoji),
    )
    await update_panel_message(panel_id)
    await interaction.response.send_message(
        f"✅ Added option **{label}** ({role.mention}) to panel `{panel_id}`.", ephemeral=True
    )


@rolemenu_group.command(name="removeoption", description="Remove a role option from a dropdown menu")
@app_commands.describe(panel_id="The panel to remove from", role="Role to remove")
@app_commands.autocomplete(panel_id=panel_autocomplete)
@app_commands.checks.has_permissions(manage_roles=True)
async def rolemenu_removeoption(interaction: discord.Interaction, panel_id: str, role: discord.Role):
    await db_run(
        "DELETE FROM role_options WHERE panel_id=? AND role_id=?", (panel_id, role.id)
    )
    await update_panel_message(panel_id)
    await interaction.response.send_message(f"✅ Removed {role.mention} from panel `{panel_id}`.", ephemeral=True)


@rolemenu_group.command(name="edit", description="Edit a role menu's title, description or color")
@app_commands.describe(panel_id="The panel to edit")
@app_commands.autocomplete(panel_id=panel_autocomplete)
@app_commands.checks.has_permissions(manage_roles=True)
async def rolemenu_edit(interaction: discord.Interaction, panel_id: str):
    panel = await db_fetchone("SELECT * FROM role_panels WHERE panel_id=?", (panel_id,))
    if not panel:
        await interaction.response.send_message("❌ Panel not found.", ephemeral=True)
        return
    await interaction.response.send_modal(EditPanelModal(dict(panel)))


@rolemenu_group.command(name="delete", description="Delete a role menu completely")
@app_commands.describe(panel_id="The panel to delete")
@app_commands.autocomplete(panel_id=panel_autocomplete)
@app_commands.checks.has_permissions(manage_roles=True)
async def rolemenu_delete(interaction: discord.Interaction, panel_id: str):
    panel = await db_fetchone("SELECT * FROM role_panels WHERE panel_id=?", (panel_id,))
    if not panel:
        await interaction.response.send_message("❌ Panel not found.", ephemeral=True)
        return

    guild = interaction.guild
    channel = guild.get_channel(panel["channel_id"])
    if channel:
        try:
            msg = await channel.fetch_message(panel["message_id"])
            await msg.delete()
        except discord.NotFound:
            pass

    await db_run("DELETE FROM role_panels WHERE panel_id=?", (panel_id,))
    await db_run("DELETE FROM role_options WHERE panel_id=?", (panel_id,))
    await interaction.response.send_message(f"✅ Deleted panel `{panel_id}`.", ephemeral=True)


@rolemenu_group.command(name="list", description="List all role menus in this server")
async def rolemenu_list(interaction: discord.Interaction):
    rows = await db_fetchall("SELECT * FROM role_panels WHERE guild_id=?", (interaction.guild.id,))
    if not rows:
        await interaction.response.send_message("No role menus created yet.", ephemeral=True)
        return

    lines = []
    for r in rows:
        count_row = await db_fetchone(
            "SELECT COUNT(*) as c FROM role_options WHERE panel_id=?", (r["panel_id"],)
        )
        lines.append(f"`{r['panel_id']}` — **{r['name']}** ({count_row['c']} options) in <#{r['channel_id']}>")

    embed = discord.Embed(title="📋 Role Menus", description="\n".join(lines), color=DEFAULT_COLOR)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="help", description="Show how to set up role menus")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(title="🔔 Role Menu Bot — Setup Guide", color=DEFAULT_COLOR)
    embed.add_field(
        name="1️⃣ Create a panel",
        value="`/rolemenu create channel:#roles`\n→ Fill in Title, Description, Color in the popup form.",
        inline=False,
    )
    embed.add_field(
        name="2️⃣ Add role options",
        value="`/rolemenu addoption panel_id:xxxx role:@Giveaways label:\"Giveaways\" description:\"Get notified for new giveaways\" emoji:🎉`\nRepeat for each role (up to 25).",
        inline=False,
    )
    embed.add_field(name="3️⃣ Edit anytime", value="`/rolemenu edit panel_id:xxxx`", inline=False)
    embed.add_field(name="4️⃣ Remove an option", value="`/rolemenu removeoption panel_id:xxxx role:@Role`", inline=False)
    embed.add_field(name="5️⃣ Delete a panel", value="`/rolemenu delete panel_id:xxxx`", inline=False)
    embed.add_field(name="📋 See all panels", value="`/rolemenu list`", inline=False)
    embed.set_footer(text="Users just click the dropdown and select — roles apply instantly!")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ============================================================
# GLOBAL ERROR HANDLER (bug-proofing)
# ============================================================
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "❌ You need the **Manage Roles** permission to use this command."
    elif isinstance(error, app_commands.CommandOnCooldown):
        msg = f"⏳ Slow down! Try again in {error.retry_after:.1f}s."
    else:
        print(f"[ERROR] {error}")
        msg = "❌ Something went wrong. Please try again or contact an admin."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass

# ============================================================
# EVENTS
# ============================================================
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

    panels = await db_fetchall("SELECT * FROM role_panels")
    for panel in panels:
        options = await get_options(panel["panel_id"])
        if options:
            view = RoleDropdownView(panel["panel_id"], options)
            bot.add_view(view, message_id=panel["message_id"])

    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
        else:
            await bot.tree.sync()
        print("Slash commands synced.")
    except Exception as e:
        print(f"Sync error: {e}")

# ============================================================
# STARTUP
# ============================================================
async def main():
    bot.tree.add_command(rolemenu_group)
    await init_db()
    keep_alive()
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())