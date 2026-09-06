# Deck outline

Twelve slides, about eight minutes, then the live demo.

## 1. Title

**Shift Readiness Agent.** For the line manager who needs to know at ten to nine whether the floor is staffed at nine.

## 2. The problem, in one screenshot

The dataset's own trip log. A billing agent, dropped at 09:20 for a 09:00 shift. `delay_reason: NODELAY`.

By the transport system's measure, nothing went wrong. The floor was short for twenty minutes.

Speaker note: this is the whole product in one row. Transport measures the trip. The line manager lives with the floor. Nobody translates between them.

## 3. Who we built for

The line manager. Runs a queue on a 24/7 support floor. Consumes the transport service; does not manage it.

What they care about, in order: is my queue staffed at nine, what does it cost me if not, what do I do about it, and what do I tell my director.

What they do not care about: vendor names, cost per kilometre, GPS traces.

## 4. What the data supports

Three months, five sites, 1.6 million rider legs. What we found before writing a line of product:

- 51% of riders are on one fixed shift all month. A team is derivable, not invented.
- Median planned buffer between drop and shift start: **5 minutes**. Journey noise: **13 minutes**. Half of arrivals are late by construction.
- Clearwater runs **46%** late on the 09:00 shift. The best peer site runs under 1%.
- 70% of late-for-shift arrivals are stamped `NODELAY`.

Speaker note: the last two are the pitch. The site is the worst in the network and the operator's own metric cannot see it.

## 5. The loop

Sense, reason, act. One diagram.

Sense: a clock over trip events. Reason: nine-state rider machine, arrival projection, queue headcount, Erlang C service level, day rollup, cause, benchmarks, cover search, hold-over cost. Act: the agent writes the alert and the drafts; the manager clicks; the system records and charges.

**The model is only in the last box, and only when the situation changes.**

## 6. What the manager sees

The board at 08:55. Screenshot.

Clock, headcount, two queue cards with service level, every rider's position, and on the right: the alert as a note from a colleague, three options with the recommended one in green, and what has already been sent.

## 7. The alert, read aloud

> Billing Support is four short until 09:30. Service is 15% now, and the day's target still holds. Move Agent 35, Agent 27, Agent 31, and Agent 29 to Billing Support.

And beside it, the option nobody chose, priced: four night agents held 31 minutes past shift end, four miss the 09:15 cab home, overtime about 1301.

Every number computed. Every name real. The model chose the order and wrote the sentences.

## 8. Why the service level matters

Losing 4 of 12 agents on a properly loaded queue: service level 92% to **15%**, not to two-thirds. Staffing is not linear and a headcount cannot convey that.

And the honest half: the interval collapses but **the day holds at 92%**. This is a floor problem, not a contract problem. The agent escalates to operations only when the day is at risk. Telling a manager when not to panic is worth as much as telling them when to.

## 9. Deterministic versus model

| Computed | Written |
|---|---|
| rider state, ETA, headcount | which two of five facts to lead with |
| service level, day rollup | the five sentences |
| cause, triggers, options | the message to the shift lead |
| who can cover, what it costs | the answer to a question |

Why: the numbers get forwarded to a director. They must be reproducible and identical between two options being compared. The model is for judgement and language, not arithmetic.

## 10. Cost at scale, metered

- A tick costs **15 ms** and no model call.
- A 150-tick morning across two queues produces **11 narratives**, not 300.
- One narrative costs about **5,000 tokens** and 9 seconds. The first working version cost 22,600. We show the meter, not the claim.
- Cache by situation: the same morning next Tuesday costs nothing.

## 11. What is synthetic

Riders, trips, times, vendors, the 24-person team, the cover pool: real.

Queue assignment, handle time, forecast, the night shift, the overtime rate: synthetic, labelled in the code and the roster.

Speaker note: say this before anyone asks. The night shift is the one invention that changes the product, because positional handover is what makes a late arrival cost somebody their morning.

## 12. Deployability, and what we cut

Postgres for everything, including agent sessions. Every table and endpoint keyed by tenant and site. ADK to Cloud Run in one command. The model is a one-line swap. Replace the replay with a live event consumer and nothing downstream changes.

Cut, deliberately: GPS, vendor contact, HR overtime rules, authentication, the other two personas.

## Then: the demo

Five stops on the slider, three minutes, Live off so every stop is instant. Rehearse once beforehand with Live on so the model has written each alert and the run is recorded.

1. 07:30, nothing to say and it says nothing.
2. 08:05, one vendor's cab has not left; the alert names the vendor and withholds cover it cannot deliver.
3. 08:30, two confirmed absences; the day is lost and operations is flagged an hour early.
4. 08:55, click cover, show the draft, point at what hold-over would have cost.
5. 09:30, billing resolves itself, tech does not; ask the chat what the morning cost.
