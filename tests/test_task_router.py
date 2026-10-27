"""Tests for the task router (pure logic, no network)."""

from jack.adapters.task_router import (
    build_context_envelope,
    build_task_prompt,
    extract_task,
    heuristic_is_task,
)


class TestExtractTask:
    def test_no_tag_returns_none_and_original(self):
        reply = "Sure thing, tell me more."
        task, spoken = extract_task(reply)
        assert task is None
        assert spoken == reply

    def test_tag_only(self):
        task, spoken = extract_task("[TASK: what time is it on the Mac]")
        assert task == "what time is it on the Mac"
        assert spoken == ""

    def test_tag_with_prefix(self):
        reply = "Let me check that for you. [TASK: read the battery level]"
        task, spoken = extract_task(reply)
        assert task == "read the battery level"
        assert spoken == "Let me check that for you."

    def test_tag_with_suffix(self):
        reply = "[TASK: check calendar] One moment."
        task, spoken = extract_task(reply)
        assert task == "check calendar"
        assert spoken == "One moment."

    def test_tag_with_prose_both_sides(self):
        reply = "On it. [TASK: send email to mom] Give me a sec."
        task, spoken = extract_task(reply)
        assert task == "send email to mom"
        assert spoken == "On it. Give me a sec."

    def test_empty_tag_is_none(self):
        task, _ = extract_task("[TASK: ]")
        assert task is None

    def test_multiline_task_payload(self):
        task, _ = extract_task("Sure.\n[TASK: run date and\nreport the output]")
        assert task is not None
        assert "run date and" in task and "report the output" in task


class TestHeuristic:
    def test_time_question(self):
        assert heuristic_is_task("Hey, what time is it on the server?")

    def test_date_question(self):
        assert heuristic_is_task("what's the date today?")

    def test_imperative_email(self):
        assert heuristic_is_task("send an email to Katrin about dinner")

    def test_reminder(self):
        assert heuristic_is_task("remind me to flip the laundry in 20 minutes")

    def test_plain_chat_is_not_task(self):
        assert not heuristic_is_task("yeah the drive was horrible, traffic everywhere")
        assert not heuristic_is_task("haha exactly!")
        assert not heuristic_is_task("what do you think about that?")

    def test_empty(self):
        assert not heuristic_is_task("")
        assert not heuristic_is_task(None or "")


class TestEnvelope:
    def test_last_n_turns_and_roles(self):
        history = [
            {"role": "system", "content": "You are the agent."},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hey! [TASK: old thing]"},
            {"role": "user", "content": "what time is it?"},
        ]
        env = build_context_envelope(history, turns=3)
        assert "Caller: hi" in env
        assert "Agent: hey!" in env
        assert "[TASK" not in env  # tags never leak into the envelope
        assert "You are the agent" not in env

    def test_empty_history(self):
        assert build_context_envelope([]) == ""


class TestPrompt:
    def test_prompt_contains_task_and_envelope(self):
        p = build_task_prompt("what time is it", "Caller: hi\nAgent: hello")
        assert "what time is it" in p
        assert "Caller: hi" in p
        assert "spoken" in p.lower()

    def test_prompt_empty_envelope(self):
        p = build_task_prompt("do the thing", "")
        assert "no prior turns" in p
