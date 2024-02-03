import json
import random

import discord
from discord import app_commands
from discord.app_commands import Range, commands
from discord.ext import tasks
from litellm import acompletion

from character import Character

config = json.loads(open("./config.json", "r").read())
api_url = config["api_url"]
key = config["discord_api_key"]

characters = config["characters"]
current_character = characters["default"]
announce_channels = config["announce_channels"]
should_auto_switch_character = config["auto_switch_characters"]
initialized_auto_character = False

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)


class Buttons(discord.ui.View):
    def __init__(self, author, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.original_author = author

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.red, emoji="🗑️")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user == self.original_author:
            await interaction.message.delete()


class NotPermitted(commands.CheckFailure):
    pass


def has_permission():
    async def predicate(interaction: discord.Interaction):
        for role in interaction.user.roles:
            if str(role.id) in config["elevated_roles"]:
                return True
        raise NotPermitted("You do not have permission to use this command.")

    return discord.app_commands.check(predicate)


@tree.command(name="talk", description="Talk to the AI")
@app_commands.choices(character_name=[app_commands.Choice(name=characters[key]["name"], value=key) for key in characters])
async def slash_command(
        interaction: discord.Interaction,
        message: str,
        temperature: Range[float, 0.01, 2.0] = 1.0,
        character_name: str = None
):
    await interaction.response.defer()
    character = characters[character_name] if character_name else current_character
    response = await acompletion(
        model=character["model"],
        messages=[{"content": f"{message}", "role": "user"}],
        api_base=api_url,
        temperature=temperature,
    )
    print(response)
    truncated_response = f"**{character['name']}:**\n" + response["choices"][0].message.content[:1800]
    await interaction.followup.send(truncated_response, view=Buttons(interaction.user))


@tree.command(name="change_character", description="Change the character")
@app_commands.choices(name=[app_commands.Choice(name=characters[key]["name"], value=key) for key in characters])
@has_permission()
async def slash_command(interaction: discord.Interaction, name: str):
    # get character
    character = characters[name]
    await change_character(character, interaction.guild)


@tasks.loop(seconds=config["character_change_interval"])
async def auto_change_character():
    global initialized_auto_character, should_auto_switch_character
    if not should_auto_switch_character:
        return
    if not initialized_auto_character:
        initialized_auto_character = True
        return
    if len(characters) < 3:
        return
    character = current_character
    while character == current_character or character["name"] == "default":
        character = characters[random.choice(list(characters.keys()))]
    for guild in client.guilds:
        await change_character(character, guild)


@slash_command.error
async def slash_command_error(ctx, error):
    if isinstance(error, NotPermitted):
        await ctx.response.send_message("You do not have permission to use this command.", ephemeral=True)


@client.event
async def on_message(message):
    if message.author == client.user:
        return
    if message.reference:
        replied_message = await message.channel.fetch_message(message.id)
        original_message = replied_message.reference.message_id
        original_message_content = await message.channel.fetch_message(original_message)
        character = current_character
        if original_message_content.author != client.user:
            return
        full_context = "bot: " + original_message_content.content + "\n user:" + message.content
        response = await acompletion(model=character["model"], prompt=full_context, api_base=api_url)
        print(response)
        truncated_response = f"**{character['name']}:**\n" + response["choices"][0].message.content[:1800]
        await replied_message.reply(truncated_response, view=Buttons(message.author))


async def change_character(character: Character, guild: discord.Guild, silent=False):
    if not character:
        return

    global current_character
    current_character = character

    try:
        # load avatar as bytes
        with open(character["avatar"], "rb") as avatar_file:
            await client.user.edit(avatar=avatar_file.read())
    except Exception as e:
        print(e)
    await client.change_presence(activity=discord.Game(name=character["name"]))
    if not silent:
        for channel_id in announce_channels:
            channel = await client.fetch_channel(channel_id)
            await channel.send(character["intro_message"])


@client.event
async def on_ready():
    global current_character
    for guild in client.guilds:
        await change_character(current_character, guild, True)
    cmds = await tree.sync()
    print("synced %d commands: %s." % (len(cmds), ", ".join(c.name for c in cmds)))
    auto_change_character.start()


if __name__ == "__main__":
    client.run(key)
