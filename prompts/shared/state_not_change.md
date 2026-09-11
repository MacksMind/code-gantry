**Write the state, not the change.** Say what is true now. Never what a
document used to say, what an earlier stage or run concluded, which documents
still disagree, or that a count has moved from one number to another — write
the number. Those are facts about this pipeline's history rather than about the
project, and history is answerable from the commit log, which cannot go stale.
A sentence phrased as a change also stops making sense the moment the change is
already true, and every later pass reads it forever.

The test is whether a reader could confirm it from the repository alone. "The
helper is called from twelve sites" can be checked. "This was previously
recorded as three blockers, none of them real" cannot be checked by anyone, and
is the shape that accumulates: each pass adds a line about what the last pass
got wrong, so the document grows a history of itself that no reader needs and
every call pays for.
