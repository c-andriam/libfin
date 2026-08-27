/**
 * Rendu partagé par les trois pages : tout ce qui touche au DOM, et rien d'autre.
 *
 * Aucune valeur n'est insérée par `innerHTML` — le PAN masqué, un hash de
 * transaction ou un message d'erreur venu du serveur sont du texte, jamais du
 * balisage. Les fonctions tolèrent l'absence de leur cible : chaque page ne
 * porte qu'une partie de ces éléments.
 */
const UI = (() => {
  'use strict';

  const $ = (id) => document.getElementById(id);

  /** Chaque état de `TransactionStatus` traduit, avec sa tonalité visuelle. */
  const STATUS = {
    PENDING:         { tone: 'pending',  label: 'En cours',              message: "Autorisation auprès de l'acquéreur." },
    FIAT_AUTHORIZED: { tone: 'pending',  label: 'Fiat autorisé',         message: 'Empreinte posée sur la carte ; remise crypto en cours.' },
    FIAT_APPROVED:   { tone: 'pending',  label: 'Fiat approuvé',         message: 'Débit effectué ; remise crypto en cours.' },
    FIAT_CAPTURED:   { tone: 'ok',       label: 'Paiement abouti',       message: 'Crypto remise, puis empreinte convertie en débit.' },
    CRYPTO_SENT:     { tone: 'ok',       label: 'Crypto envoyée',        message: 'Le transfert a été diffusé sur la chaîne.' },
    AUTH_VOIDED:     { tone: 'declined', label: 'Autorisation annulée',  message: "L'empreinte a été levée. Rien n'a été débité." },
    FIAT_DECLINED:   { tone: 'declined', label: 'Carte refusée',         message: "L'acquéreur a refusé. Aucun fonds débité." },
    CRYPTO_FAILED:   { tone: 'error',    label: 'Remise crypto échouée', message: 'Le fiat a été rendu ou son empreinte levée.' },
    REVERSED:        { tone: 'declined', label: 'Paiement annulé',       message: 'Le débit fiat a été extourné.' },
    REVERSAL_FAILED: { tone: 'error',    label: 'Extourne refusée',      message: 'Une somme est due au porteur : contactez le support.' },
    FIAT_UNKNOWN:    { tone: 'error',    label: 'État indéterminé',      message: "Réponse manquante de l'acquéreur. Ne pas rejouer le paiement." },
  };

  const describe = (status) => STATUS[status] || { tone: 'pending', label: status || 'Inconnu', message: '' };

  /**
   * Traduit un code HTTP d'échec en tonalité et titre.
   *
   * La distinction qui compte est « l'argent n'a pas bougé » contre « nous ne
   * savons pas ». Un 503 arrive avant tout débit ; un 504 signifie que
   * l'acquéreur n'a jamais répondu et que rejouer le paiement peut débiter
   * deux fois.
   */
  const HTTP_ERRORS = {
    0:   { tone: 'error',    label: 'Passerelle injoignable' },
    400: { tone: 'declined', label: 'Paiement refusé' },
    401: { tone: 'error',    label: 'Authentification refusée' },
    404: { tone: 'error',    label: 'Transaction introuvable' },
    409: { tone: 'error',    label: 'Requête en conflit' },
    422: { tone: 'declined', label: 'Saisie invalide' },
    429: { tone: 'error',    label: 'Trop de tentatives' },
    500: { tone: 'error',    label: 'Erreur de la passerelle' },
    502: { tone: 'error',    label: 'Réseau bancaire indisponible' },
    503: { tone: 'error',    label: 'Service indisponible' },
    504: { tone: 'error',    label: 'Sort du débit inconnu' },
  };

  const describeHttpError = (status) => HTTP_ERRORS[status] || { tone: 'error', label: 'Échec' };

  // ── Champs ────────────────────────────────────────────────────────────────

  /** Affiche ou efface le message d'erreur attaché à un champ. */
  function setFieldError(inputId, errorId, message) {
    const input = $(inputId);
    const holder = $(errorId);
    if (holder) holder.textContent = message || '';
    if (input) {
      if (message) input.setAttribute('aria-invalid', 'true');
      else input.removeAttribute('aria-invalid');
    }
  }

  /** Avertissement non bloquant sous un champ : informe sans interdire. */
  function setFieldWarning(warnId, message) {
    const holder = $(warnId);
    if (!holder) return;
    holder.textContent = message || '';
    holder.hidden = !message;
  }

  /** Bandeau en tête de formulaire, pour ce qui ne vise aucun champ précis. */
  function showBanner(message, tone = 'error') {
    const box = $('form-error');
    if (!box) return;
    box.textContent = message || '';
    box.className = `banner banner--${tone}`;
    box.hidden = !message;
  }

  const hideBanner = () => showBanner('');

  /** Bascule l'état occupé d'un bouton d'envoi. */
  function setBusy(busy, label) {
    const button = $('submit');
    if (!button) return;
    button.disabled = busy;
    button.classList.toggle('is-busy', busy);
    const span = button.querySelector('.btn__label');
    if (span && label) span.textContent = label;
  }

  // ── Panneau d'état (page de confirmation) ─────────────────────────────────

  function showStatus(tone, label, message) {
    const box = $('status');
    if (!box) return;
    box.className = `status status--${tone}`;
    $('status-label').textContent = label;
    $('status-message').textContent = message || '';
  }

  /** Libellés des lignes de détail, dans l'ordre où elles s'affichent. */
  const LABELS = {
    transaction_id: 'Transaction', id: 'Transaction',
    fiat_amount: 'Montant', amount: 'Montant', currency: 'Devise',
    masked_pan: 'Carte', stan: 'STAN', rrn: 'RRN',
    target_wallet: 'Portefeuille',
    tx_hash: 'Hash crypto', crypto_tx_hash: 'Hash crypto',
    created_at: 'Créée le', completed_at: 'Terminée le',
    error: 'Détail',
  };

  const ORDER = [
    'transaction_id', 'id', 'fiat_amount', 'amount', 'currency', 'masked_pan',
    'stan', 'rrn', 'target_wallet', 'tx_hash', 'crypto_tx_hash',
    'created_at', 'completed_at', 'error',
  ];

  //: Champs à présenter comme des dates plutôt que comme de l'ISO 8601 brut.
  const DATE_FIELDS = new Set(['created_at', 'completed_at']);

  /** « 27/08/2026 à 09:41 », dans le fuseau du lecteur. */
  function formatDate(iso) {
    const at = new Date(iso);
    if (Number.isNaN(at.getTime())) return String(iso);
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(at.getDate())}/${pad(at.getMonth() + 1)}/${at.getFullYear()} `
         + `à ${pad(at.getHours())}:${pad(at.getMinutes())}`;
  }

  /** Reconstruit la liste de détails à partir d'un objet de réponse. */
  function showDetails(data) {
    const list = $('details');
    if (!list) return;
    list.textContent = '';
    if (!data) return;

    const seen = new Set();
    ORDER.forEach((key) => {
      const value = data[key];
      if (value === undefined || value === null || value === '') return;
      const label = LABELS[key];
      // `id` et `transaction_id` désignent la même chose selon la réponse.
      if (seen.has(label)) return;
      seen.add(label);

      const row = document.createElement('div');
      row.className = 'details__row';
      const dt = document.createElement('dt');
      dt.textContent = label;
      const dd = document.createElement('dd');
      if (DATE_FIELDS.has(key)) dd.textContent = formatDate(value);
      else if (key === 'currency') dd.textContent = Order.currencyLabel(value);
      else dd.textContent = String(value);
      if (DATE_FIELDS.has(key)) dd.classList.add('details__date');
      row.append(dt, dd);
      list.append(row);
    });
  }

  // ── Passerelle ────────────────────────────────────────────────────────────

  function showHealth(state, text) {
    const pill = $('health');
    if (!pill) return;
    pill.className = `pill pill--${state}`;
    $('health-label').textContent = text;
  }

  /**
   * Bascule la page en mode « relayé ».
   *
   * L'API répond sur notre propre origine : un relais la sert ici même, la clé
   * d'API vit côté serveur et il n'y a plus rien à saisir. Laisser le panneau
   * visible inviterait à recoller une clé dans le navigateur — exactement ce
   * que le relais est là pour éviter.
   */
  function setRelayed() {
    const toggle = $('settings-toggle');
    if (toggle) toggle.hidden = true;
    if ($('settings')) $('settings').hidden = true;
    if ($('api-key')) $('api-key').value = '';
    if ($('base-url')) $('base-url').value = '';
    const pill = $('health');
    if (pill) pill.title = "L'API est relayée par cette origine ; la clé reste côté serveur.";
    if ($('health-label')) $('health-label').textContent += ' · relayée';
  }

  /** Panneau bloquant : la page ne peut pas faire son travail, on dit pourquoi. */
  function block(reason) {
    const box = $('blocked');
    if (!box) return;
    $('blocked-reason').textContent = reason;
    box.hidden = false;
  }

  return {
    $, describe, describeHttpError,
    setFieldError, setFieldWarning, showBanner, hideBanner, setBusy,
    showStatus, showDetails, showHealth, setRelayed, block, formatDate,
    STATUS, HTTP_ERRORS,
  };
})();

if (typeof module !== 'undefined' && module.exports) module.exports = UI;
