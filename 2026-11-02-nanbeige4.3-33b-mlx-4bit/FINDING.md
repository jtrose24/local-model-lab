# FINDING (draft): pratikgx/Nanbeige4.3-33B-MLX-4bit (4bit-g64) on JT's Mac

> Status: TEMPLATE. Fill in every TODO by hand from results.md / results.json. List every number you quote in
> post_numbers.json, then run `python3 recompute.py --seal` and `python3 recompute.py`.

## Finding (one plain sentence, with the real tradeoff)

TODO: e.g. "<model> got <right> of <graded> right on my 48GB Mac at <decode> tok/s, <n> fewer than <API model>, for about <x> times less per right answer."

## Cost per right answer

Same prompts.jsonl for both sides (compare `prompts.sha256`). Never fill in an API price without a cited source.

- Local (electricity only): `local $/right = (avg_watts × quality_wall_s / 3,600,000 × $/kWh) / right`
  - quality_wall_s = `quality.wall_s_total` = 340.5 s; right = `quality.right` = 6
  - avg_watts: TODO measured (e.g. `sudo powermetrics --samplers cpu_power,gpu_power -i 1000` during the run, or a wall meter)
  - $/kWh: TODO from JT's power bill (source: TODO)
  - Optional hardware share: `(Mac price / expected lifetime hours) × quality_wall_s / 3600`, stated separately
- API (list price): `api $/right = (Σ input_tokens × $in + Σ output_tokens × $out) / 1,000,000 / right_api`
  - Token counts and right_api must come from the API model's own run on this same prompts.jsonl (its tokenizer differs).
  - $in / $out per million tokens: TODO, source URL + date checked: TODO

| Arm | Right / graded | $ per right | Source |
|---|---|---|---|
| local pratikgx/Nanbeige4.3-33B-MLX-4bit | 6 / 6 | TODO | this bundle |
| API TODO | TODO | TODO | TODO (link to its bundle + price page) |

## One concrete failure

No auto-graded prompt failed in this run. TODO: pick one weak manual-graded output from raw/outputs.jsonl,
quote it with its line number, or say plainly that nothing failed and why the set may be too easy.

## Limits (fine print)

One run on one machine (Apple M4 Pro, 48 GB, macOS 27.2), one prompt set of 7 prompts (sha256 e9814eee0daa…), one deterministic grader per prompt and no human or model judge; 1 sample per prompt at temp 0.6, top_p 0.95, seed 0, max_tokens 4096; speed is greedy, 256 tokens, contexts 512, 4096, 32768, median of 3 after 1 warmup; 4bit-g64 via mlx mlx 0.32.4, mlx-lm 0.32.2. Other settings, quants or prompts would change the numbers.

## Check it yourself

`python3 recompute.py` recounts every number from raw/ and checks SHA256SUMS; `shasum -a 256 -c SHA256SUMS` (macOS) or `sha256sum -c SHA256SUMS` (Linux) checks every file.
