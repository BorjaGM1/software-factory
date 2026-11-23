"""Station sweep. `corpus OUT`: every name, code (as written, lower, title), name word and ordered word pair of the
shipped list, normalised here (not with the branch's own _norm). `resolve CORPUS OUT`: what each query resolves to."""
import itertools
import json
import os
import re
import sys
import unicodedata
from importlib import import_module

s = import_module(os.environ.get("GATE_SWEEP_MODULE", "renfe.stations"))


def norm(text):
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", text.replace("(todas)", " ")).strip()


if sys.argv[1] == "corpus":
    queries = set()
    for st in s.bundled():
        words = norm(st.name).split()
        queries |= {st.code, st.code.lower(), st.code.title(), st.name, norm(st.name), *words}
        queries |= {" ".join(p) for p in itertools.combinations(words, 2)}
    json.dump(sorted(q for q in queries if q), open(sys.argv[2], "w"), ensure_ascii=False)
else:
    every, out = s.bundled(), {}
    for q in json.load(open(sys.argv[2])):
        try:
            out[q] = s.resolve(q, every).code
        except s.NoMatch:
            out[q] = "<none>"
        except Exception as e:  # UsageError: several stations match
            out[q] = "<ambiguous>" if "several" in str(e) else f"<{type(e).__name__}>"
    json.dump(out, open(sys.argv[3], "w"), ensure_ascii=False)
