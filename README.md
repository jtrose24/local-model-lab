# local-model-lab

receipts for my local model tests on a 48GB apple silicon mac.

one folder per test. each folder has the model revision and file hashes, runtime and settings, machine info, prompts, raw outputs, timing logs, a script to recount the numbers, and a plain-english page on what the test does and doesn't show.

if a number is in a post, it should be checkable from here.

## layout

each test gets its own folder named by date and model, like `2026-11-02-<model-slug>/`. only real measurements from my mac go here. no mock runs, nothing estimated. screen recordings of runs are attached to GitHub Releases, not committed; their sha256 is in each bundle's `SHA256SUMS`.

## license

- scripts and code (`*.py`, shell scripts): MIT, see [LICENSE](LICENSE)
- data (results, receipt bundles, findings, recordings): CC BY 4.0, see [LICENSE-DATA](LICENSE-DATA)

## tests

- [`2026-11-04-nanbeige4.3-33b-mlx-4bit-run3-clean/`](2026-11-04-nanbeige4.3-33b-mlx-4bit-run3-clean/): **current result.** Nanbeige4.3-33B, MLX 4-bit (pratikgx/Nanbeige4.3-33B-MLX-4bit @ `2375350`), M4 Pro 48GB, on AC power, with Darkbloom stopped for the whole run (checked once a minute). Run with the fixed `bench_mlx.py` now in this repo (sha256 `e20585d5…`, matches `tool_sha256` in the manifest). Every speed run decodes exactly 256 tokens, and the prompts are exactly 512 / 4,096 / 32,768 tokens.
- [`2026-11-02-nanbeige4.3-33b-mlx-4bit/`](2026-11-02-nanbeige4.3-33b-mlx-4bit/): run 1, **superseded**. The Mac was on battery, and a Darkbloom provider was running in the background and loading models. It was run with the older script (sha256 `eaeecd92…`, no longer in the repo; that script's issues are fixed in the current one). Known issues, left as-is so the sealed receipts stay untouched:
  - the speed runs stopped at the end-of-text token after 34–52 tokens, not 256
  - the "4096" and "32768" prompts were really 3,857 and 30,625 tokens
  - redaction changed the text `n/a` to `n/<user>` inside that bundle's `recompute.py` (display text only, no numbers)
  - no screen recording
