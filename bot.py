import os
import re
import time
import asyncio
import aiosqlite
import discord
from discord.ext import commands, tasks
from discord import app_commands

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]

FRESH_SPAWNS_CHANNEL_ID = 1454402793157824603
CREATE_TICKET_CHANNEL_ID = 986685464251609118
POST_SERVICE_CHANNEL_ID = int(os.environ["POST_SERVICE_CHANNEL_ID"])
POST_SERVICE_ROLE_ID = int(os.environ["POST_SERVICE_ROLE_ID"])
GOATS_ROLE_ID = int(os.environ["GOATS_ROLE_ID"])

# Broader match so it still catches common Ticket Tool naming styles
TICKET_NAME_RE = re.compile(r"ticket-\d+", re.IGNORECASE)

ENQUIRED_ROLE_ID = 1473603025770647615
FRESH_SPAWN_ROLE_NAME = "Fresh Spawn"

DUE_SECONDS = 24 * 3600
CHECK_EVERY_MINUTES = 2
DB_PATH = "data.db"

POST_SERVICE_REVIEW_SECONDS = 7 * 24 * 3600
POST_SERVICE_FINAL_WARNING_SECONDS = 12 * 3600

WELCOME_TEXT = (
    "Thank you for joining the server, please go to "
    f"<#{CREATE_TICKET_CHANNEL_ID}> to book an optimisation. "
    "If a ticket has not been created in the next 24 hours then you will be automatically kicked "
    "and you will need to rejoin once you are ready to purchase. Please DO NOT FORGET to read the ❗PLEASE READ❗ Category especially if booking an Optimisation"
)

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ---------- DB ----------
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ticket_owners (
                guild_id   INTEGER NOT NULL,
                channel_id INTEGER NOT NULL PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS completed_tickets (
                channel_id   INTEGER NOT NULL PRIMARY KEY,
                guild_id     INTEGER NOT NULL,
                user_id      INTEGER NOT NULL,
                completed_at INTEGER NOT NULL,
                reminded     INTEGER NOT NULL DEFAULT 0,
                finalised    INTEGER NOT NULL DEFAULT 0
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
            UPDATE join_timers
            SET processed=1
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


async def save_ticket_owner(guild_id: int, channel_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO ticket_owners (guild_id, channel_id, user_id, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                guild_id=excluded.guild_id,
                user_id=excluded.user_id,
                created_at=excluded.created_at
        """, (guild_id, channel_id, user_id, int(time.time())))
        await db.commit()


async def fetch_ticket_owner(channel_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT guild_id, user_id
            FROM ticket_owners
            WHERE channel_id=?
        """, (channel_id,))
        return await cur.fetchone()


async def delete_ticket_owner(channel_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            DELETE FROM ticket_owners
            WHERE channel_id=?
        """, (channel_id,))
        await db.commit()


async def save_completed_ticket(guild_id: int, channel_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO completed_tickets (channel_id, guild_id, user_id, completed_at, reminded, finalised)
            VALUES (?, ?, ?, ?, 0, 0)
            ON CONFLICT(channel_id) DO UPDATE SET
                guild_id=excluded.guild_id,
                user_id=excluded.user_id,
                completed_at=excluded.completed_at,
                reminded=0,
                finalised=0
        """, (channel_id, guild_id, user_id, int(time.time())))
        await db.commit()


async def fetch_completed_tickets():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT channel_id, guild_id, user_id, completed_at, reminded, finalised
            FROM completed_tickets
        """)
        return await cur.fetchall()


async def mark_completed_ticket_reminded(channel_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE completed_tickets
            SET reminded=1
            WHERE channel_id=?
        """, (channel_id,))
        await db.commit()


async def mark_completed_ticket_finalised(channel_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE completed_tickets
            SET finalised=1
            WHERE channel_id=?
        """, (channel_id,))
        await db.commit()


async def delete_completed_ticket(channel_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            DELETE FROM completed_tickets
            WHERE channel_id=?
        """, (channel_id,))
        await db.commit()


# ---------- Helpers ----------
def has_only_role(member: discord.Member, role: discord.Role) -> bool:
    real_roles = [r for r in member.roles if r != member.guild.default_role]
    return len(real_roles) == 1 and real_roles[0].id == role.id


def looks_like_ticket_channel(name: str) -> bool:
    return bool(TICKET_NAME_RE.search(name))


async def post_public_welcome(guild: discord.Guild, member: discord.Member):
    ch = guild.get_channel(FRESH_SPAWNS_CHANNEL_ID)
    if not isinstance(ch, discord.TextChannel):
        print(f"[WARN] ❓-fresh-spawns channel {FRESH_SPAWNS_CHANNEL_ID} not found / not text.")
        return
    try:
        await ch.send(f"{member.mention}\n{WELCOME_TEXT}")
        print(f"[INFO] Posted welcome for {member}")
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


async def remove_enquired_role(member: discord.Member):
    role = member.guild.get_role(ENQUIRED_ROLE_ID)
    if role is None:
        roles = await member.guild.fetch_roles()
        role = discord.utils.get(roles, id=ENQUIRED_ROLE_ID)

    if role is None:
        print(f"[WARN] Enquired role {ENQUIRED_ROLE_ID} not found in guild {member.guild.id}")
        return

    if role in member.roles:
        await member.remove_roles(role, reason="Ticket closed.")
        print(f"[INFO] Removed enquired role from {member} ({member.id})")
    else:
        print(f"[INFO] {member} ({member.id}) did not have enquired role at deletion time.")


async def resolve_ticket_owner_from_channel(channel: discord.TextChannel) -> discord.Member | None:
    # small delay so Ticket Tool has time to post its opening message
    await asyncio.sleep(2)

    try:
        # Prefer bot messages mentioning the ticket opener
        async for msg in channel.history(limit=20, oldest_first=True):
            if msg.author.bot and msg.mentions:
                for m in msg.mentions:
                    if isinstance(m, discord.Member) and not m.bot:
                        return m

        # Fallback: first non-bot message author in the ticket
        async for msg in channel.history(limit=50, oldest_first=True):
            if isinstance(msg.author, discord.Member) and not msg.author.bot:
                return msg.author

    except discord.Forbidden:
        print(f"[WARN] No permission to read history in {channel.name}")
    except discord.HTTPException as e:
        print(f"[WARN] Error reading history in {channel.name}: {e}")

    return None


# ---------- Events ----------
@bot.event
async def on_ready():
    await init_db()
    if not timer_loop.is_running():
        timer_loop.start()
    if not post_service_loop.is_running():
        post_service_loop.start()

    try:
        synced = await bot.tree.sync()
        print(f"[INFO] Synced {len(synced)} app command(s).")
    except Exception as e:
        print(f"[WARN] Failed to sync app commands: {e}")

    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_member_join(member: discord.Member):
    await upsert_join_time(member.guild.id, member.id, int(time.time()))
    print(f"[INFO] Started 24h timer for {member} ({member.id}) via on_member_join")
    await post_public_welcome(member.guild, member)


@bot.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel):
    if not isinstance(channel, discord.TextChannel):
        return

    if not looks_like_ticket_channel(channel.name):
        return

    print(f"[DEBUG] Ticket channel created: {channel.name} ({channel.id})")

    owner = await resolve_ticket_owner_from_channel(channel)
    if owner is None:
        print(f"[WARN] Ticket channel {channel.name} created but owner not found.")
        return

    print(f"[DEBUG] Resolved owner: {owner} ({owner.id})")

    await save_ticket_owner(channel.guild.id, channel.id, owner.id)
    print(f"[DEBUG] Saved ticket owner mapping channel={channel.id} user={owner.id}")

    await give_enquired_role(owner)
    await mark_processed(channel.guild.id, owner.id)
    print(f"[INFO] Ended timer for {owner} ({owner.id}) because ticket was opened.")


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    if not isinstance(channel, discord.TextChannel):
        return

    print(f"[DEBUG] Channel deleted: {channel.name} ({channel.id})")

    # Do NOT check the name here.
    # Ticket Tool may rename the channel before deleting it.
    row = await fetch_ticket_owner(channel.id)
    if row is None:
        print(f"[INFO] Deleted channel {channel.name} ({channel.id}) had no stored owner mapping.")
    else:
        guild_id, user_id = row
        guild = bot.get_guild(guild_id) or channel.guild
        if guild is None:
            print(f"[WARN] Guild {guild_id} not found when deleting ticket channel {channel.id}")
        else:
            try:
                member = guild.get_member(user_id) or await guild.fetch_member(user_id)
            except discord.NotFound:
                print(f"[INFO] Ticket owner {user_id} no longer in guild {guild_id}")
            except discord.Forbidden:
                print(f"[WARN] Forbidden fetching member {user_id} in guild {guild_id}")
            except discord.HTTPException as e:
                print(f"[WARN] Failed fetching member {user_id}: {e}")
            else:
                try:
                    await remove_enquired_role(member)
                except discord.Forbidden:
                    print(f"[WARN] Can't remove enquired role from {member} - permissions/role hierarchy issue")
                except discord.HTTPException as e:
                    print(f"[WARN] Failed to remove enquired role from {member}: {e}")

        await delete_ticket_owner(channel.id)
        print(f"[DEBUG] Deleted stored ticket owner mapping for channel {channel.id}")

    await delete_completed_ticket(channel.id)


# ---------- Slash Commands ----------
    row = (interaction.guild.id, owner.id)
@bot.tree.command(name="complete", description="Mark optimisation as complete and start the 7-day review timer")
@app_commands.checks.has_permissions(manage_channels=True)
async def complete(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message(
            "❌ This command must be used inside a ticket channel.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=False)

row = await fetch_ticket_owner(interaction.channel.id)

if row is None:
    owner = await resolve_ticket_owner_from_channel(interaction.channel)

    if owner is None:
        await interaction.followup.send(
            "❌ Could not determine the ticket owner for this channel.",
            ephemeral=True
        )
        return

    await save_ticket_owner(interaction.guild.id, interaction.channel.id, owner.id)
    row = (interaction.guild.id, owner.id)

        await save_ticket_owner(interaction.guild.id, interaction.channel.id, owner.id)
        row = (interaction.guild.id, owner.id)

    _guild_id, user_id = row

    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("❌ Guild not found.", ephemeral=True)
        return

    try:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
    except discord.NotFound:
        await interaction.followup.send(
            "❌ The ticket owner is no longer in the server.",
            ephemeral=True
        )
        return

    goats_role = guild.get_role(GOATS_ROLE_ID)
    if goats_role is not None and goats_role not in member.roles:
        try:
            await member.add_roles(goats_role, reason="Optimisation marked complete")
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I couldn't assign the GOATS role due to permissions/role hierarchy.",
                ephemeral=True
            )
            return
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"❌ Failed to assign GOATS role: {e}",
                ephemeral=True
            )
            return

    await save_completed_ticket(guild.id, interaction.channel.id, member.id)

    await interaction.channel.send(
        f"{member.mention}\n"
        "✅ Your optimisation has now been marked as complete.\n\n"
        "You have now been given the **GOATS** role.\n\n"
        "⏱ A **7-day review timer** has now started from this point.\n\n"
        f"During these 7 days, if you are happy with the service, please confirm this in <#{POST_SERVICE_CHANNEL_ID}>.\n\n"
        "If you experience any issues within scope, you must make us aware **in this open ticket** before the review period ends.\n\n"
        "You will receive a reminder after 7 days if you have not already completed the post-service confirmation.\n\n"
        "If no issues are raised within the review period, and you do not complete the confirmation within the additional 12-hour reminder window, the service will be considered **accepted and completed**, the relevant role will be assigned, and the ticket may then be closed.\n\n"
        "⚠️ Do not private message staff regarding support. Use this ticket only."
    )

    await interaction.followup.send("✅ Completion recorded and 7-day timer started.", ephemeral=True)

# ---------- Timer loops ----------
@tasks.loop(minutes=CHECK_EVERY_MINUTES)
async def timer_loop():
    now_ts = int(time.time())
    due = await fetch_due(now_ts)
    if not due:
        return

    for guild_id, user_id, _joined_at in due:
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


@tasks.loop(minutes=CHECK_EVERY_MINUTES)
async def post_service_loop():
    now_ts = int(time.time())
    rows = await fetch_completed_tickets()
    if not rows:
        return

    for channel_id, guild_id, user_id, completed_at, reminded, finalised in rows:
        guild = bot.get_guild(guild_id)
        if guild is None:
            continue

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            continue

        try:
            member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        except discord.NotFound:
            print(f"[INFO] Completed ticket user {user_id} no longer in guild {guild_id}")
            await mark_completed_ticket_finalised(channel_id)
            continue
        except discord.Forbidden:
            print(f"[WARN] Forbidden fetching member {user_id} in guild {guild_id}")
            continue
        except discord.HTTPException as e:
            print(f"[WARN] Failed fetching member {user_id}: {e}")
            continue

        post_service_role = guild.get_role(POST_SERVICE_ROLE_ID)
        has_post_service_role = post_service_role in member.roles if post_service_role else False

        elapsed = now_ts - completed_at

        # If they've already confirmed, mark finalised and move on
        if has_post_service_role and not finalised:
            await mark_completed_ticket_finalised(channel_id)
            continue

        # Send 7-day reminder
        if not reminded and elapsed >= POST_SERVICE_REVIEW_SECONDS:
            try:
                await channel.send(
                    f"{member.mention}\n"
                    "⏱ It has now been **7 days** since your optimisation was marked as complete.\n\n"
                    f"If you have not already done so, please confirm your satisfaction with the service in <#{POST_SERVICE_CHANNEL_ID}>.\n\n"
                    "If you have any issues within scope, you must make us aware **in this ticket** within the next **12 hours**.\n\n"
                    "Failure to do so will result in the service being considered **accepted and completed**, the relevant role being assigned, and the ticket becoming eligible for closure as outlined in the post-service confirmation process."
                )
                await mark_completed_ticket_reminded(channel_id)
                print(f"[INFO] Sent 7-day reminder in channel {channel_id}")
            except discord.Forbidden:
                print(f"[WARN] Missing permission to send reminder in channel {channel_id}")
            except discord.HTTPException as e:
                print(f"[WARN] Failed to send reminder in channel {channel_id}: {e}")
            continue

        # Finalise 12 hours after reminder if still no confirmation
        if reminded and not finalised and elapsed >= (POST_SERVICE_REVIEW_SECONDS + POST_SERVICE_FINAL_WARNING_SECONDS):
            if post_service_role is not None and post_service_role not in member.roles:
                try:
                    await member.add_roles(
                        post_service_role,
                        reason="Post-service review period expired without issues raised"
                    )
                except discord.Forbidden:
                    print(f"[WARN] Can't assign post-service role to {member} - permissions/role hierarchy issue")
                    continue
                except discord.HTTPException as e:
                    print(f"[WARN] Failed to assign post-service role to {member}: {e}")
                    continue

            try:
                await channel.send(
                    f"{member.mention}\n"
                    "✅ The post-service review period has now ended.\n\n"
                    "No issues were raised within the review window, and the service is now considered **accepted and completed** in line with our post-service confirmation policy.\n\n"
                    "The relevant role has now been assigned. If you require further assistance from this point, a new booking may be required."
                )
            except discord.Forbidden:
                print(f"[WARN] Missing permission to send finalisation message in channel {channel_id}")
            except discord.HTTPException as e:
                print(f"[WARN] Failed to send finalisation message in channel {channel_id}: {e}")

            await mark_completed_ticket_finalised(channel_id)
            print(f"[INFO] Finalised completed ticket for channel {channel_id}")


@timer_loop.before_loop
async def before_timer_loop():
    await bot.wait_until_ready()


@post_service_loop.before_loop
async def before_post_service_loop():
    await bot.wait_until_ready()


bot.run(DISCORD_TOKEN)
