# Intégration PayMeGate — carte vers crypto (Option A)

Ce guide décrit comment faire accepter des **paiements par carte** à un
**particulier** (sans société) qui reçoit ensuite du **crypto (USDT/USDC en
priorité)** directement dans son portefeuille **Trust Wallet**, grâce à
[PayMeGate](https://paymegate.com).

PayMeGate joue le rôle d'**acquierer / marchand de référence (merchant of
record)** : il encaisse le paiement par carte (sa propre page d'hébergement),
puis **règle automatiquement le crypto vers le portefeuille que vous avez
configuré**. Vous n'avez **ni compte Stripe/Coinbase, ni hot wallet, ni KYC
marchand** à gérer côté PayMeGate à l'inscription (email + nom de marchand
suffisent).

> Ce document est **spécifique au mode PayMeGate**. Le projet libfin garde
> son chemin ISO 8583 historique intact (simulation incluse) ; PayMeGate est
> ajouté comme un **driver optionnel** sélectionné par la variable `ACQUIRER`.
> Rien de l'existant n'est supprimé.

---

## 1. Architecture du flux (Option A)

```
  Client (navigateur)
       │  votre page : saisit la carte (validée localement, jamais stockée)
       ▼
  POST /pay ───────────────►  libfin (mode paymegate)
        │                       · crée la commande chez PayMeGate (server-to-server)
        │                       · persiste la transaction (PENDING) + orderUUID
        ▼
  { checkout_url }  ◄──────────  réponse 202

  Client redirigé vers  https://www.paymegate.com/pay/<orderUUID>
       │  paie par carte sur la page hébergée par PayMeGate
       ▼
  PayMeGate  ──(order.paid, webhook signé)──►  POST /webhook/paymegate (libfin)
       │                                          · vérifie la signature HMAC
       │                                          · marque la transaction CRYPTO_SENT
       ▼
  Trust Wallet  ◄── PayMeGate règlera le crypto automatiquement (USDT/USDC)
```

Points à retenir :

- **Le crypto n'est pas réglé par libfin** dans ce mode : c'est PayMeGate qui
  paie vers votre wallet. Le moteur web3/hot-wallet de libfin est contourné.
- **La carte n'est jamais transmise à PayMeGate par la passerelle** : la saisie
  réelle du PAN a lieu sur la page d'hébergement de PayMeGate. Votre page ne
  fait qu'une validation locale d'UX.
- **Le webhook est signé** (unpadded-base64url HMAC-SHA256 sur `"{timestamp}.{corps}"`
  dans l'en-tête `X-Paymegate-Signature`, avec `X-Paymegate-Timestamp` pour la
  prévention de rejeu) et doit être vérifié avant tout changement d'état.
  Format confirmé par la spec OpenAPI PayMeGate (<https://www.paymegate.com/developers/api>).

---

## 2. Lancer le projet

Deux modes, mutuellement exclusifs via la variable `ACQUIRER`.

### 2.1 Sans rien installer / en développement (mode simulation, chemin ISO)

Le chemin ISO 8583 historique (avec banque simulée + Anvil) est inchangé :

```bash
make install-all        # dépendances (test + docs + crypto + gateway)
make sim                # construit et lance toute la simulation
```

- Passerelle : `https://localhost:8443`
- Formulaire seul (localhost) : `make front-sim` → `http://127.0.0.1:5173`

Tests :

```bash
make test               # toute la suite
make test-fast          # arrêt au premier échec
make test-unit          # unitaires seulement (hors engine/network)
make check              # lint + tests complets
```

### 2.2 Exercer le driver PayMeGate

Le driver PayMeGate se teste **à l'écran** ou **par API**, avec `ACQUIRER=paymegate`
et une clé **sandbox / réelle** configurée (voir section 3.2). Aucun docker
n'est requis pour le driver lui-même en local :

```bash
# 1. Créer .env avec le mode PayMeGate et les secrets (voir section 3)
# 2. Lancer l'API (uvicorn) pointée sur cette config
PYTHONPATH=src .venv/bin/uvicorn gateway.api:app --reload
```

> Pour un test **adhérent au contrat réel**, verrouillez le comportement du
> réseau avec les tests d'intégration du driver (ci-dessous), qui simulent
> l'API PayMeGate sans l'atteindre.

### 2.3 Test d'intégration du driver PayMeGate

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/gateway/test_paymegate.py -v
```

Couvert :

- `POST /pay` → renvoie `checkout_url` + `order_uuid`, persiste la transaction
  en `PENDING` avec `paymegate_order_id`.
- `POST /webhook/paymegate` → vérifie la signature, règlera `CRYPTO_SENT` sur
  un `order.paid`, et **refuse (401)** une signature invalide sans toucher à
  l'état.
- validation de `verify_webhook_signature` (constante-temps, variante
  `sha256=<digest>`).

---

## 3. Configuration

### 3.1 Résumé des variables d'environnement

| Variable | Exemple / défaut | Requise | Description |
|---|---|---|---|
| `ACQUIRER` | `iso8583` / `paymegate` | oui | Routage du volet fiat. `iso8583` pour l'existant, `paymegate` pour ce driver. |
| `PAYMEGATE_BASE_URL` | `https://api.paymegate.com` | non | URL de base de l'API PayMeGate. |
| `PAYMEGATE_API_KEY` | `pmg_live_…` (~89 car.) | **oui** (mode paymegate) | Clé marchand. **Secret** — ne jamais committer. |
| `PAYMEGATE_WEBHOOK_SECRET` | chaîne | **oui** (mode paymegate) | Secret HMAC du webhook (affiché une fois à la config du webhook). **Secret**. |
| `PAYMEGATE_PAYMENT_METHODS` | `*` | non | Méthodes exposées sur le checkout ; `*` = toutes activées, ou liste `card,wallet,…`. |
| `PAYMEGATE_RETURN_URL` | `https://…/return` | **oui** (mode paymegate) | URL HTTPS publique de retour après paiement. |

Le backend refuse de démarrer en `ACQUIRER=paymegate` si une variable
requise (`PAYMEGATE_API_KEY`, `PAYMEGATE_WEBHOOK_SECRET`,
`PAYMEGATE_RETURN_URL`) manque — `ConfigError` / preflight. En `iso8583`, le
bloc PayMeGate est **ignoré** (placeholders tolérés par le preflight).

### 3.2 Étapes de configuration PayMeGate

1. **Créer un compte** sur <https://paymegate.com> (un email + un nom de
   marchand, sans KYC à l'inscription).
2. **Obtenir la clé API** dans le tableau de bord → *Developers* (elle commence
   par `pmg_live_`). Mettez-la dans la variable `PAYMEGATE_API_KEY`.
3. **Configurer le règlement crypto** — via l'API `PUT /v1/wallet` depuis le
   backend, ou le tableau de bord, en indiquant le portefeuille **Trust Wallet**
   et le réseau souhaité :
   - **TRON (TRC-20) `T…`** : pour de l'USDT TRC-20 (le plus courant chez ce
     type de prestataire) — champ `trc20`.
   - **Ethereum/Base (ERC-20) `0x…`** : pour de l'USDC — champ `evm`.
4. **Configurer le webhook** vers votre endpoint public :
   `POST https://votre-domaine/webhook/paymegate`, en notant le
   `PAYMEGATE_WEBHOOK_SECRET` fourni une seule fois.
5. **Renseigner `PAYMEGATE_RETURN_URL`** (URL publique de retour).

Les deux valeurs secrètes (`PAYMEGATE_API_KEY`, `PAYMEGATE_WEBHOOK_SECRET`) ne
vivent que dans l'environnement ou un gestionnaire de secrets (Vault) — jamais
dans un fichier committé.

### 3.3 Où placer ces variables

- **Simulation / développement** : `.env.sim` (variables commentées dans le
  bloc « PayMeGate ») ou votre `.env` local.
- **Production** : copier `.env.prod.example`, remplir les `REPLACE_ME`, et
  exécuter `make prod-preflight` (refuse tout placeholder/secret restant).
- Miroir exact des variables, même sens, dans les deux fichiers.

---

## 4. Fichiers concernés

| Fichier | Rôle |
|---|---|
| `src/gateway/paymegate.py` | **Nouveau** — client API PayMeGate + `verify_webhook_signature`. |
| `src/gateway/config.py` | Charge `ACQUIRER` et les `PAYMEGATE_*` ; valide en mode PayMeGate. |
| `src/gateway/api.py` | `POST /pay` (branche PayMeGate → `checkout_url`) et `POST /webhook/paymegate`. |
| `src/gateway/models.py` | Colonne `paymegate_order_id` et transition `PENDING → CRYPTO_SENT`. |
| `migrations/versions/20260903_1541_add_paymegate_order_id.py` | **Nouvelle** migration (index unique). |
| `frontend/assets/js/payment-page.js` | Redirige vers `checkout_url` quand présent. |
| `.env.sim` / `.env.prod.example` | Documentent le bloc PayMeGate. |
| `tests/gateway/test_paymegate.py` | **Nouveaux** tests du driver. |

---

## 5. Sécurité

- **Ne committez jamais** une clé `pmg_live_…` ni `PAYMEGATE_WEBHOOK_SECRET`.
- Le webhook est **vérifié par signature HMAC avant** toute action : une
  requête non signée ne peut pas marquer une commande payée (test dédié).
- En cas de divergence, la transaction peut être re-« réconciliée » par
  `GET /transaction/{id}` qui renvoie l'état courant (lire le statut, jamais
  l'erreur interne en production).
- Pensez à déployer derrière HTTPS et à restreindre l'accès à l'endpoint
  webhook (il est public par nature, d'où l'importance de la vérification de
  signature).
