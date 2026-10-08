"""
Slack interaction handlers — Block Kit button clicks and modal submissions.

Routes by action_id for approve/reject/edit suggestion flows.
"""

import re

from slack_bolt.async_app import AsyncApp

from pm_agent.app.logging_config import get_logger

logger = get_logger("slack_interactions")


def register(app: AsyncApp) -> None:
    """Register interaction handlers on the Bolt app."""

    @app.action("approve_suggestion_task")
    async def handle_approve_task(ack, action, body, client):
        """Approve a task suggestion — creates a task record."""
        await ack()
        from pm_agent.app.services.suggestion_approval_service import (
            approve_task_suggestion,
        )

        try:
            await approve_task_suggestion(action, body, client)
        except Exception as e:
            logger.error(f"Error approving task: {e}", exc_info=True)

    @app.action("approve_suggestion_clarification")
    async def handle_approve_clarification(ack, action, body, client):
        """Approve a clarification suggestion — creates a clarification record."""
        await ack()
        from pm_agent.app.services.suggestion_approval_service import (
            approve_clarification_suggestion,
        )

        try:
            await approve_clarification_suggestion(action, body, client)
        except Exception as e:
            logger.error(f"Error approving clarification: {e}", exc_info=True)

    @app.action("approve_suggestion_decision")
    async def handle_approve_decision(ack, action, body, client):
        """Approve a decision suggestion — saves to memory."""
        await ack()
        from pm_agent.app.services.suggestion_approval_service import (
            approve_decision_suggestion,
        )

        try:
            await approve_decision_suggestion(action, body, client)
        except Exception as e:
            logger.error(f"Error approving decision: {e}", exc_info=True)

    @app.action("acknowledge_scope_risk")
    async def handle_acknowledge_scope_risk(ack, action, body, client):
        """Acknowledge a scope risk suggestion — saves it to memory."""
        await ack()
        from pm_agent.app.services.suggestion_approval_service import (
            acknowledge_scope_risk,
        )

        try:
            await acknowledge_scope_risk(action, body, client)
        except Exception as e:
            logger.error(f"Error acknowledging scope risk: {e}", exc_info=True)

    @app.action("acknowledge_assumption")
    async def handle_acknowledge_assumption(ack, action, body, client):
        """Acknowledge an assumption suggestion — saves it to memory."""
        await ack()
        from pm_agent.app.services.suggestion_approval_service import (
            acknowledge_assumption,
        )

        try:
            await acknowledge_assumption(action, body, client)
        except Exception as e:
            logger.error(f"Error acknowledging assumption: {e}", exc_info=True)

    @app.action("reject_suggestion")
    async def handle_reject(ack, action, body, client):
        """Reject a suggestion."""
        await ack()
        from pm_agent.app.services.suggestion_approval_service import (
            reject_suggestion,
        )

        try:
            await reject_suggestion(action, body, client)
        except Exception as e:
            logger.error(f"Error rejecting suggestion: {e}", exc_info=True)

    @app.action("approve_all_tasks_from_gather")
    async def handle_approve_all(ack, action, body, client):
        """Approve all pending task suggestions from a gather run."""
        await ack()
        from pm_agent.app.services.suggestion_approval_service import (
            approve_all_tasks,
        )

        try:
            await approve_all_tasks(action, body, client)
        except Exception as e:
            logger.error(f"Error approving all tasks: {e}", exc_info=True)

    @app.action("edit_suggestion")
    async def handle_edit(ack, action, body, client):
        """Open edit modal for a suggestion."""
        await ack()
        from pm_agent.app.services.suggestion_approval_service import (
            open_edit_modal,
        )

        try:
            await open_edit_modal(action, body, client)
        except Exception as e:
            logger.error(f"Error opening edit modal: {e}", exc_info=True)

    @app.action("answer_clarification_internally")
    async def handle_answer_clarification(ack, action, body, client):
        """Open a modal to answer a clarification internally."""
        await ack()
        from pm_agent.app.services.suggestion_approval_service import (
            open_clarification_answer_modal,
        )

        try:
            await open_clarification_answer_modal(action, body, client)
        except Exception as e:
            logger.error(f"Error opening clarification answer modal: {e}", exc_info=True)

    @app.action(re.compile(r"^answer_clarification_option_\d+$"))
    async def handle_answer_clarification_option(ack, action, body, client):
        """Answer a clarification using one of its listed options."""
        await ack()
        from pm_agent.app.services.suggestion_approval_service import (
            answer_clarification_option,
        )

        try:
            await answer_clarification_option(action, body, client)
        except Exception as e:
            logger.error(f"Error answering clarification option: {e}", exc_info=True)

    # ── Modal submission handlers ──

    @app.view("setup_project_modal")
    async def handle_setup_project_submission(ack, view, body, client):
        """Handle project setup modal submission."""
        await ack()
        from pm_agent.app.services.project_service import (
            handle_setup_modal_submission,
        )

        try:
            await handle_setup_modal_submission(view, body, client)
        except Exception as e:
            logger.error(f"Error handling setup modal: {e}", exc_info=True)

    @app.view("edit_suggestion_modal")
    async def handle_edit_submission(ack, view, body, client):
        """Handle suggestion edit modal submission."""
        await ack()
        from pm_agent.app.services.suggestion_approval_service import (
            handle_edit_modal_submission,
        )

        try:
            await handle_edit_modal_submission(view, body, client)
        except Exception as e:
            logger.error(f"Error handling edit modal: {e}", exc_info=True)

    @app.view("clarification_answer_modal")
    async def handle_clarification_answer(ack, view, body, client):
        """Handle internal clarification answer modal submission."""
        await ack()
        from pm_agent.app.services.suggestion_approval_service import (
            handle_clarification_answer_submission,
        )

        try:
            await handle_clarification_answer_submission(view, body, client)
        except Exception as e:
            logger.error(f"Error handling clarification answer modal: {e}", exc_info=True)

    @app.view("huddle_notes_modal")
    async def handle_huddle_notes_submission(ack, view, body, client):
        """Handle huddle notes modal submission."""
        await ack()
        from pm_agent.app.services.huddle_notes_service import (
            handle_huddle_modal_submission,
        )

        try:
            await handle_huddle_modal_submission(view, body, client)
        except Exception as e:
            logger.error(f"Error handling huddle notes modal: {e}", exc_info=True)
