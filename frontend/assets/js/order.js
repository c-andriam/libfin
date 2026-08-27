/**
 * La commande — montant, devise, portefeuille — partagée entre les pages.
 *
 * Deux transports, volontairement redondants :
 *
 * 1. `sessionStorage`, qui garde la commande à travers un rechargement et ne
 *    salit pas l'URL ;
 * 2. les paramètres d'URL, qui fonctionnent quand le stockage est refusé
 *    (navigation privée, réglages restrictifs) et rendent au passage la page de
 *    paiement adressable — un marchand peut produire un lien tout prêt.
 *
 * La page de paiement lit l'un puis l'autre, et **revalide dans tous les cas** :
 * une commande venue de l'URL est une saisie comme une autre.
 *
 * Aucune donnée de carte ne transite ici. Jamais.
 */
const Order = (() => {
  'use strict';

  const KEY = 'libfin.order';
  const CURRENCIES = ['USD', 'EUR', 'GBP'];

  //: ISO 4217 a deux écritures et la passerelle utilise les deux : « USD » dans
  //: la requête, « 840 » dans ce qu'elle stocke et renvoie (DE49 est numérique).
  //: Sans cette table, la page de confirmation affiche « Devise : 840 ».
  const NUMERIC = { 840: 'USD', 978: 'EUR', 826: 'GBP', 392: 'JPY' };

  /** Le code alphabétique, quelle que soit l'écriture reçue. */
  function currencyLabel(value) {
    const raw = String(value === undefined || value === null ? '' : value).trim();
    if (/^\d{3}$/.test(raw)) return NUMERIC[Number(raw)] || raw;
    return raw.toUpperCase();
  }

  /** Enregistre la commande. Renvoie faux si le navigateur refuse le stockage. */
  function save(order) {
    try {
      sessionStorage.setItem(KEY, JSON.stringify(order));
      return true;
    } catch (_) {
      return false;
    }
  }

  function load() {
    try {
      const raw = sessionStorage.getItem(KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (_) {
      return null;
    }
  }

  function clear() {
    try {
      sessionStorage.removeItem(KEY);
    } catch (_) {
      /* Rien à nettoyer si rien n'a pu être écrit. */
    }
  }

  /** Reconstruit une commande depuis les paramètres d'URL, sans rien valider. */
  function fromQuery(search) {
    const p = new URLSearchParams(search || window.location.search);
    if (!p.has('amount') && !p.has('wallet')) return null;
    return {
      amount: (p.get('amount') || '').trim(),
      currency: (p.get('currency') || 'USD').trim().toUpperCase(),
      wallet: (p.get('wallet') || '').trim(),
    };
  }

  /** Les paramètres correspondants, pour construire le lien vers l'étape 2. */
  function toQuery(order) {
    return new URLSearchParams({
      amount: order.amount,
      currency: order.currency,
      wallet: order.wallet,
    }).toString();
  }

  /**
   * Valide une commande, d'où qu'elle vienne.
   *
   * Renvoie `{ errors, warnings }`, chacun indexé par nom de champ. Les bornes
   * `AMOUNT_MIN` / `AMOUNT_MAX` ne sont pas vérifiées ici : la page ne les
   * connaît pas, seule la passerelle les détient. Un montant hors bornes
   * ressort donc en 422 à l'étape 2, avec le détail du serveur.
   */
  function validate(order) {
    const errors = {};
    const warnings = {};
    if (!order) {
      errors.amount = 'Commande introuvable.';
      return { errors, warnings };
    }

    const amountError = Card.amountError(order.amount);
    if (amountError) errors.amount = amountError;

    if (!CURRENCIES.includes(order.currency)) {
      errors.currency = `Devise non prise en charge (${CURRENCIES.join(', ')}).`;
    }

    const address = Address.inspect(order.wallet);
    if (address.error) errors.wallet = address.error;
    else if (address.warning) warnings.wallet = address.warning;

    return { errors, warnings };
  }

  const isValid = (order) => Object.keys(validate(order).errors).length === 0;

  /** Montant tel qu'on l'affiche : « 25.00 USD ». */
  const format = (order) => `${order.amount} ${order.currency}`;

  return {
    save, load, clear, fromQuery, toQuery, validate, isValid, format,
    currencyLabel, CURRENCIES, NUMERIC, KEY,
  };
})();

if (typeof module !== 'undefined' && module.exports) module.exports = Order;
