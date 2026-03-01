import os
import re
import time
import asyncio
import aiosqlite
import discord
from discord.ext import commands, tasks

# =======================
# CONFIG (YOUR SERVER)
# =======================
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]

# Dyno join logs channel (detect joins from Dyno embeds)
DYNO_LOG_CHANNEL_ID = 986289035133743174
DYNO_BOT_ID = 155149108183695360

# Where to post public welcome (ONLY here)
FRESH_SPAWNS_CHANNEL_ID = 1454402793157824603

# Create-a-ticket channel (for clickable mention)
CREATE_TICKET_CHANNEL_ID = 986685464251609118

# Ticket Tool ticket channel names in your server look like ticket-1447 etc.
TICKET_NAME_RE = re.compile(r"^ticket-\d+$", re.IGNORECASE)

# Role to grant when ticket is opened
ENQUIRED_ROLE_ID = 1473603025770647615

# Fresh Spawn role (by name)
FRESH_SPAWN_ROLE_NAME = "Fresh Spawn"

# Timer settings
DUE_SECONDS = 24 * 3600
CHECK_EVERY_MINUTES = 2

DB_PATH = "data.db"

WELCOME_TEXT = (
    "Thank you for joining the server, please go to "
    f"<#{CREATE_TICKET_CHANNEL_ID}> to book an optimisation. "
    "If a ticket has not been created in the next 24 hours then you will be automatically kicked "
    "and you will need to rejoin once you are ready to purchase."
)

# =======================
# DISCORD SETUP
# =======================
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
# Needed for reading Dyno log messages + reading ticket channel history
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# =======================
# DB
# =======================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS join_timers (
                guild_id   INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                joined_at  INTEGER NOT NULL,
                processed  INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        await db.commit()

async def upsert_join_time(guild_id: int, user_id: int, joined_at: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO join_timers (guild_id, user_id, joined_at, processed)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                joined_at=excluded.joined_at,
                processed=0
        """, (guild_id, user_id, joined_at))
        await db.commit()

async def mark_processed(guild_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE join_timers SET processed=1
            WHERE guild_id=? AND user_id=?
        """, (guild_id, user_id))
        await db.commit()

async def fetch_due(now_ts: int):
    cutoff = now_ts - DUE_SECONDS
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT guild_id, user_id, joined_at
            FROM join_timers
            WHERE processed=0 AND joined_at <= ?
        """, (cutoff,))
        return await cur.fetchall()

# =======================
# HELPERS
# =======================
def has_only_role(member: discord.Member, role: discord.Role) -> bool:
    # Ignore @everyone
    real_roles = [r for r in member.roles if r != member.guild.default_role]
    return len(real_roles) == 1 and real_roles[0].id == role.id

async def post_public_welcome(guild: discord.Guild, member: discord.Member):
    ch = guild.get_channel(FRESH_SPAWNS_CHANNEL_ID)
    if not isinstance(ch, discord.TextChannel):
        print(f"[WARN] ❓-fresh-spawns channel {FRESH_SPAWNS_CHANNEL_ID} not found / not text.")
        return
    try:
        await ch.send(f"{member.mention}\n{WELCOME_TEXT}")
        print(f"[INFO] Posted welcome for {member} in ❓-fresh-spawns")
    except discord.Forbidden:
        print("[WARN] Missing permission to send message in ❓-fresh-spawns.")
    except discord.HTTPException as e:
        print(f"[WARN] Failed to post welcome: {e}")

async def give_enquired_role(member: discord.Member):
    role = member.guild.get_role(ENQUIRED_ROLE_ID)
    if role is None:
        roles = await member.guild.fetch_roles()
        role = discord.utils.get(roles, id=ENQUIRED_ROLE_ID)

    if role is None:
        print(f"[WARN] Enquired role {ENQUIRED_ROLE_ID} not found in guild {member.guild.id}")
        return

    if role not in member.roles:
        await member.add_roles(role, reason="Ticket opened (Ticket Tool detected).")
        print(f"[INFO] Added enquired role to {member} ({member.id})")

async def resolve_member_from_dyno_join_log(message: discord.Message) -> discord.Member | None:
    """
    Dyno join embed (your screenshot) includes the mention inside EMBED FIELDS, not description.
    We'll search description + author + all fields for <@id>.
    """
    # First try: discord parsed mentions
    if message.mentions:
        m = message.mentions[0]
        if isinstance(m, discord.Member):
            return m

    if not message.embeds:
        return None

    emb = message.embeds[0]

    parts: list[str] = []
    if emb.title:
        parts.append(emb.title)
    if emb.description:
        parts.append(emb.description)
    if emb.author and emb.author.name:
        parts.append(emb.author.name)

    for f in emb.fields:
        if f.name:
            parts.append(f.name)
        if f.value:
            parts.append(f.value)

    text = "\n".join(parts)

    match = re.search(r"<@!?(\d{17,20})>", text)
    if not match:
        return None

    user_id = int(match.group(1))
    try:
        return message.guild.get_member(user_id) or await message.guild.fetch_member(user_id)
    except (discord.NotFound, discord.Forbidden):
        return None

async def resolve_ticket_owner_from_channel(channel: discord.TextChannel) -> discord.Member | None:
    """
    Ticket Tool posts an opening message that mentions the ticket owner (per your screenshot).
    We'll read earliest messages and extract the first mentioned human member.
    """
    await asyncio.sleep(2)  # let Ticket Tool send its first message

    try:
        async for msg in channel.history(limit=15, oldest_first=True):
            if msg.author.bot and msg.mentions:
                for m in msg.mentions:
                    if isinstance(m, discord.Member) and not m.bot:
                        return m

        # Fallback: first non-bot message author
        async for msg in channel.history(limit=50, oldest_first=True):
            if isinstance(msg.author, discord.Member) and not msg.author.bot:
                return msg.author

    except discord.Forbidden:
        print(f"[WARN] No permission to read history in {channel.name}")
    except discord.HTTPException as e:
        print(f"[WARN] Error reading history in {channel.name}: {e}")

    return None

# =======================
# EVENTS
# =======================
@bot.event
async def on_ready():
    await init_db()
    timer_loop.start()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)

    # Only handle Dyno join logs in the logs channel
    if message.channel.id != DYNO_LOG_CHANNEL_ID:
        return

    # Debug: confirm we can see Dyno logs messages
    # (You can remove once working)
    if message.author.id == DYNO_BOT_ID and message.embeds:
        emb = message.embeds[0]
        print(f"[DEBUG] Dyno embed seen: title='{emb.title}' fields={len(emb.fields)}")

    # Must be Dyno
    if message.author.id != DYNO_BOT_ID:
        return

    if not message.embeds:
        return

    emb = message.embeds[0]
    title = (emb.title or "").strip().lower()

    # Looser match (handles minor variations)
    if "member joined" not in title:
        return

    member = await resolve_member_from_dyno_join_log(message)
    if member is None:
        print("[WARN] Dyno join log detected but couldn't resolve member (mention not found).")
        return

    # Start (or reset) timer
    await upsert_join_time(message.guild.id, member.id, int(time.time()))
    print(f"[INFO] Started 24h timer from Dyno logs for {member} ({member.id})")

    # Public welcome message (ONLY in ❓-fresh-spawns)
    await post_public_welcome(message.guild, member)

@bot.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel):
    # ---- Ticket Tool ticket detection ----
    if not isinstance(channel, discord.TextChannel):
        return

    if not TICKET_NAME_RE.match(channel.name):
        return

    owner = await resolve_ticket_owner_from_channel(channel)
    if owner is None:
        print(f"[WARN] Ticket channel {channel.name} created but owner not found.")
        return

    # Give role and end timer
    await give_enquired_role(owner)
    await mark_processed(channel.guild.id, owner.id)
    print(f"[INFO] Ended timer for {owner} ({owner.id}) because ticket was opened.")

# =======================
# TIMER LOOP
# =======================
@tasks.loop(minutes=CHECK_EVERY_MINUTES)
async def timer_loop():
    now_ts = int(time.time())
    due = await fetch_due(now_ts)
    if not due:
        return

    for guild_id, user_id, joined_at in due:
        guild = bot.get_guild(guild_id)
        if guild is None:
            await mark_processed(guild_id, user_id)
            continue

        try:
            member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        except discord.NotFound:
            await mark_processed(guild_id, user_id)
            continue
        except discord.Forbidden:
            print(f"[WARN] Forbidden fetching member {user_id} in guild {guild_id}")
            continue

        enquired_role = guild.get_role(ENQUIRED_ROLE_ID)
        fresh_spawn_role = discord.utils.get(guild.roles, name=FRESH_SPAWN_ROLE_NAME)

        if fresh_spawn_role is None:
            print(f"[WARN] Fresh Spawn role '{FRESH_SPAWN_ROLE_NAME}' not found in guild {guild.id}")
            await mark_processed(guild_id, user_id)
            continue

        has_enquired = (enquired_role in member.roles) if enquired_role else False

        if has_enquired:
            print(f"[INFO] KEEP {member} - has enquired role")
        else:
            if has_only_role(member, fresh_spawn_role):
                try:
                    await member.kick(reason="24h check: only Fresh Spawn and no ticket opened.")
                    print(f"[INFO] KICK {member} - only Fresh Spawn, no ticket opened")
                except discord.Forbidden:
                    print(f"[WARN] Can't kick {member} - permissions/role hierarchy issue")
                except discord.HTTPException as e:
                    print(f"[WARN] Kick failed for {member}: {e}")
            else:
                print(f"[INFO] KEEP {member} - has roles beyond only Fresh Spawn")

        await mark_processed(guild_id, user_id)

@timer_loop.before_loop
async def before_timer_loop():
    await bot.wait_until_ready()

bot.run(DISCORD_TOKEN)
