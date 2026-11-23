"""Every name, code (as written, lower, title), name word, name word pairs in order, against the real shipped list."""
import itertools, json, sys
from renfe import stations as s
every = s.bundled()
queries = set()
for st in every:
    queries |= {st.code, st.code.lower(), st.code.title(), st.name, s._norm(st.name)}
    words = s._norm(st.name).split()
    queries |= set(words) | {" ".join(p) for p in itertools.combinations(words, 2)}
out = {}
for q in sorted(queries):
    try: out[q] = s.resolve(q, every).code
    except s.NoMatch: out[q] = "NoMatch"
    except Exception as e: out[q] = type(e).__name__
json.dump(out, open(sys.argv[1], "w"), ensure_ascii=False)
print(len(out), "queries")
