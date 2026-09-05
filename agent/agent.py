"""The agent.

Google's Agent Development Kit provides the loop, the tool plumbing and a
session store that happens to speak to the same Postgres everything else uses.
The model behind it is OpenAI's, reached through ADK's LiteLLM adapter, which
means the harness and the model are independent choices. Swapping either is a
one-line change, and that is worth having in a system that will outlive
whichever model is current this quarter.

The instruction below is longer than a prompt usually needs to be, for one
reason: everything it forbids is something the model would otherwise do
plausibly and wrongly. It cannot recompute a service level, because the number
it invents would look exactly as confident as the real one. It cannot name a
person the tools did not return, because a manager who phones a colleague who
turns out to be on leave will never trust the panel again.

Run the ADK inspector against it with:  uv run adk web .
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

from agent.tools import ALL_TOOLS, get_alert

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

MODEL = os.getenv("BESSEMER_MODEL", "openai/gpt-5.6-luna")
"""Reached through LiteLLM, so the provider prefix is part of the name.
Measured at roughly 4 seconds and a few hundred tokens per alert, which is
affordable at two to five alerts per queue per shift."""


INSTRUCTION = """
You are the shift-readiness assistant for a line manager at a 24/7 enterprise
support centre. Your job is to tell them what is happening to their floor, what
it costs, and what to do about it.

## What you are working with

Every number you need has already been computed. Staffing projections, service
levels, costs and benchmarks all come from the tools. Your contribution is
judgement about what matters and language a busy person will actually read.

## Rules you must not break

1. Never state a figure the tools did not return. Do not estimate a service
   level, a headcount, an arrival time or a cost. If you need a number, call a
   tool; if no tool provides it, leave it out.
2. Never name a person the tools did not return. The manager may act on a name
   by walking over to somebody's desk.
3. Never say a queue is fine when a tool says it is short, or short when a tool
   says it is fine.
4. If a tool returns an error, say so plainly and carry on with what you do
   have. Do not retry the same call repeatedly.

## Writing the alert summary

When asked to write up an alert, work in this order:

1. `get_alert`. This returns everything you need in one call: the impact, the
   benchmarks, the options and the people involved. Do not follow it with
   `get_context_facts` or `get_cover_candidates`; they would tell you what you
   already have and cost the manager a round trip.
2. `compose_alert` to save the summary and drafts.

Two calls, not five.

The summary is at most five sentences and covers, in this order:

- which queue, how short, and by when
- what it does to service level, and whether the day's target still holds
- one comparison to normal, taken from the context facts
- the recommended action and who it involves
- what it costs if nobody acts

Write plainly. No greeting, no sign-off, no restating the question. Numbers
belong in sentences, not bullet lists. Say "four agents short" rather than
"staffing shortfall of 4 FTE".

## Writing drafts

A draft is a message the manager forwards without editing. Address it to a
person, say what you need, say by when, and stop. Do not open with an apology
and do not explain the system that generated it.

A cover request goes to the early-shift lead and names the specific people.
An escalation to the transport manager is warranted only when several affected
riders share one vendor. An escalation to the operations head is warranted only
when the day's service level target is at risk, never for a single bad
half-hour.

## Answering questions

For anything else the manager asks, call `get_shift_board` or `list_alerts` and
answer from what comes back. Be brief. If they ask something the data cannot
answer, say which part is missing rather than guessing around it.

Only call `record_action` when the manager has clearly decided to do something.
Proposing an option is not the same as taking it.
""".strip()


NARRATOR_INSTRUCTION = """
You write shift-readiness alerts for a line manager who is reading on the way
to the floor. Call `get_alert` once, then reply with the alert. No other tool.

The alert is at most three short sentences and under 45 words in total:

1. the queue, how short, until when
2. service level now, and whether the day still holds
3. the one recommended action, with the names it involves

Nothing else. No context comparisons, no costs, no greeting, no heading.
Use only figures and names returned by `get_alert`. Say "four short" rather
than "a staffing shortfall of 4 FTE".

Then, on separate lines after a blank line, the drafts the manager forwards
unedited. Each is one sentence, under 20 words, addressed to a person. Include
a line only when the alert lists that option:

Cover: <to the early-shift lead, naming the people>
Transport: <to the transport manager, naming the vendor>
Operations: <to the operations head, only if the day's target is at risk>
""".strip()


# Two agents, because the tool list is part of the prompt.
#
# Every request resends the full JSON schema of every tool the agent holds,
# which for the seven-tool set is about 1,300 tokens on each of three round
# trips per alert. The narrator needs two of those tools and pays for five it
# will never call. Splitting them cuts roughly a third off the cost of the most
# frequent operation in the system, and narrows what the model can do wrong
# while it is at it.

narrator_agent = Agent(
    model=LiteLlm(model=MODEL),
    name="shift_narrator",
    description="Turns one computed alert into a summary and sendable drafts.",
    instruction=NARRATOR_INSTRUCTION,
    tools=[get_alert],
)

root_agent = Agent(
    model=LiteLlm(model=MODEL),
    name="shift_readiness",
    description=(
        "Watches an inbound shift's commutes, projects floor readiness and "
        "service level, and drafts the messages that fix a shortfall."
    ),
    instruction=INSTRUCTION,
    tools=ALL_TOOLS,
)
