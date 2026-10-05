# protonmail-mcp

Serveur MCP **en lecture seule** pour Proton Mail, via une instance locale de
[Proton Bridge](https://proton.me/mail/bridge).

Proton ne propose pas d'API publique de lecture de la boîte : Bridge expose la boîte en
IMAP/SMTP local, et ce projet ajoute une couche d'outils contrôlés pour un agent IA.
Aucun outil ne peut envoyer, supprimer, déplacer ou modifier un message : les boîtes sont
ouvertes en lecture seule côté IMAP (`SELECT ... readonly`), donc même les flags
`\Seen` ne sont jamais touchés.

## Architecture

```text
Proton Mail (chiffré) ←→ Proton Bridge (local)
                              │ IMAP 127.0.0.1:1143
                              ▼
                     protonmail-mcp (stdio)
                              │ outils MCP
                              ▼
                    opencode / Claude / autre agent
```

## Prérequis

- Un abonnement Proton Mail payant (requis par Bridge)
- Proton Bridge installé, lancé et connecté à ton compte
- Python ≥ 3.13 et [uv](https://docs.astral.sh/uv/)
- Dans Bridge : `Paramètres > IMAP/SMTP` (ou l'écran d'accueil) pour récupérer
  l'adresse (username) et le **mot de passe boîte** (différent de ton mot de passe Proton)

## Installation

```bash
uv sync
```

## Configuration

Variables d'environnement :

| Variable | Défaut | Rôle |
|---|---|---|
| `PROTONMAIL_BRIDGE_USERNAME` | — | Ton adresse Proton (obligatoire) |
| `PROTONMAIL_BRIDGE_PASSWORD` | — | Mot de passe boîte affiché par Bridge (obligatoire) |
| `PROTONMAIL_BRIDGE_HOST` | `127.0.0.1` | Hôte Bridge |
| `PROTONMAIL_BRIDGE_IMAP_PORT` | `1143` | Port IMAP |
| `PROTONMAIL_BRIDGE_TIMEOUT` | `30` | Timeout socket en secondes |
| `PROTONMAIL_BRIDGE_VERIFY_TLS` | `false` | Bridge utilise un certificat auto-signé |

Vérification de bout en bout :

```bash
export PROTONMAIL_BRIDGE_USERNAME="toi@proton.me"
export PROTONMAIL_BRIDGE_PASSWORD="le-mot-de-passe-bridge"
uv run protonmail-mcp --check
```

## Intégration opencode

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "protonmail": {
      "type": "local",
      "command": ["/home/mhbxyz/Projects/protonmail-mcp/.venv/bin/protonmail-mcp"],
      "environment": {
        "PROTONMAIL_BRIDGE_USERNAME": "toi@proton.me",
        "PROTONMAIL_BRIDGE_PASSWORD": "le-mot-de-passe-bridge"
      },
      "enabled": true
    }
  }
}
```

Pour Claude Desktop ou un autre client, même principe : commande `protonmail-mcp`
(ou `.venv/bin/protonmail-mcp`), transport stdio, avec les deux variables.

## Outils exposés

| Outil | Description |
|---|---|
| `list_folders` | Liste les dossiers/labels, avec leur caractère sélectionnable |
| `list_emails` | Derniers messages d'un dossier : `limit`, `unread_only`, `since_days`, `sender`, `subject` |
| `search_emails` | Recherche plein texte (en-têtes + corps) dans un dossier |
| `read_email` | Lit un message complet par `Message-ID` (corps texte, pièces jointes, flags, troncature via `max_chars`) |

Les identifiants retournés incluent l'UID IMAP et le `Message-ID`. Pour toute lecture
ultérieure, utilisez le `Message-ID` : les UID ne sont pas stables après une
resynchronisation de Bridge.

## Tests

```bash
uv run pytest
```

Les tests utilisent un faux serveur IMAP et le transport mémoire du SDK MCP : aucune
connexion à Bridge n'est nécessaire.

## Sécurité

- **Lecture seule** : aucun outil d'écriture ; boîtes ouvertes en read-only.
- **Local uniquement** : Bridge et ce serveur ne communiquent que sur `127.0.0.1`.
- **Secrets** : le mot de passe boîte est stocké là où tu déclares les variables
  d'environnement (config MCP, `.env` non commité). Ne le mets jamais dans le dépôt.
- Tout processus local ayant le mot de passe boîte peut lire les mails : c'est le
  modèle de confiance de Bridge, pas une faiblesse de ce projet.

## Feuille de route

- [ ] Création de brouillons (IMAP `APPEND` dans Drafts) sans envoi
- [ ] Envoi SMTP avec confirmation obligatoire et anti-boucle
- [ ] Dossiers en écriture : déplacer, marquer lu/non lu, archiver
- [ ] Cache SQLite local pour recherche rapide hors ligne
