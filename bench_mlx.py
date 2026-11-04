#!/usr/bin/env python3
"""bench_mlx.py - measure an MLX model on THIS Mac and write an inspectable receipt bundle.

Measures prefill tok/s, decode tok/s, TTFT, wall time and peak memory (median of --runs after 1 warmup), and with
--quality runs the prompts in --prompts (default prompts/default.jsonl) and grades them with deterministic graders.

Usage:
  python bench_mlx.py --model pratikgx/Nanbeige4.3-33B-MLX-4bit --contexts 512 4096 32768 --gen 256 --runs 3 --quality
  python bench_mlx.py --model <id> --quality --record          # also screen-record the Terminal run (macOS)
      recording/terminal-run.mov is listed in SHA256SUMS but NEVER committed (git-ignored). Publish it as a
      GitHub Release asset on jtrose24/local-model-lab:
        gh release create <bundle-folder> <bundle-folder>/recording/terminal-run.mov --repo jtrose24/local-model-lab \\
          --title "<bundle-folder> recording" --notes "sha256 is in <bundle-folder>/SHA256SUMS"
  Paths are redacted by default (home dir -> ~, username -> <user>) before hashing; --no-redact keeps them.
  python bench_mlx.py --model <id> --quality --mock             # FAKE backend: tests the pipeline anywhere, no mlx

Every run writes results/<YYYY-MM-DD>_<model-slug>_<quant>/ (MOCK runs: results/MOCK_<...>/):
  manifest.json      model repo + exact HF revision, SHA-256 of every model file, runtime versions, quant, context,
                     sampling, exact command line, start/end timestamps
  machine.json       chip, RAM, macOS, GPU cores, power source, python/package versions (all captured by script)
  prompts.jsonl      exact copy of the prompts file used
  raw/outputs.jsonl  every generated text;  raw/timing.jsonl  per-run speed + memory;  raw/rendered_prompts.jsonl
  raw/stdout.log     everything printed during the run
  results.md/.json   generated ONLY from raw/ by recompute.py
  recompute.py       recounts every number from raw/ (and post_numbers.json); exits 1 on mismatch
  FINDING.md         template: one-sentence finding, cost per right answer, one failure example, limits
  SHA256SUMS         every file in the bundle, written last (check with: shasum -a 256 -c SHA256SUMS)
It also still writes the old flat summary to results/<model-slug>-<timestamp>.json and prints the same table.
Nothing here is pre-filled: every number comes from the run on your machine (or is marked MOCK).
"""
import argparse, hashlib, importlib.util, io, json, os, platform, random, re, shlex, shutil, signal, statistics
import subprocess, sys, time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
RECOMPUTE_SRC = os.path.join(HERE, "receipts", "recompute.py")
MOCK_BANNER = "MOCK RUN - FAKE NUMBERS - NOT A REAL RESULT"

FILLER = ("The history of the printing press is a story of incremental engineering, local craft knowledge, "
          "and the slow spread of standardized type across European cities. ")
BENCH_INSTRUCTION = "\n\nSummarize the passage above in one sentence."

_spec = importlib.util.spec_from_file_location("recompute", RECOMPUTE_SRC)
recompute = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(recompute)


# ============================================================ helpers
def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sh(cmd, timeout=30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return None


def sha256_file(p):
    return recompute.sha256_file(p)


def git_blob_sha1(p):
    h = hashlib.sha1(); h.update(f"blob {os.path.getsize(p)}\0".encode())
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def slugify(s):
    return re.sub(r"[^a-z0-9.]+", "-", s.lower()).strip("-")


class Tee(io.TextIOBase):
    def __init__(self, *streams): self.streams = streams
    def write(self, s):
        for st in self.streams: st.write(s); st.flush()
        return len(s)
    def flush(self):
        for st in self.streams: st.flush()


def jsonl_append(path, row):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ============================================================ machine.json (captured, never typed)
def machine_info():
    m = {"captured_by": "bench_mlx.py machine_info()", "captured_at": now_iso(),
         "platform": platform.platform(), "machine_arch": platform.machine(),
         "python": platform.python_version(), "python_executable": sys.executable, "raw": {}}
    cmds = {"chip": ["sysctl", "-n", "machdep.cpu.brand_string"], "hw_memsize": ["sysctl", "-n", "hw.memsize"],
            "perflevels": ["sysctl", "-n", "hw.perflevel0.physicalcpu", "hw.perflevel1.physicalcpu"],
            "iogpu_wired_limit_mb": ["sysctl", "-n", "iogpu.wired_limit_mb"],
            "sw_vers": ["sw_vers"], "pmset_batt": ["pmset", "-g", "batt"], "pmset_lowpower": ["pmset", "-g"]}
    for k, c in cmds.items():
        m["raw"][k] = {"cmd": shlex.join(c), "stdout": sh(c)}
    m["chip"] = m["raw"]["chip"]["stdout"]
    try: m["ram_bytes"] = int(m["raw"]["hw_memsize"]["stdout"]); m["ram_gb"] = m["ram_bytes"] / 2**30
    except Exception: m["ram_bytes"] = m["ram_gb"] = None
    sv = m["raw"]["sw_vers"]["stdout"] or ""
    m["macos"] = dict(re.findall(r"^(\w+):\s*(.+)$", sv, re.M)) or None
    batt = m["raw"]["pmset_batt"]["stdout"] or ""
    m["power_source"] = (re.search(r"Now drawing from '([^']+)'", batt) or [None, None])[1]
    lp = re.search(r"^\s*(?:lowpowermode|powermode)\s+(\d)", m["raw"]["pmset_lowpower"]["stdout"] or "", re.M)  # macOS 27 prints "powermode"
    m["low_power_mode"] = (lp.group(1) == "1") if lp else None
    m["raw"]["pmset_lowpower"]["stdout"] = lp.group(0) if lp else None  # keep only the relevant line
    disp = sh(["system_profiler", "SPDisplaysDataType", "-json"], timeout=60)
    m["gpu_cores"] = None
    if disp:
        try:
            gpus = json.loads(disp).get("SPDisplaysDataType", [])
            m["gpu"] = [{"model": g.get("sppci_model"), "cores": g.get("sppci_cores")} for g in gpus]
            m["gpu_cores"] = next((int(g["cores"]) for g in m["gpu"] if g.get("cores")), None)
        except Exception as e:
            m["gpu_error"] = repr(e)
    m["packages"] = {}
    try:
        from importlib.metadata import version, PackageNotFoundError
        for p in ["mlx", "mlx-metal", "mlx-lm", "transformers", "huggingface_hub", "tokenizers", "numpy", "safetensors"]:
            try: m["packages"][p] = version(p)
            except PackageNotFoundError: m["packages"][p] = None
    except Exception: pass
    return m


# ============================================================ model files, revision, hashes
def classify(rel):
    n = os.path.basename(rel).lower()
    if n.endswith((".safetensors", ".gguf", ".bin", ".npz", ".pt", ".pth")): return "weights"
    if n.startswith("tokenizer") or n in ("vocab.json", "merges.txt", "special_tokens_map.json") or n.endswith((".model", ".tiktoken")):
        return "tokenizer"
    if n.endswith((".json", ".jinja", ".py")): return "config"
    return "other"


def resolve_model(repo, revision, mock=False, mock_tmp=None):
    """Return (local_dir, model_meta). Pins the exact commit, hashes every local file, cross-checks against the Hub."""
    meta = {"repo_id": repo, "requested_revision": revision, "revision": None, "hub_check": None, "files": []}
    if os.path.isdir(repo):
        local, hub = os.path.abspath(repo), None
        meta["source"] = "local directory (no Hub revision)"
    else:
        from huggingface_hub import HfApi, snapshot_download
        hub = None
        try:
            info = HfApi().model_info(repo, revision=revision, files_metadata=True)
            hub = {s.rfilename: s for s in info.siblings}
            meta["revision"] = info.sha
            meta["hub_last_modified"] = info.last_modified.isoformat() if info.last_modified else None
            meta["hub_total_bytes"] = sum((s.size or 0) for s in info.siblings)
        except Exception as e:
            meta["hub_check"] = f"Hub unreachable: {e!r}; using the local cache only"
        if mock:
            # MOCK: fetch only small non-weight files so hashing and Hub cross-checks are exercised for real
            allow = ["*.json", "*.jinja", "tokenizer*", "*.model"]
            local = snapshot_download(repo, revision=meta["revision"] or revision, allow_patterns=allow,
                                      cache_dir=mock_tmp)
        else:
            # Same as the original script's load(): fetches anything missing, a quick no-op when the cache is complete.
            size = meta.get("hub_total_bytes")
            print(f"[model] resolving {repo}@{meta['revision']} in the HF cache"
                  + (f" (full repo {size/1e9:.2f} GB; downloads only what is missing)" if size else ""), flush=True)
            try:
                local = snapshot_download(repo, revision=meta["revision"] or revision)
            except Exception:  # offline: fall back to whatever is cached
                local = snapshot_download(repo, revision=meta["revision"] or revision, local_files_only=True)
        meta["source"] = "huggingface hub cache"
        snap = os.path.basename(os.path.normpath(local))
        meta["local_snapshot_commit"] = snap
        if meta["revision"] and snap != meta["revision"]:
            meta["hub_check"] = f"WARNING local snapshot {snap} != requested commit {meta['revision']}"
        meta["revision"] = meta["revision"] or snap
    meta["local_path"] = local
    mism = 0
    for root, _, files in os.walk(local):
        for name in sorted(files):
            p = os.path.join(root, name); rel = os.path.relpath(p, local)
            if rel.startswith(".cache") or name.endswith((".lock", ".incomplete")): continue
            rp = os.path.realpath(p)
            t0 = time.perf_counter()
            row = {"path": rel, "kind": classify(rel), "bytes": os.path.getsize(rp), "sha256": sha256_file(rp)}
            if row["bytes"] > 1e8:
                print(f"[hash] {rel}: {row['sha256'][:16]}… ({row['bytes']/1e9:.2f} GB, {time.perf_counter()-t0:.0f}s)", flush=True)
            if hub and rel in hub:
                s = hub[rel]
                if s.lfs:
                    row["hub_sha256"] = s.lfs.sha256; row["hub_match"] = s.lfs.sha256 == row["sha256"]
                else:
                    row["git_blob_sha1"] = git_blob_sha1(rp); row["hub_blob_id"] = s.blob_id
                    row["hub_match"] = s.blob_id == row["git_blob_sha1"]
                mism += not row["hub_match"]
            meta["files"].append(row)
    meta["files"].sort(key=lambda r: r["path"])
    if hub:
        local_set = {r["path"] for r in meta["files"]}
        meta["hub_files_not_local"] = sorted(k for k in hub if k not in local_set and k != ".gitattributes"
                                             and not (k.lower().startswith("readme")))
        if meta["hub_check"] is None:
            meta["hub_check"] = ("OK: every local file matches the Hub's sha256 (LFS) or git blob id" if mism == 0
                                 else f"MISMATCH: {mism} local file(s) differ from the Hub at this revision")
    return local, meta


def detect_quant(local_dir, override):
    if override: return override
    try:
        cfg = json.load(open(os.path.join(local_dir, "config.json")))
        q = cfg.get("quantization") or cfg.get("quantization_config") or {}
        if q.get("bits"):
            return f"{q['bits']}bit" + (f"-g{q['group_size']}" if q.get("group_size") else "")
    except Exception: pass
    return "unquantized"


# ============================================================ backends
class MLXBackend:
    name = "mlx"

    def __init__(self, local_dir):
        import mlx.core as mx
        from mlx_lm import load, stream_generate
        from mlx_lm.sample_utils import make_sampler
        self.mx, self.stream_generate, self.make_sampler = mx, stream_generate, make_sampler
        t = time.perf_counter()
        self.model, self.tok = load(local_dir)
        self.load_s = time.perf_counter() - t

    def versions(self):
        import mlx_lm
        return {"backend": "mlx", "mlx": self.mx.__version__, "mlx_lm": getattr(mlx_lm, "__version__", None)}

    def _peak_gb(self):
        f = getattr(self.mx, "get_peak_memory", None) or self.mx.metal.get_peak_memory
        return f() / 1e9

    def _reset(self):
        (self.mx.clear_cache if hasattr(self.mx, "clear_cache") else self.mx.metal.clear_cache)()
        (getattr(self.mx, "reset_peak_memory", None) or self.mx.metal.reset_peak_memory)()

    def render(self, content, thinking=None):
        msg = [{"role": "user", "content": content}]
        kw = {} if thinking is None else {"enable_thinking": thinking}
        try:
            return self.tok.apply_chat_template(msg, add_generation_prompt=True, tokenize=False, **kw)
        except TypeError:
            return self.tok.apply_chat_template(msg, add_generation_prompt=True, tokenize=False)

    def encode(self, text): return self.tok.encode(text)

    def bench_prompt(self, n_tokens):
        """Chat-templated prompt whose total length is ~n_tokens (same construction as the original script)."""
        unit = max(len(self.tok.encode(FILLER)), 1)
        reps = max(1, n_tokens // unit)
        for _ in range(10):  # fix 2026-11-02: repeated filler tokenizes shorter than unit*reps (run 1 got 3,857/30,625)
            text = self.render(FILLER * reps + BENCH_INSTRUCTION, thinking=False)
            ids = self.tok.encode(text)
            if len(ids) >= n_tokens: break
            reps += (n_tokens - len(ids)) // unit + 1
        if len(ids) > n_tokens:  # trim filler from the front, keep the template tail
            ids = ids[:8] + ids[len(ids) - n_tokens + 8:]
        return ids, self.tok.decode(ids)

    def generate(self, ids, max_tokens, temp, top_p, seed, ignore_eos=False):
        self._reset()
        if seed is not None: self.mx.random.seed(seed)
        sampler = self.make_sampler(temp=temp, top_p=top_p) if temp > 0 else self.make_sampler(temp=0.0)
        kw = {}
        if ignore_eos:  # speed runs: mask EOS so every run decodes exactly max_tokens (fix: real runs stopped at 34-52 tok)
            eos = sorted(getattr(self.tok, "eos_token_ids", None) or [self.tok.eos_token_id])
            def _no_eos(tokens, logits, _eos=eos):
                logits[..., _eos] = -float("inf"); return logits
            kw["logits_processors"] = [_no_eos]
        t0 = time.perf_counter(); ttft = None; last = None; text = []
        for r in self.stream_generate(self.model, self.tok, ids, max_tokens=max_tokens, sampler=sampler, **kw):
            if ttft is None: ttft = time.perf_counter() - t0
            text.append(r.text); last = r
        wall = time.perf_counter() - t0
        return "".join(text), {
            "prompt_tokens": last.prompt_tokens, "gen_tokens": last.generation_tokens,
            "total_tokens": last.prompt_tokens + last.generation_tokens,
            "prefill_tps": last.prompt_tps, "decode_tps": last.generation_tps,  # mlx-lm's own rates
            "ttft_s": ttft, "wall_s": wall,
            "peak_mem_gb": max(self._peak_gb(), getattr(last, "peak_memory", 0) or 0),
            "finish_reason": getattr(last, "finish_reason", None)}


class MockBackend:
    """Fake model. Every text it returns starts with [MOCK]. Numbers are random. Exists only to test the pipeline."""
    name = "mock"

    def __init__(self, seed=0):
        self.rng = random.Random(seed); self.load_s = 0.0

    def versions(self): return {"backend": "MOCK (no model ran)", "mlx": "MOCK", "mlx_lm": "MOCK"}
    def render(self, content, thinking=None):
        return f"<|im_start|>user\n{content}<|im_end|>\n<|im_start|>assistant\n" + ("<think>\n" if thinking is not False else "<think>\n\n</think>\n\n")
    def encode(self, text): return list(range(max(1, len(text) // 4)))
    def bench_prompt(self, n):
        unit = max(1, len(FILLER) // 4)
        text = self.render(FILLER * max(1, n // unit) + BENCH_INSTRUCTION, thinking=False)
        return self.encode(text)[:n], text

    ANSWERS = {"arith-pens": "$39", "arith-weekday": "Friday",  # deliberately wrong, to exercise the failure example
               "arith-recipe": "3", "extract-order": "Boise", "instr-three-chips": "M1\nM2 Pro\nM4 Max",
               "instr-json": '{"device": "iPhone", "year": 2007}', "code-palindrome": "def is_palindrome(s): ..."}

    def generate(self, ids, max_tokens, temp, top_p, seed, prompt_id=None):
        r = self.rng
        if prompt_id is None:
            text = "[MOCK] The passage describes the printing press."
        else:
            text = f"[MOCK] fake reasoning for {prompt_id}\n</think>\n\n{self.ANSWERS.get(prompt_id, '[MOCK]')}"
        n = min(max_tokens, max(8, len(text) // 4 + r.randint(0, 40)))
        pre, dec = r.uniform(300, 600), r.uniform(15, 30)
        ttft = len(ids) / pre
        return text, {"prompt_tokens": len(ids), "gen_tokens": n, "total_tokens": len(ids) + n,
                      "prefill_tps": pre, "decode_tps": dec, "ttft_s": ttft, "wall_s": ttft + n / dec,
                      "peak_mem_gb": 18.6 + len(ids) * 2e-5 + r.uniform(0, 0.2), "finish_reason": "stop"}


# ============================================================ username redaction (default on; --no-redact to keep)
def redaction_rules():
    """(pattern, replacement) pairs that strip the macOS home dir and bare username from paths."""
    import getpass
    home = os.path.realpath(os.path.expanduser("~")).rstrip("/")
    homes = {home, os.path.expanduser("~").rstrip("/")}
    users = {os.path.basename(h) for h in homes}
    try: users.add(getpass.getuser())
    except Exception: pass
    # bare-username rules only for names of 3+ chars: a 1-letter user like "a" turned "n/a" into "n/<user>" (2026-11-02)
    users = {u for u in users if u and len(u) >= 3 and u not in ("root", "/", ".")}
    rules = [(re.compile(re.escape(h) + r"(?=[/\\\"'\s,]|$)"), "~") for h in sorted(homes, key=len, reverse=True) if len(h) > 1]
    for u in users:  # bare username, only where it sits inside a path (next to / or \\)
        rules.append((re.compile(r"(?<=[/\\])" + re.escape(u) + r"(?=[/\\\"'\s]|$)"), "<user>"))
        rules.append((re.compile(r"(?<![\w.-])" + re.escape(u) + r"(?=[/\\])"), "<user>"))
    return rules


def redact_text(s, rules):
    for rx, rep in rules: s = rx.sub(rep, s)
    return s


def redact_bundle(bundle, rules):
    """Rewrite every text file in the bundle (not recordings) with the username removed. Returns files changed."""
    changed = 0
    for root, _, files in os.walk(bundle):
        for fn in files:
            fp = os.path.join(root, fn)
            if fn.endswith((".mov", ".mp4")) or fn == "recompute.py": continue  # copied source, never has user paths
            try: s = open(fp, encoding="utf-8").read()
            except (UnicodeDecodeError, OSError): continue
            t = redact_text(s, rules)
            if t != s: open(fp, "w", encoding="utf-8").write(t); changed += 1
    return changed


# ============================================================ screen recording (only with --record)
def start_recording(bundle, args):
    path = os.path.join(bundle, "recording", "terminal-run.mov")
    tool = shutil.which("screencapture") if platform.system() == "Darwin" else None
    print("[record] Screen recording requested (--record).")
    if args.mock or not tool:
        print("[record] screencapture not available here, so nothing is being recorded. To record by hand on a Mac:\n"
              "         press Cmd-Shift-5, choose 'Record Selected Portion', frame the Terminal window, click Record,\n"
              "         start this command, and stop from the menu-bar button when it prints 'SHA256SUMS'.\n"
              f"         Save the file as {path} BEFORE the run ends so SHA256SUMS covers it,\n"
              "         Recordings are git-ignored; publish with: gh release create <bundle-folder> <bundle>/recording/terminal-run.mov")
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    print(f"[record] Starting `screencapture -v` -> {path}\n"
          "         macOS may ask to allow Screen Recording for Terminal (System Settings > Privacy & Security).")
    p = subprocess.Popen(["screencapture", "-v", "-k", path], stdin=subprocess.DEVNULL)
    time.sleep(2)
    return (p, path)


def stop_recording(rec):
    if not rec: return None
    p, path = rec
    p.send_signal(signal.SIGINT)
    try: p.wait(timeout=30)
    except subprocess.TimeoutExpired: p.terminate()
    ok = os.path.exists(path)
    print(f"[record] {'saved ' + path if ok else 'screencapture did not write a file; record by hand next time (Cmd-Shift-5)'}")
    return os.path.relpath(path, os.path.dirname(os.path.dirname(path))) if ok else None


# ============================================================ FINDING.md template
def write_finding(bundle, manifest, res, prompts, outputs):
    mock = manifest["mock"]; q = res.get("quality") or {}
    fail = next((p for p in res["per_prompt"] if p["status"] == "fail"), None)
    L = []
    if mock:
        L += [f"# ⚠️ {MOCK_BANNER}", "", "Template filled from a MOCK run to test the pipeline. Never publish or quote it.", ""]
    L += [f"# FINDING (draft): {manifest['model']['repo_id']} ({manifest['quant']}) on JT's Mac", "",
          "> Status: TEMPLATE. Fill in every TODO by hand from results.md / results.json. List every number you quote in",
          "> post_numbers.json, then run `python3 recompute.py --seal` and `python3 recompute.py`.", "",
          "## Finding (one plain sentence, with the real tradeoff)", "",
          "TODO: e.g. \"<model> got <right> of <graded> right on my 48GB Mac at <decode> tok/s, <n> fewer than <API model>, "
          "for about <x> times less per right answer.\"", "",
          "## Cost per right answer", "",
          "Same prompts.jsonl for both sides (compare `prompts.sha256`). Never fill in an API price without a cited source.", "",
          "- Local (electricity only): `local $/right = (avg_watts × quality_wall_s / 3,600,000 × $/kWh) / right`",
          f"  - quality_wall_s = `quality.wall_s_total` = {recompute.f(q.get('wall_s_total'), 1)} s; right = `quality.right` = {q.get('right', 'n/a')}",
          "  - avg_watts: TODO measured (e.g. `sudo powermetrics --samplers cpu_power,gpu_power -i 1000` during the run, or a wall meter)",
          "  - $/kWh: TODO from JT's power bill (source: TODO)",
          "  - Optional hardware share: `(Mac price / expected lifetime hours) × quality_wall_s / 3600`, stated separately",
          "- API (list price): `api $/right = (Σ input_tokens × $in + Σ output_tokens × $out) / 1,000,000 / right_api`",
          "  - Token counts and right_api must come from the API model's own run on this same prompts.jsonl (its tokenizer differs).",
          "  - $in / $out per million tokens: TODO, source URL + date checked: TODO", "",
          "| Arm | Right / graded | $ per right | Source |", "|---|---|---|---|",
          f"| local {manifest['model']['repo_id']} | {q.get('right', 'n/a')} / {q.get('auto_graded', 'n/a')} | TODO | this bundle |",
          "| API TODO | TODO | TODO | TODO (link to its bundle + price page) |", "",
          "## One concrete failure", ""]
    if fail:
        o = next(o for o in outputs if o["kind"] == "quality" and o["id"] == fail["id"] and o["run"] == fail["run"])
        line_no = outputs.index(o) + 1
        final = recompute.final_answer(o["text"], o.get("thinking_opened_in_prompt", False))
        L += [f"Prompt `{fail['id']}` (run {fail['run']}), from `raw/outputs.jsonl` line {line_no}:", "",
              "> " + prompts[fail["id"]]["prompt"].replace("\n", "\n> "), "",
              f"Expected: {prompts[fail['id']].get('expected', '(see grader)')}. Grader: {fail['reason']}.", "",
              "Final answer as written (first 600 characters):", "", "```text", (final or "(empty)")[:600], "```", "",
              "TODO: one or two plain sentences on why it failed."]
    else:
        L += ["No auto-graded prompt failed in this run. TODO: pick one weak manual-graded output from raw/outputs.jsonl,",
              "quote it with its line number, or say plainly that nothing failed and why the set may be too easy."]
    sb, sq = manifest["sampling"]["bench"], manifest["sampling"]["quality"]
    ctx = ", ".join(str(c) for c in manifest["contexts"])
    L += ["", "## Limits (fine print)", "",
          f"One run on one machine ({manifest['machine_summary']}), one prompt set of {len(prompts)} prompts "
          f"(sha256 {manifest['prompts']['sha256'][:12]}…), one deterministic grader per prompt and no human or model judge; "
          f"{manifest['quality_runs']} sample per prompt at temp {sq['temp']}, top_p {sq['top_p']}, seed {sq['seed']}, "
          f"max_tokens {sq['max_tokens']}; speed is greedy, {sb['max_tokens']} tokens, contexts {ctx}, median of "
          f"{manifest['runs']} after 1 warmup; {manifest['quant']} via {manifest['runtime'].get('backend')} "
          f"mlx {manifest['runtime'].get('mlx')}, mlx-lm {manifest['runtime'].get('mlx_lm')}. Other settings, quants or prompts would change the numbers.",
          "", "## Check it yourself", "",
          "`python3 recompute.py` recounts every number from raw/ and checks SHA256SUMS; "
          "`shasum -a 256 -c SHA256SUMS` (macOS) or `sha256sum -c SHA256SUMS` (Linux) checks every file.", ""]
    open(os.path.join(bundle, "FINDING.md"), "w", encoding="utf-8").write("\n".join(L))


# ============================================================ main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True, help="HF repo id or local MLX directory")
    ap.add_argument("--revision", default=None, help="HF branch/tag/commit to pin (default: main, resolved to a commit)")
    ap.add_argument("--contexts", type=int, nargs="+", default=[512, 4096, 32768])
    ap.add_argument("--gen", type=int, default=256, help="generated tokens per speed run")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--quality", action="store_true", help="run the prompts file")
    ap.add_argument("--prompts", default=os.path.join(HERE, "prompts", "default.jsonl"))
    ap.add_argument("--quality-runs", type=int, default=1, help="samples per quality prompt")
    ap.add_argument("--temp", type=float, default=0.6); ap.add_argument("--top-p", type=float, default=0.95)
    ap.add_argument("--seed", type=int, default=0); ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument("--quant", default=None, help="label override (default: read from config.json)")
    ap.add_argument("--llama-bench", default=None, help="path to llama-bench; records its build in manifest.json")
    ap.add_argument("--out", default="results")
    ap.add_argument("--record", action="store_true", help="screen-record the run (macOS screencapture -v) or print how")
    ap.add_argument("--no-redact", dest="redact", action="store_false",
                    help="keep the macOS username/home dir in bundle paths (default: replaced with ~ / <user>)")
    ap.add_argument("--mock", "--dry-run", dest="mock", action="store_true",
                    help="FAKE backend for testing the pipeline without mlx; every file is marked MOCK")
    a = ap.parse_args()

    started = now_iso(); t_start = time.perf_counter()
    import tempfile
    mock_tmp = tempfile.mkdtemp(prefix="mock-hf-cache-") if a.mock else None
    local, model_meta = resolve_model(a.model, a.revision, mock=a.mock, mock_tmp=mock_tmp)
    quant = detect_quant(local, a.quant)
    date = datetime.now().strftime("%Y-%m-%d")
    name = f"{date}_{slugify(a.model.rstrip('/').split('/')[-1])}_{slugify(quant)}"
    if a.mock: name = "MOCK_" + name
    bundle = os.path.join(a.out, name)
    if os.path.exists(bundle): bundle += datetime.now().strftime("_%H%M%S")
    os.makedirs(os.path.join(bundle, "raw"))
    if a.mock:
        open(os.path.join(bundle, "MOCK_NOT_A_REAL_RESULT.txt"), "w").write(
            MOCK_BANNER + "\nThis bundle was produced by bench_mlx.py --mock on a machine without a model run.\n"
            "Every output and number in it is fake and exists only to test the receipt pipeline.\n")

    log = open(os.path.join(bundle, "raw", "stdout.log"), "w", encoding="utf-8")
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = Tee(real_out, log), Tee(real_err, log)
    tag = "[MOCK] " if a.mock else ""
    if a.mock: print(f"*** {MOCK_BANNER} ***")
    print(f"{tag}bundle: {bundle}")
    print(f"{tag}model: {a.model} @ {model_meta['revision']} ({quant}); {model_meta.get('hub_check')}")
    rec = start_recording(bundle, a) if a.record else None

    machine = machine_info()
    if a.mock: machine = {"MOCK": MOCK_BANNER, **machine}
    machine_summary = (f"{machine.get('chip') or platform.machine()}, "
                       f"{recompute.f(machine.get('ram_gb'), 0)} GB, macOS {(machine.get('macos') or {}).get('ProductVersion', 'n/a')}")
    json.dump(machine, open(os.path.join(bundle, "machine.json"), "w"), indent=2)

    prompts_list = recompute.read_jsonl(a.prompts) if a.quality else []
    shutil.copyfile(a.prompts, os.path.join(bundle, "prompts.jsonl")) if a.quality else \
        open(os.path.join(bundle, "prompts.jsonl"), "w").close()
    prompts = {p["id"]: p for p in prompts_list}
    assert len(prompts) == len(prompts_list), "duplicate prompt ids"

    backend = MockBackend(a.seed) if a.mock else MLXBackend(local)
    print(f"{tag}loaded in {backend.load_s:.1f}s")
    T, O, R = (os.path.join(bundle, "raw", n) for n in ("timing.jsonl", "outputs.jsonl", "rendered_prompts.jsonl"))
    legacy = {"model": a.model, "when": started, "load_s": backend.load_s, "machine": machine, "gen_tokens": a.gen,
              "runs": a.runs, "bench": [], "quality": [], "bundle": bundle}
    m = {"mock": a.mock} if a.mock else {}

    def gen(ids, max_tokens, temp, top_p, seed, pid=None):
        if a.mock: return backend.generate(ids, max_tokens, temp, top_p, seed, prompt_id=pid)
        return backend.generate(ids, max_tokens, temp, top_p, seed, ignore_eos=pid is None)

    for ctx in a.contexts:
        bid = f"ctx{ctx}"
        ids, text = backend.bench_prompt(ctx)
        jsonl_append(R, {**m, "kind": "bench", "id": bid, "tokens": len(ids),
                         "sha256": hashlib.sha256(text.encode()).hexdigest(), "text": text})
        rs = []
        for run in range(-1, a.runs):  # run -1 = warmup (16 tokens, excluded from results)
            mt = 16 if run < 0 else a.gen
            out, t = gen(ids, mt, 0.0, 1.0, None)
            row = {**m, "kind": "bench", "id": bid, "context_target": ctx, "run": run, "warmup": run < 0, **t}
            jsonl_append(T, row)
            jsonl_append(O, {**m, "kind": "bench", "id": bid, "run": run, "warmup": run < 0, "max_tokens": mt, "text": out})
            if run >= 0: rs.append(t)
        med = {k: statistics.median(r[k] for r in rs) for k in ("prefill_tps", "decode_tps", "ttft_s", "wall_s")}
        med["peak_mem_gb"] = max(r["peak_mem_gb"] for r in rs)
        med["total_s"] = med["wall_s"]  # old key name
        legacy["bench"].append({"context": ctx, "prompt_tokens": rs[0]["prompt_tokens"], **med, "all_runs": rs})
        print(f"{tag}ctx {ctx:>6} | prompt {rs[0]['prompt_tokens']:>6} tok | prefill {med['prefill_tps']:8.1f} tok/s | "
              f"decode {med['decode_tps']:6.1f} tok/s | TTFT {med['ttft_s']:6.2f} s | peak {med['peak_mem_gb']:5.1f} GB", flush=True)

    for p in prompts_list:
        text_in = backend.render(p["prompt"])
        ids = backend.encode(text_in)
        opened = text_in.rstrip().endswith("<think>") or (text_in.rfind("<think>") > text_in.rfind("</think>"))
        jsonl_append(R, {**m, "kind": "quality", "id": p["id"], "tokens": len(ids),
                         "sha256": hashlib.sha256(text_in.encode()).hexdigest(), "text": text_in})
        mt = p.get("max_tokens", a.max_tokens)
        for run in range(a.quality_runs):
            seed = a.seed + run
            out, t = gen(ids, mt, a.temp, a.top_p, seed, pid=p["id"])
            jsonl_append(T, {**m, "kind": "quality", "id": p["id"], "context_target": None, "run": run, "warmup": False, **t})
            jsonl_append(O, {**m, "kind": "quality", "id": p["id"], "run": run, "seed": seed, "max_tokens": mt,
                             "thinking_opened_in_prompt": opened, "text": out})
            status, reason, _ = recompute.grade(p, out, opened)
            legacy["quality"].append({"name": p["id"], "run": run, "pass": {"pass": True, "fail": False}.get(status), "output": out})
            print(f"{tag}[quality] {p['id']} run {run}: {status.upper() if status != 'manual' else 'review manually'} "
                  f"({reason}; {t['gen_tokens']} tok, {t['decode_tps']:.1f} tok/s)", flush=True)

    recording = stop_recording(rec)
    runtime = backend.versions()
    if a.llama_bench:
        runtime["llama_cpp_llama_bench"] = a.llama_bench
        runtime["llama_cpp_version"] = sh([a.llama_bench, "--version"]) or "unavailable"
    sampling = {"bench": {"temp": 0.0, "top_p": None, "seed": None, "max_tokens": a.gen, "warmup_max_tokens": 16},
                "quality": {"temp": a.temp, "top_p": a.top_p, "seed": a.seed, "seed_per_run": "seed + run",
                            "max_tokens": a.max_tokens}}
    manifest = {
        **({"MOCK": MOCK_BANNER} if a.mock else {}), "mock": a.mock,
        "schema": "local-model-lab-receipt/1", "tool": "bench_mlx.py",
        "tool_sha256": sha256_file(os.path.abspath(__file__)),
        "command": shlex.join([sys.executable] + sys.argv), "cwd": os.getcwd(),
        "started_at": started, "ended_at": now_iso(), "started_at_utc": None,
        "elapsed_s": time.perf_counter() - t_start, "load_s": backend.load_s,
        "model": {k: v for k, v in model_meta.items() if k != "local_path"},
        "model_local_path": model_meta["local_path"] if not a.mock else "MOCK temporary cache (deleted)",
        "quant": quant, "contexts": a.contexts, "runs": a.runs, "quality_runs": a.quality_runs,
        "runtime": runtime, "sampling": sampling,
        "prompts": {"source_path": os.path.abspath(a.prompts) if a.quality else None, "count": len(prompts_list),
                    "sha256": sha256_file(os.path.join(bundle, "prompts.jsonl"))},
        "bench_prompt": {"filler": FILLER, "instruction": BENCH_INSTRUCTION,
                         "construction": "filler repeated to ~N tokens, chat template with thinking off, trimmed from the front"},
        "recording": recording, "machine_summary": machine_summary,
        "redacted_username": bool(a.redact),
    }
    manifest["started_at_utc"] = datetime.fromisoformat(started).astimezone(timezone.utc).isoformat()
    json.dump(manifest, open(os.path.join(bundle, "manifest.json"), "w"), indent=2)

    shutil.copyfile(RECOMPUTE_SRC, os.path.join(bundle, "recompute.py"))
    recompute.main([bundle, "--write"])
    _, _, res = recompute.compute(bundle)
    write_finding(bundle, manifest, res, prompts, recompute.read_jsonl(O))

    os.makedirs(a.out, exist_ok=True)
    lpath = os.path.join(a.out, f"{'MOCK_' if a.mock else ''}{a.model.replace('/', '__')}-{datetime.now():%Y%m%d-%H%M%S}.json")
    if not a.mock:  # old flat summary, kept for backward compatibility (bundle is the source of truth)
        json.dump(legacy, open(lpath, "w"), indent=2); print("wrote", lpath)
    print(f"{tag}wrote bundle {bundle}; sealing SHA256SUMS (this is the last line of stdout.log)")
    sys.stdout, sys.stderr = real_out, real_err
    log.close()
    if a.redact:  # before hashing, so SHA256SUMS covers the redacted files
        nred = redact_bundle(bundle, redaction_rules())
        print(f"{tag}redacted username/home dir in {nred} file(s) (--no-redact to keep)")
        recompute.main([bundle, "--write"])  # regenerate results from the redacted raw files
    n = recompute.write_sums(bundle)
    print(f"{tag}SHA256SUMS: {n} files. Verify: (cd {bundle} && python3 recompute.py && shasum -a 256 -c SHA256SUMS)")
    if mock_tmp: shutil.rmtree(mock_tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
