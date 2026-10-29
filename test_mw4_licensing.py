import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from discord.ext import commands
import mw4_licensing


class LicensingCommands(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        self.env = patch.dict(os.environ, {
            'MFG_API_URL': 'https://licensing.example.test',
            'MFG_INTERNAL_SECRET': 'test-only-secret' * 3,
            'MFG_GUILD_ID': '123', 'MFG_STAFF_ROLE_ID': '456', 'MFG_CUSTOMER_ROLE_ID': '7890',
        })
        self.env.start()

    async def asyncTearDown(self):
        self.env.stop()
        await self.bot.close()

    def interaction(self, guild=123, role=456):
        member = MagicMock(spec=discord.Member)
        member.id = 123456789
        member.roles = [SimpleNamespace(id=role)]
        return SimpleNamespace(guild_id=guild, user=member,
                               response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
                               followup=SimpleNamespace(send=AsyncMock()))

    async def test_missing_configuration_does_not_register_commands(self):
        with patch.dict(os.environ, {'MFG_API_URL': ''}):
            self.assertFalse(mw4_licensing.install(self.bot))
        self.assertIsNone(self.bot.tree.get_command('mw4'))

    async def test_permissions_reject_wrong_server_and_role(self):
        self.assertTrue(mw4_licensing.install(self.bot))
        group = self.bot.tree.get_command('mw4')
        self.assertEqual({c.name for c in group.commands}, {'issue', 'status', 'revoke', 'panel', 'code'})
        for command in group.commands:
            if command.name == 'code':
                continue
            self.assertTrue(await command.checks[0](self.interaction()))
            self.assertFalse(await command.checks[0](self.interaction(guild=999)))
            self.assertFalse(await command.checks[0](self.interaction(role=999)))

    async def test_issue_sends_code_by_dm_and_handles_failure(self):
        mw4_licensing.install(self.bot)
        command = self.bot.tree.get_command('mw4').get_command('issue')
        session = MagicMock()
        response = MagicMock(status=200)
        response.json = AsyncMock(return_value={'code': 'PRIVATE-CODE'})
        session.request.return_value.__aenter__ = AsyncMock(return_value=response)
        session.request.return_value.__aexit__ = AsyncMock(return_value=False)
        customer = SimpleNamespace(id=789, send=AsyncMock())
        with patch('mw4_licensing.aiohttp.ClientSession') as factory:
            factory.return_value.__aenter__ = AsyncMock(return_value=session)
            factory.return_value.__aexit__ = AsyncMock(return_value=False)
            interaction = self.interaction()
            await command.callback(interaction, customer)
            self.assertIn('PRIVATE-CODE', customer.send.await_args.args[0])
            self.assertNotIn('PRIVATE-CODE', interaction.followup.send.await_args.args[0])
            self.assertFalse(session.request.call_args.kwargs['allow_redirects'])
            customer.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'DMs disabled')
            await command.callback(interaction, customer)
            self.assertIn('could not deliver', interaction.followup.send.await_args.args[0])



    async def test_customer_role_and_private_response(self):
        mw4_licensing.install(self.bot)
        command = self.bot.tree.get_command('mw4').get_command('code')
        with patch('mw4_licensing.aiohttp.ClientSession') as factory:
            for interaction in (self.interaction(role=456), self.interaction(role=999), self.interaction(guild=999, role=7890)):
                await command.callback(interaction)
                self.assertTrue(interaction.response.send_message.await_args.kwargs['ephemeral'])
            factory.assert_not_called()
            session = MagicMock()
            response = MagicMock(status=200)
            response.json = AsyncMock(return_value={'code': 'PRIVATE-CODE'})
            session.request.return_value.__aenter__ = AsyncMock(return_value=response)
            session.request.return_value.__aexit__ = AsyncMock(return_value=False)
            factory.return_value.__aenter__ = AsyncMock(return_value=session)
            factory.return_value.__aexit__ = AsyncMock(return_value=False)
            interaction = self.interaction(role=7890)
            await command.callback(interaction)
            self.assertEqual(session.request.call_args.kwargs['json']['discord_id'], '123456789')
            self.assertTrue(interaction.response.defer.await_args.kwargs['ephemeral'])
            self.assertTrue(interaction.followup.send.await_args.kwargs['ephemeral'])
            self.assertIn('PRIVATE-CODE', interaction.followup.send.await_args.args[0])
            response.status = 409
            response.json.return_value = {'detail': 'Already active'}
            await command.callback(interaction)
            self.assertEqual(interaction.followup.send.await_args.args[0], 'Already active')
            self.assertTrue(interaction.followup.send.await_args.kwargs['ephemeral'])

    async def test_persistent_button_rechecks_role(self):
        mw4_licensing.install(self.bot)
        for listener in self.bot.extra_events['on_ready']:
            await listener()
            await listener()
        self.assertEqual(len(self.bot.persistent_views), 1)
        view = self.bot.persistent_views[0]
        self.assertTrue(view.is_persistent())
        self.assertEqual(view.children[0].custom_id, 'mfg:mw4:claim:v1')
        interaction = self.interaction(role=999)
        with patch('mw4_licensing.aiohttp.ClientSession') as factory:
            await view.children[0].callback(interaction)
            factory.assert_not_called()
        self.assertTrue(interaction.response.send_message.await_args.kwargs['ephemeral'])

    async def test_missing_customer_role_fails_closed(self):
        with patch.dict(os.environ, {'MFG_CUSTOMER_ROLE_ID': ''}):
            mw4_licensing.install(self.bot)
        interaction = self.interaction(role=7890)
        with patch('mw4_licensing.aiohttp.ClientSession') as factory:
            await self.bot.tree.get_command('mw4').get_command('code').callback(interaction)
            factory.assert_not_called()
        self.assertTrue(interaction.response.send_message.await_args.kwargs['ephemeral'])

if __name__ == '__main__':
    unittest.main()

