"""
Slack slash command handlers.

All /agent subcommands are routed here. Handlers ack quickly and
delegate heavy processing to service modules via asyncio.create_task.
"""

import asyncio

from slack_bolt.async_app import AsyncApp

from pm_agent.app.logging_config import get_logger
from pm_agent.app.utils.permissions import (
    COMMAND_PERMISSION_DENIED,
    can_run_command,
    get_user_role,
)

logger = get_logger("slack_commands")


def register(app: AsyncApp) -> None:
    """Register slash command handlers on the Bolt app."""

    @app.command("/agent")
    async def handle_agent_command(ack, command, say, respond, client, context):
        """
        Main /agent command router.
        Dispatches to subcommand handlers based on the text after /agent.
        """
        await ack()  # Ack within 3 seconds

        raw_text = (command.get("text") or "").strip()
        user_id = command["user_id"]
        channel_id = command["channel_id"]
        trigger_id = command.get("trigger_id")

        # Parse subcommand
        if not raw_text:
            await respond(_help_text(user_id))
            return

        parts = raw_text.split(None, 1)
        subcommand = parts[0].lower()
        args_text = parts[1] if len(parts) > 1 else ""

        logger.info(
            f"Command: /agent {subcommand} | user={user_id} channel={channel_id}"
        )

        if not can_run_command(user_id, subcommand):
            await respond(COMMAND_PERMISSION_DENIED)
            return

        if subcommand == "setup-project":
            await _handle_setup_project(
                respond, client, user_id, channel_id, trigger_id, args_text
            )
        elif subcommand == "gather-info":
            await _handle_gather_info(
                respond, client, user_id, channel_id
            )
        elif subcommand == "status":
            await _handle_status(respond, user_id, channel_id)
        elif subcommand == "update":
            await _handle_update(respond, user_id, channel_id)
        elif subcommand == "blockers":
            await _handle_blockers(respond, user_id, channel_id)
        elif subcommand == "ask":
            await _handle_ask(respond, user_id, channel_id, args_text)
        elif subcommand in ("clarify", "raise-clarification"):
            await _handle_clarify(respond, client, user_id, channel_id, args_text)
        elif subcommand == "huddle-notes":
            await _handle_huddle_notes(
                respond, client, user_id, channel_id, trigger_id
            )
        elif subcommand == "help":
            await respond(_help_text(user_id))
        else:
            await respond(
                f"Unknown subcommand: `{subcommand}`. Run `/agent help` for available commands."
            )


async def _handle_setup_project(respond, client, user_id, channel_id, trigger_id, args_text):
    """Handle /agent setup-project — create or update project record."""
    from pm_agent.app.services.project_service import setup_project

    try:
        if args_text:
            # Parse inline arguments: name="X" client="Y" internal="#channel" github="url"
            result = await setup_project(
                user_id=user_id,
                channel_id=channel_id,
                args_text=args_text,
                client=client,
            )
            await respond(result)
        else:
            # Open modal for project setup
            from pm_agent.app.services.project_service import open_setup_modal

            await open_setup_modal(client, trigger_id, channel_id)
    except Exception as e:
        logger.error(f"Error in setup-project: {e}", exc_info=True)
        await respond(f"❌ Error setting up project: {str(e)}")


async def _handle_gather_info(respond, client, user_id, channel_id):
    """Handle /agent gather-info — extract scope from client conversation."""
    from pm_agent.app.services.gather_info_service import run_gather_info

    try:
        error = await _admin_command_error(user_id, channel_id, "gather-info")
        if error:
            await respond(error)
            return
        running_error = await _running_gather_error(channel_id)
        if running_error:
            await respond(running_error)
            return
        # Send immediate response, then run pipeline in background
        await respond("🔍 Gathering this channel's project discussion. I will DM you the results.")
        asyncio.create_task(
            run_gather_info(
                user_id=user_id,
                channel_id=channel_id,
                client=client,
            )
        )
    except Exception as e:
        logger.error(f"Error starting gather-info: {e}", exc_info=True)
        await respond(f"❌ Error: {str(e)}")


async def _handle_status(respond, user_id, channel_id):
    """Handle /agent status — show current project state."""
    from pm_agent.app.services.project_service import get_project_status

    try:
        result = await get_project_status(user_id, channel_id)
        await respond(result)
    except Exception as e:
        logger.error(f"Error in status: {e}", exc_info=True)
        await respond(f"❌ Error: {str(e)}")


async def _handle_update(respond, user_id, channel_id):
    """Handle /agent update — generate client-ready draft update."""
    from pm_agent.app.services.project_service import generate_project_update

    try:
        error = await _admin_command_error(user_id, channel_id, "update")
        if error:
            await respond(error)
            return
        await respond("📝 Generating project update. I will DM you the draft.")
        asyncio.create_task(
            generate_project_update(user_id=user_id, channel_id=channel_id)
        )
    except Exception as e:
        logger.error(f"Error in update: {e}", exc_info=True)
        await respond(f"❌ Error: {str(e)}")


async def _handle_blockers(respond, user_id, channel_id):
    """Handle /agent blockers — show blocked tasks and pending clarifications."""
    from pm_agent.app.services.project_service import get_project_blockers

    try:
        result = await get_project_blockers(user_id, channel_id)
        await respond(result)
    except Exception as e:
        logger.error(f"Error in blockers: {e}", exc_info=True)
        await respond(f"❌ Error: {str(e)}")


async def _handle_ask(respond, user_id, channel_id, question):
    """Handle /agent ask <question> — answer developer question."""
    from pm_agent.app.services.question_answer_service import (
        handle_slash_ask,
    )

    if not question:
        await respond("Please provide a question. Example: `/agent ask what did the client say about exports?`")
        return

    try:
        result = await handle_slash_ask(user_id, channel_id, question)
        await respond(result)
    except Exception as e:
        logger.error(f"Error in ask: {e}", exc_info=True)
        await respond(f"❌ Error: {str(e)}")


async def _handle_clarify(respond, client, user_id, channel_id, question):
    """Handle /agent clarify <question> — raise a developer clarification."""
    from pm_agent.app.services.clarification_service import handle_clarify_command

    if not question:
        await respond(
            "Please include your question. "
            "Example: `/agent clarify should cancelled users be able to download old invoices?`"
        )
        return

    try:
        result = await handle_clarify_command(user_id, channel_id, question, client)
        await respond(result)
    except Exception as e:
        logger.error(f"Error in clarify: {e}", exc_info=True)
        await respond(f"❌ Error: {str(e)}")


async def _handle_huddle_notes(respond, client, user_id, channel_id, trigger_id):
    """Handle /agent huddle-notes — open modal for manual huddle notes."""
    from pm_agent.app.services.huddle_notes_service import open_huddle_modal

    try:
        await open_huddle_modal(client, trigger_id, channel_id, user_id)
    except Exception as e:
        logger.error(f"Error opening huddle notes modal: {e}", exc_info=True)
        await respond(f"❌ Error: {str(e)}")


def _help_text(user_id: str) -> str:
    """Return help text for /agent command."""
    if get_user_role(user_id) == "admin":
        return (
            "*PM Agent Commands*\n\n"
            "• `/agent setup-project` - Create or update a project mapping\n"
            "• `/agent gather-info` - Analyze recent channel messages and create suggestions\n"
            "• `/agent huddle-notes` - Process pasted huddle notes\n"
            "• `/agent update` - Generate a private client-ready update draft\n"
            "• `/agent status` - Show project status\n"
            "• `/agent blockers` - Show blockers and pending clarifications\n"
            "• `/agent ask <question>` - Ask project questions with full admin access\n"
            "• `/agent clarify <question>` - Raise a clarification (searches existing answers first)\n"
            "• `/agent help` - Show this help menu"
        )

    return (
        "*PM Agent Commands*\n\n"
        "• `/agent status` - Show project status\n"
        "• `/agent blockers` - Show blockers and pending clarifications\n"
        "• `/agent huddle-notes` - Submit huddle notes for processing\n"
        "• `/agent ask <question>` - Ask implementation/project-context questions\n"
        "• `/agent clarify <question>` - Raise a clarification for admin review\n"
        "• `/agent help` - Show this help menu"
    )


async def _admin_command_error(
    user_id: str,
    channel_id: str,
    command_name: str,
) -> str | None:
    """Return a private slash-command error if user cannot run admin commands."""
    from pm_agent.app.db.project_queries import find_project_by_channel
    from pm_agent.app.db.session import get_session_factory

    session_factory = get_session_factory()
    async with session_factory() as db:
        project = await find_project_by_channel(db, channel_id)
        if not project:
            return "❌ No project configured for this channel. Run `/agent setup-project` first."
        if not can_run_command(user_id, command_name):
            return COMMAND_PERMISSION_DENIED
    return None


async def _running_gather_error(channel_id: str) -> str | None:
    """Return a private slash-command error when this channel is already gathering."""
    from pm_agent.app.db.gather_queries import get_running_gather_for_channel
    from pm_agent.app.db.project_queries import find_project_by_channel
    from pm_agent.app.db.session import get_session_factory

    session_factory = get_session_factory()
    async with session_factory() as db:
        project = await find_project_by_channel(db, channel_id)
        if not project:
            return None
        running = await get_running_gather_for_channel(db, project.id, channel_id)
        if not running:
            return None
        return (
            "⏳ A gather-info run is already in progress for this project channel. "
            "Please wait for it to finish before starting another one."
        )
