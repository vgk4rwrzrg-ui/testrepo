# Centralized LLM Router Documentation

## Overview
The LLM Router is a centralized gateway for all Large Language Model interactions within the application. Instead of calling provider SDKs directly, every request passes through the `LLMRouter`, which dynamically selects the most appropriate model based on the task requirements, available capabilities, and model health.

## Architecture
The router implements a Role-Based selection system to balance cost, latency, and quality:

- **Fast**: Small/cheap models. Best for classification, extraction, and short chat.
- **Standard**: The "workhorse" models. Best for analysis over provided context (RAG) and document processing.
- **Deep**: Reasoning models. Best for multi-step logic, complex tool use, and hard problems.

### Components
- `LLMRouter`: The main entry point. Handles request parsing, routing decisions, adapter invocation, and response validation.
- `OpenAICompatAdapter`: The current adapter implementation for OpenAI-compatible APIs.
- `RoutingRequest` & `RoutingDecision`: Typed objects ensuring consistent data flow.

## Configuration
Configuration is managed via Django settings (`LLM_CONFIG`). Each role (`fast`, `standard`, `deep`) should be defined with:
- `model`: The model ID (e.g., `gpt-4o-mini`).
- `adapter`: The adapter type (default: `openai_compat`).
- `enabled`: Boolean to toggle the role.
- `supports`: A dictionary of capabilities (`tools`, `images`, `json_mode`, `streaming`).
- `context_window_tokens`: Total token limit for the model.

## Routing Policy
Requests are routed based on the following priority sequence:

1. **Capability Filter**: Roles that do not support required features (e.g., Tool use, Images) are excluded.
2. **Caller Override**: If `model_role` is provided in the request, the router attempts to use that specific role.
3. **Priority Override**: 
   - `latency` $\rightarrow$ prefers `fast` $\rightarrow$ `standard` $\rightarrow$ `deep`.
   - `quality` $\rightarrow$ prefers `deep` $\rightarrow$ `standard` $\rightarrow$ `fast`.
4. **Task-Type Routing**: Based on the `task_type` provided:
   - `classification`, `extraction`, `chat` $\rightarrow$ `fast` first.
   - `analysis`, `document` $\rightarrow$ `standard` first.
   - `reasoning` $\rightarrow$ `deep` first.
5. **Complexity Signals**: If `tools` are requested, the router automatically prefers the `deep` role.
6. **Circuit Breaker**: If a role fails $N$ times within a window, it is temporarily demoted to prevent cascading failures.

## API Usage

### 1. Non-Streaming Chat (`router.chat`)
Returns a standard `JsonResponse`.

**Request Body (JSON):**
| Field | Type | Description |
|-------|------|-------------|
| `message` | String | The current user prompt. |
| `history` | List[Dict] | Previous messages: `[{"role": "user", "content": "..."}, ...]` |
| `system_prompt`| String | Optional. Custom system instruction. |
| `task_type` | String | Optional. `chat`, `analysis`, `document`, `extraction`, `classification`, `reasoning`. |
| `priority` | String | Optional. `latency`, `balanced`, `quality`. |
| `temperature` | Float | Optional. Default `0.0`. |
| `json_mode` | Boolean | Optional. Forces JSON output. |
| `max_tokens` | Integer | Optional. Caps the output length. |
| `tools` | List | Optional. List of tool definitions. |
| `model_role` | String | Optional. Explicit override (`fast`, `standard`, `deep`). |

**Response Format:**
```json
{
  "status": "success",
  "answer": "The response text",
  "usage": { "prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30 },
  "routing": { "role": "standard", "model": "...", "reason": "TASK_ANALYSIS", "elapsed_s": 1.2, "tokens_per_s": 16.7 }
}
```

### 2. Streaming Chat (`router.stream`)
Returns a `StreamingHttpResponse` using Server-Sent Events (SSE).

**SSE Event Types:**
- `event: routing`: Sent first. Contains `{"role", "model", "reason"}`.
- `event: delta`: Sent repeatedly. Contains `{"text": "chunk"}`.
- `event: done`: Sent last. Contains `{"elapsed_s", "ttft_s", "approx_tokens"}`.
- `event: error`: Sent on failure. Contains `{"msg": "error message"}`.

## Implementation Example

```python
from llm.router import router

# In a Django view
def my_llm_view(request):
    # The router expects the HttpRequest object to parse the JSON body
    return router.chat(request) 
    # OR for streaming:
    # return router.stream(request)
```

## Validation and Escalation
The router validates that responses are not empty and conform to `json_mode` if requested. If a selected role fails or returns invalid content, the router automatically **escalates** the request to the next higher tier (e.g., `fast` $\rightarrow$ `standard` $\rightarrow$ `deep`) to ensure a successful response.

## Setup for a New Django Project

To integrate the LLM Router into a new Django project, follow these steps:

### 1. Installation and Configuration
1. **Add the App**: Add `'llm'` to your `INSTALLED_APPS` in `settings.py`.
2. **Configure Models**: Define the `LLM_CONFIG` dictionary in your `settings.py`. Example:
   ```python
   LLM_CONFIG = {
       'fast': {
           'model': 'gpt-4o-mini',
           'enabled': True,
           'supports': {'tools': True, 'images': False, 'json_mode': True},
           'context_window_tokens': 128000,
       },
       'standard': {
           'model': 'gpt-4o',
           'enabled': True,
           'supports': {'tools': True, 'images': True, 'json_mode': True},
           'context_window_tokens': 128000,
       },
       'deep': {
           'model': 'o1-preview',
           'enabled': True,
           'supports': {'tools': False, 'images': False, 'json_mode': False},
           'context_window_tokens': 128000,
       },
   }
   ```
3. **Routing URLs**: Include the LLM router URLs in your project's main `urls.py`:
   ```python
   from django.urls import path, include
   urlpatterns = [
       path('api/llm/', include('llm.urls')),
   ]
   ```
4. **Database Setup**: Run migrations to create necessary tables for session tracking:
   ```bash
   python manage.py migrate llm
   ```
5. **Environment Variables**: Ensure your API keys (e.g., `OPENAI_API_KEY`) are set in your environment.

### 2. Usage Example
You can now use the `router` object directly in your views to handle LLM interactions without worrying about specific model selection.

```python
from llm.router import router
from django.http import JsonResponse

def analyze_data_view(request):
    # The router parses the request body and routes to the best model
    # based on the 'task_type' or 'priority' provided in the JSON payload.
    return router.chat(request)
```
