"""`uv run python -m evals.run` — the offline golden-set evaluation harness (T-312, R-52).

Reports to stdout and to a JSON artifact. Writes **nothing** to `messages.evaluation`: the two
metrics this exists for are corpus-level, and FR-ANL-04's card is chat-level (R-52).

    uv run python -m evals.run                    # the whole golden set
    uv run python -m evals.run --limit 3          # smoke run, one item per band
    uv run python -m evals.run --band answerable  # one band
    uv run python -m evals.run --out report.json

Costs real money and real time: per item a router call, rerank batches, one generation and up
to four judge metrics. Seeded corpus and every row it creates are removed on the way out, on
every path including a keyboard interrupt.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.logging_config import configure_logging
from app.rag.evaluation import build_evaluator, structural_coverage
from evals.corpus import Question, load_golden_set, select
from evals.pipeline import (
    SeededCorpus,
    build_eval_graph,
    make_context,
    recall_at_k,
    run_question,
    seed,
    teardown,
)
from evals.report import ItemScore, aggregate, render_text

_RESULTS_DIR = Path(__file__).parent / "results"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="evals.run", description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="run at most N items")
    parser.add_argument(
        "--band",
        action="append",
        dest="bands",
        choices=["answerable", "unanswerable", "near_miss"],
        help="restrict to a band (repeatable)",
    )
    parser.add_argument("--corpus", type=Path, default=None, help="path to a golden-set JSON")
    parser.add_argument("--out", type=Path, default=None, help="where to write the JSON artifact")
    parser.add_argument(
        "--no-judge",
        action="store_true",
        help="run the pipeline and the free metrics only — no DeepEval calls, no cost",
    )
    return parser.parse_args(argv)


async def _score_item(
    question: Question,
    result: object,
    *,
    evaluator: object,
    corpus: SeededCorpus,
    judge: bool,
) -> ItemScore:
    """Judge one completed turn. The pipeline is done by the time this runs."""
    from evals.pipeline import TurnResult, cited_passage_ids

    assert isinstance(result, TurnResult)
    if not result.ok:
        return ItemScore(
            question_id=question.id,
            band=question.band,
            question=question.question,
            error=result.error or "no answer produced",
        )

    # Free and deterministic, so they are computed whatever `--no-judge` says: the structural
    # coverage is the gate's own number (R-49(b) recomputes rather than persists it) and
    # recall@k is the control on the paid metrics.
    coverage = structural_coverage(result.answer, [str(c) for c in result.grounding_chunk_ids])
    scores = ItemScore(
        question_id=question.id,
        band=question.band,
        question=question.question,
        answer=result.answer,
        groundedness=coverage.score,
        recall_at_k=recall_at_k(result, question),
        gate_verdict=result.gate_verdict,
        query_class=result.query_class,
        supporting_passage_ids=question.supporting_passage_ids,
        grounding_passage_ids=result.grounding_passage_ids,
        cited_passage_ids=cited_passage_ids(result, corpus),
    )
    if not judge:
        return scores

    free = await evaluator.score(  # type: ignore[attr-defined]
        question=question.question, answer=result.answer, context=list(result.context)
    )
    reference = None
    if question.reference_scored:
        # Only the answerable band: Contextual Recall measures whether the context contains
        # what the ideal answer needed, and an ideal answer of "the documents do not say"
        # needs nothing from the context (see `evals.corpus`).
        reference = await evaluator.score_reference_based(  # type: ignore[attr-defined]
            question=question.question,
            answer=result.answer,
            expected=question.expected_output,
            context=list(result.context),
        )

    from dataclasses import replace

    return replace(
        scores,
        relevancy=free.relevancy,
        faithfulness=free.faithfulness,
        ctx_precision=reference.ctx_precision if reference else None,
        ctx_recall=reference.ctx_recall if reference else None,
    )


async def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    configure_logging()
    settings = get_settings()

    if not settings.openai.api_key and not args.no_judge:
        print("OPENAI_API_KEY is empty — set it, or pass --no-judge.", file=sys.stderr)
        return 2

    golden = load_golden_set(args.corpus)
    questions = select(golden, bands=args.bands, limit=args.limit)
    print(
        f"golden set v{golden.version}: {len(golden.passages)} passages, "
        f"{len(questions)} of {len(golden.questions)} questions",
        flush=True,
    )

    from app.services.embeddings import OpenAIEmbeddingClient
    from app.services.llm import OpenAIChatClient

    # Constructed directly rather than through the process factories: T-309 recorded that a
    # live path routed through the suite's own factory silently gets the fake backend.
    embeddings = OpenAIEmbeddingClient(settings)
    chat = OpenAIChatClient(settings)
    # Never escalates (R-53): re-judging only the low scores is a biased estimator, which is
    # fine for a per-message chip and disqualifying for the instrument that measures quality
    # release over release.
    evaluator = build_evaluator(chat, settings, escalate=False)

    engine = create_async_engine(settings.database.url, pool_size=10, max_overflow=10)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    graph = build_eval_graph()

    started = time.perf_counter()
    corpus = await seed(sessionmaker, embeddings, golden)
    print(
        f"seeded {len(corpus.chunk_by_passage)} chunks under owner {corpus.owner_id}\n",
        flush=True,
    )

    items: list[ItemScore] = []
    try:
        for index, question in enumerate(questions, start=1):
            item_started = time.perf_counter()
            context = make_context(
                corpus,
                sessionmaker=sessionmaker,
                embeddings=embeddings,
                chat=chat,
                conversation_id=uuid.uuid4(),
            )
            result = await run_question(graph, question=question, corpus=corpus, context=context)
            score = await _score_item(
                question,
                result,
                evaluator=evaluator,
                corpus=corpus,
                judge=not args.no_judge,
            )
            items.append(score)
            print(
                f"[{index:>3}/{len(questions)}] {question.id:<7} {question.band:<13}"
                f" {int((time.perf_counter() - item_started) * 1000):>6} ms"
                f"  gate={score.gate_verdict or '—'}"
                f"  faith={'—' if score.faithfulness is None else f'{score.faithfulness:.2f}'}",
                flush=True,
            )
    finally:
        await teardown(sessionmaker, corpus)
        await engine.dispose()

    report = aggregate(
        items,
        meta={
            "corpus": str(golden.source),
            "corpus_version": golden.version,
            "passages": len(golden.passages),
            "chat_model": settings.openai.chat_model,
            "router_model": settings.openai.router_model,
            "rerank_model": settings.openai.rerank_model,
            "judge_model": settings.openai.judge_model,
            "rerank_top_k": settings.rerank.top_k,
            "merged_top_k": settings.retrieval.merged_top_k,
            "judged": not args.no_judge,
            "elapsed_s": round(time.perf_counter() - started, 1),
        },
    )
    print()
    print(render_text(report))

    out = args.out
    if out is None:
        _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        out = _RESULTS_DIR / f"golden-set-{time.strftime('%Y%m%dT%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report.as_dict(), indent=2, default=str), encoding="utf-8")
    print(f"artifact: {out}")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        # psycopg's async driver cannot run on the default ProactorEventLoop (R-42(12)). This
        # harness uses asyncpg only, but the loop policy is set the same way every other
        # entrypoint in this repo sets it, so a future checkpointer read here does not surprise.
        from app.runtime import selector_loop

        loop = selector_loop()
        try:
            raise SystemExit(loop.run_until_complete(main()))
        finally:
            loop.close()
    else:
        raise SystemExit(asyncio.run(main()))
