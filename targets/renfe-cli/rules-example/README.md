# Rules the demo learned

What the factory learned on renfe-cli on 21 Nov 2026, kept as examples of what a rule looks like. Nothing reads
this folder: the factory loads only `rules/` at its root, which starts empty, and what it learns there stays on
your machine (it is git-ignored).

- `rule-new-tests-fail-on-base`, `rule-mutants`: born from a failed gate step; their check is that step.
- `rule-human-20261121-224310-s8`: born from a correction. Its check is a shell command a model wrote.
- `rule-human-20261121-stalegtfs-3`: a correction still `proposed`, with no check yet.

To start the demo target from them, copy the files into `rules/`. A copied shell check does not run until you
have read it and approved it: `bin/factory approve rule-human-20261121-224310-s8`.
