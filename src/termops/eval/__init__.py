"""Evaluation framework for Termops error analysis accuracy.

Academic grounding:
    - **MAPE-K loop**: Kephart & Chess (2003), "The Vision of Autonomic Computing",
      IBM Systems Journal.  Defines the Monitor-Analyze-Plan-Execute-Knowledge
      control loop that Termops implements.
    - **Error classification metrics**: Manning, Raghavan & Schütze (2008),
      "Introduction to Information Retrieval", Cambridge.  Precision, Recall, F1.
    - **LLM code evaluation**: Chen et al. (2021), "Evaluating Large Language
      Models Trained on Code", arXiv:2107.03374.  HumanEval pass@k methodology.
    - **SWE-bench**: Jimenez et al. (2024), "SWE-bench: Can Language Models
      Resolve Real-World GitHub Issues?", ICLR 2024.  End-to-end task resolution.
    - **AgentBench**: Liu et al. (2024), "AgentBench: Evaluating LLMs as Agents",
      ICLR 2024.  Multi-dimensional agent capability assessment.
    - **Text generation metrics**: Papineni et al. (2002), "BLEU", ACL;
      Lin (2004), "ROUGE", ACL Workshop; Zhang et al. (2020), "BERTScore",
      ICLR 2020.  Semantic similarity of generated text.
    - **Safety evaluation**: Inan et al. (2023), "Llama Guard: LLM-based
      Input-Output Safeguard for Human-AI Conversations", arXiv:2312.06674.

Architecture:
    The framework evaluates TWO layers independently:
    1.  Rule engine (diagnostics.py) — deterministic, 100% reproducible.
    2.  LLM attribution (llm_client.py) — stochastic, requires multiple runs.

    Metrics are organized into four tiers following AgentBench (Liu et al., 2024):
    - Tier 1: Error classification accuracy (code, severity)
    - Tier 2: Root cause semantic similarity (BLEU, ROUGE-L, BERTScore)
    - Tier 3: Action safety & validity
    - Tier 4: End-to-end task resolution rate
"""

from .benchmark import EvalEngine, EvalResult, EvalSample, EvalSummary
from .dataset import load_dataset, load_dataset_from_jsonl, list_datasets

__all__ = [
    "EvalEngine",
    "EvalResult",
    "EvalSample",
    "EvalSummary",
    "load_dataset",
    "load_dataset_from_jsonl",
    "list_datasets",
]