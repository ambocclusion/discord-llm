import json
import random

import discord
from discord import app_commands, ui
from discord.app_commands import Range, commands
from discord.ext import tasks
from litellm import acompletion

from character import Character

config = json.loads(open("./config.json", "r").read())
api_url = config["api_url"]
key = config["discord_api_key"]
max_tokens = config["max_tokens"]

characters = config["characters"]
current_character = characters["default"]
announce_channels = config["announce_channels"]
should_auto_switch_character = config["auto_switch_characters"]
initialized_auto_character = False

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)


class ReplyModal(ui.Modal, title="Reply"):
    def __init__(self, history, character, original_author):
        super().__init__(timeout=None)
        self.history = history
        self.character = character
        self.original_author = original_author
        self.prompt = ui.TextInput(label="Prompt", placeholder="Enter a prompt", max_length=256, required=True, default="", style=discord.TextStyle.paragraph)

        self.add_item(self.prompt)

    async def on_submit(self, interaction: discord.Interaction, /) -> None:
        await interaction.response.send_message(f"{interaction.user.mention}: {self.prompt}")
        full_prompt = f"{self.history}\n **user:**{self.prompt}"
        print(full_prompt)
        response = await acompletion(
            model=self.character["model"],
            messages=[{"content": full_prompt, "role": "user"}],
            api_base=api_url,
            num_retries=3,
            max_tokens=max_tokens,
            timeout=40
        )
        truncated_response = f"**{self.character['name']}:**\n" + response["choices"][0].message.content[:1800]

        self.history = full_prompt + "\n" + truncated_response
        print(f"self.history: {self.history}")
        await interaction.message.reply(content=truncated_response, view=Buttons(self.original_author, self.history, full_prompt, self.character))


class Buttons(discord.ui.View):
    def __init__(self, author, reply_history, reroll_history, character, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.original_author = author
        self.original_message = reply_history
        self.reroll_history = reroll_history
        self.character = character

    @discord.ui.button(label="Reply", style=discord.ButtonStyle.blurple, emoji="↪️")
    async def reply(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.original_author:
            return
        modal = ReplyModal(self.original_message + "\n" + interaction.message.content, self.character, self.original_author)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Retry", style=discord.ButtonStyle.green, emoji="🔄")
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.original_author:
            return
        await interaction.response.defer()
        await interaction.message.edit(content="Retrying...", view=None)
        print(f"self.original_message: {self.reroll_history}")
        response = await acompletion(
            model=self.character["model"],
            messages=[{"content": f"{self.reroll_history}", "role": "user"}],
            api_base=api_url,
            num_retries=3,
            max_tokens=max_tokens,
            timeout=40
        )
        # print(response)
        truncated_response = f"**{self.character['name']}:**\n" + response["choices"][0].message.content[:1800]
        await interaction.message.edit(content=truncated_response, view=Buttons(self.original_author, self.reroll_history, self.reroll_history, self.character))

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
        num_retries=3,
        max_tokens=max_tokens,
        timeout=40
    )
    print(response)
    truncated_response = f"**{character['name']}:**\n" + response["choices"][0].message.content[:1800]
    await interaction.followup.send(truncated_response, view=Buttons(interaction.user, message, message, character))


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
        full_context = original_message_content.content + "\n user:" + message.content
        response = await acompletion(
            model=character["model"],
            prompt=full_context,
            api_base=api_url,
            num_retries=3,
            max_tokens=max_tokens,
            timeout=40
        )
        print(response)
        truncated_response = f"**{character['name']}:**\n" + response["choices"][0].message.content[:1800]
        await replied_message.reply(truncated_response, view=Buttons(message.author, truncated_response, full_context, character))


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
