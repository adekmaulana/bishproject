import re
from typing import TYPE_CHECKING, Any, MutableMapping, Tuple

import pyrogram
from pyrogram.filters import Filter, create

from .. import command, module, util
from .base import Base

if TYPE_CHECKING:
    from .bot import Bot


class CommandDispatcher(Base):
    commands: MutableMapping[str, command.Command]

    def __init__(self: "Bot", **kwargs: Any) -> None:
        self.commands = {}

        super().__init__(**kwargs)

    def register_command(self: "Bot", mod: module.Module, name: str,
                         func: command.CommandFunc) -> None:
        cmd = command.Command(name, mod, func)

        if name in self.commands:
            orig = self.commands[name]
            raise module.ExistingCommandError(orig, cmd)

        self.commands[name] = cmd

        for alias in cmd.aliases:
            if alias in self.commands:
                orig = self.commands[alias]
                raise module.ExistingCommandError(orig, cmd, alias=True)

            self.commands[alias] = cmd

    def unregister_command(self: "Bot", cmd: command.Command) -> None:
        del self.commands[cmd.name]

        for alias in cmd.aliases:
            try:
                del self.commands[alias]
            except KeyError:
                continue

    def register_commands(self: "Bot", mod: module.Module) -> None:
        for name, func in util.misc.find_prefixed_funcs(mod, "cmd_"):
            done = False

            try:
                self.register_command(mod, name, func)
                done = True
            finally:
                if not done:
                    self.unregister_commands(mod)

    def unregister_commands(self: "Bot", mod: module.Module) -> None:
        to_unreg = []

        for name, cmd in self.commands.items():
            if name != cmd.name:
                continue

            if cmd.module == mod:
                to_unreg.append(cmd)

        for cmd in to_unreg:
            self.unregister_command(cmd)

    def command_predicate(self: "Bot") -> Filter:

        async def func(_, __, msg: pyrogram.types.Message):
            if msg.text is not None and msg.text.startswith(self.prefix):
                parts = msg.text.split()
                parts[0] = parts[0][len(self.prefix):]
                msg.segments = parts
                return True

            return False

        return create(func)

    async def on_command(self: "Bot", _: pyrogram.Client,
                         msg: pyrogram.types.Message) -> None:
        cmd = None

        # Don't process via inline
        if msg.via_bot:
            return

        try:
            try:
                cmd = self.commands[msg.segments[0]]
            except KeyError:
                return

            # GoogleDrive module case
            if cmd.module.name == "GoogleDrive" and cmd.name not in ("gdclear",
                                                                     "gdreset"):
                await self.dispatch_event("command_gdrive", msg)

                if not cmd.module.creds:
                    return

            cmd_len = len(self.prefix) + len(msg.segments[0]) + 1
            if cmd.pattern is not None and msg.reply_to_message:
                matches = list(cmd.pattern.finditer(msg.reply_to_message.text))
            elif cmd.pattern and msg.text:
                matches = list(cmd.pattern.finditer(msg.text[cmd_len:]))
            else:
                matches = None

            ctx = command.Context(self, msg, msg.segments, cmd_len, matches)

            # Ensure specified argument needs are met
            if cmd.usage is not None and not ctx.input:
                err_base = f"⚠️ Missing parameters: {cmd.usage}"

                if cmd.usage_reply:
                    if msg.reply_to_message:
                        reply_msg = msg.reply_to_message
                        if reply_msg.text:
                            ctx.input = reply_msg.text
                        elif reply_msg.media:  # media doesn't have text pass it
                            pass
                        elif not cmd.usage_optional:
                            await ctx.respond(
                                f"{err_base}\n__The message you replied to doesn't contain text.__"
                            )
                            return
                    elif not cmd.usage_optional:
                        await ctx.respond(f"{err_base} (replying is also supported)")
                        return
                elif not cmd.usage_optional:
                    await ctx.respond(err_base)
                    return

            try:
                ret = await cmd.func(ctx)

                if isinstance(ret, Tuple):
                    if not isinstance(ret[1], (int, float)):
                        raise TypeError("Second value must be int/float, "
                                        f"got: {type(ret[1])}")
                    await ctx.respond(ret[0], delete_after=ret[1])
                elif ret is not None:
                    await ctx.respond(ret)
            except pyrogram.errors.MessageNotModified:
                cmd.module.log.warning(
                    f"Command '{cmd.name}' triggered a message edit with no changes"
                )
            except Exception as e:  # skipcq: PYL-W0703
                cmd.module.log.error(f"Error in command '{cmd.name}'",
                                     exc_info=e)
                await ctx.respond(
                    "**In**:\n"
                    f"{ctx.input if ctx.input is not None else msg.text}\n\n"
                    "**Out**:\n⚠️ Error executing command:\n"
                    f"```{util.error.format_exception(e)}```")

            await self.dispatch_event("command", cmd, msg)
        except Exception as e:  # skipcq: PYL-W0703
            if cmd is not None:
                cmd.module.log.error("Error in command handler", exc_info=e)

            await self.respond(
                msg,
                "⚠️ Error in command handler:\n"
                f"```{util.error.format_exception(e)}```",
            )
