# MW4 licensing in Fresh Spawn

Fresh Spawn keeps its existing Discord application and DISCORD_TOKEN. No second Discord bot service is needed. The optional mw4_licensing.py module is installed immediately before bot.run; the existing on_ready command sync publishes its commands. Missing or invalid configuration disables licensing commands without preventing the existing bot from starting.

Add these variables to the existing Fresh Spawn Railway service:

| Variable | Value |
| --- | --- |
| MFG_API_URL | Public HTTPS address of the MFG licensing API |
| MFG_INTERNAL_SECRET | Same 32+ character secret as INTERNAL_SECRET on the API |
| MFG_GUILD_ID | Your Discord server ID |
| MFG_STAFF_ROLE_ID | Role permitted to issue, inspect and revoke licences |
| MFG_CUSTOMER_ROLE_ID | MW4 Config role permitted to request a private activation code |

Keep values in Railway variables, never in this public repository. All four variables are required. Staff role membership and the server ID are checked on every command, including when Discord command permissions are changed. Commands cannot be used in DMs. Customer role membership is the eligibility check for self-service codes. No additional staff approval is required. A missing customer role setting disables self-service requests.

Commands:
- /mw4 panel: staff posts a persistent Get Activation Code button in the current channel.
- /mw4 code: members with the customer role receive their own code privately in the server. The button uses the same role and server checks on every click.
- /mw4 issue customer: sends a 24-hour activation code by DM. Reissuing invalidates an unused prior code. An active licence must be revoked before a new one is issued.
- /mw4 status customer: shows active/revoked MW4 device licences.
- /mw4 revoke customer: disables the active licence and any unused code. Members who retain the customer role can request a new code to transfer PCs. Remove the role as well when withdrawing access.

If DMs fail, customers can use /mw4 code or the activation button. Do not post codes in public channels. Staff replies are ephemeral.

Deploy the API from the private xijammy/MFG-Licensing repository and PostgreSQL in the same Railway project. Use the API changes in MFG-Licensing pull request #1. Do not deploy its standalone bot/ service when using this integration. The existing Fresh Spawn data.db remains separate from licensing; machine records live in the API's PostgreSQL database. Configure the desktop app with the same public API URL and build it after the API is working.

This module manages licences only; the standalone licensing bot's security alert polling is not included. Live Discord and Railway verification is still required before release.

