# Corpus OCR sidecar — Tesseract behind a minimal HTTP contract (T-217, R-88 §8.78).
#
# NFR-SEC-09 puts recognition OUTSIDE the worker process: this is the first ingestion step
# that *renders* attacker-supplied content rather than only parsing it, so the image
# decoders and the recognition engine are a new attack surface on the most hostile input
# Corpus accepts. Isolating them here means a memory-safety defect in the engine cannot
# reach the worker's database session or its object-storage credentials. Rasterisation
# stays in the worker (R-31 unchanged) — this container only ever sees a page raster.
#
# **Everything here is pinned, and that is the point.** R-88(1) makes byte-reproducible
# recognition a correctness requirement rather than a nicety: `plan_chunk_set` reuses a
# vector by set membership on `embedding_fingerprint`, so text that differs between two
# recognitions of the same page leaves `reused` permanently empty and turns every
# `/replace`, every FR-ING-04 retry and every T-608 rebuild into a silent full re-embed.
# Three things therefore move only on purpose:
#
#   1. the base image, by digest;
#   2. the engine and language packages, by exact Debian version;
#   3. the invocation flags, which live in `server.py` and are not settable over the wire.
#
# A Debian point release eventually drops a pinned version from the mirror and this build
# then fails loudly. That is the intended trade — a loud failure at build time beats the
# alternative, which is a corpus that quietly stops reusing its vectors.
FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a

# Debian 13 (trixie). `tesseract-ocr-eng` is the traineddata, versioned independently of
# the engine — 4.1.0 data on a 5.x LSTM engine is upstream's own pairing in this release.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr=5.5.0-1+b1 \
        tesseract-ocr-eng=1:4.1.0-2 \
    && rm -rf /var/lib/apt/lists/*

# OMP_THREAD_LIMIT=1 is a determinism control, not a performance one: OpenMP work-sharing
# is what would let the same page recognise differently on a busier host. LC_ALL/LANG pin
# the locale so nothing in the pipeline formats a number by the container's environment.
ENV OMP_THREAD_LIMIT=1 \
    LC_ALL=C \
    LANG=C \
    PYTHONUNBUFFERED=1 \
    TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

COPY server.py /opt/corpus-ocr/server.py

# Unprivileged: the whole point of the sidecar is containment, and a root process inside it
# gives an engine defect somewhere to go.
RUN useradd --system --no-create-home --uid 10001 ocr
USER ocr

EXPOSE 8884
ENTRYPOINT ["python3", "/opt/corpus-ocr/server.py"]
