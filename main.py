import asyncio
import csv
import json
import random
from dataclasses import dataclass

import discord
from discord import app_commands, ui
from discord.app_commands import Range, commands
from discord.ext import tasks
from litellm import acompletion

from character import Character

config = json.loads(open("./config.json", "r").read())
blocked_llm_terms = list(csv.reader(open("./blocked_llm_terms.csv", "r")))
api_url = config["api_url"]
key = config["discord_api_key"]
minimum_tokens = config["minimum_tokens"]
max_retries = config["max_retries"]
max_tokens = config["max_tokens"]

characters = config["characters"]
current_character = characters["default"]
announce_channels = config["announce_channels"]
should_auto_switch_character = config["auto_switch_characters"]
initialized_auto_character = False

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)

queue = []


class ReplyModal(ui.Modal, title="Reply"):
    def __init__(self, history, character, original_author, temperature):
        super().__init__(timeout=None)
        self.history = history
        self.character = character
        self.original_author = original_author
        self.temperature = temperature

        self.prompt = ui.TextInput(label="Prompt", placeholder="Enter a prompt", max_length=256, required=True, default="", style=discord.TextStyle.paragraph)
        self.add_item(self.prompt)

    async def on_submit(self, interaction: discord.Interaction, /) -> None:
        await interaction.response.send_message(f"{interaction.user.mention}: {self.prompt}")
        full_prompt = f"{self.history}\n **user:**{self.prompt}"
        response = await generate(full_prompt, self.character, self.temperature)
        if response is None:
            await interaction.message.reply("Failed to generate a response. Please try again.", view=Buttons(self.original_author, self.history, self.history, self.character, self.temperature))
            return
        truncated_response = f"**{self.character['name']}:**\n" + response["choices"][0].message.content[:1800]

        self.history = full_prompt + "\n" + truncated_response
        await interaction.message.reply(content=truncated_response, view=Buttons(self.original_author, self.history, full_prompt, self.character, self.temperature))


class Buttons(discord.ui.View):
    def __init__(self, author, reply_history, reroll_history, character, temperature=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.original_author = author
        self.original_message = reply_history
        self.reroll_history = reroll_history
        self.character = character
        self.temperature = temperature
        self.timeout = None

    @discord.ui.button(label="Reply", style=discord.ButtonStyle.blurple, emoji="↪️")
    async def reply(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.original_author:
            return
        modal = ReplyModal(self.original_message + "\n" + interaction.message.content, self.character, self.original_author, self.temperature)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Retry", style=discord.ButtonStyle.green, emoji="🔄")
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.original_author:
            return
        await interaction.response.defer()
        await interaction.message.edit(content="Retrying...", view=None)
        response = await generate(self.reroll_history, self.character, self.temperature)
        if response is None:
            await interaction.message.reply("Failed to generate a response. Please try again.", view=Buttons(self.original_author, self.history, self.history, self.character, self.temperature))
            return
        truncated_response = f"**{self.character['name']}:**\n" + response["choices"][0].message.content[:1800]
        await interaction.message.edit(content=truncated_response, view=Buttons(self.original_author, self.reroll_history, self.reroll_history, self.character))

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.red, emoji="🗑️")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user == self.original_author:
            await interaction.message.delete()

    @discord.ui.button(label="Log", style=discord.ButtonStyle.grey, emoji="📝")
    async def log(self, interaction: discord.Interaction, button: discord.ui.Button):
        message = f"```{self.reroll_history} \n {interaction.message.content}```"
        if len(message) >= 2000:
            with open("log.txt", "w") as log:
                log.write(message)
            with open("log.txt", "rb") as log:
                await interaction.response.send_message("Log is too large, sending as a file.", file=discord.File(log, filename="log.txt"))
        else:
            await interaction.response.send_message(message, ephemeral=True)


class NotPermitted(commands.CheckFailure):
    pass


def has_permission():
    async def predicate(interaction: discord.Interaction):
        for role in interaction.user.roles:
            if str(role.id) in config["elevated_roles"]:
                return True
        raise NotPermitted("You do not have permission to use this command.")

    return discord.app_commands.check(predicate)


@dataclass
class GenerationQueueItem:
    def __init__(self, message, character, temperature):
        self.message = message
        self.character = character
        self.response = None
        self.temperature = temperature
        self.failed = False


async def generate(message, character, temperature=None):
    global queue
    item = GenerationQueueItem(message, character, temperature)
    queue.append(item)
    while item.response is None:
        await asyncio.sleep(1)
    if item.failed == False:
        return item.response
    return None


def contains_blocked_terms(content):
    return any(term in content.lower() for row in blocked_llm_terms for term in row)


async def process_generation_queue():
    global queue
    while True:
        if len(queue) > 0:
            item = queue.pop(0)
            tokens = 0
            retries = 0
            while tokens < minimum_tokens:
                response = await acompletion(
                    model=item.character["model"],
                    messages=[{"content": f"{item.message}", "role": "user"}],
                    temperature=item.temperature,
                    api_base=api_url,
                    num_retries=max_retries,
                    max_tokens=max_tokens,
                    timeout=40
                )
                tokens = int(response["usage"]["completion_tokens"])
                if response["choices"][0].message.content.strip() == "":
                    tokens = 0
                if contains_blocked_terms(response["choices"][0].message.content.strip()):
                    print(f"said a bad word: {response['choices'][0].message.content}")
                    tokens = 0
                retries += 1
                if retries >= max_retries:
                    print(f"Failed to generate a response: {response}")
                    item.failed = True
                    break
            item.response = response
        await asyncio.sleep(1)


@tree.command(name="talk", description="Talk to the AI")
@app_commands.choices(character_name=[app_commands.Choice(name=characters[key]["name"], value=key) for key in characters])
async def slash_command(
        interaction: discord.Interaction,
        message: str,
        temperature: Range[float, 0.01, 2.0] = None,
        character_name: str = None
):
    await interaction.response.defer()
    character = characters[character_name] if character_name else current_character
    response = await generate(message, character, temperature)
    if response is None:
        await interaction.followup.send("Failed to generate a response. Please try again.", view=Buttons(interaction.user, message, message, character, temperature))
        return
    truncated_response = f"**{character['name']}:**\n" + response["choices"][0].message.content[:1800]
    await interaction.followup.send(truncated_response, view=Buttons(interaction.user, message, message, character, temperature))


@tree.command(name="change_character", description="Change the character")
@app_commands.choices(name=[app_commands.Choice(name=characters[key]["name"], value=key) for key in characters])
@has_permission()
async def slash_command(interaction: discord.Interaction, name: str):
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
        response = await generate(full_context, character)
        if response is None:
            await interaction.message.reply("Failed to generate a response. Please try again.", view=Buttons(self.original_author, self.history, self.history, self.character, self.temperature))
            return
        truncated_response = f"**{character['name']}:**\n" + response["choices"][0].message.content[:1800]
        await replied_message.reply(truncated_response, view=Buttons(message.author, truncated_response, full_context, character))


async def change_character(character: Character, guild: discord.Guild, silent=False):
    if not character:
        return

    global current_character
    current_character = character

    try:
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
    asyncio.create_task(process_generation_queue())
    for guild in client.guilds:
        await change_character(current_character, guild, True)
    cmds = await tree.sync()
    print("synced %d commands: %s." % (len(cmds), ", ".join(c.name for c in cmds)))
    auto_change_character.start()


if __name__ == "__main__":
    client.run(key)
