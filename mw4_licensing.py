"""Optional MW4 staff commands for the existing Fresh Spawn bot."""
import asyncio
import os
from urllib.parse import urlsplit

import aiohttp
import discord
from discord import app_commands


def install(bot):
    values = {name: os.getenv(name, '').strip() for name in (
        'MFG_API_URL', 'MFG_INTERNAL_SECRET', 'MFG_GUILD_ID', 'MFG_STAFF_ROLE_ID')}
    if not all(values.values()):
        print('[MFG] Licensing commands disabled: set all four MFG variables to enable.')
        return False
    url = values['MFG_API_URL'].rstrip('/')
    try:
        parsed = urlsplit(url)
    except ValueError:
        print('[MFG] Licensing commands disabled: invalid API URL.')
        return False
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or not values['MFG_GUILD_ID'].isdigit()
            or not values['MFG_STAFF_ROLE_ID'].isdigit()
            or len(values['MFG_INTERNAL_SECRET']) < 32):
        print('[MFG] Licensing commands disabled: invalid MFG configuration.')
        return False
    guild_id = int(values['MFG_GUILD_ID'])
    role_id = int(values['MFG_STAFF_ROLE_ID'])

    async def allowed(interaction):
        member = interaction.user
        permitted = (interaction.guild_id == guild_id
                     and isinstance(member, discord.Member)
                     and any(role.id == role_id for role in member.roles))
        if not permitted:
            await interaction.response.send_message('Only authorised MFG staff can use this command in the configured server.', ephemeral=True)
        return permitted

    async def request(method, path, **kwargs):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.request(method, url + path,
                    headers={'X-Internal-Secret': values['MFG_INTERNAL_SECRET']},
                    allow_redirects=False, **kwargs) as response:
                if response.status >= 500:
                    raise RuntimeError('The licensing server is unavailable. Please try again shortly.')
                if response.status in (401, 403):
                    raise RuntimeError('The licensing server refused access. Check the server and bot secret settings.')
                try:
                    data = await response.json()
                except (ValueError, aiohttp.ContentTypeError):
                    raise RuntimeError('The licensing server returned an unexpected response. Check MFG_API_URL.')
                if response.status >= 300:
                    detail = data.get('detail', 'Licensing request failed.')
                    raise RuntimeError(str(detail)[:500])
                return data

    async def report_error(interaction, exc):
        text = str(exc) if isinstance(exc, RuntimeError) else 'Unable to reach the licensing service. Please try again shortly.'
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    group = app_commands.Group(name='mw4', description='Manage single-PC MW4 licences', guild_only=True)

    @group.command(name='issue', description='Send a customer an MW4 activation code by DM')
    @app_commands.check(allowed)
    async def issue(interaction: discord.Interaction, customer: discord.Member):
        await interaction.response.defer(ephemeral=True)
        try:
            data = await request('POST', '/internal/activation/issue', json={'discord_id': str(customer.id), 'product_id': 'mw4'})
            try:
                await customer.send(
                    f"Your MFG MW4 activation code is:\n`{data['code']}`\n"
                    'Enter it in the MW4 Config Tool on your own PC. It expires in 24 hours and activates one PC. Keep it private.')
            except discord.HTTPException:
                await interaction.followup.send(
                    'The code was created but Discord could not deliver the DM. Ask the customer to enable DMs, then run /mw4 issue again. The new code replaces the previous one.', ephemeral=True)
                return
            await interaction.followup.send('Activation code sent to the customer by DM.', ephemeral=True)
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
            await report_error(interaction, exc)

    @group.command(name='status', description="Check a customer's MW4 licence")
    @app_commands.check(allowed)
    async def status(interaction: discord.Interaction, customer: discord.Member):
        await interaction.response.defer(ephemeral=True)
        try:
            data = await request('GET', f'/internal/licence/{customer.id}')
            licences = [item for item in data['licences'] if item['product_id'] == 'mw4']
            text = '\n'.join(f"{'Active' if item['active'] else 'Revoked'} — device {item['device_id']}" for item in licences)
            await interaction.followup.send((text or 'No activated MW4 licence found.')[:1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
            await report_error(interaction, exc)

    @group.command(name='revoke', description="Disable a customer's active MW4 licence")
    @app_commands.check(allowed)
    async def revoke(interaction: discord.Interaction, customer: discord.Member):
        await interaction.response.defer(ephemeral=True)
        try:
            await request('POST', '/internal/revoke', json={'discord_id': str(customer.id), 'product_id': 'mw4'})
            await interaction.followup.send('MW4 licence revoked. To allow a replacement PC, run /mw4 issue and have the customer activate on that PC.', ephemeral=True)
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
            await report_error(interaction, exc)

    @group.error
    async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            return
        text = 'The licensing command could not complete. Please try again or contact the server owner.'
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    bot.tree.add_command(group)
    print('[MFG] MW4 licensing commands registered.')
    return True
