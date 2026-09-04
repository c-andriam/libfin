# libfin — Gateway de paiement (Carte → Crypto via PayMeGate)

Ce projet est un **gateway de paiement** : il héberge une page publique sur laquelle un
client choisit un montant, paie, et reçoit un lien de paiement. Il s'appuie sur
[PayMeGate](https://paymegate.com) comme processeur de paiement par carte.

> **Important** : ce README sert à **lancer l'application sur une nouvelle machine**.
> Suis-le dans l'ordre.

---

## 1. Pré-requis

Sur la machine qui va faire tourner l'app :

- **Python 3.11+** installé.
- **cloudflared** (pour exposer l'app sur Internet via un tunnel Cloudflare gratuit).

---

## 2. Récupérer le projet + la configuration

Tu dois avoir **deux choses** :

1. Le **code** de l'application (ce dossier). Soit `git clone`, soit une copie du dossier.
2. Le fichier **`.env`** (la configuration avec les clés **PayMeGate**).
   ⚠️ **Sans ce fichier, l'app ne démarre pas.** Il n'est **jamais** committé.
   Copie-le à la racine du projet.

```bash
cd libfin
cp /chemin/vers/le/fichier/.env .   # ← TES vraies clés PayMeGate
```

---

## 3. Installer les dépendances

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[gateway,test]'
```

---

## 4. Installer `cloudflared`

`cloudflared` sert à créer le **lien public** (le tunnel) qui permet aux clients
d'accéder à l'app depuis Internet.

Deux options :

- **Option A — le binaire déjà fourni** : copie le dossier
  `~/.local/cloudflared/` (trouvé sur la machine d'origine) au même endroit.
- **Option B — télécharger** : suis la doc officielle
  https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/install

---

## 5. Lancer l'application (une seule commande)

```bash
./start.sh
```

Le script fait **tout** automatiquement :

1. Lance le **gateway** (FastAPI) sur le port **8100** (mode PayMeGate, API réelle).
2. Lance un **tunnel Cloudflare** et affiche l'**URL publique**.
3. Affiche les éventuelles étapes à faire quand le lien change.

À la fin, il imprime quelque chose comme :

```
============================================================
  PUBLIC_URL   : https://xxxx.trycloudflare.com/checkout
  WEBHOOK_URL  : https://xxxx.trycloudflare.com/webhook/paymegate
============================================================
```

---

## 6. Ce qu'il faut mettre à jour quand le lien change

Le tunnel Cloudflare est **gratuit et temporaire** : à chaque redémarrage, l'URL change.
Quand cela arrive, mets à jour **deux endroits** :

1. **`.env`** → remplace `PAYMEGATE_RETURN_URL=` par la nouvelle URL publique :
   ```
   PAYMEGATE_RETURN_URL=https://NOUVEAU-LIEN.trycloudflare.com
   ```
2. **Dashboard PayMeGate** (interface web de PayMeGate) → réglage **Webhook** :
   mets la nouvelle valeur
   ```
   https://NOUVEAU-LIEN.trycloudflare.com/webhook/paymegate
   ```

---

## 7. Vérifier que ça marche

- Ouvre dans un navigateur : `https://xxxx.trycloudflare.com/checkout`
  → tu dois voir la **page de paiement** (choix du montant).
- La santé de l'app en local : `http://127.0.0.1:8100/health` → doit répondre `200`.

---

## 8. Arrêter l'application

`Ctrl+C` dans le terminal où tourne `./start.sh` arrête le gateway.

---

## 9. Dépannage rapide

| Symptôme | Cause probable | Solution |
|---|---|---|
| `./start.sh` : "ERREUR : .env introuvable" | `.env` absent | Copie le `.env` à la racine |
| `./start.sh` : "cloudflared introuvable" | binaire manquant | voir étape 4 |
| Pas de lien public affiché | tunnel pas encore prêt | attendre ~15 s, vérifier `/tmp/tunnel.log` |
| `/health` ne répond pas | gateway pas démarré | vérifier `/tmp/gateway.log` |

---

## ⚠️ Sécurité — à ne jamais faire

- **Ne jamais committer ni partager le fichier `.env`** : il contient les **clés API
  PayMeGate** réelles.
- Ne pas exposer l'app directement sur Internet **sans HTTPS** (utiliser le tunnel).

## Fichiers / dossiers utiles

| Chemin | Rôle |
|---|---|
| `start.sh` | Point d'entrée (gateway + tunnel) |
| `scripts/run_paymegate_local.py` | Lance le gateway seul (port 8100) |
| `frontend/checkout.html` | Page de paiement publique |
| `.env` | Configuration + clés PayMeGate (à copier, jamais à committer) |
