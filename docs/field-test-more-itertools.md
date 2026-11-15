# Field test: five Claude Code agents on more-itertools

**Date:** 2026-11-15 · **Agent:** Claude Code (headless, `claude -p`) · **Target:** [more-itertools](https://github.com/more-itertools/more-itertools) at `ceafee8`. This is a pure-Python library: 7,300 lines of source, with 936 tests that run in about 10 s.

## Goal

A realistic five-run sprint against a real codebase. Each run had a $2 cap, and the whole sprint had a $10 budget.

**Success criteria:**
- Every run reaches a reviewed outcome.
- `master` stays green after the merges.
- Spend stays within budget.
- Nothing is left behind on disk.

The tasks came from probing the library for real, unfixed rough edges. None of them was made up:

| # | Task | Kind |
|---|---|---|
| 1 | `take`, `spy` and `split_into` leak `islice`'s internal error text (*"Stop argument for islice() must be None or an integer: 0 <= x <= sys.maxsize"*) for negative sizes. Make them raise a clear `ValueError`. | Bug |
| 2 | `nth_combination(range(3), 5, 0)` raises a bare `IndexError` with no message. Make `nth_combination`, `nth_permutation` and `nth_product` raise descriptive errors, keeping the exception types backwards compatible. | Bug |
| 3 | A new `circular_pairwise()`, with type stub, `__all__` export, API docs, doctest and tests. | Feature |
| 4 | Pin the inconsistent size-0 behaviour of nine chunking functions with regression tests, without changing behaviour. | Tests |
| 5 | Document empty-input behaviour of `mark_ends`, `interleave_evenly`, `partitions` and `minmax`, with verified doctests. | Docs |

Runs 1, 2 and 3 were each told to add a changelog bullet under "Unreleased" in `docs/versions.rst`. That deliberately invited a real conflict.

## Results

| # | Attempts | Cost | Change | Outcome |
|---|---|---|---|---|
| 1 | 1 | $0.52 | 5 files, +27 −1 | Merged |
| 2 | 2 (second resolved a conflict) | $1.32 | 5 files, +82 −18 | Merged after the agent resolved the conflict |
| 3 | 1 | $0.49 | 5 files, +67 | Merged |
| 4 | 1 | $0.60 | 2 files, +50 | Merged |
| 5 | 1 | $0.54 | 2 files, +32 | Merged |
| **Total** | 6 attempts | **$3.47** | | **5 of 5 merged** |

- **`master` after the merges:** 936 → **954 tests, all passing**. I re-ran the suite myself on `master` after each merge, because the conductor tests each branch, not the merged result.
- **Wall time:** 18:42:37 → 18:46:22, **3 min 45 s** from launching five runs to the last merge.
- **End state:** `git worktree list` shows only the main checkout, no `conductor/run-*` branches remain, and the worktree directory is empty.
- **Behaviour verified on `master`:**
  - `take(-1, …)` raises `ValueError: n must be at least 0`.
  - `nth_combination(range(3), 5, 0)` raises `IndexError: r must be at most the length of iterable (3), got 5`.
  - `circular_pairwise('ABCD')` returns `[('A','B'), ('B','C'), ('C','D'), ('D','A')]`.

## Timeline (UTC)

| Time | Event |
|---|---|
| 18:42:37 | Five runs created through the API. Five worktrees cut from `master`, five agents running ([screenshot](field-test/01-all-running.png)). |
| 18:43:16 | **`kill -9` of the conductor** while all five agents were mid-task. |
| 18:43:31 | All 5 runner processes are still alive while the conductor is down. |
| 18:43:35 | Conductor restarted. **5 of 5 runs re-attached**, with no run lost, duplicated or relaunched. |
| 18:43:56 | Runs 3 and 1 ready for review, tests passing. |
| 18:44:09 | Run 1 reviewed and merged. |
| 18:44:27 | Run 3 merged. Its changelog edit merged cleanly next to run 1's (see "What we learned"). `master` re-tested: green. |
| 18:44:40 | Run 2 ready, but flagged **"Finished, but conflicts with master"** in `docs/versions.rst` ([screenshot](field-test/02-conflict-detected.png)). Merge is disabled. |
| 18:45:02 | "Ask agent to resolve conflicts": the conductor merges `master` into run 2's branch and resumes the agent's own session with the conflicted file ([screenshot](field-test/03-run2-conflict-resolution.png)). |
| 18:45:14 / 18:45:21 | Runs 4 and 5 reviewed and merged. |
| 18:45:46 | Run 2's agent keeps both changelog bullets, checks no markers remain, and runs 946 tests on the merged tree. Cost of this attempt: $0.70. |
| 18:45:59 | The conductor's own test run on the resolved branch passes. The merge check is clean. |
| 18:46:22 | Run 2 merged. Every worktree and branch has been cleaned up. |

## Review notes

Every diff was read before merging, not just the green test badge.

- **Run 1.** Minimal fix. The message matches `chunked()`'s existing wording exactly, and `n=None` keeps working. The agent's first version compared every size with `0` and broke an existing test, which expects `ValueError` for a non-integer size. It caught this itself and explained the integer-only check in its summary.
- **Run 2.** Covered all three functions and kept the exception types compatible. It added a case nobody asked for: a clear message when one of `nth_product`'s iterables is empty. It also flagged an open design question in prose, explained under "What we learned".
- **Run 3.** A clean, lazy implementation, tested against an infinite iterator. It placed itself correctly in `__all__`, the `.pyi` stub and `docs/api.rst`.
- **Run 4.** On first read I suspected it had skipped `ichunked` and `chunked_even`, and had added duplicate `distribute` and `padded` tests. Both suspicions were wrong:
  - those two functions already had size-0 tests, and the agent's summary said so;
  - the "duplicates" also pin the exact error messages.

  It also handled a subtle compatibility point: Python 3.13's built-in `itertools.batched` raises immediately, while the library's own fallback raises lazily.
- **Run 5.** Docstrings only, plus one missing test. It followed an existing house pattern for wrapping long exception messages, and ran ruff unprompted.

## What we learned, and what we changed

1. **File-level overlap warnings were noise on this codebase.** All five tasks touched the 5,600-line `more.py` or the two shared test files, so every card warned about every other: 10 of 10 run pairs ([screenshot](field-test/02-conflict-detected.png)). A warning that always fires is ignored. **Fix (`fa86a9c`):** overlap is now judged on the line ranges each run changed, in the base file's coordinates, with adjacent lines counting as overlap, as git treats them. Replayed on this sprint's actual commits, it flags **1 pair instead of 10**: runs 1 and 2 in `docs/versions.rst`, the only pair that really conflicted. If runs were cut from different base commits, it falls back to file level.
2. **The activity line showed raw shell text.** None of the 44 real Bash calls carried a description, so cards read "Running: sed -n 80,140p tests/test_more.py; echo ---; …". Claude's own progress lines did the same ("Running python3 - <<'EOF' …"). **Fix (`a7e12fd`):** commands are summarised with quote-aware parsing, giving lines like "Reading docs/api.rst", "Running the tests", "Searching for raise\|IndexError" or "Running a Python snippet". Progress lines that merely echo a command are rephrased the same way.
3. **The conflict-resolution merge commit was mislabelled.** It reused the task title. It now reads "merge master (conflicts resolved by the agent)".
4. **One attempt label rendered badly.** The attempt tag overlapped the message in the activity timeline; fixed in the UI.
5. **A planned conflict didn't conflict, and that was correct.** Run 3's changelog entry added a new "New functions" block above the line run 1 appended to, so git merged them cleanly. Only run 2, appending at the same point as run 1, really conflicted. The conductor's pre-merge check (`git merge-tree`) agreed with git exactly, both times.
6. **A known limit, confirmed.** Run 2's summary ended with a genuine design question: should `nth_product(..., repeat=-1)` raise, as `itertools.product` does? It asked in prose, without the `QUESTION:` marker, so the run showed as "finished" rather than "awaiting your answer". It was caught here only because every summary was read. Detecting free-form questions in the final message is already on the roadmap. This run is now a concrete test case for it.
7. **Tests run on branches, not merge results.** Every merge stayed green here, but only because I re-ran the suite on `master` by hand after each one. Verifying the merge result automatically remains the prerequisite for any unattended merging.

## Reproduce

```bash
git clone https://github.com/more-itertools/more-itertools /tmp/mi
```

```bash
CONDUCTOR_HOME=/tmp/mi-home CONDUCTOR_MAX_PARALLEL=5 ./conductor.sh
```

Then create runs from the UI, with repository `/tmp/mi`, agent `claude` and budget `2`, using the task texts in `transcripts/field-test/run-*-attempt-1.prompt.txt`.

## Artefacts

- Agent transcripts, one raw stream-json file per attempt, plus the exact prompt each agent received: `transcripts/field-test/`
- Screenshots: `docs/field-test/`
- The more-itertools changes were made to a local clone only. Nothing was pushed upstream.
