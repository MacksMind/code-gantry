## How many stages to return

Up to **$cap**: `stage`, plus at most $rest more in `additional_stages`. Anything beyond $cap is discarded, so offering more is output spent for nothing.

**Return as many orthogonal stages as you can name.** Several runs take from what you draw, one stage each, at the same time; a batch of one leaves the others waiting on your next call, and each call costs a survey of the repository. So draw every piece of work you can already name from the reading you have done, as long as no two of them touch the same files or depend on each other. Stop at the point where naming one more would mean reading more than you otherwise would, or where it would overlap one already drawn — an overlapping stage is worse than none, since whichever runs second is refused and redrawn.

**They are independent.** Each is started when a run is free to take it, in any order, and two may run at once in different checkouts; each is cut from the project branch as it stands when it starts. So no stage may assume another stage of the batch has landed: do not write 'extend the helper the previous stage adds'. Stages may still share files, because a read is taken live when the stage runs and every stage is reviewed against the diff it actually produced rather than against your prediction of it.

**The one thing that does not survive is a quoted line range.** Before each queued stage starts, every file it quotes in `read_excerpts` is compared against the copy you read it from. If the bytes have moved, the stage comes back to you to be redrawn — and if an earlier stage of your own batch edited that file, that is the cause and it was your own doing. Everything else in a stage re-derives itself against the tree it finds; a line number cannot, because a number is not recoverable from the file it points into.

So the question to ask of each excerpt is not whether some stage is *allowed* to touch that file — it is whether you expect the batch to change it. If you do, quote it in the stage that runs first, or leave the excerpt out and say what you want; the executor can read the file itself.
