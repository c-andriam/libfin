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
	@echo "    API      : https://localhost:8443  (certificat auto-signé → curl -k)"
	@echo "    Clé API  : voir GATEWAY_API_KEY dans .env.sim"
	@echo "    Cartes   : 4111111111111111 approuvée · 4000000000000002 refusée"
	@echo "               4000000000000028 sans réponse · 4000000000000036 reversal refusé"
	@echo ""
	@echo "    Tester   : make sim-pay"
	@echo "    Vérifier : make sim-status"

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
PROD_COMPOSE := podman-compose -f podman-compose.prod.yml --env-file .env.prod

.PHONY: prod-build
prod-build: ## Construire l'image de production
	podman build -t gateway:prod -f Containerfile.prod .

.PHONY: prod-preflight
prod-preflight: ## Vérifier que tout est prêt (bloque si non)
	./scripts/preflight_check.sh

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
	@echo "  ✔ Production lancée."
	@echo "    Si Vault vient de redémarrer : make prod-vault-unseal"
	@echo "    Vérifier l'état             : make prod-status"

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
