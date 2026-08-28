# Glossary

Words this documentation uses in a particular way. Where a term has an everyday meaning too, the
entry says what Corpus means by it and, where that differs, what it does **not**.

---

### Abstention

A turn that ends without an answer, because what was retrieved does not support one. Corpus says so
in plain words rather than guessing.

An abstention is **the system working**. It is also diagnostic: three abstentions against a corpus
that should have answered are how a corrupt text layer was found in a PDF that had ingested
perfectly. That is the reason an automatic fall-back to the model's own knowledge is deliberately
*not* the default — it would have answered all three and hidden the fault.

See also [ungrounded answer](#ungrounded-answer), [groundedness gate](#groundedness-gate).

### Chunk

A contiguous run of a document's text, sized for retrieval, carrying its own [locator](#locator) and
its own vector. Chunks are cut **within** a parsed block and never across one, so a citation always
points somewhere a reader can find.

A chunk is not a page, a paragraph or a sentence. It is whatever the chunker produced under the
sizing rules in force at the time — which is why chunks carry [provenance](#provenance).

### Citation

The pairing of a claim in an answer with the passage that supports it. In the interface it is the
chip after a sentence; underneath it is a document, a [locator](#locator), the quoted passage and a
retrieval score.

The model marks *where* it is citing; it never chooses *what* the mark resolves to. A mark naming a
passage that was not supplied is dropped rather than resolved — which is why an answer cannot cite
something it was not given.

### Embedding

The vector representation of a chunk, used for similarity search. Produced by the embedding model,
which is one of the three inputs to a chunk's [provenance](#provenance) — change it and every
existing chunk is stale until [re-embedded](#re-embed).

### Grounding

The property that every claim in an answer is supported by a passage that was actually retrieved.
Corpus's central commitment: **ground or abstain**.

Grounding is enforced, not requested. The model is given passages and asked to cite them; the
[groundedness gate](#groundedness-gate) then checks the answer before anyone sees it.

### Groundedness gate

The check between generating an answer and serving it. It measures how much of the answer's
substantive content carries a citation, and refuses to serve an answer that falls below a threshold
— turning it into an [abstention](#abstention).

It is structural, not semantic: it verifies **that the model cited**, not that the passage genuinely
supports the sentence. The second question is the [judge](#judge)'s, after the fact. The gate does
no database work and no model call, which is why it costs microseconds.

### Judge

A model that scores an answer after it has been served — relevancy and faithfulness. Its scores are
the chips in the interface and the session averages in the panel.

Judge scores are **indicative, not exact**. Two frontier models disagree by more than the second
decimal on a fifth of scores, and both will mark a verbatim quotation of its own source at 0.5. Use
them to notice a weak answer; do not treat them as a measurement.

### Knowledge-base scope

Which documents a question searches. Two scopes:

- **Global** — searched in every one of that user's conversations.
- **Chat** — attached to a single conversation and searched only there.

Scope is per user. There is no shared corpus: one person's documents are never searched for another,
and the predicate that enforces it is evaluated inside the retrieval query on every turn, from the
live request's identity.

### Locator

Where in a document a [chunk](#chunk) came from, in the terms that document actually has: a **page**
for PDFs, a **section heading chain** for Word and Markdown, a **row range** for spreadsheets.

Deliberately not normalised to page numbers. Word and Markdown files have no pagination, so a page
number would be a citation the reader cannot check.

### Provenance

The three things stamped on every chunk that determine whether it is current: the **embedding
model**, the **chunking version** (which folds in the chunk-sizing settings), and the
**preprocessing version** (which changes when parsing itself changes).

Provenance is read, never recomputed — which is what lets the staleness report name *which* input
drifted rather than merely reporting that something did.

### Re-embed

Rebuilding a document's chunks and vectors under the current pipeline, because its
[provenance](#provenance) no longer matches the configuration.

A rebuild copies the stored original forward to a new version and re-embeds the whole document; the
version already indexed keeps answering until the new one is ready. It is safe against a live
deployment and it is not free — it spends an embedding call per chunk.

### Retrieval

Finding the passages a question should be answered from. Corpus searches two ways at once — by
meaning (vectors) and by words (full-text) — merges the results, and then re-reads the best
candidates with a model to order them.

### Ungrounded answer

An answer generated **without** retrieved passages, from the model's own training. Offered only
after an [abstention](#abstention), only if the deployment enabled it, and only when a user asks for
it explicitly.

It is marked, carries no citations, is never scored, and is withheld from the history later answers
are generated from — so an invented claim cannot become the grounding for a subsequent one. It never
replaces the abstention; both stay in the transcript.

---

## Words used elsewhere that mean something else here

| Word | Not this | This |
|---|---|---|
| **Index** | A search index you administer | Corpus indexes automatically on upload; there is nothing to rebuild by hand except a [re-embed](#re-embed) |
| **Training** | Corpus learning from your documents | Nothing is trained. Documents are retrieved from, never learned |
| **Conversation memory** | The model remembering you | The transcript is replayed into each turn; nothing persists in the model |
| **Score** | A measurement | A model's opinion — see [judge](#judge) |
