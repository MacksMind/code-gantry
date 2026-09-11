## The plan

This is the authority for the project, rendered from the project's ledger for this call. A stage instruction is a pointer into it, not a substitute for it.

Every heading and item carries a key like `{#$prefix.017}`. Keys are how you refer to the plan: `plan_keys` on a stage names the items it is drawn from, and a landing on those keys is what closes them; `key` on a plan note says which item a finding is about. A stage may instead be drawn from an open finding alone — `plan_keys` empty and the finding's id in `resolves` — which is how work on an item that has already landed is done. An item marked `[x]` is landed or struck. A heading or item flagged `(human)` is a person's to act on and cannot be drawn until they do — read it for context only.

The section after the plan lists what has changed since this text was last folded: landings not yet marked here, keys and findings held by other runs, stages already drawn and waiting for a run to take them, questions waiting on a person, and open findings with their ids, which `resolves` may cite. Do not draw again what is listed as waiting. Where the two disagree about whether something is outstanding, that section is later.

Nothing in this run edits the plan and there is no file to fetch: what is printed is what the ledger holds. It is still not a substitute for looking at the code — a count in an item is a claim about when someone wrote it down.
