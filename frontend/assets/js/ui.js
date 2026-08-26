/**
 * Rendu : tout ce qui touche au DOM, et rien d'autre.
 *
 * Aucune valeur n'est insérée par `innerHTML` — le PAN masqué, un hash de
 * transaction ou un message d'erreur venu du serveur sont du texte, jamais du
 * balisage.
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

  /** Libellés des lignes de détail, dans l'ordre où elles s'affichent. */
  const LABELS = {
    transaction_id: 'Transaction',
    id: 'Transaction',
    fiat_amount: 'Montant',
    amount: 'Montant',
    currency: 'Devise',
    masked_pan: 'Carte',
    stan: 'STAN',
    rrn: 'RRN',
    target_wallet: 'Portefeuille',
    tx_hash: 'Hash crypto',
    crypto_tx_hash: 'Hash crypto',
    created_at: 'Créée le',
    completed_at: 'Terminée le',
    error: 'Détail',
  };

  const ORDER = [
    'transaction_id', 'id', 'fiat_amount', 'amount', 'currency', 'masked_pan',
    'stan', 'rrn', 'target_wallet', 'tx_hash', 'crypto_tx_hash',
    'created_at', 'completed_at', 'error',
  ];

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

  /** Bascule l'état occupé du bouton d'envoi. */
  function setBusy(busy) {
    const button = $('submit');
    if (!button) return;
    button.disabled = busy;
    button.classList.toggle('is-busy', busy);
    button.querySelector('.btn__label').textContent = busy ? 'Traitement…' : 'Payer';
  }

  /** Peint le bandeau d'état du panneau de résultat. */
  function showStatus(tone, label, message) {
    $('result-empty').hidden = true;
    $('result-body').hidden = false;
    const box = $('status');
    box.className = `status status--${tone}`;
    $('status-label').textContent = label;
    $('status-message').textContent = message || '';
  }

  /** Reconstruit la liste de détails à partir d'un objet de réponse. */
  function showDetails(data) {
    const list = $('details');
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
      dd.textContent = String(value);
      row.append(dt, dd);
      list.append(row);
    });
  }

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

  /** Pastille d'état de la passerelle dans la barre supérieure. */
  function showHealth(state, text) {
    const pill = $('health');
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
    $('settings-toggle').hidden = true;
    $('settings').hidden = true;
    $('api-key').value = '';
    $('base-url').value = '';
    const pill = $('health');
    pill.title = "L'API est relayée par cette origine ; la clé reste côté serveur.";
    $('health-label').textContent += ' · relayée';
  }

  return {
    $, describe, describeHttpError, setFieldError,
    setBusy, showStatus, showDetails, showHealth, setRelayed,
    STATUS, HTTP_ERRORS,
  };
})();

if (typeof module !== 'undefined' && module.exports) module.exports = UI;
