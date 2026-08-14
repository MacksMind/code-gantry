"""Semantic search over the target repository.

`git grep` answers "where does this exact string appear". It cannot answer
"where is the CSV feed generated" when the method is called `feed` and the word
CSV never appears near it. Both kinds of question came up in the first long run
against a real codebase, and only one of them was answerable — which is part of
why the planner guessed at file names it could have found.

Talks to Qdrant and to an OpenAI-compatible embeddings endpoint directly. The
target project ships a `semantic-search` MCP server, and reading it showed a
wrapper over exactly two HTTP calls: embed the query, search the collection.
The orchestrator has no Node dependency, and adding `npx`, a subprocess and a
JSON-RPC client to reach two endpoints it can already reach would buy nothing
and add a moving part to an unattended loop.

Three properties of the index this relies on, each verified against the
project's own `bin/reindex` rather than assumed:

- It indexes `git ls-files` only, so the tracked-only boundary that keeps
  `.agent.env` out of planner context holds here too.
- A global post-commit hook reindexes incrementally, so it tracks HEAD.
- Every point carries `path`, `start_line` and `end_line`.

That last one is what makes this safe to expose. A hit is a **lead, not a
fact**: nearest-neighbour ranking always returns something, chunks can lag a
commit by the width of the hook, and a chunk is a fragment of a file rather than
the file. So the planner is told to confirm with `read_file` before asserting
anything, and the citation makes that a mechanical step rather than a judgement.

When the index is unreachable this returns a message rather than raising. A
planning step must not die because a side service is down; the planner still
has the plan, the layout, and the read tools.
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from code_gantry.apistatus import classify
from code_gantry.repotools import ToolCall


def _post(url: str, payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        method="POST",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


@dataclass
class SemanticSearchConfig:
    """Where the index lives and how to query it.

    Endpoints arrive through the environment rather than the config file. They
    carry a tailnet host, which is an identifiable infrastructure value and does
    not belong in a tracked file — the same reason `api_base_env` exists for the
    executor.
    """

    api_base: str
    qdrant_url: str
    embedding_model: str
    collection: str
    max_results: int = 8
    # A nearest neighbour is always *something*. Without a floor, a question the
    # repository has no answer to comes back with the least-bad chunk in it,
    # presented with the same confidence as a real hit.
    min_score: float = 0.4
    # Lines of each chunk shown alongside its citation. Enough to triage a
    # lead, far short of enough to act on one.
    snippet_lines: int = 2
    timeout_seconds: float = 30.0

    @classmethod
    def from_mapping(cls, data: dict | None) -> SemanticSearchConfig | None:
        """Build from config, or None when the project has no index.

        Absent means the tool is not offered at all. A project without a
        collection must not be handed a search that always fails.
        """
        if not data:
            return None
        api_base = os.environ[data["api_base_env"]]
        qdrant_url = os.environ[data["qdrant_url_env"]]
        return cls(
            api_base=api_base.rstrip("/"),
            qdrant_url=qdrant_url.rstrip("/"),
            embedding_model=data["embedding_model"],
            collection=data["collection"],
            max_results=int(data.get("max_results", 8)),
            snippet_lines=int(data.get("snippet_lines", 2)),
            min_score=float(data.get("min_score", 0.4)),
            timeout_seconds=float(data.get("timeout_seconds", 30.0)),
        )


@dataclass
class SemanticSearch:
    """Finds candidate locations by meaning. Confirms nothing."""

    cfg: SemanticSearchConfig
    http: Callable[[str, dict, float], dict] = _post
    # The reader whose ledger this shares, when there is one. Held rather than
    # its list, because the reader replaces its `Spend` wholesale between steps
    # and anything holding the list itself would go on appending to the
    # previous step's — every semantic call missing from the log, in
    # order-dependent ways, with nothing raising.
    reader: object | None = None
    # An explicit ledger to append to, for a caller that shares one belonging
    # to something other than a reader. The executor shares the *editor's*, so
    # reads and edits interleave in one chronological log.
    #
    # Safe as a bare list here in a way it was not for the reader: the editor
    # is built fresh per attempt and never replaces its list, whereas the
    # reader replaces its whole `Spend` between steps.
    ledger: list[ToolCall] | None = None
    _own_calls: list[ToolCall] = field(default_factory=list)

    @property
    def calls(self) -> list[ToolCall]:
        """One ledger with the reader, so the log stays chronological.

        Two lists concatenated say what was looked at but not in what order,
        and the order is most of how a conclusion was reached — a read that
        confirmed a semantic hit is a different act from one that preceded it.
        Falls back to its own list when there is no reader, which is how every
        test and any caller without repo access builds it.
        """
        if self.reader is not None:
            return self.reader.spend.calls
        return self._own_calls if self.ledger is None else self.ledger

    def _search(self, question: str) -> list[dict]:
        """Embed the question and ask the collection. Raises; callers classify.

        Factored out because two callers want the same request and different
        answers: `query` renders it for a model to read, `locations` takes the
        positions and nothing else.
        """
        embedded = self.http(
            f"{self.cfg.api_base}/embeddings",
            {"model": self.cfg.embedding_model, "input": [question]},
            self.cfg.timeout_seconds,
        )
        vector = embedded["data"][0]["embedding"]
        found = self.http(
            f"{self.cfg.qdrant_url}/collections/{self.cfg.collection}/points/search",
            {"vector": vector, "limit": self.cfg.max_results, "with_payload": True},
            self.cfg.timeout_seconds,
        )
        return found["result"]

    def query(self, text: str) -> list[str]:
        """Ranked `path:start-end` leads, most similar first."""
        question = (text or "").strip()
        if not question:
            return ["semantic search needs a question"]

        try:
            hits = self._search(question)
        except Exception as e:  # noqa: BLE001 - classified rather than guessed
            failure = classify(e)
            self.calls.append(ToolCall("semantic_search", question, 0))
            if failure.retry_worthwhile:
                # The index is down or busy. Genuinely transient, and the run
                # can proceed on the exact-search tools.
                return [
                    f"semantic search is unavailable ({failure.describe()}). "
                    "Use search and list_files instead; do not treat this as "
                    "evidence that nothing matches."
                ]
            # Permanent, and almost always a misconfigured collection name
            # returning 404. Left as "unavailable" it would read as an outage
            # for the whole run, and nobody would look at the config.
            return [
                f"semantic search is misconfigured and will not work for this "
                f"run ({failure.describe()}). Do not retry it. Use search and "
                "list_files, and note this in your reasoning so the operator "
                "sees it."
            ]

        lines: list[str] = []
        kept = 0
        for h in hits:
            if h.get("score", 0) < self.cfg.min_score:
                continue
            # Counted in results, not in lines. Each result contributes a
            # citation plus its snippet, so counting lines would silently cap
            # at a third of what was asked for.
            if kept >= self.cfg.max_results:
                break
            kept += 1
            p = h.get("payload", {})
            source = p.get("source") or f"{p.get('path')}:{p.get('start_line')}-{p.get('end_line')}"
            symbol = " ".join(x for x in (p.get("symbol_type"), p.get("symbol")) if x)
            lines.append(f"{h.get('score', 0):.3f}  {source}  {symbol}".rstrip())
            # A couple of lines of the chunk, so an irrelevant lead can be
            # discarded without spending a `read_file` on it. Deliberately not
            # the whole chunk: eight full chunks is several hundred lines of
            # context, and a chunk that looks complete invites being treated as
            # the file rather than as a pointer into it.
            snippet = [
                ln.strip()
                for ln in (p.get("content") or "").splitlines()
                if ln.strip()
            ][: self.cfg.snippet_lines]
            lines.extend(f"       | {ln}" for ln in snippet)

        # The lines themselves, not just how many. This is the one tool
        # whose answer cannot be fetched again from the repository.
        self.calls.append(
            ToolCall("semantic_search", question, len(lines),
                     result="\n".join(lines))
        )
        return lines


    def chunks_for(self, text: str, path: str) -> list[str]:
        """The indexed text of chunks the index associates with one file.

        Returned as a *search key*, never as an answer. The content is as it
        stood when the collection was built — rebuilt on a commit hook, so it
        lags the working tree by however many edits and commits have happened
        since — and an executor quoting bytes exactly must never be handed it.

        It is nonetheless the better anchor. By the time a caller asks, the
        model's own `old_string` has already failed to match, so it is known
        wrong; this text is known to have been real, which makes its lines far
        likelier to still exist verbatim in the file. The caller finds them
        there and reads the working tree at that point.

        Ranked by how many chunks land in the file being asked about rather
        than by score. Similarity is a statement about the whole repository and
        says nothing about position within one file.
        """
        try:
            hits = self._search(text)
        except Exception:  # noqa: BLE001 - a locator that fails is no locator
            return []

        found: list[str] = []
        for h in hits:
            payload = h.get("payload") or {}
            if payload.get("path") != path:
                continue
            content = payload.get("content") or ""
            if content.strip():
                found.append(content)
        # Recorded as `locate`, not as `semantic_search`. It costs an embedding
        # and a query like any other lookup, so it belongs in the ledger — but
        # under its own name, because no model asked for it and a summary
        # implying one did would misdescribe the attempt.
        self.calls.append(ToolCall("locate", f"{path}: {text[:60]}", len(found)))
        return found
