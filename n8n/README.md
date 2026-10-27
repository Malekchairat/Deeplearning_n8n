# Workflow n8n de SolvaRisk

`solvarisk-workflow.json` regroupe les trois routes n8n utilisées par l'application
(généré à partir de « My workflow.json » par `node n8n/build-workflow.cjs "<chemin>"`).

| Webhook | Rôle |
|---|---|
| `POST /webhook/scr-agent` | Moteur SCR v2.2 (formule standard). Erreur du moteur -> 422 JSON |
| `POST /webhook/report-pdf` | PDF d'audit généré par le conteneur `report-pdf:8002`. Erreur -> 502 JSON |
| `POST /webhook/notify` | E-mails Gmail : `{ type: "alert" \| "report_ready", details }` (réponse immédiate) |

## Mise en service

1. n8n : **Workflows > Import from file** > `solvarisk-workflow.json`.
2. **Désactiver l'ancien workflow** (« My workflow ») : deux workflows actifs ne peuvent pas partager le même chemin de webhook.
3. Ouvrir le nœud **Send Email** et choisir votre credential Gmail (celui du fichier d'origine est conservé, mais vérifiez-le).
4. Activer le nouveau workflow (interrupteur *Active*).
5. Réseau Docker : `docker network connect solva-net n8n` (une fois), et le conteneur `report-pdf` doit tourner.

## Sécurité des e-mails

Les webhooks n8n sont ouverts. Le nœud « Mail » n'accepte donc un `recipient` que s'il est du domaine `esprit.tn`
(liste `ALLOWED_DOMAINS`), sinon il envoie au destinataire par défaut (`DEFAULT_RECIPIENT`). Les valeurs affichées
sont échappées et le sujet est nettoyé (pas de retour à la ligne). Côté application, les alertes e-mail sont
**désactivées par défaut** (case à cocher dans l'onglet QRT & SCR).
