# Évaluation du retrieval — SolvaRisk

Corpus : « 0.Solvabilité 2_version courte 2024-25 (1).pdf » (12 p.  5 313 car.), « 1.Pilier1 et ORSA 2024_M1.pdf » (73 p.  42 015 car.), « Formation Solvabilite 2 - IRA (1).pdf » (106 p.  25 627 car.).

Protocole : 13 questions en français dont la réponse est dans le corpus + 2 questions hors corpus. Vérité terrain = page(s) du PDF contenant la réponse (lues à la main). Un passage est pertinent s'il vient du bon document et que ses pages recoupent les pages attendues. Requêtes brutes (sans réécriture LLM), top-5. Métriques : hit@k = au moins un passage pertinent parmi les k premiers ; MRR.

## 1. Comparaison de stratégies de découpage × modes de recherche

### A. lignes + phrases, 1100/200 (production)
96 chunks, 785 caractères en moyenne (max 1099).

| Mode | hit@1 | hit@3 | hit@5 | MRR |
|---|---|---|---|---|
| dense | 69 % | 77 % | 85 % | 0.75 |
| bm25 | 31 % | 62 % | 92 % | 0.53 |
| hybrid | 54 % | 85 % | 85 % | 0.68 |
| hybrid_rerank | 100 % | 100 % | 100 % | 1.00 |

### B. lignes + phrases, 400/80 (petits chunks)
236 chunks, 331 caractères en moyenne (max 452).

| Mode | hit@1 | hit@3 | hit@5 | MRR |
|---|---|---|---|---|
| dense | 46 % | 85 % | 92 % | 0.65 |
| bm25 | 38 % | 77 % | 85 % | 0.58 |
| hybrid | 62 % | 92 % | 92 % | 0.74 |
| hybrid_rerank | 85 % | 100 % | 100 % | 0.92 |

### C. une page = un chunk (structure du document)
191 chunks, 382 caractères en moyenne (max 1744).

| Mode | hit@1 | hit@3 | hit@5 | MRR |
|---|---|---|---|---|
| dense | 54 % | 77 % | 92 % | 0.66 |
| bm25 | 46 % | 69 % | 69 % | 0.56 |
| hybrid | 77 % | 100 % | 100 % | 0.87 |
| hybrid_rerank | 77 % | 100 % | 100 % | 0.88 |

## 2. Détail par question (stratégie de production, hybride + reranker)

| Question | Rang du 1er passage pertinent |
|---|---|
| Que représente le SCR et à quel niveau de confiance est-il calibré ? | 1 |
| Quelles sont les bornes du MCR par rapport au SCR ? | 1 |
| Comment se calcule la marge de risque ? | 1 |
| Quelle est la formule du SCR en formule standard ? | 1 |
| Qu'est-ce que l'ORSA et à quelle fréquence doit-il être réalisé ? | 1 |
| Que sont les ajustements pour la capacité d'absorption des pertes par les provisions techniques et les impôts différés ? | 1 |
| Quels sont les trois piliers de Solvabilité 2 ? | 1 |
| Comment les modules de risque sont-ils agrégés pour obtenir le SCR de base ? | 1 |
| Qu'est-ce que le best estimate des provisions techniques ? | 1 |
| De quels sous-modules est composé le risque catastrophe en non-vie ? | 1 |
| Comment se calcule le SCR des actifs incorporels ? | 1 |
| Que se passe-t-il en cas de non-respect du SCR ou du MCR ? | 1 |
| Qu'est-ce que le rapport SFCR et le RSR au titre du Pilier 3 ? | 1 |

## 3. Questions hors corpus et seuil du reranker

Meilleur score du reranker sur les questions du corpus : min 0.343, médiane 0.973, max 1.000.

| Question hors corpus | Meilleur score reranker |
|---|---|
| Quelles sont les limites quantitatives des fonds propres de Tier 2 et de Tier 3 ? | 0.147 |
| Quel est le taux directeur actuel de la Banque centrale européenne ? | 0.020 |

Seuil de production : 0,05 (`min_rerank_score`). Une question hors corpus dont le meilleur score est inférieur au seuil ne déclenche aucun appel au LLM et reçoit « Information non trouvée dans les documents. »; au-dessus, le passage est transmis au LLM, dont la règle de refus (prompt P2) doit alors intervenir.
