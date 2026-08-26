# frontend — formulaire de paiement libfin

Interface de saisie carte pour la passerelle fiat→crypto (`src/gateway`).
Trois champs de carte — **numéro**, **CVV**, **date d'expiration** — plus le
montant et le portefeuille destinataire, que l'API exige.

---

## Pile technique

**Aucune.** HTML, CSS et JavaScript natifs : pas de framework, pas de `npm`,
pas d'étape de build, pas de CDN. Le serveur ([serve.py](serve.py)) n'utilise
que la bibliothèque standard Python.

C'est délibéré. Une page qui manipule des numéros de carte ne devrait pas
embarquer un arbre de dépendances que personne ne relit, et le cœur de libfin
revendique déjà « zero package dependencies ». Le corollaire : les 700 lignes
de cette interface sont lisibles en une séance.

### Arborescence

```
frontend/
├── README.md               ← ce fichier
├── index.html              ← structure de la page, un seul écran
├── serve.py                ← serveur : fichiers statiques + relais optionnel
└── assets/
    ├── css/
    │   └── styles.css      ← thème clair/sombre, aucune police distante
    └── js/
        ├── card.js         ← règles de carte : Luhn, réseau, formats  (aucun DOM)
        ├── api.js          ← client HTTP de la passerelle             (aucun DOM)
        ├── ui.js           ← rendu : erreurs, états, détails          (aucun réseau)
        └── main.js         ← assemblage : saisie → validation → envoi → suivi
```

La séparation n'est pas décorative : `card.js` et `api.js` ne touchent pas au
DOM et se testent isolément ; `ui.js` ne parle jamais au réseau ; `main.js` est
le seul fichier qui connaît les deux côtés.

---

## Comment le frontend est relié au backend

Il ne l'est pas *à la compilation*. Ce sont deux serveurs indépendants qui ne
partagent aucun code : le lien est un appel HTTP au moment du clic sur « Payer ».
L'URL de la passerelle est une **donnée**, pas une constante — d'où deux modes
de déploiement possibles, sans changer une ligne.

### Mode 1 — relayé (recommandé)

`serve.py --gateway URL` sert la page **et** relaie trois routes vers la
passerelle, en ajoutant la clé d'API côté serveur.

```
Navigateur ──→ serve.py :5173 ──┬── /            fichiers statiques
                                └── /health      ┐
                                    /pay         ├─→ passerelle  (+ X-API-Key)
                                    /transaction/{id} ┘
```

Le navigateur ne voit qu'une seule origine. Conséquences :

* **La clé d'API ne quitte jamais le serveur.** Elle vient de `GATEWAY_API_KEY`.
* **CORS devient sans objet** — plus rien à configurer.
* **La page se configure toute seule** : elle détecte le relais et masque le
  panneau « Connexion », devenu inutile ([main.js:54](assets/js/main.js#L54)).

C'est la forme que prend aussi la production, où Nginx joue ce rôle.

### Mode 2 — deux origines (statique)

`serve.py` sans `--gateway` : la page appelle la passerelle directement, sur une
autre origine. Il faut alors saisir l'URL et la clé dans le panneau
« Connexion ». Simple, mais **la clé est dans le navigateur** — acceptable en
simulation, jamais avec de vraies cartes.

### Où le choix se fait, dans le code

[api.js:54](assets/js/api.js#L54) — tout tient dans ce repli :

```js
baseUrl() {
  const stored = (this.load().baseUrl || '').trim();
  return (stored || window.location.origin).replace(/\/+$/, '');
}
```

Champ vide → la page appelle sa propre origine, donc le relais. URL saisie → elle
appelle la passerelle en direct. [api.js:102](assets/js/api.js#L102) concatène
ensuite : `fetch(config.baseUrl() + path, …)`.

L'URL et la clé saisies vivent en `sessionStorage` sous la clé `libfin.gateway`,
donc effacées à la fermeture de l'onglet. **Aucune donnée de carte n'y transite.**

---

## Variables d'environnement

### Côté frontend — lues par `serve.py`

| Variable | Rôle | Défaut |
|---|---|---|
| `GATEWAY_API_KEY` | Injectée en `X-API-Key` sur les requêtes relayées. Lue dans l'environnement et non passée en option : une ligne de commande est visible de tout le système via `ps`. | *(vide → la passerelle répond 401)* |

Le reste passe par des options : `--gateway`, `--port`, `--host`, `--insecure`.

### Côté passerelle — celles qui concernent cette page

| Variable | Effet sur le formulaire | Défaut |
|---|---|---|
| `GATEWAY_API_KEY` | La clé attendue en `X-API-Key`. Sans elle, tout répond 401 (et 503 en production). | *(obligatoire en prod)* |
| `CORS_ORIGINS` | **Mode 2 uniquement.** Doit contenir l'origine de la page, p. ex. `http://127.0.0.1:5173`. Sans objet en mode relayé. Refusé à `*` en production. | `*` |
| `AMOUNT_MIN` / `AMOUNT_MAX` | Bornes du montant. Hors bornes → 422, affiché « Saisie invalide ». | `1.00` / `10000.00` |
| `RATE_LIMIT_PER_MINUTE` | Paiements par minute et par client. Dépassement → 429, « Trop de tentatives ». | `5` |
| `BANK_TIMEOUT_SEC` | Au-delà, la passerelle répond 504 et la page affiche « Sort du débit inconnu ». | `10` |
| `ENVIRONMENT` | En production, le détail des erreurs internes est masqué dans `/transaction/{id}`. | — |

`.env.sim` fixe déjà `GATEWAY_API_KEY=simulation-api-key-not-a-secret` et
`CORS_ORIGINS=*`.

---

## Les trois routes utilisées

Aucune autre n'est appelée, et le relais n'en laisse passer aucune autre —
c'est une liste blanche explicite ([serve.py](serve.py)), pour que
`/health/ready`, dont les noms de composants dessinent l'infrastructure, reste
hors de portée.

| Route | Quand | Réponse exploitée |
|---|---|---|
| `GET /health` | au chargement, et sur « Tester la connexion » | `status`, `mode` |
| `POST /pay` | au clic sur « Payer » | `202` + `transaction_id`, `status`, `stan` |
| `GET /transaction/{id}` | toutes les 2,5 s après un 202 | `status`, `crypto_tx_hash`, `rrn` |

### La chaîne complète d'un paiement

| # | Où | Quoi |
|---|---|---|
| 1 | [main.js:129](assets/js/main.js#L129) | construit le corps : PAN en chiffres nus, `12/30` → `3012` |
| 2 | [api.js:146](assets/js/api.js#L146) | `POST /pay` + `X-API-Key`, `Idempotency-Key`, `Content-Type` |
| 3 | navigateur | en mode 2, préflight `OPTIONS` automatique (en-têtes personnalisés) |
| 4 | [api.py:152](../src/gateway/api.py#L152) | `CORSMiddleware` répond au préflight |
| 5 | [api.py:191](../src/gateway/api.py#L191) | `authenticate_api_key` compare la clé |
| 6 | [api.py:218](../src/gateway/api.py#L218) | `PaymentRequest` revalide tout — Luhn, expiration, bornes |
| 7 | [main.js:175](assets/js/main.js#L175) | `202` reçu, la page suit `GET /transaction/{id}` |

`Idempotency-Key` est régénérée à chaque envoi
([api.js:64](assets/js/api.js#L64)) au format exigé par la passerelle : un
double-clic ou un rechargement ne débite pas deux fois.

---

## Ce que fait la page

1. **Met en forme la saisie** — numéro groupé selon le réseau (4-4-4-4, ou 4-6-5
   pour American Express), barre oblique de l'expiration, CVV limité à 3 ou 4
   chiffres selon le réseau détecté.
2. **Valide avant d'envoyer** — Luhn, longueur cohérente avec le BIN, mois
   valide, carte non expirée, montant à deux décimales, adresse `0x…` de
   40 caractères. Ces contrôles évitent un aller-retour réseau pour une faute de
   frappe ; **la passerelle revalide tout**.
3. **Suit la remise crypto** — le `202` signifie « fiat autorisé » ; la page
   interroge `GET /transaction/{id}` toutes les 2,5 s jusqu'à un état terminal,
   au plus une minute.
4. **Efface le numéro et le CVV** dès la requête partie, du DOM comme de la
   portée JavaScript, quelle que soit la réponse.

### États affichés

Ils reprennent un pour un `TransactionStatus` ([models.py](../src/gateway/models.py)) :

| État | Signification pour le porteur |
|---|---|
| `FIAT_AUTHORIZED` | empreinte posée, rien n'est encore débité |
| `FIAT_CAPTURED` | crypto remise puis débit effectué — succès |
| `AUTH_VOIDED` | empreinte levée, rien n'a été débité |
| `FIAT_DECLINED` | refus de l'acquéreur |
| `CRYPTO_FAILED` | remise impossible, le fiat est rendu |
| `REVERSAL_FAILED` | somme due au porteur — intervention humaine requise |
| `FIAT_UNKNOWN` | sort du débit inconnu — **ne pas rejouer le paiement** |

Les codes HTTP d'échec sont traduits séparément
([ui.js:118](assets/js/ui.js#L118)). La distinction qui compte : un `503` arrive
**avant** tout débit, un `504` signifie que l'acquéreur n'a jamais répondu et
que rejouer peut débiter deux fois. Les deux ne s'affichent donc pas pareil.

---

## Démarrage

### Voir la page seule

```bash
python3 frontend/serve.py        # http://127.0.0.1:5173
```

La pastille affichera **injoignable** — normal, aucune passerelle n'écoute. La
mise en forme, la détection du réseau, Luhn et le contrôle d'expiration
fonctionnent quand même : ils s'exécutent dans le navigateur.

### Avec la passerelle de simulation

```bash
make sim                         # passerelle + acquéreur simulé + chaîne locale
# accepter le certificat auto-signé : ouvrir https://localhost:8443/health
make front-sim                   # http://127.0.0.1:5173, en mode relayé
```

Sans `make` (`sudo apt install make` pour l'obtenir) :

```bash
GATEWAY_API_KEY=simulation-api-key-not-a-secret \
  python3 frontend/serve.py --gateway https://localhost:8443 --insecure
```

`--insecure` désactive la vérification TLS : réservé au certificat auto-signé
de la simulation.

### Cartes de test

Scénarios câblés dans l'acquéreur simulé
([bank_server.py](../tests/simulator/bank_server.py)). Expiration `12/30`, CVV `123` :

| Numéro | Comportement de la banque | État attendu |
|---|---|---|
| `4111 1111 1111 1111` | approuve | `FIAT_CAPTURED` |
| `4000 0000 0000 0002` | refuse — 51, provision insuffisante | `FIAT_DECLINED` |
| `4000 0000 0000 0010` | refuse — 05, ne pas honorer | `FIAT_DECLINED` |
| `4000 0000 0000 0028` | ne répond pas du tout | `FIAT_UNKNOWN` |
| `4000 0000 0000 0036` | approuve, puis refuse l'extourne | `REVERSAL_FAILED` |
| `4000 0000 0000 0044` | approuve lentement (`SIM_SLOW_DELAY`, 15 s) | dépassement de `BANK_TIMEOUT_SEC` |

Portefeuille destinataire pour les essais :
`0x742d35Cc6634C0532925a3b844Bc454e4438f44e`

---

## Confidentialité des données de carte

* Le PAN et le CVV vivent dans leur champ et dans le corps de la requête. Ils
  n'atteignent ni `sessionStorage`, ni `localStorage`, ni la console.
* Les champs sont vidés dès l'envoi, et les valeurs effacées de la portée JS.
* Seul le PAN masqué (`411111******1111`) est affiché.
* Une `Content-Security-Policy` dans `index.html` interdit tout script, style ou
  image d'une autre origine ; `serve.py` répond `Cache-Control: no-store`.
* Le relais ne transmet ni cookie, ni `Origin`, ni `Referer` à la passerelle.

---

## Passer en production

Le mode relayé règle la clé d'API. **Une réserve demeure : le PAN transite par
votre origine**, ce qui place la page dans le périmètre PCI-DSS (SAQ A-EP au
minimum). Le contournement habituel est un champ hébergé par l'acquéreur —
iframe ou tokenisation — pour que le numéro n'atteigne jamais votre JavaScript.

`serve.py` n'est pas une infrastructure de production : il ne termine pas TLS et
un proxy Python ne remplace pas Nginx. En production, Nginx tient les deux rôles
— servir les fichiers statiques et injecter la clé :

```nginx
# Fichiers statiques : y copier frontend/
location / {
    root /usr/share/nginx/html;
    try_files $uri $uri/ /index.html;
}

# L'API, sur la même origine. La clé est ajoutée ici, pas dans le navigateur.
location ~ ^/(health|pay|transaction/[0-9]+)$ {
    set $upstream_api "gateway-api:8000";
    proxy_pass http://$upstream_api;
    proxy_set_header X-API-Key $gateway_api_key;   # p. ex. via un map/include
    proxy_read_timeout 30s;
    proxy_next_upstream off;                       # un paiement ne se rejoue pas
}
```

Voir [nginx.conf](../nginx/nginx.conf) pour le reste (TLS, en-têtes de sécurité,
limitation de débit). Ce bloc n'y est pas appliqué : il modifierait le routage
d'une passerelle en service, c'est à vous d'en décider.

---

## Commandes

| Commande | Effet |
|---|---|
| `python3 frontend/serve.py` | formulaire seul sur `http://127.0.0.1:5173` |
| `python3 frontend/serve.py --gateway URL` | mode relayé (avec `GATEWAY_API_KEY`) |
| `python3 frontend/serve.py --port 8080` | change le port |
| `python3 frontend/serve.py --host 0.0.0.0` | expose sur le réseau local |
| `make front` | formulaire seul, via le Makefile |
| `make front-sim` | relayé vers la passerelle de simulation |
| `make front-relay GATEWAY=https://…` | relayé vers une passerelle quelconque |

Depuis WSL, `http://localhost:5173` est directement accessible au navigateur
Windows : la redirection de la boucle locale s'en charge, aucun réglage requis.
