"""
Generic rule-driven extraction engine
─────────────────────────────────────────────────────────────────
Design goal: extending to a new business (e.g. HK English reports) only takes
a new profile — maintainers edit an Excel rule sheet + prompt templates,
never code.

Modules:
    profile      profile loading (profiles/<name>/ config + Excel->JSON rules)
    rules_excel  Excel rule sheet -> JSON converter
    chunking     configurable chunker (Chinese page markers / headings / windows)
    retrieval    configurable scoring retrieval
    prompts      render global / per-chunk prompts from external templates
    llm          LLM calls (OpenAI-compatible) + robust JSON parsing
    extract      per-metric extraction + concurrency + Excel output
    convert      PDF->MD (MinerU cloud / optional local Docling)
    download     download adapters (cninfo; A-share / H-share editions)
    pipeline     top-level run_profile()
"""
from __future__ import annotations

__all__ = ["load_profile", "run_profile", "list_profiles"]


def load_profile(name: str):
    from engine.profile import load_profile as _lp
    return _lp(name)


def list_profiles():
    from engine.profile import list_profiles as _ls
    return _ls()


def run_profile(*args, **kwargs):
    from engine.pipeline import run_profile as _rp
    return _rp(*args, **kwargs)
