  What it has no tool for is running anything: no shell, no test invocation,
  nothing whose output it could quote. Do not write "run `grep -n ...`" or
  "run the specs and check" — there is no such tool, and asking for one is
  worse than useless: it will invent the output and argue with itself about a
  file it is already looking at. One such instruction cost ten minutes of a
  model looping over hallucinated command results.
