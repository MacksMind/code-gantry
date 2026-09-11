## What this stage has already put on its branch

This is the whole of what the stage has changed since it started, and it is what the reviewer is shown — not the delta since the last attempt. **None of it is baseline.** Reading a file will show you these lines as ordinary existing code; they are this stage's own doing, and an instruction that calls them pre-existing describes a tree the reviewer cannot see and will be blocked for contradicting the diff.

Write the revision against this, and let its scope cover everything below that you intend to keep.

```diff
$diff
```
