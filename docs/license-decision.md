# License decision: AGPL-3.0 vs Apache-2.0

**Status: decided 2026-11-07: Apache-2.0** (LICENSE, NOTICE). No CLA: under Apache-2.0
§5, contributions come in under the same license, which already allows a closed `ee/` area
later. A CLA would only matter for relicensing under different terms; revisit it then.
This is an engineering summary, not legal advice. Have a lawyer read the final choice if
a company will depend on it.

## The short answer

**Recommendation: Apache-2.0 for the core, a closed or commercial `ee/` area later for
enterprise features, and a contributor license agreement (CLA) from the first outside PR.**
Switch to AGPL-3.0 only if the interviews show the main risk is someone *hosting* callsight
for others, rather than nobody adopting it.

## What each license means for callsight

| | Apache-2.0 | AGPL-3.0 |
|---|---|---|
| Can a company run it internally, unmodified? | Yes, no obligations beyond keeping notices | Yes, no obligations. Many corporate legal policies ban AGPL anyway, without reading it |
| Can a company modify it and use it internally? | Yes, nothing to publish | Yes, but if users interact with the modified version over a network, it must offer them the source (§13). Legal teams read "users" broadly |
| Can a SaaS vendor host it and sell it? | Yes, closed changes allowed | Only if they publish their modified source. This is AGPL's main purpose |
| Can it be embedded in someone's product? | Yes | Only if their product goes under AGPL, so in practice no |
| Patent grant | Explicit | Explicit (GPLv3 §11) |
| Can you sell a commercial license on the side? | Not needed: anyone can already use it commercially | Yes, but **only with a CLA**, because you need the rights to every contributor's code |
| Can you add closed enterprise features? | Yes, the usual open-core setup | Yes, if you hold all copyrights (CLA again) |
| Compatible with reusing gtm-superintelligence (Apache-2.0) content | Yes | Yes. Apache-2.0 code can go into an AGPLv3 project, not the other way round |

## How this maps to callsight's situation

1. **The likely buyers are the people most likely to block AGPL.** Interview group B
   (regulated, data-sensitive teams) means companies with legal review. Many of them refuse
   AGPL outright, and self-hosting is the whole reason they'd pick callsight. That's the
   strongest argument for Apache.
2. **Builders want to embed it.** Group C (teams who built their own) would use callsight
   as a library or inside internal tools. AGPL makes that awkward; Apache doesn't.
3. **Obscurity is a bigger risk than being copied.** With 0 users, the realistic threat is
   that nobody adopts it, not that a vendor resells it. AGPL protects against the second
   threat and makes the first one more likely.
4. **The competitors are permissive.** gtm-superintelligence and Vexa are Apache-2.0;
   CallLens and Playcall are MIT. An AGPL callsight is the odd one out when someone compares
   options. The one AGPL project (Polylog) isn't actually under AGPL yet.
5. **AGPL's protection matters most if a SaaS vendor could profit by hosting callsight.**
   An existing CI vendor has its own product and little reason to host yours. A new
   cheap hosted CI startup might, and that's the case to watch for in interviews.

## Where the open-core line could go (if you go commercial)

Use the roadmap's week 6 list. These are features individuals don't need and companies pay for:

| Open (core) | Commercial (`ee/`) |
|---|---|
| Parsing, ASR providers, all analyzers, LLM providers | SSO / SAML, SCIM |
| Persistence, read API, CLI | Multi-tenant separation, per-team permissions |
| Single-user web UI, connectors interface | Audit log, data-retention policies |
| Eval harness and published numbers | Managed connectors for paid platforms, priority support |

Rule of thumb: if a single team running callsight on one server needs it, it goes in
the open core. If only an organization with compliance or admin needs would pay for it,
it can be commercial.

## Decisions that are hard to reverse

- **Without a CLA, the license is effectively permanent** once outside contributions
  arrive, because relicensing then needs every contributor's consent. Either use a CLA
  (CLA Assistant on GitHub is the common route) or don't merge outside PRs until you've decided.
- **Moving from Apache to AGPL later only affects future versions.** Versions already
  released under Apache stay usable under Apache forever (Grafana did this in 2021). The
  move is possible but you can't take back what was already released.
- **Dependencies:** FastAPI (MIT), Uvicorn and Starlette (BSD-3), Pydantic (MIT) and
  psycopg (LGPL-3.0, used unmodified as a separate package) all work under either license.
  Recheck when adding ASR models: model *weights* often have their own terms (some
  pyannote models need users to accept terms on Hugging Face; Llama models use a community
  license, not OSI). Users download those themselves; don't bundle them.

## What to ask in the interviews

Add one question at the close of the interview script for groups B and C:
"Does your company have rules about which open-source licenses you can use? Has AGPL ever come up?"
If two or more say AGPL would block them, that settles it.

## To apply the decision

- Add `LICENSE` with the full text and `license = "Apache-2.0"` (or `"AGPL-3.0-only"`) in
  `services/api/pyproject.toml`.
- Replace the "Not chosen yet" section in `README.md`.
- If reusing gtm-superintelligence files, keep their copyright headers and add a `NOTICE` file.
- Remove the license line from the roadmap's open questions.
