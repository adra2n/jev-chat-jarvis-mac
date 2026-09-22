"""Judge: local Laya model reads a Chinese message and returns intent + risk.

Laya is a typed-decision model that outputs calibrated probabilities for structured
questions. It's more efficient than decider-2b: no text generation, just probability
distributions over predefined options.

This module provides the same interface as judge.Judge but uses Laya as the backend.
"""

from __future__ import annotations

import threading
import time

import numpy as np

from judge import INTENTS, RISK_LEVELS, ACTION_MAP


class LayaJudge:
    """Wraps Laya's multilingual model for intent + risk classification.
    
    Unlike decider-2b which does text generation and reads logits, Laya directly
    outputs probability distributions for choice questions - much faster and cleaner.
    """

    def __init__(self, model: str = "multilingual", device: str | None = None):
        self.model = model
        self.device = device
        self._loaded = False
        self._router = None
        self._load_lock = threading.RLock()

    def _load(self):
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            from laya import Router
            self._router = Router(
                preload=False,
                device=self.device,
                default=self.model
            )
            self._loaded = True

    def warm(self) -> None:
        """Load the model and run one dummy forward to warm up."""
        with self._load_lock:
            self._load()
            self.judge("预热")

    def judge(self, message: str, context: str | None = None) -> dict:
        """Classify intent and risk for a message.
        
        Returns the same dict format as judge.Judge.judge():
        - intent: str (one of INTENTS keys)
        - confidence: float
        - intent_probs: dict[str, float]
        - risk: float (0-9)
        - risk_probs: dict[str, float]
        - actions: list[str]
        - message: str
        """
        self._load()
        
        # Build the state with context if provided
        state = f"{context}\n\n{message}" if context else message
        
        # Define questions for Laya
        questions = {
            "intent": {
                "type": "choice",
                "instructions": "这句话的真实意图是什么？",
                "criteria": INTENTS
            },
            "risk": {
                "type": "choice",
                "instructions": "如果直接回复这句话，风险有多大？",
                "criteria": {str(i): desc for i, desc in enumerate(RISK_LEVELS)}
            }
        }
        
        t0 = time.perf_counter()
        result = self._router.predict(
            {"text": state},
            questions
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        
        # Parse results
        answers = result.get("answers", {})
        
        # Intent
        intent_ans = answers.get("intent", {})
        intent = intent_ans.get("choice", "闲聊")
        if intent not in INTENTS:
            # Fallback: try to find a matching intent
            for name in INTENTS:
                if name in str(intent):
                    intent = name
                    break
            else:
                intent = "闲聊"
        
        confidence = float(intent_ans.get("confidence", 0.0))
        intent_probs = {k: float(v) for k, v in intent_ans.get("probabilities", {}).items()}
        
        # Risk
        risk_ans = answers.get("risk", {})
        risk_choice = risk_ans.get("choice", "0")
        try:
            risk = float(risk_choice)
        except (ValueError, TypeError):
            risk = 0.0
        
        risk_probs_raw = risk_ans.get("probabilities", {})
        risk_probs = {str(i): float(risk_probs_raw.get(str(i), 0.0)) 
                      for i in range(len(RISK_LEVELS))}
        
        return {
            "intent": intent,
            "confidence": confidence,
            "intent_probs": intent_probs,
            "risk": round(risk, 1),
            "risk_probs": risk_probs,
            "actions": ACTION_MAP.get(intent, []),
            "message": message,
            "backend": f"laya/{self.model}",
            "elapsed_ms": elapsed_ms,
        }

    def rank_candidates(self, message: str, intent: str,
                        candidates: list[str]) -> list[dict]:
        """Rank reply candidates by asking which one fits best.
        
        Uses Laya's choice question to rank candidates.
        """
        self._load()
        
        if not candidates:
            return []
        
        questions = {
            "best": {
                "type": "choice",
                "instructions": "哪一条回复最合适？",
                "criteria": {c: None for c in candidates}
            }
        }
        
        state = f"收到的消息：{message}\n判断出的意图：{intent}"
        
        result = self._router.predict(
            {"text": state},
            questions
        )
        
        answers = result.get("answers", {})
        best_ans = answers.get("best", {})
        probs = best_ans.get("probabilities", {})
        
        ranked = []
        for c in candidates:
            p = probs.get(c)
            if p is None:
                p = best_ans.get("confidence", 0.0) if best_ans.get("choice") == c else 0.0
            ranked.append({"text": c, "prob": float(p)})
        
        ranked.sort(key=lambda r: -r["prob"])
        return ranked


if __name__ == "__main__":
    import json
    import sys

    j = LayaJudge()
    msg = sys.argv[1] if len(sys.argv) > 1 else "这个需求你今天跟一下"
    
    print("正在加载 Laya 模型...")
    j.warm()
    
    t0 = time.perf_counter()
    result = j.judge(msg)
    elapsed = (time.perf_counter() - t0) * 1000
    
    print(json.dumps(result, ensure_ascii=False, indent=1))
    print(f"\n耗时 {elapsed:.0f}ms")
