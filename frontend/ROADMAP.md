# Roadmap — jusqu'aux vraies cartes

[CHECKLIST.md](CHECKLIST.md) dit *quoi* faire. Ce document dit *dans quel ordre*,
*pourquoi cet ordre*, et à quoi l'on reconnaît qu'une étape est finie.

Il n'y a pas de dates : elles dépendent de votre acquéreur et de votre équipe.
Il y a en revanche des dépendances, et une décision qui commande tout le reste.

---

## Le point de départ

Vérifié, aujourd'hui :

- le tunnel en trois étapes fonctionne, 64 assertions navigateur ;
- la jambe fiat passe contre la **vraie** passerelle — messages ISO 8583 `0100`
  réels, `FIAT_AUTHORIZED` et `FIAT_DECLINED` en base ;
- le relais garde la clé d'API hors du navigateur ;
- l'adresse crypto est validée par somme de contrôle EIP-55.

Manquent : le règlement crypto (pas de chaîne hors `make sim`), 3-D Secure
(absent du dépôt entier), et tout ce qui relève de la conformité.

---

## La carte des dépendances

```
Phase 0 ──▶ Phase 1 ──▶ Phase 2 ──▶ Phase 5
 refermer    OÙ VIT     3-D Secure   certifier
 l'existant  LE PAN                  puis carte réelle
                 │                        ▲
                 └──▶ Phase 3 ────────────┤
                      compléter           │
                      l'autorisation      │
                                          │
             Phase 4 ─────────────────────┘
             durcir l'exploitation
```

**La phase 1 est un embranchement, pas une tâche.** Tant qu'elle n'est pas
tranchée, peaufiner le formulaire carte est du travail qu'on risque de jeter.

Les phases 3 et 4 avancent en parallèle du reste : elles ne dépendent d'aucune
décision.

---

## Phase 0 — Refermer ce qui est déjà ouvert

*Objectif : une simulation entièrement verte, sans zone d'ombre.*

### Travaux

- Déclarer `hypothesis` dans l'extra `test` de `pyproject.toml` — la collecte
  pytest s'arrête sans lui.
- Garder `process.terminate()` dans `tests/gateway/conftest.py` : sur
  Python 3.14 il lève `ProcessLookupError` quand le sous-processus a déjà
  rendu la main. 297 tests passent, mais 14 erreurs de démontage font échouer
  `make check`.
- Installer `make` et Podman, lancer `make sim`, et mener un paiement jusqu'à
  **`FIAT_CAPTURED`**. C'est le seul trou restant dans le parcours actuel : la
  jambe crypto n'a jamais été exercée de bout en bout depuis cette interface.
- Contrôler `TRUSTED_PROXIES` dans `validate()` — il n'y figure pas, et à `*`
  la limitation de débit se contourne en forgeant un en-tête.

### Critère de sortie

`make check` passe sans erreur, et une transaction lancée depuis
`index.html` atteint `FIAT_CAPTURED` avec un hash de transfert visible à
l'étape 3.

### Débloque

Rien de formel — mais tant que ce n'est pas fait, chaque phase suivante
travaille sur un socle dont une partie n'a jamais tourné.

---

## Phase 1 — La décision : où vit le numéro de carte

*Objectif : trancher entre trois architectures. Rien d'autre ne se décide avant.*

Aujourd'hui le PAN traverse votre origine et votre JavaScript. Cela place la
page en **SAQ A-EP** : questionnaire étendu, analyse de vulnérabilités
trimestrielle par un ASV, et responsabilité pleine sur chaque ligne de la page.

### Les trois voies

| | Périmètre | Ce que devient l'étape 2 | Coût |
|---|---|---|---|
| **A. Champ hébergé (iframe acquéreur)** | SAQ A | un conteneur : l'acquéreur affiche et collecte, vous ne voyez qu'un jeton | réécriture de l'étape 2, dépendance au SDK de l'acquéreur |
| **B. Tokenisation côté client** | SAQ A-EP allégé | le formulaire reste vôtre, mais le PAN part directement chez l'acquéreur qui rend un jeton | intermédiaire ; le numéro touche encore brièvement votre JS |
| **C. Statu quo** | SAQ A-EP | inchangée | aucun code, mais toute la charge de conformité |

**Recommandation : A.** C'est la seule qui sorte réellement la page du périmètre
étendu, et le backend l'anticipe déjà — l'en-tête de
[pan_vault.py](../src/gateway/pan_vault.py) dit quoi faire d'un jeton réseau :
le stocker tel quel et supprimer le chiffrement.

### Travaux, une fois la voie choisie

- **A ou B** : remplacer le champ PAN par le composant de l'acquéreur, adapter
  `PaymentRequest` pour accepter un jeton, ajuster `pan_vault.py`.
  `card.js` perd alors l'essentiel de son rôle — Luhn et détection de réseau
  deviennent le problème de l'acquéreur.
- **C** : entamer la démarche SAQ A-EP et planifier les scans ASV.

### Critère de sortie

Une transaction de test aboutit sans qu'aucun numéro de carte n'apparaisse dans
le code de la page, ni dans une requête partant de votre origine
*(voies A et B)*.

### Débloque

Les phases 2 et 5. Le défi 3DS s'insère différemment selon la voie retenue, et
la certification en dépend directement.

---

## Phase 2 — 3-D Secure de bout en bout

*Objectif : authentifier le porteur, pour être accepté et ne plus porter la fraude.*

Il n'y a **aucune trace** de 3DS dans le dépôt. La passerelle construit
`DE2 DE3 DE4 DE7 DE11 DE12 DE13 DE14 DE18 DE22 DE25 DE32 DE37 DE38 DE39 DE41
DE42 DE43 DE49 DE70 DE90` — manquent `DE44`, `DE48`, `DE55` et `DE126`, là où
voyagent le CAVV/AAV et l'ECI. Sans eux, aucun résultat d'authentification ne
peut atteindre l'émetteur.

Conséquences de l'absence : sous PSD2, refus en masse (« soft decline », code
`65`), et **aucun transfert de responsabilité** — toute fraude reste à votre
charge.

### Travaux

**Prérequis contractuel** : un serveur 3DS (MPI), fourni par l'acquéreur ou un
tiers. Ce n'est pas du code.

**Backend**
- Cartographier le champ porteur de l'authentification dans
  [iso_dialect.py](../src/gateway/iso_dialect.py). Le dépôt gère trois dialectes
  — `visa_base1`, `iso87`, `worldpay` — et **le champ diffère selon l'acquéreur** :
  c'est une entrée par dialecte, pas une constante.
- Étendre `PaymentRequest` : CAVV/AAV, ECI, identifiant de transaction 3DS.
- Enchaîner authentification puis autorisation, et savoir traiter un refus
  d'authentification distinctement d'un refus bancaire.

**Frontend**
- Une étape de défi entre `payment.html` et `result.html` — appelons-la
  `challenge.html` : elle héberge l'iframe de l'ACS et attend le retour.
- Le parcours sans friction (*frictionless*) doit sauter cette étape sans
  la faire clignoter.
- Le retour de l'ACS atterrit sur `result.html?tx=…` : **cette forme existe
  déjà** et se recharge sans rien perdre, ce qui est précisément ce qu'exige un
  retour de redirection.
- Un nouvel état à traduire dans `ui.js` : authentification refusée.

### Critère de sortie

Contre l'environnement de test de l'acquéreur, ces trois parcours aboutissent
correctement : sans friction, avec défi réussi, avec défi échoué.

### Débloque

La phase 5. Sans 3DS, une carte réelle sera refusée ou vous coûtera la fraude.

---

## Phase 3 — Compléter la demande d'autorisation

*Objectif : baisser le taux de refus. Indépendante des autres phases.*

### Travaux

**Front + back** — chaque champ doit être accepté par `PaymentRequest` *et*
cartographié dans le dialecte, sans quoi la page reçoit un `422` :
- nom du porteur, exigé par nombre d'acquéreurs en vente à distance ;
- adresse de facturation (AVS), qui réduit sensiblement les refus.

**Front seul**
- `autocomplete="cc-number"`, `cc-exp`, `cc-csc` à la place du `off` actuel,
  que les navigateurs ignorent souvent et qui dégrade la saisie ;
- délai d'inactivité sur la page carte, qui vide les champs.

*(Si la phase 1 retient la voie A, l'acquéreur fournit peut-être déjà ces champs
dans son composant : vérifier avant d'écrire quoi que ce soit.)*

### Critère de sortie

Une autorisation de test porte tous les champs attendus par l'acquéreur, et le
taux de refus de l'environnement de test est mesuré avant / après.

---

## Phase 4 — Durcir l'exploitation

*Objectif : que la configuration de production soit la seule possible. Parallélisable.*

### Travaux

- Nginx sert les fichiers statiques **et** injecte la clé d'API — bloc prêt dans
  le [README](README.md). Le frontend n'a alors plus aucune configuration :
  ni URL, ni clé.
- **Retirer le panneau « Connexion »**, seul chemin qui met une clé d'API dans
  un navigateur. Coût assumé : plus de mode deux-origines.
- Supprimer tout usage de `serve.py` : il sert en clair et ne termine pas TLS.
- `CORS_ORIGINS` ≠ `*`, `TRUSTED_PROXIES` limité au proxy, `BANK_USE_TLS=true`,
  `BANK_TLS_INSECURE=false`, `AUTO_CREATE_SCHEMA=false`.
- Secrets dans un Vault descellé.
- Audit des journaux : vérifier qu'aucun PAN complet n'y apparaît jamais.
- Alerte sur `REVERSAL_FAILED` et `FIAT_UNKNOWN` : ces deux états appellent un
  humain, l'un parce qu'une somme est due au porteur, l'autre parce que le sort
  d'un débit est inconnu.

### Critère de sortie

`settings.require_valid()` passe en mode production, et le tunnel fonctionne
derrière Nginx en TLS sans qu'aucune valeur soit saisie dans le navigateur.

---

## Phase 5 — Certifier, puis la première carte

*Objectif : la mise en service. Rien ici ne se rattrape après coup.*

### Travaux

- SAQ correspondant à la voie retenue en phase 1 ; scans ASV si A-EP.
- Environnement de test de l'acquéreur : rejouer les six scénarios de
  [bank_server.py](../tests/simulator/bank_server.py) — approuvée, refusée 51,
  refusée 05, sans réponse, extourne refusée, lente.
- Vérifier l'idempotence : deux envois de la même clé, une seule transaction.
- Vérifier la réconciliation sur une transaction laissée en suspens.
- **Une** carte réelle, en environnement de test acquéreur, au montant minimal.
- Bascule en production, plafonds bas, supervision rapprochée les premiers jours.

### Critère de sortie

Une transaction réelle aboutit, se réconcilie, et son extourne fonctionne.

---

## Ce qui survit à toutes les décisions

Utile à savoir avant de trancher la phase 1 : l'essentiel du travail actuel ne
sera pas jeté, quelle que soit la voie.

- **L'étape 1** — montant, devise, adresse — ne touche jamais une carte. La
  validation EIP-55 garde toute sa valeur : elle protège un transfert
  irréversible, et la passerelle ne fait que vérifier une expression régulière.
- **L'étape 3** est adressée par l'identifiant de transaction. C'est déjà la
  forme qu'exige un retour d'ACS.
- **Le relais** de `serve.py` préfigure ce que fera Nginx ; la page sait déjà se
  passer de configuration quand elle le détecte.
- **La traduction des états et des codes HTTP** reste valable — notamment la
  distinction entre `503` (aucun débit) et `504` (sort du débit inconnu).
- **Le passage de commande entre pages**, par `sessionStorage` et par l'URL, ne
  transporte aucune donnée de carte : rien à revoir.

Ce qui disparaîtrait, dans les voies A et B : le champ PAN, et l'essentiel de
`card.js`.

---

## Ce qui n'est pas du code

À engager tôt, car ces délais ne se compriment pas :

- contrat acquéreur, et accès à son environnement de test ;
- serveur 3DS (MPI), fourni par l'acquéreur ou un tiers ;
- démarche PCI-DSS : SAQ, et ASV si le périmètre reste étendu ;
- nœud RPC et portefeuille chaud approvisionné, avec sa politique de recharge.
