"""
evaluate_all_checkpoints.py — Post-Training Full Evaluation
============================================================
Loads every saved final adapter and evaluates with:
  - 520 AdvBench prompts (full benchmark, vs 100 used during training)
  - 200 GSM8K test examples (same as training eval)
  - 500 Alpaca val examples (same as training eval)

Run ONCE after all seeds are complete, or incrementally after each seed.

Usage:
  python evaluate_all_checkpoints.py
  python evaluate_all_checkpoints.py --seeds 42 123 7
  python evaluate_all_checkpoints.py --seeds 42 --methods vanilla safelora agent
  python evaluate_all_checkpoints.py --dry_run   # just show what would be evaluated

Output:
  results/full_eval_summary.csv  — one row per (method, task, seed)
  results/full_eval_summary.md   — formatted markdown table for the paper
"""

import argparse
import logging
import sys
from pathlib import Path
import pandas as pd
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import PeftModel

sys.path.append(str(Path(__file__).resolve().parent))

from src.dataset_loader import (
    load_gsm8k_test,
    load_alpaca_val,
    load_advbench,
)
from src.metrics import (
    evaluate_task_gsm8k,
    evaluate_task_alpaca,
    evaluate_safety,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Configuration: every (method, task) combination
# ─────────────────────────────────────────────────────────────────────────────
# adapter_dir_template uses {task} and {seed} as placeholders.
# Set enabled=False to skip a method temporarily.

METHOD_CONFIGS = [
    {
        "method":    "vanilla",
        "tasks":     ["gsm8k", "alpaca"],
        "dir_tmpl":  "vanilla_{task}_seed{seed}",
        "enabled":   True,
    },
    {
        "method":    "safelora",
        "tasks":     ["gsm8k", "alpaca"],
        "dir_tmpl":  "safelora_{task}_seed{seed}",
        "enabled":   True,
    },
    {
        "method":    "salora",
        "tasks":     ["gsm8k", "alpaca"],
        "dir_tmpl":  "salora_{task}_seed{seed}",
        "enabled":   True,
    },
    {
        "method":    "simplectrl",
        "tasks":     ["gsm8k", "alpaca"],
        "dir_tmpl":  "simplectrl_{task}_seed{seed}",
        "enabled":   True,
    },
    {
        "method":    "agent",
        "tasks":     ["gsm8k", "alpaca"],
        "dir_tmpl":  "agent_{task}_seed{seed}",
        "enabled":   True,
    },
]

MODEL_ID = "Qwen/Qwen2.5-1.5B-Instruct"


# ─────────────────────────────────────────────────────────────────────────────
# Loader
# ─────────────────────────────────────────────────────────────────────────────

def load_adapter(model_id: str, adapter_path: Path, device: str):
    """Loads base model + PEFT adapter, moves to device."""
    if torch.cuda.is_available():
        dtype = torch.bfloat16 if torch.cuda.get_device_capability()[0] >= 8 else torch.float16
    else:
        dtype = torch.float32

    base = AutoModelForCausalLM.from_pretrained(
        model_id, torch_dtype=dtype, low_cpu_mem_usage=True
    )
    model = PeftModel.from_pretrained(base, str(adapter_path), is_trainable=False)
    model.to(device)
    model.eval()
    return model


# ─────────────────────────────────────────────────────────────────────────────
# Single evaluation
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_one(
    method: str,
    task: str,
    seed: int,
    adapter_path: Path,
    tokenizer,
    advbench_full: list,
    gsm8k_test: dict,
    alpaca_val: dict,
    device: str,
) -> dict:
    """Evaluate one (method, task, seed) checkpoint. Returns result dict."""
    logger.info(f"{'='*60}")
    logger.info(f"Evaluating: {method} | {task} | seed {seed}")
    logger.info(f"Adapter: {adapter_path}")
    logger.info(f"{'='*60}")

    model = load_adapter(MODEL_ID, adapter_path, device)

    # ── Safety: full 520 prompts ──
    logger.info("Running safety evaluation (520 prompts)...")
    refusal_520 = evaluate_safety(
        model, tokenizer, advbench_full,
        batch_size=4, device=device
    )

    # ── Safety: first 100 prompts (for consistency with training logs) ──
    logger.info("Running safety evaluation (100 prompts)...")
    refusal_100 = evaluate_safety(
        model, tokenizer, advbench_full[:100],
        batch_size=4, device=device
    )

    # ── Capability ──
    if task == "gsm8k":
        logger.info("Running GSM8K accuracy evaluation...")
        task_metric = evaluate_task_gsm8k(
            model, tokenizer, gsm8k_test,
            batch_size=4, device=device
        )
        metric_name = "gsm8k_accuracy"
    else:
        logger.info("Running Alpaca validation loss evaluation...")
        task_metric = evaluate_task_alpaca(
            model, tokenizer, alpaca_val,
            batch_size=4, device=device
        )
        metric_name = "alpaca_val_loss"

    # ── Clean up GPU memory ──
    del model
    torch.cuda.empty_cache() if torch.cuda.is_available() else None

    result = {
        "method":          method,
        "task":            task,
        "seed":            seed,
        "refusal_100":     round(refusal_100,  4),
        "refusal_520":     round(refusal_520,  4),
        "refusal_delta":   round(refusal_520 - refusal_100, 4),  # should be small
        metric_name:       round(task_metric,  4),
    }

    logger.info(
        f"Result: refusal_100={refusal_100:.3f} | "
        f"refusal_520={refusal_520:.3f} | "
        f"{metric_name}={task_metric:.4f}"
    )

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Summary table
# ─────────────────────────────────────────────────────────────────────────────

def build_summary_table(df: pd.DataFrame) -> str:
    """
    Builds a markdown table grouping by method and task.
    Shows mean ± std across seeds if multiple seeds present, 
    else just the single value.
    """
    lines = []

    for task in ["gsm8k", "alpaca"]:
        task_df = df[df["task"] == task]
        if task_df.empty:
            continue

        metric_col = "gsm8k_accuracy" if task == "gsm8k" else "alpaca_val_loss"
        capability_label = "GSM8K Acc" if task == "gsm8k" else "Alpaca Val Loss"
        direction = "↑" if task == "gsm8k" else "↓"

        lines.append(f"\n### {task.upper()} Results\n")
        lines.append(
            f"| Method | Refusal@100 | Refusal@520 | {capability_label} {direction} |"
        )
        lines.append("|--------|-------------|-------------|" + "-" * (len(capability_label) + 5) + "|")

        method_order = ["vanilla", "safelora", "salora", "simplectrl", "agent"]
        for method in method_order:
            m_df = task_df[task_df["method"] == method]
            if m_df.empty:
                continue

            seeds = m_df["seed"].tolist()
            n = len(seeds)

            def fmt(col):
                vals = m_df[col].dropna()
                if len(vals) == 0:
                    return "N/A"
                if len(vals) == 1:
                    return f"{vals.iloc[0]:.4f}"
                return f"{vals.mean():.4f} ± {vals.std():.4f}"

            r100 = fmt("refusal_100")
            r520 = fmt("refusal_520")
            cap  = fmt(metric_col) if metric_col in m_df.columns else "N/A"

            seed_str = f"({', '.join(str(s) for s in sorted(seeds))})"
            label = f"**{method}** {seed_str}" if method == "agent" else f"{method} {seed_str}"
            lines.append(f"| {label} | {r100} | {r520} | {cap} |")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Full 520-prompt evaluation of all checkpoints")
    parser.add_argument(
        "--seeds", nargs="+", type=int, default=[42],
        help="Seeds to evaluate (default: 42)"
    )
    parser.add_argument(
        "--methods", nargs="+", type=str, default=None,
        help="Methods to evaluate (default: all). Options: vanilla safelora salora simplectrl agent"
    )
    parser.add_argument(
        "--tasks", nargs="+", type=str, default=["gsm8k", "alpaca"],
        help="Tasks to evaluate (default: both)"
    )
    parser.add_argument(
        "--dry_run", action="store_true",
        help="Print what would be evaluated without running"
    )
    parser.add_argument(
        "--device", type=str, default=None,
        help="Device override (default: auto-detect cuda/cpu)"
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent
    models_dir   = project_root / "models"
    results_dir  = project_root / "results"
    results_dir.mkdir(exist_ok=True)

    device = args.device or ("cuda" if torch.cuda.is_available() else "cpu")
    logger.info(f"Device: {device}")
    logger.info(f"Seeds to evaluate: {args.seeds}")

    # ── Filter methods ──
    configs = METHOD_CONFIGS
    if args.methods:
        configs = [c for c in configs if c["method"] in args.methods]
    configs = [c for c in configs if c["enabled"]]

    # ── Build evaluation queue ──
    queue = []
    for cfg in configs:
        for seed in args.seeds:
            for task in cfg["tasks"]:
                if task not in args.tasks:
                    continue
                dir_name     = cfg["dir_tmpl"].format(task=task, seed=seed)
                adapter_path = models_dir / dir_name
                queue.append({
                    "method":       cfg["method"],
                    "task":         task,
                    "seed":         seed,
                    "adapter_path": adapter_path,
                    "exists":       adapter_path.exists(),
                })

    # ── Dry run ──
    if args.dry_run:
        print("\nEvaluation queue:")
        for item in queue:
            status = "✓" if item["exists"] else "✗ MISSING"
            print(f"  {status}  {item['method']:<12} {item['task']:<8} seed={item['seed']}  → {item['adapter_path'].name}")
        missing = [i for i in queue if not i["exists"]]
        print(f"\n{len(queue)} total, {len(missing)} missing")
        return

    # ── Load shared resources once ──
    logger.info("Loading tokenizer...")
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    logger.info("Loading evaluation datasets...")
    advbench_full = load_advbench()          # all 520
    gsm8k_test    = load_gsm8k_test(num_examples=200, seed=42)
    alpaca_val    = load_alpaca_val(num_examples=500, seed=42)

    logger.info(f"AdvBench: {len(advbench_full)} prompts")

    # ── Load existing results to allow resuming ──
    summary_csv = results_dir / "full_eval_summary.csv"
    if summary_csv.exists():
        existing_df = pd.read_csv(summary_csv)
        logger.info(f"Found {len(existing_df)} existing results in {summary_csv}")
    else:
        existing_df = pd.DataFrame()

    # ── Run evaluations ──
    new_results = []

    for item in queue:
        if not item["exists"]:
            logger.warning(f"Adapter not found, skipping: {item['adapter_path']}")
            continue

        # Skip if already evaluated
        if not existing_df.empty:
            already_done = (
                (existing_df["method"] == item["method"]) &
                (existing_df["task"]   == item["task"])   &
                (existing_df["seed"]   == item["seed"])
            ).any()
            if already_done:
                logger.info(f"Already evaluated: {item['method']} {item['task']} seed={item['seed']} — skipping")
                continue

        try:
            result = evaluate_one(
                method       = item["method"],
                task         = item["task"],
                seed         = item["seed"],
                adapter_path = item["adapter_path"],
                tokenizer    = tokenizer,
                advbench_full= advbench_full,
                gsm8k_test   = gsm8k_test,
                alpaca_val   = alpaca_val,
                device       = device,
            )
            new_results.append(result)

            # Save incrementally after each evaluation
            all_results = (
                pd.concat([existing_df, pd.DataFrame(new_results)], ignore_index=True)
                if not existing_df.empty
                else pd.DataFrame(new_results)
            )
            all_results.to_csv(summary_csv, index=False)
            logger.info(f"Saved progress to {summary_csv}")

        except Exception as e:
            logger.error(f"Failed to evaluate {item['method']} {item['task']} seed={item['seed']}: {e}")
            continue

    # ── Final summary ──
    if summary_csv.exists():
        final_df = pd.read_csv(summary_csv)
        logger.info(f"\nFinal results ({len(final_df)} evaluations):")

        # Print to console
        md_table = build_summary_table(final_df)
        print(md_table)

        # Save markdown
        md_path = results_dir / "full_eval_summary.md"
        with open(md_path, "w") as f:
            f.write("# LoRA-SafeLoop: Full Evaluation Results (520 AdvBench Prompts)\n")
            f.write(md_table)
        logger.info(f"Markdown table saved to {md_path}")
    else:
        logger.warning("No results were generated.")


if __name__ == "__main__":
    main()