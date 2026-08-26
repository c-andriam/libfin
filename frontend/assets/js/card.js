/**
 * Règles de carte, sans DOM et sans réseau.
 *
 * Ces contrôles sont un confort de saisie, pas une garantie : la passerelle
 * revalide tout (`PaymentRequest` dans src/gateway/api.py). Le but est
 * d'éviter un aller-retour réseau — et un débit — pour une faute de frappe.
 */
const Card = (() => {
  'use strict';

  /** Réseaux reconnus, du préfixe le plus spécifique au plus large. */
  const BRANDS = [
    { name: 'amex',       test: /^3[47]/,                     lengths: [15],         cvv: 4, gaps: [4, 10] },
    { name: 'diners',     test: /^3(?:0[0-5]|[68])/,          lengths: [14, 16, 19], cvv: 3, gaps: [4, 10] },
    { name: 'visa',       test: /^4/,                         lengths: [13, 16, 19], cvv: 3, gaps: [4, 8, 12] },
    { name: 'mastercard', test: /^(5[1-5]|2[2-7])/,           lengths: [16],         cvv: 3, gaps: [4, 8, 12] },
    { name: 'discover',   test: /^(6011|64[4-9]|65)/,         lengths: [16, 19],     cvv: 3, gaps: [4, 8, 12] },
    { name: 'jcb',        test: /^35(2[89]|[3-8])/,           lengths: [16, 19],     cvv: 3, gaps: [4, 8, 12] },
    { name: 'unionpay',   test: /^62/,                        lengths: [16, 19],     cvv: 3, gaps: [4, 8, 12] },
  ];

  /** Repli quand aucun préfixe ne correspond : les bornes de l'API. */
  const UNKNOWN = { name: '', lengths: null, cvv: 3, gaps: [4, 8, 12] };

  const digits = (value) => (value || '').replace(/\D+/g, '');

  /** Le réseau déduit du BIN, ou UNKNOWN. */
  function brand(pan) {
    const n = digits(pan);
    return BRANDS.find((b) => b.test.test(n)) || UNKNOWN;
  }

  /** Groupe les chiffres selon le réseau : 4-6-5 pour Amex, 4-4-4-4 sinon. */
  function formatPan(value) {
    const n = digits(value).slice(0, 19);
    const gaps = brand(n).gaps;
    let out = '';
    for (let i = 0; i < n.length; i += 1) {
      if (gaps.includes(i)) out += ' ';
      out += n[i];
    }
    return out;
  }

  /**
   * Somme de Luhn. Attrape la quasi-totalité des chiffres mal recopiés avant
   * qu'ils n'atteignent l'acquéreur.
   */
  function luhn(pan) {
    const n = digits(pan);
    if (n.length < 2) return false;
    let sum = 0;
    let double = false;
    for (let i = n.length - 1; i >= 0; i -= 1) {
      let d = Number(n[i]);
      if (double) {
        d *= 2;
        if (d > 9) d -= 9;
      }
      sum += d;
      double = !double;
    }
    return sum % 10 === 0;
  }

  /** Message d'erreur pour un PAN, ou '' s'il est acceptable. */
  function panError(value) {
    const n = digits(value);
    if (!n) return 'Numéro de carte requis.';
    if (n.length < 13 || n.length > 19) return 'Le numéro doit comporter de 13 à 19 chiffres.';
    const lengths = brand(n).lengths;
    if (lengths && !lengths.includes(n.length)) {
      return `Longueur inattendue pour ce réseau (${lengths.join(' ou ')} chiffres).`;
    }
    if (!luhn(n)) return 'Numéro invalide (contrôle de Luhn).';
    return '';
  }

  /** Insère la barre oblique et corrige un mois saisi en un seul chiffre. */
  function formatExpiry(value) {
    let n = digits(value).slice(0, 4);
    // « 5 » signifie mai : on complète en 05 dès que la suite est tapée.
    if (n.length === 1 && n > '1') n = `0${n}`;
    if (n.length <= 2) return n;
    return `${n.slice(0, 2)}/${n.slice(2)}`;
  }

  /** MM/AA saisi par un humain → YYMM attendu par l'API. */
  function expiryToApi(value) {
    const n = digits(value);
    return n.length === 4 ? n.slice(2) + n.slice(0, 2) : '';
  }

  /**
   * Message d'erreur pour une expiration, ou ''.
   *
   * Une carte reste valable jusqu'à la fin de son mois : la comparaison se
   * fait donc sur le premier jour du mois suivant.
   */
  function expiryError(value, now = new Date()) {
    const n = digits(value);
    if (!n) return "Date d'expiration requise.";
    if (n.length !== 4) return 'Format attendu : MM/AA.';
    const month = Number(n.slice(0, 2));
    const year = 2000 + Number(n.slice(2));
    if (month < 1 || month > 12) return 'Mois invalide.';
    const endOfMonth = new Date(Date.UTC(year, month, 1));
    const today = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
    if (endOfMonth <= today) return 'Carte expirée.';
    return '';
  }

  /** Message d'erreur pour un CVV, ou ''. La longueur dépend du réseau. */
  function cvvError(value, pan) {
    const n = digits(value);
    if (!n) return 'CVV requis.';
    const expected = brand(pan).cvv;
    if (n.length !== expected) return `Le CVV comporte ${expected} chiffres pour ce réseau.`;
    return '';
  }

  /** Message d'erreur pour une adresse de portefeuille, ou ''. */
  function walletError(value) {
    const v = (value || '').trim();
    if (!v) return 'Adresse de portefeuille requise.';
    if (!/^0x[a-fA-F0-9]{40}$/.test(v)) return 'Adresse attendue : 0x suivi de 40 caractères hexadécimaux.';
    return '';
  }

  /** Message d'erreur pour un montant, ou ''. Deux décimales au plus. */
  function amountError(value) {
    const v = (value || '').trim();
    if (!v) return 'Montant requis.';
    if (!/^\d+(\.\d{1,2})?$/.test(v)) return 'Montant attendu : 25 ou 25.00.';
    if (Number(v) <= 0) return 'Le montant doit être strictement positif.';
    return '';
  }

  /** Premiers six et derniers quatre — ce que la norme PCI-DSS tolère. */
  function maskPan(pan) {
    const n = digits(pan);
    if (n.length < 13) return n;
    return `${n.slice(0, 6)}${'*'.repeat(n.length - 10)}${n.slice(-4)}`;
  }

  return {
    digits, brand, formatPan, luhn, maskPan,
    formatExpiry, expiryToApi,
    panError, expiryError, cvvError, walletError, amountError,
  };
})();

// Permet de charger ce module dans Node pour des tests unitaires.
if (typeof module !== 'undefined' && module.exports) module.exports = Card;
