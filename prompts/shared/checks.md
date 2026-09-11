## What runs after the executor finishes

These commands run on every stage, after the executor has committed its work and before the diff is reviewed. Anything they change is committed onto the stage branch too, so it reaches the reviewer as part of the diff:

$listed

**An edit's blast radius includes the whitespace it strands.** Removing a line can leave blank lines around it that the formatter then deletes, so those lines change without the executor touching them. A constraint naming the exact set of lines that may change is therefore unsatisfiable whenever one of these rewrites formatting — it will be violated by the tooling, not by the work. Constrain what the code must end up doing, not which lines may differ.
