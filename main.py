import discord
from discord.app_commands import Range
from litellm import acompletion

key = ''
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)
model = "ollama/wizard-vicuna-uncensored:7b"


@tree.command(name="talk", description="Talk to the AI")
async def slash_command(interaction: discord.Interaction, message: str, temperature: Range[float, 0.01, 1.0] = 1.0):
    await interaction.response.defer()
    response = await acompletion(
        model=model,
        messages=[{"content": message, "role": "user"}],
        api_base="http://localhost:11434",
        temperature=temperature
    )
    print(response)
    await interaction.followup.send(response["choices"][0].message.content, view=Buttons(interaction.user))


@client.event
async def on_message(message):
    if message.author == client.user:
        return
    if message.reference:
        replied_message = await message.channel.fetch_message(message.id)
        original_message = replied_message.reference.message_id
        original_message_content = await message.channel.fetch_message(original_message)
        if original_message_content.author != client.user:
            return
        full_context = "bot: " + original_message_content.content + "\n user:" + message.content
        response = await acompletion(
            model=model,
            prompt=full_context,
            api_base="http://localhost:11434"
        )
        await replied_message.reply(response["choices"][0].message.content, view=Buttons(message.author))


class Buttons(discord.ui.View):
    def __init__(self, author, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.original_author = author

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.primary)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user == self.original_author:
            await interaction.message.delete()


@client.event
async def on_ready():
    cmds = await tree.sync()
    print("synced %d commands: %s." % (len(cmds), ", ".join(c.name for c in cmds)))


if __name__ == '__main__':
    client.run(key)
