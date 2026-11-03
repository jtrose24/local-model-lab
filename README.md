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

- [`2026-11-02-nanbeige4.3-33b-mlx-4bit/`](2026-11-02-nanbeige4.3-33b-mlx-4bit/): Nanbeige4.3-33B, MLX 4-bit (pratikgx/Nanbeige4.3-33B-MLX-4bit @ `2375350`), M4 Pro 48GB, on battery. Run with the `bench_mlx.py` in this repo (sha256 `eaeecd92…`, matches `tool_sha256` in the manifest). Known issues with this run, left as-is so the sealed receipts stay untouched:
  - the speed runs stopped at the model's end-of-text token after 34–52 tokens, not the 256 that `results.md` says, so decode tok/s comes from short generations
  - the "4096" and "32768" prompts were really 3,857 and 30,625 tokens
  - the default username redaction changed the text `n/a` to `n/<user>` inside the bundle's `recompute.py`. This only affects display text and no numbers
  - no screen recording for this run
