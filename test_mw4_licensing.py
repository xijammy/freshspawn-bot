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
            'MFG_GUILD_ID': '123', 'MFG_STAFF_ROLE_ID': '456',
        })
        self.env.start()

    async def asyncTearDown(self):
        self.env.stop()
        await self.bot.close()

    def interaction(self, guild=123, role=456):
        member = MagicMock(spec=discord.Member)
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
        self.assertEqual({c.name for c in group.commands}, {'issue', 'status', 'revoke'})
        for command in group.commands:
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


if __name__ == '__main__':
    unittest.main()
