# Bibliothèque de prompts — SolvaRisk

Projet : Advanced Deep Learning 2026-2027, *AI Automation* (Solvabilité II). Version de la bibliothèque : **v1.4**.
Chaque prompt est décrit par : rôle dans le système, technique, versions, cas d'échec **réellement observés**, correctif, avant/après.
Les prompts sont dans le code (fichier indiqué) ; ce document en est la référence versionnée. Quand aucune itération n'a eu lieu, c'est écrit (pas d'échec inventé).

| ID | Prompt | Fichier | Version | Technique principale |
|---|---|---|---|---|
| P1 | Réécriture de la question (RAG) | `rag_service/rag_core.py` | v1 | sortie structurée JSON, few-shot implicite (liste d'acronymes) |
| P2 | Réponse citée (RAG) | `rag_service/rag_core.py` | v1 | grounding + citations `[n]` + refus explicite |
| P3 | Analyste de risques | `rag_service/pipeline/agents/risk_analyst_agent.py` | **v3** | rôle, règles numérotées, sortie structurée Pydantic, garde-fous |
| P4 | Extraction PDF par vision | `rag_service/extraction/qrt_pdf.py` | v1 | sortie structurée, « null plutôt que deviner » |
| P5 | Extraction d'un PDF de synthèse | `rag_service/extraction/qrt_pdf.py` | v2 | sortie structurée, périmètre explicite |
| P6 | Agent Expert EIOPA & QRT | `rag_service/eiopa_agent/agent.py` | **v3** | agent à outils (ReAct), procédure obligatoire, honnêteté épistémique |
| P7 | Agent Climate & ESG | `rag_service/climate_agent/agent.py` | v1 | agent à outils, chiffres interdits au modèle |
| P8 | Prompts de développement | (historique de la conversation de travail) | — | voir §Annexe |

---

## P1 — Réécriture de la question (RAG)

- **Rôle** : transformer la question (et l'historique) en une question autonome + 2 variantes de recherche, pour que le retrieval hybride trouve les passages malgré les acronymes (SCR, BE, PSAP…).
- **Technique** : sortie JSON (`response_mime_type=application/json`), consigne de développer les acronymes (exemples fournis dans le prompt).
- **Texte (v1)** :

```
Tu aides un moteur de recherche documentaire sur Solvabilité 2 (assurance, actuariat, réassurance).
HISTORIQUE :
{history}
QUESTION : {q}

Réponds en JSON : {"standalone": "...", "variants": ["...", "..."]}
- standalone : la question reformulée de façon autonome en français (résous les pronoms et ellipses avec l'historique).
- variants : 2 reformulations courtes pour la recherche, avec un vocabulaire différent, acronymes développés
  (SCR = capital de solvabilité requis, MCR, BE = Best Estimate, PSAP, ORSA, FP = fonds propres...) et termes anglais utiles.
```
- **Échecs observés** : aucun sur ce prompt. En revanche l'appel LLM peut échouer (quota 429) : le code retombe sur la question brute (`rewrite_query`, `except` → `[question]`), ce qui dégrade le rappel sans casser le service.
- **Itérations** : aucune (v1). Mesure de l'apport de la réécriture : **non faite** (l'évaluation de retrieval utilise les requêtes brutes, voir `rag_service/eval/retrieval_evaluation.md`).

## P2 — Réponse citée (RAG)

- **Rôle** : produire la réponse du copilote uniquement à partir des extraits récupérés, avec citations.
- **Technique** : grounding strict, citations `[n]` imposées, cas partiel, **refus littéral** si rien ne répond.
- **Texte (v1)** :

```
Tu es un assistant pédagogique expert en Solvabilité 2, actuariat et réassurance (niveau Master).

RÈGLES
1. Réponds UNIQUEMENT à partir des EXTRAITS ci-dessous, sans connaissances extérieures.
2. Cite chaque affirmation avec le numéro de l'extrait entre crochets, ex. [1] ou [2][3].
3. Si les extraits ne couvrent la question que partiellement, réponds sur la partie couverte et dis précisément ce qui manque.
4. Si aucun extrait ne répond, réponds exactement : "Information non trouvée dans les documents."
5. Réponse claire et structurée, en français ; garde les sigles, formules et chiffres tels qu'ils apparaissent.

EXTRAITS
{context}

{history_block}QUESTION : {question}
RÉPONSE :
```
- **Échecs observés** : (1) Gemini recopie parfois la dernière ligne « RÉPONSE : » → nettoyé par regex en code. (2) Quand le seuil du reranker (0,05) ne retient aucun passage, le LLM n'est **pas appelé** : le refus est décidé par le code, pas par le prompt (plus fiable). (3) Erreur de la source : la diapositive 81 du cours IRA écrit « Ratio de couverture = SCR / Fonds propres éligibles » (inversé) ; le prompt, fidèle au corpus, la reproduirait — limite de la règle 1, non corrigée.
- **Itérations** : aucune sur le texte.

## P3 — Analyste de risques (« actuaire senior »)

- **Rôle** : rédiger le diagnostic ORSA (constats chiffrés, facteurs de risque, recommandations, plans d'atténuation) à partir des résultats de solvabilité et des indicateurs précalculés par le code.
- **Technique** : rôle + règles numérotées + **sortie structurée** (schéma Pydantic `RiskAnalysis`, `with_structured_output`) + garde-fous en code (`apply_guardrails`, `restore_elisions`) + repli de modèle.
- **Texte (v3)** : voir `SYSTEM_PROMPT` dans le fichier (9 règles). Extrait des règles ajoutées après les échecs :

```
6. Si la qualité des données est dégradée (statut WARNING), signale-le comme limite de l'analyse en citant les constats précis
   de `data_quality_findings` (jamais une recommandation générique sur la « qualité des données »).
7. Rédige en français correct, avec tous les accents et les apostrophes typographiques d'usage (l'organisme, d'après, qu'il, à, é, è),
   en phrases simples, sans aucune mise en forme Markdown.
9. Si `details.source` vaut "reported", les chiffres viennent d'un document de synthèse publié et n'ont pas été recalculés par la formule
   standard : mentionne-le comme limite de vérification, mais ce n'est PAS un défaut de qualité des données.
```
- **Échecs observés et correctifs** :

| # | Échec observé | Correctif | Avant → après |
|---|---|---|---|
| 1 | Recommandation générique « améliorer la qualité des données » sans lien avec les anomalies réelles | règle 6 (citer les constats précis) | v1 : conseil générique sur la qualité des données → v2 : cite les constats précis de l'agent d'audit |
| 2 | Texte français dégradé : apostrophes et accents supprimés (« lorganisme », « daprès ») | règle 7 + **nouvel essai automatique** si le texte est dégradé + réparation des élisions en code (`restore_elisions`) | exemple type : « lorganisme est conforme » → « l'organisme est conforme » |
| 3 | PDF de synthèse (chiffres publiés) : l'audit recalculait un SCR factice (10 075,96 au lieu des chiffres publiés) et le modèle concluait sur ce faux SCR | règle 9 + mode « synthèse » (`document_kind="summary"`, contrôles de cohérence au lieu d'un recalcul) + garde en code | v2 : SCR recalculé erroné → v3 : chiffres publiés repris, limite de vérification signalée |

- **Tests** : `pipeline/tests` (modèle simulé) vérifient les garde-fous, le repli de modèle et la détection de texte dégradé.

## P4 — Extraction de QRT par vision

- **Rôle** : lire un PDF scanné / sans tableau détectable et en extraire chaque ligne chiffrée (code, libellé, net, brut).
- **Technique** : PDF envoyé à Gemini (multimodal) + **sortie structurée** (schéma `ExtractedQrt`), température 0.
- **Texte (v1)** : voir `VISION_PROMPT` (règles : recopier les codes sans en inventer, pas de conversion d'unité, parenthèses = négatif, `null` si illisible, liste vide si aucun tableau).
- **Échecs observés** : sur ce prompt, aucun test en conditions réelles sur un PDF scanné (la voie principale pour les PDF texte est déterministe, par tableaux PyMuPDF) ; les règles sont **préventives**. Le résultat est marqué `method: "vision"` avec avertissement « à vérifier ».
- **Itérations** : aucune.

## P5 — Extraction d'un PDF de synthèse

- **Rôle** : relever uniquement les chiffres **publiés** (SCR, fonds propres éligibles, MCR, ratios, BSCR, diversification, modules) d'un rapport prudentiel.
- **Technique** : sortie structurée, périmètre explicite (« uniquement ce qui est explicitement publié »).
- **Texte** : voir `SUMMARY_PROMPT`.
- **Échec observé (cause de la v2)** : sur « Rapport Prudentiel — Assureur Mixte Solide.pdf », la v1 de la chaîne traitait le PDF comme des QRT et faisait recalculer un SCR faux. La v2 sépare le cas « synthèse » du cas « QRT ».

## P6 — Agent Expert EIOPA & QRT

- **Rôle** : vérifier les limites de fonds propres par Tier à partir de la base documentaire et générer un état QRT Excel calculable par l'agent SCR.
- **Technique** : **agent à outils** (`create_agent`, boucle ReAct) ; procédure obligatoire (recherche d'abord) ; règles d'honnêteté ; les outils portent les chiffres.
- **Outils** : `search_eiopa_vector_db`, `get_own_funds_portfolio`, `check_tier_limits(rule_confirmed, evidence_ids)`, `generate_excel_qrt(template)`.
- **Texte (v3)** : voir `SYSTEM_PROMPT` (extraits clés) :

```
PROCÉDURE OBLIGATOIRE
1. Commence TOUJOURS par au moins une recherche (search_eiopa_vector_db) de la règle concernée, avant d'appliquer quoi que ce soit.
RÈGLES STRICTES
- Tous les chiffres de ta réponse viennent des outils : tu n'en inventes aucun, tu n'en recalcules aucun et tu ne fais aucune addition.
- Un passage ne confirme une règle que s'il l'ÉNONCE (les seuils eux-mêmes) ... s'il est négatif, écris « règle non confirmée par la base documentaire ».
- PLAFONNÉ signifie « plafond d'éligibilité dépassé : l'excédent n'est pas éligible » (ce n'est pas une infraction) ; ÉCART signifie « minimum non atteint ».
- Ne prétends jamais avoir consulté les dernières guidelines de l'EIOPA.
- check_tier_limits ne vérifie pas les critères qualitatifs de classement : dis-le chaque fois que tu conclus sur un instrument.
```
- **Échecs observés et correctifs** :

| # | Échec observé | Correctif | Avant → après |
|---|---|---|---|
| 1 | Le champ « règles confirmées par la base » reposait sur le score de recherche : un passage sur le SCR en général « confirmait » une règle de Tier qu'il n'énonce pas | le **modèle déclare** `rule_confirmed` + `evidence_ids`, le code **vérifie** que les passages cités existent et dépassent 0,30 de pertinence | v1 : « règle confirmée » avec un passage hors sujet → v2 : « règle non confirmée par la base documentaire » |
| 2 | Le modèle additionnait lui-même Tier 2 + Tier 3 (erreurs d'arrondi) | champ `t2_plus_t3_classified` fourni par l'outil + règle « aucune addition » | v2 : le modèle calculait la somme → v3 : valeur fournie par l'outil, aucune addition demandée |
| 3 | Vocabulaire : « ÉCART » affiché pour un plafond dépassé (fausse alerte d'infraction) | statut `PLAFONNÉ` + définition dans le prompt | v2 : « ÉCART Tier 3 » → v3 : « PLAFONNÉ » |
| 4 | Arrondi 372,085 → 372,08 (Python) alors que le moteur n8n donne 372,09 | `round_half_up` (identique à `Math.round`) | v2 : 372,08 → v3 : 372,09 |
| 5 | Le corpus ne contient pas les règles de Tier : l'agent doit le dire | règle « règle non confirmée… » + vérification en code | comportement attendu (couvert par les tests, non rejoué en direct pour ce document) : `rule_confirmed=false` tant que le corpus ne contient pas les seuils |

## P7 — Agent Climate & ESG Stress Tester

- **Rôle** : croiser le portefeuille d'assurance habitation (base SQL) avec l'API climatique réelle (Open-Meteo) et rapporter la part du SCR Non-Vie menacée par les inondations.
- **Technique** : agent à outils (3 outils), procédure imposée (base → API par zone → calcul), chiffres interdits au modèle, limites à rappeler.
- **Texte (v1)** : voir `SYSTEM_PROMPT` ; règles clés : « Tous les chiffres viennent des outils », « Précise toujours les limites (modèle climatique unique, seuil supposé, SCR réparti au prorata, taux de dommage supposé) », « Ne prétends pas avoir utilisé une source que les outils ne t'ont pas renvoyée (par exemple Copernicus) ».
- **Échecs observés** : le défaut ne venait **pas du prompt** mais de la méthode de calcul (résultat insensible à l'horizon : classes figées → 20 % constant). Corrigé en code (probabilités sur l'horizon, voir rapport §4) puis le prompt a été mis à jour pour désigner `threatened_pct_of_scr` (v1.1 : la formulation de la réponse suit la nouvelle métrique).
- **Avant/après** : v1 « 20 % du SCR Non-Vie est menacé (zones à risque élevé) » → v1.1 « X % pondéré par la probabilité d'épisode sur N ans, dont Y % en zones à risque élevé ».

---

## Annexe — Prompts de développement (consignes données à l'assistant de code)

Ces prompts ont piloté la construction du système ; ils sont résumés (la conversation complète est l'historique de travail).

1. *Remplacer le copilote RAG n8n par le notebook RAG de l'équipe* → service FastAPI `rag_service/` (même retrieval hybride + reranker).
2. *Afficher la sortie exacte du moteur SCR n8n* → proxy fidèle, aucune valeur recalculée côté interface.
3. *Appel séquentiel SCR → analyste de risque → rapport PDF avec toasts* → cascade dans `QrtAndScrView.tsx`.
4. *Système multi-agents LangChain/LangGraph (qualité des données + analyste) sans n8n* → `pipeline/` (`StateGraph`).
5. *Garder l'import Excel tel quel et ajouter l'extraction PDF* → `extraction/qrt_pdf.py` (tableaux déterministes + vision).
6. *Agent « Expert EIOPA & QRT Generator » (RAG + génération Excel)* → `eiopa_agent/`, onglet « Agent EIOPA ».
7. *Le fichier généré doit être calculable par l'agent SCR comme `2_Mutuelle_IARD_Dominante.xlsx`* → format de référence, test d'aller-retour.
8. *Agent « Climate & ESG Stress Tester »* (outils : API météo + base de portefeuille) → `climate_agent/`, onglet « Climat & ESG ».
9. *Les valeurs ne changent pas* → refonte du calcul (probabilités sur l'horizon), import de portefeuille.
10. *Rapport de 8 pages en LaTeX français + bibliothèque de prompts selon le sujet d'examen* → ce document et `docs/rapport/rapport.tex`.
