# frontend — tunnel de paiement libfin

Interface client de la passerelle fiat→crypto (`src/gateway`), en **trois
étapes** : la commande, la carte, la confirmation.

---

## Pile technique

**Aucune.** HTML, CSS et JavaScript natifs : pas de framework, pas de `npm`,
pas d'étape de build, pas de CDN. Le serveur ([serve.py](serve.py)) n'utilise
que la bibliothèque standard Python.

C'est délibéré. Une page qui manipule des numéros de carte ne devrait pas
embarquer un arbre de dépendances que personne ne relit, et le cœur de libfin
revendique déjà « zero package dependencies ».

### Arborescence

```
frontend/
├── README.md               ← ce fichier
├── index.html              ← étape 1 : montant et adresse crypto
├── payment.html            ← étape 2 : carte, façon 2D Link
├── result.html             ← étape 3 : état de la transaction
├── serve.py                ← serveur : fichiers statiques + relais optionnel
└── assets/
    ├── css/
    │   └── styles.css      ← thème clair/sombre, aucune police distante
    └── js/
        ├── card.js         ← règles de carte : Luhn, réseau, formats
        ├── address.js      ← Keccak-256 et somme de contrôle EIP-55
        ├── order.js        ← la commande, partagée entre les pages
        ├── api.js          ← client HTTP de la passerelle
        ├── ui.js           ← rendu : erreurs, états, détails
        ├── boot.js         ← amorçage commun aux trois pages
        ├── order-page.js   ← étape 1
        ├── payment-page.js ← étape 2
        └── result-page.js  ← étape 3
```

Les quatre premiers modules sont purs : ni DOM, ni réseau au chargement. `ui.js`
ne parle jamais au réseau. Seuls les trois scripts de page connaissent les deux
côtés, et chacun ne gouverne que son étape.

---

## Le parcours en trois étapes

```
index.html            payment.html                 result.html?tx=42
┌──────────────┐      ┌──────────────────┐         ┌──────────────────┐
│ Montant      │      │ Récapitulatif    │         │ État + détails   │
│ Devise       │ ───▶ │ Numéro de carte  │ ──202──▶│ Suivi jusqu'à    │
│ Adresse 0x…  │      │ Expiration, CVV  │         │ l'état terminal  │
└──────────────┘      └──────────────────┘         └──────────────────┘
   validation           POST /pay                    GET /transaction/{id}
   locale seule
```

### Pourquoi trois pages et non deux

La remise crypto est asynchrone : `POST /pay` répond `202` dès que le fiat est
autorisé, et l'issue se lit ensuite sur `GET /transaction/{id}`. Garder ce suivi
sur la page carte le rendrait fragile — un rechargement perdrait tout, et
l'adresse ne désignerait rien. Une troisième page portant l'identifiant dans son
URL se recharge, se met en favori et se partage sans rien perdre.

Accessoirement, cela sépare ce qui doit l'être : l'étape 2 ne montre que la
carte, comme une page de paiement hébergée, et n'a plus rien à afficher après.

### Comment la commande passe d'une page à l'autre

Deux transports, volontairement redondants ([order.js](assets/js/order.js)) :

1. **`sessionStorage`** — survit à un rechargement, ne salit pas l'URL ;
2. **les paramètres d'URL** — fonctionnent quand le stockage est refusé
   (navigation privée) et rendent l'étape 2 adressable :
   `payment.html?amount=25.00&currency=USD&wallet=0x…` est un lien de paiement
   tout prêt.

L'étape 2 lit l'URL d'abord, le stockage ensuite, et **revalide dans tous les
cas** : un paramètre d'URL est une saisie comme une autre. Une commande absente
ou invalide n'affiche aucun champ carte — seulement un panneau qui dit pourquoi
et renvoie à l'étape 1.

**Aucune donnée de carte ne transite par ce canal.** Jamais.

---

## Validations et cas d'erreur

Toutes les vérifications locales servent à épargner un aller-retour réseau, pas
à garantir quoi que ce soit : la passerelle revalide l'intégralité
([api.py](../src/gateway/api.py)).

### Étape 1 — commande

| Cas | Message |
|---|---|
| Montant vide | « Montant requis. » |
| Montant non numérique, ou plus de deux décimales | « Montant attendu : 25 ou 25.00. » |
| Montant nul ou négatif | « Le montant doit être strictement positif. » |
| Adresse vide | « Adresse du portefeuille requise. » |
| Adresse sans `0x` | « Une adresse Ethereum commence par « 0x ». » |
| Mauvaise longueur | « Adresse trop courte de 3 caractères (40 attendus après « 0x »). » |
| Caractère non hexadécimal | « Caractère « z » interdit : seuls 0-9 et a-f sont admis. » |
| Adresse de destruction | « Cette adresse est une adresse de destruction : les fonds y seraient perdus. » |
| Somme de contrôle fausse | « Somme de contrôle invalide (EIP-55) : cette adresse comporte une faute de frappe. » |
| Adresse tout en minuscules | *Avertissement seul* : la somme de contrôle est indisponible, vérifiez caractère par caractère |
| Passerelle injoignable | Bandeau d'avertissement, puis blocage à la validation — inutile d'envoyer saisir une carte |
| Stockage du navigateur refusé | Bandeau informatif ; la commande passe par l'URL et le parcours continue |
| Redirection impossible | Bandeau citant la raison |

Les bornes `AMOUNT_MIN` / `AMOUNT_MAX` **ne sont pas** vérifiées ici : la page ne
les connaît pas. Un montant hors bornes ressort en `422` à l'étape 2, avec le
détail du serveur.

### L'adresse crypto, en détail

La passerelle n'accepte qu'une expression régulière, `^0x[a-fA-F0-9]{40}$`. Une
adresse dont un caractère a été mal recopié la satisfait tout aussi bien — et le
transfert part alors vers une adresse qui n'appartient à personne, **sans retour
possible**.

[address.js](assets/js/address.js) ajoute donc la somme de contrôle **EIP-55**,
encodée dans la casse des lettres hexadécimales. Cela demande Keccak-256, la
seule primitive que ce projet ne pouvait pas éviter — attention, ce n'est pas
SHA3-256 : Ethereum utilise le remplissage d'origine, `0x01` là où SHA-3 utilise
`0x06`. L'implémentation est vérifiée contre les vecteurs officiels et les huit
adresses de l'EIP-55.

Une adresse valide est réécrite dans sa casse canonique dès que le champ perd le
focus : le client voit la forme de référence et la compare plus facilement à sa
source.

### Étape 2 — carte

| Cas | Message |
|---|---|
| Numéro vide, trop court ou trop long | « Le numéro doit comporter de 13 à 19 chiffres. » |
| Longueur incohérente avec le réseau détecté | « Longueur inattendue pour ce réseau (16 chiffres). » |
| Somme de Luhn fausse | « Numéro invalide (contrôle de Luhn). » |
| Expiration mal formée | « Format attendu : MM/AA. » |
| Mois hors 1-12 | « Mois invalide. » |
| Carte expirée | « Carte expirée. » (valable jusqu'à la fin de son mois) |
| CVV de mauvaise longueur | « Le CVV comporte 4 chiffres pour ce réseau. » |
| Commande absente | Panneau bloquant, aucun champ carte affiché |
| Commande invalide (URL trafiquée) | Panneau bloquant citant le champ fautif |
| Double envoi | Bouton désactivé **et** verrou interne : jamais deux transactions |
| `202` sans identifiant | Bandeau demandant de contacter le support sans réessayer |
| Redirection impossible après acceptation | Bandeau donnant l'URL de suivi et interdisant de recommencer |

### Étape 3 — confirmation

| Cas | Message |
|---|---|
| `tx` absent | « Cette page attend un identifiant de transaction, par exemple « result.html?tx=42 ». » |
| `tx` non numérique | « « abc » n'est pas un identifiant de transaction. » |
| `404` de la passerelle | « Aucune transaction 42 chez la passerelle. » |
| Suivi interrompu avant l'issue | Bandeau, et bouton « Actualiser » pour relancer |

### Codes HTTP, à toutes les étapes

Traduits séparément ([ui.js](assets/js/ui.js)). La distinction qui compte : un
`503` arrive **avant** tout débit, tandis qu'un `504` signifie que l'acquéreur
n'a jamais répondu et que rejouer le paiement peut débiter deux fois.

| Code | Affiché | |
|---|---|---|
| `400` | Paiement refusé | refus bancaire |
| `401` | Authentification refusée | clé d'API |
| `404` | Transaction introuvable | |
| `409` | Requête en conflit | idempotence |
| `422` | Saisie invalide | détail Pydantic aplati |
| `429` | Trop de tentatives | `RATE_LIMIT_PER_MINUTE` |
| `502` | Réseau bancaire indisponible | |
| `503` | Service indisponible | **aucun débit** |
| `504` | Sort du débit inconnu | **ne pas rejouer** |
| réseau | Passerelle injoignable | URL, TLS ou CORS |

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
  panneau « Connexion », devenu inutile ([boot.js:58](assets/js/boot.js#L58)).

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
| 1 | [payment-page.js:125](assets/js/payment-page.js#L125) | construit le corps : PAN en chiffres nus, `12/30` → `3012`, montant et adresse repris de la commande |
| 2 | [api.js:146](assets/js/api.js#L146) | `POST /pay` + `X-API-Key`, `Idempotency-Key`, `Content-Type` |
| 3 | navigateur | en mode 2, préflight `OPTIONS` automatique (en-têtes personnalisés) |
| 4 | [api.py:152](../src/gateway/api.py#L152) | `CORSMiddleware` répond au préflight |
| 5 | [api.py:191](../src/gateway/api.py#L191) | `authenticate_api_key` compare la clé |
| 6 | [api.py:218](../src/gateway/api.py#L218) | `PaymentRequest` revalide tout — Luhn, expiration, bornes |
| 7 | [payment-page.js:168](assets/js/payment-page.js#L168) | `202` reçu, redirection vers `result.html?tx=…` |
| 8 | [result-page.js:77](assets/js/result-page.js#L77) | l'étape 3 suit `GET /transaction/{id}` jusqu'à l'état terminal |

`Idempotency-Key` est régénérée à chaque envoi
([api.js:64](assets/js/api.js#L64)) au format exigé par la passerelle : un
double-clic ou un rechargement ne débite pas deux fois.

---

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
([ui.js:39](assets/js/ui.js#L39)). La distinction qui compte : un `503` arrive
**avant** tout débit, un `504` signifie que l'acquéreur n'a jamais répondu et
que rejouer peut débiter deux fois. Les deux ne s'affichent donc pas pareil.

---

## Démarrage

### Voir les pages seules

```bash
python3 frontend/serve.py        # http://127.0.0.1:5173
```

La pastille affichera **injoignable** — normal, aucune passerelle n'écoute, et
l'étape 1 refusera de passer à l'étape 2. Pour inspecter les pages malgré tout,
elles s'ouvrent directement :

```
http://127.0.0.1:5173/index.html
http://127.0.0.1:5173/payment.html?amount=25.00&currency=USD&wallet=0x742d35Cc6634C0532925a3b844Bc454e4438f44e
http://127.0.0.1:5173/result.html?tx=1
```

Toutes les validations locales — Luhn, EIP-55, expiration, formats — fonctionnent
sans passerelle : elles s'exécutent dans le navigateur.

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

* Le PAN et le CVV n'existent qu'à l'étape 2, dans leur champ et dans le corps
  de la requête. Ils
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
