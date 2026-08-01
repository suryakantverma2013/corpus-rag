"""The one token-counting rule, shared by ingestion and the chat budget (T-310).

Promoted out of `app.ingestion.chunker`, where it lived alone while `document_chunks.
token_count` was its only consumer. T-310 gives it a second and a **normative** one: the
NFR-CAP-01 budget FR-STA-04 enforces and FR-ANL-03 displays. Two subsystems applying the
same rule from two definitions is how the number a user is shown stops matching the number
that blocked them, so there is exactly one here — the `split_answer_segments` argument
(R-48(4)) applied to arithmetic.

**It is an estimate, and that is a recorded deviation** — FR-ANL-03 says "production shall
use real token usage", which R-30 has already reinterpreted once: the API reports a single
`prompt_tokens` covering the system prompt, the retrieved chunks, the history and the query,
while R-30 counts only the last two, so **no API-reported number can produce this meter**.
What FR-ANL-03 was contrasting against is the prototype's fixed `+220`/`+640` simulation,
and counting real message text satisfies that intent.

Three things make the imprecision safe rather than merely tolerable:

* R-30(2) makes 10.4K a **product** budget, not a model limit — the OpenAI models carry far
  larger windows — so an error here can never turn into a provider 400. It moves where a
  chat stops, not whether it works.
* The rule has to run **in the browser too**: FR-STA-04 blocks *before* submission, and the
  GUI cannot ask the server to count every keystroke. `ceil(len/4)` is one line of
  TypeScript; `tiktoken` is a WASM bundle, and a mismatch between the two sides would show
  as the composer refusing a message the server would have accepted.
* R-35(7) already refused `tiktoken` for the worker because it fetches its BPE vocabulary
  over the network on first use — a cold-start hazard that is worse, not better, on a
  request path.

Swapping in an exact tokenizer later is a change to this module and to the GUI's copy of the
rule, and nothing else — no stored value depends on it (`token_count` is not an FR-ING-03
fingerprint input, and the budget is derived per request, never persisted).
"""

from __future__ import annotations

import math

__all__ = ["CHARS_PER_TOKEN", "estimate_tokens"]

#: Characters per token. English prose against the GPT tokenizers sits near four; the error
#: is a few percent on chat text and larger on code or dense punctuation. # TBD(§8.4)
CHARS_PER_TOKEN = 4.0


def estimate_tokens(text: str) -> int:
    """Approximate the token count of `text`.

    Rounds **up**, and is applied **per message** rather than to a concatenated transcript:
    `sum(ceil(len_i / 4))` and `ceil(sum(len_i) / 4)` differ, and the per-message form is the
    one the GUI can reproduce for a single composer box without knowing the whole chat.

    T-205 must **not** size embedding batches from this — an underestimate becomes a 400 from
    the API. Batch by characters or by request count and let the API be the authority.
    """
    return math.ceil(len(text) / CHARS_PER_TOKEN)
