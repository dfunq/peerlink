"""What peerlink's chat platforms share: Discord, Telegram and Slack.

A channel id says its platform: Discord's are bare digits, as they always were, Telegram's `tg:<chat id>` and
Slack's `slack:<channel id>`. Each platform's module (discordlink, …) offers the same calls, and the daemon picks one
by the prefix: post, clear_buttons, answer_press, react, unreact, download.
"""

NAMES = {"discord": "Discord", "telegram": "Telegram", "slack": "Slack"}
PREFIXES = {"tg": "telegram", "slack": "slack"}


class ChatError(ValueError):
    """A platform refused a call, or couldn't be reached; the text says which and why."""


def platform(channel_id):
    head, sep, _ = str(channel_id or "").partition(":")
    return PREFIXES.get(head, "discord") if sep else "discord"


def name(channel_id):
    """The platform's name for people: "Discord", "Telegram", "Slack"."""
    return NAMES[platform(channel_id)]
