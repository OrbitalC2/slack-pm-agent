from types import SimpleNamespace

from pm_agent.app.agents.schemas import (
    ClientDecision,
    ConfirmedRequirement,
    GatherInfoOutput,
    OpenClarification,
    ScopeRisk,
    SuggestedTask,
    UnconfirmedAssumption,
)
from pm_agent.app.utils.gather_sources import (
    collect_output_source_ids,
    sanitize_gather_output_sources,
)
from pm_agent.app.utils.source_refs import (
    huddle_run_source_ref,
    slack_message_source_ref,
    source_refs_for_message_ids,
)


def test_sanitize_gather_sources_drops_invalid_items_for_all_collections():
    output = GatherInfoOutput(
        confirmed_requirements=[
            ConfirmedRequirement(text="valid req", source_message_ids=["m1", "bad"]),
            ConfirmedRequirement(text="invalid req", source_message_ids=["bad"]),
        ],
        suggested_tasks=[
            SuggestedTask(title="valid task", source_message_ids=["m2"]),
            SuggestedTask(title="invalid task", source_message_ids=[]),
        ],
        open_clarifications=[
            OpenClarification(question="valid clar", source_message_ids=["m1"]),
            OpenClarification(question="invalid clar", source_message_ids=["nope"]),
        ],
        client_decisions=[
            ClientDecision(decision="valid decision", source_message_ids=["m2"]),
            ClientDecision(decision="invalid decision", source_message_ids=["missing"]),
        ],
        scope_risks=[
            ScopeRisk(risk="valid risk", source_message_ids=["m1"]),
            ScopeRisk(risk="invalid risk", source_message_ids=[]),
        ],
        unconfirmed_assumptions=[
            UnconfirmedAssumption(assumption="valid assumption", source_message_ids=["m2"]),
            UnconfirmedAssumption(assumption="invalid assumption", source_message_ids=["bad"]),
        ],
    )

    sanitize_gather_output_sources(output, {"m1", "m2"})

    assert [r.text for r in output.confirmed_requirements] == ["valid req"]
    assert output.confirmed_requirements[0].source_message_ids == ["m1"]
    assert [t.title for t in output.suggested_tasks] == ["valid task"]
    assert [c.question for c in output.open_clarifications] == ["valid clar"]
    assert [d.decision for d in output.client_decisions] == ["valid decision"]
    assert [r.risk for r in output.scope_risks] == ["valid risk"]
    assert [a.assumption for a in output.unconfirmed_assumptions] == ["valid assumption"]
    assert collect_output_source_ids(output) == {"m1", "m2"}


def test_source_refs_for_message_ids_preserves_order_and_dedupes():
    refs = {
        "m1": {"type": "slack_message", "message_id": "m1"},
        "m2": {"type": "slack_message", "message_id": "m2"},
    }

    result = source_refs_for_message_ids(["m2", "m1", "m2", "bad"], refs)

    assert result == [refs["m2"], refs["m1"]]


def test_slack_and_huddle_source_refs_have_stable_shape():
    message = SimpleNamespace(
        id="m1",
        slack_channel_id="C123",
        slack_message_ts="123.456",
        permalink="https://slack.example/msg",
    )

    assert slack_message_source_ref(message) == {
        "type": "slack_message",
        "message_id": "m1",
        "channel_id": "C123",
        "slack_message_ts": "123.456",
        "permalink": "https://slack.example/msg",
    }
    assert huddle_run_source_ref("h1") == {
        "type": "huddle_run",
        "huddle_run_id": "h1",
    }
