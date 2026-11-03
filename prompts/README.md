# Prompt sets

`default.jsonl` is a **starter set drafted by Grok Bot on 2026-11-02. JT should review it before the first real run.**
Every model compared gets the same file; each bundle records its sha256, so two bundles are comparable only if
`manifest.json → prompts.sha256` matches.

One JSON object per line:
- `id` (unique), `category`, `prompt` (sent as the only user message, through the model's chat template)
- `grader`: deterministic, no model judge. Types: `regex` (searched in the final answer), `last_line_regex`,
  `line_count` (`n`, optional `each_line`), `json_fields` (`only_keys`, `equals`, `matches`), `manual` (score by hand).
  The "final answer" is the text after the last `</think>`. If the template opened a `<think>` block that never closed,
  the output counts as wrong.
- `expected`: human-readable note of the right answer (not used for grading)
- optional `max_tokens` (overrides `--max-tokens`)

Changing any prompt changes the sha256, so results from the old and new sets must not be compared or summed.
The 7 starter prompts are small and easy, so they check the model and its template more than they rank models. A harder
set (a long-context needle, agentic tool calls, a real repo file) should come from JT.
