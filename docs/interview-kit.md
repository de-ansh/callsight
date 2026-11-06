# Interview kit (week 2 market check)

Goal: decide, from 5–10 conversations, whether to build past week 2. Ask about what people
have already done and paid for, not whether they'd like callsight. People are polite about
hypotheticals and honest about their past.

## The bet being tested

Some sales teams want call analysis but **won't or can't** send recordings to a SaaS vendor,
or **won't pay** Gong-level prices (~$1,300–1,600/user/yr plus a platform fee), and Avoma or
Fireflies doesn't solve it for them. If that pain isn't real, self-hosting is a feature
nobody buys.

## Who to talk to

Aim for at least 2 from each group. One conversation per company.

| Group | Why them | Where to find them |
|---|---|---|
| A. Sales leaders / RevOps at 5–50 rep teams | Felt Gong's price, may have dropped it or never bought | LinkedIn, RevGenius, Pavilion, r/salesops |
| B. Teams in regulated or data-sensitive fields (fintech, health, legal, EU) | Data control is a hard requirement, not a preference | LinkedIn search by industry + "sales enablement" / "RevOps" |
| C. Engineers or RevOps people who built call analysis in-house | Already paying the build cost; would they adopt an open core? | GitHub stargazers of gtm-superintelligence / Vexa, HN, r/LocalLLaMA |

## Outreach messages

**Cold (LinkedIn / email), under 80 words:**

> Hi {name}, I'm building an open-source tool that analyzes sales calls and runs on your
> own servers. Before I build more, I'm trying to learn how teams like {company} handle
> call review today: what you use, what it costs, what's missing. Not selling anything:
> there's nothing to buy. Would you give me 15 minutes this week or next? Happy to share
> what I learn across the conversations.

**Group C variant:** replace the second sentence with "I saw you {starred X / wrote about
Y} and I'm curious why you built your own call analysis instead of buying one."

Send 3× more messages than the interviews you want. Expect about a 20–30% reply rate.

## Script (15 minutes)

Don't mention callsight until the last 3 minutes. Take notes in the template below.

1. **Context (2 min).** What's your role? How many reps? How many calls a week?
2. **Current state (4 min).**
   - How do you review sales calls today? Walk me through the last time you did it.
   - What tool, if any? What does it cost you per year, roughly?
   - If no tool: did you evaluate one? What stopped you?
3. **Pain (5 min).** Follow whichever thread is warm:
   - What's the most annoying part of how you do it now?
   - Has security, legal or a customer ever pushed back on recording or on where
     recordings are stored? What happened?
   - Has anyone on your team built scripts or prompts to analyze calls themselves? Why?
   - What would you have to stop paying for, or stop doing, if this were solved?
4. **Demo + reaction (3 min).** Run the demo below. Ask: "What's the first thing you'd
   want this to do that it doesn't?" Don't ask "would you use this".
5. **Close (1 min).** Who else should I talk to? Can I come back to you in a month
   with something to try?
   Groups B and C only: "Does your company have rules about which open-source licenses
   you can use? Has AGPL ever come up?" (feeds [license-decision.md](license-decision.md))

**Avoid:** "Would you use…", "Would you pay…", explaining the vision, arguing with their answers.

## Demo (under 2 minutes)

```bash
cd services/api
PYTHONPATH=src python -m callsight analyze ../../examples/sample_call.txt --competitor AcmeCall
```

What to point at: every flag quotes the exact lines behind it (no next step, the rep talked
74% of the time, a budget objection, AcmeCall mentioned). It runs locally with no LLM
needed and nothing leaves the machine. If they want to see it as an API, run
`docker compose up --build db api` and show `GET /v1/calls`.

## Notes template (one per interview)

| Field | Answer |
|---|---|
| Name / company / group (A/B/C) | |
| Team size, calls/week | |
| Current tool and yearly cost | |
| Evaluated and rejected? Why? | |
| Data-control pushback that actually happened (yes/no + story) | |
| Built something themselves? | |
| Strongest pain, in their words | |
| Reaction to demo: first thing they asked for | |
| Committed to a next step? (intro, trial, follow-up) | |

## Deciding (after 5–10 interviews)

Count only things that **happened**, not things people said they'd like.

- **Continue to week 3** if at least 3 interviews show real pain callsight addresses: a deal or tool
  blocked by data concerns, Gong dropped on price with nothing good replacing it, or an
  in-house build they'd swap out. **And** at least 2 people commit to a next step (a trial or an intro).
- **Change direction** if the pain is real but different, for example they want this inside
  their CRM, or for support calls rather than sales, or in a language other than English.
  Update the roadmap before building.
- **Stop or shrink** if most people are fine with Avoma/Fireflies prices and nobody has had
  a data-control problem. In that case callsight becomes a library or side project, not a product.

Record the counts and the decision in `docs/roadmap.md` under Week 2.
