## How large one stage should be

A stage lands completely or not at all. A failure at one site reverts every site with it, and the whole stage is re-attempted against an instruction written before any of it was done — so the question is not how much work fits, it is how much you are willing to lose and redraw.

**Group sites that need the same judgement.** When a change is the identical edit at every site and nothing at any site needs its own thought, one stage is right however many files it touches. Declare every one of them in `edit_files`, and state the total number of sites in the instruction so the executor knows when it has finished — a sweep that stops early is caught by the gates in seconds, without spending a review.

**Keep apart sites where the judgement at one depends on the judgement at another** — a declaration and the things that inherit from it, two files that have to agree on a name. That is a single judgement spread across files, and splitting it is what leaves each piece unreviewable on its own. A file that is unusually large is a stage by itself.

**Independent judgements are neither.** Sites that each need their own decision, where none of them refers to any other, are several small decisions rather than one large one, and reading them together costs the reviewer no more than reading them in sequence. Group those while each is small: needing thought at every site is not on its own a reason to split, but needing the *same* thought at every site is a reason to group.
