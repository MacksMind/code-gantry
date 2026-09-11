## Patterns you must not introduce

$listed

These are checked mechanically against the lines you add. They may be correct elsewhere in the project but are out of bounds here.

Test files are exempt. A test asserting one of these is gone has to quote it, so write that assertion normally — the check skips test files and will not reject it.
