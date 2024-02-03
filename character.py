from dataclasses import dataclass


@dataclass
class Character:
    def __init__(self, name, model, avatar, intro_message):
        self.name = name
        self.model = model
        self.avatar = avatar
        self.intro_message = intro_message
