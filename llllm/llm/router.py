import json
import time
import logging
import requests
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

from django.conf import settings
from django.http import HttpRequest, JsonResponse, StreamingHttpResponse

logger = logging.getLogger(__name__)
RETRYABLE_STATUS = {429, 502, 503}

# =========================================================================
# Request / Decision models
# =========================================================================

@dataclass
class RoutingRequest:
    messages: List[Dict[str, str]]
    system_prompt: str = "You are a helpful assistant."
    task_type: str = "chat"            # chat | analysis | document | extraction | classification
    priority: str = "balanced"          # latency | balanced | quality
    temperature: float = 0.0
    json_mode: bool = False
    max_tokens: Optional[int] = None
    tools: Optional[List[Dict]] = None
    attachments: List[Any] = field(default_factory=list)

    @property
    def input_chars(self) -> int:
        return len(self.system_prompt or "") + sum(
            len(m.get("content") or "") for m in self.messages
        )

    @property
    def has_images(self) -> bool:
        return any(getattr(a, "content_type", "").startswith("image/") for a in self.attachments)


@dataclass
class RoutingDecision:
    role: str
    model: str
    reason_code: str
    reasons: List[str] = field(default_factory=list)
    max_tokens: Optional[int] = None

# =========================================================================
# Router
# =========================================================================

class LLMRouter:
    """
    Routes requests across model roles defined in settings.LLM_CONFIG.

    Roles (all optional except one must exist):
      fast      - small/cheap model: classification, extraction, short chat
      standard  - workhorse: analysis over provided context (RAG), documents
      deep      - reasoning model: multi-step logic, tool use, hard problems

    Routing order:
      1. Capability filter (tools / images / json / streaming / context window)
      2. Explicit caller override (`model_role`)
      3. Priority override (`latency` / `quality`)
      4. Task-type routing table
      5. Complexity signals (tools, output structure) — NOT raw input length
      6. Circuit breaker demotion if a role is failing
    """

    # Task type -> preferred role order (first eligible wins)
    TASK_ROUTES = {
        "classification": ["fast", "standard", "deep"],
        "extraction":     ["fast", "standard", "deep"],
        "chat":           ["fast", "standard", "deep"],
        "analysis":       ["standard", "deep", "fast"],   # RAG summarization: workhorse, not reasoner
        "document":       ["standard", "deep", "fast"],
        "reasoning":      ["deep", "standard", "fast"],
    }

    # Sensible output caps per task so slow generations are bounded
    DEFAULT_MAX_TOKENS = {
        "classification": 256,
        "extraction": 1024,
        "chat": 2048,
        "analysis": None,      # ← uncapped: depth matters here
        "document": None,      # ← uncapped
        "reasoning": 8192,
    }

    # Circuit breaker: demote a role after N failures within WINDOW seconds
    CB_FAILURES = 3
    CB_WINDOW = 120

    def __init__(self):
        self.config = getattr(settings, "LLM_CONFIG", {})
        if not self.config:
            raise RuntimeError("LLM_CONFIG not found in Django settings.")

        self.adapters: Dict[str, Any] = {}
        for role in ("fast", "standard", "deep"):
            role_cfg = self.config.get(role)
            if role_cfg and role_cfg.get("enabled", True):
                self.adapters[role] = self._build_adapter(role_cfg)

        if not self.adapters:
            raise RuntimeError("LLM_CONFIG defines no enabled roles (fast/standard/deep).")

        self._failures: Dict[str, List[float]] = {r: [] for r in self.adapters}

    def _build_adapter(self, role_cfg: Dict) -> Any:
        adapter_name = role_cfg.get("adapter", "openai_compat")
        if adapter_name == "openai_compat":
            from llm.adapters.openai_compat import OpenAICompatAdapter
            # Inject global SSL verification setting if not explicitly in role_cfg
            role_cfg = role_cfg.copy()
            if "verify" not in role_cfg:
                role_cfg["verify"] = getattr(settings, "LLM_VERIFY_SSL", True)
            return OpenAICompatAdapter(role_cfg)
        raise RuntimeError(f"Unknown adapter type: {adapter_name}")

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------

    def _record_failure(self, role: str):
        now = time.time()
        self._failures[role] = [t for t in self._failures[role] if now - t < self.CB_WINDOW]
        self._failures[role].append(now)

    def _is_healthy(self, role: str) -> bool:
        now = time.time()
        recent = [t for t in self._failures.get(role, []) if now - t < self.CB_WINDOW]
        return len(recent) < self.CB_FAILURES

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def _eligible_roles(self, req: RoutingRequest, need_stream: bool = False) -> List[str]:
        eligible = []
        for role, cfg in ((r, self.config[r]) for r in self.adapters):
            supports = cfg.get("supports", {})
            if req.tools and not supports.get("tools", False):
                continue
            if req.has_images and not supports.get("images", False):
                continue
            if req.json_mode and not supports.get("json_mode", True):
                continue
            if need_stream and not supports.get("streaming", True):
                continue
            # Context window check: ~4 chars/token heuristic, keep 20% headroom
            ctx_window = cfg.get("context_window_tokens", 128_000)
            if req.input_chars / 4 > ctx_window * 0.8:
                continue
            eligible.append(role)
        return eligible

    def _route(self, req: RoutingRequest, need_stream: bool = False,
               forced_role: Optional[str] = None) -> RoutingDecision:
        eligible = self._eligible_roles(req, need_stream)
        if not eligible:
            raise RuntimeError("No eligible LLM roles for this request's capabilities.")

        healthy = [r for r in eligible if self._is_healthy(r)] or eligible

        max_tokens = req.max_tokens or self.DEFAULT_MAX_TOKENS.get(req.task_type, 2000)

        def decide(role: str, code: str, why: str) -> RoutingDecision:
            return RoutingDecision(
                role=role,
                model=self.config[role]["model"],
                reason_code=code,
                reasons=[why],
                max_tokens=max_tokens,
            )

        # 2. Explicit caller override
        if forced_role:
            if forced_role in healthy:
                return decide(forced_role, "CALLER_OVERRIDE", f"Caller forced role={forced_role}.")
            logger.warning("Forced role %s not eligible/healthy; falling through.", forced_role)

        # 3. Priority override
        if req.priority == "latency":
            for role in ("fast", "standard", "deep"):
                if role in healthy:
                    return decide(role, "PRIORITY_LATENCY", "Caller requested low latency.")
        if req.priority == "quality":
            for role in ("deep", "standard", "fast"):
                if role in healthy:
                    return decide(role, "PRIORITY_QUALITY", "Caller requested high quality.")

        # 5. Complexity signals that genuinely need the deep model.
        #    NOTE: raw input length is deliberately NOT a signal — long RAG
        #    context is not the same as hard reasoning.
        if req.tools and "deep" in healthy:
            return decide("deep", "TOOLS_REQUIRED", "Tool use requested.")

        # 4. Task-type routing table
        for role in self.TASK_ROUTES.get(req.task_type, ["standard", "fast", "deep"]):
            if role in healthy:
                return decide(role, f"TASK_{req.task_type.upper()}",
                              f"Task '{req.task_type}' routed by table.")

        return decide(healthy[0], "FALLBACK", f"Fallback to first healthy role: {healthy[0]}.")

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate(self, response: Dict, req: RoutingRequest) -> Tuple[bool, Optional[str]]:
        if not response or not response.get("choices"):
            return False, "Empty response"
        message = response["choices"][0]["message"]
        if message.get("tool_calls"):          # ← tool calls are a valid outcome
            return True, None
        content = message.get("content") or ""
        if not content:
            return False, "Missing content"
        if req.json_mode:
            try:
                json.loads(content)
            except json.JSONDecodeError:
                return False, "Invalid JSON"
        return True, None

    def _escalation_role(self, current: str) -> Optional[str]:
        order = ["fast", "standard", "deep"]
        try:
            idx = order.index(current)
        except ValueError:
            return None
        for nxt in order[idx + 1:]:
            if nxt in self.adapters and self._is_healthy(nxt):
                return nxt
        return None

    def _call_adapter(self, role: str, req, max_tokens, attempts: int = 4):
        """Call with backoff on rate limits — a 429 is not a model failure."""
        delay = 2.0
        for attempt in range(attempts):
            try:
                return self.adapters[role].call(req, max_tokens=max_tokens)
            except requests.HTTPError as e:
                status = getattr(e.response, "status_code", None)
                if status not in RETRYABLE_STATUS or attempt == attempts - 1:
                    raise
                # Respect Retry-After if the proxy sends it
                retry_after = e.response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else delay
                logger.warning("LLM %s from %s — retrying in %.1fs (attempt %d/%d)",
                            status, role, wait, attempt + 1, attempts)
                time.sleep(min(wait, 30))
                delay *= 2
    def generate(self, system_prompt: str, user_prompt: str, task_type: str = "analysis") -> str:
        """
        Internal programmatic entry point to generate a response without an HttpRequest.
        """
        req = RoutingRequest(
            messages=[{"role": "user", "content": user_prompt}],
            system_prompt=system_prompt,
            task_type=task_type
        )
        return self.process_request(req)["choices"][0]["message"].get("content", "")

    def process_request(self, req: RoutingRequest) -> Dict:
        """
        Public programmatic entry point to route and call an LLM.
        Returns the raw response dictionary.
        """
        decision = self._route(req)
        response, _ = self._call_with_escalation(req, decision)
        return response

    # ------------------------------------------------------------------
    # Shared request parsing
    # ------------------------------------------------------------------

    def _parse(self, http_request: HttpRequest) -> Tuple[RoutingRequest, Optional[str]]:
        body = json.loads(http_request.body)
        req = RoutingRequest(
            messages=body.get("history", []) + [{"role": "user", "content": body.get("message", "")}],
            system_prompt=body.get("system_prompt", "You are a helpful assistant."),
            task_type=body.get("task_type", "chat"),
            priority=body.get("priority", "balanced"),
            temperature=body.get("temperature", 0.0),
            json_mode=body.get("json_mode", False),
            max_tokens=body.get("max_tokens"),
            tools=body.get("tools"),
        )
        return req, body.get("model_role")   # optional explicit override

    # ------------------------------------------------------------------
    # Non-streaming entry point (backwards compatible)
    # ------------------------------------------------------------------

    def chat(self, http_request: HttpRequest) -> JsonResponse:
        try:
            req, forced_role = self._parse(http_request)
            decision = self._route(req, need_stream=False, forced_role=forced_role)

            t0 = time.perf_counter()
            response, decision = self._call_with_escalation(req, decision)
            elapsed = time.perf_counter() - t0

            valid, error = self._validate(response, req)
            if not valid:
                return JsonResponse({"status": "error", "msg": f"Validation failed: {error}"}, status=502)

            answer = response["choices"][0]["message"]["content"]
            usage = response.get("usage", {})
            out_tokens = usage.get("completion_tokens", 0)
            tps = out_tokens / elapsed if elapsed > 0 and out_tokens else 0

            logger.info(
                "LLM: role=%s model=%s reason=%s task=%s in=%s out=%s time=%.2fs tps=%.1f",
                decision.role, decision.model, decision.reason_code, req.task_type,
                usage.get("prompt_tokens", "?"), out_tokens, elapsed, tps,
            )

            return JsonResponse({
                "status": "success",
                "answer": answer,
                "usage": {
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": out_tokens,
                    "total_tokens": usage.get("total_tokens", 0),
                },
                "routing": {
                    "role": decision.role,
                    "model": decision.model,
                    "reason": decision.reason_code,
                    "elapsed_s": round(elapsed, 2),
                    "tokens_per_s": round(tps, 1),
                },
            })

        except Exception as e:
            logger.exception("LLM Router error")
            return JsonResponse({"status": "error", "msg": str(e)}, status=500)

    def _call_with_escalation(self, req: RoutingRequest,
                              decision: RoutingDecision) -> Tuple[Dict, RoutingDecision]:
        """Call the selected role; on failure/invalid output, escalate one tier.
        Rate limits (429) are retried with backoff, not treated as failures."""
        try:
            response = self._call_adapter(decision.role, req, decision.max_tokens)
            valid, error = self._validate(response, req)
            if valid:
                return response, decision
            raise RuntimeError(f"Invalid response from {decision.role}: {error}")
        except Exception as first_err:
            status = getattr(getattr(first_err, "response", None),
                             "status_code", None)
            if status != 429:
                self._record_failure(decision.role)
            nxt = self._escalation_role(decision.role)
            if not nxt:
                raise
            logger.warning("Escalating %s -> %s (%s)", decision.role, nxt, first_err)
            decision.role, decision.model = nxt, self.config[nxt]["model"]
            decision.reason_code = "ESCALATED"
            response = self._call_adapter(nxt, req, decision.max_tokens)
            return response, decision

    # ------------------------------------------------------------------
    # Streaming entry point (SSE)
    # ------------------------------------------------------------------

    def stream(self, http_request: HttpRequest) -> Any:
        """
        Server-Sent Events stream. Emits:
          event: routing  -> {"role","model","reason"}      (first)
          event: delta    -> {"text": "..."}                 (many)
          event: done     -> {"usage": {...}, "elapsed_s"}   (last)
          event: error    -> {"msg": "..."}                  (on failure)
        """
        try:
            req, forced_role = self._parse(http_request)
        except Exception as e:
            return JsonResponse({"status": "error", "msg": str(e)}, status=400)

        router = self

        def sse(event: str, data: dict) -> str:
            return f"event: {event}\ndata: {json.dumps(data)}\n\n"

        def event_stream() -> Iterator[str]:
            t0 = time.perf_counter()
            try:
                decision = router._route(req, need_stream=True, forced_role=forced_role)
                yield sse("routing", {"role": decision.role, "model": decision.model,
                                      "reason": decision.reason_code})

                adapter = router.adapters[decision.role]
                out_tokens = 0
                first_token_at = None

                try:
                    for chunk in adapter.stream(req, max_tokens=decision.max_tokens):
                        if first_token_at is None:
                            first_token_at = time.perf_counter() - t0
                        out_tokens += 1  # approx: 1 chunk ~ 1 token for most providers
                        yield sse("delta", {"text": chunk})
                except Exception as stream_err:
                    # Mid-stream failure: try one escalation ONLY if nothing was sent yet
                    router._record_failure(decision.role)
                    if out_tokens == 0:
                        nxt = router._escalation_role(decision.role)
                        if nxt:
                            logger.warning("Stream escalating %s -> %s (%s)",
                                           decision.role, nxt, stream_err)
                            yield sse("routing", {"role": nxt,
                                                  "model": router.config[nxt]["model"],
                                                  "reason": "ESCALATED"})
                            for chunk in router.adapters[nxt].stream(
                                    req, max_tokens=decision.max_tokens):
                                if first_token_at is None:
                                    first_token_at = time.perf_counter() - t0
                                out_tokens += 1
                                yield sse("delta", {"text": chunk})
                        else:
                            raise
                    else:
                        raise  # partial output already sent; surface the error

                elapsed = time.perf_counter() - t0
                logger.info("LLM stream: role=%s ttft=%.2fs total=%.2fs ~tokens=%d",
                            decision.role, first_token_at or -1, elapsed, out_tokens)
                yield sse("done", {"elapsed_s": round(elapsed, 2),
                                   "ttft_s": round(first_token_at or 0, 2),
                                   "approx_tokens": out_tokens})

            except Exception as e:
                logger.exception("LLM stream error")
                yield sse("error", {"msg": str(e)})

        resp = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
        resp["Cache-Control"] = "no-cache"
        resp["X-Accel-Buffering"] = "no"   # required behind nginx or chunks get buffered
        return resp


router = LLMRouter()