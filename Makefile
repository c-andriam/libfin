# ──────────────────────────────────────────────────────────────
#  libfin — Makefile
# ──────────────────────────────────────────────────────────────
SHELL       := /bin/bash
PYTHON      ?= python3
PIP         ?= pip3
PYTEST      ?= pytest
COVERAGE    ?= coverage
SPHINX      ?= sphinx-build
FLAKE8      ?= flake8
BUMP        ?= bump2version
PACKAGE     := libfin
SRC_DIR     := src
TEST_DIR    := tests
FRONT_DIR   := frontend
FRONT_PORT  ?= 5173
DOCS_DIR    := docs
DOCS_SOURCE := $(DOCS_DIR)/source
DOCS_BUILD  := $(DOCS_DIR)/_build
DIST_DIR    := dist
BUILD_DIR   := build
VENV_DIR    := .venv

.DEFAULT_GOAL := help

# ──────────────────────────────────────────────────────────────
#  Aide
# ──────────────────────────────────────────────────────────────
.PHONY: help
help: ## Afficher cette aide
	@echo ""
	@echo "  libfin — Commandes disponibles"
	@echo "  ──────────────────────────────────────────"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
	@echo ""

# ──────────────────────────────────────────────────────────────
#  Environnement
# ──────────────────────────────────────────────────────────────
.PHONY: venv
venv: ## Créer un environnement virtuel (.venv)
	$(PYTHON) -m venv $(VENV_DIR)
	$(VENV_DIR)/bin/pip install --upgrade pip setuptools wheel
	@echo ""
	@echo "  ✔ Environnement créé. Activer avec: source $(VENV_DIR)/bin/activate"

.PHONY: install
install: ## Installer le package en mode développement
	$(PIP) install -e ".[test,crypto]"

.PHONY: install-all
install-all: ## Installer avec toutes les dépendances (test + docs + crypto + gateway)
	$(PIP) install -e ".[test,docs,crypto,gateway]"

.PHONY: deps
deps: ## Installer uniquement les dépendances (sans le package)
	$(PIP) install flake8 pytest pytest-asyncio bump2version coverage cryptography python-dateutil fastapi uvicorn web3 pydantic httpx sqlalchemy asyncpg psycopg2-binary aiosqlite celery redis hvac

# ──────────────────────────────────────────────────────────────
#  Tests
# ──────────────────────────────────────────────────────────────
.PHONY: test
test: ## Lancer tous les tests
	PYTHONPATH=$(SRC_DIR) $(PYTEST) $(TEST_DIR) -v

.PHONY: test-fast
test-fast: ## Lancer les tests (arrêt au premier échec)
	PYTHONPATH=$(SRC_DIR) $(PYTEST) $(TEST_DIR) -x -q

.PHONY: test-verbose
test-verbose: ## Lancer les tests avec sortie détaillée
	PYTHONPATH=$(SRC_DIR) $(PYTEST) $(TEST_DIR) -xvs

.PHONY: test-unit
test-unit: ## Lancer uniquement les tests unitaires (hors engine/network)
	PYTHONPATH=$(SRC_DIR) $(PYTEST) $(TEST_DIR) -v \
		--ignore=$(TEST_DIR)/engine \
		--ignore=$(TEST_DIR)/network

.PHONY: test-engine
test-engine: ## Lancer les tests du moteur ISO8583
	PYTHONPATH=$(SRC_DIR) $(PYTEST) $(TEST_DIR)/engine -v

.PHONY: test-network
test-network: ## Lancer les tests réseau (serveur/client TCP)
	PYTHONPATH=$(SRC_DIR) $(PYTEST) $(TEST_DIR)/network -v

# ──────────────────────────────────────────────────────────────
#  Couverture de code
# ──────────────────────────────────────────────────────────────
.PHONY: coverage
coverage: ## Lancer les tests avec mesure de couverture
	PYTHONPATH=$(SRC_DIR) $(COVERAGE) run -m pytest $(TEST_DIR) -v
	$(COVERAGE) report -m
	@echo ""
	@echo "  ✔ Rapport console affiché ci-dessus"

.PHONY: coverage-html
coverage-html: coverage ## Générer un rapport HTML de couverture
	$(COVERAGE) html
	@echo "  ✔ Rapport HTML: htmlcov/index.html"

# ──────────────────────────────────────────────────────────────
#  Qualité de code
# ──────────────────────────────────────────────────────────────
.PHONY: lint
lint: ## Vérifier le style de code (flake8)
	$(FLAKE8) $(SRC_DIR)/$(PACKAGE) --max-line-length=120

.PHONY: lint-tests
lint-tests: ## Vérifier le style des tests
	$(FLAKE8) $(TEST_DIR) --max-line-length=120

.PHONY: check
check: lint test ## Lancer lint + tests (vérification complète)
	@echo ""
	@echo "  ✔ Lint et tests passés avec succès"

# ──────────────────────────────────────────────────────────────
#  Documentation
# ──────────────────────────────────────────────────────────────
.PHONY: docs
docs: ## Construire la documentation Sphinx (HTML)
	$(SPHINX) -b html $(DOCS_SOURCE) $(DOCS_BUILD)/html
	@echo ""
	@echo "  ✔ Documentation: $(DOCS_BUILD)/html/index.html"

.PHONY: docs-clean
docs-clean: ## Nettoyer la documentation générée
	rm -rf $(DOCS_BUILD)

# ──────────────────────────────────────────────────────────────
#  Build & Distribution
# ──────────────────────────────────────────────────────────────
.PHONY: build
build: clean-build ## Construire les packages (sdist + wheel)
	$(PYTHON) -m build
	@echo ""
	@echo "  ✔ Packages créés dans $(DIST_DIR)/"
	@ls -lh $(DIST_DIR)/

.PHONY: publish-test
publish-test: build ## Publier sur TestPyPI
	$(PYTHON) -m twine upload --repository testpypi $(DIST_DIR)/*
	@echo ""
	@echo "  ✔ Publié sur TestPyPI"

.PHONY: publish
publish: build ## Publier sur PyPI (production)
	@echo "⚠  Publication sur PyPI en production !"
	@read -p "  Confirmer ? [y/N] " confirm && [ "$$confirm" = "y" ] || exit 1
	$(PYTHON) -m twine upload $(DIST_DIR)/*
	@echo ""
	@echo "  ✔ Publié sur PyPI"

# ──────────────────────────────────────────────────────────────
#  Versioning
# ──────────────────────────────────────────────────────────────
.PHONY: version
version: ## Afficher la version actuelle
	@$(PYTHON) -c "import importlib.metadata; print(importlib.metadata.version('$(PACKAGE)'))" 2>/dev/null || \
		grep 'version' pyproject.toml | head -1 | sed 's/.*"\(.*\)"/\1/'

.PHONY: bump-patch
bump-patch: ## Incrémenter la version patch (0.7.3 → 0.7.4)
	$(BUMP) patch

.PHONY: bump-minor
bump-minor: ## Incrémenter la version mineure (0.7.3 → 0.8.0)
	$(BUMP) minor

.PHONY: bump-major
bump-major: ## Incrémenter la version majeure (0.7.3 → 1.0.0)
	$(BUMP) major

# ──────────────────────────────────────────────────────────────
#  Nettoyage
# ──────────────────────────────────────────────────────────────
.PHONY: clean
clean: ## Nettoyer les fichiers temporaires Python
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type f -name "*.pyo" -delete 2>/dev/null || true
	find . -type f -name ".coverage" -delete 2>/dev/null || true
	rm -rf .pytest_cache htmlcov .mypy_cache
	@echo "  ✔ Fichiers temporaires supprimés"

.PHONY: clean-build
clean-build: ## Nettoyer les artifacts de build
	rm -rf $(BUILD_DIR) $(DIST_DIR) *.egg-info src/*.egg-info

.PHONY: clean-all
clean-all: clean clean-build docs-clean ## Nettoyage complet (tout)
	rm -rf $(VENV_DIR)
	@echo "  ✔ Nettoyage complet effectué"

# ──────────────────────────────────────────────────────────────
#  Frontend
# ──────────────────────────────────────────────────────────────
.PHONY: front
front: ## Servir le formulaire seul (http://127.0.0.1:5173)
	$(PYTHON) $(FRONT_DIR)/serve.py --port $(FRONT_PORT)

.PHONY: front-sim
front-sim: ## Servir le formulaire en relayant la passerelle de simulation
	@echo ""
	@echo "  Relais      : https://localhost:8443 (make sim)"
	@echo "  Clé d'API   : injectée côté serveur, jamais dans le navigateur"
	@echo "  CORS        : sans objet, une seule origine"
	@echo ""
	GATEWAY_API_KEY=simulation-api-key-not-a-secret \
	$(PYTHON) $(FRONT_DIR)/serve.py --port $(FRONT_PORT) \
		--gateway https://localhost:8443 --insecure

.PHONY: front-relay
front-relay: ## Idem, vers une passerelle quelconque : make front-relay GATEWAY=https://...
	@test -n "$(GATEWAY)" || { echo "  Précisez GATEWAY=https://votre-passerelle"; exit 1; }
	@test -n "$$GATEWAY_API_KEY" || echo "  GATEWAY_API_KEY non défini : la passerelle répondra 401"
	$(PYTHON) $(FRONT_DIR)/serve.py --port $(FRONT_PORT) --gateway $(GATEWAY)

#  Certificats TLS
# ──────────────────────────────────────────────────────────────
.PHONY: certs
certs: ## Générer un certificat TLS auto-signé (nginx/ssl/)
	./scripts/gen_certs.sh $(or $(CN),localhost)

# ──────────────────────────────────────────────────────────────
#  SIMULATION — topologie de production, dépendances simulées
# ──────────────────────────────────────────────────────────────
#  Même image, même Nginx/TLS, même Gunicorn, même worker Celery
#  qu'en production. Seuls l'acquéreur et la blockchain sont simulés.
#  C'est ici qu'on répète la production sans risquer d'argent.
# ──────────────────────────────────────────────────────────────
SIM_COMPOSE := podman-compose -f podman-compose.sim.yml --env-file .env.sim

.PHONY: sim-build
sim-build: ## Construire l'image de simulation
	podman build -t gateway:sim -f Containerfile.prod .

.PHONY: sim-migrate
sim-migrate: ## Appliquer les migrations à la base de simulation
	$(SIM_COMPOSE) exec gateway-api python /app/scripts/migrate.py

.PHONY: sim
sim: certs sim-build ## Lancer la simulation complète (build + up + migration + jeton)
	$(SIM_COMPOSE) up -d
	@echo "  Application des migrations..."
	@sleep 8
	-$(SIM_COMPOSE) exec -T gateway-api python /app/scripts/migrate.py
	@echo ""
	@echo "  Attente de la chaîne locale et du déploiement du jeton..."
	@$(SIM_COMPOSE) logs gateway-token-deployer 2>/dev/null | tail -5 || true
	@echo ""
	@echo "  ✔ Simulation lancée."
	@echo "    Formulaire : https://localhost:8443/   (certificat auto-signé)"
	@echo "    API        : même origine — /health, /pay, /transaction/<id>"
	@echo "    Clé API    : posée par Nginx ; en direct, GATEWAY_API_KEY de .env.sim"
	@echo "    Cartes     : 4111111111111111 approuvée · 4000000000000002 refusée"
	@echo "                 4000000000000028 sans réponse · 4000000000000036 reversal refusé"
	@echo ""
	@echo "    Tester     : make sim-pay"
	@echo "    Vérifier   : make sim-status"

.PHONY: sim-up
sim-up: ## Démarrer la simulation sans reconstruire
	$(SIM_COMPOSE) up -d

.PHONY: sim-down
sim-down: ## Arrêter la simulation et supprimer ses volumes
	$(SIM_COMPOSE) down -v

.PHONY: sim-logs
sim-logs: ## Suivre les logs de la simulation
	$(SIM_COMPOSE) logs -f

.PHONY: sim-status
sim-status: ## État des conteneurs de simulation
	$(SIM_COMPOSE) ps
	@echo ""
	@curl -sk https://localhost:8443/health | head -5 || echo "  API injoignable"

.PHONY: sim-pay
sim-pay: ## Envoyer un paiement de test à travers toute la chaîne
	@echo "  Paiement de test (carte approuvée)..."
	@curl -sk -X POST https://localhost:8443/pay \
		-H "Content-Type: application/json" \
		-H "X-API-Key: simulation-api-key-not-a-secret" \
		-H "Idempotency-Key: make-sim-$$(date +%s)" \
		-d '{"pan":"4111111111111111","expiry":"3012","cvv":"123","amount":"25.00","target_wallet":"0x742d35Cc6634C0532925a3b844Bc454e4438f44e"}' \
		| python3 -m json.tool || true
	@echo ""
	@echo "  Suivre le transfert crypto : make sim-logs"

.PHONY: sim-decline
sim-decline: ## Tester le refus bancaire (aucun crypto ne doit partir)
	@curl -sk -X POST https://localhost:8443/pay \
		-H "Content-Type: application/json" \
		-H "X-API-Key: simulation-api-key-not-a-secret" \
		-H "Idempotency-Key: make-dec-$$(date +%s)" \
		-d '{"pan":"4000000000000002","expiry":"3012","cvv":"123","amount":"25.00","target_wallet":"0x742d35Cc6634C0532925a3b844Bc454e4438f44e"}' \
		| python3 -m json.tool || true

.PHONY: sim-reconcile
sim-reconcile: ## Lancer la réconciliation à blanc dans la simulation
	$(SIM_COMPOSE) exec gateway-api python /app/scripts/reconciliation_cron.py --dry-run

.PHONY: sim-connect
sim-connect: ## Créer une session temporaire pour la console des liens (make sim-connect [TTL=14400], 240min par défaut)
	$(SIM_COMPOSE) exec -T gateway-api python /app/scripts/connect.py \
		--ttl $(or $(TTL),14400) --base-url https://localhost:8443

.PHONY: sim-reset
sim-reset: ## Repartir de zéro (supprime base ET file d'attente)
	@echo "  ⚠  Réinitialise base et Redis. Les deux ensemble : une file"
	@echo "     survivante référencerait des identifiants réattribués."
	$(SIM_COMPOSE) down
	-podman volume rm libfin_pgdata_sim libfin_redisdata_sim
	$(SIM_COMPOSE) up -d

# ──────────────────────────────────────────────────────────────
#  Vérifications de robustesse
# ──────────────────────────────────────────────────────────────
#  Trois angles que les tests unitaires ne couvrent pas :
#  la contention, les pannes, et le comportement sur une vraie chaîne.
# ──────────────────────────────────────────────────────────────
.PHONY: test-concurrency
test-concurrency: ## Paiements simultanés : collision de nonce ou de STAN ?
	podman run --rm --network libfin_backend-net -v ./tests/load:/t:ro,z \
		--entrypoint python3 gateway:sim /t/concurrency_test.py \
		--count $(or $(COUNT),60) --amount 1.00 \
		--base-url http://gateway-api:8000 --settle-timeout 400

.PHONY: test-chaos
test-chaos: ## Injection de pannes : l'argent du client est-il toujours protégé ?
	python3 tests/load/chaos_test.py $(if $(ONLY),--only $(ONLY),)

.PHONY: test-chaos-list
test-chaos-list: ## Lister les scénarios de panne
	python3 tests/load/chaos_test.py --list

.PHONY: test-testnet
test-testnet: ## Valider le service crypto contre une vraie chaîne publique
	python3 tests/load/testnet_check.py

# ──────────────────────────────────────────────────────────────
#  Déploiement complet — d'un dépôt fraîchement cloné à un paiement
# ──────────────────────────────────────────────────────────────
#  Deux commandes sur une machine neuve :
#
#      make tunnel     une fois — donne un nom public à cette machine
#      make deploy     à chaque fois — construit, migre, lance, vérifie
#
#  L'ordre compte. `deploy` refuse de démarrer si la configuration n'est pas
#  prête, et le tunnel demande une autorisation par navigateur qu'on ne veut
#  pas rencontrer au milieu d'un déploiement.
# ──────────────────────────────────────────────────────────────

TUNNEL_NAME     ?= libfin-pay
TUNNEL_HOSTNAME ?= pay.cookshare.me
TUNNEL_CONFIG   ?= $(HOME)/.cloudflared/$(TUNNEL_NAME).yml
CLOUDFLARED     ?= $(shell command -v cloudflared 2>/dev/null || echo $(HOME)/.local/bin/cloudflared)

.PHONY: preflight-host
preflight-host: ## Vérifier que la machine a de quoi faire tourner le projet
	@echo ""
	@echo "  Outils requis"
	@echo "  ──────────────────────────────────────────"
	@missing=0; 	for tool in podman podman-compose python3 curl openssl; do 	    if command -v $$tool >/dev/null 2>&1; then 	        printf "  \033[32m✔\033[0m %-16s %s\n" "$$tool" "$$($$tool --version 2>&1 | head -1 | cut -c1-46)"; 	    else 	        printf "  \033[31m✘\033[0m %-16s absent\n" "$$tool"; missing=1; 	    fi; 	done; 	test $$missing -eq 0 || { echo ""; echo "  Installer ce qui manque, puis relancer."; exit 1; }
	@echo ""

.PHONY: tunnel
tunnel: ## ⭐ Configurer le tunnel Cloudflare sur cette machine (une seule fois)
	TUNNEL_NAME=$(TUNNEL_NAME) TUNNEL_HOSTNAME=$(TUNNEL_HOSTNAME) 	    ./scripts/tunnel_setup.sh

.PHONY: tunnel-run
tunnel-run: ## Lancer le tunnel au premier plan (Ctrl-C l'arrête)
	$(CLOUDFLARED) tunnel --config $(TUNNEL_CONFIG) run $(TUNNEL_NAME)

#  `pgrep -x` matche le *nom* du processus, pas sa ligne de commande. Avec
#  `-f`, le motif se trouve aussi dans la commande qui le cherche : la recette
#  se tuait elle-même — trois fois de suite avant qu'on le voie. Un nom
#  d'exécutable ne peut pas se confondre avec un shell ; le filtre sur le nom
#  du tunnel se fait ensuite, en lisant /proc.
TUNNEL_PIDS = pgrep -x cloudflared 2>/dev/null | while read -r pid; do tr '\\0' ' ' < /proc/$$pid/cmdline 2>/dev/null | grep -q '$(TUNNEL_NAME)' && echo $$pid; done

.PHONY: tunnel-start
tunnel-start: ## Lancer le tunnel en arrière-plan
	@if [ -n "$$($(TUNNEL_PIDS))" ]; then echo "  Le tunnel tourne déjà."; else \
	  setsid $(CLOUDFLARED) tunnel --config $(TUNNEL_CONFIG) run $(TUNNEL_NAME) </dev/null >/tmp/libfin-tunnel.log 2>&1 & \
	  sleep 14; \
	  n=$$(grep -c 'Registered tunnel connection' /tmp/libfin-tunnel.log 2>/dev/null || echo 0); \
	  echo "  Tunnel lancé — $$n connexion(s). Journal : /tmp/libfin-tunnel.log"; \
	fi

.PHONY: tunnel-stop
tunnel-stop: ## Arrêter le tunnel (le nom public cesse de répondre)
	@pids=$$($(TUNNEL_PIDS)); \
	if [ -n "$$pids" ]; then kill $$pids 2>/dev/null; echo "  Tunnel arrêté ($$pids)."; \
	else echo "  Aucun tunnel en cours."; fi

.PHONY: tunnel-status
tunnel-status: ## État du tunnel et du nom public
	@TUNNEL_NAME=$(TUNNEL_NAME) TUNNEL_HOSTNAME=$(TUNNEL_HOSTNAME) 	    ./scripts/tunnel_setup.sh --status

.PHONY: setup-paymegate
setup-paymegate: ## Enregistrer webhook + portefeuille chez PayMeGate (PAYMEGATE_API_KEY requis)
	@test -n "$$PAYMEGATE_API_KEY" || { echo "  PAYMEGATE_API_KEY manquant."; 	    echo "  export PAYMEGATE_API_KEY=pmg_live_…  puis relancer."; exit 1; }
	@test -n "$(WALLET)" || { echo "  Indiquer le portefeuille de règlement :"; 	    echo "  make setup-paymegate WALLET=0x… [NETWORK=evm]"; exit 1; }
	./scripts/paymegate_setup.py webhook https://$(TUNNEL_HOSTNAME)/webhook/paymegate
	./scripts/paymegate_setup.py wallet $(WALLET) --network $(or $(NETWORK),evm)
	@echo ""
	@echo "  Reporter PAYMEGATE_WEBHOOK_SECRET dans $(ENV_FILE), puis : make deploy"

#  Un clone neuf n'a ni fichier d'environnement, ni certificat, ni Vault
#  initialisé — rien de tout cela n'est versionné, et pour de bonnes raisons.
#  `bootstrap` fabrique les trois, sans rien écraser de ce qui existe déjà.
#
#  Il ne peut pas fabriquer les identifiants PayMeGate : ils viennent de votre
#  compte. C'est le seul point d'arrêt, et il est explicite plutôt que découvert
#  trois étapes plus loin sous la forme d'un préflight qui refuse.
.PHONY: bootstrap
bootstrap: preflight-host ## Préparer une machine neuve : env, certificats, Vault
	@test -f $(ENV_FILE) || { echo ""; echo "  Création de $(ENV_FILE)…"; $(MAKE) --no-print-directory env-prod; }
	@test -f nginx/ssl/server.crt || { echo "  Génération d'un certificat TLS local…"; ./scripts/gen_certs.sh $(or $(CN),localhost) >/dev/null; echo "  ✔ nginx/ssl/"; }
#  VAULT_TOKEN est exclu : `vault-ensure` l'écrit lui-même au premier
#  démarrage. Le réclamer ici enverrait chercher une valeur qui arrive seule.
#  Le filtre suit celui de preflight_check.sh, qui fait autorité : seul le
#  bloc de l'acquéreur en service est exigé. Ce contrôle-ci n'existe que pour
#  le dire tôt, avant une construction de plusieurs minutes.
	@remaining=$$(grep -nE '^[A-Z_]+=.*REPLACE_ME' $(ENV_FILE) 2>/dev/null | grep -vE "$$(grep -q '^ACQUIRER=paymegate' $(ENV_FILE) && echo ':(BANK_|ACQUIRER_|WEB3_)' || echo ':PAYMEGATE_')" | grep -v ':VAULT_TOKEN' | wc -l); \
	if [ "$$remaining" -gt 0 ]; then \
	  echo ""; \
	  echo "  ──────────────────────────────────────────────────────────"; \
	  echo "   $$remaining valeur(s) restent à renseigner dans $(ENV_FILE) :"; \
	  echo ""; \
	  grep -nE '^[A-Z_]+=.*REPLACE_ME' $(ENV_FILE) | grep -vE "$$(grep -q '^ACQUIRER=paymegate' $(ENV_FILE) && echo ':(BANK_|ACQUIRER_|WEB3_)' || echo ':PAYMEGATE_')" | grep -v ':VAULT_TOKEN' | sed 's/^/     /'; \
	  echo ""; \
	  echo "   Pour un encaissement par PayMeGate, il faut ACQUIRER=paymegate"; \
	  echo "   et les trois PAYMEGATE_*. Le webhook et son secret s'obtiennent"; \
	  echo "   avec :  make setup-paymegate WALLET=0x…"; \
	  echo ""; \
	  echo "   Puis relancer : make deploy"; \
	  echo "  ──────────────────────────────────────────────────────────"; \
	  exit 1; \
	fi
	@echo "  ✔ Configuration complète."

#  Vault est amorcé après le démarrage des services de données et avant les
#  migrations : le script s'y connecte par `podman exec`, donc le conteneur
#  doit tourner, et l'API a besoin du jeton avant de démarrer.
.PHONY: vault-ensure
vault-ensure: ## Initialiser ou desceller Vault selon son état
	@out=$$(podman exec -e VAULT_ADDR=http://127.0.0.1:8200 gateway-vault-prod vault status 2>/dev/null || true); \
	if echo "$$out" | grep -q 'Initialized.*true'; then \
	  if echo "$$out" | grep -q 'Sealed.*true'; then \
	    echo "  Vault est scellé — descellement…"; $(MAKE) --no-print-directory prod-vault-unseal; \
	  else echo "  ✔ Vault déjà ouvert."; fi; \
	else echo "  Premier démarrage de Vault — initialisation…"; $(MAKE) --no-print-directory prod-vault-bootstrap; fi

.PHONY: deploy
deploy: bootstrap ## ⭐ Tout monter : env → certificats → Vault → build → migrations → services → tunnel
	#  Vault d'abord : il écrit VAULT_TOKEN dans le fichier d'environnement,
	#  et le préflight le lit. L'inverse bloquait sur une valeur que l'étape
	#  suivante allait produire.
	$(PROD_COMPOSE) up -d gateway-postgres gateway-redis gateway-vault
	@echo "  Attente des services de données…"; sleep 12
	@$(MAKE) --no-print-directory vault-ensure
	@$(MAKE) --no-print-directory prod-preflight
	@$(MAKE) --no-print-directory prod-build
	@$(MAKE) --no-print-directory prod-migrate
	$(PROD_COMPOSE) up -d
	@echo "  Attente de Nginx…"
	@i=0; until curl -skf -o /dev/null $(PROD_URL)/health; do i=$$((i+1)); test $$i -lt 60 || { echo "  ✘ Rien sur $(PROD_URL) — make prod-logs"; exit 1; }; sleep 2; done
	@$(MAKE) --no-print-directory tunnel-start
	@echo ""; echo "  Attente de la propagation du nom public…"
	@i=0; until curl -sf -o /dev/null --max-time 20 https://$(TUNNEL_HOSTNAME)/health; do i=$$((i+1)); test $$i -lt 20 || { echo "  ✘ https://$(TUNNEL_HOSTNAME) ne répond pas — make tunnel-status"; exit 1; }; sleep 6; done
	@$(MAKE) --no-print-directory deploy-verify

.PHONY: deploy-verify
deploy-verify: ## Vérifier le déploiement par le nom public, comme le ferait un client
	@echo ""
	@echo "  Vérification de https://$(TUNNEL_HOSTNAME)"
	@echo "  ──────────────────────────────────────────"
	@for path in / /links.html /payment.html /health; do 	    code=$$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 https://$(TUNNEL_HOSTNAME)$$path); 	    if [ "$$code" = "200" ]; then printf "  \033[32m✔\033[0m %-16s %s\n" "$$path" "$$code"; 	    else printf "  \033[31m✘\033[0m %-16s %s\n" "$$path" "$$code"; fi; 	done
	@code=$$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -X POST 	    -H 'Content-Type: application/json' -d '{}' 	    https://$(TUNNEL_HOSTNAME)/webhook/paymegate); 	  if [ "$$code" = "401" ]; then printf "  \033[32m✔\033[0m %-16s %s (non signé, refusé)\n" "/webhook" "$$code"; 	  else printf "  \033[31m✘\033[0m %-16s %s (attendu 401)\n" "/webhook" "$$code"; fi
	@echo ""
	@echo "  ══════════════════════════════════════════════════════════"
	@echo "   Déploiement terminé."
	@echo ""
	@echo "   Créer un lien   https://$(TUNNEL_HOSTNAME)/"
	@echo "   Vos liens       https://$(TUNNEL_HOSTNAME)/links.html"
	@echo "   Suivre en direct  make watch"
	@echo "  ══════════════════════════════════════════════════════════"
	@echo ""

.PHONY: deploy-down
deploy-down: tunnel-stop prod-down ## Tout arrêter : tunnel puis services

.PHONY: watch
watch: ## Suivre en direct les opérations de la passerelle
	./scripts/watch.sh $(ARGS)

# ──────────────────────────────────────────────────────────────
#  Démonstration
# ──────────────────────────────────────────────────────────────
.PHONY: demo
demo: ## Démonstration : un paiement carte → crypto, vérifié de bout en bout
	./scripts/demo.sh $(or $(CURRENCY),USD) $(or $(AMOUNT),300.00)

.PHONY: demo-multi
demo-multi: ## Démonstration multi-devises : USD, EUR et GBP
	./scripts/demo.sh --multi $(or $(AMOUNT),300.00)

.PHONY: demo-failure
demo-failure: ## Démonstration : la livraison échoue, le client n'est pas débité
	./scripts/demo.sh --failure

.PHONY: verify-wallet
verify-wallet: ## Vérifier sur la chaîne que le wallet détient bien ce qui a été livré
	./scripts/verify_wallet.sh $(WALLET)

.PHONY: demo-up
demo-up: ## Démarrer le stack de production pour la démonstration
	./scripts/prodtest.sh --local-chain

.PHONY: demo-down
demo-down: ## Tout démonter après la démonstration
	./scripts/prodtest.sh --down

.PHONY: testnet-wallet
testnet-wallet: ## Générer un portefeuille de testnet (à financer par un robinet)
	./scripts/gen_testnet_wallet.py

.PHONY: prodtest
prodtest: ## Répéter la production contre une vraie chaîne (nécessite un portefeuille financé)
	./scripts/prodtest.sh

.PHONY: prodtest-down
prodtest-down: ## Démonter la répétition de production
	./scripts/prodtest.sh --down

.PHONY: test-robustness
test-robustness: test-concurrency test-chaos test-testnet ## Les trois d'affilée
	@echo ""
	@echo "  ✔ Contention, pannes et chaîne réelle : tous validés"

# ──────────────────────────────────────────────────────────────
#  PRODUCTION
# ──────────────────────────────────────────────────────────────
# .env.prod est suivi par git, dans un dépôt public : c'est un modèle, il ne
# doit porter aucun secret. Le lancement réel pointe sur .env.prod.local, qui
# n'est pas suivi et que `make env-prod` génère.
#
# GATEWAY_ENV_FILE est exporté parce que podman-compose.prod.yml s'en sert pour
# le `env_file:` des conteneurs applicatifs : sans lui, l'API lirait le modèle
# et démarrerait avec des placeholders.
ENV_FILE ?= .env.prod.local
export GATEWAY_ENV_FILE := $(ENV_FILE)
PROD_COMPOSE := podman-compose -f podman-compose.prod.yml --env-file $(ENV_FILE)

.PHONY: prod-build
prod-build: ## Construire l'image de production
	podman build -t gateway:prod -f Containerfile.prod .

.PHONY: env-prod
env-prod: ## Créer .env.prod : secrets générés, le reste listé
	./scripts/init_env_prod.sh

.PHONY: prod-preflight
prod-preflight: ## Vérifier que tout est prêt (bloque si non)
	ENV_FILE=$(ENV_FILE) ./scripts/preflight_check.sh

.PHONY: prod-migrate
prod-migrate: ## Appliquer les migrations de schéma (Alembic)
	$(PROD_COMPOSE) run --rm gateway-api python /app/scripts/migrate.py

.PHONY: prod-migrate-sql
prod-migrate-sql: ## Afficher le SQL des migrations sans l'exécuter (revue avant application)
	$(PROD_COMPOSE) run --rm gateway-api python /app/scripts/migrate.py --sql

.PHONY: prod-migrate-check
prod-migrate-check: ## Vérifier que le schéma correspond aux modèles
	$(PROD_COMPOSE) run --rm gateway-api python /app/scripts/migrate.py --check

.PHONY: migration
migration: ## Générer une révision (MSG="description du changement")
	@test -n "$(MSG)" || { echo "  Usage: make migration MSG=\"ajout colonne X\""; exit 1; }
	DATABASE_URL=$${DATABASE_URL:-sqlite:///$(PWD)/.migration-scratch.db} \
		PYTHONPATH=$(SRC_DIR) $(PYTHON) -m alembic revision --autogenerate -m "$(MSG)"
	@rm -f .migration-scratch.db
	@echo "  ⚠  Relire la révision générée avant de l'appliquer."

.PHONY: prod
prod: prod-preflight prod-build ## Lancer la production (preflight → build → migrate → up)
	$(PROD_COMPOSE) up -d gateway-postgres gateway-redis gateway-vault
	@echo "  Attente des services de données..."
	@sleep 10
	$(MAKE) prod-migrate
	$(PROD_COMPOSE) up -d
	@echo ""
	@echo "  ✔ Production lancée (passerelle + formulaire, même origine TLS)."
	@echo "    Si Vault vient de redémarrer : make prod-vault-unseal"
	@echo "    Vérifier l'état             : make prod-status"
	@echo "    Tout vérifier d'un coup     : make prod-verify"

# Le port publié par Nginx. HTTPS_PORT vaut 443 par défaut ; en rootless,
# .env.prod le remonte au-dessus de 1024, et l'URL de vérification doit suivre.
PROD_PORT ?= $(shell sed -n 's/^HTTPS_PORT=//p' $(ENV_FILE) 2>/dev/null | tr -d '\r' | tail -1 | grep . || echo 443)
PROD_URL  ?= https://localhost:$(PROD_PORT)

# ⭐ La commande unique : tout le back, tout le front, une seule origine TLS.
#
# `prod-verify` est appelé depuis la recette, non déclaré en prérequis : un
# `make -j` lancerait les deux de front et vérifierait une pile qui n'est pas
# encore debout.
.PHONY: prod-full
prod-full: prod ## ⭐ Tout lancer : passerelle + formulaire sur une seule origine TLS
	@echo ""
	@echo "  Attente de Nginx (la passerelle doit d'abord être saine)..."
	@i=0; until curl -skf -o /dev/null $(PROD_URL)/health; do i=$$((i+1)); test $$i -lt 60 || { echo "  ✘ Rien sur $(PROD_URL) après deux minutes — make prod-logs"; exit 1; }; sleep 2; done
	@$(MAKE) --no-print-directory prod-verify
	@echo ""
	@echo "  ══════════════════════════════════════════════════════════"
	@echo "   Le tunnel de paiement est en ligne."
	@echo ""
	@echo "   Formulaire : $(PROD_URL)/"
	@echo "   Étape 2    : $(PROD_URL)/payment.html"
	@echo "   Étape 3    : $(PROD_URL)/result.html?tx=<id>"
	@echo ""
	@echo "   Le navigateur ne voit qu'une origine : Nginx sert les pages et"
	@echo "   relaie /health, /pay et /transaction/<id> vers la passerelle en"
	@echo "   y ajoutant X-API-Key. Aucune clé ni URL à saisir dans la page —"
	@echo "   le panneau « Connexion » se masque de lui-même."
	@echo "  ══════════════════════════════════════════════════════════"
	@echo ""
	@echo "   Si Vault vient de redémarrer : make prod-vault-unseal"
	@echo "   Logs : make prod-logs · Arrêt : make prod-down"

#  $(call probe,<code attendu>,<chemin>,<libellé>)
#
#  « probe » et non « check » : `check` est déjà une cible de ce fichier,
#  et deux sens pour un même mot se paient à la relecture.
#
#  -k parce que le certificat est auto-signé tant qu'aucun vrai n'est installé.
#  Avec un certificat émis par une AC, le retirer : une vérification qui ne
#  vérifie rien vaut moins que pas de vérification du tout.
#
#  Pas de virgule dans le libellé : $(call) découpe ses arguments dessus.
probe = code=$$(curl -sk -o /dev/null -w '%{http_code}' $(PROD_URL)$(2) 2>/dev/null || echo 000); printf '  %-5s %-24s %s\n' "$$code" '$(2)' '$(3)'; test "$$code" = '$(1)' || { echo '  ✘ attendu $(1) sur $(2)'; exit 1; }

.PHONY: prod-verify
prod-verify: ## Vérifier que les deux moitiés répondent sur la même origine
	@echo ""
	@echo "  Vérification de $(PROD_URL)"
	@echo "  ──────────────────────────────────────────────────"
	@$(call probe,200,/health,passerelle)
	@$(call probe,200,/,formulaire — étape 1)
	@$(call probe,200,/payment.html,formulaire — étape 2)
	@$(call probe,200,/result.html,formulaire — étape 3)
	@$(call probe,200,/assets/css/styles.css,feuille de style)
	@$(call probe,200,/assets/js/api.js,client de la passerelle)
	@# Le montage porte aussi README.md, CHECKLIST.md, ROADMAP.md et serve.py,
	@# et /health/ready nomme un à un les composants de l'infrastructure. La
	@# liste blanche de Nginx doit refuser les uns comme l'autre : un 200 ici
	@# veut dire qu'elle a sauté.
	@$(call probe,404,/README.md,documentation — hors liste)
	@$(call probe,404,/serve.py,serve.py — hors liste)
	@$(call probe,404,/health/ready,readiness — hors liste)
	@echo ""
	@echo "  ✔ Formulaire et passerelle répondent sur la même origine."

.PHONY: prod-down
prod-down: ## Arrêter la production (volumes conservés)
	$(PROD_COMPOSE) down

.PHONY: prod-logs
prod-logs: ## Suivre les logs de production
	$(PROD_COMPOSE) logs -f

.PHONY: prod-status
prod-status: ## État des conteneurs et santé applicative
	$(PROD_COMPOSE) ps
	@echo ""
	@$(PROD_COMPOSE) exec -T gateway-api curl -sf http://127.0.0.1:8000/health || echo "  API injoignable"

# Le premier démarrage a un ordre non évident : le preflight bloque sur
# VAULT_TOKEN, mais ce token n'existe qu'une fois Vault initialisé — et Vault
# fait partie de la pile que le preflight garde. Cette cible démarre Vault
# seul, sans preflight : aucun conteneur applicatif, rien qui puisse toucher
# une carte.
.PHONY: prod-certbot
prod-certbot: ## Certificat TLS reel : make prod-certbot DOMAIN=... EMAIL=...
	./scripts/certbot_issue.sh

.PHONY: prod-vault-bootstrap
prod-vault-bootstrap: ## Premier démarrage de Vault : init + descellement + token dans .env.prod
	./scripts/vault_bootstrap_prod.sh

.PHONY: prod-vault-init
prod-vault-init: ## Initialiser Vault en production (une seule fois)
	$(PROD_COMPOSE) exec -T gateway-vault sh -c 'VAULT_ADDR=http://127.0.0.1:8200 vault operator init -key-shares=5 -key-threshold=3'
	@echo ""
	@echo "  ⚠  Conservez les clés de descellement et le token racine hors ligne."
	@echo "     Sans elles, les secrets sont irrécupérables."

.PHONY: prod-vault-unseal
prod-vault-unseal: ## Desceller Vault (requis après chaque redémarrage)
	@echo "  Saisissez 3 clés de descellement, une par exécution :"
	$(PROD_COMPOSE) exec gateway-vault sh -c 'VAULT_ADDR=http://127.0.0.1:8200 vault operator unseal'

.PHONY: prod-vault-secrets
prod-vault-secrets: ## Enregistrer les secrets applicatifs dans Vault
	./scripts/vault_init_prod.sh

# ──────────────────────────────────────────────────────────────
#  Exploitation
# ──────────────────────────────────────────────────────────────
BACKUP_DIR ?= backups

.PHONY: db-backup
db-backup: ## Sauvegarder la base de production
	@mkdir -p $(BACKUP_DIR)
	$(PROD_COMPOSE) exec -T gateway-postgres pg_dump -U gateway -d gateway_db --format=custom \
		> $(BACKUP_DIR)/gateway_$$(date +%Y%m%d_%H%M%S).dump
	@echo "  ✔ Sauvegarde écrite dans $(BACKUP_DIR)/"
	@ls -lh $(BACKUP_DIR)/ | tail -3

.PHONY: db-restore
db-restore: ## Restaurer une sauvegarde (DUMP=chemin/vers/fichier.dump)
	@test -n "$(DUMP)" || { echo "  Usage: make db-restore DUMP=backups/gateway_....dump"; exit 1; }
	@test -f "$(DUMP)" || { echo "  Fichier introuvable: $(DUMP)"; exit 1; }
	@echo "  ⚠  Cette opération écrase la base de production."
	@read -p "  Confirmer ? [y/N] " c && [ "$$c" = "y" ] || exit 1
	$(PROD_COMPOSE) exec -T gateway-postgres pg_restore -U gateway -d gateway_db --clean --if-exists < $(DUMP)
	@echo "  ✔ Restauration terminée"

.PHONY: reconcile
reconcile: ## Réconcilier la base et la chaîne (production)
	$(PROD_COMPOSE) exec gateway-api python /app/scripts/reconciliation_cron.py

.PHONY: reconcile-dry
reconcile-dry: ## Réconciliation en lecture seule (production)
	$(PROD_COMPOSE) exec gateway-api python /app/scripts/reconciliation_cron.py --dry-run

# La console des liens (/links) n'hérite jamais de la clé injectée par Nginx —
# volontairement, voir nginx.conf. `connect` en donne un accès temporaire sans
# jamais faire circuler GATEWAY_API_KEY : le jeton expire de lui-même et ne
# peut être créé que d'ici, jamais depuis une requête HTTP.
.PHONY: connect
connect: ## Créer une session temporaire pour la console des liens (make connect [TTL=14400], 240min par défaut)
	$(PROD_COMPOSE) exec -T gateway-api python /app/scripts/connect.py \
		--ttl $(or $(TTL),14400) --base-url $(PROD_URL)

.PHONY: security-audit
security-audit: ## Analyse statique de sécurité et audit des dépendances
	@command -v bandit >/dev/null || $(PIP) install bandit
	@command -v pip-audit >/dev/null || $(PIP) install pip-audit
	-bandit -r $(SRC_DIR)/gateway -ll
	-pip-audit -r requirements-prod.txt

# ──────────────────────────────────────────────────────────────
#  Raccourcis
# ──────────────────────────────────────────────────────────────
.PHONY: ci
ci: install check coverage ## Pipeline CI : install → lint → tests → couverture
	@echo ""
	@echo "  ✔ Pipeline CI terminé avec succès"
