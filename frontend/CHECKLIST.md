# Checklist — du prototype au réel

Ce que ce tunnel fait déjà, ce qu'il faut installer pour l'exercer, et ce qui
reste à faire avant qu'une vraie carte le touche.

Voir [README.md](README.md) pour l'architecture et les variables d'environnement
en détail. Ce document-ci est une liste de tâches ; [ROADMAP.md](ROADMAP.md)
donne l'ordre dans lequel les prendre et les décisions qui commandent le reste.

---

## Où on en est

| | État | Vérifié comment |
|---|---|---|
| Tunnel en trois étapes | ✅ | 64 assertions navigateur, 8 scénarios |
| Validation carte (Luhn, réseau, expiration) | ✅ | dans un vrai Chrome |
| Validation adresse (EIP-55, Keccak-256) | ✅ | vecteurs officiels + 8 adresses EIP-55 |
| Relais : clé d'API hors du navigateur | ✅ | 9 assertions, en-têtes contrôlés côté serveur |
| Jambe fiat contre la **vraie** passerelle | ✅ | ISO 8583 `0100` réels, `FIAT_AUTHORIZED` et `FIAT_DECLINED` en base |
| Suite de tests de la passerelle | ⚠️ | 297 passent, 0 échec — 14 erreurs de démontage (voir §3) |
| Jambe crypto (règlement) | ❌ | demande une chaîne EVM — `make sim` |
| 3-D Secure | ❌ | absent du dépôt entier, front et back |
| Cartes réelles | ❌ | voir la checklist plus bas |

---

## 1. Environnement

### Poste de développement — le strict minimum

Pour ouvrir les pages et exercer toutes les validations locales :

- [x] **Python 3.9+** — `serve.py` n'utilise que la bibliothèque standard
- [x] **Un navigateur récent** — `BigInt`, `fetch`, `color-mix()`

C'est tout. Pas de Node, pas de `npm`, pas d'étape de build.

```bash
python3 frontend/serve.py        # http://127.0.0.1:5173
```

### Pour exercer la vraie passerelle sans conteneurs

- [x] **Python 3.9+** avec les extras du paquet (voir §2)
- [x] **`pip3`** — attention, sur certaines installations `pip` et
      `python3 -m pip` échouent alors que `pip3` fonctionne ; en cas de doute,
      passez par un environnement virtuel

```bash
pip3 install -e '.[gateway,test]'
python3 tests/simulator/bank_server.py
python3 scripts/run_gateway_local.py --port 8100
GATEWAY_API_KEY=simulation-api-key-not-a-secret \
  python3 frontend/serve.py --gateway http://127.0.0.1:8100
```

Réels : l'application FastAPI, la validation Pydantic, l'encodage ISO 8583 de
libfin, le dialogue TCP avec l'acquéreur, la base, la machine à états.
Simulés : SQLite, `fakeredis`, l'acquéreur — et **aucune chaîne**, donc les
paiements s'arrêtent à `FIAT_AUTHORIZED`.

### Pour la simulation complète, règlement crypto compris

- [ ] **`make`** — `sudo apt install make`
- [ ] **Podman** et **podman-compose**
- [ ] ~4 Go de RAM libre : la pile monte 10 conteneurs

```bash
make sim          # puis accepter le certificat : https://localhost:8443/health
make front-sim
```

`make sim` monte : `gateway-anvil` (chaîne locale), `gateway-token-deployer`
(déploie l'ERC-20 de test), `bank-simulator`, `gateway-postgres`,
`gateway-redis`, `gateway-vault`, `gateway-api`, `gateway-worker`,
`gateway-beat`, `gateway-nginx`. Foundry tourne **dans** le conteneur de
déploiement : rien à installer sur l'hôte.

C'est la seule configuration qui mène un paiement jusqu'à `FIAT_CAPTURED`.

### Production

- [x] **Nginx** en terminaison TLS, servant aussi les fichiers statiques
      — fait : `make prod-full`, `make sim`
- [ ] **PostgreSQL**, **Redis**, **Vault** — pas leurs doublures
- [ ] Un **nœud RPC** et un portefeuille approvisionné
- [ ] Un **acquéreur** joignable en TLS mutuel

`serve.py` n'a aucun rôle ici : il ne termine pas TLS et un proxy Python ne
remplace pas Nginx.

---

## 2. Dépendances pour tester

### Backend

Tout vient des extras déclarés dans `pyproject.toml` :

```bash
pip3 install -e '.[gateway,test]'
```

`gateway` apporte `fastapi`, `uvicorn`, `gunicorn`, `web3`, `pydantic`,
`sqlalchemy`, `asyncpg`, `psycopg2-binary`, `aiosqlite`, `alembic`, `celery`,
`redis`, `hvac`, `slowapi`, `cryptography`.
`test` apporte `pytest`, `pytest-asyncio`, `httpx`, `coverage`, `flake8`,
`bump2version`, `python-dateutil`, et surtout **`fakeredis` + `lupa`**, sans
lesquels ni la suite ni `run_gateway_local.py` ne démarrent.

> **`hypothesis` manque à l'appel.** `tests/gateway/test_invariants.py`
> l'importe, mais il n'est déclaré nulle part — ni dans les extras, ni dans la
> CI. Avec les seuls extras, la collecte pytest s'arrête sur un
> `ModuleNotFoundError`. En attendant :
>
> ```bash
> pip3 install hypothesis
> ```
>
> - [ ] L'ajouter à l'extra `test` de `pyproject.toml`

### Versions vérifiées

Installées et exercées sur **Python 3.14.4** :

| | | | |
|---|---|---|---|
| fastapi 0.141.1 | starlette 1.6.0 | uvicorn 0.52.4 | pydantic 2.13.4 |
| SQLAlchemy 2.0.52 | aiosqlite 0.22.1 | alembic 1.19.1 | slowapi 0.1.10 |
| redis 8.1.0 | fakeredis 2.37.1 | lupa 2.8 | celery 5.6.3 |
| web3 7.16.0 | hvac 2.4.0 | cryptography 50.0.1 | httpx 0.28.1 |

Aucune n'a demandé de compilation : les roues existent pour 3.14.

### Frontend

- [x] **Aucune.** Ni `npm install`, ni build, ni CDN.
- [ ] Pour les tests navigateur automatisés : **Chrome ou Chromium**, piloté en
      `--headless=new --dump-dom` (le rendu de captures veut `--headless=old`).
      Depuis WSL, le Chrome de Windows convient : la redirection de la boucle
      locale lui donne accès aux serveurs lancés côté Linux.
- [ ] Aucun runtime JS n'est nécessaire côté serveur — mais sans `node`, les
      modules purs (`card.js`, `address.js`, `order.js`) ne peuvent être testés
      que dans un navigateur.

### Ce qui n'est *pas* nécessaire

Docker, Node, un compilateur, Foundry sur l'hôte, un accès réseau une fois les
paquets installés.

---

## 3. Pour de vraies cartes

### Bloquants — rien ne part tant qu'ils tiennent

- [ ] **Sortir le PAN de votre JavaScript.** Champ hébergé par l'acquéreur
      (iframe) ou tokenisation. Aujourd'hui le numéro traverse votre origine,
      ce qui place la page en **SAQ A-EP** : questionnaire étendu, analyse de
      vulnérabilités trimestrielle par un ASV. `pan_vault.py` prévoit déjà le
      cas — son en-tête dit quoi faire d'un jeton réseau.
- [ ] **Implémenter 3-D Secure**, front *et* back. Il n'y en a **aucune trace**
      dans le dépôt : la passerelle construit `DE2 DE3 DE4 DE7 DE11 DE12 DE13
      DE14 DE18 DE22 DE25 DE32 DE37 DE38 DE39 DE41 DE42 DE43 DE49 DE70 DE90` —
      manquent `DE44`, `DE48`, `DE55`, `DE126`, là où voyagent le CAVV/AAV et
      l'ECI. Sans cela : refus en masse sous PSD2, et **aucun transfert de
      responsabilité** en cas de fraude.
- [ ] **Contractualiser avec un acquéreur** : `BANK_HOST`, certificat client,
      `ACQUIRER_TERMINAL_ID`, `ACQUIRER_MERCHANT_ID`.

### Frontend

- [ ] Étape de **défi 3DS** entre `payment.html` et `result.html`, avec retour
      de l'ACS
- [ ] Champ **nom du porteur** — exigé par nombre d'acquéreurs en vente à
      distance *(demande aussi un champ dans `PaymentRequest`)*
- [ ] **Adresse de facturation (AVS)** — réduit sensiblement le taux de refus
      *(idem : côté back aussi)*
- [ ] `autocomplete="cc-number"`, `cc-exp`, `cc-csc` à la place du `off`
      actuel, que les navigateurs ignorent souvent
- [ ] **Délai d'inactivité** sur la page carte, qui vide les champs
- [ ] **Retirer le panneau « Connexion »** — seul chemin qui met une clé d'API
      dans un navigateur *(coût : plus de mode deux-origines)*

### Backend

- [ ] Champs 3DS dans `PaymentRequest` et leur cartographie ISO 8583
- [ ] Accepter et transmettre nom du porteur et AVS
- [ ] **Contrôler `TRUSTED_PROXIES` dans `validate()`** — il n'y figure pas.
      Laissé à `*` en production, n'importe qui forge `X-Forwarded-For` et
      contourne la limitation de débit. Tous les autres réglages sensibles sont
      vérifiés ; celui-ci passe entre les mailles.
- [ ] Exécuter la suite : `make check` (lint + tests)
- [ ] **Corriger le démontage de `tests/gateway/conftest.py:124`.** Le montage
      appelle `process.terminate()` sur l'acquéreur simulé sans vérifier qu'il
      tourne encore ; sur Python 3.14 cela lève `ProcessLookupError`. Constaté :
      **297 tests passent, aucun échec** — mais 14 erreurs de démontage, qui
      suffisent à faire échouer `make check`. Un `if process.returncode is None`
      autour de la terminaison règle le cas.

### Exploitation

- [ ] **TLS partout.** `serve.py` sert en clair ; `--insecure` désactive la
      vérification amont. Ni l'un ni l'autre ne doit voir une vraie carte.
- [ ] `CORS_ORIGINS` ≠ `*` — déjà refusé en production par la passerelle
- [ ] `TRUSTED_PROXIES` limité à l'adresse de votre proxy
- [ ] `BANK_USE_TLS=true`, `BANK_TLS_INSECURE=false`
- [ ] `AUTO_CREATE_SCHEMA=false`, migrations par Alembic
- [ ] `GATEWAY_API_KEY` généré (`openssl rand -hex 32`), hors du dépôt
- [ ] Secrets dans Vault descellé, pas dans l'environnement
- [x] Nginx sert les fichiers statiques **et** injecte la clé — fait, dans
      [nginx.conf](../nginx/nginx.conf) : le frontend n'a plus aucune
      configuration, et `make prod-verify` le vérifie à chaque lancement
- [ ] Journaux : vérifier qu'aucun PAN complet n'y apparaît jamais
- [ ] Supervision de `REVERSAL_FAILED` et `FIAT_UNKNOWN` : ces deux états
      appellent un humain

### Avant la toute première carte réelle

- [ ] Parcours complet contre `make sim`, jusqu'à `FIAT_CAPTURED`
- [ ] Parcours complet contre l'**environnement de test de l'acquéreur**
- [ ] Rejeu des six scénarios de [bank_server.py](../tests/simulator/bank_server.py) :
      approuvée, refusée 51, refusée 05, sans réponse, extourne refusée, lente
- [ ] Vérifier l'idempotence : deux envois de la même clé ne créent qu'une
      transaction
- [ ] Vérifier la réconciliation sur une transaction laissée en suspens
- [ ] Une carte réelle, **en environnement de test acquéreur**, montant minimal
- [ ] Jamais en production avant que tous les bloquants ci-dessus soient levés

---

## 4. Ordre suggéré

1. **Trancher la question du champ hébergé.** Elle décide de tout le reste :
   inutile de peaufiner un formulaire qu'on remplacera par une iframe.
2. **Monter `make sim`** et mener un paiement jusqu'à `FIAT_CAPTURED`. C'est le
   seul trou qui subsiste dans la jambe crypto.
3. **3-D Secure de bout en bout**, passerelle comprise.
4. **Compléter le formulaire** : nom du porteur, AVS, `autocomplete` normalisé.
5. **Durcir l'exploitation** : Nginx, TLS, `TRUSTED_PROXIES`, Vault.
6. **Environnement de test acquéreur**, puis seulement une carte réelle.

Les étapes 2 et 4 sont indépendantes des autres et peuvent avancer en parallèle.
