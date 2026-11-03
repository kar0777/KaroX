"""The slash commands, in one table the palette, the help and the dispatcher share.

A command typed in full, picked from the palette or listed by ``/help`` is the
same row, so the three can never disagree about what exists or what it is
called. Matching is pure: the palette asks for the rows a prefix selects and the
dispatcher resolves an alias to its command, both without a screen.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SlashCommand:
    name: str
    #: i18n key of the one-line description shown in the palette and /help.
    description: str
    aliases: tuple[str, ...] = ()
    #: Shown after the name, e.g. ``<command>``; empty when it takes none.
    argument: str = ""
    #: True when the command is useless without its argument, so picking it
    #: from the palette inserts ``/name `` instead of running it.
    requires_argument: bool = False


COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("help", "cmd.help", aliases=("?",)),
    SlashCommand("connect", "cmd.connect", aliases=("provider", "login"), argument="[provider]"),
    SlashCommand("model", "cmd.model", aliases=("models",), argument="[provider/model]"),
    SlashCommand("sessions", "cmd.sessions", aliases=("resume",), argument="[id]"),
    SlashCommand("new", "cmd.new"),
    SlashCommand("session", "cmd.session"),
    SlashCommand("status", "cmd.status"),
    SlashCommand("mcp", "cmd.mcp", argument="[add]"),
    SlashCommand("skills", "cmd.skills"),
    SlashCommand("verify", "cmd.verify", argument="<command>", requires_argument=True),
    SlashCommand("unverify", "cmd.unverify"),
    SlashCommand("effort", "cmd.effort", argument="[level]"),
    SlashCommand("mode", "cmd.mode", argument="[build|plan|ideate]"),
    SlashCommand("lang", "cmd.lang", argument="[ru|en]"),
    SlashCommand("reasoning", "cmd.reasoning"),
    SlashCommand("doctor", "cmd.doctor"),
    SlashCommand("clear", "cmd.clear"),
    SlashCommand("stop", "cmd.stop"),
    SlashCommand("quit", "cmd.quit", aliases=("exit", "q")),
)

_BY_NAME: dict[str, SlashCommand] = {}
for _command in COMMANDS:
    _BY_NAME[_command.name] = _command
    for _alias in _command.aliases:
        _BY_NAME.setdefault(_alias, _command)


def resolve(name: str) -> SlashCommand | None:
    """The command a typed name or alias means, or ``None``."""

    return _BY_NAME.get(name.strip().lower())


def match(query: str) -> list[SlashCommand]:
    """Commands for a palette query (the text after ``/``), best first.

    Names and aliases that start with the query come first, in table order;
    then names that merely contain it. An empty query lists everything.
    """

    needle = query.strip().lower()
    if not needle:
        return list(COMMANDS)
    prefix: list[SlashCommand] = []
    inner: list[SlashCommand] = []
    for command in COMMANDS:
        names = (command.name, *command.aliases)
        if any(item.startswith(needle) for item in names):
            prefix.append(command)
        elif any(needle in item for item in names):
            inner.append(command)
    return prefix + inner


__all__ = ["COMMANDS", "SlashCommand", "match", "resolve"]
