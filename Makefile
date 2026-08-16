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
#  Infrastructure (Podman & Vault)
# ──────────────────────────────────────────────────────────────
.PHONY: infra-build
infra-build: ## Construire l'image Podman du Gateway
	podman build -t gateway:latest -f Containerfile .

.PHONY: infra-up
infra-up: ## Lancer les conteneurs de production
	podman-compose up -d

.PHONY: infra-dev
infra-dev: ## Lancer les conteneurs (profil dev avec simulateur)
	podman-compose --profile dev up -d

.PHONY: infra-down
infra-down: ## Arrêter et supprimer conteneurs et volumes
	podman-compose down -v

.PHONY: infra-vault
infra-vault: ## Initialiser HashiCorp Vault (doit être exécuté après infra-up)
	./scripts/vault_init.sh

.PHONY: infra-logs
infra-logs: ## Afficher les logs des conteneurs
	podman-compose logs -f

# ──────────────────────────────────────────────────────────────
#  Raccourcis
# ──────────────────────────────────────────────────────────────
.PHONY: all
all: infra-build infra-up infra-vault ## Déploiement complet: build image, lancement, et init vault

.PHONY: ci
ci: install check coverage ## Pipeline CI : install → lint → tests → couverture
	@echo ""
	@echo "  ✔ Pipeline CI terminé avec succès"
