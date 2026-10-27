# Solva RAG service

Python port of `RAG_Solva2_v2.ipynb` (hybrid dense + BM25 search, reranker, Gemini with citations),
served over HTTP for the Next.js copilot. It replaces the n8n `rag-agent` webhook.

## Run

```bash
cd rag_service
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # then set GEMINI_API_KEY
# put the course PDFs in ./documents (same ones used in the notebook)
uvicorn main:app --port 8001
```

First start downloads the embedding and reranker models and indexes `documents/`
(later starts only re-index new or changed files).

## Endpoints

- `POST /ask` `{ "chatInput": "...", "sessionId": "..." }` -> `{ "answer", "sources", "queries" }`
- `POST /ingest` (multipart `file`) adds a PDF/TXT/MD and indexes it
- `GET /health`

## Multi-agent pipeline (LangGraph) — `pipeline/`

`POST /api/pipeline/audit-and-risk` `{ "portfolio": { valuation_date, source, positions[] }, "solvency": { scr, mcr, eligibleFunds, ratio?, modules[], details{} } }`

```
START -> data_quality (rules, Art. 82) -> risk_analyst (Gemini, structured output) -> END
                      \-> BLOCKED (critical anomalies): deterministic alert, no LLM call -> END
```

- `pipeline_status`: `COMPLETED` | `BLOCKED` | `ANALYSIS_FAILED` (audit done, LLM unavailable: details in `errors`).
- Model: `PIPELINE_LLM_MODEL` (default `gemini-3.5-flash`), fallback `PIPELINE_FALLBACK_MODEL` (default `gemini-3-flash-preview`).
  `gemini-1.5-flash` is retired by Google and returns 404.
- CORS origins: `CORS_ORIGINS` (default: Next.js on localhost:3000/3001).
- Tests (no network, stubbed LLM): `python -m pytest pipeline/tests -q`

## PDF import — `extraction/`

`POST /api/extract/qrt-pdf` (multipart `file`) -> `{ sheets, method, pages, rows, warnings, model }`

The PDF counterpart of the Excel import: `sheets` has exactly the shape of the Excel parser, so the audit, SCR engine, analyst
and report downstream are unchanged.

- `method: "tables"`: ruled tables of a text PDF are read with PyMuPDF (deterministic, no LLM). Tables split over two pages and
  a single row spilling onto the next page are reassembled.
- `method: "vision"`: scanned PDFs / no detectable table -> Gemini reads the document. Flagged in `warnings`; values must be checked.
- Models: `EXTRACTION_MODEL` (default `gemini-3.5-flash`), `EXTRACTION_FALLBACK_MODEL`; key `EXTRACTION_API_KEY` (falls back to `GEMINI_API_KEY`).
- Limits: 15 MB, 40 pages. Tests: `python -m pytest extraction/tests -q`

## Expert EIOPA & QRT agent — `eiopa_agent/`

`POST /api/eiopa-agent/run` `{ "question", "own_funds": { items[{name, tier, amount, kind}], scr, mcr }, "solvency"? }`
-> `{ status, answer, rule_confirmed, evidence_ids, sources[], eligibility, file{download_url}, tool_calls[] }`;
`GET /api/eiopa-agent/files/{name}` downloads the generated workbook.

A tool-calling agent (LangChain `create_agent` on LangGraph) with three tools:

| Tool | Role |
|---|---|
| `search_eiopa_vector_db` | RAG over the indexed documents (hybrid + reranker), numbered passages with source, page, relevance |
| `check_tier_limits` | eligible own funds and Tier limits, computed in Python (same rules and rounding as the n8n engine) |
| `generate_excel_qrt` | S.23.01 / S.25.01 workbook written with openpyxl into `generated/`, re-importable by the app |

The LLM never types a number: the portfolio is read from the request, figures are computed by the tools, and sources, eligibility
and file come from the tools' own state. `rule_confirmed` is the agent's declared verdict, checked against the passages it cites
(relevance >= 0.30): a retrieval score alone is not a confirmation. If the LLM is down the deterministic `eligibility` is still returned.

- Models: `EIOPA_AGENT_MODEL` / `EIOPA_AGENT_FALLBACK_MODEL`; keys tried in turn: `EIOPA_AGENT_API_KEY`, `GEMINI_API_KEY`, `INVESTIGATOR_API_KEY` (each x both models; a key/model that answers 429 is skipped for 10 min; the response reports `key_source`, never the key).
- Scope: quantitative limits only. Qualitative criteria (subordination, duration...) are not checked, and the answer says so.
- The agent can only cite what is indexed: add the regulation / EIOPA guidelines PDFs to `documents/` (indexed automatically at startup).
- Tests: `python -m pytest eiopa_agent/tests -q`

### Generated workbook = the project's reference QRT layout

generate_excel_qrt writes the two INPUT sheets exactly as in `qrt_pieuvre_scr/*.xlsx` (`S2501_SCR`: Code EIOPA | Rubrique | Brut (C0040) |
Net (C0030) | Unité, and `Fonds_Propres_Ratios`: Code EIOPA | Rubrique | Valeur | Unité), so "Importer QRT" -> "Calculer" (and the n8n engine)
compute it like any QRT. The result sheets of the reference files (Pieuvre_SCR, Synthese, Matrices_Correlation) are produced by the engine
itself and are not written. `solvency` (`modules` [{name, value, brut?}], `details` {intangibles, operationalRisk, lacTp, lacDt}) and
`own_funds.mcr_linear` / `amcr` supply the S.25.01 / S.28.01 lines; without modules the file is flagged `computable: false`.
Verified by `eiopa_agent/tests/test_reference_roundtrip.py`: cell-by-cell fidelity with the reference file, and the real engine returns the same SCR.

## Climate & ESG Stress Tester — `climate_agent/`

`POST /api/climate-agent/run` `{ "question", "scr_nonlife" (M EUR), "horizon_years"? (default 5) }`
-> `{ status, answer, stress{threatened_pct_of_scr, threatened_scr_meur, scenario_loss_meur, by_class[], zones[]}, tool_calls[], model, key_source }`

ORSA (Pillar 2) climate stress test. A tool-calling agent (same key rotation / model fallback as the EIOPA agent) with three tools:

| Tool | Role |
|---|---|
| `query_portfolio_database` | one read-only SELECT on `insured_properties` (home insurance book: city, GPS, sum insured, flood cover). SQLite stands in for PostgreSQL (`portfolio_db.py`, seeded on first use, `CLIMATE_DB_PATH` to relocate) |
| `fetch_geospatial_weather_api` | real Open-Meteo APIs, no key: climate model (EC_Earth3P_HR) daily rain/wind over the horizon + GloFAS river discharge; neighbouring points (0.1 deg) share one call |
| `compute_climate_stress` | the % of the Non-Life SCR threatened, scenario loss, exposure by zone: computed in Python from the database and the API results |

Method (shown in the UI): policies with flood cover only. Per zone, the annual probability p of a damaging rain day (>= 40 mm, assumed threshold) comes from the
yearly maxima of a 15-year climate-model series (x 1.25 if rivers are already >= 10x their mean); over the horizon N it becomes 1-(1-p)^N, so **the result changes with the
horizon, the zone and the portfolio**. % of the Non-Life SCR threatened = sum(insured value x P) / covered insured value (SCR allocated pro rata); it does not depend on the SCR
(only the M EUR amounts do). Expected loss = that sum x an assumed 2 % damage ratio. A screening indicator, not a catastrophe model. The climate series is cached on disk
(`climate_agent/cache/`, projections do not change daily) which also spares the free Open-Meteo quota; a zone the API refuses is "non évalué" (never counted as safe).
`properties` in the request (or the CSV/Excel import in the UI) replaces the demo portfolio. If the LLM is down the deterministic stress is still returned.
Tests (no network): `python -m pytest climate_agent/tests -q`
