import asyncio

from pm_agent.app.agents.chains import gather_info_chain


class DummyLLM:
    def __init__(self):
        self.system_prompt = ""
        self.user_prompt = ""

    async def generate_json(self, system_prompt, user_prompt, schema=None):
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        return {
            "project_scope_summary": "summary",
            "confirmed_requirements": [],
            "suggested_tasks": [],
            "open_clarifications": [],
            "client_decisions": [],
            "scope_risks": [],
            "unconfirmed_assumptions": [],
        }


def test_gather_chain_uses_client_prompt_by_default():
    llm = DummyLLM()

    asyncio.run(gather_info_chain("hello", llm))

    assert "between a client and a project manager" in llm.system_prompt
    assert "Client Conversation Transcript" in llm.user_prompt


def test_gather_chain_uses_internal_prompt_for_internal_channel():
    llm = DummyLLM()

    asyncio.run(gather_info_chain("hello", llm, channel_kind="internal"))

    assert "internal Slack conversation" in llm.system_prompt
    assert "Do not treat developer suggestions" in llm.system_prompt
    assert "Internal Project Conversation Transcript" in llm.user_prompt
