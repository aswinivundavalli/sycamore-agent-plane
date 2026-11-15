"""Create the five more-itertools field-test runs (see docs/field-test-more-itertools.md).

Usage: python3 scripts/field-test/create_runs.py /path/to/more-itertools [--agent claude|fake] [--port 7700]
"""
import argparse, json, urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("repo")
ap.add_argument("--agent", default="claude")
ap.add_argument("--port", type=int, default=7700)
ap.add_argument("--budget", type=float, default=2)
args = ap.parse_args()
REPO = args.repo
COMMON = ("\n\nWork in the style of the existing code. Add or update unit tests in tests/ for every behaviour you change, "
          "and run the full suite with `python3 -m unittest discover -s tests -t .` before finishing; it must pass. "
          "Do not reformat unrelated code.")
CHANGELOG = (" Add one bullet describing the change under the 'Unreleased' heading in docs/versions.rst, "
             "following the existing format.")
TASKS = [
 ("Clear errors for negative sizes in take, spy, split_into",
  "take(), spy() and split_into() leak itertools.islice's internal error message when given a negative size "
  "(e.g. take(-1, x) raises 'Stop argument for islice() must be None or an integer: 0 <= x <= sys.maxsize'). "
  "Make each raise ValueError with a clear message naming the argument, consistent with how chunked() reports "
  "negative sizes. Document it in each docstring." + CHANGELOG),
 ("Descriptive errors for out-of-range nth_combination / nth_permutation / nth_product",
  "nth_combination(range(3), 5, 0) raises a bare IndexError with no message. Audit nth_combination, nth_permutation "
  "and nth_product for every invalid input (r larger than the pool, negative r, out-of-range index) and make each "
  "raise the documented exception type with a message that says what was wrong and the valid range. Keep the "
  "exception types backwards compatible." + CHANGELOG),
 ("New function: circular_pairwise",
  "Add a new function circular_pairwise(iterable) to more_itertools/more.py that yields overlapping pairs and wraps "
  "around: circular_pairwise('ABCD') -> ('A','B'), ('B','C'), ('C','D'), ('D','A'). A one-element input yields "
  "(x, x); an empty input yields nothing. It must consume the iterable lazily except for remembering the first item. "
  "Export it in __all__, add its signature to more_itertools/more.pyi, list it in docs/api.rst in the most fitting "
  "section, and include a doctest in its docstring." + CHANGELOG),
 ("Tests pinning down size-0 behaviour of the chunking functions",
  "The chunking functions disagree on a chunk size of 0: chunked, ichunked, sliced, chunked_even and grouper return "
  "nothing, while batched, sliding_window, padded and distribute raise ValueError. Do not change any behaviour. "
  "Add a focused set of regression tests that pins down the current n=0 behaviour of each of these functions, "
  "placed next to each function's existing tests, so any future change to it is deliberate."),
 ("Document empty-input behaviour of mark_ends, interleave_evenly, partitions, minmax",
  "Document in the docstrings what mark_ends, interleave_evenly, partitions and minmax do when given an empty "
  "iterable (for example partitions([]) yields [[]], minmax([]) raises ValueError unless default is given). Verify "
  "each claim by running the code, add a doctest example for each where it fits the existing docstring style, and "
  "make sure the doctests pass. No behaviour changes."),
]
for title, task in TASKS:
    body = {"repo_path": REPO, "title": title, "task": task + COMMON, "agent": args.agent, "budget_usd": args.budget}
    req = urllib.request.Request(f"http://127.0.0.1:{args.port}/api/runs", data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"}, method="POST")
    r = json.load(urllib.request.urlopen(req))
    print(r["id"], r["state"], r["base_branch"], r["test_cmd"], "|", title)
