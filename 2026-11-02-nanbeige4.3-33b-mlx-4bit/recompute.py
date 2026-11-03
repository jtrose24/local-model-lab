#!/usr/bin/env python3
"""recompute.py - recount every number in this receipt bundle from its raw files.

Standalone: needs only Python 3.9+ (no mlx, no network, no API key). A copy of this script lives in every bundle.

  python3 recompute.py              # check: re-grade raw/outputs.jsonl, recompute results.md + results.json from raw/,
                                    # check post_numbers.json (if present) and SHA256SUMS. Exits 1 on any mismatch.
  python3 recompute.py --write      # (used by bench_mlx.py) write results.md + results.json from raw/
  python3 recompute.py --seal       # after editing FINDING.md or adding post_numbers.json: verifies every OTHER file
                                    # still matches SHA256SUMS, then rewrites SHA256SUMS
  python3 recompute.py some/bundle  # run against another bundle directory

post_numbers.json (optional) lists every number a post or FINDING.md quotes, each tied to a results.json key:
  [{"claim": "decode tok/s at 4K context", "key": "bench.ctx4096.decode_tps_median", "value": 23.4},
   {"claim": "right answers", "key": "quality.right", "value": 5}]
A claim passes when round(results.json value, decimals of the quoted value) == quoted value.
"""
import hashlib, json, os, re, statistics, sys

RESULTS_JSON, RESULTS_MD, SUMS = "results.json", "results.md", "SHA256SUMS"
EDITABLE_AFTER_RUN = {"FINDING.md", "post_numbers.json"}  # --seal may re-hash only these (plus SHA256SUMS itself)


# ---------------------------------------------------------------- grading (bench_mlx.py imports these)
def final_answer(text, thinking_opened=False):
    """Text after the last </think>. If the prompt opened a <think> block and it never closed, there is no answer."""
    if "</think>" in text:
        return text.split("</think>")[-1].strip()
    if thinking_opened or "<think>" in text:
        return ""
    return text.strip()


def _lines(s):
    return [l.strip() for l in s.strip().splitlines() if l.strip()]


def grade(prompt, text, thinking_opened=False):
    """Returns (status, reason, final). status is 'pass', 'fail' or 'manual'. Deterministic; no model judge."""
    g = prompt.get("grader") or {"type": "manual"}
    final = final_answer(text, thinking_opened)
    t = g.get("type", "manual")
    if t == "manual":
        return "manual", g.get("note", "score by hand"), final
    if not final:
        return "fail", "no final answer (thinking never closed, or empty output)", final
    flags = re.I if g.get("ignore_case", True) else 0
    if t == "regex":  # pattern searched in the final answer
        ok = re.search(g["pattern"], final, flags) is not None
        return ("pass" if ok else "fail"), f"regex {g['pattern']!r} {'found' if ok else 'not found'} in final answer", final
    if t == "last_line_regex":
        last = _lines(final)[-1] if _lines(final) else ""
        ok = re.search(g["pattern"], last, flags) is not None
        return ("pass" if ok else "fail"), f"last line {last[:80]!r} {'matches' if ok else 'does not match'} {g['pattern']!r}", final
    if t == "line_count":  # exactly n non-empty lines, each matching an optional pattern
        ls = _lines(final)
        if len(ls) != g["n"]:
            return "fail", f"expected exactly {g['n']} lines, got {len(ls)}", final
        if g.get("each_line"):
            bad = [l for l in ls if not re.search(g["each_line"], l, flags)]
            if bad:
                return "fail", f"line {bad[0][:60]!r} does not match {g['each_line']!r}", final
        return "pass", f"{g['n']} lines, all valid", final
    if t == "json_fields":  # final answer is a JSON object (code fences allowed) with these exact field values
        body = re.sub(r"^```(?:json)?\s*|\s*```$", "", final.strip(), flags=re.I)
        try:
            obj = json.loads(body)
        except Exception as e:
            return "fail", f"not valid JSON ({type(e).__name__})", final
        if g.get("only_keys") and set(obj) != set(g["only_keys"]):
            return "fail", f"keys {sorted(obj)} != {sorted(g['only_keys'])}", final
        for k, v in (g.get("equals") or {}).items():
            if obj.get(k) != v:
                return "fail", f"{k}={obj.get(k)!r}, expected {v!r}", final
        for k, pat in (g.get("matches") or {}).items():
            if not re.search(pat, str(obj.get(k, "")), flags):
                return "fail", f"{k}={obj.get(k)!r} does not match {pat!r}", final
        return "pass", "JSON fields match", final
    raise ValueError(f"unknown grader type {t!r}")


# ---------------------------------------------------------------- reading raw files
def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def load_bundle(d):
    j = lambda p: os.path.join(d, p)
    manifest = json.load(open(j("manifest.json"), encoding="utf-8"))
    machine = json.load(open(j("machine.json"), encoding="utf-8"))
    prompts = {p["id"]: p for p in read_jsonl(j("prompts.jsonl"))}
    timing = read_jsonl(j("raw/timing.jsonl"))
    outputs = read_jsonl(j("raw/outputs.jsonl"))
    return manifest, machine, prompts, timing, outputs


# ---------------------------------------------------------------- computing every number
def compute(d):
    manifest, machine, prompts, timing, outputs = load_bundle(d)
    mock = bool(manifest.get("mock"))
    res = {"mock": mock, "bench": {}, "quality": {}, "per_prompt": []}

    # speed bench: median over measured runs (warmups excluded), peak memory = max
    order = []
    for r in timing:
        if r["kind"] == "bench" and not r.get("warmup") and r["id"] not in order:
            order.append(r["id"])
    for bid in order:
        rs = [r for r in timing if r["kind"] == "bench" and r["id"] == bid and not r.get("warmup")]
        res["bench"][bid] = {
            "context_target": rs[0]["context_target"],
            "prompt_tokens": rs[0]["prompt_tokens"],
            "runs": len(rs),
            "prefill_tps_median": statistics.median(r["prefill_tps"] for r in rs),
            "decode_tps_median": statistics.median(r["decode_tps"] for r in rs),
            "ttft_s_median": statistics.median(r["ttft_s"] for r in rs),
            "wall_s_median": statistics.median(r["wall_s"] for r in rs),
            "peak_mem_gb_max": max(r["peak_mem_gb"] for r in rs),
        }

    # quality: re-grade every output from its raw text with the grader stored in prompts.jsonl
    qt = {(r["id"], r["run"]): r for r in timing if r["kind"] == "quality"}
    counts = {"pass": 0, "fail": 0, "manual": 0}
    hit_cap = 0
    for o in (o for o in outputs if o["kind"] == "quality"):
        status, reason, final = grade(prompts[o["id"]], o["text"], o.get("thinking_opened_in_prompt", False))
        t = qt[(o["id"], o["run"])]
        capped = t["gen_tokens"] >= o["max_tokens"]
        hit_cap += capped
        counts[status] += 1
        res["per_prompt"].append({"id": o["id"], "run": o["run"], "status": status, "reason": reason,
                                  "hit_token_cap": capped, "prompt_tokens": t["prompt_tokens"],
                                  "gen_tokens": t["gen_tokens"], "decode_tps": t["decode_tps"],
                                  "wall_s": t["wall_s"], "peak_mem_gb": t["peak_mem_gb"]})
    q = [r for r in timing if r["kind"] == "quality"]
    if q:
        res["quality"] = {
            "outputs": len(q), "auto_graded": counts["pass"] + counts["fail"], "right": counts["pass"],
            "wrong": counts["fail"], "manual": counts["manual"], "hit_token_cap": hit_cap,
            "right_pct": 100.0 * counts["pass"] / max(1, counts["pass"] + counts["fail"]),
            "prompt_tokens_total": sum(r["prompt_tokens"] for r in q),
            "gen_tokens_total": sum(r["gen_tokens"] for r in q),
            "wall_s_total": sum(r["wall_s"] for r in q),
            "decode_tps_median": statistics.median(r["decode_tps"] for r in q),
            "peak_mem_gb_max": max(r["peak_mem_gb"] for r in q),
            "wall_s_per_right": (sum(r["wall_s"] for r in q) / counts["pass"]) if counts["pass"] else None,
        }
    return manifest, machine, res


def flatten(obj, prefix=""):
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}{k}."))
    elif not isinstance(obj, list):
        out[prefix[:-1]] = obj
    return out


# ---------------------------------------------------------------- rendering results.md (only from compute())
def f(x, n):
    return "n/<user>" if x is None else f"{x:,.{n}f}"


def render_md(manifest, machine, res):
    L = []
    if res["mock"]:
        L += ["# ⚠️ MOCK RUN: FAKE NUMBERS, NOT A REAL RESULT", "",
              "Generated by `bench_mlx.py --mock` to test the receipt pipeline. No model ran. Never quote anything here.", ""]
    L += [f"# Results: {manifest['model']['repo_id']} ({manifest['quant']})", "",
          "Generated by `recompute.py --write` from `raw/timing.jsonl` and `raw/outputs.jsonl` only. "
          "Run `python3 recompute.py` to recount; it exits non-zero if any number here differs.", "",
          f"- Machine: {machine.get('chip') or 'n/<user>'}, {f(machine.get('ram_gb'), 0)} GB, "
          f"macOS {(machine.get('macos') or {}).get('ProductVersion', '?')}, GPU cores {machine.get('gpu_cores') or 'n/<user>'}, "
          f"power {machine.get('power_source') or 'n/<user>'}",
          f"- Runtime: {', '.join(f'{k} {v}' for k, v in manifest['runtime'].items() if isinstance(v, str))}",
          f"- Model revision: `{manifest['model'].get('revision') or 'local path (no revision)'}`",
          f"- Prompts file sha256: `{manifest['prompts']['sha256']}`",
          f"- Run: {manifest['started_at']} → {manifest['ended_at']}", ""]
    if res["bench"]:
        s = manifest["sampling"]["bench"]
        L += ["## Speed (median of measured runs after 1 warmup; peak = max)", "",
              f"Greedy decoding (temp {s['temp']}), {s['max_tokens']} generated tokens per run.", "",
              "| Context target | Prompt tok | Runs | Prefill tok/s | Decode tok/s | TTFT s | Wall s | Peak GB |",
              "|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for b in res["bench"].values():
            L.append(f"| {b['context_target']:,} | {b['prompt_tokens']:,} | {b['runs']} | {f(b['prefill_tps_median'], 1)} | "
                     f"{f(b['decode_tps_median'], 1)} | {f(b['ttft_s_median'], 2)} | {f(b['wall_s_median'], 2)} | "
                     f"{f(b['peak_mem_gb_max'], 2)} |")
        L.append("")
    if res["quality"]:
        q, s = res["quality"], manifest["sampling"]["quality"]
        L += ["## Quality prompts (deterministic graders in prompts.jsonl; no model judge)", "",
              f"temp {s['temp']}, top_p {s['top_p']}, seed {s['seed']}, max_tokens {s['max_tokens']} (per-prompt overrides in prompts.jsonl).", "",
              f"- Right: **{q['right']} of {q['auto_graded']}** auto-graded ({f(q['right_pct'], 1)}%); "
              f"{q['manual']} need hand scoring; {q['hit_token_cap']} hit the token cap",
              f"- Tokens: {q['prompt_tokens_total']:,} prompt, {q['gen_tokens_total']:,} generated; "
              f"wall {f(q['wall_s_total'], 1)} s total, {f(q['wall_s_per_right'], 1)} s per right answer",
              f"- Decode median {f(q['decode_tps_median'], 1)} tok/s; peak {f(q['peak_mem_gb_max'], 2)} GB", "",
              "| Prompt | Run | Result | Why | Gen tok | Decode tok/s | Wall s | Peak GB |",
              "|---|---:|---|---|---:|---:|---:|---:|"]
        for p in res["per_prompt"]:
            why = p["reason"].replace("|", "\\|")
            L.append(f"| {p['id']} | {p['run']} | {p['status'].upper()}{' (cap)' if p['hit_token_cap'] else ''} | {why} | "
                     f"{p['gen_tokens']:,} | {f(p['decode_tps'], 1)} | {f(p['wall_s'], 1)} | {f(p['peak_mem_gb'], 2)} |")
        L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------- SHA256SUMS
def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def bundle_files(d):
    out = []
    for root, dirs, files in os.walk(d):
        dirs[:] = sorted(x for x in dirs if x != "__pycache__")
        for name in files:
            rel = os.path.relpath(os.path.join(root, name), d).replace(os.sep, "/")
            if rel != SUMS:
                out.append(rel)
    return sorted(out)


def write_sums(d):
    lines = [f"{sha256_file(os.path.join(d, r))}  {r}" for r in bundle_files(d)]
    open(os.path.join(d, SUMS), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return len(lines)


def check_sums(d, skip=()):
    errs, listed = [], {}
    for line in open(os.path.join(d, SUMS), encoding="utf-8"):
        if line.strip():
            h, rel = line.rstrip("\n").split("  ", 1)
            listed[rel] = h
    for rel, h in listed.items():
        if rel in skip:
            continue
        p = os.path.join(d, rel)
        if not os.path.exists(p):
            errs.append(f"SHA256SUMS: {rel} is listed but missing")
        elif sha256_file(p) != h:
            errs.append(f"SHA256SUMS: {rel} changed since the run")
    for rel in bundle_files(d):
        if rel not in listed and rel not in skip:
            errs.append(f"SHA256SUMS: {rel} is not listed (added after the run?)")
    return errs


# ---------------------------------------------------------------- main
def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    d = os.path.abspath(args[0] if args else os.path.dirname(os.path.abspath(__file__)))
    if "--seal" in argv:  # check the run's files BEFORE parsing anything, so a tampered bundle is never resealed
        errs = check_sums(d, skip=EDITABLE_AFTER_RUN) if os.path.exists(os.path.join(d, SUMS)) else [f"{SUMS} missing"]
        if errs:
            print("\n".join(errs) + "\nrefusing to reseal: only FINDING.md and post_numbers.json may change after a run")
            return 1
        compute(d)  # must still parse
        print(f"resealed {write_sums(d)} files into {SUMS}; now run: python3 recompute.py")
        return 0
    manifest, machine, res = compute(d)
    md = render_md(manifest, machine, res)
    js = json.dumps(res, indent=2, sort_keys=True) + "\n"
    banner = "MOCK BUNDLE (fake numbers, pipeline test only): " if res["mock"] else ""

    if "--write" in argv:
        open(os.path.join(d, RESULTS_MD), "w", encoding="utf-8").write(md)
        open(os.path.join(d, RESULTS_JSON), "w", encoding="utf-8").write(js)
        print(f"{banner}wrote {RESULTS_MD} and {RESULTS_JSON} from raw/")
        return 0

    errs, n_checked = [], 0
    if open(os.path.join(d, RESULTS_MD), encoding="utf-8").read() != md:
        errs.append(f"{RESULTS_MD} does not match a fresh recount from raw/")
    if open(os.path.join(d, RESULTS_JSON), encoding="utf-8").read() != js:
        errs.append(f"{RESULTS_JSON} does not match a fresh recount from raw/")
    flat = flatten({k: v for k, v in res.items() if k != "per_prompt"})
    n_checked = len(flat) + len(res["per_prompt"])
    pn = os.path.join(d, "post_numbers.json")
    if os.path.exists(pn):
        for c in json.load(open(pn, encoding="utf-8")):
            if c["key"] not in flat:
                errs.append(f"post_numbers: {c['claim']!r} cites unknown key {c['key']!r}")
                continue
            v, dec = c["value"], c.get("decimals")
            if dec is None:
                dec = len(str(v).split(".")[1]) if "." in str(v) else 0
            actual = flat[c["key"]]
            if actual is None or round(actual, dec) != v:
                errs.append(f"post_numbers: {c['claim']!r} says {v}, raw files give {actual}")
            else:
                print(f"  ok  {c['claim']}: {v}  ({c['key']})")
            n_checked += 1
    if os.path.exists(os.path.join(d, SUMS)):
        errs += check_sums(d)
    else:
        errs.append(f"{SUMS} missing")
    if errs:
        print(f"{banner}FAIL\n  " + "\n  ".join(errs))
        return 1
    print(f"{banner}OK: {n_checked} numbers recomputed from raw/ match results.md/results.json"
          f"{' and post_numbers.json' if os.path.exists(pn) else ''}; {SUMS} verified")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
