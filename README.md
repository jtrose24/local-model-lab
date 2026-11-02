# local-model-lab

receipts for my local model tests on a 48GB apple silicon mac.

one folder per test. each folder has the model revision and file hashes, runtime and settings, machine info, prompts, raw outputs, timing logs, a script to recount the numbers, and a plain-english page on what the test does and doesn't show.

if a number is in a post, it should be checkable from here.

## layout

each test gets its own folder named by date and model, like `2026-11-02-<model-slug>/`. only real measurements from my mac go here. no mock runs, nothing estimated. screen recordings of runs are attached to GitHub Releases, not committed; their sha256 is in each bundle's `SHA256SUMS`.

## license

- scripts and code (`*.py`, shell scripts): MIT, see [LICENSE](LICENSE)
- data (results, receipt bundles, findings, recordings): CC BY 4.0, see [LICENSE-DATA](LICENSE-DATA)
