# LLM Module (`llm`)

A standalone, production-grade LLM orchestration module powered by **OpenRouter** using the **Space Bunny Alpha** (`stealth/space-bunny-alpha`) model.

Built to conform strictly with **Sections 18–20** of [`Design_Infrastructure_Platform.md`](../Design_Infrastructure_Platform.md).

> **Note on Workspace Separation:**  
> This module is intentionally decoupled and housed entirely within `llm/` so it does not interfere with or collide with concurrent modifications happening in `src/` or `tests/`.

---

## 1. Architectural Role (Sections 18 & 20)

As specified in the technical architecture, the LLM is an **interpretation and orchestration component**, *never* the authoritative forecasting or safety engine.

### Permitted Operations (Section 18)
1. **Unstructured Extraction (`extract_claims`)**: Converts raw news, social, and agency text into structured claims adhering to the predicate whitelist.
2. **Semantic Event Resolution (`resolve_event`)**: Conservatively evaluates whether an incoming observation belongs to an existing active event or initiates a new event entity.
3. **Dynamic Orchestration (`select_capabilities`)**: Cost-aware planner that evaluates state deltas and recommends analytical capabilities (`cheap` vs `medium` vs `expensive`).
4. **Hypothesis Generation (`hypothesize_infrastructure`)**: Suggests infrastructure relationships (transit routes, arterial corridors, bridges) deserving deterministic validation.
5. **Evidence Synthesis (`synthesize_evidence`)**: Structures multi-source telemetry, maintaining clear separation between *Confirmed Facts*, *Reported Claims*, and *Inferred Points*.
6. **User Communication (`explain_to_user`)**: Produces calm, structured advisories addressing:
   - **WHAT** changed
   - **WHY** it matters
   - **WHAT** evidence supports it
   - **WHAT** uncertainty remains

### Non-Negotiable Guardrails (Section 20)
Enforced deterministically by [`guardrails.py`](./guardrails.py):
- **Never declares evacuations** or invents evacuation destinations.
- **Never overrides official emergency directives** (always refers to AlertSeattle / WSDOT).
- **Never asserts authoritative road closures** without verified agency confirmation.
- **Never invents unverified current facts**.
- **Never assigns criminal intent** or categorizes political groups as inherently dangerous.
- **Enforces Predicate Whitelisting**: Rejects non-registered or forbidden predicates (`emergency.evacuation`, `group.dangerousness`, etc.).

---

## 2. Directory Structure

```text
llm/
├── __init__.py           # Exports LlmClient, config, schemas, guardrails
├── config.py             # OpenRouter & endpoint settings loader (.env aware)
├── client.py             # Sync & Async LLM client implementation
├── guardrails.py         # Section 20 non-negotiable safety rules & filters
├── schemas.py            # Pydantic structured output models
├── prompts.py            # Task-specific system prompts & JSON formatting
├── mock_responses.py     # Deterministic offline mock engine
├── cli.py                # Command-line interface for testing & verification
├── test_llm.py           # Unit & integration test suite (pytest)
├── .env                  # Active credentials and settings (gitignored)
├── .env.example          # Sample environment configuration
└── README.md             # This documentation
```

---

## 3. Configuration & Environment

Copy `.env.example` to `.env` or set the environment variables:

```bash
cp llm/.env.example llm/.env
```

| Variable | Default | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | *(None)* | Your OpenRouter API key |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenRouter API base URL |
| `OPENROUTER_MODEL` | `stealth/space-bunny-alpha` | Model identifier |
| `LLM_MOCK` | `false` (or `true` if no key) | Set `true` to force offline mock responses without making network requests |
| `OPENROUTER_SITE_URL` | `https://github.com/infraimpact` | OpenRouter `HTTP-Referer` header |
| `OPENROUTER_APP_NAME` | `InfraImpact Intelligence Platform` | OpenRouter `X-Title` header |
| `LLM_TIMEOUT` | `35.0` | HTTP timeout in seconds |
| `LLM_TEMPERATURE` | `0.0` | Sampling temperature (0.0 for structured JSON) |

---

## 4. Quick Start & Verification

### Check Status
```powershell
.venv\Scripts\python.exe -m llm.cli status
```

### Run Live End-to-End Verification
```powershell
.venv\Scripts\python.exe -m llm.cli test --live
```

### Extract Claims via CLI
```powershell
.venv\Scripts\python.exe -m llm.cli extract "Demonstration of 400 gathered near 4th and Pine moving north"
```

### Run Pytest Suite
```powershell
.venv\Scripts\pytest llm\test_llm.py -v
```

---

## 5. Python API Usage

```python
from llm import LlmClient

client = LlmClient()

# 1. Claim extraction
claims = client.extract_claims(
    text="SDOT reports peaceful demonstration moving north along 4th Ave.",
    observation_id="obs_101",
)
print(claims["claims"])

# 2. Capability selection
recommendations = client.select_capabilities(
    state_summary={"active_events": 1},
    delta_summary={"movement": "northbound on 4th Ave"},
    registry=[{"capability": "road_network_exposure", "cost": "cheap"}],
)
print(recommendations["recommended_capabilities"])

# 3. User explanation
explanation = client.explain_to_user({
    "event": "Downtown demonstration",
    "corridor": "4th Ave",
    "transit_impact": "15-minute delays on lines 2 and 4",
})
print(explanation["headline"])
```
